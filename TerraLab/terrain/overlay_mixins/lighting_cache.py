"""Terrain lighting grids and byte-bounded material caches."""

from __future__ import annotations

import math

import numpy as np

from TerraLab.terrain.land_cover.visual_styles import VIBRANT_PALETTE_VERSION
from TerraLab.terrain.render.config import (
    SurfaceVisualStyle,
    TerrainCelestialLightContext,
    normalize_surface_visual_style,
)
from TerraLab.terrain.render.lighting import terrain_celestial_light_factors
from TerraLab.terrain.render.overlay_types import (
    TerrainLightingGrid,
    TerrainMaterialSamples,
)
from TerraLab.terrain.render.palette import (
    _clamp01,
    _distance_haze_factors,
    _palette_color,
    _sample_cache_value,
)


class OverlayLightingCacheMixin:
    def _terrain_light_components(
        self,
        normal_x,
        normal_y,
        normal_z,
        distance_m,
        sun_vec=None,
        sun_alt=None,
        terrain_shading_enabled=True,
        sun_visibility=None,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> TerrainLightingGrid:
        nx, ny, nz = np.broadcast_arrays(
            np.asarray(normal_x, dtype=np.float32),
            np.asarray(normal_y, dtype=np.float32),
            np.asarray(normal_z, dtype=np.float32),
        )
        settings = self.render_settings
        if light_context is None:
            derived_azimuth = None
            if sun_vec is not None:
                candidate = np.asarray(sun_vec, dtype=np.float32)
                if candidate.shape == (3,) and np.all(np.isfinite(candidate)):
                    derived_azimuth = math.degrees(
                        math.atan2(float(candidate[0]), float(candidate[1]))
                    ) % 360.0
            light_context = self._resolve_light_context(
                None,
                sun_alt=sun_alt,
                sun_az=derived_azimuth,
            )
        else:
            light_context = light_context.validated()
        factors = terrain_celestial_light_factors(light_context, settings)
        zeros = np.zeros(nx.shape, dtype=np.float32)
        if not terrain_shading_enabled or not settings.terrain_lighting_enabled:
            return TerrainLightingGrid(
                np.ones(nx.shape, dtype=np.float32),
                zeros,
                zeros,
                factors,
            )

        norm = np.sqrt(nx * nx + ny * ny + nz * nz)
        safe = np.isfinite(norm) & (norm > 1e-6)
        nx = np.where(safe, nx / np.where(safe, norm, 1.0), 0.0)
        ny = np.where(safe, ny / np.where(safe, norm, 1.0), 0.0)
        nz = np.where(safe, nz / np.where(safe, norm, 1.0), 1.0)

        if sun_vec is None or not np.all(np.isfinite(sun_vec)):
            sun_vec = self._sun_vector_enu(
                light_context.sun_altitude_deg,
                light_context.sun_azimuth_deg,
            )
        sun_vec = np.asarray(sun_vec, dtype=np.float32)
        solar_lambert = np.clip(
            nx * sun_vec[0] + ny * sun_vec[1] + nz * sun_vec[2],
            0.0,
            1.0,
        )
        if sun_visibility is not None:
            direct_visibility = np.broadcast_to(
                np.asarray(sun_visibility, dtype=np.float32), nx.shape
            )
            solar_lambert *= np.clip(direct_visibility, 0.0, 1.0)
        vibrant = (
            normalize_surface_visual_style(settings.surface_visual_style)
            == SurfaceVisualStyle.VIBRANT.value
        )
        if vibrant:
            solar_lambert = np.power(
                solar_lambert,
                float(settings.vibrant_sunlight_exponent),
            )
            day_ambient = float(
                settings.vibrant_sun_ambient_strength
            )
            day_diffuse = (
                float(settings.terrain_diffuse_strength)
                * float(settings.vibrant_sun_diffuse_boost)
            )
        else:
            day_ambient = float(settings.terrain_ambient_strength)
            day_diffuse = float(settings.terrain_diffuse_strength)

        lunar_lambert = zeros
        if (
            factors.lunar_strength > 0.0
            and light_context.moon_altitude_deg is not None
            and light_context.moon_azimuth_deg is not None
        ):
            moon_vec = self._sun_vector_enu(
                light_context.moon_altitude_deg,
                light_context.moon_azimuth_deg,
            )
            lunar_lambert = np.clip(
                nx * moon_vec[0] + ny * moon_vec[1] + nz * moon_vec[2],
                0.0,
                1.0,
            )

        # Distance only removes directional contrast.  Pulling total brightness
        # towards 1.0 here would incorrectly turn night terrain back on.
        haze = _distance_haze_factors(distance_m)
        directional_contrast = np.maximum(0.18, 1.0 - 0.78 * haze)
        solar_exposure = (
            solar_lambert
            * float(factors.solar_direct)
            * directional_contrast
        )
        lunar_exposure = (
            lunar_lambert
            * float(factors.lunar_strength)
            * directional_contrast
        )
        ambient = (
            float(settings.terrain_night_ambient_strength)
            + (
                day_ambient
                - float(settings.terrain_night_ambient_strength)
            )
            * float(factors.solar_ambient)
            + float(settings.terrain_moon_ambient_strength)
            * float(factors.lunar_strength)
        )
        intensity = (
            ambient
            + day_diffuse * solar_exposure
            + float(settings.terrain_moon_diffuse_strength)
            * lunar_exposure
        )
        minimum_brightness, maximum_brightness = self._terrain_light_bounds(
            factors
        )
        intensity = np.clip(
            intensity,
            minimum_brightness,
            maximum_brightness,
        ).astype(np.float32)
        return TerrainLightingGrid(
            intensity,
            np.asarray(solar_exposure, dtype=np.float32),
            np.asarray(lunar_exposure, dtype=np.float32),
            factors,
        )

    def _terrain_light_factor(
        self,
        normal_x,
        normal_y,
        normal_z,
        distance_m,
        sun_vec,
        sun_alt,
        terrain_shading_enabled=True,
        sun_visibility=None,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        """Compatibility brightness view over the celestial light components."""

        return self._terrain_light_components(
            normal_x,
            normal_y,
            normal_z,
            distance_m,
            sun_vec,
            sun_alt,
            terrain_shading_enabled=terrain_shading_enabled,
            sun_visibility=sun_visibility,
            light_context=light_context,
        ).intensity

    @staticmethod
    def _smooth_light_grid(
        light_grid, valid_mask, min_value=0.84, max_value=1.10
    ):
        values = np.asarray(light_grid, dtype=np.float32)
        valid = np.asarray(valid_mask, dtype=bool)
        if values.ndim != 2 or valid.shape != values.shape:
            return values

        source = np.where(valid, values, 1.0).astype(np.float32)
        weights = valid.astype(np.float32)
        source_pad = np.pad(source, ((1, 1), (1, 1)), mode="edge")
        weight_pad = np.pad(weights, ((1, 1), (1, 1)), mode="edge")
        kernel = (
            (1.0, 2.0, 1.0),
            (2.0, 4.0, 2.0),
            (1.0, 2.0, 1.0),
        )

        acc = np.zeros_like(source, dtype=np.float32)
        weight_sum = np.zeros_like(source, dtype=np.float32)
        for row in range(3):
            for col in range(3):
                weight = kernel[row][col]
                sample_weight = weight_pad[
                    row : row + values.shape[0], col : col + values.shape[1]
                ] * weight
                acc += (
                    source_pad[
                        row : row + values.shape[0],
                        col : col + values.shape[1],
                    ]
                    * sample_weight
                )
                weight_sum += sample_weight

        smoothed = np.divide(
            acc,
            np.maximum(weight_sum, 1e-6),
            out=np.ones_like(values, dtype=np.float32),
            where=weight_sum > 1e-6,
        )
        return np.where(
            valid,
            np.clip(smoothed, float(min_value), float(max_value)),
            values,
        ).astype(np.float32)

    def _profile_surface_samples(self, band, azimuths):
        cache = self._visible_surface_cache()
        rgba = _sample_cache_value(cache, "profile_rgba")
        valid = _sample_cache_value(cache, "profile_valid")
        if rgba is None or valid is None:
            shape = np.asarray(azimuths).shape
            return np.zeros(shape + (4,), dtype=np.uint8), np.zeros(shape, dtype=bool)
        rgba = np.asarray(rgba, dtype=np.uint8)
        valid = np.asarray(valid, dtype=bool)
        loaded = _sample_cache_value(cache, "profile_loaded")
        sources = _sample_cache_value(cache, "profile_source_indices")
        band_indices = np.asarray(
            _sample_cache_value(cache, "profile_band_indices", np.arange(rgba.shape[0])),
            dtype=np.int32,
        )
        target_band = int(getattr(band, "band_index", 0))
        matches = np.flatnonzero(band_indices == target_band)
        sampled_row = int(matches[0]) if matches.size else min(target_band, rgba.shape[0] - 1)
        original_azimuths = np.asarray(
            getattr(getattr(self, "profile", None), "azimuths", []), dtype=np.float64
        )
        sampled_indices = np.asarray(
            _sample_cache_value(cache, "profile_azimuth_indices", np.arange(rgba.shape[1])),
            dtype=np.int32,
        )
        sampled_angles = original_azimuths[sampled_indices]
        requested = np.asarray(azimuths, dtype=np.float64)
        distance = np.abs(
            ((requested[..., None] - sampled_angles[None, ...] + 180.0) % 360.0) - 180.0
        )
        nearest = np.argmin(distance, axis=-1)
        result_rgba = rgba[sampled_row, nearest]
        result_valid = valid[sampled_row, nearest]
        if loaded is not None:
            result_valid &= np.asarray(loaded, dtype=bool)[sampled_row, nearest]
        if _sample_cache_value(cache, "completion_state", "complete") == "visible_partial":
            result_valid &= np.isin(
                np.mod(np.rint(requested * 1_000_000.0).astype(np.int64), 360_000_000),
                np.mod(
                    np.rint(sampled_angles * 1_000_000.0).astype(np.int64),
                    360_000_000,
                ),
            )
        if sources is not None:
            result_valid = result_valid & (np.asarray(sources)[sampled_row, nearest] >= 0)
        return result_rgba, result_valid

    def _relief_surface_materials(
        self, row_indices, column_indices, mesh_shape
    ) -> TerrainMaterialSamples:
        cache = self._visible_surface_cache()
        visual_rgba = _sample_cache_value(cache, "visual_rgba")
        visual_valid = _sample_cache_value(cache, "visual_valid")
        visual_sources = _sample_cache_value(cache, "visual_source_indices")
        visual_classes = _sample_cache_value(cache, "visual_class_ids")
        visual_categorical = _sample_cache_value(cache, "visual_categorical")
        if (
            visual_rgba is not None
            and visual_valid is not None
            and visual_sources is not None
            and visual_classes is not None
            and visual_categorical is not None
            and np.shape(visual_rgba) == tuple(mesh_shape) + (4,)
            and np.shape(visual_valid) == tuple(mesh_shape)
            and np.shape(visual_sources) == tuple(mesh_shape)
            and np.shape(visual_classes) == tuple(mesh_shape)
            and np.shape(visual_categorical) == tuple(mesh_shape)
        ):
            rows = np.asarray(row_indices, dtype=np.int32)
            columns = np.asarray(column_indices, dtype=np.int32) % max(
                1, int(mesh_shape[1])
            )
            return TerrainMaterialSamples(
                np.asarray(visual_rgba, dtype=np.uint8)[rows, columns],
                np.asarray(visual_valid, dtype=bool)[rows, columns],
                np.asarray(visual_classes, dtype=np.int64)[rows, columns],
                np.asarray(visual_categorical, dtype=bool)[rows, columns],
                np.asarray(visual_sources, dtype=np.int16)[rows, columns],
            )
        rgba = _sample_cache_value(cache, "relief_rgba")
        valid = _sample_cache_value(cache, "relief_valid")
        sources = _sample_cache_value(cache, "relief_source_indices")
        classes = _sample_cache_value(cache, "relief_class_ids")
        categorical = _sample_cache_value(cache, "relief_categorical")
        rows = np.asarray(row_indices, dtype=np.int32)
        columns = np.asarray(column_indices, dtype=np.int32) % max(1, int(mesh_shape[1]))
        if rgba is None or valid is None:
            return TerrainMaterialSamples(
                np.zeros(rows.shape + (4,), dtype=np.uint8),
                np.zeros(rows.shape, dtype=bool),
                np.full(rows.shape, -1, dtype=np.int64),
                np.zeros(rows.shape, dtype=bool),
                np.full(rows.shape, -1, dtype=np.int16),
            )
        rgba = np.asarray(rgba, dtype=np.uint8)
        valid = np.asarray(valid, dtype=bool)
        sources = (
            np.asarray(sources, dtype=np.int16)
            if sources is not None
            else np.full(valid.shape, -1, dtype=np.int16)
        )
        classes = (
            np.asarray(classes, dtype=np.int64)
            if classes is not None
            else np.full(valid.shape, -1, dtype=np.int64)
        )
        categorical = (
            np.asarray(categorical, dtype=bool)
            if categorical is not None
            else np.zeros(valid.shape, dtype=bool)
        )
        if not (
            rgba.shape == valid.shape + (4,)
            and sources.shape == valid.shape
            and classes.shape == valid.shape
            and categorical.shape == valid.shape
        ):
            return TerrainMaterialSamples(
                np.zeros(rows.shape + (4,), dtype=np.uint8),
                np.zeros(rows.shape, dtype=bool),
                np.full(rows.shape, -1, dtype=np.int64),
                np.zeros(rows.shape, dtype=bool),
                np.full(rows.shape, -1, dtype=np.int16),
            )
        loaded = _sample_cache_value(cache, "relief_loaded")
        sampled_rows = np.asarray(
            _sample_cache_value(cache, "relief_distance_indices", np.arange(rgba.shape[0])),
            dtype=np.int32,
        )
        sampled_columns = np.asarray(
            _sample_cache_value(cache, "relief_azimuth_indices", np.arange(rgba.shape[1])),
            dtype=np.int32,
        ) % max(1, int(mesh_shape[1]))
        nearest_rows = np.argmin(np.abs(rows[..., None] - sampled_rows), axis=-1)
        circular = np.abs(columns[..., None] - sampled_columns)
        circular = np.minimum(circular, int(mesh_shape[1]) - circular)
        nearest_columns = np.argmin(circular, axis=-1)
        result_rgba = rgba[nearest_rows, nearest_columns]
        result_valid = valid[nearest_rows, nearest_columns]
        if loaded is not None:
            result_valid &= np.asarray(loaded, dtype=bool)[
                nearest_rows, nearest_columns
            ]
        if _sample_cache_value(cache, "completion_state", "complete") == "visible_partial":
            result_valid &= np.isin(rows, sampled_rows) & np.isin(
                columns, sampled_columns
            )
        result_sources = sources[nearest_rows, nearest_columns]
        result_classes = classes[nearest_rows, nearest_columns]
        result_categorical = categorical[nearest_rows, nearest_columns]
        result_valid &= result_sources >= 0
        return TerrainMaterialSamples(
            result_rgba,
            result_valid,
            result_classes,
            result_categorical,
            result_sources,
        )

    def _relief_surface_samples(self, row_indices, column_indices, mesh_shape):
        """Compatibility colour view over the complete material samples."""

        materials = self._relief_surface_materials(
            row_indices, column_indices, mesh_shape
        )
        return materials.base_rgba, materials.valid

    def _terrain_vertex_materials(self, asset, t_night) -> TerrainMaterialSamples:
        """Return aligned material identity and base colour for every vertex."""

        del t_night
        shape = asset.elevations.shape
        cache = self._visible_surface_cache()
        visual_rgba = _sample_cache_value(cache, "visual_rgba")
        visual_valid = _sample_cache_value(cache, "visual_valid")
        visual_loaded = _sample_cache_value(cache, "visual_loaded")
        visual_sources = _sample_cache_value(
            cache, "visual_source_indices"
        )
        visual_classes = _sample_cache_value(cache, "visual_class_ids")
        visual_categorical = _sample_cache_value(
            cache, "visual_categorical"
        )
        if (
            np.shape(visual_rgba) == shape + (4,)
            and np.shape(visual_valid) == shape
            and np.shape(visual_sources) == shape
            and np.shape(visual_classes) == shape
            and np.shape(visual_categorical) == shape
            and np.all(np.asarray(visual_valid, dtype=bool))
            and (
                visual_loaded is None
                or (
                    np.shape(visual_loaded) == shape
                    and np.all(np.asarray(visual_loaded, dtype=bool))
                )
            )
            and (
                not self.terrain_surface_opaque
                or np.all(
                    np.asarray(visual_rgba, dtype=np.uint8)[..., 3] == 255
                )
            )
        ):
            return TerrainMaterialSamples(
                np.asarray(visual_rgba, dtype=np.uint8),
                np.asarray(visual_valid, dtype=bool),
                np.asarray(visual_classes, dtype=np.int64),
                np.asarray(visual_categorical, dtype=bool),
                np.asarray(visual_sources, dtype=np.int16),
            )

        maximum = max(1.0, float(asset.distances[-1]))
        fallback = np.empty(shape + (4,), dtype=np.uint8)
        for row, distance in enumerate(asset.distances):
            palette_position = _clamp01(1.0 - float(distance) / maximum)
            _night_color, day_color = _palette_color(palette_position)
            fallback[row, :, :] = day_color.getRgb()

        rows, columns = np.indices(shape, dtype=np.int32)
        sampled = self._relief_surface_materials(
            rows, columns, shape
        )
        result = np.where(
            sampled.valid[..., None], sampled.base_rgba, fallback
        ).astype(np.uint8)
        if self.terrain_surface_opaque:
            result[..., 3] = 255
        return TerrainMaterialSamples(
            result,
            np.ones(shape, dtype=bool),
            np.where(sampled.valid, sampled.class_ids, -1),
            sampled.valid & sampled.categorical,
            np.where(sampled.valid, sampled.source_indices, -1),
        )

    @staticmethod
    def _surface_material_identity(surface_cache) -> tuple:
        """Return a stable source identity without retaining scientific arrays."""

        if surface_cache is None:
            return ("fallback",)
        completion_state = str(
            _sample_cache_value(
                surface_cache, "completion_state", "complete"
            )
        )
        cache_id = _sample_cache_value(surface_cache, "cache_id")
        if cache_id:
            return (
                "surface-cache",
                str(cache_id),
                completion_state,
            )
        key = _sample_cache_value(surface_cache, "key")
        digest = getattr(key, "digest", None)
        if digest:
            return ("surface-key", str(digest))
        return (
            "runtime-surface",
            id(surface_cache),
            completion_state,
        )

    def _static_material_settings_key(self) -> tuple:
        """Describe only settings that alter immutable terrain material."""

        settings = self.render_settings
        style = normalize_surface_visual_style(
            settings.surface_visual_style
        )
        if style != SurfaceVisualStyle.VIBRANT.value:
            return (
                "base-material-settings-v1",
                style,
                bool(self.terrain_surface_opaque),
            )
        return (
            "base-material-settings-v1",
            style,
            int(VIBRANT_PALETTE_VERSION),
            round(float(settings.vibrant_intensity), 6),
            round(
                float(settings.vibrant_territorial_luminance_variation), 6
            ),
            round(float(settings.vibrant_territorial_hue_variation), 6),
            round(float(settings.vibrant_material_midscale_variation), 6),
            round(float(settings.vibrant_material_microscale_variation), 6),
            round(float(settings.vibrant_material_altitude_influence), 6),
            round(float(settings.vibrant_material_slope_influence), 6),
            round(float(settings.vibrant_snow_rock_blend), 6),
            round(float(settings.vibrant_water_shore_variation), 6),
            bool(self.terrain_surface_opaque),
        )

    def _terrain_base_material_key(
        self, asset, surface_cache
    ) -> tuple:
        """Key a material by source, geometry/LOD and static style only."""

        return (
            "terrain-base-material-v1",
            self._surface_material_identity(surface_cache),
            int(asset.mesh_id),
            tuple(np.asarray(asset.elevations).shape),
            tuple(np.asarray(asset.near_patch_elevations).shape),
            tuple(
                _sample_cache_value(surface_cache, "source_legend_ids", ())
                or ()
            ),
            self._static_material_settings_key(),
        )

