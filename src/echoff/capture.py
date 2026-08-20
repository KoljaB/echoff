"""High-level capture lifecycle and the single AEC pairing worker."""

from __future__ import annotations

import logging
import math
import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .alignment import AdaptiveReferenceAligner, AlignmentMode, AlignmentUpdate
from .backends import create_sources
from .backends.base import CaptureSource
from .config import AecConfig
from .errors import AudioBackendError, CaptureStateError
from .models import AecFrame, AecState, AudioBlock, CaptureEvent, CaptureStatus
from .processor import AecProcessor, WebRtcAecProcessor
from .recording import CaptureArtifacts

LOGGER = logging.getLogger(__name__)

_CONSOLE_DIAGNOSTIC_LEVELS = {
    "synchronization_degraded": "ERROR",
    "synchronization_recovered": "INFO",
    "alignment_realigning": "WARNING",
    "alignment_clock_corrected": "WARNING",
    "reference_source_degraded": "ERROR",
    "microphone_source_degraded": "ERROR",
    "capture_failed": "ERROR",
}
_CONSOLE_DIAGNOSTIC_MESSAGES = {
    "synchronization_degraded": "live AEC suspended after synchronization reserve expired",
    "synchronization_recovered": "live AEC synchronization recovered",
    "alignment_realigning": "capture discontinuity invalidated the reference mapping; realigning",
    "alignment_clock_corrected": "reference clock mapping was corrected",
    "reference_source_degraded": (
        "reference capture failed; live AEC is suspended"
    ),
    "microphone_source_degraded": "microphone capture failed; live AEC is suspended",
    "capture_failed": "capture failed",
}
_ANSI_BRIGHT_RED = "\x1b[91m"
_ANSI_RESET = "\x1b[0m"
SourceFactory = Callable[
    [
        AecConfig,
        Callable[[AudioBlock], None],
        Callable[[AudioBlock], None],
        str | None,
        str | None,
    ],
    tuple[CaptureSource, CaptureSource],
]


