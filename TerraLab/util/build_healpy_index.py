"""Build a HEALPix-ordered Gaia catalog and compact lookup index.

This module builds:
1) `stars_catalog_healpy.npy` with rows ordered by HEALPix pixel (NESTED).
2) `stars_catalog_healpy.idx.npz` with sparse pixel -> row-range lookup data.

The implementation is streaming-friendly:
- Reads input catalog as memory map.
- Computes HEALPix pixels in chunks.
- Writes output catalog in chunks (avoids materializing `catalog[order]` in RAM).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional

import numpy as np

# Allow running as a standalone script: `python util/build_healpy_index.py ...`
if __package__ in (None, ""):
    _repo_root = Path(__file__).resolve().parents[2]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))

from TerraLab.common.app_paths import data_dir as runtime_data_dir_for

try:
    import hpgeom as hpg
except Exception:  # pragma: no cover
    hpg = None

try:
    import healpy as hp
except Exception:  # pragma: no cover
    hp = None


ProgressFn = Callable[[float, str], None]
DEFAULT_NSIDE = 512
DEFAULT_CHUNK_ROWS = 2_000_000


def _human_bytes(size_bytes: int) -> str:
    value = float(max(0, int(size_bytes)))
    units = ("B", "KB", "MB", "GB", "TB")
    idx = 0
    while value >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    return f"{value:.2f} {units[idx]}"


def _emit_progress(percent: float, message: str, callback: Optional[ProgressFn] = None) -> None:
    pct = max(0.0, min(100.0, float(percent)))
    print(f"[healpy-index] {pct:5.1f}% {message}")
    if callback is None:
        return
    try:
        callback(pct, str(message))
    except Exception:
        pass


def _default_input_catalog_path() -> Path:
    return runtime_data_dir_for("gaia") / "stars_catalog.npy"


def _resolve_output_paths(
    input_npy: Path,
    output_npy: Path | None,
    output_index: Path | None,
) -> tuple[Path, Path]:
    out_npy = output_npy if output_npy is not None else input_npy.with_name(f"{input_npy.stem}_healpy.npy")
    out_idx = output_index if output_index is not None else out_npy.with_suffix(".idx.npz")
    return out_npy, out_idx


def _backend_name() -> str:
    if hpg is not None:
        return "hpgeom"
    if hp is not None:
        return "healpy"
    return "none"


def _is_nside_ok(nside: int) -> bool:
    if hpg is not None:
        try:
            _ = hpg.nside_to_npixel(int(nside))
            return True
        except Exception:
            return False
    if hp is not None:
        return bool(hp.isnsideok(int(nside)))
    return False


def _ang_to_pix(nside: int, ra_deg: np.ndarray, dec_deg: np.ndarray) -> np.ndarray:
    if hpg is not None:
        return np.asarray(
            hpg.angle_to_pixel(
                int(nside),
                np.asarray(ra_deg, dtype=np.float64),
                np.asarray(dec_deg, dtype=np.float64),
                nest=True,
                lonlat=True,
                degrees=True,
            ),
            dtype=np.uint32,
        )
    if hp is not None:
        theta = np.radians(90.0 - np.asarray(dec_deg, dtype=np.float64))
        phi = np.radians(np.asarray(ra_deg, dtype=np.float64))
        return np.asarray(hp.ang2pix(int(nside), theta, phi, nest=True), dtype=np.uint32)
    raise RuntimeError("No HEALPix backend available. Install hpgeom or healpy.")


def _validate_inputs(catalog: np.ndarray, nside: int, chunk_rows: int) -> None:
    if _backend_name() == "none":
        raise RuntimeError("A HEALPix backend is required. Install with: pip install hpgeom")
    if not _is_nside_ok(int(nside)):
        raise ValueError(f"Invalid NSIDE={nside}. NSIDE must be a power of two.")
    if int(chunk_rows) <= 0:
        raise ValueError("chunk_rows must be > 0")
    if not isinstance(catalog, np.ndarray) or catalog.dtype.names is None:
        raise ValueError("Input catalog must be a structured NPY array.")
    names = set(catalog.dtype.names)
    for required in ("ra", "dec"):
        if required not in names:
            raise ValueError(f"Input catalog is missing required column '{required}'.")
    if len(catalog) <= 0:
        raise ValueError("Input catalog is empty.")


def _compute_healpix_pixels(
    catalog: np.ndarray,
    nside: int,
    chunk_rows: int,
    progress_callback: Optional[ProgressFn] = None,
) -> np.ndarray:
    """Compute HEALPix pixel id for each catalog row in chunks."""
    total_rows = int(len(catalog))
    pixels = np.empty(total_rows, dtype=np.uint32)
    total_chunks = (total_rows + chunk_rows - 1) // chunk_rows
    t0 = time.perf_counter()

    for chunk_idx, start in enumerate(range(0, total_rows, chunk_rows), start=1):
        stop = min(start + chunk_rows, total_rows)
        ra_chunk = np.asarray(catalog["ra"][start:stop], dtype=np.float64)
        dec_chunk = np.asarray(catalog["dec"][start:stop], dtype=np.float64)
        pixels[start:stop] = _ang_to_pix(int(nside), ra_chunk, dec_chunk)

        # Scale this phase to 6%..36%.
        frac = float(stop) / float(total_rows)
        pct = 6.0 + 30.0 * frac
        if chunk_idx == 1 or chunk_idx == total_chunks or (chunk_idx % 5) == 0:
            elapsed = time.perf_counter() - t0
            _emit_progress(
                pct,
                f"HEALPix chunk {chunk_idx}/{total_chunks} ({frac*100.0:.1f}%) elapsed={elapsed:.1f}s",
                callback=progress_callback,
            )

    return pixels


def _write_reordered_catalog(
    catalog: np.ndarray,
    order: np.ndarray,
    output_npy: Path,
    chunk_rows: int,
    progress_callback: Optional[ProgressFn] = None,
) -> None:
    """Write reordered catalog in chunks to avoid materializing `catalog[order]`."""
    total_rows = int(len(catalog))
    total_chunks = (total_rows + chunk_rows - 1) // chunk_rows
    output_npy.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = output_npy.with_suffix(output_npy.suffix + ".tmp")
    if tmp_out.exists():
        tmp_out.unlink(missing_ok=True)

    out_mm = np.lib.format.open_memmap(
        tmp_out,
        mode="w+",
        dtype=catalog.dtype,
        shape=(total_rows,),
    )
    t0 = time.perf_counter()
    try:
        for chunk_idx, start in enumerate(range(0, total_rows, chunk_rows), start=1):
            stop = min(start + chunk_rows, total_rows)
            idx_chunk = order[start:stop]
            out_mm[start:stop] = catalog[idx_chunk]

            # Scale this phase to 66%..95%.
            frac = float(stop) / float(total_rows)
            pct = 66.0 + 29.0 * frac
            if chunk_idx == 1 or chunk_idx == total_chunks or (chunk_idx % 3) == 0:
                elapsed = time.perf_counter() - t0
                _emit_progress(
                    pct,
                    f"Writing reordered catalog {chunk_idx}/{total_chunks} ({frac*100.0:.1f}%) elapsed={elapsed:.1f}s",
                    callback=progress_callback,
                )
        out_mm.flush()
    finally:
        del out_mm

    tmp_out.replace(output_npy)


def build_healpy_catalog_index(
    input_npy: str | Path,
    output_npy: str | Path | None = None,
    output_index: str | Path | None = None,
    *,
    nside: int = DEFAULT_NSIDE,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    progress_callback: Optional[ProgressFn] = None,
) -> Dict[str, object]:
    """Build a HEALPix-ordered catalog and index from a structured Gaia NPY file.

    Parameters
    ----------
    input_npy:
        Path to source structured NPY catalog.
    output_npy:
        Destination path for reordered NPY. Defaults to `<input_stem>_healpy.npy`.
    output_index:
        Destination path for index NPZ. Defaults to `<output_stem>.idx.npz`.
    nside:
        HEALPix NSIDE (power of two).
    chunk_rows:
        Chunk size for pixel computation and output writing.
    progress_callback:
        Optional callback `(percent, message)` for UI progress updates.
    """
    t0 = time.perf_counter()
    input_path = Path(input_npy).expanduser().resolve()
    out_npy, out_idx = _resolve_output_paths(
        input_path,
        Path(output_npy).expanduser().resolve() if output_npy else None,
        Path(output_index).expanduser().resolve() if output_index else None,
    )

    _emit_progress(0.5, f"Opening catalog: {input_path}", callback=progress_callback)
    catalog = np.load(input_path, mmap_mode="r", allow_pickle=False)
    _validate_inputs(catalog, int(nside), int(chunk_rows))
    total_rows = int(len(catalog))
    _emit_progress(
        2.0,
        f"Catalog rows={total_rows:,} dtype_itemsize={catalog.dtype.itemsize}B backend={_backend_name()}",
        callback=progress_callback,
    )

    pixels = _compute_healpix_pixels(
        catalog,
        int(nside),
        int(chunk_rows),
        progress_callback=progress_callback,
    )

    t_sort = time.perf_counter()
    _emit_progress(38.0, "Stable sorting by HEALPix pixel...", callback=progress_callback)
    order = np.argsort(pixels, kind="stable")
    _emit_progress(
        55.0,
        f"Stable sort done in {time.perf_counter() - t_sort:.2f}s",
        callback=progress_callback,
    )

    _emit_progress(57.0, "Building sparse pixel index...", callback=progress_callback)
    pixels_sorted = pixels[order]
    pixels_unics, inicis, comptes = np.unique(
        pixels_sorted,
        return_index=True,
        return_counts=True,
    )
    pixels_unics = np.asarray(pixels_unics, dtype=np.uint32)
    inicis = np.asarray(inicis, dtype=np.int64)
    comptes = np.asarray(comptes, dtype=np.int64)
    _emit_progress(
        64.0,
        f"Index ready: {len(pixels_unics):,} populated pixels",
        callback=progress_callback,
    )
    del pixels_sorted
    del pixels

    _write_reordered_catalog(
        catalog,
        order,
        out_npy,
        int(chunk_rows),
        progress_callback=progress_callback,
    )
    del order

    _emit_progress(96.0, f"Writing index: {out_idx}", callback=progress_callback)
    out_idx.parent.mkdir(parents=True, exist_ok=True)
    tmp_idx = out_idx.with_suffix(out_idx.suffix + ".tmp")
    if tmp_idx.exists():
        tmp_idx.unlink(missing_ok=True)
    with tmp_idx.open("wb") as fh:
        np.savez(
            fh,
            pixels_unics=pixels_unics,
            inicis=inicis,
            comptes=comptes,
            nside=np.asarray([int(nside)], dtype=np.int32),
        )
    tmp_idx.replace(out_idx)

    elapsed = time.perf_counter() - t0
    npy_size = out_npy.stat().st_size if out_npy.exists() else 0
    idx_size = out_idx.stat().st_size if out_idx.exists() else 0
    _emit_progress(
        100.0,
        (
            f"Done in {elapsed:.2f}s | "
            f"catalog={_human_bytes(npy_size)} | index={_human_bytes(idx_size)}"
        ),
        callback=progress_callback,
    )

    return {
        "input_npy": str(input_path),
        "output_npy": str(out_npy),
        "output_index": str(out_idx),
        "rows": total_rows,
        "nside": int(nside),
        "pixels_populated": int(len(pixels_unics)),
        "output_npy_size_bytes": int(npy_size),
        "output_index_size_bytes": int(idx_size),
        "elapsed_s": float(round(elapsed, 3)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build HEALPix-ordered Gaia catalog and sparse index"
    )
    parser.add_argument(
        "--input",
        default=str(_default_input_catalog_path()),
        help="Path to source stars_catalog.npy",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Path to output stars_catalog_healpy.npy (default: <input_stem>_healpy.npy)",
    )
    parser.add_argument(
        "--index",
        default="",
        help="Path to output index NPZ (default: <output_stem>.idx.npz)",
    )
    parser.add_argument("--nside", type=int, default=DEFAULT_NSIDE, help="HEALPix NSIDE (power of 2)")
    parser.add_argument("--chunk-rows", type=int, default=DEFAULT_CHUNK_ROWS, help="Rows per chunk")
    args = parser.parse_args()

    summary = build_healpy_catalog_index(
        input_npy=args.input,
        output_npy=(args.output or None),
        output_index=(args.index or None),
        nside=int(args.nside),
        chunk_rows=int(args.chunk_rows),
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
