# Python API

[Documentation home](README.md)

Echoff is typed and exports its main application surface from `echoff`.

## Sample contract

Unless a class says otherwise, audio is:

- 48,000 Hz;
- mono;
- Python float samples nominally in `[-1.0, 1.0]`; and
- split into 10 ms WebRTC APM frames (480 samples).

Inputs outside the float range are clipped during PCM16 conversion. Built-in
capture emits 20 ms blocks (960 samples) by default.

## `AecConfig`

`AecConfig` is an immutable dataclass shared by capture and processors.

| Field | Default | Meaning / constraint |
|---|---:|---|
| `sample_rate` | `48000` | Fixed at 48 kHz in 0.1 |
| `channels` | `1` | Fixed at mono in 0.1 |
| `block_duration_s` | `0.020` | Capture block; must contain whole 10 ms APM frames |
| `stream_delay_ms` | `50` | Non-negative WebRTC render-to-capture delay hint |
| `noise_suppression` | `False` | Enable WebRTC noise suppression in addition to AEC |
| `high_pass_filter` | `True` | Enable WebRTC high-pass filtering |
| `automatic_gain_control` | `False` | Enable WebRTC AGC; off by default to avoid changing level policy silently |
| `pair_tolerance_s` | `0.010` | Tolerance for startup/recovery timing evidence; no more than half a capture block |
| `reference_stall_grace_s` | `3.0` | Symmetric exceptional reserve for either temporarily late source; starts when the worker first observes an unmatched head |
| `queue_fatal_s` | `15.0` | Backlog duration that becomes a fatal health error |
| `startup_timeout_s` | `3.0` | Maximum wait for the first aligned pair |
| `echo_path_warmup_s` | `7.5` | Minimum paired active far-end time before readiness |
| `far_end_active_rms_min` | `0.001` | Per-frame reference RMS threshold for warm-up |
| `echo_path_quality_window_s` | `1.0` | Rolling raw/clean microphone energy window |
| `echo_path_quality_stable_s` | `0.25` | Required consecutive quality-qualified time |
| `echo_path_min_suppression_db` | `10.0` | Minimum measured raw-to-clean reduction |
| `echo_path_quality_min_raw_rms` | `0.003` | Minimum raw-mic exposure for a meaningful quality decision |
| `backend` | `"auto"` | `"auto"` or `"windows"`; built-in capture still requires Windows |
| `allow_wdmks_microphone_fallback` | `True` | Try a matched (or sole) WDM-KS mic if WASAPI cannot open |

Use `config.to_dict()` for JSON-compatible effective settings.

## `AecCapture`

```text
AecCapture(
    config=None,
    *,
    on_frame=None,
    on_reference=None,
    on_event=None,
    output_dir=None,
    reference_device=None,
    microphone_device=None,
    console_diagnostics=True,
)
```

`console_diagnostics=True` prints important runtime warnings and errors directly
to `stderr`, in red when the stream is an interactive terminal. This is enabled
by default so a lost reference mapping or capture failure is visible even when
the host application has not configured Python logging. Set it to `False` only
when the host fully surfaces `on_event` itself.

The two injectable constructor hooks used by tests (`processor` and
`source_factory`) are not needed for normal application integration.

### Callbacks

- `on_frame(AecFrame)`: each aligned, processed pair.
- `on_reference(samples, ended_monotonic)`: the reference for each confirmed
  pair emitted on the processed timeline. Use `reference_received.wav` for every
  raw reference payload, including unpaired boundaries.
- `on_event(CaptureEvent)`: structured lifecycle and alignment events.

`on_frame` and `on_reference` run on the pairing thread and must return quickly.
An exception from either is recorded as a processing failure. `on_event` runs
synchronously on whichever caller or worker emits the event; make it fast and
thread-safe. Its exceptions are logged and suppressed so telemetry cannot take
down audio processing.

### Lifecycle and methods

- `start() -> AecCapture`: single-use startup; waits for initial source audio,
  not necessarily an aligned pair.
- `stop(error=None, status_name=None)`: idempotent cleanup and finalization.
- `status() -> CaptureStatus`: immutable current snapshot.
- `raise_if_failed()`: records source failures as degraded status/events and
  raises on processing/callback failure or an unsafe fatal backlog.
- `record_event(kind, **details)`: append an application event to the capture
  timeline while artifacts are open; outside that lifetime only `on_event` can
  observe it.
- `set_summary_metadata(**values)`: add JSON-compatible final-summary data while
  running.
- `started_monotonic` and `elapsed_s()` expose lifecycle timing.
  `timeline_started_monotonic` is set after the first artifact block only when
  `output_dir` recording is enabled.
- `processed_sample_count` is the current integer cursor on the confirmed-pair
  processed timeline. Use it to bind external events to artifact samples
  without assuming wall time and device sample time remain identical.

An instance cannot be restarted after `stop()`. The context manager propagates
asynchronous processing/callback failure on a normal exit; source loss remains
explicit degraded status and telemetry.

## `AecFrame` and `AecState`

