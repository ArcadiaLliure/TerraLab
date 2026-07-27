"""Widget bootstrap and terrain-job lifecycle."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QCheckBox, QDialog, QFrame, QLabel, QVBoxLayout

from TerraLab.common.perf_events import append_perf_event
from TerraLab.common.utils import getTraduction, get_config_value, set_config_value
from TerraLab.data.layer_manager import LayerId, LayerState
from TerraLab.ui.onboarding_dialogs import AssetOnboardingDialog, WelcomeOnboardingDialog
from TerraLab.ui.widget_bootstrap_helpers import (
    widget_start_async_bootstrap,
)
from TerraLab.ui.widget_misc_helpers import (
    widget_maybe_resume_pending_gaia_download,
    widget_reload_star_catalog_async,
)


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

    def _boot_delta_ms(self) -> int:
        return int((time.perf_counter() - float(getattr(self, "_perf_boot_t0_mono", time.perf_counter()))) * 1000.0)

    def _create_startup_placeholder(self):
        if hasattr(self, "_startup_placeholder"):
            return
        self._startup_placeholder = QFrame(self)
        self._startup_placeholder.setObjectName("startupPlaceholder")
        self._startup_placeholder.setStyleSheet(
            "QFrame#startupPlaceholder { background-color: #02040a; }"
        )
        layout = QVBoxLayout(self._startup_placeholder)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        title = QLabel("TERRALAB", self._startup_placeholder)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            "color: #f3f5fa; font-size: 30px; font-weight: 600; "
            'font-family: "Segoe UI"; background: transparent;'
        )
        subtitle = QLabel("Preparant el cel…", self._startup_placeholder)
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet(
            "color: #d8b26a; font-size: 10px; "
            'font-family: "Consolas"; background: transparent;'
        )
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
        self._catalog_bootstrap_started = True
        coordinator = getattr(self, "star_data_coordinator", None)
        if coordinator is not None:
            coordinator.load_general_tile()
        print(
            "[AstroWidget] Catalog request delegated to compute process "
            f"(reason={reason})."
        )

    def _build_horizon_bake_job(self) -> dict:
        from TerraLab.common.utils import get_config_value
        from TerraLab.config import ConfigManager
        from TerraLab.data.visibility_range import TerrainRangeSettings
        import uuid
        try:
            n_bands = int(get_config_value("horizon_quality", 20))
        except Exception:
            n_bands = 20
        from TerraLab.data.ray_precision import normalize_ray_step_deg
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
        from TerraLab.data.ray_precision import ray_count

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

