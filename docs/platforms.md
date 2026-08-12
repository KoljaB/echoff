# Platform support

[Documentation home](README.md)

Echoff separates portable processing from platform-specific device capture.

| Platform | `AecCapture`, `devices`, `record` | Processor classes |
|---|---|---|
| Windows 10/11 | Supported and physically tested | Supported |
| Linux | Not implemented; PipeWire backend planned | Designed for application-owned aligned PCM where LiveKit installs; not qualified here |
| macOS | Not implemented; no committed backend roadmap | Designed for application-owned aligned PCM where LiveKit installs; not qualified here |

## Windows

System audio is captured from a WASAPI loopback endpoint through
PyAudioWPatch. The microphone uses WASAPI and, when allowed, falls back to a
matched (or sole) WDM-KS input through `sounddevice` if PortAudio cannot open
it.

The fallback is a runtime recovery path, not a separately selectable device in
`echoff devices`. Selected backend, device name, and index are recorded in
`summary.json`.

Known operational boundaries:

- loopback follows one Windows render endpoint, not every endpoint at once;
- application-specific routing can bypass the default endpoint;
- exclusive/protected playback may not appear in shared loopback;
- device/profile changes require a new capture instance; and
- independently clocked sources still require timestamp alignment even when
  nominal sample rates match.

## Linux

A future backend should capture a PipeWire sink monitor and microphone source
while preserving clock-continuous silence and monotonic block-end timestamps.
The backend must pass real duplex hardware and echo-suppression tests before
support is claimed.

## macOS

No built-in capture backend is implemented. A future design would require a
validated system-audio tap plus microphone capture, permissions, timestamp
alignment, and physical duplex tests. Echoff currently makes no macOS live
capture claim.

## Portable processor layer

`WebRtcAecProcessor`, `BufferedWebRtcAecProcessor`, and
`StreamingWebRtcAecProcessor` have no built-in device dependency. They can run
where the LiveKit WebRTC APM dependency is available, but the host must supply
48 kHz mono PCM and honor the alignment/ordering contract. Code availability is
not the same as a physically qualified platform integration.

`AecConfig(backend="auto")` is valid on every platform; creating built-in
capture sources outside Windows raises `UnsupportedPlatformError`.

Next: [Integration](integration.md) · [Architecture](architecture.md)
