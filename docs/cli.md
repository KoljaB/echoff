# Command-line interface

## `echoff devices`

Lists reference (system-output loopback) and microphone inputs.

```powershell
echoff devices
echoff devices --json
```

## `echoff record`

Records one inspectable session.

```powershell
echoff record --duration 20 --output D:\Temp\echoff-manual
```

Important options:

- `--reference-device INDEX_OR_NAME`
- `--microphone-device INDEX_OR_NAME`
- `--stream-delay-ms 50`
- `--noise-suppression`
- `--log-level DEBUG|INFO|WARNING|ERROR`
- `--play-wav PATH --repetitions 3`

An existing non-empty output directory is rejected. Echoff never overwrites a
prior capture.

## `echoff analyze`

Recomputes signal metrics from an artifact directory:

```powershell
echoff analyze D:\Temp\echoff-manual `
  --far-end-window 2.5:8.0 `
  --near-end-window 10.0:12.0
```

Only an operator-declared far-end-only window is labelled echo suppression.
The whole-run raw/clean difference remains a descriptive level change because
it may contain real microphone speech.
