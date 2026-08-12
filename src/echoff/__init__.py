"""Synchronized system-audio and microphone capture with WebRTC AEC."""

import logging

from .capture import AecCapture
from .config import AecConfig
from .errors import (
    AecCaptureError,
    AudioBackendError,
    CaptureStateError,
    UnsupportedPlatformError,
)
from .models import AecFrame, AecState, CaptureEvent, CaptureStatus, DeviceInfo
from .processor import WebRtcAecProcessor

__all__ = [
    "AecCapture",
    "AecCaptureError",
    "AecConfig",
    "AecFrame",
    "AecState",
    "AudioBackendError",
    "CaptureEvent",
    "CaptureStateError",
    "CaptureStatus",
    "DeviceInfo",
    "UnsupportedPlatformError",
    "WebRtcAecProcessor",
]

__version__ = "0.1.0"

logging.getLogger(__name__).addHandler(logging.NullHandler())
