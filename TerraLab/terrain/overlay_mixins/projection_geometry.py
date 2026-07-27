"""Projected relief geometry and visible-triangle construction."""

from __future__ import annotations

import math
import time

import numpy as np
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPainter

from TerraLab.common.performance.flags import PERFORMANCE_FLAGS
from TerraLab.terrain.render.config import TerrainCelestialLightContext
from TerraLab.terrain.render.lighting import terrain_celestial_light_factors
from TerraLab.terrain.render.overlay_types import (
    _TerrainGeometryMetrics,
    _TerrainTriangleGeometry,
)
from TerraLab.terrain.render.palette import _sample_cache_value


class OverlayProjectionGeometryMixin:
    def _project_mesh_column(
        self,
        projection_fn,
        projection_fn_numpy,
        az_value,
        altitudes,
        height,
        px_alt,
    ):
        if projection_fn_numpy:
            az_arr = np.full_like(altitudes, float(az_value), dtype=np.float32)
            projected = projection_fn_numpy(
                np.asarray(altitudes, dtype=np.float64),
                np.asarray(az_arr, dtype=np.float64),
            )
            if projected is None or len(projected) < 2:
                empty = np.full(np.shape(altitudes), np.nan, dtype=np.float64)
                return empty, empty.copy()
            sx, sy = projected[:2]
            sx = np.asarray(sx, dtype=np.float64)
            sy = np.asarray(sy, dtype=np.float64)
            if len(projected) >= 3:
                projected_valid = np.asarray(projected[2], dtype=bool)
                sx = np.where(projected_valid, sx, np.nan)
                sy = np.where(projected_valid, sy, np.nan)
            return sx, sy

        sx = []
        sy = []
        for alt in altitudes:
            point = projection_fn(float(alt), float(az_value))
            if point:
                sx.append(point[0])
                sy.append(point[1])
            else:
                sx.append(np.nan)
                sy.append(np.nan)
        return np.asarray(sx, dtype=np.float64), np.asarray(sy, dtype=np.float64)

    def _profile_horizon_lookup(self):
        if not self._layers:
            return None
        az_ref = None
        max_h = None
        for band_pts, _night_c, _day_c in self._layers:
            az_raw, h_raw = band_pts.points
            if az_raw is None:
                continue
            valid = getattr(band_pts, "valid_mask", None)
            if valid is None:
                valid = np.ones_like(az_raw, dtype=bool)
            az = np.asarray(az_raw, dtype=np.float32)
            h = np.asarray(h_raw, dtype=np.float32)
            valid = np.asarray(valid, dtype=bool)
            if az_ref is None:
                az_ref = az
                max_h = np.full_like(az_ref, -np.inf, dtype=np.float32)
            if len(az) != len(az_ref) or not np.allclose(az, az_ref):
                h = np.interp(az_ref, az, h, left=-np.inf, right=-np.inf)
                valid = np.isfinite(h)
            max_h = np.maximum(
                max_h,
                np.where(valid & np.isfinite(h), h, -np.inf).astype(
                    np.float32
                ),
            )
        if az_ref is None or max_h is None:
            return None
        valid = np.isfinite(max_h) & (max_h > -80.0)
        if not np.any(valid):
            return None
        return az_ref[valid], max_h[valid]

    @staticmethod
    def _quad_area_px(points) -> float:
        area = 0.0
        for i, p0 in enumerate(points):
            p1 = points[(i + 1) % len(points)]
            area += float(p0.x()) * float(p1.y())
            area -= float(p1.x()) * float(p0.y())
        return abs(area) * 0.5

    def _draw_terrain_surface_2d(
        self,
        painter,
        mesh,
        projection_fn,
        width,
        height,
        px_alt,
        cur_az,
        az_min,
        az_max,
        t_night,
        sky_color,
        sun_alt,
        sun_az,
        terrain_shading_enabled=True,
        projection_fn_numpy=None,
        interaction_active=False,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        self._last_surface2d_quads = 0
        self._last_surface2d_vertices = 0
        self._last_surface2d_max_error_px = 0.0
        self._last_surface2d_geometry_s = 0.0
        self._last_surface2d_paint_s = 0.0
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
            return
        az_raw = asset.azimuths
        distances = asset.distances
        elevations = asset.elevations
        valid = asset.valid
        visible = asset.visible
        normal_x = asset.normal_x
        normal_y = asset.normal_y
        normal_z = asset.normal_z

        light_context = self._resolve_light_context(
            light_context, sun_alt=sun_alt, sun_az=sun_az
        )
        sun_alt, sun_az, sun_vec = self._configured_light(
            sun_alt, sun_az, light_context
        )
        light_factors = terrain_celestial_light_factors(
            light_context, self.render_settings
        )
        terrain_shading_enabled = bool(
            terrain_shading_enabled
            and self.render_settings.terrain_lighting_enabled
        )
        shade_key = (
            asset.mesh_id,
            bool(terrain_shading_enabled),
            None if sun_alt is None else round(float(sun_alt) * 4.0) / 4.0,
            None if sun_az is None else round(float(sun_az) * 4.0) / 4.0,
            (
                None
                if light_context.moon_altitude_deg is None
                else round(float(light_context.moon_altitude_deg) * 4.0) / 4.0
            ),
            (
                None
                if light_context.moon_azimuth_deg is None
                else round(float(light_context.moon_azimuth_deg) * 4.0) / 4.0
            ),
            round(float(light_context.moon_illumination) * 256.0) / 256.0,
            round(float(light_context.eclipse_factor) * 256.0) / 256.0,
            repr(self.render_settings),
        )
        shade_grid = (
            self._terrain_shade_cache.get(shade_key)
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        if shade_grid is None:
            sun_visibility = self._terrain_sun_visibility(
                mesh,
                elevations,
                valid,
                visible,
                distances,
                az_raw,
                sun_alt,
                sun_az,
                terrain_shading_enabled=terrain_shading_enabled,
            )
            shade_grid = self._terrain_light_factor(
                normal_x,
                normal_y,
                normal_z,
                distances[:, None],
                sun_vec,
                sun_alt,
                terrain_shading_enabled=terrain_shading_enabled,
                sun_visibility=sun_visibility,
                light_context=light_context,
            )
            minimum_light, maximum_light = self._terrain_light_bounds(
                light_factors
            )
            shade_grid = self._smooth_light_grid(
                shade_grid,
                valid & visible,
                min_value=minimum_light,
                max_value=maximum_light,
            )
            if PERFORMANCE_FLAGS.relief_cached:
                self._terrain_shade_cache.put(
                    shade_key, shade_grid, int(shade_grid.nbytes)
                )
        geometry = self._terrain_geometry_for_view(
            asset,
            projection_fn,
            width,
            height,
            px_alt,
            cur_az,
            az_min,
            az_max,
            projection_fn_numpy=projection_fn_numpy,
        )
        self._last_surface2d_quads = int(geometry.metrics.spans)
        self._last_surface2d_vertices = int(geometry.metrics.output_vertices)
        self._last_surface2d_max_error_px = float(
            geometry.metrics.max_error_px
        )
        self._last_surface2d_geometry_s = float(geometry.metrics.elapsed_s)

        paint_started = time.perf_counter()
        quantized_night = round(float(t_night) * 256.0) / 256.0
        surface_cache_key = (
            int(width),
            int(height),
            shade_key,
            quantized_night,
            int(sky_color.rgba()),
            bool(terrain_shading_enabled),
            bool(self.terrain_surface_opaque),
        )
        if (
            PERFORMANCE_FLAGS.relief_cached
            and self._terrain_surface_image_geometry is geometry
            and surface_cache_key == self._terrain_surface_image_cache_key
            and self._terrain_surface_image_cache is not None
        ):
            painter.drawImage(0, 0, self._terrain_surface_image_cache)
            self._last_surface2d_quads = int(self._terrain_surface_image_drawn)
            self._last_surface2d_paint_s = float(
                time.perf_counter() - paint_started
            )
            painter.setRenderHint(QPainter.Antialiasing, True)
            return

        surface_image = None
        surface_painter = None
        target_painter = painter
        if PERFORMANCE_FLAGS.relief_cached:
            surface_image = QImage(
                int(width), int(height), QImage.Format_ARGB32_Premultiplied
            )
            surface_image.fill(0)
            surface_painter = QPainter(surface_image)
            target_painter = surface_painter

        # Adjacent depth spans share exact boundaries. Antialiasing each fill
        # independently exposes sub-pixel sky seams between those polygons;
        # the final profile ridge is antialiased separately after the surface.
        target_painter.setRenderHint(QPainter.Antialiasing, False)
        target_painter.setPen(Qt.NoPen)
        drawn = 0
        for span, polygon in self._terrain_polygons_for_geometry(geometry):
            xs = span.x
            ys = np.concatenate((span.top_y, span.bottom_y))
            if float(np.max(xs)) < -64.0 or float(np.min(xs)) > float(width) + 64.0:
                continue
            if float(np.max(ys)) < -64.0 or float(np.min(ys)) > float(height) + 64.0:
                continue
            if self._quad_area_px(polygon) < 0.35:
                continue
            segment_shade = shade_grid[
                int(span.row_index), span.column_indices
            ]
            brush = self._terrain_span_brush(
                span.x,
                segment_shade,
                span.distance_m,
                quantized_night,
                sky_color,
                sun_vec,
                sun_alt,
                terrain_shading_enabled,
                light_context=light_context,
            )
            target_painter.setBrush(brush)
            target_painter.drawPolygon(polygon)
            drawn += 1

        if surface_painter is not None and surface_image is not None:
            surface_painter.end()
            self._terrain_surface_image_cache_key = surface_cache_key
            self._terrain_surface_image_cache = surface_image
            self._terrain_surface_image_geometry = geometry
            self._terrain_surface_image_drawn = int(drawn)
            painter.drawImage(0, 0, surface_image)
        self._last_surface2d_quads = drawn
        self._last_surface2d_paint_s = float(time.perf_counter() - paint_started)
        painter.setRenderHint(QPainter.Antialiasing, True)

    def _terrain_triangles_for_view(
        self,
        asset,
        projection_fn,
        width,
        height,
        cur_az,
        az_min,
        az_max,
        projection_fn_numpy=None,
    ):
        """Project the complete polar mesh and form valid screen triangles."""

        signature = self._projection_geometry_signature(
            projection_fn, float(cur_az) - 90.0, float(cur_az) + 90.0
        )
        cache_key = (
            "triangles-v2-categorical-grid",
            int(asset.mesh_id),
            int(width),
            int(height),
            float(cur_az),
            float(az_min),
            float(az_max),
            signature,
        )
        if (
            PERFORMANCE_FLAGS.relief_cached
            and cache_key == self._terrain_geometry_cache_key
            and isinstance(self._terrain_geometry_cache, _TerrainTriangleGeometry)
        ):
            return self._terrain_geometry_cache

        started = time.perf_counter()
        azimuths = np.asarray(asset.azimuths, dtype=np.float64)
        relative = (azimuths - float(cur_az) + 180.0) % 360.0 - 180.0
        full_order = np.argsort(relative, kind="stable")
        full_unwrapped = float(cur_az) + relative[full_order]
        in_view = (
            (full_unwrapped >= float(az_min))
            & (full_unwrapped <= float(az_max))
        )
        selected = np.flatnonzero(in_view)
        if selected.size:
            selected = np.arange(
                max(0, int(selected[0]) - 1),
                min(full_order.size, int(selected[-1]) + 2),
                dtype=np.int32,
            )
        else:
            selected = np.empty(0, dtype=np.int32)
        order = full_order[selected]
        unwrapped_az = full_unwrapped[selected]
        if order.size < 2:
            return None
        altitudes = np.asarray(asset.altitudes[:, order], dtype=np.float64)
        az_grid = np.broadcast_to(unwrapped_az[None, :], altitudes.shape)

        if projection_fn_numpy is not None:
            projected = projection_fn_numpy(altitudes, az_grid)
            if projected is None or len(projected) < 2:
                return None
            sx = np.asarray(projected[0], dtype=np.float64)
            sy = np.asarray(projected[1], dtype=np.float64)
            projected_valid = np.ones(altitudes.shape, dtype=bool)
            if len(projected) >= 3:
                projected_valid &= np.asarray(projected[2], dtype=bool)
        else:
            sx = np.full(altitudes.shape, np.nan, dtype=np.float64)
            sy = np.full(altitudes.shape, np.nan, dtype=np.float64)
            projected_valid = np.zeros(altitudes.shape, dtype=bool)
            for row in range(altitudes.shape[0]):
                for column in range(altitudes.shape[1]):
                    point = projection_fn(
                        float(altitudes[row, column]),
                        float(unwrapped_az[column]),
                    )
                    if point is not None and np.all(np.isfinite(point[:2])):
                        sx[row, column], sy[row, column] = point[:2]
                        projected_valid[row, column] = True

        original_valid = np.asarray(asset.valid[:, order], dtype=bool)
        original_visible = np.asarray(asset.visible[:, order], dtype=bool)
        vertex_valid = original_valid & projected_valid & np.isfinite(sx) & np.isfinite(sy)
        az_delta = np.diff(unwrapped_az)
        finite_steps = az_delta[np.isfinite(az_delta) & (az_delta > 1e-9)]
        nominal_step = float(np.median(finite_steps)) if finite_steps.size else 1.0
        adjacent = np.isfinite(az_delta) & (az_delta > 0.0) & (
            az_delta <= nominal_step * 1.5 + 1e-9
        )
        cell_valid = (
            vertex_valid[:-1, :-1]
            & vertex_valid[1:, :-1]
            & vertex_valid[1:, 1:]
            & vertex_valid[:-1, 1:]
            & adjacent[None, :]
        )
        # A cell whose four angular samples remain below a nearer running
        # maximum cannot contribute to the final image.  Keeping every cell
        # touching a visible vertex supplies a one-cell transition band while
        # avoiding work the z-buffer would deterministically discard.
        cell_visible = (
            original_visible[:-1, :-1]
            | original_visible[1:, :-1]
            | original_visible[1:, 1:]
            | original_visible[:-1, 1:]
        )
        cell_valid &= cell_visible
        cell_rows, cell_columns = np.nonzero(cell_valid)
        if cell_rows.size:
            triangle_rows = np.empty((cell_rows.size * 2, 3), dtype=np.int32)
            triangle_columns_sorted = np.empty_like(triangle_rows)
            triangle_rows[0::2] = np.column_stack(
                (cell_rows, cell_rows + 1, cell_rows + 1)
            )
            triangle_columns_sorted[0::2] = np.column_stack(
                (cell_columns, cell_columns, cell_columns + 1)
            )
            triangle_rows[1::2] = np.column_stack(
                (cell_rows, cell_rows + 1, cell_rows)
            )
            triangle_columns_sorted[1::2] = np.column_stack(
                (cell_columns, cell_columns + 1, cell_columns + 1)
            )
            triangle_columns = order[triangle_columns_sorted].astype(np.int32)
            triangle_x = sx[triangle_rows, triangle_columns_sorted]
            triangle_y = sy[triangle_rows, triangle_columns_sorted]
            xy = np.stack((triangle_x, triangle_y), axis=2)
            depth = np.asarray(asset.distances, dtype=np.float64)[triangle_rows]
            vertex_domain = np.zeros(triangle_rows.shape, dtype=np.uint8)
        else:
            xy = np.empty((0, 3, 2), dtype=np.float64)
            depth = np.empty((0, 3), dtype=np.float64)
            triangle_rows = np.empty((0, 3), dtype=np.int32)
            triangle_columns = np.empty((0, 3), dtype=np.int32)
            vertex_domain = np.empty((0, 3), dtype=np.uint8)

        # The field immediately below the observer is a genuine Cartesian ENU
        # mesh. Unlike a polar fan it has no collapsed azimuthal edge and thus
        # remains well-conditioned when the camera points at the nadir.
        patch_shape = np.shape(asset.near_patch_altitudes)
        patch_vertex_count = int(np.prod(patch_shape)) if len(patch_shape) == 2 else 0
        if patch_shape[0] >= 2 and patch_shape[1] >= 2:
            patch_east, patch_north = np.meshgrid(
                np.asarray(asset.near_patch_eastings, dtype=np.float64),
                np.asarray(asset.near_patch_northings, dtype=np.float64),
            )
            patch_distance = np.hypot(patch_east, patch_north)
            convergence = float(
                getattr(getattr(self, "profile", None), "grid_convergence_deg", 0.0)
                or 0.0
            )
            patch_azimuth = (
                np.degrees(np.arctan2(patch_east, patch_north)) + convergence
            ) % 360.0
            patch_relative = (
                patch_azimuth - float(cur_az) + 180.0
            ) % 360.0 - 180.0
            patch_unwrapped = float(cur_az) + patch_relative
            patch_unwrapped = np.where(
                patch_distance <= 1e-9, float(cur_az), patch_unwrapped
            )
            patch_altitudes = np.asarray(
                asset.near_patch_altitudes, dtype=np.float64
            )
            if projection_fn_numpy is not None:
                patch_projected = projection_fn_numpy(
                    patch_altitudes, patch_unwrapped
                )
                patch_sx = np.asarray(patch_projected[0], dtype=np.float64)
                patch_sy = np.asarray(patch_projected[1], dtype=np.float64)
                patch_projected_valid = np.ones(patch_shape, dtype=bool)
                if len(patch_projected) >= 3:
                    patch_projected_valid &= np.asarray(
                        patch_projected[2], dtype=bool
                    )
            else:
                patch_sx = np.full(patch_shape, np.nan, dtype=np.float64)
                patch_sy = np.full(patch_shape, np.nan, dtype=np.float64)
                patch_projected_valid = np.zeros(patch_shape, dtype=bool)
                for patch_row, patch_column in np.ndindex(patch_shape):
                    point = projection_fn(
                        float(patch_altitudes[patch_row, patch_column]),
                        float(patch_unwrapped[patch_row, patch_column]),
                    )
                    if point is not None and np.all(np.isfinite(point[:2])):
                        patch_sx[patch_row, patch_column] = point[0]
                        patch_sy[patch_row, patch_column] = point[1]
                        patch_projected_valid[patch_row, patch_column] = True
            patch_vertex_valid = (
                np.asarray(asset.near_patch_valid, dtype=bool)
                & patch_projected_valid
                & np.isfinite(patch_sx)
                & np.isfinite(patch_sy)
            )
            patch_cells = (
                patch_vertex_valid[:-1, :-1]
                & patch_vertex_valid[1:, :-1]
                & patch_vertex_valid[1:, 1:]
                & patch_vertex_valid[:-1, 1:]
            )
            patch_rows, patch_columns = np.nonzero(patch_cells)
            if patch_rows.size:
                patch_triangle_rows = np.empty(
                    (patch_rows.size * 2, 3), dtype=np.int32
                )
                patch_triangle_columns = np.empty_like(patch_triangle_rows)
                patch_triangle_rows[0::2] = np.column_stack(
                    (patch_rows, patch_rows + 1, patch_rows + 1)
                )
                patch_triangle_columns[0::2] = np.column_stack(
                    (patch_columns, patch_columns, patch_columns + 1)
                )
                patch_triangle_rows[1::2] = np.column_stack(
                    (patch_rows, patch_rows + 1, patch_rows)
                )
                patch_triangle_columns[1::2] = np.column_stack(
                    (patch_columns, patch_columns + 1, patch_columns + 1)
                )
                patch_xy = np.stack(
                    (
                        patch_sx[patch_triangle_rows, patch_triangle_columns],
                        patch_sy[patch_triangle_rows, patch_triangle_columns],
                    ),
                    axis=2,
                )
                patch_depth = patch_distance[
                    patch_triangle_rows, patch_triangle_columns
                ]
                xy = np.concatenate((xy, patch_xy), axis=0)
                depth = np.concatenate((depth, patch_depth), axis=0)
                triangle_rows = np.concatenate(
                    (triangle_rows, patch_triangle_rows), axis=0
                )
                triangle_columns = np.concatenate(
                    (triangle_columns, patch_triangle_columns), axis=0
                )
                vertex_domain = np.concatenate(
                    (
                        vertex_domain,
                        np.ones(patch_triangle_rows.shape, dtype=np.uint8),
                    ),
                    axis=0,
                )

        twice_area = (
            (xy[:, 1, 0] - xy[:, 0, 0]) * (xy[:, 2, 1] - xy[:, 0, 1])
            - (xy[:, 1, 1] - xy[:, 0, 1]) * (xy[:, 2, 0] - xy[:, 0, 0])
        )
        edge_01 = np.hypot(
            xy[:, 1, 0] - xy[:, 0, 0], xy[:, 1, 1] - xy[:, 0, 1]
        )
        edge_12 = np.hypot(
            xy[:, 2, 0] - xy[:, 1, 0], xy[:, 2, 1] - xy[:, 1, 1]
        )
        edge_20 = np.hypot(
            xy[:, 0, 0] - xy[:, 2, 0], xy[:, 0, 1] - xy[:, 2, 1]
        )
        maximum_valid_edge = math.hypot(float(width), float(height)) * 2.0
        on_screen = (
            (np.max(xy[:, :, 0], axis=1) >= 0.0)
            & (np.min(xy[:, :, 0], axis=1) < float(width))
            & (np.max(xy[:, :, 1], axis=1) >= 0.0)
            & (np.min(xy[:, :, 1], axis=1) < float(height))
        )
        keep = (
            np.isfinite(twice_area)
            & (np.abs(twice_area) > 1e-9)
            & on_screen
            & (edge_01 <= maximum_valid_edge)
            & (edge_12 <= maximum_valid_edge)
            & (edge_20 <= maximum_valid_edge)
        )
        xy = xy[keep]
        depth = depth[keep]
        triangle_rows = triangle_rows[keep]
        triangle_columns = triangle_columns[keep]
        vertex_domain = vertex_domain[keep]
        elapsed = float(time.perf_counter() - started)
        metrics = _TerrainGeometryMetrics(
            spans=int(xy.shape[0]),
            source_samples=int(altitudes.size + patch_vertex_count),
            invalid_samples=int(altitudes.size - np.count_nonzero(vertex_valid)),
            output_vertices=int(xy.shape[0] * 3),
            max_error_px=0.0,
            elapsed_s=elapsed,
        )
        result = _TerrainTriangleGeometry(
            xy,
            depth,
            triangle_rows,
            triangle_columns,
            vertex_domain,
            metrics,
        )
        if PERFORMANCE_FLAGS.relief_cached:
            self._terrain_geometry_cache_key = cache_key
            self._terrain_geometry_cache = result
        return result

