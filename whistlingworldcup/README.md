# The Whistling World Cup

A LEGO car driven by a tone played from a phone tone-generator app, plus
claps. `whistling.py` grabs a live microphone stream with `pyaudio` and
classifies whatever it hears into one of four frequency zones plus a
clap-count:

| Sound | Command |
|---|---|
| 100–500 Hz tone | **REVERSE**, speed tracks the tone's pitch live |
| 600–1400 Hz tone | **LEFT** |
| 1600–2400 Hz tone | **RIGHT** |
| 2600–5000 Hz tone | **FORWARD**, speed tracks the tone's pitch live |
| two claps | **STOP** |

The tone comes from a phone app (e.g. a "frequency sound generator" app)
rather than whistling by mouth — a phone can hit and hold an exact
frequency far more reliably than a human whistle, which is what makes a
clean four-zone scheme practical here. On match day the car is also
assigned a role — **ball** or **goalie** — and the two cars coordinate the
match over MQTT.

## Files

| File | What it does |
|---|---|
| [`whistling.py`](./whistling.py) | Main script: audio capture, tone-zone/clap policy, live display, LEGO driving, MQTT game logic |
| [`songs.py`](./songs.py) | Synthesizes and plays the success/death jingles through the computer speakers |
| [`lelib.py`](./lelib.py) | Shared LEGO Education wrapper (copied from the class library) |
| [`mqttlib.py`](./mqttlib.py) | Shared `paho-mqtt` wrapper (copied from the class library) |

## Setup

```
pip install --upgrade pip
pip install pyaudio numpy matplotlib paho-mqtt legoeducation
```

`pyaudio` needs the PortAudio C library installed first (`brew install
portaudio` on macOS) — see [Getting pyaudio installed without
Homebrew](#getting-pyaudio-installed-without-homebrew).

Before running on real hardware, fill in the `TODO`s at the top of
`whistling.py`:

- `CARD_SERIAL` / `CARD_COLOR` — your team's LEGO connection card (one
  physical card, used for both the Double Motor and the Color Sensor).
- `RESULT_TOPIC` — agree on a unique MQTT subtopic with the opposing team
  (e.g. `ME193/Rogers/<your-team-name>`) so your game-outcome messages
  don't collide with anyone else on the shared public broker.

Then, on match day:

```
python whistling.py --role ball      # on the ball's laptop
python whistling.py --role goalie    # on the goalie's laptop
```

Both cars connect, calibrate (or load a saved calibration — see below),
and wait for a `"start"` message on `ME193/Rogers` before driving.

### Practicing before match day

You don't need the instructor's `"start"` message (or even a network
connection) to practice. `--practice` skips MQTT entirely and starts
driving live as soon as calibration finishes:

```
python whistling.py --role ball --practice
```

Everything else runs for real — the live plot, the actual tone-zone/clap
policy, and the Color Sensor's catch detection (getting "caught" in
practice mode still stops the car and plays the death song, it just has
no match to report the outcome to). It's the fastest way to sanity-check
your phone's tone generator against the zone boundaries in `whistling.py`.

## Questions

### How does the policy make decisions?

Every audio chunk (2048 samples, ~46 ms) is checked for two independent
things — is there a tonal (narrowband) sound present, and is there a
percussive (broadband) one — and those two can't both be true at once, so
tones and claps are never confused with each other regardless of
threshold tuning.

**A tonal sound's pitch decides FORWARD/REVERSE/LEFT/RIGHT**, via
`zone_for_freq()`:

```
100 Hz  - 500 Hz    -> REVERSE, speed varies with pitch
500 Hz  - 600 Hz    -> dead zone, ignored
600 Hz  - 1400 Hz   -> LEFT
1400 Hz - 1600 Hz   -> dead zone, ignored
1600 Hz - 2400 Hz   -> RIGHT
2400 Hz - 2600 Hz   -> dead zone, ignored
2600 Hz - 5000 Hz   -> FORWARD, speed varies with pitch
```

