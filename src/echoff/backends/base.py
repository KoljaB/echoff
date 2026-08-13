"""Backend protocol independent of any audio API."""

from __future__ import annotations

from typing import Protocol


class CaptureSource(Protocol):
    """One fixed-size, clock-continuous mono capture stream."""

    backend_name: str
    error: Exception | None
    device_block_count: int
    synthetic_silence_block_count: int
    dropped_device_block_count: int
    timestamp_regression_count: int
    invalid_timestamp_count: int
    timestamp_deviation_max_s: float
    timestamp_gap_block_count: int
    timestamp_anomaly_count: int
    callback_status_count: int
    input_overflow_count: int
    input_underflow_count: int
    padded_sample_count: int
    selected_device_name: str | None
    selected_device_index: int | None

    def start(self) -> None: ...

    def activate(self) -> None: ...

    def stop(self) -> None: ...
