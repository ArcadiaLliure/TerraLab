"""Bootstrap and runtime helpers for the astronomical widget."""

from __future__ import annotations

import os
import time

import numpy as np
from PyQt5.QtCore import QThread

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.deprecation_registry import emit_deprecation_warning
from TerraLab.common.perf_events import append_perf_event
from TerraLab.common.utils import get_config_value
from TerraLab.data.catalogs.constants import STAR_CATALOG_NAKED_EYE_MAX_MAG
from TerraLab.data.catalogs.star_catalog import _bp_rp_to_rgb_arrays
from TerraLab.light_pollution.modes import is_automatic_mode
from TerraLab.ui.scope_preload_worker import ScopeFullPreloadWorker
from TerraLab.ui.workers.catalog_loader import CatalogLoaderWorker
from TerraLab.ui.workers.scope_index import ScopeIndexWarmWorker
from TerraLab.ui.workers.skyfield_loader import SkyfieldLoaderWorker


def _queue_initial_automatic_light_pollution(widget) -> None:
    """Queue the initial location estimate after worker initialization."""
    if not is_automatic_mode(getattr(widget, "light_pollution_mode", None)):
        return
    try:
        widget.recalculate_automatic_light_pollution()
    except Exception as exc:
        print(f"[AstroWidget] Auto-Bortle startup sync error: {exc}")


def _run_catalog_ready_pipeline_stage(widget, token: int, stage: int) -> None:
    if int(token) != int(getattr(widget, "_catalog_ready_pipeline_token", 0)):
        return
    if stage == 0:
        widget.build_search_index()
        widget._schedule_lifecycle_callback(
            0,
            lambda w=widget, t=token: _run_catalog_ready_pipeline_stage(
                w, t, 1
            ),
        )
        return
    if stage == 1:
        widget._apply_scope_preloaded_spatial_index()
        widget._schedule_lifecycle_callback(
            0,
            lambda w=widget, t=token: _run_catalog_ready_pipeline_stage(
                w, t, 2
            ),
        )
        return
    if stage == 2:
        widget._ensure_scope_catalog_loaded()
        widget._schedule_lifecycle_callback(
            0,
            lambda w=widget, t=token: _run_catalog_ready_pipeline_stage(
                w, t, 3
            ),
        )
        return
    if stage == 3:
        if widget.canvas.scope_mode_enabled():
            widget._ensure_scope_spatial_index_warmup()
        widget._schedule_lifecycle_callback(
            0,
            lambda w=widget, t=token: _run_catalog_ready_pipeline_stage(
                w, t, 4
            ),
        )
        return
    if stage == 4:
        if getattr(widget, "scene_load_stage", "boot") in {"boot", "base_sky"}:
            widget._set_scene_load_stage("stars_ready")
        widget.canvas.update()
        thread = getattr(widget, "_catalog_thread", None)
        if thread is not None:
            try:
                thread.quit()
            except Exception:
                log_suppressed_exception(__name__, "_run_catalog_ready_pipeline_stage")


