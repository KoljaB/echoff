from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import wave
from collections import deque
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from echoff import AecCapture, AecConfig, AudioBackendError, CaptureStateError
from echoff.alignment import AlignmentUpdate
from echoff.models import AecState, AudioBlock


class FakeProcessor:
    def __init__(self) -> None:
        self.reset_count = 0
        self.echo_path_reset_count = 0
        self.pairs: list[tuple[tuple[float, ...], tuple[float, ...]]] = []

    def process_pair(self, reference, microphone):
        reference_tuple = tuple(reference)
        microphone_tuple = tuple(microphone)
        self.pairs.append((reference_tuple, microphone_tuple))
        return tuple(value * 0.5 for value in microphone_tuple)

    def reset_echo_path(self) -> None:
        self.echo_path_reset_count += 1

    def reset_alignment(self) -> None:
        self.reset_count += 1
        self.echo_path_reset_count += 1

    @property
    def state(self) -> AecState:
        return AecState(
            echo_path_ready=True,
            far_end_active_s=4.0,
            alignment_epoch=self.reset_count,
            stream_alignment_reset_count=self.reset_count,
            echo_path_reset_count=self.echo_path_reset_count,
        )


class LegacyProcessor:
    """A v0.2-style custom processor with no echo-path reset capability."""

    def __init__(self) -> None:
        self.pairs = []
        self.reset_count = 0

    def process_pair(self, reference, microphone):
        self.pairs.append((tuple(reference), tuple(microphone)))
        return tuple(microphone)

    def reset_alignment(self) -> None:
        self.reset_count += 1

    @property
    def state(self):
        class LegacyState:
            echo_path_ready = False
            far_end_active_s = 0.0
            alignment_epoch = 0
            stream_alignment_reset_count = 0
            echo_path_quality_ready = False
            echo_suppression_db = None
            echo_quality_s = 0.0

        return LegacyState()


