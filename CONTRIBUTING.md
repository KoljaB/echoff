# Contributing

Thank you for improving Echoff. This project treats audio timing and preserved
physical evidence as part of correctness, not as implementation detail.

## Start

```powershell
git clone https://github.com/KoljaB/echoff.git
cd echoff
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Before opening a change, run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe src
.\.venv\Scripts\python.exe -m build
```

## Change rules

- Keep capture, alignment, processing, recording, analysis, and CLI concerns in
  separate modules.
- Preserve atomic `process_pair(reference, microphone)` ordering.
- Never mix system audio into the microphone before AEC.
- Keep VAD, ASR, TTS, and conversation policy out of the package.
- Runtime timestamp discontinuities realign and reset APM once per episode;
  they do not silently pair stale frames.
- Libraries do not configure the root logger.
- Add deterministic tests for every behavior change.
- Update public docs, CLI help, and changelog when the user contract changes.

## Hardware-sensitive changes

Changes to device I/O, timestamps, queue/drop behavior, alignment, APM framing,
stream delay, warm-up, or fallback require a preserved hardware probe in
addition to unit tests. Freeze devices, volume, stimulus, geometry, settings,
and decision metrics before comparing runs. Do not submit private raw audio to a
public issue without review.

New platform backends implement the `CaptureSource` protocol and must pass the
acceptance areas in [Development](docs/development.md) before support is claimed.

## Scope and licensing

Avoid unrelated cleanup in a focused change. Echoff is licensed under the MIT
License. By contributing, you agree that your contribution may be distributed
under that license.

See the [documentation index](docs/README.md), [architecture](docs/architecture.md),
and [security guidance](SECURITY.md).