def widget_start_scope_full_preload_async(
    widget, reason: str = "runtime", force_rebuild: bool = False
):
    self = widget
    if (
        str(getattr(self, "scope_preload_mode", "startup_full"))
        != "startup_full"
    ):
        return
    if bool(getattr(self, "_scope_preload_in_progress", False)):
        return
    if bool(getattr(self, "_scope_preload_ready", False)) and (
        not bool(force_rebuild)
    ):
        self._apply_scope_preloaded_spatial_index()
        return
    if bool(getattr(self, "_scope_preload_started", False)) and (
        not bool(force_rebuild)
    ):
        return
    ra_all = getattr(self, "np_ra", None)
    dec_all = getattr(self, "np_dec", None)
    mag_all = getattr(self, "np_mag", None)
    ra_rows = int(len(ra_all)) if ra_all is not None else 0
    subset_only = bool(getattr(self, "_catalog_loaded_subset_only", False))
    if subset_only:
        return
    use_in_memory = (
        (not subset_only)
        and ra_all is not None
        and dec_all is not None
        and mag_all is not None
        and int(ra_rows) > 0
        and int(ra_rows) == int(len(dec_all)) == int(len(mag_all))
    )
    try:
        in_memory_max_rows = int(
            max(
                100_000,
                int(
                    get_config_value(
                        "performance.scope_preload_in_memory_max_rows",
                        5_000_000,
                    )
                ),
            )
        )
    except Exception:
        in_memory_max_rows = 5_000_000
    if use_in_memory and int(ra_rows) > int(in_memory_max_rows):
        use_in_memory = False
        print(
            "[AstroWidget] Scope preload switched to subprocess mode "
            f"(rows={ra_rows} > in_memory_limit={in_memory_max_rows})."
        )
    runtime_npz = ""
    try:
        runtime_catalog_hint = os.path.join(
            str(getattr(self, "_stars_catalog_dir", "") or ""),
            "stars_catalog.npy",
        )
        if runtime_catalog_hint and os.path.isfile(runtime_catalog_hint):
            runtime_npz = runtime_catalog_hint
        if use_in_memory:
            from TerraLab.data.stars_dataset import (
                get_runtime_catalog_source_info,
            )

            source_path = str(
                get_runtime_catalog_source_info().get("source_path", "") or ""
            )
            if source_path and os.path.isfile(source_path):
                runtime_npz = source_path
    except Exception as exc:
        print(
            f"[AstroWidget] Scope preload runtime path hint unavailable: {exc}"
        )
    self._scope_preload_started = True
    self._scope_preload_in_progress = True
    self._scope_preload_failed = False
    if bool(force_rebuild):
        self._scope_preload_sorted_indices = None
        self._scope_preload_offsets = None
        self._scope_preload_indices_path = ""
        self._scope_preload_offsets_path = ""
    self._scope_preload_last_progress_pct = -1.0
    self._refresh_scope_data_state(reason="preload_start")
    self._scope_preload_status("starting...")
    append_perf_event(
        "scope_preload_start",
        reason=str(reason),
        runtime_npz=runtime_npz,
        delta_ms_boot=self._boot_delta_ms(),
    )
    cache_dir = self._scope_preload_cache_dir()
    stars_dir = str(getattr(self, "_stars_catalog_dir", "") or "")
    max_mag = float("nan")
    if use_in_memory:
        print(
            f"[AstroWidget] Scope preload using in-memory catalog ({len(ra_all)} stars)."
        )
    elif subset_only:
        print(
            "[AstroWidget] Scope preload forcing disk catalog (startup catalog is subset)."
        )
    thread = QThread()
    worker = ScopeFullPreloadWorker()
    worker.moveToThread(thread)
    worker.progress.connect(self._on_scope_preload_progress)
    worker.ready.connect(self._on_scope_preload_ready)
    worker.error.connect(self._on_scope_preload_error)
    worker.ready.connect(thread.quit)
    worker.error.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    if use_in_memory:
        thread.started.connect(
            lambda w=worker, ra=ra_all, dec=dec_all, mag=mag_all, npz=runtime_npz, sdir=stars_dir, cdir=cache_dir, mm=max_mag, sv=self._scope_preload_schema_version, fr=bool(
                force_rebuild
            ): w.run_from_arrays(
                ra,
                dec,
                mag,
                npz,
                sdir,
                cdir,
                mm,
                int(sv),
                bool(fr),
            )
        )
    else:
        thread.started.connect(
            lambda w=worker, npz=runtime_npz, sdir=stars_dir, cdir=cache_dir, mm=max_mag, sv=self._scope_preload_schema_version, fr=bool(
                force_rebuild
            ): w.run(
                npz,
                sdir,
                cdir,
                mm,
                int(sv),
                bool(fr),
            )
        )
    self._scope_preload_thread = thread
    self._scope_preload_worker = worker
    thread.start()
    try:
        thread.setPriority(QThread.LowPriority)
    except Exception:
        log_suppressed_exception(__name__, "widget_start_scope_full_preload_async")


def widget_start_async_bootstrap(widget):
    self = widget
    if getattr(self, "_async_bootstrap_started", False):
        return
    self._async_bootstrap_started = True
    self._horizon_progress_ui_ts = 0.0
    self._horizon_progress_min_interval_s = max(
        0.05,
        float(
            get_config_value(
                "performance.horizon_progress_min_interval_s", 0.10
            )
        ),
    )
    self._horizon_preview_min_interval_s = max(
        0.10,
        float(
            get_config_value(
                "performance.horizon_preview_min_interval_s", 0.50
            )
        ),
    )
    self._horizon_preview_last_apply_ts = 0.0
    self._horizon_preview_schedule_id = 0
    self._horizon_preview_flush_scheduled = False
    self._horizon_preview_pending_payload = None
    try:
        from TerraLab.data.catalogs.scope_cache import (
            ScopeRuntimeCacheManager,
        )

        scope_cache_dir = os.path.join(
            str(getattr(self, "_stars_catalog_dir", "") or ""),
            "cache",
            "scope",
        )
        ScopeRuntimeCacheManager.from_cache_dir(scope_cache_dir).cleanup(
            keep_stamps=1,
            tmp_ttl_seconds=1.0,
        )
    except Exception:
        log_suppressed_exception(__name__, "widget_start_async_bootstrap")
    # --- Async Skyfield loading ---
    self._skyfield_thread = QThread()
    self._skyfield_worker = SkyfieldLoaderWorker()
    self._skyfield_worker.moveToThread(self._skyfield_thread)
    self._skyfield_worker.skyfield_ready.connect(self._on_skyfield_ready)
    self._skyfield_thread.started.connect(self._skyfield_worker.load)
    self._skyfield_thread.start()
    try:
        self._skyfield_thread.setPriority(QThread.LowPriority)
    except Exception:
        log_suppressed_exception(__name__, "widget_start_async_bootstrap")
    print("[AstroWidget] Skyfield loading in background...")
    # --- Async Catalog loading ---
    if not bool(getattr(self, "defer_catalog_until_horizon_preview", True)):
        self._start_catalog_loader_async(reason="bootstrap")
    else:
        print(
            "[AstroWidget] Star catalog loading deferred until horizon ready."
        )
        self._catalog_defer_t0 = time.perf_counter()
        self._schedule_lifecycle_callback(
            15_000, self._try_start_catalog_loader_deferred
        )
    # --- Canonical terrain coordinator bootstrap ---
    terrain_coordinator = self.terrain_coordinator
    saved_offset = float(get_config_value("observer_offset", 0.0))
    terrain_coordinator.set_observer_offset(saved_offset)
    try:
        terrain_coordinator.worker_thread.setPriority(QThread.LowPriority)
    except Exception:
        log_suppressed_exception(__name__, "widget_start_async_bootstrap")

    def trigger_bake():
        print(
            f"[AstroWidget] Emitting bake request for {self.latitude}, {self.longitude}"
        )
        self._begin_horizon_bake()

    self._schedule_lifecycle_callback(
        300,
        terrain_coordinator.initialize,
    )
    self._schedule_lifecycle_callback(
        550, lambda w=self: _queue_initial_automatic_light_pollution(w)
    )
    self._schedule_lifecycle_callback(900, trigger_bake)


