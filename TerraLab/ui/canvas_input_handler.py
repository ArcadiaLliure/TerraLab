"""Delegates Qt input events from AstroCanvas to extracted handlers."""

from __future__ import annotations

import time

from PyQt5.QtCore import QRectF, Qt, QTimer
from PyQt5.QtWidgets import QApplication, QWidget

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.utils import set_config_value
from TerraLab.widgets.spherical_math import screen_to_sky
from TerraLab.widgets.telescope_scope_mode import TelescopeScopeController


class CanvasInputHandler:
    def __init__(self, canvas):
        self._canvas = canvas

    def handle_mouse_press(self, event):
        """Executa el metode handle_mouse_press de la classe CanvasInputHandler.

        Par?metres:
        - event (Any): Valor del parametre 'event'.

        Retorna:
        - None.
        """
        c = self._canvas
        if c.drawing_mode_enabled():
            if event.button() == Qt.LeftButton:
                c.setFocus()
                c.press_pos = event.pos()
                if event.modifiers() & Qt.ControlModifier:
                    # Drawing override: Ctrl + drag keeps normal camera navigation.
                    c.dragging = True
                    c.last_mouse_x = event.x()
                    c.last_mouse_y = event.y()
                    c._drawing_ctrl_pan_started = False
                    c._drawing_ctrl_click_pending = True
                else:
                    c._drawing_ctrl_pan_started = False
                    c._drawing_ctrl_click_pending = False
                    ut_hour, day_of_year_utc, year_utc, _ = (
                        c._get_current_utc_context()
                    )
                    c.constellation_controller.on_left_click(
                        event.x(),
                        event.y(),
                        c.project_universal_stereo,
                        lambda ra, dec: c._ra_dec_to_alt_az(
                            ra, dec, ut_hour, day_of_year_utc, year_utc=year_utc
                        ),
                        c._pick_star_at,
                        force_add=bool(event.modifiers() & Qt.ShiftModifier),
                        additive_select=False,
                    )
                    if hasattr(
                        c.parent_widget, "_sync_constellation_controls"
                    ):
                        c.parent_widget._sync_constellation_controls()
                    c.update()
            elif event.button() == Qt.RightButton:
                c.setFocus()
                ut_hour, day_of_year_utc, year_utc, _ = c._get_current_utc_context()
                c.constellation_controller.on_right_click(
                    event.x(),
                    event.y(),
                    c.project_universal_stereo,
                    lambda ra, dec: c._ra_dec_to_alt_az(
                        ra, dec, ut_hour, day_of_year_utc, year_utc=year_utc
                    ),
                )
                if hasattr(c.parent_widget, "_sync_constellation_controls"):
                    c.parent_widget._sync_constellation_controls()
                c.update()
            event.accept()
            return

        if c.scope_mode_enabled():
            if event.button() == Qt.LeftButton:
                c.press_pos = event.pos()
                if event.modifiers() & Qt.ControlModifier:
                    # Ctrl + drag: només càmera. La mira es manté fixa.
                    c.scope_camera_lock_to_target = False
                    c.scope_controller.end_drag()
                    c.dragging = True
                    c.last_mouse_x = event.x()
                    c.last_mouse_y = event.y()
                    c._scope_camera_pan_started = False
                    c._scope_camera_click_pending = True
                    c._scope_combined_drag_active = False
                    c._scope_reticle_drag_armed = False
                else:
                    # Drag without Ctrl: move scope reticle.
                    # Manual reticle movement exits any active target-lock flow.
                    # This prevents a previous Goto target from re-steering the camera
                    # right after the user manually centers the reticle on a new star.
                    if getattr(c, "selected_target", None) is not None:
                        c._set_selected_target(None)
                    c.scope_reticle_lock_to_target = False
                    c.scope_camera_lock_to_target = False
                    c.dragging = False
                    c._scope_camera_pan_started = False
                    c._scope_camera_click_pending = False
                    c._scope_combined_drag_active = False
                    c._scope_reticle_drag_armed = True
                    c.last_mouse_x = event.x()
                    c.last_mouse_y = event.y()
                    # Avoid reusing a previously camera-throttled star frame while starting reticle drag.
                    c._cached_star_image = None
                    click_sky = screen_to_sky(
                        float(event.x()),
                        float(event.y()),
                        c.unproject_stereo,
                    )
                    before_center = getattr(c.scope_controller, "center", None)
                    log_fn = getattr(c, "_goto_debug_log", None)
                    debug_enabled_fn = getattr(c, "_goto_debug_enabled", None)
                    debug_enabled = (
                        bool(debug_enabled_fn())
                        if callable(debug_enabled_fn)
                        else False
                    )
                    if callable(log_fn) and debug_enabled:
                        log_fn(
                            "scope_left_press "
                            f"s=({event.x():.1f},{event.y():.1f}) "
                            f"sky={click_sky} "
                            f"before_center={before_center} "
                            f"cam=({float(getattr(c, 'elevation_angle', 0.0)):.5f},"
                            f"{float(getattr(c, 'azimuth_offset', 0.0)):.5f})"
                        )
                    handled = c.scope_controller.handle_click(
                        event.x(), event.y(), c.unproject_stereo
                    )
                    if bool(handled):
                        c.scope_controller.start_drag(event.x(), event.y())
                    mark_interaction = getattr(c, "_mark_scope_interaction", None)
                    if callable(mark_interaction):
                        mark_interaction(0.25)
                    if bool(handled):
                        parent_widget = getattr(c, "parent_widget", None)
                        if parent_widget is not None and hasattr(
                            parent_widget, "_ensure_scope_catalog_loaded"
                        ):
                            QTimer.singleShot(
                                0,
                                lambda w=parent_widget: w._ensure_scope_catalog_loaded(
                                    force_now=True
                                ),
                            )
                    if callable(log_fn) and debug_enabled:
                        log_fn(
                            "scope_left_press_result "
                            f"handled={bool(handled)} "
                            f"after_center={getattr(c.scope_controller, 'center', None)}"
                        )
                c.update()
            event.accept()
            return

        if c.measurement_tool_active():
            if event.button() == Qt.LeftButton:
                c.press_pos = event.pos()
                if event.modifiers() & Qt.ControlModifier:
                    # Measurement override: Ctrl + drag keeps normal camera navigation.
                    c.dragging = True
                    c.last_mouse_x = event.x()
                    c.last_mouse_y = event.y()
                    c.press_pos = event.pos()
                else:
                    c.measurement_controller.on_mouse_press(
                        event.x(),
                        event.y(),
                        c.unproject_stereo,
                        c.project_universal_stereo,
                    )
                c.update()
            event.accept()
            return

        if event.button() == Qt.LeftButton:
            c.dragging = True
            c.last_mouse_x = event.x()
            c.last_mouse_y = event.y()
            c.press_pos = event.pos()

    def handle_mouse_move(self, event):
        """Executa el metode handle_mouse_move de la classe CanvasInputHandler.

        Par?metres:
        - event (Any): Valor del parametre 'event'.

        Retorna:
        - None.
        """
        c = self._canvas
        tooltip_updater = getattr(
            c, "_update_surface_tooltip_for_pointer", None
        )
        if callable(tooltip_updater):
            tooltip_updater(event)
        if c.drawing_mode_enabled():
            if c.dragging:
                dx = event.x() - c.last_mouse_x
                dy = event.y() - c.last_mouse_y
                if c._drawing_ctrl_click_pending and (abs(dx) + abs(dy) >= 3):
                    c._drawing_ctrl_pan_started = True
                if c._drawing_ctrl_pan_started:
                    c.azimuth_offset = c.azimuth_offset - dx * 0.5
                    c.elevation_angle += dy * 0.5
                    c.elevation_angle = max(-90, min(90, c.elevation_angle))
                c.last_mouse_x = event.x()
                c.last_mouse_y = event.y()
            else:
                c.setFocus()
                c.constellation_controller.on_mouse_move(
                    event.x(),
                    event.y(),
                    c._pick_star_at,
                    c._screen_to_ra_dec,
                )
            c.update()
            event.accept()
            return

        if c.scope_mode_enabled():
            # Failsafe: if mouse release is missed (focus change, OS menu, etc.),
            # clear stale drag flags so scope GoTo/reticle control cannot remain stuck.
            try:
                left_pressed = bool(
                    int(QApplication.mouseButtons()) & int(Qt.LeftButton)
                )
            except Exception:
                left_pressed = True
            if not left_pressed:
                if bool(getattr(c.scope_controller, "dragging", False)):
                    c.scope_controller.end_drag()
                if bool(getattr(c, "dragging", False)):
                    c.dragging = False
                c._scope_camera_pan_started = False
                c._scope_camera_click_pending = False
                c._scope_combined_drag_active = False
                c._scope_reticle_drag_armed = False

            if c.dragging:
                dx = event.x() - c.last_mouse_x
                dy = event.y() - c.last_mouse_y
                if c._scope_camera_click_pending and (abs(dx) + abs(dy) >= 3):
                    c._scope_camera_pan_started = True
                if c._scope_camera_pan_started:
                    sensitivity = c._scope_secondary_drag_deg_per_px()
                    c.azimuth_offset = c.azimuth_offset - dx * sensitivity
                    c.elevation_angle += dy * sensitivity
                    c.elevation_angle = max(-90, min(90, c.elevation_angle))
                    c._mark_scope_interaction(0.18)
                # En mode combinat, la mateixa mira segueix el cursor.
                if bool(getattr(c, "_scope_combined_drag_active", False)):
                    c.scope_controller.drag_move(
                        event.x(), event.y(), c.unproject_stereo
                    )
                c.last_mouse_x = event.x()
                c.last_mouse_y = event.y()
                c.update()
                event.accept()
                return
            if left_pressed and bool(getattr(c, "_scope_reticle_drag_armed", False)):
                try:
                    drag_delta = int((event.pos() - c.press_pos).manhattanLength())
                except Exception:
                    drag_delta = 0
                if drag_delta >= 3:
                    c.scope_controller.start_drag(event.x(), event.y())
                    c._scope_reticle_drag_armed = False
            if c.scope_controller.drag_move(
                event.x(), event.y(), c.unproject_stereo
            ):
                c._mark_scope_interaction(0.18)
                c.update()
            event.accept()
            return

        if c.measurement_tool_active():
            if c.dragging:
                dx = event.x() - c.last_mouse_x
                dy = event.y() - c.last_mouse_y
                c.azimuth_offset = c.azimuth_offset - dx * 0.5
                c.elevation_angle += dy * 0.5
                c.elevation_angle = max(-90, min(90, c.elevation_angle))
                c.last_mouse_x = event.x()
                c.last_mouse_y = event.y()
                c.update()
                event.accept()
                return
            consumed = c.measurement_controller.on_mouse_move(
                event.x(),
                event.y(),
                c.unproject_stereo,
                c.project_universal_stereo,
            )
            if not consumed:
                c.measurement_controller.update_preview_cursor(
                    event.x(), event.y(), c.unproject_stereo
                )
            c.update()
            event.accept()
            return

        if c.dragging:
            dx = event.x() - c.last_mouse_x
            dy = event.y() - c.last_mouse_y
            c.azimuth_offset = c.azimuth_offset - dx * 0.5
            c.elevation_angle += dy * 0.5
            c.elevation_angle = max(-90, min(90, c.elevation_angle))
            c.last_mouse_x = event.x()
            c.last_mouse_y = event.y()
            # Keep cached trail image during movement to avoid flickering.
            # The Fast-Path will draw on top.
            c.update()

    def handle_mouse_release(self, event):
        """Executa el metode handle_mouse_release de la classe CanvasInputHandler.

        Par?metres:
        - event (Any): Valor del parametre 'event'.

        Retorna:
        - None.
        """
        c = self._canvas
        if c.drawing_mode_enabled():
            if c.dragging:
                was_ctrl_click = bool(
                    c._drawing_ctrl_click_pending
                    and (not c._drawing_ctrl_pan_started)
                )
                c.dragging = False
                c._drawing_ctrl_pan_started = False
                c._drawing_ctrl_click_pending = False
                c._cached_trail_image = None
                if was_ctrl_click and event.button() == Qt.LeftButton:
                    ut_hour, day_of_year_utc, year_utc, _ = (
                        c._get_current_utc_context()
                    )
                    c.constellation_controller.on_left_click(
                        event.x(),
                        event.y(),
                        c.project_universal_stereo,
                        lambda ra, dec: c._ra_dec_to_alt_az(
                            ra, dec, ut_hour, day_of_year_utc, year_utc=year_utc
                        ),
                        c._pick_star_at,
                        force_add=False,
                        additive_select=True,
                    )
                    if hasattr(
                        c.parent_widget, "_sync_constellation_controls"
                    ):
                        c.parent_widget._sync_constellation_controls()
                c.update()
            event.accept()
            return

        if c.scope_mode_enabled():
            c._scope_reticle_drag_armed = False
            if event.button() == Qt.RightButton:
                c._show_object_context_menu(event)
                event.accept()
                return
            if c.dragging:
                was_scope_click = bool(
                    c._scope_camera_click_pending
                    and (not c._scope_camera_pan_started)
                )
                c.dragging = False
                c._scope_camera_pan_started = False
                c._scope_camera_click_pending = False
                c._scope_combined_drag_active = False
                c.scope_controller.end_drag()
                # Invalidate trail cache only when movement STOPS to trigger a clean bake.
                c._cached_trail_image = None
                if (
                    was_scope_click
                    and event.button() == Qt.LeftButton
                    and (event.pos() - c.press_pos).manhattanLength() < 5
                    and bool(event.modifiers() & Qt.ShiftModifier)
                ):
                    c._set_selected_target(
                        c._pick_target_at(event.x(), event.y())
                    )
                c.update()
                event.accept()
                return
            c.scope_controller.end_drag()
            c._scope_combined_drag_active = False
            if (
                event.button() == Qt.LeftButton
                and (event.pos() - c.press_pos).manhattanLength() < 5
            ):
                # Scope-click must stay responsive: avoid expensive pick/resolve work
                # on every click. Left-click is primarily for recentering reticle.
                # Explicit object selection remains available with Shift+click or
                # right-click context menu.
                if bool(event.modifiers() & Qt.ShiftModifier):
                    picked_target = c._pick_target_at(event.x(), event.y())
                    log_fn = getattr(c, "_goto_debug_log", None)
                    debug_enabled_fn = getattr(c, "_goto_debug_enabled", None)
                    debug_enabled = (
                        bool(debug_enabled_fn())
                        if callable(debug_enabled_fn)
                        else False
                    )
                    if callable(log_fn) and debug_enabled:
                        target_repr_fn = getattr(c, "_target_debug_repr", None)
                        target_repr = (
                            target_repr_fn(picked_target)
                            if callable(target_repr_fn)
                            else repr(picked_target)
                        )
                        log_fn(
                            "scope_left_release_select "
                            f"target={target_repr} "
                            f"locks_before(cam={bool(getattr(c, 'scope_camera_lock_to_target', False))},"
                            f"ret={bool(getattr(c, 'scope_reticle_lock_to_target', False))})"
                        )
                    c._set_selected_target(picked_target)
                    if callable(log_fn) and debug_enabled:
                        log_fn(
                            "scope_left_release_after_select "
                            f"locks_after(cam={bool(getattr(c, 'scope_camera_lock_to_target', False))},"
                            f"ret={bool(getattr(c, 'scope_reticle_lock_to_target', False))}) "
                            f"scope_center={getattr(c.scope_controller, 'center', None)}"
                        )
            c.update()
            event.accept()
            return

        if c.measurement_tool_active():
            if event.button() == Qt.RightButton:
                c._show_object_context_menu(event)
                event.accept()
                return
            if c.dragging:
                c.dragging = False
                # Invalidate trail cache only when movement STOPS to trigger a clean bake.
                c._cached_trail_image = None
                c.update()
                event.accept()
                return
            if event.button() == Qt.LeftButton:
                c.measurement_controller.on_mouse_release(
                    event.x(),
                    event.y(),
                    c.unproject_stereo,
                    c.project_universal_stereo,
                )
                c.update()
            event.accept()
            return

        if event.button() == Qt.RightButton:
            c._show_object_context_menu(event)
            event.accept()
            return

        c.dragging = False
        # Invalidate trail cache only when movement STOPS to trigger a clean bake.
        c._cached_trail_image = None
        c.update()

        if event.button() == Qt.LeftButton and bool(
            getattr(c, "_suppress_constellation_release_click", False)
        ):
            c._suppress_constellation_release_click = False
            event.accept()
            return

        # Click detection (min movement).
        if (event.pos() - c.press_pos).manhattanLength() < 5:
            if (
                event.button() == Qt.LeftButton
                and c.constellation_visible()
                and (not c.scope_mode_enabled())
                and (not c.measurement_tool_active())
            ):
                ut_hour, day_of_year_utc, year_utc, _ = c._get_current_utc_context()
                consumed = c.constellation_controller.on_left_click(
                    event.x(),
                    event.y(),
                    c.project_universal_stereo,
                    lambda ra, dec: c._ra_dec_to_alt_az(
                        ra, dec, ut_hour, day_of_year_utc, year_utc=year_utc
                    ),
                    c._pick_star_at,
                    force_add=False,
                    additive_select=bool(
                        event.modifiers() & Qt.ControlModifier
                    ),
                    allow_when_disabled=True,
                )
                if consumed:
                    if hasattr(
                        c.parent_widget, "_sync_constellation_controls"
                    ):
                        c.parent_widget._sync_constellation_controls()
                    c.update()
                    event.accept()
                    return
            c._set_selected_target(c._pick_target_at(event.x(), event.y()))

    def handle_mouse_double_click(self, event):
        """Executa el metode handle_mouse_double_click de la classe CanvasInputHandler.

        Par?metres:
        - event (Any): Valor del parametre 'event'.

        Retorna:
        - None.
        """
        c = self._canvas
        if c.drawing_mode_enabled():
            if event.button() == Qt.LeftButton:
                c.setFocus()
                ut_hour, day_of_year_utc, year_utc, _ = c._get_current_utc_context()
                action = c.constellation_controller.on_double_click(
                    event.x(),
                    event.y(),
                    c.project_universal_stereo,
                    lambda ra, dec: c._ra_dec_to_alt_az(
                        ra, dec, ut_hour, day_of_year_utc, year_utc=year_utc
                    ),
                    additive_select=bool(
                        event.modifiers() & Qt.ControlModifier
                    ),
                )
                if (
                    isinstance(action, dict)
                    and action.get("action") == "rename_group"
                ):
                    if hasattr(
                        c.parent_widget, "rename_constellation_group_by_index"
                    ):
                        rect = action.get("label_rect")
                        label_rect = None
                        if isinstance(rect, (tuple, list)) and len(rect) == 4:
                            try:
                                label_rect = QRectF(
                                    float(rect[0]),
                                    float(rect[1]),
                                    float(rect[2]),
                                    float(rect[3]),
                                )
                            except Exception:
                                label_rect = None
                        c.parent_widget.rename_constellation_group_by_index(
                            int(action.get("group_index", -1)),
                            label_rect=label_rect,
                        )
                if hasattr(c.parent_widget, "_sync_constellation_controls"):
                    c.parent_widget._sync_constellation_controls()
                c.update()
            event.accept()
            return

        # Constellation label/segment interactions are available outside draw mode:
        # - double click label -> rename
        # - double click segment -> select full constellation segments
        if (
            event.button() == Qt.LeftButton
            and c.constellation_visible()
            and (not c.scope_mode_enabled())
            and (not c.measurement_tool_active())
        ):
            c.setFocus()
            ut_hour, day_of_year_utc, year_utc, _ = c._get_current_utc_context()
            action = c.constellation_controller.on_double_click(
                event.x(),
                event.y(),
                c.project_universal_stereo,
                lambda ra, dec: c._ra_dec_to_alt_az(
                    ra, dec, ut_hour, day_of_year_utc, year_utc=year_utc
                ),
                additive_select=bool(event.modifiers() & Qt.ControlModifier),
                allow_when_disabled=True,
            )
            if isinstance(action, dict) and action.get("action") != "none":
                c._suppress_constellation_release_click = True
                if action.get("action") == "rename_group":
                    if hasattr(
                        c.parent_widget, "rename_constellation_group_by_index"
                    ):
                        rect = action.get("label_rect")
                        label_rect = None
                        if isinstance(rect, (tuple, list)) and len(rect) == 4:
                            try:
                                label_rect = QRectF(
                                    float(rect[0]),
                                    float(rect[1]),
                                    float(rect[2]),
                                    float(rect[3]),
                                )
                            except Exception:
                                label_rect = None
                        c.parent_widget.rename_constellation_group_by_index(
                            int(action.get("group_index", -1)),
                            label_rect=label_rect,
                        )
                if hasattr(c.parent_widget, "_sync_constellation_controls"):
                    c.parent_widget._sync_constellation_controls()
                c.update()
                event.accept()
                return

        if c.scope_mode_enabled() and event.button() == Qt.LeftButton:
            sky = screen_to_sky(event.x(), event.y(), c.unproject_stereo)
            c._scope_jump_to_sky(sky)
            event.accept()
            return

        QWidget.mouseDoubleClickEvent(c, event)

    def handle_key_press(self, event):
        """Executa el metode handle_key_press de la classe CanvasInputHandler.

        Par?metres:
        - event (Any): Valor del parametre 'event'.

        Retorna:
        - None.
        """
        c = self._canvas
        key = event.key()

        if c.drawing_mode_enabled():
            if (event.modifiers() & Qt.ControlModifier) and key == Qt.Key_Z:
                if c.constellation_controller.undo():
                    if hasattr(
                        c.parent_widget, "_sync_constellation_controls"
                    ):
                        c.parent_widget._sync_constellation_controls()
                    c.update()
                event.accept()
                return
            if key == Qt.Key_Escape:
                c.set_constellation_draw_mode(False)
                if hasattr(c.parent_widget, "_sync_constellation_controls"):
                    c.parent_widget._sync_constellation_controls()
                event.accept()
                return
            if key in (Qt.Key_Return, Qt.Key_Enter):
                c.constellation_controller.finish_active_group()
                if hasattr(c.parent_widget, "_sync_constellation_controls"):
                    c.parent_widget._sync_constellation_controls()
                c.update()
                event.accept()
                return
            if key in (Qt.Key_Delete, Qt.Key_Backspace):
                if c.constellation_controller.has_deletable_selection():
                    c.constellation_controller.delete_selected()
                    if hasattr(
                        c.parent_widget, "_sync_constellation_controls"
                    ):
                        c.parent_widget._sync_constellation_controls()
                    c.update()
                event.accept()
                return

        if c.scope_mode_enabled():
            if key == Qt.Key_Escape:
                c.set_scope_enabled(False)
                if hasattr(c.parent_widget, "sync_scope_ui_state"):
                    c.parent_widget.sync_scope_ui_state(False)
                event.accept()
                return

            if key == Qt.Key_M:
                new_mode = (
                    TelescopeScopeController.SPEED_FAST
                    if c.scope_controller.speed_mode
                    == TelescopeScopeController.SPEED_SLOW
                    else TelescopeScopeController.SPEED_SLOW
                )
                c.scope_controller.set_speed_mode(new_mode)
                if hasattr(c.parent_widget, "sync_scope_speed_ui"):
                    c.parent_widget.sync_scope_speed_ui(new_mode)
                c.update()
                event.accept()
                return

            if key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right):
                if event.isAutoRepeat():
                    event.accept()
                    return
                c._scope_last_tick_ms = int(time.time() * 1000)
                step = c.scope_controller.short_step_deg()
                d_alt = 0.0
                d_az = 0.0
                if key == Qt.Key_Up:
                    d_alt = step
                elif key == Qt.Key_Down:
                    d_alt = -step
                elif key == Qt.Key_Left:
                    d_az = step
                elif key == Qt.Key_Right:
                    d_az = -step
                c.scope_controller.nudge(d_alt, d_az)
                c.update()
                c._scope_pressed_keys.add(key)
                if not c._scope_move_timer.isActive():
                    c._scope_move_timer.start()
                event.accept()
                return

        if c.measurement_tool_active() and key == Qt.Key_Escape:
            c.measurement_controller.cancel_current()
            c.update()
            event.accept()
            return

        if (
            c.measurement_tool_active()
            and (event.modifiers() & Qt.ControlModifier)
            and key == Qt.Key_Z
        ):
            if c.measurement_controller.undo():
                c.update()
            event.accept()
            return

        if c.measurement_tool_active() and key in (
            Qt.Key_Delete,
            Qt.Key_Backspace,
        ):
            c.measurement_controller.delete_selected()
            c.update()
            event.accept()
            return

        if key == Qt.Key_F9:
            c.debug_render_metrics = not bool(c.debug_render_metrics)
            set_config_value(
                "debug_render_metrics", bool(c.debug_render_metrics)
            )
            print(
                f"[SkyDiagnostics] debug_render_metrics={c.debug_render_metrics}"
            )
            event.accept()
            return

        if (
            (event.modifiers() & Qt.ControlModifier)
            and (event.modifiers() & Qt.ShiftModifier)
            and key == Qt.Key_S
        ):
            if hasattr(c.parent_widget, "run_smoke_scenes"):
                c.parent_widget.run_smoke_scenes()
            event.accept()
            return

        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_L:
            c.log_positions()
        QWidget.keyPressEvent(c, event)

    def handle_key_release(self, event):
        """Executa el metode handle_key_release de la classe CanvasInputHandler.

        Par?metres:
        - event (Any): Valor del parametre 'event'.

        Retorna:
        - None.
        """
        c = self._canvas
        key = event.key()
        if c.scope_mode_enabled() and key in (
            Qt.Key_Up,
            Qt.Key_Down,
            Qt.Key_Left,
            Qt.Key_Right,
        ):
            if not event.isAutoRepeat():
                c._scope_pressed_keys.discard(key)
                if not c._scope_pressed_keys:
                    c._scope_move_timer.stop()
            event.accept()
            return
        QWidget.keyReleaseEvent(c, event)

    def handle_wheel(self, event):
        """Executa el metode handle_wheel de la classe CanvasInputHandler.

        Par?metres:
        - event (Any): Valor del parametre 'event'.

        Retorna:
        - None.
        """
        c = self._canvas
        degrees = event.angleDelta().y() / 8.0
        steps = degrees / 15.0

        if c.scope_mode_enabled():
            # Mark scope as manually fixed/interacted so deep catalog is unlocked.
            try:
                if hasattr(c, "scope_controller"):
                    c.scope_controller.user_center_fixed_once = True
            except Exception:
                log_suppressed_exception(__name__, "CanvasInputHandler.handle_wheel")
            
            if event.modifiers() & Qt.ControlModifier:
                c._scope_wheel_zoom(steps)
            else:
                c._camera_wheel_zoom(steps)
            event.accept()
            return

        c._camera_wheel_zoom(steps)
