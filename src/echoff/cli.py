"""Command-line interface for device discovery, capture, and analysis."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .analysis import analyze_capture, parse_window
from .backends import list_devices
from .config import AecConfig
from .errors import AecCaptureError
from .log import configure_logging
from .probe import ProbeConfig, default_output_dir, run_probe

LOGGER = logging.getLogger(__name__)


def _add_log_level(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        type=str.upper,
        help="console/file logging threshold (default: INFO)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="echoff",
        description="Echo off: synchronized system-audio and microphone AEC",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices = subparsers.add_parser("devices", help="list selectable capture devices")
    devices.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    _add_log_level(devices)

    record = subparsers.add_parser("record", help="record an inspectable hardware session")
    record.add_argument(
        "--duration",
        type=float,
        default=15.0,
        help=(
            "positive ambient duration in seconds (default: 15; with --play-wav, "
            "stimulus timing controls runtime)"
        ),
    )
    record.add_argument(
        "--output",
        type=Path,
        help="new or empty artifact directory (default: captures/echoff-TIMESTAMP)",
    )
    record.add_argument(
        "--reference-device",
        help="WASAPI loopback index or unique case-insensitive name fragment",
    )
    record.add_argument(
        "--microphone-device",
        help="WASAPI microphone index or unique case-insensitive name fragment",
    )
    record.add_argument(
        "--stream-delay-ms",
        type=int,
        default=50,
        help="WebRTC render-to-capture delay hint in milliseconds (default: 50)",
    )
    record.add_argument(
        "--noise-suppression",
        action="store_true",
        help="enable WebRTC noise suppression in addition to AEC",
    )
    record.add_argument(
        "--play-wav",
        type=Path,
        help="play a WAV through ffplay as a repeatable far-end stimulus",
    )
    record.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="number of stimulus playbacks (default: 1)",
    )
    record.add_argument(
        "--pre-roll",
        type=float,
        default=2.0,
        help="quiet seconds before the first stimulus (default: 2)",
    )
    record.add_argument(
        "--gap",
        type=float,
        default=1.0,
        help="seconds between stimulus repetitions (default: 1)",
    )
    record.add_argument(
        "--tail",
        type=float,
        default=1.0,
        help="seconds captured after the last stimulus (default: 1)",
    )
    record.add_argument(
        "--volume",
        type=int,
        default=100,
        help="ffplay stimulus volume from 0 to 100 (default: 100)",
    )
    _add_log_level(record)

    analyze = subparsers.add_parser("analyze", help="analyze an existing capture directory")
    analyze.add_argument("capture_dir", type=Path, help="directory containing the three WAV tracks")
    analyze.add_argument(
        "--far-end-window",
        action="append",
        default=[],
        metavar="START:END",
        help="far-end-only interval in seconds; repeat for multiple windows",
    )
    analyze.add_argument(
        "--near-end-window",
        action="append",
        default=[],
        metavar="START:END",
        help="near-end speech interval in seconds; repeat for multiple windows",
    )
    _add_log_level(analyze)
    return parser


def _run_devices(args: argparse.Namespace) -> int:
    configure_logging(level=args.log_level)
    devices = list_devices()
    if args.json:
        print(json.dumps([device.to_dict() for device in devices], indent=2))
        return 0
    for kind in ("reference", "microphone"):
        print(f"{kind} devices:")
        for device in (item for item in devices if item.kind == kind):
            default = " [default]" if device.is_default else ""
            print(f"  {device.index:>3}  {device.name}{default}")
    return 0


def _run_record(args: argparse.Namespace) -> int:
    output = (args.output or default_output_dir()).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    configure_logging(level=args.log_level, log_file=output / "run.log")
    LOGGER.info("Echoff hardware recording will write to %s", output)
    if args.play_wav is None:
        LOGGER.info("play computer audio and optionally speak while recording")
    else:
        LOGGER.info("remain silent during the WAV stimulus unless testing near-end speech")
    result = run_probe(
        ProbeConfig(
            output_dir=output,
            duration_s=args.duration,
            play_wav=args.play_wav,
            repetitions=args.repetitions,
            pre_roll_s=args.pre_roll,
            gap_s=args.gap,
            tail_s=args.tail,
            volume=args.volume,
            reference_device=args.reference_device,
            microphone_device=args.microphone_device,
            aec=AecConfig(
                stream_delay_ms=args.stream_delay_ms,
                noise_suppression=args.noise_suppression,
            ),
        )
    )
    print(json.dumps(result, indent=2))
    return 0


def _run_analyze(args: argparse.Namespace) -> int:
    configure_logging(level=args.log_level)
    report = analyze_capture(
        args.capture_dir,
        far_end_windows=[parse_window(value) for value in args.far_end_window],
        near_end_windows=[parse_window(value) for value in args.near_end_window],
        write_report=False,
    )
    print(json.dumps(report, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "devices":
            return _run_devices(args)
        if args.command == "record":
            return _run_record(args)
        if args.command == "analyze":
            return _run_analyze(args)
        parser.error(f"unknown command: {args.command}")
    except KeyboardInterrupt:
        return 130
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    except AecCaptureError as exc:
        LOGGER.error("%s", exc)
        return 5
    except Exception as exc:
        LOGGER.exception("unexpected failure")
        print(f"echoff: error: {exc}", file=sys.stderr)
        return 1
    return 1
