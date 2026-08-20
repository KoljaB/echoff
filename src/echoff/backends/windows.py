"""Windows WASAPI reference and microphone capture adapters."""

from __future__ import annotations

import logging
import math
import queue
import sys
import threading
import time
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from ..clock import FixedBlockSampleClock
from ..config import AecConfig
from ..errors import AudioBackendError
from ..models import AudioBlock, DeviceInfo

LOGGER = logging.getLogger(__name__)
NATIVE_CALLBACK_DURATION_S = 0.100


def _native_callback_frames(config: AecConfig) -> int:
    """Return the hardware-qualified 100 ms native callback size."""

    return max(1, round(config.sample_rate * NATIVE_CALLBACK_DURATION_S))


@dataclass(frozen=True, slots=True)
class _RawCallbackPacket:
    """One PortAudio callback payload awaiting non-realtime decoding."""

    payload: bytes | None
    frame_count: int
    time_info: dict[str, float]
    status_flags: int
    callback_monotonic: float


def _load_pyaudio() -> Any:
    try:
        import pyaudiowpatch as pyaudio
    except ImportError as exc:
        raise AudioBackendError(
            "PyAudioWPatch is required for Windows capture; install or reinstall echoff"
        ) from exc
    return pyaudio


def list_windows_devices() -> list[DeviceInfo]:
    """List selectable WASAPI loopback and microphone devices."""

    pyaudio = _load_pyaudio()
    audio = pyaudio.PyAudio()
    try:
        default_reference = dict(audio.get_default_wasapi_loopback())
        default_microphone = dict(audio.get_default_wasapi_device())
        wasapi_index = int(audio.get_host_api_info_by_type(pyaudio.paWASAPI)["index"])
        result: list[DeviceInfo] = []
        for raw in audio.get_loopback_device_info_generator():
            info = dict(raw)
            result.append(
                DeviceInfo(
                    kind="reference",
                    backend="pyaudiowpatch_wasapi_loopback",
                    index=int(info["index"]),
                    name=str(info.get("name") or ""),
                    is_default=int(info["index"]) == int(default_reference["index"]),
                    channels=max(1, int(info.get("maxInputChannels") or 1)),
                    default_sample_rate=float(info.get("defaultSampleRate") or 0.0),
                )
            )
        for index in range(audio.get_device_count()):
            info = dict(audio.get_device_info_by_index(index))
            host_api = info.get("hostApi")
            if (
                host_api is None
                or int(host_api) != wasapi_index
                or int(info.get("maxInputChannels") or 0) <= 0
                or info.get("isLoopbackDevice")
            ):
                continue
            result.append(
                DeviceInfo(
                    kind="microphone",
                    backend="pyaudiowpatch_wasapi_microphone",
                    index=int(info["index"]),
                    name=str(info.get("name") or ""),
                    is_default=int(info["index"]) == int(default_microphone["index"]),
                    channels=int(info.get("maxInputChannels") or 1),
                    default_sample_rate=float(info.get("defaultSampleRate") or 0.0),
                )
            )
        return sorted(result, key=lambda item: (item.kind, not item.is_default, item.index))
    finally:
        audio.terminate()


class _SharedWasapiContext:
    """Serialize every PortAudio host operation on one control thread."""

    def __init__(self, users: int) -> None:
        self.pyaudio: Any | None = None
        self.audio: Any | None = None
        self._remaining_users = users
        self._stop_requested = False
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._calls: queue.Queue[
            tuple[
                Callable[[], Any],
                threading.Event,
                list[Any],
                list[BaseException],
            ]
            | None
        ] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name="echoff-wasapi-control",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(3.0):
            raise AudioBackendError("PortAudio control thread did not become ready")
        if self._startup_error is not None:
            raise AudioBackendError(
                f"PortAudio initialization failed: {self._startup_error}"
            ) from self._startup_error

    @classmethod
    def create(cls, users: int) -> _SharedWasapiContext:
        return cls(users)

    def _run(self) -> None:
        try:
            self.pyaudio = _load_pyaudio()
            self.audio = self.pyaudio.PyAudio()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                item = self._calls.get()
                if item is None:
                    return
                function, finished, results, errors = item
                try:
                    results.append(function())
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()
        finally:
            assert self.audio is not None
            self.audio.terminate()

    def call(self, function: Callable[[], Any]) -> Any:
        if self._startup_error is not None:
            raise self._startup_error
        finished = threading.Event()
        results: list[Any] = []
        errors: list[BaseException] = []
        self._calls.put((function, finished, results, errors))
        if not finished.wait(3.0):
            raise AudioBackendError("PortAudio control operation timed out")
        if errors:
            raise errors[0]
        return results[0] if results else None

    def reinitialize(self) -> Any:
        """Replace the inactive PortAudio manager on the control thread once."""

        def replace() -> Any:
            old_audio = self.audio
            if old_audio is None or self.pyaudio is None:
                raise AudioBackendError("PortAudio context is not initialized")
            old_audio.terminate()
            self.audio = self.pyaudio.PyAudio()
            return self.audio

        return self.call(replace)

    def release(self) -> None:
        request_stop = False
        should_join = False
        with self._lock:
            if self._remaining_users > 0:
                self._remaining_users -= 1
            if self._remaining_users == 0:
                should_join = True
                if not self._stop_requested:
                    self._stop_requested = True
                    request_stop = True
        if request_stop:
            self._calls.put(None)
        if should_join:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                raise AudioBackendError("PortAudio control thread did not stop")


