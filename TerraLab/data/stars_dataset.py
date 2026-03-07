"""Runtime access for packaged stars dataset (ZST/ECSV/NPY/JSON -> NPZ in APPDATA)."""

from __future__ import annotations

import csv
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List

import numpy as np

try:
    import zstandard as zstd
except Exception:  # pragma: no cover
    zstd = None

try:
    from astropy.table import Table
except Exception:  # pragma: no cover
    Table = None

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None


APP_NAME = "TerraLab"
NPZ_NAME = "stars_catalog.npz"
NPY_NAME = "stars_catalog.npy"
ZST_NAME = "stars_catalog.zst"
JSON_FALLBACK_NAME = "gaia_stars.json"
SOURCE_LOG_NAME = "stars_dataset_source.log"

REQUIRED_COLS = ("ra", "dec", "phot_g_mean_mag")
OPTIONAL_COLS = ("bp_rp", "pmra", "pmdec", "parallax", "source_id")


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


def _runtime_npy_path() -> Path:
    return _runtime_data_dir() / NPY_NAME


def _runtime_source_log_path() -> Path:
    return _runtime_data_dir() / SOURCE_LOG_NAME


def _packaged_source_log_path() -> Path:
    return _packaged_stars_dir() / SOURCE_LOG_NAME


def _source_log(message: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
    print(f"[stars_dataset] {message}")
    paths = (_runtime_source_log_path(), _packaged_source_log_path())
    for p in paths:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            # Never break dataset loading because of telemetry logging.
            pass


def _packaged_stars_dir() -> Path:
    return Path(__file__).resolve().parent / "stars"


def _packaged_zst_path() -> Path:
    return _packaged_stars_dir() / ZST_NAME


def _packaged_npz_path() -> Path:
    return _packaged_stars_dir() / NPZ_NAME


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


def _to_float_array(values: np.ndarray | Iterable, dtype: np.dtype, default: float = np.nan) -> np.ndarray:
    arr = np.asarray(values)
    if arr.ndim == 0:
        arr = arr.reshape(1)
    arr = arr.reshape(-1)
    if arr.dtype.kind in ("f", "i", "u"):
        return arr.astype(dtype, copy=False)

    out = np.empty(len(arr), dtype=dtype)
    for i, v in enumerate(arr):
        try:
            if v is None or v == "":
                out[i] = default
            else:
                out[i] = float(v)
        except Exception:
            out[i] = default
    return out


def _to_int64_array(values: np.ndarray | Iterable, default: int = -1) -> np.ndarray:
    arr = np.asarray(values)
    if arr.ndim == 0:
        arr = arr.reshape(1)
    arr = arr.reshape(-1)
    if arr.dtype.kind in ("i", "u"):
        return arr.astype(np.int64, copy=False)

    out = np.empty(len(arr), dtype=np.int64)
    for i, v in enumerate(arr):
        try:
            if v is None or v == "":
                out[i] = default
            else:
                out[i] = int(v)
        except Exception:
            try:
                out[i] = int(float(v))
            except Exception:
                out[i] = default
    return out


def _normalize_arrays(raw: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    missing = [k for k in REQUIRED_COLS if k not in raw]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    ra = _to_float_array(raw["ra"], np.float64)
    dec = _to_float_array(raw["dec"], np.float64)
    mag = _to_float_array(raw["phot_g_mean_mag"], np.float32)

    n = min(len(ra), len(dec), len(mag))
    if n <= 0:
        return {
            "ra": np.empty(0, dtype=np.float64),
            "dec": np.empty(0, dtype=np.float64),
            "phot_g_mean_mag": np.empty(0, dtype=np.float32),
            "bp_rp": np.empty(0, dtype=np.float32),
        }

    ra = ra[:n]
    dec = dec[:n]
    mag = mag[:n]

    bp_raw = raw.get("bp_rp")
    if bp_raw is None:
        bp_rp = np.full(n, 0.8, dtype=np.float32)
    else:
        bp_rp = _to_float_array(bp_raw, np.float32)
        if len(bp_rp) < n:
            bp_rp = np.full(n, 0.8, dtype=np.float32)
        else:
            bp_rp = bp_rp[:n]
    bp_rp = np.nan_to_num(bp_rp, nan=0.8, posinf=2.5, neginf=-0.5).astype(np.float32, copy=False)

    valid = np.isfinite(ra) & np.isfinite(dec) & np.isfinite(mag)
    out: Dict[str, np.ndarray] = {
        "ra": ra[valid],
        "dec": dec[valid],
        "phot_g_mean_mag": mag[valid],
        "bp_rp": bp_rp[valid],
    }

    for key in ("pmra", "pmdec", "parallax"):
        arr = raw.get(key)
        if arr is None:
            continue
        arr_f = _to_float_array(arr, np.float32)
        if len(arr_f) < n:
            continue
        out[key] = arr_f[:n][valid]

    sid_raw = raw.get("source_id")
    if sid_raw is not None:
        sid = _to_int64_array(sid_raw)
        if len(sid) >= n:
            out["source_id"] = sid[:n][valid]

    return out


def _concat_chunks(chunks: List[Dict[str, np.ndarray]]) -> Dict[str, np.ndarray]:
    if not chunks:
        return {}
    all_keys = set()
    for chunk in chunks:
        all_keys.update(chunk.keys())
    out: Dict[str, np.ndarray] = {}
    for key in sorted(all_keys):
        arrays = [c[key] for c in chunks if key in c and len(c[key]) > 0]
        if arrays:
            out[key] = np.concatenate(arrays)
    return out


def _write_npz(npz_path: Path, arrays: Dict[str, np.ndarray]) -> None:
    npz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = npz_path.with_suffix(npz_path.suffix + ".tmp")
    with tmp_out.open("wb") as fh:
        np.savez(fh, **arrays)
    tmp_out.replace(npz_path)


def _write_structured_npy(npy_path: Path, arrays: Dict[str, np.ndarray]) -> None:
    n = len(arrays.get("ra", []))
    if n <= 0:
        raise ValueError("Cannot write empty structured NPY")

    dtype = np.dtype(
        [
            ("source_id", np.int64),
            ("ra", np.float64),
            ("dec", np.float64),
            ("phot_g_mean_mag", np.float32),
            ("bp_rp", np.float32),
            ("pmra", np.float32),
            ("pmdec", np.float32),
            ("parallax", np.float32),
        ]
    )
    structured = np.empty(n, dtype=dtype)
    structured["source_id"] = np.asarray(arrays.get("source_id", np.full(n, -1, dtype=np.int64)), dtype=np.int64)
    structured["ra"] = np.asarray(arrays["ra"], dtype=np.float64)
    structured["dec"] = np.asarray(arrays["dec"], dtype=np.float64)
    structured["phot_g_mean_mag"] = np.asarray(arrays["phot_g_mean_mag"], dtype=np.float32)
    structured["bp_rp"] = np.asarray(arrays.get("bp_rp", np.full(n, 0.8, dtype=np.float32)), dtype=np.float32)
    structured["pmra"] = np.asarray(arrays.get("pmra", np.full(n, np.nan, dtype=np.float32)), dtype=np.float32)
    structured["pmdec"] = np.asarray(arrays.get("pmdec", np.full(n, np.nan, dtype=np.float32)), dtype=np.float32)
    structured["parallax"] = np.asarray(arrays.get("parallax", np.full(n, np.nan, dtype=np.float32)), dtype=np.float32)

    npy_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = npy_path.with_suffix(npy_path.suffix + ".tmp")
    with tmp_out.open("wb") as fh:
        np.save(fh, structured, allow_pickle=False)
    tmp_out.replace(npy_path)


def _read_structured_npy(path: Path) -> Dict[str, np.ndarray] | None:
    try:
        arr = np.load(path, allow_pickle=False)
    except Exception:
        return None
    if not isinstance(arr, np.ndarray) or arr.dtype.names is None:
        return None

    names = set(arr.dtype.names)
    ra_key = "ra" if "ra" in names else ("RA" if "RA" in names else None)
    dec_key = "dec" if "dec" in names else ("DEC" if "DEC" in names else None)
    mag_key = "phot_g_mean_mag" if "phot_g_mean_mag" in names else ("mag" if "mag" in names else ("g_mag" if "g_mag" in names else None))
    if ra_key is None or dec_key is None or mag_key is None:
        return None

    raw: Dict[str, np.ndarray] = {
        "ra": np.asarray(arr[ra_key]),
        "dec": np.asarray(arr[dec_key]),
        "phot_g_mean_mag": np.asarray(arr[mag_key]),
    }
    for key in OPTIONAL_COLS:
        if key in names:
            raw[key] = np.asarray(arr[key])
    return raw


def _read_split_npy(stars_dir: Path) -> Dict[str, np.ndarray] | None:
    ra_path = stars_dir / "gaia_cache_ra.npy"
    dec_path = stars_dir / "gaia_cache_dec.npy"
    mag_path = stars_dir / "gaia_cache_mag.npy"
    if not (ra_path.exists() and dec_path.exists() and mag_path.exists()):
        return None

    raw: Dict[str, np.ndarray] = {
        "ra": np.load(ra_path, allow_pickle=False),
        "dec": np.load(dec_path, allow_pickle=False),
        "phot_g_mean_mag": np.load(mag_path, allow_pickle=False),
    }

    bprp_path = stars_dir / "gaia_cache_bprp.npy"
    if bprp_path.exists():
        raw["bp_rp"] = np.load(bprp_path, allow_pickle=False)

    sid_path = stars_dir / "gaia_cache_ids.npy"
    if sid_path.exists():
        try:
            raw["source_id"] = np.load(sid_path, allow_pickle=False)
        except Exception:
            raw["source_id"] = np.load(sid_path, allow_pickle=True)

    return raw


def _read_ecsv_columns(path: Path) -> Dict[str, np.ndarray]:
    if Table is not None:
        table = Table.read(path, format="ascii.ecsv")
        out: Dict[str, np.ndarray] = {}
        for key in REQUIRED_COLS + OPTIONAL_COLS:
            if key in table.colnames:
                out[key] = np.asarray(table[key])
        return out

    if pd is not None:
        df = pd.read_csv(path, comment="#", low_memory=False)
        out = {}
        for key in REQUIRED_COLS + OPTIONAL_COLS:
            if key in df.columns:
                out[key] = df[key].to_numpy()
        return out

    with path.open("r", encoding="utf-8", newline="") as fh:
        data_lines = (line for line in fh if not line.lstrip().startswith("#"))
        reader = csv.DictReader(data_lines)
        if reader.fieldnames is None:
            return {}
        available = set(reader.fieldnames)
        needed = [k for k in REQUIRED_COLS + OPTIONAL_COLS if k in available]
        cols = {k: [] for k in needed}
        for row in reader:
            for key in needed:
                cols[key].append(row.get(key))
    return {k: np.asarray(v) for k, v in cols.items()}


def _build_from_ecsv(stars_dir: Path, npz_path: Path) -> bool:
    ecsv_files = sorted(stars_dir.glob("*.ecsv"))
    if not ecsv_files:
        _source_log(f"source=ecsv miss no *.ecsv files in '{stars_dir}'")
        return False

    chunks: List[Dict[str, np.ndarray]] = []
    for path in ecsv_files:
        try:
            raw = _read_ecsv_columns(path)
            chunk = _normalize_arrays(raw)
            if len(chunk.get("ra", [])) > 0:
                chunks.append(chunk)
        except Exception as exc:
            print(f"[stars_dataset] Skip ECSV '{path.name}': {exc}")

    if not chunks:
        return False

    merged = _concat_chunks(chunks)
    if len(merged.get("ra", [])) <= 0:
        return False
    _write_npz(npz_path, merged)
    _source_log(
        f"source=ecsv stars_dir='{stars_dir}' -> runtime_npz='{npz_path}' "
        f"rows={len(merged['ra'])}"
    )
    return True


def _read_gaia_json(stars_dir: Path) -> Dict[str, np.ndarray] | None:
    json_path = stars_dir / JSON_FALLBACK_NAME
    if not json_path.exists():
        _source_log(f"source=json miss file not found '{json_path}'")
        return None

    with json_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)

    rows = []
    names: List[str] = []
    if isinstance(payload, dict):
        rows = payload.get("data") or []
        metadata = payload.get("metadata") or []
        for item in metadata:
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    names.append(str(name))
    elif isinstance(payload, list):
        rows = payload

    if not rows:
        _source_log(f"source=json miss empty data in '{json_path}'")
        return None

    if isinstance(rows[0], dict):
        out: Dict[str, np.ndarray] = {}
        for key in REQUIRED_COLS + OPTIONAL_COLS:
            out[key] = np.asarray([row.get(key) for row in rows])
        return out

    if not names and isinstance(rows[0], (list, tuple)):
        default = (
            "source_id",
            "designation",
            "ra",
            "dec",
            "phot_g_mean_mag",
            "phot_bp_mean_mag",
            "phot_rp_mean_mag",
            "bp_rp",
            "pmra",
            "pmdec",
            "parallax",
        )
        names = list(default[: len(rows[0])])

    if not names:
        _source_log(f"source=json miss no column metadata in '{json_path}'")
        return None

    idx = {name: i for i, name in enumerate(names)}
    out = {}
    for key in REQUIRED_COLS + OPTIONAL_COLS:
        pos = idx.get(key)
        if pos is None:
            continue
        out[key] = np.asarray(
            [
                row[pos] if isinstance(row, (list, tuple)) and pos < len(row) else np.nan
                for row in rows
            ],
            dtype=object,
        )
    return out


def _build_from_json(stars_dir: Path, npz_path: Path, npy_path: Path) -> bool:
    raw = _read_gaia_json(stars_dir)
    if raw is None:
        return False
    arrays = _normalize_arrays(raw)
    if len(arrays.get("ra", [])) <= 0:
        return False
    _write_structured_npy(npy_path, arrays)
    _write_npz(npz_path, arrays)
    _source_log(
        f"source=json fallback='{JSON_FALLBACK_NAME}' stars_dir='{stars_dir}' "
        f"-> runtime_npy='{npy_path}' runtime_npz='{npz_path}' rows={len(arrays['ra'])}"
    )
    return True


def _build_from_npy_sources(stars_dir: Path, npz_path: Path, runtime_npy_path: Path) -> bool:
    npy_candidates = [
        runtime_npy_path,
        stars_dir / NPY_NAME,
        stars_dir / "gaia_stars.npy",
        stars_dir / "gaia_stars_fallback.npy",
    ]
    for path in npy_candidates:
        if not path.exists():
            continue
        raw = _read_structured_npy(path)
        if raw is None:
            _source_log(f"source=npy miss unsupported/invalid structured npy '{path}'")
            continue
        arrays = _normalize_arrays(raw)
        if len(arrays.get("ra", [])) <= 0:
            continue
        _write_structured_npy(runtime_npy_path, arrays)
        _write_npz(npz_path, arrays)
        _source_log(
            f"source=npy path='{path}' -> runtime_npy='{runtime_npy_path}' "
            f"runtime_npz='{npz_path}' rows={len(arrays['ra'])}"
        )
        return True

    try:
        split_raw = _read_split_npy(stars_dir)
    except Exception as exc:
        print(f"[stars_dataset] Split NPY read error: {exc}")
        _source_log(f"source=split_npy failed error='{exc}'")
        split_raw = None
    if split_raw is None:
        _source_log(f"source=npy miss no structured/split npy source in '{stars_dir}'")
        return False

    arrays = _normalize_arrays(split_raw)
    if len(arrays.get("ra", [])) <= 0:
        return False
    _write_structured_npy(runtime_npy_path, arrays)
    _write_npz(npz_path, arrays)
    _source_log(
        f"source=split_npy stars_dir='{stars_dir}' -> runtime_npy='{runtime_npy_path}' "
        f"runtime_npz='{npz_path}' rows={len(arrays['ra'])}"
    )
    return True


def ensure_stars_dataset() -> str:
    """Ensure `%APPDATA%/TerraLab/data/stars_catalog.npz` exists and return it."""
    npz_path = _runtime_npz_path()
    if npz_path.exists() and npz_path.is_file():
        _source_log(f"source=runtime_cache runtime_npz='{npz_path}'")
        return str(npz_path)

    runtime_npy_path = _runtime_npy_path()
    stars_dir = _packaged_stars_dir()

    zst_path = _packaged_zst_path()
    if zst_path.exists():
        try:
            _decompress_zst_to_npz(zst_path, npz_path)
            _source_log(f"source=zst path='{zst_path}' -> runtime_npz='{npz_path}'")
            return str(npz_path)
        except Exception as exc:
            # Continue to local fallbacks (NPY/ECSV/JSON) if ZST path is unusable.
            _source_log(f"source=zst failed path='{zst_path}' error='{exc}' -> trying fallbacks")
    else:
        _source_log(f"source=zst miss file not found '{zst_path}'")

    packaged_npz = _packaged_npz_path()
    if packaged_npz.exists():
        npz_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(packaged_npz, npz_path)
        _source_log(f"source=packaged_npz path='{packaged_npz}' -> runtime_npz='{npz_path}'")
        return str(npz_path)
    _source_log(f"source=packaged_npz miss file not found '{packaged_npz}'")

    if _build_from_npy_sources(stars_dir, npz_path, runtime_npy_path):
        return str(npz_path)

    if _build_from_ecsv(stars_dir, npz_path):
        return str(npz_path)

    if _build_from_json(stars_dir, npz_path, runtime_npy_path):
        return str(npz_path)

    _source_log(f"source=none no dataset source found in stars_dir='{stars_dir}'")
    raise FileNotFoundError(
        "No stars dataset source found. Expected one of: "
        f"{ZST_NAME}, {NPY_NAME}/gaia_cache_*.npy, *.ecsv, {JSON_FALLBACK_NAME} "
        f"in '{stars_dir}'."
    )


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
