# Echoff

**Echo off. Clean microphone on.**

`echoff` is a small Python library for real-time acoustic echo cancellation.
It captures the audio rendered by the computer and the microphone on separate
streams, aligns their timelines, and feeds matched 10 ms frame pairs to WebRTC's
Audio Processing Module (APM).

The package deliberately does **not** contain voice activity detection, speech
recognition, text-to-speech, or conversation policy. Applications receive clean
microphone frames and decide what to do with them.

## Current platform support

| Platform | Status | Capture backend |
|---|---|---|
| Windows | Supported | WASAPI loopback and microphone through PyAudioWPatch, with WDM-KS microphone fallback |
| Linux | Planned | PipeWire sink monitor and microphone source |

The WebRTC processor and timestamp aligner are platform-neutral. Only the device
capture adapters are platform-specific.

## Install for development on Windows

```powershell
git clone https://github.com/KoljaB/echoff D:\Projekte\echoff
cd D:\Projekte\echoff
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Five-minute hardware check

List devices:

```powershell
echoff devices
```

Record system audio, the raw microphone, and the AEC-cleaned microphone:

```powershell
python examples\record_aec_session.py --duration 20 --log-level INFO
```

While it runs, play any speech or music through the normal speakers. You may
also speak into the microphone. The output directory contains:

```text
computer_audio.wav
microphone_raw.wav
microphone_aec.wav
events.jsonl
config.json
summary.json
run.log
```

For a repeatable loudspeaker stimulus, provide a WAV file:

```powershell
python examples\record_aec_session.py `
  --play-wav D:\audio\speech.wav `
  --repetitions 3 `
  --output D:\Temp\aec-probe
```

See [Capture artifacts](docs/capture-artifacts.md) for the exact meaning of
each file and [Hardware probe](docs/hardware-probe.md) for a repeatable test.

## Library API

```python
import time
from pathlib import Path

from echoff import AecCapture, AecConfig, AecFrame


def consume(frame: AecFrame) -> None:
    # 48 kHz mono floating-point samples in [-1.0, 1.0].
    send_to_your_audio_pipeline(frame.microphone_clean)


config = AecConfig(stream_delay_ms=50)
with AecCapture(
    config,
    on_frame=consume,
    output_dir=Path("capture-artifacts"),
) as capture:
    time.sleep(20)
    print(capture.status())
```

For applications that already own their audio devices, use only the processor:

```python
from echoff import AecConfig, WebRtcAecProcessor

processor = WebRtcAecProcessor(AecConfig())
clean_microphone = processor.process_pair(reference_samples, microphone_samples)
```

`process_pair()` is intentionally atomic: the far-end reference is always
submitted immediately before its matching microphone frame.

## Design guarantees

- One worker owns reference/microphone pairing and APM call order.
- Capture blocks carry monotonic end timestamps.
- Startup phase differences and later discontinuities are realigned instead of
  silently pairing stale frames.
- Every realignment starts a fresh AEC epoch exactly once.
- The echo-path readiness signal advances only on paired, active far-end audio.
- Libraries never configure the process-wide root logger.
- Raw and processed audio can be recorded for every run.

Read [Architecture](docs/architecture.md) and [Integration](docs/integration.md)
before adding a new backend.

## Development

```powershell
python -m unittest discover -s tests -v
ruff check .
mypy src
python -m build
```

Hardware tests are deliberately separate from unit tests. Unit tests must not
open devices or play audio.

## License

No distribution license has been selected yet. Until the repository owner adds
one, the source is provided without a grant to copy, modify, or redistribute it.
