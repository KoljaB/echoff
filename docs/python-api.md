# Python API

## `AecConfig`

Immutable configuration for capture and processing. Defaults are 48 kHz mono,
20 ms capture blocks, 10 ms APM frames, 50 ms WebRTC stream delay, and a 10 ms
timestamp-pairing tolerance.

## `AecCapture`

Owns device sources, timestamp queues, alignment, APM processing, optional
artifacts, and lifecycle.

```python
with AecCapture(config, on_frame=callback, output_dir="capture") as capture:
    capture.raise_if_failed()
    status = capture.status()
```

`on_frame` receives only aligned and AEC-processed `AecFrame` objects. The
callback runs on Echoff's pairing thread and should return quickly.

Useful methods:

- `start()` / `stop()`
- `status()`
- `raise_if_failed()`
- `record_event(kind, **details)`
- `set_summary_metadata(**values)`

## `AecFrame`

Contains the matched system-audio reference, raw microphone, clean microphone,
both monotonic block-end timestamps, their skew, and an `AecState` snapshot.

## `WebRtcAecProcessor`

For applications that already capture and align PCM:

```python
processor = WebRtcAecProcessor(AecConfig())
clean = processor.process_pair(reference, microphone)
```

Both inputs must contain equal numbers of 48-kHz mono samples and a whole number
of 480-sample APM frames. `reset_alignment()` starts a new cold AEC epoch.
