"""Profile-band geometry and non-relief surface drawing."""

from __future__ import annotations

import time

import numpy as np
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QBrush, QColor

from TerraLab.common.performance.flags import PERFORMANCE_FLAGS
from TerraLab.terrain.render.config import TerrainCelestialLightContext
from TerraLab.terrain.render.geometry import _build_terrain_surface_spans
from TerraLab.terrain.render.overlay_types import (
    _TerrainGeometryMetrics,
    _TerrainSurfaceGeometry,
    _extrema_lod_indices,
)
from TerraLab.terrain.render.palette import (
    _atmospheric_haze_color,
    _distance_haze_factor,
    _lerp_color,
    _palette_color,
    _with_alpha,
)


class OverlayBandDrawMixin:
    @staticmethod
    def _projection_geometry_signature(projection_fn, az_min, az_max):
        signature = []
        for altitude in (-30.0, 0.0, 30.0):
            for azimuth in np.linspace(float(az_min), float(az_max), 5):
                try:
                    point = projection_fn(float(altitude), float(azimuth))
                    if point is None:
                        signature.append((None, None))
                    else:
                        signature.append((float(point[0]), float(point[1])))
                except Exception:
                    signature.append((None, None))
        return tuple(signature)

    def _terrain_geometry_for_view(
        self,
        asset,
        projection_fn,
        width,
        height,
        px_alt,
        cur_az,
        az_min,
        az_max,
        projection_fn_numpy=None,
    ):
        signature = self._projection_geometry_signature(
            projection_fn, az_min, az_max
        )
        cache_key = (
            int(asset.mesh_id),
            int(width),
            int(height),
            float(px_alt),
            float(cur_az),
            float(az_min),
            float(az_max),
            signature,
        )
        if (
            PERFORMANCE_FLAGS.relief_cached
            and cache_key == self._terrain_geometry_cache_key
            and self._terrain_geometry_cache is not None
        ):
            return self._terrain_geometry_cache

        started = time.perf_counter()
        azimuths = asset.azimuths
        az_diffs = np.diff(azimuths.astype(np.float64))
        az_diffs = az_diffs[np.isfinite(az_diffs) & (az_diffs > 0.0)]
        margin = float(np.median(az_diffs)) if az_diffs.size else 1.0
        base_offset = round((float(cur_az) - 180.0) / 360.0) * 360.0
        offsets = (base_offset - 360.0, base_offset, base_offset + 360.0)
        selected_azimuth_parts = []
        unwrapped_column_parts = []
        for offset in offsets:
            final_azimuths = azimuths + float(offset)
            columns = np.flatnonzero(
                (final_azimuths >= float(az_min) - margin)
                & (final_azimuths <= float(az_max) + margin)
            ).astype(np.int32)
            if columns.size == 0:
                continue
            wrap_index = int(round(float(offset) / 360.0))
            selected_azimuth_parts.append(final_azimuths[columns])
            unwrapped_column_parts.append(
                columns.astype(np.int64) + wrap_index * int(azimuths.size)
            )

        if not selected_azimuth_parts:
            return _TerrainSurfaceGeometry((), _TerrainGeometryMetrics())
        selected_azimuths = np.concatenate(selected_azimuth_parts).astype(np.float64)
        unwrapped_columns = np.concatenate(unwrapped_column_parts).astype(np.int64)
        sort_order = np.argsort(selected_azimuths, kind="stable")
        selected_azimuths = selected_azimuths[sort_order]
        unwrapped_columns = unwrapped_columns[sort_order]
        if selected_azimuths.size > 1:
            unique = np.r_[True, np.diff(selected_azimuths) > 1e-9]
            selected_azimuths = selected_azimuths[unique]
            unwrapped_columns = unwrapped_columns[unique]
        source_columns = (unwrapped_columns % int(azimuths.size)).astype(np.int32)

        selected_altitudes = np.asarray(
            asset.altitudes[:, source_columns], dtype=np.float64
        )
        if projection_fn_numpy:
            azimuth_grid = np.broadcast_to(
                selected_azimuths[None, :], selected_altitudes.shape
            )
            projected = projection_fn_numpy(selected_altitudes, azimuth_grid)
            projected_x = np.asarray(projected[0], dtype=np.float64)
            projected_y = np.asarray(projected[1], dtype=np.float64)
            if len(projected) >= 3:
                projected_valid = np.asarray(projected[2], dtype=bool)
                projected_x = np.where(projected_valid, projected_x, np.nan)
                projected_y = np.where(projected_valid, projected_y, np.nan)
        else:
            x_values = []
            y_values = []
            for source_column, azimuth in zip(source_columns, selected_azimuths):
                sx, sy = self._project_mesh_column(
                    projection_fn,
                    None,
                    float(azimuth),
                    asset.altitudes[:, int(source_column)],
                    height,
                    px_alt,
                )
                x_values.append(sx)
                y_values.append(sy)
            projected_x = np.asarray(x_values, dtype=np.float64).T
            projected_y = np.asarray(y_values, dtype=np.float64).T
        part = _build_terrain_surface_spans(
            distances=asset.distances,
            column_indices=unwrapped_columns,
            azimuths=selected_azimuths,
            projected_x=projected_x,
            projected_y=projected_y,
            valid=asset.valid[:, source_columns],
            height=float(height),
            simplify_tolerance_px=None,
            column_modulus=int(azimuths.size),
        )

        elapsed = float(time.perf_counter() - started)
        metrics = _TerrainGeometryMetrics(
            spans=len(part.spans),
            source_samples=part.metrics.source_samples,
            invalid_samples=part.metrics.invalid_samples,
            occluded_samples=part.metrics.occluded_samples,
            simplified_vertices=part.metrics.simplified_vertices,
            output_vertices=part.metrics.output_vertices,
            max_error_px=part.metrics.max_error_px,
            elapsed_s=elapsed,
        )
        result = _TerrainSurfaceGeometry(tuple(part.spans), metrics)
        if PERFORMANCE_FLAGS.relief_cached:
            self._terrain_geometry_cache_key = cache_key
            self._terrain_geometry_cache = result
        return result

    def _draw_terrain_mesh(
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
        self._draw_terrain_surface_2d(
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
            terrain_shading_enabled=terrain_shading_enabled,
            projection_fn_numpy=projection_fn_numpy,
            interaction_active=interaction_active,
            light_context=light_context,
        )

    def _draw_profile_horizon_cap(
        self,
        painter,
        projection_fn,
        height,
        px_alt,
        cur_az,
        az_min,
        az_max,
        t_night,
        sky_color,
        projection_fn_numpy=None,
        fill_to_bottom=True,
        draw_ridge=True,
    ):
        lookup = self._profile_horizon_lookup()
        if lookup is None:
            return
        az_raw, h_raw = lookup
        if az_raw.size < 2:
            return

        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        all_sx = []
        all_sy = []
        for offset in offsets:
            final_az = az_raw + offset
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask):
                continue

            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            if projection_fn_numpy:
                projected = projection_fn_numpy(culled_h, culled_az)
                sx, sy = projected[:2]
                if len(projected) >= 3:
                    projected_valid = np.asarray(projected[2], dtype=bool)
                    sx = np.where(projected_valid, sx, np.nan)
                    sy = np.where(projected_valid, sy, np.nan)
            else:
                sx = []
                sy = []
                for az_value, height_value in zip(culled_az, culled_h):
                    point = projection_fn(float(height_value), float(az_value))
                    if point:
                        sx.append(point[0])
                        sy.append(point[1])
                    else:
                        sx.append(np.nan)
                        sy.append(height * 2.0)
                sx = np.asarray(sx, dtype=np.float32)
                sy = np.asarray(sy, dtype=np.float32)

            finite_mask = np.isfinite(sx) & np.isfinite(sy)
            if not np.any(finite_mask):
                continue
            edge_mask = np.diff(
                np.pad(finite_mask.astype(np.int8), (1, 1), constant_values=0)
            )
            starts = np.where(edge_mask == 1)[0]
            stops = np.where(edge_mask == -1)[0]
            for start, stop in zip(starts, stops):
                seg_sx = np.asarray(sx[start:stop], dtype=np.float32)
                seg_sy = np.asarray(sy[start:stop], dtype=np.float32)
                if len(seg_sx) >= 2:
                    all_sx.append(seg_sx)
                    all_sy.append(seg_sy)

        if not all_sx:
            return

        cap_night, cap_day = _palette_color(0.56)
        cap_base = _lerp_color(cap_day, cap_night, t_night)
        cap_fill = _lerp_color(
            cap_base, _atmospheric_haze_color(sky_color, t_night), 0.16
        )
        if fill_to_bottom:
            self._fill_strip_downward_numpy(
                painter, all_sx, all_sy, cap_fill, height * 2.0, solid=True
            )

        if draw_ridge:
            ridge = _with_alpha(QColor(cap_fill).lighter(105), 58)
            shadow = _with_alpha(QColor(cap_fill).darker(110), 36)
            self._stroke_ridge_lines_numpy(
                painter, all_sx, all_sy, shadow, width=0.9, y_offset=0.9
            )
            self._stroke_ridge_lines_numpy(
                painter, all_sx, all_sy, ridge, width=0.65
            )

    def _band_edge_colors(self, fill_color, t_night, band_pts):
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        distance_factor = 1.0 - _distance_haze_factor(band_max_m)
        ridge_alpha = 9 + int(16 * distance_factor)
        shadow_alpha = 12 + int(20 * distance_factor)

        if t_night >= 0.45:
            ridge = QColor(fill_color).lighter(108)
            shadow = QColor(fill_color).darker(106)
            ridge_alpha += int(5 * t_night)
            shadow_alpha += int(5 * t_night)
        else:
            ridge = QColor(fill_color).darker(108)
            shadow = QColor(fill_color).lighter(104)

        return _with_alpha(ridge, ridge_alpha), _with_alpha(shadow, shadow_alpha)

    def _band_surface_color(self, fill_color, t_night, band_pts):
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        distance_factor = 1.0 - _distance_haze_factor(band_max_m)
        alpha = 7 + int(14 * distance_factor)
        if t_night >= 0.45:
            color = QColor(fill_color).lighter(106)
            alpha += int(5 * t_night)
        else:
            color = QColor(fill_color).darker(108)
        return _with_alpha(color, alpha)

    def _draw_band_linear(
        self,
        painter,
        band_pts,
        color,
        proj_fn,
        w,
        h,
        px_alt,
        cur_az,
        az_min,
        az_max,
        proj_fn_numpy=None,
        ridge_color=None,
        shadow_color=None,
        surface_color=None,
        sun_alt=None,
        sun_az=None,
        terrain_shading_enabled=True,
        sky_color=None,
        interaction_active=False,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        """
        Draw one filled silhouette band using the shared sky projection.
        """
        polygon_cache_key = ("band", id(band_pts))
        cacheable_fill = bool(
            not terrain_shading_enabled
            and ridge_color is None
            and shadow_color is None
            and surface_color is None
        )
        if cacheable_fill and self._draw_cached_profile_polygons(
            painter, polygon_cache_key, color
        ):
            return

        az_raw, h_raw = band_pts.points
        if az_raw is None:
            return
        valid_raw = getattr(band_pts, "valid_mask", None)
        if valid_raw is None:
            valid_raw = np.ones_like(az_raw, dtype=bool)

        # Calculate base offset to center around current azimuth
        # az_raw is 0..360, so center is 180.
        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        all_sx = []
        all_sy = []
        all_az = []
        all_h = []

        for offset in offsets:
            final_az = az_raw + offset

            # 1. CULLING: Only keep points within view
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask):
                continue

            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            culled_valid = np.asarray(valid_raw[mask], dtype=bool)
            if not np.any(culled_valid):
                continue

            point_budget = self._profile_point_budget(w, interaction_active)
            if len(culled_az) > point_budget and bool(np.all(culled_valid)):
                lod_indices = _extrema_lod_indices(culled_h, point_budget)
                culled_az = culled_az[lod_indices]
                culled_h = culled_h[lod_indices]
                culled_valid = culled_valid[lod_indices]

            # 2. VECTORIZED PROJECTION
            if proj_fn_numpy:
                projected = proj_fn_numpy(culled_h, culled_az)
                if projected is None or len(projected) < 2:
                    continue
                sx = np.asarray(projected[0], dtype=np.float64)
                sy = np.asarray(projected[1], dtype=np.float64)
                if len(projected) >= 3:
                    projected_valid = np.asarray(projected[2], dtype=bool)
                    sx = np.where(projected_valid, sx, np.nan)
                    sy = np.where(projected_valid, sy, np.nan)
            else:
                # Fallback to scalar (slow)
                sx = []
                sy = []
                for a_val, h_val in zip(culled_az, culled_h):
                    point = proj_fn(float(h_val), float(a_val))
                    if point:
                        sx.append(point[0])
                        sy.append(point[1])
                    else:
                        sx.append(np.nan)
                        sy.append(h * 2)  # Safety
                sx = np.array(sx)
                sy = np.array(sy)

            finite_mask = np.isfinite(sx) & np.isfinite(sy) & culled_valid
            if not np.any(finite_mask):
                continue
            edge_mask = np.diff(
                np.pad(finite_mask.astype(np.int8), (1, 1), constant_values=0)
            )
            starts = np.where(edge_mask == 1)[0]
            stops = np.where(edge_mask == -1)[0]
            for start, stop in zip(starts, stops):
                seg_sx = sx[start:stop]
                seg_sy = sy[start:stop]
                seg_az = culled_az[start:stop]
                seg_h = culled_h[start:stop]
                if len(seg_sx) >= 2:
                    all_sx.append(seg_sx)
                    all_sy.append(seg_sy)
                    all_az.append(seg_az)
                    all_h.append(seg_h)

        if all_sx:
            self._cache_profile_category_polygons(
                polygon_cache_key,
                all_sx,
                all_sy,
                all_az,
                h * 2,
            )
            if terrain_shading_enabled:
                self._fill_shaded_strip_downward_numpy(
                    painter,
                    all_sx,
                    all_sy,
                    all_az,
                    all_h,
                    color,
                    h * 2,
                    sun_alt,
                    sun_az,
                    band_pts,
                    sky_color,
                    light_context=light_context,
                )
            elif cacheable_fill and self._profile_polygon_cache_view_key is not None:
                painter.setBrush(QBrush(color))
                painter.setPen(Qt.NoPen)
                for polygon in self._cache_profile_polygons(
                    polygon_cache_key, all_sx, all_sy, h * 2
                ):
                    painter.drawPolygon(polygon)
            else:
                self._fill_strip_downward_numpy(
                    painter, all_sx, all_sy, color, h * 2, solid=True
                )
            if shadow_color is not None:
                self._stroke_ridge_lines_numpy(
                    painter,
                    all_sx,
                    all_sy,
                    shadow_color,
                    width=1.2,
                    y_offset=1.15,
                )
            if ridge_color is not None:
                self._stroke_ridge_lines_numpy(
                    painter, all_sx, all_sy, ridge_color, width=0.8
                )
                self._draw_edge_texture_numpy(
                    painter, all_sx, all_sy, all_az, ridge_color, band_pts
                )
            if surface_color is not None:
                self._draw_band_surface_linear(
                    painter,
                    band_pts,
                    proj_fn,
                    h,
                    px_alt,
                    cur_az,
                    az_min,
                    az_max,
                    proj_fn_numpy,
                    surface_color,
                )

    def _draw_band_surface_linear(
        self,
        painter,
        band_pts,
        proj_fn,
        h,
        px_alt,
        cur_az,
        az_min,
        az_max,
        proj_fn_numpy,
        color,
    ):
        az_raw, h_raw = getattr(band_pts, "surface_points", (None, None))
        if az_raw is None:
            return
        valid_raw = getattr(band_pts, "surface_valid_mask", None)
        if valid_raw is None:
            valid_raw = np.ones_like(az_raw, dtype=bool)

        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]
        all_sx = []
        all_sy = []

        for offset in offsets:
            final_az = az_raw + offset
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask):
                continue

            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            culled_valid = np.asarray(valid_raw[mask], dtype=bool)
            if not np.any(culled_valid):
                continue

            if proj_fn_numpy:
                sx, sy_base = proj_fn_numpy(
                    np.zeros_like(culled_az), culled_az
                )
                sy = sy_base + 2.0 - (culled_h * px_alt)
            else:
                sx = []
                sy = []
                for az_value, height_value in zip(culled_az, culled_h):
                    anchor = proj_fn(0, az_value)
                    if anchor:
                        sx.append(anchor[0])
                        sy.append(anchor[1] + 2.0 - height_value * px_alt)
                    else:
                        sx.append(np.nan)
                        sy.append(h * 2)
                sx = np.array(sx)
                sy = np.array(sy)

            finite_mask = np.isfinite(sx) & np.isfinite(sy) & culled_valid
            if not np.any(finite_mask):
                continue
            edge_mask = np.diff(
                np.pad(finite_mask.astype(np.int8), (1, 1), constant_values=0)
            )
            starts = np.where(edge_mask == 1)[0]
            stops = np.where(edge_mask == -1)[0]
            for start, stop in zip(starts, stops):
                seg_sx = sx[start:stop]
                seg_sy = sy[start:stop]
                if len(seg_sx) >= 2:
                    all_sx.append(seg_sx)
                    all_sy.append(seg_sy)

        if all_sx:
            self._stroke_ridge_lines_numpy(
                painter, all_sx, all_sy, color, width=0.9
            )

