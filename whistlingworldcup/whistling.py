"""
The Whistling World Cup
========================

Sound-controlled LEGO car. A live microphone feed (captured with pyaudio)
drives the car using a tone played from a phone tone-generator app (a
sustained, precisely-controllable pitch -- far more consistent than
whistling by mouth) plus claps:

    100-500 Hz    -> REVERSE, speed tracks the tone's pitch live
    600-1400 Hz   -> LEFT
    1600-2400 Hz  -> RIGHT
    2600-5000 Hz  -> FORWARD, speed tracks the tone's pitch live
    two claps     -> STOP

There's a deliberate dead zone between each pair of adjacent ranges, so a
tone that's ambiguously between two zones (e.g. while you're sliding the
frequency dial from one zone to another) doesn't fire the wrong command.
Every command *latches* -- the car keeps doing whatever was last given
until a new one arrives -- so you don't have to keep the tone playing (or
keep clapping) to keep moving. The ambient noise floor is calibrated once
and saved to disk, so you don't have to redo that "stay quiet" step on
every run (see --recalibrate).

On match day the car is also assigned a role -- "ball" or "goalie" -- and
the two cars coordinate the match over MQTT (start trigger, catch/goal
outcomes, and matching victory/death songs).

Run:
    python whistling.py --role ball
    python whistling.py --role goalie

See README.md for the full write-up (policy description, noise-masking
approach, and what happens with no sound detected) and for the constants
you need to fill in before running on real hardware (LEGO connection
cards, MQTT result topic).
"""

import argparse
import json
import os
import threading
import time
from collections import deque

import numpy as np
import pyaudio
import matplotlib.pyplot as plt
import matplotlib.animation as animation

import legoeducation as le
from lelib import doubleMotor, colorSensor
from mqttlib import MQTTClient
import songs

# --------------------------------------------------------------------------
# Hardware / MQTT configuration -- fill these in for your team's hardware.
# --------------------------------------------------------------------------

# One shared physical connection card, tapped for both hubs -- a card is a
# single printed (serial, color) pair, so the Double Motor and Color Sensor
# use the exact same values here rather than two separately-configured ones.
CARD_SERIAL = 1142
CARD_COLOR = le.LEGO_COLOR_ORANGE

MOTOR_CARD_SERIAL = CARD_SERIAL
MOTOR_CARD_COLOR = CARD_COLOR

SENSOR_CARD_SERIAL = CARD_SERIAL
SENSOR_CARD_COLOR = CARD_COLOR

START_TOPIC = "ME193/Rogers"                          # instructor-assigned start trigger (do not change)
RESULT_TOPIC = "ME193/Rogers/whistling-world-cup"      # TODO: agree on a unique subtopic with your opponent team

# --------------------------------------------------------------------------
# Audio / policy configuration
# --------------------------------------------------------------------------

SAMPLE_RATE = 44100
CHUNK = 2048
PREFERRED_MIC_NAME_CONTAINS = "airpods"  # case-insensitive substring match for the mic picker's default

# Four zones on one frequency axis, with a dead zone between each pair so a
# tone drifting/sliding between two of them doesn't fire the wrong command.
REVERSE_LOW_HZ, REVERSE_HIGH_HZ = 100, 500
LEFT_LOW_HZ, LEFT_HIGH_HZ = 600, 1400
RIGHT_LOW_HZ, RIGHT_HIGH_HZ = 1600, 2400
FORWARD_LOW_HZ, FORWARD_HIGH_HZ = 2600, 5000
TONAL_LOW_HZ = REVERSE_LOW_HZ     # combined range searched for a tonal peak at all
TONAL_HIGH_HZ = FORWARD_HIGH_HZ

# A single periodogram (one FFT of one chunk) of pure broadband noise is
# naturally spiky -- each bin's magnitude has high variance even though no
# bin is really "louder" than the rest on average -- so a single-frame
# peak/median ratio badly over-reports random bins as "tonal". Splitting
# each chunk into WELCH_SEGMENTS sub-windows and averaging their spectra
# (Welch's method) cancels that per-bin noise while a genuinely sustained
# tone stays peaked in every sub-window, so it's still detected with
# enormous margin. Verified: with 1 segment, white noise crosses
# PEAKINESS_MIN 89% of the time; with 4, essentially never.
WELCH_SEGMENTS = 4
SUB_CHUNK = CHUNK // WELCH_SEGMENTS
PEAKINESS_MIN = 6.0        # peak power must be >= this many x the in-band median power to count as tonal
CALIBRATION_S = 1.5        # seconds of ambient noise sampled at startup to set the noise floor
NOISE_FLOOR_K = 4.0        # noise floor = ambient_rms_mean + K * ambient_rms_std