class _ThreadedSource:
    callback: Callable[[AudioBlock], None]
    config: AecConfig

    backend_name = "unknown"
    source_label = "audio"

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.activate_event = threading.Event()
        self.active_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: Exception | None = None
        self.device_block_count = 0
        self.synthetic_silence_block_count = 0
        self.dropped_device_block_count = 0
        self.timestamp_regression_count = 0
        self.invalid_timestamp_count = 0
        self.timestamp_deviation_max_s = 0.0
        self.selected_device_name: str | None = None
        self.selected_device_index: int | None = None
        self._timeline_anchor_end: float | None = None
        self._timeline_last_end: float | None = None
        self._reported_prediction_end: float | None = None
        self._last_reported_end: float | None = None
        self.timestamp_gap_block_count = 0
        self.timestamp_anomaly_count = 0
        self.callback_status_count = 0
        self.input_overflow_count = 0
        self.input_underflow_count = 0
        self.padded_sample_count = 0
        self._sample_clock: FixedBlockSampleClock | None = None
        self.callback_packet_count = 0
        self.callback_payload_frame_count = 0
        self.callback_queue_high_watermark = 0
        self.callback_queue_overflow_count = 0
        self.callback_queue_age_max_s = 0.0
        self.callback_enqueue_max_s = 0.0
        self.callback_timeline_drift_s = 0.0
        self.callback_timeline_drift_max_s = 0.0
        self._first_callback_monotonic: float | None = None
        self._first_callback_frame_count = 0
        self._callback_queue: queue.Queue[_RawCallbackPacket | None] = queue.Queue()
        self._callback_decoder: threading.Thread | None = None
        self._callback_decoder_error: Exception | None = None
        self._decoded_callback_packet_count = 0
        self._resource_lock = threading.Lock()
        self._resources_released = False
        self._telemetry_lock = threading.Lock()

    def _configure_sample_clock(self, config: AecConfig) -> None:
        self._sample_clock = FixedBlockSampleClock(
            sample_rate=config.sample_rate,
            block_samples=config.block_samples,
        )
        callback_queue_capacity = max(
            1,
            math.ceil(config.queue_fatal_s / NATIVE_CALLBACK_DURATION_S),
        )
        self._callback_queue = queue.Queue(maxsize=callback_queue_capacity)

    def _sync_clock_counters(self) -> None:
        if self._sample_clock is None:
            return
        with self._telemetry_lock:
            self.timestamp_regression_count = self._sample_clock.timestamp_regression_count
            self.invalid_timestamp_count = self._sample_clock.invalid_timestamp_count
            self.timestamp_deviation_max_s = self._sample_clock.timestamp_deviation_max_s
            self.timestamp_anomaly_count = self._sample_clock.timestamp_anomaly_count
            self.padded_sample_count = self._sample_clock.padded_sample_count
            # Timestamps are observations only. They never imply missing blocks.
            self.timestamp_gap_block_count = 0

    def _note_status(self, status_flags: int, pyaudio: Any | None = None) -> bool:
        flags = int(status_flags)
        if not flags:
            return False
        input_underflow = int(getattr(pyaudio, "paInputUnderflow", 0x01))
        input_overflow = int(getattr(pyaudio, "paInputOverflow", 0x02))
        discontinuity = False
        with self._telemetry_lock:
            self.callback_status_count += 1
            if flags & input_underflow:
                self.input_underflow_count += 1
                discontinuity = True
            if flags & input_overflow:
                self.input_overflow_count += 1
                discontinuity = True
        return discontinuity

    def start(self) -> None:
        if self.thread is not None:
            raise RuntimeError(f"{self.source_label} source already started")
        self.stop_event.clear()
        self.activate_event.clear()
        self.active_event.clear()
        self.thread = threading.Thread(
            target=self._run_guarded,
            name=f"echoff-{self.source_label}",
            daemon=True,
        )
        self.thread.start()
        if not self.ready_event.wait(3.0):
            raise AudioBackendError(f"{self.source_label} source did not become ready")
        if self.error is not None:
            raise AudioBackendError(
                f"{self.source_label} source failed: {self.error}"
            ) from self.error

    def stop(self) -> None:
        self.stop_event.set()
        self.activate_event.set()
        try:
            if self.thread is not None:
                # The source thread first stops its native stream and then drains
                # every callback packet through the decoder. A bounded join here
                # would silently abandon queued device payloads during shutdown.
                self.thread.join()
        finally:
            self._release_resources()

    def _release_resources(self) -> None:
        context = getattr(self, "_context", None)
        if context is None:
            return
        with self._resource_lock:
            if self._resources_released:
                return
            context.release()
            self._resources_released = True

    def activate(self) -> None:
        if self.thread is None or not self.ready_event.is_set():
            raise RuntimeError(f"{self.source_label} source is not prepared")
        self.activate_event.set()

    def _mark_active(
        self,
        *,
        backend_name: str,
        selected_device_name: str | None,
        selected_device_index: int,
    ) -> None:
        """Publish selected metadata atomically before declaring the stream active."""

        self.backend_name = backend_name
        self.selected_device_name = selected_device_name
        self.selected_device_index = selected_device_index
        self.active_event.set()

    def _run_guarded(self) -> None:
        try:
            self._run()
        except Exception as exc:
            LOGGER.exception("%s capture failed", self.source_label)
            self.error = exc
            self.ready_event.set()
            self.stop_event.set()

    def _run(self) -> None:
        raise NotImplementedError

    def _wait_for_activation(self) -> bool:
        while not self.stop_event.is_set():
            if self.activate_event.wait(0.05):
                return not self.stop_event.is_set()
        return False

    def _enqueue_callback_packet(
        self,
        payload: bytes | None,
        frame_count: int,
        time_info: dict[str, float],
        status_flags: int,
    ) -> None:
        """Perform the constant-time portion of a PortAudio callback."""

        started = time.perf_counter()
        callback_monotonic = time.monotonic()
        # PortAudio owns the small time-info mapping. Preserve the two values
        # needed by the decoder rather than retaining backend-owned state.
        observed_time = {
            "input_buffer_adc_time": time_info.get("input_buffer_adc_time", float("nan")),
            "current_time": time_info.get("current_time", float("nan")),
        }
        packet = _RawCallbackPacket(
            payload=payload,
            frame_count=int(frame_count),
            time_info=observed_time,
            status_flags=int(status_flags),
            callback_monotonic=callback_monotonic,
        )
        with self._telemetry_lock:
            try:
                self._callback_queue.put_nowait(packet)
            except queue.Full as exc:
                self.callback_queue_overflow_count += 1
                raise AudioBackendError(
                    f"{self.source_label} callback queue exceeded its configured "
                    f"fatal capacity: {self._callback_queue.maxsize} packets"
                ) from exc
            self.callback_packet_count += 1
            self.callback_payload_frame_count += max(0, int(frame_count))
            depth = max(1, self._callback_queue.qsize())
            self.callback_queue_high_watermark = max(
                self.callback_queue_high_watermark,
                depth,
            )
            if self._first_callback_monotonic is None:
                self._first_callback_monotonic = callback_monotonic
                self._first_callback_frame_count = max(0, int(frame_count))
            else:
                payload_after_first = (
                    self.callback_payload_frame_count - self._first_callback_frame_count
                )
                drift = (
                    callback_monotonic
                    - self._first_callback_monotonic
                    - payload_after_first / self.config.sample_rate
                )
                self.callback_timeline_drift_s = drift
                self.callback_timeline_drift_max_s = max(
                    self.callback_timeline_drift_max_s,
                    abs(drift),
                )
            self.callback_enqueue_max_s = max(
                self.callback_enqueue_max_s,
                time.perf_counter() - started,
            )

    def _start_callback_decoder(
        self,
        decode: Callable[[_RawCallbackPacket], None],
    ) -> None:
        if self._callback_decoder is not None:
            raise RuntimeError(f"{self.source_label} callback decoder already started")

        def run() -> None:
            try:
                while True:
                    packet = self._callback_queue.get()
                    if packet is None:
                        return
                    with self._telemetry_lock:
                        self.callback_queue_age_max_s = max(
                            self.callback_queue_age_max_s,
                            time.monotonic() - packet.callback_monotonic,
                        )
                    decode(packet)
                    with self._telemetry_lock:
                        self._decoded_callback_packet_count += 1
            except Exception as exc:
                self._callback_decoder_error = exc
                self.stop_event.set()

        self._callback_decoder = threading.Thread(
            target=run,
            name=f"echoff-{self.source_label}-decoder",
            daemon=True,
        )
        self._callback_decoder.start()

    def _stop_callback_decoder(self) -> None:
        decoder = self._callback_decoder
        if decoder is None:
            return
        while decoder.is_alive():
            try:
                self._callback_queue.put(None, timeout=0.050)
                break
            except queue.Full:
                continue
        decoder.join()
        self._callback_decoder = None

    def _decoder_has_pending_packets(self) -> bool:
        with self._telemetry_lock:
            return self._decoded_callback_packet_count < self.callback_packet_count

    def diagnostics_snapshot(self) -> dict[str, int | float | bool | tuple[str, ...]]:
        """Return one internally consistent source-telemetry snapshot."""

        with self._telemetry_lock:
            return {
                "device_block_count": self.device_block_count,
                "synthetic_silence_block_count": self.synthetic_silence_block_count,
                "dropped_device_block_count": self.dropped_device_block_count,
                "timestamp_regression_count": self.timestamp_regression_count,
                "invalid_timestamp_count": self.invalid_timestamp_count,
                "timestamp_deviation_max_s": self.timestamp_deviation_max_s,
                "timestamp_gap_block_count": self.timestamp_gap_block_count,
                "timestamp_anomaly_count": self.timestamp_anomaly_count,
                "callback_status_count": self.callback_status_count,
                "input_overflow_count": self.input_overflow_count,
                "input_underflow_count": self.input_underflow_count,
                "padded_sample_count": self.padded_sample_count,
                "callback_packet_count": self.callback_packet_count,
                "callback_payload_frame_count": self.callback_payload_frame_count,
                "callback_queue_high_watermark": self.callback_queue_high_watermark,
                "callback_queue_high_watermark_blocks": math.ceil(
                    self.callback_queue_high_watermark
                    * NATIVE_CALLBACK_DURATION_S
                    / self.config.block_duration_s
                ),
                "callback_queue_high_watermark_packets": self.callback_queue_high_watermark,
                "callback_queue_overflow_count": self.callback_queue_overflow_count,
                "callback_queue_age_max_s": self.callback_queue_age_max_s,
                "callback_enqueue_max_s": self.callback_enqueue_max_s,
                "callback_timeline_drift_s": self.callback_timeline_drift_s,
                "callback_timeline_drift_max_s": self.callback_timeline_drift_max_s,
                "fallback_used": bool(getattr(self, "fallback_used", False)),
                "backend_attempt_errors": tuple(
                    getattr(self, "backend_attempt_errors", ())
                ),
            }

    def _raise_callback_decoder_error(self) -> None:
        if self._callback_decoder_error is not None:
            raise self._callback_decoder_error

    def _confirm_callback_stream_started(
        self,
        is_active: Callable[[], bool],
        *,
        failure_message: str,
    ) -> bool:
        """Distinguish immediate native failure from a test/user-requested stop."""

        if self.stop_event.is_set():
            return False
        if is_active():
            return True
        deadline = time.monotonic() + 0.050
        while time.monotonic() < deadline:
            if self.stop_event.wait(0.005):
                return False
            if is_active():
                return True
        if self.stop_event.is_set():
            return False
        if not is_active():
            raise AudioBackendError(failure_message)
        return True

    def _emit_samples(
        self,
        samples: list[float],
        *,
        callback_monotonic: float,
        time_info: dict[str, float],
        status_flags: int,
        discontinuity: bool,
    ) -> None:
        if self._sample_clock is None:  # pragma: no cover - configured by subclasses
            raise RuntimeError("sample clock is not configured")
        try:
            adc_start = float(time_info["input_buffer_adc_time"])
            current_time = float(time_info["current_time"])
        except (KeyError, TypeError, ValueError):
            adc_start = None
            current_time = None
        blocks = self._sample_clock.push(
            samples,
            callback_monotonic=callback_monotonic,
            adc_start=adc_start,
            current_time=current_time,
            status_flags=status_flags,
            discontinuity=discontinuity,
        )
        self._sync_clock_counters()
        for block in blocks:
            self.callback(block)
            with self._telemetry_lock:
                self.device_block_count += 1

    def _flush_sample_clock(self) -> None:
        if self._sample_clock is None:
            return
        for block in self._sample_clock.flush(callback_monotonic=time.monotonic()):
            self.callback(block)
            with self._telemetry_lock:
                self.device_block_count += 1
        self._sync_clock_counters()

    def _finish_wasapi_stream(
        self,
        context: _SharedWasapiContext,
        stream: Any | None,
        *,
        stream_started: bool,
        primary_error: BaseException | None,
        callback_errors: list[Exception],
        callback_error_lock: threading.Lock,
    ) -> None:
        """Run every cleanup action without replacing the primary failure."""

        cleanup_errors: list[Exception] = []
        late_callback_errors: list[Exception] = []
        promoted_callback_error: Exception | None = None
        callback_error_count = len(callback_errors) if primary_error is not None else 0
        decoder_error_seen = (
            self._callback_decoder_error if primary_error is not None else None
        )

        def inspect_callback_errors() -> None:
            nonlocal callback_error_count, decoder_error_seen, promoted_callback_error
            with callback_error_lock:
                current_errors = list(callback_errors)
            new_errors = current_errors[callback_error_count:]
            callback_error_count = len(current_errors)
            decoder_error = self._callback_decoder_error
            new_decoder_error = (
                decoder_error if decoder_error is not decoder_error_seen else None
            )
            decoder_error_seen = decoder_error
            errors = list(new_errors)
            if new_decoder_error is not None:
                errors.append(new_decoder_error)
            for error in errors:
                if primary_error is None and promoted_callback_error is None:
                    promoted_callback_error = error
                else:
                    late_callback_errors.append(error)

        if stream is not None and stream_started:
            try:
                context.call(stream.stop_stream)
            except Exception as exc:
                cleanup_errors.append(exc)
            inspect_callback_errors()
        try:
            self._stop_callback_decoder()
        except Exception as exc:
            cleanup_errors.append(exc)
        inspect_callback_errors()
        if stream is not None:
            try:
                context.call(stream.close)
            except Exception as exc:
                cleanup_errors.append(exc)
            inspect_callback_errors()
        try:
            self._flush_sample_clock()
        except Exception as exc:
            cleanup_errors.append(exc)
        inspect_callback_errors()
        try:
            self._release_resources()
        except Exception as exc:
            cleanup_errors.append(exc)
        inspect_callback_errors()
        if late_callback_errors:
            for error in late_callback_errors:
                LOGGER.error(
                    "%s callback shutdown error is secondary to the primary failure: %s",
                    self.source_label,
                    error,
                )
        if primary_error is None and promoted_callback_error is not None:
            if cleanup_errors:
                LOGGER.error(
                    "%s cleanup also failed while preserving callback error %r: %s",
                    self.source_label,
                    promoted_callback_error,
                    "; ".join(str(error) for error in cleanup_errors),
                )
            raise promoted_callback_error
        if not cleanup_errors:
            return
        details = "; ".join(str(error) for error in cleanup_errors)
        if primary_error is None:
            raise AudioBackendError(
                f"{self.source_label} cleanup failed: {details}"
            ) from cleanup_errors[0]
        LOGGER.error(
            "%s cleanup also failed while preserving primary error %r: %s",
            self.source_label,
            primary_error,
            details,
        )

    def _finish_sounddevice_stream(
        self,
        stream: Any,
        *,
        stream_started: bool,
        primary_error: BaseException | None,
        callback_errors: list[Exception],
        callback_error_lock: threading.Lock,
    ) -> None:
        cleanup_errors: list[Exception] = []
        late_callback_errors: list[Exception] = []
        promoted_callback_error: Exception | None = None
        callback_error_count = len(callback_errors) if primary_error is not None else 0
        decoder_error_seen = (
            self._callback_decoder_error if primary_error is not None else None
        )

        def inspect_callback_errors() -> None:
            nonlocal callback_error_count, decoder_error_seen, promoted_callback_error
            with callback_error_lock:
                current_errors = list(callback_errors)
            new_errors = current_errors[callback_error_count:]
            callback_error_count = len(current_errors)
            decoder_error = self._callback_decoder_error
            new_decoder_error = (
                decoder_error if decoder_error is not decoder_error_seen else None
            )
            decoder_error_seen = decoder_error
            errors = list(new_errors)
            if new_decoder_error is not None:
                errors.append(new_decoder_error)
            for error in errors:
                if primary_error is None and promoted_callback_error is None:
                    promoted_callback_error = error
                else:
                    late_callback_errors.append(error)

        if stream_started:
            try:
                stream.stop()
            except Exception as exc:
                cleanup_errors.append(exc)
        inspect_callback_errors()
        try:
            self._stop_callback_decoder()
        except Exception as exc:
            cleanup_errors.append(exc)
        inspect_callback_errors()
        try:
            stream.close()
        except Exception as exc:
            cleanup_errors.append(exc)
        inspect_callback_errors()
        try:
            self._flush_sample_clock()
        except Exception as exc:
            cleanup_errors.append(exc)
        inspect_callback_errors()
        if late_callback_errors:
            for error in late_callback_errors:
                LOGGER.error(
                    "%s callback shutdown error is secondary to the primary failure: %s",
                    self.source_label,
                    error,
                )
        if primary_error is None and promoted_callback_error is not None:
            if cleanup_errors:
                LOGGER.error(
                    "%s cleanup also failed while preserving callback error %r: %s",
                    self.source_label,
                    promoted_callback_error,
                    "; ".join(str(error) for error in cleanup_errors),
                )
            raise promoted_callback_error
        if not cleanup_errors:
            return
        details = "; ".join(str(error) for error in cleanup_errors)
        if primary_error is None:
            raise AudioBackendError(
                f"{self.source_label} fallback cleanup failed: {details}"
            ) from cleanup_errors[0]
        LOGGER.error(
            "%s fallback cleanup also failed while preserving primary error %r: %s",
            self.source_label,
            primary_error,
            details,
        )


