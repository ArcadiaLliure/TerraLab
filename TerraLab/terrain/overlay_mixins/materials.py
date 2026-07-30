"""Terrain base materials, atmospheric composition, and brushes."""

from __future__ import annotations

import numpy as np
from PyQt5.QtGui import QBrush, QColor, QLinearGradient

from TerraLab.common.performance.flags import PERFORMANCE_FLAGS
from TerraLab.terrain.render.atmosphere import atmospheric_fog_factor
from TerraLab.terrain.render.config import (
    SurfaceVisualStyle,
    TerrainCelestialLightContext,
    normalize_surface_visual_style,
)
from TerraLab.terrain.render.lighting import terrain_celestial_light_factors
from TerraLab.terrain.render.materials import compose_vertex_rgba
from TerraLab.terrain.render.overlay_types import (
    TerrainBaseMaterialCache,
    TerrainMaterialSamples,
    _freeze_material_samples,
    _owned_material_samples_size,
)
from TerraLab.terrain.render.palette import (
    _apply_categorical_territorial_variation,
    _atmospheric_haze_color,
    _clamp01,
    _lerp_color,
    _palette_color,
    _protected_categorical_regions,
    _qcolor_from_rgba,
    _sample_cache_value,
    _vibrant_categorical_palette,
)


def _as_qcolor(value) -> QColor:
    """Normalize palette tuples at the QPainter presentation boundary."""

    return QColor(value) if isinstance(value, QColor) else _qcolor_from_rgba(value)