def widget_on_catalog_ready(
    widget,
    celestial_objects,
    np_ra,
    np_dec,
    np_mag,
    np_r,
    np_g,
    np_b,
    np_bp_rp,
):
    self = widget
    """Callback when star catalog finishes loading in background."""
    try:
        existing_ra = getattr(self, "np_ra", None)
        existing_rows = int(len(existing_ra)) if existing_ra is not None else 0
    except Exception:
        existing_rows = 0
    incoming_rows = int(len(np_ra)) if np_ra is not None else 0

    load_mode = "unknown"
    source_kind = "unknown"
    try:
        worker = getattr(self, "_catalog_worker", None)
        load_mode = str(getattr(worker, "last_load_mode", "unknown") or "unknown")
        source_kind = str(
            getattr(worker, "last_source_kind", "unknown") or "unknown"
        )
    except Exception:
        log_suppressed_exception(__name__, "widget_on_catalog_ready")

    # Never replace a valid in-memory/deep catalog with a tiny fallback payload.
    fallback_like = (
        load_mode == "random_fallback"
        or source_kind in {"unknown", "none", "json_fallback", "ecsv_fallback"}
    )
    ignore_fallback_override = bool(
        fallback_like and existing_rows >= 5000 and incoming_rows > 0 and incoming_rows <= 1000
    )
    if ignore_fallback_override:
        print(
            "[AstroWidget] Ignoring fallback catalog payload to preserve loaded dataset: "
            f"existing_rows={existing_rows} incoming_rows={incoming_rows} "
            f"mode={load_mode} source={source_kind}"
        )
        np_ra = getattr(self, "np_ra", None)
        np_dec = getattr(self, "np_dec", None)
        np_mag = getattr(self, "np_mag", None)
        np_r = getattr(self, "np_r", None)
        np_g = getattr(self, "np_g", None)
        np_b = getattr(self, "np_b", None)
        np_bp_rp = getattr(self, "np_bp_rp", None)
        incoming_rows = int(len(np_ra)) if np_ra is not None else 0
        load_mode = "preserved_existing"
        source_kind = "preserved_existing"
    else:
        self.celestial_objects = celestial_objects

    self._catalog_mag_sorted = False
    if np_ra is not None:
        self.np_ra = np_ra
        self.np_dec = np_dec
        self.np_mag = np_mag
        self.np_r = np_r
        self.np_g = np_g
        self.np_b = np_b
        self.np_bp_rp = np_bp_rp
        self._catalog_mag_sorted = True
        try:
            self._scope_catalog_loaded_max_mag = max(
                float(self._scope_catalog_loaded_max_mag),
                (
                    float(np.nanmax(np.asarray(np_mag, dtype=np.float32)))
                    if np_mag is not None and len(np_mag) > 0
                    else float(STAR_CATALOG_NAKED_EYE_MAX_MAG)
                ),
            )
        except Exception:
            log_suppressed_exception(__name__, "widget_on_catalog_ready")
        try:
            if np_mag is not None and len(np_mag) > 0:
                self._catalog_max_mag = max(
                    float(
                        getattr(
                            self,
                            "_catalog_max_mag",
                            STAR_CATALOG_NAKED_EYE_MAX_MAG,
                        )
                    ),
                    float(np.nanmax(np.asarray(np_mag, dtype=np.float32))),
                )
        except Exception:
            log_suppressed_exception(__name__, "widget_on_catalog_ready")
    array_rows = int(len(np_ra)) if np_ra is not None else 0
    named_rows = (
        int(len(celestial_objects)) if celestial_objects is not None else 0
    )
    fallback_active = False
    fallback_reason = ""
    self._catalog_loaded_subset_only = False
    try:
        worker = getattr(self, "_catalog_worker", None)
        total_rows_hint = int(getattr(worker, "last_total_rows", 0) or 0)
        catalog_max_hint = float(
            getattr(worker, "last_catalog_max_mag", float("nan"))
            or float("nan")
        )
        if load_mode == "runtime_subset":
            self._catalog_loaded_subset_only = True
            self._scope_full_catalog_attached = False
            self._scope_base_ra = np_ra
            self._scope_base_dec = np_dec
            self._scope_base_mag = np_mag
            self._scope_base_r = np_r
            self._scope_base_g = np_g
            self._scope_base_b = np_b
            self._scope_base_bp_rp = np_bp_rp
            if total_rows_hint > 0:
                print(
                    f"[AstroWidget] Startup catalog is subset: "
                    f"rows={array_rows} of total_rows={total_rows_hint}"
                )
        if np.isfinite(catalog_max_hint):
            self._catalog_max_mag = max(
                float(
                    getattr(
                        self,
                        "_catalog_max_mag",
                        STAR_CATALOG_NAKED_EYE_MAX_MAG,
                    )
                ),
                float(catalog_max_hint),
            )
        if load_mode == "random_fallback":
            fallback_active = True
            fallback_reason = "random_stars_fallback"
        elif source_kind in {"json_fallback", "ecsv_fallback"}:
            fallback_active = True
            fallback_reason = source_kind
        elif source_kind in {"unknown", "none"} and array_rows <= 1000:
            fallback_active = True
            fallback_reason = source_kind
    except Exception:
        fallback_active = False
        fallback_reason = ""
    self._stars_fallback_active = bool(fallback_active)
    self._stars_fallback_reason = str(fallback_reason or "")
    self.refresh_light_pollution_catalog_range()
    self._refresh_scope_data_state(reason="catalog_ready")
    print(
        f"[AstroWidget] Star catalog ready (async): "
        f"catalog_rows={array_rows} named_rows={named_rows}"
    )
    append_perf_event(
        "catalog_ready",
        rows=int(array_rows),
        named_rows=int(named_rows),
        max_mag=float(
            getattr(self, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)
        ),
        delta_ms_boot=self._boot_delta_ms(),
    )
    self._refresh_stars_status_indicator()
    # Si hi ha manifest per teseles, inicialitza coordinador un cop tenim UI/canvas.
    try:
        _scope_try_init_star_data_coordinator(self)
    except Exception:
        log_suppressed_exception(__name__, "widget_on_catalog_ready")
    # Run heavy post-ready work in small queued steps to keep UI responsive.
    next_token = int(getattr(self, "_catalog_ready_pipeline_token", 0)) + 1
    self._catalog_ready_pipeline_token = next_token
    self._schedule_lifecycle_callback(
        0,
        lambda w=self, t=next_token: _run_catalog_ready_pipeline_stage(
            w, t, 0
        ),
    )


