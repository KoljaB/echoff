from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from echoff.cli import build_parser, main
from echoff.log import configure_logging, parse_log_level, shutdown_logging


class LoggingAndCliTests(unittest.TestCase):
    def tearDown(self) -> None:
        logger = logging.getLogger("echoff")
        for handler in list(logger.handlers):
            if getattr(handler, "_echoff_managed", False):
                logger.removeHandler(handler)
                handler.close()

    def test_logging_is_scoped_and_reconfiguration_does_not_duplicate_handlers(self) -> None:
        root = logging.getLogger()
        root_handlers = list(root.handlers)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.log"
            first = configure_logging(level="DEBUG", log_file=path, console=False)
            second = configure_logging(level="INFO", log_file=path, console=False)
            second.info("ready")
            for handler in second.handlers:
                handler.flush()
            self.assertIs(first, second)
            managed = [
                handler for handler in second.handlers if getattr(handler, "_echoff_managed", False)
            ]
            self.assertEqual(len(managed), 1)
            self.assertEqual(root_handlers, list(root.handlers))
            self.assertIn("ready", path.read_text(encoding="utf-8"))
            shutdown_logging()

    def test_log_level_and_cli_validation(self) -> None:
        self.assertEqual(parse_log_level("warning"), logging.WARNING)
        with self.assertRaisesRegex(ValueError, "invalid log level"):
            parse_log_level("chatty")
        parser = build_parser()
        args = parser.parse_args(["record", "--duration", "5", "--log-level", "debug"])
        self.assertEqual(args.command, "record")
        self.assertEqual(args.duration, 5.0)
        self.assertEqual(args.log_level, "DEBUG")

    def test_analyze_cli_is_read_only_and_prints_report(self) -> None:
        report = {"schema_version": "echoff-analysis-v1"}
        output = StringIO()
        with (
            patch("echoff.cli.analyze_capture", return_value=report) as analyze,
            redirect_stdout(output),
        ):
            self.assertEqual(main(["analyze", "capture"]), 0)
        analyze.assert_called_once_with(
            Path("capture"),
            far_end_windows=[],
            near_end_windows=[],
            write_report=False,
        )
        self.assertEqual(__import__("json").loads(output.getvalue()), report)

    def test_module_entrypoint_propagates_main_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing-capture"
            result = subprocess.run(
                [sys.executable, "-m", "echoff", "analyze", str(missing)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
