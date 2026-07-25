"""Target selection, star trails, and paint-event orchestration."""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QMenu
from timezonefinder import TimezoneFinder

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.utils import getTraduction, get_config_value
from TerraLab.render.stars_renderer import (
    draw_analytic_trails as render_draw_analytic_trails,
    draw_analytic_trails_numpy as render_draw_analytic_trails_numpy,
)
from TerraLab.ui.canvas_runtime_helpers import (
    canvas_log_positions,
    canvas_paintEvent,
    canvas_scope_hud_extra_lines,
    canvas_scope_hud_star_count,
    canvas_set_selected_target,
)
from TerraLab.widgets.measurement_tools import TOOL_NONE
from TerraLab.widgets.spherical_math import (
    altaz_to_ra_dec,
    get_sun_alt_az as spherical_get_sun_alt_az,
    ra_dec_to_alt_az,
    screen_to_sky,
)
from TerraLab.widgets.telescope_scope_mode import TelescopeScopeController

_TIMEZONE_FINDER = TimezoneFinder()


class CanvasSelectionAndTrailsMixin:
    def _pick_star_at(self, sx: float, sy: float, click_radius: float = 20.0):
        return self._selection.pick_star_at(float(sx), float(sy), float(click_radius))

    def _pick_target_at(self, sx: float, sy: float):
        return self._selection.pick_target_at(float(sx), float(sy))

    def _pick_ngc_at(self, sx: float, sy: float, click_radius: float = 26.0):
        x = float(sx)
        y = float(sy)
        best_item = None
        best_score = float("inf")
        for item in getattr(self, "visible_ngc_objects", []):
            if not isinstance(item, dict):
                continue
            try:
                cx = float(item.get("sx", 0.0))
                cy = float(item.get("sy", 0.0))
                marker_radius = float(item.get("pick_radius_px", 0.0))
            except Exception:
                continue
            pick_radius = max(float(click_radius), marker_radius)
            dist = math.hypot(cx - x, cy - y)
            if dist > pick_radius:
                continue
            score = dist / max(1.0, pick_radius)
            if score < best_score:
                best_score = score
                best_item = item
        if best_item is None:
            return None
        obj = best_item.get("obj")
        if obj is None:
            return None
        info = best_item.get("info")
        if not isinstance(info, dict):
            info = {
                "type": "ngc",
                "obj": obj,
                "name": getattr(obj, "common_name", None) or getattr(obj, "name", "NGC"),
            }
        return {"kind": "ngc", "obj": obj, "info": info}

    def _visible_star_count_raw(self) -> int:
        vis = getattr(self, "visible_stars", None)
        if vis is None:
            return 0
        try:
            return int(len(vis))
        except Exception:
            return 0

    def _scope_hud_star_count(self) -> int:
        return canvas_scope_hud_star_count(self)

    def _scope_contains_alt_az_mask(self, alt_deg_arr, az_deg_arr):
        if not self.scope_mode_enabled() or not hasattr(self, "scope_controller") or np is None:
            return None
        ctrl = self.scope_controller
        if (
            (not getattr(ctrl, "enabled", False))
            or getattr(ctrl, "center", None) is None
            or getattr(ctrl, "awaiting_center_click", False)
        ):
            return None
        try:
            center_alt = float(ctrl.center[0])
            center_az = float(ctrl.center[1]) % 360.0
            fov_w, fov_h = ctrl.current_fov()
        except Exception:
            return None
        alt = np.asarray(alt_deg_arr, dtype=np.float32)
        az = np.asarray(az_deg_arr, dtype=np.float32) % 360.0
        if getattr(ctrl, "shape", TelescopeScopeController.SHAPE_CIRCLE) == TelescopeScopeController.SHAPE_RECT:
            half_h = 0.5 * float(fov_h)
            half_w = 0.5 * float(fov_w)
            cos_lat = max(0.05, math.cos(math.radians(center_alt)))
            daz_lim = half_w / cos_lat
            d_az = ((az - center_az + 180.0) % 360.0) - 180.0
            return (np.abs(alt - center_alt) <= half_h) & (np.abs(d_az) <= daz_lim)
        radius_deg = 0.5 * min(float(fov_w), float(fov_h))
        c_alt = math.radians(center_alt)
        sin_c = math.sin(c_alt)
        cos_c = math.cos(c_alt)
        alt_r = np.radians(alt)
        d_az_r = np.radians(((az - center_az + 180.0) % 360.0) - 180.0)
        cos_dist = np.sin(alt_r) * sin_c + np.cos(alt_r) * cos_c * np.cos(d_az_r)
        cos_dist = np.clip(cos_dist, -1.0, 1.0)
        return cos_dist >= math.cos(math.radians(radius_deg))

    def _resolve_observer_tzinfo(self):
        lat = float(getattr(self.parent_widget, "latitude", 0.0))
        lon = float(getattr(self.parent_widget, "longitude", 0.0))
        explicit_tz = str(
            getattr(self.parent_widget, "observer_timezone", "")
            or get_config_value("observer_timezone", "")
            or ""
        ).strip()

        cache_key = (round(lat, 4), round(lon, 4), explicit_tz)
        if getattr(self, "_observer_tz_cache_key", None) == cache_key:
            cached = getattr(self, "_observer_tzinfo", None)
            if cached is not None:
                return cached

        tzinfo = None
        tz_name = explicit_tz
        if not tz_name and _TIMEZONE_FINDER is not None:
            try:
                tz_name = _TIMEZONE_FINDER.timezone_at(lat=lat, lng=lon) or ""
            except Exception:
                tz_name = ""

        if tz_name:
            try:
                tzinfo = ZoneInfo(tz_name)
            except Exception:
                tzinfo = None

        # Prefer machine local timezone when political timezone resolution is unavailable.
        if tzinfo is None:
            try:
                tzinfo = datetime.now().astimezone().tzinfo
                if tzinfo is not None and not tz_name:
                    tz_name = str(getattr(tzinfo, "key", "system"))
            except Exception:
                tzinfo = None

        # Last-resort fallback map by longitude band.
        if tzinfo is None:
            try:
                lon_norm = ((float(lon) + 180.0) % 360.0) - 180.0
                offset_h = int(round(lon_norm / 15.0))
                offset_h = max(-12, min(14, offset_h))
                tzinfo = timezone(timedelta(hours=offset_h))
                if not tz_name:
                    tz_name = f"UTC{offset_h:+d}"
            except Exception:
                tzinfo = None

        if tzinfo is None:
            tzinfo = timezone.utc

        self._observer_tz_cache_key = cache_key
        self._observer_tzinfo = tzinfo
        self._observer_tz_name = str(tz_name or getattr(tzinfo, "key", "system"))
        return tzinfo

    def _simulated_local_datetime(self, local_hour: float, sim_year: int = None, sim_day: int = None):
        if sim_year is None:
            sim_year = int(getattr(self.parent_widget, "manual_year", datetime.now().year))
        if sim_day is None:
            sim_day = int(getattr(self.parent_widget, "manual_day", 0))
        dt_local_naive = datetime(int(sim_year), 1, 1) + timedelta(days=int(sim_day), hours=float(local_hour))
        tzinfo = self._resolve_observer_tzinfo()
        try:
            return dt_local_naive.replace(tzinfo=tzinfo)
        except Exception:
            return dt_local_naive.astimezone()

    def _current_ut_context(self):
        ut_hour, day_of_year_utc, _, _ = self._get_current_utc_context()
        return ut_hour, day_of_year_utc

    def _ensure_scope_mode_for_shortcut(self):
        if self.scope_mode_enabled():
            return
        # Reuse the same activation entry point used by the scope panel.
        if hasattr(self.parent_widget, "activate_scope_mode"):
            self.parent_widget.activate_scope_mode()
            return
        # Fallback path for safety if parent widget helper is unavailable.
        self.set_scope_enabled(True)
        if hasattr(self.parent_widget, "sync_scope_ui_state"):
            self.parent_widget.sync_scope_ui_state(True)
        if self.measurement_tool_active():
            self.set_measurement_tool(TOOL_NONE)
            if hasattr(self.parent_widget, "_sync_measure_tool_buttons"):
                self.parent_widget._sync_measure_tool_buttons(TOOL_NONE)

    def _scope_jump_to_sky(self, sky):
        if sky is None:
            return False
        alt, az = float(sky[0]), float(sky[1])
        self.scope_controller.set_center((alt, az), confirmed=True)
        self.azimuth_offset = az % 360.0
        self.elevation_angle = max(-90.0, min(90.0, alt))
        # Keep "zoom towards target" behavior: never zoom out.
        fov_w, fov_h = self.scope_controller.current_fov()
        target_fov = max(0.2, min(93.9, max(fov_w, fov_h)))
        goto_zoom = max(0.5, min(140.0, 93.9 / target_fov))
        self.zoom_level = max(float(self.zoom_level), goto_zoom)
        self._cached_star_image = None
        self._cached_trail_image = None
        self.setFocus()
        self.update()
        return True

    def _goto_target(self, target):
        if target is None:
            return
        self._set_selected_target(target)
        self.scope_camera_lock_to_target = True
        ut_hour, day_of_year_utc, year_utc, _ = self._get_current_utc_context()
        sky = self._selected_target_sky_position(ut_hour, day_of_year_utc, year_utc=year_utc)
        if sky is None:
            return
        # Goto is just a shortcut into the same scope-jump flow.
        self._ensure_scope_mode_for_shortcut()
        self._scope_jump_to_sky(sky)

    def _show_object_context_menu(self, event):
        target = self._pick_target_at(event.x(), event.y())
        if target is None:
            return False
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu {"
            " background-color: rgba(18, 20, 24, 235);"
            " color: #f2f5ff;"
            " border: 1px solid rgba(210, 220, 240, 120);"
            "}"
            "QMenu::item {"
            " padding: 6px 14px;"
            " color: #f2f5ff;"
            "}"
            "QMenu::item:selected {"
            " background-color: rgba(92, 138, 255, 180);"
            " color: #ffffff;"
            "}"
        )
        act_goto = menu.addAction(getTraduction("Astro.ContextGoto", "Goto"))
        chosen = menu.exec_(event.globalPos())
        if chosen == act_goto:
            self._goto_target(target)
            return True
        return False

    def _set_selected_target(self, target):
        return canvas_set_selected_target(self, target)

    def _selected_target_sky_position(self, ut_hour: float, day_of_year_utc: int, year_utc: int = None):
        target = self.selected_target
        if not isinstance(target, dict):
            return None
        if target.get("kind") == "star":
            return self._star_alt_az(target.get("star"), ut_hour, day_of_year_utc, year_utc=year_utc)
        if target.get("kind") == "ngc":
            obj = target.get("obj")
            if obj is None:
                return None
            try:
                return self._ra_dec_to_alt_az(float(obj.ra_deg), float(obj.dec_deg), ut_hour, day_of_year_utc, year_utc=year_utc)
            except Exception:
                return None
        if target.get("kind") != "sky":
            return None
        data = getattr(self, "_sf_cache", {}).get("data", None)
        t = str(target.get("type", "")).lower()
        if isinstance(data, dict):
            if t == "sun":
                s = data.get("sun", {})
                if isinstance(s, dict) and ("alt" in s) and ("az" in s):
                    return float(s["alt"]), float(s["az"]) % 360.0
            elif t == "moon":
                m = data.get("moon", {})
                if isinstance(m, dict) and ("alt" in m) and ("az" in m):
                    return float(m["alt"]), float(m["az"]) % 360.0
            elif t == "planet":
                want = self._normalize_planet_key(target.get("key", ""))
                for p in data.get("planets", []):
                    key = self._normalize_planet_key(p.get("key", ""))
                    if key == want or self._normalize_planet_key(p.get("name", "")) == want:
                        return float(p.get("alt", 0.0)), float(p.get("az", 0.0)) % 360.0
        # Fallback to snapshot at click-time if cache is unavailable.
        try:
            return float(target.get("alt")), float(target.get("az")) % 360.0
        except Exception:
            return None

    def _star_alt_az(self, star_obj, ut_hour: float, day_of_year_utc: int, year_utc: int = None):
        coords = self._extract_star_coords(star_obj)
        if coords is None:
            return None
        ra, dec = coords
        return self._ra_dec_to_alt_az(ra, dec, ut_hour, day_of_year_utc, year_utc=year_utc)

    def _ra_dec_to_alt_az(self, ra_deg: float, dec_deg: float, ut_hour: float, day_of_year_utc: int, year_utc: int = None):
        if year_utc is None:
            try:
                year_utc = int(getattr(self.parent_widget, "manual_year", datetime.now().year))
            except Exception:
                year_utc = datetime.now().year
        return ra_dec_to_alt_az(
            float(ra_deg),
            float(dec_deg),
            float(ut_hour),
            int(day_of_year_utc),
            float(self.parent_widget.latitude),
            float(self.parent_widget.longitude),
            year=int(year_utc),
            default_az_deg=float(self.azimuth_offset),
        )

    def _get_current_utc_context(self):
        local_hour = float(self.parent_widget.get_current_hour())
        sim_y = int(self.parent_widget.manual_year)
        sim_d = int(self.parent_widget.manual_day)
        try:
            dt_local = self._simulated_local_datetime(local_hour, sim_year=sim_y, sim_day=sim_d)
            if getattr(dt_local, "tzinfo", None) is None:
                dt_local = dt_local.replace(tzinfo=self._resolve_observer_tzinfo())
            dt_utc = dt_local.astimezone(timezone.utc)
        except Exception:
            tz_offset = self.get_simulated_tz_offset(sim_d)
            dt_utc = (datetime(sim_y, 1, 1) + timedelta(days=sim_d, hours=float(local_hour) - tz_offset)).replace(tzinfo=timezone.utc)
        ut_hour = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
        day_of_year_utc = (dt_utc.date() - datetime(dt_utc.year, 1, 1).date()).days
        return ut_hour, day_of_year_utc, int(dt_utc.year), dt_utc

    def _screen_to_ra_dec(self, sx: float, sy: float):
        sky = screen_to_sky(float(sx), float(sy), self.unproject_stereo)
        if sky is None:
            return None
        ut_hour, day_of_year_utc, year_utc, _ = self._get_current_utc_context()
        return self._altaz_to_ra_dec(sky[0], sky[1], ut_hour, day_of_year_utc, year_utc=year_utc)

    def _altaz_to_ra_dec(self, alt_deg: float, az_deg: float, ut_hour: float, day_of_year_utc: int, year_utc: int = None):
        if year_utc is None:
            try:
                year_utc = int(getattr(self.parent_widget, "manual_year", datetime.now().year))
            except Exception:
                year_utc = datetime.now().year
        return altaz_to_ra_dec(
            float(alt_deg),
            float(az_deg),
            float(ut_hour),
            int(day_of_year_utc),
            float(self.parent_widget.latitude),
            float(self.parent_widget.longitude),
            year=int(year_utc),
        )

    def _format_ra_hms(self, ra_deg: float) -> str:
        h_total = (float(ra_deg) % 360.0) / 15.0
        h = int(h_total)
        m_total = (h_total - h) * 60.0
        m = int(m_total)
        s = int(round((m_total - m) * 60.0))
        if s >= 60:
            s = 0
            m += 1
        if m >= 60:
            m = 0
            h = (h + 1) % 24
        return f"{h:02d}h {m:02d}m {s:02d}s"

    def _format_dec_deg(self, dec_deg: float) -> str:
        sign = "+" if dec_deg >= 0 else "-"
        return f"{sign}{abs(float(dec_deg)):.3f}°"

    def _scope_hud_extra_lines(self, ut_hour: float, day_of_year_utc: int):
        return canvas_scope_hud_extra_lines(self, ut_hour, day_of_year_utc)

    def _draw_selected_target_marker(self, painter: QPainter, ut_hour: float, day_of_year_utc: int):
        if self.scope_mode_enabled() or self.selected_target is None:
            return
        _, _, year_utc, _ = self._get_current_utc_context()
        sky = self._selected_target_sky_position(ut_hour, day_of_year_utc, year_utc=year_utc)
        if sky is None:
            return
        pt = self.project_universal_stereo(sky[0], sky[1])
        if pt is None:
            return
        target = self.selected_target
        body_radius = 0.0
        if isinstance(target, dict) and target.get("kind") == "sky":
            item = self._lookup_visible_sky_object(target)
            if isinstance(item, dict):
                body_radius = float(item.get("radius_px", 0.0))
        elif isinstance(target, dict) and target.get("kind") == "ngc":
            obj = target.get("obj")
            if obj is not None:
                try:
                    ppd = max(0.02, (min(float(self.width()), float(self.height())) * 0.5 * float(self.zoom_level)) / 90.0)
                    body_radius = max(
                        body_radius,
                        max(4.0, float(getattr(obj, "maj_deg", 0.10) or 0.10) * 0.5 * ppd),
                    )
                except Exception:
                    log_suppressed_exception(__name__, "CanvasSelectionAndTrailsMixin._draw_selected_target_marker")
        cx = float(pt[0])
        cy = float(pt[1])
        phase = 0.5 * (math.sin(time.time() * 3.2) + 1.0)
        base_r = max(8.0, body_radius + 5.0)
        r = base_r + 3.0 * phase
        alpha = int(70 + 90 * (1.0 - phase))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, max(20, alpha // 2)), 2.0))
        painter.drawEllipse(QPointF(cx, cy), r + 2.0, r + 2.0)
        painter.setPen(QPen(QColor(255, 255, 255, alpha), 1.0))
        painter.drawEllipse(QPointF(cx, cy), r, r)
        painter.restore()

    def _apply_scope_selected_target_tracking(self, ut_hour: float, day_of_year_utc: int):
        # In scope mode, selected targets are tracked: camera and scope center follow sky motion.
        if (not self.scope_mode_enabled()) or self.selected_target is None:
            return
        # Never fight direct user input; give manual scope interaction priority.
        if self._scope_motion_active():
            return
        _, _, year_utc, _ = self._get_current_utc_context()
        sky = self._selected_target_sky_position(ut_hour, day_of_year_utc, year_utc=year_utc)
        if sky is None:
            return
        alt, az = sky
        if self.scope_reticle_lock_to_target:
            self.scope_controller.set_center((alt, az))
        # Camera lock is optional; user can disable it with Ctrl+drag while keeping tracking.
        if self.scope_camera_lock_to_target and not self.dragging:
            self.azimuth_offset = az
            self.elevation_angle = max(-90.0, min(90.0, alt))

    def _scope_wheel_zoom(self, steps: float) -> None:
        if not self.scope_mode_enabled():
            return
        if abs(steps) < 1e-9:
            return
        self._mark_scope_interaction(0.25)
        try:
            self.parent_widget._scope_index_suspend_until = time.monotonic() + 0.35
        except Exception:
            log_suppressed_exception(__name__, "CanvasSelectionAndTrailsMixin._scope_wheel_zoom")
        current = max(1.0, float(getattr(self.scope_controller, "focal_mm", 250.0)))
        factor = 1.12 ** steps  # wheel up => larger focal => narrower FOV
        new_focal = max(1.0, min(20000.0, current * factor))
        self.scope_controller.set_focal_mm(new_focal)
        # Keep controls panel in sync with wheel zoom.
        if hasattr(self.parent_widget, "sync_scope_focal_ui"):
            self.parent_widget.sync_scope_focal_ui(new_focal)
        if hasattr(self, 'hint_overlay') and not self.scope_mode_enabled():
            fov_w, fov_h = self.scope_controller.current_fov()
            fov_label = f"{fov_w:.2f}° x {fov_h:.2f}°"
            txt = getTraduction("Scope.ZoomHint", "Scope {focal} mm  ·  FOV {fov}").format(
                focal=f"{new_focal:.1f}",
                fov=fov_label,
            )
            self.hint_overlay.show_hint(txt)
        self.update()

    def _camera_wheel_zoom(self, steps: float) -> None:
        if abs(steps) < 1e-9:
            return
        self._mark_camera_interaction(0.20)
        if self.scope_mode_enabled():
            self._mark_scope_interaction(0.25)
        factor = 1.1 ** steps
        self.zoom_level *= factor
        # Allow deeper zoom for high-focal framing and dense star fields.
        self.zoom_level = max(0.5, min(140.0, self.zoom_level))
        # Keep cached trail image during movement to avoid flickering
        # The Fast-Path will draw on top.
        self.update()
        # Toast HUD: show FOV and 35mm-equivalent focal of current camera zoom.
        if hasattr(self, 'hint_overlay') and not self.scope_mode_enabled():
            fov_deg = 100.0 / self.zoom_level
            focal_eq = 1
            if fov_deg < 179.0:
                focal_rad = math.radians(fov_deg)
                focal_eq = max(1, int(round(18.0 / math.tan(focal_rad / 2.0))))
            txt = getTraduction("HUD.ZoomHint", "FOV {fov}°  ·  {focal}mm").format(
                fov=f"{fov_deg:.1f}", focal=focal_eq
            )
            self.hint_overlay.show_hint(txt)

    def keyPressEvent(self, event):
        return self._input_handler.handle_key_press(event)

    def keyReleaseEvent(self, event):
        return self._input_handler.handle_key_release(event)

    def wheelEvent(self, event):
        return self._input_handler.handle_wheel(event)

    def log_positions(self):
        return canvas_log_positions(self)

    def paintEvent(self, event):
        return canvas_paintEvent(self, event)

    def get_sun_alt_az(self, hour, lat, day_of_year):
        # Try cache first, but only if it matches full observer context.
        if hasattr(self, '_sf_cache') and isinstance(self._sf_cache, dict) and self._sf_cache.get('data'):
            cache_t = float(self._sf_cache.get('time', -1.0))
            cache_day = self._sf_cache.get('day', None)
            cache_year = self._sf_cache.get('year', None)
            cache_lat = self._sf_cache.get('lat', None)
            cache_lon = self._sf_cache.get('lon', None)
            try:
                same_geo = (
                    abs(float(cache_lat) - float(self.parent_widget.latitude)) < 1e-9
                    and abs(float(cache_lon) - float(self.parent_widget.longitude)) < 1e-9
                )
            except Exception:
                same_geo = False
            same_day = cache_day == int(day_of_year)
            same_year = cache_year == int(getattr(self.parent_widget, 'manual_year', datetime.now().year))
            if abs(float(hour) - cache_t) < 0.2 and same_day and same_year and same_geo:
                s = self._sf_cache['data'].get('sun', {})
                if isinstance(s, dict) and ('alt' in s) and ('az' in s):
                    return float(s['alt']), float(s['az'])
        return spherical_get_sun_alt_az(float(hour), float(lat), int(day_of_year))

    def draw_analytic_trails(self, painter, start_hour, end_hour):
        return render_draw_analytic_trails(self, painter, start_hour, end_hour)

    def draw_analytic_trails_numpy(self, painter, start_hour, end_hour, diff, n_steps, limit, is_moving):
        return render_draw_analytic_trails_numpy(
            self,
            painter,
            start_hour,
            end_hour,
            diff,
            n_steps,
            limit,
            is_moving,
        )