def widget_ensure_scope_catalog_loaded(widget, force_now: bool = False):
    """DEPRECATED: useu StarDataCoordinator.load_deep_tile()."""
    emit_deprecation_warning(
        "TerraLab.ui.widget_bootstrap_helpers.widget_ensure_scope_catalog_loaded",
        "TerraLab.data.star_data_coordinator.StarDataCoordinator.load_deep_tile",
    )
    self = widget
    if np is None:
        return
    coordinator = _scope_try_init_star_data_coordinator(self)
    if coordinator is not None:
        self._scope_catalog_loading = False
        scope_enabled = bool(
            getattr(
                getattr(self, "canvas", None),
                "scope_mode_enabled",
                lambda: False,
            )()
        )
        if (not bool(force_now)) and (not scope_enabled):
            return
        focus_tile_id, visible_tile_ids = _scope_pick_scope_region_request(
            self, coordinator
        )
        if not focus_tile_id:
            return
        if (
            (not bool(force_now))
            and str(getattr(self, "_scope_last_tile_request", "") or "")
            == str(focus_tile_id)
        ):
            return
        self._scope_last_tile_request = str(focus_tile_id)
        print(
            "[AstroWidget] Scope tile request: "
            f"{focus_tile_id} visible_tiles={len(visible_tile_ids)}"
        )
        coordinator.request_scope_region(
            focus_tile_id,
            priority_tile_ids=visible_tile_ids,
        )
        return
    if getattr(self, "_scope_catalog_loading", False):
        return
    stars_dir = getattr(self, "_stars_catalog_dir", "")
    if not stars_dir or not os.path.isdir(stars_dir):
        return
    loaded_max_mag = float(
        getattr(
            self,
            "_scope_catalog_loaded_max_mag",
            STAR_CATALOG_NAKED_EYE_MAX_MAG,
        )
    )
    catalog_max_mag = float(
        getattr(self, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)
    )
    force_runtime_full = False
    if bool(getattr(self, "_catalog_loaded_subset_only", False)):
        ext_npy = os.path.join(stars_dir, "stars_catalog_extension.npy")
        has_extension = os.path.isfile(ext_npy)
        if not has_extension:
            try:
                has_extension = any(
                    str(name).lower().endswith(".npz")
                    and str(name).upper().startswith("MAGNITUD_")
                    for name in os.listdir(stars_dir)
                )
            except Exception:
                has_extension = False
        if not has_extension:
            force_runtime_full = True
    if force_runtime_full:
        scope_active = bool(
            getattr(
                getattr(self, "canvas", None),
                "scope_mode_enabled",
                lambda: False,
            )()
        )
        if (not bool(force_now)) and (not scope_active):
            return
    if (not force_runtime_full) and loaded_max_mag >= (catalog_max_mag - 1e-3):
        return
    self._scope_catalog_loading = True
    if force_runtime_full:
        self._scope_set_data_state(
            "loading_deep", reason="scope_catalog_runtime_full"
        )
    thread = QThread()
    worker = CatalogLoaderWorker()
    worker.moveToThread(thread)
    worker.scope_extension_ready.connect(self._on_scope_extension_ready)
    if hasattr(worker, "scope_extension_progress"):
        worker.scope_extension_progress.connect(
            self._on_scope_extension_progress
        )
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.started.connect(
        lambda w=worker, s=stars_dir, m=loaded_max_mag, fr=bool(
            force_runtime_full
        ): w.load_scope_extensions(
            s,
            m,
            getattr(self, "np_ra", None),
            getattr(self, "np_dec", None),
            getattr(self, "np_mag", None),
            getattr(self, "np_r", None),
            getattr(self, "np_g", None),
            getattr(self, "np_b", None),
            getattr(self, "np_bp_rp", None),
            bool(fr),
        )
    )
    self._scope_catalog_thread = thread
    self._scope_catalog_worker = worker
    thread.start()
    try:
        thread.setPriority(QThread.LowPriority)
    except Exception:
        log_suppressed_exception(__name__, "widget_ensure_scope_catalog_loaded")
    if force_runtime_full:
        print(
            "[AstroWidget] Scope full runtime catalog loading in background..."
        )
    else:
        print(
            f"[AstroWidget] Scope catalog extension loading from > {loaded_max_mag:.2f} mag..."
        )