A raw per-frame zone reading only becomes the *committed* zone once 3
consecutive frames agree on it (`ToneZoneDetector`) — this debounces
brief glitches, and specifically covers sliding the tone generator's
frequency dial from one zone to another: that slide necessarily passes
through a dead zone and possibly another zone along the way, and without
debouncing, each of those would flicker the car's command mid-slide. A
dead-zone reading never itself becomes committed; it just means "no
change yet," so the car keeps doing whatever it was last told while
you're adjusting the dial.

FORWARD and REVERSE aren't just on/off, though — their speed is tracked
*continuously*, every frame, for as long as the raw reading keeps agreeing
with the committed zone. So once you're in the FORWARD zone, nudging the
dial higher within that zone speeds the car up live, not just at the
instant you first entered the zone.

**Claps decide STOP**, via `ClapStopDetector`: two claps in a row fire
STOP immediately on the second one. A single clap not followed by a
second within 0.5s is simply forgotten — there's no other meaning for one
clap. Claps work well for this because a clap is a crisp, easy-to-repeat
transient (unlike trying to count whistle taps, which turned out to be
fragile — see the note in "Known limitations" for that history).

All five outcomes *latch* — the car keeps executing the current one
(issued to the LEGO hub over Bluetooth at most every 100 ms) until a new
one fires. You don't have to keep the tone playing or keep clapping to
keep the car moving or turning.

Catching an opponent (light sensor) and scoring a goal (a dedicated
command, not implemented yet — see the `TODO` on `handle_goal()` in
`whistling.py`) are both handled outside this policy entirely.

### What does your code do if no sound is detected?

It does **nothing** — it keeps executing whichever command last fired
(starting from STOP, the safe default, at boot), except that FORWARD/
REVERSE speed simply stops updating rather than resetting to anything.
This is a deliberate consequence of the latching design: a zone or a
clap-pair is meant to set a new state and then leave it alone, so there's
no single continuous signal to "lose" by going silent — falling back to
STOP on silence would defeat the whole point of a "keep doing this until
told otherwise" control scheme. The car only stops when two claps
actually fire, the catch sensor trips, or the match ends over MQTT.

### How did you try to mask out unwanted noise?

Several layers, stacked:

1. **Band-pass restriction** — only 100–5000 Hz is even considered at
   all, which rules out most very-low rumble and electrical hiss outside
   the four zones' combined range.
2. **Adaptive amplitude gate** — at startup the script records ~1.5 s of
   ambient room noise and sets the noise floor to
   `mean(ambient RMS) + 4 * std(ambient RMS)`. Anything quieter than that
   is ignored outright, and the threshold adapts to however loud the room
   actually is instead of a hardcoded magic number.
