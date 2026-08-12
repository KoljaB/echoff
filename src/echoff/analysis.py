"""Signal-level analysis of preserved Echoff capture artifacts."""

from __future__ import annotations

import json
import math
import os
import wave
from array import array
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ANALYSIS_SCHEMA = "echoff-analysis-v1"


def read_wav(path: Path) -> tuple[tuple[float, ...], int]:
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise ValueError(f"expected PCM16 WAV: {path}")
        rate = source.getframerate()
        channels = source.getnchannels()
        values = array("h")
        values.frombytes(source.readframes(source.getnframes()))
    samples = tuple(value / 32768.0 for value in values)
    if channels > 1:
        samples = tuple(
            sum(samples[index : index + channels]) / channels
            for index in range(0, len(samples) - channels + 1, channels)
        )
    return samples, rate


def rms(samples: Sequence[float]) -> float:
    return math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples)))


def dbfs(value: float) -> float:
    return 20.0 * math.log10(max(abs(value), 1e-9))


def track_metrics(samples: Sequence[float], rate: int) -> dict[str, Any]:
    peak = max((abs(sample) for sample in samples), default=0.0)
    return {
        "frames": len(samples),
        "sample_rate_hz": rate,
        "duration_s": len(samples) / rate,
        "rms": rms(samples),
        "rms_dbfs": dbfs(rms(samples)),
        "peak": peak,
        "peak_dbfs": dbfs(peak),
        "clipped_sample_count": sum(abs(sample) >= 32767 / 32768 for sample in samples),
    }


def parse_window(value: str) -> tuple[float, float]:
    start_text, separator, end_text = value.partition(":")
    if not separator:
        raise ValueError(f"invalid window {value!r}; expected START:END")
    start = float(start_text)
    end = float(end_text)
    if not math.isfinite(start) or not math.isfinite(end) or start < 0.0 or end <= start:
        raise ValueError(f"invalid window {value!r}")
    return start, end


def _window(samples: Sequence[float], rate: int, start: float, end: float) -> Sequence[float]:
    if not math.isfinite(start) or not math.isfinite(end) or start < 0.0 or end <= start:
        raise ValueError(f"invalid analysis window {start}:{end}")
    start_index = round(start * rate)
    end_index = round(end * rate)
    if start_index < 0 or end_index <= start_index or end_index > len(samples):
        raise ValueError(f"window {start}:{end} is outside the shared microphone timeline")
    return samples[start_index:end_index]


def _level_comparison(
    raw: Sequence[float],
    clean: Sequence[float],
    rate: int,
    windows: Sequence[tuple[float, float]],
    *,
    metric_name: str,
) -> dict[str, Any]:
    rows = []
    raw_energy = 0.0
    clean_energy = 0.0
    sample_count = 0
    for start, end in windows:
        raw_values = _window(raw, rate, start, end)
        clean_values = _window(clean, rate, start, end)
        if not raw_values or len(raw_values) != len(clean_values):
            raise ValueError(f"window {start}:{end} is outside the shared microphone timeline")
        raw_rms = rms(raw_values)
        clean_rms = rms(clean_values)
        value_db = 20.0 * math.log10(max(raw_rms, 1e-9) / max(clean_rms, 1e-9))
        rows.append(
            {
                "start_s": start,
                "end_s": end,
                "raw_rms": raw_rms,
                "clean_rms": clean_rms,
                metric_name: value_db,
            }
        )
        raw_energy += sum(value * value for value in raw_values)
        clean_energy += sum(value * value for value in clean_values)
        sample_count += len(raw_values)
    pooled_raw = math.sqrt(raw_energy / max(1, sample_count))
    pooled_clean = math.sqrt(clean_energy / max(1, sample_count))
    return {
        metric_name: 20.0 * math.log10(max(pooled_raw, 1e-9) / max(pooled_clean, 1e-9)),
        "windows": rows,
    }


