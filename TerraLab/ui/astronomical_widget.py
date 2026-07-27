"""Nova facade d'AstronomicalWidget centrada en UI + coordinadors.

The public class composes focused UI responsibility mixins.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from PyQt5.QtCore import QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.custom_widget_base import CustomWidgetBase
from TerraLab.runtime.clients import (
    ProcessCatalogClient,
    ProcessEphemerisClient,
    ProcessLatestClient,
    ProcessTerrainClient,
)
from TerraLab.data.constants import STAR_CATALOG_NAKED_EYE_MAX_MAG
from TerraLab.ui.widget_mixins import (
    WidgetBootstrapTerrainMixin,
    WidgetControlsTimeMixin,
    WidgetHorizonScopeMixin,
    WidgetLayersMixin,
    WidgetSurfaceDataMixin,
)
from TerraLab.ui.widget_init_helpers import astronomical_widget_init

GAIA_CATALOG_NOT_CONFIGURED = "catalog_not_configured"
GAIA_CATALOG_UNAVAILABLE = "catalog_unavailable"
GAIA_MANIFEST_MISSING = "manifest_missing"
GAIA_MANIFEST_INVALID = "manifest_invalid"
GAIA_CATALOG_AVAILABLE = "catalog_available"


class AstronomicalWidget(
    WidgetSurfaceDataMixin,
    WidgetLayersMixin,
    WidgetControlsTimeMixin,
    WidgetHorizonScopeMixin,
    WidgetBootstrapTerrainMixin,
    CustomWidgetBase,
):
    """Own the astronomical UI lifecycle and its application coordinators."""

    request_render_signal = pyqtSignal()
    request_trails_signal = pyqtSignal()

    def __init__(self, parent=None, **kwargs):
        # The canonical class owns initialization. Mixins contribute behavior
        # only, so construction cannot change when their MRO order changes.
        astronomical_widget_init(self, parent, **kwargs)

        self.star_data_coordinator = None
        self._last_scope_tile_request = ""
        self.gaia_catalog_status = GAIA_CATALOG_NOT_CONFIGURED
        self._gaia_manifest_path = self._resolve_gaia_manifest_path()
        self._gaia_manifest_last_mtime = 0.0
        self._gaia_general_tile_loaded_mtime = 0.0
        self._try_attach_star_data_coordinator(force_general_reload=True)

        app = QApplication.instance()
        runtime = getattr(app, "terralab_runtime", None)
        if runtime is None:
            raise RuntimeError("TerraLab runtime supervisor is not available")
        self.terrain_coordinator = ProcessTerrainClient(
            runtime, parent=self
        )
        self.terrain_coordinator.horizon_ready.connect(
            self._on_terrain_coordinator_ready
        )
        self.terrain_coordinator.horizon_progress.connect(
            self._on_terrain_coordinator_progress
        )
        self.terrain_coordinator.horizon_error.connect(
            self._on_terrain_coordinator_error
        )
        self.terrain_coordinator.horizon_preview_ready.connect(
            self.queue_horizon_preview
        )
        self.terrain_coordinator.surface_ready.connect(
            self._on_terrain_surface_ready
        )
        self.terrain_coordinator.bortle_estimate_ready.connect(
            self.on_horizon_bortle_estimate
        )
        self.ephemeris_coordinator = ProcessEphemerisClient(
            runtime, parent=self
        )
        self.ephemeris_coordinator.ephemeris_ready.connect(
            self._on_async_ephemeris_ready
        )
        self.search_index_client = ProcessLatestClient(
            runtime,
            "search_index",
            request_id="search-index",
            parent=self,
        )
        self.search_index_client.result_ready.connect(
            self._on_search_index_ready
        )
        self.search_resolve_client = ProcessLatestClient(
            runtime,
            "search_resolve",
            request_id="search-resolve",
            parent=self,
        )
        self.search_resolve_client.result_ready.connect(
            self._on_search_resolved
        )
        self.weather_compute_client = ProcessLatestClient(
            runtime,
            "weather_sample",
            request_id="weather",
            parent=self,
        )
        self.weather_compute_client.result_ready.connect(
            self._on_weather_sample_ready
        )
        self.scope_goto_client = ProcessLatestClient(
            runtime,
            "coordinate_convert",
            request_id="scope-goto",
            parent=self,
        )
        self.scope_goto_client.result_ready.connect(
            self._on_scope_goto_ready
        )
        self.scope_track_client = ProcessLatestClient(
            runtime,
            "coordinate_convert",
            request_id="scope-track",
            parent=self,
        )
        self.scope_track_client.result_ready.connect(
            self._on_scope_track_ready
        )
        self.scope_reverse_client = ProcessLatestClient(
            runtime,
            "coordinate_convert",
            request_id="scope-reverse",
            parent=self,
        )
        self.scope_reverse_client.result_ready.connect(
            self._on_scope_reverse_ready
        )
        self.circumpolar_client = ProcessLatestClient(
            runtime,
            "circumpolar_align",
            request_id="circumpolar-align",
            parent=self,
        )
        self.circumpolar_client.result_ready.connect(
            self._on_circumpolar_alignment_ready
        )
        self._gaia_attach_timer = None
        self._gaia_attach_timer = QTimer(self)
        self._gaia_attach_timer.setInterval(1500)
        self._gaia_attach_timer.timeout.connect(
            self._poll_incremental_gaia_updates
        )
        self._gaia_attach_timer.start()

    def _schedule_lifecycle_callback(
        self,
        delay_ms: int,
        callback: Callable[[], None],
    ) -> QTimer | None:
        """Schedule a callback that is cancelled when this widget closes."""

        if self._closing:
            return None
        timer = QTimer(self)
        timer.setSingleShot(True)
        self._lifecycle_timers.add(timer)

        def invoke() -> None:
            self._lifecycle_timers.discard(timer)
            timer.deleteLater()
            if not self._closing:
                callback()

        timer.timeout.connect(invoke)
        timer.start(max(0, int(delay_ms)))
        return timer

    def _on_terrain_coordinator_progress(self, state) -> None:
        """Show surface-raster progress without treating it as a DEM bake."""

        if not isinstance(state, dict):
            return
        if state.get("kind") != "surface":
            self.on_horizon_progress_state(state)
            return
        percent = max(0.0, min(100.0, float(state.get("percent", 0.0))))
        raw_phase = str(state.get("phase", "") or "")
        phase = raw_phase.rsplit(":", 1)[-1]
        now = time.perf_counter()
        previous_phase = str(
            getattr(self, "_surface_progress_ui_phase", "") or ""
        )
        previous_emit = float(
            getattr(self, "_surface_progress_ui_ts", 0.0) or 0.0
        )
        terminal = percent >= 99.9 or raw_phase == "completed"
        if (
            not terminal
            and raw_phase == previous_phase
            and now - previous_emit < 0.10
        ):
            return
        self._surface_progress_ui_ts = now
        self._surface_progress_ui_phase = raw_phase
        labels = {
            "queued": "preparant",
            "discovering-sources": "detectant fonts",
            "opening-geotiff": "obrint el GeoTIFF",
            "preparing": "preparant coordenades",
            "transforming-coordinates": "transformant coordenades",
            "profile-ready": "perfil preparat",
            "relief-ready": "relleu preparat",
            "subdividing-visual-grid": "refinant la malla visual",
            "sampling-visual-detail": "mostrejant detall visual",
            "registering-cache": "registrant la memòria cau",
            "cache-ready": "memòria cau preparada",
            "persistent-cache-ready": "memòria cau persistent preparada",
            "completed": "completat",
        }
        detail = labels.get(phase, "llegint blocs del mosaic")
        percent_text = f"{percent:.1f}".rstrip("0").rstrip(".")
        self.on_horizon_progress(
            f"Carregant cobertura del sòl: {percent_text}% · {detail}"
        )

    def _on_terrain_coordinator_error(self, message: str) -> None:
        text = str(message or "").strip()
        if text:
            self._active_horizon_job_id = None
            self.on_horizon_progress(f"Error horitzó: {text}")

    def _on_terrain_coordinator_ready(self, payload) -> None:
        """Publish a completed immutable terrain artifact to Render."""

        if isinstance(payload, dict) and payload.get("profile_path"):
            self.cancel_pending_horizon_preview()
            self._full_horizon_profile = dict(payload)
            self._remote_terrain_profile_path = str(
                payload["profile_path"]
            )
            self._remote_terrain_surface_path = ""
            self._active_horizon_job_id = None
            self.on_horizon_progress("")
            self._set_scene_load_stage("scene_ready")
            self.canvas.update()
            checkbox = getattr(self, "chk_surface_layer", None)
            if checkbox is not None and bool(checkbox.isChecked()):
                self.on_surface_layer_toggled(True)
            return
        self._on_terrain_coordinator_error(
            "Compute devolvió un artefacto de terreno incompatible"
        )

    def _on_terrain_surface_ready(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        profile_path = str(payload.get("profile_path", "") or "")
        if profile_path != str(
            getattr(self, "_remote_terrain_profile_path", "") or ""
        ):
            return
        self._remote_terrain_surface_path = str(
            payload.get("surface_path", "") or ""
        )
        self._effective_data_sources_payload = {
            "surface": {
                "source_id": str(payload.get("source_id", "") or ""),
                "status": str(payload.get("status", "") or ""),
            }
        }
        self.on_horizon_progress("")
        self.canvas.update()

    def _on_async_ephemeris_ready(self, _snapshot) -> None:
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        # Force the next paint to ingest the completed snapshot immediately.
        canvas._last_skyfield_update = 0
        canvas.update()

    def _resolve_gaia_manifest_path(self) -> Path | None:
        """Resolve Gaia only from the configured data library, never the CWD."""

        runtime_layout = self.runtime_layout
        configured = str(runtime_layout.get("data_gaia", "") or "").strip()
        if not configured:
            self.gaia_catalog_status = GAIA_CATALOG_NOT_CONFIGURED
            return None

        gaia_dir = Path(configured).expanduser()
        if not gaia_dir.is_absolute():
            library_root = Path(
                str(runtime_layout.get("root", "") or "")
            ).expanduser()
            if not library_root.is_absolute():
                self.gaia_catalog_status = GAIA_CATALOG_UNAVAILABLE
                return None
            gaia_dir = library_root / gaia_dir

        manifest_path = gaia_dir / "tile_manifest.json"
        if not gaia_dir.is_dir():
            self.gaia_catalog_status = GAIA_CATALOG_UNAVAILABLE
        elif not manifest_path.is_file() and not (
            gaia_dir / "tile_all.npz"
        ).is_file():
            self.gaia_catalog_status = GAIA_MANIFEST_MISSING
        else:
            self.gaia_catalog_status = GAIA_CATALOG_AVAILABLE
        return manifest_path

    def _connect_star_data_coordinator_signals(self) -> None:
        """Connecta senyals del coordinador de teseles amb el widget."""
        if self.star_data_coordinator is None:
            return
        self.star_data_coordinator.general_tile_ready.connect(
            self._on_general_tile_ready
        )
        self.star_data_coordinator.extension_ready.connect(
            self._on_extension_ready
        )
        self.star_data_coordinator.error_occurred.connect(
            self._on_star_data_error
        )

    def _try_attach_star_data_coordinator(
        self, *, force_general_reload: bool = False
    ) -> None:
        """Inicialitza o refresca coordinador Gaia quan hi ha manifest disponible."""
        manifest_path = self._gaia_manifest_path
        if manifest_path is None:
            self.gaia_catalog_status = GAIA_CATALOG_NOT_CONFIGURED
            return
        fallback_tile = manifest_path.parent / "tile_all.npz"
        source_path = (
            manifest_path
            if manifest_path.is_file()
            else fallback_tile
        )
        try:
            manifest_mtime = float(source_path.stat().st_mtime)
        except FileNotFoundError:
            self.gaia_catalog_status = GAIA_MANIFEST_MISSING
            return
        except PermissionError as exc:
            self.gaia_catalog_status = GAIA_CATALOG_UNAVAILABLE
            self._on_star_data_error(f"Gaia manifest permission error: {exc}")
            return
        except OSError as exc:
            self.gaia_catalog_status = GAIA_CATALOG_UNAVAILABLE
            self._on_star_data_error(f"Gaia manifest unavailable: {exc}")
            return

        manifest_changed = (
            manifest_mtime > float(self._gaia_manifest_last_mtime) + 1e-6
        )
        if self.star_data_coordinator is not None and manifest_changed:
            self.star_data_coordinator.shutdown()
            self.star_data_coordinator = None

        if self.star_data_coordinator is None:
            try:
                app = QApplication.instance()
                runtime = getattr(app, "terralab_runtime", None)
                if runtime is None:
                    raise RuntimeError(
                        "TerraLab runtime supervisor is not available"
                    )
                coordinator = ProcessCatalogClient(
                    runtime,
                    manifest_path,
                    parent=self,
                )
            except FileNotFoundError:
                self.gaia_catalog_status = GAIA_MANIFEST_MISSING
                return
            except PermissionError as exc:
                self.gaia_catalog_status = GAIA_CATALOG_UNAVAILABLE
                self._on_star_data_error(
                    f"Gaia manifest permission error: {exc}"
                )
                return
            except json.JSONDecodeError as exc:
                self.gaia_catalog_status = GAIA_MANIFEST_INVALID
                self._on_star_data_error(f"Gaia manifest is invalid JSON: {exc}")
                return
            except (TypeError, ValueError) as exc:
                self.gaia_catalog_status = GAIA_MANIFEST_INVALID
                self._on_star_data_error(f"Gaia manifest is invalid: {exc}")
                return
            except OSError as exc:
                self.gaia_catalog_status = GAIA_CATALOG_UNAVAILABLE
                self._on_star_data_error(f"Gaia catalog unavailable: {exc}")
                return
            self.star_data_coordinator = coordinator
            self._connect_star_data_coordinator_signals()
            force_general_reload = True
        self.gaia_catalog_status = GAIA_CATALOG_AVAILABLE
        if (
            force_general_reload
            or manifest_changed
        ):
            self._gaia_manifest_last_mtime = float(manifest_mtime)
            self.star_data_coordinator.load_general_tile()

    def _poll_incremental_gaia_updates(self) -> None:
        """Polling lleuger per activar carrega incremental de Gaia."""
        self._try_attach_star_data_coordinator(force_general_reload=False)
        self._maybe_reload_general_tile_incrementally()

    def _gaia_state_paths(self) -> list[Path]:
        """Retorna rutes candidates de fitxer d'estat Gaia (nou + legacy)."""
        runtime_layout = self.runtime_layout
        root_dir = Path(runtime_layout.get("root", Path.home())).resolve()
        gaia_dir = Path(
            str(runtime_layout.get("data_gaia", "") or "")
        ).expanduser()
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
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            self._on_star_data_error(
                f"Gaia state file permission error: {exc}"
            )
            return None
        except json.JSONDecodeError as exc:
            self._on_star_data_error(f"Gaia state file is invalid JSON: {exc}")
            return None
        except OSError as exc:
            self._on_star_data_error(f"Gaia state file unavailable: {exc}")
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
        except (FileNotFoundError, PermissionError, OSError):
            return
        if tile_mtime <= float(self._gaia_general_tile_loaded_mtime) + 1e-6:
            return

        self._gaia_general_tile_loaded_mtime = float(tile_mtime)
        try:
            self._reload_star_catalog_async()
        except Exception:
            log_suppressed_exception(__name__, "AstronomicalWidget._maybe_reload_general_tile_incrementally")
        self._try_attach_star_data_coordinator(force_general_reload=True)
        try:
            self._set_gaia_extension_status_label(
                "Cataleg general Gaia (<8) carregat de forma incremental",
                keep_seconds=8.0,
            )
        except Exception:
            log_suppressed_exception(__name__, "AstronomicalWidget._maybe_reload_general_tile_incrementally")

    def _ensure_asset_before_enable(self, checkbox, checked: bool, asset_id: str) -> bool:
        """Evita onboarding forçat de Gaia si la descarrega ja està en curs."""
        if bool(checked) and str(asset_id or "") == "gaia_catalog":
            pending_state = self._load_pending_gaia_state()
            if self._gaia_state_is_pending(pending_state):
                try:
                    self._persist_visibility_state("estrelles", True)
                except Exception:
                    log_suppressed_exception(__name__, "AstronomicalWidget._ensure_asset_before_enable")
                try:
                    from TerraLab.ui.widget_misc_helpers import (
                        widget_refresh_gaia_download_feedback,
                    )

                    widget_refresh_gaia_download_feedback(self)
                except Exception:
                    log_suppressed_exception(__name__, "AstronomicalWidget._ensure_asset_before_enable")
                return True
        return super()._ensure_asset_before_enable(checkbox, checked, asset_id)

    def closeEvent(self, event):  # type: ignore[reportIncompatibleMethodOverride]  # PyQt5 stub calls this parameter a0.
        """Release application coordinators before closing the widget."""
        self._closing = True
        for timer in tuple(self._lifecycle_timers):
            timer.stop()
            timer.deleteLater()
        self._lifecycle_timers.clear()
        for timer in (
            self._gaia_attach_timer,
            self.timer,
            self.bake_debounce_timer,
            self.terrain_depth_debounce_timer,
            self.terrain_ray_precision_debounce_timer,
            self._gaia_extension_status_hide_timer,
        ):
            if timer is not None:
                timer.stop()

        self.canvas.shutdown()
        if self.star_data_coordinator is not None:
            self.star_data_coordinator.shutdown()
        self.terrain_coordinator.shutdown()
        self.ephemeris_coordinator.shutdown()
        self.search_index_client.shutdown()
        self.search_resolve_client.shutdown()
        self.weather_compute_client.shutdown()
        self.scope_goto_client.shutdown()
        self.scope_track_client.shutdown()
        self.scope_reverse_client.shutdown()
        self.circumpolar_client.shutdown()
        return super().closeEvent(event)

    def update_loop(self):
        """Advance time, coordinators and rendering exactly once per frame."""
        if bool(getattr(self, "_updates_paused", False)):
            return
        try:
            timer_interval_ms = 250.0
            if hasattr(self, "timer") and callable(getattr(self.timer, "interval", None)):
                timer_interval_ms = float(self.timer.interval())

            if self.use_real_time:
                now = datetime.now()
                self.manual_year = now.year
                self.manual_day = (now - datetime(now.year, 1, 1)).days
                if hasattr(self, "lbl_date"):
                    self.lbl_date.setText(self.format_date(self.manual_day))
                if hasattr(self, "time_bar"):
                    self.time_bar.update_params(
                        self.latitude,
                        self.longitude,
                        self.manual_day,
                    )
                    self.time_bar.set_time(
                        now.hour + now.minute / 60.0 + now.second / 3600.0
                    )
            else:
                self.manual_hour = (
                    self.manual_hour + timer_interval_ms / 3_600_000.0
                ) % 24.0
                if hasattr(self, "time_bar"):
                    self.time_bar.set_time(self.manual_hour)
            self._request_weather_sample()

            canvas = getattr(self, "canvas", None)
            self.ephemeris_coordinator.configure_observer(
                latitude=float(self.latitude),
                longitude=float(self.longitude),
            )
            ephemeris_hour_utc = float(self.manual_hour)
            ephemeris_day_utc = int(self.manual_day)
            ephemeris_year_utc = int(self.manual_year)
            if canvas is not None and callable(
                getattr(canvas, "_get_current_utc_context", None)
            ):
                (
                    ephemeris_hour_utc,
                    ephemeris_day_utc,
                    ephemeris_year_utc,
                    _,
                ) = canvas._get_current_utc_context()
            self.ephemeris_coordinator.request_snapshot(
                year_utc=int(ephemeris_year_utc),
                day_of_year_utc=int(ephemeris_day_utc),
                ut_hour=float(ephemeris_hour_utc),
            )
            track_scope = getattr(
                self, "request_scope_tracking_update", None
            )
            if callable(track_scope):
                track_scope()

            if (
                self.star_data_coordinator is not None
                and getattr(self, "canvas", None) is not None
                and bool(getattr(self.canvas, "scope_mode_enabled", lambda: False)())
            ):
                scope_center = getattr(getattr(self.canvas, "scope_controller", None), "center", None)
                if scope_center is not None:
                    scope_controller = self.canvas.scope_controller
                    radius = float(max(scope_controller.current_fov()))
                    query_bucket = max(0.01, radius / 20.0)
                    signature = (
                        round(float(scope_center[0]) / query_bucket),
                        round(float(scope_center[1]) / query_bucket),
                        round(radius, 3),
                        int(ephemeris_day_utc),
                        round(float(ephemeris_hour_utc) * 60.0),
                    )
                    if signature != self._last_scope_tile_request:
                        self._last_scope_tile_request = signature
                        self.star_data_coordinator.request_scope_region(
                            alt=float(scope_center[0]),
                            az=float(scope_center[1]),
                            radius_deg=radius,
                            mag_limit=22.0,
                            ut_hour=float(ephemeris_hour_utc),
                            day_of_year_utc=int(ephemeris_day_utc),
                            year_utc=int(ephemeris_year_utc),
                            latitude=float(self.latitude),
                            longitude=float(self.longitude),
                        )
        except Exception as exc:
            self._on_star_data_error(f"frame update failed: {exc}")
        self.canvas.update()

    def _on_general_tile_ready(self, payload):
        """Actualitza arrays principals quan arriba la tesela general."""
        if isinstance(payload, dict) and isinstance(
            payload.get("artifact"), dict
        ):
            self._render_catalog_artifact = dict(payload["artifact"])
            self._scope_catalog_loaded_max_mag = float(
                payload.get(
                    "loaded_max_mag",
                    STAR_CATALOG_NAKED_EYE_MAX_MAG,
                )
            )
            self._catalog_max_mag = self._scope_catalog_loaded_max_mag
            self._catalog_mag_sorted = True
            if getattr(self, "scene_load_stage", "boot") in {
                "boot",
                "base_sky",
            }:
                self._set_scene_load_stage("stars_ready")
            if hasattr(self, "canvas"):
                self.canvas.update()
            if bool(getattr(self, "_deferred_controls_ready", False)):
                self.build_search_index()
            return
        self._on_star_data_error(
            "Compute returned an invalid Gaia artifact descriptor"
        )

    def _on_extension_ready(self, payload):
        """Actualitza dataset actiu quan arriba nova extensio profunda."""
        if isinstance(payload, dict) and isinstance(
            payload.get("artifact"), dict
        ):
            self._render_catalog_artifact = dict(payload["artifact"])
            self._scope_catalog_loaded_max_mag = float(
                payload.get(
                    "loaded_max_mag",
                    self._scope_catalog_loaded_max_mag,
                )
            )
            # Without updating this cap the new deep mmap was attached, but
            # Render still discarded every star fainter than the naked-eye
            # general tile. That made telescope Goto look entirely black.
            self._catalog_max_mag = self._scope_catalog_loaded_max_mag
            self._catalog_mag_sorted = True
            if hasattr(self, "canvas"):
                self.canvas.update()
            return
        self._on_star_data_error(
            "Compute returned an invalid deep Gaia artifact descriptor"
        )

    def _on_star_data_error(self, message: str):
        """Log no fatal de errors del coordinador de dades d'estrelles."""
        try:
            print(f"[AstronomicalWidget] StarDataCoordinator error: {message}")
        except Exception:
            log_suppressed_exception(__name__, "AstronomicalWidget._on_star_data_error")