class OverlayMaterialBuildMixin:
    def _build_terrain_base_material(
        self, asset, surface_cache
    ) -> TerrainBaseMaterialCache:
        """Build the camera/time-independent material grids once."""

        key = self._terrain_base_material_key(asset, surface_cache)
        cached = (
            self._terrain_base_material_cache.get(key)
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        if isinstance(cached, TerrainBaseMaterialCache):
            self._last_base_material_cache_hit = True
            return cached

        self._last_base_material_cache_hit = False
        polar = self._terrain_vertex_materials(asset, 0.0)
        near_patch = self._near_patch_vertex_materials(asset, 0.0)
        vibrant = (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            )
            == SurfaceVisualStyle.VIBRANT.value
        )
        if vibrant:
            settings = self.render_settings
            legends = tuple(
                _sample_cache_value(
                    surface_cache, "source_legend_ids", ()
                )
                or ()
            )
            polar = _vibrant_categorical_palette(polar, surface_cache)
            polar_azimuth = np.radians(
                np.asarray(asset.azimuths, dtype=np.float64)
            )
            polar_distance = np.asarray(
                asset.distances, dtype=np.float64
            )
            polar_east = (
                polar_distance[:, None] * np.sin(polar_azimuth)[None, :]
            )
            polar_north = (
                polar_distance[:, None] * np.cos(polar_azimuth)[None, :]
            )
            polar_rgba = _apply_categorical_territorial_variation(
                polar.base_rgba,
                polar,
                polar_east,
                polar_north,
                polar.valid,
                strength=settings.vibrant_intensity,
                luminance_variation=(
                    settings.vibrant_territorial_luminance_variation
                ),
                hue_variation=settings.vibrant_territorial_hue_variation,
                midscale_variation=(
                    settings.vibrant_material_midscale_variation
                ),
                microscale_variation=(
                    settings.vibrant_material_microscale_variation
                ),
                altitude_influence=(
                    settings.vibrant_material_altitude_influence
                ),
                slope_influence=settings.vibrant_material_slope_influence,
                snow_rock_blend=settings.vibrant_snow_rock_blend,
                water_shore_variation=(
                    settings.vibrant_water_shore_variation
                ),
                elevation_m=asset.elevations,
                normal_x=asset.normal_x,
                normal_y=asset.normal_y,
                normal_z=asset.normal_z,
                source_legend_ids=legends,
                render_scale=1.0,
                include_solar_response=False,
            )
            polar = TerrainMaterialSamples(
                polar_rgba,
                polar.valid,
                polar.class_ids,
                polar.categorical,
                polar.source_indices,
            )

            near_patch = _vibrant_categorical_palette(
                near_patch, surface_cache
            )
            if np.asarray(near_patch.valid).size:
                patch_east, patch_north = np.meshgrid(
                    np.asarray(
                        asset.near_patch_eastings, dtype=np.float64
                    ),
                    np.asarray(
                        asset.near_patch_northings, dtype=np.float64
                    ),
                )
                patch_rgba = _apply_categorical_territorial_variation(
                    near_patch.base_rgba,
                    near_patch,
                    patch_east,
                    patch_north,
                    near_patch.valid,
                    strength=settings.vibrant_intensity,
                    luminance_variation=(
                        settings.vibrant_territorial_luminance_variation
                    ),
                    hue_variation=(
                        settings.vibrant_territorial_hue_variation
                    ),
                    midscale_variation=(
                        settings.vibrant_material_midscale_variation
                    ),
                    microscale_variation=(
                        settings.vibrant_material_microscale_variation
                    ),
                    altitude_influence=(
                        settings.vibrant_material_altitude_influence
                    ),
                    slope_influence=(
                        settings.vibrant_material_slope_influence
                    ),
                    snow_rock_blend=settings.vibrant_snow_rock_blend,
                    water_shore_variation=(
                        settings.vibrant_water_shore_variation
                    ),
                    elevation_m=asset.near_patch_elevations,
                    normal_x=asset.near_patch_normal_x,
                    normal_y=asset.near_patch_normal_y,
                    normal_z=asset.near_patch_normal_z,
                    source_legend_ids=legends,
                    render_scale=1.0,
                    include_solar_response=False,
                )
                near_patch = TerrainMaterialSamples(
                    patch_rgba,
                    near_patch.valid,
                    near_patch.class_ids,
                    near_patch.categorical,
                    near_patch.source_indices,
                )

        shared_arrays = tuple(
            _sample_cache_value(surface_cache, name)
            for prefix in ("visual", "relief", "near_patch")
            for name in (
                f"{prefix}_rgba",
                f"{prefix}_valid",
                f"{prefix}_class_ids",
                f"{prefix}_categorical",
                f"{prefix}_source_indices",
            )
        )
        owned_bytes = _owned_material_samples_size(
            (polar, near_patch), shared_arrays
        )
        if vibrant:
            polar_protected = _protected_categorical_regions(
                polar, surface_cache
            )
            near_patch_protected = _protected_categorical_regions(
                near_patch, surface_cache
            )
        else:
            polar_protected = np.zeros(
                np.asarray(polar.valid).shape, dtype=bool
            )
            near_patch_protected = np.zeros(
                np.asarray(near_patch.valid).shape, dtype=bool
            )
        owned_bytes += int(
            polar_protected.nbytes + near_patch_protected.nbytes
        )
        entry = TerrainBaseMaterialCache(
            key,
            _freeze_material_samples(polar),
            _freeze_material_samples(near_patch),
            polar_protected,
            near_patch_protected,
            owned_bytes,
        )
        self._terrain_base_material_builds += 1
        if PERFORMANCE_FLAGS.relief_cached:
            self._terrain_base_material_cache.put(
                key, entry, entry.resident_bytes
            )
        return entry

    def _terrain_vertex_base_rgba(self, asset, t_night):
        """Compatibility view used by legacy callers."""

        return self._terrain_vertex_materials(asset, t_night).base_rgba

    def _near_patch_vertex_materials(
        self, asset, t_night
    ) -> TerrainMaterialSamples:
        """Return aligned near-patch material samples with terrain fallback."""

        del t_night
        shape = asset.near_patch_elevations.shape
        cache = self._visible_surface_cache()
        sampled = _sample_cache_value(cache, "near_patch_rgba")
        sampled_valid = _sample_cache_value(cache, "near_patch_valid")
        sampled_loaded = _sample_cache_value(cache, "near_patch_loaded")
        sampled_sources = _sample_cache_value(
            cache, "near_patch_source_indices"
        )
        sampled_classes = _sample_cache_value(
            cache, "near_patch_class_ids"
        )
        sampled_categorical = _sample_cache_value(
            cache, "near_patch_categorical"
        )
        if (
            np.shape(sampled) == shape + (4,)
            and np.shape(sampled_valid) == shape
            and np.shape(sampled_sources) == shape
            and np.shape(sampled_classes) == shape
            and np.shape(sampled_categorical) == shape
            and np.all(np.asarray(sampled_valid, dtype=bool))
            and (
                sampled_loaded is None
                or (
                    np.shape(sampled_loaded) == shape
                    and np.all(np.asarray(sampled_loaded, dtype=bool))
                )
            )
            and (
                not self.terrain_surface_opaque
                or np.all(
                    np.asarray(sampled, dtype=np.uint8)[..., 3] == 255
                )
            )
        ):
            return TerrainMaterialSamples(
                np.asarray(sampled, dtype=np.uint8),
                np.asarray(sampled_valid, dtype=bool),
                np.asarray(sampled_classes, dtype=np.int64),
                np.asarray(sampled_categorical, dtype=bool),
                np.asarray(sampled_sources, dtype=np.int16),
            )

        _night_color, day_color = _palette_color(1.0)
        day_rgba = day_color if isinstance(day_color, tuple) and len(day_color) == 4 else (day_color[0], day_color[1], day_color[2], 255) if isinstance(day_color, tuple) else day_color.getRgb()
        fallback_color = np.asarray(
            day_rgba,
            dtype=np.uint8,
        )
        fallback = np.broadcast_to(fallback_color, shape + (4,)).copy()
        if (
            sampled is not None
            and sampled_valid is not None
            and np.shape(sampled) == shape + (4,)
            and np.shape(sampled_valid) == shape
        ):
            sampled_valid = np.asarray(sampled_valid, dtype=bool)
            result = np.where(
                sampled_valid[..., None],
                np.asarray(sampled, dtype=np.uint8),
                fallback,
            ).astype(np.uint8)
            source_indices = np.where(
                sampled_valid,
                (
                    np.asarray(sampled_sources, dtype=np.int16)
                    if sampled_sources is not None
                    and np.shape(sampled_sources) == shape
                    else -1
                ),
                -1,
            )
            class_ids = np.where(
                sampled_valid,
                (
                    np.asarray(sampled_classes, dtype=np.int64)
                    if sampled_classes is not None
                    and np.shape(sampled_classes) == shape
                    else -1
                ),
                -1,
            )
            categorical = sampled_valid & (
                np.asarray(sampled_categorical, dtype=bool)
                if sampled_categorical is not None
                and np.shape(sampled_categorical) == shape
                else np.zeros(shape, dtype=bool)
            )
        else:
            result = fallback
            source_indices = np.full(shape, -1, dtype=np.int16)
            class_ids = np.full(shape, -1, dtype=np.int64)
            categorical = np.zeros(shape, dtype=bool)
        if self.terrain_surface_opaque:
            result[..., 3] = 255
        return TerrainMaterialSamples(
            result,
            np.ones(shape, dtype=bool),
            class_ids,
            categorical,
            source_indices,
        )

    def _near_patch_vertex_base_rgba(self, asset, t_night):
        """Compatibility view used by legacy callers."""

        return self._near_patch_vertex_materials(asset, t_night).base_rgba

    def _apply_terrain_light(
        self, color: QColor, light_factor: float, sky_color: QColor, t_night: float
    ) -> QColor:
        del sky_color
        if not self.render_settings.terrain_lighting_enabled:
            return _as_qcolor(color)
        factor = max(
            0.0,
            min(
                max(
                    self.render_settings.terrain_max_brightness,
                    self.render_settings.vibrant_sun_max_brightness,
                ),
                float(light_factor),
            ),
        )
        del t_night
        rgb = np.asarray(
            (color.red(), color.green(), color.blue()), dtype=np.float32
        ) * factor
        return QColor(
            max(0, min(255, round(float(rgb[0])))),
            max(0, min(255, round(float(rgb[1])))),
            max(0, min(255, round(float(rgb[2])))),
            color.alpha(),
        )

    def _apply_terrain_atmosphere(
        self, color: QColor, distance_m: float, sky_color: QColor, t_night: float
    ) -> QColor:
        del t_night
        c_rgba = color if isinstance(color, tuple) and len(color) == 4 else (color[0], color[1], color[2], 255) if isinstance(color, tuple) else color.getRgb()
        base = np.asarray(c_rgba, dtype=np.uint8)
        result = compose_vertex_rgba(
            base,
            1.0,
            float(distance_m),
            self.render_settings,
            maximum_distance_m=self._maximum_terrain_distance_m(),
            horizon_rgb=(
                sky_color.red(),
                sky_color.green(),
                sky_color.blue(),
            ),
        )
        return _qcolor_from_rgba(result)

    def _compose_terrain_color(
        self,
        base_color: QColor,
        light_factor: float,
        distance_m: float,
        sky_color: QColor,
        t_night: float,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> QColor:
        if isinstance(light_context, TerrainCelestialLightContext):
            factors = terrain_celestial_light_factors(
                light_context, self.render_settings
            )
            vibrant = (
                normalize_surface_visual_style(
                    self.render_settings.surface_visual_style
                )
                == SurfaceVisualStyle.VIBRANT.value
            )
            day_ambient = (
                self.render_settings.vibrant_sun_ambient_strength
                if vibrant
                else self.render_settings.terrain_ambient_strength
            )
            ambient = (
                self.render_settings.terrain_night_ambient_strength
                + (
                    day_ambient
                    - self.render_settings.terrain_night_ambient_strength
                )
                * factors.solar_ambient
                + self.render_settings.terrain_moon_ambient_strength
                * factors.lunar_strength
            )
            directional = max(0.0, float(light_factor) - float(ambient))
            solar_exposure = 0.0
            lunar_exposure = 0.0
            if factors.solar_direct > 0.0:
                day_diffuse = self.render_settings.terrain_diffuse_strength * (
                    self.render_settings.vibrant_sun_diffuse_boost
                    if vibrant
                    else 1.0
                )
                solar_exposure = _clamp01(
                    directional / max(1e-6, float(day_diffuse))
                )
            elif factors.lunar_strength > 0.0:
                lunar_exposure = _clamp01(
                    directional
                    / max(
                        1e-6,
                        float(self.render_settings.terrain_moon_diffuse_strength),
                    )
                )
            return self._compose_profile_light_color(
                base_color,
                light_factor,
                distance_m,
                sky_color,
                factors,
                solar_exposure=solar_exposure,
                lunar_exposure=lunar_exposure,
            )
        lit = self._apply_terrain_light(
            _as_qcolor(base_color), light_factor, sky_color, t_night
        )
        return self._apply_terrain_atmosphere(
            lit, distance_m, sky_color, t_night
        )

    def _mesh_quad_color(
        self,
        distance_m,
        nx,
        ny,
        nz,
        t_night,
        sky_color,
        sun_vec,
        sun_alt,
        terrain_shading_enabled=True,
        light_factor=None,
        base_color=None,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        haze = float(
            atmospheric_fog_factor(
                distance_m,
                self.render_settings,
                maximum_distance_m=self._maximum_terrain_distance_m(),
            )
        )
        palette_t = _clamp01(1.0 - haze)
        night_c, day_c = _palette_color(palette_t)
        base = (
            _as_qcolor(base_color)
            if base_color is not None
            else (
                _as_qcolor(day_c)
                if light_context is not None
                else _as_qcolor(_lerp_color(day_c, night_c, t_night))
            )
        )

        if light_factor is None:
            light_factor = float(
                np.asarray(
                    self._terrain_light_factor(
                        nx,
                        ny,
                        nz,
                        distance_m,
                        sun_vec,
                        sun_alt,
                        terrain_shading_enabled=terrain_shading_enabled,
                        light_context=light_context,
                    )
                )
            )

        return self._compose_terrain_color(
            base,
            light_factor,
            distance_m,
            sky_color,
            t_night,
            light_context=light_context,
        )

    def _terrain_surface_color(
        self,
        distance_m,
        light_factor,
        t_night,
        sky_color,
        sun_vec,
        sun_alt,
        terrain_shading_enabled,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        color = self._mesh_quad_color(
            distance_m,
            0.0,
            0.0,
            1.0,
            t_night,
            sky_color,
            sun_vec,
            sun_alt,
            terrain_shading_enabled=terrain_shading_enabled,
            light_factor=light_factor,
            light_context=light_context,
        )
        if light_context is not None:
            if self.terrain_surface_opaque:
                color.setAlpha(255)
            return color
        haze = float(
            atmospheric_fog_factor(
                distance_m,
                self.render_settings,
                maximum_distance_m=self._maximum_terrain_distance_m(),
            )
        )
        calm_night, calm_day = _palette_color(
            _clamp01(0.62 + 0.18 * (1.0 - haze))
        )
        calm_base = _lerp_color(calm_day, calm_night, t_night)
        haze_color = _atmospheric_haze_color(sky_color, t_night)
        calm_base = _lerp_color(calm_base, haze_color, 0.08 + 0.22 * haze)
        color = _as_qcolor(_lerp_color(calm_base, color, 0.70))
        alpha = int(82 + 58 * (1.0 - haze))
        alpha = int(alpha * (1.0 - 0.30 * _clamp01(t_night)))
        if self.terrain_surface_opaque:
            color.setAlpha(255)
        else:
            color.setAlpha(max(68, min(140, alpha)))
        return color

    def _terrain_span_brush(
        self,
        seg_x,
        segment_shade,
        distance_m,
        t_night,
        sky_color,
        sun_vec,
        sun_alt,
        terrain_shading_enabled,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        finite = np.isfinite(seg_x) & np.isfinite(segment_shade)
        if np.count_nonzero(finite) < 2:
            finite_shade = np.asarray(segment_shade)[
                np.isfinite(segment_shade)
            ]
            light_factor = (
                float(np.mean(finite_shade)) if finite_shade.size else 1.0
            )
            return QBrush(
                self._terrain_surface_color(
                    distance_m,
                    light_factor,
                    t_night,
                    sky_color,
                    sun_vec,
                    sun_alt,
                    terrain_shading_enabled,
                    light_context,
                )
            )

        x_values = np.asarray(seg_x[finite], dtype=np.float32)
        shade_values = np.asarray(segment_shade[finite], dtype=np.float32)
        x_min = float(np.min(x_values))
        x_max = float(np.max(x_values))
        if (
            self.render_settings.terrain_shading_mode == "flat"
            or x_max - x_min < 1.0
            or np.ptp(shade_values) < 0.006
        ):
            light_factor = float(np.mean(shade_values))
            return QBrush(
                self._terrain_surface_color(
                    distance_m,
                    light_factor,
                    t_night,
                    sky_color,
                    sun_vec,
                    sun_alt,
                    terrain_shading_enabled,
                    light_context,
                )
            )

        gradient = QLinearGradient(x_min, 0.0, x_max, 0.0)
        stop_count = min(18, len(x_values))
        stop_indices = np.unique(
            np.linspace(0, len(x_values) - 1, stop_count).astype(np.int32)
        )
        stops = []
        for index in stop_indices:
            position = _clamp01(
                (float(x_values[index]) - x_min) / (x_max - x_min)
            )
            color = self._terrain_surface_color(
                distance_m,
                float(shade_values[index]),
                t_night,
                sky_color,
                sun_vec,
                sun_alt,
                terrain_shading_enabled,
                light_context,
            )
            stops.append((position, color))
        for position, color in sorted(stops, key=lambda item: item[0]):
            gradient.setColorAt(position, color)
        return QBrush(gradient)
