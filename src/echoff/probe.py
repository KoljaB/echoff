"""User-facing physical capture probe built only on the public API."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analysis import analyze_capture
from .capture import AecCapture
from .config import AecConfig

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    output_dir: Path
    duration_s: float = 15.0
    play_wav: Path | None = None
    repetitions: int = 1
    pre_roll_s: float = 2.0
    gap_s: float = 1.0
    tail_s: float = 1.0
    volume: int = 100
    reference_device: str | None = None
    microphone_device: str | None = None
    aec: AecConfig = field(default_factory=AecConfig)

    def __post_init__(self) -> None:
        if self.duration_s <= 0.0:
            raise ValueError("duration_s must be positive")
        if self.repetitions <= 0:
            raise ValueError("repetitions must be positive")
        if min(self.pre_roll_s, self.gap_s, self.tail_s) < 0.0:
            raise ValueError("pre-roll, gap, and tail durations cannot be negative")
        if not 0 <= self.volume <= 100:
            raise ValueError("volume must be between 0 and 100")


def default_output_dir() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return (Path.cwd() / "captures" / f"echoff-{stamp}").resolve()


def _wait(capture: AecCapture, duration_s: float) -> None:
    deadline = time.monotonic() + duration_s
    while True:
        capture.raise_if_failed()
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return
        time.sleep(min(0.1, remaining))


def _play_wav(capture: AecCapture, path: Path, volume: int) -> tuple[float, float]:
    executable = shutil.which("ffplay")
    if executable is None:
        raise RuntimeError("ffplay is required for --play-wav")
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nodisp",
        "-autoexit",
        "-volume",
        str(volume),
        str(path),
    ]
    started = time.monotonic()
    capture.record_event("probe_playback_started", wav=str(path), volume=volume)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        while process.poll() is None:
            capture.raise_if_failed()
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3.0)
    if process.returncode != 0:
        raise RuntimeError(f"ffplay failed with exit code {process.returncode}")
    ended = time.monotonic()
    capture.record_event(
        "probe_playback_completed",
        wav=str(path),
        duration_s=ended - started,
    )
    return started, ended


def run_probe(config: ProbeConfig) -> dict[str, Any]:
    """Run one audible or ambient hardware capture and preserve its evidence."""

    output = config.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    unexpected = sorted(path.name for path in output.iterdir() if path.name != "run.log")
    if unexpected:
        raise FileExistsError(
            f"probe output is not empty and will not be overwritten: {unexpected}"
        )
    play_wav = None if config.play_wav is None else config.play_wav.resolve()
    if play_wav is not None and not play_wav.is_file():
        raise FileNotFoundError(play_wav)
    playback_windows_absolute: list[tuple[float, float]] = []
    capture = AecCapture(
        config.aec,
        output_dir=output,
        reference_device=config.reference_device,
        microphone_device=config.microphone_device,
    )
    interrupted = False
    try:
        capture.start()
        if play_wav is None:
            LOGGER.info(
                "recording for %.1f seconds; play normal computer audio and optionally speak",
                config.duration_s,
            )
            _wait(capture, config.duration_s)
        else:
            LOGGER.info("quiet pre-roll: %.1f seconds", config.pre_roll_s)
            _wait(capture, config.pre_roll_s)
            for index in range(config.repetitions):
                LOGGER.info("playing stimulus %d/%d: %s", index + 1, config.repetitions, play_wav)
                started, ended = _play_wav(capture, play_wav, config.volume)
                playback_windows_absolute.append((started, ended))
                if index + 1 < config.repetitions:
                    _wait(capture, config.gap_s)
            _wait(capture, config.tail_s)
        timeline_origin = capture.timeline_started_monotonic
        if timeline_origin is None:
            raise RuntimeError("capture produced no artifact timeline")
        playback_windows = [
            (started - timeline_origin, ended - timeline_origin)
            for started, ended in playback_windows_absolute
        ]
        capture.set_summary_metadata(
            probe={
                "play_wav": None if play_wav is None else str(play_wav),
                "repetitions": config.repetitions,
                "pre_roll_s": config.pre_roll_s,
                "gap_s": config.gap_s,
                "tail_s": config.tail_s,
                "volume": config.volume,
                "playback_windows_s": playback_windows,
            }
        )
    except KeyboardInterrupt:
        interrupted = True
        LOGGER.warning("probe interrupted; finalizing capture artifacts")
        capture.stop(status_name="interrupted")
        raise
    except Exception as exc:
        try:
            capture.stop(error=str(exc), status_name="failed")
        except Exception:
            LOGGER.exception("capture cleanup also failed")
        raise
    finally:
        if not interrupted:
            capture.stop()

    # Trim player startup/drain edges. These are echo-suppression windows only
    # when the operator followed the probe instruction to remain silent.
    far_end_windows = [
        (start + 0.25, end - 0.25) for start, end in playback_windows if end - start > 0.6
    ]
    analysis = analyze_capture(output, far_end_windows=far_end_windows)
    LOGGER.info("probe complete: %s", output)
    LOGGER.info("tracks and diagnostics: %s", output / "summary.json")
    return {
        "output_dir": str(output),
        "summary": json.loads((output / "summary.json").read_text(encoding="utf-8")),
        "analysis": analysis,
    }
