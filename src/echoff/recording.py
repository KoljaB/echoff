"""Recoverable WAV and structured diagnostic artifact writers."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import wave
from array import array
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import AecConfig
from .errors import CaptureStateError
from .models import CaptureEvent, CaptureStatus

ARTIFACT_SCHEMA = "echoff-capture-artifacts-v2"


def canonical_json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    separators = None if pretty else (",", ":")
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2 if pretty else None,
            separators=separators,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    if path.exists() or temporary.exists():
        raise FileExistsError(path if path.exists() else temporary)
    created_temporary = False
    try:
        payload = canonical_json_bytes(value, pretty=True)
        with temporary.open("xb") as handle:
            created_temporary = True
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, path)
    except Exception:
        if created_temporary:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        raise


class PcmWavRecorder:
    """Incrementally persist mono float samples as PCM16 WAV."""

    def __init__(self, path: Path, sample_rate: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.sample_rate = int(sample_rate)
        self.sample_count = 0
        self._lock = threading.Lock()
        self._closed = False
        self._wave_closed = False
        self._raw_closed = False
        self._raw = path.open("xb")
        try:
            # This writer intentionally stays open for the recorder lifetime.
            self._wave = wave.open(self._raw, "wb")  # noqa: SIM115
        except Exception:
            self._raw.close()
            raise
        self._wave.setnchannels(1)
        self._wave.setsampwidth(2)
        self._wave.setframerate(self.sample_rate)

    def write(self, samples: Sequence[float]) -> None:
        if not samples:
            return
        pcm = array("h")
        pcm.extend(
            max(-32768, min(32767, round(max(-1.0, min(1.0, sample)) * 32767.0)))
            for sample in samples
        )
        with self._lock:
            if self._closed:
                raise CaptureStateError(f"recorder already closed: {self.path}")
            self._wave.writeframes(pcm.tobytes())
            self.sample_count += len(samples)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._wave_closed:
                self._wave.close()
                self._wave_closed = True
            if not self._raw_closed:
                self._raw.close()
                self._raw_closed = True
            self._closed = True


class EventWriter:
    """Append low-volume structured events with a stable sequence number."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = path.open("x", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False

    @property
    def count(self) -> int:
        with self._lock:
            return self._sequence

    def write(self, event: CaptureEvent) -> None:
        payload = event.to_dict()
        with self._lock:
            if self._closed:
                return
            self._sequence += 1
            payload["sequence"] = self._sequence
            self._handle.write(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._handle.flush()
            self._handle.close()
            self._closed = True


class CaptureArtifacts:
    """Own all files belonging to one capture session."""

    TRACK_NAMES = ("computer_audio", "microphone_raw", "microphone_aec")
    SOURCE_TRACK_NAMES = ("reference_received", "microphone_received")

    def __init__(self, output_dir: Path, config: AecConfig) -> None:
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        reserved = [
            self.output_dir / "config.json",
            self.output_dir / "events.jsonl",
            self.output_dir / "summary.json",
            self.output_dir / "analysis.json",
            self.output_dir / ".analysis.json.tmp",
            *(
                self.output_dir / f"{name}.wav"
                for name in (*self.TRACK_NAMES, *self.SOURCE_TRACK_NAMES)
            ),
        ]
        existing = [path.name for path in reserved if path.exists()]
        if existing:
            raise FileExistsError(
                f"capture output contains reserved files and will not be overwritten: {existing}"
            )
        events: EventWriter | None = None
        recorders: list[PcmWavRecorder] = []
        try:
            write_json_atomic(
                self.output_dir / "config.json",
                {
                    "schema_version": ARTIFACT_SCHEMA,
                    "config": config.to_dict(),
                },
            )
            events = EventWriter(self.output_dir / "events.jsonl")
            for name in self.TRACK_NAMES:
                recorders.append(
                    PcmWavRecorder(self.output_dir / f"{name}.wav", config.sample_rate)
                )
            recorders.append(
                PcmWavRecorder(
                    self.output_dir / "reference_received.wav",
                    config.sample_rate,
                )
            )
            recorders.append(
                PcmWavRecorder(
                    self.output_dir / "microphone_received.wav",
                    config.sample_rate,
                )
            )
        except Exception:
            for recorder in recorders:
                recorder.close()
            if events is not None:
                events.close()
            raise
        self.events = events
        (
            self.computer_audio,
            self.microphone_raw,
            self.microphone_aec,
            self.reference_received,
            self.microphone_received,
        ) = recorders
        self._recorders = tuple(recorders)
        self._closed_recorder_ids: set[int] = set()
        self._finalize_lock = threading.RLock()
        self._events_closed = False
        self._summary_written = False
        self._closed = False

    def write_pair(
        self,
        reference: Sequence[float],
        microphone_raw: Sequence[float],
        microphone_clean: Sequence[float],
    ) -> None:
        if not (len(reference) == len(microphone_raw) == len(microphone_clean)):
            raise ValueError("all artifact tracks must advance by equal sample counts")
        self.computer_audio.write(reference)
        self.microphone_raw.write(microphone_raw)
        self.microphone_aec.write(microphone_clean)

    def write_reference_received(self, reference: Sequence[float]) -> None:
        """Preserve every real reference payload independently of live assignment."""

        self.reference_received.write(reference)

    def write_microphone_received(self, microphone: Sequence[float]) -> None:
        """Preserve every real microphone payload independently of live AEC."""

        self.microphone_received.write(microphone)

    def close_tracks(self) -> None:
        with self._finalize_lock:
            if self._closed:
                return
            errors: list[Exception] = []
            for recorder in self._recorders:
                recorder_id = id(recorder)
                if recorder_id in self._closed_recorder_ids:
                    continue
                try:
                    recorder.close()
                except Exception as exc:
                    errors.append(exc)
                else:
                    self._closed_recorder_ids.add(recorder_id)
            self._closed = len(self._closed_recorder_ids) == len(self._recorders)
            if errors:
                raise errors[0]

    def _close_events(self) -> None:
        if self._events_closed:
            return
        self.events.close()
        self._events_closed = True

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().upper()

    @staticmethod
    def _wav_metadata(path: Path) -> dict[str, Any]:
        with wave.open(str(path), "rb") as source:
            frames = source.getnframes()
            rate = source.getframerate()
            return {
                "path": path.name,
                "bytes": path.stat().st_size,
                "sha256": CaptureArtifacts._sha256(path),
                "channels": source.getnchannels(),
                "sample_width_bytes": source.getsampwidth(),
                "sample_rate_hz": rate,
                "frames": frames,
                "duration_s": frames / rate,
            }

    def finalize(
        self,
        *,
        status_name: str,
        capture_status: CaptureStatus,
        started_utc: str,
        ended_utc: str,
        duration_s: float,
        error: str | None,
        metadata: dict[str, Any],
        timeline_started_monotonic: float | None,
    ) -> Path:
        path = self.output_dir / "summary.json"
        with self._finalize_lock:
            if self._summary_written:
                return path
            self.close_tracks()
            tracks = {
                name: self._wav_metadata(self.output_dir / f"{name}.wav")
                for name in self.TRACK_NAMES
            }
            source_tracks = {
                name: self._wav_metadata(self.output_dir / f"{name}.wav")
                for name in self.SOURCE_TRACK_NAMES
            }
            frame_counts = {int(track["frames"]) for track in tracks.values()}
            summary = {
                "schema_version": ARTIFACT_SCHEMA,
                "status": status_name,
                "started_utc": started_utc,
                "ended_utc": ended_utc,
                "duration_s": duration_s,
                "timeline_started_monotonic": timeline_started_monotonic,
                "capture": capture_status.to_dict(),
                "tracks": tracks,
                "source_tracks": source_tracks,
                "tracks_share_timeline": len(frame_counts) == 1,
                "event_count": self.events.count,
                "metadata": metadata,
                "error": error,
            }
            self._close_events()
            write_json_atomic(path, summary)
            self._summary_written = True
            return path
