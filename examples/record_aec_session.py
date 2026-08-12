"""Record a real Echoff session with three directly comparable WAV tracks.

This is an external hardware check, not a unit test. It uses only the public
Echoff probe API and always preserves the recording and diagnostics.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from echoff import AecConfig
from echoff.log import configure_logging
from echoff.probe import ProbeConfig, default_output_dir, run_probe


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--play-wav", type=Path)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--pre-roll", type=float, default=2.0)
    parser.add_argument("--gap", type=float, default=1.0)
    parser.add_argument("--tail", type=float, default=1.0)
    parser.add_argument("--volume", type=int, default=100)
    parser.add_argument("--reference-device")
    parser.add_argument("--microphone-device")
    parser.add_argument("--stream-delay-ms", type=int, default=50)
    parser.add_argument("--noise-suppression", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    output = (args.output or default_output_dir()).resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    configure_logging(level=args.log_level, log_file=output / "run.log")

    if args.play_wav is None:
        print("Recording now. Play computer audio and optionally speak into the microphone.")
    else:
        print("The WAV stimulus will be audible. Remain silent unless testing near-end speech.")

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
    print(f"\nInspect the three WAV files in: {output}")


if __name__ == "__main__":
    main()
