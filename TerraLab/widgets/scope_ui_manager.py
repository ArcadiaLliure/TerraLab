"""UI manager extracted from AstronomicalWidget scope handlers."""

from __future__ import annotations

from PyQt5.QtCore import QTimer

from TerraLab.common.perf_events import append_perf_event
from TerraLab.common.utils import get_config_value
from TerraLab.widgets.measurement_tools import TOOL_NONE
from TerraLab.widgets.telescope_runtime import on_telescope_view_enabled


class ScopeUIManager:
    def __init__(self, widget):
        self._widget = widget

    def sync_instrument_controls(self):
        """Executa el metode sync_instrument_controls de la classe ScopeUIManager.

        Par?metres:
        - Cap.

        Retorna:
        - Any: Valor retornat pel metode.
        """
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

        # Conversion and astronomical time handling live in Compute. The UI
        # submits intent and remains responsive until the newest result arrives.
        w._set_scope_coord_inputs(ra_deg, dec_deg)
        w.request_scope_goto(ra_deg, dec_deg)

    def activate(self):
        """Executa el metode activate de la classe ScopeUIManager.

        Par?metres:
        - Cap.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        w = self._widget
        append_perf_event(
            "scope_activation_request", delta_ms_boot=w._boot_delta_ms()
        )
        append_perf_event(
            "scope_activation_ready", delta_ms_boot=w._boot_delta_ms()
        )

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
        w = self._widget
        w.canvas.set_scope_enabled(False)
        self.sync_ui_state(False)
        QTimer.singleShot(0, w._update_button_pos)

    def sync_ui_state(self, enabled: bool):
        """Executa el metode sync_ui_state de la classe ScopeUIManager.

        Par?metres:
        - enabled (bool): Valor del parametre 'enabled'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        w = self._widget
        if hasattr(w, "btn_scope_activate"):
            w.btn_scope_activate.setEnabled(not bool(enabled))
        if hasattr(w, "btn_scope_exit"):
            w.btn_scope_exit.setEnabled(bool(enabled))
        return None
