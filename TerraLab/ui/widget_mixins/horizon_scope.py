"""Horizon results and telescope/scope controls."""

from __future__ import annotations

import time

from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import QLabel

from TerraLab.common.utils import getTraduction
from TerraLab.ui.widget_controls_builder import build_deferred_controls_ui
from TerraLab.ui.widget_misc_helpers import (
    widget_update_custom_theme,
)


class WidgetHorizonScopeMixin:
    def cancel_pending_horizon_preview(self) -> None:
        self._horizon_preview_schedule_id = (
            int(getattr(self, "_horizon_preview_schedule_id", 0)) + 1
        )
        self._horizon_preview_flush_scheduled = False
        self._horizon_preview_pending_payload = None

    def queue_horizon_preview(self, payload) -> None:
        """Coalesce terrain previews before applying them to Qt widgets."""

        if not isinstance(payload, dict):
            return
        job_id = str(payload.get("job_id", "") or "")
        if job_id and job_id != getattr(self, "_active_horizon_job_id", None):
            return
        self._horizon_preview_pending_payload = dict(payload)
        if bool(getattr(self, "_horizon_preview_flush_scheduled", False)):
            return
        now_mono = time.perf_counter()
        last_apply = float(
            getattr(self, "_horizon_preview_last_apply_ts", 0.0)
        )
        min_interval = max(
            0.10,
            float(getattr(self, "_horizon_preview_min_interval_s", 0.50)),
        )
        delay = max(0.0, min_interval - max(0.0, now_mono - last_apply))
        self._horizon_preview_flush_scheduled = True
        self._horizon_preview_schedule_id = (
            int(getattr(self, "_horizon_preview_schedule_id", 0)) + 1
        )
        schedule_id = self._horizon_preview_schedule_id
        QTimer.singleShot(
            int(round(delay * 1000.0)),
            lambda: self._flush_horizon_preview(schedule_id),
        )

    def _flush_horizon_preview(self, schedule_id: int) -> None:
        if int(schedule_id) != int(
            getattr(self, "_horizon_preview_schedule_id", 0)
        ):
            return
        self._horizon_preview_flush_scheduled = False
        payload = getattr(self, "_horizon_preview_pending_payload", None)
        self._horizon_preview_pending_payload = None
        if not isinstance(payload, dict):
            return
        self.on_horizon_preview_ready(payload)
        self._horizon_preview_last_apply_ts = time.perf_counter()
        queued = getattr(self, "_horizon_preview_pending_payload", None)
        if queued is not None:
            self.queue_horizon_preview(queued)

    def on_horizon_preview_ready(self, payload):
        """Publish only the immutable preview descriptor from Compute."""

        if not isinstance(payload, dict):
            return
        job_id = str(payload.get("job_id", "") or "")
        if job_id and job_id != getattr(self, "_active_horizon_job_id", None):
            return
        profile_path = str(payload.get("profile_path", "") or "")
        if profile_path:
            self._remote_terrain_profile_path = profile_path
            if getattr(self, "scene_load_stage", "base_sky") != "scene_ready":
                self._set_scene_load_stage("horizon_preview")
            self.canvas.update()

    def on_horizon_profile_ready(self, payload):
        """Publish a completed immutable profile without opening it in UI."""

        self._start_catalog_loader_async(reason="horizon_ready")
        if isinstance(payload, dict):
            job_id = str(payload.get("job_id", "") or "")
            if job_id and job_id != getattr(self, "_active_horizon_job_id", None):
                return
            profile_path = str(payload.get("profile_path", "") or "")
            if profile_path:
                self._remote_terrain_profile_path = profile_path
        if hasattr(self, 'lbl_loading'):
            self.lbl_loading.hide()
        self._set_scene_load_stage("scene_ready")
        self.canvas.update()

    def _update_terrain_depth_label(self, value_km):
        if hasattr(self, "lbl_terrain_depth"):
            self.lbl_terrain_depth.setText(
                getTraduction("Terrain.Depth", "Profunditat: {km} km").format(km=int(value_km))
            )

    def on_terrain_depth_changed(self, value_km):
        self._pending_terrain_depth_km = int(value_km)
        self._update_terrain_depth_label(value_km)
        timer = getattr(self, "terrain_depth_debounce_timer", None)
        if timer is not None:
            timer.start(2000)

    def _update_terrain_ray_precision_label(self, slider_value):
        if hasattr(self, "lbl_terrain_ray_precision"):
            from TerraLab.data.ray_precision import slider_to_ray_step

            step = slider_to_ray_step(slider_value)
            value = f"{step:.3f}".rstrip("0").rstrip(".").replace(".", ",")
            self.lbl_terrain_ray_precision.setText(
                getTraduction(
                    "Terrain.RayPrecision", "Rayos: {degrees}\N{DEGREE SIGN}"
                ).format(
                    degrees=value
                )
            )

    def on_terrain_ray_precision_changed(self, slider_value):
        from TerraLab.data.ray_precision import slider_to_ray_step

        self._pending_terrain_ray_step_deg = slider_to_ray_step(slider_value)
        self._update_terrain_ray_precision_label(slider_value)
        timer = getattr(self, "terrain_ray_precision_debounce_timer", None)
        if timer is not None:
            timer.start(2000)

    def _apply_pending_terrain_ray_precision(self):
        from TerraLab.common.utils import set_config_value
        from TerraLab.data.ray_precision import normalize_ray_step_deg

        step = normalize_ray_step_deg(self._pending_terrain_ray_step_deg)
        set_config_value("horizon_ray_step_deg", step)
        self._pending_terrain_ray_step_deg = step
        self._begin_horizon_bake()

    def _apply_pending_terrain_depth(self):
        from TerraLab.common.utils import get_config_value, set_config_value
        from TerraLab.data.visibility_range import TerrainRangeSettings

        radius_km = int(
            self._pending_terrain_depth_km
            or (self.slider_terrain_depth.value() if hasattr(self, "slider_terrain_depth") else 1)
        )
        set_config_value("terrain_display_radius_km", radius_km)
        current = TerrainRangeSettings.from_mapping(
            get_config_value("terrain_visibility_range", {})
        )
        widened = TerrainRangeSettings(
            mode="manual",
            manual_radius_km=float(radius_km),
            minimum_radius_km=current.minimum_radius_km,
            maximum_radius_km=current.maximum_radius_km,
            target_max_elevation_m=current.target_max_elevation_m,
            atmospheric_refraction_enabled=current.atmospheric_refraction_enabled,
            effective_earth_radius_factor=current.effective_earth_radius_factor,
            immediate_preload_radius_km=current.immediate_preload_radius_km,
        ).validated()
        set_config_value("terrain_visibility_range", widened.to_dict())
        self.terrain_coordinator.reload_config()
        self._begin_horizon_bake()

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
            self._position_loading_label()
            if self.lbl_loading.isHidden():
                self.lbl_loading.show()
                self.lbl_loading.raise_()
            # Queue one paint for the next event-loop turn.  A synchronous
            # repaint for every raster-row progress event can starve all input.
            self.lbl_loading.update()

    def _poll_horizon_progress(self):
        try:
            msg = self.terrain_coordinator.get_progress_text()
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
        toolbar = getattr(self, "quick_toolbar", None)
        toolbar_height = toolbar.height() if toolbar is not None else 0
        lbl.move(
            max(10, self.width() - width - 10),
            toolbar_height + 120,
        )

    def _position_loading_label(self):
        lbl = getattr(self, "lbl_loading", None)
        if lbl is None:
            return
        toolbar = getattr(self, "quick_toolbar", None)
        toolbar_height = toolbar.height() if toolbar is not None else 0
        x_pos = min(370, max(10, self.width() - lbl.width() - 10))
        lbl.move(x_pos, toolbar_height + 84)

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
            self.timer.setInterval(250)

    def _do_delayed_bake(self):
        """Actually sends the bake request after debouncing."""
        self.terrain_coordinator.abort_current_job()
        # SAVE CONFIG ONLY HERE (Avoid disk spam)
        from TerraLab.common.utils import set_config_value
        offset_val = self.spin_extra_height.value()
        set_config_value("observer_offset", offset_val)
        set_config_value("observer_lat", self.latitude)
        set_config_value("observer_lon", self.longitude)
        print(f"[AstroWidget] Emitting debounced bake request for {self.latitude}, {self.longitude}")
        self._begin_horizon_bake()

    def load_satellites_from_tle(self):
        client = getattr(self, "satellite_client", None)
        if client is not None:
            client.request_snapshot()

    def toggle_satellites(self, checked):
        self.show_satellites = checked
        if checked and not self.satellites:
            self.load_satellites_from_tle()
        self.canvas.update()

    def setup_content(self):
        if hasattr(self, 'title_bar'):
            self.title_bar.hide()
        layout = self.content_layout
        layout.setContentsMargins(0, 0, 0, 0)
        from TerraLab.ui.astro_canvas import AstroCanvas

        self.canvas = AstroCanvas(self)
        layout.addWidget(self.canvas, 1)
        # Loading indicator stays available from the first visible frame.
        self.lbl_loading = QLabel(getTraduction("Astro.LoadingTopography", "? Carregant topografia..."), self)
        self.lbl_loading.setStyleSheet(
            "color: #f1cd88; font-weight: 600; "
            "background-color: rgba(2,4,10,220); "
            "border: 1px solid #3b4559; padding: 5px; border-radius: 5px;"
        )
        self.lbl_loading.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_loading.setWordWrap(False)
        self.lbl_loading.hide()
        self.lbl_loading.resize(420, 32)
        self._position_loading_label()
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
        if hasattr(self, "sky_mode_stack") and hasattr(
            self, "sky_base_page"
        ):
            if checked:
                if hasattr(self, "btn_tools_panel"):
                    self.btn_tools_panel.blockSignals(True)
                    self.btn_tools_panel.setChecked(False)
                    self.btn_tools_panel.blockSignals(False)
                self.set_control_drawer("sky")
                self._sync_scope_coord_inputs_from_canvas()
                self.scope_panel.show()
                self.sky_mode_stack.setCurrentWidget(self.scope_panel)
            else:
                self.sky_base_page.show()
                self.sky_mode_stack.setCurrentWidget(self.sky_base_page)
            QTimer.singleShot(0, self._refresh_drawer_geometry)
            return
        if checked:
            if hasattr(self, "btn_tools_panel"):
                self.btn_tools_panel.blockSignals(True)
                self.btn_tools_panel.setChecked(False)
                self.btn_tools_panel.blockSignals(False)
            if hasattr(self, "control_tabs") and hasattr(self, "sky_tab"):
                self.control_tabs.setCurrentWidget(self.sky_tab)
            elif hasattr(self, "tools_panel"):
                self.tools_panel.hide()
            self._sync_scope_coord_inputs_from_canvas()
        if hasattr(self, "scope_panel"):
            self.scope_panel.setVisible(checked)
        QTimer.singleShot(0, self._update_button_pos)

    def toggle_tools_panel(self, checked):
        if hasattr(self, "drawer_stack"):
            if checked:
                if hasattr(self, "btn_scope_panel"):
                    self.btn_scope_panel.setChecked(False)
                self.set_control_drawer("tools")
            elif (
                getattr(self, "_current_drawer_key", None) == "tools"
                and self.control_drawer.isVisible()
            ):
                self.set_control_drawer("sky")
            QTimer.singleShot(0, self._refresh_drawer_geometry)
            return
        if hasattr(self, "control_tabs") and hasattr(self, "tools_tab"):
            if checked:
                if hasattr(self, "btn_scope_panel"):
                    self.btn_scope_panel.blockSignals(True)
                    self.btn_scope_panel.setChecked(False)
                    self.btn_scope_panel.blockSignals(False)
                if hasattr(self, "scope_panel"):
                    self.scope_panel.hide()
                if hasattr(self, "tools_panel"):
                    self.tools_panel.show()
                self.control_tabs.setCurrentWidget(self.tools_tab)
            elif self.control_tabs.currentWidget() is self.tools_tab:
                self.control_tabs.setCurrentWidget(self.sky_tab)
            QTimer.singleShot(0, self._update_button_pos)
            return
        if checked:
            if hasattr(self, "btn_scope_panel"):
                self.btn_scope_panel.blockSignals(True)
                self.btn_scope_panel.setChecked(False)
                self.btn_scope_panel.blockSignals(False)
            if hasattr(self, "scope_panel"):
                self.scope_panel.hide()
        if hasattr(self, "tools_panel"):
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

