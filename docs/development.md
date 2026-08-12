# Development

[Documentation home](README.md)

## Set up a checkout

```powershell
git clone https://github.com/KoljaB/echoff.git
cd echoff
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Use the environment's executables explicitly or activate it before running
unqualified commands.

## Required local checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe src
.\.venv\Scripts\python.exe -m build
```

Run `python -m echoff --help` and any changed subcommand help when editing CLI
or user documentation. Build the wheel and inspect its README rendering before
a release.

## Test layers

1. Processor tests inject a fake APM and prove 10 ms ordering, buffering, reset,
   and warm-up behavior.
2. Aligner tests use deterministic timestamps and prove startup, tolerance, and
   runtime realignment.
3. Capture tests inject fake sources and verify callbacks, failure propagation,
   and equal artifact timelines.
4. Backend unit tests fake host APIs and never open physical devices.
5. Hardware probes open real devices and preserve WAV/JSON evidence.

Unit tests must be deterministic and require no speaker, microphone, network,
or model service. A unit test cannot replace physical evidence for a change to
device timing or echo behavior.

## Adding or changing a backend

Implement `CaptureSource`, return `DeviceInfo` rows from a device lister, and
add one factory branch. Backends must:

- emit fixed-size mono float blocks at the configured rate;
- supply finite, strictly increasing monotonic block-end timestamps;
- preserve clock-continuous silence rather than pausing callbacks;
- report real/synthetic/dropped block counters and selected device identity;
- stop and clean up both success and failure paths; and
- contain no WebRTC, VAD, ASR, TTS, recording, or application policy.

Before support is claimed on a new platform, preserve evidence for startup
alignment, runtime discontinuity recovery, far-end suppression, near-end
retention, device selection, disconnect/cleanup, and repeated cold starts.

## Documentation contract

Public behavior changes update the README route, relevant guide/reference page,
CLI help, and changelog in the same change. Copy-paste commands must use the
installed package unless explicitly labeled source-checkout-only.

Relative links inside `docs/` are fine. Links in the root README must be
absolute GitHub URLs because the same Markdown is rendered on PyPI.

Next: [Contributing](../CONTRIBUTING.md) · [Releasing](releasing.md) ·
[Architecture](architecture.md)