def _near_end_comparison(
    raw: Sequence[float],
    clean: Sequence[float],
    rate: int,
    windows: Sequence[tuple[float, float]],
) -> dict[str, Any]:
    rows = []
    raw_energy = 0.0
    clean_energy = 0.0
    sample_count = 0
    for start, end in windows:
        raw_values = _window(raw, rate, start, end)
        clean_values = _window(clean, rate, start, end)
        if not raw_values or len(raw_values) != len(clean_values):
            raise ValueError(f"window {start}:{end} is outside the shared microphone timeline")
        raw_rms = rms(raw_values)
        clean_rms = rms(clean_values)
        rows.append(
            {
                "start_s": start,
                "end_s": end,
                "raw_rms": raw_rms,
                "clean_rms": clean_rms,
                "near_end_retained_db": (
                    20.0 * math.log10(max(clean_rms, 1e-9) / max(raw_rms, 1e-9))
                ),
            }
        )
        raw_energy += sum(value * value for value in raw_values)
        clean_energy += sum(value * value for value in clean_values)
        sample_count += len(raw_values)
    pooled_raw = math.sqrt(raw_energy / max(1, sample_count))
    pooled_clean = math.sqrt(clean_energy / max(1, sample_count))
    return {
        "near_end_retained_db": (
            20.0 * math.log10(max(pooled_clean, 1e-9) / max(pooled_raw, 1e-9))
        ),
        "windows": rows,
    }


def _energy_envelope(samples: Sequence[float], rate: int, frame_s: float = 0.01) -> list[float]:
    frame_samples = max(1, round(rate * frame_s))
    return [
        rms(samples[offset : offset + frame_samples])
        for offset in range(0, len(samples) - frame_samples + 1, frame_samples)
    ]


def energy_lag(
    reference: Sequence[float],
    microphone: Sequence[float],
    rate: int,
) -> dict[str, Any]:
    """Estimate broad render-to-microphone lag using 10 ms energy envelopes."""

    left = _energy_envelope(reference, rate)
    right = _energy_envelope(microphone, rate)
    length = min(len(left), len(right))
    if length < 10:
        return {"lag_ms": None, "normalized_correlation": None}
    left = left[:length]
    right = right[:length]
    best_lag = 0
    best_correlation = -1.0
    for lag in range(-100, 101):
        if lag >= 0:
            stop = length - lag
            if stop <= 0:
                continue
            x = left[:stop]
            y = right[lag:]
        else:
            x = left[-lag:]
            y = right[: length + lag]
        if min(len(x), len(y)) < 10:
            continue
        x_mean = sum(x) / len(x)
        y_mean = sum(y) / len(y)
        numerator = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y, strict=True))
        denominator = math.sqrt(
            sum((a - x_mean) ** 2 for a in x) * sum((b - y_mean) ** 2 for b in y)
        )
        correlation = 0.0 if denominator <= 1e-12 else numerator / denominator
        if correlation > best_correlation:
            best_lag = lag
            best_correlation = correlation
    return {
        "lag_ms": best_lag * 10.0,
        "normalized_correlation": best_correlation,
    }


def analyze_capture(
    capture_dir: str | Path,
    *,
    far_end_windows: Sequence[tuple[float, float]] = (),
    near_end_windows: Sequence[tuple[float, float]] = (),
    write_report: bool = True,
) -> dict[str, Any]:
    root = Path(capture_dir).resolve()
    reference, reference_rate = read_wav(root / "computer_audio.wav")
    raw, raw_rate = read_wav(root / "microphone_raw.wav")
    clean, clean_rate = read_wav(root / "microphone_aec.wav")
    if len({reference_rate, raw_rate, clean_rate}) != 1:
        raise ValueError("capture tracks do not share one sample rate")
    if len({len(reference), len(raw), len(clean)}) != 1:
        raise ValueError("capture tracks do not share one sample timeline")
    report: dict[str, Any] = {
        "schema_version": ANALYSIS_SCHEMA,
        "capture_dir": str(root),
        "tracks": {
            "computer_audio": track_metrics(reference, reference_rate),
            "microphone_raw": track_metrics(raw, raw_rate),
            "microphone_aec": track_metrics(clean, clean_rate),
        },
        "loopback_to_raw_energy_alignment": energy_lag(reference, raw, raw_rate),
        # This whole-run level change is descriptive only. It must not be
        # called echo suppression because it may include real near-end speech.
        "whole_run_raw_to_clean_level_change_db": (
            20.0 * math.log10(max(rms(raw), 1e-9) / max(rms(clean), 1e-9))
        ),
    }
    if far_end_windows:
        report["far_end_only"] = _level_comparison(
            raw,
            clean,
            raw_rate,
            far_end_windows,
            metric_name="echo_suppression_db",
        )
    if near_end_windows:
        report["near_end"] = _near_end_comparison(
            raw,
            clean,
            raw_rate,
            near_end_windows,
        )
    if write_report:
        path = root / "analysis.json"
        temporary = root / ".analysis.json.tmp"
        if path.exists() or temporary.exists():
            raise FileExistsError(path if path.exists() else temporary)
        payload = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, path)
    return report
