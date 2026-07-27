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

from TerraLab.common.exception_reporting import log_suppressed_exception
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
DEFAULT_CHUNK_ROWS = 1_000_000
MAX_CHUNK_BYTES = 128 * 1024**2


def _human_bytes(size_bytes: int) -> str:
    value = float(max(0, int(size_bytes)))
    units = ("B", "KB", "MB", "GB", "TB")
    idx = 0
    while value >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    return f"{value:.2f} {units[idx]}"


def _emit_progress(
    percent: float, message: str, callback: Optional[ProgressFn] = None
) -> None:
    pct = max(0.0, min(100.0, float(percent)))
    print(f"[healpy-index] {pct:5.1f}% {message}")
    if callback is None:
        return
    try:
        callback(pct, str(message))
    except Exception:
        log_suppressed_exception(__name__, "_emit_progress")


def _default_input_catalog_path() -> Path:
    return runtime_data_dir_for("gaia") / "stars_catalog.npy"


def _resolve_output_paths(
    input_npy: Path,
    output_npy: Path | None,
    output_index: Path | None,
) -> tuple[Path, Path]:
    out_npy = (
        output_npy
        if output_npy is not None
        else input_npy.with_name(f"{input_npy.stem}_healpy.npy")
    )
    out_idx = (
        output_index
        if output_index is not None
        else out_npy.with_suffix(".idx.npz")
    )
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


def _ang_to_pix(
    nside: int, ra_deg: np.ndarray, dec_deg: np.ndarray
) -> np.ndarray:
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
        return np.asarray(
            hp.ang2pix(int(nside), theta, phi, nest=True), dtype=np.uint32
        )
    raise RuntimeError(
        "No HEALPix backend available. Install hpgeom or healpy."
    )


def _validate_inputs(catalog: np.ndarray, nside: int, chunk_rows: int) -> None:
    if _backend_name() == "none":
        raise RuntimeError(
            "A HEALPix backend is required. Install with: pip install hpgeom"
        )
    if not _is_nside_ok(int(nside)):
        raise ValueError(
            f"Invalid NSIDE={nside}. NSIDE must be a power of two."
        )
    if int(chunk_rows) <= 0:
        raise ValueError("chunk_rows must be > 0")
    if not isinstance(catalog, np.ndarray) or catalog.dtype.names is None:
        raise ValueError("Input catalog must be a structured NPY array.")
    names = set(catalog.dtype.names)
    for required in ("ra", "dec"):
        if required not in names:
            raise ValueError(
                f"Input catalog is missing required column '{required}'."
            )
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

    for chunk_idx, start in enumerate(
        range(0, total_rows, chunk_rows), start=1
    ):
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
        for chunk_idx, start in enumerate(
            range(0, total_rows, chunk_rows), start=1
        ):
            stop = min(start + chunk_rows, total_rows)
            idx_chunk = order[start:stop]
            out_mm[start:stop] = catalog[idx_chunk]

            # Scale this phase to 66%..95%.
            frac = float(stop) / float(total_rows)
            pct = 66.0 + 29.0 * frac
            if (
                chunk_idx == 1
                or chunk_idx == total_chunks
                or (chunk_idx % 3) == 0
            ):
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


def _count_healpix_pixels_streaming(
    catalog: np.ndarray,
    nside: int,
    chunk_rows: int,
    progress_callback: Optional[ProgressFn] = None,
) -> np.ndarray:
    """First pass: bounded pixel histogram, never a row-sized key array."""

    npixels = 12 * int(nside) * int(nside)
    counts = np.zeros(npixels, dtype=np.int64)
    total = int(len(catalog))
    for chunk_index, start in enumerate(range(0, total, chunk_rows), start=1):
        stop = min(total, start + chunk_rows)
        pixels = _ang_to_pix(
            nside,
            np.asarray(catalog["ra"][start:stop], dtype=np.float64),
            np.asarray(catalog["dec"][start:stop], dtype=np.float64),
        )
        counts += np.bincount(pixels, minlength=npixels).astype(np.int64, copy=False)
        if chunk_index == 1 or stop == total or chunk_index % 5 == 0:
            _emit_progress(
                6.0 + 24.0 * (float(stop) / total),
                f"Counting HEALPix cells {stop:,}/{total:,}",
                callback=progress_callback,
            )
    return counts


def _scatter_catalog_by_pixel(
    catalog: np.ndarray,
    counts: np.ndarray,
    nside: int,
    output_tmp: Path,
    chunk_rows: int,
    progress_callback: Optional[ProgressFn] = None,
) -> tuple[np.memmap, np.ndarray]:
    """Second pass: counting-sort scatter with only chunk-sized argsorts."""

    starts = np.zeros(counts.size, dtype=np.int64)
    if counts.size > 1:
        np.cumsum(counts[:-1], dtype=np.int64, out=starts[1:])
    cursors = starts.copy()
    output_tmp.parent.mkdir(parents=True, exist_ok=True)
    output_tmp.unlink(missing_ok=True)
    output = np.lib.format.open_memmap(
        output_tmp, mode="w+", dtype=catalog.dtype, shape=(len(catalog),)
    )
    total = int(len(catalog))
    for chunk_index, start in enumerate(range(0, total, chunk_rows), start=1):
        stop = min(total, start + chunk_rows)
        chunk = catalog[start:stop]
        pixels = _ang_to_pix(
            nside,
            np.asarray(chunk["ra"], dtype=np.float64),
            np.asarray(chunk["dec"], dtype=np.float64),
        )
        order = np.argsort(pixels, kind="stable")
        pixels_sorted = pixels[order]
        unique_pixels, first, local_counts = np.unique(
            pixels_sorted, return_index=True, return_counts=True
        )
        group_start_for_row = np.repeat(first, local_counts)
        local_rank = np.arange(len(chunk), dtype=np.int64) - group_start_for_row
        targets = cursors[pixels_sorted] + local_rank
        output[targets] = chunk[order]
        cursors[unique_pixels] += local_counts
        if chunk_index == 1 or stop == total or chunk_index % 3 == 0:
            _emit_progress(
                32.0 + 30.0 * (float(stop) / total),
                f"Scattering catalogue {stop:,}/{total:,}",
                callback=progress_callback,
            )
    output.flush()
    return output, starts


