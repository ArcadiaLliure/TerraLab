"""Conversio build-time del mapa Planck HEALPix a equirectangular comprimit."""

from __future__ import annotations

import argparse
import io
import math
import multiprocessing as mp
import os
from pathlib import Path
from typing import Optional

import numpy as np
from astropy.io import fits
import hpgeom


_WORKER_VALUES = None
_WORKER_HDUL = None
_WORKER_NSIDE = 0
_WORKER_NEST = False
_WORKER_WIDTH = 0
_WORKER_HEIGHT = 0
_WORKER_RA = None


def _column_name_from_hdu(hdu) -> str:
    if not hasattr(hdu, "columns") or hdu.columns is None:
        raise RuntimeError("FITS sense taula de columnes HEALPix")
    names = [str(n) for n in hdu.columns.names]
    for candidate in ("TAU353", "TAU", "I_STOKES"):
        if candidate in names:
            return candidate
    if not names:
        raise RuntimeError("No hi ha columnes disponibles al FITS")
    return names[0]


def _load_planck_field(fits_path: str) -> tuple[np.ndarray, int, bool, str]:
    with fits.open(fits_path, memmap=True) as hdul:
        hdu = None
        for item in hdul:
            if isinstance(item, fits.BinTableHDU):
                hdu = item
                break
        if hdu is None:
            raise RuntimeError("No s'ha trobat cap BinTableHDU")

        column = _column_name_from_hdu(hdu)
        values = np.asarray(hdu.data[column], dtype=np.float32).reshape(-1)
        if values.size == 0:
            raise RuntimeError("La columna de pols esta buida")

        header = hdu.header
        nside = int(header.get("NSIDE", 0))
        if nside <= 0:
            nside = int(round(math.sqrt(values.size / 12.0)))
        if nside <= 0 or 12 * nside * nside != int(values.size):
            raise RuntimeError("No es pot deduir un NSIDE valid del FITS")

        ordering = str(header.get("ORDERING", "RING")).upper()
        nest = ordering.startswith("NEST")
        return values, nside, nest, ordering


def _init_worker(fits_path: str, column_name: str, nside: int, nest: bool, width: int, height: int) -> None:
    global _WORKER_VALUES, _WORKER_HDUL, _WORKER_NSIDE, _WORKER_NEST, _WORKER_WIDTH, _WORKER_HEIGHT, _WORKER_RA
    _WORKER_HDUL = fits.open(fits_path, memmap=True)
    hdu = None
    for item in _WORKER_HDUL:
        if isinstance(item, fits.BinTableHDU):
            hdu = item
            break
    if hdu is None:
        raise RuntimeError("No s'ha trobat cap BinTableHDU al worker")
    _WORKER_VALUES = np.asarray(hdu.data[column_name]).reshape(-1)

    _WORKER_NSIDE = int(nside)
    _WORKER_NEST = bool(nest)
    _WORKER_WIDTH = int(width)
    _WORKER_HEIGHT = int(height)
    _WORKER_RA = np.linspace(0.0, 360.0, num=_WORKER_WIDTH, endpoint=False, dtype=np.float64) + (180.0 / _WORKER_WIDTH)


def _convert_rows(task: tuple[int, int]) -> tuple[int, np.ndarray]:
    y_start, y_end = task
    y_start = int(y_start)
    y_end = int(y_end)
    rows = max(0, y_end - y_start)
    out = np.zeros((rows, _WORKER_WIDTH), dtype=np.float32)

    for local_y, y in enumerate(range(y_start, y_end)):
        dec = 90.0 - (((float(y) + 0.5) / float(_WORKER_HEIGHT)) * 180.0)
        lat_arr = np.full(_WORKER_WIDTH, dec, dtype=np.float64)
        pix = hpgeom.angle_to_pixel(
            _WORKER_NSIDE,
            _WORKER_RA,
            lat_arr,
            nest=_WORKER_NEST,
            lonlat=True,
            degrees=True,
        )
        out[local_y, :] = np.asarray(_WORKER_VALUES[pix], dtype=np.float32)
    return y_start, out


def _normalize_to_u16(data: np.ndarray, percentile_low: float, percentile_high: float) -> tuple[np.ndarray, float, float, np.ndarray]:
    finite = np.asarray(np.isfinite(data), dtype=bool)
    if not np.any(finite):
        zeros = np.zeros_like(data, dtype=np.float32)
        return np.zeros_like(data, dtype=np.uint16), 0.0, 1.0, zeros

    valid = data[finite]
    low = float(np.percentile(valid, percentile_low))
    high = float(np.percentile(valid, percentile_high))
    if high <= low:
        high = low + 1e-6

    normalized = np.asarray((data - low) / (high - low), dtype=np.float32)
    normalized = np.clip(normalized, 0.0, 1.0)
    normalized = np.where(np.isfinite(normalized), normalized, 0.0)
    as_u16 = np.asarray(np.rint(normalized * 65535.0), dtype=np.uint16)
    return as_u16, low, high, normalized


def _save_preview_png(normalized: np.ndarray, preview_path: str) -> bool:
    try:
        from PIL import Image
    except Exception:
        return False

    arr = np.asarray(np.clip(np.rint(normalized * 255.0), 0.0, 255.0), dtype=np.uint8)
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    Image.fromarray(arr, mode="L").save(preview_path)
    return True


