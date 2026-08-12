"""Atomic paired-frame adapter for LiveKit's native WebRTC APM."""

from __future__ import annotations

import math
import threading
from array import array
from collections.abc import Sequence
from typing import Any, Protocol

from .config import AecConfig
from .errors import AudioBackendError
from .models import AecState


class AecProcessor(Protocol):
    """Minimal processor interface consumed by :class:`AecCapture`."""

    def process_pair(
        self,
        reference: Sequence[float],
        microphone: Sequence[float],
    ) -> tuple[float, ...]: ...

    def reset_alignment(self) -> None: ...

    @property
    def state(self) -> AecState: ...


class WebRtcAecProcessor:
    """Synchronous WebRTC echo canceller with one atomic pair operation."""

    def __init__(
        self,
        config: AecConfig | None = None,
        *,
        _rtc: Any | None = None,
        _apm_type: Any | None = None,
    ) -> None:
        self.config = config or AecConfig()
        if (_rtc is None) != (_apm_type is None):
            raise ValueError("_rtc and _apm_type must be supplied together")
        if _rtc is None:
            try:
                from livekit import rtc
                from livekit.rtc.apm import AudioProcessingModule
            except ImportError as exc:
                raise AudioBackendError(
                    "LiveKit APM is required; install the package's base dependencies"
                ) from exc
            _rtc = rtc
            _apm_type = AudioProcessingModule
        assert _apm_type is not None
        self._rtc = _rtc
        self._apm_type = _apm_type
        self._lock = threading.Lock()
        self._apm = self._new_apm()
        self._paired_far_end_active_frames = 0
        self._alignment_epoch = 0
        self._stream_alignment_reset_count = 0

    def _new_apm(self) -> Any:
        apm = self._apm_type(
            echo_cancellation=True,
            noise_suppression=self.config.noise_suppression,
            high_pass_filter=self.config.high_pass_filter,
            auto_gain_control=self.config.automatic_gain_control,
        )
        apm.set_stream_delay_ms(self.config.stream_delay_ms)
        return apm

    @staticmethod
    def _float_to_pcm16(samples: Sequence[float]) -> bytes:
        values = array("h")
        values.extend(
            max(-32768, min(32767, round(max(-1.0, min(1.0, sample)) * 32767.0)))
            for sample in samples
        )
        return values.tobytes()

    @staticmethod
    def _pcm16_to_float(data: bytes) -> tuple[float, ...]:
        values = array("h")
        values.frombytes(data)
        return tuple(sample / 32768.0 for sample in values)

    def _frame(self, samples: Sequence[float]) -> Any:
        return self._rtc.AudioFrame(
            data=self._float_to_pcm16(samples),
            sample_rate=self.config.sample_rate,
            num_channels=1,
            samples_per_channel=self.config.apm_frame_samples,
        )

    def process_pair(
        self,
        reference: Sequence[float],
        microphone: Sequence[float],
    ) -> tuple[float, ...]:
        """Process equal-duration reference and microphone samples.

        Every reverse-stream frame is submitted immediately before the matching
        microphone frame while one lock prevents interleaving by callers.
        """

        if len(reference) != len(microphone):
            raise ValueError("reference and microphone blocks must have equal lengths")
        frame_samples = self.config.apm_frame_samples
        if not reference or len(reference) % frame_samples:
            raise ValueError("paired blocks must contain whole 10 ms APM frames")
        output: list[float] = []
        with self._lock:
            for offset in range(0, len(reference), frame_samples):
                reference_chunk = reference[offset : offset + frame_samples]
                microphone_chunk = microphone[offset : offset + frame_samples]
                self._apm.process_reverse_stream(self._frame(reference_chunk))
                microphone_frame = self._frame(microphone_chunk)
                self._apm.process_stream(microphone_frame)
                output.extend(self._pcm16_to_float(bytes(microphone_frame.data)))
                rms = math.sqrt(sum(sample * sample for sample in reference_chunk) / frame_samples)
                if rms >= self.config.far_end_active_rms_min:
                    self._paired_far_end_active_frames += 1
        return tuple(output)

    def reset_alignment(self) -> None:
        """Create a fresh APM and cold echo-path epoch after realignment."""

        with self._lock:
            self._apm = self._new_apm()
            self._paired_far_end_active_frames = 0
            self._alignment_epoch += 1
            self._stream_alignment_reset_count += 1

    @property
    def state(self) -> AecState:
        with self._lock:
            active_s = self._paired_far_end_active_frames / 100.0
            return AecState(
                echo_path_ready=active_s + 1e-12 >= self.config.echo_path_warmup_s,
                far_end_active_s=active_s,
                alignment_epoch=self._alignment_epoch,
                stream_alignment_reset_count=self._stream_alignment_reset_count,
            )