class FakeSource:
    def __init__(
        self,
        backend: str,
        callback,
        rows,
        *,
        start_error: Exception | None = None,
        activate_error: Exception | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self.backend_name = backend
        self.callback = callback
        self.rows = rows
        self.error = None
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
        self.selected_device_name = backend
        self.selected_device_index = 1
        self.stopped = False
        self.start_error = start_error
        self.activate_error = activate_error
        self.stop_error = stop_error

    def start(self) -> None:
        if self.start_error is not None:
            self.error = self.start_error
            raise self.start_error

    def activate(self) -> None:
        if self.activate_error is not None:
            self.error = self.activate_error
            raise self.activate_error
        for sequence, (value, ended) in enumerate(self.rows):
            self.callback(
                AudioBlock(
                    (value,) * 960,
                    ended,
                    sequence=sequence,
                    callback_monotonic=time.monotonic(),
                    observed_end_monotonic=ended,
                )
            )
            self.device_block_count += 1

    def stop(self) -> None:
        self.stopped = True
        if self.stop_error is not None:
            raise self.stop_error


class ShutdownOverflowSource(FakeSource):
    def __init__(self, backend: str, callback, rows) -> None:
        super().__init__(backend, callback, rows)
        self.callback_queue_overflow_count = 0

    def stop(self) -> None:
        self.callback_queue_overflow_count = 1
        super().stop()


def factory_for(reference_rows, microphone_rows):
    sources = []

    def factory(_config, reference_callback, microphone_callback, _reference_device, _mic_device):
        reference = FakeSource("fake-reference", reference_callback, reference_rows)
        microphone = FakeSource("fake-microphone", microphone_callback, microphone_rows)
        sources.extend([reference, microphone])
        return reference, microphone

    return factory, sources


class AecCaptureTests(unittest.TestCase):
    def test_capture_ready_waits_for_active_backend_metadata(self) -> None:
        release_active = threading.Event()
        callback_sent = threading.Event()
        events = []

        class BarrierMicrophone(FakeSource):
            def __init__(self, callback) -> None:
                super().__init__("uninitialized", callback, [(0.1, 0.02)])
                self.selected_device_name = None
                self.selected_device_index = None
                self.active_event = threading.Event()
                self.publisher: threading.Thread | None = None

            def activate(self) -> None:
                super().activate()
                callback_sent.set()

                def publish() -> None:
                    release_active.wait()
                    self.backend_name = "active-microphone"
                    self.selected_device_name = "Active microphone"
                    self.selected_device_index = 7
                    self.active_event.set()

                self.publisher = threading.Thread(target=publish, daemon=True)
                self.publisher.start()

            def stop(self) -> None:
                release_active.set()
                if self.publisher is not None:
                    self.publisher.join(timeout=1.0)
                super().stop()

        def factory(
            _config,
            reference_callback,
            microphone_callback,
            _reference_device,
            _microphone_device,
        ):
            reference = FakeSource(
                "active-reference",
                reference_callback,
                [(1.0, 0.02)],
            )
            microphone = BarrierMicrophone(microphone_callback)
            return reference, microphone

        capture = AecCapture(
            AecConfig(startup_timeout_s=1.0),
            processor=FakeProcessor(),
            source_factory=factory,
            on_event=events.append,
        )
        start_errors: list[Exception] = []

        def start_capture() -> None:
            try:
                capture.start()
            except Exception as exc:
                start_errors.append(exc)

        starter = threading.Thread(target=start_capture)
        starter.start()
        self.assertTrue(callback_sent.wait(1.0))
        self.assertNotIn("capture_ready", [event.kind for event in events])
        release_active.set()
        starter.join(timeout=1.0)

        self.assertFalse(starter.is_alive())
        self.assertEqual(start_errors, [])
        ready = [event for event in events if event.kind == "capture_ready"]
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].details["microphone_backend"], "active-microphone")
        self.assertEqual(
            ready[0].details["microphone_device_name"],
            "Active microphone",
        )
        capture.stop()

    def test_legacy_processor_remains_usable_without_echo_path_reset(self) -> None:
        rows = [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)]
        processor = LegacyProcessor()
        capture = AecCapture(
            processor=processor,
            source_factory=factory_for(rows, rows)[0],
        )

        capture.start()
        deadline = time.monotonic() + 0.5
        while len(processor.pairs) < 3 and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(len(processor.pairs), 3)
        self.assertEqual(capture.status().echo_path_reset_count, 0)
        with self.assertRaisesRegex(CaptureStateError, "does not support"):
            capture.reset_echo_path()
        capture.stop()

    def test_echo_path_reset_preserves_capture_alignment(self) -> None:
        processor = FakeProcessor()
        events = []
        capture = AecCapture(
            processor=processor,
            source_factory=factory_for([], [])[0],
            on_event=events.append,
        )
        with self.assertRaisesRegex(CaptureStateError, "not running"):
            capture.reset_echo_path()

        before = capture._aligner.snapshot
        capture._running = True
        try:
            capture.reset_echo_path()
        finally:
            capture._running = False
        after = capture._aligner.snapshot

        self.assertEqual(processor.reset_count, 0)
        self.assertEqual(processor.echo_path_reset_count, 1)
        self.assertEqual(after.epoch, before.epoch)
        self.assertEqual(after.pair_count, before.pair_count)
        self.assertEqual(capture.status().echo_path_reset_count, 1)
        self.assertEqual(events[-1].kind, "echo_path_reset")
        self.assertEqual(events[-1].details["alignment_epoch"], 0)

    def test_console_diagnostics_are_visible_by_default_and_can_be_disabled(self) -> None:
        output = StringIO()
        capture = AecCapture(
            processor=FakeProcessor(),
            source_factory=factory_for([], [])[0],
        )
        with patch.object(output, "isatty", return_value=True), redirect_stderr(output):
            capture._emit("synchronization_degraded", missing_source="reference")
        rendered = output.getvalue()
        self.assertIn("\x1b[91m", rendered)
        self.assertIn("[echoff ERROR]", rendered)
        self.assertIn("live AEC suspended", rendered)

        output = StringIO()
        with patch.object(output, "isatty", return_value=True), redirect_stderr(output):
            capture._emit("synchronization_recovered", microphone_sequence=12)
        rendered = output.getvalue()
        self.assertNotIn("\x1b[91m", rendered)
        self.assertIn("[echoff INFO] live AEC synchronization recovered", rendered)

        output = StringIO()
        capture = AecCapture(
            console_diagnostics=False,
            processor=FakeProcessor(),
            source_factory=factory_for([], [])[0],
        )
        with redirect_stderr(output):
            capture._emit("synchronization_degraded", missing_source="reference")
        self.assertEqual(output.getvalue(), "")

    def test_status_exposes_raw_callback_queue_and_clock_telemetry(self) -> None:
        rows = [(0.1, 1.00), (0.2, 1.02), (0.3, 1.04)]
        factory, sources = factory_for(rows, rows)
        capture = AecCapture(
            processor=FakeProcessor(),
            source_factory=factory,
        ).start()
        try:
            reference, microphone = sources
            reference.callback_packet_count = 11
            reference.callback_payload_frame_count = 10_560
            reference.callback_queue_high_watermark = 7
            reference.callback_queue_age_max_s = 0.125
            reference.callback_enqueue_max_s = 0.0004
            reference.callback_timeline_drift_s = 0.230
            reference.callback_timeline_drift_max_s = 0.250
            microphone.callback_packet_count = 13
            microphone.callback_payload_frame_count = 12_480
            microphone.callback_queue_high_watermark = 5
            microphone.callback_queue_age_max_s = 0.075
            microphone.callback_enqueue_max_s = 0.0003
            microphone.callback_timeline_drift_s = -0.120
            microphone.callback_timeline_drift_max_s = 0.140

            status = capture.status()
            self.assertEqual(status.reference_callback_packet_count, 11)
            self.assertEqual(status.reference_callback_payload_frames, 10_560)
            self.assertEqual(status.reference_callback_queue_high_watermark_blocks, 7)
            self.assertAlmostEqual(status.reference_callback_queue_age_max_ms, 125.0)
            self.assertAlmostEqual(status.reference_callback_enqueue_max_ms, 0.4)
            self.assertAlmostEqual(status.reference_callback_timeline_drift_ms, 230.0)
            self.assertAlmostEqual(status.reference_callback_timeline_drift_max_ms, 250.0)
            self.assertEqual(status.microphone_callback_packet_count, 13)
            self.assertEqual(status.microphone_callback_payload_frames, 12_480)
            self.assertEqual(status.microphone_callback_queue_high_watermark_blocks, 5)
            self.assertAlmostEqual(status.microphone_callback_queue_age_max_ms, 75.0)
            self.assertAlmostEqual(status.microphone_callback_enqueue_max_ms, 0.3)
            self.assertAlmostEqual(status.microphone_callback_timeline_drift_ms, -120.0)
            self.assertAlmostEqual(status.microphone_callback_timeline_drift_max_ms, 140.0)
        finally:
            capture.stop()

    def test_reference_delay_recovers_losslessly_with_confirmed_offset(self) -> None:
        processor = FakeProcessor()
        events = []
        capture = AecCapture(
            AecConfig(reference_stall_grace_s=1.0),
            processor=processor,
            source_factory=factory_for([], [])[0],
            on_event=events.append,
        )
        capture._processor = processor

        def put_reference(sequence: int, slot: int, *, residual_s: float = 0.0) -> None:
            capture._enqueue_reference(
                AudioBlock(
                    (10.0 + sequence,) * 960,
                    1.0 + slot * 0.020,
                    sequence=sequence,
                    callback_monotonic=time.monotonic(),
                    observed_end_monotonic=1.0 + slot * 0.020 + residual_s,
                )
            )

        def put_microphone(slot: int) -> None:
            capture._enqueue_microphone(
                AudioBlock(
                    (20.0 + slot,) * 960,
                    1.0 + slot * 0.020,
                    sequence=slot,
                    callback_monotonic=time.monotonic(),
                    observed_end_monotonic=1.0 + slot * 0.020,
                )
            )

        def wait_for_pairs(count: int, timeout_s: float = 0.5) -> None:
            deadline = time.monotonic() + timeout_s
            while len(processor.pairs) < count and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertGreaterEqual(len(processor.pairs), count)

        for slot in range(3):
            put_reference(slot + 1, slot)
            put_microphone(slot)
        capture._start_processing()
        wait_for_pairs(3)

        put_microphone(3)
        time.sleep(0.5)
        self.assertEqual(len(processor.pairs), 3)
        put_reference(4, 3, residual_s=0.769)
        wait_for_pairs(4)

        immediate_started = time.monotonic()
        put_reference(5, 4)
        put_microphone(4)
        wait_for_pairs(5)
        self.assertLess(time.monotonic() - immediate_started, 0.050)

        capture._stop_processing()

        self.assertEqual(
            [pair[0][0] for pair in processor.pairs],
            [11.0, 12.0, 13.0, 14.0, 15.0],
        )
        self.assertEqual(
            [pair[1][0] for pair in processor.pairs],
            [20.0, 21.0, 22.0, 23.0, 24.0],
        )
        self.assertEqual(processor.reset_count, 0)
        self.assertTrue(capture.status().alignment_locked)
        self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
        self.assertEqual(capture.status().late_reference_blocks, 0)
        self.assertEqual(
            [event.kind for event in events].count("synchronization_wait_started"),
            1,
        )
        self.assertEqual(
            [event.kind for event in events].count("synchronization_wait_ended"),
            1,
        )

    def test_initial_join_ignores_rejected_portaudio_timestamps(self) -> None:
        processor = FakeProcessor()
        events = []
        capture = AecCapture(
            AecConfig(reference_stall_grace_s=0.060),
            processor=processor,
            source_factory=factory_for([], [])[0],
            on_event=events.append,
        )
        capture._processor = processor

        capture._start_processing()
        capture._enqueue_reference(
            AudioBlock(
                (99.0,) * 960,
                3.980,
                sequence=0,
                callback_monotonic=19.980,
                observed_end_monotonic=100.100,
                timing_valid=False,
            )
        )
        for slot in range(3):
            capture._enqueue_reference(
                AudioBlock(
                    (100.0 + slot,) * 960,
                    4.000 + slot * 0.020,
                    sequence=slot + 1,
                    callback_monotonic=20.0 + slot * 0.020,
                    observed_end_monotonic=100.0 - slot * 0.100,
                    timing_valid=False,
                )
            )
            capture._enqueue_microphone(
                AudioBlock(
                    (200.0 + slot,) * 960,
                    4.000 + slot * 0.020,
                    sequence=slot,
                    callback_monotonic=30.0 + slot * 0.020,
                    observed_end_monotonic=-100.0 + slot * 0.100,
                    timing_valid=False,
                )
            )
            # Reproduce hardware startup: the worker first observes only one
            # unmatched mic head, then look-ahead grows behind that same head.
            time.sleep(0.020)
        deadline = time.monotonic() + 0.5
        while len(processor.pairs) < 3 and time.monotonic() < deadline:
            time.sleep(0.002)

        capture._enqueue_microphone(
            AudioBlock(
                (203.0,) * 960,
                4.060,
                sequence=3,
                callback_monotonic=30.060,
                observed_end_monotonic=-99.700,
                timing_valid=False,
            )
        )
        time.sleep(0.020)
        capture._enqueue_reference(
            AudioBlock(
                (103.0,) * 960,
                4.060,
                sequence=4,
                callback_monotonic=20.060,
                observed_end_monotonic=99.700,
                timing_valid=False,
            )
        )
        deadline = time.monotonic() + 0.5
        while len(processor.pairs) < 4 and time.monotonic() < deadline:
            time.sleep(0.002)
        capture._stop_processing()

        self.assertEqual(len(processor.pairs), 4)
        self.assertEqual(
            [pair[0][0] for pair in processor.pairs],
            [100.0, 101.0, 102.0, 103.0],
        )
        self.assertEqual(processor.reset_count, 0)
        self.assertEqual(capture.status().hard_discontinuity_count, 0)
        self.assertEqual(capture.status().startup_unpaired_reference_blocks, 1)
        self.assertEqual(
            capture.status().hard_discontinuity_unpaired_reference_blocks,
            0,
        )
        self.assertEqual(capture.status().synchronization_wait_timeout_count, 0)
        self.assertNotIn("synchronization_degraded", [event.kind for event in events])
        self.assertNotIn("synchronization_wait_started", [event.kind for event in events])
        self.assertNotIn("synchronization_wait_ended", [event.kind for event in events])

    def test_three_block_microphone_preroll_is_retired_without_stale_reference(self) -> None:
        """A later reference must not deadlock or synthesize paired far-end audio."""

        processor = FakeProcessor()
        events = []
        frames = []
        config = AecConfig(
            block_duration_s=0.010,
            pair_tolerance_s=0.005,
            reference_stall_grace_s=0.060,
        )
        capture = AecCapture(
            config,
            processor=processor,
            source_factory=factory_for([], [])[0],
            on_event=events.append,
            on_frame=frames.append,
        )
        capture._processor = processor

        sample_count = config.block_samples
        microphone_phase = 4.000
        reference_phase = microphone_phase + 3 * config.block_duration_s
        capture._start_processing()
        try:
            # Reproduce the hardware startup order from the failing artifact:
            # the worker first blocks on microphone sequence 0, then both
            # healthy sources continue queuing while reference sequence 0 maps
            # to microphone slot 3.
            capture._enqueue_microphone(
                AudioBlock(
                    (200.0,) * sample_count,
                    microphone_phase,
                    sequence=0,
                    callback_monotonic=time.monotonic(),
                    observed_end_monotonic=microphone_phase,
                )
            )
            time.sleep(0.020)
            for sequence in range(1, 6):
                ended = microphone_phase + sequence * config.block_duration_s
                capture._enqueue_microphone(
                    AudioBlock(
                        (200.0 + sequence,) * sample_count,
                        ended,
                        sequence=sequence,
                        callback_monotonic=time.monotonic(),
                        observed_end_monotonic=ended,
                    )
                )
            for sequence in range(3):
                ended = reference_phase + sequence * config.block_duration_s
                capture._enqueue_reference(
                    AudioBlock(
                        (100.0 + sequence,) * sample_count,
                        ended,
                        sequence=sequence,
                        callback_monotonic=time.monotonic(),
                        observed_end_monotonic=ended,
                    )
                )

            deadline = time.monotonic() + 0.5
            while len(frames) < 6 and time.monotonic() < deadline:
                time.sleep(0.002)
        finally:
            capture._stop_processing()

        self.assertEqual(len(frames), 3)
        self.assertEqual(
            [frame.microphone_raw[0] for frame in frames],
            [203.0, 204.0, 205.0],
        )
        self.assertEqual(
            [frame.reference_present for frame in frames],
            [True, True, True],
        )
        self.assertEqual(
            [pair[0][0] for pair in processor.pairs],
            [100.0, 101.0, 102.0],
        )
        self.assertEqual(capture.status().processed_pair_count, 3)
        self.assertEqual(capture.status().matched_reference_blocks, 3)
        self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
        self.assertEqual(capture.status().startup_unpaired_microphone_blocks, 3)
        self.assertEqual(capture.status().synchronization_wait_timeout_count, 0)
        self.assertNotIn("synchronization_degraded", [event.kind for event in events])

    def test_microphone_delay_recovers_losslessly_without_reset(self) -> None:
        processor = FakeProcessor()
        capture = AecCapture(
            AecConfig(reference_stall_grace_s=1.0),
            processor=processor,
            source_factory=factory_for([], [])[0],
        )
        capture._processor = processor

        def reference(sequence: int, slot: int) -> AudioBlock:
            ended = 2.0 + slot * 0.020
            return AudioBlock(
                (100.0 + sequence,) * 960,
                ended,
                sequence=sequence,
                callback_monotonic=time.monotonic(),
                observed_end_monotonic=ended,
            )

        def microphone(slot: int) -> AudioBlock:
            ended = 2.0 + slot * 0.020
            return AudioBlock(
                (200.0 + slot,) * 960,
                ended,
                sequence=slot,
                callback_monotonic=time.monotonic(),
                observed_end_monotonic=ended,
            )

        for slot in range(3):
            capture._enqueue_reference(reference(slot + 1, slot))
            capture._enqueue_microphone(microphone(slot))
        capture._start_processing()
        deadline = time.monotonic() + 0.5
        while len(processor.pairs) < 3 and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(len(processor.pairs), 3)

        capture._enqueue_reference(reference(4, 3))
        time.sleep(0.5)
        self.assertEqual(len(processor.pairs), 3)
        capture._enqueue_microphone(microphone(3))
        deadline = time.monotonic() + 0.5
        while len(processor.pairs) < 4 and time.monotonic() < deadline:
            time.sleep(0.002)
        capture._stop_processing()

        self.assertEqual(len(processor.pairs), 4)
        self.assertEqual(processor.pairs[-1][0][0], 104.0)
        self.assertEqual(processor.pairs[-1][1][0], 203.0)
        self.assertEqual(processor.reset_count, 0)
        self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
        self.assertEqual(capture.status().late_reference_blocks, 0)

    def test_each_direction_recovers_at_two_point_nine_seconds(self) -> None:
        for delayed_source in ("reference", "microphone"):
            with self.subTest(delayed_source=delayed_source):
                processor = FakeProcessor()
                capture = AecCapture(
                    processor=processor,
                    source_factory=factory_for([], [])[0],
                )
                capture._processor = processor

                def put_reference(
                    sequence: int,
                    slot: int,
                    active_capture: AecCapture = capture,
                ) -> None:
                    ended = 3.0 + slot * 0.020
                    active_capture._enqueue_reference(
                        AudioBlock(
                            (300.0 + sequence,) * 960,
                            ended,
                            sequence=sequence,
                            callback_monotonic=time.monotonic(),
                            observed_end_monotonic=ended,
                        )
                    )

                def put_microphone(
                    slot: int,
                    active_capture: AecCapture = capture,
                ) -> None:
                    ended = 3.0 + slot * 0.020
                    active_capture._enqueue_microphone(
                        AudioBlock(
                            (400.0 + slot,) * 960,
                            ended,
                            sequence=slot,
                            callback_monotonic=time.monotonic(),
                            observed_end_monotonic=ended,
                        )
                    )

                for slot in range(3):
                    put_reference(slot + 1, slot)
                    put_microphone(slot)
                capture._start_processing()
                deadline = time.monotonic() + 0.5
                while len(processor.pairs) < 3 and time.monotonic() < deadline:
                    time.sleep(0.002)
                self.assertEqual(len(processor.pairs), 3)

                if delayed_source == "reference":
                    put_microphone(3)
                else:
                    put_reference(4, 3)
                time.sleep(2.9)
                self.assertEqual(len(processor.pairs), 3)
                if delayed_source == "reference":
                    put_reference(4, 3)
                else:
                    put_microphone(3)
                deadline = time.monotonic() + 0.5
                while len(processor.pairs) < 4 and time.monotonic() < deadline:
                    time.sleep(0.002)
                self.assertEqual(len(processor.pairs), 4)

                immediate_started = time.monotonic()
                put_reference(5, 4)
                put_microphone(4)
                deadline = time.monotonic() + 0.5
                while len(processor.pairs) < 5 and time.monotonic() < deadline:
                    time.sleep(0.002)
                capture._stop_processing()

                self.assertEqual(len(processor.pairs), 5)
                self.assertLess(time.monotonic() - immediate_started, 0.050)
                self.assertEqual(processor.reset_count, 0)
                self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
                self.assertEqual(capture.status().late_reference_blocks, 0)

    def test_future_mic_discontinuity_resets_only_at_its_master_slot(self) -> None:
        processor = FakeProcessor()
        order: list[tuple[str, float]] = []
        capture = AecCapture(
            processor=processor,
            source_factory=factory_for([], [])[0],
            on_frame=lambda frame: order.append(("frame", frame.microphone_raw[0])),
            on_event=lambda event: order.append((event.kind, -1.0)),
        )
        capture._processor = processor
        for sequence in range(8):
            ended = 1.0 + sequence * 0.020
            capture._reference_queue.put(
                AudioBlock(
                    (10.0 + sequence,) * 960,
                    ended,
                    sequence=sequence,
                    callback_monotonic=ended,
                    observed_end_monotonic=ended,
                    discontinuity=sequence == 5,
                )
            )
            capture._microphone_queue.put(
                AudioBlock(
                    (20.0 + sequence,) * 960,
                    ended,
                    sequence=sequence,
                    callback_monotonic=ended,
                    observed_end_monotonic=ended,
                    discontinuity=sequence == 5,
                )
            )
        capture._processing_stop.set()

        capture._run_processing()

        self.assertEqual(
            [pair[0][0] for pair in processor.pairs],
            [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0],
        )
        self.assertEqual(
            [pair[1][0] for pair in processor.pairs],
            [20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0],
        )
        self.assertEqual(processor.reset_count, 1)
        reset_index = order.index(("alignment_realigning", -1.0))
        self.assertLess(order.index(("frame", 24.0)), reset_index)
        self.assertLess(reset_index, order.index(("frame", 25.0)))
        self.assertEqual(
            [kind for kind, _value in order].count("alignment_discontinuity_pending"),
            1,
        )

    def test_retired_startup_discontinuity_resets_before_first_real_pair(self) -> None:
        processor = FakeProcessor()
        order: list[tuple[str, float]] = []
        capture = AecCapture(
            AecConfig(
                block_duration_s=0.010,
                pair_tolerance_s=0.005,
                reference_stall_grace_s=0.060,
            ),
            processor=processor,
            source_factory=factory_for([], [])[0],
            on_frame=lambda frame: order.append(("frame", frame.microphone_raw[0])),
            on_event=lambda event: order.append((event.kind, -1.0)),
        )
        capture._processor = processor
        for sequence in range(6):
            ended = 4.000 + sequence * 0.010
            capture._microphone_queue.put(
                AudioBlock(
                    (200.0 + sequence,) * 480,
                    ended,
                    sequence=sequence,
                    callback_monotonic=ended,
                    observed_end_monotonic=ended,
                    discontinuity=sequence == 0,
                )
            )
        for sequence in range(3):
            ended = 4.030 + sequence * 0.010
            capture._reference_queue.put(
                AudioBlock(
                    (100.0 + sequence,) * 480,
                    ended,
                    sequence=sequence,
                    callback_monotonic=ended,
                    observed_end_monotonic=ended,
                )
            )
        capture._processing_stop.set()

        capture._run_processing()

        self.assertEqual(len(processor.pairs), 3)
        self.assertEqual(processor.reset_count, 1)
        self.assertEqual(capture.status().startup_unpaired_microphone_blocks, 3)
        self.assertEqual(
            [kind for kind, _value in order].count("alignment_discontinuity_pending"),
            1,
        )
        reset_index = order.index(("alignment_realigning", -1.0))
        self.assertLess(reset_index, order.index(("frame", 203.0)))

    def test_discontinuity_without_a_new_pair_is_reported_but_not_reset(self) -> None:
        processor = FakeProcessor()
        events = []
        capture = AecCapture(
            processor=processor,
            source_factory=factory_for([], [])[0],
            on_event=events.append,
        )
        capture._processor = processor
        capture._microphone_queue.put(
            AudioBlock(
                (0.1,) * 960,
                1.0,
                sequence=0,
                callback_monotonic=1.0,
                observed_end_monotonic=1.0,
                discontinuity=True,
            )
        )
        capture._processing_stop.set()

        capture._run_processing()

        self.assertEqual(processor.reset_count, 0)
        self.assertEqual(
            [event.kind for event in events].count("alignment_discontinuity_pending"),
            1,
        )

    def test_microphone_epoch_discards_all_previously_mapped_references(self) -> None:
        capture = AecCapture(
            processor=FakeProcessor(),
            source_factory=factory_for([], [])[0],
        )
        reference_slots = {
            slot: AudioBlock((float(slot),) * 960, float(slot), sequence=slot)
            for slot in range(3, 10)
        }
        update = AlignmentUpdate(
            unmapped=(AudioBlock((1.0,) * 960, 1.0, sequence=1),),
            hard_discontinuity=True,
        )

        capture._requeue_invalidated_references(
            update,
            slot=7,
            references=deque(),
            reference_slots=reference_slots,
        )

        self.assertEqual(reference_slots, {})
        self.assertEqual(
            capture.status().hard_discontinuity_unpaired_reference_blocks,
            8,
        )

    def test_context_manager_writes_equal_timeline_tracks_and_summary(self) -> None:
        factory, sources = factory_for(
            [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)],
            [(0.2, 0.02), (0.4, 0.04), (0.6, 0.06)],
        )
        processor = FakeProcessor()
        frames = []
        references = []
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            with AecCapture(
                AecConfig(),
                on_frame=frames.append,
                on_reference=lambda samples, ended: references.append((samples, ended)),
                on_event=events.append,
                output_dir=output,
                processor=processor,
                source_factory=factory,
            ) as capture:
                capture.raise_if_failed()

            self.assertEqual(len(frames), 3)
            self.assertEqual([ended for _samples, ended in references], [0.02, 0.04, 0.06])
            self.assertEqual(len(processor.pairs), 3)
            self.assertTrue(all(source.stopped for source in sources))
            frame_counts = set()
            for name in ("computer_audio", "microphone_raw", "microphone_aec"):
                with wave.open(str(output / f"{name}.wav"), "rb") as source:
                    self.assertEqual(source.getframerate(), 48_000)
                    self.assertEqual(source.getnchannels(), 1)
                    frame_counts.add(source.getnframes())
            self.assertEqual(frame_counts, {2_880})
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "completed")
            self.assertTrue(summary["tracks_share_timeline"])
            self.assertAlmostEqual(summary["timeline_started_monotonic"], 0.0)
            self.assertEqual(capture.timeline_started_monotonic, 0.0)
            self.assertEqual(summary["capture"]["processed_pair_count"], 3)
            self.assertEqual(capture.processed_sample_count, 2_880)
            kinds = [event.kind for event in events]
            self.assertIn("alignment_locked", kinds)
            self.assertIn("capture_ready", kinds)
            self.assertIn("capture_stopped", kinds)

    def test_shutdown_preserves_raw_sources_without_primary_track_inflation(self) -> None:
        factory, _sources = factory_for(
            [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)],
            [(0.1, 0.02), (0.2, 0.04), (0.3, 0.06), (0.4, 0.08), (0.5, 0.10)],
        )
        processor = FakeProcessor()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            capture = AecCapture(
                output_dir=output,
                processor=processor,
                source_factory=factory,
            )
            capture.start()
            capture.stop()

            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(processor.reset_count, 0)
            self.assertEqual(summary["capture"]["hard_discontinuity_count"], 0)
            self.assertEqual(summary["capture"]["zero_filled_reference_blocks"], 0)
            self.assertEqual(summary["capture"]["processed_pair_count"], 3)
            self.assertEqual(summary["capture"]["shutdown_unpaired_reference_blocks"], 0)
            self.assertEqual(summary["capture"]["shutdown_unpaired_microphone_blocks"], 2)
            counts = {track["frames"] for track in summary["tracks"].values()}
            self.assertEqual(counts, {2_880})
            self.assertEqual(
                summary["source_tracks"]["reference_received"]["frames"],
                2_880,
            )
            self.assertEqual(
                summary["source_tracks"]["microphone_received"]["frames"],
                4_800,
            )
            self.assertEqual([pair[0][0] for pair in processor.pairs], [1, 2, 3])
            self.assertEqual([pair[1][0] for pair in processor.pairs], [0.1, 0.2, 0.3])

    def test_missing_reference_suspends_primary_aec_without_false_lock(self) -> None:
        microphone_rows = [(0.1, 0.02), (0.2, 0.04), (0.3, 0.06)]
        factory, _sources = factory_for([], microphone_rows)
        processor = FakeProcessor()
        events = []
        capture = AecCapture(
            AecConfig(startup_timeout_s=0.5),
            processor=processor,
            source_factory=factory,
            on_event=events.append,
        )

        capture.start()
        capture.stop()

        self.assertEqual(len(processor.pairs), 0)
        self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
        self.assertEqual(capture.status().shutdown_unpaired_microphone_blocks, 3)
        self.assertFalse(capture.status().alignment_locked)
        self.assertIn("capture_ready", [event.kind for event in events])
        stopped = [event for event in events if event.kind == "capture_stopped"]
        self.assertEqual(stopped[-1].details["status"], "incomplete")

    def test_late_starting_reference_retires_early_microphones_without_stale_reference(
        self,
    ) -> None:
        factory, _sources = factory_for(
            [(1.0, 0.08), (2.0, 0.10), (3.0, 0.12)],
            [
                (0.1, 0.02),
                (0.2, 0.04),
                (0.3, 0.06),
                (0.4, 0.08),
                (0.5, 0.10),
                (0.6, 0.12),
            ],
        )
        processor = FakeProcessor()
        frames = []
        references = []
        capture = AecCapture(
            AecConfig(startup_timeout_s=0.5),
            processor=processor,
            source_factory=factory,
            on_frame=frames.append,
            on_reference=lambda samples, _ended: references.append(samples[0]),
        )

        capture.start()
        capture.stop()

        self.assertTrue(capture.status().alignment_locked)
        self.assertEqual(
            [pair[0][0] for pair in processor.pairs],
            [1.0, 2.0, 3.0],
        )
        self.assertEqual(
            [pair[1][0] for pair in processor.pairs],
            [0.4, 0.5, 0.6],
        )
        self.assertEqual(
            [frame.reference_present for frame in frames],
            [True, True, True],
        )
        self.assertEqual(references, [1.0, 2.0, 3.0])
        self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
        self.assertEqual(capture.status().startup_unpaired_microphone_blocks, 3)
        self.assertEqual(capture.status().shutdown_unpaired_microphone_blocks, 0)

    def test_reference_callback_is_aligned_while_raw_reference_is_preserved(self) -> None:
        factory, _sources = factory_for(
            [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)],
            [(0.1, 0.02), (0.2, 0.04), (0.3, 0.06)],
        )
        references = []
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            capture = AecCapture(
                processor=FakeProcessor(),
                source_factory=factory,
                output_dir=output,
                on_reference=lambda samples, ended: references.append((samples[0], ended)),
            )
            capture.start()
            capture.stop()
            self.assertEqual(references, [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)])
            with wave.open(str(output / "reference_received.wav"), "rb") as source:
                self.assertEqual(source.getnframes(), 2_880)

    def test_frame_preserves_canonical_and_observed_microphone_times(self) -> None:
        capture = AecCapture(
            processor=FakeProcessor(),
            source_factory=factory_for([], [])[0],
        )
        capture._processor = FakeProcessor()
        frames = []
        references = []
        capture.on_frame = frames.append
        capture.on_reference = lambda samples, ended: references.append(ended)
        reference = AudioBlock(
            (1.0,) * 960,
            10.020,
            sequence=0,
            callback_monotonic=10.030,
            observed_end_monotonic=10.025,
            timing_valid=True,
        )
        microphone = AudioBlock(
            (0.1,) * 960,
            10.020,
            sequence=0,
            callback_monotonic=10.032,
            observed_end_monotonic=10.027,
            timing_valid=True,
        )

        capture._process_master_slot(reference=reference, microphone=microphone)

        self.assertEqual(references, [10.020])
        self.assertEqual(frames[0].microphone_ended_monotonic, 10.020)
        self.assertEqual(frames[0].reference_ended_monotonic, 10.020)
        self.assertEqual(frames[0].microphone_observed_end_monotonic, 10.027)
        self.assertEqual(frames[0].reference_observed_end_monotonic, 10.025)

    def test_reference_discontinuity_during_joining_never_loops(self) -> None:
        capture = AecCapture(
            processor=FakeProcessor(),
            source_factory=factory_for([], [])[0],
        )
        capture._processor = FakeProcessor()
        for sequence in range(3):
            ended = 1.0 + sequence * 0.020
            capture._reference_queue.put(
                AudioBlock(
                    (10.0 + sequence,) * 960,
                    ended,
                    sequence=sequence,
                    callback_monotonic=ended,
                    observed_end_monotonic=ended,
                    discontinuity=sequence == 2,
                )
            )
            capture._microphone_queue.put(
                AudioBlock(
                    (20.0 + sequence,) * 960,
                    ended,
                    sequence=sequence,
                    callback_monotonic=ended,
                    observed_end_monotonic=ended,
                )
            )
        capture._processing_stop.set()
        worker = threading.Thread(target=capture._run_processing)
        worker.start()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(capture._processor.pairs), 0)
        self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
        self.assertEqual(capture.status().shutdown_unpaired_microphone_blocks, 3)

    def test_worker_backlog_with_old_callback_times_pairs_immediately(self) -> None:
        processor = FakeProcessor()
        capture = AecCapture(
            processor=processor,
            source_factory=factory_for([], [])[0],
        )
        capture._processor = processor
        old_callback = time.monotonic() - 10.0
        for slot in range(3):
            ended = 4.0 + slot * 0.020
            capture._enqueue_reference(
                AudioBlock(
                    (500.0 + slot,) * 960,
                    ended,
                    sequence=slot,
                    callback_monotonic=old_callback,
                    observed_end_monotonic=ended,
                )
            )
            capture._enqueue_microphone(
                AudioBlock(
                    (600.0 + slot,) * 960,
                    ended,
                    sequence=slot,
                    callback_monotonic=old_callback,
                    observed_end_monotonic=ended,
                )
            )
        capture._processing_stop.set()
        started = time.monotonic()
        capture._run_processing()

        self.assertLess(time.monotonic() - started, 0.050)
        self.assertEqual(len(processor.pairs), 3)
        self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
        self.assertEqual(capture.status().synchronization_wait_timeout_count, 0)
        self.assertEqual(processor.reset_count, 0)

    def test_reference_runtime_failure_degrades_once_without_aborting(self) -> None:
        factory, sources = factory_for(
            [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)],
            [(0.1, 0.02), (0.2, 0.04), (0.3, 0.06)],
        )
        events = []
        processor = FakeProcessor()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            capture = AecCapture(
                AecConfig(reference_stall_grace_s=0.060),
                processor=processor,
                source_factory=factory,
                on_event=events.append,
                output_dir=output,
            )
            capture.start()
            sources[0].error = RuntimeError("render endpoint disconnected")
            capture.raise_if_failed()
            capture.raise_if_failed()
            for sequence in range(3, 13):
                ended = 0.02 + sequence * 0.020
                capture._enqueue_microphone(
                    AudioBlock(
                        (0.1 + sequence / 10.0,) * 960,
                        ended,
                        sequence=sequence,
                        callback_monotonic=time.monotonic(),
                        observed_end_monotonic=ended,
                    )
                )
            deadline = time.monotonic() + 0.5
            while (
                capture.status().source_failure_unpaired_microphone_blocks < 7
                and time.monotonic() < deadline
            ):
                time.sleep(0.002)
            capture.stop()
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))

            self.assertEqual(len(processor.pairs), 3)
            self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
            self.assertIsNone(capture.status().error)
            self.assertIn(
                "render endpoint disconnected",
                capture.status().reference_error or "",
            )
            self.assertEqual(
                summary["source_tracks"]["microphone_received"]["frames"],
                13 * 960,
            )
            self.assertEqual(summary["tracks"]["microphone_raw"]["frames"], 2_880)
            self.assertEqual(
                capture.status().source_failure_unpaired_microphone_blocks,
                7,
            )
            self.assertEqual(capture.status().shutdown_unpaired_microphone_blocks, 3)
            self.assertEqual(
                [event.kind for event in events].count("reference_source_degraded"),
                1,
            )
            self.assertEqual(
                [event.kind for event in events].count("synchronization_degraded"),
                1,
            )

    def test_timeout_degraded_state_preserves_raw_payloads_and_reconciles_causes(
        self,
    ) -> None:
        processor = FakeProcessor()
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            initial_reference = [
                (700.0 + slot, 5.0 + slot * 0.020) for slot in range(3)
            ]
            initial_microphone = [
                (800.0 + slot, 5.0 + slot * 0.020) for slot in range(3)
            ]
            capture = AecCapture(
                AecConfig(reference_stall_grace_s=0.060),
                processor=processor,
                source_factory=factory_for(
                    initial_reference,
                    initial_microphone,
                )[0],
                on_event=events.append,
                output_dir=output,
            )
            capture.start()
            deadline = time.monotonic() + 0.5
            while len(processor.pairs) < 3 and time.monotonic() < deadline:
                time.sleep(0.002)
            self.assertEqual(len(processor.pairs), 3)

            for slot in range(3, 13):
                ended = 5.0 + slot * 0.020
                capture._enqueue_microphone(
                    AudioBlock(
                        (800.0 + slot,) * 960,
                        ended,
                        sequence=slot,
                        callback_monotonic=time.monotonic(),
                        observed_end_monotonic=ended,
                    )
                )
            deadline = time.monotonic() + 0.5
            while (
                capture.status().degraded_unpaired_microphone_blocks < 7
                and time.monotonic() < deadline
            ):
                time.sleep(0.002)
            capture.raise_if_failed()
            capture.stop()
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))

            status = capture.status()
            self.assertEqual(len(processor.pairs), 3)
            self.assertEqual(processor.reset_count, 0)
            self.assertEqual(status.zero_filled_reference_blocks, 0)
            self.assertFalse(status.echo_path_ready)
            self.assertEqual(status.synchronization_wait_timeout_count, 1)
            self.assertEqual(status.degraded_unpaired_microphone_blocks, 7)
            self.assertEqual(status.wait_timeout_unpaired_microphone_blocks, 7)
            self.assertEqual(status.source_failure_unpaired_microphone_blocks, 0)
            self.assertEqual(status.shutdown_unpaired_microphone_blocks, 3)
            self.assertEqual(
                3
                + status.degraded_unpaired_microphone_blocks
                + status.shutdown_unpaired_microphone_blocks,
                13,
            )
            self.assertEqual(
                summary["source_tracks"]["microphone_received"]["frames"],
                13 * 960,
            )
            self.assertEqual(summary["tracks"]["microphone_raw"]["frames"], 3 * 960)
            self.assertEqual(summary["status"], "degraded")
            self.assertEqual(
                [event.kind for event in events].count("synchronization_degraded"),
                1,
            )
            self.assertEqual(
                [
                    event.kind for event in events
                ].count("synchronization_live_buffer_retired"),
                1,
            )

    def test_proven_new_epoch_resets_once_and_recovers_after_degraded_state(self) -> None:
        processor = FakeProcessor()
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            initial_rows = [
                (900.0 + sequence, 6.0 + sequence * 0.020)
                for sequence in range(3)
            ]
            capture = AecCapture(
                AecConfig(reference_stall_grace_s=0.060),
                processor=processor,
                source_factory=factory_for(initial_rows, initial_rows)[0],
                on_event=events.append,
                output_dir=Path(temporary) / "capture",
            )

            def enqueue_pair(sequence: int, *, discontinuity: bool = False) -> None:
                ended = 6.0 + sequence * 0.020
                capture._enqueue_reference(
                    AudioBlock(
                        (900.0 + sequence,) * 960,
                        ended,
                        sequence=sequence,
                        callback_monotonic=time.monotonic(),
                        observed_end_monotonic=ended,
                        discontinuity=discontinuity,
                    )
                )
                capture._enqueue_microphone(
                    AudioBlock(
                        (1_000.0 + sequence,) * 960,
                        ended,
                        sequence=sequence,
                        callback_monotonic=time.monotonic(),
                        observed_end_monotonic=ended,
                        discontinuity=discontinuity,
                    )
                )

            capture.start()
            deadline = time.monotonic() + 0.5
            while len(processor.pairs) < 3 and time.monotonic() < deadline:
                time.sleep(0.002)
            self.assertEqual(len(processor.pairs), 3)

            for sequence in range(3, 7):
                ended = 6.0 + sequence * 0.020
                capture._enqueue_microphone(
                    AudioBlock(
                        (1_000.0 + sequence,) * 960,
                        ended,
                        sequence=sequence,
                        callback_monotonic=time.monotonic(),
                        observed_end_monotonic=ended,
                    )
                )
            deadline = time.monotonic() + 0.5
            while (
                capture.status().alignment_mode != "degraded"
                and time.monotonic() < deadline
            ):
                time.sleep(0.002)
            self.assertEqual(capture.status().alignment_mode, "degraded")

            for sequence in range(100, 103):
                enqueue_pair(sequence, discontinuity=sequence == 100)
            deadline = time.monotonic() + 0.5
            while len(processor.pairs) < 6 and time.monotonic() < deadline:
                time.sleep(0.002)
            capture.stop()

            self.assertEqual(len(processor.pairs), 6)
            self.assertEqual(
                [pair[0][0] for pair in processor.pairs[-3:]],
                [1_000.0, 1_001.0, 1_002.0],
            )
            self.assertEqual(
                [pair[1][0] for pair in processor.pairs[-3:]],
                [1_100.0, 1_101.0, 1_102.0],
            )
            self.assertEqual(processor.reset_count, 1)
            self.assertEqual(capture.status().hard_discontinuity_count, 1)
            self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
            self.assertEqual(
                [event.kind for event in events].count("alignment_realigning"),
                1,
            )

    def test_reference_only_barrier_recovers_from_degraded_state(self) -> None:
        processor = FakeProcessor()
        events = []
        initial_rows = [
            (float(sequence), 8.0 + sequence * 0.020)
            for sequence in range(3)
        ]
        capture = AecCapture(
            AecConfig(reference_stall_grace_s=0.060),
            processor=processor,
            source_factory=factory_for(initial_rows, initial_rows)[0],
            on_event=events.append,
        )

        def block(value: float, sequence: int, *, discontinuity: bool = False) -> AudioBlock:
            ended = 8.0 + sequence * 0.020
            return AudioBlock(
                (value,) * 960,
                ended,
                sequence=sequence,
                callback_monotonic=time.monotonic(),
                observed_end_monotonic=ended,
                discontinuity=discontinuity,
            )

        capture.start()
        deadline = time.monotonic() + 0.5
        while len(processor.pairs) < 3 and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(len(processor.pairs), 3)

        for sequence in range(3, 7):
            capture._enqueue_microphone(block(100.0 + sequence, sequence))
        deadline = time.monotonic() + 0.5
        while capture.status().alignment_mode != "degraded" and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(capture.status().alignment_mode, "degraded")

        for sequence in range(3, 8):
            capture._enqueue_reference(
                block(
                    200.0 + sequence,
                    sequence,
                    discontinuity=sequence == 3,
                )
            )
        capture._enqueue_microphone(block(107.0, 7))
        deadline = time.monotonic() + 0.75
        while len(processor.pairs) < 6 and time.monotonic() < deadline:
            time.sleep(0.002)
        capture.stop()

        self.assertGreaterEqual(len(processor.pairs), 6)
        self.assertEqual(processor.reset_count, 1)
        self.assertEqual(capture.status().hard_discontinuity_count, 1)
        self.assertIn("alignment_discontinuity_pending", [event.kind for event in events])
        self.assertIn("alignment_realigning", [event.kind for event in events])

    def test_same_epoch_degraded_recovery_keeps_sequence_mapping(self) -> None:
        processor = FakeProcessor()
        with tempfile.TemporaryDirectory() as temporary:
            initial_rows = [
                (float(sequence), 7.0 + sequence * 0.020)
                for sequence in range(3)
            ]
            capture = AecCapture(
                AecConfig(reference_stall_grace_s=0.060),
                processor=processor,
                source_factory=factory_for(initial_rows, initial_rows)[0],
                output_dir=Path(temporary) / "capture",
            )
            capture.start()
            deadline = time.monotonic() + 0.5
            while len(processor.pairs) < 3 and time.monotonic() < deadline:
                time.sleep(0.002)
            self.assertEqual(len(processor.pairs), 3)

            for sequence in range(3, 7):
                ended = 7.0 + sequence * 0.020
                capture._enqueue_microphone(
                    AudioBlock(
                        (100.0 + sequence,) * 960,
                        ended,
                        sequence=sequence,
                        callback_monotonic=time.monotonic(),
                        observed_end_monotonic=ended,
                    )
                )
            deadline = time.monotonic() + 0.5
            while (
                capture.status().degraded_unpaired_microphone_blocks < 1
                and time.monotonic() < deadline
            ):
                time.sleep(0.002)
            self.assertEqual(
                capture.status().degraded_unpaired_microphone_blocks,
                1,
            )

            for sequence in range(3, 7):
                ended = 7.0 + sequence * 0.020
                capture._enqueue_reference(
                    AudioBlock(
                        (200.0 + sequence,) * 960,
                        ended,
                        sequence=sequence,
                        callback_monotonic=time.monotonic(),
                        observed_end_monotonic=ended + 0.769,
                    )
                )
            deadline = time.monotonic() + 0.5
            while len(processor.pairs) < 6 and time.monotonic() < deadline:
                time.sleep(0.002)
            capture.stop()

            self.assertEqual(len(processor.pairs), 6)
            self.assertEqual(
                [pair[0][0] for pair in processor.pairs[-3:]],
                [204.0, 205.0, 206.0],
            )
            self.assertEqual(
                [pair[1][0] for pair in processor.pairs[-3:]],
                [104.0, 105.0, 106.0],
            )
            self.assertEqual(processor.reset_count, 0)
            self.assertEqual(capture.status().alignment_mode, "locked")
            self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
            self.assertEqual(capture.status().degraded_unpaired_reference_blocks, 1)
            self.assertEqual(capture.status().degraded_unpaired_microphone_blocks, 1)

    def test_reference_prepare_failure_starts_in_microphone_only_mode(self) -> None:
        sources = []

        def factory(_config, reference_callback, microphone_callback, _ref, _mic):
            reference = FakeSource(
                "fake-reference",
                reference_callback,
                [],
                start_error=RuntimeError("cannot open render endpoint"),
            )
            microphone = FakeSource(
                "fake-microphone",
                microphone_callback,
                [(0.1, 0.02), (0.2, 0.04), (0.3, 0.06)],
            )
            sources.extend((reference, microphone))
            return reference, microphone

        events = []
        processor = FakeProcessor()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            capture = AecCapture(
                AecConfig(startup_timeout_s=0.5),
                processor=processor,
                source_factory=factory,
                on_event=events.append,
                output_dir=output,
            )
            capture.start()
            capture.stop()
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))

            self.assertEqual(processor.pairs, [])
            self.assertEqual(capture.status().zero_filled_reference_blocks, 0)
            self.assertIsNone(capture.status().error)
            self.assertIn(
                "cannot open render endpoint",
                capture.status().reference_error or "",
            )
            self.assertEqual(
                summary["source_tracks"]["microphone_received"]["frames"],
                2_880,
            )
            self.assertEqual(summary["tracks"]["microphone_raw"]["frames"], 0)
            self.assertEqual(summary["status"], "degraded")
            self.assertEqual(
                [event.kind for event in events].count("reference_source_degraded"),
                1,
            )
            self.assertEqual(
                [event.kind for event in events].count("capture_degraded_ready"),
                1,
            )
            self.assertNotIn("capture_ready", [event.kind for event in events])
            self.assertTrue(all(source.stopped for source in sources))

    def test_internal_worker_backlog_is_included_in_health_limit(self) -> None:
        capture = AecCapture(
            AecConfig(queue_fatal_s=0.020),
            processor=FakeProcessor(),
            source_factory=factory_for([], [])[0],
        )
        capture._internal_reference_pending_blocks = 2

        with self.assertRaisesRegex(AudioBackendError, "processing backlog"):
            capture.raise_if_failed()

    def test_internal_worker_buffers_are_bounded_and_fail_explicitly(self) -> None:
        config = AecConfig(
            reference_stall_grace_s=0.100,
            queue_fatal_s=0.100,
        )
        capture = AecCapture(
            config,
            processor=FakeProcessor(),
            source_factory=factory_for([], [])[0],
        )
        capture._processor = FakeProcessor()
        capture._start_processing()

        def block(sequence: int, *, reference: bool) -> AudioBlock:
            ended = (100.0 if reference else 1.0) + sequence * config.block_duration_s
            return AudioBlock(
                (float(sequence),) * config.block_samples,
                ended,
                sequence=sequence,
                callback_monotonic=time.monotonic(),
                observed_end_monotonic=ended,
            )

        for sequence in range(3):
            capture._enqueue_microphone(block(sequence, reference=False))
        for sequence in range(20):
            capture._enqueue_reference(block(sequence, reference=True))
            time.sleep(0.010)

        deadline = time.monotonic() + 0.5
        while capture._processing_error is None and time.monotonic() < deadline:
            time.sleep(0.002)

        self.assertIsNotNone(capture._processing_error)
        self.assertLessEqual(
            capture._internal_reference_pending_blocks,
            capture._capture_queue_capacity_blocks,
        )
        with self.assertRaisesRegex(AudioBackendError, "processing backlog"):
            capture.raise_if_failed()
        capture._stop_processing()

    def test_capture_ingress_queue_has_a_hard_fatal_capacity(self) -> None:
        capture = AecCapture(
            AecConfig(queue_fatal_s=0.040),
            processor=FakeProcessor(),
            source_factory=factory_for([], [])[0],
        )
        block = AudioBlock((0.1,) * 960, 1.0, sequence=0)

        capture._enqueue_microphone(block)
        capture._enqueue_microphone(block)
        with self.assertRaisesRegex(AudioBackendError, "fatal capacity"):
            capture._enqueue_microphone(block)

        self.assertEqual(capture._microphone_queue.qsize(), 2)
        self.assertEqual(capture.status().microphone_queue_overflow_count, 1)
        with self.assertRaisesRegex(AudioBackendError, "capture processing failed"):
            capture.raise_if_failed()

    def test_fatal_limit_caps_custom_long_grace_without_artifacts(self) -> None:
        config = AecConfig(
            block_duration_s=0.010,
            pair_tolerance_s=0.005,
            reference_stall_grace_s=0.500,
            queue_fatal_s=0.080,
        )
        processor = FakeProcessor()
        events = []
        capture = AecCapture(
            config,
            processor=processor,
            source_factory=factory_for([], [])[0],
            on_event=events.append,
        )
        capture._processor = processor

        def audio(value: float, sequence: int) -> AudioBlock:
            ended = 10.0 + sequence * config.block_duration_s
            return AudioBlock(
                (value,) * config.block_samples,
                ended,
                sequence=sequence,
                callback_monotonic=ended,
                observed_end_monotonic=ended,
            )

        capture._start_processing()
        for sequence in range(3):
            capture._enqueue_reference(audio(100.0 + sequence, sequence))
            capture._enqueue_microphone(audio(200.0 + sequence, sequence))
        deadline = time.monotonic() + 0.5
        while len(processor.pairs) < 3 and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(len(processor.pairs), 3)

        for sequence in range(3, 15):
            capture._enqueue_microphone(audio(200.0 + sequence, sequence))
            time.sleep(config.block_duration_s * 1.2)
        deadline = time.monotonic() + 0.5
        while (
            capture.status().degraded_unpaired_microphone_blocks < 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.002)
        capture.raise_if_failed()
        capture._stop_processing()

        status = capture.status()
        self.assertEqual(status.synchronization_wait_timeout_count, 1)
        self.assertGreaterEqual(status.degraded_unpaired_microphone_blocks, 1)
        self.assertLessEqual(status.microphone_queue_s, config.queue_fatal_s)
        retirements = [
            event
            for event in events
            if event.kind == "synchronization_live_buffer_retired"
        ]
        self.assertEqual(len(retirements), 1)
        self.assertFalse(retirements[0].details["raw_payload_preserved"])
        degraded = [event for event in events if event.kind == "synchronization_degraded"]
        self.assertEqual(len(degraded), 1)
        self.assertEqual(degraded[0].details["effective_wait_limit_s"], 0.080)

    def test_capture_instance_cannot_be_restarted(self) -> None:
        factory, _sources = factory_for([(1.0, 0.02)], [(1.0, 0.02)])
        capture = AecCapture(processor=FakeProcessor(), source_factory=factory)
        capture.start()
        capture.stop()
        with self.assertRaisesRegex(RuntimeError, "cannot be restarted"):
            capture.start()

    def test_runtime_microphone_failure_degrades_without_session_exception(self) -> None:
        rows = [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)]
        factory, sources = factory_for(rows, rows)
        events = []
        with AecCapture(
                processor=FakeProcessor(),
                source_factory=factory,
                on_event=events.append,
            ) as capture:
            sources[1].error = RuntimeError("device disconnected")
            capture.raise_if_failed()
            capture.raise_if_failed()
            self.assertIsNone(capture.status().error)
            self.assertIn(
                "device disconnected",
                capture.status().microphone_error or "",
            )
        self.assertEqual(
            [event.kind for event in events].count("microphone_source_degraded"),
            1,
        )

    def test_one_source_stop_failure_does_not_finalize_before_retry(self) -> None:
        sources = []
        events = []

        def factory(_config, reference_callback, microphone_callback, _ref_device, _mic_device):
            reference = FakeSource(
                "fake-reference",
                reference_callback,
                [(1.0, 0.02)],
                stop_error=RuntimeError("reference stop failed"),
            )
            microphone = FakeSource("fake-microphone", microphone_callback, [(1.0, 0.02)])
            sources.extend([reference, microphone])
            return reference, microphone

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            capture = AecCapture(
                output_dir=output,
                processor=FakeProcessor(),
                source_factory=factory,
                on_event=events.append,
            )
            capture.start()
            with self.assertRaisesRegex(AudioBackendError, "reference stop failed"):
                capture.stop()

            self.assertTrue(all(source.stopped for source in sources))
            self.assertFalse((output / "summary.json").exists())
            self.assertEqual(
                [event.kind for event in events].count("capture_stopped"),
                0,
            )

            sources[0].stop_error = None
            capture.stop()
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "failed")
            self.assertIn("reference stop failed", summary["error"])
            self.assertEqual(
                [event.kind for event in events].count("capture_stopped"),
                1,
            )

    def test_stop_retries_transient_cleanup_and_finalization_exactly_once(self) -> None:
        class RetryReleaseSource(FakeSource):
            def __init__(self, backend, callback, rows) -> None:
                super().__init__(backend, callback, rows)
                self.stop_calls = 0

            def stop(self) -> None:
                self.stop_calls += 1
                self.stopped = True
                if self.stop_calls == 1:
                    raise RuntimeError("transient reference release")

        rows = [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)]
        sources = []
        events = []

        def factory(
            _config,
            reference_callback,
            microphone_callback,
            _reference_device,
            _microphone_device,
        ):
            reference = RetryReleaseSource(
                "fake-reference",
                reference_callback,
                rows,
            )
            microphone = FakeSource("fake-microphone", microphone_callback, rows)
            sources.extend((reference, microphone))
            return reference, microphone

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            capture = AecCapture(
                output_dir=output,
                processor=FakeProcessor(),
                source_factory=factory,
                on_event=events.append,
            )
            capture.start()
            artifacts = capture._artifacts
            self.assertIsNotNone(artifacts)
            assert artifacts is not None
            failed_close = artifacts.microphone_aec.close
            successful_close = artifacts.computer_audio.close
            failed_close_calls = 0
            successful_close_calls = 0

            def fail_once() -> None:
                nonlocal failed_close_calls
                failed_close_calls += 1
                if failed_close_calls == 1:
                    raise RuntimeError("transient recorder close")
                failed_close()

            def track_successful_close() -> None:
                nonlocal successful_close_calls
                successful_close_calls += 1
                successful_close()

            try:
                with (
                    patch.object(
                        artifacts.microphone_aec,
                        "close",
                        side_effect=fail_once,
                    ),
                    patch.object(
                        artifacts.computer_audio,
                        "close",
                        side_effect=track_successful_close,
                    ),
                ):
                    with self.assertRaisesRegex(
                        AudioBackendError,
                        "transient reference release",
                    ):
                        capture.stop(error="first primary error", status_name="failed")

                    self.assertFalse((output / "summary.json").exists())
                    with self.assertRaisesRegex(
                        AudioBackendError,
                        "transient recorder close",
                    ):
                        capture.stop(error="later stop error", status_name="completed")
                    self.assertFalse((output / "summary.json").exists())
                    capture.stop(error="third stop error", status_name="completed")

                summary_path = output / "summary.json"
                self.assertTrue(summary_path.is_file())
                summary_bytes = summary_path.read_bytes()
                summary = json.loads(summary_bytes)
                self.assertEqual(sources[0].stop_calls, 2)
                self.assertEqual(sources[1].stopped, True)
                self.assertEqual(failed_close_calls, 2)
                self.assertEqual(successful_close_calls, 1)
                self.assertEqual(summary["status"], "failed")
                self.assertEqual(summary["error"], "first primary error")
                self.assertEqual(
                    [event.kind for event in events].count("capture_stopped"),
                    1,
                )
                self.assertEqual(len(list(output.glob("summary.json"))), 1)
                self.assertTrue(
                    all(track["frames"] == 2_880 for track in summary["tracks"].values())
                )
                self.assertTrue(
                    all(
                        track["frames"] == 2_880
                        for track in summary["source_tracks"].values()
                    )
                )

                capture.stop(error="fourth stop error", status_name="completed")
                self.assertEqual(summary_path.read_bytes(), summary_bytes)
                self.assertEqual(
                    [event.kind for event in events].count("capture_stopped"),
                    1,
                )
            finally:
                if not artifacts.microphone_aec._closed:
                    failed_close()
                artifacts.events.close()

    def test_stop_retries_a_still_live_processing_worker(self) -> None:
        worker_started = threading.Event()
        release_worker = threading.Event()

        def wait_for_release() -> None:
            worker_started.set()
            release_worker.wait(1.0)

        capture = AecCapture(
            processor=FakeProcessor(),
            source_factory=factory_for([], [])[0],
        )
        capture._running = True
        capture._ever_started = True
        worker = threading.Thread(target=wait_for_release, daemon=True)
        capture._processing_thread = worker
        worker.start()
        self.assertTrue(worker_started.wait(1.0))

        try:
            with (
                patch.object(worker, "join", return_value=None),
                self.assertRaisesRegex(
                    AudioBackendError,
                    "capture processing thread did not stop",
                ),
            ):
                capture.stop()

            self.assertIs(capture._processing_thread, worker)
            release_worker.set()
            capture.stop()
            self.assertIsNone(capture._processing_thread)
        finally:
            release_worker.set()
            worker.join(timeout=1.0)

    def test_stop_retries_a_failed_stopped_event_before_finalizing(self) -> None:
        rows = [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)]
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            capture = AecCapture(
                output_dir=output,
                processor=FakeProcessor(),
                source_factory=factory_for(rows, rows)[0],
                on_event=events.append,
            )
            capture.start()
            artifacts = capture._artifacts
            self.assertIsNotNone(artifacts)
            assert artifacts is not None
            original_write = artifacts.events.write
            stopped_event_write_calls = 0

            def fail_stopped_event_once(event) -> None:
                nonlocal stopped_event_write_calls
                if event.kind == "capture_stopped":
                    stopped_event_write_calls += 1
                    if stopped_event_write_calls == 1:
                        raise RuntimeError("transient stopped event write")
                original_write(event)

            with patch.object(
                artifacts.events,
                "write",
                side_effect=fail_stopped_event_once,
            ):
                with self.assertRaisesRegex(
                    AudioBackendError,
                    "transient stopped event write",
                ):
                    capture.stop()
                self.assertFalse((output / "summary.json").exists())
                capture.stop()

            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            event_lines = [
                json.loads(line)
                for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            stopped_lines = [line for line in event_lines if line["kind"] == "capture_stopped"]
            self.assertEqual(stopped_event_write_calls, 2)
            self.assertEqual(len(stopped_lines), 1)
            self.assertEqual(
                [event.kind for event in events].count("capture_stopped"),
                1,
            )
            self.assertEqual(summary["event_count"], len(event_lines))

    def test_stop_serializes_concurrent_and_reentrant_calls(self) -> None:
        source_stop_entered = threading.Event()
        release_source_stop = threading.Event()
        second_stop_completed = threading.Event()
        events = []
        sources = []

        class BlockingStopSource(FakeSource):
            def __init__(self, backend, callback, rows) -> None:
                super().__init__(backend, callback, rows)
                self.stop_calls = 0

            def stop(self) -> None:
                self.stop_calls += 1
                source_stop_entered.set()
                release_source_stop.wait(1.0)
                self.stopped = True

        rows = [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)]

        def factory(
            _config,
            reference_callback,
            microphone_callback,
            _reference_device,
            _microphone_device,
        ):
            reference = BlockingStopSource(
                "fake-reference",
                reference_callback,
                rows,
            )
            microphone = FakeSource("fake-microphone", microphone_callback, rows)
            sources.extend((reference, microphone))
            return reference, microphone

        capture: AecCapture

        def on_event(event) -> None:
            events.append(event)
            if event.kind == "capture_stopped":
                capture.stop(error="reentrant later error", status_name="completed")

        capture = AecCapture(
            processor=FakeProcessor(),
            source_factory=factory,
            on_event=on_event,
        )
        capture.start()

        first = threading.Thread(target=capture.stop, daemon=True)

        def stop_second() -> None:
            capture.stop(error="concurrent later error", status_name="completed")
            second_stop_completed.set()

        second = threading.Thread(target=stop_second, daemon=True)
        first.start()
        self.assertTrue(source_stop_entered.wait(1.0))
        second.start()

        try:
            self.assertFalse(second_stop_completed.wait(0.100))
            release_source_stop.set()
            first.join(timeout=1.0)
            second.join(timeout=1.0)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertTrue(second_stop_completed.is_set())
            self.assertEqual(sources[0].stop_calls, 1)
            self.assertTrue(sources[1].stopped)
            self.assertEqual(
                [event.kind for event in events].count("capture_stopped"),
                1,
            )
        finally:
            release_source_stop.set()
            first.join(timeout=1.0)
            second.join(timeout=1.0)

    def test_callback_queue_overflow_during_source_stop_is_failed_not_degraded(self) -> None:
        rows = [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)]
        sources = []
        events = []

        def factory(
            _config,
            reference_callback,
            microphone_callback,
            _reference_device,
            _microphone_device,
        ):
            reference = ShutdownOverflowSource(
                "windows-reference",
                reference_callback,
                rows,
            )
            microphone = FakeSource("windows-microphone", microphone_callback, rows)
            sources.extend([reference, microphone])
            return reference, microphone

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            capture = AecCapture(
                output_dir=output,
                processor=FakeProcessor(),
                source_factory=factory,
                on_event=events.append,
            )
            capture.start()
            capture.stop()

            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            stopped = [event for event in events if event.kind == "capture_stopped"]
            self.assertEqual(len(stopped), 1)
            self.assertEqual(stopped[0].details["status"], "failed")
            self.assertEqual(summary["status"], "failed")
            self.assertIn("callback queue overflow", summary["error"])
            self.assertNotEqual(summary["status"], "degraded")
            self.assertEqual(capture.status().reference_callback_queue_overflow_count, 1)
            self.assertTrue(all(source.stopped for source in sources))


if __name__ == "__main__":
    unittest.main()
