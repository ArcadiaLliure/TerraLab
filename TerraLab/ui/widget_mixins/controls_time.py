"""Time, animation, star-trail, and observing controls."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from PyQt5.QtCore import QRectF, QTimer
from PyQt5.QtWidgets import QDialog, QInputDialog

from TerraLab.common.utils import getTraduction, set_config_value
from TerraLab.ui.widget_misc_helpers import (
    widget_set_scope_coord_inputs,
    widget_sync_constellation_controls,
)
from TerraLab.ui.widget_runtime_helpers import (
    recompute_visual_magnitude_model as widget_recompute_visual_magnitude_model,
    request_relocation as widget_request_relocation,
)
from TerraLab.widgets.measurement_tools import (
    TOOL_CIRCLE,
    TOOL_NONE,
    TOOL_RECTANGLE,
    TOOL_RULER,
    TOOL_SQUARE,
)
from TerraLab.widgets.telescope_scope_mode import TelescopeScopeController


class WidgetControlsTimeMixin:
    def on_scope_speed_changed(self, index):
        mode = self.scope_speed_combo.itemData(index)
        self.canvas.set_scope_speed_mode(mode)

    def on_scope_aperture_mode_changed(self, index):
        if (
            str(getattr(self, "scope_instrument_profile", "telescope"))
            != "telescope"
        ):
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
        if not hasattr(self, "scope_aperture_spin") or not hasattr(
            self, "scope_aperture_label"
        ):
            return
        mode = str(getattr(self, "scope_aperture_input_mode", "diameter_mm"))
        spin = self.scope_aperture_spin
        if mode == "f_number":
            self.scope_aperture_label.setText(
                getTraduction("Astro.ScopeFNumber", "f/")
            )
            spin.blockSignals(True)
            spin.setRange(0.7, 64.0)
            spin.setDecimals(1)
            spin.setSingleStep(0.1)
            spin.setValue(float(getattr(self, "scope_aperture_f_number", 4.0)))
            spin.blockSignals(False)
        else:
            self.scope_aperture_label.setText(
                getTraduction("Astro.ScopeAperture", "Aperture (mm)")
            )
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
            f_number = max(
                0.7, float(getattr(self, "scope_aperture_f_number", 4.0))
            )
            return max(1.0, focal_mm / f_number)
        return max(1.0, float(getattr(self, "scope_aperture_mm", 80.0)))

    def _persist_visual_magnitude_settings(self):
        set_config_value("magnitude_limit", float(self.magnitude_limit))
        set_config_value(
            "scope_instrument_profile", str(self.scope_instrument_profile)
        )
        set_config_value("scope_aperture_mm", float(self.scope_aperture_mm))
        set_config_value(
            "scope_aperture_f_number", float(self.scope_aperture_f_number)
        )
        set_config_value(
            "scope_aperture_input_mode", str(self.scope_aperture_input_mode)
        )
        set_config_value("scope_eyepiece_mm", float(self.scope_eyepiece_mm))
        set_config_value("scope_iso", int(self.scope_iso))
        set_config_value("scope_exposure_s", float(self.scope_exposure_s))

    def _on_scope_aperture_changed(self, value):
        if (
            str(getattr(self, "scope_aperture_input_mode", "diameter_mm"))
            == "f_number"
        ):
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
        center = getattr(
            getattr(self, "canvas", None), "scope_controller", None
        )
        center = getattr(center, "center", None)
        if center is None:
            return
        self.request_scope_reverse(float(center[0]), float(center[1]))

    def on_scope_goto_radec(self) -> None:
        return self._scope_ui_manager.goto_radec()

    def _estimate_eye_pupil_mm(self, sun_alt_deg: float) -> float:
        if sun_alt_deg >= 0.0:
            return 2.2
        if sun_alt_deg <= -18.0:
            return float(self.scope_eye_pupil_dark_mm)
        t = (0.0 - sun_alt_deg) / 18.0
        return 2.2 + (float(self.scope_eye_pupil_dark_mm) - 2.2) * t

    def recompute_visual_magnitude_model(
        self, target_alt_deg=None, sun_alt_deg=-18.0, now_utc=None
    ):
        return widget_recompute_visual_magnitude_model(
            self,
            target_alt_deg=target_alt_deg,
            sun_alt_deg=sun_alt_deg,
            now_utc=now_utc,
        )

    def _sync_scope_aspect_controls(self, shape: str):
        is_rect = shape == TelescopeScopeController.SHAPE_RECT
        if hasattr(self, "scope_aspect_combo"):
            self.scope_aspect_combo.setEnabled(is_rect)
        if hasattr(self, "scope_aspect_custom_spin") and hasattr(
            self, "scope_aspect_combo"
        ):
            is_custom = (
                self.scope_aspect_combo.itemData(
                    self.scope_aspect_combo.currentIndex()
                )
                == "custom"
            )
            self.scope_aspect_custom_spin.setEnabled(is_rect and is_custom)

    def _apply_scope_aspect_from_ui(self):
        if not hasattr(self, "scope_aspect_combo"):
            return
        shape = self.scope_shape_combo.itemData(
            self.scope_shape_combo.currentIndex()
        )
        self._sync_scope_aspect_controls(shape)
        if shape != TelescopeScopeController.SHAPE_RECT:
            self.canvas.set_scope_aspect_ratio(None)
            return
        data = self.scope_aspect_combo.itemData(
            self.scope_aspect_combo.currentIndex()
        )
        if data is None:
            self.canvas.set_scope_aspect_ratio(None)
            return
        if data == "custom":
            self.canvas.set_scope_aspect_ratio(
                self.scope_aspect_custom_spin.value()
            )
            return
        self.canvas.set_scope_aspect_ratio(float(data))

    def sync_scope_speed_ui(self, mode: str):
        if not hasattr(self, "scope_speed_combo"):
            return
        for i in range(self.scope_speed_combo.count()):
            if self.scope_speed_combo.itemData(i) == mode:
                self.scope_speed_combo.blockSignals(True)
                self.scope_speed_combo.setCurrentIndex(i)
                self.scope_speed_combo.blockSignals(False)
                break

    def sync_scope_focal_ui(self, focal_mm: float):
        if not hasattr(self, "scope_focal_spin"):
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
            (getattr(self, "btn_tool_ruler", None), TOOL_RULER),
            (getattr(self, "btn_tool_square", None), TOOL_SQUARE),
            (getattr(self, "btn_tool_rect", None), TOOL_RECTANGLE),
            (getattr(self, "btn_tool_circle", None), TOOL_CIRCLE),
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
        selected_groups = len(
            getattr(ctrl, "selected_group_indices", set()) or set()
        )
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

    def constellation_primary_action(self):
        ctrl = self.canvas.constellation_controller
        if bool(getattr(ctrl, "group_drawing_active", False)):
            self.finish_constellation_group()
            return
        self.create_constellation_group()

    def create_constellation_group(self):
        default_name = self.canvas.constellation_controller.next_default_name()
        title = getTraduction(
            "Astro.ConstellationDialogTitle", "Constellation"
        )
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

    def rename_constellation_group_by_index(
        self, group_index: int, label_rect: Optional[QRectF] = None
    ):
        ctrl = self.canvas.constellation_controller
        gi = int(group_index)
        if not (0 <= gi < len(ctrl.groups)):
            return
        ctrl.active_group_index = gi
        ctrl.selected_group_index = gi
        ctrl.selected_node_index = None
        ctrl.selected_segment_index = None
        self.canvas.begin_inline_constellation_rename(
            group_index=gi, label_rect=label_rect
        )
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
        self._sync_measure_tool_buttons(
            self.canvas.measurement_controller.active_tool
        )

    def on_time_bar_drag_state_changed(self, active):
        self._dragging_time = bool(active)
        if hasattr(self, "canvas"):
            self.canvas.update()
            if not self._dragging_time:
                # Repaint once more after the lightweight interaction frame so
                # the settled view restores its normal-detail representation.
                QTimer.singleShot(0, self.canvas.update)

    def on_time_bar_change(self, val):
        self.use_real_time = False
        self.btn_realtime.setChecked(False)
        self.manual_hour = val
        self._last_seek_hour = val
        if hasattr(self, "canvas") and hasattr(self.canvas, "_mark_camera_interaction"):
            self.canvas._mark_camera_interaction(0.35)
        coord = getattr(self, "ephemeris_coordinator", None)
        if coord is not None:
            canvas = getattr(self, "canvas", None)
            try:
                ut_h, day_u, yr_u, _ = canvas._get_current_utc_context()
            except Exception:
                ut_h = float(val)
                day_u = int(getattr(self, "manual_day", 0))
                yr_u = int(getattr(self, "manual_year", 2026))
            coord.request_snapshot(
                year_utc=int(yr_u),
                day_of_year_utc=int(day_u),
                ut_hour=float(ut_h),
            )
        self.canvas.update()
        # Time bar stays LOCAL; hint shows local and UTC from observer tz conversion.
        if hasattr(self.canvas, "hint_overlay"):
            try:
                ut_h, _, _, _ = self.canvas._get_current_utc_context()
            except Exception:
                ut_h = float(val)
            lh = f"{int(val % 24):02d}:{int((val % 1) * 60):02d}"
            uth = f"{int(ut_h % 24):02d}:{int(((ut_h % 24) % 1) * 60):02d}"
            txt = getTraduction(
                "HUD.TimeHint", "🕒 {local_h} local  ·  UT {ut_h}"
            ).format(local_h=lh, ut_h=uth)
            self.canvas.hint_overlay.show_hint(txt)

    def request_relocation(self):
        return widget_request_relocation(self)

    def update_location(self):
        """Called by ReturnPressed on line edits."""
        self.request_relocation()

    def prev_day(self):
        self.manual_day = (self.manual_day - 1) % 365
        self.lbl_date.setText(self.format_date(self.manual_day))
        self.time_bar.update_params(
            self.latitude, self.longitude, self.manual_day
        )
        coord = getattr(self, "ephemeris_coordinator", None)
        if coord is not None:
            coord.request_snapshot(
                year_utc=int(getattr(self, "manual_year", 2026)),
                day_of_year_utc=int(self.manual_day),
                ut_hour=float(getattr(self, "manual_hour", 12.0)),
            )
        self.canvas.update()

    def next_day(self):
        self.manual_day = (self.manual_day + 1) % 365
        self.lbl_date.setText(self.format_date(self.manual_day))
        self.time_bar.update_params(
            self.latitude, self.longitude, self.manual_day
        )
        coord = getattr(self, "ephemeris_coordinator", None)
        if coord is not None:
            coord.request_snapshot(
                year_utc=int(getattr(self, "manual_year", 2026)),
                day_of_year_utc=int(self.manual_day),
                ut_hour=float(getattr(self, "manual_hour", 12.0)),
            )
        self.canvas.update()

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
        self.canvas.elevation_angle = 0.1  # Low angle for landscape
        self.canvas.vertical_offset_ratio = 0.35  # Horizon lower-third rule
        self.canvas.zoom_level = 1  # Wider FOV (~220 degrees)
        if self.latitude >= 0:
            self.canvas.azimuth_offset = 180  # Facing South (North Hemi)
        else:
            self.canvas.azimuth_offset = 0  # Facing North (South Hemi)
        if hasattr(self, "btn_view"):
            self.btn_view.setText(getTraduction("Astro.ViewZenith", "Cénit"))
        self.canvas.update()

    def set_zenith_view(self):
        self.canvas.elevation_angle = 90  # Dome view up
        self.canvas.vertical_offset_ratio = 0.0  # Center
        self.canvas.zoom_level = 1.0  # Wide Fisheye
        self.canvas.azimuth_offset = 0
        if hasattr(self, "btn_view"):
            self.btn_view.setText(
                getTraduction("Astro.ViewHorizontal", "Horizonte")
            )
        self.canvas.update()

    def on_extra_height_changed(self, val):
        self.terrain_coordinator.set_observer_offset(val)
        self.update_altitude_label()
        # Debounce the expensive topography recalculation.
        self.bake_debounce_timer.start(1500)

    def update_altitude_label(self):
        bare = self.terrain_coordinator.get_bare_elevation(
            self.latitude, self.longitude
        )
        offset = (
            self.spin_extra_height.value()
            if hasattr(self, "spin_extra_height")
            else 0.0
        )
        if bare is not None:
            total = bare + offset
            tpl = getTraduction(
                "Astro.AltitudeInfo",
                "Altitud terreno: {dem} m | Total observador: {total} m",
            )
            if hasattr(self, "lbl_altitude_info"):
                self.lbl_altitude_info.setText(
                    tpl.format(dem=f"{bare:.1f}", total=f"{total:.1f}")
                )
        else:
            fallback_str = getTraduction(
                "Astro.AltitudeInfo",
                "Altitud terreno: {dem} m | Total observador: {total} m",
            )
            fallback_str = fallback_str.replace("{dem}", "--").replace(
                "{total}", "--"
            )
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
        print(
            f"[DEBUG] Spike threshold updated to: {self.spike_magnitude_threshold} (slider val: {val})"
        )
        self.canvas.update()

    def on_layers_changed(self, text):
        """Update layer count from UI combo box and trigger a re-bake."""
        try:
            val = int(text)
            from TerraLab.common.utils import set_config_value

            set_config_value("horizon_quality", val)
            self.terrain_coordinator.reload_config()
            self.request_relocation()
        except ValueError:
            pass

    def configure_terrain(self):
        """Open DEM configuration dialog."""
        from TerraLab.widgets.terrain_config_dialog import TerrainConfigDialog

        dlg = TerrainConfigDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            # Re-read config in worker
            self.terrain_coordinator.reload_config()
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
                if abs(step) < 0.5:
                    step = 0.5 if step > 0 else -0.5
                self.canvas.azimuth_offset = (current + step) % 360
        # 2. Elevation Animation
        if getattr(self, "target_elevation", None) is not None:
            current_el = self.canvas.elevation_angle
            diff_el = self.target_elevation - current_el
            if abs(diff_el) < 0.5:
                self.canvas.elevation_angle = self.target_elevation
                self.target_elevation = None
            else:
                running = True
                step_el = diff_el * 0.1
                if abs(step_el) < 0.5:
                    step_el = 0.5 if step_el > 0 else -0.5
                self.canvas.elevation_angle = current_el + step_el
        if not running:
            self.canvas.dragging = False
        else:
            self.canvas.dragging = True
        self.canvas.update()

    def on_trails_toggled(self, checked):
        self._update_circumpolar_button_text()
        if checked:
            try:
                start_hour, _, _, _ = (
                    self.canvas._get_current_utc_context()
                )
            except (AttributeError, TypeError, ValueError):
                start_hour = float(getattr(self, "manual_hour", 12.0))
            self.canvas.trail_start_hour = float(start_hour)
            self.target_azimuth = None
            self.target_elevation = None
            request_alignment = getattr(
                self, "request_circumpolar_alignment", None
            )
            if callable(request_alignment):
                request_alignment()
        else:
            self.canvas.trail_start_hour = None
            self.target_azimuth = 180  # Return to South
            self.target_elevation = 40  # Default nice view
        self.canvas.update()

    def _update_circumpolar_button_text(self):
        if not hasattr(self, "chk_trails"):
            return
        checked = bool(self.chk_trails.isChecked())
        if checked:
            self.chk_trails.setText(
                getTraduction("Astro.StopCircumpolar", "Aturar circumpolar")
            )
        else:
            self.chk_trails.setText(
                getTraduction("Astro.StartCircumpolar", "Iniciar circumpolar")
            )

    def get_month_name(self, month_idx):
        # Manual translation since locale might be erratic
        months = [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
        keys = [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
        m_en = months[month_idx - 1]
        return getTraduction(f"Month.{keys[month_idx - 1]}", m_en)

    def format_date(self, day_index):
        # Conversion using manual_year
        date = datetime(self.manual_year, 1, 1) + timedelta(
            days=int(day_index)
        )
        # Custom localized format
        month_name = self.get_month_name(date.month)
        return f"{date.day} {month_name} {date.year}"
