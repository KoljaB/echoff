# Command-line interface

[Documentation home](README.md)

The commands below assume the intended virtual environment is activated.
`python -m echoff` binds Echoff to the selected `python` interpreter; the
installed `echoff` executable is equivalent when its Scripts directory is on
PATH.

## `devices`

List selectable devices for the current platform:

```powershell
python -m echoff devices
python -m echoff devices --json
```

Defaults are marked in text output. JSON emits `DeviceInfo.to_dict()` rows.
On Windows, the list contains WASAPI loopback references and WASAPI microphones;
the optional WDM-KS microphone fallback is selected only after a unique
normalized physical-name match and successful start, and is hidden from this
list. On Linux, the list contains PipeWire sink monitors and ordinary sources.
`AecConfig(backend="auto")` chooses the platform backend; `record` has no
`--backend` flag.

## `record`

Record one inspectable hardware session:

```powershell
python -m echoff record --duration 20 --output captures\manual
```

| Option | Default | Meaning |
|---|---:|---|
| `--duration SECONDS` | `15` | Positive ambient duration; with `--play-wav` it remains validated but stimulus timing controls runtime |
| `--output PATH` | timestamped `captures\` directory | New or empty artifact directory |
| `--reference-device SELECTOR` | default output loopback | Numeric index or unique case-insensitive name fragment |
| `--microphone-device SELECTOR` | default microphone | Numeric index or unique case-insensitive name fragment |
| `--stream-delay-ms MS` | `50` | WebRTC render-to-capture delay hint |
| `--noise-suppression` | off | Enable WebRTC noise suppression in addition to AEC |
| `--play-wav PATH` | none | Play a repeatable far-end stimulus through `ffplay` |
| `--repetitions N` | `1` | Stimulus playbacks |
| `--pre-roll SECONDS` | `2` | Quiet capture before the first stimulus |
| `--gap SECONDS` | `1` | Time between repetitions |
| `--tail SECONDS` | `1` | Capture after the final stimulus |
| `--volume 0..100` | `100` | `ffplay` volume |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

With `--play-wav`, runtime is pre-roll + WAV playback(s) + gaps + tail. Echoff
records `ffplay` process lifetime and trims 250 ms from each edge for the
automatic far-end-only analysis. These are reproducible process-timing windows,
not sample-accurate sound-card boundaries.

An existing non-empty output directory is rejected. Echoff never overwrites a
prior capture. If the finalized summary is `incomplete`, `degraded`, or
`failed`, the command reports failure while retaining the artifact directory.

## `analyze`

Print a fresh read-only analysis of the three WAV tracks:

```powershell
python -m echoff analyze captures\manual `
  --far-end-window 2.5:8.0 `
  --near-end-window 10.0:12.0
```

`--far-end-window START:END` and `--near-end-window START:END` are repeatable;
times are seconds from sample zero in the shared artifact timeline. The command
prints JSON and does not replace the capture's existing `analysis.json`.

Label a window far-end-only only when no real microphone speech or unrelated
near-end sound occurs there. The whole-run raw/clean level difference is not an
echo-suppression metric.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | completed successfully |
| `1` | other runtime failure such as a missing stimulus file or `ffplay` failure |
| `2` | command-line validation error |
| `5` | package-raised `AecCaptureError` |
| `130` | interrupted with Ctrl+C |

Next: [Getting started](getting-started.md) · [Hardware probe](hardware-probe.md)
· [Troubleshooting](troubleshooting.md)
