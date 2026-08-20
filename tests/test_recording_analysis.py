from __future__ import annotations

import json
import math
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from echoff import AecConfig, AudioBackendError
from echoff.analysis import analyze_capture
from echoff.models import CaptureStatus
from echoff.probe import (
    ANALYSIS_EDGE_TRIM_S,
    PLAYBACK_WINDOW_KIND,
    ProbeConfig,
    run_probe,
)
from echoff.recording import CaptureArtifacts


def empty_status() -> CaptureStatus:
    values = {}
    for name, field in CaptureStatus.__dataclass_fields__.items():
        if name in {"running", "alignment_locked", "echo_path_ready"}:
            values[name] = False
        elif field.type in {str, str | None}:
            values[name] = None
        else:
            values[name] = 0
    values.update(
        reference_backend="test",
        microphone_backend="test",
        reference_device_name=None,
        reference_device_index=None,
        microphone_device_name=None,
        microphone_device_index=None,
        last_mismatch_ms=None,
        first_callback_skew_ms=None,
        error=None,
    )
    return CaptureStatus(**values)


class RecordingAndAnalysisTests(unittest.TestCase):
    def test_probe_rejects_nonfinite_timing_without_opening_devices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            for field in ("duration_s", "pre_roll_s", "gap_s", "tail_s"):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    ProbeConfig(output_dir=output, **{field: math.nan})

    def test_probe_window_contract_is_explicitly_process_timed(self) -> None:
        self.assertEqual(
            PLAYBACK_WINDOW_KIND,
            "ffplay_process_lifetime_on_confirmed_pair_timeline",
        )
        self.assertEqual(ANALYSIS_EDGE_TRIM_S, 0.25)

    def test_artifacts_are_exclusive_and_analysis_measures_declared_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            artifacts = CaptureArtifacts(output, AecConfig())
            artifacts.write_pair([0.2] * 48_000, [0.1] * 48_000, [0.01] * 48_000)
            artifacts.finalize(
                status_name="completed",
                capture_status=empty_status(),
                started_utc="2026-01-01T00:00:00.000Z",
                ended_utc="2026-01-01T00:00:01.000Z",
                duration_s=1.0,
                error=None,
                metadata={},
                timeline_started_monotonic=123.0,
            )

            report = analyze_capture(
                output,
                far_end_windows=[(0.0, 1.0)],
                near_end_windows=[(0.0, 1.0)],
            )

            self.assertAlmostEqual(report["far_end_only"]["echo_suppression_db"], 20.0, places=1)
            self.assertAlmostEqual(report["near_end"]["near_end_retained_db"], -20.0, places=1)
            self.assertTrue((output / "analysis.json").is_file())
            with self.assertRaises(FileExistsError):
                analyze_capture(output)
            with self.assertRaisesRegex(ValueError, "outside the shared processed timeline"):
                analyze_capture(
                    output,
                    far_end_windows=[(0.0, 999.0)],
                    write_report=False,
                )
            with self.assertRaisesRegex(ValueError, "invalid analysis window"):
                analyze_capture(
                    output,
                    near_end_windows=[(-1.0, 0.5)],
                    write_report=False,
                )
            read_only_report = analyze_capture(
                output,
                far_end_windows=[(0.0, 1.0)],
                write_report=False,
            )
            self.assertAlmostEqual(
                read_only_report["far_end_only"]["echo_suppression_db"],
                20.0,
                places=1,
            )
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["schema_version"], "echoff-capture-artifacts-v2")
            self.assertTrue(summary["tracks_share_timeline"])
            with self.assertRaises(FileExistsError):
                CaptureArtifacts(output, AecConfig())

    def test_raw_reference_does_not_advance_the_confirmed_pair_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            artifacts = CaptureArtifacts(output, AecConfig())
            artifacts.write_reference_received([0.2] * 960)
            artifacts.write_pair([0.0] * 960, [0.1] * 960, [0.05] * 960)
            self.assertEqual(artifacts.reference_received.sample_count, 960)
            self.assertEqual(artifacts.computer_audio.sample_count, 960)
            self.assertEqual(artifacts.microphone_raw.sample_count, 960)
            self.assertEqual(artifacts.microphone_aec.sample_count, 960)
            artifacts.close_tracks()
            artifacts.events.close()

    def test_close_tracks_retries_only_the_recorder_that_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            artifacts = CaptureArtifacts(output, AecConfig())
            artifacts.write_pair([0.2] * 960, [0.1] * 960, [0.01] * 960)
            failed_close = artifacts.microphone_raw.close
            successful_close = artifacts.computer_audio.close
            failed_close_calls = 0
            successful_close_calls = 0

            def fail_once() -> None:
                nonlocal failed_close_calls
                failed_close_calls += 1
                if failed_close_calls == 1:
                    raise RuntimeError("transient microphone recorder close")
                failed_close()

            def track_successful_close() -> None:
                nonlocal successful_close_calls
                successful_close_calls += 1
                successful_close()

            try:
                with (
                    patch.object(
                        artifacts.microphone_raw,
                        "close",
                        side_effect=fail_once,
                    ),
                    patch.object(
                        artifacts.computer_audio,
                        "close",
                        side_effect=track_successful_close,
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "transient microphone recorder close",
                    ):
                        artifacts.close_tracks()
                    artifacts.close_tracks()

                self.assertEqual(failed_close_calls, 2)
                self.assertEqual(successful_close_calls, 1)
                self.assertTrue(artifacts.microphone_raw._closed)
                self.assertTrue(artifacts.computer_audio._closed)
            finally:
                if not artifacts.microphone_raw._closed:
                    failed_close()
                artifacts.events.close()

    def test_finalize_retries_atomic_summary_without_rewriting_completed_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            artifacts = CaptureArtifacts(output, AecConfig())
            artifacts.write_pair([0.2] * 960, [0.1] * 960, [0.01] * 960)
            arguments = {
                "status_name": "completed",
                "capture_status": empty_status(),
                "started_utc": "2026-01-01T00:00:00.000Z",
                "ended_utc": "2026-01-01T00:00:01.000Z",
                "duration_s": 1.0,
                "error": None,
                "metadata": {},
                "timeline_started_monotonic": 123.0,
            }
            unrelated_temp = output / ".unrelated.tmp"
            unrelated_temp.write_text("preserve", encoding="utf-8")

            with (
                patch(
                    "echoff.recording.os.rename",
                    side_effect=OSError("transient summary rename"),
                ),
                self.assertRaisesRegex(OSError, "transient summary rename"),
            ):
                artifacts.finalize(**arguments)

            self.assertFalse((output / ".summary.json.tmp").exists())
            self.assertEqual(unrelated_temp.read_text(encoding="utf-8"), "preserve")

            summary_path = artifacts.finalize(**arguments)
            summary_bytes = summary_path.read_bytes()
            with wave.open(str(output / "computer_audio.wav"), "rb") as source:
                self.assertEqual(source.getnframes(), 960)

            immutable_arguments = dict(arguments)
            immutable_arguments.update(
                status_name="failed",
                ended_utc="2026-01-01T00:00:02.000Z",
                error="later error",
            )
            self.assertEqual(artifacts.finalize(**immutable_arguments), summary_path)
            self.assertEqual(summary_path.read_bytes(), summary_bytes)

    def test_probe_rejects_a_nonempty_output_before_device_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            output.mkdir()
            (output / "private.txt").write_text("preserve me", encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "will not be overwritten"):
                run_probe(ProbeConfig(output_dir=output))

            self.assertEqual((output / "private.txt").read_text(encoding="utf-8"), "preserve me")

    def test_probe_rejects_an_incomplete_capture_after_preserving_summary(self) -> None:
        class IncompleteCapture:
            def __init__(self, config, *, output_dir, **_kwargs) -> None:
                self.config = config
                self.output_dir = output_dir
                self.timeline_started_monotonic = 1.0

            def start(self) -> None:
                pass

            def raise_if_failed(self) -> None:
                pass

            def set_summary_metadata(self, **_metadata) -> None:
                pass

            def stop(self, **_kwargs) -> None:
                (self.output_dir / "summary.json").write_text(
                    json.dumps({"status": "incomplete"}),
                    encoding="utf-8",
                )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            with (
                patch("echoff.probe.AecCapture", IncompleteCapture),
                patch("echoff.probe._wait"),
                self.assertRaisesRegex(AudioBackendError, "status='incomplete'"),
            ):
                run_probe(ProbeConfig(output_dir=output, duration_s=0.001))

            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