ZONE_VOTE_FRAMES = 3       # consecutive frames that must agree on a new zone before it's committed to

# FORWARD/REVERSE both vary speed continuously with pitch within their zone.
MIN_FORWARD_SPEED, MAX_FORWARD_SPEED = 30, 90
MIN_REVERSE_SPEED, MAX_REVERSE_SPEED = 20, 55
TURN_SPEED = 55             # tank-turn speed %% while the current command is LEFT/RIGHT (fixed, not variable)

# Claps: STOP. A clap is loud and broadband -- the opposite of the tone's
# narrowband pitch -- so it's never confused with a zone no matter how the
# frequencies above are tuned.
PERCUSSIVE_RMS_MARGIN = 1.5   # a clap must be this many x the noise floor (claps are loud)
PERCUSSIVE_MAX_S = 0.25       # a broadband burst longer than this isn't a clap (e.g. a scrape) -- ignored
CLAP_PHRASE_TIMEOUT_S = 0.5   # forget a lone pending clap if a 2nd one doesn't follow within this long

COMMAND_PERIOD_S = 0.1     # minimum time between motor commands sent over Bluetooth

CATCH_REFLECTION = 200     # colorSensor.reflection() at/above this = something is right up on the sensor
CATCH_HOLD_FRAMES = 3      # consecutive high readings required before declaring a catch

# The noise floor calibration ("stay quiet for 1.5s") is saved here after
# the first run and reused on every run after that, so you don't have to
# redo it every time -- pass --recalibrate to force a fresh one.
CALIBRATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".calibration.json")

# Frequencies/window are sized to the Welch sub-window, since that's the
# resolution the averaged spectrum actually comes out at.
FREQS = np.fft.rfftfreq(SUB_CHUNK, d=1.0 / SAMPLE_RATE)
BAND_MASK = (FREQS >= TONAL_LOW_HZ) & (FREQS <= TONAL_HIGH_HZ)
BAND_FREQS = FREQS[BAND_MASK]
SUB_WINDOW = np.hanning(SUB_CHUNK).astype(np.float32)


# --------------------------------------------------------------------------
# Shared state -- audio thread, sensor thread, MQTT thread, and the GUI
# thread (matplotlib) all touch this through the lock.
# --------------------------------------------------------------------------

class SharedState:
    def __init__(self, role):
        self.lock = threading.Lock()
        self.role = role

        self.samples = np.zeros(CHUNK, dtype=np.float32)
        self.spectrum = np.zeros_like(FREQS)
        self.rms = 0.0
        self.noise_floor = 0.0
        self.peak_freq = 0.0
        self.peakiness = 0.0
        self.tonal_detected = False
        self.zone_now = None          # this frame's raw zone reading (informational)
        self.committed_zone = None    # the debounced zone actually driving FORWARD/REVERSE/LEFT/RIGHT
        self.pending_claps = 0        # claps counted so far, waiting to see if a 2nd completes STOP
        self.forward_speed = MIN_FORWARD_SPEED  # last live-tracked FORWARD speed
        self.reverse_speed = MIN_REVERSE_SPEED  # last live-tracked REVERSE speed
        self.command = "STOP"         # the currently-latched drive command

        self.reflection = 0

        self.start_received = False
        self.game_over = False
        self.outcome = None  # "won" / "lost"


# --------------------------------------------------------------------------
# Signal analysis
# --------------------------------------------------------------------------

def zone_for_freq(freq):
    if REVERSE_LOW_HZ <= freq <= REVERSE_HIGH_HZ:
        return "REVERSE"
    if LEFT_LOW_HZ <= freq <= LEFT_HIGH_HZ:
        return "LEFT"
    if RIGHT_LOW_HZ <= freq <= RIGHT_HIGH_HZ:
        return "RIGHT"
    if FORWARD_LOW_HZ <= freq <= FORWARD_HIGH_HZ:
        return "FORWARD"
    return None  # a dead zone between two ranges, or out of range entirely


