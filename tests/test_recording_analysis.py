from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from echoff import AecConfig
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
        self.assertEqual(PLAYBACK_WINDOW_KIND, "ffplay_process_lifetime")
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
            with self.assertRaisesRegex(ValueError, "outside the shared microphone timeline"):
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
            self.assertTrue(summary["tracks_share_timeline"])
            with self.assertRaises(FileExistsError):
                CaptureArtifacts(output, AecConfig())

    def test_unmatched_blocks_keep_three_tracks_on_one_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            artifacts = CaptureArtifacts(output, AecConfig())
            artifacts.write_unmatched_reference([0.2] * 960)
            artifacts.write_unmatched_microphone([0.1] * 960)
            self.assertEqual(artifacts.computer_audio.sample_count, 1_920)
            self.assertEqual(artifacts.microphone_raw.sample_count, 1_920)
            self.assertEqual(artifacts.microphone_aec.sample_count, 1_920)
            artifacts.close_tracks()
            artifacts.events.close()

    def test_probe_rejects_a_nonempty_output_before_device_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            output.mkdir()
            (output / "private.txt").write_text("preserve me", encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "will not be overwritten"):
                run_probe(ProbeConfig(output_dir=output))

            self.assertEqual((output / "private.txt").read_text(encoding="utf-8"), "preserve me")


if __name__ == "__main__":
    unittest.main()
