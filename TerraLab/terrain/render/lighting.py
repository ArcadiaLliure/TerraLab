"""Pure terrain celestial and Lambert lighting."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from TerraLab.terrain.render.config import TerrainCelestialLightContext, TerrainRenderSettings

@dataclass(frozen=True)
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
