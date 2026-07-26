"""Canvas input state, visible-object registry, and picking support."""

from __future__ import annotations

import html
import math
import time
from typing import Optional

from PyQt5.QtCore import QEvent, QRectF, Qt
from PyQt5.QtWidgets import QLineEdit, QToolTip

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.scene.projection import unproject_universal_stereo_point
from TerraLab.terrain.data_sources import SurfaceMode
from TerraLab.ui.canvas_runtime_helpers import canvas_update_skyfield_cache
from TerraLab.widgets.measurement_tools import TOOL_NONE
from TerraLab.widgets.telescope_scope_mode import TelescopeScopeController


class CanvasInteractionMixin:
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
        self._refresh_overlay_cursor()
        self.update()

    def set_constellation_visible(self, visible: bool) -> None:
        self.finish_inline_constellation_rename(apply=True)
        self.constellation_controller.set_visible(bool(visible))
        if not bool(visible):
            self.constellation_controller.set_enabled(False)
        self._refresh_overlay_cursor()
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
            log_suppressed_exception(__name__, "CanvasInteractionMixin.finish_inline_constellation_rename")
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

    def _surface_category_at_pointer(self, position):
        """Return cached land-cover metadata when passive hover is allowed."""

        parent = getattr(self, "parent_widget", None)
        manager = getattr(parent, "layer_manager", None)
        registry = getattr(manager, "data_sources", None)
        surface_enabled = self._parent_checkbox_checked(
            "chk_surface_layer", True
        )
        categorical_mode = bool(
            registry is not None
            and registry.surface_mode is SurfaceMode.LAND_COVER
        )
        interactive = bool(
            getattr(self, "dragging", False)
            or self.scope_mode_enabled()
            or self.measurement_tool_active()
            or self.drawing_mode_enabled()
            or getattr(
                getattr(self, "scope_controller", None),
                "dragging",
                False,
            )
            or getattr(parent, "_dragging_time", False)
        )
        if not surface_enabled or not categorical_mode or interactive:
            return None
        lookup = getattr(
            getattr(self, "horizon_overlay", None),
            "category_at_screen",
            None,
        )
        if not callable(lookup):
            return None
        return lookup(position.x(), position.y())

    def _update_surface_tooltip_for_pointer(self, event) -> bool:
        """Update the land-cover tooltip directly from a pointer event."""

        info = CanvasInteractionMixin._surface_category_at_pointer(
            self, event.pos()
        )
        if info is None:
            QToolTip.hideText()
            return False
        name = html.escape(str(info.name))
        description = html.escape(str(info.description))
        product = html.escape(str(info.product))
        QToolTip.showText(
            event.globalPos(),
            f"<b>{name}</b><br>{description}<br>"
            f"Classe {int(info.class_id)} · {product}",
            self,
        )
        return True

    def event(self, event):
        if event.type() == QEvent.ToolTip:
            if CanvasInteractionMixin._update_surface_tooltip_for_pointer(
                self, event
            ):
                event.accept()
                return True
            event.ignore()
            return True
        if event.type() == QEvent.Leave:
            QToolTip.hideText()
        return super().event(event)

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

    def _mark_camera_interaction(self, hold_seconds: float = 0.20) -> None:
        """Keep transient camera LOD active until wheel/step input settles."""
        try:
            hold = max(0.0, float(hold_seconds))
        except Exception:
            hold = 0.2
        self._camera_interaction_until = max(
            float(getattr(self, "_camera_interaction_until", 0.0)),
            time.monotonic() + hold,
        )
        timer = getattr(self, "_camera_idle_timer", None)
        if timer is not None:
            # Small guard avoids an early timer firing on coarse Windows clocks
            # before the monotonic hold deadline has actually elapsed.
            timer.start(max(1, int(round(hold * 1000.0)) + 25))

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
            log_suppressed_exception(__name__, "CanvasInteractionMixin._scope_motion_active")
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
        wheel_or_step_active = time.monotonic() < float(
            getattr(self, "_camera_interaction_until", 0.0)
        )
        return bool(
            (
                self.dragging
                or dragging_time
                or anim_active
                or wheel_or_step_active
            )
            and (not self._scope_reticle_drag_active())
        )

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
                log_suppressed_exception(__name__, "CanvasInteractionMixin._lookup_visible_sky_object")
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
            log_suppressed_exception(__name__, "CanvasInteractionMixin._resolve_star_by_index")
        for ch in ("r", "g", "b"):
            arr = getattr(pw, f"np_{ch}", None)
            try:
                if arr is not None and idx < len(arr):
                    val = float(arr[idx])
                    if math.isfinite(val):
                        star[ch] = val
            except Exception:
                log_suppressed_exception(__name__, "CanvasInteractionMixin._resolve_star_by_index")
        return star

    def _pick_sky_object_at(self, sx: float, sy: float, click_radius: float = 20.0):
        return self._selection.pick_sky_object_at(float(sx), float(sy), float(click_radius))

