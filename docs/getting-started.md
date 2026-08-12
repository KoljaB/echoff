# Getting started

## Requirements

- Windows 10 or 11 for built-in device capture
- Python 3.11 or newer
- A normal playback endpoint and microphone
- Optional: `ffplay` for repeatable WAV-stimulus probes

## Install

```powershell
git clone https://github.com/KoljaB/echoff D:\Projekte\echoff
cd D:\Projekte\echoff
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## Confirm devices

```powershell
.\.venv\Scripts\echoff.exe devices
```

Defaults are marked in the output. An index or unique case-insensitive name
fragment can be supplied to the recording command.

## First recording

```powershell
.\.venv\Scripts\echoff.exe record --duration 15 --log-level INFO
```

Play computer audio over the speakers. Speak for part of the run if you want to
check that near-end speech survives. Then inspect `summary.json` and the three
WAV tracks in the printed directory.

Use `examples\record_aec_session.py` when you prefer a standalone Python file
over the installed CLI.
