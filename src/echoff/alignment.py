"""Sequence-authoritative pair mapping with bounded symmetric recovery."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import pairwise
from statistics import median

from .errors import AudioBackendError
from .models import AudioBlock


class AlignmentMode(StrEnum):
    MICROPHONE_ONLY = "microphone_only"
    JOINING = "joining"
    LOCKED = "locked"
    DEGRADED = "degraded"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class MappedReference:
    slot: int
    block: AudioBlock


@dataclass(frozen=True, slots=True)
class AlignmentUpdate:
    mapped: tuple[MappedReference, ...] = ()
    unmapped: tuple[AudioBlock, ...] = ()
    locks_alignment: bool = False
    correction_slots: int = 0
    hard_discontinuity: bool = False
    starts_reference_grace: bool = False


@dataclass(frozen=True, slots=True)
class AlignmentSnapshot:
    locked: bool
    mode: str
    epoch: int
    pair_count: int
    pair_skew_abs_total_s: float
    pair_skew_max_s: float
    observed_skew_max_s: float
    first_callback_skew_s: float | None
    clock_suspect_observation_count: int
    hard_discontinuity_count: int
    last_mismatch_s: float | None
    shutdown_unpaired_reference_blocks: int
    shutdown_unpaired_microphone_blocks: int
    zero_filled_reference_blocks: int
    late_reference_blocks: int
    clock_correction_count: int
    startup_unpaired_reference_blocks: int
    degraded_unpaired_reference_blocks: int
    degraded_unpaired_microphone_blocks: int
    wait_timeout_unpaired_reference_blocks: int
    wait_timeout_unpaired_microphone_blocks: int
    source_failure_unpaired_reference_blocks: int
    source_failure_unpaired_microphone_blocks: int
    hard_discontinuity_unpaired_reference_blocks: int
    hard_discontinuity_unpaired_microphone_blocks: int


class AdaptiveReferenceAligner:
    """Map an intermittent reference stream onto a continuous mic timeline.

    Received sample order and each source's sample-count clock are authoritative.
    PortAudio timestamp observations are diagnostics only; no single observation
    can create a slot or reset WebRTC APM.
    """

    JOIN_CONFIRMATIONS = 3
    # Clock slips are rare (roughly one every 100 s at 200 ppm). Require a full
    # ten-second evidence window and use its robust centre. This tolerates the
    # isolated +/-20 ms PortAudio observation noise seen in real sessions while
    # refusing short-lived timestamp steps.
    CORRECTION_WINDOW = 500
    RECOVERY_PAIRS = 10
    PHASE_CONFIRMATIONS = 3
    PHASE_WINDOW = 51
    PHASE_OUTLIER_S = 0.100
    MAX_PHASE_SLEW_PPM = 1_000.0
    JOIN_HORIZON_BLOCKS = 4
    JOIN_PENDING_BLOCKS = JOIN_CONFIRMATIONS

    def __init__(self, block_duration_s: float, tolerance_s: float) -> None:
        if block_duration_s <= 0.0 or tolerance_s <= 0.0:
            raise ValueError("block duration and alignment tolerance must be positive")
        self.block_duration_s = block_duration_s
        self.tolerance_s = tolerance_s
        self._mode = AlignmentMode.MICROPHONE_ONLY
        self._microphone_phase: float | None = None
        self._first_microphone_phase: float | None = None
        self._microphone_phase_observations: deque[float] = deque(
            maxlen=self.PHASE_WINDOW
        )
        self._last_microphone_sequence: int | None = None
        self._last_reference_sequence: int | None = None
        self._first_reference_observed_end: float | None = None
        self._reference_offset: int | None = None
        self._join_pending: list[AudioBlock] = []
        self._join_candidates: deque[tuple[int, float]] = deque(
            maxlen=self.JOIN_CONFIRMATIONS
        )
        self._pair_skew_baseline: deque[float] = deque(maxlen=50)
        self._pair_skew_target: float | None = None
        self._correction_window: deque[float] = deque(maxlen=self.CORRECTION_WINDOW)
        self._correction_to_report = 0
        self._hard_episode_open = False
        self._recovery_good = 0
        self._epoch = 0
        self._pair_count = 0
        self._pair_skew_abs_total_s = 0.0
        self._pair_skew_max_s = 0.0
        self._observed_skew_max_s = 0.0
        self._clock_suspect_observation_count = 0
        self._hard_discontinuity_count = 0
        self._last_mismatch_s: float | None = None
        self._zero_filled_reference_blocks = 0
        self._late_reference_blocks = 0
        self._clock_correction_count = 0
        self._shutdown_unpaired_reference_blocks = 0
        self._shutdown_unpaired_microphone_blocks = 0
        self._startup_unpaired_reference_blocks = 0
        self._degraded_unpaired_reference_blocks = 0
        self._degraded_unpaired_microphone_blocks = 0
        self._wait_timeout_unpaired_reference_blocks = 0
        self._wait_timeout_unpaired_microphone_blocks = 0
        self._source_failure_unpaired_reference_blocks = 0
        self._source_failure_unpaired_microphone_blocks = 0
        self._hard_discontinuity_unpaired_reference_blocks = 0
        self._hard_discontinuity_unpaired_microphone_blocks = 0

    @staticmethod
    def event_end(block: AudioBlock) -> float:
        value = block.observed_end_monotonic if block.timing_valid else None
        if value is None or not math.isfinite(value):
            value = block.ended_monotonic
        if not math.isfinite(value):
            raise AudioBackendError("capture produced no finite block time")
        return float(value)

    def observe_microphone(self, block: AudioBlock) -> AlignmentUpdate:
        sequence_discontinuity = (
            self._last_microphone_sequence is not None
            and block.sequence != self._last_microphone_sequence + 1
        )
        self._last_microphone_sequence = block.sequence
        hard_discontinuity = block.discontinuity or sequence_discontinuity
        self._update_microphone_phase(block, reanchor=hard_discontinuity)
        unmapped: tuple[AudioBlock, ...] = ()
        if hard_discontinuity:
            unmapped = tuple(self._join_pending)
            self._correction_window.clear()
            self._pair_skew_baseline.clear()
            self._pair_skew_target = None
            self._correction_to_report = 0
            self._reference_offset = None
            self._join_pending.clear()
            self._join_candidates.clear()
            self._last_reference_sequence = None
            self._mode = AlignmentMode.JOINING
        if self._join_pending:
            joined = self._try_join()
            return AlignmentUpdate(
                mapped=joined.mapped,
                unmapped=unmapped + joined.unmapped,
                locks_alignment=joined.locks_alignment,
                correction_slots=joined.correction_slots,
                hard_discontinuity=hard_discontinuity,
            )
        return AlignmentUpdate(
            unmapped=unmapped,
            hard_discontinuity=hard_discontinuity,
        )

    def confirm_microphone_phase(self, pending: tuple[AudioBlock, ...]) -> bool:
        """Confirm the mic phase from queued sample-count clock blocks.

        This look-ahead never advances source sequence state and stops before a
        future discontinuity. ``ended_monotonic`` is the canonical clock advanced
        only by emitted sample counts, so rejected PortAudio observations cannot
        prevent an otherwise healthy capture from establishing its initial map.
        """

        if self._microphone_phase is not None:
            return True
        observations: list[float] = []
        previous_sequence: int | None = None
        for index, block in enumerate(pending):
            if index and (
                block.discontinuity
                or previous_sequence is None
                or block.sequence != previous_sequence + 1
            ):
                break
            previous_sequence = block.sequence
            observation = block.ended_monotonic - block.sequence * self.block_duration_s
            observations.append(observation)
            if len(observations) < self.PHASE_CONFIRMATIONS:
                continue
            recent = observations[-self.PHASE_CONFIRMATIONS :]
            if max(recent) - min(recent) > self.tolerance_s:
                continue
            self._microphone_phase = median(recent)
            self._microphone_phase_observations.clear()
            self._microphone_phase_observations.extend(recent)
            if self._first_microphone_phase is None:
                self._first_microphone_phase = self._microphone_phase
            return True
        return False

    def ingest_reference(self, block: AudioBlock) -> AlignmentUpdate:
        sequence_discontinuity = (
            self._last_reference_sequence is not None
            and block.sequence != self._last_reference_sequence + 1
        )
        if sequence_discontinuity and not block.discontinuity:
            block = replace(block, discontinuity=True)
        self._last_reference_sequence = block.sequence
        if (
            self._first_reference_observed_end is None
            and block.timing_valid
            and block.observed_end_monotonic is not None
        ):
            self._first_reference_observed_end = block.observed_end_monotonic

        hard_discontinuity = block.discontinuity
        unmapped: tuple[AudioBlock, ...] = ()
        if hard_discontinuity:
            unmapped = tuple(self._join_pending)
            self._reference_offset = None
            self._join_pending.clear()
            self._join_candidates.clear()
            self._correction_window.clear()
            self._pair_skew_baseline.clear()
            self._pair_skew_target = None
            self._correction_to_report = 0
            self._mode = AlignmentMode.JOINING

        if self._mode in {
            AlignmentMode.MICROPHONE_ONLY,
            AlignmentMode.JOINING,
        } or self._reference_offset is None:
            self._mode = AlignmentMode.JOINING
            self._join_pending.append(block)
            while len(self._join_pending) > self.JOIN_PENDING_BLOCKS:
                self._join_pending.pop(0)
                self.note_late_reference()
            joined = self._try_join()
            if hard_discontinuity:
                return AlignmentUpdate(
                    mapped=joined.mapped,
                    unmapped=unmapped + joined.unmapped,
                    locks_alignment=joined.locks_alignment,
                    correction_slots=joined.correction_slots,
                    hard_discontinuity=True,
                )
            return joined

        assert self._reference_offset is not None
        if self._mode is AlignmentMode.RECOVERY:
            return AlignmentUpdate(
                mapped=(MappedReference(block.sequence + self._reference_offset, block),)
            )

        correction_to_report = self._correction_to_report
        self._correction_to_report = 0
        if not block.timing_valid or block.observed_end_monotonic is None:
            return AlignmentUpdate(
                mapped=(MappedReference(block.sequence + self._reference_offset, block),),
                correction_slots=correction_to_report,
            )
        residual = self._mapping_residual(block, self._reference_offset)
        self._observed_skew_max_s = max(self._observed_skew_max_s, abs(residual))
        correction = round(residual / self.block_duration_s)
        if correction:
            self._clock_suspect_observation_count += 1
            self._last_mismatch_s = residual
        return AlignmentUpdate(
            mapped=(MappedReference(block.sequence + self._reference_offset, block),),
            correction_slots=correction_to_report,
        )

    def _update_microphone_phase(self, block: AudioBlock, *, reanchor: bool) -> None:
        if reanchor:
            self._microphone_phase = None
            self._microphone_phase_observations.clear()
        # The canonical end advances from the source's sample count. An invalid
        # PortAudio observation therefore remains ineligible while the payload
        # sequence can still establish and preserve synchronization.
        event_end = block.ended_monotonic
        observation = event_end - block.sequence * self.block_duration_s
        if self._microphone_phase is None:
            self._microphone_phase_observations.append(observation)
            if len(self._microphone_phase_observations) < self.PHASE_CONFIRMATIONS:
                return
            recent = tuple(self._microphone_phase_observations)[
                -self.PHASE_CONFIRMATIONS :
            ]
            if max(recent) - min(recent) > self.tolerance_s:
                return
            self._microphone_phase = median(recent)
            if self._first_microphone_phase is None:
                self._first_microphone_phase = self._microphone_phase
            return
        if abs(observation - self._microphone_phase) > self.PHASE_OUTLIER_S:
            return
        self._microphone_phase_observations.append(observation)
        target = median(self._microphone_phase_observations)
        max_slew = self.block_duration_s * self.MAX_PHASE_SLEW_PPM / 1_000_000.0
        delta = max(-max_slew, min(max_slew, target - self._microphone_phase))
        self._microphone_phase += delta

    def _sample_clock_candidate(self, block: AudioBlock) -> tuple[int, float] | None:
        """Estimate identity from canonical sample clocks, never observations."""

        if self._microphone_phase is None:
            return None
        observed = block.ended_monotonic
        estimated_slot = round(
            (observed - self._microphone_phase) / self.block_duration_s
        )
        offset = estimated_slot - block.sequence
        expected = self._microphone_phase + estimated_slot * self.block_duration_s
        return offset, observed - expected

    def reference_slot_hint(self, block: AudioBlock) -> int | None:
        """Return the currently supported master slot without mutating state."""

        if self._reference_offset is not None:
            return int(block.sequence + self._reference_offset)
        candidate = self._sample_clock_candidate(block)
        if candidate is None:
            return None
        offset, _residual = candidate
        return int(block.sequence + offset)

    def _try_join(self) -> AlignmentUpdate:
        if self._microphone_phase is None or self._last_microphone_sequence is None:
            return AlignmentUpdate()
        selected_offset: int | None = None
        selected_start = 0
        for start in range(0, len(self._join_pending) - self.JOIN_CONFIRMATIONS + 1):
            window = self._join_pending[start : start + self.JOIN_CONFIRMATIONS]
            candidates = [self._sample_clock_candidate(block) for block in window]
            if any(candidate is None for candidate in candidates):
                continue
            concrete = [candidate for candidate in candidates if candidate is not None]
            offsets = [offset for offset, _residual in concrete]
            residuals = [residual for _offset, residual in concrete]
            latest_mapped = window[-1].sequence + offsets[-1]
            distance = latest_mapped - self._last_microphone_sequence
            within_horizon = -2 <= distance <= self.JOIN_HORIZON_BLOCKS
            if (
                len(set(offsets)) == 1
                and abs(median(residuals)) <= self.tolerance_s
                and within_horizon
            ):
                selected_offset = offsets[0]
                selected_start = start
                break
        if selected_offset is None:
            return AlignmentUpdate()
        self._reference_offset = selected_offset
        for _block in self._join_pending[:selected_start]:
            self.note_late_reference()
        mapped = tuple(
            MappedReference(block.sequence + self._reference_offset, block)
            for block in self._join_pending[selected_start:]
        )
        self._join_pending.clear()
        self._join_candidates.clear()
        was_realigning = self._hard_episode_open
        self._mode = AlignmentMode.RECOVERY if was_realigning else AlignmentMode.LOCKED
        self._recovery_good = 0
        return AlignmentUpdate(
            mapped=mapped,
            locks_alignment=not was_realigning,
            starts_reference_grace=distance < 0,
        )

    def _mapping_residual(self, block: AudioBlock, offset: int) -> float:
        assert self._microphone_phase is not None
        slot = block.sequence + offset
        expected = self._microphone_phase + slot * self.block_duration_s
        return float(self.event_end(block) - expected)

    def _confirmed_pair_correction(self) -> int:
        if len(self._correction_window) < self.CORRECTION_WINDOW:
            return 0
        residuals = list(self._correction_window)
        bin_size = 50
        centres = [
            median(residuals[start : start + bin_size])
            for start in range(0, self.CORRECTION_WINDOW, bin_size)
        ]
        latest = centres[-1]
        if abs(latest) < 0.60 * self.block_duration_s:
            return 0
        candidate = 1 if latest > 0.0 else -1
        changes = [later - earlier for earlier, later in pairwise(centres)]
        total_change = centres[-1] - centres[0]
        # A persistent rate error moves the cross-stream skew gradually. A
        # fixed timestamp step, isolated callback jitter, or alternating
        # PortAudio estimates must not change payload identity.
        if candidate * total_change < 0.0005:
            return 0
        if max(abs(change) for change in changes) > 0.005:
            return 0
        if sum(candidate * change >= -0.001 for change in changes) < 7:
            return 0
        corrected = latest - candidate * self.block_duration_s
        if abs(latest) - abs(corrected) < 0.008:
            return 0
        return candidate

    def mark_synchronization_degraded(self) -> bool:
        """Suspend AEC readiness without abandoning a confirmed mapping."""

        if self._mode is AlignmentMode.DEGRADED:
            return False
        self._mode = AlignmentMode.DEGRADED
        self._correction_window.clear()
        return True

    def mark_synchronization_recovered(self) -> bool:
        if self._mode is not AlignmentMode.DEGRADED or self._reference_offset is None:
            return False
        self._mode = AlignmentMode.LOCKED
        return True

    def note_degraded_unpaired(
        self,
        source: str,
        count: int,
        *,
        cause: str,
        last_sequence: int | None = None,
    ) -> None:
        if source == "reference":
            self._degraded_unpaired_reference_blocks += count
            if cause == "wait_timeout":
                self._wait_timeout_unpaired_reference_blocks += count
            elif cause == "source_failure":
                self._source_failure_unpaired_reference_blocks += count
            else:
                raise ValueError(f"unknown degraded cause: {cause!r}")
            if last_sequence is not None:
                self._last_reference_sequence = last_sequence
        elif source == "microphone":
            self._degraded_unpaired_microphone_blocks += count
            if cause == "wait_timeout":
                self._wait_timeout_unpaired_microphone_blocks += count
            elif cause == "source_failure":
                self._source_failure_unpaired_microphone_blocks += count
            else:
                raise ValueError(f"unknown degraded cause: {cause!r}")
            if last_sequence is not None:
                self._last_microphone_sequence = last_sequence
        else:
            raise ValueError(f"unknown source: {source!r}")

    def note_hard_discontinuity_unpaired(self, source: str, count: int) -> None:
        if source == "reference":
            self._hard_discontinuity_unpaired_reference_blocks += count
        elif source == "microphone":
            self._hard_discontinuity_unpaired_microphone_blocks += count
        else:
            raise ValueError(f"unknown source: {source!r}")

    def begin_hard_discontinuity(self) -> bool:
        """Open or extend one recovery episode; return whether APM must reset."""

        reset_required = not self._hard_episode_open
        self._hard_episode_open = True
        self._recovery_good = 0
        self._correction_window.clear()
        self._pair_skew_baseline.clear()
        self._pair_skew_target = None
        self._correction_to_report = 0
        if reset_required:
            self._hard_discontinuity_count += 1
            self._epoch += 1
        if self._mode not in {
            AlignmentMode.MICROPHONE_ONLY,
            AlignmentMode.JOINING,
        }:
            self._mode = AlignmentMode.RECOVERY
        return reset_required

    def drain_pending_references(self) -> tuple[AudioBlock, ...]:
        pending = tuple(self._join_pending)
        self._join_pending.clear()
        self._join_candidates.clear()
        self._correction_window.clear()
        return pending

    def note_pair(self, reference: AudioBlock, microphone: AudioBlock) -> bool:
        reference_end = self.event_end(reference)
        microphone_end = self.event_end(microphone)
        skew = reference_end - microphone_end
        self._pair_count += 1
        self._pair_skew_abs_total_s += abs(skew)
        self._pair_skew_max_s = max(self._pair_skew_max_s, abs(skew))
        if (
            self._mode is AlignmentMode.LOCKED
            and reference.timing_valid
            and microphone.timing_valid
            and self._reference_offset is not None
        ):
            if self._pair_skew_target is None:
                self._pair_skew_baseline.append(skew)
                if len(self._pair_skew_baseline) == self._pair_skew_baseline.maxlen:
                    self._pair_skew_target = median(self._pair_skew_baseline)
            else:
                self._correction_window.append(skew - self._pair_skew_target)
                # Timestamp residuals remain long-window diagnostics. The
                # confirmed sequence mapping is authoritative until a proven
                # hard discontinuity opens a new epoch.
        if self._mode is AlignmentMode.RECOVERY:
            if (
                reference.timing_valid
                and microphone.timing_valid
                and abs(skew) <= self.tolerance_s
            ):
                self._recovery_good += 1
            elif reference.timing_valid and microphone.timing_valid:
                self._recovery_good = 0
            if self._recovery_good >= self.RECOVERY_PAIRS:
                self._mode = AlignmentMode.LOCKED
                self._hard_episode_open = False
                return True
        return False

    def note_late_reference(self) -> None:
        self._late_reference_blocks += 1

    def note_startup_unpaired_reference(self, count: int) -> None:
        """Account for reference pre-roll before the first microphone slot."""

        self._startup_unpaired_reference_blocks += count

    def note_shutdown_unpaired(self, source: str, count: int) -> None:
        if source == "reference":
            self._shutdown_unpaired_reference_blocks += count
        elif source == "microphone":
            self._shutdown_unpaired_microphone_blocks += count
        else:
            raise ValueError(f"unknown source: {source!r}")

    @property
    def alignment_ready(self) -> bool:
        return self._mode in {AlignmentMode.LOCKED, AlignmentMode.RECOVERY}

    @property
    def mode(self) -> AlignmentMode:
        return self._mode

    @property
    def pending_reference_count(self) -> int:
        return len(self._join_pending)

    @property
    def reference_offset(self) -> int | None:
        """Confirmed source-sequence to microphone-slot offset, if known."""

        return self._reference_offset

    @property
    def snapshot(self) -> AlignmentSnapshot:
        first_skew = None
        if (
            self._first_microphone_phase is not None
            and self._first_reference_observed_end is not None
        ):
            first_skew = self._first_reference_observed_end - self._first_microphone_phase
        return AlignmentSnapshot(
            locked=self.alignment_ready,
            mode=self._mode.value,
            epoch=self._epoch,
            pair_count=self._pair_count,
            pair_skew_abs_total_s=self._pair_skew_abs_total_s,
            pair_skew_max_s=self._pair_skew_max_s,
            observed_skew_max_s=self._observed_skew_max_s,
            first_callback_skew_s=first_skew,
            clock_suspect_observation_count=self._clock_suspect_observation_count,
            hard_discontinuity_count=self._hard_discontinuity_count,
            last_mismatch_s=self._last_mismatch_s,
            shutdown_unpaired_reference_blocks=self._shutdown_unpaired_reference_blocks,
            shutdown_unpaired_microphone_blocks=self._shutdown_unpaired_microphone_blocks,
            zero_filled_reference_blocks=self._zero_filled_reference_blocks,
            late_reference_blocks=self._late_reference_blocks,
            clock_correction_count=self._clock_correction_count,
            startup_unpaired_reference_blocks=(
                self._startup_unpaired_reference_blocks
            ),
            degraded_unpaired_reference_blocks=(
                self._degraded_unpaired_reference_blocks
            ),
            degraded_unpaired_microphone_blocks=(
                self._degraded_unpaired_microphone_blocks
            ),
            wait_timeout_unpaired_reference_blocks=(
                self._wait_timeout_unpaired_reference_blocks
            ),
            wait_timeout_unpaired_microphone_blocks=(
                self._wait_timeout_unpaired_microphone_blocks
            ),
            source_failure_unpaired_reference_blocks=(
                self._source_failure_unpaired_reference_blocks
            ),
            source_failure_unpaired_microphone_blocks=(
                self._source_failure_unpaired_microphone_blocks
            ),
            hard_discontinuity_unpaired_reference_blocks=(
                self._hard_discontinuity_unpaired_reference_blocks
            ),
            hard_discontinuity_unpaired_microphone_blocks=(
                self._hard_discontinuity_unpaired_microphone_blocks
            ),
        )
