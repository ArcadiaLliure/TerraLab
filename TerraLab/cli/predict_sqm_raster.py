"""SQM raster prediction command."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from TerraLab.light_pollution.processing import predict_sqm_raster


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Predict an SQM raster from aggregated DVNL."
    )
    parser.add_argument("input_agg", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--elevation", type=float, default=0.0)
    return parser


def run(args: argparse.Namespace) -> Path:
    if not args.input_agg.is_file():
        raise FileNotFoundError(f"input file does not exist: {args.input_agg}")
    if not args.model.is_file():
        raise FileNotFoundError(f"model file does not exist: {args.model}")
    predict_sqm_raster(
        args.input_agg,
        args.model,
        args.output,
        elevation_m=args.elevation,
    )
    return args.output


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output = run(args)
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"SQM prediction complete: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
