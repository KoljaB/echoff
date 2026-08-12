# Changelog

All notable changes will be documented here.

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
- Timestamp-based duplex stream alignment and runtime realignment.
- Windows WASAPI loopback and microphone capture.
- WAV, JSONL, and summary artifacts for physical diagnostics.
- Device listing and recording CLI.
