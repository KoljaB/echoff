from __future__ import annotations

import queue
import threading
import time
import unittest
from array import array
from itertools import pairwise
from unittest.mock import patch

from echoff import AecConfig
from echoff.backends.windows import WasapiMicrophoneSource, WasapiReferenceSource


class _FakeLoopbackStream:
    def __init__(self, *, first_payload: bytes | None, first_delay_s: float) -> None:
        self.first_payload = first_payload
        self.first_delay_s = first_delay_s
        self.stop_requested = threading.Event()
        self.read_count = 0

    def read(self, _frames: int, *, exception_on_overflow: bool) -> bytes:
        del exception_on_overflow
        self.read_count += 1
        if self.read_count == 1 and self.first_payload is not None:
            if self.stop_requested.wait(self.first_delay_s):
                raise OSError("stream stopped")
            return self.first_payload
        self.stop_requested.wait()
        raise OSError("stream stopped")

    def stop_stream(self) -> None:
        self.stop_requested.set()

    def close(self) -> None:
        pass


class _SequencedLoopbackStream(_FakeLoopbackStream):
    def __init__(self, responses: list[tuple[bytes, float]]) -> None:
        super().__init__(first_payload=None, first_delay_s=0.0)
        self.responses = list(responses)

    def read(self, _frames: int, *, exception_on_overflow: bool) -> bytes:
        del exception_on_overflow
        self.read_count += 1
        if self.responses:
            payload, delay_s = self.responses.pop(0)
            if self.stop_requested.wait(delay_s):
                raise OSError("stream stopped")
            return payload
        self.stop_requested.wait()
        raise OSError("stream stopped")


class _FakePyAudio:
    def __init__(self, stream: _FakeLoopbackStream) -> None:
        self.stream = stream

    def get_default_wasapi_loopback(self) -> dict[str, object]:
        return {"index": 10, "name": "Fake loopback", "maxInputChannels": 1}

    def open(self, **_kwargs: object) -> _FakeLoopbackStream:
        return self.stream

    def terminate(self) -> None:
        pass


class _FakePyAudioModule:
    paInt16 = 8

    def __init__(self, stream: _FakeLoopbackStream) -> None:
        self.audio = _FakePyAudio(stream)

    def PyAudio(self) -> _FakePyAudio:
        return self.audio


class WindowsBackendUnitTests(unittest.TestCase):
    def test_surplus_collapse_keeps_one_device_clock_reserve(self) -> None:
        source = WasapiMicrophoneSource.__new__(WasapiMicrophoneSource)
        source.dropped_device_block_count = 0
        blocks: queue.Queue[bytes] = queue.Queue()
        blocks.put(b"first")
        blocks.put(b"second")
        blocks.put(b"third")

        selected = source._take_device_block(
            blocks,
            timeout_s=0.0,
            discard_surplus=True,
        )

        self.assertEqual(selected, b"second")
        self.assertEqual(blocks.qsize(), 1)
        self.assertEqual(blocks.get_nowait(), b"third")
        self.assertEqual(source.dropped_device_block_count, 1)

    def test_reference_waits_for_a_slightly_late_device_block(self) -> None:
        pcm = array("h", [16384] * 960).tobytes()
        stream = _FakeLoopbackStream(first_payload=pcm, first_delay_s=0.025)
        received: list[list[float]] = []
        callback_finished = threading.Event()

        def on_audio(samples: list[float], _ended_monotonic: float) -> None:
            received.append(samples)
            callback_finished.set()
            source.stop_event.set()

        source = WasapiReferenceSource(AecConfig(), on_audio)
        module = _FakePyAudioModule(stream)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            self.assertTrue(callback_finished.wait(1.0))
            source.stop()

        self.assertEqual(source.error, None)
        self.assertEqual(source.device_block_count, 1)
        self.assertEqual(source.synthetic_silence_block_count, 0)
        self.assertEqual(received, [[0.5] * 960])

    def test_active_reference_preserves_a_late_blocks_original_tick(self) -> None:
        first_pcm = array("h", [8192] * 960).tobytes()
        late_pcm = array("h", [16384] * 960).tobytes()
        stream = _SequencedLoopbackStream(
            [
                (first_pcm, 0.005),
                # This completion is well beyond the former 18 ms decision
                # margin but within the active-stream stall grace.
                (late_pcm, 0.075),
            ]
        )
        received: list[tuple[list[float], float, float]] = []
        callback_finished = threading.Event()

        def on_audio(samples: list[float], ended_monotonic: float) -> None:
            received.append((samples, ended_monotonic, time.monotonic()))
            if len(received) == 2:
                callback_finished.set()
                source.stop_event.set()

        source = WasapiReferenceSource(AecConfig(reference_stall_grace_s=0.100), on_audio)
        module = _FakePyAudioModule(stream)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            self.assertTrue(callback_finished.wait(1.0))
            source.stop()

        self.assertEqual(source.error, None)
        self.assertEqual(source.device_block_count, 2)
        self.assertEqual(source.synthetic_silence_block_count, 0)
        self.assertEqual(received[0][0], [0.25] * 960)
        self.assertEqual(received[1][0], [0.5] * 960)
        self.assertAlmostEqual(received[1][1] - received[0][1], 0.020, places=6)
        self.assertGreaterEqual(received[1][2] - received[1][1], 0.025)

    def test_reference_emits_clock_continuous_silence_when_endpoint_is_idle(self) -> None:
        stream = _FakeLoopbackStream(first_payload=None, first_delay_s=0.0)
        received: list[list[float]] = []
        callback_finished = threading.Event()

        def on_audio(samples: list[float], _ended_monotonic: float) -> None:
            received.append(samples)
            callback_finished.set()
            source.stop_event.set()

        source = WasapiReferenceSource(AecConfig(reference_stall_grace_s=0.060), on_audio)
        module = _FakePyAudioModule(stream)
        started = time.monotonic()
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            self.assertTrue(callback_finished.wait(1.0))
            source.stop()

        self.assertGreaterEqual(time.monotonic() - started, 0.06)
        self.assertEqual(source.error, None)
        self.assertEqual(source.device_block_count, 0)
        self.assertEqual(source.synthetic_silence_block_count, 1)
        self.assertEqual(received, [[0.0] * 960])

    def test_active_reference_stall_catches_up_without_a_timestamp_gap(self) -> None:
        pcm = array("h", [16384] * 960).tobytes()
        stream = _SequencedLoopbackStream([(pcm, 0.005)])
        timestamps: list[float] = []
        callback_finished = threading.Event()

        def on_audio(_samples: list[float], ended_monotonic: float) -> None:
            timestamps.append(ended_monotonic)
            if len(timestamps) == 4:
                callback_finished.set()
                source.stop_event.set()

        source = WasapiReferenceSource(AecConfig(reference_stall_grace_s=0.060), on_audio)
        module = _FakePyAudioModule(stream)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            self.assertTrue(callback_finished.wait(1.0))
            source.stop()

        self.assertEqual(source.error, None)
        self.assertEqual(source.device_block_count, 1)
        self.assertGreaterEqual(source.synthetic_silence_block_count, 3)
        for earlier, later in pairwise(timestamps):
            self.assertAlmostEqual(later - earlier, 0.020, places=6)


if __name__ == "__main__":
    unittest.main()