def forward_speed_for_freq(freq):
    span = max(FORWARD_HIGH_HZ - FORWARD_LOW_HZ, 1)
    frac = min(max((freq - FORWARD_LOW_HZ) / span, 0.0), 1.0)
    return MIN_FORWARD_SPEED + frac * (MAX_FORWARD_SPEED - MIN_FORWARD_SPEED)


def reverse_speed_for_freq(freq):
    span = max(REVERSE_HIGH_HZ - REVERSE_LOW_HZ, 1)
    frac = min(max((freq - REVERSE_LOW_HZ) / span, 0.0), 1.0)
    return MIN_REVERSE_SPEED + frac * (MAX_REVERSE_SPEED - MIN_REVERSE_SPEED)


def _analyze_frame(samples, noise_floor):
    """Pure signal analysis for one audio chunk, no shared-state side effects.
    Returns (rms, spectrum, peak_freq, peakiness, tonal).

    The magnitude spectrum is Welch-averaged across WELCH_SEGMENTS
    sub-windows rather than a single FFT of the whole chunk (see the
    WELCH_SEGMENTS comment above) -- this is what keeps loud broadband
    noise (talking, claps) from randomly reading as "tonal"."""
    rms = float(np.sqrt(np.mean(samples ** 2)))
    sub_mags = [np.abs(np.fft.rfft(samples[i * SUB_CHUNK:(i + 1) * SUB_CHUNK] * SUB_WINDOW))
                for i in range(WELCH_SEGMENTS)]
    spectrum = np.mean(sub_mags, axis=0)
    band_mag = spectrum[BAND_MASK]

    if band_mag.size == 0 or rms < noise_floor:
        return rms, spectrum, 0.0, 0.0, False

    # A sustained tone is narrowband: nearly all its energy sits in one FFT
    # bin. Background noise (talking, claps) is broadband, so its peak bin
    # isn't much louder than the rest of the band. That ratio -- not just
    # loudness -- is what tells a tone from noise.
    peak_idx = int(np.argmax(band_mag))
    peak_freq = float(BAND_FREQS[peak_idx])
    peak_power = float(band_mag[peak_idx]) ** 2
    median_power = float(np.median(band_mag)) ** 2 + 1e-12
    peakiness = peak_power / median_power
    tonal = peakiness >= PEAKINESS_MIN

    return rms, spectrum, peak_freq, peakiness, tonal


def classify_frame(state, samples):
    """Update state with this frame's signal, and return (peak_freq, tonal, rms, spectrum)."""
    with state.lock:
        noise_floor = state.noise_floor

    rms, spectrum, peak_freq, peakiness, tonal = _analyze_frame(samples, noise_floor)

    with state.lock:
        state.samples = samples
        state.spectrum = spectrum
        state.rms = rms
        state.peak_freq = peak_freq
        state.peakiness = peakiness
        state.tonal_detected = tonal

    return peak_freq, tonal, rms, spectrum


def is_percussive(rms, tonal, noise_floor):
    """Loud + broadband (not tonal) -- a candidate clap transient."""
    return rms >= noise_floor * PERCUSSIVE_RMS_MARGIN and not tonal


# --------------------------------------------------------------------------
# Command detectors
# --------------------------------------------------------------------------

class ToneZoneDetector:
    """Debounces the raw per-frame zone classification: a new zone only
    becomes *committed* once ZONE_VOTE_FRAMES consecutive frames agree on
    it, so briefly passing through an unintended zone (e.g. sliding the
    tone generator's frequency dial from REVERSE to FORWARD crosses the
    dead zones and LEFT/RIGHT along the way) doesn't flicker the wrong
    command. A dead-zone reading (None) never itself becomes committed --
    it just means "no change yet," preserving whatever was last committed.
    """

    def __init__(self):
        self.recent = deque(maxlen=ZONE_VOTE_FRAMES)
        self.committed_zone = None

    def update(self, zone_now):
        """Returns the newly-committed zone if it just changed, else None."""
        self.recent.append(zone_now)
        if len(self.recent) == ZONE_VOTE_FRAMES and len(set(self.recent)) == 1:
            candidate = self.recent[-1]
            if candidate is not None and candidate != self.committed_zone:
                self.committed_zone = candidate
                return candidate
        return None


