# Architecture

[Documentation home](README.md)

The package has one job: produce a microphone stream cleaned with the captured
render-endpoint reference that could have reached the loudspeakers.

```text
Windows system audio ----> reference source ----> timestamp queue --+
                                                                  aligner
Physical microphone -----> microphone source ---> timestamp queue --+
                                                                       |
                                  reference frame -> WebRTC reverse ----+
                                  microphone frame -> WebRTC capture --> clean mic
```

## Responsibilities

- `config.py` validates public configuration.
- `models.py` contains typed blocks, frames, events, and status snapshots.
- `processor.py` owns WebRTC APM framing, state, and reset behavior.
- `alignment.py` reconciles startup heads, then preserves established pair order
  and records clock skew.
- `backends/` owns operating-system device capture only.
- `capture.py` coordinates source lifetime and one pairing worker.
- `recording.py` writes recoverable WAV and JSON artifacts.
- `analysis.py` reads artifacts and computes signal-level diagnostics.
- `cli.py` provides device, recording, and read-only analysis commands.

No module owns both platform device I/O and WebRTC processing.

## Pairing contract

The three processed tracks advance exactly once for each confirmed
reference/microphone pair and therefore always have equal length. Unpaired input
never creates a synthetic processed slot. Leading unpaired microphone blocks are
retired from the processed timeline and counted in
`startup_unpaired_microphone_blocks`. When artifact recording is enabled, the
received-source tracks independently preserve every parseable payload in source
order.

Each source owns a sample-count clock. The callback captures local monotonic time
before parsing, then maps PortAudio's `inputBufferAdcTime` relative to the same
callback's `currentTime`. This cancels unrelated WASAPI/WDM-KS clock origins.
The mapped time is evidence only: received sample count determines block sequence
and a timestamp jump cannot create, remove, or duplicate samples.

Startup requires three consecutive compatible observations. The resulting
sequence offset remains authoritative throughout the epoch. Leading microphone
blocks without a matching reference are retired from the processed timeline and
counted in `startup_unpaired_microphone_blocks`; they are never processed with a
synthetic zero far-end channel. When artifacts are enabled, the raw microphone
payload remains in `microphone_received.wav`. Once the first real pair has been
processed, when either expected head is missing, one explicit synchronization
episode starts at the worker's first observation and buffers both directions for
up to the configured reserve (3 seconds by default). Arrival inside the reserve
drains all confirmed pairs in order without synthetic audio, mapping loss, or APM
reset.

After lock, timestamp residuals and gradual device-clock drift are telemetry;
they do not change pair identity. A source failure or expired reserve enters an
explicit degraded state and suspends paired output. Excess live buffers are
retired under disjoint cause counters even without artifact recording; when
artifacts are enabled, raw source payloads are also preserved in the received
tracks. A proven source sequence discontinuity opens one new epoch and resets
WebRTC immediately before the first new-epoch pair, never before already valid
queued pairs.

Callbacks with arbitrary positive sample counts are accumulated into fixed
blocks without reordering. A PortAudio status flag preserves the current payload
and marks it as discontinuous; it does not itself terminate capture. Source
failures are surfaced and degrade pairing rather than directly raising from
`raise_if_failed()`. Processing/callback failures and an unsafe unbounded
backlog remain failures.

When artifact recording is enabled, all received reference and microphone
payloads are written in source order to `reference_received.wav` and
`microphone_received.wav`. After degraded live buffers exceed the bounded
reserve, retirements cannot be inserted retroactively without unbounded latency;
they are reconciled by explicit timeout or source-failure counters. Without
artifacts, the bounded live buffers are still retired and counted, but no raw
source track is written.

## APM contract

WebRTC APM consumes exact 10 ms mono PCM16 frames. The processor validates
equal reference/microphone lengths, submits reverse audio immediately before
the corresponding microphone frame, and returns 48 kHz floating-point output.

The default capture block is 20 ms, so every call contains two APM frames.

## Echo-path readiness

`echo_path_ready` is a conservative signal gate, not a guarantee of WebRTC
filter convergence. With the default configuration it requires at least 7.5
seconds of paired reference frames with RMS at or above 0.001. It then requires
a rolling one-second raw-to-clean microphone reduction of at least 10 dB to
remain true for 250 ms before readiness latches. The raw microphone must also
have enough exposure to make that reduction meaningful. Silence and unpaired
startup/shutdown frames do not advance the gate. `reset_echo_path()` recreates
APM and restarts this quality gate without changing the capture alignment epoch,
but it is a hard boundary: any partial streaming or buffered adapter tails are
discarded. `reset_alignment()` performs the same cold start, clears adapter tails,
and also opens a new alignment epoch. Same-epoch waits and recovery do not reset
APM automatically, although degraded alignment gates the public readiness state
false.

This is a signal-derived state, not an application policy. Applications decide
whether to defer VAD or barge-in while the path is cold and must still validate
physical suppression on their hardware.

Next: [Integration](integration.md) · [Python API](python-api.md) ·
[Platform support](platforms.md)
