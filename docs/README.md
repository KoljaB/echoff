# Echoff documentation

[Back to the project README](../README.md)

Choose the shortest path that matches what you are trying to do.

## Start

- [Windows getting started](getting-started.md) — install the PyPI package,
  confirm devices, and record the first inspectable Windows session.
- [Linux getting started](getting-started-linux.md) — install PipeWire tools,
  select a sink monitor and microphone, and validate a Linux session.
- [Command-line interface](cli.md) — commands, flags, defaults, and exit codes.

## Integrate

- [Integration](integration.md) — choose an ownership model and handle
  callbacks, health, shutdown, and synchronization stalls.
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

- [Architecture](architecture.md) — why timestamps matter and how startup
  pairing, symmetric recovery, and WebRTC APM interact.
- [Platform support](platforms.md) — built-in Windows and Linux capture plus
  the portable processor boundary.

## Contribute and release

- [Contributing](../CONTRIBUTING.md) — change requirements and validation.
- [Development](development.md) — test layers and backend work.
- [Releasing](releasing.md) — immutable TestPyPI/PyPI release procedure.
- [Security](../SECURITY.md) — private-audio and vulnerability handling.
- [Changelog](../CHANGELOG.md) — version history and release status.

## Current support boundary

Echoff 0.2 is alpha software. Built-in physical capture is supported on
Windows and Linux. Linux live capture requires PipeWire command-line tools.
The macOS processor path is designed for applications that already own
correctly aligned PCM and environments where LiveKit installs, but it is not
CI- or hardware-qualified and built-in macOS capture is not implemented. The
repository is licensed under the [MIT License](../LICENSE).