class ClapStopDetector:
    """Two claps in a row fire STOP. A lone clap not followed by a second
    one within CLAP_PHRASE_TIMEOUT_S is simply forgotten. update(now,
    percussive) is called once per audio frame and returns "STOP" or None.
    """

    def __init__(self):
        self.burst_active = False
        self.burst_start = None
        self.clap_count = 0
        self.first_clap_time = None

    def update(self, now, percussive):
        command = None

        if percussive:
            if not self.burst_active:
                self.burst_active = True
                self.burst_start = now
        elif self.burst_active:
            self.burst_active = False
            duration = now - self.burst_start
            if duration <= PERCUSSIVE_MAX_S:
                if self.clap_count == 0:
                    self.first_clap_time = now
                self.clap_count += 1
                if self.clap_count >= 2:
                    command = "STOP"
                    self.clap_count = 0
                    self.first_clap_time = None
            else:
                # Too long to be a clap (e.g. a scrape) -- don't count it.
                self.clap_count = 0
                self.first_clap_time = None

        if self.clap_count > 0 and self.first_clap_time is not None:
            if now - self.first_clap_time > CLAP_PHRASE_TIMEOUT_S:
                self.clap_count = 0
                self.first_clap_time = None

        return command


def apply_command(dm, command, forward_speed, reverse_speed):
    if command == "STOP":
        dm.movement_move_tank(0, 0)
    elif command == "FORWARD":
        dm.movement_move_tank(forward_speed, forward_speed)
    elif command == "REVERSE":
        dm.movement_move_tank(-reverse_speed, -reverse_speed)
    elif command == "LEFT":
        dm.movement_move_tank(-TURN_SPEED, TURN_SPEED)
    elif command == "RIGHT":
        dm.movement_move_tank(TURN_SPEED, -TURN_SPEED)


# --------------------------------------------------------------------------
# Game logic (MQTT)
# --------------------------------------------------------------------------

def stop_robot(dm):
    try:
        dm.movement_move_tank(0, 0)
    except Exception:
        pass


def handle_caught(state, dm, mqtt_client):
    """Our own light sensor sees the opponent right on top of us."""
    with state.lock:
        if state.game_over:
            return
        state.game_over = True
        state.outcome = "lost"
    stop_robot(dm)
    if mqtt_client is not None:
        mqtt_client.publish(RESULT_TOPIC, f"caught:{state.role}")
    songs.play_death_song()


def handle_goal(state, dm, mqtt_client):
    """We (the ball) gave the special scoring command.

    TODO: nothing calls this yet. Every simple pattern is already taken --
    FORWARD/REVERSE/LEFT/RIGHT (tone zones) and STOP (2 claps) -- so
    scoring needs something clearly distinct from all of them (e.g. three
    claps, or a tone held well above FORWARD_HIGH_HZ), picked and wired up
    here the way catch-detection is wired to handle_caught().
    """
    with state.lock:
        if state.game_over:
            return
        state.game_over = True
        state.outcome = "won"
    stop_robot(dm)
    if mqtt_client is not None:
        mqtt_client.publish(RESULT_TOPIC, f"goal:{state.role}")
    songs.play_success_song()


def on_start_message(state, topic, payload):
    if payload.strip().lower() == "start":
        with state.lock:
            state.start_received = True
        print(f"[{topic}] start received -- driving is live")


def on_result_message(state, dm, _topic, payload):
    with state.lock:
        if state.game_over:
            return
    try:
        event, other_role = payload.split(":", 1)
    except ValueError:
        return
    if other_role == state.role:
        return  # this is the broker echoing our own publish back to us -- already handled locally

    with state.lock:
        state.game_over = True
        state.outcome = "won" if event == "caught" else "lost"
    stop_robot(dm)
    if event == "caught":
        songs.play_success_song()  # the other car got caught -- we win the point
    elif event == "goal":
        songs.play_death_song()    # the other car scored -- we lose the point


# --------------------------------------------------------------------------
# Calibration (persisted to disk so it doesn't repeat every run)
# --------------------------------------------------------------------------

def _load_saved_noise_floor():
    try:
        with open(CALIBRATION_FILE) as f:
            return json.load(f).get("noise_floor")
    except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError):
        return None


