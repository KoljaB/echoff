# Echoff documentation

[Back to the project README](../README.md)

Choose the shortest path that matches what you are trying to do.

## Start

- [Getting started](getting-started.md) — install the PyPI package on Windows,
  confirm devices, and record the first inspectable session.
- [Command-line interface](cli.md) — commands, flags, defaults, and exit codes.

## Integrate

- [Integration](integration.md) — choose an ownership model and handle
  callbacks, health, shutdown, and realignment.
- [Python API](python-api.md) — public classes, data contracts, configuration,
  buffering, and errors.
- [Logging](logging.md) — opt-in application logging without root-logger side
  effects.

## Validate and diagnose

- [Hardware probe](hardware-probe.md) — produce repeatable physical evidence.
- [Capture artifacts](capture-artifacts.md) — understand tracks, events,
  summaries, metrics, and privacy.
- [Troubleshooting](troubleshooting.md) — work from symptom to evidence to
  resolution.

## Understand the system

- [Architecture](architecture.md) — why timestamps matter and how pairing,
  WebRTC APM, and realignment interact.
- [Platform support](platforms.md) — built-in Windows capture and the portable
  processor boundary.

## Contribute and release

- [Contributing](../CONTRIBUTING.md) — change requirements and validation.
- [Development](development.md) — test layers and backend work.
- [Releasing](releasing.md) — immutable TestPyPI/PyPI release procedure.
- [Security](../SECURITY.md) — private-audio and vulnerability handling.
- [Changelog](../CHANGELOG.md) — version history and release status.

## Current support boundary

Echoff 0.1 is alpha software. Built-in physical capture is supported on
Windows. The Linux and macOS processor paths are designed for applications that
already own correctly aligned PCM and environments where LiveKit installs;
they are not CI- or hardware-qualified by this project. The repository
is licensed under the [MIT License](../LICENSE).
