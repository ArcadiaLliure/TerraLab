"""Gaia table importer: stack ECSV/CSV files and build TerraLab runtime artifacts."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

import numpy as np

from TerraLab.data.stars_dataset import (
    OPTIONAL_COLS,
    REQUIRED_COLS,
    _concat_chunks,
    _normalize_arrays,
    _read_ecsv_columns,
    _write_npz,
    _write_structured_npy,
)

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None

try:
    import zstandard as zstd
except Exception:  # pragma: no cover
    zstd = None


ProgressFn = Callable[[float, str], None]


_ALIASES = {
    "ra": ("ra", "raj2000", "ra_deg"),
    "dec": ("dec", "dej2000", "dec_deg"),
    "phot_g_mean_mag": ("phot_g_mean_mag", "mag", "g_mag", "phot_g"),
    "bp_rp": ("bp_rp", "bprp", "bp-rp", "bp_rp_color"),
    "pmra": ("pmra",),
    "pmdec": ("pmdec",),
    "parallax": ("parallax",),
    "source_id": ("source_id", "sourceid", "id"),
}


def _progress(callback: Optional[ProgressFn], percent: float, message: str) -> None:
    if callback is None:
        return
    try:
        callback(float(percent), str(message))
    except Exception:
        pass


def _normalized_field_map(headers: Iterable[str]) -> Dict[str, str]:
    by_norm: Dict[str, str] = {}
    for h in headers:
        key = str(h)
        norm = key.strip().lower().replace(" ", "_")
        by_norm[norm] = key
    return by_norm


def _extract_with_aliases(rows: Dict[str, np.ndarray], headers: Iterable[str]) -> Dict[str, np.ndarray]:
    out: Dict[str, np.ndarray] = {}
    by_norm = _normalized_field_map(headers)
    for target, aliases in _ALIASES.items():
        for alias in aliases:
            src = by_norm.get(alias.lower())
            if src and src in rows:
                out[target] = np.asarray(rows[src])
                break
            # Direct key match for already-normalized readers.
            if alias in rows:
                out[target] = np.asarray(rows[alias])
                break
    return out


def _read_csv_columns(path: Path) -> Dict[str, np.ndarray]:
    if pd is not None:
        df = pd.read_csv(path, low_memory=False)
        raw = {str(col): df[col].to_numpy() for col in df.columns}
        return _extract_with_aliases(raw, df.columns)

    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return {}
        fieldnames = [str(x) for x in reader.fieldnames]
        cols: Dict[str, List[object]] = {name: [] for name in fieldnames}
        for row in reader:
            for name in fieldnames:
                cols[name].append(row.get(name))
    raw = {name: np.asarray(values, dtype=object) for name, values in cols.items()}
    return _extract_with_aliases(raw, fieldnames)


def _read_input_table(path: Path) -> Dict[str, np.ndarray]:
    suffix = path.suffix.lower()
    if suffix == ".ecsv":
        return _read_ecsv_columns(path)
    if suffix == ".csv":
        return _read_csv_columns(path)
    return {}


def _write_zst_from_npz(npz_path: Path, zst_path: Path, level: int = 12) -> bool:
    if zstd is None:
        return False
    raw = npz_path.read_bytes()
    compressor = zstd.ZstdCompressor(level=int(level))
    packed = compressor.compress(raw)
    zst_path.parent.mkdir(parents=True, exist_ok=True)
    zst_path.write_bytes(packed)
    return True


def build_gaia_catalog_from_tables(
    input_paths: Iterable[str],
    output_dir: str,
    *,
    output_basename: str = "stars_catalog",
    zst_level: int = 12,
    progress_callback: Optional[ProgressFn] = None,
) -> Dict[str, object]:
    paths = [Path(p) for p in input_paths if str(p).strip()]
    if not paths:
        raise ValueError("No input files provided for Gaia importer.")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    npz_path = out_dir / f"{output_basename}.npz"
    npy_path = out_dir / f"{output_basename}.npy"
    zst_path = out_dir / f"{output_basename}.zst"

    chunks: List[Dict[str, np.ndarray]] = []
    total = max(1, len(paths))
    _progress(progress_callback, 2.0, "Llegint fitxers Gaia...")
    for idx, path in enumerate(paths):
        if not path.exists():
            continue
        raw = _read_input_table(path)
        # Support pre-normalized keys where reader already returns canonical fields.
        if raw and all(k in raw for k in REQUIRED_COLS):
            canonical = {k: np.asarray(raw[k]) for k in REQUIRED_COLS + OPTIONAL_COLS if k in raw}
        else:
            canonical = raw
        if not canonical:
            continue
        chunk = _normalize_arrays(canonical)
        if len(chunk.get("ra", [])) > 0:
            chunks.append(chunk)
        pct = 2.0 + (70.0 * float(idx + 1) / float(total))
        _progress(progress_callback, pct, f"Processant {path.name} ({idx+1}/{total})")

    if not chunks:
        raise ValueError(
            "No s'ha pogut construir el catàleg Gaia. "
            "Formats admesos: ECSV (.ecsv) i CSV (.csv) amb columnes Gaia estàndard."
        )

    merged = _concat_chunks(chunks)
    if len(merged.get("ra", [])) <= 0:
        raise ValueError("El catàleg Gaia resultant és buit després de la normalització.")

    _progress(progress_callback, 80.0, "Generant NPZ runtime...")
    _write_npz(npz_path, merged)
    _progress(progress_callback, 88.0, "Generant NPY runtime...")
    _write_structured_npy(npy_path, merged)

    _progress(progress_callback, 94.0, "Comprimint ZST...")
    zst_written = _write_zst_from_npz(npz_path, zst_path, level=int(zst_level))
    _progress(progress_callback, 100.0, "Catàleg Gaia preparat.")

    return {
        "rows": int(len(merged["ra"])),
        "output_npz": str(npz_path),
        "output_npy": str(npy_path),
        "output_zst": str(zst_path) if zst_written else None,
        "zst_written": bool(zst_written),
    }

