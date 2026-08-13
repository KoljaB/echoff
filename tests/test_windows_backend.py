from __future__ import annotations

import math
import threading
import time
import unittest
from array import array
from dataclasses import dataclass
from itertools import pairwise
from unittest.mock import patch

from echoff import AecConfig, AudioBackendError
from echoff.backends.windows import (
    WasapiMicrophoneSource,
    WasapiReferenceSource,
    _SharedWasapiContext,
)


@dataclass(frozen=True)
class _CallbackResponse:
    payload: bytes
    frame_count: int
    adc_time: float
    current_time: float | None = None
    status_flags: int = 0


class _FakeCallbackStream:
    def __init__(self, responses: list[_CallbackResponse]) -> None:
        self.responses = list(responses)
        self.callback = None
        self.active = False
        self.stop_requested = threading.Event()

        self.thread: threading.Thread | None = None

    def install(self, callback) -> None:
        self.callback = callback

    def start_stream(self) -> None:
        if self.callback is None:
            raise RuntimeError("callback was not installed")
        self.active = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        assert self.callback is not None
        try:
            for response in self.responses:
                if self.stop_requested.is_set():
                    break
                _output, result = self.callback(
                    response.payload,
                    response.frame_count,
                    {
                        "input_buffer_adc_time": response.adc_time,
                        "current_time": (
                            response.adc_time
                            if response.current_time is None
                            else response.current_time
                        ),
                        "output_buffer_dac_time": 0.0,
                    },
                    response.status_flags,
                )
                if result != _FakePyAudioModule.paContinue:
                    break
        finally:
            self.active = False

    def is_active(self) -> bool:
        return self.active

    def stop_stream(self) -> None:
        self.stop_requested.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.0)
        self.active = False

    def close(self) -> None:
        pass


class _FakePyAudio:
    def __init__(self, stream: _FakeCallbackStream) -> None:
        self.stream = stream
        self.open_kwargs: dict[str, object] | None = None
        self.terminated_thread_id: int | None = None

    def get_default_wasapi_loopback(self) -> dict[str, object]:
        return {"index": 10, "name": "Fake loopback", "maxInputChannels": 2}

    def get_default_wasapi_device(self) -> dict[str, object]:
        return {"index": 11, "name": "Fake microphone", "maxInputChannels": 1}

    def open(self, **kwargs: object) -> _FakeCallbackStream:
        self.open_kwargs = kwargs
        self.stream.install(kwargs["stream_callback"])
        return self.stream

    def terminate(self) -> None:
        self.terminated_thread_id = threading.get_ident()


class _FakePyAudioModule:
    paInt16 = 8
    paContinue = 0
    paComplete = 1
    paAbort = 2

    def __init__(self, stream: _FakeCallbackStream) -> None:
        self.audio = _FakePyAudio(stream)

    def PyAudio(self) -> _FakePyAudio:
        return self.audio


class _ReinitializingPyAudioModule(_FakePyAudioModule):
    def __init__(self, stream: _FakeCallbackStream) -> None:
        super().__init__(stream)
        self.managers: list[_FakePyAudio] = []

    def PyAudio(self) -> _FakePyAudio:
        manager = _FakePyAudio(self.audio.stream)
        self.managers.append(manager)
        if len(self.managers) == 1:
            def fail_open(**_kwargs: object) -> _FakeCallbackStream:
                raise OSError(-9996, "Invalid device")

            manager.open = fail_open  # type: ignore[method-assign]
        return manager


class _AlwaysFailingPyAudioModule(_ReinitializingPyAudioModule):
    def PyAudio(self) -> _FakePyAudio:
        manager = _FakePyAudio(self.audio.stream)
        self.managers.append(manager)

        def fail_open(**_kwargs: object) -> _FakeCallbackStream:
            raise OSError(-9996, "Invalid device")

        manager.open = fail_open  # type: ignore[method-assign]
        return manager


