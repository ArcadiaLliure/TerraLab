"""AstronomicalWidget misc helpers extracted from sky_widget_impl."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from TerraLab.common.deprecation_registry import (
    emit_deprecation_warning,
    register_deprecated_method,
)

register_deprecated_method(
    entry_id="TerraLab.ui.widget_misc_helpers.widget_reload_star_catalog_async",
    module_path="TerraLab.ui.widget_misc_helpers",
    class_name=None,
    method_name="widget_reload_star_catalog_async",
    replacement="TerraLab.data.star_data_coordinator.StarDataCoordinator.load_general_tile",
    phase_introduced=6,
    notes="Recarrega legacy substituida per coordinador de tesela general",
)


def _load_gaia_state_from_json(path: Path) -> Optional[dict]:
    """Carrega un fitxer JSON d'estat Gaia i valida format minim."""
    state_path = Path(path).expanduser()
    if not state_path.exists():
        return None
    try:
        with state_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    payload["_state_path"] = str(state_path)
    return payload


def _is_gaia_state_pending(state: Optional[dict]) -> bool:
    """Indica si un estat Gaia representa una descarrega pendent."""
    if not isinstance(state, dict):
        return False
    status = str(state.get("status", "")).strip().lower()
    phase = str(state.get("phase", "")).strip().lower()
    done_tokens = {"done", "completed", "success"}
    return status not in done_tokens and phase not in done_tokens


def _gaia_state_progress_percent(state: Optional[dict]) -> float:
    """Calcula percentatge de progres per estat legacy o per teseles."""
    if not isinstance(state, dict):
        return 0.0
    try:
        if "progress_percent" in state:
            return float(
                max(
                    0.0,
                    min(
                        100.0,
                        float(state.get("progress_percent", 0.0) or 0.0),
                    ),
                )
            )
    except Exception:
        pass

    deep_tiles = state.get("deep_tiles")
    if not isinstance(deep_tiles, dict):
        return 0.0

    try:
        tile_size_deg = float(state.get("tile_size_deg", 5.0) or 5.0)
    except Exception:
        tile_size_deg = 5.0
    tile_size_deg = max(0.1, float(tile_size_deg))
    total_tiles = int((360.0 / tile_size_deg) * (180.0 / tile_size_deg))
    done_tiles = sum(
        1
        for item in deep_tiles.values()
        if isinstance(item, dict) and bool(item.get("done", False))
    )
    progress = 10.0 + 85.0 * (float(done_tiles) / float(max(1, total_tiles)))
    if bool(state.get("general_tile_done", False)):
        progress = max(progress, 9.0)
    if str(state.get("status", "")).strip().lower() in {
        "done",
        "completed",
        "success",
    }:
        progress = 100.0
    return float(max(0.0, min(100.0, progress)))


def _gaia_state_target_mag(state: Optional[dict]) -> float:
    """Extreu magnitud objectiu des d'estat legacy o estat per teseles."""
    if not isinstance(state, dict):
        return 0.0
    for key_name in ("mag_limit", "target_mag"):
        try:
            value = float(state.get(key_name, 0.0) or 0.0)
            if value > 0.0:
                return value
        except Exception:
            continue
    return 0.0


def _candidate_gaia_state_paths(widget) -> list[Path]:
    """Construeix rutes candidates d'estat Gaia (nou + legacy)."""
    runtime_layout = getattr(widget, "runtime_layout", {}) or {}
    try:
        root_dir = Path(runtime_layout.get("root", Path.home())).resolve()
    except Exception:
        root_dir = Path.home()
    try:
        data_gaia_dir = Path(
            str(runtime_layout.get("data_gaia", ""))
        ).expanduser()
    except Exception:
        data_gaia_dir = Path()

    ordered_candidates = [
        root_dir / "logs" / "gaia_tiles_state.json",
        data_gaia_dir / "gaia_tiles_state.json",
        root_dir / "logs" / "gaia_tap_state.json",
    ]
    unique_paths: list[Path] = []
    seen_paths: set[str] = set()
    for candidate in ordered_candidates:
        key = str(candidate)
        if not key or key in seen_paths:
            continue
        seen_paths.add(key)
        unique_paths.append(candidate)
    return unique_paths