3. **Tonal "peakiness" ratio, averaged across sub-windows** — a sustained
   tone is narrowband: nearly all its energy sits in one FFT bin.
   Talking, clapping, and background rumble are broadband, so their
   loudest bin isn't much louder than the rest of the band. We require
   the peak bin's power to be at least 6x the band's median power before
   calling something tonal — this is what actually distinguishes "a tone"
   from "loud noise," on top of the volume gate. **This one had a real
   bug worth calling out**: a single FFT of one audio chunk is a noisy
   estimate — a single periodogram of pure broadband noise has high
   per-bin variance purely by chance, so a *single-frame* peak/median
   ratio crossed our threshold 89% of the time in testing, even for plain
   white noise. Fixed by splitting each chunk into 4 sub-windows and
   averaging their spectra (Welch's method) before computing the ratio —
   that dropped the false-positive rate to ~0% in the same test while a
   real tone still crosses the threshold by a factor of billions, not a
   random few percent. This is also what makes clap detection reliable,
   since it depends on broadband sounds *not* randomly reading as tonal.
4. **Dead zones between every pair of adjacent frequency ranges** — a
   pitch that drifts near a boundary (100Hz of slack on either side)
   can't accidentally flip the car between two different commands; it
   just gets dropped until it's clearly inside one zone or another.
5. **Live calibration, persisted** — because the peakiness/RMS thresholds
   interact with whatever mic and room you're in, the noise floor is
   measured for real rather than trusting a number tuned in a different
   room — but only once, not every run (see below).

The live plot (waveform + tone spectrum with the four zones shaded and
labeled, the detected peak marked, plus a status readout of RMS/peak
frequency/peakiness/zone/speeds/pending claps/command) makes it possible
to see in real time which of these gates is rejecting a given sound,
which is how the thresholds above were tuned.

## Match logic (MQTT)

- `ME193/Rogers` — instructor-published `"start"` trigger. Both roles
  subscribe and hold at STOP until it arrives.
- `RESULT_TOPIC` (`ME193/Rogers/whistling-world-cup` by default — rename
  this per the setup note above) — used for the two outcome messages, each
  published as `"<event>:<role>"`:
  - `"caught:ball"` — the ball's own light sensor saw the goalie get close.
    The ball stops, publishes this, and plays its death song immediately
    (it doesn't wait for its own message to echo back). The goalie, on
    receiving it, plays the success song.
  - `"goal:ball"` — the ball gave the special scoring command (not
    implemented yet). The ball stops, publishes this, and plays its
    success song. The goalie, on receiving it, plays the death song.

  Each side reacts to the *other* role's message (a client ignores a
  message carrying its own role, since the broker echoes every publish
  back to its own subscribers too) — so both songs always come out
  opposite, as required.

## Getting `pyaudio` installed without Homebrew

This machine had no Homebrew and no PortAudio headers, so `pip install
pyaudio` normally fails with `fatal error: 'portaudio.h' file not found`.
It's working now — PortAudio was built from source into a local prefix and
`pyaudio` was linked against that:

```
curl -sLo /tmp/portaudio.tgz http://files.portaudio.com/archives/pa_stable_v190700_20210406.tgz
tar xzf /tmp/portaudio.tgz -C /tmp
cd /tmp/portaudio
./configure --prefix="$HOME/AIRobotics/.portaudio" --disable-shared --enable-static
sed -i '' 's/-Werror//' Makefile   # newer clang trips on old unused-variable warnings treated as errors
make -j4 && make install
cp include/pa_mac_core.h "$HOME/AIRobotics/.portaudio/include/"  # not installed by `make install`, but pyaudio needs it

CFLAGS="-I$HOME/AIRobotics/.portaudio/include" \
LDFLAGS="-L$HOME/AIRobotics/.portaudio/lib -framework CoreAudio -framework AudioToolbox -framework AudioUnit -framework CoreFoundation -framework CoreServices" \
pip install pyaudio
```

If you do have Homebrew, it's much simpler: `brew install portaudio && pip
install pyaudio`.

## Using AirPods (or any Bluetooth mic) as the microphone

`whistling.py`'s device picker lists every input device pyaudio can see
and, when you press Enter, defaults to whichever one has `"airpods"` in
its name (`PREFERRED_MIC_NAME_CONTAINS` at the top of the file) rather
than the OS's own default input, which often stays pinned to the built-in
mic even once a headset is connected. Verified on this machine: once the
AirPods are connected over Bluetooth they show up as a dedicated input
device (`Tasha's AirPods #2`, mono, 24 kHz native — but 44.1 kHz also
opens fine) separate from their stereo output device.

One tradeoff worth knowing: using a Bluetooth headset's mic switches
macOS to the lower-quality HFP audio profile for that device, which would
also degrade anything played back through the *same* AirPods for as long
as the mic is in use. That's why `songs.py` doesn't just use the system
default output (which follows the AirPods once connected) -- it looks for
a device with `"macbook"` in its name (`PREFERRED_OUTPUT_NAME_CONTAINS`)
and plays the success/death songs there instead, so mic input (AirPods)
and song output (MacBook speakers) are deliberately split. Verified on
this machine: input is `Tasha's AirPods #2`, output resolves to `MacBook
Air Speakers`.

## Calibration only runs once

The noise floor is saved to `.calibration.json` (next to `whistling.py`,
git-ignored) the first time it's measured, and loaded from disk on every
run after that — no more "stay quiet for 1.5s" every single time you
start the script. Pass `--recalibrate` if you change rooms/mics and want
to redo it for real.

## Design history (why a phone tone generator, why claps, not snaps)

