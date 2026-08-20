# Echoff

[![PyPI](https://img.shields.io/pypi/v/echoff.svg)](https://pypi.org/project/echoff/)
[![Python](https://img.shields.io/pypi/pyversions/echoff.svg)](https://pypi.org/project/echoff/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/KoljaB/echoff/blob/main/LICENSE)
[![Typed](https://img.shields.io/badge/typing-py.typed-2f74c0)](https://peps.python.org/pep-0561/)
[![Live capture](https://img.shields.io/badge/live_capture-Windows%20%7C%20Linux-0078d4)](https://github.com/KoljaB/echoff/blob/main/docs/platforms.md)

**Echo off. Clean microphone on.**

Stops your voice agent from transcribing its own voice.

When your agent speaks through the selected output device, its playback
can leak into the microphone and reach speech recognition as if you had said it.
Echoff synchronizes that system-audio loopback with microphone capture, then
uses WebRTC acoustic echo cancellation (AEC) to reduce the playback before your
application receives the microphone stream. It is a Python library and CLI, not
a virtual microphone, VAD, ASR, TTS, or conversation system. Applications
receive matched reference, raw microphone, and echo-reduced microphone PCM.

## Hear the difference

A 20-second comparison presents the same six-second recording three ways: raw
microphone, Echoff's echo-reduced output, and the matching computer-audio
reference. Listen with headphones.

<video src="https://github.com/user-attachments/assets/ed13f50e-a774-4378-acab-1ee5935bca09" controls></video>

If the player is unavailable, [open the comparison video](https://github.com/user-attachments/assets/ed13f50e-a774-4378-acab-1ee5935bca09).

The raw-microphone and Echoff-output tracks use the same +8 dB monitor gain so
quiet details remain audible. The computer-audio reference is unmodified.

> **Note:** Echoff 0.3.0 is alpha software. Built-in live capture is
> hardware-tested on Windows 10/11 and Ubuntu with PipeWire. The macOS capture
> backend is not implemented. Metrics are diagnostics, not a substitute for
> listening to the raw and AEC tracks on your own hardware.

## Support at a glance

| Platform | Built-in live capture | Processor-only use with aligned PCM |
|---|---|---|
| Windows 10/11 | **Supported**: WASAPI loopback reference + WASAPI microphone; optional strict WDM-KS microphone fallback | Supported |
| Linux | **Supported**: PipeWire sink-monitor reference + ordinary source microphone via `pactl`, `pw-dump`, and `pw-record` | Supported |
| macOS | No built-in capture backend | Processor-only use with application-owned aligned PCM |

Python 3.11 or newer is required. See [Platform support](https://github.com/KoljaB/echoff/blob/main/docs/platforms.md)
for the exact boundary between portable processing and platform-specific
capture.

## Windows quickstart

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

Then record a 20-second raw-vs-clean comparison:

```powershell
.\.venv\Scripts\python.exe -m echoff record --duration 20
```

While it runs, play continuous speech or music through the selected output
device for at least ten seconds. The command reports whether its default
echo-path readiness heuristic sees a sufficiently active, paired echo path.
Speak during a separate part of the run if you also want to listen for near-end
speech preservation.

For a repeatable test, provide a known speech WAV. This requires `ffplay`:

```powershell
.\.venv\Scripts\python.exe -m echoff record `
  --play-wav C:\audio\known-speech.wav `
  --repetitions 1
```

On Windows, a silent output endpoint may produce no WASAPI loopback callbacks.
Echoff then stops after its synchronization reserve instead of manufacturing a
fake reference. Play system audio or use `--play-wav` when testing live capture.

The command prints the artifact directory and writes the three tracks to
compare:

```text
computer_audio.wav  # captured render-endpoint reference
microphone_raw.wav  # microphone before AEC
microphone_aec.wav  # microphone after AEC
```

It also writes received-payload tracks, lifecycle events, effective
configuration, summary, analysis, and logs. See
[Capture artifacts](https://github.com/KoljaB/echoff/blob/main/docs/capture-artifacts.md)
for the complete schema.

A technically clean smoke test has `status: "completed"`, equal frame counts in
the three primary tracks, and no reported source failure, unsafe overflow, or
hard discontinuity. AEC effectiveness still requires listening to
`microphone_raw.wav` and `microphone_aec.wav`.

Listen first to `microphone_raw.wav` and `microphone_aec.wav`. Speaker playback
should be lower in the AEC track while your own speech remains intelligible.
The whole-run level difference is only descriptive when real microphone speech
is present; use a controlled far-end-only window before calling a number echo
suppression. The [hardware probe guide](https://github.com/KoljaB/echoff/blob/main/docs/hardware-probe.md)
shows the repeatable path.

## Linux quickstart

Install the PipeWire command-line tools. On Ubuntu:

```bash
sudo apt update
sudo apt install pipewire-bin pulseaudio-utils
```

Create an isolated environment and install the published package:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install echoff
```

List the available sink monitors and microphone sources, then record the same
raw-vs-clean comparison:

```bash
.venv/bin/python -m echoff devices
.venv/bin/python -m echoff record --duration 20
```

PipeWire's default sink monitor and source are selected automatically. If they
are not the physical stereo and microphone you want, pass their displayed
indexes with `--reference-device` and `--microphone-device`. See the
[Linux getting-started guide](https://github.com/KoljaB/echoff/blob/main/docs/getting-started-linux.md)
for explicit routing and repeatable playback probes.

## Why Echoff exists

System loopback and microphone devices start independently. Matching their
first callbacks by arrival order can pair audio from different moments, leaving
WebRTC with the wrong echo reference even when both streams have the same block
count. Echoff instead:

1. captures the render reference and microphone on separate streams;
2. maps each stream's source timing into one local monotonic domain;
3. establishes one sequence offset at startup and then treats received sample
   order as authoritative;
4. retires and counts leading microphone blocks that have no matching reference
   in the processed timeline (`startup_unpaired_microphone_blocks`); when
   artifacts are enabled, their raw payload is preserved in
   `microphone_received.wav`;
5. waits symmetrically for any later missing counterpart; and
6. submits each reference frame immediately before its matching microphone
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
matched reference and raw microphone, canonical processed-timeline end times,
nullable per-source observed end times, pair skew, and an AEC state snapshot.
The canonical `reference_ended_monotonic` and `microphone_ended_monotonic` are
equal for the processed pair; `pair_skew_s` uses observed timing when valid.
An `AecCapture` instance is single-use.

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
System output -> platform loopback -> fixed-block queue --+
                                                         +-> align -> WebRTC APM -> AecFrame
Physical mic -> platform capture -> fixed-block queue ----+
```

At startup, Echoff uses source timing to establish the sequence mapping. Leading
microphone blocks without a matching reference are retired from the processed
timeline and counted in `startup_unpaired_microphone_blocks`; they never create a
synthetic reference slot or an `AecFrame`. When artifacts are enabled, the raw
payload remains in `microphone_received.wav`. Once real paired capture has begun,
received sample order is authoritative. If either expected head is missing,
Echoff waits symmetrically for its counterpart instead of creating synthetic
audio. The configured stall reserve defaults to 3 seconds and adds no
normal-path latency.

If the reserve expires or a source fails, Echoff marks alignment degraded and
suspends unsafe paired AEC output. A proven sequence discontinuity opens one new
epoch and resets WebRTC at most once. See [Architecture](https://github.com/KoljaB/echoff/blob/main/docs/architecture.md)
for recovery behavior, timing, and clock boundaries.

With default settings, `echo_path_ready` requires 7.5 seconds of active paired
reference audio, sufficient microphone exposure, and at least 10 dB raw-to-clean
reduction sustained for 250 ms over a rolling one-second window of active
far-end frames. This is a readiness heuristic, not proof that all echo has been
removed. The application decides whether it should gate VAD or barge-in.

## Validate on your hardware

For repeatable far-end-only evidence, confirm `ffplay -version`, choose a speech
WAV, remain silent, and run:

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

- [Logging](https://github.com/KoljaB/echoff/blob/main/docs/logging.md) — configure application logs and control visible runtime diagnostics.
- [Documentation home](https://github.com/KoljaB/echoff/blob/main/docs/README.md) — every guide, grouped by task.
- [Windows getting started](https://github.com/KoljaB/echoff/blob/main/docs/getting-started.md), [Linux getting started](https://github.com/KoljaB/echoff/blob/main/docs/getting-started-linux.md), and [CLI](https://github.com/KoljaB/echoff/blob/main/docs/cli.md) — install, select devices, and complete the first capture.
- [Integration](https://github.com/KoljaB/echoff/blob/main/docs/integration.md) and [Python API](https://github.com/KoljaB/echoff/blob/main/docs/python-api.md) — choose an ownership model and wire Echoff safely.
- [Hardware probe](https://github.com/KoljaB/echoff/blob/main/docs/hardware-probe.md), [Capture artifacts](https://github.com/KoljaB/echoff/blob/main/docs/capture-artifacts.md), and [Troubleshooting](https://github.com/KoljaB/echoff/blob/main/docs/troubleshooting.md) — validate real hardware and diagnose failures.
- [Architecture](https://github.com/KoljaB/echoff/blob/main/docs/architecture.md) and [Platform support](https://github.com/KoljaB/echoff/blob/main/docs/platforms.md) — understand timing, clock drift, and OS boundaries.

## Privacy

Diagnostic captures may contain private microphone speech and application
audio. The default `captures\` tree and general WAV/JSONL/log patterns are
ignored by this repository; only the three curated demo WAVs under `assets/`
are explicitly exempt. Custom locations and JSON metadata are not guaranteed to
be ignored. They are not encrypted; check `git status` and review every artifact
before sharing.

## Development

Clone the repository only when contributing. See [Contributing](https://github.com/KoljaB/echoff/blob/main/CONTRIBUTING.md)
and [Development](https://github.com/KoljaB/echoff/blob/main/docs/development.md)
for the editable install, deterministic tests, and hardware-evidence contract.

## License

Echoff is open source under the [MIT License](https://github.com/KoljaB/echoff/blob/main/LICENSE).
