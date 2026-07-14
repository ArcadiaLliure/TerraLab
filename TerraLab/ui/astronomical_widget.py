"""Nova facade d'AstronomicalWidget centrada en UI + coordinadors.

Manté compatibilitat temporal amb implementacio legacy de sky_widget_impl.
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

try:
    from PyQt5.QtCore import QTimer
except Exception:  # pragma: no cover
    QTimer = None

from TerraLab.astro.ephemeris_coordinator import EphemerisCoordinator
from TerraLab.common.deprecation_registry import (
    emit_deprecation_warning,
    register_deprecated_method,
)
from TerraLab.data.star_data_coordinator import StarDataCoordinator
from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    normalize_light_pollution_mode,
    resolve_bortle_class,
)
from TerraLab.scene.scene_controller import SceneController
from TerraLab.terrain.terrain_coordinator import TerrainCoordinator
from TerraLab.ui.sky_widget_impl import AstronomicalWidget as LegacyAstronomicalWidget
from TerraLab.widgets.sky_legacy_components import STAR_CATALOG_NAKED_EYE_MAX_MAG


register_deprecated_method(
    entry_id="TerraLab.ui.sky_widget_impl.AstronomicalWidget.update_loop",
    module_path="TerraLab.ui.sky_widget_impl",
    class_name="AstronomicalWidget",
    method_name="update_loop",
    replacement="TerraLab.scene.scene_controller.SceneController.update",
    phase_introduced=12,
    notes="Bucle de temps i frame state delegat a SceneController",
)


class AstronomicalWidget(LegacyAstronomicalWidget):
    """Widget de UI que agrega coordinadors nous sense trencar compatibilitat."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Coordinadors nous (fase 4-10) en parallel al flux legacy.
        self.scene_controller = SceneController(
            latitude=float(getattr(self, "latitude", 0.0)),
            longitude=float(getattr(self, "longitude", 0.0)),
            altitude_m=float(getattr(self, "_observer_offset", 0.0)),
            manual_year=int(getattr(self, "manual_year", 2026)),
            manual_day=int(getattr(self, "manual_day", 0)),
            manual_hour=float(getattr(self, "manual_hour", 12.0)),
            use_real_time=bool(getattr(self, "use_real_time", True)),
            azimuth_offset=float(getattr(getattr(self, "canvas", None), "azimuth_offset", 0.0)),
            elevation_angle=float(getattr(getattr(self, "canvas", None), "elevation_angle", 40.0)),
            zoom_level=float(getattr(getattr(self, "canvas", None), "zoom_level", 1.0)),
            vertical_offset_ratio=float(getattr(getattr(self, "canvas", None), "vertical_offset_ratio", 0.3)),
            light_pollution_mode=normalize_light_pollution_mode(
                getattr(self, "light_pollution_mode", LP_MODE_AUTOMATIC)
            ),
        )

        self.star_data_coordinator = None
        self._last_scope_tile_request = ""
        self._gaia_manifest_path = self._resolve_gaia_manifest_path()
        self._gaia_manifest_last_mtime = 0.0
        self._gaia_general_tile_loaded_mtime = 0.0
        self._try_attach_star_data_coordinator(force_general_reload=True)

        self.terrain_coordinator = TerrainCoordinator(
            tiles_dir=str(getattr(self, "runtime_layout", {}).get("data_elevation", "") or "")
        )
        self.ephemeris_coordinator = EphemerisCoordinator()
        self._horizon_worker_bridge_connected = False
        self._gaia_attach_timer = None
        if QTimer is not None:
            self._gaia_attach_timer = QTimer(self)
            self._gaia_attach_timer.setInterval(1500)
            self._gaia_attach_timer.timeout.connect(
                self._poll_incremental_gaia_updates
            )
            self._gaia_attach_timer.start()
        self._attach_legacy_horizon_bridge()

    def _attach_legacy_horizon_bridge(self) -> None:
        """Connecta el worker legacy d'horitzó amb `TerrainCoordinator`."""
        self._try_connect_horizon_worker()
        if QTimer is None:
            return
        # El worker legacy es crea de manera diferida al bootstrap.
        QTimer.singleShot(700, self._try_connect_horizon_worker)
        QTimer.singleShot(1600, self._try_connect_horizon_worker)

    def _resolve_gaia_manifest_path(self) -> Path:
        """Resol la ruta esperada del `tile_manifest.json` Gaia runtime."""
        runtime_layout = getattr(self, "runtime_layout", {}) or {}
        gaia_dir = Path(str(runtime_layout.get("data_gaia", "") or "")).expanduser()
        if str(gaia_dir).strip():
            return gaia_dir / "tile_manifest.json"
        return Path("tile_manifest.json")

    def _connect_star_data_coordinator_signals(self) -> None:
        """Connecta senyals del coordinador de teseles amb el widget."""
        if self.star_data_coordinator is None:
            return
        self.star_data_coordinator.general_tile_ready.connect(
            self._on_general_tile_ready
        )
        self.star_data_coordinator.deep_tile_ready.connect(
            self._on_deep_tile_ready
        )
        self.star_data_coordinator.extension_ready.connect(
            self._on_extension_ready
        )
        self.star_data_coordinator.scope_index_ready.connect(
            self._on_scope_index_ready
        )
        self.star_data_coordinator.error_occurred.connect(
            self._on_star_data_error
        )

    def _try_attach_star_data_coordinator(
        self, *, force_general_reload: bool = False
    ) -> None:
        """Inicialitza o refresca coordinador Gaia quan hi ha manifest disponible."""
        manifest_path = self._gaia_manifest_path
        self._ensure_partial_manifest_from_general_tile()
        if not manifest_path.is_file():
            return
        try:
            manifest_mtime = float(manifest_path.stat().st_mtime)
        except Exception:
            manifest_mtime = 0.0

        manifest_changed = (
            manifest_mtime > float(self._gaia_manifest_last_mtime) + 1e-6
        )
        if self.star_data_coordinator is not None and manifest_changed:
            try:
                self.star_data_coordinator.shutdown()
            except Exception:
                pass
            self.star_data_coordinator = None

        if self.star_data_coordinator is None:
            self.star_data_coordinator = StarDataCoordinator(manifest_path)
            self._connect_star_data_coordinator_signals()
            force_general_reload = True
        if (
            force_general_reload
            or manifest_changed
        ):
            self._gaia_manifest_last_mtime = float(manifest_mtime)
            self.star_data_coordinator.load_general_tile()

    def _ensure_partial_manifest_from_general_tile(self) -> None:
        """Crea manifest parcial minim quan existeix `tile_all` pero no manifest."""
        manifest_path = self._gaia_manifest_path
        if manifest_path.is_file():
            return
        tile_all_path = manifest_path.parent / "tile_all.npz"
        if not tile_all_path.is_file():
            return
        try:
            with np.load(tile_all_path, allow_pickle=False) as tile_data:
                if "ra" in tile_data:
                    row_count = int(len(tile_data["ra"]))
                elif "RA" in tile_data:
                    row_count = int(len(tile_data["RA"]))
                else:
                    row_count = 0
        except Exception:
            return
        if row_count <= 0:
            return

        payload = {
            "version": 1,
            "tile_size_deg": 5.0,
            "partial": True,
            "general_tile": {
                "id": "tile_all",
                "file": "tile_all.npz",
                "mag_limit": float(STAR_CATALOG_NAKED_EYE_MAX_MAG),
                "coverage": "full_sky",
                "star_count": int(row_count),
            },
            "deep_tiles": [],
        }
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            with manifest_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=True)
        except Exception:
            return

    def _poll_incremental_gaia_updates(self) -> None:
        """Polling lleuger per activar carrega incremental de Gaia."""
        self._try_attach_star_data_coordinator(force_general_reload=False)
        self._maybe_reload_general_tile_incrementally()

    def _gaia_state_paths(self) -> list[Path]:
        """Retorna rutes candidates de fitxer d'estat Gaia (nou + legacy)."""
        runtime_layout = getattr(self, "runtime_layout", {}) or {}
        try:
            root_dir = Path(runtime_layout.get("root", Path.home())).resolve()
        except Exception:
            root_dir = Path.home()
        try:
            gaia_dir = Path(
                str(runtime_layout.get("data_gaia", "") or "")
            ).expanduser()
        except Exception:
            gaia_dir = Path()
        return [
            root_dir / "logs" / "gaia_tiles_state.json",
            gaia_dir / "gaia_tiles_state.json",
            root_dir / "logs" / "gaia_tap_state.json",
        ]

    def _read_gaia_state_file(self, state_path: Path) -> dict | None:
        """Llegeix un JSON d'estat Gaia i retorna `dict` si és vàlid."""
        candidate_path = Path(state_path).expanduser()
        if not candidate_path.exists():
            return None
        try:
            with candidate_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                payload["_state_path"] = str(candidate_path)
                return payload
        except Exception:
            return None
        return None

    def _gaia_state_is_pending(self, state: dict | None) -> bool:
        """Determina si una entrada d'estat Gaia està pendent."""
        if not isinstance(state, dict):
            return False
        status_name = str(state.get("status", "")).strip().lower()
        phase_name = str(state.get("phase", "")).strip().lower()
        done_tokens = {"done", "completed", "success"}
        return status_name not in done_tokens and phase_name not in done_tokens

    def _load_pending_gaia_state(self) -> dict | None:
        """Sobreescrit: carrega estat pendent Gaia (teseles + legacy)."""
        for state_path in self._gaia_state_paths():
            state = self._read_gaia_state_file(state_path)
            if self._gaia_state_is_pending(state):
                return state
        return None

    def _maybe_reload_general_tile_incrementally(self) -> None:
        """Recarrega catàleg quan arriba `tile_all` durant descàrrega pendent."""
        if not bool(getattr(self, "chk_enable_sky", None) and self.chk_enable_sky.isChecked()):
            return
        pending_state = self._load_pending_gaia_state()
        if not self._gaia_state_is_pending(pending_state):
            return
        assert isinstance(pending_state, dict)
        if not bool(pending_state.get("general_tile_done", False)):
            return

        output_dir = str(
            pending_state.get("output_dir", "")
            or getattr(self, "runtime_layout", {}).get("data_gaia", "")
        ).strip()
        if not output_dir:
            return
        tile_path = Path(output_dir).expanduser() / "tile_all.npz"
        if not tile_path.is_file():
            return
        try:
            tile_mtime = float(tile_path.stat().st_mtime)
        except Exception:
            return
        if tile_mtime <= float(self._gaia_general_tile_loaded_mtime) + 1e-6:
            return

        self._gaia_general_tile_loaded_mtime = float(tile_mtime)
        try:
            self._reload_star_catalog_async()
        except Exception:
            pass
        self._try_attach_star_data_coordinator(force_general_reload=True)
        try:
            self._set_gaia_extension_status_label(
                "Cataleg general Gaia (<8) carregat de forma incremental",
                keep_seconds=8.0,
            )
        except Exception:
            pass

    def _ensure_asset_before_enable(self, checkbox, checked: bool, asset_id: str) -> bool:
        """Evita onboarding forçat de Gaia si la descarrega ja està en curs."""
        if bool(checked) and str(asset_id or "") == "gaia_catalog":
            pending_state = self._load_pending_gaia_state()
            if self._gaia_state_is_pending(pending_state):
                try:
                    self._persist_visibility_state("estrelles", True)
                except Exception:
                    pass
                try:
                    from TerraLab.ui.widget_misc_helpers import (
                        widget_refresh_gaia_download_feedback,
                    )

                    widget_refresh_gaia_download_feedback(self)
                except Exception:
                    pass
                return True
        return super()._ensure_asset_before_enable(checkbox, checked, asset_id)

    def _try_connect_horizon_worker(self) -> None:
        """Intenta vincular senyals del worker legacy quan estigui disponible."""
        if self._horizon_worker_bridge_connected:
            return
        worker_horizon = getattr(self, "horizon_worker", None)
        if worker_horizon is None:
            return
        try:
            worker_horizon.profile_ready.connect(
                self.terrain_coordinator.ingest_profile_payload
            )
            worker_horizon.preview_ready.connect(
                self.terrain_coordinator.ingest_preview_payload
            )
            self._horizon_worker_bridge_connected = True
        except Exception:
            pass

    def closeEvent(self, event):
        """Allibera coordinadors nous abans de tancar el widget."""
        try:
            if self._gaia_attach_timer is not None:
                self._gaia_attach_timer.stop()
        except Exception:
            pass
        try:
            if self.star_data_coordinator is not None:
                self.star_data_coordinator.shutdown()
        except Exception:
            pass
        try:
            self.terrain_coordinator.shutdown()
        except Exception:
            pass
        try:
            self.ephemeris_coordinator.shutdown()
        except Exception:
            pass
        return super().closeEvent(event)

    def update_loop(self):
        """DEPRECATED: useu SceneController.update() en el nou bucle de frame."""
        emit_deprecation_warning(
            "TerraLab.ui.sky_widget_impl.AstronomicalWidget.update_loop",
            "TerraLab.scene.scene_controller.SceneController.update",
        )
        try:
            timer_interval_ms = 16.0
            if hasattr(self, "timer") and callable(getattr(self.timer, "interval", None)):
                timer_interval_ms = float(self.timer.interval())

            self.scene_controller.latitude = float(getattr(self, "latitude", self.scene_controller.latitude))
            self.scene_controller.longitude = float(getattr(self, "longitude", self.scene_controller.longitude))
            self.scene_controller.altitude_m = float(getattr(self, "_observer_offset", self.scene_controller.altitude_m))
            self.scene_controller.manual_year = int(getattr(self, "manual_year", self.scene_controller.manual_year))
            self.scene_controller.manual_day = int(getattr(self, "manual_day", self.scene_controller.manual_day))
            self.scene_controller.manual_hour = float(getattr(self, "manual_hour", self.scene_controller.manual_hour))
            self.scene_controller.use_real_time = bool(getattr(self, "use_real_time", self.scene_controller.use_real_time))
            self.scene_controller.mag_limit = float(getattr(self, "magnitude_limit", self.scene_controller.mag_limit))
            self.scene_controller.light_pollution_mode = normalize_light_pollution_mode(
                getattr(
                    self,
                    "light_pollution_mode",
                    self.scene_controller.light_pollution_mode,
                )
            )
            light_pollution_enabled = bool(
                getattr(self, "light_pollution_enabled", True)
            )
            self.scene_controller.bortle = int(
                round(
                    resolve_bortle_class(
                        self.scene_controller.light_pollution_mode,
                        automatic_bortle=getattr(self, "auto_bortle_estimate", 1),
                        bortle_value=getattr(self, "bortle_value", 1),
                        magnitude_limit=getattr(
                            self, "magnitude_limit", self.scene_controller.mag_limit
                        ),
                        light_pollution_enabled=light_pollution_enabled,
                    )
                )
            )

            canvas = getattr(self, "canvas", None)
            if canvas is not None:
                self.scene_controller.azimuth_offset = float(getattr(canvas, "azimuth_offset", self.scene_controller.azimuth_offset))
                self.scene_controller.elevation_angle = float(getattr(canvas, "elevation_angle", self.scene_controller.elevation_angle))
                self.scene_controller.zoom_level = float(getattr(canvas, "zoom_level", self.scene_controller.zoom_level))
                self.scene_controller.vertical_offset_ratio = float(
                    getattr(canvas, "vertical_offset_ratio", self.scene_controller.vertical_offset_ratio)
                )
                self.scene_controller.scope_enabled = bool(
                    getattr(canvas, "scope_mode_enabled", lambda: False)()
                )
                scope_controller = getattr(canvas, "scope_controller", None)
                scope_center = getattr(scope_controller, "center", None)
                if scope_center is None:
                    self.scene_controller.scope_center = None
                else:
                    self.scene_controller.scope_center = (
                        float(scope_center[0]),
                        float(scope_center[1]),
                    )
                try:
                    fov_w, fov_h = scope_controller.current_fov() if scope_controller is not None else (5.0, 5.0)
                    self.scene_controller.scope_fov_deg = (float(fov_w), float(fov_h))
                except Exception:
                    self.scene_controller.scope_fov_deg = (5.0, 5.0)

            self.scene_controller.update(max(0.0, timer_interval_ms / 1000.0))
            self.ephemeris_coordinator.configure_observer(
                latitude=float(self.scene_controller.latitude),
                longitude=float(self.scene_controller.longitude),
            )
            self.ephemeris_coordinator.request_snapshot(
                year_utc=int(self.scene_controller.manual_year),
                day_of_year_utc=int(self.scene_controller.manual_day),
                ut_hour=float(self.scene_controller.manual_hour),
            )

            if (
                self.star_data_coordinator is not None
                and getattr(self, "canvas", None) is not None
                and bool(getattr(self.canvas, "scope_mode_enabled", lambda: False)())
            ):
                scope_center = getattr(getattr(self.canvas, "scope_controller", None), "center", None)
                if scope_center is not None and hasattr(self.canvas, "_altaz_to_ra_dec"):
                    ra_dec = self.canvas._altaz_to_ra_dec(
                        float(scope_center[0]),
                        float(scope_center[1]),
                        float(self.scene_controller.manual_hour),
                        int(self.scene_controller.manual_day),
                    )
                    if ra_dec is not None:
                        ra_center, dec_center = float(ra_dec[0]), float(ra_dec[1])
                        radius = float(max(self.scene_controller.scope_fov_deg))
                        central_tile = self.star_data_coordinator.manifest().get_primary_tile_for_region(
                            ra_center=ra_center,
                            dec_center=dec_center,
                            radius_deg=radius,
                        )
                        if central_tile is not None:
                            central_tile_id = str(central_tile.tile_id)
                            if central_tile_id != self._last_scope_tile_request:
                                self._last_scope_tile_request = central_tile_id
                                self.star_data_coordinator.load_deep_tile(central_tile_id)
                                self.star_data_coordinator.preload_adjacent_tiles(central_tile_id)
                                self.star_data_coordinator.build_scope_index(central_tile_id)
        except Exception:
            pass
        return super().update_loop()

    def _on_general_tile_ready(self, payload):
        """Actualitza arrays principals quan arriba la tesela general."""
        self._apply_star_payload(payload)
        self._scope_catalog_loaded_max_mag = float(
            max(
                float(getattr(self, "_scope_catalog_loaded_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)),
                float(STAR_CATALOG_NAKED_EYE_MAX_MAG),
            )
        )
        if hasattr(self, "canvas"):
            self.canvas.update()

    def _on_deep_tile_ready(self, tile_id: str, _tile_payload):
        """Recepcio de tesela profunda carregada.

        No pre-carreguem aqui per evitar expansio en cascada (veina de veina...).
        La precarrega controlada es fa des del loop quan canvia la tesela central.
        """
        _ = tile_id
        _ = _tile_payload

    def _on_extension_ready(self, payload):
        """Actualitza dataset actiu quan arriba nova extensio profunda."""
        self._apply_star_payload(payload)
        if hasattr(self, "canvas"):
            self.canvas.update()

    def _on_scope_index_ready(self, payload):
        """Aplica l'index scope generat pel coordinador al renderer actiu."""
        if not isinstance(payload, dict):
            return
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        stars_renderer = getattr(
            getattr(canvas, "sky_renderer", None), "stars_renderer", None
        )
        if stars_renderer is None:
            return
        sorted_indices = payload.get("sorted_indices")
        offsets = payload.get("offsets")
        if sorted_indices is None or offsets is None:
            return

        ra_all = getattr(self, "np_ra", None)
        dec_all = getattr(self, "np_dec", None)
        if ra_all is None or dec_all is None:
            return
        expected_rows = int(payload.get("row_count", 0) or 0)
        if expected_rows > 0 and expected_rows != int(len(ra_all)):
            # Arriba un index stale d'un dataset anterior; el seguent request el renovara.
            return

        try:
            catalog_key = stars_renderer._catalog_array_key(ra_all, dec_all)
            stars_renderer.apply_scope_spatial_index_payload(
                catalog_key, sorted_indices, offsets
            )
            self._scope_index_target_key = catalog_key
            self._scope_index_loading = False
            self._scope_index_rewarm_requested = False
            self._scope_index_loaded_mag_cap = float(
                max(
                    float(getattr(self, "_scope_index_loaded_mag_cap", 0.0)),
                    float(payload.get("loaded_max_mag", 0.0) or 0.0),
                )
            )
            print(
                "[AstronomicalWidget] Scope index ready (coordinator): "
                f"rows={int(len(ra_all))} max_mag={self._scope_index_loaded_mag_cap:.2f}"
            )
            if bool(getattr(canvas, "scope_mode_enabled", lambda: False)()):
                canvas.update()
        except Exception as exc:
            try:
                print(f"[AstronomicalWidget] scope index apply error: {exc}")
            except Exception:
                pass

    def _on_star_data_error(self, message: str):
        """Log no fatal de errors del coordinador de dades d'estrelles."""
        try:
            print(f"[AstronomicalWidget] StarDataCoordinator error: {message}")
        except Exception:
            pass

    def _apply_star_payload(self, payload):
        """Assigna arrays de cataleg al widget des del payload del coordinador."""
        if not isinstance(payload, dict):
            return
        if np is None:
            return
        self.np_ra = np.asarray(payload.get("ra", np.empty(0, dtype=np.float32)), dtype=np.float32)
        self.np_dec = np.asarray(payload.get("dec", np.empty(0, dtype=np.float32)), dtype=np.float32)
        self.np_mag = np.asarray(payload.get("mag", np.empty(0, dtype=np.float32)), dtype=np.float32)
        self.np_r = np.asarray(payload.get("r", np.empty(0, dtype=np.float32)), dtype=np.float32)
        self.np_g = np.asarray(payload.get("g", np.empty(0, dtype=np.float32)), dtype=np.float32)
        self.np_b = np.asarray(payload.get("b", np.empty(0, dtype=np.float32)), dtype=np.float32)
        self.np_bp_rp = np.asarray(payload.get("bp_rp", np.empty(0, dtype=np.float32)), dtype=np.float32)

        if len(self.np_mag) > 0:
            try:
                self._catalog_max_mag = float(np.nanmax(self.np_mag))
            except Exception:
                self._catalog_max_mag = float(STAR_CATALOG_NAKED_EYE_MAX_MAG)
        self.refresh_light_pollution_catalog_range()
        self._catalog_mag_sorted = True
        self._catalog_loaded_subset_only = False
        self._stars_fallback_active = False
        self._stars_fallback_reason = ""
        self._stars_fallback_since = None
        self._stars_primary_since = None
        if hasattr(self, "lbl_stars_fallback"):
            try:
                self.lbl_stars_fallback.hide()
            except Exception:
                pass
        try:
            self._refresh_stars_status_indicator()
        except Exception:
            pass
