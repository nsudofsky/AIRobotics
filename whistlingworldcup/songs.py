"""
Short synthesized jingles played through the *computer's* speakers (not the
LEGO hub, which only supports a handful of canned beep patterns). Used to
announce the match outcome: a success fanfare for whoever wins the point,
a death song for whoever loses it.
"""

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 44100

# Play through the laptop's own speakers, not the AirPods used as the mic --
# the system default output otherwise follows the AirPods once they're
# connected, which is not where you want the songs to come out.
PREFERRED_OUTPUT_NAME_CONTAINS = "macbook"

_output_device_index = "unresolved"  # cached after the first lookup


def _output_device():
    global _output_device_index
    if _output_device_index != "unresolved":
        return _output_device_index

    _output_device_index = None  # fall back to the system default if nothing matches
    try:
        for i, info in enumerate(sd.query_devices()):
            if info["max_output_channels"] > 0 and PREFERRED_OUTPUT_NAME_CONTAINS in info["name"].lower():
                _output_device_index = i
                break
    except Exception as exc:
        print(f"  (could not enumerate output devices, using system default: {exc})")

    return _output_device_index

# (frequency_hz, duration_s) pairs. frequency_hz == 0 is a rest (silence).
SUCCESS_SONG = [(523, 0.12), (659, 0.12), (784, 0.12), (1047, 0.30)]  # C-E-G-C major arpeggio, rising
DEATH_SONG = [(392, 0.25), (0, 0.03), (370, 0.25), (0, 0.03), (349, 0.25), (0, 0.03), (330, 0.7)]  # descending slide


def _tone(freq, duration, volume=0.4, fade_ms=15):
    t = np.linspace(0, duration, int(SAMPLE_RATE * duration), endpoint=False)
    wave = (volume * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    fade_samples = int(SAMPLE_RATE * fade_ms / 1000)
    if 0 < fade_samples * 2 < len(wave):
        fade = np.linspace(0, 1, fade_samples, dtype=np.float32)
        wave[:fade_samples] *= fade
        wave[-fade_samples:] *= fade[::-1]
    return wave


def _play_notes(notes, label):
    chunks = []
    for freq, duration in notes:
        if freq <= 0:
            chunks.append(np.zeros(int(SAMPLE_RATE * duration), dtype=np.float32))
        else:
            chunks.append(_tone(freq, duration))
    audio = np.concatenate(chunks)
    print(label)
    try:
        sd.play(audio, SAMPLE_RATE, device=_output_device())
        sd.wait()
    except Exception as exc:  # no output device, etc. -- don't crash the match over it
        print(f"  (could not play audio: {exc})")


def play_success_song():
    _play_notes(SUCCESS_SONG, "playing success song...")


def play_death_song():
    _play_notes(DEATH_SONG, "playing death song...")
