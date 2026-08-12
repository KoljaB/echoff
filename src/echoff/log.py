"""Opt-in logging configuration for applications and the CLI."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

LOGGER_NAME = "echoff"


def parse_log_level(value: str | int) -> int:
    if isinstance(value, int):
        return value
    level = logging.getLevelName(value.upper())
    if not isinstance(level, int):
        raise ValueError(f"invalid log level: {value!r}")
    return level


def configure_logging(
    *,
    level: str | int = "INFO",
    log_file: str | Path | None = None,
    console: bool = True,
) -> logging.Logger:
    """Configure only the ``echoff`` logger hierarchy.

    Repeated calls replace handlers installed by this function and never touch
    the process-wide root logger.
    """

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(parse_log_level(level))
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_echoff_managed", False):
            logger.removeHandler(handler)
            handler.close()
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if console:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        stream_handler._echoff_managed = True  # type: ignore[attr-defined]
        logger.addHandler(stream_handler)
    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler._echoff_managed = True  # type: ignore[attr-defined]
        logger.addHandler(file_handler)
    return logger


def shutdown_logging() -> None:
    """Close handlers previously installed by :func:`configure_logging`."""

    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, "_echoff_managed", False):
            logger.removeHandler(handler)
            handler.close()
