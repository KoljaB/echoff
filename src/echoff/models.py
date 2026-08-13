"""Typed data passed between capture, alignment, and application code."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class AudioBlock:
    """One fixed-duration mono payload with sample and timing provenance."""

    samples: tuple[float, ...]
    ended_monotonic: float
    sequence: int = -1
    callback_monotonic: float | None = None
    observed_end_monotonic: float | None = None
    timing_valid: bool = True
    discontinuity: bool = False
    status_flags: int = 0
    valid_samples: int | None = None


@dataclass(frozen=True, slots=True)
class AecState:
    """AEC state associated with one processed pair."""

    echo_path_ready: bool
    far_end_active_s: float
    alignment_epoch: int
    stream_alignment_reset_count: int
    echo_path_quality_ready: bool = False
    echo_suppression_db: float | None = None
    echo_quality_s: float = 0.0


@dataclass(frozen=True, slots=True)
class AecFrame:
    """A matched reference/microphone block and its cleaned microphone."""

    reference: tuple[float, ...]
    microphone_raw: tuple[float, ...]
    microphone_clean: tuple[float, ...]
    reference_ended_monotonic: float
    microphone_ended_monotonic: float
    pair_skew_s: float
    state: AecState
    reference_present: bool = True
    microphone_present: bool = True


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """One selectable audio device."""

    kind: Literal["reference", "microphone"]
    backend: str
    index: int
    name: str
    is_default: bool = False
    channels: int = 1
    default_sample_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CaptureEvent:
    """One structured lifecycle or alignment event."""

    kind: str
    monotonic: float
    utc: str
    details: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "echoff-event-v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    """Immutable snapshot of a running or completed capture."""

    running: bool
    alignment_locked: bool
    alignment_epoch: int
    processed_pair_count: int
    matched_reference_blocks: int
    pair_tolerance_ms: float
    pair_skew_abs_mean_ms: float
    pair_skew_max_ms: float
    observed_skew_max_ms: float
    first_callback_skew_ms: float | None
    clock_suspect_observation_count: int
    hard_discontinuity_count: int
    alignment_mode: str
    zero_filled_reference_blocks: int
    late_reference_blocks: int
    clock_correction_count: int
    last_mismatch_ms: float | None
    shutdown_unpaired_reference_blocks: int
    shutdown_unpaired_microphone_blocks: int
    reference_audio_s: float
    microphone_audio_s: float
    reference_queue_s: float
    microphone_queue_s: float
    reference_backend: str
    microphone_backend: str
    reference_device_name: str | None
    reference_device_index: int | None
    microphone_device_name: str | None
    microphone_device_index: int | None
    reference_device_blocks: int
    reference_silence_blocks: int
    reference_dropped_device_blocks: int
    reference_timestamp_regressions: int
    reference_invalid_timestamps: int
    reference_timestamp_deviation_max_ms: float
    reference_timestamp_gap_blocks: int
    reference_timestamp_anomalies: int
    reference_callback_status_count: int
    reference_input_overflow_count: int
    reference_input_underflow_count: int
    reference_padded_samples: int
    reference_callback_packet_count: int
    reference_callback_payload_frames: int
    reference_callback_queue_high_watermark_blocks: int
    reference_callback_queue_age_max_ms: float
    reference_callback_enqueue_max_ms: float
    reference_callback_timeline_drift_ms: float
    reference_callback_timeline_drift_max_ms: float
    microphone_device_blocks: int
    microphone_silence_blocks: int
    microphone_dropped_device_blocks: int
    microphone_timestamp_regressions: int
    microphone_invalid_timestamps: int
    microphone_timestamp_deviation_max_ms: float
    microphone_timestamp_gap_blocks: int
    microphone_timestamp_anomalies: int
    microphone_callback_status_count: int
    microphone_input_overflow_count: int
    microphone_input_underflow_count: int
    microphone_padded_samples: int
    microphone_callback_packet_count: int
    microphone_callback_payload_frames: int
    microphone_callback_queue_high_watermark_blocks: int
    microphone_callback_queue_age_max_ms: float
    microphone_callback_enqueue_max_ms: float
    microphone_callback_timeline_drift_ms: float
    microphone_callback_timeline_drift_max_ms: float
    echo_path_ready: bool
    far_end_active_s: float
    stream_alignment_reset_count: int
    echo_path_quality_ready: bool
    echo_suppression_db: float | None
    echo_quality_s: float
    processing_mean_ms: float
    processing_max_ms: float
    worker_slot_mean_ms: float
    worker_slot_max_ms: float
    synchronization_wait_count: int
    synchronization_wait_completed_count: int
    synchronization_wait_timeout_count: int
    synchronization_wait_total_ms: float
    synchronization_wait_max_ms: float
    synchronization_max_backlog_blocks: int
    synchronization_catchup_total_ms: float
    synchronization_catchup_max_ms: float
    source_failure_count: int
    degraded_unpaired_reference_blocks: int
    degraded_unpaired_microphone_blocks: int
    wait_timeout_unpaired_reference_blocks: int
    wait_timeout_unpaired_microphone_blocks: int
    source_failure_unpaired_reference_blocks: int
    source_failure_unpaired_microphone_blocks: int
    startup_unpaired_reference_blocks: int
    hard_discontinuity_unpaired_reference_blocks: int
    hard_discontinuity_unpaired_microphone_blocks: int
    reference_error: str | None = None
    microphone_error: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
