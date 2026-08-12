# Contributing

1. Create a virtual environment.
2. Install `.[dev]`. Windows capture dependencies are selected automatically.
3. Run `python -m unittest discover -s tests -v`.
4. Run `ruff check .` and `mypy src` before opening a change.

Behavioral changes to capture timing, alignment, APM ordering, warm-up, or
device fallback require both deterministic tests and a preserved hardware
probe directory. Do not replace physical evidence with a unit test.

New platform backends implement the `CaptureSource` protocol and must not add
platform branches to the processor or aligner.
