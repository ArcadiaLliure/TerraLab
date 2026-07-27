"""Command-line adapter for point elevation queries."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Sequence

from TerraLab.common.utils import get_config_value
from TerraLab.terrain.services.elevation_query import (
    DEFAULT_EYE_HEIGHT_M,
    ElevationQueryResult,
    query_elevation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query a WGS84 point using a TerraLab DEM provider."
    )
    parser.add_argument("latitude", type=float, help="WGS84 latitude in degrees")
    parser.add_argument("longitude", type=float, help="WGS84 longitude in degrees")
    parser.add_argument(
        "--dem-path",
        "--dem",
        dest="dem_path",
        help="DEM file or directory; defaults to TerraLab's configured raster_path",
    )
    parser.add_argument("--observer-offset-m", type=float, default=0.0)
    parser.add_argument("--eye-height-m", type=float, default=DEFAULT_EYE_HEIGHT_M)
    parser.add_argument("--json", action="store_true", help="Write machine-readable JSON")
    parser.add_argument(
        "--precision",
        type=int,
        default=3,
        choices=range(10),
        metavar="0..9",
    )
    return parser


def _configured_dem_path(cli_value: str | None) -> str:
    path = str(cli_value or "").strip()
    if not path:
        path = str(get_config_value("raster_path", "") or "").strip()
    if not path:
        raise FileNotFoundError(
            "TerraLab has no configured raster_path; pass --dem-path"
        )
    return path


def run(args: argparse.Namespace) -> ElevationQueryResult:
    return query_elevation(
        args.latitude,
        args.longitude,
        dem_path=_configured_dem_path(args.dem_path),
        observer_offset_m=args.observer_offset_m,
        eye_height_m=args.eye_height_m,
    )


def _print_human(result: ElevationQueryResult, precision: int) -> None:
    number = f"{{:.{int(precision)}f}}"
    print(f"GPS WGS84: {result.latitude_deg:.9f}, {result.longitude_deg:.9f}")
    print(
        f"Coordinates {result.internal_crs}: "
        f"X={result.projected_x_m:.3f} m, Y={result.projected_y_m:.3f} m"
    )
    print(f"DEM: {result.dem_path}")
    source = f", source_id={result.source_id}" if result.source_id else ""
    print(f"Provider: {result.provider}{source}")
    if result.nominal_resolution_m is not None:
        print(f"Nominal resolution: {number.format(result.nominal_resolution_m)} m")
    if not result.has_coverage:
        print("Elevation: no valid DEM coverage")
        return
    print(f"Terrain elevation: {number.format(result.elevation_m)} m")
    print(
        "Observer elevation with offset: "
        f"{number.format(result.observer_ground_elevation_m)} m"
    )
    print(
        "Raycast eye elevation: "
        f"{number.format(result.observer_eye_elevation_m)} m"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output_context = (
            contextlib.redirect_stdout(sys.stderr)
            if args.json
            else contextlib.nullcontext()
        )
        with output_context:
            result = run(args)
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    except Exception as exc:
        parser.exit(1, f"error querying DEM: {exc}\n")
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_human(result, args.precision)
    return 0 if result.has_coverage else 3


if __name__ == "__main__":
    raise SystemExit(main())
