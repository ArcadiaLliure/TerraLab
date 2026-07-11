# -*- coding: latin-1 -*-
import math
import os
import sys
import time
import random
import json
import unicodedata
from pathlib import Path
from typing import Optional
try:
    import numpy as np
except ImportError:
    np = None
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
try:
    from dateutil import tz as dateutil_tz
except Exception:
    dateutil_tz = None
try:
    from timezonefinder import TimezoneFinder
except Exception:
    TimezoneFinder = None
_TIMEZONE_FINDER = TimezoneFinder() if TimezoneFinder is not None else None
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QSlider, QLineEdit, QPushButton, QFrame,
                             QSizePolicy, QCheckBox, QGridLayout, QDialog, QCalendarWidget, QApplication, QGroupBox, QMenu, QMessageBox, QInputDialog, QShortcut)
from PyQt5.QtCore import Qt, QTimer, QPointF, QRectF, pyqtSignal, pyqtSlot, QObject, QThread, QLineF, QUrl, QEvent, QMetaObject
from PyQt5.QtGui import QPainter, QColor, QPen, QRadialGradient, QBrush, QPainterPath, QLinearGradient, QPixmap, QFont, QTransform, QImage, QPolygonF, QDesktopServices
from TerraLab.common.custom_widget_base import CustomWidgetBase
from TerraLab.common.perf_events import append_perf_event
from TerraLab.common.utils import (
    resource_path,
    getTraduction,
    get_config_value,
    set_config_value,
    get_base_dir,
)
from TerraLab.common.app_paths import constellations_path
from TerraLab.data.assets_manager import AssetManager
from TerraLab.ui.canvas_input_handler import CanvasInputHandler
from TerraLab.ui.canvas_selection import CanvasSelection
from TerraLab.ui.widget_init_helpers import (
    astro_canvas_init,
    astronomical_widget_init,
)
from TerraLab.ui.canvas_runtime_helpers import (
    canvas_update_skyfield_cache,
    canvas_scope_hud_star_count,
    canvas_set_selected_target,
    canvas_scope_hud_extra_lines,
    canvas_log_positions,
    canvas_paintEvent,
)
from TerraLab.ui.scope_preload_worker import ScopeFullPreloadWorker
from TerraLab.ui.onboarding_dialogs import AssetOnboardingDialog, WelcomeOnboardingDialog
from TerraLab.astro.search_engine import (
    AstroSearchEngine,
    build_search_index_for_widget,
    center_on_object_for_widget,
    load_named_star_entries,
    load_ngc_entries,
    on_search_triggered_for_widget,
)
from TerraLab.weather.system import (WeatherSystem, WeatherPalette,
                            WeatherControlWidget, Cloud, Particle)
from TerraLab.layers.village import VillageOverlay
from TerraLab.terrain.overlay import HorizonOverlay
from TerraLab.widgets.telescope_scope_mode import TelescopeScopeController
from TerraLab.widgets.measurement_tools import (
    MeasurementController,
    TOOL_NONE,
    TOOL_RULER,
    TOOL_SQUARE,
    TOOL_RECTANGLE,
    TOOL_CIRCLE,
)
from TerraLab.widgets.constellation_drawing import ConstellationDrawingController
from TerraLab.widgets.scope_ui_manager import ScopeUIManager
from TerraLab.ui.widget_controls_builder import build_deferred_controls_ui
from TerraLab.ui.widget_bootstrap_helpers import (
    widget_start_scope_full_preload_async,
    widget_start_async_bootstrap,
    widget_on_catalog_ready,
    widget_ensure_scope_catalog_loaded,
    widget_ensure_scope_spatial_index_warmup,
    widget_on_scope_extension_ready,
)
from TerraLab.ui.widget_misc_helpers import (
    widget_apply_scope_preloaded_spatial_index,
    widget_on_scope_preload_ready,
    widget_maybe_resume_pending_gaia_download,
    widget_reload_star_catalog_async,
    widget_on_scope_spatial_index_ready,
    widget_update_custom_theme,
    widget_set_scope_coord_inputs,
    widget_sync_constellation_controls,
    widget_refresh_milkyway_status_indicator,
    widget_refresh_climate_status_indicator,
    widget_ensure_copernicus_credentials_prompt,
)
from TerraLab.ui.widget_runtime_helpers import (
    open_calendar as widget_open_calendar,
    recompute_visual_magnitude_model as widget_recompute_visual_magnitude_model,
    request_relocation as widget_request_relocation,
    run_smoke_scenes as widget_run_smoke_scenes,
    load_catalog as widget_load_catalog,
    widget_update_loop,
)
from TerraLab.widgets.spherical_math import (
    altaz_to_ra_dec,
    calculate_sun_times as spherical_calculate_sun_times,
    get_sun_alt_az as spherical_get_sun_alt_az,
    gmst_deg as spherical_gmst_deg,
    julian_day as spherical_julian_day,
    lst_deg as spherical_lst_deg,
    ra_dec_to_alt_az,
    screen_to_sky,
)
from TerraLab.widgets.visual_magnitude_engine import (
    VisualMagnitudeEngine,
    VisualMagnitudeInputs,
)
from TerraLab.widgets.telescope_runtime import (
    update_telescope_hud,
    on_telescope_view_enabled,
    on_resize as telescope_on_resize,
    update_star_rendering_params,
)
from TerraLab.debug.diagnostics import Diagnostics
from TerraLab.render.overlays_renderer import (
    draw_compass as render_draw_compass,
    draw_compass_impl as render_draw_compass_impl,
    draw_light_domes as render_draw_light_domes,
    draw_light_domes_impl as render_draw_light_domes_impl,
    draw_moon_skyfield as render_draw_moon_skyfield,
    draw_moon_skyfield_impl as render_draw_moon_skyfield_impl,
    draw_ngc_overlay as render_draw_ngc_overlay,
    draw_ngc_overlay_impl as render_draw_ngc_overlay_impl,
    get_eclipse_dimming_factor as render_get_eclipse_dimming_factor,
    get_eclipse_dimming_factor_impl as render_get_eclipse_dimming_factor_impl,
    get_moon_projection as render_get_moon_projection,
    get_moon_projection_impl as render_get_moon_projection_impl,
    draw_planet as render_draw_planet,
    draw_planet_impl as render_draw_planet_impl,
    draw_satellites as render_draw_satellites,
    draw_satellites_impl as render_draw_satellites_impl,
    draw_single_city_dome as render_draw_single_city_dome,
    draw_skyfield_objects_impl as render_draw_skyfield_objects_impl,
    draw_sun_skyfield as render_draw_sun_skyfield,
    draw_sun_skyfield_impl as render_draw_sun_skyfield_impl,
    ngc_symbol_for_type as render_ngc_symbol_for_type,
)
from TerraLab.render.grid_renderer import (
    draw_celestial_grid as render_draw_celestial_grid,
    draw_celestial_grid_impl as render_draw_celestial_grid_impl,
)
from TerraLab.render.horizon_renderer import (
    draw_ground_mask as render_draw_ground_mask,
    draw_ground_mask_impl as render_draw_ground_mask_impl,
)
from TerraLab.render.sky_renderer import (
    SkyRenderer,
    draw_background as render_draw_background,
    draw_background_impl as render_draw_background_impl,
    sky_color_phys as render_sky_color_phys,
    sky_color_phys_impl as render_sky_color_phys_impl,
)
from TerraLab.render.stars_renderer import (
    draw_analytic_trails_impl as render_draw_analytic_trails_impl,
    draw_analytic_trails_numpy_impl as render_draw_analytic_trails_numpy_impl,
    draw_scope_solar_disc as render_draw_scope_solar_disc,
    draw_scope_solar_disc_impl as render_draw_scope_solar_disc_impl,
    get_scope_solar_pattern as render_get_scope_solar_pattern,
    get_scope_solar_pattern_impl as render_get_scope_solar_pattern_impl,
)
from TerraLab.scene.camera import Camera
from TerraLab.scene.projection import (
    project_universal_stereo_numpy,
    project_universal_stereo_point,
    unproject_universal_stereo_point,
)
from TerraLab.scene.render_context import RenderContext
from TerraLab.scene.scene_state import SceneState, build_star_scene_state
from TerraLab.util.math2d import clamp
# Skyfield imports
try:
    from skyfield.api import load, wgs84, N, W, E, S
    from skyfield import almanac
    from skyfield.framelib import ecliptic_frame
    SKYFIELD_AVAILABLE = True
except ImportError:
    SKYFIELD_AVAILABLE = False
    print("WARNING: Skyfield not available. Install with: pip install skyfield")
from TerraLab.widgets.sky_legacy_components import (
    STAR_CATALOG_NAKED_EYE_MAX_MAG,
    _discover_star_catalog_npz_entries,
    _select_base_star_catalog_entry,
    _load_star_npz_arrays,
    _bp_rp_to_rgb_arrays,
    _build_celestial_objects_from_arrays,
    ClickableLabel,
    AstroEngine,
    RusticTimeBar,
    CatalogLoaderWorker,
    SkyfieldLoaderWorker,
    StarRenderWorker,
)
class ScopeIndexWarmWorker(QObject):
    ready = pyqtSignal(object, object, float)
    error = pyqtSignal(str)
    @pyqtSlot(object, object, object, float)
    def build(self, ra_all, dec_all, mag_all, max_mag):
        try:
            from TerraLab.render.stars_renderer import build_scope_spatial_index_payload
            sorted_indices, offsets = build_scope_spatial_index_payload(
                ra_all,
                dec_all,
                mag_all=mag_all,
                max_mag=float(max_mag),
            )
            self.ready.emit(sorted_indices, offsets, float(max_mag))
        except Exception as exc:
            self.error.emit(str(exc))
