"""Procedural fallback ground and vectorized ridge fills."""

from __future__ import annotations

import math
import random

import numpy as np
from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainterPath,
    QPen,
    QPolygonF,
)

from TerraLab.terrain.render.config import TerrainCelestialLightContext
from TerraLab.terrain.render.overlay_types import _extrema_lod_indices
from TerraLab.terrain.render.palette import (
    _BandPoints,
    _clamp01,
    _distance_haze_factor,
)


class OverlayFallbackMixin:
    def _fill_shaded_strip_downward_numpy(
        self,
        painter,
        list_sx,
        list_sy,
        list_az,
        list_h,
        base_color,
        bottom_y,
        sun_alt,
        sun_az,
        band_pts,
        sky_color,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        for sx_arr, sy_arr, az_arr, h_arr in zip(
            list_sx, list_sy, list_az, list_h
        ):
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue

            f_sx = np.asarray(sx_arr[valid], dtype=np.float32)
            f_sy = np.asarray(sy_arr[valid], dtype=np.float32)
            f_az = np.asarray(az_arr[valid], dtype=np.float32)
            f_h = np.asarray(h_arr[valid], dtype=np.float32)
            if len(f_sx) < 2:
                continue

            light_grid = self._terrain_profile_light_grid(
                f_az,
                f_h,
                sun_alt,
                sun_az,
                band_pts,
                light_context=light_context,
            )
            shade_values = light_grid.intensity
            surface_rgba, surface_valid = self._profile_surface_samples(
                band_pts, f_az
            )
            surface_rgba = np.asarray(surface_rgba, dtype=np.uint8)
            surface_valid = np.asarray(surface_valid, dtype=bool)
            has_surface = bool(
                surface_rgba.shape == (len(f_az), 4)
                and surface_valid.shape == (len(f_az),)
                and np.any(surface_valid)
            )
            band_distance = float(
                getattr(band_pts, "band_max", 0.0) or 0.0
            )
            if (
                not has_surface
                and np.nanmax(np.abs(shade_values - 1.0)) < 0.006
            ):
                color = self._compose_profile_light_color(
                    base_color,
                    float(np.nanmean(shade_values)),
                    band_distance,
                    sky_color,
                    light_grid.factors,
                    solar_exposure=float(
                        np.nanmean(light_grid.solar_exposure)
                    ),
                    lunar_exposure=float(
                        np.nanmean(light_grid.lunar_exposure)
                    ),
                )
                self._fill_strip_downward_numpy(
                    painter, [f_sx], [f_sy], color, bottom_y, solid=True
                )
                continue

            min_x = float(np.nanmin(f_sx))
            max_x = float(np.nanmax(f_sx))
            if max_x - min_x < 1.0:
                sample_color = QColor(base_color)
                if has_surface:
                    valid_indices = np.flatnonzero(surface_valid)
                    if valid_indices.size:
                        rgba = surface_rgba[int(valid_indices[0])]
                        sample_color = QColor(*(int(value) for value in rgba))
                color = self._compose_profile_light_color(
                    sample_color,
                    float(np.nanmean(shade_values)),
                    band_distance,
                    sky_color,
                    light_grid.factors,
                    solar_exposure=float(
                        np.nanmean(light_grid.solar_exposure)
                    ),
                    lunar_exposure=float(
                        np.nanmean(light_grid.lunar_exposure)
                    ),
                )
                self._fill_strip_downward_numpy(
                    painter, [f_sx], [f_sy], color, bottom_y, solid=True
                )
                continue

            path = QPainterPath()
            path.moveTo(float(f_sx[0]), float(f_sy[0]))
            for x, y in zip(f_sx[1:], f_sy[1:]):
                path.lineTo(float(x), float(y))
            path.lineTo(float(f_sx[-1]), float(bottom_y))
            path.lineTo(float(f_sx[0]), float(bottom_y))
            path.closeSubpath()

            gradient = QLinearGradient(min_x, 0.0, max_x, 0.0)
            stop_count = min(96 if has_surface else 18, len(f_sx))
            stop_indices = np.linspace(0, len(f_sx) - 1, stop_count).astype(int)
            stops = []
            for idx in stop_indices:
                pos = _clamp01((float(f_sx[idx]) - min_x) / (max_x - min_x))
                stops.append(
                    (
                        pos,
                        int(idx),
                        float(shade_values[idx]),
                        float(light_grid.solar_exposure[idx]),
                        float(light_grid.lunar_exposure[idx]),
                    )
                )
            stops.sort(key=lambda item: item[0])

            last_pos = -1.0
            for pos, idx, shade, solar_exposure, lunar_exposure in stops:
                if pos <= last_pos + 0.001:
                    continue
                material_color = QColor(base_color)
                if has_surface and bool(surface_valid[idx]):
                    rgba = surface_rgba[idx]
                    material_color = QColor(
                        *(int(value) for value in rgba)
                    )
                color = self._compose_profile_light_color(
                    material_color,
                    shade,
                    band_distance,
                    sky_color,
                    light_grid.factors,
                    solar_exposure=solar_exposure,
                    lunar_exposure=lunar_exposure,
                )
                color.setAlpha(255)
                gradient.setColorAt(pos, color)
                last_pos = pos
            if last_pos < 1.0:
                material_color = QColor(base_color)
                if has_surface and bool(surface_valid[-1]):
                    material_color = QColor(
                        *(int(value) for value in surface_rgba[-1])
                    )
                color = self._compose_profile_light_color(
                    material_color,
                    float(shade_values[-1]),
                    band_distance,
                    sky_color,
                    light_grid.factors,
                    solar_exposure=float(light_grid.solar_exposure[-1]),
                    lunar_exposure=float(light_grid.lunar_exposure[-1]),
                )
                color.setAlpha(255)
                gradient.setColorAt(1.0, color)

            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(gradient))
            painter.drawPath(path)

    def _draw_edge_texture_numpy(
        self, painter, list_sx, list_sy, list_az, color, band_pts
    ):
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        distance_factor = 1.0 - _distance_haze_factor(band_max_m)
        if distance_factor <= 0.18:
            return

        texture_color = QColor(color)
        texture_color.setAlpha(min(color.alpha(), 5 + int(10 * distance_factor)))
        if texture_color.alpha() <= 0:
            return

        pen = QPen(texture_color)
        pen.setWidthF(0.55)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        for sx_arr, sy_arr, az_arr in zip(list_sx, list_sy, list_az):
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue
            f_sx = np.asarray(sx_arr[valid], dtype=np.float32)
            f_sy = np.asarray(sy_arr[valid], dtype=np.float32)
            f_az = np.asarray(az_arr[valid], dtype=np.float32)
            if len(f_sx) < 2:
                continue

            step = max(2, int(math.ceil(len(f_sx) / 110.0)))
            for i in range(0, len(f_sx), step):
                seed = float(f_az[i]) * 12.9898 + band_max_m * 0.001
                noise = math.sin(seed) * 43758.5453
                noise -= math.floor(noise)
                if noise < 0.52:
                    continue
                length = 0.6 + 2.0 * noise * distance_factor
                x = float(f_sx[i])
                y = float(f_sy[i]) + 0.55
                painter.drawLine(QPointF(x, y), QPointF(x, y + length))

    def _draw_ground_linear(
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
        overlap_px=0.0,
        projection_fn_numpy=None,
        ridge_color=None,
        interaction_active=False,
    ):
        """
        Draw ground fill using the same projection logic as bands.
        """
        polygon_cache_key = ("ground", id(band_pts), float(overlap_px))
        if ridge_color is None and self._draw_cached_profile_polygons(
            painter, polygon_cache_key, color
        ):
            return

        az_raw, h_raw = band_pts.points
        if az_raw is None:
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
            point_budget = self._profile_point_budget(w, interaction_active)
            if len(culled_az) > point_budget:
                lod_indices = _extrema_lod_indices(culled_h, point_budget)
                culled_az = culled_az[lod_indices]
                culled_h = culled_h[lod_indices]

            if projection_fn_numpy:
                projected = projection_fn_numpy(culled_h, culled_az)
                if projected is None or len(projected) < 2:
                    continue
                sx = np.asarray(projected[0], dtype=np.float64)
                sy = np.asarray(projected[1], dtype=np.float64) - overlap_px
                if len(projected) >= 3:
                    projected_valid = np.asarray(projected[2], dtype=bool)
                    sx = np.where(projected_valid, sx, np.nan)
                    sy = np.where(projected_valid, sy, np.nan)
            else:
                sx = []
                sy = []
                for a_val, h_val in zip(culled_az, culled_h):
                    point = proj_fn(float(h_val), float(a_val))
                    if point:
                        sx.append(point[0])
                        sy.append(point[1] - overlap_px)
                    else:
                        sx.append(np.nan)
                        sy.append(h * 2)
                sx = np.array(sx)
                sy = np.array(sy)

            all_sx.append(sx)
            all_sy.append(sy)

        if all_sx:
            if ridge_color is None and self._profile_polygon_cache_view_key is not None:
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
            if ridge_color is not None:
                self._stroke_ridge_lines_numpy(
                    painter, all_sx, all_sy, ridge_color, width=0.65
                )

    def _stroke_ridge_lines_numpy(
        self, painter, list_sx, list_sy, color, width=1.0, y_offset=0.0
    ):
        pen = QPen(color)
        pen.setWidthF(float(width))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        for sx_arr, sy_arr in zip(list_sx, list_sy):
            if len(sx_arr) < 2:
                continue

            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue

            f_sx = sx_arr[valid]
            f_sy = sy_arr[valid] + float(y_offset)
            if len(f_sx) < 2:
                continue

            path = QPainterPath()
            path.moveTo(float(f_sx[0]), float(f_sy[0]))
            for x, y in zip(f_sx[1:], f_sy[1:]):
                path.lineTo(float(x), float(y))
            painter.drawPath(path)

    def _fill_strip_downward_numpy(
        self, painter, list_sx, list_sy, color, bottom_y, solid=False
    ):
        """Vectorized polygon drawing from NumPy arrays."""
        painter.setBrush(QBrush(color))
        if solid:
            painter.setPen(Qt.NoPen)
        else:
            painter.setPen(QPen(color, 1))

        for sx_arr, sy_arr in zip(list_sx, list_sy):
            if len(sx_arr) < 2:
                continue

            # 1. Filter out NaNs/Infs (projection singularities)
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue

            f_sx = sx_arr[valid]
            f_sy = sy_arr[valid]

            if len(f_sx) < 2:
                continue

            # Constructing QPolygonF from list of QPointF
            # Convert to float explicit for compatibility
            pts = [QPointF(float(x), float(y)) for x, y in zip(f_sx, f_sy)]

            # Close downward
            pts.append(QPointF(float(f_sx[-1]), float(bottom_y)))
            pts.append(QPointF(float(f_sx[0]), float(bottom_y)))

            poly = QPolygonF(pts)
            painter.drawPolygon(poly)

    def _build_procedural_fallback(self):
        print(
            "[HorizonOverlay] WARNING: Using procedural fallback (South Flat / North Mountains)."
        )
        rng = random.Random(42)

        # 3 simple layers matching POC colors
        configs = [
            (
                "far_25_60",
                QColor(38, 48, 68),
                QColor(140, 155, 175),
                0.6,
                3.0,
                1.5,
            ),
            (
                "mid_3_10",
                QColor(18, 25, 42),
                QColor(100, 120, 135),
                0.8,
                2.0,
                2.0,
            ),
            (
                "near_0_1",
                QColor(8, 12, 22),
                QColor(70, 90, 100),
                1.0,
                5.0,
                1.0,
            ),
        ]

        for bid, nc, dc, base, freq, amp in configs:
            pts_az = []
            pts_h = []
            # Use the same angular sampling selected for real horizon rays.
            from TerraLab.common.utils import get_config_value
            from TerraLab.terrain.ray_precision import (
                normalize_ray_step_deg,
                ray_count,
            )

            ray_step_deg = normalize_ray_step_deg(
                get_config_value("horizon_ray_step_deg", 0.5)
            )
            for step in range(ray_count(ray_step_deg)):
                az = step * ray_step_deg
                # Normalize az to 0..360
                norm_az = az % 360.0

                # Logic: South is approx 90..270. North is 270..360 + 0..90.
                # Let's define "Flat Zone" as 110 to 250 to have some transition


                if 135 < norm_az < 225:
                    # Pure Flat
                    val = 0.2
                else:
                    # Mountains
                    rad = math.radians(az)

                    # Noise composition
                    n1 = abs(math.sin(rad * freq)) * amp
                    n2 = abs(math.sin(rad * freq * 2.3)) * (amp * 0.5)
                    n3 = abs(math.sin(rad * freq * 5.1)) * (amp * 0.25)

                    val = base + (n1 + n2 + n3) * rng.uniform(0.9, 1.1)

                    # Smooth transition to flat zone?
                    # Simple lerp if near boundaries (90..135 and 225..270)
                    if 90 < norm_az <= 135:
                        t = (norm_az - 90) / 45.0  # 0..1
                        # 1=Flat, 0=Mount
                        val = val * (1.0 - t) + 0.2 * t
                    elif 225 <= norm_az < 270:
                        t = (norm_az - 225) / 45.0  # 0..1
                        # 0=Flat, 1=Mount
                        val = 0.2 * (1.0 - t) + val * t

                pts_az.append(az)
                pts_h.append(max(0.2, val))

            bp = _BandPoints.__new__(_BandPoints)
            bp.points = (
                np.array(pts_az, dtype=np.float32),
                np.array(pts_h, dtype=np.float32),
            )
            bp.valid_mask = np.ones(len(pts_az), dtype=bool)
            self._layers.append((bp, nc, dc))

