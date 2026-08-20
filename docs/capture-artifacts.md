# Capture artifacts

[Documentation home](README.md)

`AecCapture(output_dir=...)` creates or uses a diagnostic directory and
exclusively creates its reserved files. It tolerates unrelated existing files;
`echoff record` and the probe API are stricter and require a new or empty output
directory. Reserved files are never overwritten. `config.json` and
`summary.json` identify the additive capture schema
`echoff-capture-artifacts-v2`.

## Core capture files

| File | Format | Meaning |
|---|---|---|
| `computer_audio.wav` | PCM16 mono 48 kHz | Digital render reference captured from the selected output endpoint |
| `microphone_raw.wav` | PCM16 mono 48 kHz | Physical microphone before AEC |
| `microphone_aec.wav` | PCM16 mono 48 kHz | Microphone after WebRTC AEC |
| `reference_received.wav` | PCM16 mono 48 kHz | Every received reference payload in source order, independent of live assignment |
| `microphone_received.wav` | PCM16 mono 48 kHz | Every received microphone payload in source order, independent of live assignment |
| `events.jsonl` | UTF-8 JSON Lines | Ordered lifecycle, alignment, application, and error events |
| `config.json` | UTF-8 JSON | Effective public configuration and artifact schema |
| `summary.json` | UTF-8 JSON | Final status, devices, counters, durations, track metadata, and hashes |

The three primary WAVs share one confirmed-pair timeline and advance only when
both sources are present. Echoff does not create a zero-reference slot after a
temporary stall. If the three-second reserve expires, unsafe paired output is
suspended and bounded live-buffer retirements are counted by cause whether or
not artifacts are enabled. With artifacts enabled, raw received-source tracks
continue to preserve those payloads; without artifacts, no raw source track is
written. If capture stops in the middle of a 20 ms source block, the primary
tracks may contain one explicitly padded final pair; `*_padded_samples` reports
source padding. The received-source tracks contain only real received samples.

## Probe and CLI additions

| File | Created by | Meaning |
|---|---|---|
| `analysis.json` | `echoff record`, probe API, or direct analyzer write | Signal metrics and any declared far-/near-end windows |
| `run.log` | `echoff record` or an application that requests file logging | Human-readable log; `devices` and `analyze` do not create it automatically |

`echoff analyze` is read-only and prints JSON. Direct
`analyze_capture(..., write_report=True)` uses exclusive-create semantics.

## `summary.json`

Start with:

- `status` and `error`; status is one of `completed`, `incomplete`, `degraded`,
  or `failed`;
- selected backend/device name/index for reference and microphone;
- `tracks_share_timeline` and each processed track's sample count/hash, plus
  independent source-track counts and hashes;
- alignment mode, matched counts, synchronization waits/backlog, degraded
  retirements by cause, hard discontinuities, and shutdown-tail counts;
- source device/silence/drop counters;
- raw PortAudio timestamp regression/invalid/deviation counters (telemetry only);
- AEC readiness, active far-end time, and reset count; and
- optional application/probe metadata.

`CaptureStatus` additions are defaulted and additive. In particular, inspect
`startup_unpaired_microphone_blocks` for retired leading microphone input,
`echo_path_reset_count` for adaptive-filter resets, queue-overflow counters,
fallback usage/attempt errors, and callback queue high-water marks. A probe
preserves finalized artifacts but raises when the final status is not
`completed`.

Shutdown waits for pending source cleanup and the processing worker before
finalizing artifacts. Concurrent and repeated `stop()` calls are serialized;
transient source, recorder-close, event-write, or atomic-summary failures can be
retried without changing WAV frames. The first terminal error/status is retained,
`capture_stopped` is written once, and one `summary.json` is committed atomically.

Application `AecFrame` objects expose canonical processed-timeline end times in
`reference_ended_monotonic` and `microphone_ended_monotonic`, plus nullable
per-source `reference_observed_end_monotonic` and
`microphone_observed_end_monotonic` values. `pair_skew_s` uses the observed
values when valid and otherwise the canonical timeline.

`clock_suspect_observation_count` counts post-lock timestamp observations that
disagree with the established mapping. These are diagnostics only and do not
commit a rate correction. The `degraded_unpaired_*` totals reconcile live-buffer
retirement into explicit wait-timeout and source-failure causes; the raw
received-source tracks preserve those payloads only when artifacts are enabled.

`*_timestamp_regressions` and `*_invalid_timestamps` describe unreliable
PortAudio time estimates. Echoff anchors once and then advances time from the
received sample count, so these values do not stop capture or imply discarded
audio. `*_timestamp_deviation_max_ms` records the largest difference between the
reported estimate and that continuous sample timeline.

## `events.jsonl`

Every row contains:

```json
{
  "schema_version": "echoff-event-v1",
  "sequence": 1,
  "kind": "capture_starting",
  "utc": "...",
  "monotonic": 123.456,
  "details": {}
}
```

Core kinds include `capture_starting`, `alignment_locked`, `capture_ready`,
`synchronization_wait_started`, `synchronization_wait_ended`,
`synchronization_degraded`, `synchronization_recovered`,
`capture_degraded_ready`, `alignment_discontinuity_pending`, `echo_path_reset`,
`capture_failed`, and `capture_stopped`. Probe runs add
`probe_playback_started` and `probe_playback_completed`. Applications may add
their own low-volume kinds via `record_event()`.

Consumers should switch on `schema_version` and `kind`, preserve sequence order,
and ignore unknown event kinds and detail keys. Status and event additions are
compatible for consumers that treat them as open sets; strict consumers should
explicitly handle the new degraded-ready, discontinuity, and echo-path-reset
events. Detail objects may grow within one event schema.

## `analysis.json`

- `tracks`: duration, RMS/peak dBFS, and clipping for every WAV.
- `loopback_to_raw_energy_alignment`: broad 10 ms envelope lag/correlation.
- `whole_run_raw_to_clean_level_change_db`: descriptive only; it may include
  real near-end speech.
- `far_end_only`: echo suppression for operator-declared far-end-only windows;
  the label is valid only when no near-end sound occurred.
- `near_end`: retained level only for explicitly declared near-end windows.

Positive `echo_suppression_db` means the clean track is lower than the raw mic
in that far-end-only window. `near_end_retained_db` near 0 means similar level;
it does not prove intelligibility or freedom from artifacts.

## Privacy and retention

Raw recordings can contain private speech, notifications, media, calls, or any
other audio routed to the selected endpoint. The default `captures/` tree and
general WAV/JSONL/log patterns are ignored by this repository; only the two
reviewed demo WAVs under `assets/` are explicitly exempt. Custom output paths
and JSON metadata are not guaranteed to be ignored. Nothing is encrypted or
anonymized. Check `git status` and treat the whole directory as private unless
reviewed.

Next: [Hardware probe](hardware-probe.md) · [Troubleshooting](troubleshooting.md)
