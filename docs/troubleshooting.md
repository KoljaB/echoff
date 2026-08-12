# Troubleshooting

[Documentation home](README.md)

Work from routing and evidence before changing AEC parameters. Keep the full
artifact directory for any run whose signal behavior matters.

## `echoff` is not found or the wrong version imports

Use one interpreter for install and execution:

```powershell
.\.venv\Scripts\python.exe -m pip show echoff
.\.venv\Scripts\python.exe -c "import echoff; print(echoff.__version__, echoff.__file__)"
.\.venv\Scripts\python.exe -m echoff --help
```

The module form avoids a stale `echoff.exe` from another environment.

## Windows dependencies are missing

There is no separate Windows extra. Base installation selects Windows capture
dependencies automatically:

```powershell
.\.venv\Scripts\python.exe -m pip show echoff PyAudioWPatch sounddevice livekit
.\.venv\Scripts\python.exe -m pip install --force-reinstall echoff
```

If `pip show` points at a development checkout unexpectedly, create a new venv
before diagnosing packaging.

## No devices are listed

1. Confirm the command is running on Windows.
2. Confirm `PyAudioWPatch` is installed in the same environment.
3. Confirm Windows sees an enabled default output and microphone.
4. Close applications using a device exclusively, then retry.
5. Run `python -m echoff devices --json` and preserve the error output.

`devices` lists selectable WASAPI endpoints. WDM-KS microphone fallback is
automatic after a WASAPI open failure and is not independently listed.

## A device selector is ambiguous or not found

Use the exact numeric index printed by `devices`, or a longer unique name
fragment:

```powershell
python -m echoff record --reference-device 17 --microphone-device 4
```

Echoff rejects ambiguous fragments rather than guessing.

## Capture never becomes ready

`start()` needs one reference and microphone block whose end timestamps fall
within the configured tolerance. Check device errors, permissions, exclusive
use, and `events.jsonl`. Do not increase the tolerance merely to force a lock;
that can pair different audio intervals.

## `computer_audio.wav` is silent or contains the wrong application

The selected loopback endpoint must be the endpoint that actually drives the
speakers. Windows may route an application to a non-default device. Run
`devices`, select the correct reference index explicitly, and verify the WAV
before interpreting AEC quality.

Exclusive-mode or protected playback may not appear in shared-mode loopback.

## `microphone_raw.wav` has little speaker leakage

That is good acoustically but makes an AEC-quality test inconclusive. Raise the
physical exposure in a controlled way: use speakers rather than isolated
headphones, confirm system volume, and use a speech-shaped far-end stimulus.
Do not infer suppression from a clean track when the raw mic contained no echo.

## Echo remains strong in `microphone_aec.wav`

Check in this order:

1. correct output and microphone identities in `summary.json`;
2. audible far-end exposure in both `computer_audio.wav` and
   `microphone_raw.wav`;
3. `status: completed` and no source/processing error;
4. alignment lock, skew, mismatch, realignment, silence, and drop counters;
5. whether the current AEC epoch accumulated at least 3.25 seconds of active
   paired reference; and
6. a repeatable far-end-only window rather than whole-run levels.

Only then compare a predeclared `stream_delay_ms` setting under fixed hardware.
Do not compensate with ASR-text matching or a higher VAD threshold.

## Echo becomes strong after an endpoint or device change

Create a new `AecCapture` instance. Preserve the failed run and compare selected
device IDs, first callback skew, realignment events, and the three WAV tracks to
a known-good baseline. Hardware/driver changes can alter latency and routing
without changing Python configuration.

## The echo path is never ready

Readiness advances only on paired reference frames with RMS at least `0.001`.
Silence is expected to remain cold. Play normal far-end audio for at least 3.25
seconds in one alignment epoch. A realignment resets accumulated warm-up.

## The clean microphone is silent, distorted, or clipped

Compare raw and clean tracks in an interval with near-end speech. Check clipping
counts in `analysis.json`, disable optional noise suppression, and confirm that
the microphone input itself is not clipped. Automatic gain control is off by
default. A level comparison alone cannot establish speech quality; listen to
the preserved PCM.

## `ffplay is required for --play-wav`

Install an FFmpeg distribution that includes `ffplay` and confirm:

```powershell
ffplay -version
```

Ambient `record --duration ...` does not require `ffplay`.

## Output directory is not empty

Choose a new directory. Echoff intentionally refuses to overwrite evidence:

```powershell
python -m echoff record --output captures\probe-002
```

Do not delete an earlier directory until you have confirmed it contains no
unique recording or diagnostic data.

## Linux or macOS reports unsupported platform

Built-in device listing and capture are Windows-only in 0.1. The portable
processor classes remain importable when your application supplies correctly
aligned PCM. See [Platform support](platforms.md).

## Before filing an issue

Include package/Python/Windows versions, the command, redacted `config.json`,
`summary.json`, relevant event rows, and whether the raw mic had far-end
exposure. Do not attach raw audio publicly without reviewing its private
content.

Next: [Capture artifacts](capture-artifacts.md) ·
[Hardware probe](hardware-probe.md) · [Architecture](architecture.md)
