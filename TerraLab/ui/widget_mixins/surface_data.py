"""Surface refresh, data-layer dialog, and runtime status handling."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QMessageBox

from TerraLab.astro.search_engine import (
    AstroSearchEngine,
    build_search_index_for_widget,
    center_on_object_for_widget,
    load_named_star_entries,
    load_ngc_entries,
    on_search_triggered_for_widget,
)
from TerraLab.common.utils import (
    getTraduction,
    get_base_dir,
    get_config_value,
    set_config_value,
)
from TerraLab.data.layer_manager import LayerId
from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    LP_MODE_BORTLE,
    is_automatic_mode,
    normalize_light_pollution_mode,
)
from TerraLab.terrain.data_sources import SurfaceMode
from TerraLab.ui.data_layers_dialog import DataLayerChanges, DataLayersDialog
from TerraLab.ui.widget_misc_helpers import (
    widget_ensure_copernicus_credentials_prompt,
    widget_refresh_climate_status_indicator,
    widget_refresh_milkyway_status_indicator,
)
from TerraLab.ui.widget_runtime_helpers import (
    run_smoke_scenes as widget_run_smoke_scenes,
)
from TerraLab.widgets.spherical_math import ra_dec_to_alt_az
from TerraLab.widgets.telescope_runtime import on_resize as telescope_on_resize


class WidgetSurfaceDataMixin:
    def on_surface_mode_changed(self, value):
        """Persist and apply the semantic surface mode chosen by the switch."""
        grouped = self._usable_surface_mode_sources()
        if not all(grouped.values()):
            self._sync_surface_mode_control()
            return
        target_mode = (
            SurfaceMode.LAND_COVER if int(value) else SurfaceMode.ORTHOPHOTO
        )
        manager = getattr(self, "layer_manager", None)
        registry = getattr(manager, "data_sources", None)
        target_sources = grouped[target_mode]
        if not self._surface_sources_covering_observer(target_sources):
            self._sync_surface_mode_control()
            QMessageBox.information(
                self,
                (
                    "Ortofoto fora de cobertura"
                    if target_mode is SurfaceMode.ORTHOPHOTO
                    else "Cobertura fora de l'àrea"
                ),
                self._surface_coverage_message(target_mode, target_sources),
            )
            return
        registry.set_surface_mode(target_mode)
        self._effective_data_sources_payload = None
        self._sync_surface_mode_control()

        checkbox = getattr(self, "chk_surface_layer", None)
        coordinator = getattr(self, "terrain_coordinator", None)
        refresh = getattr(coordinator, "request_surface_refresh", None)
        if checkbox is not None and checkbox.isChecked() and callable(refresh):
            view_context = getattr(self, "_surface_refresh_view_kwargs", None)
            refresh(
                profile=getattr(self, "_full_horizon_profile", None),
                surface_mode=target_mode.value,
                atomic_surface_swap=True,
                **(view_context() if callable(view_context) else {}),
            )
        canvas = getattr(self, "canvas", None)
        if canvas is not None:
            canvas.update()

    def _activate_checked_surface_layer_startup(self):
        if bool(getattr(self, "_initial_surface_refresh_requested", False)):
            return
        checkbox = getattr(self, "chk_surface_layer", None)
        if checkbox is None or not bool(checkbox.isChecked()):
            return
        self._initial_surface_refresh_requested = True
        self.on_surface_layer_toggled(True)

    def on_solar_system_toggled(self, checked):
        checked = bool(checked)
        self._persist_visibility_state("sistema_solar", checked)
        for name in ("chk_sun_moon", "chk_planets"):
            control = getattr(self, name, None)
            if control is not None:
                control.setEnabled(checked)
        self.canvas.update()

    def open_data_layers_dialog(self, focus_layer_id=None):
        dialog = DataLayersDialog(
            self,
            registry=self.asset_manager.data_sources,
            asset_manager=self.asset_manager,
            latitude=float(self.latitude),
            longitude=float(self.longitude),
            focus_layer_id=focus_layer_id,
        )
        result = dialog.exec_()
        dialog.post_close_changes.connect(self._apply_data_layer_changes)
        # Layer-library actions are persisted immediately, so closing the
        # window through its title bar must refresh runtime state as well.
        self._apply_data_layer_changes(dialog.changes)
        self._sync_layer_visibility_controls()
        self._sync_runtime_asset_config()
        return result

    def _sync_layer_visibility_controls(self):
        manager = getattr(self, "layer_manager", None)
        if manager is None:
            return
        controls = (
            ("chk_enable_sky", LayerId.SKY_STARS),
            ("chk_deep_space", LayerId.SKY_NGC),
            ("chk_enable_milkyway", LayerId.SKY_MILKY_WAY),
            ("chk_enable_planck_dust", LayerId.SKY_PLANCK_DUST),
            ("chk_solar_system", LayerId.SKY_SOLAR_SYSTEM),
            ("chk_clima", LayerId.SKY_WEATHER),
            ("chk_enable_village", LayerId.EARTH_TERRAIN),
            ("chk_surface_layer", LayerId.EARTH_SURFACE),
            ("chk_light_pollution", LayerId.EARTH_LIGHT_POLLUTION),
        )
        for attribute, layer_id in controls:
            checkbox = getattr(self, attribute, None)
            desired = manager.is_visible(layer_id)
            if checkbox is not None and bool(checkbox.isChecked()) != desired:
                checkbox.setChecked(desired)
        self._sync_surface_mode_control()
        if hasattr(self, "canvas"):
            self.canvas.update()

    def _apply_data_layer_changes(self, changes: DataLayerChanges):
        if changes is None:
            return
        self._effective_data_sources_payload = None
        coordinator = getattr(self, "terrain_coordinator", None)
        if bool(changes.surface):
            refresh = getattr(coordinator, "request_surface_refresh", None)
            if callable(refresh):
                view_context = getattr(
                    self, "_surface_refresh_view_kwargs", None
                )
                refresh(**(view_context() if callable(view_context) else {}))
        if bool(changes.elevation or changes.representation):
            abort = getattr(coordinator, "abort_current_job", None)
            if callable(abort):
                abort()
            relocate = getattr(self, "request_relocation", None)
            if callable(relocate):
                relocate()
        if bool(changes.light_pollution):
            reload_config = getattr(coordinator, "reload_config", None)
            if callable(reload_config):
                reload_config()
            recalculate = getattr(
                self, "recalculate_automatic_light_pollution", None
            )
            if callable(recalculate) and is_automatic_mode(
                getattr(self, "light_pollution_mode", LP_MODE_BORTLE)
            ):
                recalculate()
        self._refresh_data_layer_indicators()

    def _refresh_data_layer_indicators(self):
        manager = getattr(self, "layer_manager", None)
        if manager is not None:
            # Status is evaluated lazily from the registry; touching it here
            # refreshes availability after asynchronous inspections.
            for descriptor in manager.list_layers():
                manager.status(descriptor.id)
        self._sync_surface_mode_control()
        if hasattr(self, "canvas"):
            self.canvas.update()

    def _effective_layer_label(
        self,
        selection,
        fallback: str,
        *,
        runtime=None,
        registry=None,
    ):
        """Describe the source actually used by runtime, including fallbacks."""
        runtime = runtime if isinstance(runtime, dict) else None
        catalogue = getattr(selection, "effective", None)
        effective = catalogue
        reason = str(getattr(selection, "reason", "") or "")
        if runtime is not None:
            reason = str(runtime.get("status", "") or reason)
            source_id = str(runtime.get("source_id", "") or "")
            effective = (
                registry.get(source_id)
                if source_id and registry is not None
                else None
            )
        if effective is None:
            text = str(fallback)
        else:
            text = str(
                getattr(effective, "display_name", "")
                or getattr(effective, "id", "")
                or fallback
            )
            try:
                resolution = float(
                    getattr(effective, "resolution_m", 0.0) or 0.0
                )
            except (TypeError, ValueError):
                resolution = 0.0
            if resolution > 0.0:
                text += f" · {resolution:g} m"
        tooltip_lines = []
        if (
            runtime is not None
            and catalogue is not None
            and effective is not catalogue
        ):
            candidate_name = str(
                getattr(catalogue, "display_name", "")
                or getattr(catalogue, "id", "")
            )
            if candidate_name:
                tooltip_lines.append(
                    f"Candidata del catàleg: {candidate_name}"
                )
        configured = getattr(selection, "configured", None)
        if configured is not None and configured is not catalogue:
            configured_name = str(
                getattr(configured, "display_name", "") or ""
            )
            if configured_name:
                tooltip_lines.append(f"Font configurada: {configured_name}")
        if reason:
            tooltip_lines.append(f"Motiu: {reason}")
        return text, "\n".join(tooltip_lines)

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
            self.weather.set_remote_user_agent(
                self.asset_manager.get_user_agent()
            )
        if hasattr(self, "canvas") and hasattr(self.canvas, "weather"):
            self.canvas.weather.set_remote_weather_enabled(enabled)
            self.canvas.weather.set_remote_user_agent(
                self.asset_manager.get_user_agent()
            )
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
        if not hasattr(self, "lbl_stars_fallback") or not hasattr(
            self, "chk_enable_sky"
        ):
            return
        if not bool(self.chk_enable_sky.isChecked()):
            self.lbl_stars_fallback.hide()
            self._stars_fallback_since = None
            self._stars_primary_since = None
            return
        fallback_active = bool(getattr(self, "_stars_fallback_active", False))
        fallback_reason = str(
            getattr(self, "_stars_fallback_reason", "") or ""
        )
        now_m = time.monotonic()
        if not fallback_active:
            self._stars_fallback_since = None
            if self._stars_primary_since is None:
                self._stars_primary_since = now_m
            stable_s = now_m - self._stars_primary_since
            if stable_s >= float(
                getattr(self, "_stars_primary_hide_delay_s", 0.8)
            ):
                self.lbl_stars_fallback.hide()
            return
        self._stars_primary_since = None
        if self._stars_fallback_since is None:
            self._stars_fallback_since = now_m
        stable_s = now_m - self._stars_fallback_since
        if stable_s >= float(
            getattr(self, "_stars_fallback_show_delay_s", 1.2)
        ):
            txt = getTraduction(
                "Astro.StarsFallbackActive", "Catàleg fallback"
            )
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

    def _request_automatic_bortle_estimate(self) -> bool:
        """Request a location estimate asynchronously."""
        if not is_automatic_mode(self.light_pollution_mode):
            return False
        coordinator = getattr(self, "terrain_coordinator", None)
        if coordinator is None:
            return False
        request_id = int(getattr(self, "_auto_bortle_request_seq", 0)) + 1
        self._auto_bortle_request_seq = request_id
        self._auto_bortle_pending_request_id = request_id
        coordinator.request_bortle_estimate(
            float(self.latitude),
            float(self.longitude),
            int(request_id),
        )
        return True

    def _apply_automatic_bortle_estimate(self, bortle_value: int) -> None:
        """Apply a validated estimate without changing the selected mode."""
        if not is_automatic_mode(self.light_pollution_mode):
            return
        valor_bortle = int(max(1, min(9, int(bortle_value))))
        print(f"[AstroWidget] Applied auto-estimated Bortle: {valor_bortle}")
        self.auto_bortle_estimate = valor_bortle
        set_config_value("auto_bortle_estimate", int(valor_bortle))
        self._sync_light_pollution_controls()
        self._apply_light_pollution_graphics()

    def on_horizon_bortle_estimate(
        self, request_id: int, lat: float, lon: float, bortle_value: int
    ) -> None:
        """Discard stale estimates and apply only the active location."""
        pending_request_id = int(
            getattr(self, "_auto_bortle_pending_request_id", 0)
        )
        if not pending_request_id or int(request_id) != pending_request_id:
            return
        self._auto_bortle_pending_request_id = 0
        if not is_automatic_mode(self.light_pollution_mode):
            return
        if (
            abs(float(lat) - float(self.latitude)) > 1e-7
            or abs(float(lon) - float(self.longitude)) > 1e-7
        ):
            return
        self._apply_automatic_bortle_estimate(int(bortle_value))

    def recalculate_automatic_light_pollution(self):
        """Recalculate the locked automatic mode for the current location."""
        self._request_automatic_bortle_estimate()

    def update_lp_slider(self, val):
        mode = normalize_light_pollution_mode(self.light_pollution_mode)
        if mode == LP_MODE_AUTOMATIC:
            self._sync_light_pollution_controls()
            return
        if mode == LP_MODE_BORTLE:
            self.bortle_value = int(max(1, min(9, int(val))))
            set_config_value("bortle_value", int(self.bortle_value))
        else:
            self.magnitude_limit = val / 10.0
            set_config_value("magnitude_limit", float(self.magnitude_limit))
        self._apply_light_pollution_graphics()

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
        default_path = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "stars"
            / "no_gaia_stars.json"
        )
        self._named_star_search_entries_cache = load_named_star_entries(
            default_path
        )
        return self._named_star_search_entries_cache

    def _load_ngc_search_entries(self):
        cached = getattr(self, "_ngc_search_entries_cache", None)
        if cached is not None:
            return cached
        path = getattr(self, "_astro_ngc_catalog_path", "")
        if not path:
            cfg_path = str(
                get_config_value("ngc_catalog_path", "") or ""
            ).strip()
            if cfg_path and os.path.isfile(cfg_path):
                path = cfg_path
        if not path:
            runtime_layout = getattr(self, "runtime_layout", {}) or {}
            runtime_path = (
                Path(runtime_layout.get("data_ngc", get_base_dir()))
                / "openngc_catalog.csv"
            )
            if runtime_path.exists():
                path = str(runtime_path)
            else:
                path = os.path.abspath(
                    os.path.join(
                        os.path.dirname(__file__),
                        "..",
                        "data",
                        "sky",
                        "openngc_catalog.csv",
                    )
                )
        self._ngc_search_entries_cache = load_ngc_entries(path)
        return self._ngc_search_entries_cache

    def _normalize_search_key(text: str) -> str:
        return AstroSearchEngine.normalize_key(text)

    def _prepare_skyfield_cache_for_search(self):
        """Force a fresh cache sample so planet search can resolve current Alt/Az."""
        if not hasattr(self, "canvas") or not hasattr(self, "eph"):
            return None
        try:
            ut_hour, day_of_year_utc, _, _ = (
                self.canvas._get_current_utc_context()
            )
            self.canvas.update_skyfield_cache(ut_hour, day_of_year_utc)
        except Exception as ex:
            print(f"[AstroWidget] Search cache update failed: {ex}")
        sf_cache = getattr(self.canvas, "_sf_cache", None)
        if isinstance(sf_cache, dict):
            return sf_cache.get("data")
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
        if not hasattr(self, "panels_widget"):
            return
        # Current state based on panel visibility
        is_visible = self.panels_widget.isVisible()
        should_hide = is_visible  # If visible, we want to hide
        # Toggle Panels
        self.panels_widget.setVisible(not should_hide)
        # Update styling/transparency
        if should_hide:
            # COLLAPSED STATE: Transparent background, no border
            # Only time bar is visible (row 2)
            self.frame_controls.setStyleSheet(
                "QFrame { background: transparent; border: none; }"
            )
            self.time_bar.setVisible(True)  # Keep timebar
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
        if hasattr(self, "frame_controls") and hasattr(self, "btn_collapse"):
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
            bx, by = state.get(
                "collapse_button_pos", (rect.right(), rect.top())
            )
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
            return n.hour + n.minute / 60.0
        return self.manual_hour
