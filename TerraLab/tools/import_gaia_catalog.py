"""CLI entrypoint for Gaia ECSV/CSV stacking and runtime artifact generation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as a standalone script: `python tools/import_gaia_catalog.py ...`
if __package__ in (None, ""):
    _repo_root = Path(__file__).resolve().parents[2]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))

from TerraLab.common.app_paths import ensure_runtime_layout
from TerraLab.util.gaia_importer import build_gaia_catalog_from_tables


def _default_output_dir() -> str:
    layout = ensure_runtime_layout()
    return str(Path(layout["data_gaia"]).resolve())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stack Gaia ECSV/CSV/ZST files and build runtime artifacts"
    )
    parser.add_argument("inputs", nargs="+", help="Input Gaia tables (.ecsv, .csv or .zst)")
    parser.add_argument("--output-dir", default=_default_output_dir(), help="Destination folder")
    parser.add_argument("--basename", default="stars_catalog", help="Output base name")
    parser.add_argument("--zst-level", type=int, default=12, help="Zstandard level")
    parser.add_argument("--write-npz", dest="write_npz", action="store_true", default=True)
    parser.add_argument("--no-write-npz", dest="write_npz", action="store_false")
    parser.add_argument("--write-npy", dest="write_npy", action="store_true", default=False)
    parser.add_argument("--no-write-npy", dest="write_npy", action="store_false")
    parser.add_argument("--write-zst", dest="write_zst", action="store_true", default=True)
    parser.add_argument("--no-write-zst", dest="write_zst", action="store_false")
    args = parser.parse_args()

    def _progress(percent: float, message: str) -> None:
        print(f"[gaia-import] {percent:5.1f}% {message}")

    summary = build_gaia_catalog_from_tables(
        args.inputs,
        args.output_dir,
        output_basename=args.basename,
        zst_level=args.zst_level,
        write_npz=bool(args.write_npz),
        write_npy=bool(args.write_npy),
        write_zst=bool(args.write_zst),
        progress_callback=_progress,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
