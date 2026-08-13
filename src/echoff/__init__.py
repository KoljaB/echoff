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
from .processor import (
    BufferedWebRtcAecProcessor,
    PassthroughAecProcessor,
    StreamingWebRtcAecProcessor,
    WebRtcAecProcessor,
)
from .recording import PcmWavRecorder

__all__ = [
    "AecCapture",
    "AecCaptureError",
    "AecConfig",
    "AecFrame",
    "AecState",
    "AudioBackendError",
    "BufferedWebRtcAecProcessor",
    "CaptureEvent",
    "CaptureStateError",
    "CaptureStatus",
    "DeviceInfo",
    "PassthroughAecProcessor",
    "PcmWavRecorder",
    "StreamingWebRtcAecProcessor",
    "UnsupportedPlatformError",
    "WebRtcAecProcessor",
]

__version__ = "0.1.4"

logging.getLogger(__name__).addHandler(logging.NullHandler())
