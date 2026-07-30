"""Top-level terrain draw dispatch and frame mode selection."""

from __future__ import annotations

import math

import numpy as np
from PyQt5.QtGui import QBrush, QColor, QImage, QPainter, QPen

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.terrain.render.config import (
    SurfaceVisualStyle,
    TerrainCelestialLightContext,
    normalize_surface_visual_style,
)
from TerraLab.terrain.render.lighting import (
    TerrainCelestialLightFactors,
    terrain_celestial_light_factors,
)
from TerraLab.terrain.render.materials import (
    apply_vibrant_color_grade,
    compose_vertex_rgba,
)
from TerraLab.terrain.render.overlay_types import (
    TerrainLightingGrid,
)
from TerraLab.terrain.render.palette import (
    GROUND_DAY,
    _lerp_color,
    _qcolor_from_rgba,
    _surface_cache_has_categorical_material,
)


class OverlayDrawDispatchMixin:
    def draw(
        self,
        painter: QPainter,
        projection_fn,
        width: int,
        height: int,
        current_azimuth: float,
        zoom_level: float,
        elevation_angle: float,
        ut_hour: float,
        draw_flat_line: bool = False,
        projection_fn_numpy=None,
        draw_domes_callback=None,
        sun_alt: float | None = None,
        sun_az: float | None = None,
        terrain_shading_enabled: bool = True,
        sky_color_fn=None,
        interaction_active: bool = False,
        terrain_3d_enabled: bool | None = None,
        light_context: TerrainCelestialLightContext | None = None,
        surface_enabled: bool = True,
    ):
        """
        Main entry: draw all terrain layers.
        If draw_flat_line is True, ignores loaded data/fallback and draws a simple straight line.
        """
        if elevation_angle > 60.0:
            return  # Looking at zenith — skip terrain

        self._set_surface_visible(bool(surface_enabled), request_repaint=False)

        # Compatibility: the old control only disabled mesh lighting. The
        # replacement selects between two complete representations instead.
        if terrain_3d_enabled is None:
            terrain_3d_enabled = bool(terrain_shading_enabled)

        light_context = self._resolve_light_context(
            light_context,
            sun_alt=sun_alt,
            sun_az=sun_az,
        )
        sun_alt = light_context.sun_altitude_deg
        sun_az = light_context.sun_azimuth_deg
        light_factors = terrain_celestial_light_factors(
            light_context, self.render_settings
        )
        t_night = light_factors.night

        bottom_y = height * 2.0

        # Flat Line Mode
        if draw_flat_line:
            flat_light = self._terrain_light_components(
                0.0,
                0.0,
                1.0,
                0.0,
                light_context=light_context,
            )
            color = self._compose_profile_light_color(
                GROUND_DAY,
                float(flat_light.intensity),
                0.0,
                self._reference_sky_color(
                    sky_color_fn,
                    sun_alt,
                    sun_az,
                    current_azimuth,
                    t_night,
                ),
                flat_light.factors,
                solar_exposure=float(flat_light.solar_exposure),
                lunar_exposure=float(flat_light.lunar_exposure),
            )
            painter.setPen(QPen(color, 2))
            painter.setBrush(QBrush(color))

            pt = projection_fn(0.0, current_azimuth)
            if pt:
                y = pt[1]
                # If y is off-screen top, drawn from top of screen
                y_draw = int(max(-bottom_y, y))
                painter.drawRect(
                    0, y_draw, width, int(bottom_y * 1.5)
                )  # Big enough to cover
            return

        fov_deg = math.degrees(
            4.0 * math.atan(width / (2.0 * height * max(zoom_level, 1e-6)))
        )
        vert_scale = self.vert_exaggeration * zoom_level
        px_per_alt_deg = (height / 45.0) * vert_scale

        painter.setRenderHint(QPainter.Antialiasing)

        # Pre-calculate Culling range
        # CULLING_MARGIN: degrees outside viewport to keep for smooth transitions
        # Elevated terrain can move substantially inward relative to the
        # altitude-zero horizon under stereographic projection, especially
        # with a steep camera pitch. Keep a broad angular guard band so the
        # projected surface cannot terminate inside a lateral viewport edge.
        culling_margin = 45.0
        az_min = current_azimuth - (fov_deg / 2.0) - culling_margin
        az_max = current_azimuth + (fov_deg / 2.0) + culling_margin

        # ── Pre-process Domes (Darrere cap a davant) ─────────────────────────
        pending_domes = []
        if (
            draw_domes_callback
            and hasattr(self, "profile")
            and hasattr(self.profile, "light_domes")
        ):
            ld = self.profile.light_domes
            lpd = self.profile.light_peak_distances
            n = len(ld)
            # Peak detection to avoid saturation (grouping azimuths)
            for i in range(n):
                val = ld[i]
                if val < 0.2:
                    continue  # Threshold
                prev_val = ld[(i - 1) % n]
                next_val = ld[(i + 1) % n]
                # Local maximum check
                if val >= prev_val and val >= next_val:
                    # Simple plateau handling: only pick the first point
                    if val == next_val:
                        continue
                    pending_domes.append({"idx": i, "dist": lpd[i]})
            pending_domes.sort(key=lambda x: x["dist"], reverse=True)

            # Final step: Azimuthal Clustering to avoid 107 centers
            # We group peaks within 15 degrees to consolidate urban centers.
            if pending_domes:
                clustered = []
                # Sort by intensity to keep the brightest peak as the cluster center
                sorted_by_intensity = sorted(
                    pending_domes, key=lambda x: ld[x["idx"]], reverse=True
                )
                used_indices = set()

                for d in sorted_by_intensity:
                    if d["idx"] in used_indices:
                        continue

                    # New Cluster
                    center_az = self.profile.azimuths[d["idx"]]
                    clustered.append(d)
                    used_indices.add(d["idx"])

                    # Consume neighbors
                    for other in sorted_by_intensity:
                        if other["idx"] in used_indices:
                            continue
                        other_az = self.profile.azimuths[other["idx"]]

                        # Shortest angular distance
                        diff = abs(other_az - center_az) % 360
                        if diff > 180:
                            diff = 360 - diff

                        if diff < 15.0:  # 15 degree cluster radius
                            used_indices.add(other["idx"])

                pending_domes = sorted(
                    clustered, key=lambda x: x["dist"], reverse=True
                )

        # ── Dibuix de cada banda de darrera cap a davant ─────────────────────────
        sky_ref = self._reference_sky_color(
            sky_color_fn, sun_alt, sun_az, current_azimuth, t_night
        )
        terrain_mesh = getattr(self.profile, "terrain_mesh", None)
        has_terrain_mesh = bool(terrain_mesh)
        has_surface2d = self._has_terrain_surface_2d(terrain_mesh)
        use_3d_relief = bool(terrain_3d_enabled and has_surface2d)
        profile_image = None
        profile_image_painter = None
        profile_image_key = None

        if use_3d_relief:
            self._profile_polygon_cache_view_key = None
            self._profile_polygon_cache.clear()
            self._profile_category_hit_cache.clear()
            while pending_domes:
                d_info = pending_domes.pop(0)
                draw_domes_callback(painter, d_info["idx"], d_info["dist"])
        else:
            # A 2-D frame must not retain hit-test or material geometry from
            # the previous 3-D frame. Category lookup is rebuilt from the
            # profile polygons painted below.
            self._terrain_surface_image_geometry = None
            self._terrain_resolved_materials = None
            self._terrain_raster_cache_key = None
            self._terrain_raster_cache = None
            self._prepare_profile_polygon_cache(
                self.profile,
                projection_fn,
                width,
                height,
                current_azimuth,
                az_min,
                az_max,
                interaction_active,
            )
            if not interaction_active and not pending_domes and self._layers:
                profile_image_key = (
                    self._profile_polygon_cache_view_key,
                    round(float(t_night) * 256.0) / 256.0,
                    int(sky_ref.rgba()),
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
                        else round(float(light_context.moon_azimuth_deg) * 4.0)
                        / 4.0
                    ),
                    round(float(light_context.moon_illumination) * 256.0)
                    / 256.0,
                    round(float(light_context.eclipse_factor) * 256.0) / 256.0,
                    repr(self.render_settings),
                )
                if (
                    profile_image_key == self._profile_image_cache_key
                    and self._profile_image_cache is not None
                ):
                    painter.drawImage(0, 0, self._profile_image_cache)
                    return
                profile_image = QImage(
                    int(width), int(height), QImage.Format_ARGB32_Premultiplied
                )
                profile_image.fill(0)
                profile_image_painter = QPainter(profile_image)
                try:
                    profile_image_painter.setRenderHint(
                        QPainter.Antialiasing,
                        painter.testRenderHint(QPainter.Antialiasing),
                    )
                except Exception:
                    if profile_image_painter.isActive():
                        profile_image_painter.end()
                    raise

        # ── Farciment del terra amb gradient de perspectiva ───────────────────────
        # Simulem el pla de terra que s'allunya amb un gradient fosc→color terra,
        # evitant el rectangle pla uniforme que trenca el realisme.
        def draw_profile_layers_and_ground(target_painter: QPainter) -> None:
            # Bands are already ordered far-to-near. In silhouette mode the
            # layer-count control remains authoritative even if a mesh exists.
            terrain_layers = self._profile_layers_for_frame(interaction_active)
            for band_pts, _night_c, day_c in terrain_layers:
                # First: draw domes behind or within this band.
                while (
                    pending_domes
                    and pending_domes[0]["dist"] >= band_pts.band_min
                ):
                    d_info = pending_domes.pop(0)
                    draw_domes_callback(painter, d_info["idx"], d_info["dist"])

                color = _qcolor_from_rgba(day_c)
                self._draw_band_linear(
                    target_painter,
                    band_pts,
                    color,
                    projection_fn,
                    width,
                    height,
                    px_per_alt_deg,
                    current_azimuth,
                    az_min,
                    az_max,
                    projection_fn_numpy,
                    sun_alt=sun_alt,
                    sun_az=sun_az,
                    terrain_shading_enabled=True,
                    sky_color=sky_ref,
                    interaction_active=interaction_active,
                    light_context=light_context,
                )

            profile_resolved = getattr(self.profile, "resolved_mask", None)
            profile_is_partial = profile_resolved is not None and not bool(
                np.all(profile_resolved)
            )
            if (
                self._layers
                and not has_terrain_mesh
                and not profile_is_partial
            ):
                ground_light = self._terrain_light_components(
                    0.0,
                    0.0,
                    1.0,
                    0.0,
                    light_context=light_context,
                )
                ground_c = self._compose_profile_light_color(
                    GROUND_DAY,
                    float(ground_light.intensity),
                    0.0,
                    sky_ref,
                    ground_light.factors,
                    solar_exposure=float(ground_light.solar_exposure),
                    lunar_exposure=float(ground_light.lunar_exposure),
                )
                nearest = self._layers[-1]
                self._draw_ground_linear(
                    target_painter,
                    nearest[0],
                    ground_c,
                    projection_fn,
                    width,
                    height,
                    px_per_alt_deg,
                    current_azimuth,
                    az_min,
                    az_max,
                    overlap_px=1.0,
                    projection_fn_numpy=projection_fn_numpy,
                    interaction_active=interaction_active,
                )

        if not use_3d_relief:
            if profile_image_painter is not None and profile_image is not None:
                try:
                    draw_profile_layers_and_ground(profile_image_painter)
                finally:
                    # The cache owns the image; never let it be destroyed
                    # while Qt still has an active painter for it.
                    if profile_image_painter.isActive():
                        profile_image_painter.end()
                self._profile_image_cache_key = profile_image_key
                self._profile_image_cache = profile_image
                painter.drawImage(0, 0, profile_image)
                return
            draw_profile_layers_and_ground(painter)

        if use_3d_relief:
            rendered = False
            # Preserve subclass hooks and synthetic mesh-only profiles used by
            # integrations: they have no colour-band context from which the
            # enhanced per-vertex base colours can be derived reliably.
            supports_interpolated = (
                bool(self._layers)
                and "_draw_terrain_surface_2d" not in type(self).__dict__
            )
            categorical_surface = _surface_cache_has_categorical_material(
                self._visible_surface_cache()
            )
            if (
                self.render_settings.terrain_shading_mode == "interpolated"
                or categorical_surface
            ) and supports_interpolated:
                rendered = self._draw_terrain_interpolated(
                    painter,
                    terrain_mesh,
                    projection_fn,
                    width,
                    height,
                    current_azimuth,
                    az_min,
                    az_max,
                    t_night,
                    sky_ref,
                    sun_alt=sun_alt,
                    sun_az=sun_az,
                    projection_fn_numpy=projection_fn_numpy,
                    interaction_active=interaction_active,
                    light_context=light_context,
                )
            if not rendered:
                self._draw_terrain_surface_2d(
                    painter,
                    terrain_mesh,
                    projection_fn,
                    width,
                    height,
                    px_per_alt_deg,
                    current_azimuth,
                    az_min,
                    az_max,
                    t_night,
                    sky_ref,
                    sun_alt,
                    sun_az,
                    True,
                    projection_fn_numpy=projection_fn_numpy,
                    interaction_active=interaction_active,
                    light_context=light_context,
                )
        elif terrain_3d_enabled and has_terrain_mesh and not self._layers:
            self._draw_terrain_mesh(
                painter,
                terrain_mesh,
                projection_fn,
                width,
                height,
                px_per_alt_deg,
                current_azimuth,
                az_min,
                az_max,
                t_night,
                sky_ref,
                sun_alt,
                sun_az,
                terrain_shading_enabled,
                projection_fn_numpy=projection_fn_numpy,
                light_context=light_context,
            )

    def _reference_sky_color(
        self, sky_color_fn, sun_alt, sun_az, current_azimuth, t_night
    ):
        if (
            callable(sky_color_fn)
            and sun_alt is not None
            and sun_az is not None
        ):
            try:
                color = sky_color_fn(
                    0.0,
                    float(current_azimuth) % 360.0,
                    float(sun_alt),
                    float(sun_az),
                )
                if isinstance(color, QColor):
                    return color
            except Exception:
                log_suppressed_exception(
                    __name__, "OverlayDrawDispatchMixin._reference_sky_color"
                )
        return _qcolor_from_rgba(
            _lerp_color(QColor(170, 195, 215), QColor(5, 5, 12), t_night)
        )

    def _terrain_polygon_layers(self, has_terrain_mesh: bool):
        if not has_terrain_mesh or len(self._layers) <= 16:
            return self._layers

        target_layers = 12
        indices = np.linspace(0, len(self._layers) - 1, target_layers)
        indices = np.unique(np.rint(indices).astype(np.int32))
        if indices[-1] != len(self._layers) - 1:
            indices = np.append(indices, len(self._layers) - 1)
        return [self._layers[int(i)] for i in indices]

    def _has_terrain_surface_2d(self, mesh) -> bool:
        if not mesh:
            return False
        try:
            version = int(np.asarray(mesh.get("version", 1)).item())
            altitudes = np.asarray(mesh.get("altitudes"), dtype=np.float32)
            visible = np.asarray(mesh.get("visible"), dtype=bool)
            valid = np.asarray(mesh.get("valid"), dtype=bool)
            azimuths = np.asarray(mesh.get("azimuths"), dtype=np.float32)
            distances = np.asarray(mesh.get("distances"), dtype=np.float32)
        except Exception:
            return False
        return (
            version >= 2
            and altitudes.ndim == 2
            and altitudes.shape == visible.shape
            and altitudes.shape == valid.shape
            and distances.size == altitudes.shape[0]
            and azimuths.size == altitudes.shape[1]
            and bool(np.any(visible & valid))
        )

    def _apply_atmospheric_perspective(
        self, base_color: QColor, sky_color: QColor, band_pts, t_night: float
    ) -> QColor:
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        return self._apply_terrain_atmosphere(
            base_color, band_max_m, sky_color, t_night
        )

    def _terrain_shade_values(
        self,
        az_arr,
        h_arr,
        sun_alt,
        sun_az,
        band_pts,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> np.ndarray:
        return self._terrain_profile_light_grid(
            az_arr,
            h_arr,
            sun_alt,
            sun_az,
            band_pts,
            light_context=light_context,
        ).intensity

    def _terrain_profile_light_grid(
        self,
        az_arr,
        h_arr,
        sun_alt,
        sun_az,
        band_pts,
        *,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> TerrainLightingGrid:
        """Approximate visible profile normals and apply the shared sky light."""

        del h_arr
        context = self._resolve_light_context(
            light_context, sun_alt=sun_alt, sun_az=sun_az
        )
        azimuth = np.deg2rad(np.asarray(az_arr, dtype=np.float32))
        # A silhouette has no complete DEM normal.  Its visible flank faces the
        # observer, with a broad upward component that avoids wall-like bands.
        horizontal = 0.66
        normal_x = -np.sin(azimuth) * horizontal
        normal_y = -np.cos(azimuth) * horizontal
        normal_z = np.full(np.shape(normal_x), 0.75, dtype=np.float32)
        distance = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        return self._terrain_light_components(
            normal_x,
            normal_y,
            normal_z,
            distance,
            self._sun_vector_enu(
                context.sun_altitude_deg, context.sun_azimuth_deg
            ),
            context.sun_altitude_deg,
            terrain_shading_enabled=True,
            light_context=context,
        )

    def _compose_profile_light_color(
        self,
        base_color: QColor,
        intensity: float,
        distance_m: float,
        sky_color: QColor,
        factors: TerrainCelestialLightFactors,
        *,
        solar_exposure: float = 0.0,
        lunar_exposure: float = 0.0,
    ) -> QColor:
        """Run fallback/profile colours through the same surface pipeline."""

        base_rgba = (
            base_color
            if isinstance(base_color, tuple) and len(base_color) == 4
            else (base_color[0], base_color[1], base_color[2], 255)
            if isinstance(base_color, tuple)
            else base_color.getRgb()
        )
        base = np.asarray(base_rgba, dtype=np.uint8)
        vibrant = (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            )
            == SurfaceVisualStyle.VIBRANT.value
        )
        composed = compose_vertex_rgba(
            base,
            float(intensity),
            float(distance_m),
            self.render_settings,
            maximum_distance_m=self._maximum_terrain_distance_m(),
            horizon_rgb=(
                sky_color.red(),
                sky_color.green(),
                sky_color.blue(),
            ),
            atmosphere_strength=0.0 if vibrant else 1.0,
        )
        if vibrant:
            composed = apply_vibrant_color_grade(
                composed,
                float(intensity),
                float(distance_m),
                self.render_settings,
                maximum_distance_m=self._maximum_terrain_distance_m(),
                daylight_factor=factors.solar_ambient,
                moonlight_factor=factors.lunar_strength,
                solar_exposure=float(solar_exposure),
                lunar_exposure=float(lunar_exposure),
                atmosphere_rgb=(
                    sky_color.red(),
                    sky_color.green(),
                    sky_color.blue(),
                ),
            )
        return _qcolor_from_rgba(composed)

    def _sun_vector_enu(self, sun_alt, sun_az):
        if sun_alt is None or sun_az is None:
            return None
        alt_rad = math.radians(float(sun_alt))
        az_rad = math.radians(float(sun_az))
        cos_alt = math.cos(alt_rad)
        return np.array(
            [
                math.sin(az_rad) * cos_alt,
                math.cos(az_rad) * cos_alt,
                math.sin(alt_rad),
            ],
            dtype=np.float32,
        )
