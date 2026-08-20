"""Public configuration and validation."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AecConfig:
    """Configuration for capture, alignment, and WebRTC APM.

    The current processor requires 48 kHz mono capture. A 20 ms capture block
    contains two exact 10 ms WebRTC APM frames.
    """

    sample_rate: int = 48_000
    channels: int = 1
    block_duration_s: float = 0.020
    stream_delay_ms: int = 50
    noise_suppression: bool = False
    high_pass_filter: bool = True
    automatic_gain_control: bool = False
    pair_tolerance_s: float = 0.010
    # Exceptional synchronization reserve. It begins when the pairing worker
    # first observes an unmatched head and adds no normal-path latency.
    reference_stall_grace_s: float = 15.0
    queue_fatal_s: float = 15.0
    startup_timeout_s: float = 3.0
    echo_path_warmup_s: float = 7.5
    far_end_active_rms_min: float = 0.001
    echo_path_quality_window_s: float = 1.0
    echo_path_quality_stable_s: float = 0.25
    echo_path_min_suppression_db: float = 10.0
    echo_path_quality_min_raw_rms: float = 0.003
    backend: str = "auto"
    allow_wdmks_microphone_fallback: bool = True

    def __post_init__(self) -> None:
        if self.sample_rate != 48_000:
            raise ValueError("WebRTC APM capture currently requires sample_rate=48000")
        if self.channels != 1:
            raise ValueError("AEC capture currently requires channels=1")
        if not math.isfinite(self.block_duration_s) or self.block_duration_s <= 0.0:
            raise ValueError("block_duration_s must be finite and positive")
        block_samples = self.block_samples
        if block_samples % self.apm_frame_samples:
            raise ValueError("block_duration_s must contain a whole number of 10 ms APM frames")
        if (
            isinstance(self.stream_delay_ms, bool)
            or not isinstance(self.stream_delay_ms, int)
            or self.stream_delay_ms < 0
        ):
            raise ValueError("stream_delay_ms must be a non-negative integer")
        if not math.isfinite(self.pair_tolerance_s) or self.pair_tolerance_s <= 0.0:
            raise ValueError("pair_tolerance_s must be finite and positive")
        if self.pair_tolerance_s > self.block_duration_s / 2.0 + 1e-12:
            raise ValueError("pair_tolerance_s cannot exceed half a capture block")
        if (
            not math.isfinite(self.reference_stall_grace_s)
            or self.reference_stall_grace_s < 3.0 * self.block_duration_s
        ):
            raise ValueError(
                "reference_stall_grace_s must be finite and at least three capture blocks"
            )
        if not math.isfinite(self.queue_fatal_s) or self.queue_fatal_s <= 0.0:
            raise ValueError("queue_fatal_s must be finite and positive")
        if not math.isfinite(self.startup_timeout_s) or self.startup_timeout_s <= 0.0:
            raise ValueError("startup_timeout_s must be finite and positive")
        if not math.isfinite(self.echo_path_warmup_s) or self.echo_path_warmup_s < 0.0:
            raise ValueError("echo_path_warmup_s must be finite and non-negative")
        if not math.isfinite(self.far_end_active_rms_min) or self.far_end_active_rms_min < 0.0:
            raise ValueError("far_end_active_rms_min must be finite and non-negative")
        if (
            not math.isfinite(self.echo_path_quality_window_s)
            or self.echo_path_quality_window_s <= 0.0
        ):
            raise ValueError("echo_path_quality_window_s must be finite and positive")
        if (
            not math.isfinite(self.echo_path_min_suppression_db)
            or self.echo_path_min_suppression_db < 0.0
        ):
            raise ValueError(
                "echo_path_min_suppression_db must be finite and non-negative"
            )
        if (
            not math.isfinite(self.echo_path_quality_stable_s)
            or self.echo_path_quality_stable_s <= 0.0
        ):
            raise ValueError("echo_path_quality_stable_s must be finite and positive")
        if (
            not math.isfinite(self.echo_path_quality_min_raw_rms)
            or self.echo_path_quality_min_raw_rms < 0.0
        ):
            raise ValueError(
                "echo_path_quality_min_raw_rms must be finite and non-negative"
            )
        if self.backend not in {"auto", "windows", "pipewire"}:
            raise ValueError(f"unsupported backend: {self.backend!r}")

    @property
    def block_samples(self) -> int:
        return round(self.sample_rate * self.block_duration_s)

    @property
    def apm_frame_samples(self) -> int:
        return self.sample_rate // 100

    @property
    def echo_path_quality_frames(self) -> int:
        return max(1, math.ceil(self.echo_path_quality_window_s * 100.0))

    @property
    def echo_path_quality_stable_frames(self) -> int:
        return max(1, math.ceil(self.echo_path_quality_stable_s * 100.0))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