def _find_pending_gaia_state(widget) -> Optional[dict]:
    """Localitza el millor estat Gaia pendent per mostrar prompt de resume."""
    legacy_loader = getattr(widget, "_load_pending_gaia_state", None)
    if callable(legacy_loader):
        try:
            loaded_state = legacy_loader()
            if _is_gaia_state_pending(loaded_state):
                return loaded_state
        except Exception:
            pass

    for candidate_path in _candidate_gaia_state_paths(widget):
        loaded_state = _load_gaia_state_from_json(candidate_path)
        if _is_gaia_state_pending(loaded_state):
            return loaded_state
    return None


def widget_apply_scope_preloaded_spatial_index(widget):
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    if not bool(getattr(self, "_scope_preload_ready", False)):
        return False
    if bool(getattr(self, "_catalog_loaded_subset_only", False)):
        return False
    sorted_indices = getattr(self, "_scope_preload_sorted_indices", None)
    offsets = getattr(self, "_scope_preload_offsets", None)
    if sorted_indices is None or offsets is None:
        idx_path = str(getattr(self, "_scope_preload_indices_path", "") or "")
        off_path = str(getattr(self, "_scope_preload_offsets_path", "") or "")
        if (
            idx_path
            and off_path
            and os.path.isfile(idx_path)
            and os.path.isfile(off_path)
        ):
            try:
                sorted_indices = np.load(
                    idx_path, mmap_mode="r", allow_pickle=False
                )
                offsets = np.load(off_path, mmap_mode="r", allow_pickle=False)
                self._scope_preload_sorted_indices = sorted_indices
                self._scope_preload_offsets = offsets
            except Exception as exc:
                print(f"[AstroWidget] Scope preload mmap attach failed: {exc}")
                return False
    if sorted_indices is None or offsets is None:
        return False
    stars_renderer = getattr(
        getattr(self.canvas, "sky_renderer", None), "stars_renderer", None
    )
    if stars_renderer is None:
        return False
    ra_all = getattr(self, "np_ra", None)
    dec_all = getattr(self, "np_dec", None)
    if ra_all is None or dec_all is None:
        return False
    preload_rows = int(getattr(self, "_scope_preload_rows", 0) or 0)
    if preload_rows > 0 and preload_rows != int(len(ra_all)):
        return False
    try:
        key = stars_renderer._catalog_array_key(ra_all, dec_all)
        stars_renderer.apply_scope_spatial_index_payload(
            key, sorted_indices, offsets
        )
        self._scope_index_loaded_mag_cap = float(
            max(
                float(getattr(self, "_scope_index_loaded_mag_cap", 0.0)),
                float(getattr(self, "_scope_preload_loaded_max_mag", 0.0)),
            )
        )
        return True
    except Exception as exc:
        print(f"[AstroWidget] Scope preload apply failed: {exc}")
        return False


def widget_on_scope_preload_ready(widget, payload):
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    self._scope_preload_in_progress = False
    self._scope_preload_ready = True
    self._scope_preload_failed = False
    self._scope_preload_wait_logged = False
    self._scope_preload_cache_path = str(payload.get("cache_path", "") or "")
    self._scope_preload_dataset_signature = str(
        payload.get("dataset_signature", "") or ""
    )
    self._scope_preload_loaded_max_mag = float(
        payload.get("loaded_max_mag", 0.0) or 0.0
    )
    self._scope_preload_sorted_indices = payload.get("sorted_indices")
    self._scope_preload_offsets = payload.get("offsets")
    self._scope_preload_indices_path = str(
        payload.get("indices_path", "") or ""
    )
    self._scope_preload_offsets_path = str(
        payload.get("offsets_path", "") or ""
    )
    self._scope_preload_rows = int(payload.get("rows", 0) or 0)
    rows = int(payload.get("rows", 0) or 0)
    cached = bool(payload.get("cached", False))
    print(
        f"[AstroWidget] Scope preload ready: rows={rows} "
        f"max_mag={self._scope_preload_loaded_max_mag:.2f} cached={cached}"
    )
    self._scope_preload_status(
        f"ready ({rows} stars, max {self._scope_preload_loaded_max_mag:.2f})",
        keep_seconds=20.0,
    )
    append_perf_event(
        "scope_preload_ready",
        rows=rows,
        loaded_max_mag=float(self._scope_preload_loaded_max_mag),
        cached=bool(cached),
        delta_ms_boot=self._boot_delta_ms(),
    )
    self._apply_scope_preloaded_spatial_index()
    self._refresh_scope_data_state(reason="preload_ready")
    if bool(getattr(self, "_scope_preload_pending_activation", False)) or bool(
        getattr(
            getattr(self, "canvas", None), "scope_mode_enabled", lambda: False
        )()
    ):
        append_perf_event(
            "scope_activation_ready", delta_ms_boot=self._boot_delta_ms()
        )
    self._cleanup_scope_preload_worker()
    if bool(getattr(self, "_scope_preload_pending_activation", False)):
        if not bool(
            getattr(
                getattr(self, "canvas", None),
                "scope_mode_enabled",
                lambda: False,
            )()
        ):
            self._scope_preload_pending_activation = False
            QTimer.singleShot(0, self.activate_scope_mode)
        else:
            self._scope_preload_pending_activation = False


