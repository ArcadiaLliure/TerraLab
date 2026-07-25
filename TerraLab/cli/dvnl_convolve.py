"""DVNL convolution command."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from TerraLab.light_pollution.kernels import (
    create_gaussian_kernel,
    create_power_law_kernel,
)
from TerraLab.light_pollution.processing import convolve_dvnl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convolve a DVNL raster with a dispersion kernel."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--kernel", choices=["gaussian", "power-law"], default="gaussian"
    )
    parser.add_argument("--sigma", type=float, default=30.0)
    parser.add_argument("--rmax", type=float, default=200.0)
    parser.add_argument("--res", type=float, default=1.0)
    parser.add_argument("--tile-size", type=int, default=1024)
    return parser


def run(args: argparse.Namespace) -> Path:
    if not args.input.is_file():
        raise FileNotFoundError(f"input file does not exist: {args.input}")
    if args.kernel == "gaussian":
        kernel = create_gaussian_kernel(args.sigma, args.rmax, args.res)
    else:
        kernel = create_power_law_kernel(
            p=2.0,
            r0_km=1.0,
            lambda_km=50.0,
            max_radius_km=args.rmax,
            res_km=args.res,
        )
    convolve_dvnl(
        args.input,
        args.output,
        kernel,
        tile_size=args.tile_size,
    )
    return args.output


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output = run(args)
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"DVNL convolution complete: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