class WasapiReferenceSource(_ThreadedSource):
    """Clock-continuous capture of a Windows render endpoint."""

    backend_name = "pyaudiowpatch_wasapi_loopback"
    source_label = "reference"

    def __init__(
        self,
        config: AecConfig,
        callback: Callable[[AudioBlock], None],
        device: str | None = None,
        *,
        _context: _SharedWasapiContext | None = None,
    ) -> None:
        super().__init__()
        self.backend_name = "uninitialized"
        self.config = config
        self._configure_sample_clock(config)
        self.callback = callback
        self.device = device
        self._context = _context

    def _select_device(self, audio: Any) -> dict[str, Any]:
        if self.device is None:
            return dict(audio.get_default_wasapi_loopback())
        requested = str(self.device).casefold()
        try:
            index = int(self.device)
        except (TypeError, ValueError):
            index = None
        if index is not None:
            info = dict(audio.get_device_info_by_index(index))
            if not info.get("isLoopbackDevice"):
                info = dict(audio.get_wasapi_loopback_analogue_by_dict(info))
            return info
        matches = [
            dict(info)
            for info in audio.get_loopback_device_info_generator()
            if requested in str(info.get("name", "")).casefold()
        ]
        if not matches:
            raise AudioBackendError(f"WASAPI loopback device not found: {self.device}")
        if len(matches) > 1:
            raise AudioBackendError(
                f"WASAPI loopback device selector is ambiguous: {self.device!r}; "
                f"matches={[item.get('name') for item in matches]}"
            )
        return matches[0]

    def _run(self) -> None:
        context = self._context or _SharedWasapiContext.create(1)
        self._context = context
        pyaudio = context.pyaudio
        audio = context.audio
        if pyaudio is None or audio is None:  # pragma: no cover - guarded by context startup
            raise AudioBackendError("PortAudio context is not initialized")
        stream = None
        stream_started = False
        callback_errors: list[Exception] = []
        callback_error_lock = threading.Lock()
        try:
            info = context.call(lambda: self._select_device(audio))
            selected_device_name = str(info.get("name") or "") or None
            selected_device_index = int(info["index"])
            channels = max(1, int(info.get("maxInputChannels") or 1))
            block_frames = self.config.block_samples
            # The 100 ms callback cadence is the hardware-qualified default.
            # Payloads are still emitted downstream as fixed-size AEC blocks.
            callback_frames = _native_callback_frames(self.config)

            def decode(packet: _RawCallbackPacket) -> None:
                discontinuity = self._note_status(packet.status_flags, pyaudio)
                if packet.payload is None:
                    pcm = array("h", [0] * max(0, packet.frame_count * channels))
                    with self._telemetry_lock:
                        self.synthetic_silence_block_count += max(
                            1,
                            math.ceil(max(0, packet.frame_count) / block_frames),
                        )
                    discontinuity = True
                else:
                    pcm = array("h")
                    pcm.frombytes(packet.payload)
                if len(pcm) % channels:
                    raise AudioBackendError(
                        "reference callback payload does not contain whole channel frames"
                    )
                actual_frames = len(pcm) // channels
                if actual_frames <= 0:
                    return
                scale = 1.0 / 32768.0
                if channels == 1:
                    samples = [value * scale for value in pcm]
                else:
                    samples = [
                        sum(pcm[index : index + channels]) * scale / channels
                        for index in range(0, len(pcm), channels)
                    ]
                self._emit_samples(
                    samples,
                    callback_monotonic=packet.callback_monotonic,
                    time_info=packet.time_info,
                    status_flags=packet.status_flags,
                    discontinuity=discontinuity,
                )

            self._start_callback_decoder(decode)

            def on_audio(
                payload: bytes | None,
                frame_count: int,
                time_info: dict[str, float],
                status_flags: int,
            ) -> tuple[None, int]:
                try:
                    self._enqueue_callback_packet(
                        payload,
                        frame_count,
                        time_info,
                        status_flags,
                    )
                    return (
                        None,
                        pyaudio.paComplete if self.stop_event.is_set() else pyaudio.paContinue,
                    )
                except Exception as exc:
                    with callback_error_lock:
                        callback_errors.append(exc)
                    self.stop_event.set()
                    return None, pyaudio.paAbort

            try:
                stream = context.call(
                    lambda: audio.open(
                        format=pyaudio.paInt16,
                        channels=channels,
                        rate=self.config.sample_rate,
                        input=True,
                        input_device_index=selected_device_index,
                        frames_per_buffer=callback_frames,
                        start=False,
                        stream_callback=on_audio,
                    )
                )
            except Exception as exc:
                raise AudioBackendError(
                    "failed to open WASAPI reference device "
                    f"{selected_device_name!r}: {exc}"
                ) from exc
            self.ready_event.set()
            LOGGER.info(
                "reference device prepared: %s (index %s)",
                selected_device_name,
                selected_device_index,
            )
            if not self._wait_for_activation():
                return
            try:
                context.call(stream.start_stream)
            except Exception as exc:
                try:
                    stream_started = bool(context.call(stream.is_active))
                except Exception:
                    # A failed activity probe leaves the native state unknown.
                    # Stop before close and retain the original start failure.
                    stream_started = True
                    LOGGER.exception("could not probe partially started reference stream")
                raise AudioBackendError(
                    "failed to start WASAPI reference device "
                    f"{selected_device_name!r}: {exc}"
                ) from exc
            stream_started = True
            with callback_error_lock:
                callback_failure = None if not callback_errors else callback_errors[0]
            if callback_failure is not None:
                raise callback_failure
            self._raise_callback_decoder_error()
            if not self._confirm_callback_stream_started(
                lambda: bool(context.call(stream.is_active)),
                failure_message="reference PortAudio callback stream stopped at start",
            ):
                with callback_error_lock:
                    callback_failure = None if not callback_errors else callback_errors[0]
                if callback_failure is not None:
                    raise callback_failure
                self._raise_callback_decoder_error()
                return
            self._mark_active(
                backend_name="pyaudiowpatch_wasapi_loopback",
                selected_device_name=selected_device_name,
                selected_device_index=selected_device_index,
            )
            while not self.stop_event.wait(0.05):
                with callback_error_lock:
                    callback_failure = None if not callback_errors else callback_errors[0]
                if callback_failure is not None:
                    raise callback_failure
                self._raise_callback_decoder_error()
                if (
                    not context.call(stream.is_active)
                    and not self._decoder_has_pending_packets()
                ):
                    raise AudioBackendError("reference PortAudio callback stream stopped")
            with callback_error_lock:
                callback_failure = None if not callback_errors else callback_errors[0]
            if callback_failure is not None:
                raise callback_failure
            self._raise_callback_decoder_error()
        finally:
            self._finish_wasapi_stream(
                context,
                stream,
                stream_started=stream_started,
                primary_error=sys.exc_info()[1],
                callback_errors=callback_errors,
                callback_error_lock=callback_error_lock,
            )
        self._raise_callback_decoder_error()