def widget_maybe_resume_pending_gaia_download(widget):
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    if bool(getattr(self, "_gaia_resume_prompt_shown", False)):
        return
    self._gaia_resume_prompt_shown = True
    state = _find_pending_gaia_state(self)
    if not isinstance(state, dict):
        return
    pct = _gaia_state_progress_percent(state)
    target_mag = _gaia_state_target_mag(state)
    if target_mag > 0.0:
        prompt = getTraduction(
            "Astro.GaiaResumePrompt",
            "Hi ha una descarrega Gaia pendent ({pct:.1f}% fins ara, objectiu mag {mag:.2f}).\n\nVols reprendre-la ara en segon pla?",
        ).format(pct=pct, mag=target_mag)
    else:
        prompt = getTraduction(
            "Astro.GaiaResumePromptNoMag",
            "Hi ha una descarrega Gaia pendent ({pct:.1f}% fins ara).\n\nVols reprendre-la ara en segon pla?",
        ).format(pct=pct)
    ans = QMessageBox.question(
        self,
        "TerraLab",
        prompt,
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.Yes,
    )
    if ans != QMessageBox.Yes:
        return
    dlg = AssetOnboardingDialog(self.asset_manager, "gaia_catalog", self)
    dlg.setModal(False)
    dlg.finished.connect(self._on_gaia_background_dialog_finished)
    self._gaia_background_dialog = dlg
    try:
        dlg.start_gaia_tap_resume()
        dlg.show()
    except Exception as exc:
        self._gaia_background_dialog = None
        QMessageBox.warning(
            self,
            "TerraLab",
            getTraduction(
                "Astro.GaiaResumeError",
                "No s'ha pogut reprendre la descarrega Gaia: {err}",
            ).format(err=str(exc)),
        )


def widget_reload_star_catalog_async(widget):
    """DEPRECATED: useu StarDataCoordinator.load_general_tile()."""
    emit_deprecation_warning(
        "TerraLab.ui.widget_misc_helpers.widget_reload_star_catalog_async",
        "TerraLab.data.star_data_coordinator.StarDataCoordinator.load_general_tile",
    )
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    """Reload Gaia catalog after onboarding import without requiring app restart."""
    try:
        old_thread = getattr(self, "_catalog_thread", None)
        if old_thread is not None and old_thread.isRunning():
            old_thread.quit()
            old_thread.finished.connect(old_thread.deleteLater)
    except Exception:
        pass
    _gaia_catalog_dir = self.runtime_layout.get(
        "data_gaia", Path(get_base_dir()) / "data" / "gaia"
    )
    self._stars_catalog_dir = str(Path(_gaia_catalog_dir).expanduser())
    self._scope_preload_started = False
    self._scope_preload_in_progress = False
    self._scope_preload_ready = False
    self._scope_preload_failed = False
    self._scope_preload_wait_logged = False
    self._scope_preload_sorted_indices = None
    self._scope_preload_offsets = None
    self._scope_preload_indices_path = ""
    self._scope_preload_offsets_path = ""
    self._scope_preload_rows = 0
    self._scope_preload_loaded_max_mag = 0.0
    self._catalog_loaded_subset_only = False
    self._scope_base_ra = None
    self._scope_base_dec = None
    self._scope_base_mag = None
    self._scope_base_r = None
    self._scope_base_g = None
    self._scope_base_b = None
    self._scope_base_bp_rp = None
    self._scope_full_catalog_attached = False
    self._refresh_scope_data_state(reason="catalog_reload")
    self._cleanup_scope_preload_worker()
    self._catalog_thread = QThread()
    self._catalog_worker = CatalogLoaderWorker()
    self._catalog_worker.moveToThread(self._catalog_thread)
    self._catalog_worker.catalog_ready.connect(self._on_catalog_ready)
    self._catalog_thread.started.connect(
        lambda: self._catalog_worker.load(self._stars_catalog_dir)
    )
    self._catalog_thread.start()
    try:
        self._catalog_thread.setPriority(QThread.LowPriority)
    except Exception:
        pass
    print("[AstroWidget] Star catalog reloading in background...")


