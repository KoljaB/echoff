# Hardware probe

Unit tests validate ordering and state transitions but cannot prove physical
echo suppression. The recording probe uses the real audio devices and always
keeps the evidence needed for review.

## Ambient recording

```powershell
python examples\record_aec_session.py --duration 20 --log-level DEBUG
```

Play normal computer audio and optionally speak. Compare the three WAV files in
an editor. Far-end speech should be much quieter in `microphone_aec.wav` than in
`microphone_raw.wav`; near-end speech should remain present.

## Repeatable stimulus

```powershell
python examples\record_aec_session.py `
  --play-wav D:\audio\known-speech.wav `
  --repetitions 3 `
  --pre-roll 2 `
  --gap 1 `
  --tail 1 `
  --log-level INFO
```

Remain silent unless the test plan explicitly asks for near-end speech. The CLI
records exact playback windows in `summary.json` and computes raw-versus-clean
RMS suppression in `analysis.json`.

Do not tune thresholds after looking at a result. Device identity, volume,
speaker position, stream delay, and input WAV should be fixed before comparing
changes.