def _scope_try_init_star_data_coordinator(widget):
    """Inicialitza coordinador per teseles si hi ha `tile_manifest.json`."""
    self = widget
    coordinator = getattr(self, "star_data_coordinator", None)
    if coordinator is not None:
        return coordinator

    try:
        from pathlib import Path

        from TerraLab.data.star_data_coordinator import StarDataCoordinator
    except Exception:
        return None

    runtime_layout = getattr(self, "runtime_layout", {}) or {}
    gaia_dir = str(runtime_layout.get("data_gaia", "") or "").strip()
    if not gaia_dir:
        return None
    manifest_path = Path(gaia_dir).expanduser() / "tile_manifest.json"
    if not manifest_path.is_file():
        return None

    try:
        coordinator = StarDataCoordinator(manifest_path)
    except Exception as exc:
        try:
            print(
                "[AstroWidget] StarDataCoordinator init error: "
                f"{type(exc).__name__}: {exc}"
            )
        except Exception:
            log_suppressed_exception(__name__, "_scope_try_init_star_data_coordinator")
        return None

    self.star_data_coordinator = coordinator
    try:
        print(f"[AstroWidget] StarDataCoordinator ready: {manifest_path}")
    except Exception:
        log_suppressed_exception(__name__, "_scope_try_init_star_data_coordinator")
    try:
        coordinator.general_tile_ready.connect(
            lambda payload, w=self: _scope_apply_coordinator_payload(
                w, payload, "general"
            )
        )
        coordinator.extension_ready.connect(
            lambda payload, w=self: _scope_apply_coordinator_payload(
                w, payload, "extension"
            )
        )
        coordinator.scope_index_ready.connect(
            lambda payload, w=self: _scope_apply_coordinator_scope_index(
                w, payload
            )
        )
        coordinator.error_occurred.connect(
            lambda msg: print(f"[AstroWidget] StarDataCoordinator error: {msg}")
        )
        coordinator.load_general_tile()
    except Exception as exc:
        try:
            print(
                "[AstroWidget] StarDataCoordinator signal wiring error: "
                f"{type(exc).__name__}: {exc}"
            )
        except Exception:
            log_suppressed_exception(__name__, "_scope_try_init_star_data_coordinator")
    return coordinator


def _scope_apply_coordinator_payload(widget, payload, reason: str) -> None:
    """Aplica dataset actiu del coordinador al widget legacy."""
    self = widget
    if np is None or (not isinstance(payload, dict)):
        return
    try:
        np_ra = np.asarray(payload.get("ra", np.empty(0, dtype=np.float32)))
        np_dec = np.asarray(payload.get("dec", np.empty(0, dtype=np.float32)))
        np_mag = np.asarray(payload.get("mag", np.empty(0, dtype=np.float32)))
        np_r = np.asarray(payload.get("r", np.empty(0, dtype=np.float32)))
        np_g = np.asarray(payload.get("g", np.empty(0, dtype=np.float32)))
        np_b = np.asarray(payload.get("b", np.empty(0, dtype=np.float32)))
        np_bp_rp = np.asarray(
            payload.get("bp_rp", np.full(len(np_mag), 0.8, dtype=np.float32))
        )
    except Exception:
        return

    row_count = int(len(np_ra))
    if row_count <= 0:
        return
    if not (
        row_count == int(len(np_dec))
        == int(len(np_mag))
        == int(len(np_r))
        == int(len(np_g))
        == int(len(np_b))
    ):
        return

    loaded_tile_ids = payload.get("loaded_tile_ids", ())
    try:
        signature = tuple(sorted(str(tid) for tid in loaded_tile_ids))
    except Exception:
        signature = tuple()

    prev_signature = tuple(
        getattr(self, "_scope_manifest_active_signature", tuple()) or tuple()
    )
    prev_ra = getattr(self, "np_ra", None)
    prev_rows = int(len(prev_ra)) if prev_ra is not None else 0
    if signature == prev_signature and prev_rows == row_count:
        return

    self.np_ra = np_ra
    self.np_dec = np_dec
    self.np_mag = np_mag
    self.np_r = np_r
    self.np_g = np_g
    self.np_b = np_b
    self.np_bp_rp = np_bp_rp
    self._scope_manifest_active_signature = signature
    self._scope_catalog_loading = False
    self._scope_full_catalog_attached = True
    self._catalog_loaded_subset_only = False
    self._catalog_mag_sorted = False
    self._refresh_scope_data_state(reason=f"coordinator_{str(reason)}")
    try:
        max_loaded = float(np.nanmax(np_mag))
    except Exception:
        max_loaded = float(STAR_CATALOG_NAKED_EYE_MAX_MAG)
    self._scope_catalog_loaded_max_mag = max(
        float(
            getattr(
                self,
                "_scope_catalog_loaded_max_mag",
                STAR_CATALOG_NAKED_EYE_MAX_MAG,
            )
        ),
        float(max_loaded),
    )
    self._catalog_max_mag = max(
        float(getattr(self, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)),
        float(max_loaded),
    )
    self.refresh_light_pollution_catalog_range()
    try:
        self._scope_set_data_state(
            "ready_deep", reason=f"coordinator_{str(reason)}"
        )
    except Exception:
        log_suppressed_exception(__name__, "_scope_apply_coordinator_payload")
    canvas = getattr(self, "canvas", None)
    if canvas is not None:
        try:
            canvas._cached_star_image = None
            canvas._cached_trail_image = None
        except Exception:
            log_suppressed_exception(__name__, "_scope_apply_coordinator_payload")
        try:
            canvas.update()
        except Exception:
            log_suppressed_exception(__name__, "_scope_apply_coordinator_payload")


