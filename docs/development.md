# Development

## Test layers

1. Processor tests inject a fake APM and prove 10 ms ordering and warm-up.
2. Aligner tests use deterministic timestamps and prove startup/runtime paths.
3. Capture tests inject fake sources and verify callbacks and artifact timelines.
4. Hardware probes open real devices and preserve WAV/JSON evidence.

Unit tests must remain deterministic and must not need speakers, a microphone,
network access, or a running model service.

## Adding a backend

Implement `CaptureSource`, return `DeviceInfo` records from a device lister, and
add a factory branch. Backends emit fixed-size mono float blocks with a
monotonically increasing `ended_monotonic` timestamp. They do not know about
WebRTC, recording, VAD, or application policy.