class WasapiMicrophoneSource(_ThreadedSource):
    """Capture the native Windows microphone clock at 48 kHz."""

    backend_name = "pyaudiowpatch_wasapi_microphone"
    source_label = "microphone"

    def __init__(
        self,
        config: AecConfig,
        callback: Callable[[AudioBlock], None],
        device: str | None = None,
        *,
        _context: _SharedWasapiContext | None = None,
    ) -> None:
        super().__init__()
        self.backend_name = "uninitialized"
        self.config = config
        self._configure_sample_clock(config)
        self.callback = callback
        self.device = device
        self._context = _context
        self.backend_attempt_errors: list[str] = []
        self.fallback_used = False

    def _select_device(self, audio: Any, pyaudio: Any) -> dict[str, Any]:
        if self.device is None:
            return dict(audio.get_default_wasapi_device())
        requested = str(self.device).casefold()
        try:
            index = int(self.device)
        except (TypeError, ValueError):
            index = None
        if index is not None:
            info = dict(audio.get_device_info_by_index(index))
            if int(info.get("maxInputChannels") or 0) <= 0 or info.get("isLoopbackDevice"):
                raise AudioBackendError(f"device {index} is not a microphone input")
            return info
        wasapi_index = int(audio.get_host_api_info_by_type(pyaudio.paWASAPI)["index"])
        matches: list[dict[str, Any]] = []
        for candidate_index in range(audio.get_device_count()):
            info = dict(audio.get_device_info_by_index(candidate_index))
            host_api = info.get("hostApi")
            if (
                host_api is not None
                and int(host_api) == wasapi_index
                and int(info.get("maxInputChannels") or 0) > 0
                and not info.get("isLoopbackDevice")
                and requested in str(info.get("name", "")).casefold()
            ):
                matches.append(info)
        if not matches:
            raise AudioBackendError(f"WASAPI microphone device not found: {self.device}")
        if len(matches) > 1:
            raise AudioBackendError(
                f"WASAPI microphone selector is ambiguous: {self.device!r}; "
                f"matches={[item.get('name') for item in matches]}"
            )
        return matches[0]

    @staticmethod
    def _device_name_key(value: object) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        words = "".join(
            character if character.isalnum() else " " for character in text.casefold()
        ).split()
        key = "".join(
            word for word in words if word not in {"mikrofon", "microphone", "mic", "2"}
        )
        if key in {"none", "unknown", "unnamed"}:
            return ""
        return key

    def _select_wdmks_fallback_device(
        self,
        sounddevice: Any,
        preferred_name: object,
    ) -> tuple[int, dict[str, Any]]:
        host_apis = sounddevice.query_hostapis()
        candidates: list[tuple[int, dict[str, Any]]] = []
        for index, raw in enumerate(sounddevice.query_devices()):
            info = dict(raw)
            host_name = str(host_apis[int(info["hostapi"])]["name"])
            if "wdm-ks" in host_name.casefold() and int(info.get("max_input_channels") or 0) > 0:
                candidates.append((index, info))
        preferred_key = self._device_name_key(preferred_name)
        matching: list[tuple[int, dict[str, Any]]] = []
        if preferred_key:
            for item in candidates:
                candidate_key = self._device_name_key(item[1].get("name"))
                if candidate_key and candidate_key == preferred_key:
                    matching.append(item)
        if len(matching) == 1:
            return matching[0]
        raise AudioBackendError(
            "could not prove one matching WDM-KS fallback for microphone "
            f"{preferred_name!r}: {[item[1].get('name') for item in candidates]}"
        )

    def _run_wdmks_fallback(
        self,
        *,
        preferred_name: object,
        callback_frames: int,
    ) -> None:
        try:
            import sounddevice
        except ImportError as exc:
            raise AudioBackendError(
                "sounddevice is required for the WDM-KS microphone fallback"
            ) from exc
        device_index, info = self._select_wdmks_fallback_device(sounddevice, preferred_name)
        selected_device_name = str(info.get("name") or "") or None
        callback_error: list[Exception] = []
        callback_error_lock = threading.Lock()
        stream_started = False

        self._stop_callback_decoder()

        def decode(packet: _RawCallbackPacket) -> None:
            if packet.payload is None:
                raise AudioBackendError("WDM-KS microphone returned an empty payload")
            samples = array("f")
            samples.frombytes(packet.payload)
            if len(samples) != packet.frame_count:
                raise AudioBackendError(
                    "WDM-KS microphone payload frame count does not match callback metadata"
                )
            discontinuity = self._note_status(packet.status_flags)
            self._emit_samples(
                samples.tolist(),
                callback_monotonic=packet.callback_monotonic,
                time_info=packet.time_info,
                status_flags=packet.status_flags,
                discontinuity=discontinuity,
            )

        self._start_callback_decoder(decode)

        def on_audio(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            try:
                flags = 0
                if bool(getattr(status, "input_underflow", False)):
                    flags |= 0x01
                if bool(getattr(status, "input_overflow", False)):
                    flags |= 0x02
                if status and not flags:
                    flags = 0x20
                self._enqueue_callback_packet(
                    indata.tobytes(),
                    frames,
                    {
                        "input_buffer_adc_time": float(time_info.inputBufferAdcTime),
                        "current_time": float(time_info.currentTime),
                    },
                    flags,
                )
            except Exception as exc:
                with callback_error_lock:
                    callback_error.append(exc)
                raise sounddevice.CallbackAbort from exc

        try:
            stream = sounddevice.InputStream(
                device=device_index,
                channels=1,
                samplerate=self.config.sample_rate,
                dtype="float32",
                blocksize=callback_frames,
                callback=on_audio,
            )
        except Exception as exc:
            raise AudioBackendError(
                f"failed to open WDM-KS microphone {selected_device_name!r}: {exc}"
            ) from exc
        try:
            self.ready_event.set()
            LOGGER.info(
                "microphone fallback prepared: %s (index %s)",
                selected_device_name,
                device_index,
            )
            if not self._wait_for_activation():
                return
            try:
                stream.start()
            except Exception as exc:
                try:
                    stream_started = bool(getattr(stream, "active", False))
                except Exception:
                    # A failed activity probe leaves the native state unknown.
                    # Stop before close and retain the original start failure.
                    stream_started = True
                    LOGGER.exception("could not probe partially started WDM-KS stream")
                raise AudioBackendError(
                    f"failed to start WDM-KS microphone {selected_device_name!r}: {exc}"
                ) from exc
            stream_started = True
            with callback_error_lock:
                failure = None if not callback_error else callback_error[0]
            if failure is not None:
                raise failure
            self._raise_callback_decoder_error()
            if not self._confirm_callback_stream_started(
                lambda: bool(getattr(stream, "active", True)),
                failure_message="WDM-KS microphone callback stream stopped at start",
            ):
                with callback_error_lock:
                    failure = None if not callback_error else callback_error[0]
                if failure is not None:
                    raise failure
                self._raise_callback_decoder_error()
                return
            with self._telemetry_lock:
                self.fallback_used = True
            self._mark_active(
                backend_name="sounddevice_wdmks_microphone",
                selected_device_name=selected_device_name,
                selected_device_index=device_index,
            )
            while not self.stop_event.wait(0.05):
                with callback_error_lock:
                    failure = None if not callback_error else callback_error[0]
                if failure is not None:
                    raise failure
                self._raise_callback_decoder_error()
                if (
                    not bool(getattr(stream, "active", True))
                    and not self._decoder_has_pending_packets()
                ):
                    raise AudioBackendError("WDM-KS microphone callback stream stopped")
            with callback_error_lock:
                failure = None if not callback_error else callback_error[0]
            if failure is not None:
                raise failure
            self._raise_callback_decoder_error()
        finally:
            self._finish_sounddevice_stream(
                stream,
                stream_started=stream_started,
                primary_error=sys.exc_info()[1],
                callback_errors=callback_error,
                callback_error_lock=callback_error_lock,
            )

    def _run(self) -> None:
        context = self._context or _SharedWasapiContext.create(1)
        self._context = context
        pyaudio = context.pyaudio
        audio = context.audio
        if pyaudio is None or audio is None:  # pragma: no cover - guarded by context startup
            raise AudioBackendError("PortAudio context is not initialized")
        stream = None
        stream_started = False
        callback_errors: list[Exception] = []
        callback_error_lock = threading.Lock()
        try:
            block_frames = self.config.block_samples
            callback_frames = _native_callback_frames(self.config)
            info: dict[str, Any] | None = None
            open_errors: list[Exception] = []

            def decode(packet: _RawCallbackPacket) -> None:
                discontinuity = self._note_status(packet.status_flags, pyaudio)
                if packet.payload is None:
                    pcm = array("h", [0] * max(0, packet.frame_count))
                    with self._telemetry_lock:
                        self.synthetic_silence_block_count += max(
                            1,
                            math.ceil(max(0, packet.frame_count) / block_frames),
                        )
                    discontinuity = True
                else:
                    pcm = array("h")
                    pcm.frombytes(packet.payload)
                if not pcm:
                    return
                self._emit_samples(
                    [value / 32768.0 for value in pcm],
                    callback_monotonic=packet.callback_monotonic,
                    time_info=packet.time_info,
                    status_flags=packet.status_flags,
                    discontinuity=discontinuity,
                )

            self._start_callback_decoder(decode)

            def on_audio(
                payload: bytes | None,
                frame_count: int,
                time_info: dict[str, float],
                status_flags: int,
            ) -> tuple[None, int]:
                try:
                    self._enqueue_callback_packet(
                        payload,
                        frame_count,
                        time_info,
                        status_flags,
                    )
                    return (
                        None,
                        pyaudio.paComplete if self.stop_event.is_set() else pyaudio.paContinue,
                    )
                except Exception as exc:
                    with callback_error_lock:
                        callback_errors.append(exc)
                    self.stop_event.set()
                    return None, pyaudio.paAbort

            for attempt in range(2):
                info = context.call(partial(self._select_device, audio, pyaudio))
                try:
                    selected_index = int(info["index"])
                    stream = context.call(
                        partial(
                            audio.open,
                            format=pyaudio.paInt16,
                            channels=1,
                            rate=self.config.sample_rate,
                            input=True,
                            input_device_index=selected_index,
                            frames_per_buffer=callback_frames,
                            start=False,
                            stream_callback=on_audio,
                        )
                    )
                    break
                except Exception as exc:
                    open_errors.append(exc)
                    with self._telemetry_lock:
                        self.backend_attempt_errors.append(
                            f"WASAPI open attempt {attempt + 1}: {exc}"
                        )
                    if attempt == 0:
                        audio = context.reinitialize()
            if stream is None:
                device_name = "unknown" if info is None else str(info.get("name", "unknown"))
                open_error_details = "; ".join(
                    f"attempt {index + 1}: {error}"
                    for index, error in enumerate(open_errors)
                )
                if not self.config.allow_wdmks_microphone_fallback:
                    raise AudioBackendError(
                        f"failed to open WASAPI microphone {device_name!r} after one "
                        f"fresh-context retry: {open_error_details}"
                    )
                try:
                    self._run_wdmks_fallback(
                        preferred_name=device_name,
                        callback_frames=callback_frames,
                    )
                    return
                except Exception as fallback_error:
                    with self._telemetry_lock:
                        self.backend_attempt_errors.append(
                            f"WDM-KS fallback: {fallback_error}"
                        )
                    raise AudioBackendError(
                        f"failed to open WASAPI microphone {device_name!r} after one "
                        "fresh-context retry: "
                        f"{open_error_details}; WDM-KS fallback failed: {fallback_error}"
                    ) from fallback_error
            if info is None:
                raise AudioBackendError("microphone device metadata is unavailable")
            selected_device_name = str(info.get("name") or "") or None
            selected_device_index = int(info["index"])
            self.ready_event.set()
            LOGGER.info(
                "microphone device prepared: %s (index %s)",
                selected_device_name,
                selected_device_index,
            )
            if not self._wait_for_activation():
                return
            try:
                context.call(stream.start_stream)
            except Exception as exc:
                try:
                    stream_started = bool(context.call(stream.is_active))
                except Exception:
                    # A failed activity probe leaves the native state unknown.
                    # Stop before close and retain the original start failure.
                    stream_started = True
                    LOGGER.exception("could not probe partially started microphone stream")
                raise AudioBackendError(
                    "failed to start WASAPI microphone "
                    f"{selected_device_name!r}: {exc}"
                ) from exc
            stream_started = True
            with callback_error_lock:
                callback_failure = None if not callback_errors else callback_errors[0]
            if callback_failure is not None:
                raise callback_failure
            self._raise_callback_decoder_error()
            if not self._confirm_callback_stream_started(
                lambda: bool(context.call(stream.is_active)),
                failure_message="microphone PortAudio callback stream stopped at start",
            ):
                with callback_error_lock:
                    callback_failure = None if not callback_errors else callback_errors[0]
                if callback_failure is not None:
                    raise callback_failure
                self._raise_callback_decoder_error()
                return
            self._mark_active(
                backend_name="pyaudiowpatch_wasapi_microphone",
                selected_device_name=selected_device_name,
                selected_device_index=selected_device_index,
            )
            while not self.stop_event.wait(0.05):
                with callback_error_lock:
                    callback_failure = None if not callback_errors else callback_errors[0]
                if callback_failure is not None:
                    raise callback_failure
                self._raise_callback_decoder_error()
                if (
                    not context.call(stream.is_active)
                    and not self._decoder_has_pending_packets()
                ):
                    raise AudioBackendError("microphone PortAudio callback stream stopped")
            with callback_error_lock:
                callback_failure = None if not callback_errors else callback_errors[0]
            if callback_failure is not None:
                raise callback_failure
            self._raise_callback_decoder_error()
        finally:
            self._finish_wasapi_stream(
                context,
                stream,
                stream_started=stream_started,
                primary_error=sys.exc_info()[1],
                callback_errors=callback_errors,
                callback_error_lock=callback_error_lock,
            )
        self._raise_callback_decoder_error()


def create_windows_sources(
    config: AecConfig,
    reference_callback: Callable[[AudioBlock], None],
    microphone_callback: Callable[[AudioBlock], None],
    reference_device: str | None,
    microphone_device: str | None,
) -> tuple[WasapiReferenceSource, WasapiMicrophoneSource]:
    context = _SharedWasapiContext.create(2)
    return (
        WasapiReferenceSource(
            config,
            reference_callback,
            reference_device,
            _context=context,
        ),
        WasapiMicrophoneSource(
            config,
            microphone_callback,
            microphone_device,
            _context=context,
        ),
    )
