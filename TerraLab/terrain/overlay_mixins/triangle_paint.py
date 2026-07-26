"""Z-buffered triangle painting and reusable polygon geometry."""

from __future__ import annotations

import math
import time

import numpy as np
from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QImage, QPolygonF

from TerraLab.common.performance.flags import PERFORMANCE_FLAGS
from TerraLab.terrain.render.config import (
    SurfaceVisualStyle,
    normalize_surface_visual_style,
)
from TerraLab.terrain.render.geometry import _apply_horizon_coverage, _soften_categorical_edges
from TerraLab.terrain.render.lighting import TerrainCelestialLightFactors
from TerraLab.terrain.render.materials import (
    apply_vibrant_color_grade,
    compose_vertex_rgba,
)
from TerraLab.terrain.render.palette import (
    _apply_categorical_solar_response,
    _apply_vibrant_ambient_occlusion,
    _apply_vibrant_bloom,
    _sample_cache_value,
    _vibrant_relief_occlusion,
    _vibrant_valley_haze,
)
from TerraLab.terrain.render.triangle_raster import (
    _interpolate_triangle_continuous_values,
    _rasterize_terrain_triangles,
)


class OverlayTrianglePaintMixin:
    def _paint_terrain_triangles(
        self,
        painter,
        asset,
        geometry,
        vertex_materials,
        patch_materials,
        vertex_light,
        patch_vertex_light,
        width,
        height,
        material_key,
        frame_key,
        interaction_active=False,
        vertex_solar=None,
        patch_vertex_solar=None,
        vertex_lunar=None,
        patch_vertex_lunar=None,
        light_factors: TerrainCelestialLightFactors | None = None,
        sky_color: QColor | None = None,
    ):
        """Resolve exact screen visibility and compose the cached RGBA surface."""

        self._last_surface2d_quads = int(geometry.metrics.spans)
        self._last_surface2d_vertices = int(geometry.metrics.output_vertices)
        self._last_surface2d_max_error_px = 0.0
        self._last_surface2d_geometry_s = float(geometry.metrics.elapsed_s)
        paint_started = time.perf_counter()
        render_scale = 0.5 if interaction_active else 1.0
        render_width = max(1, int(math.ceil(float(width) * render_scale)))
        render_height = max(1, int(math.ceil(float(height) * render_scale)))
        supersample = 1
        cache_key = (
            "terrain-frame-image-v1",
            int(width),
            int(height),
            bool(interaction_active),
            frame_key,
        )
        if (
            PERFORMANCE_FLAGS.relief_cached
            and self._terrain_surface_image_geometry is geometry
            and self._terrain_surface_image_cache_key == cache_key
            and self._terrain_surface_image_cache is not None
        ):
            self._last_frame_cache_hit = True
            self._terrain_frame_cache_hits += 1
            painter.drawImage(0, 0, self._terrain_surface_image_cache)
            self._last_surface2d_paint_s = time.perf_counter() - paint_started
            self._last_terrain_raster_s = 0.0
            self._last_horizon_antialias_s = 0.0
            return

        self._last_frame_cache_hit = False
        self._terrain_frame_cache_misses += 1
        raster_started = time.perf_counter()
        raster_key = (
            geometry.cache_token,
            render_width,
            render_height,
            supersample,
        )
        scaled_xy = np.asarray(geometry.xy, dtype=np.float64).copy()
        scaled_xy[:, :, 0] *= float(render_width) / float(width)
        scaled_xy[:, :, 1] *= float(render_height) / float(height)
        if (
            PERFORMANCE_FLAGS.relief_cached
            and raster_key == self._terrain_raster_cache_key
            and self._terrain_raster_cache is not None
        ):
            self._last_raster_cache_hit = True
            triangle_id, bary_u, bary_v = self._terrain_raster_cache
        else:
            self._last_raster_cache_hit = False
            self._terrain_raster_builds += 1
            _depth, triangle_id, bary_u, bary_v = _rasterize_terrain_triangles(
                scaled_xy,
                geometry.depth,
                render_width,
                render_height,
                supersample=supersample,
            )
            if PERFORMANCE_FLAGS.relief_cached:
                self._terrain_raster_cache_key = raster_key
                self._terrain_raster_cache = (triangle_id, bary_u, bary_v)
        covered = triangle_id >= 0
        rgba_high = np.zeros(
            (
                int(render_height) * supersample,
                int(render_width) * supersample,
                4,
            ),
            dtype=np.uint8,
        )
        resolved_material = None
        vibrant_enabled = (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            )
            == SurfaceVisualStyle.VIBRANT.value
        )
        detailed_vibrant = vibrant_enabled and not bool(interaction_active)
        if np.any(covered):
            vertex_rows = np.asarray(geometry.vertex_rows, dtype=np.int32)
            vertex_domain = np.asarray(geometry.vertex_domain, dtype=np.uint8)
            polar_vertices = vertex_domain == 0
            polar_rows = np.where(polar_vertices, vertex_rows, 0)
            polar_columns = np.where(
                polar_vertices, geometry.vertex_columns, 0
            )
            resolved_entry = self._resolve_screen_material(
                asset,
                geometry,
                triangle_id,
                bary_u,
                bary_v,
                covered,
                vertex_materials,
                patch_materials,
                raster_key=raster_key,
                material_key=material_key,
                render_scale=render_scale,
            )
            resolved_material = resolved_entry.materials
            triangle_materials = resolved_entry.triangle_materials
            triangle_surface_xy = resolved_entry.triangle_surface_xy
            protected_categories = resolved_entry.protected
            material_valid = np.asarray(
                triangle_materials.valid, dtype=bool
            )
            categorical = np.asarray(
                triangle_materials.categorical, dtype=bool
            )
            class_ids = np.asarray(
                triangle_materials.class_ids, dtype=np.int64
            )
            source_indices = np.asarray(
                triangle_materials.source_indices, dtype=np.int16
            )
            light_values = np.asarray(
                vertex_light[polar_rows, polar_columns], dtype=np.float64
            )
            solar_values = np.asarray(
                (
                    vertex_solar[polar_rows, polar_columns]
                    if vertex_solar is not None
                    else np.zeros(polar_rows.shape, dtype=np.float32)
                ),
                dtype=np.float64,
            )
            lunar_values = np.asarray(
                (
                    vertex_lunar[polar_rows, polar_columns]
                    if vertex_lunar is not None
                    else np.zeros(polar_rows.shape, dtype=np.float32)
                ),
                dtype=np.float64,
            )
            elevation_values = None
            normal_x_values = None
            normal_y_values = None
            normal_z_values = None
            if detailed_vibrant:
                elevation_values = np.asarray(
                    asset.elevations[polar_rows, polar_columns],
                    dtype=np.float64,
                )
                normal_x_values = np.asarray(
                    asset.normal_x[polar_rows, polar_columns],
                    dtype=np.float64,
                )
                normal_y_values = np.asarray(
                    asset.normal_y[polar_rows, polar_columns],
                    dtype=np.float64,
                )
                normal_z_values = np.asarray(
                    asset.normal_z[polar_rows, polar_columns],
                    dtype=np.float64,
            )
            patch_vertices = vertex_domain == 1
            if np.any(patch_vertices):
                light_values = light_values.copy()
                solar_values = solar_values.copy()
                lunar_values = lunar_values.copy()
                if detailed_vibrant:
                    elevation_values = elevation_values.copy()
                    normal_x_values = normal_x_values.copy()
                    normal_y_values = normal_y_values.copy()
                    normal_z_values = normal_z_values.copy()
                vertex_rows[patch_vertices]
                geometry.vertex_columns[patch_vertices]
                light_values[patch_vertices] = np.asarray(
                    patch_vertex_light[
                        vertex_rows[patch_vertices],
                        geometry.vertex_columns[patch_vertices],
                    ],
                    dtype=np.float64,
                )
                if patch_vertex_solar is not None:
                    solar_values[patch_vertices] = np.asarray(
                        patch_vertex_solar[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
                if patch_vertex_lunar is not None:
                    lunar_values[patch_vertices] = np.asarray(
                        patch_vertex_lunar[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
                if detailed_vibrant:
                    elevation_values[patch_vertices] = np.asarray(
                        asset.near_patch_elevations[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
                    normal_x_values[patch_vertices] = np.asarray(
                        asset.near_patch_normal_x[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
                    normal_y_values[patch_vertices] = np.asarray(
                        asset.near_patch_normal_y[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
                    normal_z_values[patch_vertices] = np.asarray(
                        asset.near_patch_normal_z[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
            surface_cache = self._visible_surface_cache()
            continuous_channels = [
                light_values[..., None],
                np.asarray(geometry.depth, dtype=np.float64)[..., None],
                triangle_surface_xy,
                solar_values[..., None],
                lunar_values[..., None],
            ]
            if detailed_vibrant:
                continuous_channels.extend(
                    (
                        elevation_values[..., None],
                        normal_x_values[..., None],
                        normal_y_values[..., None],
                        normal_z_values[..., None],
                    )
                )
            continuous_values = np.concatenate(
                tuple(continuous_channels), axis=2
            )
            interpolated_continuous, _ = (
                _interpolate_triangle_continuous_values(
                triangle_id,
                bary_u,
                bary_v,
                continuous_values,
                flat=self.render_settings.terrain_shading_mode == "flat",
                )
            )
            maximum_distance_m = (
                self._maximum_terrain_distance_m()
                or float(np.nanmax(geometry.depth))
            )
            material_pixels = covered & resolved_material.valid
            material_rgba = resolved_material.base_rgba
            if vibrant_enabled:
                detail_scale = 1.0 if detailed_vibrant else 0.0
                material_rgba = _apply_categorical_solar_response(
                    material_rgba,
                    resolved_material,
                    interpolated_continuous[..., 4],
                    material_pixels,
                    strength=self.render_settings.vibrant_intensity,
                    midscale_variation=(
                        self.render_settings.vibrant_material_midscale_variation
                        * detail_scale
                    ),
                    microscale_variation=(
                        self.render_settings
                        .vibrant_material_microscale_variation
                        * detail_scale
                    ),
                    slope_influence=(
                        self.render_settings.vibrant_material_slope_influence
                        * detail_scale
                    ),
                    snow_rock_blend=(
                        self.render_settings.vibrant_snow_rock_blend
                        * detail_scale
                    ),
                    water_shore_variation=(
                        self.render_settings.vibrant_water_shore_variation
                        * detail_scale
                    ),
                    normal_x=(
                        interpolated_continuous[..., 7]
                        if detailed_vibrant
                        else None
                    ),
                    normal_y=(
                        interpolated_continuous[..., 8]
                        if detailed_vibrant
                        else None
                    ),
                    normal_z=(
                        interpolated_continuous[..., 9]
                        if detailed_vibrant
                        else None
                    ),
                    source_legend_ids=tuple(
                        _sample_cache_value(
                            surface_cache, "source_legend_ids", ()
                        )
                        or ()
                    ),
                )
            composed = compose_vertex_rgba(
                material_rgba,
                interpolated_continuous[..., 0],
                interpolated_continuous[..., 1],
                self.render_settings,
                maximum_distance_m=maximum_distance_m,
                horizon_rgb=(
                    (
                        sky_color.red(),
                        sky_color.green(),
                        sky_color.blue(),
                    )
                    if isinstance(sky_color, QColor)
                    else None
                ),
                atmosphere_strength=0.0 if vibrant_enabled else 1.0,
            )
            if vibrant_enabled:
                valley_haze = None
                if detailed_vibrant:
                    relief_occlusion = _vibrant_relief_occlusion(
                        interpolated_continuous[..., 6],
                        interpolated_continuous[..., 9],
                        material_pixels,
                        radius_px=(
                            self.render_settings
                            .vibrant_ambient_occlusion_radius_px
                            * render_scale
                        ),
                        relief_scale_m=(
                            self.render_settings
                            .vibrant_ambient_occlusion_relief_scale_m
                        ),
                    )
                    composed = _apply_vibrant_ambient_occlusion(
                        composed,
                        relief_occlusion,
                        material_pixels,
                        strength=(
                            self.render_settings
                            .vibrant_ambient_occlusion_strength
                        ),
                    )
                    valley_haze = _vibrant_valley_haze(
                        relief_occlusion,
                        interpolated_continuous[..., 1],
                        material_pixels,
                        maximum_distance_m=maximum_distance_m,
                        strength=(
                            self.render_settings.vibrant_valley_haze_strength
                        ),
                    )
                composed = apply_vibrant_color_grade(
                    composed,
                    interpolated_continuous[..., 0],
                    interpolated_continuous[..., 1],
                    self.render_settings,
                    maximum_distance_m=maximum_distance_m,
                    valid_mask=material_pixels,
                    additional_haze=valley_haze,
                    daylight_factor=(
                        light_factors.solar_ambient
                        if light_factors is not None
                        else 1.0
                    ),
                    moonlight_factor=(
                        light_factors.lunar_strength
                        if light_factors is not None
                        else 0.0
                    ),
                    solar_exposure=interpolated_continuous[..., 4],
                    lunar_exposure=interpolated_continuous[..., 5],
                    atmosphere_rgb=(
                        (
                            sky_color.red(),
                            sky_color.green(),
                            sky_color.blue(),
                        )
                        if isinstance(sky_color, QColor)
                        else None
                    ),
                )
            rgba_high[material_pixels] = composed[material_pixels]
            if vibrant_enabled:
                rgba_high = _soften_categorical_edges(
                    rgba_high,
                    resolved_material,
                    covered,
                    strength=(
                        self.render_settings.categorical_edge_smoothing_strength
                    ),
                    protected=protected_categories,
                )
                rgba_high = _apply_vibrant_bloom(
                    rgba_high,
                    material_pixels,
                    self.render_settings,
                    render_scale=render_scale,
                    light_intensity=interpolated_continuous[..., 0],
                    distance_m=interpolated_continuous[..., 1],
                    maximum_distance_m=maximum_distance_m,
                    daylight_factor=(
                        light_factors.solar_ambient
                        if light_factors is not None
                        else 1.0
                    ),
                    moonlight_factor=(
                        light_factors.lunar_strength
                        if light_factors is not None
                        else 0.0
                    ),
                )
        self._last_terrain_raster_s = time.perf_counter() - raster_started

        settings = self.render_settings
        antialias_started = time.perf_counter()
        if (
            settings.horizon_antialiasing_enabled
            and settings.horizon_antialiasing_mode != "off"
            and supersample == 1
        ):
            coverage_samples = (
                settings.horizon_supersampling_factor
                if settings.horizon_antialiasing_mode == "supersample"
                else 1
            )
            rgba_high = _apply_horizon_coverage(
                rgba_high,
                triangle_id,
                scaled_xy,
                filter_width_px=settings.horizon_filter_width_px,
                supersampling_factor=coverage_samples,
            )
        self._last_horizon_antialias_s = (
            time.perf_counter() - antialias_started
        )

        if supersample > 1:
            # Average premultiplied sub-samples, then return to straight RGBA.
            premultiplied = rgba_high.astype(np.float32)
            alpha = premultiplied[:, :, 3:4] / 255.0
            premultiplied[:, :, :3] *= alpha
            reduced = premultiplied.reshape(
                int(render_height),
                supersample,
                int(render_width),
                supersample,
                4,
            ).mean(axis=(1, 3))
            reduced_alpha = reduced[:, :, 3:4] / 255.0
            reduced[:, :, :3] = np.divide(
                reduced[:, :, :3],
                np.maximum(reduced_alpha, 1e-12),
                out=np.zeros_like(reduced[:, :, :3]),
                where=reduced_alpha > 0.0,
            )
            rgba = np.clip(np.rint(reduced), 0, 255).astype(np.uint8)
        else:
            rgba = rgba_high
        image = QImage(
            rgba.data,
            int(render_width),
            int(render_height),
            int(rgba.strides[0]),
            QImage.Format_RGBA8888,
        ).copy()
        if render_width != int(width) or render_height != int(height):
            image = image.scaled(
                int(width),
                int(height),
                Qt.IgnoreAspectRatio,
                Qt.SmoothTransformation,
            )
        visible_material_categories = (
            resolved_material is not None
            and np.any(
                np.asarray(resolved_material.valid, dtype=bool)
                & np.asarray(resolved_material.categorical, dtype=bool)
            )
        )
        self._terrain_resolved_materials = (
            resolved_material if visible_material_categories else None
        )
        if (
            self.render_settings.terrain_surface_diagnostics_enabled
            and resolved_material is not None
        ):
            material_rgba = np.zeros_like(rgba_high)
            diagnostic_pixels = covered & resolved_material.valid
            material_rgba[diagnostic_pixels] = resolved_material.base_rgba[
                diagnostic_pixels
            ]
            material_image = QImage(
                material_rgba.data,
                int(render_width),
                int(render_height),
                int(material_rgba.strides[0]),
                QImage.Format_RGBA8888,
            ).copy()
            if render_width != int(width) or render_height != int(height):
                material_image = material_image.scaled(
                    int(width),
                    int(height),
                    Qt.IgnoreAspectRatio,
                    Qt.SmoothTransformation,
                )
            visible_categories = (
                diagnostic_pixels & resolved_material.categorical
            )
            histogram = {}
            if np.any(visible_categories):
                pairs = np.column_stack(
                    (
                        resolved_material.source_indices[visible_categories],
                        resolved_material.class_ids[visible_categories],
                    )
                )
                unique_pairs, counts = np.unique(
                    pairs, axis=0, return_counts=True
                )
                histogram = {
                    f"{int(source)}:{int(class_id)}": int(count)
                    for (source, class_id), count in zip(unique_pairs, counts)
                }
            categorical_vertices = categorical & material_valid
            mixed_triangles = int(
                np.count_nonzero(
                    np.any(categorical_vertices, axis=1)
                    & np.any(~categorical_vertices, axis=1)
                )
            )
            categorical_counts = {1: 0, 2: 0, 3: 0}
            for triangle_index in np.flatnonzero(
                np.any(categorical_vertices, axis=1)
            ):
                mask = categorical_vertices[triangle_index]
                identities = np.column_stack(
                    (
                        source_indices[triangle_index, mask],
                        class_ids[triangle_index, mask],
                    )
                )
                count = int(len(np.unique(identities, axis=0)))
                categorical_counts[min(3, max(1, count))] += 1
            self._terrain_material_image = material_image
            self._terrain_surface_diagnostics = {
                "class_histogram": histogram,
                "categorical_triangles": {
                    str(key): int(value)
                    for key, value in categorical_counts.items()
                },
                "mixed_triangles": mixed_triangles,
                "material_resolution_s": float(
                    self._last_material_resolution_s
                ),
                "rasterization_s": float(self._last_terrain_raster_s),
            }
        elif not self.render_settings.terrain_surface_diagnostics_enabled:
            self._terrain_material_image = None
            self._terrain_surface_diagnostics = {}
        if PERFORMANCE_FLAGS.relief_cached:
            self._terrain_surface_image_cache_key = cache_key
            self._terrain_surface_image_cache = image
            self._terrain_surface_image_geometry = geometry
        painter.drawImage(0, 0, image)
        self._last_surface2d_paint_s = float(time.perf_counter() - paint_started)

    def _terrain_polygons_for_geometry(self, geometry):
        if (
            self._terrain_polygon_cache_geometry is geometry
            and self._terrain_polygon_cache is not None
        ):
            return self._terrain_polygon_cache
        polygons = []
        for span in geometry.spans:
            points = [
                QPointF(float(x), float(y))
                for x, y in zip(span.x, span.top_y)
            ]
            points.extend(
                QPointF(float(x), float(y))
                for x, y in zip(span.bottom_x[::-1], span.bottom_y[::-1])
            )
            polygons.append((span, QPolygonF(points)))
        result = tuple(polygons)
        self._terrain_polygon_cache_key = id(geometry)
        self._terrain_polygon_cache_geometry = geometry
        self._terrain_polygon_cache = result
        return result

