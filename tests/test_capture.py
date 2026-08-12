from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path

from echoff import AecCapture, AecConfig, AudioBackendError
from echoff.models import AecState


class FakeProcessor:
    def __init__(self) -> None:
        self.reset_count = 0
        self.pairs: list[tuple[tuple[float, ...], tuple[float, ...]]] = []

    def process_pair(self, reference, microphone):
        reference_tuple = tuple(reference)
        microphone_tuple = tuple(microphone)
        self.pairs.append((reference_tuple, microphone_tuple))
        return tuple(value * 0.5 for value in microphone_tuple)

    def reset_alignment(self) -> None:
        self.reset_count += 1

    @property
    def state(self) -> AecState:
        return AecState(True, 4.0, self.reset_count, self.reset_count)


class FakeSource:
    def __init__(
        self, backend: str, callback, rows, *, stop_error: Exception | None = None
    ) -> None:
        self.backend_name = backend
        self.callback = callback
        self.rows = rows
        self.error = None
        self.device_block_count = 0
        self.synthetic_silence_block_count = 0
        self.dropped_device_block_count = 0
        self.selected_device_name = backend
        self.selected_device_index = 1
        self.stopped = False
        self.stop_error = stop_error

    def start(self) -> None:
        for value, ended in self.rows:
            self.callback([value] * 960, ended)
            self.device_block_count += 1

    def stop(self) -> None:
        self.stopped = True
        if self.stop_error is not None:
            raise self.stop_error


def factory_for(reference_rows, microphone_rows):
    sources = []

    def factory(_config, reference_callback, microphone_callback, _reference_device, _mic_device):
        reference = FakeSource("fake-reference", reference_callback, reference_rows)
        microphone = FakeSource("fake-microphone", microphone_callback, microphone_rows)
        sources.extend([reference, microphone])
        return reference, microphone

    return factory, sources


class AecCaptureTests(unittest.TestCase):
    def test_context_manager_writes_equal_timeline_tracks_and_summary(self) -> None:
        factory, sources = factory_for(
            [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06)],
            [(0.2, 0.02), (0.4, 0.04), (0.6, 0.06)],
        )
        processor = FakeProcessor()
        frames = []
        events = []
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            with AecCapture(
                AecConfig(),
                on_frame=frames.append,
                on_event=events.append,
                output_dir=output,
                processor=processor,
                source_factory=factory,
            ) as capture:
                capture.raise_if_failed()

            self.assertEqual(len(frames), 3)
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
            self.assertEqual(summary["capture"]["processed_pair_count"], 3)
            kinds = [event.kind for event in events]
            self.assertIn("alignment_locked", kinds)
            self.assertIn("capture_ready", kinds)
            self.assertIn("capture_stopped", kinds)

    def test_runtime_realigns_once_and_pads_all_artifact_tracks(self) -> None:
        factory, _sources = factory_for(
            [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06), (4.0, 0.08), (5.0, 0.10)],
            [(0.1, 0.02), (0.4, 0.08), (0.5, 0.10)],
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
            self.assertEqual(processor.reset_count, 1)
            self.assertEqual(summary["capture"]["runtime_realignments"], 1)
            self.assertEqual(summary["capture"]["runtime_dropped_reference_blocks"], 2)
            counts = {track["frames"] for track in summary["tracks"].values()}
            self.assertEqual(counts, {4_800})

    def test_capture_instance_cannot_be_restarted(self) -> None:
        factory, _sources = factory_for([(1.0, 0.02)], [(1.0, 0.02)])
        capture = AecCapture(processor=FakeProcessor(), source_factory=factory)
        capture.start()
        capture.stop()
        with self.assertRaisesRegex(RuntimeError, "cannot be restarted"):
            capture.start()

    def test_one_source_stop_failure_does_not_skip_other_cleanup_or_summary(self) -> None:
        sources = []

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
            )
            capture.start()
            with self.assertRaisesRegex(AudioBackendError, "reference stop failed"):
                capture.stop()

            self.assertTrue(all(source.stopped for source in sources))
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "failed")
            self.assertIn("reference stop failed", summary["error"])


if __name__ == "__main__":
    unittest.main()
