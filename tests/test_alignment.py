from __future__ import annotations

import unittest
from collections import deque

from echoff.alignment import AdaptiveReferenceAligner, AlignmentMode
from echoff.models import AudioBlock


def block(
    sequence: int,
    ended: float,
    *,
    discontinuity: bool = False,
) -> AudioBlock:
    return AudioBlock(
        (float(sequence),) * 960,
        ended,
        sequence=sequence,
        callback_monotonic=ended,
        observed_end_monotonic=ended,
        discontinuity=discontinuity,
    )


class AdaptiveReferenceAlignerTests(unittest.TestCase):
    def locked_aligner(self) -> AdaptiveReferenceAligner:
        aligner = AdaptiveReferenceAligner(0.020, 0.010)
        for sequence in range(3):
            aligner.observe_microphone(block(sequence, 1.000 + sequence * 0.020))
        aligner.ingest_reference(block(0, 1.000))
        aligner.ingest_reference(block(1, 1.020))
        update = aligner.ingest_reference(block(2, 1.040))
        self.assertTrue(update.locks_alignment)
        self.assertEqual([item.slot for item in update.mapped], [0, 1, 2])
        return aligner

    def test_join_requires_three_consistent_observations(self) -> None:
        aligner = AdaptiveReferenceAligner(0.020, 0.010)
        for sequence in range(3):
            aligner.observe_microphone(block(sequence, 10.000 + sequence * 0.020))
        self.assertFalse(aligner.ingest_reference(block(0, 10.006)).mapped)
        self.assertFalse(aligner.ingest_reference(block(1, 10.026)).mapped)
        update = aligner.ingest_reference(block(2, 10.046))
        self.assertTrue(update.locks_alignment)
        self.assertEqual([item.slot for item in update.mapped], [0, 1, 2])
        self.assertEqual(aligner.mode, AlignmentMode.LOCKED)

    def test_startup_join_uses_newest_contiguous_microphone_tail(self) -> None:
        aligner = AdaptiveReferenceAligner(0.020, 0.010)
        microphones = tuple(
            block(sequence, 1.000 + sequence * 0.020)
            for sequence in range(83)
        )
        aligner.observe_microphone(microphones[0])
        self.assertTrue(aligner.confirm_microphone_phase(microphones))
        self.assertEqual(aligner.join_validation_microphone_sequence, 82)

        updates = [
            aligner.ingest_reference(
                block(sequence, 1.000 + (80 + sequence) * 0.020)
            )
            for sequence in range(3)
        ]

        self.assertTrue(updates[-1].locks_alignment)
        self.assertEqual(aligner.reference_offset, 80)
        self.assertEqual([item.slot for item in updates[-1].mapped], [80, 81, 82])

    def test_startup_join_rejects_reference_candidates_past_safe_microphone_tail(self) -> None:
        aligner = AdaptiveReferenceAligner(0.020, 0.010)
        microphones = tuple(
            block(sequence, 1.000 + sequence * 0.020)
            for sequence in range(83)
        )
        aligner.observe_microphone(microphones[0])
        self.assertTrue(aligner.confirm_microphone_phase(microphones))

        updates = [
            aligner.ingest_reference(
                block(sequence, 1.000 + (87 + sequence) * 0.020)
            )
            for sequence in range(3)
        ]

        self.assertFalse(any(update.locks_alignment for update in updates))
        self.assertEqual(aligner.mode, AlignmentMode.JOINING)
        self.assertIsNone(aligner.reference_offset)

    def test_startup_join_does_not_bridge_a_queued_microphone_barrier(self) -> None:
        aligner = AdaptiveReferenceAligner(0.020, 0.010)
        microphones = (
            *(
                block(sequence, 1.000 + sequence * 0.020)
                for sequence in range(83)
            ),
            block(83, 2.660, discontinuity=True),
            block(84, 2.680),
            block(85, 2.700),
        )
        aligner.observe_microphone(microphones[0])
        self.assertTrue(aligner.confirm_microphone_phase(microphones))
        self.assertEqual(aligner.join_validation_microphone_sequence, 82)
        self.assertTrue(aligner.join_validation_microphone_barrier)

        updates = [
            aligner.ingest_reference(
                block(sequence, 1.000 + (84 + sequence) * 0.020)
            )
            for sequence in range(3)
        ]

        self.assertFalse(any(update.locks_alignment for update in updates))
        self.assertEqual(aligner.mode, AlignmentMode.JOINING)
        self.assertIsNone(aligner.reference_offset)

    def test_observed_windows_flap_signature_never_changes_pair_identity(self) -> None:
        aligner = self.locked_aligner()
        corrections = []
        for sequence in range(3, 10_003):
            deviation = 0.0
            if sequence % 14 == 0:
                deviation = 0.014222609 if (sequence // 14) % 2 else -0.025777391
            update = aligner.ingest_reference(
                block(sequence, 1.0 + sequence * 0.020 + deviation)
            )
            corrections.append(update.correction_slots)
        self.assertEqual(set(corrections), {0})
        self.assertEqual(aligner.snapshot.clock_correction_count, 0)
        self.assertEqual(aligner.snapshot.hard_discontinuity_count, 0)

    def test_alternating_real_trace_observations_never_flap_mapping(self) -> None:
        aligner = self.locked_aligner()
        mapped_slots = []
        for sequence in range(3, 2_003):
            residual = 0.014222609 if sequence % 2 else -0.025777391
            update = aligner.ingest_reference(
                block(sequence, 1.0 + sequence * 0.020 + residual)
            )
            mapped_slots.extend(item.slot for item in update.mapped)
            for mapped in update.mapped:
                aligner.note_pair(
                    mapped.block,
                    block(mapped.slot, 1.0 + mapped.slot * 0.020),
                )

        self.assertEqual(mapped_slots, list(range(3, 2_003)))
        self.assertEqual(aligner.snapshot.clock_correction_count, 0)
        self.assertEqual(aligner.snapshot.hard_discontinuity_count, 0)

    def test_persistent_timestamp_step_is_not_mistaken_for_clock_drift(self) -> None:
        aligner = self.locked_aligner()
        updates = [
            aligner.ingest_reference(block(sequence, 1.0 + sequence * 0.020 + 0.020))
            for sequence in range(3, 603)
        ]
        committed = [update.correction_slots for update in updates if update.correction_slots]
        self.assertEqual(committed, [])
        mapped = [item.slot for update in updates for item in update.mapped]
        self.assertEqual(mapped, list(range(3, 603)))
        self.assertEqual(aligner.snapshot.clock_correction_count, 0)

    def test_short_timestamp_step_is_observed_but_never_committed(self) -> None:
        aligner = self.locked_aligner()
        corrections = []
        for sequence in range(3, 303):
            step = 0.020 if 100 <= sequence < 105 else 0.0
            update = aligner.ingest_reference(
                block(sequence, 1.0 + sequence * 0.020 + step)
            )
            corrections.append(update.correction_slots)

        self.assertEqual(set(corrections), {0})
        self.assertEqual(aligner.snapshot.clock_correction_count, 0)
        self.assertEqual(aligner.mode, AlignmentMode.LOCKED)

    def test_twenty_minute_clock_drift_never_changes_pair_identity(self) -> None:
        for microphone_ppm, reference_ppm in ((100.0, 0.0), (0.0, 100.0)):
            with self.subTest(
                microphone_ppm=microphone_ppm,
                reference_ppm=reference_ppm,
            ):
                aligner = AdaptiveReferenceAligner(0.020, 0.010)
                microphone_by_slot: dict[int, AudioBlock] = {}
                reference_by_slot: dict[int, AudioBlock] = {}
                correction_directions: list[int] = []
                for sequence in range(60_000):
                    microphone = block(
                        sequence,
                        1.0 + sequence * 0.020 * (1.0 + microphone_ppm / 1_000_000.0),
                    )
                    reference = block(
                        sequence,
                        1.0 + sequence * 0.020 * (1.0 + reference_ppm / 1_000_000.0),
                    )
                    microphone_by_slot[sequence] = microphone
                    aligner.observe_microphone(microphone)
                    update = aligner.ingest_reference(reference)
                    if update.correction_slots:
                        correction_directions.append(update.correction_slots)
                    for mapped in update.mapped:
                        reference_by_slot[mapped.slot] = mapped.block
                    ready = set(microphone_by_slot).intersection(reference_by_slot)
                    for slot in sorted(ready):
                        aligner.note_pair(
                            reference_by_slot.pop(slot),
                            microphone_by_slot.pop(slot),
                        )

                self.assertEqual(aligner.snapshot.clock_correction_count, 0)
                self.assertEqual(correction_directions, [])
                self.assertEqual(aligner.snapshot.hard_discontinuity_count, 0)

    def test_fast_monotonic_pair_skew_is_diagnostic_only(self) -> None:
        aligner = self.locked_aligner()
        correction_directions: list[int] = []
        for sequence in range(3, 3_003):
            elapsed = sequence * 0.020
            flap = 0.0
            if sequence % 14 == 0:
                flap = 0.020 if (sequence // 14) % 2 else -0.020
            update = aligner.ingest_reference(
                block(sequence, 1.0 + elapsed + elapsed * 0.0025 + flap)
            )
            if update.correction_slots:
                correction_directions.append(update.correction_slots)
            for mapped in update.mapped:
                aligner.note_pair(
                    mapped.block,
                    block(mapped.slot, 1.0 + mapped.slot * 0.020),
                )

        self.assertEqual(aligner.snapshot.clock_correction_count, 0)
        self.assertEqual(correction_directions, [])
        self.assertEqual(aligner.snapshot.hard_discontinuity_count, 0)

    def test_thirty_minute_dual_clock_trace_preserves_sequence_identity(self) -> None:
        duration_s = 1_800.0
        block_s = 0.020
        phase_s = -0.005777391

        for microphone_ppm, reference_ppm in (
            (-200.0, 200.0),
            (200.0, -200.0),
        ):
            with self.subTest(
                microphone_ppm=microphone_ppm,
                reference_ppm=reference_ppm,
            ):
                aligner = AdaptiveReferenceAligner(block_s, 0.010)
                microphone_period = block_s * (1.0 + microphone_ppm / 1_000_000.0)
                reference_period = block_s * (1.0 + reference_ppm / 1_000_000.0)
                microphone_count = int(duration_s / microphone_period)
                reference_count = int(duration_s / reference_period)
                pending_microphones: deque[AudioBlock] = deque()
                references_by_slot: dict[int, AudioBlock] = {}
                seen_reference_sequences: set[int] = set()
                correction_directions: list[int] = []
                paired = 0
                zero_reference = 0
                late_reference = 0
                microphone_sequence = 0
                reference_sequence = 0

                def apply(
                    update,
                    correction_directions=correction_directions,
                    references_by_slot=references_by_slot,
                    seen_reference_sequences=seen_reference_sequences,
                ) -> None:
                    self.assertFalse(update.hard_discontinuity)
                    self.assertFalse(update.unmapped)
                    if update.correction_slots:
                        correction_directions.append(update.correction_slots)
                    for mapped in update.mapped:
                        self.assertNotIn(mapped.slot, references_by_slot)
                        self.assertNotIn(mapped.block.sequence, seen_reference_sequences)
                        references_by_slot[mapped.slot] = mapped.block
                        seen_reference_sequences.add(mapped.block.sequence)

                def drain(
                    *,
                    final: bool = False,
                    pending_microphones=pending_microphones,
                    references_by_slot=references_by_slot,
                    aligner=aligner,
                ) -> None:
                    nonlocal paired, zero_reference, late_reference
                    while pending_microphones:
                        slot = pending_microphones[0].sequence
                        for stale in sorted(key for key in references_by_slot if key < slot):
                            references_by_slot.pop(stale)
                            aligner.note_late_reference()
                            late_reference += 1
                        reference = references_by_slot.pop(slot, None)
                        if reference is not None:
                            microphone = pending_microphones.popleft()
                            aligner.note_pair(reference, microphone)
                            paired += 1
                            continue
                        if final or (references_by_slot and min(references_by_slot) > slot):
                            pending_microphones.popleft()
                            zero_reference += 1
                            continue
                        break

                while (
                    microphone_sequence < microphone_count
                    or reference_sequence < reference_count
                ):
                    microphone_time = (
                        float("inf")
                        if microphone_sequence >= microphone_count
                        else (microphone_sequence + 1) * microphone_period
                    )
                    reference_time = (
                        float("inf")
                        if reference_sequence >= reference_count
                        else (reference_sequence + 1) * reference_period
                    )
                    if microphone_time <= reference_time:
                        microphone = block(microphone_sequence, microphone_time)
                        pending_microphones.append(microphone)
                        apply(aligner.observe_microphone(microphone))
                        microphone_sequence += 1
                    else:
                        flap = 0.0
                        if reference_sequence and reference_sequence % 14 == 0:
                            flap = (
                                0.020
                                if (reference_sequence // 14) % 2
                                else -0.020
                            )
                        reference = block(
                            reference_sequence,
                            reference_time + phase_s + flap,
                        )
                        apply(aligner.ingest_reference(reference))
                        reference_sequence += 1
                    drain()

                drain(final=True)
                pending = aligner.drain_pending_references()
                aligner.note_shutdown_unpaired(
                    "reference", len(references_by_slot) + len(pending)
                )
                shutdown_reference = len(references_by_slot) + len(pending)

                self.assertEqual(paired + zero_reference, microphone_count)
                self.assertEqual(
                    paired + late_reference + shutdown_reference,
                    reference_count,
                )
                self.assertEqual(correction_directions, [])
                self.assertEqual(aligner.snapshot.clock_correction_count, 0)
                self.assertEqual(aligner.snapshot.hard_discontinuity_count, 0)
                self.assertEqual(aligner.mode, AlignmentMode.LOCKED)

    def test_mic_discontinuity_reanchors_the_master_clock_once(self) -> None:
        aligner = self.locked_aligner()
        microphones = (
            block(3, 1.120, discontinuity=True),
            block(4, 1.140),
            block(5, 1.160),
        )
        update = aligner.observe_microphone(microphones[0])
        self.assertTrue(update.hard_discontinuity)
        self.assertTrue(aligner.begin_hard_discontinuity())
        self.assertTrue(aligner.confirm_microphone_phase(microphones))
        for sequence in (3, 4):
            aligner.ingest_reference(block(sequence + 3, 1.120 + (sequence - 3) * 0.020))
        joined = aligner.ingest_reference(block(8, 1.160))

        self.assertEqual([item.slot for item in joined.mapped], [3, 4, 5])
        self.assertEqual(aligner.snapshot.hard_discontinuity_count, 1)

    def test_phase_lookahead_stops_before_a_future_discontinuity(self) -> None:
        aligner = AdaptiveReferenceAligner(0.020, 0.010)
        pending = (
            block(0, 1.000),
            block(1, 1.020),
            block(2, 2.040, discontinuity=True),
            block(3, 2.060),
            block(4, 2.080),
        )

        self.assertFalse(aligner.confirm_microphone_phase(pending))
        updates = [aligner.ingest_reference(block(i, 1.0 + i * 0.020)) for i in range(3)]
        self.assertFalse(any(update.locks_alignment for update in updates))

    def test_degraded_state_does_not_rearm_a_hard_discontinuity_episode(self) -> None:
        aligner = self.locked_aligner()
        first = aligner.ingest_reference(block(3, 1.060, discontinuity=True))
        self.assertTrue(first.hard_discontinuity)
        self.assertTrue(aligner.begin_hard_discontinuity())
        aligner.mark_synchronization_degraded()
        resets = []
        for sequence in (4, 5, 6):
            aligner.ingest_reference(
                block(sequence, 1.0 + sequence * 0.020, discontinuity=True)
            )
            resets.append(aligner.begin_hard_discontinuity())
        self.assertEqual(resets, [False, False, False])
        self.assertEqual(aligner.snapshot.hard_discontinuity_count, 1)

    def test_hard_discontinuity_opens_one_reset_episode(self) -> None:
        aligner = self.locked_aligner()
        first = aligner.ingest_reference(block(3, 1.060, discontinuity=True))
        self.assertTrue(first.hard_discontinuity)
        self.assertTrue(aligner.begin_hard_discontinuity())
        second = aligner.ingest_reference(block(4, 1.080, discontinuity=True))
        third = aligner.ingest_reference(block(5, 1.100))
        self.assertTrue(second.hard_discontinuity)
        self.assertFalse(third.hard_discontinuity)
        self.assertFalse(aligner.begin_hard_discontinuity())
        self.assertEqual(aligner.snapshot.hard_discontinuity_count, 1)

    def test_implausibly_old_reference_cannot_lock(self) -> None:
        aligner = AdaptiveReferenceAligner(0.020, 0.010)
        for sequence in range(3):
            aligner.observe_microphone(block(sequence, 10.000 + sequence * 0.020))
        updates = [
            aligner.ingest_reference(block(sequence, 8.500 + sequence * 0.020))
            for sequence in range(3)
        ]
        self.assertFalse(any(update.locks_alignment for update in updates))
        self.assertEqual(aligner.mode, AlignmentMode.JOINING)

    def test_one_bad_first_mic_timestamp_cannot_create_a_wrong_lock(self) -> None:
        aligner = AdaptiveReferenceAligner(0.020, 0.010)
        aligner.observe_microphone(block(0, 1.045))
        aligner.observe_microphone(block(1, 1.020))
        aligner.observe_microphone(block(2, 1.040))
        early = [aligner.ingest_reference(block(i, 1.0 + i * 0.020)) for i in range(3)]
        self.assertFalse(any(update.locks_alignment for update in early))

        update = aligner.observe_microphone(block(3, 1.060))
        self.assertTrue(update.locks_alignment)
        self.assertEqual([mapped.slot for mapped in update.mapped], [0, 1, 2])
        self.assertEqual(
            [mapped.slot for mapped in aligner.ingest_reference(block(3, 1.060)).mapped],
            [3],
        )

    def test_invalid_portaudio_timing_joins_from_sample_count_clocks(self) -> None:
        aligner = AdaptiveReferenceAligner(0.020, 0.010)
        microphones = [
            AudioBlock(
                (float(sequence),) * 960,
                1.000 + sequence * 0.020,
                sequence=sequence,
                callback_monotonic=10.0 + sequence * 0.020,
                observed_end_monotonic=20.0 - sequence * 0.040,
                timing_valid=False,
            )
            for sequence in range(3)
        ]
        references = [
            AudioBlock(
                (100.0 + sequence,) * 960,
                1.000 + (sequence - 1) * 0.020,
                sequence=sequence,
                callback_monotonic=30.0 + sequence * 0.020,
                observed_end_monotonic=-10.0 + sequence * 0.100,
                timing_valid=False,
            )
            for sequence in range(1, 4)
        ]

        for microphone in microphones:
            aligner.observe_microphone(microphone)
        updates = [aligner.ingest_reference(reference) for reference in references]

        self.assertTrue(updates[-1].locks_alignment)
        self.assertEqual(
            [mapped.slot for mapped in updates[-1].mapped],
            [0, 1, 2],
        )
        self.assertEqual(aligner.reference_offset, -1)

    def test_recovery_requires_ten_valid_in_tolerance_pairs(self) -> None:
        aligner = self.locked_aligner()
        self.assertTrue(aligner.begin_hard_discontinuity())
        for sequence in range(10):
            self.assertFalse(
                aligner.note_pair(
                    block(sequence, 2.020 + sequence * 0.020),
                    block(sequence, 2.000 + sequence * 0.020),
                )
            )
        self.assertEqual(aligner.mode, AlignmentMode.RECOVERY)
        recovered = False
        for sequence in range(10, 20):
            recovered = aligner.note_pair(
                block(sequence, 2.000 + sequence * 0.020),
                block(sequence, 2.000 + sequence * 0.020),
            )
        self.assertTrue(recovered)
        self.assertEqual(aligner.mode, AlignmentMode.LOCKED)

    def test_degraded_state_preserves_mapping_without_realignment(self) -> None:
        aligner = self.locked_aligner()
        self.assertTrue(aligner.mark_synchronization_degraded())
        self.assertFalse(aligner.mark_synchronization_degraded())
        self.assertEqual(aligner.mode, AlignmentMode.DEGRADED)
        self.assertEqual(aligner.reference_offset, 0)
        self.assertEqual(aligner.snapshot.hard_discontinuity_count, 0)


if __name__ == "__main__":
    unittest.main()
