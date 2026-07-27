"""Data contracts and pure resolution logic for visibility range."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping


EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class TerrainRangeSettings:
    mode: str = "auto"
    manual_radius_km: float = 150.0
    minimum_radius_km: float = 25.0
    maximum_radius_km: float = 530.0
    target_max_elevation_m: float = 8_849.0
    atmospheric_refraction_enabled: bool = True
    effective_earth_radius_factor: float = 7.0 / 6.0
    immediate_preload_radius_km: float = 25.0

    def validated(self) -> "TerrainRangeSettings":
        mode = str(self.mode).strip().lower()
        if mode not in {"auto", "manual"}:
            mode = "auto"
        minimum = max(1.0, float(self.minimum_radius_km))
        maximum = min(530.0, max(minimum, float(self.maximum_radius_km)))
        return TerrainRangeSettings(
            mode=mode,
            manual_radius_km=min(maximum, max(minimum, float(self.manual_radius_km))),
            minimum_radius_km=minimum,
            maximum_radius_km=maximum,
            target_max_elevation_m=max(0.0, float(self.target_max_elevation_m)),
            atmospheric_refraction_enabled=bool(self.atmospheric_refraction_enabled),
            effective_earth_radius_factor=max(1.0, float(self.effective_earth_radius_factor)),
            immediate_preload_radius_km=max(1.0, min(maximum, float(self.immediate_preload_radius_km))),
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "TerrainRangeSettings":
        values = values or {}
        kwargs = {field: values[field] for field in asdict(cls()) if field in values}
        return cls(**kwargs).validated()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.validated())


@dataclass(frozen=True)
class TerrainVisibilityRange:
    settings: TerrainRangeSettings
    observer_elevation_m: float
    observer_horizon_m: float
    target_horizon_m: float
    calculated_radius_m: float
    resolved_radius_m: float


def horizon_distance_m(height_m: float, earth_radius_m: float) -> float:
    """Exact tangent distance ``sqrt(2 R h + h²)`` for non-negative height."""
    height = max(0.0, float(height_m))
    radius = float(earth_radius_m)
    return math.sqrt(2.0 * radius * height + height * height)


def resolve_visibility_range(
    settings: TerrainRangeSettings,
    observer_elevation_m: float,
) -> TerrainVisibilityRange:
    settings = settings.validated()
    factor = settings.effective_earth_radius_factor if settings.atmospheric_refraction_enabled else 1.0
    effective_radius = EARTH_RADIUS_M * factor
    observer = max(0.0, float(observer_elevation_m))
    observer_horizon = horizon_distance_m(observer, effective_radius)
    target_horizon = horizon_distance_m(settings.target_max_elevation_m, effective_radius)
    calculated = (
        settings.manual_radius_km * 1000.0
        if settings.mode == "manual"
        else observer_horizon + target_horizon
    )
    resolved = min(settings.maximum_radius_km * 1000.0, max(settings.minimum_radius_km * 1000.0, calculated))
    return TerrainVisibilityRange(settings, observer, observer_horizon, target_horizon, calculated, resolved)
