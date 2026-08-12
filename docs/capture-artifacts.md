# Capture artifacts

[Documentation home](README.md)

`AecCapture(output_dir=...)` creates or uses a diagnostic directory and
exclusively creates its reserved files. It tolerates unrelated existing files;
`echoff record` and the probe API are stricter and require a new or empty output
directory. Reserved files are never overwritten.

## Core capture files

| File | Format | Meaning |
|---|---|---|
| `computer_audio.wav` | PCM16 mono 48 kHz | Digital render reference captured from the selected output endpoint |
| `microphone_raw.wav` | PCM16 mono 48 kHz | Physical microphone before AEC |
| `microphone_aec.wav` | PCM16 mono 48 kHz | Microphone after WebRTC AEC |
| `events.jsonl` | UTF-8 JSON Lines | Ordered lifecycle, alignment, application, and error events |
| `config.json` | UTF-8 JSON | Effective public configuration and artifact schema |
| `summary.json` | UTF-8 JSON | Final status, devices, counters, durations, track metadata, and hashes |

The three WAVs share one sample timeline. An unmatched reference block is
retained in `computer_audio.wav`; zeros occupy the two microphone tracks. An
unmatched microphone block is retained in `microphone_raw.wav`; zeros occupy
the reference and clean tracks. Events and counters explain those intervals.

## Probe and CLI additions

| File | Created by | Meaning |
|---|---|---|
| `analysis.json` | `echoff record`, probe API, or direct analyzer write | Signal metrics and any declared far-/near-end windows |
| `run.log` | `echoff record` or an application that requests file logging | Human-readable log; `devices` and `analyze` do not create it automatically |

`echoff analyze` is read-only and prints JSON. Direct
`analyze_capture(..., write_report=True)` uses exclusive-create semantics.

## `summary.json`

Start with:

- `status` and `error`;
- selected backend/device name/index for reference and microphone;
- `tracks_share_timeline` and each track's sample count/hash;
- alignment lock, skew, drop, mismatch, realignment, and shutdown-tail counts;
- source device/silence/drop counters;
- AEC readiness, active far-end time, and reset count; and
- optional application/probe metadata.

Nonzero realignment is not automatically a failed run: it means Echoff detected
a discontinuity, reset the adaptive filter once for that episode, removed stale
heads, and resumed. Inspect the corresponding events and affected signal.

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
`alignment_realigning`, `alignment_recovered`, `capture_failed`, and
`capture_stopped`. Probe runs add `probe_playback_started` and
`probe_playback_completed`. Applications may add their own low-volume kinds via
`record_event()`.

Consumers should switch on `schema_version` and `kind`, preserve sequence order,
and ignore unknown detail keys. Detail objects may grow within one event schema.

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
