from __future__ import annotations

import unittest

from echoff import AecConfig


class AecConfigTests(unittest.TestCase):
    def test_defaults_match_the_validated_capture_contract(self) -> None:
        config = AecConfig()

        self.assertEqual(config.sample_rate, 48_000)
        self.assertEqual(config.block_samples, 960)
        self.assertEqual(config.apm_frame_samples, 480)
        self.assertEqual(config.stream_delay_ms, 50)
        self.assertEqual(config.echo_path_warmup_s, 3.25)
        self.assertEqual(config.far_end_active_rms_min, 0.001)

    def test_rejects_invalid_frame_and_alignment_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "sample_rate=48000"):
            AecConfig(sample_rate=16_000)
        with self.assertRaisesRegex(ValueError, "whole number"):
            AecConfig(block_duration_s=0.015)
        with self.assertRaisesRegex(ValueError, "half a capture block"):
            AecConfig(pair_tolerance_s=0.011)
        with self.assertRaisesRegex(ValueError, "unsupported backend"):
            AecConfig(backend="imaginary")


if __name__ == "__main__":
    unittest.main()
