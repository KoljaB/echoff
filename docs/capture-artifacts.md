# Capture artifacts

When `output_dir` is supplied, one run produces a self-contained diagnostic
directory.

| File | Format | Meaning |
|---|---|---|
| `computer_audio.wav` | PCM16 mono 48 kHz | Audio rendered on the selected system endpoint |
| `microphone_raw.wav` | PCM16 mono 48 kHz | Physical microphone before AEC |
| `microphone_aec.wav` | PCM16 mono 48 kHz | Microphone after WebRTC AEC |
| `events.jsonl` | UTF-8 JSON Lines | Ordered lifecycle, alignment, state, and error events |
| `config.json` | UTF-8 JSON | Effective public configuration and schema version |
| `summary.json` | UTF-8 JSON | Final devices, counters, durations, and artifact metadata |
| `analysis.json` | UTF-8 JSON | Reproducible signal metrics written by the probe or analyzer |
| `run.log` | UTF-8 text | Human-readable CLI log with the requested log level |

Unmatched microphone blocks are represented by equal-duration silence in the
clean WAV. This keeps its timeline aligned with `microphone_raw.wav` while
making the discontinuity explicit in `events.jsonl` and summary counters.

Raw recordings can contain private speech or application audio. They are
ignored by Git but are not encrypted.

## Event stability

Events contain a `schema_version`, `kind`, UTC timestamp, monotonic timestamp,
and a structured `details` object. Consumers should switch on `kind` and ignore
unknown detail keys.
