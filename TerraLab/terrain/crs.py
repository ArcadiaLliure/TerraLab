"""Shared coordinate-reference-system services for terrain data.

The current horizon engine still uses ETRS89 / UTM zone 31N as its metric
working frame.  Providers deliberately receive that frame as an argument,
however, so raster implementations do not need to know which projected CRS
the engine selected.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Tuple

import numpy as np
from pyproj import CRS, Geod, Proj, Transformer


CRS_GEOGRAPHIC = "EPSG:4326"
CRS_TERRAIN_INTERNAL = "EPSG:25831"

# Kept public for compatibility with modules that serialize pyproj creation.
PYPROJ_TRANSFORMER_LOCK = threading.RLock()
_EPSG_FASTPATH_RE = re.compile(r"^\s*EPSG\s*:\s*(\d+)\s*$", re.IGNORECASE)


def _fast_normalize_crs(value: Any) -> str | None:
    if isinstance(value, str):
        text = str(value).strip()
        if not text:
            return None
        match = _EPSG_FASTPATH_RE.match(text)
        if match:
            return f"EPSG:{int(match.group(1))}"
        upper = text.upper()
        if upper in {"OGC:CRS84", "CRS84"}:
            return "OGC:CRS84"
        return None
    if isinstance(value, int):
        return f"EPSG:{int(value)}"
    return None


def normalize_crs(value: Any, *, fallback: Any | None = None) -> str:
    """Return a stable CRS string, or use ``fallback`` when value is empty."""

    candidate = value if value not in (None, "") else fallback
    if candidate in (None, ""):
        raise ValueError("A coordinate reference system is required")
    fast = _fast_normalize_crs(candidate)
    if fast is not None:
        return fast
    return CRS.from_user_input(candidate).to_string()


@dataclass(frozen=True)
class CoordinateFrame:
    """Coordinate frame used by geometry or a raster provider."""

    crs: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "crs", normalize_crs(self.crs))

    @property
    def is_geographic(self) -> bool:
        return bool(CRS.from_user_input(self.crs).is_geographic)


class CoordinateTransformService:
    """Thread-safe, cached and vectorized CRS transformations."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @staticmethod
    @lru_cache(maxsize=128)
    def _build_transformer(source_crs: str, target_crs: str) -> Transformer:
        with PYPROJ_TRANSFORMER_LOCK:
            return Transformer.from_crs(
                source_crs,
                target_crs,
                always_xy=True,
            )

    def transformer(self, source_crs: Any, target_crs: Any) -> Transformer:
        source = normalize_crs(source_crs)
        target = normalize_crs(target_crs)
        return self._build_transformer(source, target)

    def transform_xy(
        self,
        x: Any,
        y: Any,
        source_crs: Any,
        target_crs: Any,
    ) -> Tuple[Any, Any]:
        """Transform scalar or NumPy-compatible x/y coordinates."""

        source = normalize_crs(source_crs)
        target = normalize_crs(target_crs)
        scalar = np.isscalar(x) and np.isscalar(y)
        if source == target:
            if scalar:
                return float(x), float(y)
            return np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
        transformer = self._build_transformer(source, target)
        if scalar:
            transform_x, transform_y = float(x), float(y)
        else:
            # pyproj versions paired with NumPy 2.x can route plain ndarrays
            # through their scalar path and emit one warning per coordinate.
            # MaskedArray uses pyproj's buffer path without copying values into
            # Python lists, which matters for large pre-sampling batches.
            x_array, y_array = np.broadcast_arrays(
                np.asarray(x, dtype=np.float64),
                np.asarray(y, dtype=np.float64),
            )
            transform_x = np.ma.asarray(x_array)
            transform_y = np.ma.asarray(y_array)
        with self._lock:
            tx, ty = transformer.transform(transform_x, transform_y)
        if scalar:
            return float(tx), float(ty)
        return np.asarray(tx, dtype=np.float64), np.asarray(ty, dtype=np.float64)

    def transform_bounds(
        self,
        bounds: tuple[float, float, float, float],
        source_crs: Any,
        target_crs: Any,
        *,
        densify_points: int = 21,
    ) -> tuple[float, float, float, float]:
        source = normalize_crs(source_crs)
        target = normalize_crs(target_crs)
        left, bottom, right, top = (float(value) for value in bounds)
        if source == target:
            return left, bottom, right, top
        transformer = self._build_transformer(source, target)
        with self._lock:
            result = transformer.transform_bounds(
                left,
                bottom,
                right,
                top,
                densify_pts=max(2, int(densify_points)),
            )
        return tuple(float(value) for value in result)

    def metric_circle_bounds(
        self,
        center_x: float,
        center_y: float,
        radius_m: float,
        source_crs: Any,
        target_crs: Any,
        *,
        samples: int = 48,
    ) -> tuple[float, float, float, float]:
        """Transform a metric ROI after densifying its circular boundary."""

        radius = max(0.0, float(radius_m))
        angles = np.linspace(0.0, math.tau, max(12, int(samples)), endpoint=False)
        xs = float(center_x) + radius * np.cos(angles)
        ys = float(center_y) + radius * np.sin(angles)
        tx, ty = self.transform_xy(xs, ys, source_crs, target_crs)
        finite = np.isfinite(tx) & np.isfinite(ty)
        if not np.any(finite):
            raise ValueError("ROI cannot be transformed into the target CRS")
        return (
            float(np.min(tx[finite])),
            float(np.min(ty[finite])),
            float(np.max(tx[finite])),
            float(np.max(ty[finite])),
        )

    def resolution_metres(
        self,
        crs: Any,
        transform: Any,
        width: int,
        height: int,
    ) -> float | None:
        """Estimate nominal pixel size in metres for an arbitrary raster CRS."""

        if int(width) <= 0 or int(height) <= 0:
            return None
        try:
            col = max(0.0, (float(width) - 1.0) * 0.5)
            row = max(0.0, (float(height) - 1.0) * 0.5)
            x0, y0 = transform * (col + 0.5, row + 0.5)
            x1, y1 = transform * (col + 1.5, row + 0.5)
            x2, y2 = transform * (col + 0.5, row + 1.5)
            lon, lat = self.transform_xy(
                np.asarray([x0, x1, x2], dtype=np.float64),
                np.asarray([y0, y1, y2], dtype=np.float64),
                crs,
                CRS_GEOGRAPHIC,
            )
            geod = Geod(ellps="WGS84")
            _, _, dx = geod.inv(lon[0], lat[0], lon[1], lat[1])
            _, _, dy = geod.inv(lon[0], lat[0], lon[2], lat[2])
            candidates = [abs(float(dx)), abs(float(dy))]
            candidates = [value for value in candidates if math.isfinite(value) and value > 0]
            return min(candidates) if candidates else None
        except Exception:
            return None


DEFAULT_TRANSFORM_SERVICE = CoordinateTransformService()


@lru_cache(maxsize=512)
def meridian_convergence_degrees(
    longitude_deg: float,
    latitude_deg: float,
    projected_crs: Any = CRS_TERRAIN_INTERNAL,
) -> float:
    """Return grid-north minus true-north rotation at a geographic point.

    PyProj reports convergence with the sign required by
    ``grid_azimuth = true_azimuth - convergence``.
    """

    try:
        crs = normalize_crs(projected_crs)
        with PYPROJ_TRANSFORMER_LOCK:
            factors = Proj(crs).get_factors(
                float(longitude_deg), float(latitude_deg)
            )
        value = float(factors.meridian_convergence)
        return value if math.isfinite(value) else 0.0
    except Exception:
        return 0.0


__all__ = [
    "CRS_GEOGRAPHIC",
    "CRS_TERRAIN_INTERNAL",
    "PYPROJ_TRANSFORMER_LOCK",
    "CoordinateFrame",
    "CoordinateTransformService",
    "DEFAULT_TRANSFORM_SERVICE",
    "meridian_convergence_degrees",
    "normalize_crs",
]
