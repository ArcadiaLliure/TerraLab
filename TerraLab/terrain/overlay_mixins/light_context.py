"""Celestial light context and immutable render-asset preparation."""

from __future__ import annotations

import math

import numpy as np

from TerraLab.terrain.mesh.normals import compute_polar_mesh_normals
from TerraLab.terrain.render.config import (
    SurfaceVisualStyle,
    TerrainCelestialLightContext,
    normalize_surface_visual_style,
)
from TerraLab.terrain.render.lighting import TerrainCelestialLightFactors
from TerraLab.terrain.render.overlay_types import (
    _TerrainRenderAsset,
)
from TerraLab.terrain.render.palette import EARTH_RADIUS_M, _sample_cache_value


class OverlayLightContextMixin:
    def _resolve_light_context(
        self,
        light_context: TerrainCelestialLightContext | None = None,
        *,
        sun_alt: float | None = None,
        sun_az: float | None = None,
    ) -> TerrainCelestialLightContext:
        """Resolve shared astronomy, retaining the fixed light as a fallback."""

        if isinstance(light_context, TerrainCelestialLightContext):
            try:
                valid_context_sun = bool(
                    np.isfinite(float(light_context.sun_altitude_deg))
                    and np.isfinite(float(light_context.sun_azimuth_deg))
                )
            except (TypeError, ValueError):
                valid_context_sun = False
            if valid_context_sun:
                return light_context.validated()
        settings = self.render_settings
        try:
            valid_legacy_sun = bool(
                sun_alt is not None
                and sun_az is not None
                and np.isfinite(float(sun_alt))
                and np.isfinite(float(sun_az))
            )
        except (TypeError, ValueError):
            valid_legacy_sun = False
        if valid_legacy_sun:
            return TerrainCelestialLightContext(
                float(sun_alt), float(sun_az)
            ).validated()
        return TerrainCelestialLightContext(
            settings.terrain_light_elevation_deg,
            settings.terrain_light_azimuth_deg,
        ).validated()

    def _configured_light(
        self,
        sun_alt: float | None = None,
        sun_az: float | None = None,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> tuple[float, float, np.ndarray | None]:
        """Compatibility view of the real astronomical sun in every style."""

        context = self._resolve_light_context(
            light_context, sun_alt=sun_alt, sun_az=sun_az
        )
        vector = (
            self._sun_vector_enu(
                context.sun_altitude_deg, context.sun_azimuth_deg
            )
            if self.render_settings.terrain_lighting_enabled
            else None
        )
        return (
            context.sun_altitude_deg,
            context.sun_azimuth_deg,
            vector,
        )

    def _terrain_light_bounds(
        self,
        factors: TerrainCelestialLightFactors | None = None,
    ) -> tuple[float, float]:
        settings = self.render_settings
        if (
            normalize_surface_visual_style(settings.surface_visual_style)
            == SurfaceVisualStyle.VIBRANT.value
        ):
            day_minimum = settings.vibrant_sun_min_brightness
            day_maximum = settings.vibrant_sun_max_brightness
        else:
            day_minimum = settings.terrain_min_brightness
            day_maximum = settings.terrain_max_brightness
        if factors is None:
            return day_minimum, day_maximum
        night_minimum = (
            float(settings.terrain_night_ambient_strength)
            + float(settings.terrain_moon_ambient_strength)
            * factors.lunar_strength
        )
        night_maximum = (
            night_minimum
            + float(settings.terrain_moon_diffuse_strength)
            * factors.lunar_strength
        )
        minimum = night_minimum + (
            day_minimum - night_minimum
        ) * factors.solar_ambient
        maximum = night_maximum + (
            day_maximum - night_maximum
        ) * factors.solar_ambient
        return max(0.0, minimum), max(minimum, maximum)

    def _terrain_lighting_settings_key(self) -> tuple:
        """Describe Lambert/celestial settings without material or camera state."""

        settings = self.render_settings
        return (
            "terrain-lighting-settings-v1",
            bool(settings.terrain_lighting_enabled),
            normalize_surface_visual_style(settings.surface_visual_style),
            round(float(settings.terrain_ambient_strength), 6),
            round(float(settings.terrain_diffuse_strength), 6),
            round(float(settings.terrain_min_brightness), 6),
            round(float(settings.terrain_max_brightness), 6),
            round(
                float(settings.terrain_twilight_dark_altitude_deg), 6
            ),
            round(float(settings.terrain_sun_full_altitude_deg), 6),
            round(float(settings.terrain_night_ambient_strength), 6),
            round(
                float(settings.terrain_moon_horizon_fade_start_deg), 6
            ),
            round(
                float(settings.terrain_moon_horizon_fade_end_deg), 6
            ),
            round(float(settings.terrain_moon_ambient_strength), 6),
            round(float(settings.terrain_moon_diffuse_strength), 6),
            round(float(settings.terrain_moon_phase_exponent), 6),
            round(float(settings.vibrant_sun_ambient_strength), 6),
            round(float(settings.vibrant_sun_diffuse_boost), 6),
            round(float(settings.vibrant_sunlight_exponent), 6),
            round(float(settings.vibrant_sun_min_brightness), 6),
            round(float(settings.vibrant_sun_max_brightness), 6),
        )

    def _terrain_lighting_key(
        self,
        asset,
        light_context: TerrainCelestialLightContext,
        *,
        lighting_enabled: bool,
    ) -> tuple:
        """Key lighting by geometry and quantized celestial state, never camera."""

        return (
            "terrain-lighting-v1",
            int(asset.mesh_id),
            bool(lighting_enabled),
            round(float(light_context.sun_altitude_deg) * 4.0) / 4.0,
            round(float(light_context.sun_azimuth_deg) * 4.0) / 4.0,
            (
                None
                if light_context.moon_altitude_deg is None
                else round(
                    float(light_context.moon_altitude_deg) * 4.0
                )
                / 4.0
            ),
            (
                None
                if light_context.moon_azimuth_deg is None
                else round(
                    float(light_context.moon_azimuth_deg) * 4.0
                )
                / 4.0
            ),
            round(float(light_context.moon_illumination) * 256.0) / 256.0,
            round(float(light_context.eclipse_factor) * 256.0) / 256.0,
            self._terrain_lighting_settings_key(),
        )

    def _maximum_terrain_distance_m(self) -> float | None:
        value = getattr(getattr(self, "profile", None), "resolved_radius_m", None)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if np.isfinite(value) and value > 0.0 else None

    @staticmethod
    def _sample_polar_elevations(
        elevations,
        valid,
        distances,
        azimuths,
        sample_x,
        sample_y,
    ):
        sample_x = np.asarray(sample_x, dtype=np.float32)
        sample_y = np.asarray(sample_y, dtype=np.float32)
        sample_distance = np.hypot(sample_x, sample_y).astype(np.float32)
        sample_azimuth = (
            np.degrees(np.arctan2(sample_x, sample_y)) % 360.0
        ).astype(np.float32)

        distance_hi = np.searchsorted(
            distances, sample_distance, side="right"
        )
        inside = (distance_hi > 0) & (distance_hi < len(distances))
        distance_hi = np.clip(distance_hi, 1, len(distances) - 1)
        distance_lo = distance_hi - 1
        distance_span = np.maximum(
            distances[distance_hi] - distances[distance_lo], 1e-6
        )
        distance_t = np.clip(
            (sample_distance - distances[distance_lo]) / distance_span,
            0.0,
            1.0,
        )

        az_diffs = np.diff(azimuths.astype(np.float32))
        az_diffs = az_diffs[np.isfinite(az_diffs) & (az_diffs > 0.0)]
        az_step = float(np.median(az_diffs)) if az_diffs.size else 360.0
        az_position = ((sample_azimuth - float(azimuths[0])) % 360.0) / max(
            az_step, 1e-6
        )
        az_lo_float = np.floor(az_position)
        az_t = (az_position - az_lo_float).astype(np.float32)
        az_lo = az_lo_float.astype(np.int32) % len(azimuths)
        az_hi = (az_lo + 1) % len(azimuths)

        w00 = (1.0 - distance_t) * (1.0 - az_t)
        w01 = (1.0 - distance_t) * az_t
        w10 = distance_t * (1.0 - az_t)
        w11 = distance_t * az_t
        samples = (
            (distance_lo, az_lo, w00),
            (distance_lo, az_hi, w01),
            (distance_hi, az_lo, w10),
            (distance_hi, az_hi, w11),
        )

        weighted_height = np.zeros(sample_distance.shape, dtype=np.float32)
        weight_sum = np.zeros(sample_distance.shape, dtype=np.float32)
        for distance_idx, azimuth_idx, weight in samples:
            corner_valid = valid[distance_idx, azimuth_idx]
            corner_weight = np.where(corner_valid, weight, 0.0).astype(
                np.float32
            )
            weighted_height += (
                elevations[distance_idx, azimuth_idx] * corner_weight
            )
            weight_sum += corner_weight

        sampled = np.divide(
            weighted_height,
            np.maximum(weight_sum, 1e-6),
            out=np.zeros_like(weighted_height),
            where=weight_sum > 1e-6,
        )
        sampled_valid = inside & (weight_sum >= 0.50)
        return sampled, sample_distance, sampled_valid

    def _prepare_terrain_render_asset(self, mesh):
        if not isinstance(mesh, dict):
            return None
        try:
            azimuths = np.asarray(mesh.get("azimuths"), dtype=np.float32)
            distances = np.asarray(mesh.get("distances"), dtype=np.float32)
            altitudes = np.asarray(mesh.get("altitudes"), dtype=np.float32)
            elevations = np.asarray(mesh.get("elevations"), dtype=np.float32)
            valid = np.asarray(mesh.get("valid"), dtype=bool)
            visible = np.asarray(mesh.get("visible"), dtype=bool)
        except Exception:
            return None
        shape = (distances.size, azimuths.size)
        if azimuths.size < 2 or distances.size < 2 or altitudes.shape != shape:
            return None
        if elevations.shape != shape:
            elevations = np.zeros(shape, dtype=np.float32)
        if valid.shape != shape:
            valid = np.isfinite(altitudes)
        if visible.shape != shape:
            visible = valid.copy()
        surface_cache = self._visible_surface_cache()
        visual_distances = _sample_cache_value(surface_cache, "visual_distances")
        visual_azimuths = _sample_cache_value(surface_cache, "visual_azimuths")
        visual_altitudes = _sample_cache_value(surface_cache, "visual_altitudes")
        visual_elevations = _sample_cache_value(surface_cache, "visual_elevations")
        visual_valid = _sample_cache_value(surface_cache, "visual_valid")
        visual_visible = _sample_cache_value(surface_cache, "visual_visible")
        visual_shape = (
            len(visual_distances) if visual_distances is not None else 0,
            len(visual_azimuths) if visual_azimuths is not None else 0,
        )
        if (
            _sample_cache_value(surface_cache, "completion_state", "complete")
            == "complete"
            and
            visual_shape[0] >= 2
            and visual_shape[1] >= 2
            and np.shape(visual_altitudes) == visual_shape
            and np.shape(visual_elevations) == visual_shape
            and np.shape(visual_valid) == visual_shape
        ):
            # Subdivide only the visual mesh. Heights/angles were interpolated
            # from the DEM in the worker, so geometry gains no fictitious
            # topographic information while a finer land-cover raster retains
            # detail between original DEM vertices.
            azimuths = np.asarray(visual_azimuths, dtype=np.float32)
            distances = np.asarray(visual_distances, dtype=np.float32)
            altitudes = np.asarray(visual_altitudes, dtype=np.float32)
            elevations = np.asarray(visual_elevations, dtype=np.float32)
            valid = np.asarray(visual_valid, dtype=bool)
            visible = (
                np.asarray(visual_visible, dtype=bool)
                if np.shape(visual_visible) == visual_shape
                else valid.copy()
            )
            shape = visual_shape
        computed = compute_polar_mesh_normals(
            elevations, valid, distances, azimuths
        )
        saved = tuple(
            np.asarray(mesh.get(name), dtype=np.float32)
            for name in ("normal_x", "normal_y", "normal_z")
        )
        if all(item.shape == shape for item in saved):
            saved_norm = np.sqrt(saved[0] ** 2 + saved[1] ** 2 + saved[2] ** 2)
            use_saved = visible & np.isfinite(saved_norm) & (saved_norm > 1e-5)
            normals = tuple(
                np.where(use_saved, saved[index], computed[index]).astype(np.float32)
                for index in range(3)
            )
        else:
            normals = tuple(np.asarray(item, dtype=np.float32) for item in computed)
        azimuths_closed = np.concatenate(
            [azimuths, [azimuths[0] + 360.0]]
        ).astype(np.float32)
        patch_eastings = np.asarray(
            mesh.get("near_patch_eastings", ()), dtype=np.float32
        )
        patch_northings = np.asarray(
            mesh.get("near_patch_northings", ()), dtype=np.float32
        )
        patch_shape = (patch_northings.size, patch_eastings.size)
        patch_altitudes = np.asarray(
            mesh.get("near_patch_altitudes", ()), dtype=np.float32
        )
        patch_elevations = np.asarray(
            mesh.get("near_patch_elevations", ()), dtype=np.float32
        )
        patch_valid = np.asarray(mesh.get("near_patch_valid", ()), dtype=bool)
        patch_normals = tuple(
            np.asarray(mesh.get(name, ()), dtype=np.float32)
            for name in (
                "near_patch_normal_x",
                "near_patch_normal_y",
                "near_patch_normal_z",
            )
        )
        if (
            patch_shape[0] < 2
            or patch_shape[1] < 2
            or patch_altitudes.shape != patch_shape
            or patch_elevations.shape != patch_shape
            or patch_valid.shape != patch_shape
        ):
            patch_eastings = np.empty(0, dtype=np.float32)
            patch_northings = np.empty(0, dtype=np.float32)
            patch_altitudes = np.empty((0, 0), dtype=np.float32)
            patch_elevations = np.empty((0, 0), dtype=np.float32)
            patch_valid = np.empty((0, 0), dtype=bool)
            patch_normals = (
                np.empty((0, 0), dtype=np.float32),
                np.empty((0, 0), dtype=np.float32),
                np.empty((0, 0), dtype=np.float32),
            )
        elif not all(value.shape == patch_shape for value in patch_normals):
            filled = np.where(patch_valid, patch_elevations, 0.0).astype(np.float64)
            gradient_north, gradient_east = np.gradient(
                filled,
                patch_northings.astype(np.float64),
                patch_eastings.astype(np.float64),
                edge_order=1,
            )
            patch_nx = -gradient_east
            patch_ny = -gradient_north
            patch_nz = np.ones(patch_shape, dtype=np.float64)
            patch_norm = np.sqrt(
                patch_nx * patch_nx + patch_ny * patch_ny + patch_nz * patch_nz
            )
            patch_normals = tuple(
                np.where(patch_valid, value / np.maximum(patch_norm, 1e-12), fallback).astype(np.float32)
                for value, fallback in ((patch_nx, 0.0), (patch_ny, 0.0), (patch_nz, 1.0))
            )
        return _TerrainRenderAsset(
            mesh_id=hash((id(mesh), id(visual_altitudes)))
            if visual_shape == shape and visual_shape[0] >= 2
            else id(mesh),
            azimuths=azimuths,
            azimuths_closed=azimuths_closed,
            distances=distances,
            altitudes=altitudes,
            altitudes_closed=np.concatenate([altitudes, altitudes[:, :1]], axis=1),
            elevations=elevations,
            valid=valid,
            valid_closed=np.concatenate([valid, valid[:, :1]], axis=1),
            visible=visible,
            visible_closed=np.concatenate([visible, visible[:, :1]], axis=1),
            normal_x=normals[0],
            normal_y=normals[1],
            normal_z=normals[2],
            near_patch_eastings=patch_eastings,
            near_patch_northings=patch_northings,
            near_patch_altitudes=patch_altitudes,
            near_patch_elevations=patch_elevations,
            near_patch_valid=patch_valid,
            near_patch_normal_x=patch_normals[0],
            near_patch_normal_y=patch_normals[1],
            near_patch_normal_z=patch_normals[2],
        )

    def _terrain_surface_normals(
        self, mesh, elevations, valid, distances, azimuths
    ):
        cache_key = (id(mesh), elevations.shape)
        if (
            cache_key == self._terrain_normal_cache_key
            and self._terrain_normal_cache is not None
        ):
            return self._terrain_normal_cache

        normals = compute_polar_mesh_normals(
            elevations, valid, distances, azimuths
        )
        self._terrain_normal_cache_key = cache_key
        self._terrain_normal_cache = normals
        return normals

    def _terrain_sun_visibility(
        self,
        mesh,
        elevations,
        valid,
        visible,
        distances,
        azimuths,
        sun_alt,
        sun_az,
        terrain_shading_enabled=True,
    ):
        shape = elevations.shape
        if (
            not terrain_shading_enabled
            or sun_alt is None
            or sun_az is None
            or float(sun_alt) <= -0.5
        ):
            return np.ones(shape, dtype=np.float32)

        cache_key = (
            id(mesh),
            shape,
            round(float(sun_alt) * 4.0) / 4.0,
            round(float(sun_az) * 4.0) / 4.0,
        )
        if (
            cache_key == self._terrain_shadow_cache_key
            and self._terrain_shadow_cache is not None
            and self._terrain_shadow_cache.shape == shape
        ):
            return self._terrain_shadow_cache

        result = np.ones(shape, dtype=np.float32)
        active = valid & visible & np.isfinite(elevations)
        active_rows, active_cols = np.nonzero(active)
        if active_rows.size == 0:
            self._terrain_shadow_cache_key = cache_key
            self._terrain_shadow_cache = result
            return result

        target_distance = distances[active_rows].astype(np.float32)
        target_azimuth = np.deg2rad(
            azimuths[active_cols].astype(np.float32)
        )
        target_x = target_distance * np.sin(target_azimuth)
        target_y = target_distance * np.cos(target_azimuth)
        target_z = elevations[active_rows, active_cols].astype(np.float32)
        target_z -= (target_distance * target_distance) / (
            2.0 * EARTH_RADIUS_M
        )

        sun_az_rad = math.radians(float(sun_az))
        sun_dx = math.sin(sun_az_rad)
        sun_dy = math.cos(sun_az_rad)
        sun_slope = math.tan(math.radians(max(0.15, float(sun_alt))))

        near_steps = np.diff(distances[: min(len(distances), 32)])
        near_steps = near_steps[np.isfinite(near_steps) & (near_steps > 0.0)]
        ray_start = max(
            20.0,
            min(80.0, float(np.median(near_steps)) * 2.0)
            if near_steps.size
            else 40.0,
        )
        ray_limit = max(ray_start * 2.0, float(distances[-1]) * 1.35)
        ray_offsets = np.geomspace(ray_start, ray_limit, 38).astype(
            np.float32
        )
        max_clearance = np.full(active_rows.shape, -np.inf, dtype=np.float32)

        for ray_offset in ray_offsets:
            sample_x = target_x + ray_offset * sun_dx
            sample_y = target_y + ray_offset * sun_dy
            sampled_elevation, sample_distance, sampled_valid = (
                self._sample_polar_elevations(
                    elevations,
                    valid,
                    distances,
                    azimuths,
                    sample_x,
                    sample_y,
                )
            )
            sampled_z = sampled_elevation - (
                sample_distance * sample_distance
            ) / (2.0 * EARTH_RADIUS_M)
            ray_z = target_z + ray_offset * sun_slope
            self_bias = 2.0 + ray_offset * 0.00015
            clearance = sampled_z - ray_z - self_bias
            max_clearance = np.where(
                sampled_valid,
                np.maximum(max_clearance, clearance),
                max_clearance,
            )

        penumbra = np.clip((max_clearance + 2.0) / 14.0, 0.0, 1.0)
        penumbra = penumbra * penumbra * (3.0 - 2.0 * penumbra)
        result[active_rows, active_cols] = 1.0 - penumbra.astype(np.float32)
        result = self._smooth_light_grid(
            result, active, min_value=0.0, max_value=1.0
        )
        result = np.where(active, np.clip(result, 0.0, 1.0), 1.0).astype(
            np.float32
        )

        self._terrain_shadow_cache_key = cache_key
        self._terrain_shadow_cache = result
        return result

