# Python API

## `AecConfig`

Immutable configuration for capture and processing. Defaults are 48 kHz mono,
20 ms capture blocks, 10 ms APM frames, 50 ms WebRTC stream delay, and a 10 ms
timestamp-pairing tolerance.

## `AecCapture`

Owns device sources, timestamp queues, alignment, APM processing, optional
artifacts, and lifecycle.

```python
with AecCapture(
    config,
    on_frame=clean_microphone_callback,
    on_reference=system_audio_callback,
    output_dir="capture",
) as capture:
    capture.raise_if_failed()
    status = capture.status()
```

`on_frame` receives only aligned and AEC-processed `AecFrame` objects. The
callback runs on Echoff's pairing thread and should return quickly.

`on_reference(samples, ended_monotonic)` receives every captured reference
block exactly once, including blocks dropped from AEC pairing during a phase
realignment. This is useful when the application also consumes system audio.

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

## `StreamingWebRtcAecProcessor`

For deterministic replay or another application-owned clocked stream that
delivers the far-end continuously and microphone audio separately:

```python
processor.process_reference(reference)
clean = processor.process_microphone(microphone)
```

Do not use this adapter for two independent hardware capture clocks. Use
`AecCapture`, which aligns timestamps before APM processing.

## `BufferedWebRtcAecProcessor`

For application-owned streams that already provide exact paired blocks but do
not guarantee 10-ms call boundaries. `process_pair()` buffers partial frames
while preserving reference-then-microphone order for every native APM frame;
`flush()` returns the final partial clean block without exposing its internal
zero padding.