def _scope_apply_coordinator_scope_index(widget, payload) -> None:
    """Aplica index scope publicat pel coordinador al renderer actual."""
    self = widget
    if not isinstance(payload, dict):
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
        return
    stars_renderer = getattr(
        getattr(getattr(self, "canvas", None), "sky_renderer", None),
        "stars_renderer",
        None,
    )
    if stars_renderer is None:
        return
    try:
        catalog_key = stars_renderer._catalog_array_key(ra_all, dec_all)
        self._on_scope_spatial_index_ready(
            catalog_key,
            sorted_indices,
            offsets,
            float(payload.get("loaded_max_mag", 0.0) or 0.0),
        )
    except Exception as exc:
        try:
            print(f"[AstroWidget] scope index payload apply error: {exc}")
        except Exception:
            log_suppressed_exception(__name__, "_scope_apply_coordinator_scope_index")


def _scope_pick_scope_region_request(widget, coordinator) -> tuple[str, tuple[str, ...]]:
    """Resol tesela focus i teseles visibles segons el camp real del scope."""
    self = widget
    canvas = getattr(self, "canvas", None)
    if canvas is None:
        return "", tuple()
    if not bool(getattr(canvas, "scope_mode_enabled", lambda: False)()):
        return "", tuple()

    scope_ctrl = getattr(canvas, "scope_controller", None)
    center = getattr(scope_ctrl, "center", None)
    if center is None:
        return "", tuple()

    try:
        import math

        ut_hour, day_of_year_utc = canvas._current_ut_context()
        ra_dec = canvas._altaz_to_ra_dec(
            float(center[0]),
            float(center[1]),
            float(ut_hour),
            int(day_of_year_utc),
        )
        if ra_dec is None:
            return "", tuple()
        ra_center = float(ra_dec[0])
        dec_center = float(ra_dec[1])
        try:
            fov_w, fov_h = scope_ctrl.current_fov()
            radius = 0.5 * math.hypot(float(fov_w), float(fov_h))
        except Exception:
            radius = 5.0
        radius = float(max(2.0, min(45.0, radius)))
        manifest = coordinator.manifest()
        tiles = manifest.get_tiles_for_region(
            ra_center=ra_center,
            dec_center=dec_center,
            radius_deg=radius,
        )
        if not tiles:
            return "", tuple()

        primary_tile = manifest.get_primary_tile_for_region(
            ra_center=ra_center,
            dec_center=dec_center,
            radius_deg=radius,
        )
        if primary_tile is None:
            return "", tuple()
        visible_tile_ids = tuple(
            str(getattr(tile, "tile_id", "") or "")
            for tile in tiles
            if str(getattr(tile, "tile_id", "") or "")
        )
        return str(getattr(primary_tile, "tile_id", "") or ""), visible_tile_ids
    except Exception:
        return "", tuple()