def _save_noise_floor(noise_floor):
    try:
        with open(CALIBRATION_FILE, "w") as f:
            json.dump({"noise_floor": noise_floor}, f)
    except OSError as exc:
        print(f"  (could not save calibration for next time: {exc})")


def calibrate_noise_floor(state, stream, force=False):
    if not force:
        saved = _load_saved_noise_floor()
        if saved is not None:
            with state.lock:
                state.noise_floor = saved
            print(f"Using saved noise floor {saved:.5f} from a previous run (pass --recalibrate to redo it).")
            return

    print(f"Calibrating ambient noise floor ({CALIBRATION_S:.1f}s) -- stay quiet...")
    n_frames = max(1, int(CALIBRATION_S * SAMPLE_RATE / CHUNK))
    rms_samples = []
    for _ in range(n_frames):
        raw = stream.read(CHUNK, exception_on_overflow=False)
        samples = np.frombuffer(raw, dtype=np.float32)
        rms_samples.append(float(np.sqrt(np.mean(samples ** 2))))
    mean, std = float(np.mean(rms_samples)), float(np.std(rms_samples))
    floor = mean + NOISE_FLOOR_K * std
    with state.lock:
        state.noise_floor = floor
    _save_noise_floor(floor)
    print(f"Noise floor set to {floor:.5f} (ambient mean={mean:.5f}, std={std:.5f}) -- saved for next time.")


# --------------------------------------------------------------------------
# Worker threads
# --------------------------------------------------------------------------

def audio_worker(state, dm, _mqtt_client, pa, device_index, stop_event, force_recalibrate):
    # _mqtt_client: unused for now -- reserved for the goal-command TODO in
    # handle_goal() once that pattern is wired up here.
    stream = pa.open(format=pyaudio.paFloat32, channels=1, rate=SAMPLE_RATE,
                      input=True, input_device_index=device_index,
                      frames_per_buffer=CHUNK)
    try:
        calibrate_noise_floor(state, stream, force=force_recalibrate)
        with state.lock:
            noise_floor = state.noise_floor

        zone_detector = ToneZoneDetector()
        clap_detector = ClapStopDetector()
        current_command = "STOP"  # safe default until the first recognized command
        current_forward_speed = MIN_FORWARD_SPEED
        current_reverse_speed = MIN_REVERSE_SPEED
        last_command_time = 0.0

        while not stop_event.is_set():
            raw = stream.read(CHUNK, exception_on_overflow=False)
            samples = np.frombuffer(raw, dtype=np.float32)
            peak_freq, tonal, rms, _spectrum = classify_frame(state, samples)
            now = time.monotonic()

            zone_now = zone_for_freq(peak_freq) if tonal else None
            newly_committed = zone_detector.update(zone_now)
            if newly_committed is not None:
                current_command = newly_committed
                print(f"tone zone command: {current_command}")

            # Keep tracking speed live while actively sustaining a
            # variable-speed zone, not just at the instant it committed.
            if zone_detector.committed_zone == "FORWARD" and zone_now == "FORWARD":
                current_forward_speed = forward_speed_for_freq(peak_freq)
            elif zone_detector.committed_zone == "REVERSE" and zone_now == "REVERSE":
                current_reverse_speed = reverse_speed_for_freq(peak_freq)

            percussive = is_percussive(rms, tonal, noise_floor)
            clap_command = clap_detector.update(now, percussive)
            if clap_command is not None:
                current_command = clap_command
                print(f"clap command: {current_command}")

            with state.lock:
                state.zone_now = zone_now
                state.committed_zone = zone_detector.committed_zone
                state.pending_claps = clap_detector.clap_count
                state.forward_speed = current_forward_speed
                state.reverse_speed = current_reverse_speed
                state.command = current_command
                start_received = state.start_received
                game_over = state.game_over

            if start_received and not game_over and now - last_command_time >= COMMAND_PERIOD_S:
                apply_command(dm, current_command, current_forward_speed, current_reverse_speed)
                last_command_time = now
    finally:
        stream.stop_stream()
        stream.close()


def sensor_worker(state, cs, dm, mqtt_client, stop_event):
    consecutive = 0
    while not stop_event.is_set():
        reflection = cs.reflection()
        with state.lock:
            state.reflection = reflection
            game_over = state.game_over

        consecutive = consecutive + 1 if reflection >= CATCH_REFLECTION else 0
        if consecutive >= CATCH_HOLD_FRAMES and not game_over:
            handle_caught(state, dm, mqtt_client)

        time.sleep(0.1)


