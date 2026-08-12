"""Pure timestamp classification and alignment accounting."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .errors import AudioBackendError
from .models import AudioBlock


class AlignmentAction(StrEnum):
    PAIR = "pair"
    DROP_REFERENCE = "drop_reference"
    DROP_MICROPHONE = "drop_microphone"


@dataclass(frozen=True, slots=True)
class AlignmentDecision:
    action: AlignmentAction
    skew_s: float
    starts_realigning: bool = False
    completes_realigning: bool = False
    locks_alignment: bool = False


@dataclass(frozen=True, slots=True)
class AlignmentSnapshot:
    locked: bool
    epoch: int
    pair_count: int
    pair_skew_abs_total_s: float
    pair_skew_max_s: float
    observed_skew_max_s: float
    first_callback_skew_s: float | None
    initial_dropped_reference_blocks: int
    initial_dropped_microphone_blocks: int
    runtime_mismatch_count: int
    runtime_realignments: int
    runtime_dropped_reference_blocks: int
    runtime_dropped_microphone_blocks: int
    last_mismatch_s: float | None
    shutdown_unpaired_reference_blocks: int
    shutdown_unpaired_microphone_blocks: int


class TimestampAligner:
    """Classify two queue heads and retain deterministic alignment counters."""

    def __init__(self, tolerance_s: float) -> None:
        if tolerance_s <= 0.0:
            raise ValueError("alignment tolerance must be positive")
        self.tolerance_s = tolerance_s
        self._last_reference_end: float | None = None
        self._last_microphone_end: float | None = None
        self._first_reference_end: float | None = None
        self._first_microphone_end: float | None = None
        self._locked = False
        self._realigning = False
        self._epoch = 0
        self._pair_count = 0
        self._pair_skew_abs_total_s = 0.0
        self._pair_skew_max_s = 0.0
        self._observed_skew_max_s = 0.0
        self._initial_dropped_reference_blocks = 0
        self._initial_dropped_microphone_blocks = 0
        self._runtime_mismatch_count = 0
        self._runtime_realignments = 0
        self._runtime_dropped_reference_blocks = 0
        self._runtime_dropped_microphone_blocks = 0
        self._last_mismatch_s: float | None = None
        self._shutdown_unpaired_reference_blocks = 0
        self._shutdown_unpaired_microphone_blocks = 0

    def observe(self, source: str, block: AudioBlock) -> None:
        if source not in {"reference", "microphone"}:
            raise ValueError(f"unknown source: {source!r}")
        ended = block.ended_monotonic
        if not math.isfinite(ended):
            raise AudioBackendError(f"{source} capture produced a non-finite timestamp")
        last_attribute = f"_last_{source}_end"
        previous = getattr(self, last_attribute)
        if previous is not None and ended <= previous:
            raise AudioBackendError(
                f"{source} capture timestamp did not advance: "
                f"previous={previous:.6f} current={ended:.6f}"
            )
        setattr(self, last_attribute, ended)
        first_attribute = f"_first_{source}_end"
        if getattr(self, first_attribute) is None:
            setattr(self, first_attribute, ended)

    def decide(self, reference: AudioBlock, microphone: AudioBlock) -> AlignmentDecision:
        skew_s = reference.ended_monotonic - microphone.ended_monotonic
        self._observed_skew_max_s = max(self._observed_skew_max_s, abs(skew_s))
        tolerance_s = self.tolerance_s + 1e-9
        if skew_s < -tolerance_s:
            starts = self._note_mismatch(reference_is_older=True, skew_s=skew_s)
            return AlignmentDecision(
                AlignmentAction.DROP_REFERENCE,
                skew_s,
                starts_realigning=starts,
            )
        if skew_s > tolerance_s:
            starts = self._note_mismatch(reference_is_older=False, skew_s=skew_s)
            return AlignmentDecision(
                AlignmentAction.DROP_MICROPHONE,
                skew_s,
                starts_realigning=starts,
            )
        locks = not self._locked
        completes = self._realigning
        self._locked = True
        self._realigning = False
        self._pair_count += 1
        self._pair_skew_abs_total_s += abs(skew_s)
        self._pair_skew_max_s = max(self._pair_skew_max_s, abs(skew_s))
        return AlignmentDecision(
            AlignmentAction.PAIR,
            skew_s,
            completes_realigning=completes,
            locks_alignment=locks,
        )

    def _note_mismatch(self, *, reference_is_older: bool, skew_s: float) -> bool:
        if not self._locked:
            if reference_is_older:
                self._initial_dropped_reference_blocks += 1
            else:
                self._initial_dropped_microphone_blocks += 1
            return False
        self._runtime_mismatch_count += 1
        self._last_mismatch_s = skew_s
        if reference_is_older:
            self._runtime_dropped_reference_blocks += 1
        else:
            self._runtime_dropped_microphone_blocks += 1
        if self._realigning:
            return False
        self._realigning = True
        self._epoch += 1
        self._runtime_realignments += 1
        return True

    def note_shutdown_unpaired(self, source: str, count: int) -> None:
        if source == "reference":
            self._shutdown_unpaired_reference_blocks += count
        elif source == "microphone":
            self._shutdown_unpaired_microphone_blocks += count
        else:
            raise ValueError(f"unknown source: {source!r}")

    @property
    def snapshot(self) -> AlignmentSnapshot:
        first_skew = None
        if self._first_reference_end is not None and self._first_microphone_end is not None:
            first_skew = self._first_reference_end - self._first_microphone_end
        return AlignmentSnapshot(
            locked=self._locked,
            epoch=self._epoch,
            pair_count=self._pair_count,
            pair_skew_abs_total_s=self._pair_skew_abs_total_s,
            pair_skew_max_s=self._pair_skew_max_s,
            observed_skew_max_s=self._observed_skew_max_s,
            first_callback_skew_s=first_skew,
            initial_dropped_reference_blocks=self._initial_dropped_reference_blocks,
            initial_dropped_microphone_blocks=self._initial_dropped_microphone_blocks,
            runtime_mismatch_count=self._runtime_mismatch_count,
            runtime_realignments=self._runtime_realignments,
            runtime_dropped_reference_blocks=self._runtime_dropped_reference_blocks,
            runtime_dropped_microphone_blocks=self._runtime_dropped_microphone_blocks,
            last_mismatch_s=self._last_mismatch_s,
            shutdown_unpaired_reference_blocks=self._shutdown_unpaired_reference_blocks,
            shutdown_unpaired_microphone_blocks=self._shutdown_unpaired_microphone_blocks,
        )
