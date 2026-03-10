"""Milky Way FITS importer utilities (FITS -> RGBA PNG with optional star removal)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import numpy as np
from astropy.io import fits

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


ProgressFn = Callable[[float, str], None]


def _progress(callback: Optional[ProgressFn], percent: float, message: str) -> None:
    if callback is None:
        return
    try:
        callback(float(percent), str(message))
    except Exception:
        pass


def _robust_normalize(channel: np.ndarray) -> np.ndarray:
    arr = np.asarray(channel, dtype=np.float32)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    p1, p99 = np.percentile(arr, [1.0, 99.0])
    if not np.isfinite(p1) or not np.isfinite(p99) or p99 <= p1:
        p1 = float(np.min(arr))
        p99 = float(np.max(arr))
    if p99 <= p1:
        return np.zeros_like(arr, dtype=np.float32)
    norm = (arr - p1) / (p99 - p1)
    return np.clip(norm, 0.0, 1.0).astype(np.float32, copy=False)


def _to_rgb_cube(data: np.ndarray) -> np.ndarray:
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 2:
        mono = _robust_normalize(arr)
        return np.stack([mono, mono, mono], axis=-1)
    if arr.ndim == 3:
        # Accept channel-first (3,H,W) or channel-last (H,W,3).
        if arr.shape[0] in (3, 4):
            r = _robust_normalize(arr[0])
            g = _robust_normalize(arr[1])
            b = _robust_normalize(arr[2])
            return np.stack([r, g, b], axis=-1)
        if arr.shape[-1] in (3, 4):
            r = _robust_normalize(arr[..., 0])
            g = _robust_normalize(arr[..., 1])
            b = _robust_normalize(arr[..., 2])
            return np.stack([r, g, b], axis=-1)
    raise ValueError(f"Unsupported FITS shape for Milky Way texture: {arr.shape}")


def _box_blur2d(arr: np.ndarray, radius: int) -> np.ndarray:
    radius = int(max(0, radius))
    src = np.asarray(arr, dtype=np.float32)
    if radius <= 0:
        return src.copy()
    k = 2 * radius + 1
    padded = np.pad(src, ((radius, radius), (radius, radius)), mode="reflect")
    integral = np.pad(np.cumsum(np.cumsum(padded, axis=0), axis=1), ((1, 0), (1, 0)), mode="constant")
    out = integral[k:, k:] - integral[:-k, k:] - integral[k:, :-k] + integral[:-k, :-k]
    out = out / float(k * k)
    return np.asarray(out, dtype=np.float32)


def _remove_star_like_sources(
    rgb: np.ndarray,
    *,
    threshold_sigma: float = 3.8,
    bg_radius: int = 5,
    grow_radius: int = 2,
) -> tuple[np.ndarray, dict]:
    rgb = np.asarray(rgb, dtype=np.float32)
    if rgb.ndim != 3 or rgb.shape[-1] != 3:
        return rgb.copy(), {"masked_pixels": 0, "sigma": 0.0, "threshold": 0.0}

    lum = np.clip((rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114), 0.0, 1.0)
    bg = _box_blur2d(lum, int(max(1, bg_radius)))
    residual = lum - bg

    med = float(np.median(residual))
    mad = float(np.median(np.abs(residual - med)))
    sigma = 1.4826 * mad
    if (not np.isfinite(sigma)) or sigma <= 1e-6:
        sigma = float(np.std(residual))
    if (not np.isfinite(sigma)) or sigma <= 1e-6:
        sigma = 1e-3

    threshold = max(0.004, float(threshold_sigma) * sigma)
    mask = residual > threshold

    # Keep only bright local peaks to avoid flattening broad nebulosity.
    hi = float(np.percentile(lum, 68.0))
    mask &= lum > hi

    if int(max(0, grow_radius)) > 0:
        grown = _box_blur2d(mask.astype(np.float32), int(grow_radius))
        mask = grown > 1e-4

    masked_pixels = int(np.count_nonzero(mask))
    if masked_pixels <= 0:
        return rgb.copy(), {"masked_pixels": 0, "sigma": float(sigma), "threshold": float(threshold)}

    # Fill masked pixels from a smooth background estimate.
    bg_rgb = np.empty_like(rgb, dtype=np.float32)
    for c in range(3):
        bg_rgb[..., c] = _box_blur2d(rgb[..., c], int(max(2, bg_radius)))

    out = np.where(mask[..., None], bg_rgb, rgb)
    out = np.clip(out, 0.0, 1.0)

    return out, {
        "masked_pixels": masked_pixels,
        "sigma": float(sigma),
        "threshold": float(threshold),
    }


def convert_milkyway_fits_to_png(
    fits_path: str,
    output_png: str,
    *,
    alpha_floor: float = 0.05,
    alpha_gamma: float = 0.85,
    remove_stars: bool = True,
    star_threshold_sigma: float = 3.8,
    star_bg_radius: int = 5,
    star_grow_radius: int = 2,
    png_compress_level: int = 0,
    progress_callback: Optional[ProgressFn] = None,
) -> dict:
    if Image is None:
        raise RuntimeError("Pillow is required for FITS->PNG conversion. Install with: pip install pillow")

    fits_p = Path(fits_path)
    out_p = Path(output_png)
    if not fits_p.exists():
        raise FileNotFoundError(f"Milky Way FITS not found: {fits_p}")

    _progress(progress_callback, 5.0, "Reading Milky Way FITS...")
    with fits.open(fits_p, memmap=True) as hdul:
        data = None
        for hdu in hdul:
            if getattr(hdu, "data", None) is not None:
                data = np.asarray(hdu.data)
                break
    if data is None:
        raise ValueError(f"No image data found in FITS: {fits_p}")

    _progress(progress_callback, 30.0, "Normalizing RGB channels...")
    rgb = _to_rgb_cube(data)

    star_stats = {"masked_pixels": 0, "sigma": 0.0, "threshold": 0.0}
    if bool(remove_stars):
        _progress(progress_callback, 45.0, "Removing star-like points (starless approx)...")
        rgb, star_stats = _remove_star_like_sources(
            rgb,
            threshold_sigma=float(star_threshold_sigma),
            bg_radius=int(star_bg_radius),
            grow_radius=int(star_grow_radius),
        )

    lum = np.clip((rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114), 0.0, 1.0)
    alpha = np.clip((lum - float(alpha_floor)) / max(1e-6, 1.0 - float(alpha_floor)), 0.0, 1.0)
    alpha = np.power(alpha, float(max(0.1, alpha_gamma)))

    _progress(progress_callback, 70.0, "Composing PNG with transparency...")
    rgba_u8 = np.empty((*rgb.shape[:2], 4), dtype=np.uint8)
    rgba_u8[..., :3] = np.clip(np.rint(rgb * 255.0), 0.0, 255.0).astype(np.uint8)
    rgba_u8[..., 3] = np.clip(np.rint(alpha * 255.0), 0.0, 255.0).astype(np.uint8)

    out_p.parent.mkdir(parents=True, exist_ok=True)
    compress_level = int(max(0, min(9, int(png_compress_level))))
    Image.fromarray(rgba_u8, mode="RGBA").save(
        out_p,
        optimize=False,
        compress_level=compress_level,
    )
    _progress(progress_callback, 100.0, "Milky Way conversion completed.")

    return {
        "output_png": str(out_p),
        "width": int(rgba_u8.shape[1]),
        "height": int(rgba_u8.shape[0]),
        "alpha_min": int(np.min(rgba_u8[..., 3])),
        "alpha_max": int(np.max(rgba_u8[..., 3])),
        "starless_applied": bool(remove_stars),
        "starless_masked_pixels": int(star_stats.get("masked_pixels", 0)),
        "starless_sigma": float(star_stats.get("sigma", 0.0)),
        "starless_threshold": float(star_stats.get("threshold", 0.0)),
        "png_compress_level": int(compress_level),
    }
