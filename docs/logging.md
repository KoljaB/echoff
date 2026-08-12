# Logging

[Documentation home](README.md)

Echoff uses Python's `echoff` logger hierarchy. Importing the package attaches
only a `NullHandler`; it never configures the root logger or installs an
emitting handler behind the application's back.

Applications may use ordinary `logging` configuration or the opt-in helper:

```python
from echoff.log import configure_logging

configure_logging(level="DEBUG", log_file="aec.log")
```

Repeated helper calls update Echoff-managed handlers instead of duplicating
them. They do not alter unrelated root handlers.

CLI behavior:

- `echoff record` logs to the console and `run.log` in its artifact directory.
- `echoff devices` and `echoff analyze` log to the console only.
- all commands accept `--log-level DEBUG|INFO|WARNING|ERROR`.

Human logs explain what happened. Prefer `events.jsonl`, `summary.json`, and
`analysis.json` for automation because their schemas and structured values are
more stable than prose.

Next: [Capture artifacts](capture-artifacts.md) · [Python API](python-api.md)
