from __future__ import annotations

import math
import unittest

from echoff.alignment import AlignmentAction, TimestampAligner
from echoff.errors import AudioBackendError
from echoff.models import AudioBlock


def block(ended: float) -> AudioBlock:
    return AudioBlock((0.0,) * 960, ended)


class TimestampAlignerTests(unittest.TestCase):
    def observe_pair(
        self,
        aligner: TimestampAligner,
        reference_end: float,
        microphone_end: float,
    ):
        reference = block(reference_end)
        microphone = block(microphone_end)
        aligner.observe("reference", reference)
        aligner.observe("microphone", microphone)
        return aligner.decide(reference, microphone)

    def test_startup_drops_older_heads_and_locks_on_first_matching_pair(self) -> None:
        aligner = TimestampAligner(0.010)
        references = [block(0.02), block(0.04), block(0.06)]
        microphone = block(0.06)
        for reference in references:
            aligner.observe("reference", reference)
        aligner.observe("microphone", microphone)

        first = aligner.decide(references[0], microphone)
        second = aligner.decide(references[1], microphone)
        third = aligner.decide(references[2], microphone)

        self.assertEqual(first.action, AlignmentAction.DROP_REFERENCE)
        self.assertEqual(second.action, AlignmentAction.DROP_REFERENCE)
        self.assertEqual(third.action, AlignmentAction.PAIR)
        self.assertTrue(third.locks_alignment)
        self.assertEqual(aligner.snapshot.initial_dropped_reference_blocks, 2)
        self.assertAlmostEqual(aligner.snapshot.first_callback_skew_s or 0.0, -0.04)

    def test_runtime_phase_jump_is_one_realigning_episode(self) -> None:
        aligner = TimestampAligner(0.010)
        self.assertTrue(self.observe_pair(aligner, 0.02, 0.02).locks_alignment)

        reference_04 = block(0.04)
        reference_06 = block(0.06)
        microphone_08 = block(0.08)
        aligner.observe("reference", reference_04)
        aligner.observe("reference", reference_06)
        aligner.observe("microphone", microphone_08)
        first = aligner.decide(reference_04, microphone_08)
        second = aligner.decide(reference_06, microphone_08)
        reference_08 = block(0.08)
        aligner.observe("reference", reference_08)
        recovered = aligner.decide(reference_08, microphone_08)

        self.assertTrue(first.starts_realigning)
        self.assertFalse(second.starts_realigning)
        self.assertTrue(recovered.completes_realigning)
        self.assertEqual(aligner.snapshot.runtime_realignments, 1)
        self.assertEqual(aligner.snapshot.runtime_mismatch_count, 2)
        self.assertEqual(aligner.snapshot.runtime_dropped_reference_blocks, 2)

    def test_tolerance_boundary_is_inclusive(self) -> None:
        aligner = TimestampAligner(0.010)
        decision = self.observe_pair(aligner, 1.0, 0.99)
        self.assertEqual(decision.action, AlignmentAction.PAIR)

    def test_rejects_nonfinite_and_nonadvancing_timestamps(self) -> None:
        aligner = TimestampAligner(0.010)
        with self.assertRaisesRegex(AudioBackendError, "non-finite"):
            aligner.observe("reference", block(math.nan))
        aligner.observe("microphone", block(1.0))
        with self.assertRaisesRegex(AudioBackendError, "did not advance"):
            aligner.observe("microphone", block(1.0))


if __name__ == "__main__":
    unittest.main()
