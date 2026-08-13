"""Sample-authoritative capture clocks and fixed-block accumulation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .models import AudioBlock


@dataclass(frozen=True, slots=True)
class ClockObservation:
    """One callback-time observation in the local monotonic clock domain."""

    callback_monotonic: float
    observed_end_monotonic: float | None
    timing_valid: bool


class FixedBlockSampleClock:
    """Preserve every sample and emit fixed blocks on a sample-count clock.

    PortAudio timestamps are noisy observations. They may qualify alignment,
    but they never create, remove, or duplicate samples or logical blocks.
    """

    MAX_OBSERVATION_AGE_S = 2.0
    MAX_OBSERVATION_FUTURE_S = 0.100

    def __init__(self, *, sample_rate: int, block_samples: int) -> None:
        self.sample_rate = sample_rate
        self.block_samples = block_samples
        self.block_duration_s = block_samples / sample_rate
        self._pending: list[float] = []
        self._sequence = 0
        self._last_canonical_end: float | None = None
        self._last_reported_end: float | None = None
        self._pending_discontinuity = False
        self._pending_status_flags = 0
        self.invalid_timestamp_count = 0
        self.timestamp_regression_count = 0
        self.timestamp_anomaly_count = 0
        self.timestamp_deviation_max_s = 0.0
        self.padded_sample_count = 0

    def _observe(
        self,
        *,
        callback_monotonic: float,
        adc_start: float | None,
        current_time: float | None,
        frame_count: int,
        count_invalid: bool,
    ) -> ClockObservation:
        observed: float | None = None
        if (
            adc_start is not None
            and current_time is not None
            and math.isfinite(adc_start)
            and math.isfinite(current_time)
            and math.isfinite(callback_monotonic)
        ):
            observed = callback_monotonic + (
                adc_start + frame_count / self.sample_rate - current_time
            )
            if not math.isfinite(observed):
                observed = None
        if observed is not None and not (
            callback_monotonic - self.MAX_OBSERVATION_AGE_S
            <= observed
            <= callback_monotonic + self.MAX_OBSERVATION_FUTURE_S
        ):
            observed = None
            self.timestamp_anomaly_count += 1
        if observed is None:
            if count_invalid:
                self.invalid_timestamp_count += 1
            return ClockObservation(callback_monotonic, None, False)
        if self._last_reported_end is not None and observed <= self._last_reported_end:
            self.timestamp_regression_count += 1
        self._last_reported_end = observed
        return ClockObservation(callback_monotonic, observed, True)

    def push(
        self,
        samples: Sequence[float],
        *,
        callback_monotonic: float,
        adc_start: float | None,
        current_time: float | None,
        status_flags: int = 0,
        discontinuity: bool = False,
        count_invalid_timestamp: bool = True,
    ) -> tuple[AudioBlock, ...]:
        """Append one callback payload and emit all complete fixed blocks."""

        values = tuple(float(value) for value in samples)
        observation = self._observe(
            callback_monotonic=callback_monotonic,
            adc_start=adc_start,
            current_time=current_time,
            frame_count=len(values),
            count_invalid=count_invalid_timestamp,
        )
        self._pending_discontinuity = self._pending_discontinuity or discontinuity
        self._pending_status_flags |= int(status_flags)
        self._pending.extend(values)
        output: list[AudioBlock] = []
        while len(self._pending) >= self.block_samples:
            block_values = tuple(self._pending[: self.block_samples])
            del self._pending[: self.block_samples]
            remaining_s = len(self._pending) / self.sample_rate
            observed_end = (
                None
                if observation.observed_end_monotonic is None
                else observation.observed_end_monotonic - remaining_s
            )
            if self._last_canonical_end is None:
                canonical_end = (
                    observed_end
                    if observed_end is not None
                    else callback_monotonic - remaining_s
                )
            else:
                canonical_end = self._last_canonical_end + self.block_duration_s
            if observed_end is not None:
                deviation = observed_end - canonical_end
                self.timestamp_deviation_max_s = max(
                    self.timestamp_deviation_max_s,
                    abs(deviation),
                )
                if abs(deviation) > self.block_duration_s * 0.75:
                    self.timestamp_anomaly_count += 1
                    # A rejected observation is diagnostics only; it must not
                    # remain eligible for alignment or payload identity.
                    observation = replace(observation, timing_valid=False)
            block = AudioBlock(
                samples=block_values,
                ended_monotonic=canonical_end,
                sequence=self._sequence,
                callback_monotonic=callback_monotonic,
                observed_end_monotonic=observed_end,
                timing_valid=observation.timing_valid,
                discontinuity=self._pending_discontinuity,
                status_flags=self._pending_status_flags,
                valid_samples=self.block_samples,
            )
            output.append(block)
            self._sequence += 1
            self._last_canonical_end = canonical_end
            self._pending_discontinuity = False
            self._pending_status_flags = 0
        return tuple(output)

    def flush(
        self,
        *,
        callback_monotonic: float,
    ) -> tuple[AudioBlock, ...]:
        """Pad and emit the final partial block without losing real samples."""

        if not self._pending:
            return ()
        valid_samples = len(self._pending)
        padding = self.block_samples - valid_samples
        self.padded_sample_count += padding
        blocks = self.push(
            [0.0] * padding,
            callback_monotonic=callback_monotonic,
            adc_start=None,
            current_time=None,
            # A padded final block is an orderly shutdown detail, not evidence
            # that either capture device lost or jumped audio.
            discontinuity=False,
            count_invalid_timestamp=False,
        )
        if len(blocks) != 1:  # pragma: no cover - fixed by the padding calculation
            raise RuntimeError("final sample-clock flush produced an invalid block count")
        return (replace(blocks[0], valid_samples=valid_samples),)
