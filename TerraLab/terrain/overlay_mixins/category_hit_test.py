"""Surface-category hit testing and cached profile polygons."""

from __future__ import annotations

import math

import numpy as np
from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QBrush, QPolygonF

from TerraLab.scene.plans.terrain_geometry import TerrainGeometryPlan
from TerraLab.terrain.land_cover.legends.category_info import (
    LandCoverCategoryInfo,
)
from TerraLab.terrain.render.overlay_types import (
    _TerrainSurfaceGeometry,
    _TerrainTriangleGeometry,
)
from TerraLab.terrain.render.palette import _sample_cache_value


class OverlayCategoryHitTestMixin:
    def category_at_screen(
        self, x: float, y: float
    ) -> LandCoverCategoryInfo | None:
        """Return the already-sampled visible category below a screen point.

        This method is deliberately cache-only: it cannot open a GeoTIFF,
        enqueue a worker request or interpolate semantic class identifiers.
        """

        cache = self._visible_surface_cache()
        if cache is None:
            return None
        geometry = self._terrain_surface_image_geometry
        if geometry is None:
            return self._profile_category_at_screen(cache, float(x), float(y))
        asset = self._terrain_render_asset
        if asset is None:
            return None
        point_x = float(x)
        point_y = float(y)

        if isinstance(geometry, TerrainGeometryPlan):
            geometry = geometry.triangle_geometry

        if isinstance(geometry, _TerrainTriangleGeometry):
            display_materials = self._terrain_resolved_materials
            display_image = self._terrain_surface_image_cache
            if display_materials is not None and display_image is not None:
                material_height, material_width = display_materials.valid.shape
                material_x = int(
                    math.floor(
                        point_x
                        * material_width
                        / max(1, int(display_image.width()))
                    )
                )
                material_y = int(
                    math.floor(
                        point_y
                        * material_height
                        / max(1, int(display_image.height()))
                    )
                )
                if (
                    0 <= material_x < material_width
                    and 0 <= material_y < material_height
                    and bool(display_materials.valid[material_y, material_x])
                    and bool(
                        display_materials.categorical[material_y, material_x]
                    )
                ):
                    class_id = int(
                        display_materials.class_ids[material_y, material_x]
                    )
                    source_index = int(
                        display_materials.source_indices[
                            material_y, material_x
                        ]
                    )
                    if class_id >= 0 and source_index >= 0:
                        return self._category_metadata(
                            cache, class_id, source_index
                        )
            if (
                self._terrain_raster_cache is None
                or not isinstance(self._terrain_raster_cache_key, tuple)
                or len(self._terrain_raster_cache_key) < 4
                or self._terrain_raster_cache_key[0]
                is not geometry.cache_token
            ):
                return None
            render_width = int(self._terrain_raster_cache_key[1])
            render_height = int(self._terrain_raster_cache_key[2])
            triangle_id, bary_u, bary_v = self._terrain_raster_cache
            image = self._terrain_surface_image_cache
            display_width = (
                int(image.width()) if image is not None else render_width
            )
            display_height = (
                int(image.height()) if image is not None else render_height
            )
            px = int(
                math.floor(point_x * render_width / max(1, display_width))
            )
            py = int(
                math.floor(point_y * render_height / max(1, display_height))
            )
            if not (0 <= px < render_width and 0 <= py < render_height):
                return None
            triangle = int(triangle_id[py, px])
            if triangle < 0:
                return None
            weights = np.asarray(
                [
                    float(bary_u[py, px]),
                    float(bary_v[py, px]),
                    1.0 - float(bary_u[py, px]) - float(bary_v[py, px]),
                ],
                dtype=np.float64,
            )
            mesh_shape = tuple(np.asarray(asset.elevations).shape)
            # At a class boundary, use the categorical vertex with the
            # greatest barycentric contribution. Class codes are never mixed.
            for vertex in np.argsort(-weights):
                row = int(geometry.vertex_rows[triangle, vertex])
                column = int(geometry.vertex_columns[triangle, vertex])
                if int(geometry.vertex_domain[triangle, vertex]) == 1:
                    value = self._category_grid_value(
                        cache, "near_patch", row, column
                    )
                else:
                    value = self._relief_category_value(
                        cache, row, column, mesh_shape
                    )
                if value is not None:
                    return self._category_metadata(cache, value[0], value[1])
            return None

        if isinstance(geometry, _TerrainSurfaceGeometry):
            point = QPointF(point_x, point_y)
            mesh_shape = tuple(np.asarray(asset.elevations).shape)
            # Polygons are painted in order; the last containing polygon is
            # the visible upper contribution at this pixel.
            for span, polygon in reversed(
                self._terrain_polygons_for_geometry(geometry)
            ):
                if not polygon.containsPoint(point, Qt.OddEvenFill):
                    continue
                nearest = int(np.argmin(np.abs(np.asarray(span.x) - point_x)))
                column = int(span.column_indices[nearest])
                value = self._relief_category_value(
                    cache, int(span.row_index), column, mesh_shape
                )
                if value is not None:
                    return self._category_metadata(cache, value[0], value[1])
                return None
        return None

    def terrain_surface_diagnostics(self) -> dict:
        """Return cache-only diagnostics from the most recently painted frame."""

        return dict(self._terrain_surface_diagnostics)

    def terrain_cache_diagnostics(self) -> dict:
        """Return hit, build, timing and resident-memory terrain metrics."""

        def _cache_metrics(cache, builds: int, last_hit: bool) -> dict:
            return {
                "hits": int(cache.hits),
                "misses": int(cache.misses),
                "evictions": int(cache.evictions),
                "builds": int(builds),
                "resident_bytes": int(cache.resident_bytes),
                "budget_bytes": int(cache.max_bytes),
                "last_hit": bool(last_hit),
            }

        return {
            "base_material": {
                **_cache_metrics(
                    self._terrain_base_material_cache,
                    self._terrain_base_material_builds,
                    self._last_base_material_cache_hit,
                ),
                "last_time_s": float(self._last_terrain_color_s),
            },
            "lighting": _cache_metrics(
                self._terrain_shade_cache,
                self._terrain_lighting_builds,
                self._last_lighting_cache_hit,
            ),
            "resolved_material": {
                **_cache_metrics(
                    self._terrain_resolved_material_cache,
                    self._terrain_resolved_material_builds,
                    self._last_resolved_material_cache_hit,
                ),
                "last_time_s": float(self._last_material_resolution_s),
            },
            "raster": {
                "builds": int(self._terrain_raster_builds),
                "last_hit": bool(self._last_raster_cache_hit),
                "last_time_s": float(self._last_terrain_raster_s),
            },
            "frame": {
                "hits": int(self._terrain_frame_cache_hits),
                "misses": int(self._terrain_frame_cache_misses),
                "last_hit": bool(self._last_frame_cache_hit),
            },
        }

    def terrain_surface_diagnostic_at_screen(
        self, x: float, y: float
    ) -> dict | None:
        """Inspect the resolved pixel material without touching raster sources."""

        materials = self._terrain_resolved_materials
        image = self._terrain_surface_image_cache
        if materials is None or image is None:
            return None
        height, width = materials.valid.shape
        px = int(math.floor(float(x) * width / max(1, int(image.width()))))
        py = int(math.floor(float(y) * height / max(1, int(image.height()))))
        if not (0 <= px < width and 0 <= py < height):
            return None
        if not bool(materials.valid[py, px]):
            return None
        source_index = int(materials.source_indices[py, px])
        class_id = int(materials.class_ids[py, px])
        categorical = bool(materials.categorical[py, px])
        cache = self._visible_surface_cache()
        legends = tuple(
            _sample_cache_value(cache, "source_legend_ids", ()) or ()
        )
        info = (
            self._category_metadata(cache, class_id, source_index)
            if categorical
            else None
        )
        raster_row = raster_column = None
        lod_factor = None
        sample_origin = None
        geometry = self._terrain_surface_image_geometry
        raster_cache = self._terrain_raster_cache
        if (
            isinstance(geometry, _TerrainTriangleGeometry)
            and raster_cache is not None
        ):
            triangle_id, bary_u, bary_v = raster_cache
            triangle = int(triangle_id[py, px])
            if triangle >= 0:
                weights = np.asarray(
                    (
                        bary_u[py, px],
                        bary_v[py, px],
                        1.0 - bary_u[py, px] - bary_v[py, px],
                    )
                )
                vertex = int(np.argmax(weights))
                row = int(geometry.vertex_rows[triangle, vertex])
                column = int(geometry.vertex_columns[triangle, vertex])
                domain = int(geometry.vertex_domain[triangle, vertex])
                prefix = "near_patch" if domain == 1 else "visual"
                asset = self._terrain_render_asset
                shape = (
                    np.shape(asset.near_patch_elevations)
                    if domain == 1 and asset is not None
                    else np.shape(asset.elevations)
                    if asset is not None
                    else ()
                )
                diagnostic = _sample_cache_value(
                    cache, f"{prefix}_lod_factors"
                )
                if domain == 0 and (
                    diagnostic is None or np.shape(diagnostic) != tuple(shape)
                ):
                    prefix = "relief"
                    sampled_rows = np.asarray(
                        _sample_cache_value(
                            cache,
                            "relief_distance_indices",
                            (),
                        ),
                        dtype=np.int32,
                    )
                    sampled_columns = np.asarray(
                        _sample_cache_value(
                            cache,
                            "relief_azimuth_indices",
                            (),
                        ),
                        dtype=np.int32,
                    )
                    if sampled_rows.size and sampled_columns.size:
                        row = int(np.argmin(np.abs(sampled_rows - row)))
                        circular = np.abs(
                            sampled_columns - (column % max(1, int(shape[1])))
                        )
                        circular = np.minimum(
                            circular,
                            max(1, int(shape[1])) - circular,
                        )
                        column = int(np.argmin(circular))
                values = {}
                for name in (
                    "raster_rows",
                    "raster_columns",
                    "lod_factors",
                    "sample_origins",
                ):
                    grid = _sample_cache_value(cache, f"{prefix}_{name}")
                    if grid is not None and np.ndim(grid) == 2:
                        grid_array = np.asarray(grid)
                        if (
                            0 <= row < grid_array.shape[0]
                            and 0 <= column < grid_array.shape[1]
                        ):
                            values[name] = int(grid_array[row, column])
                raster_row = values.get("raster_rows")
                raster_column = values.get("raster_columns")
                lod_factor = values.get("lod_factors")
                sample_origin = values.get("sample_origins")
        origin_names = {
            1: "exact",
            2: "modal",
            3: "source_fallback",
            4: "terrain_fallback",
        }
        return {
            "class_id": class_id if categorical else None,
            "category_name": getattr(info, "name", None),
            "source_index": source_index,
            "legend_id": (
                legends[source_index]
                if 0 <= source_index < len(legends)
                else ""
            ),
            "material_type": "categorical" if categorical else "continuous",
            "lod_factor": lod_factor,
            "raster_row": raster_row,
            "raster_column": raster_column,
            "sample_origin": origin_names.get(
                sample_origin,
                "source_fallback" if source_index >= 0 else "terrain_fallback",
            ),
        }

    @staticmethod
    def _profile_point_budget(width: int, interaction_active: bool) -> int:
        width = max(1, int(width))
        if interaction_active:
            return max(256, min(1024, width))
        return max(512, min(2048, width * 2))

    def _profile_layers_for_frame(self, interaction_active: bool):
        layers = self._layers
        if not interaction_active or len(layers) <= 12:
            return layers
        indices = np.unique(
            np.rint(np.linspace(0, len(layers) - 1, 12)).astype(np.int32)
        )
        return [layers[int(index)] for index in indices]

    def _prepare_profile_polygon_cache(
        self,
        profile,
        projection_fn,
        width,
        height,
        current_azimuth,
        az_min,
        az_max,
        interaction_active,
    ) -> None:
        view_key = (
            id(profile),
            int(width),
            int(height),
            float(current_azimuth),
            float(az_min),
            float(az_max),
            bool(interaction_active),
            self._profile_point_budget(width, interaction_active),
            self._projection_geometry_signature(projection_fn, az_min, az_max),
        )
        if view_key != self._profile_polygon_cache_view_key:
            self._profile_polygon_cache_view_key = view_key
            self._profile_polygon_cache.clear()
            self._profile_category_hit_cache.clear()

    def _draw_cached_profile_polygons(self, painter, cache_key, color) -> bool:
        if self._profile_polygon_cache_view_key is None:
            return False
        polygons = self._profile_polygon_cache.get(cache_key)
        if polygons is None:
            return False
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        for polygon in polygons:
            painter.drawPolygon(polygon)
        return True

    def _cache_profile_polygons(self, cache_key, list_sx, list_sy, bottom_y):
        polygons = []
        for sx_arr, sy_arr in zip(list_sx, list_sy):
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if np.count_nonzero(valid) < 2:
                continue
            f_sx = np.asarray(sx_arr[valid], dtype=np.float32)
            f_sy = np.asarray(sy_arr[valid], dtype=np.float32)
            points = [QPointF(float(x), float(y)) for x, y in zip(f_sx, f_sy)]
            points.append(QPointF(float(f_sx[-1]), float(bottom_y)))
            points.append(QPointF(float(f_sx[0]), float(bottom_y)))
            polygons.append(QPolygonF(points))
        result = tuple(polygons)
        if self._profile_polygon_cache_view_key is not None:
            self._profile_polygon_cache[cache_key] = result
        return result

    def _cache_profile_category_polygons(
        self,
        cache_key,
        list_sx,
        list_sy,
        list_azimuths,
        bottom_y,
    ) -> None:
        entries = []
        for sx_arr, sy_arr, azimuth_arr in zip(
            list_sx, list_sy, list_azimuths
        ):
            sx_arr = np.asarray(sx_arr)
            sy_arr = np.asarray(sy_arr)
            azimuth_arr = np.asarray(azimuth_arr)
            valid = (
                np.isfinite(sx_arr)
                & np.isfinite(sy_arr)
                & np.isfinite(azimuth_arr)
            )
            if np.count_nonzero(valid) < 2:
                continue
            f_sx = np.asarray(sx_arr[valid], dtype=np.float32)
            f_sy = np.asarray(sy_arr[valid], dtype=np.float32)
            f_azimuths = np.asarray(azimuth_arr[valid], dtype=np.float32)
            points = [QPointF(float(x), float(y)) for x, y in zip(f_sx, f_sy)]
            points.append(QPointF(float(f_sx[-1]), float(bottom_y)))
            points.append(QPointF(float(f_sx[0]), float(bottom_y)))
            entries.append((QPolygonF(points), f_sx, f_azimuths))
        self._profile_category_hit_cache[cache_key] = tuple(entries)
