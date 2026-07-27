"""Typed point-elevation queries over TerraLab elevation providers."""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from TerraLab.terrain.providers import RasterProvider, create_elevation_provider


DEFAULT_EYE_HEIGHT_M = 1.7


@dataclass(frozen=True, slots=True)
class ElevationQueryResult:
    """One WGS84 point query with explicit coverage and provenance."""

    latitude_deg: float
    longitude_deg: float
    has_coverage: bool
    internal_crs: str
    projected_x_m: float
    projected_y_m: float
    dem_path: str
    provider: str
    source_id: str | None
    nominal_resolution_m: float | None
    elevation_m: float | None
    observer_offset_m: float
    eye_height_m: float
    observer_ground_elevation_m: float | None
    observer_eye_elevation_m: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_gps(latitude: float, longitude: float) -> tuple[float, float]:
    latitude = float(latitude)
    longitude = float(longitude)
    if not math.isfinite(latitude) or not -90.0 <= latitude <= 90.0:
        raise ValueError("latitude must be between -90 and 90 degrees")
    if not math.isfinite(longitude) or not -180.0 <= longitude <= 180.0:
        raise ValueError("longitude must be between -180 and 180 degrees")
    return latitude, longitude


def _validate_path(dem_path: str | os.PathLike[str]) -> str:
    selected = str(dem_path).strip()
    if not selected:
        raise ValueError("a DEM path is required")
    resolved = str(Path(selected).expanduser().resolve())
    if not Path(resolved).exists():
        raise FileNotFoundError(f"DEM source does not exist: {resolved}")
    return resolved


def _source_id(provider: RasterProvider, source_index: int) -> str | None:
    providers = tuple(getattr(provider, "providers", ()) or ())
    selected = (
        providers[source_index]
        if 0 <= source_index < len(providers)
        else provider
    )
    value = str(getattr(selected, "source_id", "") or "").strip()
    return value or None


def query_elevation(
    latitude: float,
    longitude: float,
    *,
    dem_path: str | os.PathLike[str],
    observer_offset_m: float = 0.0,
    eye_height_m: float = DEFAULT_EYE_HEIGHT_M,
) -> ElevationQueryResult:
    """Query a point without inventing elevation outside DEM coverage."""

    latitude, longitude = _validate_gps(latitude, longitude)
    observer_offset_m = float(observer_offset_m)
    eye_height_m = float(eye_height_m)
    if not math.isfinite(observer_offset_m):
        raise ValueError("observer offset must be finite")
    if not math.isfinite(eye_height_m) or eye_height_m < 0.0:
        raise ValueError("eye height must be finite and non-negative")

    resolved_dem_path = _validate_path(dem_path)
    provider = create_elevation_provider(resolved_dem_path)
    try:
        x, y = provider.transform_coordinates(latitude, longitude)
        input_crs = str(getattr(provider, "internal_crs", "EPSG:25831"))
        batch = provider.sample_elevations(float(x), float(y), input_crs=input_crs)
        covered = bool(np.asarray(batch.valid).item())
        elevation = (
            float(np.asarray(batch.values).item()) if covered else None
        )
        source_index = (
            int(np.asarray(batch.source_indices).item()) if covered else -1
        )
        resolution = provider.get_nominal_resolution_m()
        resolution = (
            float(resolution)
            if resolution is not None and math.isfinite(float(resolution))
            else None
        )
        ground = (
            float(elevation) + observer_offset_m
            if elevation is not None
            else None
        )
        return ElevationQueryResult(
            latitude_deg=latitude,
            longitude_deg=longitude,
            has_coverage=covered,
            internal_crs=str(provider.get_native_crs()),
            projected_x_m=float(x),
            projected_y_m=float(y),
            dem_path=resolved_dem_path,
            provider=type(provider).__name__,
            source_id=_source_id(provider, source_index) if covered else None,
            nominal_resolution_m=resolution,
            elevation_m=elevation,
            observer_offset_m=observer_offset_m,
            eye_height_m=eye_height_m,
            observer_ground_elevation_m=ground,
            observer_eye_elevation_m=(
                ground + eye_height_m if ground is not None else None
            ),
        )
    finally:
        provider.close()
