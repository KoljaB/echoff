# Integration

## Recommended API

Use `AecCapture` when the package should own system-audio and microphone
devices. The callback runs on the capture processing thread, so hand expensive
work to an application queue.

```python
from queue import Queue

from echoff import AecCapture, AecConfig

clean_audio = Queue()

with AecCapture(AecConfig(), on_frame=clean_audio.put) as capture:
    run_application()
```

Call `capture.raise_if_failed()` from the application's health loop. Device and
worker failures remain fatal; ordinary timestamp realignment is observable but
recoverable.

## Existing audio ownership

If an application already captures both streams, instantiate
`WebRtcAecProcessor` and call `process_pair()` with time-aligned blocks.

```python
clean = processor.process_pair(reference_20ms, microphone_20ms)
```

Both inputs must be 48 kHz mono and contain the same number of samples. Their
length must be a multiple of 480 samples (10 ms).

## Application policy stays outside

The library exposes `frame.state.echo_path_ready`. A voice application may use
that state to prevent cold-start residual echo from entering VAD during local
playback. The library itself never discards microphone frames for this reason.

Likewise, detecting user turns, interrupting playback, resampling for ASR, and
committing conversation history remain application responsibilities.
