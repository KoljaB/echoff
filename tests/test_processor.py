from __future__ import annotations

import math
import unittest
from typing import ClassVar

from echoff import AecConfig, WebRtcAecProcessor
from echoff.errors import AudioBackendError


class FakeFrame:
    def __init__(self, *, data: bytes, **_kwargs: object) -> None:
        self.data = bytearray(data)


class FakeRtc:
    AudioFrame = FakeFrame


class FakeApm:
    instances: ClassVar[list[FakeApm]] = []

    def __init__(self, **options: object) -> None:
        self.options = options
        self.delay_ms: int | None = None
        self.calls: list[str] = []
        type(self).instances.append(self)

    def set_stream_delay_ms(self, value: int) -> None:
        self.delay_ms = value

    def process_reverse_stream(self, _frame: FakeFrame) -> None:
        self.calls.append("reference")

    def process_stream(self, _frame: FakeFrame) -> None:
        self.calls.append("microphone")


class WebRtcAecProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeApm.instances.clear()

    def processor(self, **config_values: object) -> WebRtcAecProcessor:
        return WebRtcAecProcessor(
            AecConfig(**config_values),
            _rtc=FakeRtc,
            _apm_type=FakeApm,
        )

    def test_process_pair_keeps_reference_immediately_before_microphone(self) -> None:
        processor = self.processor()
        reference = [0.2] * 960
        microphone = [0.1] * 960

        output = processor.process_pair(reference, microphone)

        self.assertEqual(len(output), 960)
        self.assertEqual(
            FakeApm.instances[0].calls,
            ["reference", "microphone", "reference", "microphone"],
        )
        self.assertEqual(FakeApm.instances[0].delay_ms, 50)
        self.assertEqual(
            FakeApm.instances[0].options,
            {
                "echo_cancellation": True,
                "noise_suppression": False,
                "high_pass_filter": True,
                "auto_gain_control": False,
            },
        )

    def test_pair_requires_equal_complete_ten_millisecond_frames(self) -> None:
        processor = self.processor()
        with self.assertRaisesRegex(ValueError, "equal lengths"):
            processor.process_pair([0.0] * 480, [0.0] * 960)
        with self.assertRaisesRegex(ValueError, "whole 10 ms"):
            processor.process_pair([0.0] * 481, [0.0] * 481)

    def test_warmup_counts_only_paired_active_reference_and_reset_is_cold(self) -> None:
        processor = self.processor(echo_path_warmup_s=3.25)
        silent = [0.0] * 480
        below = [0.00099] * 480
        active = [0.001] * 480

        processor.process_pair(silent, silent)
        processor.process_pair(below, silent)
        self.assertEqual(processor.state.far_end_active_s, 0.0)

        for _index in range(324):
            processor.process_pair(active, silent)
        self.assertAlmostEqual(processor.state.far_end_active_s, 3.24)
        self.assertFalse(processor.state.echo_path_ready)

        processor.process_pair(active, silent)
        self.assertTrue(processor.state.echo_path_ready)
        self.assertEqual(processor.state.alignment_epoch, 0)

        first_apm = FakeApm.instances[-1]
        processor.reset_alignment()
        self.assertIsNot(FakeApm.instances[-1], first_apm)
        self.assertFalse(processor.state.echo_path_ready)
        self.assertEqual(processor.state.far_end_active_s, 0.0)
        self.assertEqual(processor.state.alignment_epoch, 1)
        self.assertEqual(processor.state.stream_alignment_reset_count, 1)

    def test_native_apm_reduces_synthetic_echo_and_retains_near_end(self) -> None:
        try:
            processor = WebRtcAecProcessor(AecConfig())
        except AudioBackendError as exc:
            self.skipTest(str(exc))
        rate = 48_000
        delay = round(rate * 0.05)
        total = rate * 3
        state = 7
        reference = []
        for _index in range(total):
            state = (1664525 * state + 1013904223) & 0xFFFFFFFF
            reference.append(((state / 0xFFFFFFFF) * 2.0 - 1.0) * 0.35)
        microphone = [0.0] * total
        for index in range(delay, total):
            microphone[index] = 0.65 * reference[index - delay]
        clean: list[float] = []
        for offset in range(0, total, 960):
            clean.extend(
                processor.process_pair(
                    reference[offset : offset + 960],
                    microphone[offset : offset + 960],
                )
            )

        def rms(values: list[float]) -> float:
            return math.sqrt(sum(value * value for value in values) / len(values))

        self.assertLess(rms(clean[2 * rate :]), 0.01 * rms(microphone[rate : 2 * rate]))

        near_processor = WebRtcAecProcessor(AecConfig())
        near = [0.08 * math.sin(2.0 * math.pi * 300 * index / rate) for index in range(total)]
        retained: list[float] = []
        for offset in range(0, total, 960):
            retained.extend(near_processor.process_pair([0.0] * 960, near[offset : offset + 960]))
        self.assertGreater(rms(retained[2 * rate :]), 0.40 * rms(near[2 * rate :]))


if __name__ == "__main__":
    unittest.main()
