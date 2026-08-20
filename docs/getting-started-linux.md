# Getting started on Linux

Echoff captures speaker output and a microphone through PipeWire, then writes
the reference, raw microphone, and cleaned microphone as separate tracks.

## Requirements

- Linux with a running PipeWire audio session
- Python 3.11 or newer
- `pactl`, `pw-dump`, and `pw-record`
- `ffplay` only for the repeatable audible playback probe

On Ubuntu, install the command-line tools with:

```bash
sudo apt update
sudo apt install pipewire-bin pulseaudio-utils ffmpeg
```

Create an isolated environment and install Echoff:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install echoff
```

## Select devices

List the PipeWire endpoints Echoff can select:

```bash
.venv/bin/echoff devices
```

Use the exact selector shown for the stereo output's monitor as
`--reference-device` and the microphone source as `--microphone-device`.
Explicit selectors make a probe repeatable even if the desktop default later
changes. `AecConfig(backend="auto")` selects the Linux PipeWire backend; the
`record` command has no `--backend` option.

## Record an inspectable session

```bash
.venv/bin/echoff record \
  --reference-device '<sink-monitor-selector>' \
  --microphone-device '<microphone-selector>' \
  --duration 15 \
  --output captures/linux-first
```

While it records, play speech through the selected stereo output and speak near
the microphone. The command preserves independent `computer_audio.wav`,
`microphone_raw.wav`, and `microphone_aec.wav` tracks plus the received-source
tracks `reference_received.wav` and `microphone_received.wav`, events, metrics,
and a summary. It never mixes system audio into the microphone before AEC.

## Repeatable audible probe

For a PipeWire-compatible sink name from `pactl list short sinks`, run:

```bash
PULSE_SINK='<sink-name>' .venv/bin/echoff record \
  --reference-device '<sink-monitor-selector>' \
  --microphone-device '<microphone-selector>' \
  --play-wav /usr/share/sounds/alsa/Front_Center.wav \
  --repetitions 8 \
  --gap 1 \
  --pre-roll 2 \
  --tail 3 \
  --output captures/linux-audible-probe
```

Confirm that playback came from the intended stereo, then inspect
`summary.json`. A healthy run has distinct source tracks, locked reference
alignment, zero dropped or degraded blocks, zero synchronization timeouts, and
no unexpected APM resets. Echo suppression depends on the room, device gain,
speaker volume, and amount of far-end audio, so preserve the entire artifact
directory when comparing runs.

See [Platform support](platforms.md), [Capture artifacts](capture-artifacts.md),
and [Troubleshooting](troubleshooting.md) for the exact backend boundary and
diagnostics.
