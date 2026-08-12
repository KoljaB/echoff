"""Windows WASAPI reference and microphone capture adapters."""

from __future__ import annotations

import logging
import queue
import threading
import time
from array import array
from collections.abc import Callable
from typing import Any

from ..config import AecConfig
from ..errors import AudioBackendError
from ..models import DeviceInfo

LOGGER = logging.getLogger(__name__)


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


class _ThreadedSource:
    backend_name = "unknown"
    source_label = "audio"

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: Exception | None = None
        self.device_block_count = 0
        self.synthetic_silence_block_count = 0
        self.dropped_device_block_count = 0
        self.selected_device_name: str | None = None
        self.selected_device_index: int | None = None

    def start(self) -> None:
        if self.thread is not None:
            raise RuntimeError(f"{self.source_label} source already started")
        self.stop_event.clear()
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
        if self.thread is not None:
            self.thread.join(timeout=3.0)

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

    def _take_device_block(
        self,
        device_queue: queue.Queue[bytes],
        *,
        timeout_s: float,
        discard_surplus: bool,
    ) -> bytes | None:
        """Wait briefly for one device block and optionally collapse stale surplus."""

        try:
            payload = device_queue.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            return None
        if discard_surplus:
            while device_queue.qsize() > 1:
                try:
                    payload = device_queue.get_nowait()
                except queue.Empty:
                    break
                self.dropped_device_block_count += 1
        return payload


class WasapiReferenceSource(_ThreadedSource):
    """Clock-continuous capture of a Windows render endpoint."""

    backend_name = "pyaudiowpatch_wasapi_loopback"
    source_label = "reference"

    def __init__(
        self,
        config: AecConfig,
        callback: Callable[[list[float], float], None],
        device: str | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.callback = callback
        self.device = device

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
        pyaudio = _load_pyaudio()
        audio = pyaudio.PyAudio()
        stream = None
        reader: threading.Thread | None = None
        try:
            info = self._select_device(audio)
            self.selected_device_name = str(info.get("name") or "") or None
            self.selected_device_index = int(info["index"])
            channels = max(1, int(info.get("maxInputChannels") or 1))
            block_frames = self.config.block_samples
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=self.config.sample_rate,
                input=True,
                input_device_index=self.selected_device_index,
                frames_per_buffer=block_frames,
            )
            device_queue: queue.Queue[bytes] = queue.Queue()
            reader_error: list[Exception] = []

            def read_device() -> None:
                try:
                    while not self.stop_event.is_set():
                        device_queue.put(stream.read(block_frames, exception_on_overflow=False))
                except Exception as exc:
                    if not self.stop_event.is_set():
                        reader_error.append(exc)

            reader = threading.Thread(
                target=read_device,
                name="echoff-reference-reader",
                daemon=True,
            )
            reader.start()
            self.ready_event.set()
            LOGGER.info(
                "reference device ready: %s (index %s)",
                self.selected_device_name,
                self.selected_device_index,
            )
            scale = 1.0 / 32768.0
            next_tick = time.monotonic() + self.config.block_duration_s
            silence = [0.0] * block_frames
            reference_state = "unknown"
            while not self.stop_event.is_set():
                if self.stop_event.wait(max(0.0, next_tick - time.monotonic())):
                    break
                if reader_error:
                    raise reader_error[0]
                wait_budget_s = (
                    self.config.reference_stall_grace_s
                    if reference_state != "idle"
                    else min(0.018, self.config.block_duration_s * 0.9)
                )
                payload = self._take_device_block(
                    device_queue,
                    timeout_s=max(0.0, next_tick + wait_budget_s - time.monotonic()),
                    discard_surplus=False,
                )
                if reader_error:
                    raise reader_error[0]
                if payload is not None:
                    pcm = array("h")
                    pcm.frombytes(payload)
                    if channels == 1:
                        samples = [value * scale for value in pcm]
                    else:
                        samples = [
                            sum(pcm[index : index + channels]) * scale / channels
                            for index in range(0, len(pcm) - channels + 1, channels)
                        ]
                    self.device_block_count += 1
                    reference_state = "active"
                    self.callback(samples, next_tick)
                    next_tick += self.config.block_duration_s
                    continue

                # A blocking loopback read may complete slightly after its nominal
                # scheduler tick. While the endpoint is active (or its state is not
                # known yet), preserve that tick for a bounded grace period instead
                # of inserting silence and relabelling the real block as the next
                # tick. Once the grace expires, classify the endpoint as idle and
                # catch the synthetic clock up in one bounded burst. Idle ticks use
                # only the short normal read margin, so microphone delivery does not
                # inherit the full stall grace while the endpoint is silent.
                reference_state = "idle"
                now = time.monotonic()
                while next_tick <= now + 1e-9 and not self.stop_event.is_set():
                    self.synthetic_silence_block_count += 1
                    self.callback(silence, next_tick)
                    next_tick += self.config.block_duration_s
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                finally:
                    if reader is not None:
                        reader.join(timeout=1.0)
                    stream.close()
            audio.terminate()