@dataclass(slots=True)
class _SynchronizationWait:
    missing_source: str
    started_monotonic: float
    expected_microphone_sequence: int | None
    expected_reference_sequence: int | None
    microphone_head_sequence: int | None
    reference_head_sequence: int | None
    microphone_callback_monotonic: float | None
    reference_callback_monotonic: float | None
    max_backlog_blocks: int
    raw_reference_queue_depth: int
    raw_microphone_queue_depth: int
    internal_reference_queue_depth: int
    internal_microphone_queue_depth: int
    mapped_reference_slot_depth: int
    reported: bool = False


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _default_source_factory(
    config: AecConfig,
    reference_callback: Callable[[AudioBlock], None],
    microphone_callback: Callable[[AudioBlock], None],
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

    WAIT_EVENT_THRESHOLD_S = 0.100

    def __init__(
        self,
        config: AecConfig | None = None,
        *,
        on_frame: Callable[[AecFrame], None] | None = None,
        on_reference: Callable[[tuple[float, ...], float], None] | None = None,
        on_event: Callable[[CaptureEvent], None] | None = None,
        output_dir: str | Path | None = None,
        reference_device: str | None = None,
        microphone_device: str | None = None,
        console_diagnostics: bool = True,
        processor: AecProcessor | None = None,
        source_factory: SourceFactory | None = None,
    ) -> None:
        self.config = config or AecConfig()
        self.on_frame = on_frame or (lambda _frame: None)
        self.on_reference = on_reference or (lambda _samples, _ended: None)
        self.on_event = on_event or (lambda _event: None)
        self.output_dir = None if output_dir is None else Path(output_dir).resolve()
        self.reference_device = reference_device
        self.microphone_device = microphone_device
        self.console_diagnostics = console_diagnostics
        self._processor = processor
        self._source_factory = source_factory or _default_source_factory
        self._reference: CaptureSource | None = None
        self._microphone: CaptureSource | None = None
        self._capture_queue_capacity_blocks = max(
            1,
            math.ceil(self.config.queue_fatal_s / self.config.block_duration_s),
        )
        self._reference_queue: queue.Queue[AudioBlock] = queue.Queue(
            maxsize=self._capture_queue_capacity_blocks
        )
        self._microphone_queue: queue.Queue[AudioBlock] = queue.Queue(
            maxsize=self._capture_queue_capacity_blocks
        )
        self._reference_queue_overflow_count = 0
        self._microphone_queue_overflow_count = 0
        self._processing_stop = threading.Event()
        self._startup_ready = threading.Event()
        self._processing_thread: threading.Thread | None = None
        self._processing_error: Exception | None = None
        self._aligner = AdaptiveReferenceAligner(
            self.config.block_duration_s,
            self.config.pair_tolerance_s,
        )
        self._artifacts: CaptureArtifacts | None = None
        self._state_lock = threading.RLock()
        self._stop_condition = threading.Condition(threading.RLock())
        self._stop_in_progress = False
        self._stop_owner_thread_id: int | None = None
        self._stop_retry_scheduled = False
        self._stop_request_recorded = False
        self._stop_requested_error: str | None = None
        self._stop_requested_status_name: str | None = None
        self._stop_sources_initialized = False
        self._stop_pending_source_names: set[str] = set()
        self._stop_first_cleanup_error: Exception | None = None
        self._stop_terminal_decided = False
        self._stop_terminal_error: str | None = None
        self._stop_terminal_status: str | None = None
        self._stop_event_emitted = False
        self._stop_artifacts_finalized = False
        self._stop_complete = False
        self._running = False
        self._ever_started = False
        self._started_monotonic: float | None = None
        self._started_utc: str | None = None
        self._timeline_started_monotonic: float | None = None
        self._summary_metadata: dict[str, Any] = {}
        self._reference_sample_count = 0
        self._microphone_sample_count = 0
        self._processing_total_s = 0.0
        self._processing_max_s = 0.0
        self._worker_slot_total_s = 0.0
        self._worker_slot_max_s = 0.0
        self._processed_slot_count = 0
        self._internal_reference_pending_blocks = 0
        self._internal_microphone_pending_blocks = 0
        self._reference_failure: Exception | None = None
        self._reference_failure_reported = False
        self._microphone_failure: Exception | None = None
        self._microphone_failure_reported = False
        self._synchronization_wait_count = 0
        self._synchronization_wait_completed_count = 0
        self._synchronization_wait_timeout_count = 0
        self._synchronization_wait_total_s = 0.0
        self._synchronization_wait_max_s = 0.0
        self._synchronization_max_backlog_blocks = 0
        self._synchronization_catchup_total_s = 0.0
        self._synchronization_catchup_max_s = 0.0
        self._source_failure_count = 0
        self._degraded_reason: str | None = None
        self._degraded_retirement_reported: set[tuple[str, str]] = set()

    def __enter__(self) -> AecCapture:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self.stop(error=None if exc is None else str(exc))
            if exc is None:
                self.raise_if_failed()
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
            # Prepare both PortAudio streams before either one begins producing
            # callbacks. Activating them back-to-back keeps startup phase small
            # without inventing scheduler timestamps or synthetic capture data.
            self._microphone.start()
            reference_prepared = True
            try:
                self._reference.start()
            except Exception as exc:
                reference_prepared = False
                self._note_reference_failure(phase="prepare", error=exc)
            self._microphone.activate()
            if reference_prepared:
                try:
                    self._reference.activate()
                except Exception as exc:
                    self._note_reference_failure(phase="activate", error=exc)
            startup_deadline = time.monotonic() + self.config.startup_timeout_s
            self._wait_for_source_active(
                self._microphone,
                source_kind="microphone",
                deadline=startup_deadline,
            )
            if reference_prepared:
                self._wait_for_source_active(
                    self._reference,
                    source_kind="reference",
                    deadline=startup_deadline,
                )
            startup_remaining_s = max(0.0, startup_deadline - time.monotonic())
            if not self._startup_ready.wait(startup_remaining_s):
                raise AudioBackendError("capture streams did not produce any startup audio")
            self.raise_if_failed()
            startup_degraded = (
                self._reference_failure is not None
                or self._microphone_failure is not None
            )
            microphone_diagnostics = self._source_diagnostics(self._microphone)
            self._emit(
                "capture_degraded_ready" if startup_degraded else "capture_ready",
                reference_backend=self._reference.backend_name,
                reference_device_name=self._reference.selected_device_name,
                reference_device_index=self._reference.selected_device_index,
                microphone_backend=self._microphone.backend_name,
                microphone_device_name=self._microphone.selected_device_name,
                microphone_device_index=self._microphone.selected_device_index,
                reference_error=(
                    None
                    if self._reference_failure is None
                    else str(self._reference_failure)
                ),
                microphone_error=(
                    None
                    if self._microphone_failure is None
                    else str(self._microphone_failure)
                ),
                microphone_fallback_used=microphone_diagnostics.get(
                    "fallback_used", False
                ),
                microphone_backend_attempt_errors=microphone_diagnostics.get(
                    "backend_attempt_errors", ()
                ),
            )
            LOGGER.info(
                "%s: reference=%s microphone=%s",
                "capture degraded" if startup_degraded else "capture ready",
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

    def _wait_for_source_active(
        self,
        source: CaptureSource,
        *,
        source_kind: str,
        deadline: float,
    ) -> bool:
        """Wait for built-in backends to publish active device metadata."""

        active_event = getattr(source, "active_event", None)
        wait = getattr(active_event, "wait", None)
        if not callable(wait):
            return True
        while True:
            if wait(0.010):
                return True
            if source.error is not None:
                if source_kind == "reference":
                    self._note_reference_failure(phase="start", error=source.error)
                else:
                    self._note_microphone_failure(phase="start", error=source.error)
                return False
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0.0:
                failure = AudioBackendError(
                    f"{source_kind} source did not become active before startup timeout"
                )
                if source_kind == "reference":
                    self._note_reference_failure(phase="start", error=failure)
                else:
                    self._note_microphone_failure(phase="start", error=failure)
                return False

    @staticmethod
    def _source_diagnostics(
        source: CaptureSource | None,
    ) -> dict[str, int | float | bool | tuple[str, ...]]:
        if source is None:
            return {}
        snapshot = getattr(source, "diagnostics_snapshot", None)
        if callable(snapshot):
            return cast(
                dict[str, int | float | bool | tuple[str, ...]],
                snapshot(),
            )
        return {}

    @staticmethod
    def _source_diagnostic_value(
        source: CaptureSource | None,
        diagnostics: dict[str, int | float | bool | tuple[str, ...]],
        name: str,
        default: Any,
    ) -> Any:
        if name in diagnostics:
            return diagnostics[name]
        return default if source is None else getattr(source, name, default)

    @property
    def started_monotonic(self) -> float | None:
        return self._started_monotonic

    def elapsed_s(self, monotonic: float | None = None) -> float:
        if self._started_monotonic is None:
            raise CaptureStateError("capture has not started")
        return (time.monotonic() if monotonic is None else monotonic) - self._started_monotonic

    @property
    def timeline_started_monotonic(self) -> float | None:
        """Monotonic origin of sample zero in the artifact WAV timeline."""

        return self._timeline_started_monotonic

    @property
    def processed_sample_count(self) -> int:
        """Number of samples already committed to the confirmed-pair timeline."""

        return self._processed_slot_count * self.config.block_samples

    def reset_echo_path(self) -> None:
        """Cold-start AEC adaptation without disturbing capture alignment."""

        with self._state_lock:
            if not self._running:
                raise CaptureStateError("capture is not running")
            if self._processor is None:
                raise CaptureStateError("capture processor is not initialized")
            reset = getattr(self._processor, "reset_echo_path", None)
            if not callable(reset):
                raise CaptureStateError(
                    "capture processor does not support echo-path reset"
                )
            reset()
            state = self._processor.state
        self._emit(
            "echo_path_reset",
            echo_path_reset_count=getattr(state, "echo_path_reset_count", 0),
            alignment_epoch=state.alignment_epoch,
        )

    def stop(self, *, error: str | None = None, status_name: str | None = None) -> None:
        """Stop once, retrying only cleanup work left by an earlier attempt."""

        if self._is_processing_worker():
            self._request_stop_from_processing_worker(error, status_name)
            return

        owner_thread_id = threading.get_ident()
        with self._stop_condition:
            while True:
                if self._stop_complete:
                    return
                if self._stop_in_progress:
                    if self._stop_owner_thread_id == owner_thread_id:
                        return
                    self._stop_condition.wait()
                    continue
                with self._state_lock:
                    if not self._running and not self._ever_started:
                        return
                    self._running = False
                self._record_stop_request_locked(error, status_name)
                self._stop_in_progress = True
                self._stop_owner_thread_id = owner_thread_id
                break

        try:
            self._stop_once()
        finally:
            with self._stop_condition:
                self._stop_in_progress = False
                self._stop_owner_thread_id = None
                self._stop_condition.notify_all()

    def _is_processing_worker(self) -> bool:
        with self._state_lock:
            return self._processing_thread is threading.current_thread()

    def _request_stop_from_processing_worker(
        self,
        error: str | None,
        status_name: str | None,
    ) -> None:
        """Avoid making the pairing thread wait for its own join."""

        launch_retry = False
        with self._stop_condition:
            if self._stop_complete:
                return
            self._record_stop_request_locked(error, status_name)
            if not self._stop_retry_scheduled:
                self._stop_retry_scheduled = True
                launch_retry = True
        self._processing_stop.set()
        if launch_retry:
            threading.Thread(
                target=self._retry_stop_after_processing_callback,
                name="echoff-stop-finalizer",
                daemon=True,
            ).start()

    def _retry_stop_after_processing_callback(self) -> None:
        try:
            self.stop()
        except Exception:
            LOGGER.exception("capture stop requested from processing callback failed")
        finally:
            with self._stop_condition:
                self._stop_retry_scheduled = False

    def _record_stop_request_locked(
        self,
        error: str | None,
        status_name: str | None,
    ) -> None:
        if self._stop_request_recorded:
            return
        self._stop_request_recorded = True
        self._stop_requested_error = error
        self._stop_requested_status_name = status_name

    def _stop_once(self) -> None:
        cleanup_errors: list[Exception] = []
        self._initialize_pending_source_stops()
        for label, source in (
            ("reference", self._reference),
            ("microphone", self._microphone),
        ):
            if label not in self._stop_pending_source_names:
                continue
            if source is None:
                self._stop_pending_source_names.discard(label)
                continue
            try:
                source.stop()
            except Exception as exc:
                LOGGER.exception("%s source cleanup failed", label)
                self._remember_stop_cleanup_error(exc)
                cleanup_errors.append(exc)
            else:
                self._stop_pending_source_names.discard(label)

        worker_joined = self._stop_processing()
        if not worker_joined:
            worker_error = self._processing_error
            if worker_error is None:  # pragma: no cover - defensive fallback
                worker_error = AudioBackendError("capture processing thread did not stop")
            self._remember_stop_cleanup_error(worker_error)
            cleanup_errors.append(worker_error)

        if worker_joined and not self._stop_pending_source_names:
            self._freeze_stop_terminal()
            self._emit_stop_event_once(cleanup_errors)
            if self._stop_event_emitted:
                self._finalize_stop_artifacts(cleanup_errors)

        if self._stop_lifecycle_complete():
            with self._stop_condition:
                self._stop_complete = True
            LOGGER.info("capture stopped: status=%s", self._stop_terminal_status)

        if cleanup_errors:
            raise AudioBackendError(
                f"capture cleanup failed: {cleanup_errors[0]}"
            ) from cleanup_errors[0]

    def _initialize_pending_source_stops(self) -> None:
        if self._stop_sources_initialized:
            return
        self._stop_sources_initialized = True
        if self._reference is not None:
            self._stop_pending_source_names.add("reference")
        if self._microphone is not None:
            self._stop_pending_source_names.add("microphone")

    def _remember_stop_cleanup_error(self, error: Exception) -> None:
        if self._stop_first_cleanup_error is None:
            self._stop_first_cleanup_error = error

    def _freeze_stop_terminal(self) -> None:
        if self._stop_terminal_decided:
            return
        effective_error = self._stop_requested_error
        if effective_error is None:
            try:
                self.raise_if_failed()
            except Exception as exc:
                effective_error = str(exc)
        if effective_error is None and self._stop_first_cleanup_error is not None:
            effective_error = f"capture cleanup failed: {self._stop_first_cleanup_error}"
        self._stop_terminal_error = effective_error
        self._stop_terminal_status = self._derive_terminal_status(effective_error)
        self._stop_terminal_decided = True

    def _derive_terminal_status(self, effective_error: str | None) -> str:
        reference_blocks = (
            0 if self._reference is None else self._reference.device_block_count
        )
        microphone_blocks = (
            0 if self._microphone is None else self._microphone.device_block_count
        )
        incomplete = (
            reference_blocks <= 0
            or microphone_blocks <= 0
            or self._processed_slot_count <= 0
            or self._aligner.mode is not AlignmentMode.LOCKED
        )
        degraded = (
            self._reference_failure is not None
            or self._microphone_failure is not None
            or self._aligner.mode is AlignmentMode.DEGRADED
        )
        if effective_error is not None:
            return "failed"
        if (
            self._stop_requested_status_name is not None
            and self._stop_requested_status_name != "completed"
        ):
            return self._stop_requested_status_name
        if degraded:
            return "degraded"
        if incomplete:
            return "incomplete"
        return "completed"

    def _emit_stop_event_once(self, cleanup_errors: list[Exception]) -> None:
        if self._stop_event_emitted:
            return
        try:
            self._emit(
                "capture_stopped",
                status=self._stop_terminal_status,
                error=self._stop_terminal_error,
            )
        except Exception as exc:
            LOGGER.exception("capture stop event could not be written")
            cleanup_errors.append(exc)
        else:
            self._stop_event_emitted = True

    def _finalize_stop_artifacts(self, cleanup_errors: list[Exception]) -> None:
        if self._stop_artifacts_finalized:
            return
        if self._artifacts is None or self._started_monotonic is None:
            self._stop_artifacts_finalized = True
            return
        try:
            self._artifacts.finalize(
                status_name=self._stop_terminal_status or "failed",
                capture_status=self.status(),
                started_utc=self._started_utc or _utc_now(),
                ended_utc=_utc_now(),
                duration_s=max(0.0, time.monotonic() - self._started_monotonic),
                error=self._stop_terminal_error,
                metadata=dict(self._summary_metadata),
                timeline_started_monotonic=self._timeline_started_monotonic,
            )
        except Exception as exc:
            LOGGER.exception("capture artifact finalization failed")
            cleanup_errors.append(exc)
        else:
            self._stop_artifacts_finalized = True

    def _stop_lifecycle_complete(self) -> bool:
        with self._state_lock:
            worker_joined = self._processing_thread is None
        return (
            not self._stop_pending_source_names
            and worker_joined
            and self._stop_terminal_decided
            and self._stop_event_emitted
            and self._stop_artifacts_finalized
        )

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
        self._note_reference_failure(phase="health")
        self._note_microphone_failure(phase="health")
        if self._processing_error is not None:
            raise AudioBackendError(f"capture processing failed: {self._processing_error}")
        reference_queue_s, microphone_queue_s = self._pending_audio_seconds()
        worker_manages_degraded_backlog = (
            self._aligner.mode is AlignmentMode.DEGRADED
            and self._processing_thread is not None
            and self._processing_thread.is_alive()
        )
        if (
            not worker_manages_degraded_backlog
            and max(reference_queue_s, microphone_queue_s) > self.config.queue_fatal_s
        ):
            raise AudioBackendError(
                "capture processing backlog exceeded the configured fatal limit: "
                f"reference={reference_queue_s:.3f}s microphone={microphone_queue_s:.3f}s"
            )
        reference_diagnostics = self._source_diagnostics(self._reference)
        microphone_diagnostics = self._source_diagnostics(self._microphone)
        reference_callback_queue_overflows = int(
            self._source_diagnostic_value(
                self._reference,
                reference_diagnostics,
                "callback_queue_overflow_count",
                0,
            )
            or 0
        )
        microphone_callback_queue_overflows = int(
            self._source_diagnostic_value(
                self._microphone,
                microphone_diagnostics,
                "callback_queue_overflow_count",
                0,
            )
            or 0
        )
        if reference_callback_queue_overflows or microphone_callback_queue_overflows:
            prior_failure = self._reference_failure or self._microphone_failure
            overflow_error = AudioBackendError(
                "capture callback queue overflow: "
                f"reference={reference_callback_queue_overflows} "
                f"microphone={microphone_callback_queue_overflows}"
            )
            if prior_failure is not None:
                raise AudioBackendError(
                    f"{prior_failure}; {overflow_error}"
                ) from prior_failure
            raise overflow_error

    def status(self) -> CaptureStatus:
        snapshot = self._aligner.snapshot
        processor_state = (
            self._processor.state if self._processor is not None else AecState(False, 0.0, 0, 0)
        )
        reference = self._reference
        microphone = self._microphone
        reference_diagnostics = self._source_diagnostics(reference)
        microphone_diagnostics = self._source_diagnostics(microphone)
        reference_failure = self._reference_failure
        if reference_failure is None and reference is not None:
            reference_failure = reference.error
        microphone_failure = self._microphone_failure
        if microphone_failure is None and microphone is not None:
            microphone_failure = microphone.error
        reference_callback_queue_overflows = int(
            self._source_diagnostic_value(
                reference,
                reference_diagnostics,
                "callback_queue_overflow_count",
                0,
            )
            or 0
        )
        microphone_callback_queue_overflows = int(
            self._source_diagnostic_value(
                microphone,
                microphone_diagnostics,
                "callback_queue_overflow_count",
                0,
            )
            or 0
        )
        errors: list[str] = []
        if self._processing_error is not None:
            errors.append(f"capture processing failed: {self._processing_error}")
        if reference_callback_queue_overflows or microphone_callback_queue_overflows:
            errors.append(
                "capture callback queue overflow: "
                f"reference={reference_callback_queue_overflows} "
                f"microphone={microphone_callback_queue_overflows}"
            )
        pair_count = snapshot.pair_count
        return CaptureStatus(
            running=self._running,
            alignment_locked=snapshot.locked,
            alignment_epoch=snapshot.epoch,
            processed_pair_count=self._processed_slot_count,
            matched_reference_blocks=pair_count,
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
            clock_suspect_observation_count=snapshot.clock_suspect_observation_count,
            hard_discontinuity_count=snapshot.hard_discontinuity_count,
            alignment_mode=snapshot.mode,
            zero_filled_reference_blocks=snapshot.zero_filled_reference_blocks,
            late_reference_blocks=snapshot.late_reference_blocks,
            clock_correction_count=snapshot.clock_correction_count,
            last_mismatch_ms=(
                None if snapshot.last_mismatch_s is None else 1000.0 * snapshot.last_mismatch_s
            ),
            shutdown_unpaired_reference_blocks=snapshot.shutdown_unpaired_reference_blocks,
            shutdown_unpaired_microphone_blocks=snapshot.shutdown_unpaired_microphone_blocks,
            reference_audio_s=self._reference_sample_count / self.config.sample_rate,
            microphone_audio_s=self._microphone_sample_count / self.config.sample_rate,
            reference_queue_s=self._pending_audio_seconds()[0],
            microphone_queue_s=self._pending_audio_seconds()[1],
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
            reference_device_blocks=self._source_diagnostic_value(
                reference, reference_diagnostics, "device_block_count", 0
            ),
            reference_silence_blocks=self._source_diagnostic_value(
                reference,
                reference_diagnostics,
                "synthetic_silence_block_count",
                0,
            ),
            reference_dropped_device_blocks=self._source_diagnostic_value(
                reference,
                reference_diagnostics,
                "dropped_device_block_count",
                0,
            ),
            reference_timestamp_regressions=self._source_diagnostic_value(
                reference, reference_diagnostics, "timestamp_regression_count", 0
            ),
            reference_invalid_timestamps=self._source_diagnostic_value(
                reference, reference_diagnostics, "invalid_timestamp_count", 0
            ),
            reference_timestamp_deviation_max_ms=1000.0
            * self._source_diagnostic_value(
                reference, reference_diagnostics, "timestamp_deviation_max_s", 0.0
            ),
            reference_timestamp_gap_blocks=self._source_diagnostic_value(
                reference, reference_diagnostics, "timestamp_gap_block_count", 0
            ),
            reference_timestamp_anomalies=self._source_diagnostic_value(
                reference, reference_diagnostics, "timestamp_anomaly_count", 0
            ),
            reference_callback_status_count=self._source_diagnostic_value(
                reference, reference_diagnostics, "callback_status_count", 0
            ),
            reference_input_overflow_count=self._source_diagnostic_value(
                reference, reference_diagnostics, "input_overflow_count", 0
            ),
            reference_input_underflow_count=self._source_diagnostic_value(
                reference, reference_diagnostics, "input_underflow_count", 0
            ),
            reference_padded_samples=self._source_diagnostic_value(
                reference, reference_diagnostics, "padded_sample_count", 0
            ),
            reference_callback_packet_count=self._source_diagnostic_value(
                reference, reference_diagnostics, "callback_packet_count", 0
            ),
            reference_callback_payload_frames=self._source_diagnostic_value(
                reference, reference_diagnostics, "callback_payload_frame_count", 0
            ),
            reference_callback_queue_high_watermark_blocks=self._source_diagnostic_value(
                reference,
                reference_diagnostics,
                "callback_queue_high_watermark_blocks",
                0
                if reference is None
                else getattr(reference, "callback_queue_high_watermark", 0),
            ),
            reference_callback_queue_age_max_ms=1000.0
            * self._source_diagnostic_value(
                reference, reference_diagnostics, "callback_queue_age_max_s", 0.0
            ),
            reference_callback_enqueue_max_ms=1000.0
            * self._source_diagnostic_value(
                reference, reference_diagnostics, "callback_enqueue_max_s", 0.0
            ),
            reference_callback_timeline_drift_ms=1000.0
            * self._source_diagnostic_value(
                reference, reference_diagnostics, "callback_timeline_drift_s", 0.0
            ),
            reference_callback_timeline_drift_max_ms=1000.0
            * self._source_diagnostic_value(
                reference,
                reference_diagnostics,
                "callback_timeline_drift_max_s",
                0.0,
            ),
            microphone_device_blocks=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "device_block_count", 0
            ),
            microphone_silence_blocks=self._source_diagnostic_value(
                microphone,
                microphone_diagnostics,
                "synthetic_silence_block_count",
                0,
            ),
            microphone_dropped_device_blocks=self._source_diagnostic_value(
                microphone,
                microphone_diagnostics,
                "dropped_device_block_count",
                0,
            ),
            microphone_timestamp_regressions=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "timestamp_regression_count", 0
            ),
            microphone_invalid_timestamps=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "invalid_timestamp_count", 0
            ),
            microphone_timestamp_deviation_max_ms=1000.0
            * self._source_diagnostic_value(
                microphone, microphone_diagnostics, "timestamp_deviation_max_s", 0.0
            ),
            microphone_timestamp_gap_blocks=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "timestamp_gap_block_count", 0
            ),
            microphone_timestamp_anomalies=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "timestamp_anomaly_count", 0
            ),
            microphone_callback_status_count=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "callback_status_count", 0
            ),
            microphone_input_overflow_count=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "input_overflow_count", 0
            ),
            microphone_input_underflow_count=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "input_underflow_count", 0
            ),
            microphone_padded_samples=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "padded_sample_count", 0
            ),
            microphone_callback_packet_count=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "callback_packet_count", 0
            ),
            microphone_callback_payload_frames=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "callback_payload_frame_count", 0
            ),
            microphone_callback_queue_high_watermark_blocks=self._source_diagnostic_value(
                microphone,
                microphone_diagnostics,
                "callback_queue_high_watermark_blocks",
                0
                if microphone is None
                else getattr(microphone, "callback_queue_high_watermark", 0),
            ),
            microphone_callback_queue_age_max_ms=1000.0
            * self._source_diagnostic_value(
                microphone, microphone_diagnostics, "callback_queue_age_max_s", 0.0
            ),
            microphone_callback_enqueue_max_ms=1000.0
            * self._source_diagnostic_value(
                microphone, microphone_diagnostics, "callback_enqueue_max_s", 0.0
            ),
            microphone_callback_timeline_drift_ms=1000.0
            * self._source_diagnostic_value(
                microphone, microphone_diagnostics, "callback_timeline_drift_s", 0.0
            ),
            microphone_callback_timeline_drift_max_ms=1000.0
            * self._source_diagnostic_value(
                microphone,
                microphone_diagnostics,
                "callback_timeline_drift_max_s",
                0.0,
            ),
            echo_path_ready=(
                processor_state.echo_path_ready
                and self._aligner.alignment_ready
                and reference_failure is None
            ),
            far_end_active_s=processor_state.far_end_active_s,
            stream_alignment_reset_count=processor_state.stream_alignment_reset_count,
            echo_path_quality_ready=processor_state.echo_path_quality_ready,
            echo_suppression_db=processor_state.echo_suppression_db,
            echo_quality_s=processor_state.echo_quality_s,
            echo_path_reset_count=getattr(processor_state, "echo_path_reset_count", 0),
            processing_mean_ms=(
                0.0
                if self._processed_slot_count <= 0
                else 1000.0 * self._processing_total_s / self._processed_slot_count
            ),
            processing_max_ms=1000.0 * self._processing_max_s,
            worker_slot_mean_ms=(
                0.0
                if self._processed_slot_count <= 0
                else 1000.0 * self._worker_slot_total_s / self._processed_slot_count
            ),
            worker_slot_max_ms=1000.0 * self._worker_slot_max_s,
            synchronization_wait_count=self._synchronization_wait_count,
            synchronization_wait_completed_count=(
                self._synchronization_wait_completed_count
            ),
            synchronization_wait_timeout_count=(
                self._synchronization_wait_timeout_count
            ),
            synchronization_wait_total_ms=1000.0 * self._synchronization_wait_total_s,
            synchronization_wait_max_ms=1000.0 * self._synchronization_wait_max_s,
            synchronization_max_backlog_blocks=(
                self._synchronization_max_backlog_blocks
            ),
            synchronization_catchup_total_ms=(
                1000.0 * self._synchronization_catchup_total_s
            ),
            synchronization_catchup_max_ms=(
                1000.0 * self._synchronization_catchup_max_s
            ),
            source_failure_count=self._source_failure_count,
            degraded_unpaired_reference_blocks=(
                snapshot.degraded_unpaired_reference_blocks
            ),
            degraded_unpaired_microphone_blocks=(
                snapshot.degraded_unpaired_microphone_blocks
            ),
            wait_timeout_unpaired_reference_blocks=(
                snapshot.wait_timeout_unpaired_reference_blocks
            ),
            wait_timeout_unpaired_microphone_blocks=(
                snapshot.wait_timeout_unpaired_microphone_blocks
            ),
            source_failure_unpaired_reference_blocks=(
                snapshot.source_failure_unpaired_reference_blocks
            ),
            source_failure_unpaired_microphone_blocks=(
                snapshot.source_failure_unpaired_microphone_blocks
            ),
            startup_unpaired_reference_blocks=(
                snapshot.startup_unpaired_reference_blocks
            ),
            startup_unpaired_microphone_blocks=(
                snapshot.startup_unpaired_microphone_blocks
            ),
            reference_queue_overflow_count=self._reference_queue_overflow_count,
            microphone_queue_overflow_count=self._microphone_queue_overflow_count,
            reference_callback_queue_overflow_count=self._source_diagnostic_value(
                reference, reference_diagnostics, "callback_queue_overflow_count", 0
            ),
            microphone_callback_queue_overflow_count=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "callback_queue_overflow_count", 0
            ),
            microphone_fallback_used=self._source_diagnostic_value(
                microphone, microphone_diagnostics, "fallback_used", False
            ),
            microphone_backend_attempt_errors=tuple(
                self._source_diagnostic_value(
                    microphone,
                    microphone_diagnostics,
                    "backend_attempt_errors",
                    (),
                )
            ),
            reference_callback_queue_high_watermark_packets=self._source_diagnostic_value(
                reference,
                reference_diagnostics,
                "callback_queue_high_watermark_packets",
                0,
            ),
            microphone_callback_queue_high_watermark_packets=self._source_diagnostic_value(
                microphone,
                microphone_diagnostics,
                "callback_queue_high_watermark_packets",
                0,
            ),
            hard_discontinuity_unpaired_reference_blocks=(
                snapshot.hard_discontinuity_unpaired_reference_blocks
            ),
            hard_discontinuity_unpaired_microphone_blocks=(
                snapshot.hard_discontinuity_unpaired_microphone_blocks
            ),
            reference_error=(
                None
                if reference_failure is None
                else f"reference source failed: {reference_failure}"
            ),
            microphone_error=(
                None
                if microphone_failure is None
                else f"microphone source failed: {microphone_failure}"
            ),
            error=None if not errors else "; ".join(errors),
        )

    def _note_reference_failure(
        self,
        *,
        phase: str,
        error: Exception | None = None,
    ) -> Exception | None:
        source = self._reference
        failure = error
        if failure is None and source is not None:
            failure = source.error
        if failure is None:
            return self._reference_failure
        emit = False
        with self._state_lock:
            if self._reference_failure is None:
                self._reference_failure = failure
                self._source_failure_count += 1
            if not self._reference_failure_reported:
                self._reference_failure_reported = True
                emit = True
            recorded = self._reference_failure
        if emit:
            self._emit(
                "reference_source_degraded",
                phase=phase,
                error=str(recorded),
                backend=None if source is None else source.backend_name,
                device_name=None if source is None else source.selected_device_name,
                device_index=None if source is None else source.selected_device_index,
            )
        return recorded

    def _note_microphone_failure(
        self,
        *,
        phase: str,
        error: Exception | None = None,
    ) -> Exception | None:
        source = self._microphone
        failure = error
        if failure is None and source is not None:
            failure = source.error
        if failure is None:
            return self._microphone_failure
        emit = False
        with self._state_lock:
            if self._microphone_failure is None:
                self._microphone_failure = failure
                self._source_failure_count += 1
            if not self._microphone_failure_reported:
                self._microphone_failure_reported = True
                emit = True
            recorded = self._microphone_failure
        if emit:
            self._emit(
                "microphone_source_degraded",
                phase=phase,
                error=str(recorded),
                backend=None if source is None else source.backend_name,
                device_name=None if source is None else source.selected_device_name,
                device_index=None if source is None else source.selected_device_index,
            )
        return recorded

    def _emit(self, kind: str, **details: Any) -> None:
        event = CaptureEvent(
            kind=kind,
            monotonic=time.monotonic(),
            utc=_utc_now(),
            details=details,
        )
        self._write_console_diagnostic(event)
        if self._artifacts is not None:
            self._artifacts.events.write(event)
        try:
            self.on_event(event)
        except Exception:
            LOGGER.exception("on_event callback failed for %s", kind)

    def _write_console_diagnostic(self, event: CaptureEvent) -> None:
        if not self.console_diagnostics:
            return
        level = _CONSOLE_DIAGNOSTIC_LEVELS.get(event.kind)
        if level is None:
            return
        message = _CONSOLE_DIAGNOSTIC_MESSAGES[event.kind]
        details = " ".join(
            f"{key}={value!r}" for key, value in sorted(event.details.items())
        )
        line = f"[echoff {level}] {message}"
        if details:
            line = f"{line} ({details})"
        stream = sys.stderr
        try:
            if stream.isatty() and level in {"ERROR", "WARNING"}:
                line = f"{_ANSI_BRIGHT_RED}{line}{_ANSI_RESET}"
            print(line, file=stream, flush=True)
        except Exception:
            LOGGER.exception("console diagnostic could not be written for %s", event.kind)

    def _enqueue_reference(self, block: AudioBlock) -> None:
        valid_samples = (
            len(block.samples) if block.valid_samples is None else block.valid_samples
        )
        try:
            self._reference_queue.put_nowait(block)
        except queue.Full as exc:
            error = AudioBackendError(
                "reference capture queue exceeded its configured fatal capacity: "
                f"{self._capture_queue_capacity_blocks} blocks"
            )
            with self._state_lock:
                self._reference_queue_overflow_count += 1
                if self._processing_error is None:
                    self._processing_error = error
            self._startup_ready.set()
            raise error from exc
        self._reference_sample_count += valid_samples
        self._startup_ready.set()

    def _enqueue_microphone(self, block: AudioBlock) -> None:
        valid_samples = (
            len(block.samples) if block.valid_samples is None else block.valid_samples
        )
        try:
            self._microphone_queue.put_nowait(block)
        except queue.Full as exc:
            error = AudioBackendError(
                "microphone capture queue exceeded its configured fatal capacity: "
                f"{self._capture_queue_capacity_blocks} blocks"
            )
            with self._state_lock:
                self._microphone_queue_overflow_count += 1
                if self._processing_error is None:
                    self._processing_error = error
            self._startup_ready.set()
            raise error from exc
        self._microphone_sample_count += valid_samples
        self._startup_ready.set()

    def _pending_audio_seconds(self) -> tuple[float, float]:
        with self._state_lock:
            internal_reference = self._internal_reference_pending_blocks
            internal_microphone = self._internal_microphone_pending_blocks
        return (
            (self._reference_queue.qsize() + internal_reference)
            * self.config.block_duration_s,
            (self._microphone_queue.qsize() + internal_microphone)
            * self.config.block_duration_s,
        )

    def _set_internal_pending_counts(
        self,
        references: deque[AudioBlock],
        microphones: deque[AudioBlock],
        reference_slots: dict[int, AudioBlock],
    ) -> None:
        with self._state_lock:
            self._internal_reference_pending_blocks = (
                len(references)
                + len(reference_slots)
                + self._aligner.pending_reference_count
            )
            self._internal_microphone_pending_blocks = len(microphones)

    def _start_processing(self) -> None:
        self._processing_stop.clear()
        self._processing_error = None
        self._processing_thread = threading.Thread(
            target=self._run_processing_guarded,
            name="echoff-pairing",
            daemon=True,
        )
        self._processing_thread.start()

    def _stop_processing(self) -> bool:
        self._processing_stop.set()
        with self._state_lock:
            thread = self._processing_thread
        if thread is None:
            return True
        if thread is threading.current_thread():  # pragma: no cover - guarded by stop
            return False
        thread.join(timeout=3.0)
        if thread.is_alive():
            if self._processing_error is None:
                self._processing_error = AudioBackendError(
                    "capture processing thread did not stop"
                )
            return False
        with self._state_lock:
            if self._processing_thread is thread:
                self._processing_thread = None
        return True

    def _run_processing_guarded(self) -> None:
        try:
            self._run_processing()
        except Exception as exc:
            LOGGER.exception("capture processing worker failed")
            self._processing_error = exc
            self._processing_stop.set()
            self._startup_ready.set()
            self._emit("capture_failed", phase="processing", error=str(exc))

    def _begin_synchronization_wait(
        self,
        *,
        missing_source: str,
        microphones: deque[AudioBlock],
        references: deque[AudioBlock],
        reference_slots: dict[int, AudioBlock],
    ) -> _SynchronizationWait:
        now = time.monotonic()
        microphone_head = microphones[0] if microphones else None
        reference_slot = min(reference_slots) if reference_slots else None
        reference_head = (
            reference_slots[reference_slot]
            if reference_slot is not None
            else (references[0] if references else None)
        )
        offset = self._aligner.reference_offset
        expected_microphone = (
            reference_slot if missing_source == "microphone" else None
        )
        expected_reference = (
            None
            if microphone_head is None or offset is None
            else microphone_head.sequence - offset
        )
        backlog = len(microphones) + len(references) + len(reference_slots)
        wait = _SynchronizationWait(
            missing_source=missing_source,
            started_monotonic=now,
            expected_microphone_sequence=expected_microphone,
            expected_reference_sequence=expected_reference,
            microphone_head_sequence=(
                None if microphone_head is None else microphone_head.sequence
            ),
            reference_head_sequence=(
                None if reference_head is None else reference_head.sequence
            ),
            microphone_callback_monotonic=(
                None if microphone_head is None else microphone_head.callback_monotonic
            ),
            reference_callback_monotonic=(
                None if reference_head is None else reference_head.callback_monotonic
            ),
            max_backlog_blocks=backlog,
            raw_reference_queue_depth=self._reference_queue.qsize(),
            raw_microphone_queue_depth=self._microphone_queue.qsize(),
            internal_reference_queue_depth=len(references),
            internal_microphone_queue_depth=len(microphones),
            mapped_reference_slot_depth=len(reference_slots),
        )
        self._synchronization_wait_count += 1
        self._synchronization_max_backlog_blocks = max(
            self._synchronization_max_backlog_blocks,
            backlog,
        )
        return wait

    def _report_synchronization_wait(self, wait: _SynchronizationWait) -> None:
        """Emit one detailed event only after a wait becomes exceptional."""

        if wait.reported:
            return
        wait.reported = True
        self._emit(
            "synchronization_wait_started",
            missing_source=wait.missing_source,
            expected_microphone_sequence=wait.expected_microphone_sequence,
            expected_reference_sequence=wait.expected_reference_sequence,
            microphone_head_sequence=wait.microphone_head_sequence,
            reference_head_sequence=wait.reference_head_sequence,
            microphone_callback_monotonic=wait.microphone_callback_monotonic,
            reference_callback_monotonic=wait.reference_callback_monotonic,
            worker_first_observed_monotonic=wait.started_monotonic,
            raw_reference_queue_depth=wait.raw_reference_queue_depth,
            raw_microphone_queue_depth=wait.raw_microphone_queue_depth,
            internal_reference_queue_depth=wait.internal_reference_queue_depth,
            internal_microphone_queue_depth=wait.internal_microphone_queue_depth,
            mapped_reference_slot_depth=wait.mapped_reference_slot_depth,
        )

    def _finish_synchronization_wait(
        self,
        wait: _SynchronizationWait,
        *,
        references: deque[AudioBlock],
        microphones: deque[AudioBlock],
        reference_slots: dict[int, AudioBlock],
    ) -> float | None:
        now = time.monotonic()
        duration = max(0.0, now - wait.started_monotonic)
        backlog = len(microphones) + len(references) + len(reference_slots)
        wait.max_backlog_blocks = max(wait.max_backlog_blocks, backlog)
        self._synchronization_wait_completed_count += 1
        self._synchronization_wait_total_s += duration
        self._synchronization_wait_max_s = max(
            self._synchronization_wait_max_s,
            duration,
        )
        self._synchronization_max_backlog_blocks = max(
            self._synchronization_max_backlog_blocks,
            wait.max_backlog_blocks,
        )
        if duration >= self.WAIT_EVENT_THRESHOLD_S:
            self._report_synchronization_wait(wait)
            self._emit(
                "synchronization_wait_ended",
                missing_source=wait.missing_source,
                duration_ms=1000.0 * duration,
                cause="counterpart_arrived",
                maximum_backlog_blocks=wait.max_backlog_blocks,
                internal_reference_queue_depth=len(references),
                internal_microphone_queue_depth=len(microphones),
                mapped_reference_slot_depth=len(reference_slots),
            )
            return now
        return None

    def _retire_degraded_excess(
        self,
        *,
        references: deque[AudioBlock],
        microphones: deque[AudioBlock],
        reference_slots: dict[int, AudioBlock],
        reference_limit: int,
        microphone_limit: int,
    ) -> bool:
        """Bound live AEC buffers after raw payload persistence."""

        made_progress = False
        retired_microphones: list[AudioBlock] = []
        while len(microphones) > microphone_limit:
            retired_microphones.append(microphones.popleft())
        if retired_microphones:
            self._note_degraded_retirement(
                "microphone",
                len(retired_microphones),
                last_sequence=retired_microphones[-1].sequence,
            )
            made_progress = True
        while len(references) + len(reference_slots) > reference_limit:
            if reference_slots:
                reference_slots.pop(min(reference_slots))
                self._note_degraded_retirement("reference", 1)
            else:
                retired = references.popleft()
                self._note_degraded_retirement(
                    "reference",
                    1,
                    last_sequence=retired.sequence,
                )
            made_progress = True
        return made_progress

    def _note_degraded_retirement(
        self,
        source: str,
        count: int,
        *,
        last_sequence: int | None = None,
    ) -> None:
        cause = self._degraded_reason
        if cause not in {"wait_timeout", "source_failure"}:
            raise CaptureStateError("degraded live retirement has no explicit cause")
        self._aligner.note_degraded_unpaired(
            source,
            count,
            cause=cause,
            last_sequence=last_sequence,
        )
        key = (cause, source)
        if key in self._degraded_retirement_reported:
            return
        self._degraded_retirement_reported.add(key)
        self._emit(
            "synchronization_live_buffer_retired",
            source=source,
            cause=cause,
            retired_blocks=count,
            last_sequence=last_sequence,
            raw_payload_preserved=self._artifacts is not None,
        )

    def _emit_synchronization_checkpoint(
        self,
        *,
        wait: _SynchronizationWait | None,
        references: deque[AudioBlock],
        microphones: deque[AudioBlock],
        reference_slots: dict[int, AudioBlock],
    ) -> None:
        snapshot = self._aligner.snapshot
        self._emit(
            "synchronization_checkpoint",
            mode=self._aligner.mode.value,
            wait_missing_source=None if wait is None else wait.missing_source,
            wait_duration_ms=(
                0.0
                if wait is None
                else 1000.0 * max(0.0, time.monotonic() - wait.started_monotonic)
            ),
            internal_reference_queue_depth=len(references),
            internal_microphone_queue_depth=len(microphones),
            mapped_reference_slot_depth=len(reference_slots),
            synchronization_max_backlog_blocks=(
                self._synchronization_max_backlog_blocks
            ),
            degraded_unpaired_reference_blocks=(
                snapshot.degraded_unpaired_reference_blocks
            ),
            degraded_unpaired_microphone_blocks=(
                snapshot.degraded_unpaired_microphone_blocks
            ),
            processed_slots=self._processed_slot_count,
        )

    def _run_processing(self) -> None:
        microphones: deque[AudioBlock] = deque()
        references: deque[AudioBlock] = deque()
        reference_slots: dict[int, AudioBlock] = {}
        observed_microphone_sequence: int | None = None
        slot_hard_discontinuity = False
        synchronization_wait: _SynchronizationWait | None = None
        catchup_started: float | None = None
        catchup_max_backlog = 0
        next_checkpoint = time.monotonic() + 30.0
        synchronization_wait_limit_s = min(
            self.config.reference_stall_grace_s,
            self.config.queue_fatal_s,
        )
        fatal_capacity_blocks = self._capture_queue_capacity_blocks
        fatal_backlog = False
        fatal_spooled_reference_blocks = 0
        fatal_spooled_microphone_blocks = 0
        buffer_limit = max(
            1,
            math.ceil(synchronization_wait_limit_s / self.config.block_duration_s),
        )
        poll_s = min(0.005, self.config.block_duration_s / 4.0)
        while True:
            stopping = self._processing_stop.is_set()
            reference_failed = self._note_reference_failure(phase="runtime") is not None
            microphone_failed = self._note_microphone_failure(phase="runtime") is not None
            if not fatal_backlog and self._processing_error is not None:
                fatal_backlog = True
                self._emit(
                    "capture_failed",
                    phase="processing",
                    error=str(self._processing_error),
                )
            if fatal_backlog:
                while True:
                    try:
                        received_reference = self._reference_queue.get_nowait()
                    except queue.Empty:
                        break
                    if self._artifacts is not None:
                        valid_samples = (
                            len(received_reference.samples)
                            if received_reference.valid_samples is None
                            else received_reference.valid_samples
                        )
                        self._artifacts.write_reference_received(
                            received_reference.samples[:valid_samples]
                        )
                    fatal_spooled_reference_blocks += 1
                while True:
                    try:
                        received_microphone = self._microphone_queue.get_nowait()
                    except queue.Empty:
                        break
                    if self._artifacts is not None:
                        valid_samples = (
                            len(received_microphone.samples)
                            if received_microphone.valid_samples is None
                            else received_microphone.valid_samples
                        )
                        self._artifacts.write_microphone_received(
                            received_microphone.samples[:valid_samples]
                        )
                    fatal_spooled_microphone_blocks += 1
                self._set_internal_pending_counts(
                    references,
                    microphones,
                    reference_slots,
                )
                if stopping:
                    pending = self._aligner.drain_pending_references()
                    self._aligner.note_shutdown_unpaired(
                        "reference",
                        len(references)
                        + len(reference_slots)
                        + len(pending)
                        + fatal_spooled_reference_blocks,
                    )
                    self._aligner.note_shutdown_unpaired(
                        "microphone",
                        len(microphones) + fatal_spooled_microphone_blocks,
                    )
                    with self._state_lock:
                        self._internal_reference_pending_blocks = 0
                        self._internal_microphone_pending_blocks = 0
                    return
                self._processing_stop.wait(poll_s)
                continue

            reference_internal_blocks = (
                len(references)
                + len(reference_slots)
                + self._aligner.pending_reference_count
            )
            while stopping or reference_internal_blocks < fatal_capacity_blocks:
                try:
                    received_reference = self._reference_queue.get_nowait()
                except queue.Empty:
                    break
                if self._artifacts is not None:
                    valid_samples = (
                        len(received_reference.samples)
                        if received_reference.valid_samples is None
                        else received_reference.valid_samples
                    )
                    self._artifacts.write_reference_received(
                        received_reference.samples[:valid_samples]
                    )
                references.append(received_reference)
                reference_internal_blocks += 1
            microphone_internal_blocks = len(microphones)
            while stopping or microphone_internal_blocks < fatal_capacity_blocks:
                try:
                    microphone = self._microphone_queue.get_nowait()
                except queue.Empty:
                    break
                if self._artifacts is not None:
                    valid_samples = (
                        len(microphone.samples)
                        if microphone.valid_samples is None
                        else microphone.valid_samples
                    )
                    self._artifacts.write_microphone_received(
                        microphone.samples[:valid_samples]
                    )
                microphones.append(microphone)
                microphone_internal_blocks += 1

            self._set_internal_pending_counts(references, microphones, reference_slots)
            now = time.monotonic()
            if now >= next_checkpoint:
                self._emit_synchronization_checkpoint(
                    wait=synchronization_wait,
                    references=references,
                    microphones=microphones,
                    reference_slots=reference_slots,
                )
                next_checkpoint = now + 30.0

            made_progress = False

            # A discontinuity is a proven epoch barrier. Pairable old-epoch
            # data has already drained; remaining payloads stay in the raw
            # source artifacts and receive an explicit cause.
            if self._aligner.mode is AlignmentMode.DEGRADED:
                microphone_barrier = next(
                    (
                        index
                        for index, block in enumerate(microphones)
                        if block.discontinuity
                    ),
                    None,
                )
                reference_barrier = next(
                    (
                        index
                        for index, block in enumerate(references)
                        if block.discontinuity
                    ),
                    None,
                )
                if microphone_barrier is not None or reference_barrier is not None:
                    old_microphones = 0 if microphone_barrier is None else microphone_barrier
                    for _index in range(old_microphones):
                        microphones.popleft()
                    old_references = len(reference_slots)
                    reference_slots.clear()
                    if reference_barrier is not None:
                        old_references += reference_barrier
                        for _index in range(reference_barrier):
                            references.popleft()
                    elif microphone_barrier is not None:
                        # Without a reference-side epoch marker, no queued
                        # reference can be proven to belong after the mic barrier.
                        old_references += len(references)
                        references.clear()
                    if old_microphones:
                        self._aligner.note_hard_discontinuity_unpaired(
                            "microphone",
                            old_microphones,
                        )
                    if old_references:
                        self._aligner.note_hard_discontinuity_unpaired(
                            "reference",
                            old_references,
                        )
                    observed_microphone_sequence = None
                    synchronization_wait = None
                    made_progress = bool(old_microphones or old_references)

            # With a confirmed mapping, references can be assigned by sequence
            # while the microphone callback is the delayed source.
            while (
                not microphones
                and references
                and self._aligner.reference_offset is not None
                and not references[0].discontinuity
            ):
                update = self._aligner.ingest_reference(references.popleft())
                self._apply_alignment_update(
                    update,
                    reference_slots,
                    next_master_slot=None,
                )
                made_progress = True

            while microphones:
                microphone = microphones[0]
                slot = microphone.sequence
                if observed_microphone_sequence != slot:
                    update = self._aligner.observe_microphone(microphone)
                    if update.hard_discontinuity:
                        if not slot_hard_discontinuity:
                            self._emit(
                                "alignment_discontinuity_pending",
                                source="microphone",
                                sequence=microphone.sequence,
                            )
                        self._requeue_invalidated_references(
                            update,
                            slot=slot,
                            references=references,
                            reference_slots=reference_slots,
                        )
                        slot_hard_discontinuity = True
                    observed_microphone_sequence = slot
                    self._apply_alignment_update(
                        update,
                        reference_slots,
                        next_master_slot=slot,
                        suppress_lock_event=slot_hard_discontinuity,
                    )
                # The head stays in place until a reference can be mapped. Keep
                # confirming from newly queued mic blocks instead of testing the
                # one-block look-ahead only once and deadlocking initial join.
                self._aligner.confirm_microphone_phase(tuple(microphones))

                while references:
                    candidate = references[0]
                    hint = self._aligner.reference_slot_hint(candidate)
                    stable_mapping = self._aligner.reference_offset is not None
                    if (
                        stable_mapping
                        and candidate.discontinuity
                        and hint != slot
                        and self._aligner.mode is not AlignmentMode.DEGRADED
                    ):
                        break
                    if hint is None and not stable_mapping:
                        break
                    joining = self._aligner.mode in {
                        AlignmentMode.MICROPHONE_ONLY,
                        AlignmentMode.JOINING,
                    }
                    if (
                        not stable_mapping
                        and hint is not None
                        and hint > slot
                        and (
                            not joining
                            or (
                                hint
                                > (
                                    self._aligner.join_validation_microphone_sequence
                                    if self._aligner.join_validation_microphone_sequence
                                    is not None
                                    else slot
                                )
                                + (
                                    0
                                    if self._aligner.join_validation_microphone_barrier
                                    else self._aligner.JOIN_HORIZON_BLOCKS
                                )
                                + (
                                    0
                                    if self._aligner.join_validation_microphone_barrier
                                    else self._aligner.pending_reference_count
                                )
                            )
                            or candidate.discontinuity
                        )
                    ):
                        break
                    references.popleft()
                    update = self._aligner.ingest_reference(candidate)
                    if update.hard_discontinuity:
                        if not slot_hard_discontinuity:
                            self._emit(
                                "alignment_discontinuity_pending",
                                source="reference",
                                sequence=candidate.sequence,
                            )
                        self._discard_invalidated_references(
                            update,
                            slot=slot,
                            reference_slots=reference_slots,
                        )
                        slot_hard_discontinuity = True
                    self._apply_alignment_update(
                        update,
                        reference_slots,
                        next_master_slot=slot,
                        suppress_lock_event=slot_hard_discontinuity,
                    )
                    made_progress = True
                    if slot in reference_slots and self._aligner.alignment_ready:
                        break

                if self._aligner.mode is AlignmentMode.DEGRADED:
                    while reference_slots and min(reference_slots) < slot:
                        reference_slots.pop(min(reference_slots))
                        self._note_degraded_retirement("reference", 1)
                        made_progress = True

                reference = reference_slots.pop(slot) if slot in reference_slots else None
                if reference is not None:
                    if synchronization_wait is not None:
                        catchup_started = self._finish_synchronization_wait(
                            synchronization_wait,
                            references=references,
                            microphones=microphones,
                            reference_slots=reference_slots,
                        )
                        catchup_max_backlog = (
                            len(references) + len(microphones) + len(reference_slots)
                        )
                        synchronization_wait = None
                    if self._aligner.mark_synchronization_recovered():
                        self._emit(
                            "synchronization_recovered",
                            microphone_sequence=microphone.sequence,
                            reference_sequence=reference.sequence,
                            reset_apm=False,
                        )
                        self._degraded_reason = None
                        self._degraded_retirement_reported.clear()
                    microphones.popleft()
                    self._handle_slot_discontinuity(
                        slot=slot,
                        microphone=microphone,
                        reference=reference,
                        slot_hard_discontinuity=slot_hard_discontinuity,
                    )
                    self._process_master_slot(reference=reference, microphone=microphone)
                    observed_microphone_sequence = None
                    slot_hard_discontinuity = False
                    made_progress = True
                    continue

                # A confirmed initial map can prove that the reference source
                # started after the microphone. Those leading microphone slots
                # can never receive a real counterpart. Retire them explicitly;
                # raw source artifacts already preserve their actual payload.
                if (
                    self._aligner.alignment_ready
                    and self._aligner.snapshot.pair_count == 0
                    and reference_slots
                    and min(reference_slots) > slot
                ):
                    microphones.popleft()
                    self._aligner.note_startup_unpaired_microphone(1)
                    observed_microphone_sequence = None
                    made_progress = True
                    continue

                if self._aligner.mode is AlignmentMode.DEGRADED and reference_slots:
                    earliest_reference_slot = min(reference_slots)
                    if earliest_reference_slot > slot:
                        microphones.popleft()
                        self._note_degraded_retirement(
                            "microphone",
                            1,
                            last_sequence=microphone.sequence,
                        )
                        observed_microphone_sequence = None
                        made_progress = True
                        continue

                if synchronization_wait is None:
                    synchronization_wait = self._begin_synchronization_wait(
                        missing_source="reference",
                        microphones=microphones,
                        references=references,
                        reference_slots=reference_slots,
                    )
                backlog = len(microphones) + len(references) + len(reference_slots)
                synchronization_wait.max_backlog_blocks = max(
                    synchronization_wait.max_backlog_blocks,
                    backlog,
                )
                elapsed = time.monotonic() - synchronization_wait.started_monotonic
                if elapsed >= self.WAIT_EVENT_THRESHOLD_S:
                    self._report_synchronization_wait(synchronization_wait)
                reason = None
                if reference_failed:
                    reason = "source_failure"
                elif elapsed >= synchronization_wait_limit_s:
                    reason = "wait_timeout"
                if reason is not None and self._aligner.mark_synchronization_degraded():
                    self._report_synchronization_wait(synchronization_wait)
                    self._degraded_reason = reason
                    wait_duration = max(0.0, elapsed)
                    self._synchronization_wait_total_s += wait_duration
                    self._synchronization_wait_max_s = max(
                        self._synchronization_wait_max_s,
                        wait_duration,
                    )
                    self._synchronization_max_backlog_blocks = max(
                        self._synchronization_max_backlog_blocks,
                        synchronization_wait.max_backlog_blocks,
                    )
                    if reason == "wait_timeout":
                        self._synchronization_wait_timeout_count += 1
                    self._emit(
                        "synchronization_degraded",
                        missing_source="reference",
                        reason=reason,
                        wait_duration_ms=1000.0 * max(0.0, elapsed),
                        expected_microphone_sequence=(
                            synchronization_wait.expected_microphone_sequence
                        ),
                        expected_reference_sequence=(
                            synchronization_wait.expected_reference_sequence
                        ),
                        microphone_head_sequence=microphone.sequence,
                        reference_head_sequence=(
                            None
                            if not reference_slots
                            else reference_slots[min(reference_slots)].sequence
                        ),
                        internal_reference_queue_depth=len(references),
                        internal_microphone_queue_depth=len(microphones),
                        mapped_reference_slot_depth=len(reference_slots),
                        maximum_backlog_blocks=(
                            synchronization_wait.max_backlog_blocks
                        ),
                        effective_wait_limit_s=synchronization_wait_limit_s,
                        reset_apm=False,
                    )
                    synchronization_wait = None
                break

            if not microphones and (references or reference_slots):
                if synchronization_wait is None:
                    synchronization_wait = self._begin_synchronization_wait(
                        missing_source="microphone",
                        microphones=microphones,
                        references=references,
                        reference_slots=reference_slots,
                    )
                backlog = len(microphones) + len(references) + len(reference_slots)
                synchronization_wait.max_backlog_blocks = max(
                    synchronization_wait.max_backlog_blocks,
                    backlog,
                )
                elapsed = time.monotonic() - synchronization_wait.started_monotonic
                if elapsed >= self.WAIT_EVENT_THRESHOLD_S:
                    self._report_synchronization_wait(synchronization_wait)
                reason = None
                if microphone_failed:
                    reason = "source_failure"
                elif elapsed >= synchronization_wait_limit_s:
                    reason = "wait_timeout"
                if reason is not None and self._aligner.mark_synchronization_degraded():
                    self._report_synchronization_wait(synchronization_wait)
                    self._degraded_reason = reason
                    wait_duration = max(0.0, elapsed)
                    self._synchronization_wait_total_s += wait_duration
                    self._synchronization_wait_max_s = max(
                        self._synchronization_wait_max_s,
                        wait_duration,
                    )
                    self._synchronization_max_backlog_blocks = max(
                        self._synchronization_max_backlog_blocks,
                        synchronization_wait.max_backlog_blocks,
                    )
                    if reason == "wait_timeout":
                        self._synchronization_wait_timeout_count += 1
                    self._emit(
                        "synchronization_degraded",
                        missing_source="microphone",
                        reason=reason,
                        wait_duration_ms=1000.0 * max(0.0, elapsed),
                        expected_microphone_sequence=(
                            synchronization_wait.expected_microphone_sequence
                        ),
                        expected_reference_sequence=(
                            synchronization_wait.expected_reference_sequence
                        ),
                        microphone_head_sequence=None,
                        reference_head_sequence=(
                            None
                            if not reference_slots
                            else reference_slots[min(reference_slots)].sequence
                        ),
                        internal_reference_queue_depth=len(references),
                        internal_microphone_queue_depth=0,
                        mapped_reference_slot_depth=len(reference_slots),
                        maximum_backlog_blocks=(
                            synchronization_wait.max_backlog_blocks
                        ),
                        effective_wait_limit_s=synchronization_wait_limit_s,
                        reset_apm=False,
                    )
                    synchronization_wait = None

            if self._aligner.mode is AlignmentMode.DEGRADED:
                retired = self._retire_degraded_excess(
                    references=references,
                    microphones=microphones,
                    reference_slots=reference_slots,
                    reference_limit=max(
                        0,
                        buffer_limit
                        - self._reference_queue.qsize()
                        - self._aligner.pending_reference_count,
                    ),
                    microphone_limit=max(
                        0,
                        buffer_limit - self._microphone_queue.qsize(),
                    ),
                )
                if retired:
                    if (
                        not microphones
                        or observed_microphone_sequence != microphones[0].sequence
                    ):
                        observed_microphone_sequence = None
                    made_progress = True

            if not stopping:
                reference_total_blocks = (
                    len(references)
                    + len(reference_slots)
                    + self._aligner.pending_reference_count
                    + self._reference_queue.qsize()
                )
                microphone_total_blocks = len(microphones) + self._microphone_queue.qsize()
                if max(reference_total_blocks, microphone_total_blocks) > fatal_capacity_blocks:
                    error = AudioBackendError(
                        "capture processing backlog exceeded the configured fatal limit: "
                        f"reference={reference_total_blocks * self.config.block_duration_s:.3f}s "
                        f"microphone={microphone_total_blocks * self.config.block_duration_s:.3f}s"
                    )
                    with self._state_lock:
                        if self._processing_error is None:
                            self._processing_error = error
                    self._emit("capture_failed", phase="processing", error=str(error))
                    fatal_backlog = True
                    continue

            backlog = len(microphones) + len(references) + len(reference_slots)
            if catchup_started is not None:
                catchup_max_backlog = max(catchup_max_backlog, backlog)
                if synchronization_wait is None and backlog == 0:
                    drain_duration = max(0.0, time.monotonic() - catchup_started)
                    self._synchronization_catchup_total_s += drain_duration
                    self._synchronization_catchup_max_s = max(
                        self._synchronization_catchup_max_s,
                        drain_duration,
                    )
                    self._synchronization_max_backlog_blocks = max(
                        self._synchronization_max_backlog_blocks,
                        catchup_max_backlog,
                    )
                    self._emit(
                        "synchronization_catchup_drained",
                        drain_duration_ms=1000.0 * drain_duration,
                        maximum_backlog_blocks=catchup_max_backlog,
                    )
                    catchup_started = None
                    catchup_max_backlog = 0

            if stopping and self._reference_queue.empty() and self._microphone_queue.empty():
                pending = self._aligner.drain_pending_references()
                self._aligner.note_shutdown_unpaired(
                    "reference", len(references) + len(reference_slots) + len(pending)
                )
                self._aligner.note_shutdown_unpaired("microphone", len(microphones))
                with self._state_lock:
                    self._internal_reference_pending_blocks = 0
                    self._internal_microphone_pending_blocks = 0
                return
            self._set_internal_pending_counts(references, microphones, reference_slots)
            if not made_progress:
                self._processing_stop.wait(poll_s)

    def _requeue_invalidated_references(
        self,
        update: AlignmentUpdate,
        *,
        slot: int,
        references: deque[AudioBlock],
        reference_slots: dict[int, AudioBlock],
    ) -> None:
        """Retire every reference already mapped under the invalidated epoch."""

        del slot, references
        retired = len(update.unmapped) + len(reference_slots)
        reference_slots.clear()
        self._aligner.note_hard_discontinuity_unpaired("reference", retired)

    def _discard_invalidated_references(
        self,
        update: AlignmentUpdate,
        *,
        slot: int,
        reference_slots: dict[int, AudioBlock],
    ) -> None:
        """Close a reference-source epoch without replaying pre-boundary data."""

        self._aligner.note_hard_discontinuity_unpaired(
            "reference",
            len(update.unmapped),
        )
        del slot
        invalidated = len(reference_slots)
        reference_slots.clear()
        self._aligner.note_hard_discontinuity_unpaired(
            "reference",
            invalidated,
        )

    def _apply_alignment_update(
        self,
        update: AlignmentUpdate,
        reference_slots: dict[int, AudioBlock],
        *,
        next_master_slot: int | None,
        suppress_lock_event: bool = False,
    ) -> None:
        for mapped in update.mapped:
            if next_master_slot is not None and mapped.slot < next_master_slot:
                if (
                    self._aligner.mode is AlignmentMode.DEGRADED
                    and self._artifacts is not None
                ):
                    self._note_degraded_retirement(
                        "reference",
                        1,
                        last_sequence=mapped.block.sequence,
                    )
                elif self._aligner.snapshot.pair_count == 0:
                    self._aligner.note_startup_unpaired_reference(1)
                else:
                    self._aligner.note_late_reference()
                continue
            if mapped.slot in reference_slots:
                self._aligner.note_hard_discontinuity_unpaired("reference", 1)
                continue
            reference_slots[mapped.slot] = mapped.block
        if update.locks_alignment and not suppress_lock_event:
            snapshot = self._aligner.snapshot
            self._emit(
                "alignment_locked",
                mode=snapshot.mode,
                first_callback_skew_ms=(
                    None
                    if snapshot.first_callback_skew_s is None
                    else 1000.0 * snapshot.first_callback_skew_s
                ),
            )
        if update.correction_slots:
            self._emit(
                "alignment_clock_corrected",
                correction_slots=update.correction_slots,
                total_corrections=self._aligner.snapshot.clock_correction_count,
                reset_apm=False,
            )

    def _handle_slot_discontinuity(
        self,
        *,
        slot: int,
        microphone: AudioBlock,
        reference: AudioBlock | None,
        slot_hard_discontinuity: bool,
    ) -> None:
        if not (
            slot_hard_discontinuity
            or microphone.discontinuity
            or (reference is not None and reference.discontinuity)
        ):
            return
        reset_required = self._aligner.begin_hard_discontinuity()
        if not reset_required:
            return
        if self._processor is None:  # pragma: no cover - guarded by start
            raise CaptureStateError("AEC processor is not initialized")
        self._processor.reset_alignment()
        self._emit(
            "alignment_realigning",
            epoch=self._aligner.snapshot.epoch,
            master_slot=slot,
            microphone_discontinuity=(
                microphone.discontinuity
            ),
            reference_discontinuity=(
                reference is not None and reference.discontinuity
            ),
        )
    def _process_master_slot(
        self,
        *,
        reference: AudioBlock,
        microphone: AudioBlock,
    ) -> None:
        """Advance the processed timeline exactly once for one confirmed pair."""

        if self._processor is None:  # pragma: no cover - guarded by start
            raise CaptureStateError("AEC processor is not initialized")
        worker_started = time.perf_counter()
        reference_samples = reference.samples
        microphone_samples = microphone.samples
        ended = microphone.ended_monotonic
        pair_skew = self._aligner.event_end(reference) - self._aligner.event_end(microphone)
        recovered = self._aligner.note_pair(reference, microphone)
        if recovered:
            self._emit(
                "alignment_recovered",
                epoch=self._aligner.snapshot.epoch,
            )
        self.on_reference(reference_samples, ended)
        started = time.perf_counter()
        clean = self._processor.process_pair(reference_samples, microphone_samples)
        self._processed_slot_count += 1
        elapsed = time.perf_counter() - started
        self._processing_total_s += elapsed
        self._processing_max_s = max(self._processing_max_s, elapsed)
        if self._artifacts is not None:
            self._note_artifact_timeline(microphone)
            self._artifacts.write_pair(reference_samples, microphone_samples, clean)
        processor_state = self._processor.state
        state = AecState(
            echo_path_ready=(
                processor_state.echo_path_ready
                and self._aligner.alignment_ready
            ),
            far_end_active_s=processor_state.far_end_active_s,
            alignment_epoch=processor_state.alignment_epoch,
            stream_alignment_reset_count=processor_state.stream_alignment_reset_count,
            echo_path_quality_ready=processor_state.echo_path_quality_ready,
            echo_suppression_db=processor_state.echo_suppression_db,
            echo_quality_s=processor_state.echo_quality_s,
            echo_path_reset_count=getattr(processor_state, "echo_path_reset_count", 0),
        )
        self.on_frame(
            AecFrame(
                reference=reference_samples,
                microphone_raw=microphone_samples,
                microphone_clean=clean,
                reference_ended_monotonic=ended,
                microphone_ended_monotonic=ended,
                pair_skew_s=pair_skew,
                state=state,
                reference_present=True,
                microphone_present=True,
                reference_observed_end_monotonic=(
                    reference.observed_end_monotonic if reference.timing_valid else None
                ),
                microphone_observed_end_monotonic=(
                    microphone.observed_end_monotonic if microphone.timing_valid else None
                ),
            )
        )
        worker_elapsed = time.perf_counter() - worker_started
        self._worker_slot_total_s += worker_elapsed
        self._worker_slot_max_s = max(self._worker_slot_max_s, worker_elapsed)
        self._startup_ready.set()

    def _note_artifact_timeline(self, block: AudioBlock) -> None:
        if self._timeline_started_monotonic is None:
            self._timeline_started_monotonic = (
                block.ended_monotonic - len(block.samples) / self.config.sample_rate
            )
