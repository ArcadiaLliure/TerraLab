"""Serializable terrain rendering configuration."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Any, Mapping


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