def _save_optional_zst(data_u16: np.ndarray, output_zst: str, zst_level: int) -> bool:
    try:
        import zstandard as zstd
    except Exception:
        return False

    buf = io.BytesIO()
    np.save(buf, data_u16, allow_pickle=False)
    payload = buf.getvalue()
    compressor = zstd.ZstdCompressor(level=int(zst_level))
    compressed = compressor.compress(payload)

    os.makedirs(os.path.dirname(output_zst), exist_ok=True)
    with open(output_zst, "wb") as fh:
        fh.write(compressed)
    return True


def convert_planck_fits_to_cache(
    *,
    fits_path: str,
    output_npz: str,
    width: int = 3600,
    height: int = 1800,
    workers: int = 2,
    chunk_rows: int = 64,
    percentile_low: float = 1.0,
    percentile_high: float = 99.5,
    write_zst: bool = False,
    output_zst: Optional[str] = None,
    zst_level: int = 12,
    preview_png: Optional[str] = None,
) -> dict:
    fits_path = os.path.abspath(str(fits_path))
    output_npz = os.path.abspath(str(output_npz))
    if output_zst:
        output_zst = os.path.abspath(str(output_zst))
    if preview_png:
        preview_png = os.path.abspath(str(preview_png))

    values, nside, nest, ordering = _load_planck_field(fits_path)
    if values.size != 12 * nside * nside:
        raise RuntimeError("Tamany de mapa inconsistent amb NSIDE")

    width = int(max(16, width))
    height = int(max(8, height))
    workers = int(max(1, workers))
    chunk_rows = int(max(1, chunk_rows))

    tasks = []
    y = 0
    while y < height:
        y2 = min(height, y + chunk_rows)
        tasks.append((y, y2))
        y = y2

    column_name = "TAU353"
    with fits.open(fits_path, memmap=True) as hdul:
        for item in hdul:
            if isinstance(item, fits.BinTableHDU):
                column_name = _column_name_from_hdu(item)
                break

    result = np.zeros((height, width), dtype=np.float32)

    if workers <= 1:
        _init_worker(fits_path, column_name, nside, nest, width, height)
        for task in tasks:
            y_start, chunk = _convert_rows(task)
            result[y_start : y_start + chunk.shape[0], :] = chunk
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(
            processes=workers,
            initializer=_init_worker,
            initargs=(fits_path, column_name, nside, nest, width, height),
        ) as pool:
            for y_start, chunk in pool.imap_unordered(_convert_rows, tasks):
                result[int(y_start) : int(y_start) + chunk.shape[0], :] = chunk

    data_u16, low, high, normalized = _normalize_to_u16(
        result,
        percentile_low=float(percentile_low),
        percentile_high=float(percentile_high),
    )

    os.makedirs(os.path.dirname(output_npz), exist_ok=True)
    np.savez_compressed(
        output_npz,
        opacity_u16=data_u16,
        width=np.int32(width),
        height=np.int32(height),
        nside=np.int32(nside),
        ordering=np.asarray(ordering),
        source_fits=np.asarray(fits_path),
        normalization_low=np.float32(low),
        normalization_high=np.float32(high),
    )

    zst_written = False
    zst_path = None
    if write_zst:
        if not output_zst:
            zst_path = str(Path(output_npz).with_suffix(".npy.zst"))
        else:
            zst_path = output_zst
        zst_written = _save_optional_zst(data_u16, zst_path, int(zst_level))

    preview_written = False
    if preview_png:
        preview_written = _save_preview_png(normalized, preview_png)

    return {
        "fits_path": fits_path,
        "output_npz": output_npz,
        "output_zst": zst_path if zst_written else None,
        "preview_png": preview_png if preview_written else None,
        "width": int(width),
        "height": int(height),
        "workers": int(workers),
        "nside": int(nside),
        "ordering": ordering,
        "normalization_low": float(low),
        "normalization_high": float(high),
        "zst_written": bool(zst_written),
        "preview_written": bool(preview_written),
    }


def _default_fits_path() -> str:
    return str(
        Path(__file__).resolve().parents[1]
        / "data"
        / "sky"
        / "COM_CompMap_Dust-GNILC-Model-Opacity_2048_R2.01.fits"
    )


def _default_output_npz() -> str:
    return str(
        Path(__file__).resolve().parents[1]
        / "data"
        / "sky"
        / "derived"
        / "planck_dust_opacity_eq_u16.npz"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Converteix un FITS Planck a cache equirectangular comprimit.")
    parser.add_argument("--fits-path", default=_default_fits_path())
    parser.add_argument("--output-npz", default=_default_output_npz())
    parser.add_argument("--width", type=int, default=3600)
    parser.add_argument("--height", type=int, default=1800)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--chunk-rows", type=int, default=64)
    parser.add_argument("--percentile-low", type=float, default=1.0)
    parser.add_argument("--percentile-high", type=float, default=99.5)
    parser.add_argument("--zst", action="store_true")
    parser.add_argument("--output-zst", default="")
    parser.add_argument("--zst-level", type=int, default=12)
    parser.add_argument("--preview-png", default="")
    args = parser.parse_args()

    summary = convert_planck_fits_to_cache(
        fits_path=args.fits_path,
        output_npz=args.output_npz,
        width=args.width,
        height=args.height,
        workers=args.workers,
        chunk_rows=args.chunk_rows,
        percentile_low=args.percentile_low,
        percentile_high=args.percentile_high,
        write_zst=bool(args.zst),
        output_zst=args.output_zst or None,
        zst_level=args.zst_level,
        preview_png=args.preview_png or None,
    )

    print("[convert_planck_dust] done")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
