from __future__ import annotations

import queue
import unittest

from echoff.backends.windows import WasapiMicrophoneSource


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


if __name__ == "__main__":
    unittest.main()
