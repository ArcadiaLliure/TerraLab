"""Pure, configurable terrain rendering stages shared by Qt and tests.

This module intentionally imports neither Qt nor DEM providers.  Its values are
safe to serialize into the terrain subprocess and its NumPy functions are safe
to reuse from the GUI renderer.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Any, Mapping

import numpy as np


_REFERENCE_VISIBILITY_RADIUS_KM = 150.0


class SurfaceVisualStyle(str, Enum):
    """Presentation applied after resolving the immutable surface source."""

    ORIGINAL = "original"
    VIBRANT = "vibrant"


@dataclass(frozen=True, slots=True)
class TerrainCelestialLightContext:
    """Astronomical inputs shared by every terrain representation."""

    sun_altitude_deg: float
    sun_azimuth_deg: float
    moon_altitude_deg: float | None = None
    moon_azimuth_deg: float | None = None
    moon_illumination: float = 0.0
    eclipse_factor: float = 1.0

    def validated(self) -> "TerrainCelestialLightContext":
        """Return finite, bounded values without inventing lunar ephemerides."""

        sun_altitude = _as_float(self.sun_altitude_deg, -90.0)
        sun_azimuth = _as_float(self.sun_azimuth_deg, 0.0)
        try:
            moon_altitude = float(self.moon_altitude_deg)
            moon_azimuth = float(self.moon_azimuth_deg)
        except (TypeError, ValueError):
            moon_altitude = None
            moon_azimuth = None
        if (
            moon_altitude is not None
            and (
                not math.isfinite(moon_altitude)
                or not math.isfinite(moon_azimuth)
            )
        ):
            moon_altitude = None
            moon_azimuth = None
        moon_illumination = (
            0.0
            if moon_altitude is None
            else max(
                0.0, min(1.0, _as_float(self.moon_illumination, 0.0))
            )
        )
        return TerrainCelestialLightContext(
            sun_altitude_deg=max(-90.0, min(90.0, sun_altitude)),
            sun_azimuth_deg=sun_azimuth % 360.0,
            moon_altitude_deg=(
                None
                if moon_altitude is None
                else max(-90.0, min(90.0, moon_altitude))
            ),
            moon_azimuth_deg=(
                None if moon_azimuth is None else moon_azimuth % 360.0
            ),
            moon_illumination=moon_illumination,
            eclipse_factor=max(
                0.0, min(1.0, _as_float(self.eclipse_factor, 1.0))
            ),
        )


def normalize_surface_visual_style(value: Any) -> str:
    """Return a stable serialized visual-style value."""

    text = str(getattr(value, "value", value) or "").strip().lower()
    return (
        SurfaceVisualStyle.VIBRANT.value
        if text == SurfaceVisualStyle.VIBRANT.value
        else SurfaceVisualStyle.ORIGINAL.value
    )


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


def _parse_multiplier(
    value: Any, default: tuple[float, float, float]
) -> tuple[float, float, float]:
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            parsed = tuple(float(value[index]) for index in range(3))
        except (TypeError, ValueError):
            parsed = ()
        if len(parsed) == 3 and all(math.isfinite(item) for item in parsed):
            return tuple(max(0.0, min(2.0, item)) for item in parsed)
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
    terrain_twilight_dark_altitude_deg: float = -18.0
    terrain_sun_full_altitude_deg: float = 6.0
    terrain_night_ambient_strength: float = 0.015
    terrain_moon_horizon_fade_start_deg: float = -1.0
    terrain_moon_horizon_fade_end_deg: float = 10.0
    terrain_moon_ambient_strength: float = 0.035
    terrain_moon_diffuse_strength: float = 0.14
    terrain_moon_phase_exponent: float = 2.2
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
    surface_visual_style: str = SurfaceVisualStyle.ORIGINAL.value
    vibrant_intensity: float = 1.0
    vibrant_use_astronomical_sun: bool = True
    vibrant_sun_ambient_strength: float = 0.68
    vibrant_sun_diffuse_boost: float = 1.62
    vibrant_sunlight_exponent: float = 1.16
    vibrant_sun_min_brightness: float = 0.58
    vibrant_sun_max_brightness: float = 1.31
    vibrant_saturation_strength: float = 0.10
    vibrant_saturation_soft_limit: float = 0.80
    vibrant_saturation_compression: float = 0.56
    vibrant_mid_contrast: float = 0.07
    vibrant_midtone_lift: float = 0.034
    vibrant_black_lift: float = 0.035
    vibrant_highlight_compression: float = 0.085
    vibrant_bloom_strength: float = 0.15
    vibrant_bloom_threshold: float = 0.74
    vibrant_bloom_radius_px: float = 6.25
    vibrant_haze_strength: float = 1.0
    vibrant_linear_depth_haze: bool = True
    vibrant_distance_desaturation: float = 0.10
    vibrant_distance_contrast_reduction: float = 0.10
    vibrant_distance_brightness_gain: float = 0.04
    vibrant_atmosphere_color: tuple[int, int, int] = (174, 185, 199)
    vibrant_shadow_sky_mix: float = 0.24
    vibrant_shadow_sky_color: tuple[int, int, int] = (174, 185, 199)
    vibrant_shadow_tint: tuple[float, float, float] = (0.97, 0.992, 1.035)
    vibrant_midtones_tint: tuple[float, float, float] = (1.018, 1.012, 0.996)
    vibrant_highlights_tint: tuple[float, float, float] = (1.06, 1.035, 0.98)
    vibrant_sun_tint: tuple[float, float, float] = (1.055, 1.025, 0.97)
    vibrant_moon_tint: tuple[float, float, float] = (0.88, 0.93, 1.0)
    vibrant_night_chroma: float = 0.18
    vibrant_full_moon_chroma: float = 0.40
    vibrant_moon_bloom_scale: float = 0.12
    vibrant_territorial_luminance_variation: float = 0.04
    vibrant_territorial_hue_variation: float = 0.014
    vibrant_material_midscale_variation: float = 0.024
    vibrant_material_microscale_variation: float = 0.007
    vibrant_material_altitude_influence: float = 0.055
    vibrant_material_slope_influence: float = 0.06
    vibrant_ambient_occlusion_strength: float = 0.12
    vibrant_ambient_occlusion_radius_px: float = 5.0
    vibrant_ambient_occlusion_relief_scale_m: float = 42.0
    vibrant_snow_rock_blend: float = 0.28
    vibrant_water_shore_variation: float = 0.08
    vibrant_valley_haze_strength: float = 0.10
    # Retained as a compatibility/configuration key.  The main UI now drives
    # regularization through ``surface_visual_style == "vibrant"``.
    categorical_edge_smoothing_enabled: bool = False
    categorical_region_smoothing_radius_px: float = 8.0
    categorical_edge_smoothing_strength: float = 0.76
    terrain_performance_logging_enabled: bool = False
    terrain_surface_diagnostics_enabled: bool = False

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
        vibrant_sun_minimum = max(
            0.0,
            min(
                2.0,
                _as_float(
                    _lookup(
                        raw,
                        "vibrant_sun_min_brightness",
                        defaults.vibrant_sun_min_brightness,
                    ),
                    defaults.vibrant_sun_min_brightness,
                ),
            ),
        )
        vibrant_sun_maximum = max(
            vibrant_sun_minimum,
            min(
                3.0,
                _as_float(
                    _lookup(
                        raw,
                        "vibrant_sun_max_brightness",
                        defaults.vibrant_sun_max_brightness,
                    ),
                    defaults.vibrant_sun_max_brightness,
                ),
            ),
        )
        twilight_dark_altitude = max(
            -30.0,
            min(
                0.0,
                _as_float(
                    _lookup(
                        raw,
                        "terrain_twilight_dark_altitude_deg",
                        defaults.terrain_twilight_dark_altitude_deg,
                    ),
                    defaults.terrain_twilight_dark_altitude_deg,
                ),
            ),
        )
        sun_full_altitude = max(
            0.0,
            min(
                30.0,
                _as_float(
                    _lookup(
                        raw,
                        "terrain_sun_full_altitude_deg",
                        defaults.terrain_sun_full_altitude_deg,
                    ),
                    defaults.terrain_sun_full_altitude_deg,
                ),
            ),
        )
        moon_fade_start = max(
            -10.0,
            min(
                10.0,
                _as_float(
                    _lookup(
                        raw,
                        "terrain_moon_horizon_fade_start_deg",
                        defaults.terrain_moon_horizon_fade_start_deg,
                    ),
                    defaults.terrain_moon_horizon_fade_start_deg,
                ),
            ),
        )
        moon_fade_end = max(
            moon_fade_start + 0.1,
            min(
                45.0,
                _as_float(
                    _lookup(
                        raw,
                        "terrain_moon_horizon_fade_end_deg",
                        defaults.terrain_moon_horizon_fade_end_deg,
                    ),
                    defaults.terrain_moon_horizon_fade_end_deg,
                ),
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
            terrain_twilight_dark_altitude_deg=twilight_dark_altitude,
            terrain_sun_full_altitude_deg=sun_full_altitude,
            terrain_night_ambient_strength=max(
                0.0,
                min(
                    0.2,
                    _as_float(
                        _lookup(
                            raw,
                            "terrain_night_ambient_strength",
                            defaults.terrain_night_ambient_strength,
                        ),
                        defaults.terrain_night_ambient_strength,
                    ),
                ),
            ),
            terrain_moon_horizon_fade_start_deg=moon_fade_start,
            terrain_moon_horizon_fade_end_deg=moon_fade_end,
            terrain_moon_ambient_strength=max(
                0.0,
                min(
                    0.2,
                    _as_float(
                        _lookup(
                            raw,
                            "terrain_moon_ambient_strength",
                            defaults.terrain_moon_ambient_strength,
                        ),
                        defaults.terrain_moon_ambient_strength,
                    ),
                ),
            ),
            terrain_moon_diffuse_strength=max(
                0.0,
                min(
                    0.5,
                    _as_float(
                        _lookup(
                            raw,
                            "terrain_moon_diffuse_strength",
                            defaults.terrain_moon_diffuse_strength,
                        ),
                        defaults.terrain_moon_diffuse_strength,
                    ),
                ),
            ),
            terrain_moon_phase_exponent=max(
                0.5,
                min(
                    6.0,
                    _as_float(
                        _lookup(
                            raw,
                            "terrain_moon_phase_exponent",
                            defaults.terrain_moon_phase_exponent,
                        ),
                        defaults.terrain_moon_phase_exponent,
                    ),
                ),
            ),
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
            surface_visual_style=normalize_surface_visual_style(
                _lookup(
                    raw,
                    "surface_visual_style",
                    defaults.surface_visual_style,
                )
            ),
            vibrant_intensity=max(
                0.0,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_intensity",
                            defaults.vibrant_intensity,
                        ),
                        defaults.vibrant_intensity,
                    ),
                ),
            ),
            vibrant_use_astronomical_sun=_as_bool(
                _lookup(
                    raw,
                    "vibrant_use_astronomical_sun",
                    defaults.vibrant_use_astronomical_sun,
                ),
                defaults.vibrant_use_astronomical_sun,
            ),
            vibrant_sun_ambient_strength=max(
                0.0,
                min(
                    2.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_sun_ambient_strength",
                            defaults.vibrant_sun_ambient_strength,
                        ),
                        defaults.vibrant_sun_ambient_strength,
                    ),
                ),
            ),
            vibrant_sun_diffuse_boost=max(
                0.0,
                min(
                    3.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_sun_diffuse_boost",
                            defaults.vibrant_sun_diffuse_boost,
                        ),
                        defaults.vibrant_sun_diffuse_boost,
                    ),
                ),
            ),
            vibrant_sunlight_exponent=max(
                0.25,
                min(
                    4.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_sunlight_exponent",
                            defaults.vibrant_sunlight_exponent,
                        ),
                        defaults.vibrant_sunlight_exponent,
                    ),
                ),
            ),
            vibrant_sun_min_brightness=vibrant_sun_minimum,
            vibrant_sun_max_brightness=vibrant_sun_maximum,
            vibrant_saturation_strength=max(
                0.0,
                min(
                    0.5,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_saturation_strength",
                            defaults.vibrant_saturation_strength,
                        ),
                        defaults.vibrant_saturation_strength,
                    ),
                ),
            ),
            vibrant_saturation_soft_limit=max(
                0.4,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_saturation_soft_limit",
                            defaults.vibrant_saturation_soft_limit,
                        ),
                        defaults.vibrant_saturation_soft_limit,
                    ),
                ),
            ),
            vibrant_saturation_compression=max(
                0.0,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_saturation_compression",
                            defaults.vibrant_saturation_compression,
                        ),
                        defaults.vibrant_saturation_compression,
                    ),
                ),
            ),
            vibrant_mid_contrast=max(
                0.0,
                min(
                    0.5,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_mid_contrast",
                            defaults.vibrant_mid_contrast,
                        ),
                        defaults.vibrant_mid_contrast,
                    ),
                ),
            ),
            vibrant_midtone_lift=max(
                0.0,
                min(
                    0.15,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_midtone_lift",
                            defaults.vibrant_midtone_lift,
                        ),
                        defaults.vibrant_midtone_lift,
                    ),
                ),
            ),
            vibrant_black_lift=max(
                0.0,
                min(
                    0.2,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_black_lift",
                            defaults.vibrant_black_lift,
                        ),
                        defaults.vibrant_black_lift,
                    ),
                ),
            ),
            vibrant_highlight_compression=max(
                0.0,
                min(
                    0.5,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_highlight_compression",
                            defaults.vibrant_highlight_compression,
                        ),
                        defaults.vibrant_highlight_compression,
                    ),
                ),
            ),
            vibrant_bloom_strength=max(
                0.0,
                min(
                    0.5,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_bloom_strength",
                            defaults.vibrant_bloom_strength,
                        ),
                        defaults.vibrant_bloom_strength,
                    ),
                ),
            ),
            vibrant_bloom_threshold=max(
                0.0,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_bloom_threshold",
                            defaults.vibrant_bloom_threshold,
                        ),
                        defaults.vibrant_bloom_threshold,
                    ),
                ),
            ),
            vibrant_bloom_radius_px=max(
                0.0,
                min(
                    24.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_bloom_radius_px",
                            defaults.vibrant_bloom_radius_px,
                        ),
                        defaults.vibrant_bloom_radius_px,
                    ),
                ),
            ),
            vibrant_haze_strength=max(
                0.0,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_haze_strength",
                            defaults.vibrant_haze_strength,
                        ),
                        defaults.vibrant_haze_strength,
                    ),
                ),
            ),
            vibrant_linear_depth_haze=_as_bool(
                _lookup(
                    raw,
                    "vibrant_linear_depth_haze",
                    defaults.vibrant_linear_depth_haze,
                ),
                defaults.vibrant_linear_depth_haze,
            ),
            vibrant_distance_desaturation=max(
                0.0,
                min(
                    0.5,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_distance_desaturation",
                            defaults.vibrant_distance_desaturation,
                        ),
                        defaults.vibrant_distance_desaturation,
                    ),
                ),
            ),
            vibrant_distance_contrast_reduction=max(
                0.0,
                min(
                    0.5,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_distance_contrast_reduction",
                            defaults.vibrant_distance_contrast_reduction,
                        ),
                        defaults.vibrant_distance_contrast_reduction,
                    ),
                ),
            ),
            vibrant_distance_brightness_gain=max(
                0.0,
                min(
                    0.2,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_distance_brightness_gain",
                            defaults.vibrant_distance_brightness_gain,
                        ),
                        defaults.vibrant_distance_brightness_gain,
                    ),
                ),
            ),
            vibrant_atmosphere_color=_parse_color(
                _lookup(
                    raw,
                    "vibrant_atmosphere_color",
                    defaults.vibrant_atmosphere_color,
                ),
                defaults.vibrant_atmosphere_color,
            ),
            vibrant_shadow_sky_mix=max(
                0.0,
                min(
                    0.5,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_shadow_sky_mix",
                            defaults.vibrant_shadow_sky_mix,
                        ),
                        defaults.vibrant_shadow_sky_mix,
                    ),
                ),
            ),
            vibrant_shadow_sky_color=_parse_color(
                _lookup(
                    raw,
                    "vibrant_shadow_sky_color",
                    defaults.vibrant_shadow_sky_color,
                ),
                defaults.vibrant_shadow_sky_color,
            ),
            vibrant_shadow_tint=_parse_multiplier(
                _lookup(
                    raw,
                    "vibrant_shadow_tint",
                    defaults.vibrant_shadow_tint,
                ),
                defaults.vibrant_shadow_tint,
            ),
            vibrant_midtones_tint=_parse_multiplier(
                _lookup(
                    raw,
                    "vibrant_midtones_tint",
                    defaults.vibrant_midtones_tint,
                ),
                defaults.vibrant_midtones_tint,
            ),
            vibrant_highlights_tint=_parse_multiplier(
                _lookup(
                    raw,
                    "vibrant_highlights_tint",
                    defaults.vibrant_highlights_tint,
                ),
                defaults.vibrant_highlights_tint,
            ),
            vibrant_sun_tint=_parse_multiplier(
                _lookup(
                    raw,
                    "vibrant_sun_tint",
                    defaults.vibrant_sun_tint,
                ),
                defaults.vibrant_sun_tint,
            ),
            vibrant_moon_tint=_parse_multiplier(
                _lookup(
                    raw,
                    "vibrant_moon_tint",
                    defaults.vibrant_moon_tint,
                ),
                defaults.vibrant_moon_tint,
            ),
            vibrant_night_chroma=max(
                0.0,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_night_chroma",
                            defaults.vibrant_night_chroma,
                        ),
                        defaults.vibrant_night_chroma,
                    ),
                ),
            ),
            vibrant_full_moon_chroma=max(
                0.0,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_full_moon_chroma",
                            defaults.vibrant_full_moon_chroma,
                        ),
                        defaults.vibrant_full_moon_chroma,
                    ),
                ),
            ),
            vibrant_moon_bloom_scale=max(
                0.0,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_moon_bloom_scale",
                            defaults.vibrant_moon_bloom_scale,
                        ),
                        defaults.vibrant_moon_bloom_scale,
                    ),
                ),
            ),
            vibrant_territorial_luminance_variation=max(
                0.0,
                min(
                    0.12,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_territorial_luminance_variation",
                            defaults.vibrant_territorial_luminance_variation,
                        ),
                        defaults.vibrant_territorial_luminance_variation,
                    ),
                ),
            ),
            vibrant_territorial_hue_variation=max(
                0.0,
                min(
                    0.08,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_territorial_hue_variation",
                            defaults.vibrant_territorial_hue_variation,
                        ),
                        defaults.vibrant_territorial_hue_variation,
                    ),
                ),
            ),
            vibrant_material_midscale_variation=max(
                0.0,
                min(
                    0.08,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_material_midscale_variation",
                            defaults.vibrant_material_midscale_variation,
                        ),
                        defaults.vibrant_material_midscale_variation,
                    ),
                ),
            ),
            vibrant_material_microscale_variation=max(
                0.0,
                min(
                    0.03,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_material_microscale_variation",
                            defaults.vibrant_material_microscale_variation,
                        ),
                        defaults.vibrant_material_microscale_variation,
                    ),
                ),
            ),
            vibrant_material_altitude_influence=max(
                0.0,
                min(
                    0.2,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_material_altitude_influence",
                            defaults.vibrant_material_altitude_influence,
                        ),
                        defaults.vibrant_material_altitude_influence,
                    ),
                ),
            ),
            vibrant_material_slope_influence=max(
                0.0,
                min(
                    0.2,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_material_slope_influence",
                            defaults.vibrant_material_slope_influence,
                        ),
                        defaults.vibrant_material_slope_influence,
                    ),
                ),
            ),
            vibrant_ambient_occlusion_strength=max(
                0.0,
                min(
                    0.4,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_ambient_occlusion_strength",
                            defaults.vibrant_ambient_occlusion_strength,
                        ),
                        defaults.vibrant_ambient_occlusion_strength,
                    ),
                ),
            ),
            vibrant_ambient_occlusion_radius_px=max(
                0.0,
                min(
                    24.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_ambient_occlusion_radius_px",
                            defaults.vibrant_ambient_occlusion_radius_px,
                        ),
                        defaults.vibrant_ambient_occlusion_radius_px,
                    ),
                ),
            ),
            vibrant_ambient_occlusion_relief_scale_m=max(
                1.0,
                min(
                    500.0,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_ambient_occlusion_relief_scale_m",
                            defaults.vibrant_ambient_occlusion_relief_scale_m,
                        ),
                        defaults.vibrant_ambient_occlusion_relief_scale_m,
                    ),
                ),
            ),
            vibrant_snow_rock_blend=max(
                0.0,
                min(
                    0.75,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_snow_rock_blend",
                            defaults.vibrant_snow_rock_blend,
                        ),
                        defaults.vibrant_snow_rock_blend,
                    ),
                ),
            ),
            vibrant_water_shore_variation=max(
                0.0,
                min(
                    0.3,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_water_shore_variation",
                            defaults.vibrant_water_shore_variation,
                        ),
                        defaults.vibrant_water_shore_variation,
                    ),
                ),
            ),
            vibrant_valley_haze_strength=max(
                0.0,
                min(
                    0.4,
                    _as_float(
                        _lookup(
                            raw,
                            "vibrant_valley_haze_strength",
                            defaults.vibrant_valley_haze_strength,
                        ),
                        defaults.vibrant_valley_haze_strength,
                    ),
                ),
            ),
            categorical_edge_smoothing_enabled=_as_bool(
                _lookup(
                    raw,
                    "categorical_edge_smoothing_enabled",
                    defaults.categorical_edge_smoothing_enabled,
                ),
                defaults.categorical_edge_smoothing_enabled,
            ),
            categorical_region_smoothing_radius_px=max(
                0.0,
                min(
                    16.0,
                    _as_float(
                        _lookup(
                            raw,
                            "categorical_region_smoothing_radius_px",
                            defaults.categorical_region_smoothing_radius_px,
                        ),
                        defaults.categorical_region_smoothing_radius_px,
                    ),
                ),
            ),
            categorical_edge_smoothing_strength=max(
                0.0,
                min(
                    1.0,
                    _as_float(
                        _lookup(
                            raw,
                            "categorical_edge_smoothing_strength",
                            defaults.categorical_edge_smoothing_strength,
                        ),
                        defaults.categorical_edge_smoothing_strength,
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
            terrain_surface_diagnostics_enabled=_as_bool(
                _lookup(
                    raw,
                    "terrain_surface_diagnostics_enabled",
                    defaults.terrain_surface_diagnostics_enabled,
                ),
                defaults.terrain_surface_diagnostics_enabled,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in (
            "atmosphere_horizon_color",
            "vibrant_atmosphere_color",
            "vibrant_shadow_sky_color",
            "vibrant_shadow_tint",
            "vibrant_midtones_tint",
            "vibrant_highlights_tint",
            "vibrant_sun_tint",
            "vibrant_moon_tint",
        ):
            result[key] = list(result[key])
        return result

    @classmethod
    def config_keys(cls) -> tuple[str, ...]:
        return tuple(item.name for item in fields(cls))


@dataclass(frozen=True, slots=True)
class TerrainCelestialLightFactors:
    """Continuous global weights derived from the astronomical context."""

    solar_ambient: float
    solar_direct: float
    lunar_strength: float

    @property
    def night(self) -> float:
        return 1.0 - self.solar_ambient


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    if edge0 == edge1:
        return 1.0 if value >= edge1 else 0.0
    amount = max(0.0, min(1.0, (float(value) - edge0) / (edge1 - edge0)))
    return amount * amount * (3.0 - 2.0 * amount)


def terrain_celestial_light_factors(
    context: TerrainCelestialLightContext,
    settings: TerrainRenderSettings,
) -> TerrainCelestialLightFactors:
    """Resolve smooth sunlight, twilight and physically gated moonlight."""

    light = context.validated()
    solar_ambient = _smoothstep(
        float(settings.terrain_twilight_dark_altitude_deg),
        float(settings.terrain_sun_full_altitude_deg),
        light.sun_altitude_deg,
    )
    solar_direct = _smoothstep(
        -0.833,
        float(settings.terrain_sun_full_altitude_deg),
        light.sun_altitude_deg,
    )
    solar_ambient *= light.eclipse_factor
    solar_direct *= light.eclipse_factor

    lunar_strength = 0.0
    if (
        light.moon_altitude_deg is not None
        and light.moon_azimuth_deg is not None
        and light.moon_illumination > 0.0
    ):
        moon_visibility = _smoothstep(
            float(settings.terrain_moon_horizon_fade_start_deg),
            float(settings.terrain_moon_horizon_fade_end_deg),
            light.moon_altitude_deg,
        )
        moon_phase = light.moon_illumination ** float(
            settings.terrain_moon_phase_exponent
        )
        daylight_suppression = 1.0 - _smoothstep(
            float(settings.terrain_twilight_dark_altitude_deg),
            0.0,
            light.sun_altitude_deg,
        )
        lunar_strength = moon_visibility * moon_phase * daylight_suppression

    return TerrainCelestialLightFactors(
        solar_ambient=max(0.0, min(1.0, solar_ambient)),
        solar_direct=max(0.0, min(1.0, solar_direct)),
        lunar_strength=max(0.0, min(1.0, lunar_strength)),
    )


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


def vibrant_depth_haze_factor(
    distance_m: Any,
    settings: TerrainRenderSettings,
    *,
    maximum_distance_m: float | None = None,
) -> np.ndarray:
    """Return the configurable BOTW-style depth interpolation factor."""

    distances = np.maximum(np.asarray(distance_m, dtype=np.float32), 0.0)
    if not settings.atmospheric_perspective_enabled:
        return np.zeros(distances.shape, dtype=np.float32)
    if not settings.vibrant_linear_depth_haze:
        return atmospheric_fog_factor(
            distances,
            settings,
            maximum_distance_m=maximum_distance_m,
        )
    depth_limit = float(
        maximum_distance_m
        if maximum_distance_m is not None
        and math.isfinite(float(maximum_distance_m))
        and float(maximum_distance_m) > 0.0
        else settings.atmosphere_end_distance_km * 1000.0
    )
    return np.clip(
        distances / max(depth_limit, 1.0), 0.0, 1.0
    ).astype(np.float32)


def compose_vertex_rgba(
    base_rgba: Any,
    intensity: Any,
    distance_m: Any,
    settings: TerrainRenderSettings,
    *,
    maximum_distance_m: float | None = None,
    horizon_rgb: Any | None = None,
    atmosphere_strength: float = 1.0,
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
    fog *= max(0.0, min(1.0, float(atmosphere_strength)))
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
        atmospheric_daylight = max(
            0.0, min(1.0, atmosphere_luminance / 128.0)
        )
        rgb += (
            fog
            * float(settings.atmosphere_brightness_gain)
            * atmospheric_daylight
            * 255.0
        )[..., None]
        rgb = rgb * (1.0 - fog[..., None]) + atmosphere * fog[..., None]

    return np.concatenate(
        (
            np.clip(np.rint(rgb), 0.0, 255.0),
            np.clip(np.rint(alpha), 0.0, 255.0),
        ),
        axis=-1,
    ).astype(np.uint8)


def apply_vibrant_color_grade(
    rgba: Any,
    intensity: Any,
    distance_m: Any,
    settings: TerrainRenderSettings,
    *,
    maximum_distance_m: float | None = None,
    valid_mask: Any | None = None,
    additional_haze: Any | None = None,
    daylight_factor: Any = 1.0,
    moonlight_factor: Any = 0.0,
    solar_exposure: Any | None = None,
    lunar_exposure: Any | None = None,
    atmosphere_rgb: Any | None = None,
) -> np.ndarray:
    """Apply the naturalistic, non-destructive Vibrant colour pipeline.

    A post-shading 8-10% saturation lift is hue-weighted and bounded by a
    soft shoulder so intense colours do not clip.  Tone, split-lighting and
    distance atmosphere remain independent, and bloom stays in a separate
    screen-space pass because it needs neighbouring pixels.
    """

    source = np.asarray(rgba, dtype=np.uint8)
    if source.shape[-1:] != (4,):
        raise ValueError("Vibrant colour grading expects RGBA channels")
    if normalize_surface_visual_style(settings.surface_visual_style) != (
        SurfaceVisualStyle.VIBRANT.value
    ):
        return source.copy()

    shape = source.shape[:-1]
    mask = (
        np.asarray(source[..., 3] > 0, dtype=bool)
        if valid_mask is None
        else np.broadcast_to(np.asarray(valid_mask, dtype=bool), shape)
    )
    if not np.any(mask):
        return source.copy()

    amount = float(settings.vibrant_intensity)
    rgb = np.asarray(source[..., :3], dtype=np.float32) / 255.0
    working = rgb.copy()
    daylight = np.clip(
        np.broadcast_to(
            np.asarray(daylight_factor, dtype=np.float32), shape
        ),
        0.0,
        1.0,
    )
    moonlight = np.clip(
        np.broadcast_to(
            np.asarray(moonlight_factor, dtype=np.float32), shape
        ),
        0.0,
        1.0,
    )
    light = np.broadcast_to(np.asarray(intensity, dtype=np.float32), shape)
    if solar_exposure is None:
        solar = np.clip((light - 0.98) / 0.31, 0.0, 1.0)
    else:
        solar = np.clip(
            np.broadcast_to(
                np.asarray(solar_exposure, dtype=np.float32), shape
            ),
            0.0,
            1.0,
        )
    if lunar_exposure is None:
        lunar = np.zeros(shape, dtype=np.float32)
    else:
        lunar = np.clip(
            np.broadcast_to(
                np.asarray(lunar_exposure, dtype=np.float32), shape
            ),
            0.0,
            1.0,
        )
    day_amount = amount * daylight

    luma_weights = np.asarray(
        (0.2126, 0.7152, 0.0722), dtype=np.float32
    )

    # A modest toe lift avoids closed black terrain.  The stronger shoulder
    # prevents direct DEM lighting from clipping one colour channel before
    # the chromatic treatment is applied.
    black_lift = float(settings.vibrant_black_lift) * day_amount
    working = (
        black_lift[..., None]
        + (1.0 - black_lift[..., None]) * working
    )
    midtone_lift = float(settings.vibrant_midtone_lift) * day_amount
    tone_luminance = np.sum(working * luma_weights, axis=-1)
    lifted_luminance = tone_luminance + (
        midtone_lift
        * 4.0
        * tone_luminance
        * (1.0 - tone_luminance)
    )
    working *= np.divide(
        lifted_luminance,
        np.maximum(tone_luminance, 1e-5),
        out=np.ones_like(tone_luminance),
        where=tone_luminance > 1e-5,
    )[..., None]
    contrast = float(settings.vibrant_mid_contrast) * day_amount
    working += (
        contrast[..., None]
        * (working - 0.5)
        * 4.0
        * working
        * (1.0 - working)
    )
    shoulder = float(settings.vibrant_highlight_compression) * day_amount
    highlight_excess = np.maximum(working - 0.68, 0.0)
    working -= (
        shoulder[..., None]
        * highlight_excess
        * highlight_excess
        / 0.32
    )
    working = np.clip(working, 0.0, 0.992)

    luminance = np.sum(working * luma_weights, axis=-1)
    channel_max = np.max(working, axis=-1)
    channel_min = np.min(working, axis=-1)
    saturation = np.divide(
        channel_max - channel_min,
        np.maximum(channel_max, 1e-6),
        out=np.zeros(shape, dtype=np.float32),
        where=channel_max > 1e-6,
    )
    dominant = np.argmax(working, axis=-1)
    red, green, blue = (
        working[..., 0],
        working[..., 1],
        working[..., 2],
    )
    warm = (red >= green * 0.90) & (green > blue * 1.08)
    yellow_green = (
        (red > 0.34)
        & (green > 0.34)
        & (blue < np.minimum(red, green) * 0.72)
    )
    grey_brown = (
        (red > green)
        & (green > blue)
        & (saturation < 0.42)
    )

    hue_weight = np.full(shape, 0.90, dtype=np.float32)
    hue_weight[dominant == 1] = 1.0
    hue_weight[dominant == 2] = 0.96
    hue_weight[warm] = 0.82
    hue_weight[luminance < 0.22] *= 0.75
    hue_weight[saturation < 0.08] *= 0.25
    saturation_gain = (
        float(settings.vibrant_saturation_strength) * day_amount
    )
    target_saturation = saturation * (
        1.0 + saturation_gain * hue_weight
    )
    # A soft shoulder is preferable to a hard clamp: already saturated
    # sources retain hue differences, but primary greens and yellows can no
    # longer reach the marker-pen look caused by channel clipping.
    saturation_limit = float(settings.vibrant_saturation_soft_limit)
    saturation_compression = (
        float(settings.vibrant_saturation_compression) * day_amount
    )
    excess = np.maximum(target_saturation - saturation_limit, 0.0)
    target_saturation -= saturation_compression * excess
    high_colour = np.clip(
        (saturation - 0.52) / 0.38, 0.0, 1.0
    )
    target_saturation *= 1.0 - (
        amount
        * high_colour
        * (
            0.045 * yellow_green.astype(np.float32)
            + 0.027
            * ((dominant == 1) & (saturation > 0.58)).astype(np.float32)
        )
    )
    neutral_desaturation = (
        np.clip((0.16 - saturation) / 0.16, 0.0, 1.0)
        * 0.04
        * amount
    )
    shadow_desaturation = (
        np.clip((0.24 - luminance) / 0.24, 0.0, 1.0)
        * 0.035
        * amount
    )
    target_saturation *= 1.0 - np.maximum(
        neutral_desaturation, shadow_desaturation
    )
    chroma_scale = np.divide(
        target_saturation,
        np.maximum(saturation, 1e-5),
        out=np.ones(shape, dtype=np.float32),
        where=saturation > 1e-5,
    )
    working = luminance[..., None] + (
        working - luminance[..., None]
    ) * chroma_scale[..., None]

    # Nudge extreme landscape hues towards neighbouring natural pigments.
    # These offsets are deliberately much smaller than the semantic palette
    # differences and therefore also work safely on orthophotos.
    green_mask = dominant == 1
    blue_mask = dominant == 2
    green_naturalize = (
        green_mask.astype(np.float32)
        * np.clip((saturation - 0.38) / 0.42, 0.0, 1.0)
        * amount
    )
    working[..., 0] += 0.004 * green_naturalize
    working[..., 1] *= 1.0 - 0.002 * green_naturalize
    working[..., 2] += 0.003 * green_naturalize
    working[..., 1] = np.where(
        blue_mask,
        working[..., 1] + 0.005 * working[..., 2] * amount,
        working[..., 1],
    )
    working[..., 2] = np.where(
        blue_mask, working[..., 2] * (1.0 + 0.003 * amount), working[..., 2]
    )
    working[..., 0] = np.where(
        warm, working[..., 0] * (1.0 - 0.003 * amount), working[..., 0]
    )
    working[..., 2] = np.where(
        warm, working[..., 2] + 0.004 * amount, working[..., 2]
    )
    working[..., 0] = np.where(
        grey_brown, working[..., 0] * (1.0 + 0.005 * amount), working[..., 0]
    )
    working[..., 1] = np.where(
        grey_brown, working[..., 1] * (1.0 + 0.003 * amount), working[..., 1]
    )

    # Split-tone grading keeps the local material colour dominant.  Cool
    # shadows and cream-gold highlights are multiplicative and intentionally
    # close to neutral.
    luminance = np.sum(working * luma_weights, axis=-1)
    shadow_weight = np.clip((0.48 - luminance) / 0.48, 0.0, 1.0)
    highlight_weight = np.clip((luminance - 0.52) / 0.48, 0.0, 1.0)
    midtone_weight = np.clip(
        1.0 - shadow_weight - highlight_weight, 0.0, 1.0
    )
    shadow_tint = np.asarray(settings.vibrant_shadow_tint, dtype=np.float32)
    midtone_tint = np.asarray(settings.vibrant_midtones_tint, dtype=np.float32)
    highlight_tint = np.asarray(
        settings.vibrant_highlights_tint, dtype=np.float32
    )
    tint = (
        shadow_weight[..., None] * shadow_tint
        + midtone_weight[..., None] * midtone_tint
        + highlight_weight[..., None] * highlight_tint
    )

    cool_shadow = np.clip((0.88 - light) / 0.42, 0.0, 1.0)
    warm_light = solar * daylight
    tint *= (
        1.0
        + cool_shadow[..., None]
        * np.asarray((-0.008, 0.0, 0.012), dtype=np.float32)
        + warm_light[..., None]
        * np.asarray((0.012, 0.006, -0.006), dtype=np.float32)
    )
    working *= 1.0 + day_amount[..., None] * (tint - 1.0)
    shadow_sky = (
        np.asarray(settings.vibrant_shadow_sky_color, dtype=np.float32)
        / 255.0
    )
    shadow_sky_mix = (
        cool_shadow
        * float(settings.vibrant_shadow_sky_mix)
        * amount
        * daylight
    )[..., None]
    working = (
        working * (1.0 - shadow_sky_mix)
        + shadow_sky * shadow_sky_mix
    )
    working += (
        warm_light[..., None]
        * np.asarray((0.013, 0.012, 0.009), dtype=np.float32)
        * amount
    )
    sun_tint = np.asarray(settings.vibrant_sun_tint, dtype=np.float32)
    moon_tint = np.asarray(settings.vibrant_moon_tint, dtype=np.float32)
    working *= (
        1.0
        + amount
        * warm_light[..., None]
        * (sun_tint - 1.0)
    )
    working *= (
        1.0
        + amount
        * lunar[..., None]
        * (moon_tint - 1.0)
    )

    # At night the categorical or photographic source remains identifiable,
    # but most chroma disappears.  A high full moon restores only part of it.
    luminance = np.sum(working * luma_weights, axis=-1)
    night_chroma = (
        float(settings.vibrant_night_chroma)
        + (
            float(settings.vibrant_full_moon_chroma)
            - float(settings.vibrant_night_chroma)
        )
        * moonlight
    )
    chroma_retention = daylight + (1.0 - daylight) * night_chroma
    working = luminance[..., None] + (
        working - luminance[..., None]
    ) * chroma_retention[..., None]

    # Vibrant uses a configurable linear depth interpolation by default:
    # distance / maximum_distance.  This reproduces the pale blue-grey
    # mountain layering of the art direction without altering Original.
    fog = vibrant_depth_haze_factor(
        distance_m,
        settings,
        maximum_distance_m=maximum_distance_m,
    )
    fog = np.broadcast_to(fog, shape).astype(np.float32)
    if additional_haze is not None:
        local_haze = np.clip(
            np.broadcast_to(
                np.asarray(additional_haze, dtype=np.float32), shape
            ),
            0.0,
            1.0,
        )
        fog = np.clip(fog + local_haze * (1.0 - fog), 0.0, 1.0)
    distance_amount = fog * amount
    luminance = np.sum(working * luma_weights, axis=-1)
    distance_chroma = (
        distance_amount * float(settings.vibrant_distance_desaturation)
    )
    working = luminance[..., None] + (
        working - luminance[..., None]
    ) * (1.0 - distance_chroma[..., None])
    day_atmosphere = (
        np.asarray(settings.vibrant_atmosphere_color, dtype=np.float32)
        / 255.0
    )
    if atmosphere_rgb is None:
        atmospheric_rgb = np.broadcast_to(day_atmosphere, shape + (3,))
    else:
        night_atmosphere = np.asarray(atmosphere_rgb, dtype=np.float32)
        if np.nanmax(night_atmosphere) > 2.0:
            night_atmosphere = night_atmosphere / 255.0
        night_atmosphere = np.broadcast_to(
            night_atmosphere, shape + (3,)
        )
        atmospheric_rgb = (
            night_atmosphere * (1.0 - daylight[..., None])
            + day_atmosphere * daylight[..., None]
        )
    atmospheric_luminance = np.sum(
        atmospheric_rgb * luma_weights, axis=-1
    )
    distance_contrast = 1.0 - (
        distance_amount
        * float(settings.vibrant_distance_contrast_reduction)
    )
    working = atmospheric_luminance[..., None] + (
        working - atmospheric_luminance[..., None]
    ) * distance_contrast[..., None]
    distance_brightness = (
        distance_amount
        * float(settings.vibrant_distance_brightness_gain)
        * (daylight + float(settings.vibrant_moon_bloom_scale) * moonlight)
    )
    working += (
        distance_brightness[..., None]
        * np.clip(1.0 - working, 0.0, 1.0)
    )
    haze = (
        fog * float(settings.vibrant_haze_strength) * amount
    )[..., None]
    working = working * (1.0 - haze) + atmospheric_rgb * haze

    # Compress out-of-gamut chroma around luminance instead of clipping an
    # individual channel.  This is the final guard against pure lemon, green
    # or cyan highlights.
    luminance = np.sum(working * luma_weights, axis=-1)
    anchor = np.clip(luminance, 0.0, 0.975)
    chroma = working - luminance[..., None]
    upper_scale = np.where(
        chroma > 1e-6,
        (0.985 - anchor[..., None]) / np.maximum(chroma, 1e-6),
        np.inf,
    )
    lower_scale = np.where(
        chroma < -1e-6,
        (0.0 - anchor[..., None]) / np.minimum(chroma, -1e-6),
        np.inf,
    )
    gamut_scale = np.minimum(
        1.0,
        np.minimum(
            np.min(upper_scale, axis=-1),
            np.min(lower_scale, axis=-1),
        ),
    )
    working = anchor[..., None] + chroma * gamut_scale[..., None]
    working = np.clip(working, 0.0, 0.985)

    result = source.copy()
    graded = np.clip(np.rint(working * 255.0), 0.0, 255.0).astype(np.uint8)
    result[..., :3][mask] = graded[mask]
    return result


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
