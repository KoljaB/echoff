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
- `alignment.py` decides whether queue heads form a pair or one head is stale.
- `backends/` owns operating-system device capture only.
- `capture.py` coordinates source lifetime and one pairing worker.
- `recording.py` writes recoverable WAV and JSON artifacts.
- `analysis.py` reads artifacts and computes signal-level diagnostics.
- `cli.py` provides device, recording, and read-only analysis commands.

No module owns both platform device I/O and WebRTC processing.

## Pairing contract

The reference and microphone sources start independently. Equal callback
counts do not prove that their first blocks represent the same time interval.
Every block therefore carries its monotonic end time.

The aligner compares the two queue heads:

- Within tolerance: process one pair.
- Reference is older: do not submit it to APM; deliver it to `on_reference` and,
  when artifacts are enabled, preserve it on the shared WAV timeline.
- Microphone is older: do not submit it to APM; when artifacts are enabled,
  preserve the raw mic and write equal-duration silence to the other tracks.

After the first pair, a discontinuity starts a realignment episode. The APM is
reset once, stale heads are removed, and normal processing resumes on the next
matched pair. The event and counters remain visible to the caller.

## APM contract

WebRTC APM consumes exact 10 ms mono PCM16 frames. The processor validates
equal reference/microphone lengths, submits reverse audio immediately before
the corresponding microphone frame, and returns 48 kHz floating-point output.

The default capture block is 20 ms, so every call contains two APM frames.

## Echo-path readiness

`echo_path_ready` is a conservative warm-up heuristic, not a measurement or
guarantee of WebRTC filter convergence. With the default configuration it
becomes true after 3.25 seconds of paired reference frames with RMS at or above
0.001. Silence and unpaired frames do not advance the counter. A realignment
resets it.

This is a signal-derived state, not an application policy. Applications decide
whether to defer VAD or barge-in while the path is cold and must still validate
physical suppression on their hardware.

Next: [Integration](integration.md) · [Python API](python-api.md) ·
[Platform support](platforms.md)
