"""Pure surface-grid alignment, LOD, and interpolation functions."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from TerraLab.terrain.crs import normalize_crs
from TerraLab.terrain.providers import RasterMetadata
from TerraLab.terrain.surface.cache import SurfaceSamplingRequest

def raster_grids_aligned(
    first: RasterMetadata,
    second: RasterMetadata,
    *,
    tolerance: float = 1e-7,
) -> bool:
    """Return whether two rasters share CRS, pixel vectors and pixel centres.

    Equal nominal resolution is insufficient: the affine origins must differ
    by an integer number of pixels.  Callers may use this result for a direct
    index fast path; all other cases continue through world coordinates and
    the normal CRS-aware sampler.
    """

    if normalize_crs(first.native_crs) != normalize_crs(second.native_crs):
        return False
    first_transform = np.asarray(first.extra.get("transform", ()), dtype=np.float64)
    second_transform = np.asarray(second.extra.get("transform", ()), dtype=np.float64)
    if first_transform.shape != (6,) or second_transform.shape != (6,):
        return False
    first_basis = np.asarray(
        ((first_transform[0], first_transform[1]), (first_transform[3], first_transform[4])),
        dtype=np.float64,
    )
    second_basis = np.asarray(
        ((second_transform[0], second_transform[1]), (second_transform[3], second_transform[4])),
        dtype=np.float64,
    )
    scale = max(1.0, float(np.max(np.abs(first_basis))))
    if not np.allclose(first_basis, second_basis, rtol=tolerance, atol=tolerance * scale):
        return False
    try:
        offset = np.linalg.solve(
            first_basis,
            np.asarray(
                (
                    second_transform[2] - first_transform[2],
                    second_transform[5] - first_transform[5],
                ),
                dtype=np.float64,
            ),
        )
    except np.linalg.LinAlgError:
        return False
    return bool(np.allclose(offset, np.rint(offset), atol=tolerance, rtol=0.0))


def _sample_indices(size: int, other_size: int, max_samples: int) -> np.ndarray:
    if size <= 0:
        return np.asarray([], dtype=np.int32)
    permitted = max(1, int(max_samples) // max(1, int(other_size)))
    count = min(int(size), permitted)
    if count >= int(size):
        return np.arange(size, dtype=np.int32)
    return np.unique(np.rint(np.linspace(0, size - 1, count)).astype(np.int32))


def _visible_azimuth_indices(
    indices: np.ndarray,
    azimuths: np.ndarray,
    request: SurfaceSamplingRequest,
) -> np.ndarray:
    if request.stage == "complete":
        return indices
    width = min(360.0, request.view_fov_deg + 2.0 * request.fov_margin_deg)
    if width >= 360.0:
        return indices
    values = np.asarray(azimuths, dtype=np.float64)[indices]
    delta = (values - request.view_azimuth_deg + 180.0) % 360.0 - 180.0
    return indices[np.abs(delta) <= width * 0.5 + 1e-9]


def _lod_factors_for_polar_grid(
    distances: Any,
    azimuths: Any,
    resolution_m: float | None,
    *,
    viewport_width_px: int | None = None,
    view_fov_deg: float | None = None,
    categorical: bool = False,
) -> np.ndarray:
    """Choose the coarsest power-of-two level no larger than half a cell."""

    distance_grid = np.asarray(distances, dtype=np.float64)
    azimuth_axis = np.asarray(azimuths, dtype=np.float64)
    if distance_grid.ndim == 1:
        distance_grid = distance_grid[:, None]
    if distance_grid.ndim != 2:
        return np.ones(distance_grid.shape, dtype=np.int16)
    if distance_grid.shape[1] == 1 and azimuth_axis.size > 1:
        distance_grid = np.broadcast_to(
            distance_grid, (distance_grid.shape[0], azimuth_axis.size)
        )
    resolution = float(resolution_m or 0.0)
    if not math.isfinite(resolution) or resolution <= 0.0:
        return np.ones(distance_grid.shape, dtype=np.int16)
    if distance_grid.shape[0] > 1:
        radial_spacing = np.abs(np.gradient(distance_grid, axis=0))
    else:
        radial_spacing = np.full(distance_grid.shape, np.inf, dtype=np.float64)
    if azimuth_axis.size > 1:
        sorted_azimuths = np.sort(azimuth_axis % 360.0)
        steps = np.diff(np.r_[sorted_azimuths, sorted_azimuths[0] + 360.0])
        positive = steps[steps > 1e-9]
        angular_step = math.radians(
            float(np.median(positive)) if positive.size else 360.0
        )
    else:
        angular_step = math.inf
    angular_spacing = np.abs(distance_grid) * angular_step
    spacing = np.minimum(radial_spacing, angular_spacing)
    maximum = np.floor(spacing / (2.0 * resolution))
    maximum = np.where(np.isfinite(maximum), maximum, 128.0)
    levels = np.asarray((1, 2, 4, 8, 16, 32, 64, 128), dtype=np.int16)
    positions = np.searchsorted(levels, np.maximum(1.0, maximum), side="right") - 1
    result = levels[np.clip(positions, 0, len(levels) - 1)]
    if categorical and viewport_width_px and view_fov_deg:
        fov_radians = math.radians(
            min(360.0, max(1e-3, float(view_fov_deg)))
        )
        pixels_per_radian = float(viewport_width_px) / fov_radians
        projected_native = (
            resolution
            / np.maximum(np.abs(distance_grid), resolution)
            * pixels_per_radian
        )
        maximum_factor = np.maximum(
            1.0, np.floor(2.0 / np.maximum(projected_native, 1e-12))
        )
        cap_positions = (
            np.searchsorted(levels, maximum_factor, side="right") - 1
        )
        screen_cap = levels[
            np.clip(cap_positions, 0, len(levels) - 1)
        ]
        result = np.minimum(result, screen_cap)
        result = np.where(projected_native >= 0.5, 1, result)
    return np.asarray(result, dtype=np.int16)


def _subdivided_axis(values: np.ndarray, factor: int, *, circular: bool = False) -> np.ndarray:
    source = np.asarray(values, dtype=np.float64)
    factor = max(1, int(factor))
    if source.size < 2 or factor == 1:
        return source.astype(np.float32)
    if circular:
        extended = np.concatenate((source, [float(source[0]) + 360.0]))
        parts = [
            np.linspace(extended[index], extended[index + 1], factor, endpoint=False)
            for index in range(source.size)
        ]
        return np.concatenate(parts).astype(np.float32)
    parts = [
        np.linspace(source[index], source[index + 1], factor, endpoint=False)
        for index in range(source.size - 1)
    ]
    return np.concatenate((*parts, source[-1:])).astype(np.float32)


def _interpolate_polar_grid(
    values: Any,
    distances: np.ndarray,
    azimuths: np.ndarray,
    new_distances: np.ndarray,
    new_azimuths: np.ndarray,
    *,
    nearest: bool = False,
) -> np.ndarray:
    """Interpolate a DEM-derived polar grid, wrapping azimuth at 360 degrees."""

    source = np.asarray(values)
    if source.shape != (len(distances), len(azimuths)):
        raise ValueError("Polar grid shape does not match its axes")
    d_hi = np.searchsorted(distances, new_distances, side="right")
    d_hi = np.clip(d_hi, 1, len(distances) - 1)
    d_lo = d_hi - 1
    d_span = np.maximum(distances[d_hi] - distances[d_lo], 1e-9)
    d_t = (new_distances - distances[d_lo]) / d_span

    az = np.asarray(azimuths, dtype=np.float64)
    az_step_values = np.diff(az)
    az_step_values = az_step_values[az_step_values > 0]
    az_step = float(np.median(az_step_values)) if az_step_values.size else 360.0
    a_position = ((new_azimuths - float(az[0])) % 360.0) / max(az_step, 1e-9)
    a_floor = np.floor(a_position)
    a_lo = a_floor.astype(np.int64) % len(az)
    a_hi = (a_lo + 1) % len(az)
    a_t = a_position - a_floor
    if nearest:
        d_index = np.where(d_t < 0.5, d_lo, d_hi)
        a_index = np.where(a_t < 0.5, a_lo, a_hi)
        return source[np.ix_(d_index, a_index)]

    v00 = source[np.ix_(d_lo, a_lo)].astype(np.float64)
    v01 = source[np.ix_(d_lo, a_hi)].astype(np.float64)
    v10 = source[np.ix_(d_hi, a_lo)].astype(np.float64)
    v11 = source[np.ix_(d_hi, a_hi)].astype(np.float64)
    radial_low = v00 * (1.0 - a_t[None, :]) + v01 * a_t[None, :]
    radial_high = v10 * (1.0 - a_t[None, :]) + v11 * a_t[None, :]
    return radial_low * (1.0 - d_t[:, None]) + radial_high * d_t[:, None]
