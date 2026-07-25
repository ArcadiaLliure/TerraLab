"""Overlay lifecycle, profile ownership, and category cache access."""

from __future__ import annotations

import os
import time

import numpy as np
from PyQt5.QtCore import QPointF, Qt

from TerraLab.common.cache import ByteLRU
from TerraLab.common.performance.budget import DEFAULT_PERFORMANCE_BUDGET
from TerraLab.common.performance.flags import PERFORMANCE_FLAGS
from TerraLab.config import ConfigManager
from TerraLab.terrain.land_cover.legends.category_info import (
    LandCoverCategoryInfo,
    category_info,
)
from TerraLab.terrain.persistence.profile_npz import load_profile
from TerraLab.terrain.render.config import normalize_surface_visual_style
from TerraLab.terrain.render.palette import LAYER_DEFS, _BandPoints, _sample_cache_value


class OverlayProfileCacheMixin:
    def __init__(
        self,
        parent=None,
        horizon_profile_path=None,
        vert_exaggeration=1.0,
        allow_procedural_fallback=True,
        terrain_surface_opaque=True,
    ):
        super().__init__(parent)
        self.vert_exaggeration = vert_exaggeration
        self.allow_procedural_fallback = bool(allow_procedural_fallback)
        self.terrain_surface_opaque = bool(terrain_surface_opaque)
        self._surface_visible = True
        self._layers = (
            []
        )  # list of (_BandPoints, night_col, day_col)
        self.profile = None  # Store reference to the current profile
        self._loaded = False
        # Retained as a private compatibility attribute for older diagnostics;
        # it is no longer a hard cap that can truncate visible terrain.
        self._max_terrain_surface_quads = 4500
        self._last_surface2d_quads = 0
        self._last_surface2d_vertices = 0
        self._last_surface2d_max_error_px = 0.0
        self._last_surface2d_geometry_s = 0.0
        self._last_surface2d_paint_s = 0.0
        self._last_terrain_color_s = 0.0
        self._last_terrain_raster_s = 0.0
        self._last_horizon_antialias_s = 0.0
        self._last_terrain_total_s = 0.0
        self._last_material_resolution_s = 0.0
        self._last_base_material_cache_hit = False
        self._last_lighting_cache_hit = False
        self._last_resolved_material_cache_hit = False
        self._last_raster_cache_hit = False
        self._last_frame_cache_hit = False
        self._terrain_base_material_builds = 0
        self._terrain_lighting_builds = 0
        self._terrain_resolved_material_builds = 0
        self._terrain_raster_builds = 0
        self._terrain_frame_cache_hits = 0
        self._terrain_frame_cache_misses = 0
        self._terrain_material_image = None
        self._terrain_surface_diagnostics = {}
        self._terrain_resolved_materials = None
        self._terrain_geometry_cache_key = None
        self._terrain_geometry_cache = None
        self._terrain_polygon_cache_key = None
        self._terrain_polygon_cache = None
        self._terrain_polygon_cache_geometry = None
        self._profile_polygon_cache_view_key = None
        self._profile_polygon_cache = {}
        self._profile_category_hit_cache = {}
        self._profile_image_cache_key = None
        self._profile_image_cache = None
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        self._terrain_material_image = None
        self._terrain_surface_diagnostics = {}
        self._terrain_resolved_materials = None
        self._terrain_surface_image_drawn = 0
        self._terrain_raster_cache_key = None
        self._terrain_raster_cache = None
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._terrain_normal_cache_key = None
        self._terrain_normal_cache = None
        self._terrain_render_asset = None
        self._terrain_shade_cache = ByteLRU(
            max(16 * 1024**2, DEFAULT_PERFORMANCE_BUDGET.transient_bytes // 4)
        )
        self._terrain_base_material_cache = ByteLRU(
            max(16 * 1024**2, DEFAULT_PERFORMANCE_BUDGET.transient_bytes // 6)
        )
        self._terrain_resolved_material_cache = ByteLRU(
            max(32 * 1024**2, DEFAULT_PERFORMANCE_BUDGET.transient_bytes // 4)
        )
        self.render_settings = ConfigManager().get_terrain_render_settings()

        if horizon_profile_path:
            print(
                f"[HorizonOverlay] Attempting to load profile from: {horizon_profile_path}"
            )
            if not os.path.exists(horizon_profile_path):
                print(
                    f"[HorizonOverlay] ERROR: Profile file not found at {horizon_profile_path}"
                )

            try:
                profile = load_profile(horizon_profile_path)
                if profile is not None:
                    self.profile = profile
                    if PERFORMANCE_FLAGS.relief_cached:
                        self._terrain_render_asset = self._prepare_terrain_render_asset(
                            getattr(profile, "terrain_mesh", None)
                        )
                    print(
                        "[HorizonOverlay] Profile loaded. Processing layers..."
                    )
                    for band_id, night_c, day_c in LAYER_DEFS:
                        bp = _BandPoints(profile, band_id, vert_exaggeration)
                        if bp.points[0] is not None:
                            self._layers.append((bp, night_c, day_c))
                        else:
                            print(
                                f"[HorizonOverlay]   Band '{band_id}': no data, skipped"
                            )
                    self._loaded = bool(self._layers)
                else:
                    print("[HorizonOverlay] load_profile returned None.")
            except Exception as e:
                print(f"[HorizonOverlay] Exception loading profile: {e}")

        if not self._layers and self.allow_procedural_fallback:
            print(
                "[HorizonOverlay] No real data loaded — activating procedural fallback."
            )
            self._build_procedural_fallback()

    def set_terrain_surface_opaque(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.terrain_surface_opaque:
            return
        self.terrain_surface_opaque = enabled
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        self.request_update.emit()

    def _set_surface_visible(
        self, enabled: bool, *, request_repaint: bool
    ) -> None:
        """Apply surface visibility and invalidate material-dependent state."""

        enabled = bool(enabled)
        if enabled == self._surface_visible:
            return
        self._surface_visible = enabled
        self._profile_image_cache_key = None
        self._profile_image_cache = None
        self._profile_category_hit_cache.clear()
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        self._terrain_resolved_materials = None
        self._terrain_raster_cache_key = None
        self._terrain_raster_cache = None
        self._terrain_render_asset = None
        self._terrain_base_material_cache.clear()
        self._terrain_resolved_material_cache.clear()
        if request_repaint:
            self.request_update.emit()

    def set_surface_visible(self, enabled: bool) -> None:
        """Show or hide sampled surface materials without discarding data."""

        self._set_surface_visible(enabled, request_repaint=True)

    def _visible_surface_cache(self):
        if not self._surface_visible:
            return None
        return getattr(getattr(self, "profile", None), "surface_samples", None)

    def reload_render_settings(self) -> None:
        """Reload terrain-only settings and invalidate colour-derived caches."""

        previous_static_key = self._static_material_settings_key()
        previous_lighting_key = self._terrain_lighting_settings_key()
        previous_resolved_key = (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            ),
            str(self.render_settings.terrain_shading_mode),
            round(
                float(
                    self.render_settings
                    .categorical_region_smoothing_radius_px
                ),
                6,
            ),
        )
        self.render_settings = ConfigManager().get_terrain_render_settings()
        next_static_key = self._static_material_settings_key()
        next_lighting_key = self._terrain_lighting_settings_key()
        next_resolved_key = (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            ),
            str(self.render_settings.terrain_shading_mode),
            round(
                float(
                    self.render_settings
                    .categorical_region_smoothing_radius_px
                ),
                6,
            ),
        )
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._profile_image_cache_key = None
        self._profile_image_cache = None
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        if previous_lighting_key != next_lighting_key:
            self._terrain_shade_cache.clear()
        if previous_static_key != next_static_key:
            self._terrain_base_material_cache.clear()
            self._terrain_resolved_material_cache.clear()
        elif previous_resolved_key != next_resolved_key:
            self._terrain_resolved_material_cache.clear()
        self.request_update.emit()

    def set_profile(self, profile, layer_defs=None):
        """Update the overlay with a new HorizonProfile object (e.g. from background worker).

        Args:
            profile: HorizonProfile with baked bands
            layer_defs: Optional list of (band_id, night_QColor, day_QColor).
                        Generated by overlay.generate_layer_defs(bands). If None, uses LAYER_DEFS.
        """
        if profile is None:
            return
        self.profile = profile
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._terrain_normal_cache_key = None
        self._terrain_normal_cache = None
        self._terrain_geometry_cache_key = None
        self._terrain_geometry_cache = None
        self._terrain_polygon_cache_key = None
        self._terrain_polygon_cache = None
        self._terrain_polygon_cache_geometry = None
        self._profile_polygon_cache_view_key = None
        self._profile_polygon_cache.clear()
        self._profile_category_hit_cache.clear()
        self._profile_image_cache_key = None
        self._profile_image_cache = None
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        self._terrain_raster_cache_key = None
        self._terrain_raster_cache = None
        self._terrain_render_asset = (
            self._prepare_terrain_render_asset(getattr(profile, "terrain_mesh", None))
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        self._terrain_shade_cache.clear()
        self._terrain_base_material_cache.clear()
        self._terrain_resolved_material_cache.clear()

        effective_defs = layer_defs if layer_defs is not None else LAYER_DEFS

        now_mono = float(time.monotonic())
        last_log = float(getattr(self, "_last_set_profile_log_mono", 0.0))
        if (now_mono - last_log) >= 2.0:
            print(
                f"[HorizonOverlay] Updating profile for {profile.observer_lat}, {profile.observer_lon} ({len(effective_defs)} layers)"
            )
            self._last_set_profile_log_mono = now_mono
        self._layers.clear()

        try:
            for band_id, night_c, day_c in effective_defs:
                bp = _BandPoints(profile, band_id, self.vert_exaggeration)
                if bp.points[0] is not None:
                    self._layers.append((bp, night_c, day_c))

            self._loaded = bool(self._layers)
            self.request_update.emit()

        except Exception as e:
            print(f"[HorizonOverlay] Error setting profile: {e}")

    def clear_profile(self):
        """Executa el metode clear_profile de la classe HorizonOverlay.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        self.profile = None
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._terrain_normal_cache_key = None
        self._terrain_normal_cache = None
        self._terrain_geometry_cache_key = None
        self._terrain_geometry_cache = None
        self._terrain_polygon_cache_key = None
        self._terrain_polygon_cache = None
        self._terrain_polygon_cache_geometry = None
        self._profile_polygon_cache_view_key = None
        self._profile_polygon_cache.clear()
        self._profile_category_hit_cache.clear()
        self._profile_image_cache_key = None
        self._profile_image_cache = None
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        self._terrain_render_asset = None
        self._terrain_shade_cache.clear()
        self._terrain_base_material_cache.clear()
        self._terrain_resolved_material_cache.clear()
        self._layers.clear()
        self._loaded = False
        if self.allow_procedural_fallback:
            self._build_procedural_fallback()
        self.request_update.emit()

    @staticmethod
    def _category_metadata(
        cache, class_id: int, source_index: int
    ) -> LandCoverCategoryInfo | None:
        if class_id < 0 or source_index < 0:
            return None
        source_ids = tuple(
            _sample_cache_value(cache, "source_ids", ()) or ()
        )
        source_names = tuple(
            _sample_cache_value(cache, "source_names", ()) or ()
        )
        legend_ids = tuple(
            _sample_cache_value(cache, "source_legend_ids", ()) or ()
        )
        if source_index >= len(source_ids):
            return None
        source_name = (
            source_names[source_index]
            if source_index < len(source_names)
            else source_ids[source_index]
        )
        legend_id = (
            legend_ids[source_index]
            if source_index < len(legend_ids)
            else ""
        )
        return category_info(
            legend_id,
            int(class_id),
            source_name=str(source_name or source_ids[source_index]),
            locale="ca",
        )

    @staticmethod
    def _category_grid_value(
        cache,
        prefix: str,
        row: int,
        column: int,
    ) -> tuple[int, int] | None:
        classes = _sample_cache_value(cache, f"{prefix}_class_ids")
        categorical = _sample_cache_value(
            cache, f"{prefix}_categorical"
        )
        sources = _sample_cache_value(
            cache, f"{prefix}_source_indices"
        )
        if classes is None or categorical is None or sources is None:
            return None
        class_grid = np.asarray(classes)
        categorical_grid = np.asarray(categorical, dtype=bool)
        source_grid = np.asarray(sources)
        if (
            class_grid.ndim != 2
            or categorical_grid.shape != class_grid.shape
            or source_grid.shape != class_grid.shape
        ):
            return None
        row = int(row)
        column = int(column) % max(1, int(class_grid.shape[1]))
        if not (0 <= row < class_grid.shape[0]):
            return None
        if not bool(categorical_grid[row, column]):
            return None
        class_id = int(class_grid[row, column])
        source_index = int(source_grid[row, column])
        if class_id < 0 or source_index < 0:
            return None
        return class_id, source_index

    def _relief_category_value(
        self,
        cache,
        row: int,
        column: int,
        mesh_shape: tuple[int, int],
    ) -> tuple[int, int] | None:
        visual_classes = _sample_cache_value(
            cache, "visual_class_ids"
        )
        visual_categorical = _sample_cache_value(
            cache, "visual_categorical"
        )
        visual_sources = _sample_cache_value(
            cache, "visual_source_indices"
        )
        if (
            visual_classes is not None
            and visual_categorical is not None
            and visual_sources is not None
            and np.shape(visual_classes) == tuple(mesh_shape)
            and np.shape(visual_categorical) == tuple(mesh_shape)
            and np.shape(visual_sources) == tuple(mesh_shape)
        ):
            return self._category_grid_value(
                cache, "visual", row, column
            )

        classes = _sample_cache_value(cache, "relief_class_ids")
        categorical = _sample_cache_value(
            cache, "relief_categorical"
        )
        sources = _sample_cache_value(
            cache, "relief_source_indices"
        )
        if classes is None or categorical is None or sources is None:
            return None
        classes = np.asarray(classes)
        categorical = np.asarray(categorical, dtype=bool)
        sources = np.asarray(sources)
        if (
            classes.ndim != 2
            or categorical.shape != classes.shape
            or sources.shape != classes.shape
            or not classes.size
        ):
            return None
        sampled_rows = np.asarray(
            _sample_cache_value(
                cache,
                "relief_distance_indices",
                np.arange(classes.shape[0]),
            ),
            dtype=np.int32,
        )
        sampled_columns = np.asarray(
            _sample_cache_value(
                cache,
                "relief_azimuth_indices",
                np.arange(classes.shape[1]),
            ),
            dtype=np.int32,
        ) % max(1, int(mesh_shape[1]))
        nearest_row = int(np.argmin(np.abs(sampled_rows - int(row))))
        circular = np.abs(
            sampled_columns - (int(column) % max(1, int(mesh_shape[1])))
        )
        circular = np.minimum(
            circular, max(1, int(mesh_shape[1])) - circular
        )
        nearest_column = int(np.argmin(circular))
        if not bool(categorical[nearest_row, nearest_column]):
            return None
        class_id = int(classes[nearest_row, nearest_column])
        source_index = int(sources[nearest_row, nearest_column])
        return (
            (class_id, source_index)
            if class_id >= 0 and source_index >= 0
            else None
        )

    def _profile_category_value(
        self, cache, band_id: str, azimuth_deg: float
    ) -> tuple[int, int] | None:
        classes = _sample_cache_value(cache, "profile_class_ids")
        categorical = _sample_cache_value(
            cache, "profile_categorical"
        )
        sources = _sample_cache_value(
            cache, "profile_source_indices"
        )
        if classes is None or categorical is None or sources is None:
            return None
        classes = np.asarray(classes)
        categorical = np.asarray(categorical, dtype=bool)
        sources = np.asarray(sources)
        if (
            classes.ndim != 2
            or categorical.shape != classes.shape
            or sources.shape != classes.shape
            or not classes.size
        ):
            return None
        profile = getattr(self, "profile", None)
        bands = tuple(getattr(profile, "bands", ()) or ())
        target_band = next(
            (
                index
                for index, band in enumerate(bands)
                if str(band.get("id", "")) == str(band_id)
            ),
            0,
        )
        sampled_bands = np.asarray(
            _sample_cache_value(
                cache,
                "profile_band_indices",
                np.arange(classes.shape[0]),
            ),
            dtype=np.int32,
        )
        matches = np.flatnonzero(sampled_bands == int(target_band))
        if not matches.size:
            return None
        sampled_row = int(matches[0])
        profile_azimuths = np.asarray(
            getattr(profile, "azimuths", ()), dtype=np.float64
        )
        sampled_indices = np.asarray(
            _sample_cache_value(
                cache,
                "profile_azimuth_indices",
                np.arange(classes.shape[1]),
            ),
            dtype=np.int32,
        )
        if (
            profile_azimuths.size == 0
            or sampled_indices.size != classes.shape[1]
            or np.any(sampled_indices < 0)
            or np.any(sampled_indices >= profile_azimuths.size)
        ):
            return None
        sampled_azimuths = profile_azimuths[sampled_indices]
        circular = np.abs(
            (
                (
                    sampled_azimuths
                    - (float(azimuth_deg) % 360.0)
                    + 180.0
                )
                % 360.0
            )
            - 180.0
        )
        column = int(np.argmin(circular))
        if not bool(categorical[sampled_row, column]):
            return None
        class_id = int(classes[sampled_row, column])
        source_index = int(sources[sampled_row, column])
        return (
            (class_id, source_index)
            if class_id >= 0 and source_index >= 0
            else None
        )

    def _profile_category_at_screen(
        self, cache, point_x: float, point_y: float
    ) -> LandCoverCategoryInfo | None:
        point = QPointF(float(point_x), float(point_y))
        # Profile bands are painted far-to-near. Reverse that order to resolve
        # the uppermost visible polygon at the cursor.
        for band_pts, _night, _day in reversed(self._layers):
            entries = self._profile_category_hit_cache.get(
                ("band", id(band_pts)), ()
            )
            for polygon, screen_x, azimuths in reversed(entries):
                if not polygon.containsPoint(point, Qt.OddEvenFill):
                    continue
                nearest = int(
                    np.argmin(
                        np.abs(
                            np.asarray(screen_x, dtype=np.float64)
                            - float(point_x)
                        )
                    )
                )
                value = self._profile_category_value(
                    cache,
                    str(getattr(band_pts, "band_id", "")),
                    float(np.asarray(azimuths)[nearest]),
                )
                if value is not None:
                    return self._category_metadata(
                        cache, value[0], value[1]
                    )
                return None
        return None

