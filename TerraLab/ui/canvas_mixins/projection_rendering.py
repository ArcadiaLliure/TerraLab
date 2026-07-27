"""Projection adapters and renderer-facing canvas operations."""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from PyQt5.QtCore import QPointF
from PyQt5.QtGui import QColor, QPainter, QPainterPath

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.astro.engine import AstroEngine
from TerraLab.render.grid_renderer import (
    draw_celestial_grid as render_draw_celestial_grid,
)
from TerraLab.render.horizon_renderer import (
    draw_ground_mask as render_draw_ground_mask,
)
from TerraLab.render.overlays_renderer import (
    draw_compass as render_draw_compass,
    draw_light_domes as render_draw_light_domes,
    draw_ngc_overlay as render_draw_ngc_overlay,
    draw_satellites as render_draw_satellites,
    draw_single_city_dome as render_draw_single_city_dome,
    draw_skyfield_objects as render_draw_skyfield_objects,
    draw_sun_skyfield as render_draw_sun_skyfield,
    get_moon_projection as render_get_moon_projection,
    ngc_symbol_for_type as render_ngc_symbol_for_type,
)
from TerraLab.render.qt.context import RenderContext
from TerraLab.render.sky_renderer import (
    draw_background as render_draw_background,
    sky_color_phys as render_sky_color_phys,
)
from TerraLab.render.stars_renderer import (
    draw_scope_solar_disc as render_draw_scope_solar_disc,
    get_scope_solar_pattern as render_get_scope_solar_pattern,
)
from TerraLab.scene.projection import (
    project_universal_stereo_numpy,
    project_universal_stereo_point,
)
from TerraLab.scene.scene_state import build_star_scene_state
from TerraLab.widgets.spherical_math import (
    calculate_sun_times as spherical_calculate_sun_times,
    gmst_deg as spherical_gmst_deg,
    julian_day as spherical_julian_day,
    lst_deg as spherical_lst_deg,
)


