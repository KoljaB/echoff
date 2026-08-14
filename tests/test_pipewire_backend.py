from __future__ import annotations

import io
import sys
import threading
import time
import unittest
from array import array
from unittest.mock import patch

from echoff import AecConfig, AudioBackendError
from echoff.backends.pipewire import (
    PipeWireSource,
    _PipeWireDevice,
    _PipeWireStartupCoordinator,
    _run_pactl,
    _run_pw_dump,
    _select_device,
    list_pipewire_devices,
)
from echoff.models import AudioBlock


class _FakeProcess:
    def __init__(self, payload: bytes, returncode: int = 0) -> None:
        self.stdout = io.BytesIO(payload)
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode


class PipeWireBackendUnitTests(unittest.TestCase):
    def test_missing_pipewire_commands_have_actionable_errors(self) -> None:
        with (
            patch("echoff.backends.pipewire.shutil.which", return_value=None),
            self.assertRaisesRegex(AudioBackendError, "pactl is required"),
        ):
            _run_pactl("get-default-sink")
        with (
            patch("echoff.backends.pipewire.shutil.which", return_value=None),
            self.assertRaisesRegex(AudioBackendError, "pw-dump is required"),
        ):
            _run_pw_dump()
        source = PipeWireSource(AecConfig(), lambda _block: None, "microphone", None)
        with (
            patch("echoff.backends.pipewire.shutil.which", return_value=None),
            self.assertRaisesRegex(AudioBackendError, "pw-record is required"),
        ):
            source.start()

    def test_device_selector_rejects_missing_and_ambiguous_matches(self) -> None:
        devices = [
            _PipeWireDevice("microphone", 10, "Jabra microphone", 1, 48_000.0, True),
            _PipeWireDevice("microphone", 11, "Jabra microphone backup", 1, 48_000.0, False),
        ]
        with (
            patch("echoff.backends.pipewire._discover_devices", return_value=devices),
            self.assertRaisesRegex(AudioBackendError, "is ambiguous"),
        ):
            _select_device("microphone", "Jabra")
        with (
            patch("echoff.backends.pipewire._discover_devices", return_value=devices),
            self.assertRaisesRegex(AudioBackendError, "did not match"),
        ):
            _select_device("microphone", "missing")

    def test_numeric_device_selector_takes_precedence_over_name_fragment(self) -> None:
        devices = [
            _PipeWireDevice("reference", 49, "Jabra SPEAK 510 monitor", 1, 48_000.0, True),
            _PipeWireDevice("reference", 51, "Built-in analog stereo monitor", 1, 48_000.0, False),
        ]
        with patch("echoff.backends.pipewire._discover_devices", return_value=devices):
            selected = _select_device("reference", "51")

        self.assertEqual(selected.index, 51)

    def test_startup_coordinator_preserves_and_orders_both_streams(self) -> None:
        received: list[tuple[str, int]] = []
        coordinator = _PipeWireStartupCoordinator(
            lambda block: received.append(("reference", block.sequence)),
            lambda block: received.append(("microphone", block.sequence)),
        )
        for sequence in range(6):
            coordinator.submit(
                "microphone",
                AudioBlock((0.0,), 10.00 + sequence * 0.02, sequence),
            )
        self.assertEqual(received, [])
        for sequence in range(3):
            coordinator.submit(
                "reference",
                AudioBlock((0.0,), 10.01 + sequence * 0.02, sequence),
            )

        self.assertEqual(
            received,
            [
                ("microphone", 0),
                ("reference", 0),
                ("microphone", 1),
                ("reference", 1),
                ("microphone", 2),
                ("reference", 2),
            ],
        )
        coordinator._release_after = 0.0
        coordinator.submit("reference", AudioBlock((0.0,), 10.07, 3))
        self.assertEqual(
            received[6:],
            [
                ("microphone", 3),
                ("reference", 3),
                ("microphone", 4),
                ("microphone", 5),
            ],
        )

    def test_device_listing_classifies_monitor_and_microphone_sources(self) -> None:
        defaults = {
            ("get-default-sink",): "alsa_output.pci-main.analog-stereo",
            ("get-default-source",): "alsa_input.usb-jabra.mono-fallback",
            ("list", "short", "sources"): (
                "51\talsa_output.pci-main.analog-stereo.monitor\tPipeWire\t"
                "s32le 2ch 48000Hz\tSUSPENDED\n"
                "50\talsa_input.usb-jabra.mono-fallback\tPipeWire\t"
                "s16le 1ch 16000Hz\tSUSPENDED"
            ),
        }
        nodes = [
            {
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {
                        "object.serial": 51,
                        "node.name": "alsa_output.pci-main.analog-stereo",
                        "media.class": "Audio/Sink",
                    }
                },
            },
            {
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {
                        "object.serial": 50,
                        "node.name": "alsa_input.usb-jabra.mono-fallback",
                        "media.class": "Audio/Source",
                    }
                },
            },
        ]
        with (
            patch(
                "echoff.backends.pipewire._run_pactl",
                side_effect=lambda *arguments: defaults[arguments],
            ),
            patch("echoff.backends.pipewire._run_pw_dump", return_value=nodes),
        ):
            devices = list_pipewire_devices()

        self.assertEqual([device.kind for device in devices], ["microphone", "reference"])
        self.assertTrue(all(device.is_default for device in devices))
        self.assertEqual(devices[0].channels, 1)
        self.assertEqual(devices[0].name, "alsa_input.usb-jabra.mono-fallback")
        self.assertEqual(devices[1].name, "alsa_output.pci-main.analog-stereo.monitor")
        self.assertEqual(devices[0].default_sample_rate, 16_000.0)
        self.assertEqual(devices[1].channels, 2)
        self.assertEqual(devices[1].default_sample_rate, 48_000.0)

    def test_source_emits_fixed_mono_blocks_on_one_monotonic_sample_clock(self) -> None:
        samples = array("f", [0.25] * 960 + [0.5] * 960)
        if sys.byteorder != "little":
            samples.byteswap()
        process = _FakeProcess(samples.tobytes())
        received = []

        def receive(block: AudioBlock) -> None:
            received.append(block)
            if len(received) == 2:
                source._stop.set()

        source = PipeWireSource(AecConfig(), receive, "microphone", None)
        source.selected_device_name = "alsa_input.usb-jabra.mono-fallback"
        source.selected_device_index = 50

        with patch("echoff.backends.pipewire.subprocess.Popen", return_value=process):
            source._capture()

        self.assertEqual(len(received), 2)
        self.assertEqual(received[0].samples, (0.25,) * 960)
        self.assertEqual(received[1].samples, (0.5,) * 960)
        self.assertAlmostEqual(
            received[1].ended_monotonic - received[0].ended_monotonic, 0.02
        )
        self.assertEqual([block.sequence for block in received], [0, 1])
        self.assertEqual(source.device_block_count, 2)
        self.assertEqual(source.callback_payload_frame_count, 1_920)

    def test_pw_record_startup_failure_reports_node_exit_and_stderr(self) -> None:
        process = _FakeProcess(b"", returncode=2)
        source = PipeWireSource(AecConfig(), lambda _block: None, "reference", None)
        source.selected_device_name = "alsa_output.test.monitor"
        source.selected_device_index = 51

        def launch(*_args, stderr, **_kwargs):
            stderr.write(b"target node not found\n")
            return process

        with (
            patch("echoff.backends.pipewire.subprocess.Popen", side_effect=launch),
            self.assertRaisesRegex(
                AudioBackendError,
                r"reference node 51.*exit 2: target node not found",
            ),
        ):
            source._capture()

    def test_device_disconnect_after_audio_is_reported_without_losing_block(self) -> None:
        samples = array("f", [0.25] * 960)
        process = _FakeProcess(samples.tobytes(), returncode=7)
        received = []
        source = PipeWireSource(AecConfig(), received.append, "microphone", None)
        source.selected_device_name = "alsa_input.usb-jabra.mono-fallback"
        source.selected_device_index = 50

        def launch(*_args, stderr, **_kwargs):
            stderr.write(b"target removed\n")
            return process

        with (
            patch("echoff.backends.pipewire.subprocess.Popen", side_effect=launch),
            self.assertRaisesRegex(AudioBackendError, r"node 50.*target removed"),
        ):
            source._capture()

        self.assertEqual(len(received), 1)
        self.assertEqual(source.device_block_count, 1)

    def test_microphone_launch_waits_for_reference_gate(self) -> None:
        gate = threading.Event()
        samples = array("f", [0.25] * 960)
        process = _FakeProcess(samples.tobytes())

        def receive(_block: AudioBlock) -> None:
            source._stop.set()

        source = PipeWireSource(
            AecConfig(), receive, "microphone", None, launch_gate=gate
        )
        source.selected_device_name = "alsa_input.usb-jabra.mono-fallback"
        source.selected_device_index = 50
        with patch(
            "echoff.backends.pipewire.subprocess.Popen", return_value=process
        ) as launch:
            thread = threading.Thread(target=source._capture)
            thread.start()
            time.sleep(0.02)
            launch.assert_not_called()
            gate.set()
            thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        launch.assert_called_once()

    def test_microphone_waiting_for_failed_reference_stops_without_launch(self) -> None:
        gate = threading.Event()
        source = PipeWireSource(
            AecConfig(), lambda _block: None, "microphone", None, launch_gate=gate
        )
        source.selected_device_name = "alsa_input.usb-jabra.mono-fallback"
        source.selected_device_index = 50
        with patch("echoff.backends.pipewire.subprocess.Popen") as launch:
            thread = threading.Thread(target=source._capture)
            thread.start()
            time.sleep(0.02)
            source._stop.set()
            thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