# --------------------------------------------------------------------------
# Live display -- shows the signal (waveform + spectrum) and the decision
# it produced, updated in real time.
# --------------------------------------------------------------------------

ZONE_SPANS = [
    (REVERSE_LOW_HZ, REVERSE_HIGH_HZ, "REVERSE", "tab:red"),
    (LEFT_LOW_HZ, LEFT_HIGH_HZ, "LEFT", "tab:blue"),
    (RIGHT_LOW_HZ, RIGHT_HIGH_HZ, "RIGHT", "tab:orange"),
    (FORWARD_LOW_HZ, FORWARD_HIGH_HZ, "FORWARD", "tab:green"),
]


def run_display(state):
    fig, (ax_wave, ax_spec) = plt.subplots(2, 1, figsize=(9, 7))
    fig.canvas.manager.set_window_title("Whistling World Cup -- live signal & decision")

    wave_line, = ax_wave.plot(np.arange(CHUNK), np.zeros(CHUNK))
    ax_wave.set_ylim(-1, 1)
    ax_wave.set_xlim(0, CHUNK)
    ax_wave.set_title("Microphone waveform")
    ax_wave.set_xlabel("sample")
    ax_wave.set_ylabel("amplitude")

    spec_line, = ax_spec.plot(BAND_FREQS, np.zeros_like(BAND_FREQS))
    peak_marker, = ax_spec.plot([], [], "ro", markersize=8)
    ax_spec.set_xlim(TONAL_LOW_HZ, TONAL_HIGH_HZ)
    ax_spec.set_title("Tone spectrum & zones")
    ax_spec.set_xlabel("Hz")
    ax_spec.set_ylabel("magnitude")

    for lo, hi, label, color in ZONE_SPANS:
        ax_spec.axvspan(lo, hi, color=color, alpha=0.12)
        ax_spec.text((lo + hi) / 2, 0.02, label, ha="center", va="bottom", fontsize=8,
                     transform=ax_spec.get_xaxis_transform())

    status_text = ax_spec.text(0.98, 0.95, "", transform=ax_spec.transAxes,
                                va="top", ha="right", fontsize=10, family="monospace",
                                bbox=dict(boxstyle="round", fc="white", alpha=0.85))

    def update(_frame):
        with state.lock:
            samples = state.samples
            band_mag = state.spectrum[BAND_MASK]
            rms = state.rms
            noise_floor = state.noise_floor
            peak_freq = state.peak_freq
            peakiness = state.peakiness
            tonal = state.tonal_detected
            zone_now = state.zone_now
            committed_zone = state.committed_zone
            pending_claps = state.pending_claps
            forward_speed = state.forward_speed
            reverse_speed = state.reverse_speed
            command = state.command
            reflection = state.reflection
            role = state.role
            start_received = state.start_received
            game_over = state.game_over
            outcome = state.outcome

        wave_line.set_ydata(samples)

        spec_line.set_data(BAND_FREQS, band_mag)
        ax_spec.set_ylim(0, max(float(band_mag.max()) if band_mag.size else 1.0, 1.0) * 1.2)
        if tonal:
            peak_marker.set_data([peak_freq], [float(band_mag.max()) if band_mag.size else 0])
        else:
            peak_marker.set_data([], [])

        status = (
            f"role: {role:<7} match: {'LIVE' if start_received else 'waiting for start'}\n"
            f"rms:  {rms:.4f}  (floor {noise_floor:.4f})\n"
            f"peak: {peak_freq:7.1f} Hz   peakiness: {peakiness:6.1f}x\n"
            f"tonal detected: {'YES' if tonal else 'no'}   zone now: {zone_now or '-'}   committed: {committed_zone or '-'}\n"
            f"forward speed: {forward_speed:.0f}%   reverse speed: {reverse_speed:.0f}%\n"
            f"claps pending: {pending_claps}  (2 claps = STOP)\n"
            f"command: {command}\n"
            f"light sensor: {reflection}"
        )
        if game_over:
            status += f"\n\n*** GAME OVER: {outcome.upper()} ***"
        status_text.set_text(status)

        return wave_line, spec_line, peak_marker, status_text

    ani = animation.FuncAnimation(fig, update, interval=70, blit=False, cache_frame_data=False)
    plt.tight_layout()
    plt.show(block=True)  # must block -- interactive mode would make this return instantly and tear everything down
    return ani


