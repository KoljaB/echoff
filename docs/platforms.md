# Platform support

## Windows

Implemented. System audio is captured from a WASAPI loopback endpoint through
PyAudioWPatch. The microphone uses WASAPI and falls back to a matching WDM-KS
input through `sounddevice` if PortAudio cannot open it.

## Linux

Planned. A backend should capture a PipeWire sink monitor and a microphone
source while preserving clock-continuous silence and monotonic block end times.
The processor and aligner do not need Linux-specific branches.

`backend="auto"` currently raises `UnsupportedPlatformError` outside Windows.
The package does not claim support until a backend has passed physical duplex
capture and echo-suppression tests.
