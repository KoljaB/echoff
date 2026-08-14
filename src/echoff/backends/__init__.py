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
    backend = config.backend
    if backend == "auto":
        if sys.platform == "win32":
            backend = "windows"
        elif sys.platform == "linux":
            backend = "pipewire"
    if backend == "windows" and sys.platform == "win32":
        from .windows import create_windows_sources

        sources: tuple[CaptureSource, CaptureSource] = create_windows_sources(
            config,
            reference_callback,
            microphone_callback,
            reference_device,
            microphone_device,
        )
        return sources
    if backend == "pipewire" and sys.platform == "linux":
        from .pipewire import create_pipewire_sources

        sources = create_pipewire_sources(
            config,
            reference_callback,
            microphone_callback,
            reference_device,
            microphone_device,
        )
        return sources
    if backend not in {"windows", "pipewire"}:
        raise UnsupportedPlatformError(
            "built-in device capture is supported on Windows and Linux; "
            "use WebRtcAecProcessor with application-owned PCM on this platform"
        )
    raise UnsupportedPlatformError(
        f"backend {backend!r} is not available on platform {sys.platform!r}"
    )


def list_devices() -> list[DeviceInfo]:
    if sys.platform == "win32":
        from .windows import list_windows_devices

        devices: list[DeviceInfo] = list_windows_devices()
        return devices
    if sys.platform == "linux":
        from .pipewire import list_pipewire_devices

        return list_pipewire_devices()
    raise UnsupportedPlatformError("device listing is supported only on Windows and Linux")


__all__ = ["CaptureSource", "create_sources", "list_devices"]
