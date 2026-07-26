"""Widget bootstrap and terrain-job lifecycle."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QThread, Qt
from PyQt5.QtWidgets import QCheckBox, QDialog, QFrame, QLabel, QVBoxLayout

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.perf_events import append_perf_event
from TerraLab.common.utils import getTraduction, get_base_dir, get_config_value, set_config_value
from TerraLab.data.layer_manager import LayerId, LayerState
from TerraLab.ui.onboarding_dialogs import AssetOnboardingDialog, WelcomeOnboardingDialog
from TerraLab.ui.widget_bootstrap_helpers import (
    widget_start_async_bootstrap,
    widget_start_scope_full_preload_async,
)
from TerraLab.ui.widget_misc_helpers import (
    widget_apply_scope_preloaded_spatial_index,
    widget_maybe_resume_pending_gaia_download,
    widget_on_scope_preload_ready,
    widget_reload_star_catalog_async,
)
from TerraLab.ui.workers.catalog_loader import CatalogLoaderWorker


class WidgetBootstrapTerrainMixin:
    def _set_scene_load_stage(self, stage: str):
        valid = {"boot", "base_sky", "stars_ready", "horizon_preview", "scene_ready"}
        if stage not in valid:
            return
        if getattr(self, "scene_load_stage", None) == stage:
            return
        self.scene_load_stage = stage
        append_perf_event("scene_stage", stage=str(stage), delta_ms_boot=self._boot_delta_ms())
        if hasattr(self, "canvas"):
            self.canvas.update()
        if stage == "scene_ready":
            self._start_scope_full_preload_async(reason="scene_ready")
            self._ensure_scope_catalog_loaded()

    def _boot_delta_ms(self) -> int:
        return int((time.perf_counter() - float(getattr(self, "_perf_boot_t0_mono", time.perf_counter()))) * 1000.0)

    def _scope_preload_cache_dir(self) -> str:
        root = Path(self.runtime_layout.get("root", get_base_dir()))
        path = root / "cache" / "scope"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _scope_preload_should_wait(self) -> bool:
        return bool(
            self.scope_preload_mode == "startup_full"
            and bool(getattr(self, "scope_requires_full_catalog", True))
            and str(getattr(self, "scope_activation_policy", "wait_until_ready")) == "wait_until_ready"
        )

    def _scope_set_data_state(self, new_state: str, *, reason: str = "") -> None:
        state = str(new_state or "").strip().lower()
        if state not in {"ready_deep", "loading_deep", "error_deep"}:
            return
        prev = str(getattr(self, "_scope_data_state", "ready_deep") or "ready_deep")
        if prev == state:
            return
        self._scope_data_state = state
        scope_active = bool(getattr(getattr(self, "canvas", None), "scope_mode_enabled", lambda: False)())
        pending_scope = bool(getattr(self, "_scope_preload_pending_activation", False))
        emit_state_events = bool(scope_active or pending_scope)
        if emit_state_events and state in {"loading_deep", "error_deep"} and prev == "ready_deep":
            append_perf_event(
                "scope_fallback_on",
                state=state,
                reason=str(reason or ""),
                delta_ms_boot=self._boot_delta_ms(),
            )
        elif emit_state_events and prev in {"loading_deep", "error_deep"} and state == "ready_deep":
            append_perf_event(
                "scope_fallback_off",
                reason=str(reason or ""),
                delta_ms_boot=self._boot_delta_ms(),
            )
        if hasattr(self, "canvas"):
            try:
                self.canvas._cached_star_image = None
                self.canvas._cached_trail_image = None
            except Exception:
                log_suppressed_exception(__name__, "WidgetBootstrapTerrainMixin._scope_set_data_state")
            self.canvas.update()

    def _refresh_scope_data_state(self, *, reason: str = "") -> None:
        if bool(getattr(self, "_scope_preload_failed", False)):
            self._scope_set_data_state("error_deep", reason=reason or "preload_error")
            return
        wait_full_ready = bool(
            self._scope_preload_should_wait()
            and (not bool(getattr(self, "_scope_preload_ready", False)))
        )
        subset_only = bool(getattr(self, "_catalog_loaded_subset_only", False))
        if subset_only or wait_full_ready:
            self._scope_set_data_state("loading_deep", reason=reason or "loading")
            return
        self._scope_set_data_state("ready_deep", reason=reason or "ready")

    def _scope_preload_status(self, message: str, *, keep_seconds: float = 0.0):
        self._set_gaia_extension_status_label(f"[Scope preload] {str(message)}", keep_seconds=keep_seconds)

    def _apply_scope_preloaded_spatial_index(self) -> bool:
        return widget_apply_scope_preloaded_spatial_index(self)

    def _finalize_scope_preload_worker_refs(self):
        thread = getattr(self, "_scope_preload_thread", None)
        if thread is not None:
            try:
                if thread.isRunning():
                    return
            except Exception:
                log_suppressed_exception(__name__, "WidgetBootstrapTerrainMixin._finalize_scope_preload_worker_refs")
        self._scope_preload_thread = None
        self._scope_preload_worker = None

    def _cleanup_scope_preload_worker(self):
        thread = getattr(self, "_scope_preload_thread", None)
        if thread is None:
            self._finalize_scope_preload_worker_refs()
            return
        try:
            thread.finished.connect(self._finalize_scope_preload_worker_refs)
        except Exception:
            log_suppressed_exception(__name__, "WidgetBootstrapTerrainMixin._cleanup_scope_preload_worker")
        try:
            if thread.isRunning():
                thread.quit()
                return
        except Exception:
            log_suppressed_exception(__name__, "WidgetBootstrapTerrainMixin._cleanup_scope_preload_worker")
        self._finalize_scope_preload_worker_refs()

    def _on_scope_preload_progress(self, payload):
        if not isinstance(payload, dict):
            return
        pct = max(0.0, min(100.0, float(payload.get("percent", 0.0))))
        msg = str(payload.get("message", "scope preload")).strip() or "scope preload"
        stage = str(payload.get("stage", "")).strip()
        self._scope_preload_status(f"{msg} ({int(round(pct))}%)")
        last_pct = float(getattr(self, "_scope_preload_last_progress_pct", -1.0))
        if abs(pct - last_pct) >= 1.0 or pct in (0.0, 100.0):
            self._scope_preload_last_progress_pct = pct
            append_perf_event(
                "scope_preload_progress",
                percent=float(pct),
                stage=stage,
                message=msg,
                delta_ms_boot=self._boot_delta_ms(),
            )

    def _on_scope_preload_ready(self, payload):
        return widget_on_scope_preload_ready(self, payload)

    def _on_scope_preload_error(self, message: str):
        self._scope_preload_started = False
        self._scope_preload_in_progress = False
        self._scope_preload_failed = True
        self._scope_preload_ready = False
        self._scope_preload_pending_activation = False
        self._scope_preload_sorted_indices = None
        self._scope_preload_offsets = None
        self._scope_preload_indices_path = ""
        self._scope_preload_offsets_path = ""
        msg = str(message or "unknown preload error")
        print(f"[AstroWidget] Scope preload error: {msg}")
        self._scope_preload_status(f"error: {msg}", keep_seconds=20.0)
        append_perf_event("scope_preload_error", message=msg, delta_ms_boot=self._boot_delta_ms())
        self._scope_preload_rows = 0
        self._refresh_scope_data_state(reason="preload_error")
        self._cleanup_scope_preload_worker()

    def _start_scope_full_preload_async(self, reason: str = "runtime", force_rebuild: bool = False):
        return widget_start_scope_full_preload_async(self, reason, force_rebuild)

    def _create_startup_placeholder(self):
        if hasattr(self, "_startup_placeholder"):
            return
        self._startup_placeholder = QFrame(self)
        self._startup_placeholder.setObjectName("startupPlaceholder")
        self._startup_placeholder.setStyleSheet(
            "QFrame#startupPlaceholder { background-color: #000000; }"
        )
        layout = QVBoxLayout(self._startup_placeholder)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        title = QLabel("TerraLab", self._startup_placeholder)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color: #eef4ff; font-size: 28px; font-weight: bold; background: transparent;")
        subtitle = QLabel("Initializing sky...", self._startup_placeholder)
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: #8ea3c2; font-size: 14px; background: transparent;")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(2)
        self._startup_placeholder.show()
        self._startup_placeholder.raise_()

    def _position_startup_placeholder(self):
        if not hasattr(self, "_startup_placeholder"):
            return
        self._startup_placeholder.setGeometry(self.rect())
        self._startup_placeholder.raise_()

    def _hide_startup_placeholder(self):
        if getattr(self, "_startup_placeholder_visible", False) and hasattr(self, "_startup_placeholder"):
            self._startup_placeholder_visible = False
            self._startup_placeholder.hide()

    def _schedule_deferred_controls_build(self):
        if getattr(self, "_deferred_controls_ready", False):
            return
        if getattr(self, "_deferred_controls_build_scheduled", False):
            return
        self._deferred_controls_build_scheduled = True
        self._schedule_lifecycle_callback(
            60, self._build_deferred_controls_ui
        )

    def _on_canvas_first_useful_paint(self):
        self._hide_startup_placeholder()
        self._start_async_bootstrap()
        self._schedule_deferred_controls_build()

    def _gaia_tap_state_path(self) -> Path:
        try:
            root = Path(self.runtime_layout.get("root", Path.home())).resolve()
        except Exception:
            root = Path.home()
        return root / "logs" / "gaia_tap_state.json"

    def _load_pending_gaia_state(self) -> Optional[dict]:
        state_path = self._gaia_tap_state_path()
        if not state_path.exists():
            return None
        try:
            with state_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                return None
            status = str(payload.get("status", "")).strip().lower()
            phase = str(payload.get("phase", "")).strip().lower()
            if status in {"done", "completed", "success"} or phase in {"done", "completed"}:
                return None
            return payload
        except Exception:
            return None

    def _on_gaia_background_dialog_finished(self, _code: int) -> None:
        dlg = getattr(self, "_gaia_background_dialog", None)
        if dlg is not None and bool(getattr(dlg, "completed", False)):
            self._sync_runtime_asset_config(updated_asset_id="gaia_catalog")
        self._gaia_background_dialog = None

    def _maybe_resume_pending_gaia_download(self) -> None:
        return widget_maybe_resume_pending_gaia_download(self)

    def _open_quick_welcome(self):
        dlg = WelcomeOnboardingDialog(self.asset_manager, self, mandatory=False)
        if dlg.exec_() == QDialog.Accepted:
            self._sync_runtime_asset_config(updated_asset_id=None)

    def _open_asset_onboarding(self, asset_id: str) -> bool:
        dlg = AssetOnboardingDialog(self.asset_manager, asset_id, self)
        ok = dlg.exec_() == QDialog.Accepted and bool(getattr(dlg, "completed", False))
        if ok:
            # Keep runtime knobs in sync when onboarding modifies config paths/settings.
            self._sync_runtime_asset_config(updated_asset_id=str(asset_id))
        return bool(ok)

    def _sync_runtime_asset_config(self, updated_asset_id: Optional[str] = None):
        self.milkyway_overlay_texture_path = str(
            get_config_value("milkyway_overlay_texture_path", self.milkyway_overlay_texture_path)
        )
        self.dust_map_path = str(get_config_value("dust_map_path", self.dust_map_path))
        self.light_pollution_enabled = bool(
            get_config_value("light_pollution_enabled", self.light_pollution_enabled)
        )
        if hasattr(self, "weather"):
            self.weather.set_remote_user_agent(self.asset_manager.get_user_agent())
        if hasattr(self, "canvas") and hasattr(self.canvas, "weather"):
            self.canvas.weather.set_remote_user_agent(self.asset_manager.get_user_agent())
        self._ngc_search_entries_cache = None
        if hasattr(self, "chk_deep_space") and bool(self.chk_deep_space.isChecked()):
            self.build_search_index()
        self.terrain_coordinator.reload_config()
        if str(updated_asset_id or "") == "gaia_catalog" and self.asset_manager.asset_ready("gaia_catalog"):
            self._reload_star_catalog_async()
        self._sync_surface_mode_control()

    def _reload_star_catalog_async(self):
        return widget_reload_star_catalog_async(self)

    def _ensure_asset_before_enable(self, checkbox: QCheckBox, checked: bool, asset_id: str) -> bool:
        checked = bool(checked)
        if not checked:
            return False
        if self.asset_manager.asset_ready(asset_id):
            return True
        ok = self._open_asset_onboarding(asset_id)
        if ok:
            return True
        checkbox.blockSignals(True)
        checkbox.setChecked(False)
        checkbox.blockSignals(False)
        return False

    def _missing_layer_target(self, layer_id):
        """Return the library row to guide to, or ``None`` when data is ready."""
        manager = getattr(self, "layer_manager", None)
        if manager is None:
            return None
        try:
            normalized = (
                layer_id
                if isinstance(layer_id, LayerId)
                else LayerId(str(layer_id))
            )
        except ValueError:
            return None
        if normalized in {
            LayerId.EARTH_ORTHOPHOTO,
            LayerId.EARTH_SURFACE_CATEGORICAL,
            LayerId.EARTH_SURFACE_RGB,
        }:
            surface_layers = (
                LayerId.EARTH_ORTHOPHOTO,
                LayerId.EARTH_SURFACE_CATEGORICAL,
                LayerId.EARTH_SURFACE_RGB,
            )
            ready = False
            for candidate in surface_layers:
                try:
                    ready = (
                        manager.status(candidate).state
                        is LayerState.READY
                    )
                except (KeyError, ValueError):
                    # Compatibility with integrations exposing the two
                    # pre-v3 surface rows only.
                    continue
                if ready:
                    break
            if ready:
                return None
            return normalized
        return (
            None
            if manager.status(normalized).state is LayerState.READY
            else normalized
        )

    def _guide_missing_layer(self, checked: bool, layer_id) -> bool:
        """Open the layer library at missing data after a user checkbox click."""
        if not bool(checked):
            return False
        target = self._missing_layer_target(layer_id)
        if target is None:
            return False
        self._schedule_lifecycle_callback(
            0,
            lambda target_layer=target: self.open_data_layers_dialog(
                focus_layer_id=target_layer
            ),
        )
        return True

    def _persist_visibility_state(self, key: str, checked: bool) -> None:
        set_config_value(f"ui.visibility.{key}", bool(checked))
        stable_ids = {
            "estrelles": LayerId.SKY_STARS,
            "espai_profund": LayerId.SKY_NGC,
            "via_lactia": LayerId.SKY_MILKY_WAY,
            "pols_planck": LayerId.SKY_PLANCK_DUST,
            "sistema_solar": LayerId.SKY_SOLAR_SYSTEM,
            "clima": LayerId.SKY_WEATHER,
            "topografia": LayerId.EARTH_TERRAIN,
            "superficie": LayerId.EARTH_SURFACE,
            "contaminacio_luminica": LayerId.EARTH_LIGHT_POLLUTION,
        }
        layer_id = stable_ids.get(str(key))
        manager = getattr(self, "layer_manager", None)
        if layer_id is not None and manager is not None:
            manager.set_visible(layer_id, bool(checked))

    def _load_visibility_state(self, key: str, default: bool) -> bool:
        return bool(get_config_value(f"ui.visibility.{key}", default))

    def _validate_checked_assets_startup(self):
        """Keep persisted visibility and expose fallback state without prompts."""
        self._refresh_climate_status_indicator()
        self._refresh_stars_status_indicator()
        self._refresh_milkyway_status_indicator()
        if hasattr(self, "canvas"):
            self.canvas.update()

    def _start_async_bootstrap(self):
        return widget_start_async_bootstrap(self)

    def _start_catalog_loader_async(self, reason: str = "runtime"):
        if bool(getattr(self, "_catalog_bootstrap_started", False)):
            return
        np_ra = getattr(self, "np_ra", None)
        np_dec = getattr(self, "np_dec", None)
        np_mag = getattr(self, "np_mag", None)
        np_r = getattr(self, "np_r", None)
        np_g = getattr(self, "np_g", None)
        np_b = getattr(self, "np_b", None)
        np_bp_rp = getattr(self, "np_bp_rp", None)
        subset_only = bool(getattr(self, "_catalog_loaded_subset_only", False))
        try:
            existing_rows = int(len(np_ra)) if np_ra is not None else 0
        except Exception:
            existing_rows = 0
        has_consistent_arrays = (
            np_ra is not None
            and np_dec is not None
            and np_mag is not None
            and np_r is not None
            and np_g is not None
            and np_b is not None
            and np_bp_rp is not None
            and existing_rows > 0
            and existing_rows == int(len(np_dec)) == int(len(np_mag))
        )
        # With StarDataCoordinator refactor active, keep current in-memory catalog
        # and skip legacy worker source probing.
        if has_consistent_arrays and (not subset_only) and existing_rows >= 5000:
            self._catalog_bootstrap_started = True
            print(
                "[AstroWidget] Star catalog loader skipped: using in-memory "
                f"catalog rows={existing_rows} (reason={reason})"
            )
            try:
                existing_named = getattr(self, "celestial_objects", [])
                self._on_catalog_ready(
                    existing_named,
                    np_ra,
                    np_dec,
                    np_mag,
                    np_r,
                    np_g,
                    np_b,
                    np_bp_rp,
                )
            except Exception as e:
                print(f"[AstroWidget] In-memory catalog finalize failed: {e}")
            return
        self._catalog_bootstrap_started = True
        self._catalog_thread = QThread()
        self._catalog_worker = CatalogLoaderWorker()
        self._catalog_worker.moveToThread(self._catalog_thread)
        self._catalog_worker.catalog_ready.connect(self._on_catalog_ready)
        self._catalog_thread.started.connect(lambda: self._catalog_worker.load(self._stars_catalog_dir))
        self._catalog_thread.start()
        try:
            self._catalog_thread.setPriority(QThread.LowPriority)
        except Exception:
            log_suppressed_exception(__name__, "WidgetBootstrapTerrainMixin._start_catalog_loader_async")
        print(f"[AstroWidget] Star catalog loading in background... (reason={reason})")

    def _try_start_catalog_loader_deferred(self):
        if bool(getattr(self, "_catalog_bootstrap_started", False)):
            return
        stage = str(getattr(self, "scene_load_stage", "boot"))
        if stage == "scene_ready":
            self._start_catalog_loader_async(reason="defer_after_scene_ready")
            return
        elapsed = float(time.perf_counter() - float(getattr(self, "_catalog_defer_t0", 0.0)))
        # Hard safety valve if horizon never reaches scene_ready.
        if elapsed >= 300.0:
            self._start_catalog_loader_async(reason="defer_hard_timeout")
            return
        self._schedule_lifecycle_callback(
            15_000, self._try_start_catalog_loader_deferred
        )

    def _build_horizon_bake_job(self) -> dict:
        from TerraLab.common.utils import get_config_value
        from TerraLab.config import ConfigManager
        from TerraLab.terrain.visibility_range import TerrainRangeSettings
        import uuid
        try:
            n_bands = int(get_config_value("horizon_quality", 20))
        except Exception:
            n_bands = 20
        from TerraLab.terrain.ray_precision import normalize_ray_step_deg
        ray_step_deg = normalize_ray_step_deg(
            get_config_value("horizon_ray_step_deg", 0.5)
        )
        current_fov = 100.0 / max(0.001, float(getattr(self.canvas, "zoom_level", 1.0)))
        return {
            "job_id": uuid.uuid4().hex,
            "lat": float(self.latitude),
            "lon": float(self.longitude),
            "observer_offset": float(self.terrain_coordinator.observer_offset),
            "bands": max(1, int(n_bands)),
            "ray_step_deg": ray_step_deg,
            "view_azimuth": float(getattr(self.canvas, "azimuth_offset", 180.0)) % 360.0,
            "view_fov_deg": float(current_fov),
            "view_elevation": float(getattr(self.canvas, "elevation_angle", 0.0)),
            "viewport_height_px": max(1, int(self.canvas.height())),
            "view_zoom_level": max(
                0.001, float(getattr(self.canvas, "zoom_level", 1.0))
            ),
            "sampling_settings": ConfigManager()
            .get_terrain_sampling_settings()
            .to_dict(),
            "terrain_performance_logging_enabled": ConfigManager()
            .get_terrain_render_settings()
            .terrain_performance_logging_enabled,
            "range_settings": TerrainRangeSettings.from_mapping(
                get_config_value("terrain_visibility_range", {})
            ).to_dict(),
        }

    def _begin_horizon_bake(self):
        from TerraLab.terrain.ray_precision import ray_count

        self.terrain_coordinator.abort_current_job()
        self._active_horizon_job_id = None
        # Keep the last covered profile visible until a wider bake yields previews.
        target_stage = "stars_ready" if hasattr(self, "np_ra") else "base_sky"
        self._set_scene_load_stage(target_stage)
        job = self._build_horizon_bake_job()
        self._active_horizon_job_id = str(job["job_id"])
        self.on_horizon_progress_state(
            {
                "job_id": self._active_horizon_job_id,
                "phase": "prepare",
                "percent": 0.0,
                "current": 0,
                "total": ray_count(job["ray_step_deg"]),
            }
        )
        self.terrain_coordinator.request_bake(job)

    def on_horizon_progress_state(self, state):
        if not isinstance(state, dict):
            return
        job_id = str(state.get("job_id", "") or "")
        if job_id and getattr(self, "_active_horizon_job_id", None) and job_id != self._active_horizon_job_id:
            return
        percent = max(0.0, min(100.0, float(state.get("percent", 0.0))))
        phase = str(state.get("phase", "") or "")
        now_mono = time.perf_counter()
        last_ui_ts = float(getattr(self, "_horizon_progress_ui_ts", 0.0))
        min_interval = max(
            0.05,
            float(getattr(self, "_horizon_progress_min_interval_s", 0.10)),
        )
        is_final = percent >= 99.9 or phase in {"save", "done"}
        if not is_final and now_mono - last_ui_ts < min_interval:
            return
        self._horizon_progress_ui_ts = now_mono
        percent_text = f"{percent:.1f}"
        if percent_text.endswith(".0"):
            percent_text = percent_text[:-2]
        current = state.get("current")
        total = state.get("total")
        msg = getTraduction("Horizon.CalculatingHorizon", "Calculating horizon: {pct}%").format(pct=percent_text)
        if current is not None and total:
            msg = f"{msg} · {int(current)}/{int(total)}"
        self.on_horizon_progress(msg)