# --------------------------------------------------------------------------
# Mic selection (pyaudio)
# --------------------------------------------------------------------------

def pick_input_device(pa):
    try:
        system_default_index = pa.get_default_input_device_info().get("index")
    except IOError:
        system_default_index = None

    print("Available microphone inputs:")
    inputs = []
    preferred_index = None
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0:
            inputs.append(i)
            name = info["name"]
            is_preferred = preferred_index is None and PREFERRED_MIC_NAME_CONTAINS in name.lower()
            if is_preferred:
                preferred_index = i
            tag = " <- preferred" if is_preferred else (" <- system default" if i == system_default_index else "")
            print(f"  [{i}] {name}{tag}")

    if not inputs:
        print("No input devices found; using system default.")
        return None

    # A named preference (e.g. AirPods) beats the OS's own default input
    # device, since macOS often still points that at the built-in mic even
    # once a Bluetooth headset is connected.
    default_index = preferred_index if preferred_index is not None else system_default_index

    choice = input("Enter device number (or press Enter for default): ").strip()
    if not choice:
        return default_index
    try:
        idx = int(choice)
        if idx in inputs:
            return idx
    except ValueError:
        pass
    print("Invalid selection; using default.")
    return default_index


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sound-controlled LEGO car for the Whistling World Cup.")
    parser.add_argument("--role", choices=["ball", "goalie"], required=True)
    parser.add_argument("--device", type=int, default=None,
                         help="pyaudio input device index (skips the interactive picker)")
    parser.add_argument("--practice", action="store_true",
                         help="skip MQTT and the 'start' wait entirely -- drive as soon as you make a sound, "
                              "for practicing before match day")
    parser.add_argument("--recalibrate", action="store_true",
                         help="redo the ambient noise-floor calibration instead of reusing the saved one")
    args = parser.parse_args()

    state = SharedState(role=args.role)
    print(f"=== Whistling World Cup -- role: {args.role}{' (PRACTICE MODE)' if args.practice else ''} ===")

    print("Connecting to LEGO Double Motor...")
    dm = doubleMotor()
    dm.connect(card_serial=MOTOR_CARD_SERIAL, card_color=MOTOR_CARD_COLOR)
    print("Connected to motor hub.")

    print("Connecting to LEGO Color Sensor (front light sensor)...")
    cs = colorSensor()
    cs.connect(card_serial=SENSOR_CARD_SERIAL, card_color=SENSOR_CARD_COLOR)
    print("Connected to color sensor.")

    if args.practice:
        # No broker, no waiting for the instructor's "start" -- drive as soon
        # as calibration finishes. Catch/goal detection still run against the
        # real sensor/mic, but with nowhere to publish to, they just stop the
        # car and play the song locally instead of ending a real match.
        mqtt_client = None
        state.start_received = True
        print("Practice mode: skipping MQTT -- driving is live as soon as calibration finishes.")
    else:
        mqtt_client = MQTTClient()
        mqtt_client.connect()
        mqtt_client.subscribe(START_TOPIC, lambda topic, payload: on_start_message(state, topic, payload))
        mqtt_client.subscribe(RESULT_TOPIC, lambda topic, payload: on_result_message(state, dm, topic, payload))
        print(f"MQTT connected. Waiting for 'start' on {START_TOPIC} ...")

    pa = pyaudio.PyAudio()
    device_index = args.device if args.device is not None else pick_input_device(pa)

    stop_event = threading.Event()
    audio_thread = threading.Thread(target=audio_worker,
                                     args=(state, dm, mqtt_client, pa, device_index, stop_event, args.recalibrate),
                                     daemon=True)
    sensor_thread = threading.Thread(target=sensor_worker,
                                      args=(state, cs, dm, mqtt_client, stop_event),
                                      daemon=True)
    audio_thread.start()
    sensor_thread.start()

    try:
        run_display(state)
    finally:
        stop_event.set()
        stop_robot(dm)
        if mqtt_client is not None:
            mqtt_client.disconnect()
        pa.terminate()
        audio_thread.join(timeout=2)
        sensor_thread.join(timeout=2)


if __name__ == "__main__":
    main()
