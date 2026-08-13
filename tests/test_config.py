from __future__ import annotations

import math
import unittest

from echoff import AecConfig


class AecConfigTests(unittest.TestCase):
    def test_defaults_match_the_validated_capture_contract(self) -> None:
        config = AecConfig()

        self.assertEqual(config.sample_rate, 48_000)
        self.assertEqual(config.block_samples, 960)
        self.assertEqual(config.apm_frame_samples, 480)
        self.assertEqual(config.stream_delay_ms, 50)
        self.assertEqual(config.echo_path_warmup_s, 7.5)
        self.assertEqual(config.far_end_active_rms_min, 0.001)
        self.assertEqual(config.echo_path_quality_window_s, 1.0)
        self.assertEqual(config.echo_path_quality_stable_s, 0.25)
        self.assertEqual(config.echo_path_min_suppression_db, 10.0)
        self.assertEqual(config.echo_path_quality_min_raw_rms, 0.003)

    def test_rejects_invalid_frame_and_alignment_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "sample_rate=48000"):
            AecConfig(sample_rate=16_000)
        with self.assertRaisesRegex(ValueError, "whole number"):
            AecConfig(block_duration_s=0.015)
        with self.assertRaisesRegex(ValueError, "half a capture block"):
            AecConfig(pair_tolerance_s=0.011)
        for field in (
            "block_duration_s",
            "pair_tolerance_s",
            "reference_stall_grace_s",
            "queue_fatal_s",
            "startup_timeout_s",
            "echo_path_warmup_s",
            "far_end_active_rms_min",
            "echo_path_quality_window_s",
            "echo_path_quality_stable_s",
            "echo_path_min_suppression_db",
            "echo_path_quality_min_raw_rms",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                AecConfig(**{field: math.nan})
        for value in (math.nan, math.inf, 1.5, True):
            with self.subTest(stream_delay_ms=value), self.assertRaisesRegex(
                ValueError, "stream_delay_ms"
            ):
                AecConfig(stream_delay_ms=value)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "unsupported backend"):
            AecConfig(backend="imaginary")


if __name__ == "__main__":
    unittest.main()