class CanvasProjectionAndRenderingMixin:
    def project_universal_stereo(self, alt, az):
        self._sync_camera_state()
        return project_universal_stereo_point(
            float(alt),
            float(az),
            int(self.width()),
            int(self.height()),
            self.camera,
        )

    def project_universal_stereo_numpy(self, alt_array, az_array):
        if np is None:
            return None, None
        self._sync_camera_state()
        sx, sy, valid = project_universal_stereo_numpy(
            alt_array,
            az_array,
            width=int(self.width()),
            height=int(self.height()),
            camera=self.camera,
        )
        if sx is None:
            return None, None
        sx = np.where(valid, sx, np.nan)
        sy = np.where(valid, sy, np.nan)
        return sx, sy

    def calculate_sun_times(self, lat, day_of_year):
        return spherical_calculate_sun_times(float(lat), int(day_of_year))

    def sky_color_phys(
        self, view_alt, view_az, sun_alt, sun_az, bortle=1, twilight_factor=1.0
    ):
        return render_sky_color_phys(
            view_alt,
            view_az,
            sun_alt,
            sun_az,
            bortle=bortle,
            twilight_factor=twilight_factor,
        )

    def draw_background(self, painter, sun_alt, sun_az, view_az, dimming=1.0):
        return render_draw_background(
            self,
            painter,
            sun_alt,
            sun_az,
            view_az,
            dimming=dimming,
        )

    def draw_celestial_grid(self, painter, hour):
        return render_draw_celestial_grid(self, painter, hour)

    def _on_star_result(self, img, vis_stars):
        self._cached_star_image = img
        self.visible_stars = vis_stars  # Update interactions
        if (
            np is not None
            and isinstance(vis_stars, list)
            and vis_stars
            and isinstance(vis_stars[0], tuple)
        ):
            try:
                self.visible_stars_sx = np.array(
                    [float(v[0]) for v in vis_stars], dtype=np.float32
                )
                self.visible_stars_sy = np.array(
                    [float(v[1]) for v in vis_stars], dtype=np.float32
                )
            except Exception:
                self.visible_stars_sx = np.array([], dtype=np.float32)
                self.visible_stars_sy = np.array([], dtype=np.float32)
        elif np is not None:
            self.visible_stars_sx = np.array([], dtype=np.float32)
            self.visible_stars_sy = np.array([], dtype=np.float32)
        self.rendering_busy = False
        self.update()  # Force repaint to show new stars immediately

    def _on_trail_result(self, img):
        self._cached_trail_image = img
        self.trail_rendering_busy = False

    def draw_stars(
        self,
        painter,
        hour,
        sun_alt,
        sun_az,
        visibility_factor=1.0,
        moon_mask=None,
        mag_limit=None,
        eff_lat=None,
        day_of_year=None,
    ):
        if np is not None:
            self.draw_stars_numpy(
                painter,
                hour,
                sun_alt,
                sun_az,
                mag_limit=mag_limit,
                eff_lat=eff_lat,
                day_of_year=day_of_year,
            )
            return
        if visibility_factor <= 0:
            return

    def _sync_camera_state(self):
        self.camera.azimuth_offset = float(self.azimuth_offset)
        self.camera.elevation_angle = float(self.elevation_angle)
        self.camera.zoom_level = float(self.zoom_level)
        self.camera.vertical_offset_ratio = float(self.vertical_offset_ratio)

    def _build_star_scene_state(
        self, hour, sun_alt, sun_az, mag_limit, eff_lat, day_of_year
    ):
        return build_star_scene_state(
            self,
            hour,
            sun_alt,
            sun_az,
            mag_limit,
            eff_lat,
            day_of_year,
        )

    def _emit_render_diagnostics(self, stars_result):
        if not self.debug_render_metrics:
            return
        now_t = time.time()
        if now_t - self._last_diagnostics_log_time < 0.4:
            return
        self._last_diagnostics_log_time = now_t
        fov_deg = 100.0 / max(0.001, float(self.zoom_level))
        snap = self.scene_diagnostics.snapshot()
        counters = snap.counters
        timings = snap.timings_ms
        line = (
            f"[SkyDiagnostics] viewport={self.width()}x{self.height()} "
            f"zoom={self.zoom_level:.2f} fov={fov_deg:.2f} "
            f"cam_ra={self.azimuth_offset % 360.0:.2f} cam_dec={self.elevation_angle:.2f} "
            f"total_in_view={stars_result.total_in_view} "
            f"view_prefilter={int(counters.get('view_prefilter_count', 0))} "
            f"view_tiles={int(counters.get('view_tile_count', 0))} "
            f"view_tile_candidates={int(counters.get('view_tile_candidates', 0))} "
            f"view_fov_pre={float(counters.get('view_prefilter_fov_deg', 0.0)):.2f} "
            f"scope_prefilter={int(counters.get('scope_prefilter_count', 0))} "
            f"scope_mask={int(counters.get('scope_after_mask', 0))} "
            f"scope_bounds={int(counters.get('scope_after_bounds', 0))} "
            f"scope_tiles={int(counters.get('scope_tile_count', 0))} "
            f"scope_tile_candidates={int(counters.get('scope_tile_candidates', 0))} "
            f"after_mag={stars_result.after_mag_cut} "
            f"after_bucket={stars_result.after_bucket} "
            f"avg_radius={stars_result.avg_radius:.2f} "
            f"halos={int(counters.get('halo_count', 0))} "
            f"halo_grad={int(counters.get('halo_gradient_count', 0))} "
            f"halo_solid={int(counters.get('halo_solid_count', 0))} "
            f"first_fix_pending={int(counters.get('scope_first_fix_pending', 0))} "
            f"force_naked_eye={int(counters.get('scope_force_naked_eye', 0))} "
            f"mLim={float(counters.get('limiting_mag', 0.0)):.2f} "
            f"preLim={float(counters.get('pre_limit', 0.0)):.2f} "
            f"catalogMax={float(counters.get('catalog_max_mag', -1.0)):.2f} "
            f"ms_horizon={timings.get('renderer_horizon', 0.0):.2f} "
            f"ms_milkyway={timings.get('renderer_milkyway', 0.0):.2f} "
            f"ms_grid={timings.get('renderer_grid', 0.0):.2f} "
            f"ms_view_pre={timings.get('stars_view_prefilter', 0.0):.2f} "
            f"ms_scope_pre={timings.get('stars_scope_prefilter', 0.0):.2f} "
            f"ms_altaz={timings.get('stars_altaz', 0.0):.2f} "
            f"ms_proj={timings.get('stars_projection', 0.0):.2f} "
            f"ms_stars_renderer={timings.get('renderer_stars', 0.0):.2f} "
            f"ms_overlays={timings.get('renderer_overlays', 0.0):.2f}"
        )
        print(line)

    def _milkyway_overlay_status_line(self) -> str:
        renderer = getattr(
            getattr(self, "sky_renderer", None), "milkyway_overlay", None
        )
        if renderer is None or not hasattr(renderer, "runtime_status"):
            return "MW: n/a"
        try:
            status = renderer.runtime_status()
        except Exception:
            return "MW: status error"
        if not isinstance(status, dict):
            return "MW: pending"
        if not bool(status.get("enabled", False)):
            return "MW: OFF"
        if not bool(status.get("texture_loaded", False)):
            return "MW: PNG NO"
        dust_requested = bool(status.get("dust_requested", False))
        dust_loaded = bool(status.get("dust_loaded", False))
        opacity = float(status.get("effective_opacity", 0.0))
        blend_mode = str(status.get("blend_mode", "add"))
        opacity_reason = str(status.get("opacity_reason", ""))
        rgb_gain = float(status.get("texture_rgb_gain", 1.0))
        texture_frame = str(status.get("texture_frame", "galactic"))
        frame_short = "gal" if texture_frame.startswith("gal") else "eq"
        ra_off = float(status.get("ra_offset_deg", 0.0))
        lat_flip = bool(status.get("texture_lat_flip", False))
        lon_flip = bool(status.get("texture_lon_flip", False))
        flip_txt = ("vf=1" if lat_flip else "vf=0") + (
            " hf=1" if lon_flip else " hf=0"
        )
        dust_den = float(status.get("dust_density_strength", 0.0))
        dust_ext = float(status.get("dust_extinction_strength", 0.0))
        dust_gain_txt = f"d={dust_den:.2f} e={dust_ext:.2f}"
        if dust_requested:
            dust_txt = "Planck OK" if dust_loaded else "Planck NO"
        else:
            dust_txt = "Planck OFF"
        if opacity <= 1e-4:
            return (
                f"MW: ON op=0.00 ({opacity_reason}) g={rgb_gain:.2f} "
                f"fr={frame_short} off={ra_off:.0f} {flip_txt} {dust_gain_txt} {dust_txt}"
            )
        return (
            f"MW: ON op={opacity:.2f} {blend_mode} g={rgb_gain:.2f} "
            f"fr={frame_short} off={ra_off:.0f} {flip_txt} {dust_gain_txt} {dust_txt}"
        )

    def draw_stars_numpy(
        self,
        painter,
        hour,
        sun_alt,
        sun_az,
        mag_limit=None,
        eff_lat=None,
        day_of_year=None,
    ):
        if np is None:
            self.visible_stars = []
            self.visible_stars_sx = (
                np.array([], dtype=np.float32) if np is not None else []
            )
            self.visible_stars_sy = (
                np.array([], dtype=np.float32) if np is not None else []
            )
            return
        state = self._build_star_scene_state(
            hour=hour,
            sun_alt=sun_alt,
            sun_az=sun_az,
            mag_limit=mag_limit,
            eff_lat=eff_lat,
            day_of_year=day_of_year,
        )
        # Cataleg actiu del frame per garantir pick/goto coherents.
        self._active_catalog_ra = getattr(state, "ra", None)
        self._active_catalog_dec = getattr(state, "dec", None)
        self._active_catalog_mag = getattr(state, "mag", None)
        self._active_catalog_bp_rp = getattr(state, "bp_rp", None)
        self._active_catalog_r = getattr(state, "color_r", None)
        self._active_catalog_g = getattr(state, "color_g", None)
        self._active_catalog_b = getattr(state, "color_b", None)
        self._active_catalog_ids = getattr(state, "star_ids", None)
        self.scene_diagnostics.reset()
        ctx = RenderContext(
            painter=painter,
            width=self.width(),
            height=self.height(),
            diagnostics=self.scene_diagnostics,
        )
        stars_result = self.sky_renderer.render(ctx, state)
        self.visible_stars = stars_result.visible_indices
        self.visible_stars_sx = stars_result.visible_sx
        self.visible_stars_sy = stars_result.visible_sy
        self._emit_render_diagnostics(stars_result)

    def get_star_color(self, bp_rp):
        # Map BP-RP index to RGB
        if bp_rp < 0.0:
            return QColor(160, 190, 255)  # Blue
        elif bp_rp < 0.5:
            t = (bp_rp - 0.0) / 0.5
            return QColor(160 + int(95 * t), 190 + int(65 * t), 255)
        elif bp_rp < 1.0:
            t = (bp_rp - 0.5) / 0.5
            return QColor(255, 255, 255 - int(55 * t))
        elif bp_rp < 2.0:
            t = (bp_rp - 1.0) / 1.0
            return QColor(255, 255 - int(80 * t), 200 - int(100 * t))
        else:
            return QColor(255, 175, 100)  # Red

    def get_datetime_utc(self, ut_hour):
        # Construct UTC datetime from manual year/day/hour
        y = self.parent_widget.manual_year
        d = self.parent_widget.manual_day
        dt_start = datetime(y, 1, 1, tzinfo=timezone.utc)
        return dt_start + timedelta(days=d, hours=ut_hour)

    def julian_day(self, dt):
        return spherical_julian_day(dt)

    def gmst_deg(self, jd):
        return spherical_gmst_deg(float(jd))

    def lst_deg(self, jd, lon_deg):
        return spherical_lst_deg(float(jd), float(lon_deg))

    def sun_alt_az_from_ra_dec(self, ra, dec, lat, lst):
        # Standard conversion helper
        ha = lst - ra
        ha_rad = math.radians(ha)
        lat_rad = math.radians(lat)
        dec_rad = math.radians(dec)
        sin_lat = math.sin(lat_rad)
        cos_lat = math.cos(lat_rad)
        sin_dec = math.sin(dec_rad)
        cos_dec = math.cos(dec_rad)
        sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * math.cos(ha_rad)
        alt = math.degrees(math.asin(max(-1, min(1, sin_alt))))
        cos_az = (sin_dec - sin_alt * sin_lat) / (
            math.cos(math.radians(alt)) * cos_lat + 1e-10
        )
        az = math.degrees(math.acos(max(-1, min(1, cos_az))))
        if math.sin(ha_rad) > 0:
            az = 360 - az
        return alt, az

    def get_sun_ra_dec(self, d):
        # Precise Sun (Meeus Ch 25)
        # Wraps AstroEngine for Sun
        # d is Days since J2000 UTC.
        # Add Delta T (72s) for TDB based T
        d_tdb = d + (72.0 / 86400.0)
        T = d_tdb / 36525.0
        longitude, latitude, _radius = AstroEngine.get_sun_position_vsop(T)
        ra, dec = AstroEngine.ecliptic_to_equatorial(
            longitude, latitude, T
        )
        return ra, dec

    def get_moon_ra_dec(self, d):
        # Wraps AstroEngine for Moon
        d_tdb = d + (72.0 / 86400.0)
        T = d_tdb / 36525.0
        longitude, latitude, radius = AstroEngine.get_moon_position_elp(T)
        ra, dec = AstroEngine.ecliptic_to_equatorial(
            longitude, latitude, T
        )
        return ra, dec, radius

    def get_topocentric_position(
        self, ra_geo, dec_geo, dist_km, obs_lat, obs_lon, jd
    ):
        return AstroEngine.get_topocentric_position(
            ra_geo, dec_geo, dist_km, obs_lat, obs_lon, jd
        )

    def get_moon_projection(self, hour):
        return render_get_moon_projection(self, hour)

    def draw_ground_mask(self, painter, is_day):
        return render_draw_ground_mask(self, painter, is_day)

    def draw_compass(self, painter):
        return render_draw_compass(self, painter)

    def _ngc_symbol_for_type(self, obj_type: str) -> str:
        return render_ngc_symbol_for_type(obj_type)

    def draw_ngc_overlay(
        self, painter: QPainter, ut_hour: float, day_of_year_utc: int
    ) -> None:
        return render_draw_ngc_overlay(self, painter, ut_hour, day_of_year_utc)

    def draw_satellites(self, painter, ut_hour):
        return render_draw_satellites(self, painter, ut_hour)

    def mousePressEvent(self, event):
        return self._input_handler.handle_mouse_press(event)

    def mouseMoveEvent(self, event):
        return self._input_handler.handle_mouse_move(event)

    def mouseReleaseEvent(self, event):
        return self._input_handler.handle_mouse_release(event)

    def mouseDoubleClickEvent(self, event):
        return self._input_handler.handle_mouse_double_click(event)

    def get_simulated_tz_offset(self, day_of_year=None):
        try:
            sim_day = int(
                self.parent_widget.manual_day
                if day_of_year is None
                else day_of_year
            )
            sim_year = int(
                getattr(self.parent_widget, "manual_year", datetime.now().year)
            )
            local_hour = float(self.parent_widget.get_current_hour())
            dt_local = self._simulated_local_datetime(
                local_hour, sim_year=sim_year, sim_day=sim_day
            )
            off = dt_local.utcoffset()
            if off is not None:
                return float(off.total_seconds() / 3600.0)
        except Exception:
            log_suppressed_exception(__name__, "CanvasProjectionAndRenderingMixin.get_simulated_tz_offset")
        fallback = datetime.now().astimezone().utcoffset()
        if fallback is None:
            return 0.0
        return float(fallback.total_seconds() / 3600.0)

    def perceived_disc_scale(
        self,
        alt_deg,
        sun_alt_deg=None,
        is_trained=None,
        horizon_refs=None,
        flattening=None,
        atmos=None,
        falloff_deg=35.0,
    ):
        # Defaults
        if is_trained is None:
            is_trained = self.trained_observer
        if horizon_refs is None:
            horizon_refs = self.horizon_refs
        if flattening is None:
            flattening = self.dome_flattening
        if atmos is None:
            atmos = self.atmospheric_context
        # Eclipse Lock Check (Blind check relies on caller, but we check global enable)
        if not self.illusion_enabled:
            return 1.0
        # 1. Base Illusion Magnitude
        # lerp(0.22, 0.50, horizon_refs)
        max_illusion = 0.22 + (0.50 - 0.22) * horizon_refs
        # 2. Dome Flattening Boost
        # lerp(1.0, 1.25, flattening)
        max_illusion *= 1.0 + 0.25 * flattening
        # 3. Altitude Falloff (Smoothstep logic)
        # clamp(alt, 0, falloff) -> normalized 0..1
        val = max(0.0, min(1.0, alt_deg / falloff_deg))
        # Invert: 1 at horizon, 0 at falloff
        w = 1.0 - val
        # Smoothstep: 3x^2 - 2x^3
        w = w * w * (3.0 - 2.0 * w)
        # 4. Trained Observer Reduction
        if is_trained:
            max_illusion *= 0.6
        # 5. Atmospheric Context Boost
        illusion_val = (max_illusion * w) * (1.0 + atmos * 0.10)
        # 6. Final Scale (Base 1.0 + illusion)
        # Limit to 1.6x max
        scale = max(1.0, min(1.6, 1.0 + illusion_val))
        return scale

    def draw_light_domes(self, painter, profile, eff_sun_alt, eclipse_dimming):
        return render_draw_light_domes(
            self,
            painter,
            profile,
            eff_sun_alt,
            eclipse_dimming,
        )

    def _draw_single_city_dome(
        self, painter, profile, idx, dist, twilight_factor
    ):
        return render_draw_single_city_dome(
            self, painter, profile, idx, dist, twilight_factor
        )

    def _sun_weather_dim_factor(self) -> float:
        w = None
        if hasattr(self.parent_widget, "weather"):
            w = self.parent_widget.weather
        elif hasattr(self, "weather"):
            w = self.weather
        if w is None:
            return 0.0
        try:
            cover = max(0.0, min(1.0, float(getattr(w, "current_cover", 0.0))))
            precip = max(0.0, min(1.0, float(getattr(w, "precip_int", 0.0))))
            fog = max(0.0, min(1.0, float(getattr(w, "fog_cover", 0.0))))
            humidity = max(0.0, min(1.0, float(getattr(w, "humidity", 0.0))))
            dim_cover = max(0.0, (cover - 0.55) / 0.45)
            dim_fog = min(1.0, fog * 1.10 + max(0.0, humidity - 0.80) * 0.85)
            dim = max(dim_cover, precip * 1.20, dim_fog)
            return max(0.0, min(1.0, dim**1.15))
        except Exception:
            return 0.0

    def draw_skyfield_objects(
        self, painter, ut_hour, day_of_year, ambient_light=1.0, mag_limit=None
    ):
        return render_draw_skyfield_objects(
            self,
            painter,
            ut_hour,
            day_of_year,
            ambient_light=ambient_light,
            mag_limit=mag_limit,
        )

    def update_loop(self):
        # Keep the main update loop at 60 FPS for smooth movement.
        target_interval_ms = 16
        try:
            if bool(getattr(self, "_updates_paused", False)):
                return
        except Exception:
            target_interval_ms = 16
        if (
            hasattr(self, "timer")
            and self.timer.interval() != target_interval_ms
        ):
            self.timer.setInterval(target_interval_ms)
        if self.use_real_time:
            now = datetime.now()
            # Sync Day & Year
            self.manual_year = now.year
            self.manual_day = (now - datetime(now.year, 1, 1)).days
            if hasattr(self, "lbl_date"):
                self.lbl_date.setText(self.format_date(self.manual_day))
            # Sync Gradient
            if hasattr(self, "time_bar"):
                self.time_bar.update_params(
                    self.latitude, self.longitude, self.manual_day
                )
            # Sync Time
            h = now.hour + now.minute / 60.0 + now.second / 3600.0
            if hasattr(self, "time_bar"):
                self.time_bar.set_time(h)
        else:
            # Manual Mode: "Time keeps running forward"
            # Increment manual_hour by elapsed time
            # Timer interval varies, so usage of constant '0.1' is wrong if we change FPS?
            # We should measure actual dt.
            # For simplicity, we assume the interval is respected or we use current interval.
            dt_sec = self.timer.interval() / 1000.0
            dt_hours = dt_sec / 3600.0
            self.manual_hour = (self.manual_hour + dt_hours) % 24.0
            if hasattr(self, "time_bar"):
                self.time_bar.set_time(self.manual_hour)
        self.canvas.update()

    def get_refracted_body_path(self, radius, alt, pixels_per_deg):
        """
        Generates a QPainterPath representing the body.
        SIMPLIFICATION: We have disabled differential refraction (squashing)
        to ensure perfect geometric overlap during eclipses.
        The Sun and Moon will remain perfect circles.
        The 'lift' effect is already handled by Skyfield's altitude positioning.
        """
        path = QPainterPath()
        path.addEllipse(QPointF(0, 0), radius, radius)
        return path

    def _get_scope_solar_pattern(self):
        return render_get_scope_solar_pattern(self)

    def _draw_scope_solar_disc(self, painter, radius):
        return render_draw_scope_solar_disc(self, painter, radius)

    def draw_sun_skyfield(
        self, painter, alt, az, radius, color, corona_opacity, pixels_per_deg
    ):
        return render_draw_sun_skyfield(
            self,
            painter,
            alt,
            az,
            radius,
            color,
            corona_opacity,
            pixels_per_deg,
        )