`AecFrame` contains:

- `reference`, `microphone_raw`, `microphone_clean`;
- `reference_ended_monotonic`, `microphone_ended_monotonic`;
- `pair_skew_s` (`reference_end - microphone_end`); and
- `state: AecState`.

`AecState` contains `echo_path_ready`, cumulative `far_end_active_s` for the
current epoch, `echo_path_quality_ready`, the latest `echo_suppression_db`,
consecutive `echo_quality_s`, `alignment_epoch`, and
`stream_alignment_reset_count`.

## `CaptureStatus`

`status().to_dict()` groups operational evidence into these families:

- lifecycle: `running`, processing `error`, and separate source errors;
- alignment: mode/epoch, processed and matched counts, tolerance, skew,
  clock-suspect observations, synchronization waits/backlog, degraded
  retirements by cause, hard discontinuities, and shutdown-tail counters;
- echo quality: readiness, active-reference time, latest measured suppression,
  and the consecutive quality-qualified duration;
- devices: backend, selected name/index, status/overflow/underflow and timestamp
  anomaly counters;
- queues/timeline: captured audio seconds, current queue seconds, callback
  packet/payload totals, queue high-water/age, enqueue time, and timeline drift;
- AEC: readiness, active far-end seconds, reset count; and
- processing: mean/max processing milliseconds.

Serialized captures identify `echoff-capture-artifacts-v2`. Echoff is alpha;
validate the schema before consuming fields and use `to_dict()` rather than
reflecting over dataclass internals.

## `WebRtcAecProcessor`

```python
from echoff import AecConfig, WebRtcAecProcessor

reference = (0.0,) * 480
microphone = (0.0,) * 480
processor = WebRtcAecProcessor(AecConfig())
clean = processor.process_pair(reference, microphone)
processor.reset_alignment()
state = processor.state
```

`process_pair()` requires non-empty equal blocks with lengths divisible by 480.
It submits every reverse frame immediately before the matching microphone frame
while holding one internal lock, and returns the same sample count.

## `BufferedWebRtcAecProcessor`

Accepts aligned equal pairs with arbitrary call boundaries. It returns only
complete 480-sample outputs; therefore a call can return fewer samples than it
received. `flush()` zero-pads inside APM but returns only the real pending sample
count. `reset_alignment()` clears partial buffers.

```python
from echoff import BufferedWebRtcAecProcessor

reference_chunk = (0.0,) * 700
microphone_chunk = (0.0,) * 700
processor = BufferedWebRtcAecProcessor()
clean_now = processor.process_pair(reference_chunk, microphone_chunk)
clean_tail = processor.flush()
```

## `StreamingWebRtcAecProcessor`

`process_reference(samples)` submits complete reference frames as they become
available. `process_microphone(samples)` returns complete processed microphone
frames. This class does not align timestamps; use it only when the host owns one
deterministic clock and ordering contract.

```python
from echoff import StreamingWebRtcAecProcessor

reference_chunk = (0.0,) * 960
microphone_chunk = (0.0,) * 960
processor = StreamingWebRtcAecProcessor()
processor.process_reference(reference_chunk)
clean = processor.process_microphone(microphone_chunk)
```

## `PassthroughAecProcessor`

Implements the capture processor contract while returning microphone samples
unchanged. It is useful for controlled baselines and host tests, not echo
cancellation. Its readiness remains false.

## Devices, events, and errors

- `DeviceInfo`: kind, backend, index, name, default flag, channels, and reported
  default sample rate.
- `CaptureEvent`: schema, kind, UTC/monotonic times, and structured details.
- `AecCaptureError`: base for expected package errors.
- `AudioBackendError`, `CaptureStateError`, `UnsupportedPlatformError`: concrete
  failure categories.

## `PcmWavRecorder`

`PcmWavRecorder(path, sample_rate)` incrementally writes mono float samples as
exclusive-create PCM16 WAV. `write(samples)` clips to the PCM16 range;
`close()` is idempotent. It is a low-level utility; `AecCapture(output_dir=...)`
is preferred for synchronized diagnostic artifacts.

## Probe and analysis submodules

The CLI is the stable first choice for hardware evidence. Advanced applications
may use these supported submodule APIs:

```python
from pathlib import Path

from echoff.analysis import analyze_capture
from echoff.probe import ProbeConfig, run_probe

result = run_probe(
    ProbeConfig(
        output_dir=Path("captures/probe-001"),
        duration_s=20,
    )
)
report = analyze_capture(
    result["output_dir"],
    far_end_windows=[(2.0, 7.0)],
    write_report=False,
)
```

`run_probe()` requires a new or empty output directory and writes capture plus
analysis artifacts. `analyze_capture()` requires the three equal-timeline WAVs;
with `write_report=False` it returns a report without modifying the directory.
With `write_report=True` (default), it exclusive-creates `analysis.json` and
fails if that report or its temporary file already exists.

Next: [Integration](integration.md) · [Architecture](architecture.md) ·
[Troubleshooting](troubleshooting.md)