class AstroCanvas(QWidget):
    request_render_signal = pyqtSignal(dict)
    request_trails_signal = pyqtSignal(dict)
    def __init__(self, parent):
        return astro_canvas_init(self, parent)
    def _parent_checkbox_checked(self, attr_name: str, default: bool = False) -> bool:
        widget = getattr(self.parent_widget, attr_name, None)
        if widget is None:
            return bool(default)
        try:
            return bool(widget.isChecked())
        except Exception:
            return bool(default)
    def reset_zoom_human(self):
        """Reset zoom to human eye equivalent (43mm)."""
        # Based on research article (Fotoruanopro): 43mm is the true normal.
        f_human = 43.0
        width_sensor = 36.0
        # Calculate horizontal FOV for 36mm sensor and 43mm lens
        # FOV_horiz = 2 * atan(36 / (2 * 43))
        fov_rad = 2.0 * math.atan(width_sensor / (2.0 * f_human))
        fov_deg = math.degrees(fov_rad)
        # Logic: current_fov = base_fov / zoom
        # zoom = base_fov / target_fov
        if fov_deg > 0:
            target_zoom = 100.0 / fov_deg
            self.zoom_level = target_zoom
            self.update()
    def update_skyfield_cache(self, ut_hour, day_of_year):
        return canvas_update_skyfield_cache(self, ut_hour, day_of_year)
    def unproject_stereo(self, sx, sy):
        self._sync_camera_state()
        return unproject_universal_stereo_point(
            float(sx),
            float(sy),
            int(self.width()),
            int(self.height()),
            self.camera,
        )
    def _anim_azimuth_step(self):
        if self.target_azimuth is None:
            self.azimuth_anim_timer.stop()
            return
        current = self.azimuth_offset
        target = self.target_azimuth
        # Shortest path logic
        diff = (target - current + 180) % 360 - 180
        if abs(diff) < 0.1:
            self.azimuth_offset = target
            self.target_azimuth = None
            self.azimuth_anim_timer.stop()
        else:
            # Easing
            step = diff * 0.1
            self.azimuth_offset = (current + step)
        self.update()
    def resizeEvent(self, event):
        self._bg_cache_key = None
        self.weather.resize(self.width(), self.height())
        super().resizeEvent(event)
    def scope_mode_enabled(self) -> bool:
        return bool(getattr(self.scope_controller, "enabled", False))
    def measurement_tool_active(self) -> bool:
        return getattr(self.measurement_controller, "active_tool", TOOL_NONE) != TOOL_NONE
    def drawing_mode_enabled(self) -> bool:
        return bool(getattr(self.constellation_controller, "enabled", False))
    def constellation_visible(self) -> bool:
        return bool(getattr(self.constellation_controller, "visible", True))
    def set_scope_enabled(self, enabled: bool) -> None:
        self.finish_inline_constellation_rename(apply=True)
        if enabled:
            if getattr(self.scope_controller, "center", None) is None:
                # Prevent "empty sky" on activation: scope starts centered on current camera.
                self.scope_controller.set_center((float(self.elevation_angle), float(self.azimuth_offset)))
            self.scope_controller.activate()
            self._sync_scope_zoom_from_optics()
            if hasattr(self, "hint_overlay"):
                self.hint_overlay.hide()
        else:
            self.scope_controller.deactivate()
            self._scope_pressed_keys.clear()
            self._scope_move_timer.stop()
        self._refresh_overlay_cursor()
        self._update_selection_pulse_timer()
        self.update()
    def set_scope_shape(self, shape: str) -> None:
        self.scope_controller.set_shape(shape)
        self.update()
    def set_scope_speed_mode(self, mode: str) -> None:
        self.scope_controller.set_speed_mode(mode)
        self.update()
    def _sync_scope_zoom_from_optics(self) -> None:
        if not self.scope_mode_enabled():
            return
        try:
            fov_w, fov_h = self.scope_controller.current_fov()
            target_fov = max(0.2, min(93.9, max(float(fov_w), float(fov_h))))
            target_zoom = max(0.5, min(140.0, 93.9 / target_fov))
        except Exception:
            return
        if abs(float(self.zoom_level) - float(target_zoom)) > 1e-4:
            self.zoom_level = float(target_zoom)
            self._cached_star_image = None
            self._cached_trail_image = None
    def set_scope_focal_mm(self, focal_mm: float) -> None:
        self.scope_controller.set_focal_mm(focal_mm)
        self._sync_scope_zoom_from_optics()
        self.update()
    def set_scope_sensor(self, sensor_key: str) -> None:
        self.scope_controller.set_sensor_key(sensor_key)
        self._sync_scope_zoom_from_optics()
        self.update()
    def set_scope_aspect_ratio(self, ratio):
        self.scope_controller.set_aspect_ratio(ratio)
        self._sync_scope_zoom_from_optics()
        self.update()
    def set_measurement_tool(self, tool: str) -> None:
        self.finish_inline_constellation_rename(apply=True)
        self.measurement_controller.set_tool(tool)
        self._refresh_overlay_cursor()
        self.update()
    def set_constellation_draw_mode(self, enabled: bool) -> None:
        self.finish_inline_constellation_rename(apply=True)
        if bool(enabled) and (not self.constellation_visible()):
            self.constellation_controller.set_visible(True)
        self.constellation_controller.set_enabled(bool(enabled))
        # Eraser mode is deprecated in UI; keep it hard-disabled to avoid stale runtime states.
        self.constellation_controller.set_eraser_mode(False)
        self._refresh_overlay_cursor()
        self.update()
    def set_constellation_visible(self, visible: bool) -> None:
        self.finish_inline_constellation_rename(apply=True)
        self.constellation_controller.set_visible(bool(visible))
        if not bool(visible):
            self.constellation_controller.set_enabled(False)
            self.constellation_controller.set_eraser_mode(False)
        self._refresh_overlay_cursor()
        self.update()
    def set_constellation_eraser_mode(self, enabled: bool) -> None:
        self.constellation_controller.set_eraser_mode(bool(enabled))
        self.update()
    def create_constellation_group(self, name: Optional[str] = None) -> None:
        self.constellation_controller.create_group(name=name)
        self.update()
    def rename_active_constellation(self, name: str) -> bool:
        ok = self.constellation_controller.rename_active(name)
        if ok:
            self.update()
        return ok
    def begin_inline_constellation_rename(self, group_index: Optional[int] = None, label_rect: Optional[QRectF] = None) -> bool:
        ctrl = self.constellation_controller
        gi = ctrl.active_group_index if group_index is None else int(group_index)
        if gi is None or not (0 <= int(gi) < len(ctrl.groups)):
            return False
        gi = int(gi)
        self.finish_inline_constellation_rename(apply=True)
        if label_rect is None:
            label_rect = ctrl.get_label_rect(gi)
        if label_rect is not None:
            rect = QRectF(label_rect)
            box_w = max(120, int(rect.width() + 12.0))
            box_h = max(24, int(rect.height()))
            x = int(round(rect.left() - 6.0))
            y = int(round(rect.top()))
        else:
            box_w = 180
            box_h = 24
            x = int((self.width() - box_w) * 0.5)
            y = max(22, int(self.height() - 120))
        x = max(4, min(max(4, self.width() - box_w - 4), x))
        y = max(4, min(max(4, self.height() - box_h - 4), y))
        editor = QLineEdit(self)
        editor.setGeometry(x, y, box_w, box_h)
        editor.setText(str(ctrl.groups[gi].name))
        editor.selectAll()
        editor.setFocus(Qt.MouseFocusReason)
        editor.setStyleSheet(
            "QLineEdit {"
            "background: rgba(10, 14, 28, 230);"
            "color: #f5faff;"
            "border: 1px solid rgba(140, 205, 255, 220);"
            "border-radius: 5px;"
            "padding: 2px 6px;"
            "}"
        )
        editor.installEventFilter(self)
        editor.returnPressed.connect(lambda: self.finish_inline_constellation_rename(apply=True))
        editor.editingFinished.connect(lambda: self.finish_inline_constellation_rename(apply=True))
        editor.show()
        self._constellation_rename_editor = editor
        self._constellation_rename_group_index = gi
        return True
    def finish_inline_constellation_rename(self, apply: bool = True) -> bool:
        editor = self._constellation_rename_editor
        gi = self._constellation_rename_group_index
        if editor is None:
            return False
        self._constellation_rename_editor = None
        self._constellation_rename_group_index = None
        text = str(editor.text() or "").strip()
        try:
            editor.removeEventFilter(self)
        except Exception:
            pass
        editor.hide()
        editor.deleteLater()
        renamed = False
        if apply and text and gi is not None:
            ctrl = self.constellation_controller
            if 0 <= int(gi) < len(ctrl.groups):
                ctrl.active_group_index = int(gi)
                ctrl.selected_group_index = int(gi)
                ctrl.selected_node_index = None
                ctrl.selected_segment_index = None
                renamed = bool(self.rename_active_constellation(text))
                if hasattr(self.parent_widget, "_sync_constellation_controls"):
                    self.parent_widget._sync_constellation_controls()
        self.setFocus(Qt.MouseFocusReason)
        self.update()
        return renamed
    def eventFilter(self, obj, event):
        if obj is self._constellation_rename_editor:
            if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self.finish_inline_constellation_rename(apply=False)
                return True
            if event.type() == QEvent.FocusOut:
                self.finish_inline_constellation_rename(apply=True)
                return True
        return super().eventFilter(obj, event)
    def clear_measurements(self) -> None:
        self.measurement_controller.clear()
        self._refresh_overlay_cursor()
        self.update()
    def _refresh_overlay_cursor(self):
        if self.scope_mode_enabled() or self.measurement_tool_active() or self.drawing_mode_enabled():
            self.setCursor(Qt.CrossCursor)
        else:
            self.unsetCursor()
    def _scope_move_tick(self):
        if not self.scope_mode_enabled() or not self._scope_pressed_keys:
            self._scope_move_timer.stop()
            return
        now_ms = int(time.time() * 1000)
        dt = max(0.001, (now_ms - self._scope_last_tick_ms) / 1000.0)
        self._scope_last_tick_ms = now_ms
        rate = self.scope_controller.hold_rate_deg_per_s()
        step = rate * dt
        d_alt = 0.0
        d_az = 0.0
        if Qt.Key_Up in self._scope_pressed_keys:
            d_alt += step
        if Qt.Key_Down in self._scope_pressed_keys:
            d_alt -= step
        if Qt.Key_Left in self._scope_pressed_keys:
            d_az += step
        if Qt.Key_Right in self._scope_pressed_keys:
            d_az -= step
        if d_alt != 0.0 or d_az != 0.0:
            self.scope_controller.nudge(d_alt, d_az)
            self._mark_scope_interaction(0.15)
            self.update()
    def _mark_scope_interaction(self, hold_seconds: float = 0.20) -> None:
        try:
            hold = max(0.0, float(hold_seconds))
        except Exception:
            hold = 0.2
        self._scope_interaction_until = max(
            float(getattr(self, "_scope_interaction_until", 0.0)),
            time.monotonic() + hold,
        )
    def _scope_motion_active(self) -> bool:
        if not self.scope_mode_enabled():
            return False
        if bool(getattr(self, "_scope_pressed_keys", None)):
            return True
        if bool(getattr(self, "dragging", False)):
            return True
        try:
            if bool(getattr(self.scope_controller, "dragging", False)):
                return True
        except Exception:
            pass
        return time.monotonic() < float(getattr(self, "_scope_interaction_until", 0.0))
    def _scope_secondary_drag_deg_per_px(self) -> float:
        # Secondary camera drag sensitivity in scope mode follows scope speed mode.
        if self.scope_controller.speed_mode == TelescopeScopeController.SPEED_SLOW:
            return 0.5 / 60.0
        return 0.5
    def _scope_reticle_drag_active(self) -> bool:
        return bool(
            self.scope_mode_enabled()
            and hasattr(self, "scope_controller")
            and bool(getattr(self.scope_controller, "dragging", False))
        )
    def _camera_interaction_active(self, include_time_drag: bool = True, include_animation: bool = False) -> bool:
        # Scope-reticle drag is an overlay interaction; it must not trigger camera/star LOD degradation.
        dragging_time = bool(getattr(self.parent_widget, "_dragging_time", False)) if include_time_drag else False
        anim_active = bool(hasattr(self, "anim_timer") and self.anim_timer.isActive()) if include_animation else False
        return bool((self.dragging or dragging_time or anim_active) and (not self._scope_reticle_drag_active()))
    def _selection_pulse_tick(self):
        if self.selected_target is None or self.scope_mode_enabled():
            if self._selection_pulse_timer.isActive():
                self._selection_pulse_timer.stop()
            return
        self.update()
    def _update_selection_pulse_timer(self):
        should_run = (self.selected_target is not None) and (not self.scope_mode_enabled())
        if should_run and (not self._selection_pulse_timer.isActive()):
            self._selection_pulse_timer.start()
        elif (not should_run) and self._selection_pulse_timer.isActive():
            self._selection_pulse_timer.stop()
    def _normalize_planet_key(self, value):
        return self._selection.normalize_planet_key(value)
    def _extract_star_coords(self, star_obj):
        return self._selection.extract_star_coords(star_obj)
    def _register_visible_sky_object(
        self,
        obj_type: str,
        key: str,
        name: str,
        alt: float,
        az: float,
        sx: float,
        sy: float,
        radius_px: float,
        *,
        mag: float | None = None,
    ):
        try:
            item = {
                "type": str(obj_type or "").lower(),
                "key": str(key or "").strip(),
                "name": str(name or "").strip(),
                "alt": float(alt),
                "az": float(az) % 360.0,
                "sx": float(sx),
                "sy": float(sy),
                "radius_px": float(max(0.0, radius_px)),
            }
            if mag is not None:
                item["mag"] = float(mag)
            self.visible_sky_objects.append(item)
        except Exception:
            return
    def _lookup_visible_sky_object(self, target):
        if not isinstance(target, dict):
            return None
        if str(target.get("kind", "")).lower() != "sky":
            return None
        wanted_type = str(target.get("type", "")).lower()
        wanted_key = self._normalize_planet_key(target.get("key", ""))
        best = None
        best_score = float("inf")
        for item in getattr(self, "visible_sky_objects", []):
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "")).lower()
            if item_type != wanted_type:
                continue
            if wanted_type == "planet":
                item_key = self._normalize_planet_key(item.get("key", "") or item.get("name", ""))
                if wanted_key and item_key and item_key != wanted_key:
                    continue
            score = 0.0
            try:
                t_alt = float(target.get("alt", item.get("alt", 0.0)))
                t_az = float(target.get("az", item.get("az", 0.0))) % 360.0
                i_alt = float(item.get("alt", 0.0))
                i_az = float(item.get("az", 0.0)) % 360.0
                d_az = abs(((i_az - t_az + 180.0) % 360.0) - 180.0)
                score = abs(i_alt - t_alt) + d_az
            except Exception:
                pass
            if score < best_score:
                best_score = score
                best = item
        return best
    def _resolve_star_by_index(self, star_ref):
        if isinstance(star_ref, dict):
            return star_ref if self._extract_star_coords(star_ref) is not None else None
        try:
            idx = int(star_ref)
        except Exception:
            return None
        pw = getattr(self, "parent_widget", None)
        if pw is None:
            return None
        ra_all = getattr(pw, "np_ra", None)
        dec_all = getattr(pw, "np_dec", None)
        mag_all = getattr(pw, "np_mag", None)
        if ra_all is None or dec_all is None or mag_all is None:
            return None
        try:
            n = min(len(ra_all), len(dec_all), len(mag_all))
        except Exception:
            return None
        if idx < 0 or idx >= n:
            return None
        try:
            ra = float(ra_all[idx])
            dec = float(dec_all[idx])
            mag = float(mag_all[idx])
        except Exception:
            return None
        if (not math.isfinite(ra)) or (not math.isfinite(dec)) or (not math.isfinite(mag)):
            return None
        star = {
            "id": int(idx),
            "ra": ra % 360.0,
            "dec": max(-90.0, min(90.0, dec)),
            "mag": mag,
        }
        bp_all = getattr(pw, "np_bp_rp", None)
        try:
            if bp_all is not None and idx < len(bp_all):
                bp = float(bp_all[idx])
                if math.isfinite(bp):
                    star["bp_rp"] = bp
        except Exception:
            pass
        for ch in ("r", "g", "b"):
            arr = getattr(pw, f"np_{ch}", None)
            try:
                if arr is not None and idx < len(arr):
                    val = float(arr[idx])
                    if math.isfinite(val):
                        star[ch] = val
            except Exception:
                pass
        return star
    def _pick_sky_object_at(self, sx: float, sy: float, click_radius: float = 20.0):
        return self._selection.pick_sky_object_at(float(sx), float(sy), float(click_radius))
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

        if tz_name and ZoneInfo is not None:
            try:
                tzinfo = ZoneInfo(tz_name)
            except Exception:
                tzinfo = None

        # Fallback with DST-aware rules when zoneinfo data is unavailable.
        if tzinfo is None and tz_name and dateutil_tz is not None:
            try:
                tzinfo = dateutil_tz.gettz(tz_name)
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
    def _set_selected_star(self, star_obj):
        # Backward-compatible wrapper.
        if star_obj is None:
            self._set_selected_target(None)
        else:
            self._set_selected_target({"kind": "star", "star": star_obj})
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
                    pass
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
    def _draw_selected_star_marker(self, painter: QPainter, ut_hour: float, day_of_year_utc: int):
        # Backward-compatible wrapper.
        self._draw_selected_target_marker(painter, ut_hour, day_of_year_utc)
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
    def _apply_scope_selected_star_tracking(self, ut_hour: float, day_of_year_utc: int):
        # Backward-compatible wrapper.
        self._apply_scope_selected_target_tracking(ut_hour, day_of_year_utc)
    def _scope_wheel_zoom(self, steps: float) -> None:
        if not self.scope_mode_enabled():
            return
        if abs(steps) < 1e-9:
            return
        self._mark_scope_interaction(0.25)
        try:
            self.parent_widget._scope_index_suspend_until = time.monotonic() + 0.35
        except Exception:
            pass
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
        if hasattr(self, '_sf_cache') and isinstance(self._sf_cache, dict) and self._sf_cache.get('data') and SKYFIELD_AVAILABLE:
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
        return render_draw_analytic_trails_impl(self, painter, start_hour, end_hour)
    def draw_analytic_trails_numpy(self, painter, start_hour, end_hour, diff, n_steps, limit, is_moving):
        return render_draw_analytic_trails_numpy_impl(
            self,
            painter,
            start_hour,
            end_hour,
            diff,
            n_steps,
            limit,
            is_moving,
        )
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
# ... (inside AstronomicalWidget)
    def calculate_sun_times(self, lat, day_of_year):
        return spherical_calculate_sun_times(float(lat), int(day_of_year))
    def sky_color_phys(self, view_alt, view_az, sun_alt, sun_az, bortle=1, twilight_factor=1.0):
        return render_sky_color_phys(
            view_alt,
            view_az,
            sun_alt,
            sun_az,
            bortle=bortle,
            twilight_factor=twilight_factor,
            impl=self._sky_color_phys_impl,
        )
    def _sky_color_phys_impl(self, view_alt, view_az, sun_alt, sun_az, bortle=1, twilight_factor=1.0):
        return render_sky_color_phys_impl(
            view_alt,
            view_az,
            sun_alt,
            sun_az,
            bortle=bortle,
            twilight_factor=twilight_factor,
        )
    def draw_background(self, painter, sun_alt, sun_az, view_az, dimming=1.0):
        return render_draw_background(
            painter,
            sun_alt,
            sun_az,
            view_az,
            dimming=dimming,
            impl=self._draw_background_impl,
        )
    def _draw_background_impl(self, painter, sun_alt, sun_az, view_az, dimming=1.0):
        return render_draw_background_impl(
            self,
            painter,
            sun_alt,
            sun_az,
            view_az,
            dimming=dimming,
        )
    def draw_celestial_grid(self, painter, hour):
        return render_draw_celestial_grid(
            painter,
            hour,
            impl=self._draw_celestial_grid_impl,
        )
    def _draw_celestial_grid_impl(self, painter, hour):
        return render_draw_celestial_grid_impl(self, painter, hour)
    def _on_star_result(self, img, vis_stars):
        self._cached_star_image = img
        self.visible_stars = vis_stars # Update interactions
        if np is not None and isinstance(vis_stars, list) and vis_stars and isinstance(vis_stars[0], tuple):
            try:
                self.visible_stars_sx = np.array([float(v[0]) for v in vis_stars], dtype=np.float32)
                self.visible_stars_sy = np.array([float(v[1]) for v in vis_stars], dtype=np.float32)
            except Exception:
                self.visible_stars_sx = np.array([], dtype=np.float32)
                self.visible_stars_sy = np.array([], dtype=np.float32)
        elif np is not None:
            self.visible_stars_sx = np.array([], dtype=np.float32)
            self.visible_stars_sy = np.array([], dtype=np.float32)
        self.rendering_busy = False
        self.update() # Force repaint to show new stars immediately
    def _on_trail_result(self, img):
        self._cached_trail_image = img
        self.trail_rendering_busy = False
    def draw_stars(self, painter, hour, sun_alt, sun_az, for_trails=False, visibility_factor=1.0, moon_mask=None, mag_limit=None, eff_lat=None, day_of_year=None):
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
    def _build_star_scene_state(self, hour, sun_alt, sun_az, mag_limit, eff_lat, day_of_year):
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
        renderer = getattr(getattr(self, "sky_renderer", None), "milkyway_overlay", None)
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
        flip_txt = ("vf=1" if lat_flip else "vf=0") + (" hf=1" if lon_flip else " hf=0")
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
    def draw_stars_numpy(self, painter, hour, sun_alt, sun_az, mag_limit=None, eff_lat=None, day_of_year=None):
        if np is None:
            self.visible_stars = []
            self.visible_stars_sx = np.array([], dtype=np.float32) if np is not None else []
            self.visible_stars_sy = np.array([], dtype=np.float32) if np is not None else []
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
            return QColor(160, 190, 255) # Blue
        elif bp_rp < 0.5:
            t = (bp_rp - 0.0) / 0.5
            return QColor(160 + int(95*t), 190 + int(65*t), 255)
        elif bp_rp < 1.0:
            t = (bp_rp - 0.5) / 0.5
            return QColor(255, 255, 255 - int(55*t))
        elif bp_rp < 2.0:
            t = (bp_rp - 1.0) / 1.0
            return QColor(255, 255 - int(80*t), 200 - int(100*t))
        else:
            return QColor(255, 175, 100) # Red
    # draw_sun removed (Legacy)
    # get_horizon_size_multiplier removed (Superseded)
    # --- Accurate Time & LST Methods ---
    def get_datetime_utc(self, ut_hour):
        # Construct UTC datetime from manual year/day/hour
        y = self.parent_widget.manual_year
        d = self.parent_widget.manual_day
        dt_start = datetime(y, 1, 1, tzinfo=timezone.utc)
        return dt_start + timedelta(days=d, hours=ut_hour)
    def julian_day(self, dt):
        return spherical_julian_day(dt)
    def days_since_j2000(self, ut_hour):
        # Deprecated in favor of direct JD usage; kept for backward compatibility
        dt = self.get_datetime_utc(ut_hour)
        jd = self.julian_day(dt)
        return jd - 2451545.0
    # REMOVED manual algorithm julian_day to avoid bugs.
    def gmst_deg(self, jd):
        return spherical_gmst_deg(float(jd))
    def lst_deg(self, jd, lon_deg):
        return spherical_lst_deg(float(jd), float(lon_deg))
    def sun_alt_az_from_ra_dec(self, ra, dec, lat, lst):
        # Standard conversion helper
        ha = (lst - ra)
        ha_rad = math.radians(ha)
        lat_rad = math.radians(lat)
        dec_rad = math.radians(dec)
        sin_lat = math.sin(lat_rad)
        cos_lat = math.cos(lat_rad)
        sin_dec = math.sin(dec_rad)
        cos_dec = math.cos(dec_rad)
        sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * math.cos(ha_rad)
        alt = math.degrees(math.asin(max(-1, min(1, sin_alt))))
        cos_az = (sin_dec - sin_alt * sin_lat) / (math.cos(math.radians(alt)) * cos_lat + 1e-10)
        az = math.degrees(math.acos(max(-1, min(1, cos_az))))
        if math.sin(ha_rad) > 0: az = 360 - az
        return alt, az
    def get_sun_ra_dec(self, d):
        # Precise Sun (Meeus Ch 25)
        # Wraps AstroEngine for Sun
        # d is Days since J2000 UTC.
        # Add Delta T (72s) for TDB based T
        d_tdb = d + (72.0 / 86400.0)
        T = d_tdb / 36525.0
        l, b, r = AstroEngine.get_sun_position_vsop(T)
        ra, dec = AstroEngine.ecliptic_to_equatorial(l, b, T)
        return ra, dec
    def get_moon_ra_dec(self, d):
        # Wraps AstroEngine for Moon
        d_tdb = d + (72.0 / 86400.0)
        T = d_tdb / 36525.0
        l, b, r = AstroEngine.get_moon_position_elp(T)
        ra, dec = AstroEngine.ecliptic_to_equatorial(l, b, T)
        return ra, dec, r
    def get_topocentric_position(self, ra_geo, dec_geo, dist_km, obs_lat, obs_lon, jd):
        return AstroEngine.get_topocentric_position(ra_geo, dec_geo, dist_km, obs_lat, obs_lon, jd)
    def get_moon_projection(self, hour):
        return render_get_moon_projection(
            hour,
            impl=self._get_moon_projection_impl,
        )
    def _get_moon_projection_impl(self, hour):
        return render_get_moon_projection_impl(self, hour)
    # draw_moon removed (Legacy)
    # moon_visibility_alpha removed (Legacy)
    # _render_moon_pixmap removed (Legacy)
    def draw_ground_mask(self, painter, is_day):
        return render_draw_ground_mask(
            painter,
            is_day,
            impl=self._draw_ground_mask_impl,
        )
    def _draw_ground_mask_impl(self, painter, is_day):
        return render_draw_ground_mask_impl(self, painter, is_day)
    def draw_compass(self, painter):
        return render_draw_compass(painter, impl=self._draw_compass_impl)
    def _draw_compass_impl(self, painter):
        return render_draw_compass_impl(self, painter)
    def _ngc_symbol_for_type(self, obj_type: str) -> str:
        return render_ngc_symbol_for_type(obj_type)
    def draw_ngc_overlay(self, painter: QPainter, ut_hour: float, day_of_year_utc: int) -> None:
        return render_draw_ngc_overlay(
            painter,
            ut_hour,
            day_of_year_utc,
            impl=self._draw_ngc_overlay_impl,
        )
    def _draw_ngc_overlay_impl(self, painter: QPainter, ut_hour: float, day_of_year_utc: int) -> None:
        return render_draw_ngc_overlay_impl(self, painter, ut_hour, day_of_year_utc)
    # draw_planets removed (Unused)
    def draw_satellites(self, painter, ut_hour):
        return render_draw_satellites(
            painter,
            ut_hour,
            impl=self._draw_satellites_impl,
        )
    def _draw_satellites_impl(self, painter, ut_hour):
        return render_draw_satellites_impl(self, painter, ut_hour)
    def mousePressEvent(self, event):
        return self._input_handler.handle_mouse_press(event)
    def mouseMoveEvent(self, event):
        return self._input_handler.handle_mouse_move(event)
    def mouseReleaseEvent(self, event):
        return self._input_handler.handle_mouse_release(event)
    def mouseDoubleClickEvent(self, event):
        return self._input_handler.handle_mouse_double_click(event)
    # --- SKYFIELD INTEGRATION ---
    def get_simulated_tz_offset(self, day_of_year=None):
        try:
            sim_day = int(self.parent_widget.manual_day if day_of_year is None else day_of_year)
            sim_year = int(getattr(self.parent_widget, "manual_year", datetime.now().year))
            local_hour = float(self.parent_widget.get_current_hour())
            dt_local = self._simulated_local_datetime(local_hour, sim_year=sim_year, sim_day=sim_day)
            off = dt_local.utcoffset()
            if off is not None:
                return float(off.total_seconds() / 3600.0)
        except Exception:
            pass
        fallback = datetime.now().astimezone().utcoffset()
        if fallback is None:
            return 0.0
        return float(fallback.total_seconds() / 3600.0)
    def perceived_disc_scale(self, alt_deg, sun_alt_deg=None, is_trained=None, horizon_refs=None, flattening=None, atmos=None, falloff_deg=35.0):
        # Defaults
        if is_trained is None: is_trained = self.trained_observer
        if horizon_refs is None: horizon_refs = self.horizon_refs
        if flattening is None: flattening = self.dome_flattening
        if atmos is None: atmos = self.atmospheric_context
        # Eclipse Lock Check (Blind check relies on caller, but we check global enable)
        if not self.illusion_enabled:
            return 1.0
        # 1. Base Illusion Magnitude
        # lerp(0.22, 0.50, horizon_refs)
        max_illusion = 0.22 + (0.50 - 0.22) * horizon_refs
        # 2. Dome Flattening Boost
        # lerp(1.0, 1.25, flattening)
        max_illusion *= (1.0 + 0.25 * flattening)
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
            painter,
            profile,
            eff_sun_alt,
            eclipse_dimming,
            impl=self._draw_light_domes_impl,
        )
    def _draw_light_domes_impl(self, painter, profile, eff_sun_alt, eclipse_dimming):
        return render_draw_light_domes_impl(self, painter, profile, eff_sun_alt, eclipse_dimming)
    def _draw_single_city_dome(self, painter, profile, idx, dist, twilight_factor):
        return render_draw_single_city_dome(self, painter, profile, idx, dist, twilight_factor)
    def _sun_weather_dim_factor(self) -> float:
        w = None
        if hasattr(self.parent_widget, 'weather'):
            w = self.parent_widget.weather
        elif hasattr(self, 'weather'):
            w = self.weather
        if w is None:
            return 0.0
        try:
            cover = max(0.0, min(1.0, float(getattr(w, 'current_cover', 0.0))))
            precip = max(0.0, min(1.0, float(getattr(w, 'precip_int', 0.0))))
            fog = max(0.0, min(1.0, float(getattr(w, 'fog_cover', 0.0))))
            humidity = max(0.0, min(1.0, float(getattr(w, 'humidity', 0.0))))
            dim_cover = max(0.0, (cover - 0.55) / 0.45)
            dim_fog = min(1.0, fog * 1.10 + max(0.0, humidity - 0.80) * 0.85)
            dim = max(dim_cover, precip * 1.20, dim_fog)
            return max(0.0, min(1.0, dim ** 1.15))
        except Exception:
            return 0.0
    def draw_skyfield_objects(self, painter, ut_hour, day_of_year, ambient_light=1.0, mag_limit=None):
        return render_draw_skyfield_objects_impl(
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
        if hasattr(self, "timer") and self.timer.interval() != target_interval_ms:
            self.timer.setInterval(target_interval_ms)
        if self.use_real_time:
            now = datetime.now()
            # Sync Day & Year
            self.manual_year = now.year
            self.manual_day = (now - datetime(now.year, 1, 1)).days
            if hasattr(self, 'lbl_date'):
               self.lbl_date.setText(self.format_date(self.manual_day))
            # Sync Gradient
            if hasattr(self, 'time_bar'):
                self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
            # Sync Time
            h = now.hour + now.minute/60.0 + now.second/3600.0
            if hasattr(self, 'time_bar'):
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
            if hasattr(self, 'time_bar'):
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
        path.addEllipse(QPointF(0,0), radius, radius)
        return path
    def _get_scope_solar_pattern(self):
        return render_get_scope_solar_pattern(impl=self._get_scope_solar_pattern_impl)
    def _get_scope_solar_pattern_impl(self):
        return render_get_scope_solar_pattern_impl(self)
    def _draw_scope_solar_disc(self, painter, radius):
        return render_draw_scope_solar_disc(painter, radius, impl=self._draw_scope_solar_disc_impl)
    def _draw_scope_solar_disc_impl(self, painter, radius):
        return render_draw_scope_solar_disc_impl(self, painter, radius)
    def draw_sun_skyfield(self, painter, alt, az, radius, color, corona_opacity, pixels_per_deg):
        return render_draw_sun_skyfield(
            painter,
            alt,
            az,
            radius,
            color,
            corona_opacity,
            pixels_per_deg,
            project_fn=self.project_universal_stereo,
            register_sky_object_fn=self._register_visible_sky_object,
            impl=self._draw_sun_skyfield_impl,
        )
    def _draw_sun_skyfield_impl(self, painter, alt, az, radius, color, corona_opacity, pixels_per_deg):
        return render_draw_sun_skyfield_impl(
            self,
            painter,
            alt,
            az,
            radius,
            color,
            corona_opacity,
            pixels_per_deg,
        )
    def draw_moon_skyfield(self, painter, alt, az, illum, rotation_deg, radius, alpha, is_eclipsing=False, is_day=False, sun_params=None, pixels_per_deg=None, tint_color=None):
        return render_draw_moon_skyfield(
            painter,
            alt,
            az,
            illum,
            rotation_deg,
            radius,
            alpha,
            is_eclipsing=is_eclipsing,
            is_day=is_day,
            sun_params=sun_params,
            pixels_per_deg=pixels_per_deg,
            tint_color=tint_color,
            project_fn=self.project_universal_stereo,
            register_sky_object_fn=self._register_visible_sky_object,
            impl=self._draw_moon_skyfield_impl,
        )
    def _draw_moon_skyfield_impl(self, painter, alt, az, illum, rotation_deg, radius, alpha, is_eclipsing=False, is_day=False, sun_params=None, pixels_per_deg=None, tint_color=None):
        return render_draw_moon_skyfield_impl(
            self,
            painter,
            alt,
            az,
            illum,
            rotation_deg,
            radius,
            alpha,
            is_eclipsing=is_eclipsing,
            is_day=is_day,
            sun_params=sun_params,
            pixels_per_deg=pixels_per_deg,
            tint_color=tint_color,
        )
    def draw_planet(self, painter, alt, az, name, col, sz, mag, key=None):
        return render_draw_planet(
            painter,
            alt,
            az,
            name,
            col,
            sz,
            mag,
            key=key,
            project_fn=self.project_universal_stereo,
            register_sky_object_fn=self._register_visible_sky_object,
            impl=self._draw_planet_impl,
        )
    def _draw_planet_impl(self, painter, alt, az, name, col, sz, mag, key=None):
        return render_draw_planet_impl(self, painter, alt, az, name, col, sz, mag, key=key)
    def draw_satellite(self, painter, alt, az, name, mag):
        pt = self.project_universal_stereo(alt, az)
        if not pt: return
        x, y = pt
        painter.setPen(QPen(Qt.red, 2))
        painter.drawPoint(QPointF(x,y))
        painter.setPen(Qt.white)
        painter.drawText(int(x)+5, int(y)-5, f"{name} {mag:.1f}")
    def calculate_planet_magnitude(self, name, d_au, phase):
        # Revised Base Magnitudes (Normalized to ~1 AU distance + Albedo)
        # Formula uses +5*log10(d), so Base must handle the subtraction of distance modulus.
        # e.g. Saturn at 9AU: 5*log(9) = +4.77. Target Mag ~0.5. Base should be -4.3.
        base = {
            'Mercury': -0.6, 'Venus': -4.4,
            'Mars': -0.5, # Adjusted (was -2.0)
            'Jupiter': -5.8, # Adjusted (was -2.7)
            'Saturn': -4.3, # Adjusted (was 0.5)
            'Uranus': -0.7, 'Neptune': 0.5, 'Pluto': 6.0
        }.get(name, 0)
        return base + 5*math.log10(d_au) + 0.01*phase
    def get_eclipse_dimming_factor(self, ut_hour, day_of_year):
        return render_get_eclipse_dimming_factor(
            ut_hour,
            day_of_year,
            impl=self._get_eclipse_dimming_factor_impl,
        )
    def _get_eclipse_dimming_factor_impl(self, ut_hour, day_of_year):
        return render_get_eclipse_dimming_factor_impl(self, ut_hour, day_of_year)
class AstronomicalWidget(CustomWidgetBase):
    request_render_signal = pyqtSignal()
    request_trails_signal = pyqtSignal()
    # Signal to start baking in background thread
    request_horizon_bake = pyqtSignal(object)
    request_horizon_bortle = pyqtSignal(float, float, int)
    def __init__(self, parent=None, **kwargs):
        return astronomical_widget_init(self, parent, **kwargs)
    def _set_scene_load_stage(self, stage: str):
        valid = {"boot", "base_sky", "stars_ready", "horizon_preview", "scene_ready"}
        if stage not in valid:
            return
        if getattr(self, "scene_load_stage", None) == stage:
            return
        self.scene_load_stage = stage
        append_perf_event("scene_stage", stage=str(stage), delta_ms_boot=self._boot_delta_ms())
        if hasattr(self, "canvas"):
            self.canvas.update()
        if stage == "scene_ready":
            self._start_scope_full_preload_async(reason="scene_ready")
            self._ensure_scope_catalog_loaded()
    def _boot_delta_ms(self) -> int:
        return int((time.perf_counter() - float(getattr(self, "_perf_boot_t0_mono", time.perf_counter()))) * 1000.0)
    def _scope_preload_cache_dir(self) -> str:
        root = Path(self.runtime_layout.get("root", get_base_dir()))
        path = root / "cache" / "scope"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    def _scope_preload_should_wait(self) -> bool:
        return bool(
            self.scope_preload_mode == "startup_full"
            and bool(getattr(self, "scope_requires_full_catalog", True))
            and str(getattr(self, "scope_activation_policy", "wait_until_ready")) == "wait_until_ready"
        )
    def _scope_set_data_state(self, new_state: str, *, reason: str = "") -> None:
        state = str(new_state or "").strip().lower()
        if state not in {"ready_deep", "loading_deep", "error_deep"}:
            return
        prev = str(getattr(self, "_scope_data_state", "ready_deep") or "ready_deep")
        if prev == state:
            return
        self._scope_data_state = state
        scope_active = bool(getattr(getattr(self, "canvas", None), "scope_mode_enabled", lambda: False)())
        pending_scope = bool(getattr(self, "_scope_preload_pending_activation", False))
        emit_state_events = bool(scope_active or pending_scope)
        if emit_state_events and state in {"loading_deep", "error_deep"} and prev == "ready_deep":
            append_perf_event(
                "scope_fallback_on",
                state=state,
                reason=str(reason or ""),
                delta_ms_boot=self._boot_delta_ms(),
            )
        elif emit_state_events and prev in {"loading_deep", "error_deep"} and state == "ready_deep":
            append_perf_event(
                "scope_fallback_off",
                reason=str(reason or ""),
                delta_ms_boot=self._boot_delta_ms(),
            )
        if hasattr(self, "canvas"):
            try:
                self.canvas._cached_star_image = None
                self.canvas._cached_trail_image = None
            except Exception:
                pass
            self.canvas.update()
    def _refresh_scope_data_state(self, *, reason: str = "") -> None:
        if bool(getattr(self, "_scope_preload_failed", False)):
            self._scope_set_data_state("error_deep", reason=reason or "preload_error")
            return
        wait_full_ready = bool(
            self._scope_preload_should_wait()
            and (not bool(getattr(self, "_scope_preload_ready", False)))
        )
        subset_only = bool(getattr(self, "_catalog_loaded_subset_only", False))
        if subset_only or wait_full_ready:
            self._scope_set_data_state("loading_deep", reason=reason or "loading")
            return
        self._scope_set_data_state("ready_deep", reason=reason or "ready")
    def _scope_preload_status(self, message: str, *, keep_seconds: float = 0.0):
        self._set_gaia_extension_status_label(f"[Scope preload] {str(message)}", keep_seconds=keep_seconds)
    def _apply_scope_preloaded_spatial_index(self) -> bool:
        return widget_apply_scope_preloaded_spatial_index(self)
    def _finalize_scope_preload_worker_refs(self):
        thread = getattr(self, "_scope_preload_thread", None)
        if thread is not None:
            try:
                if thread.isRunning():
                    return
            except Exception:
                pass
        self._scope_preload_thread = None
        self._scope_preload_worker = None
    def _cleanup_scope_preload_worker(self):
        thread = getattr(self, "_scope_preload_thread", None)
        if thread is None:
            self._finalize_scope_preload_worker_refs()
            return
        try:
            thread.finished.connect(self._finalize_scope_preload_worker_refs)
        except Exception:
            pass
        try:
            if thread.isRunning():
                thread.quit()
                return
        except Exception:
            pass
        self._finalize_scope_preload_worker_refs()
    def _on_scope_preload_progress(self, payload):
        if not isinstance(payload, dict):
            return
        pct = max(0.0, min(100.0, float(payload.get("percent", 0.0))))
        msg = str(payload.get("message", "scope preload")).strip() or "scope preload"
        stage = str(payload.get("stage", "")).strip()
        self._scope_preload_status(f"{msg} ({int(round(pct))}%)")
        last_pct = float(getattr(self, "_scope_preload_last_progress_pct", -1.0))
        if abs(pct - last_pct) >= 1.0 or pct in (0.0, 100.0):
            self._scope_preload_last_progress_pct = pct
            append_perf_event(
                "scope_preload_progress",
                percent=float(pct),
                stage=stage,
                message=msg,
                delta_ms_boot=self._boot_delta_ms(),
            )
    def _on_scope_preload_ready(self, payload):
        return widget_on_scope_preload_ready(self, payload)
    def _on_scope_preload_error(self, message: str):
        self._scope_preload_started = False
        self._scope_preload_in_progress = False
        self._scope_preload_failed = True
        self._scope_preload_ready = False
        self._scope_preload_pending_activation = False
        self._scope_preload_sorted_indices = None
        self._scope_preload_offsets = None
        self._scope_preload_indices_path = ""
        self._scope_preload_offsets_path = ""
        msg = str(message or "unknown preload error")
        print(f"[AstroWidget] Scope preload error: {msg}")
        self._scope_preload_status(f"error: {msg}", keep_seconds=20.0)
        append_perf_event("scope_preload_error", message=msg, delta_ms_boot=self._boot_delta_ms())
        self._scope_preload_rows = 0
        self._refresh_scope_data_state(reason="preload_error")
        self._cleanup_scope_preload_worker()
    def _start_scope_full_preload_async(self, reason: str = "runtime", force_rebuild: bool = False):
        return widget_start_scope_full_preload_async(self, reason, force_rebuild)
    def _create_startup_placeholder(self):
        if hasattr(self, "_startup_placeholder"):
            return
        self._startup_placeholder = QFrame(self)
        self._startup_placeholder.setObjectName("startupPlaceholder")
        self._startup_placeholder.setStyleSheet(
            "QFrame#startupPlaceholder { background-color: #000000; }"
        )
        layout = QVBoxLayout(self._startup_placeholder)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        title = QLabel("TerraLab", self._startup_placeholder)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color: #eef4ff; font-size: 28px; font-weight: bold; background: transparent;")
        subtitle = QLabel("Initializing sky...", self._startup_placeholder)
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: #8ea3c2; font-size: 14px; background: transparent;")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(2)
        self._startup_placeholder.show()
        self._startup_placeholder.raise_()
    def _position_startup_placeholder(self):
        if not hasattr(self, "_startup_placeholder"):
            return
        self._startup_placeholder.setGeometry(self.rect())
        self._startup_placeholder.raise_()
    def _hide_startup_placeholder(self):
        if getattr(self, "_startup_placeholder_visible", False) and hasattr(self, "_startup_placeholder"):
            self._startup_placeholder_visible = False
            self._startup_placeholder.hide()
    def _schedule_deferred_controls_build(self):
        if getattr(self, "_deferred_controls_ready", False):
            return
        if getattr(self, "_deferred_controls_build_scheduled", False):
            return
        self._deferred_controls_build_scheduled = True
        QTimer.singleShot(60, self._build_deferred_controls_ui)
    def _on_canvas_first_useful_paint(self):
        self._hide_startup_placeholder()
        self._start_async_bootstrap()
        self._schedule_deferred_controls_build()
    def _maybe_run_first_time_onboarding(self):
        if bool(get_config_value("ui_onboarding_done", False)):
            return
        dlg = WelcomeOnboardingDialog(self.asset_manager, self, mandatory=True)
        if dlg.exec_() == QDialog.Accepted:
            set_config_value("ui_onboarding_done", True)
            self._sync_runtime_asset_config(updated_asset_id=None)
            self._validate_checked_assets_startup()
    def _gaia_tap_state_path(self) -> Path:
        try:
            root = Path(self.runtime_layout.get("root", Path.home())).resolve()
        except Exception:
            root = Path.home()
        return root / "logs" / "gaia_tap_state.json"
    def _load_pending_gaia_state(self) -> Optional[dict]:
        state_path = self._gaia_tap_state_path()
        if not state_path.exists():
            return None
        try:
            with state_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                return None
            status = str(payload.get("status", "")).strip().lower()
            phase = str(payload.get("phase", "")).strip().lower()
            if status in {"done", "completed", "success"} or phase in {"done", "completed"}:
                return None
            return payload
        except Exception:
            return None
    def _on_gaia_background_dialog_finished(self, _code: int) -> None:
        dlg = getattr(self, "_gaia_background_dialog", None)
        if dlg is not None and bool(getattr(dlg, "completed", False)):
            self._sync_runtime_asset_config(updated_asset_id="gaia_catalog")
        self._gaia_background_dialog = None
    def _maybe_resume_pending_gaia_download(self) -> None:
        return widget_maybe_resume_pending_gaia_download(self)
    def _open_quick_welcome(self):
        dlg = WelcomeOnboardingDialog(self.asset_manager, self, mandatory=False)
        if dlg.exec_() == QDialog.Accepted:
            self._sync_runtime_asset_config(updated_asset_id=None)
    def _open_asset_onboarding(self, asset_id: str) -> bool:
        dlg = AssetOnboardingDialog(self.asset_manager, asset_id, self)
        ok = dlg.exec_() == QDialog.Accepted and bool(getattr(dlg, "completed", False))
        if ok:
            # Keep runtime knobs in sync when onboarding modifies config paths/settings.
            self._sync_runtime_asset_config(updated_asset_id=str(asset_id))
        return bool(ok)
    def _sync_runtime_asset_config(self, updated_asset_id: Optional[str] = None):
        self.milkyway_overlay_texture_path = str(
            get_config_value("milkyway_overlay_texture_path", self.milkyway_overlay_texture_path)
        )
        self.dust_map_path = str(get_config_value("dust_map_path", self.dust_map_path))
        self.light_pollution_enabled = bool(
            get_config_value("light_pollution_enabled", self.light_pollution_enabled)
        )
        if hasattr(self, "weather"):
            self.weather.set_remote_user_agent(self.asset_manager.get_user_agent())
        if hasattr(self, "canvas") and hasattr(self.canvas, "weather"):
            self.canvas.weather.set_remote_user_agent(self.asset_manager.get_user_agent())
        self._ngc_search_entries_cache = None
        if hasattr(self, "chk_deep_space") and bool(self.chk_deep_space.isChecked()):
            self.build_search_index()
        if hasattr(self, "horizon_worker"):
            self.horizon_worker.reload_config()
        if str(updated_asset_id or "") == "gaia_catalog" and self.asset_manager.asset_ready("gaia_catalog"):
            self._reload_star_catalog_async()
    def _reload_star_catalog_async(self):
        return widget_reload_star_catalog_async(self)
    def _ensure_asset_before_enable(self, checkbox: QCheckBox, checked: bool, asset_id: str) -> bool:
        checked = bool(checked)
        if not checked:
            return False
        if self.asset_manager.asset_ready(asset_id):
            return True
        ok = self._open_asset_onboarding(asset_id)
        if ok:
            return True
        checkbox.blockSignals(True)
        checkbox.setChecked(False)
        checkbox.blockSignals(False)
        return False
    def _persist_visibility_state(self, key: str, checked: bool) -> None:
        set_config_value(f"ui.visibility.{key}", bool(checked))
    def _load_visibility_state(self, key: str, default: bool) -> bool:
        return bool(get_config_value(f"ui.visibility.{key}", default))
    def _validate_checked_assets_startup(self):
        """Disable persisted layer toggles whose required asset is currently missing."""
        checks = (
            ("chk_clima", "clima", "climate_metno"),
            ("chk_enable_sky", "estrelles", "gaia_catalog"),
            ("chk_enable_milkyway", "via_lactia", "milkyway_texture"),
            ("chk_enable_planck_dust", "pols_planck", "planck_dust"),
            ("chk_deep_space", "espai_profund", "ngc_catalog"),
            ("chk_light_pollution", "contaminacio_luminica", "light_pollution"),
            ("chk_enable_village", "topografia", "elevation_dem"),
        )
        for attr_name, key, asset_id in checks:
            chk = getattr(self, attr_name, None)
            if chk is None:
                continue
            if (not bool(chk.isChecked())) or self.asset_manager.asset_ready(asset_id):
                continue
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)
            self._persist_visibility_state(key, False)
            if attr_name == "chk_clima":
                if hasattr(self.canvas, "weather"):
                    self.canvas.weather.enabled = False
                if hasattr(self, "weather"):
                    self.weather.enabled = False
            if attr_name == "chk_light_pollution":
                self.light_pollution_enabled = False
                set_config_value("light_pollution_enabled", False)
        if hasattr(self, "canvas"):
            self.canvas.update()
    def _start_async_bootstrap(self):
        return widget_start_async_bootstrap(self)
    def _start_catalog_loader_async(self, reason: str = "runtime"):
        if bool(getattr(self, "_catalog_bootstrap_started", False)):
            return
        np_ra = getattr(self, "np_ra", None)
        np_dec = getattr(self, "np_dec", None)
        np_mag = getattr(self, "np_mag", None)
        np_r = getattr(self, "np_r", None)
        np_g = getattr(self, "np_g", None)
        np_b = getattr(self, "np_b", None)
        np_bp_rp = getattr(self, "np_bp_rp", None)
        subset_only = bool(getattr(self, "_catalog_loaded_subset_only", False))
        try:
            existing_rows = int(len(np_ra)) if np_ra is not None else 0
        except Exception:
            existing_rows = 0
        has_consistent_arrays = (
            np_ra is not None
            and np_dec is not None
            and np_mag is not None
            and np_r is not None
            and np_g is not None
            and np_b is not None
            and np_bp_rp is not None
            and existing_rows > 0
            and existing_rows == int(len(np_dec)) == int(len(np_mag))
        )
        # With StarDataCoordinator refactor active, keep current in-memory catalog
        # and skip legacy worker source probing.
        if has_consistent_arrays and (not subset_only) and existing_rows >= 5000:
            self._catalog_bootstrap_started = True
            print(
                "[AstroWidget] Star catalog loader skipped: using in-memory "
                f"catalog rows={existing_rows} (reason={reason})"
            )
            try:
                existing_named = getattr(self, "celestial_objects", [])
                self._on_catalog_ready(
                    existing_named,
                    np_ra,
                    np_dec,
                    np_mag,
                    np_r,
                    np_g,
                    np_b,
                    np_bp_rp,
                )
            except Exception as e:
                print(f"[AstroWidget] In-memory catalog finalize failed: {e}")
            return
        self._catalog_bootstrap_started = True
        self._catalog_thread = QThread()
        self._catalog_worker = CatalogLoaderWorker()
        self._catalog_worker.moveToThread(self._catalog_thread)
        self._catalog_worker.catalog_ready.connect(self._on_catalog_ready)
        self._catalog_thread.started.connect(lambda: self._catalog_worker.load(self._stars_catalog_dir))
        self._catalog_thread.start()
        try:
            self._catalog_thread.setPriority(QThread.LowPriority)
        except Exception:
            pass
        print(f"[AstroWidget] Star catalog loading in background... (reason={reason})")
    def _try_start_catalog_loader_deferred(self):
        if bool(getattr(self, "_catalog_bootstrap_started", False)):
            return
        stage = str(getattr(self, "scene_load_stage", "boot"))
        if stage == "scene_ready":
            self._start_catalog_loader_async(reason="defer_after_scene_ready")
            return
        elapsed = float(time.perf_counter() - float(getattr(self, "_catalog_defer_t0", 0.0)))
        # Hard safety valve if horizon never reaches scene_ready.
        if elapsed >= 300.0:
            self._start_catalog_loader_async(reason="defer_hard_timeout")
            return
        QTimer.singleShot(15000, self._try_start_catalog_loader_deferred)
    def _build_horizon_bake_job(self) -> dict:
        from TerraLab.common.utils import get_config_value
        import uuid
        try:
            n_bands = int(get_config_value("horizon_quality", 20))
        except Exception:
            n_bands = 20
        current_fov = 100.0 / max(0.001, float(getattr(self.canvas, "zoom_level", 1.0)))
        return {
            "job_id": uuid.uuid4().hex,
            "lat": float(self.latitude),
            "lon": float(self.longitude),
            "observer_offset": float(getattr(self.horizon_worker, "observer_offset", 0.0)),
            "bands": max(1, int(n_bands)),
            "view_azimuth": float(getattr(self.canvas, "azimuth_offset", 180.0)) % 360.0,
            "view_fov_deg": float(current_fov),
            "view_elevation": float(getattr(self.canvas, "elevation_angle", 0.0)),
        }
    def _begin_horizon_bake(self):
        if not hasattr(self, "horizon_worker"):
            return
        self.horizon_worker.abort_current_job()
        self._active_horizon_job_id = None
        if hasattr(self.canvas, "horizon_overlay"):
            self.canvas.horizon_overlay.clear_profile()
        target_stage = "stars_ready" if hasattr(self, "np_ra") else "base_sky"
        self._set_scene_load_stage(target_stage)
        job = self._build_horizon_bake_job()
        self._active_horizon_job_id = str(job["job_id"])
        self.on_horizon_progress_state(
            {
                "job_id": self._active_horizon_job_id,
                "phase": "prepare",
                "percent": 0.0,
                "current": 0,
                "total": int(round(360.0 / 0.5)),
            }
        )
        self.request_horizon_bake.emit(job)
    def on_horizon_progress_state(self, state):
        if not isinstance(state, dict):
            return
        job_id = str(state.get("job_id", "") or "")
        if job_id and getattr(self, "_active_horizon_job_id", None) and job_id != self._active_horizon_job_id:
            return
        percent = max(0.0, min(100.0, float(state.get("percent", 0.0))))
        percent_text = f"{percent:.1f}"
        if percent_text.endswith(".0"):
            percent_text = percent_text[:-2]
        current = state.get("current")
        total = state.get("total")
        msg = getTraduction("Horizon.CalculatingHorizon", "Calculating horizon: {pct}%").format(pct=percent_text)
        if current is not None and total:
            msg = f"{msg} Â· {int(current)}/{int(total)}"
        self.on_horizon_progress(msg)
    def on_horizon_preview_ready(self, payload):
        if not isinstance(payload, dict):
            return
        job_id = str(payload.get("job_id", "") or "")
        if job_id and job_id != getattr(self, "_active_horizon_job_id", None):
            return
        profile = payload.get("profile")
        if profile is None:
            return
        layer_defs = None
        band_defs = getattr(profile, "_band_defs", None)
        if band_defs is not None:
            try:
                from TerraLab.terrain.overlay import generate_layer_defs
                layer_defs = generate_layer_defs(band_defs)
            except Exception as exc:
                print(f"[AstroWidget] Warning: Could not generate preview layer_defs: {exc}")
        if hasattr(self.canvas, "horizon_overlay"):
            self.canvas.horizon_overlay.set_profile(profile, layer_defs=layer_defs)
        if getattr(self, "scene_load_stage", "base_sky") != "scene_ready":
            self._set_scene_load_stage("horizon_preview")
    def on_horizon_profile_ready(self, profile):
        """Callback when background worker finishes baking horizon."""
        self._start_catalog_loader_async(reason="horizon_ready")
        if isinstance(profile, dict):
            job_id = str(profile.get("job_id", "") or "")
            if job_id and job_id != getattr(self, "_active_horizon_job_id", None):
                return
            profile = profile.get("profile")
        if profile is None:
            return
        print(f"[AstroWidget] New Horizon Profile received! Bands: {len(profile.bands)}")
        # Hide Loading Label
        if hasattr(self, 'lbl_loading'):
            self.lbl_loading.hide()
        # Build matching layer_defs from band_defs attached by the worker
        layer_defs = None
        band_defs = getattr(profile, '_band_defs', None)
        if band_defs is not None:
            try:
                from TerraLab.terrain.overlay import generate_layer_defs
                layer_defs = generate_layer_defs(band_defs)
            except Exception as e:
                print(f"[AstroWidget] Warning: Could not generate layer_defs: {e}")
        # Update Horizon Overlay (Background Mountains)
        if hasattr(self.canvas, 'horizon_overlay'):
            self.canvas.horizon_overlay.set_profile(profile, layer_defs=layer_defs)
        # Update Village Overlay (Foreground Objects)
        if hasattr(self.canvas, 'village'):
             self.canvas.village.set_profile(profile)
        # Refresh the UI altitude label now that the worker has safely initialized the DEM data
        self.update_altitude_label()
        # Initial Bortle Sync if in Auto mode
        if getattr(self, 'is_auto_bortle', True):
            self.reset_lp_to_auto()
        self._set_scene_load_stage("scene_ready")
        self.canvas.update()
    def on_horizon_progress(self, msg):
        """Update loading label with progress message."""
        self._last_horizon_progress_text = str(msg or "")
        if hasattr(self, 'lbl_loading'):
            if not msg:
                self.lbl_loading.hide()
                return
            self.lbl_loading.setText(msg)
            fm = self.lbl_loading.fontMetrics()
            required_w = fm.horizontalAdvance(msg) + 28
            required_h = max(fm.height() + 12, 32)
            self.lbl_loading.resize(
                min(max(required_w, 360), max(360, self.width() - 20)),
                required_h,
            )
            if self.lbl_loading.isHidden():
                self.lbl_loading.show()
                self.lbl_loading.raise_()
            self.lbl_loading.repaint()
    def _poll_horizon_progress(self):
        if not hasattr(self, "horizon_worker"):
            return
        try:
            msg = self.horizon_worker.get_progress_text()
        except Exception:
            return
        msg = str(msg or "")
        if msg != getattr(self, "_last_horizon_progress_text", ""):
            self.on_horizon_progress(msg)
    def _position_gaia_extension_status_label(self):
        lbl = getattr(self, "lbl_gaia_extension_status", None)
        if lbl is None:
            return
        fm = lbl.fontMetrics()
        text = lbl.text() or ""
        width = min(max(320, fm.horizontalAdvance(text) + 30), max(320, self.width() - 20))
        height = max(32, fm.height() + 12)
        lbl.resize(width, height)
        lbl.move(max(10, self.width() - width - 10), 10)
    def _set_gaia_extension_status_label(self, message: str, *, keep_seconds: float = 0.0):
        lbl = getattr(self, "lbl_gaia_extension_status", None)
        if lbl is None:
            return
        msg = str(message or "").strip()
        if not msg:
            self._hide_gaia_extension_status_label()
            return
        lbl.setText(msg)
        self._position_gaia_extension_status_label()
        lbl.show()
        lbl.raise_()
        if keep_seconds > 0:
            self._gaia_extension_status_hide_timer.start(int(max(1000.0, float(keep_seconds) * 1000.0)))
        else:
            self._gaia_extension_status_hide_timer.stop()
    def _hide_gaia_extension_status_label(self):
        lbl = getattr(self, "lbl_gaia_extension_status", None)
        if lbl is None:
            return
        lbl.hide()
    def _on_scope_extension_progress(self, percent: float, message: str):
        pct = max(0.0, min(100.0, float(percent)))
        msg = str(message or "").strip()
        if not msg:
            msg = "Carregant extensio d'estrelles"
        self._set_gaia_extension_status_label(f"{msg} ({int(round(pct))}%)")
    def pause_updates(self):
        """Pause sky updates to free up main thread for video loading."""
        if not self._updates_paused:
            self._updates_paused = True
            self._saved_interval = self.timer.interval()
            self.timer.stop()
            print("[SKY] Updates PAUSED for video loading")
    def resume_updates(self):
        """Resume sky updates after video has loaded."""
        if self._updates_paused:
            self._updates_paused = False
            self.timer.start(self._saved_interval)
            print("[SKY] Updates RESUMED")
    def set_low_fps_mode(self, low=True):
        """Switch to low FPS mode (5 FPS) when video is playing."""
        if low:
            self.timer.setInterval(200)  # 5 FPS
        else:
            self.timer.setInterval(16)   # 60 FPS
    def _on_skyfield_ready(self, ts, eph):
        """Callback when Skyfield finishes loading in background."""
        if ts is not None and eph is not None:
            self.ts = ts
            self.eph = eph
            print("[AstroWidget] Skyfield ready (async).")
            if self.show_satellites:
                # Only show loading label if not blocking (i.e., if satellites are being loaded)
                if hasattr(self, 'lbl_loading'):
                    self.lbl_loading.show()
                self.load_satellites_from_tle()
            self.canvas.update()
        else:
            print("[AstroWidget] Skyfield failed to load.")
        # Clean up thread
        self._skyfield_thread.quit()
    def _do_delayed_bake(self):
        """Actually sends the bake request after debouncing."""
        if hasattr(self, 'horizon_worker'):
             self.horizon_worker.abort_current_job()
        # SAVE CONFIG ONLY HERE (Avoid disk spam)
        from TerraLab.common.utils import set_config_value
        offset_val = self.spin_extra_height.value()
        set_config_value("observer_offset", offset_val)
        set_config_value("observer_lat", self.latitude)
        set_config_value("observer_lon", self.longitude)
        print(f"[AstroWidget] Emitting debounced bake request for {self.latitude}, {self.longitude}")
        self._begin_horizon_bake()
    def _on_catalog_ready(self, celestial_objects, np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp):
        return widget_on_catalog_ready(self, celestial_objects, np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp)
    def _maybe_refresh_gaia_extension_catalog(self):
        stars_dir = getattr(self, "_stars_catalog_dir", "")
        if not stars_dir:
            return
        ext_path = os.path.join(stars_dir, "stars_catalog_extension.npy")
        if not os.path.isfile(ext_path):
            return
        try:
            mtime = float(os.path.getmtime(ext_path))
        except Exception:
            return
        already_loaded_mag = float(getattr(self, "_scope_catalog_loaded_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG))
        if (mtime <= float(getattr(self, "_gaia_extension_mtime_loaded", 0.0)) + 1e-6) and (already_loaded_mag > STAR_CATALOG_NAKED_EYE_MAX_MAG + 1e-3):
            return
        self._gaia_extension_mtime_loaded = float(mtime)
        if not bool(getattr(self, "_scope_catalog_loading", False)):
            self._ensure_scope_catalog_loaded()
    def _ensure_scope_catalog_loaded(self, force_now: bool = False):
        return widget_ensure_scope_catalog_loaded(self, force_now)
    def _finalize_scope_catalog_loader_refs(self):
        thread = getattr(self, "_scope_catalog_thread", None)
        if thread is not None:
            try:
                if thread.isRunning():
                    return
            except Exception:
                pass
        self._scope_catalog_worker = None
        self._scope_catalog_thread = None
    def _cleanup_scope_catalog_loader(self):
        thread = getattr(self, "_scope_catalog_thread", None)
        if thread is None:
            self._finalize_scope_catalog_loader_refs()
            return
        try:
            thread.finished.connect(self._finalize_scope_catalog_loader_refs)
        except Exception:
            pass
        try:
            if thread.isRunning():
                thread.quit()
                return
        except Exception:
            pass
        self._finalize_scope_catalog_loader_refs()
    def _finalize_scope_index_warmup_refs(self):
        thread = getattr(self, "_scope_index_thread", None)
        if thread is not None:
            try:
                if thread.isRunning():
                    return
            except Exception:
                pass
        self._scope_index_worker = None
        self._scope_index_thread = None
    def _cleanup_scope_index_warmup(self):
        thread = getattr(self, "_scope_index_thread", None)
        if thread is None:
            self._finalize_scope_index_warmup_refs()
            return
        try:
            thread.finished.connect(self._finalize_scope_index_warmup_refs)
        except Exception:
            pass
        try:
            if thread.isRunning():
                thread.quit()
                return
        except Exception:
            pass
        self._finalize_scope_index_warmup_refs()
    def _scope_target_index_mag_cap(self) -> float:
        base_cap = float(STAR_CATALOG_NAKED_EYE_MAX_MAG)
        try:
            catalog_cap = float(getattr(self, "_catalog_max_mag", base_cap))
        except Exception:
            catalog_cap = base_cap
        catalog_cap = max(base_cap, catalog_cap)
        if not self.canvas.scope_mode_enabled():
            return min(catalog_cap, base_cap)
        try:
            ctrl = getattr(self.canvas, "scope_controller", None)
            first_fix_pending = bool(ctrl is not None and (not bool(getattr(ctrl, "user_center_fixed_once", False))))
        except Exception:
            first_fix_pending = False
        if first_fix_pending:
            return min(catalog_cap, base_cap)
        target = base_cap + 1.0
        vm_state = getattr(self, "visual_magnitude_result", None)
        if vm_state is not None:
            try:
                target = float(getattr(vm_state, "scope_limit_mag", target)) + 0.75
            except Exception:
                pass
        return float(max(base_cap, min(catalog_cap, target)))
    def _ensure_scope_spatial_index_warmup(self):
        return widget_ensure_scope_spatial_index_warmup(self)
    def _on_scope_spatial_index_ready(self, catalog_key, sorted_indices, offsets, ready_mag_cap):
        return widget_on_scope_spatial_index_ready(self, catalog_key, sorted_indices, offsets, ready_mag_cap)
    def _on_scope_spatial_index_error(self, message: str):
        self._scope_index_loading = False
        self._scope_index_rewarm_requested = False
        print(f"[AstroWidget] Scope spatial index warm-up error: {message}")
        try:
            stars_renderer = getattr(getattr(self.canvas, "sky_renderer", None), "stars_renderer", None)
            if stars_renderer is not None:
                stars_renderer.clear_scope_index_warmup(self._scope_index_target_key)
        finally:
            self._cleanup_scope_index_warmup()
    def _on_scope_extension_ready(self, np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp, loaded_max_mag):
        return widget_on_scope_extension_ready(self, np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp, loaded_max_mag)
    def init_skyfield(self):
        """Legacy synchronous init. Kept for compatibility."""
        try:
            self.ts = load.timescale()
            self.eph = load('de421.bsp')
            print("Skyfield Initialized.")
        except Exception as e:
            print(f"Skyfield Error: {e}")
    def load_satellites_from_tle(self):
        if not SKYFIELD_AVAILABLE: return
        try:
            from skyfield.api import EarthSatellite
            # ISS TLE (Example - normally from CelesTrak)
            line1 = "1 25544U 98067A   23015.53927649  .00010079  00000-0  18231-3 0  9993"
            line2 = "2 25544  51.6421  42.5312 0005527  38.8344 321.3283 15.49830575378370"
            iss = EarthSatellite(line1, line2, 'ISS', self.ts)
            self.satellites = [{
                'name': 'ISS',
                'obj': iss,
                'std_mag': -1.8
            }]
        except Exception as e:
            print(f"Sat Load Error: {e}")
    def toggle_satellites(self, checked):
        self.show_satellites = checked
        if checked and not self.satellites and SKYFIELD_AVAILABLE:
            self.load_satellites_from_tle()
        self.canvas.update()
    def setup_content(self):
        if hasattr(self, 'title_bar'):
            self.title_bar.hide()
        layout = self.content_layout
        layout.setContentsMargins(0, 0, 0, 0)
        try:
            from TerraLab.ui.astro_canvas import AstroCanvas as ReducedAstroCanvas
            self.canvas = ReducedAstroCanvas(self)
        except Exception:
            self.canvas = AstroCanvas(self)
        layout.addWidget(self.canvas, 1)
        # Loading indicator stays available from the first visible frame.
        self.lbl_loading = QLabel(getTraduction("Astro.LoadingTopography", "? Carregant topografia..."), self)
        self.lbl_loading.setStyleSheet(
            "color: yellow; font-weight: bold; background-color: rgba(0,0,0,100); "
            "padding: 5px; border-radius: 4px;"
        )
        self.lbl_loading.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_loading.setWordWrap(False)
        self.lbl_loading.hide()
        self.lbl_loading.move(10, 50)
        self.lbl_loading.resize(420, 32)
        self.lbl_gaia_extension_status = QLabel("", self)
        self.lbl_gaia_extension_status.setStyleSheet(
            "color: #ffe680; font-weight: bold; background-color: rgba(0,0,0,140); "
            "padding: 5px; border-radius: 4px;"
        )
        self.lbl_gaia_extension_status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_gaia_extension_status.setWordWrap(False)
        self.lbl_gaia_extension_status.hide()
        self._position_gaia_extension_status_label()
    def _build_deferred_controls_ui(self):
        return build_deferred_controls_ui(self)
    def apply_styles(self):
        super().apply_styles()
        self.update_custom_theme()
    def update_custom_theme(self):
        return widget_update_custom_theme(self)
    def toggle_scope_panel(self, checked):
        if checked:
            if hasattr(self, 'btn_tools_panel'):
                self.btn_tools_panel.blockSignals(True)
                self.btn_tools_panel.setChecked(False)
                self.btn_tools_panel.blockSignals(False)
            if hasattr(self, 'tools_panel'):
                self.tools_panel.hide()
            self._sync_scope_coord_inputs_from_canvas()
        if hasattr(self, 'scope_panel'):
            self.scope_panel.setVisible(checked)
        QTimer.singleShot(0, self._update_button_pos)
    def toggle_tools_panel(self, checked):
        if checked:
            if hasattr(self, 'btn_scope_panel'):
                self.btn_scope_panel.blockSignals(True)
                self.btn_scope_panel.setChecked(False)
                self.btn_scope_panel.blockSignals(False)
            if hasattr(self, 'scope_panel'):
                self.scope_panel.hide()
        if hasattr(self, 'tools_panel'):
            self.tools_panel.setVisible(checked)
        QTimer.singleShot(0, self._update_button_pos)
    def on_scope_shape_changed(self, index):
        shape = self.scope_shape_combo.itemData(index)
        self.canvas.set_scope_shape(shape)
        self._sync_scope_aspect_controls(shape)
        self._apply_scope_aspect_from_ui()
    def on_scope_instrument_changed(self, index):
        mode = self.scope_instrument_combo.itemData(index)
        if mode not in ("telescope", "camera_aps_c", "camera_full_frame"):
            mode = "telescope"
        self.scope_instrument_profile = str(mode)
        self._sync_scope_instrument_controls()
        self._persist_visual_magnitude_settings()
        self._apply_scope_aspect_from_ui()
        self.canvas.update()
    def _set_scope_sensor_key(self, sensor_key: str):
        if not hasattr(self, "scope_sensor_combo"):
            return
        idx = -1
        for i in range(self.scope_sensor_combo.count()):
            if self.scope_sensor_combo.itemData(i) == sensor_key:
                idx = i
                break
        if idx < 0:
            return
        self.scope_sensor_combo.blockSignals(True)
        self.scope_sensor_combo.setCurrentIndex(idx)
        self.scope_sensor_combo.blockSignals(False)
        self.canvas.set_scope_sensor(sensor_key)
        self._apply_scope_aspect_from_ui()
    def _sync_scope_instrument_controls(self):
        return self._scope_ui_manager.sync_instrument_controls()
    def on_scope_sensor_changed(self, index):
        sensor = self.scope_sensor_combo.itemData(index)
        self.canvas.set_scope_sensor(sensor)
        self._apply_scope_aspect_from_ui()
    def on_scope_aspect_changed(self, index):
        self._apply_scope_aspect_from_ui()
    def on_scope_aspect_custom_changed(self, value):
        _ = value
        self._apply_scope_aspect_from_ui()
    def on_scope_speed_changed(self, index):
        mode = self.scope_speed_combo.itemData(index)
        self.canvas.set_scope_speed_mode(mode)
    def on_scope_aperture_mode_changed(self, index):
        if str(getattr(self, "scope_instrument_profile", "telescope")) != "telescope":
            self.scope_aperture_input_mode = "f_number"
            self._sync_scope_aperture_controls()
            self._persist_visual_magnitude_settings()
            self.canvas.update()
            return
        mode = self.scope_aperture_mode_combo.itemData(index)
        if mode not in ("diameter_mm", "f_number"):
            mode = "diameter_mm"
        self.scope_aperture_input_mode = str(mode)
        self._sync_scope_aperture_controls()
        self._persist_visual_magnitude_settings()
        self.canvas.update()
    def _sync_scope_aperture_controls(self):
        if not hasattr(self, "scope_aperture_spin") or not hasattr(self, "scope_aperture_label"):
            return
        mode = str(getattr(self, "scope_aperture_input_mode", "diameter_mm"))
        spin = self.scope_aperture_spin
        if mode == "f_number":
            self.scope_aperture_label.setText(getTraduction("Astro.ScopeFNumber", "f/"))
            spin.blockSignals(True)
            spin.setRange(0.7, 64.0)
            spin.setDecimals(1)
            spin.setSingleStep(0.1)
            spin.setValue(float(getattr(self, "scope_aperture_f_number", 4.0)))
            spin.blockSignals(False)
        else:
            self.scope_aperture_label.setText(getTraduction("Astro.ScopeAperture", "Aperture (mm)"))
            spin.blockSignals(True)
            spin.setRange(10.0, 2000.0)
            spin.setDecimals(1)
            spin.setSingleStep(5.0)
            spin.setValue(float(getattr(self, "scope_aperture_mm", 80.0)))
            spin.blockSignals(False)
    def _effective_scope_aperture_mm(self, focal_mm: float = None) -> float:
        mode = str(getattr(self, "scope_aperture_input_mode", "diameter_mm"))
        if focal_mm is None:
            if hasattr(self, "scope_focal_spin"):
                focal_mm = float(self.scope_focal_spin.value())
            else:
                focal_mm = 250.0
        focal_mm = max(1.0, float(focal_mm))
        if mode == "f_number":
            f_number = max(0.7, float(getattr(self, "scope_aperture_f_number", 4.0)))
            return max(1.0, focal_mm / f_number)
        return max(1.0, float(getattr(self, "scope_aperture_mm", 80.0)))
    def _persist_visual_magnitude_settings(self):
        set_config_value("manual_eye_limit_mag", float(self.magnitude_limit))
        set_config_value("scope_instrument_profile", str(self.scope_instrument_profile))
        set_config_value("scope_aperture_mm", float(self.scope_aperture_mm))
        set_config_value("scope_aperture_f_number", float(self.scope_aperture_f_number))
        set_config_value("scope_aperture_input_mode", str(self.scope_aperture_input_mode))
        set_config_value("scope_eyepiece_mm", float(self.scope_eyepiece_mm))
        set_config_value("scope_iso", int(self.scope_iso))
        set_config_value("scope_exposure_s", float(self.scope_exposure_s))
    def _on_scope_aperture_changed(self, value):
        if str(getattr(self, "scope_aperture_input_mode", "diameter_mm")) == "f_number":
            self.scope_aperture_f_number = float(value)
        else:
            self.scope_aperture_mm = float(value)
        self._persist_visual_magnitude_settings()
        self.canvas.update()
    def _on_scope_eyepiece_changed(self, value):
        self.scope_eyepiece_mm = float(value)
        self._persist_visual_magnitude_settings()
        self.canvas.update()
    def _on_scope_iso_changed(self, value):
        self.scope_iso = int(value)
        self._persist_visual_magnitude_settings()
        self.canvas.update()
    def _on_scope_exposure_changed(self, value):
        self.scope_exposure_s = float(value)
        self._persist_visual_magnitude_settings()
        self.canvas.update()
    def _scope_coord_inputs_have_focus(self) -> bool:
        fields = [
            "scope_ra_h_spin",
            "scope_ra_m_spin",
            "scope_ra_s_spin",
            "scope_dec_sign_combo",
            "scope_dec_d_spin",
            "scope_dec_m_spin",
            "scope_dec_s_spin",
        ]
        for name in fields:
            w = getattr(self, name, None)
            if w is not None and hasattr(w, "hasFocus") and w.hasFocus():
                return True
        return False
    def _set_scope_coord_inputs(self, ra_deg: float, dec_deg: float) -> None:
        return widget_set_scope_coord_inputs(self, ra_deg, dec_deg)
    def _sync_scope_coord_inputs_from_canvas(self) -> None:
        if not hasattr(self, "scope_ra_h_spin"):
            return
        if self._scope_coord_inputs_have_focus():
            return
        center = getattr(getattr(self, "canvas", None), "scope_controller", None)
        center = getattr(center, "center", None)
        if center is None:
            return
        try:
            ut_hour, day_of_year_utc = self.canvas._current_ut_context()
            ra_dec = self.canvas._altaz_to_ra_dec(center[0], center[1], ut_hour, day_of_year_utc)
            if ra_dec is None:
                return
            self._set_scope_coord_inputs(float(ra_dec[0]), float(ra_dec[1]))
        except Exception:
            return
    def on_scope_goto_radec(self) -> None:
        return self._scope_ui_manager.goto_radec()
    def _estimate_eye_pupil_mm(self, sun_alt_deg: float) -> float:
        if sun_alt_deg >= 0.0:
            return 2.2
        if sun_alt_deg <= -18.0:
            return float(self.scope_eye_pupil_dark_mm)
        t = (0.0 - sun_alt_deg) / 18.0
        return 2.2 + (float(self.scope_eye_pupil_dark_mm) - 2.2) * t
    def recompute_visual_magnitude_model(self, target_alt_deg=None, sun_alt_deg=-18.0, now_utc=None):
        return widget_recompute_visual_magnitude_model(
            self,
            target_alt_deg=target_alt_deg,
            sun_alt_deg=sun_alt_deg,
            now_utc=now_utc,
        )
    def _sync_scope_aspect_controls(self, shape: str):
        is_rect = shape == TelescopeScopeController.SHAPE_RECT
        if hasattr(self, 'scope_aspect_combo'):
            self.scope_aspect_combo.setEnabled(is_rect)
        if hasattr(self, 'scope_aspect_custom_spin') and hasattr(self, 'scope_aspect_combo'):
            is_custom = self.scope_aspect_combo.itemData(self.scope_aspect_combo.currentIndex()) == "custom"
            self.scope_aspect_custom_spin.setEnabled(is_rect and is_custom)
    def _apply_scope_aspect_from_ui(self):
        if not hasattr(self, 'scope_aspect_combo'):
            return
        shape = self.scope_shape_combo.itemData(self.scope_shape_combo.currentIndex())
        self._sync_scope_aspect_controls(shape)
        if shape != TelescopeScopeController.SHAPE_RECT:
            self.canvas.set_scope_aspect_ratio(None)
            return
        data = self.scope_aspect_combo.itemData(self.scope_aspect_combo.currentIndex())
        if data is None:
            self.canvas.set_scope_aspect_ratio(None)
            return
        if data == "custom":
            self.canvas.set_scope_aspect_ratio(self.scope_aspect_custom_spin.value())
            return
        self.canvas.set_scope_aspect_ratio(float(data))
    def sync_scope_speed_ui(self, mode: str):
        if not hasattr(self, 'scope_speed_combo'):
            return
        for i in range(self.scope_speed_combo.count()):
            if self.scope_speed_combo.itemData(i) == mode:
                self.scope_speed_combo.blockSignals(True)
                self.scope_speed_combo.setCurrentIndex(i)
                self.scope_speed_combo.blockSignals(False)
                break
    def sync_scope_focal_ui(self, focal_mm: float):
        if not hasattr(self, 'scope_focal_spin'):
            return
        spin = self.scope_focal_spin
        v = float(max(spin.minimum(), min(spin.maximum(), focal_mm)))
        spin.blockSignals(True)
        spin.setValue(v)
        spin.blockSignals(False)
    def activate_scope_mode(self):
        return self._scope_ui_manager.activate()
    def exit_scope_mode(self):
        return self._scope_ui_manager.exit()
    def sync_scope_ui_state(self, enabled: bool):
        return self._scope_ui_manager.sync_ui_state(bool(enabled))
    def _sync_measure_tool_buttons(self, active_tool: str):
        tool_buttons = [
            (getattr(self, 'btn_tool_ruler', None), TOOL_RULER),
            (getattr(self, 'btn_tool_square', None), TOOL_SQUARE),
            (getattr(self, 'btn_tool_rect', None), TOOL_RECTANGLE),
            (getattr(self, 'btn_tool_circle', None), TOOL_CIRCLE),
        ]
        for btn, key in tool_buttons:
            if btn is None:
                continue
            btn.blockSignals(True)
            btn.setChecked(active_tool == key)
            btn.blockSignals(False)
    def _sync_constellation_controls(self):
        return widget_sync_constellation_controls(self)
    def _delete_constellation_shortcut(self):
        if not hasattr(self, "canvas"):
            return
        if not self.canvas.drawing_mode_enabled():
            return
        ctrl = self.canvas.constellation_controller
        if not ctrl.has_deletable_selection():
            return
        deleted = ctrl.delete_selected()
        if deleted:
            self.canvas.update()
            self._sync_constellation_controls()
    def _finish_constellation_shortcut(self):
        if not hasattr(self, "canvas"):
            return
        if not self.canvas.drawing_mode_enabled():
            return
        self.finish_constellation_group()
    def toggle_constellation_visibility(self, checked: bool):
        visible = bool(checked)
        self.canvas.set_constellation_visible(visible)
        if not visible:
            self.canvas.set_constellation_draw_mode(False)
        self.canvas.setFocus()
        self._sync_constellation_controls()
    def toggle_constellation_draw_mode(self, checked: bool):
        enabled = bool(checked)
        if enabled:
            self.exit_scope_mode()
            self.canvas.set_measurement_tool(TOOL_NONE)
            self._sync_measure_tool_buttons(TOOL_NONE)
        self.canvas.set_constellation_draw_mode(enabled)
        self.canvas.setFocus()
        self._sync_constellation_controls()
    def delete_constellation_action(self):
        ctrl = self.canvas.constellation_controller
        deleted = False
        selected_groups = len(getattr(ctrl, "selected_group_indices", set()) or set())
        if selected_groups > 1:
            deleted = ctrl.delete_selected_groups() > 0
        else:
            if len(getattr(ctrl, "groups", [])) > 0:
                ctrl.clear_all()
                deleted = True
        if deleted:
            self.canvas.update()
        self.canvas.setFocus()
        self._sync_constellation_controls()
    def toggle_constellation_eraser_mode(self, checked: bool):
        # Legacy no-op: eraser mode has been replaced by selection + delete actions.
        self.canvas.setFocus()
        self._sync_constellation_controls()
    def constellation_primary_action(self):
        ctrl = self.canvas.constellation_controller
        if bool(getattr(ctrl, "group_drawing_active", False)):
            self.finish_constellation_group()
            return
        self.create_constellation_group()
    def create_constellation_group(self):
        default_name = self.canvas.constellation_controller.next_default_name()
        title = getTraduction("Astro.ConstellationDialogTitle", "Constellation")
        prompt = getTraduction("Astro.ConstellationNamePrompt", "Name")
        name, ok = QInputDialog.getText(self, title, prompt, text=default_name)
        if not ok:
            return
        final_name = str(name or "").strip() or default_name
        self.canvas.create_constellation_group(final_name)
        self.canvas.set_constellation_draw_mode(True)
        self.canvas.setFocus()
        self._sync_constellation_controls()
    def finish_constellation_group(self):
        ctrl = self.canvas.constellation_controller
        if not bool(getattr(ctrl, "group_drawing_active", False)):
            return
        ctrl.finish_active_group()
        self.canvas.update()
        self.canvas.setFocus()
        self._sync_constellation_controls()
    def rename_constellation_group(self):
        ctrl = self.canvas.constellation_controller
        gi = getattr(ctrl, "active_group_index", None)
        if gi is None or not (0 <= gi < len(ctrl.groups)):
            return
        self.canvas.begin_inline_constellation_rename(group_index=int(gi))
        self._sync_constellation_controls()
    def rename_constellation_group_by_index(self, group_index: int, label_rect: Optional[QRectF] = None):
        ctrl = self.canvas.constellation_controller
        gi = int(group_index)
        if not (0 <= gi < len(ctrl.groups)):
            return
        ctrl.active_group_index = gi
        ctrl.selected_group_index = gi
        ctrl.selected_node_index = None
        ctrl.selected_segment_index = None
        self.canvas.begin_inline_constellation_rename(group_index=gi, label_rect=label_rect)
        self._sync_constellation_controls()
    def select_measurement_tool(self, tool: str):
        current = self.canvas.measurement_controller.active_tool
        if current == tool:
            self.canvas.set_measurement_tool(TOOL_NONE)
            self._sync_measure_tool_buttons(TOOL_NONE)
            return
        # Single active tool at a time.
        self.canvas.set_measurement_tool(tool)
        self.canvas.setFocus()
        self._sync_measure_tool_buttons(tool)
        # Measurement and scope mode should not compete for mouse/keys.
        self.exit_scope_mode()
        self.canvas.set_constellation_draw_mode(False)
        self._sync_constellation_controls()
    def clear_measurement_overlays(self):
        self.canvas.clear_measurements()
        self._sync_measure_tool_buttons(self.canvas.measurement_controller.active_tool)
    def on_time_bar_change(self, val):
        self.use_real_time = False
        self.btn_realtime.setChecked(False)
        self.manual_hour = val
        self._last_seek_hour = val
        self.canvas.update()
        self.canvas.update()
        # Time bar stays LOCAL; hint shows local and UTC from observer tz conversion.
        if hasattr(self.canvas, 'hint_overlay'):
            try:
                ut_h, _, _, _ = self.canvas._get_current_utc_context()
            except Exception:
                ut_h = float(val)
            lh = f"{int(val % 24):02d}:{int((val % 1)*60):02d}"
            uth = f"{int(ut_h % 24):02d}:{int(((ut_h % 24) % 1)*60):02d}"
            txt = getTraduction("HUD.TimeHint", "?? {local_h} local  Â·  UT {ut_h}").format(
                local_h=lh, ut_h=uth
            )
            self.canvas.hint_overlay.show_hint(txt)
    def request_relocation(self):
        return widget_request_relocation(self)
    def update_location(self):
        """Called by ReturnPressed on line edits."""
        self.request_relocation()
    def prev_day(self):
        self.manual_day = (self.manual_day - 1) % 365
        self.lbl_date.setText(self.format_date(self.manual_day))
        self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
        self.canvas.update()
    def next_day(self):
        self.manual_day = (self.manual_day + 1) % 365
        self.lbl_date.setText(self.format_date(self.manual_day))
        self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
        self.canvas.update()
    def load_catalog(self):
        return widget_load_catalog(self)
    def update_loop(self):
        return widget_update_loop(self)
    def toggle_realtime(self, checked):
        self.use_real_time = checked
        self.canvas.update()
    def toggle_view(self):
        # Toggle between Zenith (90) and Horizon (20)
        current = self.canvas.elevation_angle
        if abs(current - 90) < 5:
            self.set_horizon_view()
        else:
            self.set_zenith_view()
    def set_horizon_view(self):
        self.canvas.elevation_angle = 0.1 # Low angle for landscape
        self.canvas.vertical_offset_ratio = 0.35 # Horizon lower-third rule
        self.canvas.zoom_level = 1 # Wider FOV (~220 degrees)
        if self.latitude >= 0:
            self.canvas.azimuth_offset = 180 # Facing South (North Hemi)
        else:
            self.canvas.azimuth_offset = 0   # Facing North (South Hemi)
        if hasattr(self, "btn_view"):
            self.btn_view.setText(getTraduction("Astro.ViewZenith", "CÃ©nit"))
        self.canvas.update()
    def set_zenith_view(self):
        self.canvas.elevation_angle = 90 # Dome view up
        self.canvas.vertical_offset_ratio = 0.0 # Center
        self.canvas.zoom_level = 1.0 # Wide Fisheye
        self.canvas.azimuth_offset = 0
        if hasattr(self, "btn_view"):
            self.btn_view.setText(getTraduction("Astro.ViewHorizontal", "Horizonte"))
        self.canvas.update()
    def on_extra_height_changed(self, val):
        # Update worker state immediately for synchronous altitude label feedback
        if hasattr(self, 'horizon_worker'):
            self.horizon_worker.set_observer_offset(val)
            self.update_altitude_label()
            # Start debounce timer for the heavy topography recalculation
            # 1.5s debounce for spinbox as requested (let the user stop for a second)
            self.bake_debounce_timer.start(1500)
    def update_altitude_label(self):
        if hasattr(self, 'horizon_worker'):
            bare = self.horizon_worker.get_bare_elevation(self.latitude, self.longitude)
            offset = self.spin_extra_height.value() if hasattr(self, "spin_extra_height") else 0.0
            if bare is not None:
                total = bare + offset
                tpl = getTraduction("Astro.AltitudeInfo", "Altitud terreno: {dem} m | Total observador: {total} m")
                if hasattr(self, "lbl_altitude_info"):
                    self.lbl_altitude_info.setText(tpl.format(dem=f"{bare:.1f}", total=f"{total:.1f}"))
            else:
                fallback_str = getTraduction("Astro.AltitudeInfo", "Altitud terreno: {dem} m | Total observador: {total} m")
                fallback_str = fallback_str.replace("{dem}", "--").replace("{total}", "--")
                if hasattr(self, "lbl_altitude_info"):
                    self.lbl_altitude_info.setText(fallback_str)
    def update_star_scale(self, val):
        self.star_scale = val / 10.0
        self.canvas.update()
    def toggle_pure_colors(self, checked):
        self.pure_colors = checked
        self.canvas.update()
    def update_spikes(self, val):
        self.spike_magnitude_threshold = val / 10.0
        print(f"[DEBUG] Spike threshold updated to: {self.spike_magnitude_threshold} (slider val: {val})")
        self.canvas.update()
    def on_layers_changed(self, text):
        """Update layer count from UI combo box and trigger a re-bake."""
        try:
            val = int(text)
            from TerraLab.common.utils import set_config_value
            set_config_value("horizon_quality", val)
            if hasattr(self, 'horizon_worker'):
                self.horizon_worker.reload_config()
            self.request_relocation()
        except ValueError:
            pass
    def configure_terrain(self):
        """Open DEM configuration dialog."""
        from TerraLab.widgets.terrain_config_dialog import TerrainConfigDialog
        dlg = TerrainConfigDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            # Re-read config in worker
            if hasattr(self, 'horizon_worker'):
                self.horizon_worker.reload_config()
            # Trigger re-bake
            self.request_relocation()
    def update_illusion_enabled(self, checked):
        self.canvas.illusion_enabled = checked
        self.canvas.update()
    def update_horizon_refs(self, val):
        self.canvas.horizon_refs = val / 100.0
        self.canvas.update()
    def update_dome_flattening(self, val):
        self.canvas.dome_flattening = val / 100.0
        self.canvas.update()
    def update_trained_observer(self, checked):
        self.canvas.trained_observer = checked
        self.canvas.update()
    def update_eclipse_lock(self, checked):
        self.canvas.eclipse_lock_mode = checked
        self.canvas.update()
    def animate_view(self):
        running = False
        # 1. Azimuth Animation
        if self.target_azimuth is not None:
            # Normalize Input First to avoid "Unwinding" large rotations
            self.canvas.azimuth_offset %= 360
            current = self.canvas.azimuth_offset
            diff = self.target_azimuth - current
            # Normalize -180..180 (Shortest Path)
            diff = (diff + 180) % 360 - 180
            if abs(diff) < 0.5:
                self.canvas.azimuth_offset = self.target_azimuth
                self.target_azimuth = None
            else:
                running = True
                # More agile speed (0.25)
                step = diff * 0.25
                # Min speed to snap
                if abs(step) < 0.5: step = 0.5 if step > 0 else -0.5
                self.canvas.azimuth_offset = (current + step) % 360
        # 2. Elevation Animation
        if getattr(self, 'target_elevation', None) is not None:
            current_el = self.canvas.elevation_angle
            diff_el = self.target_elevation - current_el
            if abs(diff_el) < 0.5:
                self.canvas.elevation_angle = self.target_elevation
                self.target_elevation = None
            else:
                running = True
                step_el = diff_el * 0.1
                if abs(step_el) < 0.5: step_el = 0.5 if step_el > 0 else -0.5
                self.canvas.elevation_angle = current_el + step_el
        if not running:
            self.anim_timer.stop()
            self.canvas.dragging = False
        else:
            self.canvas.dragging = True
        self.canvas.update()
    def on_trails_toggled(self, checked):
        self._update_circumpolar_button_text()
        if checked:
            self.target_azimuth = 0 # Rotate to North
            # Point to Polaris (Altitude = Latitude)
            self.target_elevation = self.latitude
        else:
            self.target_azimuth = 180 # Return to South
            self.target_elevation = 40 # Default nice view
        self.anim_timer.start(16) # ~60 FPS
    def _update_circumpolar_button_text(self):
        if not hasattr(self, "chk_trails"):
            return
        checked = bool(self.chk_trails.isChecked())
        if checked:
            self.chk_trails.setText(getTraduction("Astro.StopCircumpolar", "Aturar circumpolar"))
        else:
            self.chk_trails.setText(getTraduction("Astro.StartCircumpolar", "Iniciar circumpolar"))
    def get_month_name(self, month_idx):
        # Manual translation since locale might be erratic
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        keys = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        m_en = months[month_idx - 1]
        return getTraduction(f"Month.{keys[month_idx-1]}", m_en)
    def format_date(self, day_index):
        # Conversion using manual_year
        date = datetime(self.manual_year, 1, 1) + timedelta(days=int(day_index))
        # Custom localized format
        month_name = self.get_month_name(date.month)
        return f"{date.day} {month_name} {date.year}"
    def update_date(self, val):
        self.use_real_time = False
        self.btn_realtime.setChecked(False)
        self.manual_day = val
        self.lbl_date.setText(self.format_date(val))
        # Sync Gradient
        self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
        self.canvas.update()
    def prev_day(self):
        self.update_date(self.manual_day - 1)
    def next_day(self):
        self.update_date(self.manual_day + 1)
        # Update gradient when date changes
        self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
        self.canvas.update()
    def open_calendar(self):
        return widget_open_calendar(self)
    def on_lp_mode_changed(self, index):
        """index 0 = Automatic (Bortle), index 1 = Manual (Magnitud)."""
        self.is_auto_bortle = (index == 0)
        set_config_value("is_auto_bortle", bool(self.is_auto_bortle))
        if self.is_auto_bortle:
            self.lbl_light_text.setText(getTraduction("Astro.BortleLabel", "Bortle"))
            self.slider_light.setRange(1, 9)
            if hasattr(self.slider_light, '_lbl_min'):
                self.slider_light._lbl_min.setText("1")
                self.slider_light._lbl_max.setText("9")
            auto_val = getattr(self.canvas, 'auto_bortle_estimate', 4)
            self.slider_light.set_silent_value(int(auto_val))
        else:
            self.lbl_light_text.setText(getTraduction("Astro.MagnitudeLabel", "Magnitude"))
            self.slider_light.setRange(-270, 100) # -27.0 to 10.0
            if hasattr(self.slider_light, '_lbl_min'):
                self.slider_light._lbl_min.setText("-27")
                self.slider_light._lbl_max.setText("10")
            self.slider_light.set_silent_value(int(self.magnitude_limit * 10))
        self.canvas.update()
    def on_stars_toggled(self, checked):
        checked = bool(checked)
        if checked and (not self._ensure_asset_before_enable(self.chk_enable_sky, checked, "gaia_catalog")):
            checked = False
        if checked != bool(self.chk_enable_sky.isChecked()):
            self.chk_enable_sky.blockSignals(True)
            self.chk_enable_sky.setChecked(checked)
            self.chk_enable_sky.blockSignals(False)
        self._persist_visibility_state("estrelles", checked)
        self._refresh_stars_status_indicator()
        self.canvas.update()
    def on_climate_toggled(self, checked):
        checked = bool(checked)
        if checked and (not self._ensure_asset_before_enable(self.chk_clima, checked, "climate_metno")):
            checked = False
        if checked != bool(self.chk_clima.isChecked()):
            self.chk_clima.blockSignals(True)
            self.chk_clima.setChecked(checked)
            self.chk_clima.blockSignals(False)
        self._persist_visibility_state("clima", checked)
        if hasattr(self.canvas, "weather"):
            self.canvas.weather.enabled = checked
            self.canvas.weather.set_remote_user_agent(self.asset_manager.get_user_agent())
        if hasattr(self, "weather"):
            self.weather.enabled = checked
            self.weather.set_remote_user_agent(self.asset_manager.get_user_agent())
        if checked:
            self._ensure_copernicus_credentials_prompt()
        self._refresh_climate_status_indicator()
        self.canvas.update()
    def on_light_pollution_toggled(self, checked):
        checked = bool(checked)
        if checked and (not self._ensure_asset_before_enable(self.chk_light_pollution, checked, "light_pollution")):
            checked = False
        if checked != bool(self.chk_light_pollution.isChecked()):
            self.chk_light_pollution.blockSignals(True)
            self.chk_light_pollution.setChecked(checked)
            self.chk_light_pollution.blockSignals(False)
        self.light_pollution_enabled = bool(checked)
        self._persist_visibility_state("contaminacio_luminica", checked)
        set_config_value("light_pollution_enabled", bool(checked))
        if hasattr(self, "horizon_worker"):
            self.horizon_worker.reload_config()
            if not (os.name == "nt" and sys.version_info >= (3, 13)):
                QTimer.singleShot(
                    0,
                    lambda: QMetaObject.invokeMethod(self.horizon_worker, "initialize", Qt.QueuedConnection),
                )
        if checked:
            if bool(getattr(self, "is_auto_bortle", True)):
                QTimer.singleShot(80, self.reset_lp_to_auto)
        else:
            # Amb LP desactivada, el comportament esperat és equivalent a Bortle 1.
            self.auto_bortle_estimate = 1
            self.canvas.auto_bortle_estimate = 1
            set_config_value("auto_bortle_estimate", 1)
            if hasattr(self, "slider_light") and bool(getattr(self, "is_auto_bortle", True)):
                self.slider_light.set_silent_value(1)
        self.canvas.update()
    def on_milkyway_toggled(self, checked):
        checked = bool(checked)
        if checked and (not self._ensure_asset_before_enable(self.chk_enable_milkyway, checked, "milkyway_texture")):
            checked = False
        if checked != bool(self.chk_enable_milkyway.isChecked()):
            self.chk_enable_milkyway.blockSignals(True)
            self.chk_enable_milkyway.setChecked(checked)
            self.chk_enable_milkyway.blockSignals(False)
        self._persist_visibility_state("via_lactia", checked)
        self.milkyway_overlay_enabled = bool(checked)
        set_config_value("milkyway_overlay_enabled", bool(self.milkyway_overlay_enabled))
        self._refresh_milkyway_status_indicator()
        self.canvas.update()
    def on_planck_dust_toggled(self, checked):
        checked = bool(checked)
        if checked and (not self._ensure_asset_before_enable(self.chk_enable_planck_dust, checked, "planck_dust")):
            checked = False
        if checked != bool(self.chk_enable_planck_dust.isChecked()):
            self.chk_enable_planck_dust.blockSignals(True)
            self.chk_enable_planck_dust.setChecked(checked)
            self.chk_enable_planck_dust.blockSignals(False)
        self._persist_visibility_state("pols_planck", checked)
        self.dust_map_enabled = bool(checked)
        set_config_value("dust_map_enabled", bool(self.dust_map_enabled))
        if self.dust_map_enabled and float(getattr(self, "dust_density_strength", 0.0)) <= 0.0 and float(getattr(self, "dust_extinction_strength", 0.0)) <= 0.0:
            self.dust_extinction_strength = 0.65
            set_config_value("dust_extinction_strength", float(self.dust_extinction_strength))
        self._refresh_milkyway_status_indicator()
        self.canvas.update()
    def on_deep_space_toggled(self, checked):
        checked = bool(checked)
        if checked and (not self._ensure_asset_before_enable(self.chk_deep_space, checked, "ngc_catalog")):
            checked = False
        if checked != bool(self.chk_deep_space.isChecked()):
            self.chk_deep_space.blockSignals(True)
            self.chk_deep_space.setChecked(checked)
            self.chk_deep_space.blockSignals(False)
        self._persist_visibility_state("espai_profund", checked)
        self._ngc_search_entries_cache = None
        self.build_search_index()
        self.canvas.update()
    def on_topography_toggled(self, checked):
        checked = bool(checked)
        if checked and (not self._ensure_asset_before_enable(self.chk_enable_village, checked, "elevation_dem")):
            checked = False
        if checked != bool(self.chk_enable_village.isChecked()):
            self.chk_enable_village.blockSignals(True)
            self.chk_enable_village.setChecked(checked)
            self.chk_enable_village.blockSignals(False)
        self._persist_visibility_state("topografia", checked)
        self.canvas.update()
    def _refresh_milkyway_status_indicator(self):
        return widget_refresh_milkyway_status_indicator(self)
    def set_weather_remote_metno_enabled(self, enabled: bool):
        enabled = bool(enabled)
        if enabled and not self.asset_manager.asset_ready("climate_metno"):
            self._open_asset_onboarding("climate_metno")
            enabled = self.asset_manager.asset_ready("climate_metno")
        self.weather_use_remote_metno = enabled
        set_config_value("weather_use_remote_metno", enabled)
        if hasattr(self, "weather"):
            self.weather.set_remote_weather_enabled(enabled)
            self.weather.set_remote_user_agent(self.asset_manager.get_user_agent())
        if hasattr(self, "canvas") and hasattr(self.canvas, "weather"):
            self.canvas.weather.set_remote_weather_enabled(enabled)
            self.canvas.weather.set_remote_user_agent(self.asset_manager.get_user_agent())
        self._refresh_climate_status_indicator()
    def set_weather_cache_enabled(self, enabled: bool):
        enabled = bool(enabled)
        self.weather_cache_enabled = enabled
        set_config_value("weather_cache_enabled", enabled)
        if hasattr(self, "weather"):
            self.weather.set_cache_enabled(enabled)
        if hasattr(self, "canvas") and hasattr(self.canvas, "weather"):
            self.canvas.weather.set_cache_enabled(enabled)
        self._refresh_climate_status_indicator()
    def _active_weather_system(self):
        if hasattr(self, "canvas") and hasattr(self.canvas, "weather"):
            return self.canvas.weather
        if hasattr(self, "weather"):
            return self.weather
        return None
    def _refresh_climate_status_indicator(self):
        return widget_refresh_climate_status_indicator(self)
    def _refresh_stars_status_indicator(self):
        if not hasattr(self, "lbl_stars_fallback") or not hasattr(self, "chk_enable_sky"):
            return
        if not bool(self.chk_enable_sky.isChecked()):
            self.lbl_stars_fallback.hide()
            self._stars_fallback_since = None
            self._stars_primary_since = None
            return
        fallback_active = bool(getattr(self, "_stars_fallback_active", False))
        fallback_reason = str(getattr(self, "_stars_fallback_reason", "") or "")
        now_m = time.monotonic()
        if not fallback_active:
            self._stars_fallback_since = None
            if self._stars_primary_since is None:
                self._stars_primary_since = now_m
            stable_s = now_m - self._stars_primary_since
            if stable_s >= float(getattr(self, "_stars_primary_hide_delay_s", 0.8)):
                self.lbl_stars_fallback.hide()
            return
        self._stars_primary_since = None
        if self._stars_fallback_since is None:
            self._stars_fallback_since = now_m
        stable_s = now_m - self._stars_fallback_since
        if stable_s >= float(getattr(self, "_stars_fallback_show_delay_s", 1.2)):
            txt = getTraduction("Astro.StarsFallbackActive", "CatÃ leg fallback")
            self.lbl_stars_fallback.setText(txt)
            self.lbl_stars_fallback.setToolTip(fallback_reason)
            self.lbl_stars_fallback.show()
    def get_weather_cache_path(self):
        if hasattr(self, "canvas") and hasattr(self.canvas, "weather"):
            return self.canvas.weather.get_cache_path()
        if hasattr(self, "weather"):
            return self.weather.get_cache_path()
        return None
    def _ensure_copernicus_credentials_prompt(self):
        return widget_ensure_copernicus_credentials_prompt(self)
    def _request_auto_bortle_estimate(self) -> bool:
        """Demana una estimacio Bortle al HorizonWorker de forma asincrona."""
        worker = getattr(self, "horizon_worker", None)
        if worker is None or not hasattr(self, "request_horizon_bortle"):
            return False
        request_id = int(getattr(self, "_auto_bortle_request_seq", 0)) + 1
        self._auto_bortle_request_seq = request_id
        self._auto_bortle_pending_request_id = request_id
        self.request_horizon_bortle.emit(
            float(self.latitude),
            float(self.longitude),
            int(request_id),
        )
        return True
    def _apply_auto_bortle_estimate(self, bortle_value: int) -> None:
        """Aplica una classe Bortle validada al model visual i a la UI."""
        valor_bortle = int(max(1, min(9, int(bortle_value))))
        print(f"[AstroWidget] Resetting LP to auto-estimated Bortle: {valor_bortle}")
        self.combo_lp_mode.setCurrentIndex(0)
        self.slider_light.set_silent_value(valor_bortle)
        self.auto_bortle_estimate = valor_bortle
        self.canvas.auto_bortle_estimate = valor_bortle
        set_config_value("auto_bortle_estimate", int(valor_bortle))
        if hasattr(self.canvas, 'weather'):
            self.canvas.weather.set_bortle(valor_bortle)
        self.canvas.update()
    def on_horizon_bortle_estimate(self, request_id: int, lat: float, lon: float, bortle_value: int) -> None:
        """Rep l'estimacio Bortle del worker i descarta respostes antigues."""
        pending_request_id = int(getattr(self, "_auto_bortle_pending_request_id", 0))
        if pending_request_id and int(request_id) != pending_request_id:
            return
        self._auto_bortle_pending_request_id = 0
        self._apply_auto_bortle_estimate(int(bortle_value))
    def reset_lp_to_auto(self):
        """Demana l'estimacio Bortle actual al worker (no bloquejant)."""
        self._request_auto_bortle_estimate()
    def update_lp_slider(self, val):
        if self.is_auto_bortle:
            self.auto_bortle_estimate = val
            self.canvas.auto_bortle_estimate = val
            set_config_value("auto_bortle_estimate", int(val))
            if hasattr(self.canvas, 'weather'):
                self.canvas.weather.set_bortle(val)
        else:
            # Manual mode: Sliders acts as Magnitude Filter
            self.magnitude_limit = val / 10.0
            set_config_value("manual_eye_limit_mag", float(self.magnitude_limit))
        self.canvas.update()
    def update_magnitude(self, val):
        # Mag slider 10-200 -> 1.0-20.0
        self.magnitude_limit = val / 10.0
        set_config_value("manual_eye_limit_mag", float(self.magnitude_limit))
        self.canvas.update()
    def build_search_index(self):
        return build_search_index_for_widget(self)
    def _attach_search_completer(self, names):
        if not hasattr(self, "txt_search"):
            return
        from PyQt5.QtWidgets import QCompleter
        from PyQt5.QtCore import Qt
        completer = QCompleter(names, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.txt_search.setCompleter(completer)
        completer.activated[str].connect(self.on_search_triggered)
        self.txt_search.setEnabled(True)
    def _load_named_star_search_entries(self):
        cached = getattr(self, "_named_star_search_entries_cache", None)
        if cached is not None:
            return cached
        default_path = Path(__file__).resolve().parents[1] / "data" / "stars" / "no_gaia_stars.json"
        self._named_star_search_entries_cache = load_named_star_entries(default_path)
        return self._named_star_search_entries_cache
    def _load_ngc_search_entries(self):
        cached = getattr(self, "_ngc_search_entries_cache", None)
        if cached is not None:
            return cached
        path = getattr(self, "_astro_ngc_catalog_path", "")
        if not path:
            cfg_path = str(get_config_value("ngc_catalog_path", "") or "").strip()
            if cfg_path and os.path.isfile(cfg_path):
                path = cfg_path
        if not path:
            runtime_layout = getattr(self, "runtime_layout", {}) or {}
            runtime_path = Path(runtime_layout.get("data_ngc", get_base_dir())) / "openngc_catalog.csv"
            if runtime_path.exists():
                path = str(runtime_path)
            else:
                path = os.path.abspath(
                    os.path.join(os.path.dirname(__file__), "..", "data", "sky", "openngc_catalog.csv")
                )
        self._ngc_search_entries_cache = load_ngc_entries(path)
        return self._ngc_search_entries_cache
    def _normalize_search_key(text: str) -> str:
        return AstroSearchEngine.normalize_key(text)
    def _prepare_skyfield_cache_for_search(self):
        """Force a fresh cache sample so planet search can resolve current Alt/Az."""
        if not SKYFIELD_AVAILABLE or not hasattr(self, 'canvas') or not hasattr(self, 'eph'):
            return None
        try:
            ut_hour, day_of_year_utc, _, _ = self.canvas._get_current_utc_context()
            self.canvas.update_skyfield_cache(ut_hour, day_of_year_utc)
        except Exception as ex:
            print(f"[AstroWidget] Search cache update failed: {ex}")
        sf_cache = getattr(self.canvas, '_sf_cache', None)
        if isinstance(sf_cache, dict):
            return sf_cache.get('data')
        return None
    def on_search_triggered(self, text_override=None):
        return on_search_triggered_for_widget(self, text_override)
    def center_on_object(self, info):
        return center_on_object_for_widget(self, info)
    def get_horizontal_coords(self, ra, dec):
        """Helper to convert RA/Dec to Az/Alt for current time/location."""
        alt_deg, az_deg = ra_dec_to_alt_az(
            float(ra),
            float(dec),
            float(self.time_bar.current_hour),
            int(self.manual_day),
            float(self.latitude),
            float(self.longitude),
        )
        return az_deg, alt_deg
    def run_smoke_scenes(self):
        return widget_run_smoke_scenes(self)
    def toggle_controls(self):
        if not hasattr(self, 'panels_widget'): return
        # Current state based on panel visibility
        is_visible = self.panels_widget.isVisible()
        should_hide = is_visible # If visible, we want to hide
        # Toggle Panels
        self.panels_widget.setVisible(not should_hide)
        # Update styling/transparency
        if should_hide:
            # COLLAPSED STATE: Transparent background, no border
            # Only time bar is visible (row 2)
            self.frame_controls.setStyleSheet("QFrame { background: transparent; border: none; }")
            self.time_bar.setVisible(True) # Keep timebar
            self.btn_collapse.setText("+")
        else:
            # EXPANDED STATE: Restore Theme Style
            self.update_custom_theme()
            self.btn_collapse.setText("-")
        # Force layout update to recalculate geometry of frame_controls
        self.layout().activate()
        QApplication.processEvents()
        # Manually update button position
        self._update_button_pos()
    def _update_button_pos(self):
        if hasattr(self, 'frame_controls') and hasattr(self, 'btn_collapse'):
            rect = self.frame_controls.geometry()
            state = {
                "panel_x": int(rect.x()),
                "panel_y": int(rect.y()),
                "panel_w": int(rect.width()),
                "button_w": int(self.btn_collapse.width()),
                "button_h": int(self.btn_collapse.height()),
                "margin_right": 20,
                "overlap_top": 1,
            }
            telescope_on_resize(state)
            bx, by = state.get("collapse_button_pos", (rect.right(), rect.top()))
            self.btn_collapse.move(bx, by)
            self.btn_collapse.raise_()
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_startup_placeholder()
        self._position_gaia_extension_status_label()
        # Defer position update to ensure layout geometry is final
        QTimer.singleShot(0, self._update_button_pos)
    def get_current_hour(self):
        if self.use_real_time:
            n = datetime.now()
            return n.hour + n.minute/60.0
        return self.manual_hour
"""  """










