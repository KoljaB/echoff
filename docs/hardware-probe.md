# Hardware probe

[Documentation home](README.md)

Unit tests prove ordering and state transitions. They cannot prove that a real
speaker, room, endpoint, and microphone produce useful echo suppression. The
probe opens the production capture path and preserves enough evidence to tell
an AEC failure from bad routing or insufficient acoustic exposure.

## Fast ambient check

```powershell
python -m echoff record --duration 20 --log-level INFO
```

Play ordinary speech through the selected speakers. Speak during a separate
part of the recording if you also want a qualitative near-end check. Confirm:

1. `summary.json` reports `status: completed` and the intended devices.
2. `computer_audio.wav` contains the played audio.
3. `microphone_raw.wav` contains audible speaker leakage; without exposure, AEC
   quality is inconclusive.
4. `microphone_aec.wav` reduces that leakage without destroying your speech.

Do not describe the whole-run level change as echo suppression if the run
contains real microphone speech.

## Repeatable far-end-only stimulus

Install `ffplay`, choose a speech-shaped PCM WAV, keep the room quiet, and run:

```powershell
python -m echoff record `
  --play-wav C:\audio\known-speech.wav `
  --repetitions 3 `
  --pre-roll 2 `
  --gap 1 `
  --tail 1 `
  --output captures\probe-001
```

The command records each `ffplay` process lifetime and, for playback windows
long enough after edge trimming, writes an automatic far-end-only section to
`analysis.json`. The edge-trim value and window kind are also stored in
`summary.json`. These windows are reproducible but are not sample-accurate DAC
timing.

## Read the result

| Field | Direction | Interpretation |
|---|---|---|
| `far_end_only.echo_suppression_db` | higher positive is more attenuation | Pooled raw-mic level divided by clean-mic level in declared silent windows |
| per-window `echo_suppression_db` | consistent is preferable | Exposes startup, drift, or content-specific weak periods hidden by a pooled value |
| `near_end.near_end_retained_db` | closer to 0 dB is more retained level | Only meaningful in declared near-end windows; level is not intelligibility |
| `loopback_to_raw_energy_alignment.lag_ms` | diagnostic, not a target | Broad 10 ms energy-envelope lag estimate |
| `normalized_correlation` | closer to `+1` means a clearer positive envelope match | Weak/ambiguous acoustic exposure can make lag unstable |
| `summary.capture.runtime_realignments` | usually zero in stable hardware | Nonzero means a timestamp discontinuity was observed and APM was reset |

Echoff does not publish a universal pass threshold: rooms, speaker placement,
microphone directionality, endpoint processing, and volume change the physical
problem. Establish a fixed local baseline, inspect every window, and compare
changes under the same setup.

## Near-end preservation check

For a level-only check, run a separate ambient recording and speak during a
predeclared interval while the far end is silent. After recording, analyze that
interval:

```powershell
python -m echoff analyze captures\near-end-check `
  --near-end-window 6.0:8.0
```

Record the interval before listening. Inspect both level retention and the WAV;
a number near 0 dB does not prove that speech is undistorted.

For the more important double-talk case—speaking while far-end audio plays—the
raw mic contains both voice and echo, while the clean mic should contain mostly
voice. Their RMS ratio cannot isolate speech retention: a lower clean level may
mean successful echo removal rather than damaged speech. Listen to the fixed
interval or use a separate speech-quality method; do not interpret proximity to
0 dB as retention during double-talk.

## Compare a configuration change

Before the first run, freeze:

- output endpoint and microphone;
- Windows volume, speaker position, and room geometry;
- stimulus file and its hash;
- repetitions and timing;
- stream-delay/noise-suppression settings; and
- which metrics decide the comparison.

Use a fresh output directory for every run. Do not select only the best retry or
tune thresholds after seeing output. If you test `stream_delay_ms`, predeclare a
small grid, change no other input, and verify the chosen value again with
near-end speech. Timestamp alignment and the WebRTC delay hint solve different
problems; do not use delay tuning to hide capture discontinuities.

## Privacy

The artifact directory contains raw microphone and system audio. The default
`captures/` tree and general WAV/JSONL/log patterns are ignored by this
repository; only the two reviewed demo WAVs under `assets/` are explicitly
exempt. Custom locations and JSON metadata are not guaranteed to be ignored.
Nothing is encrypted. Check `git status` and review or redact artifacts before sharing.

Next: [Capture artifacts](capture-artifacts.md) ·
[Troubleshooting](troubleshooting.md) · [Architecture](architecture.md)