This control scheme went through a few iterations worth recording, since
each one taught us something about what's actually reliable on real
hardware with a real (imprecise) human:

1. **Continuous whistle pitch → 4 zones** (the very first version).
   Abandoned because whistling a wide, precise pitch range on demand
   turned out to be genuinely hard — the zone boundaries were fine in
   theory but impossible to hit reliably by mouth.
2. **Counting whistle taps** (1 toot = forward, 3 toots = stop). Abandoned
   because timing several short whistle taps into one tight phrase was
   also fragile — real gaps between whistles didn't line up with a fixed
   timing window.
3. **Whistle duration + snap/clap by sound** (hold = go, 2 toots = stop,
   snap = left, clap = right, snap/clap told apart by spectral centroid).
   The snap half of this didn't work: this mic couldn't reliably pick up
   snaps above the noise floor at all, so there was nothing reliable to
   classify. (A separate idea, "route snap and clap through one AirPod
   each," was never viable regardless — a Bluetooth mic is one mono
   stream, not two independently-addressable ears.)
4. **Humming for speed + whistle duration + clap counting** (hum for
   forward, long whistle for reverse, clap count for turning). Clap
   *counting* worked well (claps are crisp and repeatable, unlike whistle
   taps), but distinguishing hum from whistle by pitch range alone, on
   top of everything else, was getting complicated to keep untangled.
5. **Speech recognition for left/right** was considered next, but adding
   a full ASR engine (cloud-dependent, or a large local model) for a
   two-word vocabulary was overkill and added real match-day risk
   (network dependence, or new heavy dependencies).
6. **Current version: a phone tone generator + claps.** Playing the tone
   from a phone app sidesteps the whole "can a human hold a precise pitch"
   problem that doomed version 1 — a phone can hit and hold an exact
   frequency all day — which makes the original clean 4-zone idea
   practical again. Claps stay for STOP specifically because clap
   *counting* was the one piece that already proved reliable in version 4.

## Known limitations / TODO

- **Scoring a goal isn't wired up yet.** `handle_goal()` still exists
  (stops the car, publishes `"goal:<role>"`, plays the success song) but
  nothing calls it yet — every simple pattern is already taken (4 tone
  zones + 2 claps), so it needs something clearly distinct, e.g. three
  claps, or a tone held well above `FORWARD_HIGH_HZ`.
- **Tone-onset transients are an untested edge case.** The very start of
  a tone (a brief attack before it's audible as a clean sustained pitch)
  is momentarily broadband, in principle close to what a clap looks like.
  `PERCUSSIVE_MAX_S` (0.25s) should keep a full tonal burst from ever
  being read as percussive, but a real attack landing in its own frame
  right at the start of a tone hasn't been tested on hardware -- watch
  for a phantom clap-count tick right as a tone starts, and tighten
  `PERCUSSIVE_MAX_S` or raise `PERCUSSIVE_RMS_MARGIN` if it happens.
- The FFT signal-analysis math (including the Welch-averaging fix above),
  `ToneZoneDetector`'s debouncing (verified: 2 agreeing frames isn't
  enough to commit, 3 is; a dead-zone blip doesn't lose the committed
  zone), `ClapStopDetector`'s state machine (2 claps fire STOP, a lone
  clap times out to nothing, a long scrape isn't miscounted), calibration
  persistence (verified a 2nd run makes zero audio reads when a saved
  value exists), the AirPods-preferring device picker, and raw `pyaudio`
  mic capture were all verified directly with synthetic signals and real
  short mic captures. What's still untested is the full hardware loop —
  LEGO BLE connect/drive, an MQTT round-trip between two laptops, and
  actually playing a phone tone at a real microphone — which needs the
  LEGO hub, the phone app, and a second laptop in hand.
- `CATCH_REFLECTION` (the "opponent is right on top of the sensor"
  threshold) and the zone boundaries/speed ranges are starting guesses —
  recalibrate them against your actual color sensor and your phone
  speaker's real output level at whatever distance it'll sit from the
  mic on match day. The live status readout on the plot (zone now vs.
  committed, live forward/reverse speed, pending claps) is there to help
  with exactly that.
