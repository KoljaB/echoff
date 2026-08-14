"""Linux PipeWire sink-monitor and microphone capture adapters."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..clock import FixedBlockSampleClock
from ..config import AecConfig
from ..errors import AudioBackendError
from ..models import AudioBlock, DeviceInfo


@dataclass(frozen=True, slots=True)
class _PipeWireDevice:
    kind: Literal["reference", "microphone"]
    index: int
    name: str
    channels: int
    sample_rate: float
    is_default: bool


def _run_pactl(*arguments: str) -> str:
    if shutil.which("pactl") is None:
        raise AudioBackendError("pactl is required for PipeWire device discovery")
    result = subprocess.run(
        ("pactl", *arguments),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise AudioBackendError(f"pactl {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def _run_pw_dump() -> list[object]:
    if shutil.which("pw-dump") is None:
        raise AudioBackendError("pw-dump is required for PipeWire device discovery")
    result = subprocess.run(("pw-dump",), capture_output=True, text=True, check=False)
    if result.returncode:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise AudioBackendError(f"pw-dump failed: {detail}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AudioBackendError(f"pw-dump returned invalid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise AudioBackendError("pw-dump returned an unexpected document")
    return value


def _discover_devices() -> list[_PipeWireDevice]:
    default_sink = _run_pactl("get-default-sink")
    default_source = _run_pactl("get-default-source")
    source_formats: dict[str, tuple[int, float]] = {}
    for line in _run_pactl("list", "short", "sources").splitlines():
        fields = line.split("\t")
        if len(fields) < 4:
            continue
        channels_match = re.search(r"\b(\d+)ch\b", fields[3])
        rate_match = re.search(r"\b(\d+)Hz\b", fields[3])
        source_formats[fields[1]] = (
            int(channels_match.group(1)) if channels_match else 1,
            float(rate_match.group(1)) if rate_match else 0.0,
        )
    devices: list[_PipeWireDevice] = []
    for item in _run_pw_dump():
        if not isinstance(item, dict) or item.get("type") != "PipeWire:Interface:Node":
            continue
        try:
            info = item["info"]
            props = info["props"]
            media_class = str(props["media.class"])
            node_name = str(props["node.name"])
            index = int(props["object.serial"])
        except (KeyError, TypeError, ValueError):
            continue
        if media_class == "Audio/Sink":
            kind: Literal["reference", "microphone"] = "reference"
            name = f"{node_name}.monitor"
            is_default = node_name == default_sink
        elif media_class == "Audio/Source":
            kind = "microphone"
            name = node_name
            is_default = node_name == default_source
        else:
            continue
        channels, sample_rate = source_formats.get(name, (1, 0.0))
        devices.append(
            _PipeWireDevice(
                kind=kind,
                index=index,
                name=name,
                channels=channels,
                sample_rate=sample_rate,
                is_default=is_default,
            )
        )
    if not devices:
        raise AudioBackendError("PipeWire reported no capture sources through pactl")
    return devices


def list_pipewire_devices() -> list[DeviceInfo]:
    """List PipeWire sink monitors as references and ordinary input sources as microphones."""

    return [
        DeviceInfo(
            kind=device.kind,
            backend="pipewire",
            index=device.index,
            name=device.name,
            is_default=device.is_default,
            channels=device.channels,
            default_sample_rate=device.sample_rate,
        )
        for device in sorted(
            _discover_devices(), key=lambda item: (item.kind, not item.is_default, item.index)
        )
    ]


def _select_device(
    kind: Literal["reference", "microphone"], selector: str | None
) -> _PipeWireDevice:
    candidates = [device for device in _discover_devices() if device.kind == kind]
    if not candidates:
        raise AudioBackendError(f"PipeWire reported no {kind} devices")
    if selector is None:
        defaults = [device for device in candidates if device.is_default]
        if defaults:
            return defaults[0]
        if len(candidates) == 1:
            return candidates[0]
        raise AudioBackendError(
            f"PipeWire has no default {kind}; select one by index or unique name fragment"
        )
    index_matches = [device for device in candidates if str(device.index) == selector]
    if len(index_matches) == 1:
        return index_matches[0]
    normalized = selector.casefold()
    matches = [
        device
        for device in candidates
        if normalized in device.name.casefold()
    ]
    if len(matches) != 1:
        names = ", ".join(f"{device.index}: {device.name}" for device in candidates)
        qualifier = "did not match" if not matches else "is ambiguous for"
        raise AudioBackendError(f"{kind} selector {selector!r} {qualifier}: {names}")
    return matches[0]


class PipeWireSource:
    """One fixed-block mono stream read from a native ``pw-record`` client."""

    backend_name = "pipewire"

    def __init__(
        self,
        config: AecConfig,
        callback: Callable[[AudioBlock], None],
        kind: Literal["reference", "microphone"],
        selector: str | None,
        *,
        launch_gate: threading.Event | None = None,
        ready_signal: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.callback = callback
        self.kind = kind
        self.selector = selector
        self._launch_gate = launch_gate
        self._ready_signal = ready_signal
        self.error: Exception | None = None
        self.device_block_count = 0
        self.synthetic_silence_block_count = 0
        self.dropped_device_block_count = 0
        self.timestamp_regression_count = 0
        self.invalid_timestamp_count = 0
        self.timestamp_deviation_max_s = 0.0
        self.timestamp_gap_block_count = 0
        self.timestamp_anomaly_count = 0
        self.callback_status_count = 0
        self.input_overflow_count = 0
        self.input_underflow_count = 0
        self.padded_sample_count = 0
        self.callback_packet_count = 0
        self.callback_payload_frame_count = 0
        self.callback_queue_high_watermark = 0
        self.callback_queue_age_max_s = 0.0
        self.callback_enqueue_max_s = 0.0
        self.callback_timeline_drift_s = 0.0
        self.callback_timeline_drift_max_s = 0.0
        self.selected_device_name: str | None = None
        self.selected_device_index: int | None = None
        self._clock = FixedBlockSampleClock(
            sample_rate=config.sample_rate,
            block_samples=config.block_samples,
        )
        self._stop = threading.Event()
        self._activate = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._process_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError(f"{self.kind} source already started")
        if shutil.which("pw-record") is None:
            raise AudioBackendError("pw-record is required for PipeWire capture")
        device = _select_device(self.kind, self.selector)
        self.selected_device_name = device.name
        self.selected_device_index = device.index
        self._thread = threading.Thread(
            target=self._run_guarded,
            name=f"echoff-pipewire-{self.kind}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(self.config.startup_timeout_s):
            raise AudioBackendError(f"PipeWire {self.kind} source did not become ready")
        if self.error is not None:
            raise AudioBackendError(f"PipeWire {self.kind} source failed: {self.error}")

    def activate(self) -> None:
        if self._thread is None or not self._ready.is_set():
            raise RuntimeError(f"PipeWire {self.kind} source is not prepared")
        self._activate.set()

    def stop(self) -> None:
        self._stop.set()
        self._activate.set()
        with self._process_lock:
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                raise AudioBackendError(f"PipeWire {self.kind} source did not stop")

    def _run_guarded(self) -> None:
        try:
            self._ready.set()
            while not self._stop.is_set() and not self._activate.wait(0.05):
                pass
            if not self._stop.is_set():
                self._capture()
        except Exception as exc:
            self.error = exc
            self._ready.set()
            self._stop.set()

    def _capture(self) -> None:
        assert self.selected_device_name is not None
        if self._launch_gate is not None:
            while not self._stop.is_set() and not self._launch_gate.wait(0.05):
                pass
            if self._stop.is_set():
                return
        command = (
            "pw-record",
            "--target",
            str(self.selected_device_index),
            "--rate",
            str(self.config.sample_rate),
            "--channels",
            "1",
            "--channel-map",
            "MONO",
            "--format",
            "f32",
            "--latency",
            str(self.config.block_samples),
            "-",
        )
        with tempfile.TemporaryFile(mode="w+b") as stderr_stream:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=stderr_stream,
            )
            with self._process_lock:
                self._process = process
            assert process.stdout is not None
            frame_bytes = self.config.block_samples * 4
            unexpected_eof = False
            try:
                while not self._stop.is_set():
                    payload = process.stdout.read(frame_bytes)
                    if not payload:
                        unexpected_eof = True
                        break
                    while len(payload) < frame_bytes and not self._stop.is_set():
                        extra = process.stdout.read(frame_bytes - len(payload))
                        if not extra:
                            unexpected_eof = True
                            break
                        payload += extra
                    if len(payload) != frame_bytes:
                        break
                    pcm = array("f")
                    pcm.frombytes(payload)
                    if sys.byteorder != "little":
                        pcm.byteswap()
                    callback_monotonic = time.monotonic()
                    blocks = self._clock.push(
                        pcm,
                        callback_monotonic=callback_monotonic,
                        adc_start=None,
                        current_time=None,
                        count_invalid_timestamp=False,
                    )
                    self.callback_packet_count += 1
                    self.callback_payload_frame_count += len(pcm)
                    for block in blocks:
                        if self._ready_signal is not None:
                            self._ready_signal.set()
                            self._ready_signal = None
                        self.callback(block)
                        self.device_block_count += 1
                    self._sync_clock_counters()
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
                with self._process_lock:
                    self._process = None
                self._sync_clock_counters()
            if unexpected_eof and not self._stop.is_set():
                stderr_stream.seek(0)
                detail = stderr_stream.read().decode(errors="replace").strip()
                selected = (
                    f"node {self.selected_device_index} "
                    f"({self.selected_device_name})"
                )
                exit_detail = f"exit {process.returncode}"
                if detail:
                    exit_detail = f"{exit_detail}: {detail}"
                raise AudioBackendError(
                    f"pw-record stopped unexpectedly for {self.kind} {selected}: "
                    f"{exit_detail}"
                )

    def _sync_clock_counters(self) -> None:
        self.timestamp_regression_count = self._clock.timestamp_regression_count
        self.invalid_timestamp_count = self._clock.invalid_timestamp_count
        self.timestamp_deviation_max_s = self._clock.timestamp_deviation_max_s
        self.timestamp_anomaly_count = self._clock.timestamp_anomaly_count
        self.padded_sample_count = self._clock.padded_sample_count


class _PipeWireStartupCoordinator:
    """Release both independently launched streams from one ordered startup buffer."""

    STARTUP_BLOCKS = 3
    RELEASE_DELAY_S = 0.100

    def __init__(
        self,
        reference_callback: Callable[[AudioBlock], None],
        microphone_callback: Callable[[AudioBlock], None],
    ) -> None:
        self._callbacks = {
            "reference": reference_callback,
            "microphone": microphone_callback,
        }
        self._pending: dict[str, list[AudioBlock]] = {
            "reference": [],
            "microphone": [],
        }
        self._started = False
        self._released = False
        self._release_after = 0.0
        self._lock = threading.Lock()

    def submit(
        self, kind: Literal["reference", "microphone"], block: AudioBlock
    ) -> None:
        with self._lock:
            if self._released:
                self._callbacks[kind](block)
                return
            self._pending[kind].append(block)
            if not self._started and any(
                len(blocks) < self.STARTUP_BLOCKS for blocks in self._pending.values()
            ):
                return
            if not self._started:
                initial = [
                    (source_kind, pending_block)
                    for source_kind, blocks in self._pending.items()
                    for pending_block in blocks[: self.STARTUP_BLOCKS]
                ]
                initial.sort(
                    key=lambda item: (
                        item[1].ended_monotonic,
                        0 if item[0] == "microphone" else 1,
                    )
                )
                for source_kind in self._pending:
                    del self._pending[source_kind][: self.STARTUP_BLOCKS]
                self._started = True
                self._release_after = time.monotonic() + self.RELEASE_DELAY_S
                for source_kind, pending_block in initial:
                    self._callbacks[source_kind](pending_block)
                return
            if time.monotonic() < self._release_after:
                return
            buffered = [
                (source_kind, pending_block)
                for source_kind, blocks in self._pending.items()
                for pending_block in blocks
            ]
            buffered.sort(
                key=lambda item: (
                    item[1].ended_monotonic,
                    0 if item[0] == "microphone" else 1,
                )
            )
            self._released = True
            self._pending = {"reference": [], "microphone": []}
            for source_kind, pending_block in buffered:
                self._callbacks[source_kind](pending_block)


def create_pipewire_sources(
    config: AecConfig,
    reference_callback: Callable[[AudioBlock], None],
    microphone_callback: Callable[[AudioBlock], None],
    reference_device: str | None,
    microphone_device: str | None,
) -> tuple[PipeWireSource, PipeWireSource]:
    coordinator = _PipeWireStartupCoordinator(reference_callback, microphone_callback)
    reference_ready = threading.Event()
    return (
        PipeWireSource(
            config,
            lambda block: coordinator.submit("reference", block),
            "reference",
            reference_device,
            ready_signal=reference_ready,
        ),
        PipeWireSource(
            config,
            lambda block: coordinator.submit("microphone", block),
            "microphone",
            microphone_device,
            launch_gate=reference_ready,
        ),
    )