def widget_on_scope_spatial_index_ready(
    widget, catalog_key, sorted_indices, offsets, ready_mag_cap
):
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    self._scope_index_loading = False
    restart_warmup = False
    retry_deeper = False
    try:
        stars_renderer = getattr(
            getattr(self.canvas, "sky_renderer", None), "stars_renderer", None
        )
        if stars_renderer is None:
            return
        current_key = stars_renderer._catalog_array_key(
            getattr(self, "np_ra", None),
            getattr(self, "np_dec", None),
        )
        if current_key != catalog_key:
            append_perf_event(
                "scope_deep_stale_discard",
                reason="scope_index_catalog_key_mismatch",
                delta_ms_boot=self._boot_delta_ms(),
            )
            stars_renderer.clear_scope_index_warmup(catalog_key)
            restart_warmup = True
        else:
            stars_renderer.apply_scope_spatial_index_payload(
                catalog_key, sorted_indices, offsets
            )
            self._scope_index_loaded_mag_cap = float(
                max(0.0, float(ready_mag_cap))
            )
            self._refresh_scope_data_state(reason="scope_index_ready")
            print(
                "[AstroWidget] Scope spatial index ready "
                f"(<= {float(ready_mag_cap):.2f} mag)."
            )
            self.canvas.update()
            needed_mag_cap = float(self._scope_target_index_mag_cap())
            retry_deeper = bool(
                getattr(self, "_scope_index_rewarm_requested", False)
            ) or (needed_mag_cap > self._scope_index_loaded_mag_cap + 0.05)
    finally:
        self._cleanup_scope_index_warmup()
        self._scope_index_rewarm_requested = False
    if restart_warmup:
        self._ensure_scope_spatial_index_warmup()
    elif retry_deeper:
        QTimer.singleShot(0, self._ensure_scope_spatial_index_warmup)


