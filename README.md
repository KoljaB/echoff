# Echoff

[![PyPI](https://img.shields.io/pypi/v/echoff.svg)](https://pypi.org/project/echoff/)
[![Python](https://img.shields.io/pypi/pyversions/echoff.svg)](https://pypi.org/project/echoff/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/KoljaB/echoff/blob/main/LICENSE)
[![Typed](https://img.shields.io/badge/typing-py.typed-2f74c0)](https://peps.python.org/pep-0561/)
[![Windows capture](https://img.shields.io/badge/live_capture-Windows-0078d4)](https://github.com/KoljaB/echoff/blob/main/docs/platforms.md)

**Reduce computer-speaker audio leaking back into a live microphone stream.**

Echoff is a focused Python package for synchronized duplex capture and real-time
acoustic echo cancellation (AEC). It timestamp-aligns Windows system-audio
loopback and microphone blocks *before* feeding matched frame pairs to WebRTC's
Audio Processing Module. Applications receive the reference, raw microphone,
and echo-reduced microphone PCM.

## Hear the difference

The same 14-second moment from a live gameplay session: computer playback, the
physical microphone before AEC, and Echoff's output. Headphones make the
comparison easiest to hear:

| 1. Echoff output — echo reduced | 2. Original microphone — echo present | 3. Computer audio — reference |
|---|---|---|
| <video src="https://github.com/user-attachments/assets/d7049699-87dd-408c-b12b-c206c81046fa" controls></video> | <video src="https://github.com/user-attachments/assets/7a4b879d-26e8-459b-b904-d812938d80dd" controls></video> | <video src="https://github.com/user-attachments/assets/d12a0150-2043-4ac0-93f6-cf32f9e5ac06" controls></video> |

GitHub starts embedded media muted; use the speaker button before comparing.

> **Project status:** Echoff 0.1 is alpha software. Built-in live capture is
> physically tested on Windows. The processor APIs are designed for
> application-owned aligned PCM on other platforms where the LiveKit dependency
> installs, but those paths are not CI- or hardware-qualified here and Linux and
> macOS capture backends are not implemented. APIs and artifact schemas may
> change before 1.0. Echoff is licensed under the MIT License.

## Support at a glance

| Platform | Built-in live capture | Processor-only use with aligned PCM |
|---|---|---|
| Windows 10/11 | **Supported**: WASAPI loopback + WASAPI microphone, with WDM-KS microphone fallback | Supported |
| Linux | Planned: PipeWire backend | Designed for application-owned PCM where LiveKit installs; not qualified here |
| macOS | Not implemented | Designed for application-owned PCM where LiveKit installs; not qualified here |

Python 3.11 or newer is required. See [Platform support](https://github.com/KoljaB/echoff/blob/main/docs/platforms.md)
for the exact boundary between portable processing and platform-specific
capture.

## Three-minute Windows quickstart

Create an isolated environment and install the published package:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install echoff
```

List the endpoints Echoff can select:

```powershell
.\.venv\Scripts\python.exe -m echoff devices
```

Then start a 20-second evidence-preserving recording:

```powershell
.\.venv\Scripts\python.exe -m echoff record --duration 20
```

While it runs, play continuous speech or music through the normal speakers for
at least ten seconds. Readiness requires 7.5 seconds of active, correctly paired
reference audio plus measured suppression of the microphone signal. Speak for part of the run only if you
also want to check that near-end speech survives. The command prints the
artifact directory and writes:

```text
computer_audio.wav   # captured render-endpoint reference
microphone_raw.wav   # microphone before AEC
microphone_aec.wav   # microphone after AEC
reference_received.wav  # every received reference payload in source order
microphone_received.wav # every received microphone payload in source order
events.jsonl         # lifecycle and alignment events
config.json          # effective AEC configuration
summary.json         # devices, counters, timing, and final status
analysis.json        # signal-level diagnostics
run.log              # human-readable CLI log
```

Listen first to `microphone_raw.wav` and `microphone_aec.wav`. Speaker playback
should be lower in the AEC track while your own speech remains intelligible.
The whole-run level difference is only descriptive when real microphone speech
is present; use a controlled far-end-only window before calling a number echo
suppression. The [hardware probe guide](https://github.com/KoljaB/echoff/blob/main/docs/hardware-probe.md)
shows the repeatable path.

## Why Echoff exists

System loopback and microphone devices start independently. Matching their
first callbacks by arrival order can pair audio from different moments, leaving
WebRTC with the wrong echo reference even when both streams have the same block
count. Echoff instead:

1. captures the render reference and microphone on separate streams;
2. maps each stream's PortAudio estimate into one local monotonic domain;
3. establishes one sequence offset at startup and then treats received sample
   order as authoritative;
4. waits symmetrically for a temporarily late counterpart instead of creating
   a synthetic slot; and
5. submits each reference frame immediately before its matching microphone
   frame.

This is the capture-and-alignment layer that a bare AEC wrapper does not
provide.

## Choose the right API

| Audio-source situation | Use | Why |
|---|---|---|
| Echoff should open the system-output loopback and microphone | `AecCapture` | Echoff captures, timestamp-aligns, processes, and optionally records both streams |
| exact time-aligned reference/microphone pairs | `WebRtcAecProcessor` | One atomic call preserves reference-before-microphone ordering |
| exact pairs with arbitrary block boundaries | `BufferedWebRtcAecProcessor` | Buffers partial 10 ms WebRTC frames and flushes the final tail |
| deterministic streams on one shared clock | `StreamingWebRtcAecProcessor` | Accepts continuous reference and microphone input separately; performs no timestamp alignment |

Do **not** use the streaming adapter for two independently clocked physical
devices. Use `AecCapture` or align the streams before calling a processor.

## Minimal application skeleton

`on_frame` and `on_reference` run on Echoff's pairing thread, so move
application work to a queue and poll capture health from the application loop:

```python
import time
from queue import Empty, Queue

from echoff import AecCapture, AecConfig, AecFrame

frames: Queue[AecFrame] = Queue()


def handle_clean_audio(samples: tuple[float, ...]) -> None:
    """Replace this with your VAD, recorder, stream, or ASR handoff."""
    pass


capture = AecCapture(AecConfig(), on_frame=frames.put)
capture.start()
try:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        capture.raise_if_failed()
        try:
            frame = frames.get(timeout=0.1)
        except Empty:
            continue
        handle_clean_audio(frame.microphone_clean)
finally:
    capture.stop()

# Surface a device or processing failure that happened near shutdown.
capture.raise_if_failed()
```

This bounded-duration example uses an unbounded queue for clarity. Production
applications need a bounded, non-blocking handoff and an explicit overload
policy; blocking the callback stalls Echoff's pairing thread.

`AecFrame` contains 48 kHz mono floating-point samples in `[-1.0, 1.0]`, the
matched reference and raw microphone, both timestamps, pair skew, and an AEC
state snapshot. An `AecCapture` instance is single-use.

For an application that already owns aligned PCM:

```python
from echoff import AecConfig, WebRtcAecProcessor

reference_10ms = (0.0,) * 480
microphone_10ms = (0.0,) * 480
processor = WebRtcAecProcessor(AecConfig())
clean_microphone = processor.process_pair(reference_10ms, microphone_10ms)
```

Both inputs must be equal-length, 48 kHz mono floats and contain a whole number
of 480-sample (10 ms) frames. Read [Integration](https://github.com/KoljaB/echoff/blob/main/docs/integration.md)
before wiring physical devices or separate capture clocks.

## How the live path works

```text
Windows output -> WASAPI loopback -> timestamp queue --+
                                                       +-> align -> WebRTC APM -> AecFrame
Physical mic  -> WASAPI / WDM-KS -> timestamp queue ---+
```

Startup needs three consistent observations to establish the sequence mapping.
After lock, matching heads are processed immediately. If either expected head
is missing, Echoff starts a wait when the pairing worker first observes it and
buffers both directions for up to three seconds. A counterpart that arrives
inside that reserve drains in order with the same mapping: no synthetic audio,
payload discard, or WebRTC reset is involved.

If the reserve expires or a source fails, Echoff marks alignment degraded and
suspends unsafe paired AEC output. With `output_dir` enabled, every received
source payload continues into its raw source track while bounded live buffers
may be retired with explicit cause counters. Source errors remain visible in
status and events; processing/callback failures and an unsafe unbounded backlog
remain health errors. A proven sequence discontinuity opens one new epoch and
resets WebRTC at most once.

The `echo_path_ready` state turns true only after at least 7.5 seconds of paired,
active far-end audio and 250 ms of stable measured suppression of at least 10 dB
over a rolling one-second window. The application decides whether that state
should gate VAD or barge-in. Echoff does not resample the live streams or automatically retune the
configured WebRTC `stream_delay_ms` hint. The Windows backend uses PortAudio
callback timestamps from one shared WASAPI host context. After startup those
timestamps are diagnostics only: they cannot invent samples, renumber pairs, or
commit a live rate correction. Relative hardware-rate correction remains out of
scope for 0.1.

## Validate on your hardware

For repeatable far-end-only evidence, confirm `ffplay -version`, choose a speech WAV,
remain silent, and run:

```powershell
.\.venv\Scripts\python.exe -m echoff record `
  --play-wav C:\audio\known-speech.wav `
  --repetitions 3
```

Echoff preserves all three tracks plus the process-timed stimulus windows.
Compare runs only after fixing endpoint, microphone, speaker position, volume,
input WAV, and stream-delay setting. Do not tune acceptance thresholds after
seeing the result.

## Documentation

- [Logging](https://github.com/KoljaB/echoff/blob/main/docs/logging.md) - configure application logs and control visible runtime diagnostics.
- [Documentation home](https://github.com/KoljaB/echoff/blob/main/docs/README.md) — every guide, grouped by task.
- [Getting started](https://github.com/KoljaB/echoff/blob/main/docs/getting-started.md) and [CLI](https://github.com/KoljaB/echoff/blob/main/docs/cli.md) — install, select devices, and complete the first capture.
- [Integration](https://github.com/KoljaB/echoff/blob/main/docs/integration.md) and [Python API](https://github.com/KoljaB/echoff/blob/main/docs/python-api.md) — choose an ownership model and wire Echoff safely.
- [Hardware probe](https://github.com/KoljaB/echoff/blob/main/docs/hardware-probe.md) and [Troubleshooting](https://github.com/KoljaB/echoff/blob/main/docs/troubleshooting.md) — validate real hardware and diagnose failures.
- [Architecture](https://github.com/KoljaB/echoff/blob/main/docs/architecture.md) and [Platform support](https://github.com/KoljaB/echoff/blob/main/docs/platforms.md) — understand timing, clock drift, and OS boundaries.

## Privacy

Diagnostic captures may contain private microphone speech and application
audio. The default `captures\` tree and general WAV/JSONL/log patterns are
ignored by this repository; only the two curated demo WAVs under `assets/` are
explicitly exempt. Custom locations and JSON metadata are not guaranteed to be
ignored. They are not encrypted; check `git status` and review every artifact
before sharing.

## Development

Clone the repository only when contributing. See [Contributing](https://github.com/KoljaB/echoff/blob/main/CONTRIBUTING.md)
and [Development](https://github.com/KoljaB/echoff/blob/main/docs/development.md)
for the editable install, deterministic tests, and hardware-evidence contract.

## License

Echoff is open source under the [MIT License](https://github.com/KoljaB/echoff/blob/main/LICENSE).
