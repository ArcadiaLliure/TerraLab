"""Build unified stars dataset from ECSV into distribution ZST.

Usage:
    python scripts/build_stars_dataset.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
from pathlib import Path
from typing import Dict, List

import numpy as np

try:
    from astropy.table import Table
except Exception:  # pragma: no cover
    Table = None

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None

try:
    import zstandard as zstd
except Exception:  # pragma: no cover
    zstd = None


REQUIRED_COLS = ("ra", "dec", "phot_g_mean_mag", "bp_rp")
OPTIONAL_COLS = ("pmra", "pmdec", "parallax", "source_id")


def _read_with_astropy(path: Path) -> Dict[str, np.ndarray]:
    if Table is None:
        return {}

    table = Table.read(path, format="ascii.ecsv")
    available = set(table.colnames)
    needed = [c for c in REQUIRED_COLS + OPTIONAL_COLS if c in available]
    if not all(c in available for c in REQUIRED_COLS):
        missing = [c for c in REQUIRED_COLS if c not in available]
        raise ValueError(f"Missing required columns in {path.name}: {missing}")

    out: Dict[str, np.ndarray] = {}
    for col in needed:
        out[col] = np.asarray(table[col])
    return out


def _read_with_pandas(path: Path) -> Dict[str, np.ndarray]:
    if pd is None:
        raise RuntimeError("pandas is required for fallback ECSV parsing")

    df = pd.read_csv(path, comment="#", low_memory=False)
    available = set(df.columns)
    if not all(c in available for c in REQUIRED_COLS):
        missing = [c for c in REQUIRED_COLS if c not in available]
        raise ValueError(f"Missing required columns in {path.name}: {missing}")

    out: Dict[str, np.ndarray] = {}
    for col in REQUIRED_COLS + OPTIONAL_COLS:
        if col in available:
            out[col] = pd.to_numeric(df[col], errors="coerce").to_numpy()
    return out


def read_ecsv_columns(path: Path) -> Dict[str, np.ndarray]:
    if Table is not None:
        return _read_with_astropy(path)
    return _read_with_pandas(path)


def normalize_arrays(raw: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    ra = np.asarray(raw["ra"], dtype=np.float64)
    dec = np.asarray(raw["dec"], dtype=np.float64)
    mag = np.asarray(raw["phot_g_mean_mag"], dtype=np.float32)
    bp_rp = np.asarray(raw.get("bp_rp", np.full(len(mag), 0.8, dtype=np.float32)), dtype=np.float32)

    if len(bp_rp) != len(mag):
        bp_rp = np.full(len(mag), 0.8, dtype=np.float32)
    bp_rp = np.nan_to_num(bp_rp, nan=0.8, posinf=2.5, neginf=-0.5)

    valid = np.isfinite(ra) & np.isfinite(dec) & np.isfinite(mag)
    out: Dict[str, np.ndarray] = {
        "ra": ra[valid],
        "dec": dec[valid],
        "phot_g_mean_mag": mag[valid],
        "bp_rp": bp_rp[valid],
    }

    for key in OPTIONAL_COLS:
        arr = raw.get(key)
        if arr is None:
            continue
        arr_np = np.asarray(arr)
        if len(arr_np) != len(valid):
            continue
        if key == "source_id":
            out[key] = np.nan_to_num(arr_np, nan=-1).astype(np.int64, copy=False)[valid]
        else:
            out[key] = np.asarray(arr_np, dtype=np.float32)[valid]

    return out


def concat_chunks(chunks: List[Dict[str, np.ndarray]]) -> Dict[str, np.ndarray]:
    keys = set()
    for c in chunks:
        keys.update(c.keys())

    out: Dict[str, np.ndarray] = {}
    total_rows = len(chunks[0]["ra"]) if chunks else 0
    for key in sorted(keys):
        arrays = [c[key] for c in chunks if key in c and len(c[key]) > 0]
        if not arrays:
            continue
        out[key] = np.concatenate(arrays)
    return out


def compress_npz_to_zst(npz_path: Path, zst_path: Path, level: int) -> None:
    if zstd is None:
        raise RuntimeError("zstandard is required to create .zst dataset. Install with: pip install zstandard")

    cctx = zstd.ZstdCompressor(level=level)
    with npz_path.open("rb") as src, zst_path.open("wb") as dst:
        with cctx.stream_writer(dst) as compressor:
            shutil.copyfileobj(src, compressor, length=1024 * 1024)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build TerraLab stars dataset ECSV -> ZST")
    parser.add_argument(
        "--stars-dir",
        default=str(Path(__file__).resolve().parents[1] / "gaia_stars" / "ecsv"),
        help="Directory containing *.ecsv files",
    )
    parser.add_argument("--zstd-level", type=int, default=12)
    args = parser.parse_args()

    stars_dir = Path(args.stars_dir)
    if not stars_dir.exists():
        raise SystemExit(f"Directory not found: {stars_dir}")

    ecsv_files = sorted(stars_dir.glob("*.ecsv"))
    if not ecsv_files:
        raise SystemExit(f"No ECSV files found in: {stars_dir}")

    t0 = time.time()
    chunks: List[Dict[str, np.ndarray]] = []
    total_rows = 0

    print(f"[build_stars_dataset] Reading {len(ecsv_files)} ECSV file(s) from {stars_dir}")
    for path in ecsv_files:
        t_file = time.time()
        raw = read_ecsv_columns(path)
        normalized = normalize_arrays(raw)
        n = len(normalized["ra"])
        total_rows += n
        chunks.append(normalized)
        print(f"  - {path.name}: {n} rows ({time.time()-t_file:.2f}s)")

    merged = concat_chunks(chunks)
    merged_rows = len(merged.get("ra", []))
    if merged_rows == 0:
        raise SystemExit("No rows after normalization")

    temp_npz = stars_dir / "stars_catalog.tmp.npz"
    final_zst = stars_dir / "stars_catalog.zst"

    # Dtype policy:
    # - ra/dec float64 (projection precision)
    # - phot_g_mean_mag/bp_rp/pm*/parallax float32 (compact)
    # - source_id int64
    np.savez(
        temp_npz,
        **merged,
    )
    print(f"[build_stars_dataset] Temporary NPZ written: {temp_npz} ({temp_npz.stat().st_size / (1024*1024):.1f} MiB)")

    compress_npz_to_zst(temp_npz, final_zst, level=int(args.zstd_level))
    print(f"[build_stars_dataset] ZST written: {final_zst} ({final_zst.stat().st_size / (1024*1024):.1f} MiB)")

    try:
        temp_npz.unlink()
    except Exception:
        pass

    dt = time.time() - t0
    print(
        f"[build_stars_dataset] DONE rows={merged_rows} files={len(ecsv_files)} "
        f"time={dt:.2f}s zstd_level={args.zstd_level}"
    )


if __name__ == "__main__":
    main()
