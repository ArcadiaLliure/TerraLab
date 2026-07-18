from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from TerraLab.terrain.light_pollution_sampler import LightPollutionSampler


RESULT_PREFIX = "TERRALAB_LP_RESULT="


def estimate_light_pollution(
    raster_path: str, lat: float, lon: float
) -> tuple[float, int]:
    path = os.path.abspath(os.path.expanduser(str(raster_path)))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Light-pollution raster not found: {path}")

    sampler = LightPollutionSampler(path)
    try:
        return sampler.estimate_zenith_sqm(float(lat), float(lon))
    finally:
        sampler.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Estimate zenith SQM and Bortle from a light raster."
    )
    parser.add_argument("--raster", required=True)
    parser.add_argument("--lat", required=True, type=float)
    parser.add_argument("--lon", required=True, type=float)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        sqm, bortle = estimate_light_pollution(
            args.raster,
            args.lat,
            args.lon,
        )
    except Exception as exc:
        print(
            f"[LPSampler:Process] {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    payload = {
        "sqm": float(sqm),
        "bortle": int(max(1, min(9, int(bortle)))),
    }
    print(
        f"{RESULT_PREFIX}{json.dumps(payload, separators=(',', ':'))}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
