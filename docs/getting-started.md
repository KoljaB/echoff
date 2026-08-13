# Getting started on Windows

[Documentation home](README.md)

This path starts from the published package. Clone the repository only if you
intend to contribute.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- a normal playback endpoint and microphone
- speakers for a meaningful acoustic-echo test; headphones can verify capture
  and processing but usually leak too little sound to demonstrate cancellation
- optional: `ffplay` for a repeatable WAV stimulus

## 1. Create an environment and install

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install echoff
```

Using the environment's Python explicitly avoids PATH and wrong-interpreter
problems.

Confirm the installed version:

```powershell
.\.venv\Scripts\python.exe -c "import echoff; print(echoff.__version__)"
```

## 2. Confirm devices

```powershell
.\.venv\Scripts\python.exe -m echoff devices
```

Echoff lists WASAPI reference and microphone devices and marks the defaults.
The recording command accepts either an exact numeric index or one unique,
case-insensitive name fragment:

```powershell
.\.venv\Scripts\python.exe -m echoff record `
  --reference-device "Speakers" `
  --microphone-device "Microphone" `
  --duration 20
```

An ambiguous name is rejected instead of selecting silently. If the selected
WASAPI microphone cannot open, Echoff may match and open its WDM-KS counterpart;
that fallback is reported in the artifacts rather than listed as a separate
selectable device.

## 3. Record the first session

```powershell
.\.venv\Scripts\python.exe -m echoff record --duration 20
```

Play continuous speech or music through the selected output endpoint for at
least ten seconds. The default readiness gate needs at least 7.5 seconds of
active, correctly paired reference audio plus stable measured suppression. Speak during part of the recording if
you also want to inspect near-end preservation. The command prints a new
directory under `captures\` unless `--output` is supplied. It never overwrites
a non-empty directory.

Start with these files:

1. `summary.json`: verify `status` is `completed`, the selected devices are
   correct, and no error was recorded.
2. `microphone_raw.wav`: confirm the loudspeaker signal really reached the mic.
3. `microphone_aec.wav`: confirm loudspeaker playback is reduced while your
   own speech remains present.
4. `analysis.json`: inspect signal levels and alignment diagnostics. Do not call
   the whole-run raw/clean difference echo suppression when the window contains
   real microphone speech.

## 4. Run a repeatable far-end-only probe

With `ffplay` installed, choose a speech-shaped WAV and remain silent:

```powershell
.\.venv\Scripts\python.exe -m echoff record `
  --play-wav C:\audio\known-speech.wav `
  --repetitions 3 `
  --pre-roll 2 `
  --gap 1 `
  --tail 1
```

When `--play-wav` is used, `--duration` must remain positive but does not
control runtime. Runtime is determined by the pre-roll, WAV duration,
repetitions, gaps, and tail. The probe records
process-timed playback windows and calculates far-end-only suppression over
their trimmed interiors.

## 5. Choose the next guide

- Use [Integration](integration.md) if Echoff should feed your application.
- Use [Python API](python-api.md) if your application already owns audio PCM.
- Use [Hardware probe](hardware-probe.md) to compare devices or code changes.
- Use [Troubleshooting](troubleshooting.md) if device selection, exposure, or
  suppression is wrong.

The `echoff` executable is a shorthand for `python -m echoff`; the module form
is used here because it guarantees the intended environment.
