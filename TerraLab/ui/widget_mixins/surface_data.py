"""Surface refresh, data-layer dialog, and runtime status handling."""

from __future__ import annotations

import os
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QMessageBox

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
from TerraLab.data.source_catalog import SurfaceMode
from TerraLab.ui.data_layers_dialog import DataLayerChanges, DataLayersDialog
from TerraLab.ui.widget_misc_helpers import (
    widget_ensure_copernicus_credentials_prompt,
    widget_refresh_climate_status_indicator,
    widget_refresh_milkyway_status_indicator,
)
from TerraLab.ui.widget_runtime_helpers import (
    run_smoke_scenes as widget_run_smoke_scenes,
)
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
        client = getattr(self, "search_index_client", None)
        if client is None:
            return
        runtime_layout = getattr(self, "runtime_layout", {}) or {}
        catalog_artifact = getattr(
            self, "_render_catalog_artifact", {}
        )
        catalog_artifact = (
            catalog_artifact
            if isinstance(catalog_artifact, dict)
            else {}
        )
        client.request(
            {
                "named_stars_path": str(
                    Path(__file__).resolve().parents[2]
                    / "data"
                    / "stars"
                    / "no_gaia_stars.json"
                ),
                "ngc_paths": [
                    str(
                        get_config_value("ngc_catalog_path", "") or ""
                    ),
                    str(
                        Path(
                            runtime_layout.get(
                                "data_ngc", get_base_dir()
                            )
                        )
                        / "openngc_catalog.csv"
                    ),
                    str(
                        Path(__file__).resolve().parents[2]
                        / "data"
                        / "sky"
                        / "openngc_catalog.csv"
                    ),
                ],
                "gaia_catalog_path": str(
                    catalog_artifact.get("catalog_path", "") or ""
                ),
                "gaia_suggestion_limit": 5000,
            }
        )

    def _attach_search_completer(self, names):
        if not hasattr(self, "txt_search"):
            return
        from PyQt5.QtWidgets import QCompleter
        from PyQt5.QtCore import Qt

        previous = self.txt_search.completer()
        if previous is not None:
            try:
                previous.activated[str].disconnect(
                    self.on_search_triggered
                )
            except (TypeError, RuntimeError):
                pass
            previous.deleteLater()
        completer = QCompleter(names, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.txt_search.setCompleter(completer)
        completer.activated[str].connect(self.on_search_triggered)
        self.txt_search.setEnabled(True)

    @staticmethod
    def _normalize_search_key(text: str) -> str:
        lowered = str(text or "").strip().lower()
        folded = unicodedata.normalize("NFKD", lowered)
        return "".join(
            character
            for character in folded
            if not unicodedata.combining(character)
        )

    def _on_search_index_ready(self, payload) -> None:
        records = (
            payload.get("records", ())
            if isinstance(payload, dict)
            else ()
        )
        lookup = {}
        names = []
        for raw in records:
            if not isinstance(raw, dict):
                continue
            record = dict(raw)
            for alias in record.get("aliases", ()) or ():
                name = str(alias or "").strip()
                key = self._normalize_search_key(name)
                if key and key not in lookup:
                    lookup[key] = record
                    names.append(name)
        self.search_lookup = lookup
        artifact = payload.get("ngc_artifact")
        self._render_ngc_artifact = (
            dict(artifact) if isinstance(artifact, dict) else {}
        )
        self.search_index = {
            name: lookup[self._normalize_search_key(name)]
            for name in names
        }
        self._attach_search_completer(
            sorted(names, key=lambda value: value.lower())
        )
        self.canvas.update()

    def _search_record(self, text: str):
        key = self._normalize_search_key(text)
        record = getattr(self, "search_lookup", {}).get(key)
        if record is not None:
            return record
        for candidate, value in getattr(
            self, "search_lookup", {}
        ).items():
            if key and key in candidate:
                return value
        return None

    def on_search_triggered(self, text_override=None):
        raw = (
            text_override
            if isinstance(text_override, str)
            else self.txt_search.text()
        )
        text = str(raw or "").strip()
        if not text:
            return
        normalized = self._normalize_search_key(text)
        now = time.monotonic()
        if (
            normalized
            and normalized
            == getattr(self, "_last_search_trigger_key", "")
            and now
            - float(getattr(self, "_last_search_trigger_mono", 0.0))
            < 0.20
        ):
            # Selecting a completer row with Return can emit both activated
            # and returnPressed. One logical Goto must produce one request.
            return
        self._last_search_trigger_key = normalized
        self._last_search_trigger_mono = now
        record = self._search_record(text)
        if record is None:
            print(
                getTraduction(
                    "Astro.SearchNotFound",
                    "Object '{name}' not found in index.",
                ).format(name=text)
            )
            return
        self.center_on_object(record)

    def center_on_object(self, info):
        if not isinstance(info, dict):
            return
        record = dict(info)
        legacy_type = str(record.get("type", "") or "")
        if "kind" not in record and legacy_type:
            record["kind"] = legacy_type
        obj = record.pop("obj", None)
        if obj is not None and record.get("kind") in {"ngc", "star"}:
            record.setdefault("ra", getattr(obj, "ra_deg", None))
            record.setdefault("dec", getattr(obj, "dec_deg", None))
        try:
            ut_hour, day, year, _ = self.canvas._get_current_utc_context()
        except (AttributeError, TypeError, ValueError):
            return
        request_sequence = int(
            getattr(self, "_search_request_sequence", 0)
        ) + 1
        self._search_request_sequence = request_sequence
        request_token = str(request_sequence)
        self._pending_search_token = request_token
        view_revision = int(
            getattr(self.canvas, "_view_interaction_revision", 0)
        )
        self.search_resolve_client.request(
            {
                "record": record,
                "year_utc": int(year),
                "day_of_year_utc": int(day),
                "ut_hour": float(ut_hour),
                "latitude": float(self.latitude),
                "longitude": float(self.longitude),
                "client_token": request_token,
                "view_revision": view_revision,
            },
            force=True,
        )

    def _on_search_resolved(self, result) -> None:
        if not isinstance(result, dict):
            return
        result = dict(result)
        response_token = str(
            result.pop("_client_token", "") or ""
        )
        if response_token and response_token != str(
            getattr(self, "_pending_search_token", "") or ""
        ):
            return
        try:
            request_view_revision = int(
                result.pop(
                    "_view_revision",
                    getattr(
                        self.canvas,
                        "_view_interaction_revision",
                        0,
                    ),
                )
            )
        except (TypeError, ValueError):
            return
        if request_view_revision != int(
            getattr(self.canvas, "_view_interaction_revision", 0)
        ):
            # A delayed Compute response must not override a drag/zoom made
            # after the search was submitted.
            return
        self._pending_search_token = None
        altitude = max(
            -89.9,
            min(89.9, float(result.get("alt", 0.0))),
        )
        azimuth = float(result.get("az", 0.0)) % 360.0
        self.target_azimuth = None
        self.target_elevation = None
        anim_timer = getattr(self, "anim_timer", None)
        if anim_timer is not None:
            anim_timer.stop()
        self.canvas._set_selected_target(dict(result))
        self.canvas.azimuth_offset = azimuth
        self.canvas.elevation_angle = altitude
        self.canvas.dragging = False
        if self.canvas.scope_mode_enabled():
            self.canvas.scope_camera_lock_to_target = True
            self.canvas.scope_reticle_lock_to_target = True
            self.canvas.scope_controller.set_center(
                (altitude, azimuth), confirmed=True
            )
        if altitude < -5 and hasattr(self.canvas, "hint_overlay"):
            self.canvas.hint_overlay.show_hint(
                f"Object below horizon ({altitude:.1f} deg)"
            )
        self.canvas.update()

    def _request_weather_sample(self) -> None:
        client = getattr(self, "weather_compute_client", None)
        if client is None:
            return
        try:
            hour, day, year, _ = self.canvas._get_current_utc_context()
        except (AttributeError, TypeError, ValueError):
            return
        client.request(
            {
                "latitude": float(self.latitude),
                "longitude": float(self.longitude),
                "year_utc": int(year),
                "day_of_year_utc": int(day),
                # Forecast samples change only at the surrounding hourly pair.
                "ut_hour": int(float(hour)) % 24,
                "use_remote": bool(self.weather_use_remote_metno),
                "cache_enabled": bool(self.weather_cache_enabled),
                "user_agent": str(self.asset_manager.get_user_agent()),
            }
        )

    def _on_weather_sample_ready(self, payload) -> None:
        self.weather.set_sample_artifact(payload)
        self.canvas.update()

    def _coordinate_context(self) -> dict | None:
        try:
            hour, day, year, _ = self.canvas._get_current_utc_context()
        except (AttributeError, TypeError, ValueError):
            return None
        return {
            "year_utc": int(year),
            "day_of_year_utc": int(day),
            "ut_hour": float(hour),
            "latitude": float(self.latitude),
            "longitude": float(self.longitude),
        }

    def request_scope_goto(self, ra_deg: float, dec_deg: float) -> None:
        context = self._coordinate_context()
        if context is None:
            return
        self.scope_goto_client.request(
            {
                **context,
                "direction": "radec_to_altaz",
                "ra": float(ra_deg),
                "dec": float(dec_deg),
            },
            force=True,
        )

    def request_circumpolar_alignment(self) -> None:
        context = self._coordinate_context()
        client = getattr(self, "circumpolar_client", None)
        if context is None or client is None:
            return
        client.request(context, force=True)

    def _on_circumpolar_alignment_ready(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        control = getattr(self, "chk_trails", None)
        if control is None or not bool(control.isChecked()):
            return
        altitude = float(payload["alt"])
        azimuth = float(payload["az"]) % 360.0
        self.target_azimuth = None
        self.target_elevation = None
        self.canvas.azimuth_offset = azimuth
        self.canvas.elevation_angle = max(-90.0, min(90.0, altitude))
        self.canvas.dragging = False
        self.canvas._set_selected_target(
            {
                "kind": "star",
                "name": str(payload.get("name", "Polaris")),
                "alt": altitude,
                "az": azimuth,
                "star": {
                    "name": str(payload.get("name", "Polaris")),
                    "ra": float(payload.get("ra", 37.95456067)),
                    "dec": float(payload.get("dec", 89.26410897)),
                },
            }
        )
        self.canvas.update()

    def _on_scope_goto_ready(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        altitude = float(payload["alt"])
        azimuth = float(payload["az"]) % 360.0
        self.canvas._set_selected_target(
            {
                "kind": "radec",
                "name": "RA/Dec",
                "ra": float(payload.get("ra", 0.0)) % 360.0,
                "dec": float(payload.get("dec", 0.0)),
                "alt": altitude,
                "az": azimuth,
            }
        )
        self.canvas.scope_camera_lock_to_target = True
        self.canvas.scope_reticle_lock_to_target = True
        if not self.canvas.scope_mode_enabled():
            self._scope_ui_manager.activate()
        self.canvas.scope_controller.set_center(
            (altitude, azimuth), confirmed=True
        )
        self.canvas.azimuth_offset = azimuth
        self.canvas.elevation_angle = max(-90.0, min(90.0, altitude))
        self.canvas.dragging = False
        self.canvas.update()

    def request_scope_tracking_update(self) -> None:
        canvas = getattr(self, "canvas", None)
        if (
            canvas is None
            or not canvas.scope_mode_enabled()
            or not bool(
                getattr(canvas, "scope_camera_lock_to_target", False)
            )
        ):
            return
        target = getattr(canvas, "selected_target", None)
        if not isinstance(target, dict):
            return

        target_type = str(target.get("type", "") or "").lower()
        if target.get("kind") == "sky" and target_type in {
            "sun",
            "moon",
            "planet",
        }:
            snapshot = self.ephemeris_coordinator.get_snapshot() or {}
            body = None
            if target_type in {"sun", "moon"}:
                body = snapshot.get(target_type)
            else:
                wanted = str(target.get("key", "") or "").lower()
                body = next(
                    (
                        item
                        for item in snapshot.get("planets", ()) or ()
                        if isinstance(item, dict)
                        and str(item.get("key", "") or "").lower()
                        == wanted
                    ),
                    None,
                )
            if isinstance(body, dict):
                self._apply_scope_tracking_position(
                    float(body.get("alt", target.get("alt", 0.0))),
                    float(body.get("az", target.get("az", 0.0))),
                )
            return

        ra = target.get("ra")
        dec = target.get("dec")
        star = target.get("star")
        if isinstance(star, dict):
            ra = star.get("ra", ra)
            dec = star.get("dec", dec)
        if ra is None or dec is None:
            return
        context = self._coordinate_context()
        client = getattr(self, "scope_track_client", None)
        if context is None or client is None:
            return
        client.request(
            {
                **context,
                "direction": "radec_to_altaz",
                "ra": float(ra),
                "dec": float(dec),
            }
        )

    def _on_scope_track_ready(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        canvas = getattr(self, "canvas", None)
        if canvas is None or not canvas.scope_mode_enabled():
            return
        target = getattr(canvas, "selected_target", None)
        if not isinstance(target, dict):
            return
        target_ra = target.get("ra")
        target_dec = target.get("dec")
        if isinstance(target.get("star"), dict):
            target_ra = target["star"].get("ra", target_ra)
            target_dec = target["star"].get("dec", target_dec)
        try:
            if (
                abs(
                    (
                        float(payload["ra"])
                        - float(target_ra)
                        + 180.0
                    )
                    % 360.0
                    - 180.0
                )
                > 1e-5
                or abs(float(payload["dec"]) - float(target_dec))
                > 1e-5
            ):
                return
        except (KeyError, TypeError, ValueError):
            return
        self._apply_scope_tracking_position(
            float(payload["alt"]), float(payload["az"])
        )

    def _apply_scope_tracking_position(
        self, altitude: float, azimuth: float
    ) -> None:
        canvas = self.canvas
        if not canvas.scope_mode_enabled():
            return
        altitude = max(-89.9, min(89.9, float(altitude)))
        azimuth = float(azimuth) % 360.0
        if bool(getattr(canvas, "scope_reticle_lock_to_target", False)):
            canvas.scope_controller.set_center(
                (altitude, azimuth), confirmed=True
            )
        if bool(getattr(canvas, "scope_camera_lock_to_target", False)):
            canvas.azimuth_offset = azimuth
            canvas.elevation_angle = altitude
        canvas.update()

    def request_scope_reverse(self, alt: float, az: float) -> None:
        context = self._coordinate_context()
        if context is None:
            return
        self.scope_reverse_client.request(
            {
                **context,
                "direction": "altaz_to_radec",
                "alt": float(alt),
                "az": float(az),
            }
        )

    def _on_scope_reverse_ready(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        self._set_scope_coord_inputs(
            float(payload["ra"]), float(payload["dec"])
        )

    def run_smoke_scenes(self):
        return widget_run_smoke_scenes(self)

    def _refresh_drawer_geometry(self):
        shell = getattr(self, "viewport_shell", None)
        if shell is not None and shell.layout() is not None:
            shell.layout().invalidate()
            shell.layout().activate()
            shell.updateGeometry()
        canvas = getattr(self, "canvas", None)
        if canvas is not None:
            canvas.updateGeometry()
            canvas.update()
        content_layout = getattr(self, "content_layout", None)
        if content_layout is not None:
            content_layout.invalidate()
            content_layout.activate()

    def _sync_drawer_buttons(self, active_key=None):
        for key, button in getattr(self, "drawer_buttons", {}).items():
            button.blockSignals(True)
            button.setChecked(key == active_key)
            button.blockSignals(False)

    def set_control_drawer(self, key, *, toggle=False):
        pages = getattr(self, "drawer_pages", {})
        drawer = getattr(self, "control_drawer", None)
        stack = getattr(self, "drawer_stack", None)
        if key not in pages or drawer is None or stack is None:
            return

        current_key = getattr(self, "_current_drawer_key", None)
        if toggle and drawer.isVisible() and current_key == key:
            self.close_control_drawer()
            return

        stack.setCurrentWidget(pages[key])
        titles = {
            "location": "Ubicació",
            "sky": "Cel",
            "earth": "Terra",
            "tools": "Eines",
        }
        title = getattr(self, "lbl_drawer_title", None)
        if title is not None:
            title.setText(titles[key])
        self._current_drawer_key = key
        self._last_drawer_key = key
        drawer.show()
        self._sync_drawer_buttons(key)

        tools_button = getattr(self, "btn_tools_panel", None)
        if tools_button is not None:
            tools_button.blockSignals(True)
            tools_button.setChecked(key == "tools")
            tools_button.blockSignals(False)

        self._refresh_drawer_geometry()
        QTimer.singleShot(0, self._refresh_drawer_geometry)

    def close_control_drawer(self):
        drawer = getattr(self, "control_drawer", None)
        if drawer is None:
            return
        drawer.hide()
        self._current_drawer_key = None
        self._sync_drawer_buttons()

        tools_button = getattr(self, "btn_tools_panel", None)
        if tools_button is not None:
            tools_button.blockSignals(True)
            tools_button.setChecked(False)
            tools_button.blockSignals(False)

        self._refresh_drawer_geometry()
        QTimer.singleShot(0, self._refresh_drawer_geometry)

    def toggle_controls(self):
        if hasattr(self, "control_drawer"):
            if self.control_drawer.isVisible():
                self.close_control_drawer()
            else:
                self.set_control_drawer(
                    getattr(self, "_last_drawer_key", "sky") or "sky"
                )
            return
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
        self._position_loading_label()
        self._position_gaia_extension_status_label()
        # Defer position update to ensure layout geometry is final
        QTimer.singleShot(0, self._update_button_pos)

    def get_current_hour(self):
        if self.use_real_time:
            n = datetime.now()
            return n.hour + n.minute / 60.0
        return self.manual_hour
