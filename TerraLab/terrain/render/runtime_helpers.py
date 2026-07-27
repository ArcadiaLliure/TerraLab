"""Pure terrain-lighting helpers owned by the render layer."""

from __future__ import annotations

import math

from TerraLab.terrain.render.config import TerrainCelestialLightContext


TERRAIN_SUSPEND_RELIEF_DURING_INTERACTION = False


def _terrain_relief_enabled_for_frame(
    configured_enabled: bool,
    camera_interaction_active: bool,
    *,
    surface_enabled: bool = False,
    suspend_during_interaction: bool | None = None,
) -> bool:
    suspend = (
        TERRAIN_SUSPEND_RELIEF_DURING_INTERACTION
        if suspend_during_interaction is None
        else bool(suspend_during_interaction)
    )
    return bool(
        surface_enabled
        or (
            configured_enabled
            and not (camera_interaction_active and suspend)
        )
    )


def _terrain_celestial_light_context(
    sun_alt: float,
    sun_az: float,
    ephemeris_data,
    eclipse_factor: float,
) -> TerrainCelestialLightContext:
    try:
        resolved_sun_alt = float(sun_alt)
        resolved_sun_az = float(sun_az)
    except (TypeError, ValueError):
        resolved_sun_alt = math.nan
        resolved_sun_az = math.nan
    moon_alt = None
    moon_az = None
    moon_illumination = 0.0
    data = ephemeris_data if isinstance(ephemeris_data, dict) else {}
    sun = data.get("sun")
    if isinstance(sun, dict):
        try:
            candidate_alt = float(sun.get("alt"))
            candidate_az = float(sun.get("az"))
            if math.isfinite(candidate_alt) and math.isfinite(candidate_az):
                resolved_sun_alt = candidate_alt
                resolved_sun_az = candidate_az
        except (TypeError, ValueError):
            pass
    moon = data.get("moon")
    if isinstance(moon, dict):
        try:
            candidate_alt = float(moon.get("alt"))
            candidate_az = float(moon.get("az"))
            if math.isfinite(candidate_alt) and math.isfinite(candidate_az):
                moon_alt = candidate_alt
                moon_az = candidate_az
        except (TypeError, ValueError):
            moon_alt = None
            moon_az = None
        try:
            illumination = moon.get("illumination")
            if illumination is None:
                separation = float(moon.get("sep_real"))
                illumination = (
                    1.0 - math.cos(math.radians(separation))
                ) / 2.0
            moon_illumination = float(illumination)
        except (TypeError, ValueError):
            moon_illumination = 0.0
    try:
        resolved_eclipse_factor = float(eclipse_factor)
    except (TypeError, ValueError):
        resolved_eclipse_factor = 1.0
    return TerrainCelestialLightContext(
        sun_altitude_deg=resolved_sun_alt,
        sun_azimuth_deg=resolved_sun_az,
        moon_altitude_deg=moon_alt,
        moon_azimuth_deg=moon_az,
        moon_illumination=moon_illumination,
        eclipse_factor=resolved_eclipse_factor,
    )
