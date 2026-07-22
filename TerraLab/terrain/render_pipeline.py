"""Pure, configurable terrain rendering stages shared by Qt and tests.

This module intentionally imports neither Qt nor DEM providers.  Its values are
safe to serialize into the terrain subprocess and its NumPy functions are safe
to reuse from the GUI renderer.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping

import numpy as np


_REFERENCE_VISIBILITY_RADIUS_KM = 150.0


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _as_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _lookup(mapping: Mapping[str, Any], name: str, default: Any) -> Any:
    if name in mapping:
        return mapping[name]
    terrain = mapping.get("terrain_rendering")
    if isinstance(terrain, Mapping) and name in terrain:
        return terrain[name]
    return default


def _parse_color(value: Any, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 6:
            try:
                return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))
            except ValueError:
                pass
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return tuple(max(0, min(255, int(value[index]))) for index in range(3))
        except (TypeError, ValueError):
            pass
    return tuple(default)


@dataclass(frozen=True, slots=True)
class TerrainRenderSettings:
    """Validated switches and parameters for the GUI-side terrain stages."""

    terrain_lighting_enabled: bool = True
    terrain_light_azimuth_deg: float = 315.0
    terrain_light_elevation_deg: float = 35.0
    terrain_ambient_strength: float = 0.55
    terrain_diffuse_strength: float = 0.55
    terrain_min_brightness: float = 0.45
    terrain_max_brightness: float = 1.15
    terrain_shading_mode: str = "interpolated"
    atmospheric_perspective_enabled: bool = True
    atmosphere_start_distance_km: float = 8.0
    atmosphere_end_distance_km: float = 150.0
    atmosphere_density: float = 2.2
    atmosphere_desaturation_strength: float = 0.55
    atmosphere_contrast_reduction: float = 0.35
    atmosphere_brightness_gain: float = 0.08
    atmosphere_horizon_color: tuple[int, int, int] = (184, 207, 223)
    atmosphere_auto_scale: bool = True
    horizon_antialiasing_enabled: bool = True
    horizon_antialiasing_mode: str = "coverage"
    horizon_supersampling_factor: int = 4
    horizon_filter_width_px: float = 1.25
    terrain_performance_logging_enabled: bool = False

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "TerrainRenderSettings":
        raw = mapping if isinstance(mapping, Mapping) else {}
        defaults = cls()
        mode = str(
            _lookup(raw, "terrain_shading_mode", defaults.terrain_shading_mode)
        ).strip().lower()
        if mode not in {"flat", "vertex", "interpolated"}:
            mode = defaults.terrain_shading_mode
        aa_mode = str(
            _lookup(
                raw,
                "horizon_antialiasing_mode",
                defaults.horizon_antialiasing_mode,
            )
        ).strip().lower()
        if aa_mode not in {"coverage", "supersample", "off"}:
            aa_mode = defaults.horizon_antialiasing_mode
        minimum = max(
            0.0,
            _as_float(
                _lookup(
                    raw,
                    "terrain_min_brightness",
                    defaults.terrain_min_brightness,
                ),
                defaults.terrain_min_brightness,
            ),
        )
        maximum = max(
            minimum,
            _as_float(
                _lookup(
                    raw,
                    "terrain_max_brightness",
                    defaults.terrain_max_brightness,
                ),
                defaults.terrain_max_brightness,
            ),
        )
        start_km = max(
            0.0,
            _as_float(
                _lookup(
                    raw,
                    "atmosphere_start_distance_km",
                    defaults.atmosphere_start_distance_km,
                ),
                defaults.atmosphere_start_distance_km,
            ),
        )
        end_km = max(
            start_km + 0.001,
            _as_float(
                _lookup(
                    raw,
                    "atmosphere_end_distance_km",
                    defaults.atmosphere_end_distance_km,
                ),
                defaults.atmosphere_end_distance_km,
            ),
        )
        return cls(
            terrain_lighting_enabled=_as_bool(
                _lookup(
                    raw,
                    "terrain_lighting_enabled",
                    defaults.terrain_lighting_enabled,
                ),
                defaults.terrain_lighting_enabled,
            ),
            terrain_light_azimuth_deg=_as_float(
                _lookup(
                    raw,
                    "terrain_light_azimuth_deg",
                    defaults.terrain_light_azimuth_deg,
                ),
                defaults.terrain_light_azimuth_deg,
            )
            % 360.0,
            terrain_light_elevation_deg=max(
                -90.0,
                min(
                    90.0,
                    _as_float(
                        _lookup(
                            raw,
                            "terrain_light_elevation_deg",
                            defaults.terrain_light_elevation_deg,
                        ),
                        defaults.terrain_light_elevation_deg,
                    ),
                ),
            ),
            terrain_ambient_strength=max(
                0.0,
                _as_float(
                    _lookup(
                        raw,
                        "terrain_ambient_strength",
                        defaults.terrain_ambient_strength,
                    ),
                    defaults.terrain_ambient_strength,
                ),
            ),
            terrain_diffuse_strength=max(
                0.0,
                _as_float(
                    _lookup(
                        raw,
                        "terrain_diffuse_strength",
                        defaults.terrain_diffuse_strength,
                    ),
                    defaults.terrain_diffuse_strength,
                ),
            ),
            terrain_min_brightness=minimum,
            terrain_max_brightness=maximum,
            terrain_shading_mode=mode,
            atmospheric_perspective_enabled=_as_bool(
                _lookup(
                    raw,
                    "atmospheric_perspective_enabled",
                    defaults.atmospheric_perspective_enabled,
                ),
                defaults.atmospheric_perspective_enabled,
            ),
            atmosphere_start_distance_km=start_km,
            atmosphere_end_distance_km=end_km,
            atmosphere_density=max(
                0.0,
                _as_float(
                    _lookup(
                        raw, "atmosphere_density", defaults.atmosphere_density
                    ),
                    defaults.atmosphere_density,
                ),
            ),
            atmosphere_desaturation_strength=max(
                0.0,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "atmosphere_desaturation_strength",
                            defaults.atmosphere_desaturation_strength,
                        ),
                        defaults.atmosphere_desaturation_strength,
                    ),
                ),
            ),
            atmosphere_contrast_reduction=max(
                0.0,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "atmosphere_contrast_reduction",
                            defaults.atmosphere_contrast_reduction,
                        ),
                        defaults.atmosphere_contrast_reduction,
                    ),
                ),
            ),
            atmosphere_brightness_gain=max(
                0.0,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "atmosphere_brightness_gain",
                            defaults.atmosphere_brightness_gain,
                        ),
                        defaults.atmosphere_brightness_gain,
                    ),
                ),
            ),
            atmosphere_horizon_color=_parse_color(
                _lookup(
                    raw,
                    "atmosphere_horizon_color",
                    defaults.atmosphere_horizon_color,
                ),
                defaults.atmosphere_horizon_color,
            ),
            atmosphere_auto_scale=_as_bool(
                _lookup(
                    raw, "atmosphere_auto_scale", defaults.atmosphere_auto_scale
                ),
                defaults.atmosphere_auto_scale,
            ),
            horizon_antialiasing_enabled=_as_bool(
                _lookup(
                    raw,
                    "horizon_antialiasing_enabled",
                    defaults.horizon_antialiasing_enabled,
                ),
                defaults.horizon_antialiasing_enabled,
            ),
            horizon_antialiasing_mode=aa_mode,
            horizon_supersampling_factor=max(
                1,
                min(
                    8,
                    _as_int(
                        _lookup(
                            raw,
                            "horizon_supersampling_factor",
                            defaults.horizon_supersampling_factor,
                        ),
                        defaults.horizon_supersampling_factor,
                    ),
                ),
            ),
            horizon_filter_width_px=max(
                0.25,
                min(
                    4.0,
                    _as_float(
                        _lookup(
                            raw,
                            "horizon_filter_width_px",
                            defaults.horizon_filter_width_px,
                        ),
                        defaults.horizon_filter_width_px,
                    ),
                ),
            ),
            terrain_performance_logging_enabled=_as_bool(
                _lookup(
                    raw,
                    "terrain_performance_logging_enabled",
                    defaults.terrain_performance_logging_enabled,
                ),
                defaults.terrain_performance_logging_enabled,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["atmosphere_horizon_color"] = list(self.atmosphere_horizon_color)
        return result

    @classmethod
    def config_keys(cls) -> tuple[str, ...]:
        return tuple(item.name for item in fields(cls))


@dataclass(frozen=True, slots=True)
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


def light_direction_enu(azimuth_deg: float, elevation_deg: float) -> np.ndarray:
    """Return a unit vector toward a light in the East/North/Up frame."""

    azimuth = math.radians(float(azimuth_deg))
    elevation = math.radians(float(elevation_deg))
    cos_elevation = math.cos(elevation)
    return np.asarray(
        (
            math.sin(azimuth) * cos_elevation,
            math.cos(azimuth) * cos_elevation,
            math.sin(elevation),
        ),
        dtype=np.float32,
    )


def lambert_intensity(
    normals: np.ndarray,
    settings: TerrainRenderSettings,
) -> np.ndarray:
    """Calculate ambient plus diffuse illumination for shared vertex normals."""

    normal_array = np.asarray(normals, dtype=np.float32)
    if normal_array.shape[-1:] != (3,):
        raise ValueError("Terrain normals must end in three ENU components")
    if not settings.terrain_lighting_enabled:
        return np.ones(normal_array.shape[:-1], dtype=np.float32)
    lengths = np.linalg.norm(normal_array, axis=-1, keepdims=True)
    safe = np.isfinite(lengths) & (lengths > 1e-7)
    unit = np.divide(
        normal_array,
        np.maximum(lengths, 1e-7),
        out=np.zeros_like(normal_array),
        where=safe,
    )
    unit = np.where(safe, unit, np.asarray((0.0, 0.0, 1.0), dtype=np.float32))
    light = light_direction_enu(
        settings.terrain_light_azimuth_deg,
        settings.terrain_light_elevation_deg,
    )
    diffuse = np.maximum(np.sum(unit * light, axis=-1), 0.0)
    intensity = (
        float(settings.terrain_ambient_strength)
        + float(settings.terrain_diffuse_strength) * diffuse
    )
    return np.clip(
        intensity,
        settings.terrain_min_brightness,
        settings.terrain_max_brightness,
    ).astype(np.float32)


def atmospheric_fog_factor(
    distance_m: Any,
    settings: TerrainRenderSettings,
    *,
    maximum_distance_m: float | None = None,
) -> np.ndarray:
    """Return a smooth normalized exponential fog factor in ``[0, 1]``."""

    distances = np.maximum(np.asarray(distance_m, dtype=np.float32), 0.0)
    if not settings.atmospheric_perspective_enabled:
        return np.zeros(distances.shape, dtype=np.float32)
    scale = 1.0
    if (
        settings.atmosphere_auto_scale
        and maximum_distance_m is not None
        and math.isfinite(float(maximum_distance_m))
        and float(maximum_distance_m) > 0.0
    ):
        scale = float(maximum_distance_m) / (_REFERENCE_VISIBILITY_RADIUS_KM * 1000.0)
    start = settings.atmosphere_start_distance_km * 1000.0 * scale
    end = settings.atmosphere_end_distance_km * 1000.0 * scale
    if maximum_distance_m is not None and math.isfinite(float(maximum_distance_m)):
        end = min(end, max(start + 1.0, float(maximum_distance_m)))
    span = max(1.0, end - start)
    normalized = np.clip((distances - start) / span, 0.0, 1.0)
    density = max(0.0, float(settings.atmosphere_density))
    if density <= 1e-8:
        return normalized.astype(np.float32)
    denominator = max(1e-8, 1.0 - math.exp(-density))
    fog = (1.0 - np.exp(-density * normalized)) / denominator
    return np.clip(fog, 0.0, 1.0).astype(np.float32)


def compose_vertex_rgba(
    base_rgba: Any,
    intensity: Any,
    distance_m: Any,
    settings: TerrainRenderSettings,
    *,
    maximum_distance_m: float | None = None,
    horizon_rgb: Any | None = None,
) -> np.ndarray:
    """Apply lighting then atmospheric colour transforms to terrain vertices."""

    rgba = np.asarray(base_rgba)
    if rgba.shape[-1:] != (4,):
        raise ValueError("Terrain base colours must end in RGBA channels")
    rgb = np.asarray(rgba[..., :3], dtype=np.float32)
    alpha = np.asarray(rgba[..., 3:4], dtype=np.float32)
    light = np.broadcast_to(np.asarray(intensity, dtype=np.float32), rgb.shape[:-1])
    if settings.terrain_lighting_enabled:
        rgb = rgb * light[..., None]

    fog = atmospheric_fog_factor(
        distance_m,
        settings,
        maximum_distance_m=maximum_distance_m,
    )
    fog = np.broadcast_to(fog, rgb.shape[:-1]).astype(np.float32)
    if settings.atmospheric_perspective_enabled and np.any(fog > 0.0):
        luminance = np.sum(
            rgb * np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32),
            axis=-1,
            keepdims=True,
        )
        desaturation = (
            fog * float(settings.atmosphere_desaturation_strength)
        )[..., None]
        rgb = rgb * (1.0 - desaturation) + luminance * desaturation

        atmosphere = np.asarray(
            horizon_rgb
            if horizon_rgb is not None
            else settings.atmosphere_horizon_color,
            dtype=np.float32,
        )[:3]
        atmosphere_luminance = float(
            np.dot(atmosphere, np.asarray((0.2126, 0.7152, 0.0722)))
        )
        contrast = (
            1.0 - fog * float(settings.atmosphere_contrast_reduction)
        )[..., None]
        rgb = atmosphere_luminance + (rgb - atmosphere_luminance) * contrast
        rgb += (
            fog * float(settings.atmosphere_brightness_gain) * 255.0
        )[..., None]
        rgb = rgb * (1.0 - fog[..., None]) + atmosphere * fog[..., None]

    return np.concatenate(
        (
            np.clip(np.rint(rgb), 0.0, 255.0),
            np.clip(np.rint(alpha), 0.0, 255.0),
        ),
        axis=-1,
    ).astype(np.uint8)


def apparent_elevation_radians(
    terrain_elevation_m: Any,
    horizontal_distance_m: Any,
    observer_eye_elevation_m: float,
    earth_radius_m: float,
) -> np.ndarray:
    """Calculate the apparent terrain elevation used by raycast and mesh."""

    distance = np.asarray(horizontal_distance_m, dtype=np.float64)
    terrain = np.asarray(terrain_elevation_m, dtype=np.float64)
    safe_distance = np.maximum(distance, 1e-9)
    drop = safe_distance * safe_distance / (2.0 * float(earth_radius_m))
    return np.arctan2(
        terrain - drop - float(observer_eye_elevation_m), safe_distance
    )


def apparent_elevation_degrees(
    terrain_elevation_m: Any,
    horizontal_distance_m: Any,
    observer_eye_elevation_m: float,
    earth_radius_m: float,
) -> np.ndarray:
    """Return :func:`apparent_elevation_radians` in degrees."""

    return np.degrees(
        apparent_elevation_radians(
            terrain_elevation_m,
            horizontal_distance_m,
            observer_eye_elevation_m,
            earth_radius_m,
        )
    )
