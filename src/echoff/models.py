"""Typed data passed between capture, alignment, and application code."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class AudioBlock:
    """One fixed-duration mono block ending at a monotonic timestamp."""

    samples: tuple[float, ...]
    ended_monotonic: float


@dataclass(frozen=True, slots=True)
class AecState:
    """AEC state associated with one processed pair."""

    echo_path_ready: bool
    far_end_active_s: float
    alignment_epoch: int
    stream_alignment_reset_count: int


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
    pair_tolerance_ms: float
    pair_skew_abs_mean_ms: float
    pair_skew_max_ms: float
    observed_skew_max_ms: float
    first_callback_skew_ms: float | None
    initial_dropped_reference_blocks: int
    initial_dropped_microphone_blocks: int
    runtime_mismatch_count: int
    runtime_realignments: int
    runtime_dropped_reference_blocks: int
    runtime_dropped_microphone_blocks: int
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
    microphone_device_blocks: int
    microphone_silence_blocks: int
    microphone_dropped_device_blocks: int
    echo_path_ready: bool
    far_end_active_s: float
    stream_alignment_reset_count: int
    processing_mean_ms: float
    processing_max_ms: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
