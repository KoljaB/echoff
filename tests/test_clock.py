from __future__ import annotations

import unittest

from echoff.clock import FixedBlockSampleClock


class FixedBlockSampleClockTests(unittest.TestCase):
    def make_clock(self) -> FixedBlockSampleClock:
        return FixedBlockSampleClock(sample_rate=48_000, block_samples=960)

    def test_independent_stream_origins_map_to_the_same_local_end(self) -> None:
        first = self.make_clock().push(
            [0.1] * 960,
            callback_monotonic=1_000.0,
            adc_start=500.0,
            current_time=500.02,
        )[0]
        second = self.make_clock().push(
            [0.2] * 960,
            callback_monotonic=1_000.0,
            adc_start=0.0,
            current_time=0.02,
        )[0]

        self.assertAlmostEqual(first.observed_end_monotonic or 0.0, 1_000.0)
        self.assertAlmostEqual(second.observed_end_monotonic or 0.0, 1_000.0)

    def test_partial_callbacks_preserve_samples_and_discontinuity(self) -> None:
        clock = self.make_clock()
        first = tuple(index / 1_000.0 for index in range(480))
        second = tuple(index / 1_000.0 for index in range(480, 960))

        self.assertEqual(
            clock.push(
                first,
                callback_monotonic=10.0,
                adc_start=4.0,
                current_time=4.01,
                status_flags=2,
                discontinuity=True,
            ),
            (),
        )
        blocks = clock.push(
            second,
            callback_monotonic=10.01,
            adc_start=4.01,
            current_time=4.02,
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].samples, first + second)
        self.assertTrue(blocks[0].discontinuity)
        self.assertEqual(blocks[0].status_flags, 2)

    def test_large_timestamp_jump_never_creates_a_sample_gap(self) -> None:
        clock = self.make_clock()
        blocks = []
        for sequence, observed_shift in enumerate((0.0, 28.0, 0.0)):
            blocks.extend(
                clock.push(
                    [float(sequence)] * 960,
                    callback_monotonic=100.0 + sequence * 0.02,
                    adc_start=50.0 + sequence * 0.02 + observed_shift,
                    current_time=50.02 + sequence * 0.02,
                )
            )

        self.assertEqual([block.sequence for block in blocks], [0, 1, 2])
        self.assertEqual([block.samples[0] for block in blocks], [0.0, 1.0, 2.0])
        self.assertAlmostEqual(blocks[1].ended_monotonic - blocks[0].ended_monotonic, 0.02)
        self.assertAlmostEqual(blocks[2].ended_monotonic - blocks[1].ended_monotonic, 0.02)
        self.assertGreaterEqual(clock.timestamp_anomaly_count, 1)
        self.assertFalse(blocks[1].timing_valid)

    def test_sustained_implausible_timestamp_mapping_stays_low_confidence(self) -> None:
        clock = self.make_clock()
        blocks = []
        for sequence in range(10):
            blocks.extend(
                clock.push(
                    [float(sequence)] * 960,
                    callback_monotonic=100.0 + sequence * 0.02,
                    adc_start=78.0 + sequence * 0.02,
                    current_time=50.02 + sequence * 0.02,
                )
            )

        self.assertEqual(len(blocks), 10)
        self.assertTrue(all(not item.timing_valid for item in blocks))
        self.assertEqual([item.sequence for item in blocks], list(range(10)))
        self.assertEqual(clock.timestamp_anomaly_count, 10)

    def test_flush_pads_only_the_final_partial_block(self) -> None:
        clock = self.make_clock()
        real = tuple(0.25 for _ in range(481))
        self.assertEqual(
            clock.push(
                real,
                callback_monotonic=3.0,
                adc_start=None,
                current_time=None,
            ),
            (),
        )
        invalid_before_flush = clock.invalid_timestamp_count

        block = clock.flush(callback_monotonic=3.1)[0]

        self.assertEqual(block.samples[:481], real)
        self.assertEqual(block.samples[481:], (0.0,) * 479)
        self.assertEqual(clock.padded_sample_count, 479)
        self.assertEqual(block.valid_samples, 481)
        self.assertEqual(clock.invalid_timestamp_count, invalid_before_flush)
        self.assertFalse(block.discontinuity)


if __name__ == "__main__":
    unittest.main()
