"""DVNL preprocessing command."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from TerraLab.light_pollution.processing import preprocess_dvnl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and normalize a DVNL GeoTIFF."
    )
    parser.add_argument("input", type=Path, help="Input DVNL GeoTIFF")
    parser.add_argument("output", type=Path, help="Cleaned output GeoTIFF")
    return parser


def run(args: argparse.Namespace) -> Path:
    if not args.input.is_file():
        raise FileNotFoundError(f"input file does not exist: {args.input}")
    preprocess_dvnl(args.input, args.output)
    return args.output


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output = run(args)
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"DVNL preprocessing complete: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