def widget_ensure_scope_spatial_index_warmup(widget):
    """DEPRECATED: useu StarDataCoordinator.build_scope_index()."""
    emit_deprecation_warning(
        "TerraLab.ui.widget_bootstrap_helpers.widget_ensure_scope_spatial_index_warmup",
        "TerraLab.data.star_data_coordinator.StarDataCoordinator.build_scope_index",
    )
    self = widget
    if np is None:
        return
    # Refactor path: `StarDataCoordinator` ja construeix i publica index scope.
    # Evitem relancar el warm-up legacy (costos i duplicat).
    if getattr(self, "star_data_coordinator", None) is not None:
        return
    # Avoid parallel heavy jobs: preload already builds the persistent full index.
    if bool(getattr(self, "_scope_preload_in_progress", False)):
        return
    if time.monotonic() < float(
        getattr(self, "_scope_index_suspend_until", 0.0)
    ):
        return
    if self._scope_preload_should_wait() and (
        not bool(getattr(self, "_scope_preload_ready", False))
    ):
        return
    if (
        bool(getattr(self, "_scope_preload_ready", False))
        and self._apply_scope_preloaded_spatial_index()
    ):
        return
    if not self.canvas.scope_mode_enabled():
        return
    stars_renderer = getattr(
        getattr(self.canvas, "sky_renderer", None), "stars_renderer", None
    )
    if stars_renderer is None:
        return
    ra_all = getattr(self, "np_ra", None)
    dec_all = getattr(self, "np_dec", None)
    mag_all = getattr(self, "np_mag", None)
    if ra_all is None or dec_all is None:
        return
    target_mag_cap = float(self._scope_target_index_mag_cap())
    key = stars_renderer._catalog_array_key(ra_all, dec_all)
    if stars_renderer._scope_grid_key != key:
        self._scope_index_loaded_mag_cap = 0.0
    if (
        stars_renderer._scope_grid_key == key
        and stars_renderer._scope_grid_indices is not None
        and stars_renderer._scope_grid_offsets is not None
        and float(getattr(self, "_scope_index_loaded_mag_cap", 0.0))
        >= (target_mag_cap - 1e-3)
    ):
        return
    if getattr(self, "_scope_index_loading", False):
        if (
            target_mag_cap
            > float(getattr(self, "_scope_index_requested_mag_cap", 0.0))
            + 0.05
        ):
            self._scope_index_requested_mag_cap = float(target_mag_cap)
            self._scope_index_rewarm_requested = True
        return
    self._scope_index_loading = True
    self._scope_index_target_key = key
    self._scope_index_requested_mag_cap = float(target_mag_cap)
    self._scope_index_rewarm_requested = False
    stars_renderer.begin_scope_index_warmup(ra_all, dec_all)
    thread = QThread()
    worker = ScopeIndexWarmWorker()
    worker.moveToThread(thread)
    worker.ready.connect(
        lambda sorted_indices, offsets, ready_mag_cap, catalog_key=key: self._on_scope_spatial_index_ready(
            catalog_key,
            sorted_indices,
            offsets,
            ready_mag_cap,
        )
    )
    worker.ready.connect(thread.quit)
    worker.error.connect(self._on_scope_spatial_index_error)
    worker.error.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.started.connect(
        lambda w=worker, ra=ra_all, dec=dec_all, mag=mag_all, cap=target_mag_cap: w.build(
            ra,
            dec,
            mag,
            cap,
        )
    )
    self._scope_index_thread = thread
    self._scope_index_worker = worker
    thread.start()
    try:
        thread.setPriority(QThread.LowPriority)
    except Exception:
        log_suppressed_exception(__name__, "widget_ensure_scope_spatial_index_warmup")
    print(
        "[AstroWidget] Scope spatial index warm-up started "
        f"(<= {target_mag_cap:.2f} mag)."
    )