def widget_update_custom_theme(widget):
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    t = self.current_theme
    # Extract colors or defaults
    bg = t.get(
        "content_bg", t.get("widget_background", "rgba(20, 20, 30, 220)")
    )
    txt = t.get("title_text_color", t.get("text_primary", "white"))
    border = t.get("widget_border_color", "#555")
    # Ensure bg has alpha if needed, or just use as is
    self.panel_style = f"""
        #controlFrame {{
            background-color: {bg};
            color: {txt};
            border: 1px solid {border};
            border-radius: 8px;
        }}
        #controlFrame QLineEdit {{ background: rgba(0,0,0,50); color: {txt}; border: 1px solid {border}; border-radius: 4px; padding: 2px; }}
        #controlFrame QLineEdit:focus {{ border: 2px solid {txt}; }}
        #controlFrame QPushButton {{ background: rgba(255,255,255,20); border: 1px solid {border}; border-radius: 3px; color: {txt}; font-weight: bold; }}
        #controlFrame QPushButton:hover {{ background: rgba(255,255,255,50); }}
        #controlFrame QPushButton:checked {{ background: rgba(100,200,255,100); color: white; }}
        #controlFrame QLabel {{ color: {txt}; }}
        #controlFrame QCheckBox {{ color: {txt}; }}
        #controlFrame QSlider::handle:horizontal {{ background: {border}; border: 1px solid {txt}; width: 10px; margin: -2px 0; border-radius: 5px; }}
        #controlFrame QSlider::groove:horizontal {{ border: 1px solid #999; height: 4px; background: rgba(255,255,255,50); margin: 2px 0; }}
    """
    if hasattr(self, "frame_controls"):
        self.frame_controls.setStyleSheet(self.panel_style)
    if hasattr(self, "btn_collapse"):
        # Tab Style
        self.btn_collapse.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {bg};
                color: {txt};
                border: 1px solid {border};
                border-bottom: 2px solid {bg};
                font-size: 16px;
                font-weight: bold;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
                margin-bottom: -1px;
                padding-bottom: 2px;
            }}
            QPushButton:hover {{ background-color: {bg}; border: 1px solid rgba(255,255,255,200); }}
        """
        )


def widget_set_scope_coord_inputs(widget, ra_deg: float, dec_deg: float):
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    if not hasattr(self, "scope_ra_h_spin"):
        return
    h_total = (float(ra_deg) % 360.0) / 15.0
    h = int(h_total)
    m_total = (h_total - h) * 60.0
    m = int(m_total)
    s = (m_total - m) * 60.0
    if s >= 59.95:
        s = 0.0
        m += 1
    if m >= 60:
        m = 0
        h = (h + 1) % 24
    dec_v = abs(float(dec_deg))
    d = int(dec_v)
    dm_total = (dec_v - d) * 60.0
    dm = int(dm_total)
    ds = (dm_total - dm) * 60.0
    if ds >= 59.95:
        ds = 0.0
        dm += 1
    if dm >= 60:
        dm = 0
        d = min(90, d + 1)
    widgets = [
        self.scope_ra_h_spin,
        self.scope_ra_m_spin,
        self.scope_ra_s_spin,
        self.scope_dec_sign_combo,
        self.scope_dec_d_spin,
        self.scope_dec_m_spin,
        self.scope_dec_s_spin,
    ]
    for w in widgets:
        w.blockSignals(True)
    try:
        self.scope_ra_h_spin.setValue(h)
        self.scope_ra_m_spin.setValue(m)
        self.scope_ra_s_spin.setValue(round(float(s), 1))
        self.scope_dec_sign_combo.setCurrentIndex(
            0 if float(dec_deg) >= 0.0 else 1
        )
        self.scope_dec_d_spin.setValue(d)
        self.scope_dec_m_spin.setValue(dm)
        self.scope_dec_s_spin.setValue(round(float(ds), 1))
    finally:
        for w in widgets:
            w.blockSignals(False)


def widget_sync_constellation_controls(widget):
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    if not hasattr(self, "canvas"):
        return
    ctrl = self.canvas.constellation_controller
    draw_enabled = bool(getattr(ctrl, "enabled", False))
    visible_enabled = bool(getattr(ctrl, "visible", True))
    drawing_active = (
        bool(getattr(ctrl, "group_drawing_active", False)) and draw_enabled
    )
    selected_groups = len(
        getattr(ctrl, "selected_group_indices", set()) or set()
    )
    if hasattr(self, "btn_const_visibility"):
        self.btn_const_visibility.blockSignals(True)
        self.btn_const_visibility.setChecked(visible_enabled)
        self.btn_const_visibility.blockSignals(False)
    if hasattr(self, "btn_const_draw"):
        self.btn_const_draw.blockSignals(True)
        self.btn_const_draw.setChecked(draw_enabled)
        self.btn_const_draw.setEnabled(visible_enabled)
        self.btn_const_draw.blockSignals(False)
    if hasattr(self, "btn_const_eraser"):
        if selected_groups > 1:
            self.btn_const_eraser.setText(
                getTraduction(
                    "Astro.ConstellationDeleteSelected",
                    "Delete selected ({n})",
                ).format(n=selected_groups)
            )
        else:
            self.btn_const_eraser.setText(
                getTraduction("Astro.ConstellationDeleteAll", "Delete all")
            )
        self.btn_const_eraser.setEnabled(
            visible_enabled and (len(getattr(ctrl, "groups", [])) > 0)
        )
    if hasattr(self, "btn_const_new"):
        self.btn_const_new.setText(
            getTraduction("Astro.ConstellationFinish", "Finish constellation")
            if drawing_active
            else getTraduction("Astro.ConstellationNew", "New constellation")
        )
        self.btn_const_new.setEnabled(visible_enabled)
    if hasattr(self, "btn_const_rename"):
        self.btn_const_rename.setEnabled(visible_enabled)
    if hasattr(self, "lbl_constellation_state"):
        total = len(getattr(ctrl, "groups", []))
        active = getattr(ctrl, "active_group_index", None)
        active_txt = "-"
        if active is not None and 0 <= active < total:
            active_txt = str(ctrl.groups[active].name)
        self.lbl_constellation_state.setText(
            getTraduction(
                "Astro.ConstellationStatus", "Groups: {n} | Active: {name}"
            ).format(
                n=total,
                name=active_txt,
            )
        )


def widget_refresh_milkyway_status_indicator(widget):
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    if not hasattr(self, "lbl_milkyway_status"):
        return
    if not hasattr(self, "canvas"):
        return
    status = None
    renderer = getattr(
        getattr(self.canvas, "sky_renderer", None), "milkyway_overlay", None
    )
    if renderer is not None and hasattr(renderer, "runtime_status"):
        try:
            status = renderer.runtime_status()
        except Exception:
            status = None
    enabled = bool(getattr(self, "milkyway_overlay_enabled", True))
    if not enabled:
        self.lbl_milkyway_status.setText("MW: OFF")
        return
    if not isinstance(status, dict):
        self.lbl_milkyway_status.setText("MW: ON (pending)")
        return
    tex_ok = bool(status.get("texture_loaded", False))
    dust_req = bool(status.get("dust_requested", False))
    dust_ok = bool(status.get("dust_loaded", False))
    opacity = float(status.get("effective_opacity", 0.0))
    opacity_reason = str(status.get("opacity_reason", ""))
    rgb_gain = float(status.get("texture_rgb_gain", 1.0))
    texture_frame = str(status.get("texture_frame", "galactic"))
    frame_short = "gal" if texture_frame.startswith("gal") else "eq"
    ra_off = float(status.get("ra_offset_deg", 0.0))
    lat_flip = bool(status.get("texture_lat_flip", False))
    lon_flip = bool(status.get("texture_lon_flip", False))
    flip_txt = ("vf=1" if lat_flip else "vf=0") + (
        " hf=1" if lon_flip else " hf=0"
    )
    dust_den = float(status.get("dust_density_strength", 0.0))
    dust_ext = float(status.get("dust_extinction_strength", 0.0))
    dust_gain_txt = f"d={dust_den:.2f} e={dust_ext:.2f}"
    if not tex_ok:
        self.lbl_milkyway_status.setText("MW: textura NO carregada")
        return
    if dust_req:
        dust_txt = "Planck OK" if dust_ok else "Planck NO"
    else:
        dust_txt = "Planck OFF"
    if opacity <= 1e-4:
        self.lbl_milkyway_status.setText(
            f"MW: op=0.00 ({opacity_reason}) g={rgb_gain:.2f} fr={frame_short} off={ra_off:.0f} {flip_txt} {dust_gain_txt} {dust_txt}"
        )
        return
    self.lbl_milkyway_status.setText(
        f"MW: ON op={opacity:.2f} g={rgb_gain:.2f} fr={frame_short} off={ra_off:.0f} {flip_txt} {dust_gain_txt} {dust_txt}"
    )


def widget_refresh_climate_status_indicator(widget):
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    if not hasattr(self, "lbl_climate_fallback") or not hasattr(
        self, "chk_clima"
    ):
        return
    if not bool(self.chk_clima.isChecked()):
        self.lbl_climate_fallback.hide()
        self._climate_fallback_since = None
        self._climate_remote_since = None
        return
    weather = self._active_weather_system()
    source = "fallback"
    reason = ""
    if weather is not None and hasattr(weather, "get_runtime_status"):
        try:
            status = weather.get_runtime_status()
            source = str(status.get("source", "fallback"))
            reason = str(status.get("reason", "") or "")
            if bool(status.get("requires_user_agent", False)):
                reason = "missing_user_agent"
        except Exception:
            source = "fallback"
            reason = "status_error"
    elif weather is None:
        reason = "weather_missing"
    now_m = time.monotonic()
    is_remote = source == "remote"
    if is_remote:
        self._climate_fallback_since = None
        if self._climate_remote_since is None:
            self._climate_remote_since = now_m
        remote_stable_s = now_m - self._climate_remote_since
        if remote_stable_s >= float(
            getattr(self, "_climate_remote_hide_delay_s", 1.2)
        ):
            self.lbl_climate_fallback.hide()
        return
    # Fallback branch.
    self._climate_remote_since = None
    if self._climate_fallback_since is None:
        self._climate_fallback_since = now_m
    fallback_stable_s = now_m - self._climate_fallback_since
    if fallback_stable_s >= float(
        getattr(self, "_climate_fallback_show_delay_s", 2.5)
    ):
        self.lbl_climate_fallback.setText(
            getTraduction(
                "Astro.ClimateFallbackActive",
                "Real weather unavailable (fallback)",
            )
        )
        self.lbl_climate_fallback.setToolTip(reason)
        self.lbl_climate_fallback.show()


def widget_refresh_gaia_download_feedback(widget):
    """Actualitza feedback visual de descarrega Gaia pendent a la UI principal."""
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    label_widget = getattr(self, "lbl_gaia_download_status", None)
    progress_widget = getattr(self, "progress_gaia_download", None)
    stars_toggle = getattr(self, "chk_enable_sky", None)
    if label_widget is None or progress_widget is None or stars_toggle is None:
        return
    if not bool(stars_toggle.isChecked()):
        label_widget.hide()
        progress_widget.hide()
        return

    now_mono = time.monotonic()
    last_poll_mono = float(
        getattr(self, "_gaia_download_status_last_poll_mono", 0.0)
    )
    min_interval_s = float(
        getattr(self, "_gaia_download_status_min_interval_s", 0.9)
    )
    if (now_mono - last_poll_mono) < max(0.2, min_interval_s):
        return
    self._gaia_download_status_last_poll_mono = now_mono

    pending_state = _find_pending_gaia_state(self)
    if not _is_gaia_state_pending(pending_state):
        label_widget.hide()
        progress_widget.hide()
        return

    percent_value = _gaia_state_progress_percent(pending_state)
    message_text = str(pending_state.get("status_message", "") or "").strip()
    tile_identifier = str(pending_state.get("current_tile_id", "") or "").strip()
    if not message_text:
        message_text = "Descarregant Gaia per teseles"
    if tile_identifier:
        message_text = f"{message_text} ({tile_identifier})"

    label_widget.setText(f"Gaia: {message_text}")
    label_widget.setToolTip(
        str(pending_state.get("_state_path", "") or "gaia_tiles_state.json")
    )
    label_widget.show()

    if percent_value <= 0.0:
        progress_widget.setRange(0, 0)
    else:
        progress_widget.setRange(0, 100)
        progress_widget.setValue(
            max(0, min(100, int(round(float(percent_value)))))
        )
    progress_widget.show()


def widget_ensure_copernicus_credentials_prompt(widget):
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
    self = widget
    if bool(get_config_value("copernicus_informed", False)):
        return
    title = getTraduction(
        "Astro.CopernicusDialogTitle", "Copernicus Climate setup"
    )
    intro = getTraduction(
        "Astro.CopernicusDialogIntro",
        "To enable online climate and aerosol data, create a free CDS account and generate your API key.",
    )
    url_text = getTraduction("Astro.CopernicusDialogUrl", "Open guide")
    key_prompt = getTraduction(
        "Astro.CopernicusDialogKeyPrompt", "Paste your CDS API key"
    )
    key_ok = getTraduction(
        "Astro.CopernicusDialogSaved", "API key saved in local config."
    )
    key_empty = getTraduction(
        "Astro.CopernicusDialogEmpty",
        "No key entered. Offline fallback will be used.",
    )
    copernicus_url = "https://cds.climate.copernicus.eu/how-to-api"
    msg = QMessageBox(self)
    msg.setIcon(QMessageBox.Information)
    msg.setWindowTitle(title)
    msg.setText(intro)
    msg.setInformativeText(copernicus_url)
    open_btn = msg.addButton(url_text, QMessageBox.ActionRole)
    enter_btn = msg.addButton(
        getTraduction("Astro.CopernicusDialogEnterKey", "Enter API key"),
        QMessageBox.AcceptRole,
    )
    skip_btn = msg.addButton(
        getTraduction("Astro.CopernicusDialogSkip", "Skip"),
        QMessageBox.RejectRole,
    )
    msg.exec_()
    clicked = msg.clickedButton()
    if clicked is open_btn:
        QDesktopServices.openUrl(QUrl(copernicus_url))
        clicked = enter_btn
    if clicked is enter_btn:
        current_key = str(get_config_value("copernicus_api_key", "") or "")
        key, ok = QInputDialog.getText(
            self, title, key_prompt, text=current_key
        )
        key = str(key or "").strip()
        if ok and key:
            set_config_value("copernicus_api_key", key)
            set_config_value(
                "copernicus_api_url", "https://cds.climate.copernicus.eu/api"
            )
            QMessageBox.information(self, title, key_ok)
        else:
            QMessageBox.information(self, title, key_empty)
    elif clicked is skip_btn:
        # Keep offline fallback mode until user enters the key manually.
        pass
    # The user has already been informed at least once.
    set_config_value("copernicus_informed", True)
