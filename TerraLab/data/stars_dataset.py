"""Runtime access for packaged stars dataset (ZST -> NPZ in APPDATA)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict

import numpy as np

try:
    import zstandard as zstd
except Exception:  # pragma: no cover
    zstd = None


APP_NAME = "TerraLab"
NPZ_NAME = "stars_catalog.npz"
ZST_NAME = "stars_catalog.zst"


def _appdata_root() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata)
    # Fallback for non-Windows local runs.
    return Path.home() / "AppData" / "Roaming"


def _runtime_data_dir() -> Path:
    return _appdata_root() / APP_NAME / "data"


def _runtime_npz_path() -> Path:
    return _runtime_data_dir() / NPZ_NAME


def _packaged_zst_path() -> Path:
    return Path(__file__).resolve().parent / "stars" / ZST_NAME


def _decompress_zst_to_npz(zst_path: Path, npz_path: Path) -> None:
    if zstd is None:
        raise RuntimeError(
            "zstandard is required to unpack stars_catalog.zst. Install with: pip install zstandard"
        )

    npz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = npz_path.with_suffix(npz_path.suffix + ".tmp")

    dctx = zstd.ZstdDecompressor()
    with zst_path.open("rb") as src, tmp_out.open("wb") as dst:
        with dctx.stream_reader(src) as reader:
            shutil.copyfileobj(reader, dst, length=1024 * 1024)

    tmp_out.replace(npz_path)


def ensure_stars_dataset() -> str:
    """Ensure `%APPDATA%/TerraLab/data/stars_catalog.npz` exists and return it."""
    npz_path = _runtime_npz_path()
    if npz_path.exists() and npz_path.is_file():
        return str(npz_path)

    zst_path = _packaged_zst_path()
    if not zst_path.exists():
        raise FileNotFoundError(f"Packaged stars dataset not found: {zst_path}")

    _decompress_zst_to_npz(zst_path, npz_path)
    return str(npz_path)


def _pick_first(npz_obj, keys):
    for key in keys:
        if key in npz_obj:
            return npz_obj[key]
    return None


def load_stars_dataset(npz_path: str | None = None) -> Dict[str, np.ndarray]:
    """Load stars dataset arrays from ensured runtime NPZ."""
    path = Path(npz_path) if npz_path else Path(ensure_stars_dataset())
    with np.load(path, allow_pickle=False) as data:
        ra_raw = _pick_first(data, ("ra", "RA"))
        dec_raw = _pick_first(data, ("dec", "DEC"))
        mag_raw = _pick_first(data, ("phot_g_mean_mag", "mag", "g_mag"))
        bp_rp_raw = _pick_first(data, ("bp_rp", "bprp"))

        if ra_raw is None or dec_raw is None or mag_raw is None:
            raise ValueError(f"Dataset NPZ missing required arrays (ra/dec/mag): {path}")

        ra = np.asarray(ra_raw, dtype=np.float64)
        dec = np.asarray(dec_raw, dtype=np.float64)
        mag = np.asarray(mag_raw, dtype=np.float32)

        if bp_rp_raw is None:
            bp_rp = np.full(len(mag), 0.8, dtype=np.float32)
        else:
            bp_rp = np.asarray(bp_rp_raw, dtype=np.float32)
            if len(bp_rp) != len(mag):
                bp_rp = np.full(len(mag), 0.8, dtype=np.float32)
        bp_rp = np.nan_to_num(bp_rp, nan=0.8, posinf=2.5, neginf=-0.5)

        out: Dict[str, np.ndarray] = {
            "ra": ra,
            "dec": dec,
            "phot_g_mean_mag": mag,
            "bp_rp": bp_rp,
        }

        for optional_name in ("pmra", "pmdec", "parallax", "source_id"):
            if optional_name in data:
                out[optional_name] = np.asarray(data[optional_name])

    valid = np.isfinite(out["ra"]) & np.isfinite(out["dec"]) & np.isfinite(out["phot_g_mean_mag"])
    if not np.all(valid):
        for key, arr in list(out.items()):
            if hasattr(arr, "__len__") and len(arr) == len(valid):
                out[key] = arr[valid]

    return out
