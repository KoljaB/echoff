# Changelog

All notable changes will be documented here.

## 0.1.4 - 2026-08-14

- Make received sample order authoritative and treat PortAudio timestamps as
  noisy long-term observations, preventing invented gaps and repeated APM
  resets when Windows revises its clock estimates.
- Preserve every raw source payload separately, emit processed output only for
  confirmed pairs, and keep the established sequence mapping authoritative.
- Wait symmetrically for either temporarily missing source for up to three
  seconds. Stalls inside that reserve recover without discarding payloads or
  resetting APM; count every wait and emit detailed events for exceptional
  episodes.
- Keep the Windows PortAudio callbacks constant-time, move decoding and
  fixed-block conversion to losslessly drained worker threads, and use
  symmetric 100 ms native packets while preserving the 20 ms processing API.
- Report callback packet and payload totals, queue high-water and age,
  enqueue time, callback-timeline drift, synchronization backlog, and recovery
  counters through runtime telemetry and capture artifacts.
- After a source failure or an expired reserve, suspend unsafe live AEC output,
  preserve received source artifacts, and report bounded live-buffer retirement
  explicitly instead of synthesizing a reference or directly aborting the host.
- Version the expanded, incompatible capture artifact contract as v2.
- Print critical capture and reference-alignment diagnostics to `stderr` by
  default, in red on interactive terminals, with an `AecCapture`
  `console_diagnostics=False` opt-out.
- Add exact padding, internal-backlog, rejoin, discontinuity, and 30-minute
  dual-clock regression coverage, including slow consumers and lossless
  shutdown drain.

## 0.1.3 - 2026-08-12

- License Echoff under the MIT License and publish the license in package
  metadata and distributions.

## 0.1.2 - 2026-08-12 (TestPyPI only)

- Rebuild the README and documentation around PyPI-first onboarding, clear API
  ownership routes, hardware validation, troubleshooting, and a searchable
  documentation index.
- Make `echoff analyze` read-only so it can inspect an existing probe without
  colliding with its preserved `analysis.json`.
- Propagate `python -m echoff` exit codes and asynchronous capture failures from
  a normally exiting context manager.
- Reject non-finite configuration, probe timing, and analysis-window values.
- Add complete CLI help, release instructions, and package API contracts.
- Prevent periodic WASAPI loopback-reference holes by reading device blocks on
  a dedicated blocking thread, retaining a late active block's original clock
  slot for a bounded grace period, and only then classifying the endpoint idle.

## 0.1.1 - 2026-08-12

- Add application-owned streaming and buffered paired processor APIs.
- Expose every reference block to host applications without duplicating capture.
- Anchor probe playback windows to the actual WAV sample timeline.

## 0.1.0 - Internal development version (not published)

- Initial standalone package structure.
- WebRTC APM processor with an atomic paired-frame API.
- Timestamp-based duplex startup alignment and skew telemetry.
- Windows WASAPI loopback and microphone capture.
- WAV, JSONL, and summary artifacts for physical diagnostics.
- Device listing and recording CLI.