def _sort_pixel_ranges_v2(
    output: np.memmap,
    counts: np.ndarray,
    starts: np.ndarray,
    chunk_rows: int,
    progress_callback: Optional[ProgressFn] = None,
) -> None:
    """Sort bounded groups of whole pixels by (pixel, magnitude, source_id)."""

    names = set(output.dtype.names or ())
    mag_name = "phot_g_mean_mag" if "phot_g_mean_mag" in names else "mag"
    if mag_name not in names:
        raise ValueError("Catalogue requires phot_g_mean_mag or mag for v2")
    id_name = "source_id" if "source_id" in names else None
    populated = np.flatnonzero(counts)
    if populated.size == 0:
        return
    total_rows = int(len(output))
    group_start = 0
    processed = 0
    max_rows = max(1, int(chunk_rows))
    while group_start < populated.size:
        group_stop = group_start
        rows = 0
        while group_stop < populated.size:
            next_rows = int(counts[populated[group_stop]])
            if rows and rows + next_rows > max_rows:
                break
            rows += next_rows
            group_stop += 1
        pixels = populated[group_start:group_stop]
        start = int(starts[pixels[0]])
        stop = int(starts[pixels[-1]] + counts[pixels[-1]])
        chunk = np.array(output[start:stop], copy=True)
        pixel_keys = np.repeat(pixels.astype(np.uint32), counts[pixels])
        source_ids = (
            np.asarray(chunk[id_name], dtype=np.int64)
            if id_name is not None
            else np.zeros(len(chunk), dtype=np.int64)
        )
        order = np.lexsort(
            (source_ids, np.asarray(chunk[mag_name], dtype=np.float32), pixel_keys)
        )
        output[start:stop] = chunk[order]
        processed += len(chunk)
        group_start = group_stop
        if group_start == populated.size or processed == len(chunk) or processed % (max_rows * 5) < len(chunk):
            _emit_progress(
                64.0 + 30.0 * (float(processed) / total_rows),
                f"Sorting v2 pixel ranges {processed:,}/{total_rows:,}",
                callback=progress_callback,
            )
    output.flush()


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

    _emit_progress(
        0.5, f"Opening catalog: {input_path}", callback=progress_callback
    )
    catalog = np.load(input_path, mmap_mode="r", allow_pickle=False)
    _validate_inputs(catalog, int(nside), int(chunk_rows))
    # Sorting/scattering holds keys, order arrays and a structured row copy.
    # Account for that working set as well as the explicit one-million cap.
    bounded_chunk_rows = min(
        1_000_000,
        int(chunk_rows),
        max(1, MAX_CHUNK_BYTES // max(1, int(catalog.dtype.itemsize) * 2 + 24)),
    )
    total_rows = int(len(catalog))
    _emit_progress(
        2.0,
        f"Catalog rows={total_rows:,} dtype_itemsize={catalog.dtype.itemsize}B backend={_backend_name()}",
        callback=progress_callback,
    )

    counts_full = _count_healpix_pixels_streaming(
        catalog,
        int(nside),
        bounded_chunk_rows,
        progress_callback=progress_callback,
    )
    tmp_out = out_npy.with_suffix(out_npy.suffix + ".tmp")
    output = None
    try:
        output, starts_full = _scatter_catalog_by_pixel(
            catalog,
            counts_full,
            int(nside),
            tmp_out,
            bounded_chunk_rows,
            progress_callback=progress_callback,
        )
        _sort_pixel_ranges_v2(
            output,
            counts_full,
            starts_full,
            bounded_chunk_rows,
            progress_callback=progress_callback,
        )
        del output
        output = None
        out_npy.parent.mkdir(parents=True, exist_ok=True)
        tmp_out.replace(out_npy)
    except Exception:
        if output is not None:
            del output
        tmp_out.unlink(missing_ok=True)
        raise

    pixels_unics = np.flatnonzero(counts_full).astype(np.uint32)
    inicis = starts_full[pixels_unics].astype(np.int64, copy=False)
    comptes = counts_full[pixels_unics].astype(np.int64, copy=False)
    _emit_progress(
        95.0,
        f"V2 index ready: {len(pixels_unics):,} populated pixels",
        callback=progress_callback,
    )

    _emit_progress(
        96.0, f"Writing index: {out_idx}", callback=progress_callback
    )
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
            format_version=np.asarray([2], dtype=np.int16),
            sorted_by=np.asarray("healpix,phot_g_mean_mag,source_id"),
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
    parser.add_argument(
        "--nside",
        type=int,
        default=DEFAULT_NSIDE,
        help="HEALPix NSIDE (power of 2)",
    )
    parser.add_argument(
        "--chunk-rows",
        type=int,
        default=DEFAULT_CHUNK_ROWS,
        help="Rows per chunk",
    )
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
