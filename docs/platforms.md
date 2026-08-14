# Platform support

[Documentation home](README.md)

Echoff separates portable processing from platform-specific device capture.

| Platform | `AecCapture`, `devices`, `record` | Processor classes |
|---|---|---|
| Windows 10/11 | Supported and physically tested | Supported |
| Linux | PipeWire backend; physically qualified on Ubuntu | Supported |
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

The PipeWire backend captures a sink monitor as the render reference and an
ordinary PipeWire source as the microphone. It uses the system `pactl` and
`pw-record` tools, requests 48 kHz mono streams, emits fixed 20 ms blocks, and
keeps monotonically increasing block-end timestamps on each stream's sample
clock. `echoff devices` lists monitor sources as references and other sources
as microphones.

The selected output monitor must correspond to the physical output that can
reach the selected microphone. Select explicit devices when PipeWire's default
sink or source is not the desired hardware.

The qualified Ubuntu configuration used PipeWire 1.0.5, a Jabra Speak 510
microphone, and built-in analog stereo output. Its 598-second periodic-playback
run processed 29,885 pairs without source failures, dropped or degraded blocks,
synchronization timeouts, clock corrections, discontinuities, or APM resets.
This qualifies that configuration; it does not claim equivalent physical
coverage for every distribution, PipeWire release, or audio device.

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

`AecConfig(backend="auto")` selects WASAPI on Windows and PipeWire on Linux.

Next: [Integration](integration.md) · [Architecture](architecture.md)