class WasapiMicrophoneSource(_ThreadedSource):
    """Capture the native Windows microphone clock at 48 kHz."""

    backend_name = "pyaudiowpatch_wasapi_microphone"
    source_label = "microphone"

    def __init__(
        self,
        config: AecConfig,
        callback: Callable[[list[float], float], None],
        device: str | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.callback = callback
        self.device = device

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
        sample_clock_end: float | None = None

        def on_audio(indata: Any, frames: int, _time_info: Any, status: Any) -> None:
            nonlocal sample_clock_end
            try:
                if status:
                    self.dropped_device_block_count += 1
                samples = indata.reshape(-1).tolist()
                sample_clock_end = (
                    time.monotonic()
                    if sample_clock_end is None
                    else sample_clock_end + frames / self.config.sample_rate
                )
                self.device_block_count += 1
                self.callback(samples, sample_clock_end)
            except Exception as exc:
                callback_error.append(exc)
                raise sounddevice.CallbackAbort from exc

        with sounddevice.InputStream(
            device=device_index,
            channels=1,
            samplerate=self.config.sample_rate,
            dtype="float32",
            blocksize=block_frames,
            callback=on_audio,
        ):
            self.ready_event.set()
            LOGGER.info(
                "microphone fallback ready: %s (index %s)",
                self.selected_device_name,
                self.selected_device_index,
            )
            while not self.stop_event.wait(0.05):
                if callback_error:
                    raise callback_error[0]

    def _run(self) -> None:
        pyaudio = _load_pyaudio()
        audio = pyaudio.PyAudio()
        stream = None
        reader: threading.Thread | None = None
        try:
            block_frames = self.config.block_samples
            device_queue: queue.Queue[bytes] = queue.Queue()
            reader_error: list[Exception] = []
            info: dict[str, Any] | None = None
            last_open_error: OSError | None = None
            for attempt in range(3):
                info = self._select_device(audio, pyaudio)
                try:
                    stream = audio.open(
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=self.config.sample_rate,
                        input=True,
                        input_device_index=int(info["index"]),
                        frames_per_buffer=block_frames,
                    )
                    break
                except OSError as exc:
                    last_open_error = exc
                    if attempt < 2:
                        time.sleep(0.15)
            if stream is None:
                device_name = "unknown" if info is None else str(info.get("name", "unknown"))
                if not self.config.allow_wdmks_microphone_fallback:
                    raise AudioBackendError(
                        f"failed to open WASAPI microphone {device_name!r}: {last_open_error}"
                    )
                try:
                    self._run_wdmks_fallback(
                        preferred_name=device_name,
                        block_frames=block_frames,
                    )
                    return
                except Exception as fallback_error:
                    raise AudioBackendError(
                        f"failed to open WASAPI microphone {device_name!r} after 3 attempts: "
                        f"{last_open_error}; WDM-KS fallback failed: {fallback_error}"
                    ) from fallback_error
            if info is None:
                raise AudioBackendError("microphone device metadata is unavailable")
            self.selected_device_name = str(info.get("name") or "") or None
            self.selected_device_index = int(info["index"])

            def read_device() -> None:
                try:
                    while not self.stop_event.is_set():
                        device_queue.put(stream.read(block_frames, exception_on_overflow=False))
                except Exception as exc:
                    if not self.stop_event.is_set():
                        reader_error.append(exc)

            reader = threading.Thread(
                target=read_device,
                name="echoff-microphone-reader",
                daemon=True,
            )
            reader.start()
            self.ready_event.set()
            LOGGER.info(
                "microphone device ready: %s (index %s)",
                self.selected_device_name,
                self.selected_device_index,
            )
            next_tick = time.monotonic()
            silence = [0.0] * block_frames
            while not self.stop_event.is_set():
                next_tick += self.config.block_duration_s
                if self.stop_event.wait(max(0.0, next_tick - time.monotonic())):
                    break
                if reader_error:
                    raise reader_error[0]
                payload = self._take_device_block(
                    device_queue,
                    timeout_s=min(0.018, self.config.block_duration_s * 0.9),
                    discard_surplus=(time.monotonic() - next_tick < self.config.block_duration_s),
                )
                if payload is not None:
                    pcm = array("h")
                    pcm.frombytes(payload)
                    samples = [value / 32768.0 for value in pcm]
                    self.device_block_count += 1
                else:
                    samples = silence
                    self.synthetic_silence_block_count += 1
                self.callback(samples, next_tick)
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                finally:
                    if reader is not None:
                        reader.join(timeout=1.0)
                    stream.close()
            audio.terminate()
