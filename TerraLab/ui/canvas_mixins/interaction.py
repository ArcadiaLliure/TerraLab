"""Canvas input state, visible-object registry, and picking support."""

from __future__ import annotations

import math
import time
from typing import Optional

from PyQt5.QtCore import QEvent, QRectF, Qt
from PyQt5.QtWidgets import QLineEdit

from TerraLab.common.exception_reporting import log_suppressed_exception
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

    def set_hud_visible(self, visible):
        """Mostra o amaga només la caixa informativa del visor."""

        self.hud_visible = bool(visible)
        if hasattr(self, "btn_human_eye"):
            self.btn_human_eye.setVisible(bool(visible))
        if hasattr(self, "btn_hud_toggle"):
            self.btn_hud_toggle.setText(
                "\U0001F441  HUD" if visible else "\u25cb  HUD"
            )
        self.update()

    def _position_hud_toggle(self):
        if not hasattr(self, "btn_hud_toggle"):
            return
        margin = 12
        self.btn_hud_toggle.move(
            max(margin, self.width() - self.btn_hud_toggle.width() - margin),
            margin,
        )
        self.btn_hud_toggle.raise_()

    def resizeEvent(self, event):
        self._bg_cache_key = None
        weather = getattr(self, "weather", None)
        if weather is not None:
            weather.resize(self.width(), self.height())
        self._position_hud_toggle()
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
        update_pulse = getattr(
            self, "_update_selection_pulse_timer", None
        )
        if callable(update_pulse):
            update_pulse()
        self._refresh_overlay_cursor()
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
            "background: rgba(8, 12, 22, 240);"
            "color: #f3f5fa;"
            "border: 1px solid #d8b26a;"
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

    def clear_measurements(self) -> None:
        self.measurement_controller.clear()
        self._measurement_clear_revision = (
            int(getattr(self, "_measurement_clear_revision", 0)) + 1
        )
        self._refresh_overlay_cursor()
        self.update()

    def _refresh_overlay_cursor(self):
        if self.scope_mode_enabled() or self.measurement_tool_active() or self.drawing_mode_enabled():
            self.setCursor(Qt.CrossCursor)
        else:
            self.unsetCursor()

    def _mark_scope_interaction(self, hold_seconds: float = 0.20) -> None:
        self._view_interaction_revision = int(
            getattr(self, "_view_interaction_revision", 0)
        ) + 1
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
        self._view_interaction_revision = int(
            getattr(self, "_view_interaction_revision", 0)
        ) + 1
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
        anim_active = (
            bool(
                getattr(self.parent_widget, "target_azimuth", None)
                is not None
                or getattr(self.parent_widget, "target_elevation", None)
                is not None
            )
            if include_animation
            else False
        )
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

