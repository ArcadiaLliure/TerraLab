"""Renderer-neutral terrain material, lighting, and atmosphere planning."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from TerraLab.terrain.render.atmosphere import (
    atmospheric_fog_factor,
    vibrant_depth_haze_factor,
)
from TerraLab.terrain.render.config import (
    SurfaceVisualStyle,
    TerrainCelestialLightContext,
    TerrainRenderSettings,
    normalize_surface_visual_style,
)
from TerraLab.terrain.render.lighting import (
    TerrainCelestialLightFactors,
    terrain_celestial_light_factors,
)
from TerraLab.terrain.render.materials import (
    apply_vibrant_color_grade,
    compose_vertex_rgba,
)


@dataclass(frozen=True)
class TerrainAtmospherePlan:
    """Immutable renderer-neutral atmosphere and haze description."""

    fog_factors: np.ndarray
    horizon_rgb: np.ndarray
    haze_color: np.ndarray
    atmosphere_strength: float = 1.0

    def __post_init__(self) -> None:
        if not self.fog_factors.flags.writeable:
            return
        self.fog_factors.flags.writeable = False
        self.horizon_rgb.flags.writeable = False
        self.haze_color.flags.writeable = False


@dataclass(frozen=True)
class TerrainMaterialPlan:
    """Immutable renderer-neutral material and lighting plan."""

    mesh_id: int
    base_rgba: np.ndarray
    vertex_rgba: np.ndarray
    light_factors: TerrainCelestialLightFactors
    style: str = "original"
    atmosphere: TerrainAtmospherePlan | None = None
    cache_key: tuple = ()

    def __post_init__(self) -> None:
        if not self.base_rgba.flags.writeable:
            return
        self.base_rgba.flags.writeable = False
        self.vertex_rgba.flags.writeable = False


def build_terrain_material_plan(
    mesh_id: int,
    base_rgba: np.ndarray,
    intensity: np.ndarray,
    distance_m: np.ndarray,
    settings: TerrainRenderSettings,
    light_context: TerrainCelestialLightContext | None = None,
    *,
    maximum_distance_m: float | None = None,
    horizon_rgb: np.ndarray | tuple[int, int, int] | None = None,
    atmosphere_strength: float = 1.0,
    style_override: str | None = None,
    valid_mask: np.ndarray | None = None,
) -> TerrainMaterialPlan:
    """Pure planner building a complete TerrainMaterialPlan without Qt dependencies."""

    base_arr = np.asarray(base_rgba, dtype=np.uint8)
    if base_arr.shape[-1:] != (4,):
        raise ValueError("Terrain base colours must end in RGBA channels")

    resolved_style = (
        style_override
        if style_override is not None
        else normalize_surface_visual_style(settings.surface_visual_style)
    )

    context = (
        light_context.validated()
        if light_context is not None
        else TerrainCelestialLightContext(
            sun_altitude_deg=45.0, sun_azimuth_deg=180.0
        ).validated()
    )
    light_factors = terrain_celestial_light_factors(context, settings)

    if resolved_style == SurfaceVisualStyle.VIBRANT.value:
        v_haze = vibrant_depth_haze_factor(
            distance_m,
            settings,
            maximum_distance_m=maximum_distance_m,
        )
        h_color = np.asarray(
            horizon_rgb
            if horizon_rgb is not None
            else settings.atmosphere_horizon_color,
            dtype=np.float32,
        )[:3]

        vertex_rgba = apply_vibrant_color_grade(
            base_arr,
            intensity,
            distance_m,
            settings,
            maximum_distance_m=maximum_distance_m,
            valid_mask=valid_mask,
            additional_haze=v_haze,
            daylight_factor=light_factors.solar_ambient,
            moonlight_factor=light_factors.lunar_strength,
            atmosphere_rgb=h_color,
        )
        fog_factors = v_haze
    else:
        fog_factors = atmospheric_fog_factor(
            distance_m,
            settings,
            maximum_distance_m=maximum_distance_m,
        )
        h_color = np.asarray(
            horizon_rgb
            if horizon_rgb is not None
            else settings.atmosphere_horizon_color,
            dtype=np.float32,
        )[:3]

        vertex_rgba = compose_vertex_rgba(
            base_arr,
            intensity,
            distance_m,
            settings,
            maximum_distance_m=maximum_distance_m,
            horizon_rgb=h_color,
            atmosphere_strength=atmosphere_strength,
        )

    atmosphere_plan = TerrainAtmospherePlan(
        fog_factors=fog_factors,
        horizon_rgb=h_color,
        haze_color=h_color,
        atmosphere_strength=atmosphere_strength,
    )

    cache_key = (
        mesh_id,
        resolved_style,
        light_factors.solar_ambient,
        light_factors.lunar_strength,
        float(atmosphere_strength),
    )

    return TerrainMaterialPlan(
        mesh_id=mesh_id,
        base_rgba=base_arr,
        vertex_rgba=vertex_rgba,
        light_factors=light_factors,
        style=resolved_style,
        atmosphere=atmosphere_plan,
        cache_key=cache_key,
    )
