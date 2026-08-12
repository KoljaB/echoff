# Development rules

- Keep capture, alignment, processing, recording, analysis, and CLI code in
  separate modules.
- The core package must not contain VAD, ASR, TTS, or conversation policy.
- Preserve the atomic `process_pair(reference, microphone)` contract.
- Do not mix system audio into the microphone before AEC.
- Capture backends must emit fixed-size mono blocks with monotonically
  increasing end timestamps.
- Runtime discontinuities are realigned and reported. Do not silently pair
  stale frames and do not terminate a healthy session merely because the two
  callback counters temporarily diverge.
- A realignment resets the WebRTC APM once per episode.
- The library must not configure the root logger. Applications and the CLI own
  handlers and log levels.
- Unit tests never access physical audio devices. Hardware tests live under
  `examples/` or the `echoff record` command and always preserve artifacts.
- Keep source modules focused. Split a module before it becomes responsible for
  unrelated concerns.
