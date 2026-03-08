"""Build unified stars dataset from ECSV into distribution ZST.

Usage:
    python scripts/build_stars_dataset.py
"""

from __future__ import annotations

import argparse
import json
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
JSON_SUPPLEMENT_PATH = Path(__file__).resolve().parents[1] / "TerraLab" / "data" / "stars" / "gaia_stars.json"


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


def _read_named_star_json_supplement(path: Path) -> Dict[str, np.ndarray] | None:
    # Temporary patch:
    # gaia_stars.json is used to backfill missing bright/named stars while the ECSV
    # source is incomplete. This must remain optional: if the JSON disappears later,
    # the build must continue from ECSV only without crashing.
    if not path.exists():
        return None

    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)

    rows = []
    names: List[str] = []
    if isinstance(payload, dict):
        rows = payload.get("data") or []
        metadata = payload.get("metadata") or []
        for item in metadata:
            if isinstance(item, dict):
                names.append(str(item.get("name", "")))
    elif isinstance(payload, list):
        rows = payload

    if not rows:
        return None

    if isinstance(rows[0], dict):
        raw: Dict[str, np.ndarray] = {}
        for key in REQUIRED_COLS + OPTIONAL_COLS:
            raw[key] = np.asarray([row.get(key) for row in rows], dtype=object)
        return normalize_arrays(raw)

    if not names:
        return None

    idx = {name: i for i, name in enumerate(names)}
    raw = {}
    for key in REQUIRED_COLS + OPTIONAL_COLS:
        pos = idx.get(key)
        if pos is None:
            continue
        raw[key] = np.asarray(
            [
                row[pos] if isinstance(row, (list, tuple)) and pos < len(row) else np.nan
                for row in rows
            ],
            dtype=object,
        )
    if not raw:
        return None
    return normalize_arrays(raw)


def _dedupe_by_source_id_first(arrays: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    source_id = arrays.get("source_id")
    if source_id is None:
        return arrays

    sid = np.asarray(source_id, dtype=np.int64)
    n = len(sid)
    if n <= 1:
        return arrays

    valid_mask = sid >= 0
    if not np.any(valid_mask):
        return arrays

    valid_idx = np.where(valid_mask)[0]
    valid_sid = sid[valid_mask]
    _, first_pos = np.unique(valid_sid, return_index=True)
    keep_valid_idx = valid_idx[first_pos]
    invalid_idx = np.where(~valid_mask)[0]
    keep_idx = np.sort(np.concatenate((keep_valid_idx, invalid_idx))).astype(np.int64, copy=False)

    if len(keep_idx) == n:
        return arrays

    out: Dict[str, np.ndarray] = {}
    for key, arr in arrays.items():
        arr_np = np.asarray(arr)
        if len(arr_np) == n:
            out[key] = arr_np[keep_idx]
        else:
            out[key] = arr_np
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

    print("[build_stars_dataset] Stage: load optional named-star supplement")
    t_stage = time.time()
    try:
        supplement = _read_named_star_json_supplement(JSON_SUPPLEMENT_PATH)
    except Exception as exc:
        supplement = None
        print(f"[build_stars_dataset] Named-star supplement skipped: {exc}")
    if supplement is not None and len(supplement.get("ra", [])) > 0:
        chunks.append(supplement)
        print(
            "[build_stars_dataset] Temporary named-star supplement appended: "
            f"{len(supplement['ra'])} rows from {JSON_SUPPLEMENT_PATH.name}"
        )
    print(f"[build_stars_dataset] Stage done: supplement ({time.time()-t_stage:.2f}s)")

    print(
        "[build_stars_dataset] Stage: concatenate chunks "
        f"({len(chunks)} chunk(s), approx_rows={sum(len(c.get('ra', [])) for c in chunks)})"
    )
    t_stage = time.time()
    merged = concat_chunks(chunks)
    print(
        "[build_stars_dataset] Stage done: concatenate "
        f"({len(merged.get('ra', []))} rows, {time.time()-t_stage:.2f}s)"
    )

    merged_before_dedupe = len(merged.get("ra", []))
    if supplement is not None and len(supplement.get("ra", [])) > 0:
        print("[build_stars_dataset] Stage: deduplicate by source_id")
        t_stage = time.time()
        merged = _dedupe_by_source_id_first(merged)
        print(f"[build_stars_dataset] Stage done: deduplicate ({time.time()-t_stage:.2f}s)")
    merged_rows = len(merged.get("ra", []))
    if merged_rows == 0:
        raise SystemExit("No rows after normalization")
    if merged_rows != merged_before_dedupe:
        print(
            "[build_stars_dataset] Deduplicated by source_id: "
            f"{merged_before_dedupe - merged_rows} duplicate row(s) removed"
        )

    temp_npz = stars_dir / "stars_catalog.tmp.npz"
    final_zst = stars_dir / "stars_catalog.zst"

    # Dtype policy:
    # - ra/dec float64 (projection precision)
    # - phot_g_mean_mag/bp_rp/pm*/parallax float32 (compact)
    # - source_id int64
    print(f"[build_stars_dataset] Stage: write temporary NPZ -> {temp_npz}")
    t_stage = time.time()
    np.savez(
        temp_npz,
        **merged,
    )
    print(
        f"[build_stars_dataset] Temporary NPZ written: {temp_npz} "
        f"({temp_npz.stat().st_size / (1024*1024):.1f} MiB, {time.time()-t_stage:.2f}s)"
    )

    print(f"[build_stars_dataset] Stage: compress NPZ -> ZST -> {final_zst}")
    t_stage = time.time()
    compress_npz_to_zst(temp_npz, final_zst, level=int(args.zstd_level))
    print(
        f"[build_stars_dataset] ZST written: {final_zst} "
        f"({final_zst.stat().st_size / (1024*1024):.1f} MiB, {time.time()-t_stage:.2f}s)"
    )

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