class WindowsBackendUnitTests(unittest.TestCase):
    def test_shared_context_serializes_portaudio_lifecycle_on_one_thread(self) -> None:
        module = _FakePyAudioModule(_FakeCallbackStream([]))
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            context = _SharedWasapiContext.create(2)
            first = context.call(threading.get_ident)
            second = context.call(threading.get_ident)
            self.assertEqual(first, second)
            self.assertNotEqual(first, threading.get_ident())
            context.release()
            self.assertIsNone(module.audio.terminated_thread_id)
            context.release()

        self.assertEqual(module.audio.terminated_thread_id, first)

    def test_callback_timeline_drift_uses_100ms_payload_cadence(self) -> None:
        source = WasapiMicrophoneSource(AecConfig(), lambda _block: None)
        payload = array("h", [0] * 4_800).tobytes()
        time_info = {"input_buffer_adc_time": 1.0, "current_time": 1.0}
        with patch(
            "echoff.backends.windows.time.monotonic",
            side_effect=(100.000, 100.110, 100.220),
        ):
            for _index in range(3):
                source._enqueue_callback_packet(payload, 4_800, time_info, 0)

        # Three 100-ms packets contain 200 ms after the first callback, while
        # the callback clock advanced 220 ms. The missing 20 ms remains
        # visible at the coarser native callback cadence.
        self.assertEqual(source.callback_packet_count, 3)
        self.assertEqual(source.callback_payload_frame_count, 14_400)
        self.assertAlmostEqual(source.callback_timeline_drift_s, 0.020, places=9)
        self.assertAlmostEqual(source.callback_timeline_drift_max_s, 0.020, places=9)

    def test_microphone_retries_once_with_a_fresh_portaudio_context(self) -> None:
        payload = array("h", [8192] * 960).tobytes()
        stream = _FakeCallbackStream([_CallbackResponse(payload, 960, 10.0)])
        module = _ReinitializingPyAudioModule(stream)
        received = []

        def on_audio(block) -> None:
            received.append(block)
            source.stop_event.set()

        source = WasapiMicrophoneSource(AecConfig(), on_audio)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            source.activate()
            deadline = time.monotonic() + 1.0
            while not received and time.monotonic() < deadline:
                time.sleep(0.010)
            source.stop()

        self.assertIsNone(source.error)
        self.assertEqual(len(module.managers), 2)
        self.assertIsNotNone(module.managers[0].terminated_thread_id)
        self.assertEqual(
            module.managers[0].terminated_thread_id,
            module.managers[1].terminated_thread_id,
        )
        self.assertIsNotNone(module.managers[1].open_kwargs)
        assert module.managers[1].open_kwargs is not None
        self.assertEqual(module.managers[1].open_kwargs["input_device_index"], 11)
        self.assertEqual(len(received), 1)

    def test_microphone_never_opens_more_than_twice(self) -> None:
        module = _AlwaysFailingPyAudioModule(_FakeCallbackStream([]))
        source = WasapiMicrophoneSource(
            AecConfig(allow_wdmks_microphone_fallback=False),
            lambda _block: None,
        )

        with (
            patch("echoff.backends.windows._load_pyaudio", return_value=module),
            self.assertRaisesRegex(AudioBackendError, "fresh-context retry"),
        ):
            source.start()

        self.assertEqual(len(module.managers), 2)
        self.assertTrue(
            all(manager.terminated_thread_id is not None for manager in module.managers)
        )

    def test_microphone_waits_for_activation_and_normalizes_backend_clock(self) -> None:
        payloads = [
            array("h", [8192] * 960).tobytes(),
            array("h", [16384] * 960).tobytes(),
            array("h", [24576] * 960).tobytes(),
        ]
        stream = _FakeCallbackStream(
            [
                _CallbackResponse(payload, 960, 10.0 + index * 0.020)
                for index, payload in enumerate(payloads)
            ]
        )
        received: list[tuple[float, float]] = []
        finished = threading.Event()

        def on_audio(block) -> None:
            received.append((block.samples[0], block.ended_monotonic))
            if len(received) == 3:
                finished.set()
                source.stop_event.set()

        source = WasapiMicrophoneSource(AecConfig(), on_audio)
        module = _FakePyAudioModule(stream)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            time.sleep(0.020)
            self.assertEqual(received, [])
            source.activate()
            self.assertTrue(finished.wait(1.0))
            source.stop()

        self.assertIsNone(source.error)
        self.assertEqual([value for value, _ended in received], [0.25, 0.5, 0.75])
        ends = [ended for _value, ended in received]
        self.assertTrue(all(math.isfinite(ended) for ended in ends))
        self.assertAlmostEqual(ends[1] - ends[0], 0.02)
        self.assertAlmostEqual(ends[2] - ends[1], 0.02)
        self.assertEqual(source.device_block_count, 3)
        self.assertEqual(source.synthetic_silence_block_count, 0)
        self.assertEqual(source.dropped_device_block_count, 0)
        assert module.audio.open_kwargs is not None
        self.assertFalse(module.audio.open_kwargs["start"])
        self.assertIn("stream_callback", module.audio.open_kwargs)
        self.assertEqual(module.audio.open_kwargs["frames_per_buffer"], 4_800)

    def test_microphone_keeps_audio_when_portaudio_timestamp_moves_backwards(self) -> None:
        payload = array("h", [8192] * 960).tobytes()
        stream = _FakeCallbackStream(
            [
                _CallbackResponse(payload, 960, 10.000000, 10.000000),
                _CallbackResponse(payload, 960, 10.020000, 10.020000),
                _CallbackResponse(payload, 960, 10.013283, 10.040000),
                _CallbackResponse(payload, 960, 10.060000, 10.060000),
            ]
        )
        received: list[float] = []
        finished = threading.Event()

        def on_audio(block) -> None:
            received.append(block.ended_monotonic)
            if len(received) == 4:
                finished.set()
                source.stop_event.set()

        source = WasapiMicrophoneSource(AecConfig(), on_audio)
        module = _FakePyAudioModule(stream)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            source.activate()
            self.assertTrue(finished.wait(1.0))
            source.stop()

        self.assertIsNone(source.error)
        self.assertEqual(source.device_block_count, 4)
        self.assertEqual(source.timestamp_regression_count, 1)
        self.assertEqual(source.invalid_timestamp_count, 0)
        self.assertGreaterEqual(source.timestamp_regression_count, 1)
        for previous, current in pairwise(received):
            self.assertAlmostEqual(current - previous, 0.02)

    def test_forward_timestamp_jump_never_invents_capture_blocks(self) -> None:
        received = []
        source = WasapiMicrophoneSource(AecConfig(), received.append)
        for index, value in enumerate((10.00, 10.02, 10.08, 10.10)):
            source._emit_samples(
                [float(index)] * 960,
                callback_monotonic=100.0 + index * 0.020,
                time_info={"input_buffer_adc_time": value, "current_time": 10.0 + index * 0.020},
                status_flags=0,
                discontinuity=False,
            )
        self.assertEqual(len(received), 4)
        self.assertEqual([item.sequence for item in received], [0, 1, 2, 3])
        self.assertEqual(source.timestamp_gap_block_count, 0)
        for previous, current in pairwise(item.ended_monotonic for item in received):
            self.assertAlmostEqual(current - previous, 0.020)

    def test_reference_downmixes_callback_payload_on_the_same_clock(self) -> None:
        stereo = array("h")
        for _index in range(960):
            stereo.extend((16384, 8192))
        stream = _FakeCallbackStream(
            [_CallbackResponse(stereo.tobytes(), 960, 20.5)]
        )
        received: list[tuple[list[float], float]] = []
        finished = threading.Event()

        def on_audio(block) -> None:
            received.append((list(block.samples), block.ended_monotonic))
            finished.set()
            source.stop_event.set()

        source = WasapiReferenceSource(AecConfig(), on_audio)
        module = _FakePyAudioModule(stream)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            source.activate()
            self.assertTrue(finished.wait(1.0))
            source.stop()

        self.assertIsNone(source.error)
        self.assertEqual(received[0][0], [0.375] * 960)
        self.assertTrue(math.isfinite(received[0][1]))
        self.assertEqual(source.device_block_count, 1)
        self.assertEqual(source.synthetic_silence_block_count, 0)
        assert module.audio.open_kwargs is not None
        self.assertEqual(module.audio.open_kwargs["frames_per_buffer"], 4_800)

    def test_100ms_microphone_callback_splits_into_five_ordered_blocks(self) -> None:
        pcm = array("h")
        for value in range(5):
            pcm.extend([value * 4096] * 960)
        stream = _FakeCallbackStream(
            [_CallbackResponse(pcm.tobytes(), 4_800, 25.0)]
        )
        received = []
        finished = threading.Event()

        def on_audio(block) -> None:
            received.append(block)
            if len(received) == 5:
                finished.set()
                source.stop_event.set()

        source = WasapiMicrophoneSource(AecConfig(), on_audio)
        module = _FakePyAudioModule(stream)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            source.activate()
            self.assertTrue(finished.wait(1.0))
            source.stop()

        self.assertIsNone(source.error)
        self.assertEqual([block.sequence for block in received], list(range(5)))
        self.assertEqual(
            [round(block.samples[0] * 32768.0) for block in received],
            [0, 4096, 8192, 12288, 16384],
        )
        for previous, current in pairwise(block.ended_monotonic for block in received):
            self.assertAlmostEqual(current - previous, 0.020)
        self.assertEqual(source.callback_packet_count, 1)
        self.assertEqual(source.callback_payload_frame_count, 4_800)
        self.assertEqual(source.device_block_count, 5)
        assert module.audio.open_kwargs is not None
        self.assertEqual(module.audio.open_kwargs["frames_per_buffer"], 4_800)

    def test_callback_status_preserves_current_payload_and_reports_discontinuity(self) -> None:
        payload = array("h", [0] * 960).tobytes()
        stream = _FakeCallbackStream(
            [_CallbackResponse(payload, 960, 30.0, status_flags=2)]
        )
        received = []

        def on_audio(block) -> None:
            received.append(block)
            source.stop_event.set()

        source = WasapiMicrophoneSource(AecConfig(), on_audio)
        module = _FakePyAudioModule(stream)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            source.activate()
            deadline = time.monotonic() + 1.0
            while source.error is None and time.monotonic() < deadline:
                time.sleep(0.010)
            source.stop()

        self.assertIsNone(source.error)
        self.assertEqual(len(received), 1)
        self.assertTrue(received[0].discontinuity)
        self.assertEqual(source.input_overflow_count, 1)
        self.assertEqual(source.device_block_count, 1)
        # PortAudio proves that some upstream input was lost, but does not
        # report a block count. The current callback payload is still kept.
        self.assertEqual(source.dropped_device_block_count, 0)
        self.assertEqual(source.input_overflow_count, 1)

    def test_nonfinite_callback_timestamp_falls_back_without_stopping_capture(self) -> None:
        payload = array("h", [0] * 960).tobytes()
        stream = _FakeCallbackStream(
            [
                _CallbackResponse(payload, 960, float("nan")),
                _CallbackResponse(payload, 960, float("nan")),
            ]
        )
        received: list[float] = []
        finished = threading.Event()

        def on_audio(block) -> None:
            received.append(block.ended_monotonic)
            if len(received) == 2:
                finished.set()
                source.stop_event.set()

        source = WasapiMicrophoneSource(AecConfig(), on_audio)
        module = _FakePyAudioModule(stream)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            source.activate()
            self.assertTrue(finished.wait(1.0))
            source.stop()

        self.assertIsNone(source.error)
        self.assertEqual(source.device_block_count, 2)
        self.assertEqual(source.invalid_timestamp_count, 2)
        self.assertGreater(received[1], received[0])
        self.assertAlmostEqual(received[1] - received[0], 0.020, places=9)

    def test_partial_callback_frames_are_accumulated_without_loss(self) -> None:
        first = array("h", [4096] * 480).tobytes()
        second = array("h", [8192] * 480).tobytes()
        stream = _FakeCallbackStream(
            [
                _CallbackResponse(first, 480, 40.0),
                _CallbackResponse(second, 480, 40.01),
            ]
        )
        received = []

        def on_audio(block) -> None:
            received.append(block)
            source.stop_event.set()

        source = WasapiMicrophoneSource(AecConfig(), on_audio)
        module = _FakePyAudioModule(stream)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            source.activate()
            deadline = time.monotonic() + 1.0
            while not received and time.monotonic() < deadline:
                time.sleep(0.010)
            source.stop()

        self.assertIsNone(source.error)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].samples[:480], (0.125,) * 480)
        self.assertEqual(received[0].samples[480:], (0.25,) * 480)

    def test_100ms_callbacks_never_wait_for_decoder_and_stop_drains_losslessly(
        self,
    ) -> None:
        packet_count = 8
        responses = []
        for packet_index in range(packet_count):
            pcm = array("h")
            for block_index in range(5):
                pcm.extend([packet_index * 5 + block_index] * 960)
            responses.append(
                _CallbackResponse(
                    pcm.tobytes(),
                    4_800,
                    50.0 + packet_index * 0.100,
                )
            )
        stream = _FakeCallbackStream(responses)
        decoder_entered = threading.Event()
        release_decoder = threading.Event()
        received: list[int] = []

        def on_audio(block) -> None:
            received.append(round(block.samples[0] * 32768.0))
            if len(received) == 1:
                decoder_entered.set()
                self.assertTrue(release_decoder.wait(1.0))

        source = WasapiMicrophoneSource(AecConfig(), on_audio)
        module = _FakePyAudioModule(stream)
        with patch("echoff.backends.windows._load_pyaudio", return_value=module):
            source.start()
            source.activate()
            self.assertTrue(decoder_entered.wait(1.0))
            deadline = time.monotonic() + 1.0
            while (
                source.callback_packet_count < packet_count
                and time.monotonic() < deadline
            ):
                time.sleep(0.001)

            # All native callbacks complete although the decoder/downstream
            # path is deliberately blocked on the first packet.
            self.assertEqual(source.callback_packet_count, packet_count)
            self.assertGreaterEqual(source.callback_queue_high_watermark, packet_count - 1)

            stop_finished = threading.Event()

            def stop_source() -> None:
                source.stop()
                stop_finished.set()

            stopper = threading.Thread(target=stop_source, daemon=True)
            stopper.start()
            self.assertFalse(stop_finished.wait(0.020))
            release_decoder.set()
            self.assertTrue(stop_finished.wait(1.0))
            stopper.join(timeout=1.0)

        self.assertIsNone(source.error)
        self.assertEqual(received, list(range(packet_count * 5)))
        self.assertEqual(source.device_block_count, packet_count * 5)
        self.assertEqual(source.callback_payload_frame_count, packet_count * 4_800)
        self.assertEqual(source.dropped_device_block_count, 0)
        self.assertGreater(source.callback_queue_age_max_s, 0.0)


if __name__ == "__main__":
    unittest.main()
