# Troubleshooting

## No Windows devices are listed

Install the Windows extra in the same environment:

```powershell
python -m pip install -e .
```

## Capture starts but no reference audio is visible

Confirm that the selected WASAPI loopback device belongs to the endpoint that
actually drives the speakers. Run `echoff devices` and pass the index with
`--reference-device`.

## Echo becomes strong after a device change

Keep the complete artifact directory. Inspect alignment events, selected device
names, realignment counters, and all three WAV files. Do not compensate by
comparing ASR text or by increasing an application VAD threshold.

## The echo path is never ready

Readiness advances only while paired far-end RMS is at least 0.001. Silent
capture is expected to remain cold. Play normal computer audio for at least
3.25 seconds.

## Linux or macOS reports unsupported platform

Those capture adapters are not implemented in version 0.1. The processor can
still be used with application-owned, already aligned PCM streams.
