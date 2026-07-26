"""Screen-space interpolation of terrain materials."""

from __future__ import annotations

import time

import numpy as np
from PyQt5.QtGui import QColor

from TerraLab.common.perf_events import append_perf_event
from TerraLab.common.performance.flags import PERFORMANCE_FLAGS
from TerraLab.terrain.render.config import (
    SurfaceVisualStyle,
    TerrainCelestialLightContext,
    normalize_surface_visual_style,
)
from TerraLab.terrain.render.geometry import _regularize_categorical_regions
from TerraLab.terrain.render.lighting import terrain_celestial_light_factors
from TerraLab.terrain.render.overlay_types import (
    TerrainLightingGrid,
    TerrainMaterialSamples,
    TerrainResolvedMaterialCache,
    _freeze_material_samples,
)
from TerraLab.terrain.render.palette import (
    _protected_categorical_regions,
    _sample_cache_value,
)
from TerraLab.terrain.render.triangle_raster import _resolve_surface_material


class OverlayInterpolatedMaterialMixin:
    def _draw_terrain_interpolated(
        self,
        painter,
        mesh,
        projection_fn,
        width,
        height,
        cur_az,
        az_min,
        az_max,
        t_night,
        sky_color,
        *,
        sun_alt=None,
        sun_az=None,
        projection_fn_numpy=None,
        interaction_active=False,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> bool:
        """Render the relief with shared per-vertex colours and a z-buffer."""

        frame_started = time.perf_counter()
        asset = self._terrain_render_asset
        surface_cache = self._visible_surface_cache()
        visual_altitudes = _sample_cache_value(surface_cache, "visual_altitudes")
        mesh_token = (
            hash((id(mesh), id(visual_altitudes)))
            if visual_altitudes is not None
            else id(mesh)
        )
        if asset is None or asset.mesh_id != mesh_token:
            asset = self._prepare_terrain_render_asset(mesh)
            if PERFORMANCE_FLAGS.relief_cached:
                self._terrain_render_asset = asset
        if asset is None:
            return False

        light_context = self._resolve_light_context(
            light_context,
            sun_alt=sun_alt,
            sun_az=sun_az,
        )
        light_alt, light_az, light_vector = self._configured_light(
            sun_alt, sun_az, light_context
        )
        light_factors = terrain_celestial_light_factors(
            light_context, self.render_settings
        )
        lighting_enabled = bool(
            self.render_settings.terrain_lighting_enabled
            and light_vector is not None
        )
        shade_key = self._terrain_lighting_key(
            asset,
            light_context,
            lighting_enabled=lighting_enabled,
        )
        lighting_grid = (
            self._terrain_shade_cache.get(shade_key)
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        polar_lighting_hit = isinstance(
            lighting_grid, TerrainLightingGrid
        )
        if not isinstance(lighting_grid, TerrainLightingGrid):
            self._terrain_lighting_builds += 1
            lighting_grid = self._terrain_light_components(
                asset.normal_x,
                asset.normal_y,
                asset.normal_z,
                asset.distances[:, None],
                light_vector,
                light_alt,
                terrain_shading_enabled=lighting_enabled,
                sun_visibility=None,
                light_context=light_context,
            )
            minimum_light, maximum_light = self._terrain_light_bounds(
                lighting_grid.factors
            )
            lighting_grid = TerrainLightingGrid(
                self._smooth_light_grid(
                    lighting_grid.intensity,
                    asset.valid,
                    min_value=minimum_light,
                    max_value=maximum_light,
                ),
                self._smooth_light_grid(
                    lighting_grid.solar_exposure,
                    asset.valid,
                    min_value=0.0,
                    max_value=1.0,
                ),
                self._smooth_light_grid(
                    lighting_grid.lunar_exposure,
                    asset.valid,
                    min_value=0.0,
                    max_value=1.0,
                ),
                lighting_grid.factors,
            )
            if PERFORMANCE_FLAGS.relief_cached:
                light_bytes = (
                    lighting_grid.intensity.nbytes
                    + lighting_grid.solar_exposure.nbytes
                    + lighting_grid.lunar_exposure.nbytes
                )
                self._terrain_shade_cache.put(
                    shade_key, lighting_grid, int(light_bytes)
                )

        patch_east, patch_north = np.meshgrid(
            np.asarray(asset.near_patch_eastings, dtype=np.float32),
            np.asarray(asset.near_patch_northings, dtype=np.float32),
        )
        patch_distances = np.hypot(patch_east, patch_north).astype(np.float32)
        patch_shade_key = shade_key + ("near-patch",)
        patch_lighting = (
            self._terrain_shade_cache.get(patch_shade_key)
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        patch_lighting_hit = isinstance(
            patch_lighting, TerrainLightingGrid
        )
        if not isinstance(patch_lighting, TerrainLightingGrid) and patch_distances.size == 0:
            empty = np.empty(patch_distances.shape, dtype=np.float32)
            patch_lighting = TerrainLightingGrid(
                empty, empty.copy(), empty.copy(), light_factors
            )
            patch_lighting_hit = True
        elif not isinstance(patch_lighting, TerrainLightingGrid):
            patch_lighting = self._terrain_light_components(
                asset.near_patch_normal_x,
                asset.near_patch_normal_y,
                asset.near_patch_normal_z,
                patch_distances,
                light_vector,
                light_alt,
                terrain_shading_enabled=lighting_enabled,
                sun_visibility=None,
                light_context=light_context,
            )
            minimum_light, maximum_light = self._terrain_light_bounds(
                patch_lighting.factors
            )
            patch_lighting = TerrainLightingGrid(
                self._smooth_light_grid(
                    patch_lighting.intensity,
                    asset.near_patch_valid,
                    min_value=minimum_light,
                    max_value=maximum_light,
                ),
                self._smooth_light_grid(
                    patch_lighting.solar_exposure,
                    asset.near_patch_valid,
                    min_value=0.0,
                    max_value=1.0,
                ),
                self._smooth_light_grid(
                    patch_lighting.lunar_exposure,
                    asset.near_patch_valid,
                    min_value=0.0,
                    max_value=1.0,
                ),
                patch_lighting.factors,
            )
            if PERFORMANCE_FLAGS.relief_cached:
                patch_light_bytes = (
                    patch_lighting.intensity.nbytes
                    + patch_lighting.solar_exposure.nbytes
                    + patch_lighting.lunar_exposure.nbytes
                )
                self._terrain_shade_cache.put(
                    patch_shade_key, patch_lighting, int(patch_light_bytes)
                )
        self._last_lighting_cache_hit = bool(
            polar_lighting_hit and patch_lighting_hit
        )

        geometry = self._terrain_triangles_for_view(
            asset,
            projection_fn,
            width,
            height,
            cur_az,
            az_min,
            az_max,
            projection_fn_numpy=projection_fn_numpy,
        )
        if geometry is None or geometry.xy.size == 0:
            return False

        color_started = time.perf_counter()
        base_material = self._build_terrain_base_material(
            asset, surface_cache
        )
        vertex_materials = base_material.polar
        patch_materials = base_material.near_patch
        self._last_terrain_color_s = time.perf_counter() - color_started
        frame_key = (
            "terrain-frame-v1",
            base_material.key,
            shade_key,
            round(float(t_night) * 256.0) / 256.0,
            (
                int(sky_color.rgba())
                if isinstance(sky_color, QColor)
                else None
            ),
            repr(self.render_settings),
        )
        self._paint_terrain_triangles(
            painter,
            asset,
            geometry,
            vertex_materials,
            patch_materials,
            lighting_grid.intensity,
            patch_lighting.intensity,
            width,
            height,
            base_material.key,
            frame_key,
            interaction_active=interaction_active,
            vertex_solar=lighting_grid.solar_exposure,
            patch_vertex_solar=patch_lighting.solar_exposure,
            vertex_lunar=lighting_grid.lunar_exposure,
            patch_vertex_lunar=patch_lighting.lunar_exposure,
            light_factors=lighting_grid.factors,
            sky_color=sky_color,
        )
        self._last_terrain_total_s = time.perf_counter() - frame_started
        if self.render_settings.terrain_performance_logging_enabled:
            append_perf_event(
                "terrain.render",
                geometry_s=round(float(geometry.metrics.elapsed_s), 6),
                colors_s=round(float(self._last_terrain_color_s), 6),
                material_resolution_s=round(
                    float(self._last_material_resolution_s), 6
                ),
                rasterization_s=round(float(self._last_terrain_raster_s), 6),
                horizon_antialias_s=round(
                    float(self._last_horizon_antialias_s), 6
                ),
                total_s=round(float(self._last_terrain_total_s), 6),
                rays=int(asset.azimuths.size),
                samples=int(asset.elevations.size),
                vertices=int(asset.elevations.size),
                triangles=int(geometry.xy.shape[0]),
                width=int(width),
                height=int(height),
                shading_mode=self.render_settings.terrain_shading_mode,
                base_material_cache_hit=bool(
                    self._last_base_material_cache_hit
                ),
                lighting_cache_hit=bool(
                    self._last_lighting_cache_hit
                ),
                resolved_material_cache_hit=bool(
                    self._last_resolved_material_cache_hit
                ),
                base_material_builds=int(
                    self._terrain_base_material_builds
                ),
                lighting_builds=int(self._terrain_lighting_builds),
                resolved_material_builds=int(
                    self._terrain_resolved_material_builds
                ),
                raster_builds=int(self._terrain_raster_builds),
                raster_cache_hit=bool(self._last_raster_cache_hit),
                frame_cache_hit=bool(self._last_frame_cache_hit),
                base_material_cache_bytes=int(
                    self._terrain_base_material_cache.resident_bytes
                ),
                lighting_cache_bytes=int(
                    self._terrain_shade_cache.resident_bytes
                ),
                resolved_material_cache_bytes=int(
                    self._terrain_resolved_material_cache.resident_bytes
                ),
            )
        return True

    def _resolve_screen_material(
        self,
        asset,
        geometry,
        triangle_id,
        bary_u,
        bary_v,
        covered,
        vertex_materials,
        patch_materials,
        *,
        raster_key: tuple,
        material_key: tuple,
        render_scale: float,
    ) -> TerrainResolvedMaterialCache:
        """Resolve projected classes/colours once for a stable camera raster."""

        key = (
            "terrain-resolved-material-v1",
            raster_key,
            material_key,
            str(self.render_settings.terrain_shading_mode),
            round(
                float(
                    self.render_settings
                    .categorical_region_smoothing_radius_px
                )
                * float(render_scale),
                4,
            ),
        )
        cached = (
            self._terrain_resolved_material_cache.get(key)
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        expected_vertex_shape = np.shape(geometry.vertex_rows)
        cached_matches_geometry = (
            isinstance(cached, TerrainResolvedMaterialCache)
            and np.shape(cached.triangle_materials.valid)
            == expected_vertex_shape
            and np.shape(cached.triangle_surface_xy)
            == expected_vertex_shape + (2,)
            and np.shape(cached.materials.valid) == np.shape(triangle_id)
        )
        if cached_matches_geometry:
            self._last_resolved_material_cache_hit = True
            self._last_material_resolution_s = 0.0
            return cached

        self._last_resolved_material_cache_hit = False
        material_started = time.perf_counter()
        vertex_rows = np.asarray(geometry.vertex_rows, dtype=np.int32)
        vertex_domain = np.asarray(geometry.vertex_domain, dtype=np.uint8)
        vertex_columns = np.asarray(
            geometry.vertex_columns, dtype=np.int32
        )
        polar_vertices = vertex_domain == 0
        polar_rows = np.where(polar_vertices, vertex_rows, 0)
        polar_columns = np.where(polar_vertices, vertex_columns, 0)
        base_rgba = np.asarray(
            vertex_materials.base_rgba[polar_rows, polar_columns],
            dtype=np.uint8,
        )
        material_valid = np.asarray(
            vertex_materials.valid[polar_rows, polar_columns], dtype=bool
        )
        class_ids = np.asarray(
            vertex_materials.class_ids[polar_rows, polar_columns],
            dtype=np.int64,
        )
        categorical = np.asarray(
            vertex_materials.categorical[polar_rows, polar_columns],
            dtype=bool,
        )
        source_indices = np.asarray(
            vertex_materials.source_indices[polar_rows, polar_columns],
            dtype=np.int16,
        )
        patch_vertices = vertex_domain == 1
        if np.any(patch_vertices):
            base_rgba = base_rgba.copy()
            material_valid = material_valid.copy()
            class_ids = class_ids.copy()
            categorical = categorical.copy()
            source_indices = source_indices.copy()
            patch_rows = vertex_rows[patch_vertices]
            patch_columns = vertex_columns[patch_vertices]
            base_rgba[patch_vertices] = np.asarray(
                patch_materials.base_rgba[patch_rows, patch_columns],
                dtype=np.uint8,
            )
            material_valid[patch_vertices] = np.asarray(
                patch_materials.valid[patch_rows, patch_columns], dtype=bool
            )
            class_ids[patch_vertices] = np.asarray(
                patch_materials.class_ids[patch_rows, patch_columns],
                dtype=np.int64,
            )
            categorical[patch_vertices] = np.asarray(
                patch_materials.categorical[patch_rows, patch_columns],
                dtype=bool,
            )
            source_indices[patch_vertices] = np.asarray(
                patch_materials.source_indices[patch_rows, patch_columns],
                dtype=np.int16,
            )

        triangle_materials = TerrainMaterialSamples(
            base_rgba,
            material_valid,
            class_ids,
            categorical,
            source_indices,
        )
        triangle_surface_xy = np.zeros(
            vertex_rows.shape + (2,), dtype=np.float64
        )
        polar_distance = np.asarray(
            asset.distances[polar_rows], dtype=np.float64
        )
        polar_azimuth = np.radians(
            np.asarray(
                asset.azimuths[polar_columns], dtype=np.float64
            )
        )
        triangle_surface_xy[..., 0] = (
            polar_distance * np.sin(polar_azimuth)
        )
        triangle_surface_xy[..., 1] = (
            polar_distance * np.cos(polar_azimuth)
        )
        if np.any(patch_vertices):
            patch_rows = vertex_rows[patch_vertices]
            patch_columns = vertex_columns[patch_vertices]
            triangle_surface_xy[..., 0][patch_vertices] = np.asarray(
                asset.near_patch_eastings[patch_columns],
                dtype=np.float64,
            )
            triangle_surface_xy[..., 1][patch_vertices] = np.asarray(
                asset.near_patch_northings[patch_rows],
                dtype=np.float64,
            )

        resolved = _resolve_surface_material(
            triangle_id,
            bary_u,
            bary_v,
            triangle_materials,
            triangle_surface_xy,
            vertex_domain,
            vertex_materials,
            asset.distances,
            asset.azimuths,
            patch_materials,
            asset.near_patch_eastings,
            asset.near_patch_northings,
            flat_continuous=(
                self.render_settings.terrain_shading_mode == "flat"
            ),
        )
        surface_cache = self._visible_surface_cache()
        protected = np.zeros(np.asarray(covered).shape, dtype=bool)
        if (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            )
            == SurfaceVisualStyle.VIBRANT.value
        ):
            protected = _protected_categorical_regions(
                resolved, surface_cache
            )
            resolved = _regularize_categorical_regions(
                resolved,
                covered,
                radius_px=(
                    self.render_settings
                    .categorical_region_smoothing_radius_px
                    * render_scale
                ),
                protected=protected,
            )

        triangle_surface_xy.setflags(write=False)
        protected.setflags(write=False)
        entry = TerrainResolvedMaterialCache(
            key,
            _freeze_material_samples(resolved),
            _freeze_material_samples(triangle_materials),
            triangle_surface_xy,
            protected,
        )
        self._terrain_resolved_material_builds += 1
        self._last_material_resolution_s = (
            time.perf_counter() - material_started
        )
        if PERFORMANCE_FLAGS.relief_cached:
            self._terrain_resolved_material_cache.put(
                key, entry, entry.resident_bytes
            )
        return entry

