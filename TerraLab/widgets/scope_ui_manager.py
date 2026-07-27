"""UI manager extracted from AstronomicalWidget scope handlers."""

from __future__ import annotations

from PyQt5.QtCore import QTimer

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.perf_events import append_perf_event
from TerraLab.common.utils import get_config_value
from TerraLab.widgets.measurement_tools import TOOL_NONE
from TerraLab.widgets.telescope_runtime import on_telescope_view_enabled

_MISSING = object()


class ScopeUIManager:
    def __init__(self, widget):
        self._widget = widget

    def _legacy(self, name: str, *args):
        fn = getattr(self._widget, name, None)
        if callable(fn):
            return fn(*args)
        return _MISSING

    def sync_instrument_controls(self):
        """Executa el metode sync_instrument_controls de la classe ScopeUIManager.

        Par?metres:
        - Cap.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        legacy = self._legacy("_sync_scope_instrument_controls_impl")
        if legacy is not _MISSING:
            return legacy

        w = self._widget
        profile = str(getattr(w, "scope_instrument_profile", "telescope"))
        is_telescope = profile == "telescope"

        if hasattr(w, "scope_eyepiece_label"):
            w.scope_eyepiece_label.setVisible(is_telescope)
        if hasattr(w, "scope_eyepiece_spin"):
            w.scope_eyepiece_spin.setVisible(is_telescope)

        if is_telescope:
            if hasattr(w, "scope_aperture_mode_label"):
                w.scope_aperture_mode_label.setVisible(True)
            if hasattr(w, "scope_aperture_mode_combo"):
                w.scope_aperture_mode_combo.setVisible(True)
                w.scope_aperture_mode_combo.setEnabled(True)
        else:
            w.scope_aperture_input_mode = "f_number"
            if hasattr(w, "scope_aperture_mode_combo"):
                w.scope_aperture_mode_combo.blockSignals(True)
                for i in range(w.scope_aperture_mode_combo.count()):
                    if w.scope_aperture_mode_combo.itemData(i) == "f_number":
                        w.scope_aperture_mode_combo.setCurrentIndex(i)
                        break
                w.scope_aperture_mode_combo.blockSignals(False)
                w.scope_aperture_mode_combo.setEnabled(False)
                w.scope_aperture_mode_combo.setVisible(False)
            if hasattr(w, "scope_aperture_mode_label"):
                w.scope_aperture_mode_label.setVisible(False)

        if profile == "camera_aps_c":
            w._set_scope_sensor_key("aps_c")
            w.scope_sensor_combo.setEnabled(False)
        elif profile == "camera_full_frame":
            w._set_scope_sensor_key("full_frame")
            w.scope_sensor_combo.setEnabled(False)
        else:
            w.scope_sensor_combo.setEnabled(True)

        w._sync_scope_aperture_controls()
        return None

    def goto_radec(self) -> None:
        """Executa el metode goto_radec de la classe ScopeUIManager.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        legacy = self._legacy("_scope_ui_goto_radec_impl")
        if legacy is not _MISSING:
            return legacy

        w = self._widget
        if not hasattr(w, "scope_ra_h_spin"):
            return
        ra_h = int(w.scope_ra_h_spin.value())
        ra_m = int(w.scope_ra_m_spin.value())
        ra_s = float(w.scope_ra_s_spin.value())
        dec_sign = 1.0 if w.scope_dec_sign_combo.currentText() != "-" else -1.0
        dec_d = int(w.scope_dec_d_spin.value())
        dec_m = int(w.scope_dec_m_spin.value())
        dec_s = float(w.scope_dec_s_spin.value())

        ra_deg = (ra_h + (ra_m / 60.0) + (ra_s / 3600.0)) * 15.0
        dec_deg = dec_sign * (dec_d + (dec_m / 60.0) + (dec_s / 3600.0))

        try:
            # Manual GoTo should not remain tied to previous selected-target tracking.
            if hasattr(w.canvas, "_set_selected_target"):
                w.canvas._set_selected_target(None)
            if hasattr(w.canvas, "scope_camera_lock_to_target"):
                w.canvas.scope_camera_lock_to_target = False
            if hasattr(w.canvas, "scope_reticle_lock_to_target"):
                w.canvas.scope_reticle_lock_to_target = False
            if hasattr(w.canvas, "scope_controller"):
                w.canvas.scope_controller.end_drag()
            if hasattr(w.canvas, "dragging"):
                w.canvas.dragging = False

            ut_hour, day_of_year_utc = w.canvas._current_ut_context()
            sky = w.canvas._ra_dec_to_alt_az(
                ra_deg, dec_deg, ut_hour, day_of_year_utc
            )
            if sky is None:
                print("[ScopeGoto] abort: could not convert RA/Dec to Alt/Az")
                return
            before_center = getattr(w.canvas.scope_controller, "center", None)
            print(
                "[ScopeGoto] start "
                f"ra={ra_deg:.6f} dec={dec_deg:.6f} "
                f"sky=({float(sky[0]):.6f},{float(sky[1]):.6f}) "
                f"before_center={before_center}"
            )
            if not w.canvas.scope_mode_enabled():
                self.activate()
            moved_ok = bool(w.canvas._scope_jump_to_sky(sky))
            after_center = getattr(w.canvas.scope_controller, "center", None)
            if (not moved_ok) or (after_center is None):
                # Hard fallback: enforce center/camera directly so GoTo never
                # remains in an indeterminate state.
                alt = float(sky[0])
                az = float(sky[1]) % 360.0
                w.canvas.scope_controller.set_center((alt, az), confirmed=True)
                w.canvas.azimuth_offset = az
                w.canvas.elevation_angle = max(-90.0, min(90.0, alt))
                w.canvas.update()
                moved_ok = True
                after_center = getattr(w.canvas.scope_controller, "center", None)
                print("[ScopeGoto] fallback applied")
            w._set_scope_coord_inputs(ra_deg, dec_deg)
            print(f"[ScopeGoto] end moved={bool(moved_ok)} after_center={after_center}")
        except Exception as exc:
            print(f"[ScopeGoto] goto_radec failed: {exc}")
            return

    def activate(self):
        """Executa el metode activate de la classe ScopeUIManager.

        Par?metres:
        - Cap.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        legacy = self._legacy("_scope_ui_activate_impl")
        if legacy is not _MISSING:
            return legacy

        w = self._widget
        append_perf_event(
            "scope_activation_request", delta_ms_boot=w._boot_delta_ms()
        )
        w._start_scope_full_preload_async(reason="scope_activate")
        subset_only = bool(getattr(w, "_catalog_loaded_subset_only", False))
        waiting_preload = bool(
            w._scope_preload_should_wait()
            and (not bool(getattr(w, "_scope_preload_ready", False)))
        )
        if subset_only or waiting_preload:
            if not bool(
                getattr(w, "_scope_preload_pending_activation", False)
            ):
                append_perf_event(
                    "scope_activation_wait_start",
                    delta_ms_boot=w._boot_delta_ms(),
                )
            w._scope_preload_pending_activation = True
            if not bool(getattr(w, "_scope_preload_wait_logged", False)):
                if subset_only and waiting_preload:
                    print(
                        "[AstroWidget] Scope activation waiting for full scope catalog and preload."
                    )
                elif subset_only:
                    print(
                        "[AstroWidget] Scope activation waiting for full scope catalog."
                    )
                else:
                    print(
                        "[AstroWidget] Scope activation waiting for full catalog preload."
                    )
                w._scope_preload_wait_logged = True
            if subset_only and waiting_preload:
                w._scope_preload_status(
                    "waiting full scope catalog + preload..."
                )
                w._scope_set_data_state(
                    "loading_deep", reason="subset+preload_wait"
                )
            elif subset_only:
                w._scope_preload_status("waiting full scope catalog...")
                w._scope_set_data_state(
                    "loading_deep", reason="subset_catalog"
                )
            else:
                w._scope_preload_status("waiting full catalog preload...")
                w._scope_set_data_state("loading_deep", reason="preload_wait")
        else:
            w._scope_preload_pending_activation = False
            w._scope_preload_wait_logged = False
            if bool(getattr(w, "_scope_preload_ready", False)):
                w._apply_scope_preloaded_spatial_index()
            append_perf_event(
                "scope_activation_ready", delta_ms_boot=w._boot_delta_ms()
            )
            w._refresh_scope_data_state(reason="scope_activate_ready")

        w.canvas.setUpdatesEnabled(False)
        try:
            w.canvas.set_scope_focal_mm(w.scope_focal_spin.value())
            w.canvas.set_scope_shape(
                w.scope_shape_combo.itemData(
                    w.scope_shape_combo.currentIndex()
                )
            )
            w.canvas.set_scope_sensor(
                w.scope_sensor_combo.itemData(
                    w.scope_sensor_combo.currentIndex()
                )
            )
            w._apply_scope_aspect_from_ui()
            w.canvas.set_scope_speed_mode(
                w.scope_speed_combo.itemData(
                    w.scope_speed_combo.currentIndex()
                )
            )
            w.canvas.set_scope_enabled(True)
            QTimer.singleShot(0, w._ensure_scope_spatial_index_warmup)
            QTimer.singleShot(
                120, lambda: w._ensure_scope_catalog_loaded(force_now=False)
            )
            append_perf_event(
                "scope_mode_enabled", delta_ms_boot=w._boot_delta_ms()
            )
            instrument_profile = str(
                getattr(w, "scope_instrument_profile", "telescope")
            )
            eyepiece_mm = float(
                w.scope_eyepiece_mm
                if instrument_profile == "telescope"
                else w.scope_focal_spin.value()
            )
            aperture_mm_effective = w._effective_scope_aperture_mm(
                w.scope_focal_spin.value()
            )
            on_telescope_view_enabled(
                {
                    "scope_enabled": True,
                    "lat": float(w.latitude),
                    "lon": float(w.longitude),
                    "h_deg": float(w.canvas.elevation_angle),
                    "focal_mm": float(w.scope_focal_spin.value()),
                    "aperture_mm": float(aperture_mm_effective),
                    "ocular_mm": eyepiece_mm,
                    "instrument_profile": instrument_profile,
                    "k_fallback": float(w.scope_k_fallback),
                    "weather_enabled": bool(
                        getattr(w.canvas.weather, "enabled", False)
                    ),
                    "copernicus_api_key": str(
                        get_config_value("copernicus_api_key", "") or ""
                    ),
                    "copernicus_api_url": str(
                        get_config_value(
                            "copernicus_api_url",
                            "https://cds.climate.copernicus.eu/api",
                        )
                        or ""
                    ),
                },
                allow_remote_fetch=False,
            )
            w._sync_scope_coord_inputs_from_canvas()
            w.canvas.setFocus()
            self.sync_ui_state(True)

            w.canvas.set_measurement_tool(TOOL_NONE)
            w._sync_measure_tool_buttons(TOOL_NONE)
            w.canvas.set_constellation_draw_mode(False)
            w._sync_constellation_controls()
        finally:
            w.canvas.setUpdatesEnabled(True)
            w.canvas.update()
        QTimer.singleShot(0, w._update_button_pos)

    def exit(self):
        """Executa el metode exit de la classe ScopeUIManager.

        Par?metres:
        - Cap.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        legacy = self._legacy("_scope_ui_exit_impl")
        if legacy is not _MISSING:
            return legacy

        w = self._widget
        w.canvas.set_scope_enabled(False)
        if bool(getattr(w, "_scope_full_catalog_attached", False)):
            base_ra = getattr(w, "_scope_base_ra", None)
            base_dec = getattr(w, "_scope_base_dec", None)
            base_mag = getattr(w, "_scope_base_mag", None)
            if (
                base_ra is not None
                and base_dec is not None
                and base_mag is not None
            ):
                try:
                    if int(len(base_ra)) > 0 and int(len(base_ra)) == int(
                        len(base_dec)
                    ) == int(len(base_mag)):
                        w.np_ra = base_ra
                        w.np_dec = base_dec
                        w.np_mag = base_mag
                        w.np_bp_rp = getattr(w, "_scope_base_bp_rp", None)
                        w.np_r = getattr(w, "_scope_base_r", None)
                        w.np_g = getattr(w, "_scope_base_g", None)
                        w.np_b = getattr(w, "_scope_base_b", None)
                        w._catalog_loaded_subset_only = True
                        w._catalog_mag_sorted = True
                        w._scope_full_catalog_attached = False
                        w.canvas._cached_star_image = None
                        w.canvas._cached_trail_image = None
                        w._refresh_scope_data_state(
                            reason="scope_exit_restore_subset"
                        )
                except Exception:
                    log_suppressed_exception(__name__, "ScopeUIManager.exit")
        self.sync_ui_state(False)
        QTimer.singleShot(0, w._update_button_pos)

    def sync_ui_state(self, enabled: bool):
        """Executa el metode sync_ui_state de la classe ScopeUIManager.

        Par?metres:
        - enabled (bool): Valor del parametre 'enabled'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        legacy = self._legacy("_scope_ui_sync_state_impl", bool(enabled))
        if legacy is not _MISSING:
            return legacy

        w = self._widget
        if hasattr(w, "btn_scope_activate"):
            w.btn_scope_activate.setEnabled(not bool(enabled))
        if hasattr(w, "btn_scope_exit"):
            w.btn_scope_exit.setEnabled(bool(enabled))
        return None
