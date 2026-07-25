"""Gaia tiled-catalog download command."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from TerraLab.common.data_library import DataLibrary
from TerraLab.data.gaia_downloader import (
    DEFAULT_VISIBLE_MAG_LIMIT,
    GaiaTileDownloader,
    GaiaTileDownloaderConfig,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Gaia catalogue data in 5° × 5° tiles."
    )
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--mag-limit", type=float, default=0.0)
    parser.add_argument(
        "--visible-mag-limit",
        type=float,
        default=DEFAULT_VISIBLE_MAG_LIMIT,
    )
    parser.add_argument("--tile-size-deg", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--maxrec", type=int, default=-1)
    parser.add_argument("--max-concurrent-requests", type=int, default=2)
    parser.add_argument("--request-retries", type=int, default=3)
    parser.add_argument("--retry-backoff-s", type=float, default=1.5)
    parser.add_argument("--state-file", default="")
    parser.add_argument("--no-resume", action="store_true")
    return parser


def _output_dir(value: str) -> Path:
    if str(value).strip():
        return Path(value).expanduser().resolve()
    layout = DataLibrary.current(require_configured=True).layout(create=True)
    return Path(layout["data_gaia"]).resolve()


def run(args: argparse.Namespace) -> dict:
    state_file = (
        Path(args.state_file).expanduser().resolve()
        if str(args.state_file).strip()
        else None
    )
    config = GaiaTileDownloaderConfig(
        output_dir=_output_dir(args.output_dir),
        mag_limit=float(args.mag_limit),
        visible_mag_limit=float(args.visible_mag_limit),
        tile_size_deg=float(args.tile_size_deg),
        timeout_s=float(args.timeout),
        maxrec=int(args.maxrec),
        state_file=state_file,
        max_concurrent_requests=int(args.max_concurrent_requests),
        request_retries=int(args.request_retries),
        retry_backoff_s=float(args.retry_backoff_s),
    )
    return GaiaTileDownloader(config).download(
        resume=not bool(args.no_resume)
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = run(args)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
