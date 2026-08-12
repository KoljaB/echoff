"""High-level capture lifecycle and the single AEC pairing worker."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .alignment import AlignmentAction, TimestampAligner
from .backends import create_sources
from .backends.base import CaptureSource
from .config import AecConfig
from .errors import AudioBackendError, CaptureStateError
from .models import AecFrame, AecState, AudioBlock, CaptureEvent, CaptureStatus
from .processor import AecProcessor, WebRtcAecProcessor
from .recording import CaptureArtifacts

LOGGER = logging.getLogger(__name__)
SourceFactory = Callable[
    [
        AecConfig,
        Callable[[list[float], float], None],
        Callable[[list[float], float], None],
        str | None,
        str | None,
    ],
    tuple[CaptureSource, CaptureSource],
]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _default_source_factory(
    config: AecConfig,
    reference_callback: Callable[[list[float], float], None],
    microphone_callback: Callable[[list[float], float], None],
    reference_device: str | None,
    microphone_device: str | None,
) -> tuple[CaptureSource, CaptureSource]:
    return create_sources(
        config,
        reference_callback,
        microphone_callback,
        reference_device=reference_device,
        microphone_device=microphone_device,
    )


class AecCapture:
    """Own two capture sources, timestamp alignment, WebRTC APM, and artifacts."""

    def __init__(
        self,
        config: AecConfig | None = None,
        *,
        on_frame: Callable[[AecFrame], None] | None = None,
        on_event: Callable[[CaptureEvent], None] | None = None,
        output_dir: str | Path | None = None,
        reference_device: str | None = None,
        microphone_device: str | None = None,
        processor: AecProcessor | None = None,
        source_factory: SourceFactory | None = None,
    ) -> None:
        self.config = config or AecConfig()
        self.on_frame = on_frame or (lambda _frame: None)
        self.on_event = on_event or (lambda _event: None)
        self.output_dir = None if output_dir is None else Path(output_dir).resolve()
        self.reference_device = reference_device
        self.microphone_device = microphone_device
        self._processor = processor
        self._source_factory = source_factory or _default_source_factory
        self._reference: CaptureSource | None = None
        self._microphone: CaptureSource | None = None
        self._reference_queue: queue.Queue[AudioBlock] = queue.Queue()
        self._microphone_queue: queue.Queue[AudioBlock] = queue.Queue()
        self._processing_stop = threading.Event()
        self._alignment_ready = threading.Event()
        self._processing_thread: threading.Thread | None = None
        self._processing_error: Exception | None = None
        self._aligner = TimestampAligner(self.config.pair_tolerance_s)
        self._artifacts: CaptureArtifacts | None = None
        self._state_lock = threading.RLock()
        self._running = False
        self._ever_started = False
        self._started_monotonic: float | None = None
        self._started_utc: str | None = None
        self._summary_metadata: dict[str, Any] = {}
        self._reference_sample_count = 0
        self._microphone_sample_count = 0
        self._processing_total_s = 0.0
        self._processing_max_s = 0.0

    def __enter__(self) -> AecCapture:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self.stop(error=None if exc is None else str(exc))
        except Exception:
            if exc is None:
                raise
            LOGGER.exception("capture cleanup also failed while propagating another exception")

    def start(self) -> AecCapture:
        with self._state_lock:
            if self._running:
                raise CaptureStateError("capture already running")
            if self._ever_started:
                raise CaptureStateError("an AecCapture instance cannot be restarted")
            self._ever_started = True
            self._running = True
            self._started_monotonic = time.monotonic()
            self._started_utc = _utc_now()
        try:
            if self.output_dir is not None:
                self._artifacts = CaptureArtifacts(self.output_dir, self.config)
            self._emit(
                "capture_starting",
                config=self.config.to_dict(),
                output_dir=None if self.output_dir is None else str(self.output_dir),
            )
            if self._processor is None:
                self._processor = WebRtcAecProcessor(self.config)
            self._reference, self._microphone = self._source_factory(
                self.config,
                self._enqueue_reference,
                self._enqueue_microphone,
                self.reference_device,
                self.microphone_device,
            )
            self._start_processing()
            # Preserve the physically validated startup order. Timestamp
            # alignment makes independent source startup phase safe.
            self._microphone.start()
            self._reference.start()
            if not self._alignment_ready.wait(self.config.startup_timeout_s):
                raise AudioBackendError("capture streams did not produce an aligned startup pair")
            self.raise_if_failed()
            self._emit(
                "capture_ready",
                reference_backend=self._reference.backend_name,
                reference_device_name=self._reference.selected_device_name,
                reference_device_index=self._reference.selected_device_index,
                microphone_backend=self._microphone.backend_name,
                microphone_device_name=self._microphone.selected_device_name,
                microphone_device_index=self._microphone.selected_device_index,
            )
            LOGGER.info(
                "capture ready: reference=%s microphone=%s",
                self._reference.selected_device_name,
                self._microphone.selected_device_name,
            )
            return self
        except Exception as exc:
            LOGGER.exception("capture startup failed")
            self._emit("capture_failed", phase="startup", error=str(exc))
            try:
                self.stop(error=str(exc), status_name="failed")
            except Exception:
                LOGGER.exception("capture startup cleanup also failed")
            raise

    @property
    def started_monotonic(self) -> float | None:
        return self._started_monotonic

    def elapsed_s(self, monotonic: float | None = None) -> float:
        if self._started_monotonic is None:
            raise CaptureStateError("capture has not started")
        return (time.monotonic() if monotonic is None else monotonic) - self._started_monotonic

    def stop(self, *, error: str | None = None, status_name: str | None = None) -> None:
        with self._state_lock:
            if not self._running:
                return
            self._running = False
        cleanup_errors: list[Exception] = []
        for label, source in (
            ("reference", self._reference),
            ("microphone", self._microphone),
        ):
            if source is None:
                continue
            try:
                source.stop()
            except Exception as exc:
                LOGGER.exception("%s source cleanup failed", label)
                cleanup_errors.append(exc)
        try:
            self._stop_processing()
        except Exception as exc:
            LOGGER.exception("capture processing cleanup failed")
            cleanup_errors.append(exc)
        effective_error = error
        if effective_error is None:
            try:
                self.raise_if_failed()
            except Exception as exc:
                effective_error = str(exc)
        if effective_error is None and cleanup_errors:
            effective_error = f"capture cleanup failed: {cleanup_errors[0]}"
        final_name = status_name or ("failed" if effective_error else "completed")
        try:
            self._emit("capture_stopped", status=final_name, error=effective_error)
        except Exception as exc:
            LOGGER.exception("capture stop event could not be written")
            cleanup_errors.append(exc)
        if self._artifacts is not None and self._started_monotonic is not None:
            try:
                self._artifacts.finalize(
                    status_name=final_name,
                    capture_status=self.status(),
                    started_utc=self._started_utc or _utc_now(),
                    ended_utc=_utc_now(),
                    duration_s=max(0.0, time.monotonic() - self._started_monotonic),
                    error=effective_error,
                    metadata=dict(self._summary_metadata),
                )
            except Exception as exc:
                LOGGER.exception("capture artifact finalization failed")
                cleanup_errors.append(exc)
        LOGGER.info("capture stopped: status=%s", final_name)
        if cleanup_errors:
            raise AudioBackendError(
                f"capture cleanup failed: {cleanup_errors[0]}"
            ) from cleanup_errors[0]

    def set_summary_metadata(self, **values: Any) -> None:
        """Add JSON-compatible application metadata before capture stops."""

        with self._state_lock:
            if not self._running:
                raise CaptureStateError("summary metadata can only be changed while running")
            self._summary_metadata.update(values)

    def record_event(self, kind: str, **details: Any) -> None:
        """Append a low-volume application/probe event to the same timeline."""

        self._emit(kind, **details)

    def raise_if_failed(self) -> None:
        if self._reference is not None and self._reference.error is not None:
            raise AudioBackendError(f"reference source failed: {self._reference.error}")
        if self._microphone is not None and self._microphone.error is not None:
            raise AudioBackendError(f"microphone source failed: {self._microphone.error}")
        if self._processing_error is not None:
            raise AudioBackendError(f"capture processing failed: {self._processing_error}")
        reference_queue_s = self._reference_queue.qsize() * self.config.block_duration_s
        microphone_queue_s = self._microphone_queue.qsize() * self.config.block_duration_s
        if max(reference_queue_s, microphone_queue_s) > self.config.queue_fatal_s:
            raise AudioBackendError(
                "capture processing backlog exceeded the configured fatal limit: "
                f"reference={reference_queue_s:.3f}s microphone={microphone_queue_s:.3f}s"
            )

    def status(self) -> CaptureStatus:
        snapshot = self._aligner.snapshot
        processor_state = (
            self._processor.state if self._processor is not None else AecState(False, 0.0, 0, 0)
        )
        reference = self._reference
        microphone = self._microphone
        pair_count = snapshot.pair_count
        return CaptureStatus(
            running=self._running,
            alignment_locked=snapshot.locked,
            alignment_epoch=snapshot.epoch,
            processed_pair_count=pair_count,
            pair_tolerance_ms=1000.0 * self.config.pair_tolerance_s,
            pair_skew_abs_mean_ms=(
                0.0 if pair_count <= 0 else 1000.0 * snapshot.pair_skew_abs_total_s / pair_count
            ),
            pair_skew_max_ms=1000.0 * snapshot.pair_skew_max_s,
            observed_skew_max_ms=1000.0 * snapshot.observed_skew_max_s,
            first_callback_skew_ms=(
                None
                if snapshot.first_callback_skew_s is None
                else 1000.0 * snapshot.first_callback_skew_s
            ),
            initial_dropped_reference_blocks=snapshot.initial_dropped_reference_blocks,
            initial_dropped_microphone_blocks=snapshot.initial_dropped_microphone_blocks,
            runtime_mismatch_count=snapshot.runtime_mismatch_count,
            runtime_realignments=snapshot.runtime_realignments,
            runtime_dropped_reference_blocks=snapshot.runtime_dropped_reference_blocks,
            runtime_dropped_microphone_blocks=snapshot.runtime_dropped_microphone_blocks,
            last_mismatch_ms=(
                None if snapshot.last_mismatch_s is None else 1000.0 * snapshot.last_mismatch_s
            ),
            shutdown_unpaired_reference_blocks=snapshot.shutdown_unpaired_reference_blocks,
            shutdown_unpaired_microphone_blocks=snapshot.shutdown_unpaired_microphone_blocks,
            reference_audio_s=self._reference_sample_count / self.config.sample_rate,
            microphone_audio_s=self._microphone_sample_count / self.config.sample_rate,
            reference_queue_s=self._reference_queue.qsize() * self.config.block_duration_s,
            microphone_queue_s=self._microphone_queue.qsize() * self.config.block_duration_s,
            reference_backend="uninitialized" if reference is None else reference.backend_name,
            microphone_backend=("uninitialized" if microphone is None else microphone.backend_name),
            reference_device_name=None if reference is None else reference.selected_device_name,
            reference_device_index=None if reference is None else reference.selected_device_index,
            microphone_device_name=(
                None if microphone is None else microphone.selected_device_name
            ),
            microphone_device_index=(
                None if microphone is None else microphone.selected_device_index
            ),
            reference_device_blocks=(0 if reference is None else reference.device_block_count),
            reference_silence_blocks=(
                0 if reference is None else reference.synthetic_silence_block_count
            ),
            reference_dropped_device_blocks=(
                0 if reference is None else reference.dropped_device_block_count
            ),
            microphone_device_blocks=(0 if microphone is None else microphone.device_block_count),
            microphone_silence_blocks=(
                0 if microphone is None else microphone.synthetic_silence_block_count
            ),
            microphone_dropped_device_blocks=(
                0 if microphone is None else microphone.dropped_device_block_count
            ),
            echo_path_ready=processor_state.echo_path_ready,
            far_end_active_s=processor_state.far_end_active_s,
            stream_alignment_reset_count=processor_state.stream_alignment_reset_count,
            processing_mean_ms=(
                0.0 if pair_count <= 0 else 1000.0 * self._processing_total_s / pair_count
            ),
            processing_max_ms=1000.0 * self._processing_max_s,
            error=None if self._processing_error is None else str(self._processing_error),
        )

    def _emit(self, kind: str, **details: Any) -> None:
        event = CaptureEvent(
            kind=kind,
            monotonic=time.monotonic(),
            utc=_utc_now(),
            details=details,
        )
        if self._artifacts is not None:
            self._artifacts.events.write(event)
        try:
            self.on_event(event)
        except Exception:
            LOGGER.exception("on_event callback failed for %s", kind)

    def _enqueue_reference(self, samples: list[float], ended_monotonic: float) -> None:
        self._reference_sample_count += len(samples)
        self._reference_queue.put(AudioBlock(tuple(samples), ended_monotonic))

    def _enqueue_microphone(self, samples: list[float], ended_monotonic: float) -> None:
        self._microphone_sample_count += len(samples)
        self._microphone_queue.put(AudioBlock(tuple(samples), ended_monotonic))

    def _start_processing(self) -> None:
        self._processing_stop.clear()
        self._processing_error = None
        self._processing_thread = threading.Thread(
            target=self._run_processing_guarded,
            name="echoff-pairing",
            daemon=True,
        )
        self._processing_thread.start()

    def _stop_processing(self) -> None:
        self._processing_stop.set()
        thread = self._processing_thread
        self._processing_thread = None
        if thread is not None:
            thread.join(timeout=3.0)
            if thread.is_alive() and self._processing_error is None:
                self._processing_error = AudioBackendError("capture processing thread did not stop")

    def _run_processing_guarded(self) -> None:
        try:
            self._run_processing()
        except Exception as exc:
            LOGGER.exception("capture processing worker failed")
            self._processing_error = exc
            self._processing_stop.set()
            self._alignment_ready.set()
            self._emit("capture_failed", phase="processing", error=str(exc))

    def _run_processing(self) -> None:
        reference: AudioBlock | None = None
        microphone: AudioBlock | None = None
        while True:
            stopping = self._processing_stop.is_set()
            if reference is None:
                try:
                    reference = self._reference_queue.get(timeout=0.0 if stopping else 0.1)
                except queue.Empty:
                    if stopping:
                        self._drain_microphone_tail(microphone)
                        return
                    continue
                self._aligner.observe("reference", reference)
            if microphone is None:
                try:
                    microphone = self._microphone_queue.get(timeout=0.0 if stopping else 0.1)
                except queue.Empty:
                    if stopping:
                        self._drain_reference_tail(reference)
                        return
                    continue
                self._aligner.observe("microphone", microphone)

            decision = self._aligner.decide(reference, microphone)
            if decision.action is AlignmentAction.DROP_REFERENCE:
                if decision.starts_realigning:
                    self._start_realignment(decision.skew_s)
                if self._artifacts is not None:
                    self._artifacts.write_unmatched_reference(reference.samples)
                reference = None
                continue
            if decision.action is AlignmentAction.DROP_MICROPHONE:
                if decision.starts_realigning:
                    self._start_realignment(decision.skew_s)
                if self._artifacts is not None:
                    self._artifacts.write_unmatched_microphone(microphone.samples)
                microphone = None
                continue

            if self._processor is None:  # pragma: no cover - guarded by start
                raise CaptureStateError("AEC processor is not initialized")
            started = time.perf_counter()
            clean = self._processor.process_pair(reference.samples, microphone.samples)
            elapsed = time.perf_counter() - started
            self._processing_total_s += elapsed
            self._processing_max_s = max(self._processing_max_s, elapsed)
            if self._artifacts is not None:
                self._artifacts.write_pair(reference.samples, microphone.samples, clean)
            frame = AecFrame(
                reference=reference.samples,
                microphone_raw=microphone.samples,
                microphone_clean=clean,
                reference_ended_monotonic=reference.ended_monotonic,
                microphone_ended_monotonic=microphone.ended_monotonic,
                pair_skew_s=decision.skew_s,
                state=self._processor.state,
            )
            self.on_frame(frame)
            if decision.locks_alignment:
                snapshot = self._aligner.snapshot
                self._emit(
                    "alignment_locked",
                    initial_dropped_reference_blocks=(snapshot.initial_dropped_reference_blocks),
                    initial_dropped_microphone_blocks=(snapshot.initial_dropped_microphone_blocks),
                    first_callback_skew_ms=(
                        None
                        if snapshot.first_callback_skew_s is None
                        else 1000.0 * snapshot.first_callback_skew_s
                    ),
                    pair_skew_ms=1000.0 * decision.skew_s,
                )
                self._alignment_ready.set()
            if decision.completes_realigning:
                self._emit(
                    "alignment_recovered",
                    epoch=self._aligner.snapshot.epoch,
                    pair_skew_ms=1000.0 * decision.skew_s,
                )
            reference = None
            microphone = None

    def _start_realignment(self, skew_s: float) -> None:
        if self._processor is None:  # pragma: no cover - guarded by start
            raise CaptureStateError("AEC processor is not initialized")
        self._processor.reset_alignment()
        snapshot = self._aligner.snapshot
        self._emit(
            "alignment_realigning",
            epoch=snapshot.epoch,
            mismatch_ms=1000.0 * skew_s,
        )
        LOGGER.warning(
            "capture streams realigning: epoch=%s skew_ms=%.3f",
            snapshot.epoch,
            1000.0 * skew_s,
        )

    def _drain_reference_tail(self, pending: AudioBlock | None) -> None:
        items = [] if pending is None else [pending]
        while True:
            try:
                block = self._reference_queue.get_nowait()
            except queue.Empty:
                break
            self._aligner.observe("reference", block)
            items.append(block)
        self._aligner.note_shutdown_unpaired("reference", len(items))
        if self._artifacts is not None:
            for item in items:
                self._artifacts.write_unmatched_reference(item.samples)

    def _drain_microphone_tail(self, pending: AudioBlock | None) -> None:
        items = [] if pending is None else [pending]
        while True:
            try:
                block = self._microphone_queue.get_nowait()
            except queue.Empty:
                break
            self._aligner.observe("microphone", block)
            items.append(block)
        self._aligner.note_shutdown_unpaired("microphone", len(items))
        if self._artifacts is not None:
            for item in items:
                self._artifacts.write_unmatched_microphone(item.samples)
