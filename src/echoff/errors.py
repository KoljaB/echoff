"""Package-specific exceptions."""


class AecCaptureError(RuntimeError):
    """Base class for recoverable package errors."""


class AudioBackendError(AecCaptureError):
    """An audio device or native processing backend failed."""


class CaptureStateError(AecCaptureError):
    """A capture lifecycle operation was requested in the wrong state."""


class UnsupportedPlatformError(AecCaptureError):
    """No device capture backend exists for the current platform."""
