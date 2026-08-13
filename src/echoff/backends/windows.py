"""Windows WASAPI reference and microphone capture adapters."""

from __future__ import annotations

import logging
import math
import queue
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
            if (
                int(info.get("hostApi") or -1) != wasapi_index
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
        should_stop = False
        with self._lock:
            if self._remaining_users <= 0:
                return
            self._remaining_users -= 1
            if self._remaining_users == 0:
                should_stop = True
        if should_stop:
            self._calls.put(None)
            self._thread.join(timeout=3.0)


class _ThreadedSource:
    callback: Callable[[AudioBlock], None]
    config: AecConfig

    backend_name = "unknown"
    source_label = "audio"

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.activate_event = threading.Event()
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

    def _configure_sample_clock(self, config: AecConfig) -> None:
        self._sample_clock = FixedBlockSampleClock(
            sample_rate=config.sample_rate,
            block_samples=config.block_samples,
        )

    def _sync_clock_counters(self) -> None:
        if self._sample_clock is None:
            return
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
        self.callback_status_count += 1
        input_underflow = int(getattr(pyaudio, "paInputUnderflow", 0x01))
        input_overflow = int(getattr(pyaudio, "paInputOverflow", 0x02))
        discontinuity = False
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
        if self.thread is not None:
            # The source thread first stops its native stream and then drains
            # every callback packet through the decoder. A bounded join here
            # would silently abandon queued device payloads during shutdown.
            self.thread.join()

    def activate(self) -> None:
        if self.thread is None or not self.ready_event.is_set():
            raise RuntimeError(f"{self.source_label} source is not prepared")
        self.activate_event.set()

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
        self._callback_queue.put_nowait(packet)
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
                    self.callback_queue_age_max_s = max(
                        self.callback_queue_age_max_s,
                        time.monotonic() - packet.callback_monotonic,
                    )
                    decode(packet)
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
        self._callback_queue.put_nowait(None)
        decoder.join()
        self._callback_decoder = None

    def _decoder_has_pending_packets(self) -> bool:
        return self._decoded_callback_packet_count < self.callback_packet_count

    def _raise_callback_decoder_error(self) -> None:
        if self._callback_decoder_error is not None:
            raise self._callback_decoder_error

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
            self.device_block_count += 1

    def _flush_sample_clock(self) -> None:
        if self._sample_clock is None:
            return
        for block in self._sample_clock.flush(callback_monotonic=time.monotonic()):
            self.callback(block)
            self.device_block_count += 1
        self._sync_clock_counters()


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
        callback_errors: list[Exception] = []
        try:
            info = context.call(lambda: self._select_device(audio))
            self.selected_device_name = str(info.get("name") or "") or None
            self.selected_device_index = int(info["index"])
            channels = max(1, int(info.get("maxInputChannels") or 1))
            block_frames = self.config.block_samples
            callback_frames = block_frames * 5

            def decode(packet: _RawCallbackPacket) -> None:
                discontinuity = self._note_status(packet.status_flags, pyaudio)
                if packet.payload is None:
                    pcm = array("h", [0] * max(0, packet.frame_count * channels))
                    self.synthetic_silence_block_count += max(
                        1, math.ceil(max(0, packet.frame_count) / block_frames)
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
                    callback_errors.append(exc)
                    self.stop_event.set()
                    return None, pyaudio.paAbort

            stream = context.call(
                lambda: audio.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=self.config.sample_rate,
                    input=True,
                    input_device_index=self.selected_device_index,
                    frames_per_buffer=callback_frames,
                    start=False,
                    stream_callback=on_audio,
                )
            )
            self.ready_event.set()
            LOGGER.info(
                "reference device prepared: %s (index %s)",
                self.selected_device_name,
                self.selected_device_index,
            )
            if not self._wait_for_activation():
                return
            context.call(stream.start_stream)
            while not self.stop_event.wait(0.05):
                if callback_errors:
                    raise callback_errors[0]
                self._raise_callback_decoder_error()
                if (
                    not context.call(stream.is_active)
                    and not self._decoder_has_pending_packets()
                ):
                    raise AudioBackendError("reference PortAudio callback stream stopped")
            if callback_errors:
                raise callback_errors[0]
            self._raise_callback_decoder_error()
        finally:
            if stream is not None:
                try:
                    context.call(stream.stop_stream)
                finally:
                    try:
                        self._stop_callback_decoder()
                    finally:
                        context.call(stream.close)
            else:
                self._stop_callback_decoder()
            self._flush_sample_clock()
            context.release()
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
        self.config = config
        self._configure_sample_clock(config)
        self.callback = callback
        self.device = device
        self._context = _context

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
            if (
                int(info.get("hostApi") or -1) == wasapi_index
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
        words = "".join(
            character if character.isalnum() else " " for character in str(value).casefold()
        ).split()
        return "".join(word for word in words if word not in {"mikrofon", "microphone", "mic", "2"})

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
        matching = [
            item
            for item in candidates
            if preferred_key
            and (
                preferred_key in self._device_name_key(item[1].get("name"))
                or self._device_name_key(item[1].get("name")) in preferred_key
            )
        ]
        if len(matching) == 1:
            return matching[0]
        if not matching and len(candidates) == 1:
            return candidates[0]
        raise AudioBackendError(
            "could not select one WDM-KS fallback for microphone "
            f"{preferred_name!r}: {[item[1].get('name') for item in candidates]}"
        )

    def _run_wdmks_fallback(
        self,
        *,
        preferred_name: object,
        block_frames: int,
    ) -> None:
        try:
            import sounddevice
        except ImportError as exc:
            raise AudioBackendError(
                "sounddevice is required for the WDM-KS microphone fallback"
            ) from exc
        device_index, info = self._select_wdmks_fallback_device(sounddevice, preferred_name)
        self.backend_name = "sounddevice_wdmks_microphone"
        self.selected_device_name = str(info.get("name") or "") or None
        self.selected_device_index = device_index
        callback_error: list[Exception] = []
        def on_audio(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            callback_monotonic = time.monotonic()
            try:
                flags = 0
                if bool(getattr(status, "input_underflow", False)):
                    flags |= 0x01
                if bool(getattr(status, "input_overflow", False)):
                    flags |= 0x02
                if status and not flags:
                    flags = 0x20
                discontinuity = self._note_status(flags)
                samples = indata.reshape(-1).tolist()
                self._emit_samples(
                    samples,
                    callback_monotonic=callback_monotonic,
                    time_info={
                        "input_buffer_adc_time": float(time_info.inputBufferAdcTime),
                        "current_time": float(time_info.currentTime),
                    },
                    status_flags=flags,
                    discontinuity=discontinuity,
                )
            except Exception as exc:
                callback_error.append(exc)
                raise sounddevice.CallbackAbort from exc

        stream = sounddevice.InputStream(
            device=device_index,
            channels=1,
            samplerate=self.config.sample_rate,
            dtype="float32",
            blocksize=block_frames,
            callback=on_audio,
        )
        try:
            self.ready_event.set()
            LOGGER.info(
                "microphone fallback prepared: %s (index %s)",
                self.selected_device_name,
                self.selected_device_index,
            )
            if not self._wait_for_activation():
                return
            stream.start()
            while not self.stop_event.wait(0.05):
                if callback_error:
                    raise callback_error[0]
        finally:
            stream.stop()
            stream.close()
            self._flush_sample_clock()

    def _run(self) -> None:
        context = self._context or _SharedWasapiContext.create(1)
        self._context = context
        pyaudio = context.pyaudio
        audio = context.audio
        if pyaudio is None or audio is None:  # pragma: no cover - guarded by context startup
            raise AudioBackendError("PortAudio context is not initialized")
        stream = None
        callback_errors: list[Exception] = []
        try:
            block_frames = self.config.block_samples
            callback_frames = block_frames * 5
            info: dict[str, Any] | None = None
            last_open_error: OSError | None = None

            def decode(packet: _RawCallbackPacket) -> None:
                discontinuity = self._note_status(packet.status_flags, pyaudio)
                if packet.payload is None:
                    pcm = array("h", [0] * max(0, packet.frame_count))
                    self.synthetic_silence_block_count += max(
                        1, math.ceil(max(0, packet.frame_count) / block_frames)
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
                except OSError as exc:
                    last_open_error = exc
                    if attempt == 0:
                        audio = context.reinitialize()
            if stream is None:
                device_name = "unknown" if info is None else str(info.get("name", "unknown"))
                if not self.config.allow_wdmks_microphone_fallback:
                    raise AudioBackendError(
                        f"failed to open WASAPI microphone {device_name!r} after one "
                        f"fresh-context retry: {last_open_error}"
                    )
                try:
                    self._run_wdmks_fallback(
                        preferred_name=device_name,
                        block_frames=block_frames,
                    )
                    return
                except Exception as fallback_error:
                    raise AudioBackendError(
                        f"failed to open WASAPI microphone {device_name!r} after one "
                        "fresh-context retry: "
                        f"{last_open_error}; WDM-KS fallback failed: {fallback_error}"
                    ) from fallback_error
            if info is None:
                raise AudioBackendError("microphone device metadata is unavailable")
            self.selected_device_name = str(info.get("name") or "") or None
            self.selected_device_index = int(info["index"])
            self.ready_event.set()
            LOGGER.info(
                "microphone device prepared: %s (index %s)",
                self.selected_device_name,
                self.selected_device_index,
            )
            if not self._wait_for_activation():
                return
            context.call(stream.start_stream)
            while not self.stop_event.wait(0.05):
                if callback_errors:
                    raise callback_errors[0]
                self._raise_callback_decoder_error()
                if (
                    not context.call(stream.is_active)
                    and not self._decoder_has_pending_packets()
                ):
                    raise AudioBackendError("microphone PortAudio callback stream stopped")
            if callback_errors:
                raise callback_errors[0]
            self._raise_callback_decoder_error()
        finally:
            if stream is not None:
                try:
                    context.call(stream.stop_stream)
                finally:
                    try:
                        self._stop_callback_decoder()
                    finally:
                        context.call(stream.close)
            else:
                self._stop_callback_decoder()
            self._flush_sample_clock()
            context.release()
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