def widget_on_scope_extension_ready(
    widget, np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp, loaded_max_mag
):
    self = widget
    self._scope_catalog_loading = False
    try:
        scope_worker = getattr(self, "_scope_catalog_worker", None)
        scope_payload = getattr(scope_worker, "scope_extension_payload", None)
        if (
            isinstance(scope_payload, dict)
            and str(scope_payload.get("mode", "")) == "runtime_mmap_bundle"
        ):
            catalog_path = str(scope_payload.get("catalog_path", "") or "")
            r_path = str(scope_payload.get("r_path", "") or "")
            g_path = str(scope_payload.get("g_path", "") or "")
            b_path = str(scope_payload.get("b_path", "") or "")
            if not catalog_path or (not os.path.isfile(catalog_path)):
                raise RuntimeError("Missing runtime mmap catalog path")
            from TerraLab.data.catalogs.scope_cache import (
                ScopeRuntimeCacheManager,
            )

            cache_mgr = ScopeRuntimeCacheManager.from_cache_dir(
                os.path.dirname(catalog_path)
            )
            resolved = cache_mgr.resolve_bundle_payload_paths(
                catalog_path, r_path, g_path, b_path
            )
            catalog_path = str(resolved.get("catalog_path", "") or "")
            r_path = str(resolved.get("r_path", "") or "")
            g_path = str(resolved.get("g_path", "") or "")
            b_path = str(resolved.get("b_path", "") or "")
            keep_stamp = resolved.get("stamp")
            cache_mgr.cleanup(
                keep_stamps=1,
                keep_stamp=int(keep_stamp) if keep_stamp is not None else None,
                tmp_ttl_seconds=1.0,
            )

            arr = np.load(catalog_path, mmap_mode="r", allow_pickle=False)
            if (not isinstance(arr, np.ndarray)) or arr.dtype.names is None:
                raise RuntimeError("Invalid runtime mmap catalog format")
            names = set(arr.dtype.names or ())
            if not {"ra", "dec", "phot_g_mean_mag"}.issubset(names):
                raise RuntimeError(
                    "Runtime mmap catalog missing required fields"
                )
            self._scope_runtime_catalog_mmap = arr
            self.np_ra = np.asarray(arr["ra"])
            self.np_dec = np.asarray(arr["dec"])
            self.np_mag = np.asarray(arr["phot_g_mean_mag"])
            if "bp_rp" in names:
                self.np_bp_rp = np.asarray(arr["bp_rp"], dtype=np.float32)
            else:
                self.np_bp_rp = np.full(
                    int(len(self.np_mag)), 0.8, dtype=np.float32
                )
            rgb_fallback = None
            if r_path and os.path.isfile(r_path):
                self.np_r = np.load(r_path, mmap_mode="r", allow_pickle=False)
            else:
                if rgb_fallback is None:
                    rgb_fallback = _bp_rp_to_rgb_arrays(self.np_bp_rp)
                self.np_r = rgb_fallback[0]
            if g_path and os.path.isfile(g_path):
                self.np_g = np.load(g_path, mmap_mode="r", allow_pickle=False)
            else:
                if rgb_fallback is None:
                    rgb_fallback = _bp_rp_to_rgb_arrays(self.np_bp_rp)
                self.np_g = rgb_fallback[1]
            if b_path and os.path.isfile(b_path):
                self.np_b = np.load(b_path, mmap_mode="r", allow_pickle=False)
            else:
                if rgb_fallback is None:
                    rgb_fallback = _bp_rp_to_rgb_arrays(self.np_bp_rp)
                self.np_b = rgb_fallback[2]
            self._catalog_mag_sorted = bool(
                scope_payload.get("catalog_sorted", False)
            )
            self._catalog_loaded_subset_only = False
            self._scope_full_catalog_attached = True
            self._refresh_scope_data_state(reason="scope_extension_mmap_ready")
            rows = int(
                scope_payload.get("rows", len(self.np_ra)) or len(self.np_ra)
            )
            print(
                f"[AstroWidget] Scope extension attached via mmap bundle: "
                f"{rows} stars."
            )
            self._start_scope_full_preload_async(
                reason="scope_catalog_ready", force_rebuild=False
            )
            self.canvas._cached_star_image = None
            self.canvas._cached_trail_image = None
            if self.canvas.scope_mode_enabled():
                self._ensure_scope_spatial_index_warmup()
            self.canvas.update()
            try:
                max_hint = float(
                    scope_payload.get("loaded_max_mag", loaded_max_mag)
                    or loaded_max_mag
                )
                if np.isfinite(max_hint):
                    self._catalog_max_mag = max(
                        float(
                            getattr(
                                self,
                                "_catalog_max_mag",
                                STAR_CATALOG_NAKED_EYE_MAX_MAG,
                            )
                        ),
                        max_hint,
                    )
            except Exception:
                log_suppressed_exception(__name__, "widget_on_scope_extension_ready")
            self.refresh_light_pollution_catalog_range()
            self._set_gaia_extension_status_label(
                "Finalitzat", keep_seconds=20.0
            )
            return
        if np_ra is not None and len(np_ra) > 0:
            # Worker already merges base + extension in background to avoid blocking the UI thread.
            scope_mode = str(
                getattr(scope_worker, "last_scope_load_mode", "") or ""
            )
            self.np_ra = np_ra
            self.np_dec = np_dec
            self.np_mag = np_mag
            self.np_r = np_r
            self.np_g = np_g
            self.np_b = np_b
            self.np_bp_rp = np_bp_rp
            self._catalog_mag_sorted = bool(scope_mode == "sorted_output")
            self._catalog_loaded_subset_only = False
            self._scope_full_catalog_attached = True
            self._refresh_scope_data_state(reason="scope_extension_ready")
            print(
                f"[AstroWidget] Scope extension merged in background: {len(np_ra)} stars."
            )
            self._start_scope_full_preload_async(
                reason="scope_catalog_ready", force_rebuild=False
            )
            self.canvas._cached_star_image = None
            self.canvas._cached_trail_image = None
            if self.canvas.scope_mode_enabled():
                self._ensure_scope_spatial_index_warmup()
            self.canvas.update()
            try:
                max_hint = float(loaded_max_mag)
                if np.isfinite(max_hint):
                    self._catalog_max_mag = max(
                        float(
                            getattr(
                                self,
                                "_catalog_max_mag",
                                STAR_CATALOG_NAKED_EYE_MAX_MAG,
                            )
                        ),
                        max_hint,
                    )
            except Exception:
                log_suppressed_exception(__name__, "widget_on_scope_extension_ready")
            self.refresh_light_pollution_catalog_range()
            self._set_gaia_extension_status_label(
                "Finalitzat", keep_seconds=20.0
            )
        else:
            print("[AstroWidget] Scope extension already up to date.")
            ext_path = os.path.join(
                getattr(self, "_stars_catalog_dir", ""),
                "stars_catalog_extension.npy",
            )
            if os.path.isfile(ext_path):
                self._set_gaia_extension_status_label(
                    "Finalitzat", keep_seconds=20.0
                )
            self._refresh_scope_data_state(reason="scope_extension_uptodate")
    except Exception as e:
        print(f"[AstroWidget] Scope extension merge error: {e}")
        self._set_gaia_extension_status_label(
            f"Error carregant extensio: {e}", keep_seconds=20.0
        )
        self._scope_set_data_state(
            "error_deep", reason="scope_extension_error"
        )
    finally:
        try:
            self._scope_catalog_loaded_max_mag = max(
                float(
                    getattr(
                        self,
                        "_scope_catalog_loaded_max_mag",
                        STAR_CATALOG_NAKED_EYE_MAX_MAG,
                    )
                ),
                float(loaded_max_mag),
            )
        except Exception:
            log_suppressed_exception(__name__, "widget_on_scope_extension_ready")
        try:
            ext_path = os.path.join(
                getattr(self, "_stars_catalog_dir", ""),
                "stars_catalog_extension.npy",
            )
            if os.path.isfile(ext_path):
                self._gaia_extension_mtime_loaded = float(
                    os.path.getmtime(ext_path)
                )
        except Exception:
            log_suppressed_exception(__name__, "widget_on_scope_extension_ready")
        self._cleanup_scope_catalog_loader()
        if bool(getattr(self, "_scope_preload_pending_activation", False)):
            if not self.canvas.scope_mode_enabled():
                self._schedule_lifecycle_callback(
                    0, self.activate_scope_mode
                )
            else:
                self._scope_preload_pending_activation = False
        if self.canvas.scope_mode_enabled():
            self._schedule_lifecycle_callback(
                0, self._ensure_scope_spatial_index_warmup
            )
