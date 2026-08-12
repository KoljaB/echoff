# Logging

The library uses Python's standard `logging` package under the `echoff`
logger hierarchy. It never installs handlers on import.

Applications can configure logging explicitly:

```python
from echoff.log import configure_logging

configure_logging(level="DEBUG", log_file="aec.log")
```

The CLI accepts `--log-level DEBUG|INFO|WARNING|ERROR` and writes both console
output and `run.log` in the capture directory.

Human logs explain what happened. `events.jsonl` and `summary.json` are the
machine-readable audit trail and should be preferred by automation.
