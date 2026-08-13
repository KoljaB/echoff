"""Platform capture backend selection."""

from __future__ import annotations

import sys
from collections.abc import Callable

from ..config import AecConfig
from ..errors import UnsupportedPlatformError
from ..models import AudioBlock, DeviceInfo
from .base import CaptureSource


def create_sources(
    config: AecConfig,
    reference_callback: Callable[[AudioBlock], None],
    microphone_callback: Callable[[AudioBlock], None],
    *,
    reference_device: str | None = None,
    microphone_device: str | None = None,
) -> tuple[CaptureSource, CaptureSource]:
    backend = "windows" if config.backend == "auto" and sys.platform == "win32" else config.backend
    if backend != "windows" or sys.platform != "win32":
        raise UnsupportedPlatformError(
            "device capture is currently supported only on Windows; "
            "use WebRtcAecProcessor with application-owned PCM on this platform"
        )
    from .windows import create_windows_sources

    return create_windows_sources(
        config,
        reference_callback,
        microphone_callback,
        reference_device,
        microphone_device,
    )


def list_devices() -> list[DeviceInfo]:
    if sys.platform != "win32":
        raise UnsupportedPlatformError("device listing is currently supported only on Windows")
    from .windows import list_windows_devices

    return list_windows_devices()


__all__ = ["CaptureSource", "create_sources", "list_devices"]
