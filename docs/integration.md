# Integration

[Documentation home](README.md)

Start by deciding who owns the two audio streams. The wrong API can preserve
sample counts while pairing the wrong moments.

## Choose an ownership model

| Input ownership | API | Alignment responsibility |
|---|---|---|
| Echoff opens system loopback and microphone | `AecCapture` | Echoff timestamps and aligns independent devices |
| Your app supplies exact aligned pairs | `WebRtcAecProcessor` | Your app proves each pair represents the same interval |
| Your app supplies exact pairs with arbitrary boundaries | `BufferedWebRtcAecProcessor` | Your app aligns; Echoff buffers 10 ms APM frames |
| Your deterministic pipeline owns one shared clock | `StreamingWebRtcAecProcessor` | Your app preserves reference-before-microphone ordering |

Never mix system audio into microphone samples before AEC. WebRTC needs the
far-end reference and near-end microphone as distinct inputs.

## Echoff owns the devices

`AecCapture` is the recommended live path on Windows:

```python
import time
from queue import Empty, Queue

from echoff import AecCapture, AecConfig, AecFrame

frames: Queue[AecFrame] = Queue()
capture = AecCapture(
    AecConfig(),
    on_frame=frames.put,
    reference_device=None,   # default WASAPI loopback endpoint
    microphone_device=None,  # default WASAPI microphone
)

capture.start()
try:
    while application_is_running():
        capture.raise_if_failed()
        try:
            frame = frames.get(timeout=0.1)
        except Empty:
            continue
        application_audio_queue.put(frame.microphone_clean)
finally:
    capture.stop()
capture.raise_if_failed()
```

`on_frame` and `on_reference` run on Echoff's pairing thread. They must return
quickly: enqueue the frame, update a cheap counter, or copy data into an
application-owned buffer. A slow or failing audio callback is a capture
hazard, not backpressure: an exception is an immediate processing failure, while
a slow callback builds queue backlog and can trigger the fatal backlog health
check when the host polls `raise_if_failed()`. Queue capacity and overload
policy belong to the host; do not allow a stalled consumer to grow memory
without bound. `on_event` runs synchronously on whichever caller or worker
emits the event, so it must be fast and thread-safe; its failures are logged and
do not stop audio processing.

Use `on_reference(samples, ended_monotonic)` when the application also consumes
the reference from each confirmed pair. Echoff emits no synthetic reference
callback for an unmatched microphone block. The diagnostic
`reference_received.wav` preserves every received raw reference payload. Use
`on_event(event)` for low-volume
structured lifecycle and alignment telemetry.

### Lifecycle and health

- `start()` waits for initial source audio or the configured startup timeout;
  alignment may still be joining or explicitly degraded when it returns.
- An `AecCapture` object is single-use. Create a new instance after `stop()`.
- Poll `raise_if_failed()` from the application's normal health loop.
- `with AecCapture(...)` stops capture and propagates an asynchronous processing
  failure when the block exits normally. Source failures remain explicit
  degraded status/events.
- `stop()` is idempotent, stops both sources, drains the worker, and finalizes
  artifacts. Cleanup errors are raised after all cleanup paths have run.
- Post-lock device-clock drift remains telemetry and does not change pair
  identity or reset APM. Timestamp regressions and PortAudio status flags are
  evidence, not session failures. Source failure degrades paired output; a
  processing/callback failure or unsafe unbounded backlog remains fatal.

If `output_dir` is set, Echoff reserves the artifact filenames exclusively and
never overwrites an earlier run. Use a new directory per capture.

Call `record_event()` while capture is running if the event must be persisted to
`events.jsonl`; outside the artifact lifetime it can still reach `on_event` but
cannot be appended to the closed file.

## Your application owns exact pairs

Use the atomic processor when both inputs already share one timeline:

```python
from echoff import AecConfig, WebRtcAecProcessor

reference_20ms = (0.0,) * 960
microphone_20ms = (0.0,) * 960
processor = WebRtcAecProcessor(AecConfig())
clean = processor.process_pair(reference_20ms, microphone_20ms)
```

The inputs are mono float samples in `[-1.0, 1.0]` at 48 kHz. They must be
non-empty, equal length, and a multiple of 480 samples. Echoff clips values to
the PCM16 range for WebRTC APM and returns a tuple of floats with the same
length.

“Aligned” means that the first sample in each argument represents the same time
interval. Equal list length, callback count, or wall-clock arrival order does
not establish this. If the streams come from independent physical devices, use
timestamped capture and `AecCapture` rather than this API.

Call `reset_alignment()` after any discontinuity that changes pair identity.
The processor is internally locked, but one ordered calling path is easier to
reason about than concurrent producers.

## Exact pairs with arbitrary boundaries

`BufferedWebRtcAecProcessor` accepts equal paired chunks of any positive length:

```python
from echoff import BufferedWebRtcAecProcessor

reference_chunk = (0.0,) * 700
microphone_chunk = (0.0,) * 700
processor = BufferedWebRtcAecProcessor()
clean_now = processor.process_pair(reference_chunk, microphone_chunk)
clean_tail = processor.flush()
```

`process_pair()` may return fewer samples than were submitted while it waits for
a complete 480-sample frame. Call `flush()` exactly once at end of stream to
return the final partial block. `reset_alignment()` discards pending partial
data and starts a cold AEC epoch.

## Deterministic shared-clock streams

`StreamingWebRtcAecProcessor` exists for replay or another pipeline where both
streams already use one clock:

```python
from echoff import StreamingWebRtcAecProcessor

reference_chunk = (0.0,) * 960
microphone_chunk = (0.0,) * 960
processor = StreamingWebRtcAecProcessor()
processor.process_reference(reference_chunk)
clean = processor.process_microphone(microphone_chunk)
```

Submit enough reference audio before the corresponding microphone audio. The
adapter buffers partial 10 ms frames but performs no timestamp matching. If the
reference is under-supplied, microphone frames still process, but missing
reference activity cannot advance echo-path readiness.

## Application policy stays outside

`frame.state.echo_path_ready` indicates that the current AEC epoch passed both
the configured active-reference warm-up and the rolling raw-to-clean
suppression gate. A voice application may defer VAD or barge-in while local
playback is active and the path is cold. Echoff itself does not discard
microphone frames for this reason.

Turn detection, playback interruption, resampling for ASR, speaker identity,
and conversation history are application responsibilities. Avoid transcript
comparison as an echo-cancellation substitute; diagnose the signal and capture
timeline instead.

## Failure handling

Catch `AecCaptureError` for expected package failures:

- `UnsupportedPlatformError`: no built-in capture backend on this platform.
- `AudioBackendError`: device, native APM, worker, callback, or backlog failure.
- `CaptureStateError`: invalid lifecycle operation, such as restarting one
  capture instance or writing to a closed recorder.

Preserve the artifact directory when diagnosing a physical run. It contains the
selected devices, three time-aligned tracks, skew counters, and final status
needed to separate bad AEC from bad input or routing.

Next: [Python API](python-api.md) · [Capture artifacts](capture-artifacts.md) ·
[Troubleshooting](troubleshooting.md)
