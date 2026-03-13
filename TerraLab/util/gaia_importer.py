"""Gaia table importer: stack ECSV/CSV files and build TerraLab runtime artifacts."""

from __future__ import annotations

import io
import csv
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional

import numpy as np

from TerraLab.data.stars_dataset import (
    OPTIONAL_COLS,
    REQUIRED_COLS,
    _normalize_arrays,
    _read_ecsv_columns,
    _write_npz,
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

_STRUCTURED_DTYPE = np.dtype(
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


def _csv_source_to_target(fieldnames: Iterable[str]) -> Dict[str, str]:
    by_norm = _normalized_field_map(fieldnames)
    src_to_target: Dict[str, str] = {}
    for target, aliases in _ALIASES.items():
        for alias in aliases:
            src = by_norm.get(alias.lower())
            if src:
                src_to_target[src] = target
                break
    missing = [k for k in REQUIRED_COLS if k not in src_to_target.values()]
    if missing:
        raise ValueError(f"CSV is missing required Gaia columns: {missing}")
    return src_to_target


def _iter_csv_normalized_chunks(path: Path, chunk_rows: int = 250_000) -> Iterator[Dict[str, np.ndarray]]:
    chunk_rows = max(10_000, int(chunk_rows))
    with path.open("r", encoding="utf-8-sig", newline="") as fh_header:
        header_reader = csv.reader(fh_header)
        fieldnames = next(header_reader, None)
    if not fieldnames:
        return
    src_to_target = _csv_source_to_target(fieldnames)
    usecols = list(src_to_target.keys())

    if pd is not None:
        dtype_map = {
            src: ("string" if target == "source_id" else ("float64" if target in {"ra", "dec"} else "float32"))
            for src, target in src_to_target.items()
        }
        try:
            reader = pd.read_csv(
                path,
                usecols=usecols,
                dtype=dtype_map,
                low_memory=True,
                chunksize=chunk_rows,
            )
            for chunk in reader:
                raw: Dict[str, np.ndarray] = {}
                for src, target in src_to_target.items():
                    series = chunk[src]
                    if target == "source_id":
                        raw[target] = series.to_numpy(dtype=object, copy=False)
                    else:
                        raw[target] = series.to_numpy(copy=False)
                yield _normalize_arrays(raw)
            return
        except Exception:
            # Fallback to stdlib parser for maximum compatibility / low-memory behavior.
            pass

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return
        src_to_target = _csv_source_to_target([str(x) for x in reader.fieldnames])
        targets = list(dict.fromkeys(src_to_target.values()))
        buckets: Dict[str, List[object]] = {target: [] for target in targets}
        current_rows = 0

        for row in reader:
            for src, target in src_to_target.items():
                buckets[target].append(row.get(src))
            current_rows += 1
            if current_rows < chunk_rows:
                continue
            raw = {key: np.asarray(values, dtype=object) for key, values in buckets.items()}
            yield _normalize_arrays(raw)
            buckets = {target: [] for target in targets}
            current_rows = 0

        if current_rows > 0:
            raw = {key: np.asarray(values, dtype=object) for key, values in buckets.items()}
            yield _normalize_arrays(raw)


def _arrays_to_structured_chunk(arrays: Dict[str, np.ndarray]) -> np.ndarray:
    n = int(len(arrays.get("ra", [])))
    if n <= 0:
        return np.empty(0, dtype=_STRUCTURED_DTYPE)

    structured = np.empty(n, dtype=_STRUCTURED_DTYPE)
    structured["source_id"] = np.asarray(arrays.get("source_id", np.full(n, -1, dtype=np.int64)), dtype=np.int64)
    structured["ra"] = np.asarray(arrays["ra"], dtype=np.float64)
    structured["dec"] = np.asarray(arrays["dec"], dtype=np.float64)
    structured["phot_g_mean_mag"] = np.asarray(arrays["phot_g_mean_mag"], dtype=np.float32)
    structured["bp_rp"] = np.asarray(arrays.get("bp_rp", np.full(n, 0.8, dtype=np.float32)), dtype=np.float32)
    structured["pmra"] = np.asarray(arrays.get("pmra", np.full(n, np.nan, dtype=np.float32)), dtype=np.float32)
    structured["pmdec"] = np.asarray(arrays.get("pmdec", np.full(n, np.nan, dtype=np.float32)), dtype=np.float32)
    structured["parallax"] = np.asarray(arrays.get("parallax", np.full(n, np.nan, dtype=np.float32)), dtype=np.float32)
    return structured


def _write_structured_npy_from_raw(raw_path: Path, npy_path: Path, rows: int) -> None:
    rows_i = int(rows)
    if rows_i <= 0:
        raise ValueError("Cannot write structured NPY from empty raw payload.")
    npy_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = npy_path.with_suffix(npy_path.suffix + ".tmp")
    header = {
        "descr": _STRUCTURED_DTYPE.descr,
        "fortran_order": False,
        "shape": (rows_i,),
    }
    with tmp_out.open("wb") as dst:
        np.lib.format.write_array_header_2_0(dst, header)
        with raw_path.open("rb") as src:
            shutil.copyfileobj(src, dst, length=8 * 1024 * 1024)
    tmp_out.replace(npy_path)


def _structured_npy_arrays(path: Path) -> Dict[str, np.ndarray]:
    arr = np.load(path, mmap_mode="r", allow_pickle=False)
    if not isinstance(arr, np.ndarray) or arr.dtype.names is None:
        raise ValueError("Structured NPY expected for Gaia export.")
    names = set(arr.dtype.names)
    out: Dict[str, np.ndarray] = {
        "ra": np.asarray(arr["ra"], dtype=np.float64),
        "dec": np.asarray(arr["dec"], dtype=np.float64),
        "phot_g_mean_mag": np.asarray(arr["phot_g_mean_mag"], dtype=np.float32),
        "bp_rp": np.asarray(arr["bp_rp"], dtype=np.float32),
    }
    for key in ("pmra", "pmdec", "parallax", "source_id"):
        if key in names:
            out[key] = np.asarray(arr[key])
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


def _extract_from_npz_bytes(blob: bytes) -> Dict[str, np.ndarray]:
    try:
        with np.load(io.BytesIO(blob), allow_pickle=False) as payload:
            raw = {str(k): np.asarray(payload[k]) for k in payload.files}
        return _extract_with_aliases(raw, raw.keys())
    except Exception:
        return {}


def _extract_from_structured_npy_blob(blob: bytes) -> Dict[str, np.ndarray]:
    try:
        arr = np.load(io.BytesIO(blob), allow_pickle=False)
    except Exception:
        return {}
    if not isinstance(arr, np.ndarray) or arr.dtype.names is None:
        return {}
    raw = {str(name): np.asarray(arr[name]) for name in arr.dtype.names}
    return _extract_with_aliases(raw, raw.keys())


def _extract_from_text_csv_blob(blob: bytes) -> Dict[str, np.ndarray]:
    try:
        text = blob.decode("utf-8-sig")
    except Exception:
        return {}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {}
    if "," not in lines[0] and ";" not in lines[0]:
        return {}
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;")
        delim = str(getattr(dialect, "delimiter", ",") or ",")
    except Exception:
        delim = ";" if (";" in lines[0] and "," not in lines[0]) else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    if not reader.fieldnames:
        return {}
    fieldnames = [str(x) for x in reader.fieldnames]
    cols: Dict[str, List[object]] = {name: [] for name in fieldnames}
    for row in reader:
        for name in fieldnames:
            cols[name].append(row.get(name))
    raw = {name: np.asarray(values, dtype=object) for name, values in cols.items()}
    return _extract_with_aliases(raw, fieldnames)


def _read_zst_columns(path: Path) -> Dict[str, np.ndarray]:
    if zstd is None:
        raise ValueError(
            "No es pot llegir .zst sense zstandard. Instal-la amb: pip install zstandard"
        )
    dctx = zstd.ZstdDecompressor()
    with path.open("rb") as src, dctx.stream_reader(src) as reader:
        decompressed = reader.read()
    if decompressed.startswith(b"\x93NUMPY"):
        return _extract_from_structured_npy_blob(decompressed)
    if decompressed.startswith(b"PK\x03\x04"):
        return _extract_from_npz_bytes(decompressed)
    return _extract_from_text_csv_blob(decompressed)


def _read_input_table(path: Path) -> Dict[str, np.ndarray]:
    suffix = path.suffix.lower()
    if suffix == ".ecsv":
        return _read_ecsv_columns(path)
    if suffix == ".csv":
        return _read_csv_columns(path)
    if suffix == ".zst":
        return _read_zst_columns(path)
    return {}


def _write_zst_from_file(source_path: Path, zst_path: Path, level: int = 12) -> bool:
    if zstd is None:
        return False
    compressor = zstd.ZstdCompressor(level=int(level))
    zst_path.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("rb") as src, zst_path.open("wb") as dst:
        with compressor.stream_writer(dst) as writer:
            shutil.copyfileobj(src, writer, length=8 * 1024 * 1024)
    return True


def build_gaia_catalog_from_tables(
    input_paths: Iterable[str],
    output_dir: str,
    *,
    output_basename: str = "stars_catalog",
    zst_level: int = 12,
    write_npz: bool = True,
    write_npy: bool = False,
    write_zst: bool = True,
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

    total_files = max(1, len(paths))
    total_rows = 0
    _progress(progress_callback, 2.0, "Llegint fitxers Gaia...")

    with tempfile.TemporaryDirectory(prefix="terralab_gaia_import_") as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        raw_structured_path = tmp_dir_path / f"{output_basename}.raw"
        staged_npy_path = npy_path if bool(write_npy) else (out_dir / f".{output_basename}.staged.npy")

        with raw_structured_path.open("wb") as raw_out:
            for idx, path in enumerate(paths):
                if not path.exists():
                    continue

                file_rows = 0
                if path.suffix.lower() == ".csv":
                    for chunk_idx, chunk in enumerate(_iter_csv_normalized_chunks(path), start=1):
                        n = int(len(chunk.get("ra", [])))
                        if n <= 0:
                            continue
                        structured_chunk = _arrays_to_structured_chunk(chunk)
                        raw_out.write(structured_chunk.tobytes(order="C"))
                        file_rows += n
                        total_rows += n
                        if chunk_idx == 1 or (chunk_idx % 5) == 0:
                            pct_mid = 2.0 + (70.0 * (float(idx) + 0.5) / float(total_files))
                            _progress(
                                progress_callback,
                                pct_mid,
                                f"Processant {path.name}: {file_rows:,} files",
                            )
                else:
                    raw = _read_input_table(path)
                    if raw and all(k in raw for k in REQUIRED_COLS):
                        canonical = {k: np.asarray(raw[k]) for k in REQUIRED_COLS + OPTIONAL_COLS if k in raw}
                    else:
                        canonical = raw
                    if canonical:
                        chunk = _normalize_arrays(canonical)
                        n = int(len(chunk.get("ra", [])))
                        if n > 0:
                            structured_chunk = _arrays_to_structured_chunk(chunk)
                            raw_out.write(structured_chunk.tobytes(order="C"))
                            file_rows = n
                            total_rows += n

                pct = 2.0 + (70.0 * float(idx + 1) / float(total_files))
                _progress(
                    progress_callback,
                    pct,
                    f"Processat {path.name} ({file_rows:,} files)",
                )

        if int(total_rows) <= 0:
            raise ValueError(
                "No s'ha pogut construir el cataleg Gaia. "
                "Formats admesos: ECSV (.ecsv), CSV (.csv) i ZST (.zst) amb dades Gaia compatibles."
            )

        _progress(progress_callback, 82.0, "Generant NPY runtime...")
        _write_structured_npy_from_raw(raw_structured_path, staged_npy_path, total_rows)

    zst_written = False
    if bool(write_npz):
        _progress(progress_callback, 90.0, "Generant NPZ runtime...")
        arrays_for_npz = _structured_npy_arrays(staged_npy_path)
        _write_npz(npz_path, arrays_for_npz)
    else:
        try:
            npz_path.unlink(missing_ok=True)
        except Exception:
            pass

    if not bool(write_npy):
        try:
            npy_path.unlink(missing_ok=True)
        except Exception:
            pass

    zst_source: Optional[Path] = None
    if npz_path.exists():
        zst_source = npz_path
    elif staged_npy_path.exists():
        zst_source = staged_npy_path

    if bool(write_zst):
        _progress(progress_callback, 94.0, "Comprimint ZST...")
        if zst_source is not None and zst_source.exists():
            zst_written = _write_zst_from_file(zst_source, zst_path, level=int(zst_level))
    else:
        try:
            zst_path.unlink(missing_ok=True)
        except Exception:
            pass

    if (not bool(write_npy)) and staged_npy_path.exists():
        try:
            staged_npy_path.unlink(missing_ok=True)
        except Exception:
            pass

    if bool(write_npz):
        if bool(write_npy):
            final_rows = int(len(np.load(npy_path, mmap_mode="r", allow_pickle=False)))
        else:
            final_rows = int(total_rows)
    elif bool(write_npy):
        final_rows = int(len(np.load(npy_path, mmap_mode="r", allow_pickle=False)))
    else:
        final_rows = int(total_rows)

    _progress(progress_callback, 100.0, "Cataleg Gaia preparat.")

    return {
        "rows": int(final_rows),
        "output_npz": str(npz_path) if bool(write_npz) and npz_path.exists() else None,
        "output_npy": str(npy_path) if bool(write_npy) and npy_path.exists() else None,
        "output_zst": str(zst_path) if zst_written else None,
        "write_npz": bool(write_npz),
        "write_npy": bool(write_npy),
        "write_zst": bool(write_zst),
        "zst_written": bool(zst_written),
    }

