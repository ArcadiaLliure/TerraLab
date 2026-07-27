"""Serializable terrain sampling configuration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping

from TerraLab.terrain.render.config import _as_bool, _as_float, _as_int, _lookup

@dataclass(frozen=True)
class TerrainSamplingSettings:
    """Validated subprocess-safe ray sampling policy."""

    adaptive_sampling_enabled: bool = True
    sampling_near_step_m: float = 25.0
    sampling_far_step_m: float = 400.0
    sampling_step_growth: float = 1.045
    sampling_max_projected_error_px: float = 0.75
    sampling_max_elevation_error_m: float = 8.0
    sampling_max_slope_delta_deg: float = 4.0
    sampling_max_subdivision_depth: int = 6
    sampling_max_samples_per_ray: int = 4096

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "TerrainSamplingSettings":
        raw = mapping if isinstance(mapping, Mapping) else {}
        defaults = cls()
        near = max(
            0.5,
            _as_float(
                _lookup(raw, "sampling_near_step_m", defaults.sampling_near_step_m),
                defaults.sampling_near_step_m,
            ),
        )
        far = max(
            near,
            _as_float(
                _lookup(raw, "sampling_far_step_m", defaults.sampling_far_step_m),
                defaults.sampling_far_step_m,
            ),
        )
        return cls(
            adaptive_sampling_enabled=_as_bool(
                _lookup(
                    raw,
                    "adaptive_sampling_enabled",
                    defaults.adaptive_sampling_enabled,
                ),
                defaults.adaptive_sampling_enabled,
            ),
            sampling_near_step_m=near,
            sampling_far_step_m=far,
            sampling_step_growth=max(
                1.0,
                _as_float(
                    _lookup(
                        raw, "sampling_step_growth", defaults.sampling_step_growth
                    ),
                    defaults.sampling_step_growth,
                ),
            ),
            sampling_max_projected_error_px=max(
                0.0,
                _as_float(
                    _lookup(
                        raw,
                        "sampling_max_projected_error_px",
                        defaults.sampling_max_projected_error_px,
                    ),
                    defaults.sampling_max_projected_error_px,
                ),
            ),
            sampling_max_elevation_error_m=max(
                0.0,
                _as_float(
                    _lookup(
                        raw,
                        "sampling_max_elevation_error_m",
                        defaults.sampling_max_elevation_error_m,
                    ),
                    defaults.sampling_max_elevation_error_m,
                ),
            ),
            sampling_max_slope_delta_deg=max(
                0.0,
                _as_float(
                    _lookup(
                        raw,
                        "sampling_max_slope_delta_deg",
                        defaults.sampling_max_slope_delta_deg,
                    ),
                    defaults.sampling_max_slope_delta_deg,
                ),
            ),
            sampling_max_subdivision_depth=max(
                0,
                min(
                    16,
                    _as_int(
                        _lookup(
                            raw,
                            "sampling_max_subdivision_depth",
                            defaults.sampling_max_subdivision_depth,
                        ),
                        defaults.sampling_max_subdivision_depth,
                    ),
                ),
            ),
            sampling_max_samples_per_ray=max(
                2,
                _as_int(
                    _lookup(
                        raw,
                        "sampling_max_samples_per_ray",
                        defaults.sampling_max_samples_per_ray,
                    ),
                    defaults.sampling_max_samples_per_ray,
                ),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def config_keys(cls) -> tuple[str, ...]:
        return tuple(item.name for item in fields(cls))
