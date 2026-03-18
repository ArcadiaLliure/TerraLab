"""Bootstrap/runtime helpers extracted from sky_widget_impl."""

from __future__ import annotations

def widget_start_scope_full_preload_async(widget, reason: str = "runtime", force_rebuild: bool = False):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = widget
    if str(getattr(self, "scope_preload_mode", "startup_full")) != "startup_full":
        return
    if bool(getattr(self, "_scope_preload_in_progress", False)):
        return
    if bool(getattr(self, "_scope_preload_ready", False)) and (not bool(force_rebuild)):
        self._apply_scope_preloaded_spatial_index()
        return
    if bool(getattr(self, "_scope_preload_started", False)) and (not bool(force_rebuild)):
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
        and
        ra_all is not None
        and dec_all is not None
        and mag_all is not None
        and int(ra_rows) > 0
        and int(ra_rows) == int(len(dec_all)) == int(len(mag_all))
    )
    try:
        in_memory_max_rows = int(
            max(
                100_000,
                int(get_config_value("performance.scope_preload_in_memory_max_rows", 5_000_000)),
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
        runtime_catalog_hint = os.path.join(str(getattr(self, "_stars_catalog_dir", "") or ""), "stars_catalog.npy")
        if runtime_catalog_hint and os.path.isfile(runtime_catalog_hint):
            runtime_npz = runtime_catalog_hint
        if use_in_memory:
            from TerraLab.data.stars_dataset import get_runtime_catalog_source_info
            source_path = str(get_runtime_catalog_source_info().get("source_path", "") or "")
            if source_path and os.path.isfile(source_path):
                runtime_npz = source_path
    except Exception as exc:
        print(f"[AstroWidget] Scope preload runtime path hint unavailable: {exc}")
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
        print(f"[AstroWidget] Scope preload using in-memory catalog ({len(ra_all)} stars).")
    elif subset_only:
        print("[AstroWidget] Scope preload forcing disk catalog (startup catalog is subset).")
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
            lambda w=worker, ra=ra_all, dec=dec_all, mag=mag_all, npz=runtime_npz, sdir=stars_dir, cdir=cache_dir, mm=max_mag, sv=self._scope_preload_schema_version, fr=bool(force_rebuild): w.run_from_arrays(
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
            lambda w=worker, npz=runtime_npz, sdir=stars_dir, cdir=cache_dir, mm=max_mag, sv=self._scope_preload_schema_version, fr=bool(force_rebuild): w.run(
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
        pass

def widget_start_async_bootstrap(widget):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = widget
    if getattr(self, "_async_bootstrap_started", False):
        return
    self._async_bootstrap_started = True
    try:
        from TerraLab.widgets.scope_runtime_cache import ScopeRuntimeCacheManager

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
        pass
    # --- Async Skyfield loading ---
    if SKYFIELD_AVAILABLE:
        self._skyfield_thread = QThread()
        self._skyfield_worker = SkyfieldLoaderWorker()
        self._skyfield_worker.moveToThread(self._skyfield_thread)
        self._skyfield_worker.skyfield_ready.connect(self._on_skyfield_ready)
        self._skyfield_thread.started.connect(self._skyfield_worker.load)
        self._skyfield_thread.start()
        try:
            self._skyfield_thread.setPriority(QThread.LowPriority)
        except Exception:
            pass
        print("[AstroWidget] Skyfield loading in background...")
    # --- Async Catalog loading ---
    if not bool(getattr(self, "defer_catalog_until_horizon_preview", True)):
        self._start_catalog_loader_async(reason="bootstrap")
    else:
        print("[AstroWidget] Star catalog loading deferred until horizon ready.")
        self._catalog_defer_t0 = time.perf_counter()
        QTimer.singleShot(15000, self._try_start_catalog_loader_deferred)
    # --- Horizon worker bootstrap ---
    from TerraLab.terrain.worker import HorizonWorker
    self.horizon_thread = QThread()
    self.horizon_worker = HorizonWorker()
    saved_offset = float(get_config_value("observer_offset", 0.0))
    self.horizon_worker.set_observer_offset(saved_offset)
    self.horizon_worker.moveToThread(self.horizon_thread)
    self.horizon_worker.profile_ready.connect(self.on_horizon_profile_ready)
    self.horizon_worker.preview_ready.connect(self.on_horizon_preview_ready)
    self.horizon_worker.progress_state.connect(self.on_horizon_progress_state)
    self.horizon_worker.error_occurred.connect(lambda err: print(f"[HorizonWorker] THREAD ERROR: {err}"))
    self.request_horizon_bake.connect(self.horizon_worker.request_bake)
    print("[AstroWidget] Starting Horizon Thread... (Path managed by Worker)")
    self.horizon_thread.start()
    try:
        self.horizon_thread.setPriority(QThread.LowPriority)
    except Exception:
        pass
    print(
        f"[AstroWidget] Horizon Thread started. ID: "
        f"{int(self.horizon_thread.currentThreadId()) if self.horizon_thread.currentThreadId() else 'N/A'}"
    )
    def trigger_bake():
        print(f"[AstroWidget] Emitting bake request for {self.latitude}, {self.longitude}")
        self._begin_horizon_bake()
    QTimer.singleShot(300, lambda: QMetaObject.invokeMethod(self.horizon_worker, "initialize", Qt.QueuedConnection))
    QTimer.singleShot(900, trigger_bake)

def widget_on_catalog_ready(widget, celestial_objects, np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = widget
    """Callback when star catalog finishes loading in background."""
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
                float(np.nanmax(np.asarray(np_mag, dtype=np.float32)))
                if np_mag is not None and len(np_mag) > 0
                else float(STAR_CATALOG_NAKED_EYE_MAX_MAG),
            )
        except Exception:
            pass
        try:
            if np_mag is not None and len(np_mag) > 0:
                self._catalog_max_mag = max(
                    float(getattr(self, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)),
                    float(np.nanmax(np.asarray(np_mag, dtype=np.float32))),
                )
        except Exception:
            pass
    array_rows = int(len(np_ra)) if np_ra is not None else 0
    named_rows = int(len(celestial_objects)) if celestial_objects is not None else 0
    fallback_active = False
    fallback_reason = ""
    self._catalog_loaded_subset_only = False
    try:
        worker = getattr(self, "_catalog_worker", None)
        load_mode = str(getattr(worker, "last_load_mode", "unknown") or "unknown")
        source_kind = str(getattr(worker, "last_source_kind", "unknown") or "unknown")
        total_rows_hint = int(getattr(worker, "last_total_rows", 0) or 0)
        catalog_max_hint = float(getattr(worker, "last_catalog_max_mag", float("nan")) or float("nan"))
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
                float(getattr(self, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)),
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
    self._refresh_scope_data_state(reason="catalog_ready")
    print(
        f"[AstroWidget] Star catalog ready (async): "
        f"catalog_rows={array_rows} named_rows={named_rows}"
    )
    append_perf_event(
        "catalog_ready",
        rows=int(array_rows),
        named_rows=int(named_rows),
        max_mag=float(getattr(self, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)),
        delta_ms_boot=self._boot_delta_ms(),
    )
    self._refresh_stars_status_indicator()
    self.build_search_index()
    # Do not start full scope preload during startup subset load.
    # Startup must stay responsive; preload is triggered on scope activation.
    self._apply_scope_preloaded_spatial_index()
    self._ensure_scope_catalog_loaded()
    if self.canvas.scope_mode_enabled():
        self._ensure_scope_spatial_index_warmup()
    if getattr(self, "scene_load_stage", "boot") in {"boot", "base_sky"}:
        self._set_scene_load_stage("stars_ready")
    self.canvas.update()
    # Clean up thread
    self._catalog_thread.quit()

def widget_ensure_scope_catalog_loaded(widget, force_now: bool = False):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = widget
    if np is None:
        return
    if getattr(self, "_scope_catalog_loading", False):
        return
    stars_dir = getattr(self, "_stars_catalog_dir", "")
    if not stars_dir or not os.path.isdir(stars_dir):
        return
    loaded_max_mag = float(getattr(self, "_scope_catalog_loaded_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG))
    catalog_max_mag = float(getattr(self, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG))
    force_runtime_full = False
    if bool(getattr(self, "_catalog_loaded_subset_only", False)):
        ext_npy = os.path.join(stars_dir, "stars_catalog_extension.npy")
        has_extension = os.path.isfile(ext_npy)
        if not has_extension:
            try:
                has_extension = any(
                    str(name).lower().endswith(".npz") and str(name).upper().startswith("MAGNITUD_")
                    for name in os.listdir(stars_dir)
                )
            except Exception:
                has_extension = False
        if not has_extension:
            force_runtime_full = True
    if force_runtime_full:
        scope_active = bool(getattr(getattr(self, "canvas", None), "scope_mode_enabled", lambda: False)())
        if (not bool(force_now)) and (not scope_active):
            return
    if (not force_runtime_full) and loaded_max_mag >= (catalog_max_mag - 1e-3):
        return
    self._scope_catalog_loading = True
    if force_runtime_full:
        self._scope_set_data_state("loading_deep", reason="scope_catalog_runtime_full")
    thread = QThread()
    worker = CatalogLoaderWorker()
    worker.moveToThread(thread)
    worker.scope_extension_ready.connect(self._on_scope_extension_ready)
    if hasattr(worker, "scope_extension_progress"):
        worker.scope_extension_progress.connect(self._on_scope_extension_progress)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.started.connect(
        lambda w=worker, s=stars_dir, m=loaded_max_mag, fr=bool(force_runtime_full): w.load_scope_extensions(
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
        pass
    if force_runtime_full:
        print("[AstroWidget] Scope full runtime catalog loading in background...")
    else:
        print(f"[AstroWidget] Scope catalog extension loading from > {loaded_max_mag:.2f} mag...")

def widget_ensure_scope_spatial_index_warmup(widget):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = widget
    if np is None:
        return
    # Avoid parallel heavy jobs: preload already builds the persistent full index.
    if bool(getattr(self, "_scope_preload_in_progress", False)):
        return
    if time.monotonic() < float(getattr(self, "_scope_index_suspend_until", 0.0)):
        return
    if self._scope_preload_should_wait() and (not bool(getattr(self, "_scope_preload_ready", False))):
        return
    if bool(getattr(self, "_scope_preload_ready", False)) and self._apply_scope_preloaded_spatial_index():
        return
    if not self.canvas.scope_mode_enabled():
        return
    stars_renderer = getattr(getattr(self.canvas, "sky_renderer", None), "stars_renderer", None)
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
        and float(getattr(self, "_scope_index_loaded_mag_cap", 0.0)) >= (target_mag_cap - 1e-3)
    ):
        return
    if getattr(self, "_scope_index_loading", False):
        if target_mag_cap > float(getattr(self, "_scope_index_requested_mag_cap", 0.0)) + 0.05:
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
        pass
    print(
        "[AstroWidget] Scope spatial index warm-up started "
        f"(<= {target_mag_cap:.2f} mag)."
    )

def widget_on_scope_extension_ready(widget, np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp, loaded_max_mag):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = widget
    self._scope_catalog_loading = False
    try:
        scope_worker = getattr(self, "_scope_catalog_worker", None)
        scope_payload = getattr(scope_worker, "scope_extension_payload", None)
        if isinstance(scope_payload, dict) and str(scope_payload.get("mode", "")) == "runtime_mmap_bundle":
            catalog_path = str(scope_payload.get("catalog_path", "") or "")
            r_path = str(scope_payload.get("r_path", "") or "")
            g_path = str(scope_payload.get("g_path", "") or "")
            b_path = str(scope_payload.get("b_path", "") or "")
            if not catalog_path or (not os.path.isfile(catalog_path)):
                raise RuntimeError("Missing runtime mmap catalog path")
            from TerraLab.widgets.scope_runtime_cache import ScopeRuntimeCacheManager

            cache_mgr = ScopeRuntimeCacheManager.from_cache_dir(os.path.dirname(catalog_path))
            resolved = cache_mgr.resolve_bundle_payload_paths(catalog_path, r_path, g_path, b_path)
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
                raise RuntimeError("Runtime mmap catalog missing required fields")
            self._scope_runtime_catalog_mmap = arr
            self.np_ra = np.asarray(arr["ra"])
            self.np_dec = np.asarray(arr["dec"])
            self.np_mag = np.asarray(arr["phot_g_mean_mag"])
            if "bp_rp" in names:
                self.np_bp_rp = np.asarray(arr["bp_rp"], dtype=np.float32)
            else:
                self.np_bp_rp = np.full(int(len(self.np_mag)), 0.8, dtype=np.float32)
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
            self._catalog_mag_sorted = bool(scope_payload.get("catalog_sorted", False))
            self._catalog_loaded_subset_only = False
            self._scope_full_catalog_attached = True
            self._refresh_scope_data_state(reason="scope_extension_mmap_ready")
            rows = int(scope_payload.get("rows", len(self.np_ra)) or len(self.np_ra))
            print(
                f"[AstroWidget] Scope extension attached via mmap bundle: "
                f"{rows} stars."
            )
            self._start_scope_full_preload_async(reason="scope_catalog_ready", force_rebuild=False)
            self.canvas._cached_star_image = None
            self.canvas._cached_trail_image = None
            if self.canvas.scope_mode_enabled():
                self._ensure_scope_spatial_index_warmup()
            self.canvas.update()
            try:
                max_hint = float(scope_payload.get("loaded_max_mag", loaded_max_mag) or loaded_max_mag)
                if np.isfinite(max_hint):
                    self._catalog_max_mag = max(
                        float(getattr(self, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)),
                        max_hint,
                    )
            except Exception:
                pass
            self._set_gaia_extension_status_label("Finalitzat", keep_seconds=20.0)
            return
        if np_ra is not None and len(np_ra) > 0:
            # Worker already merges base + extension in background to avoid blocking the UI thread.
            scope_mode = str(getattr(scope_worker, "last_scope_load_mode", "") or "")
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
            print(f"[AstroWidget] Scope extension merged in background: {len(np_ra)} stars.")
            self._start_scope_full_preload_async(reason="scope_catalog_ready", force_rebuild=False)
            self.canvas._cached_star_image = None
            self.canvas._cached_trail_image = None
            if self.canvas.scope_mode_enabled():
                self._ensure_scope_spatial_index_warmup()
            self.canvas.update()
            try:
                max_hint = float(loaded_max_mag)
                if np.isfinite(max_hint):
                    self._catalog_max_mag = max(
                        float(getattr(self, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)),
                        max_hint,
                    )
            except Exception:
                pass
            self._set_gaia_extension_status_label("Finalitzat", keep_seconds=20.0)
        else:
            print("[AstroWidget] Scope extension already up to date.")
            ext_path = os.path.join(getattr(self, "_stars_catalog_dir", ""), "stars_catalog_extension.npy")
            if os.path.isfile(ext_path):
                self._set_gaia_extension_status_label("Finalitzat", keep_seconds=20.0)
            self._refresh_scope_data_state(reason="scope_extension_uptodate")
    except Exception as e:
        print(f"[AstroWidget] Scope extension merge error: {e}")
        self._set_gaia_extension_status_label(f"Error carregant extensio: {e}", keep_seconds=20.0)
        self._scope_set_data_state("error_deep", reason="scope_extension_error")
    finally:
        try:
            self._scope_catalog_loaded_max_mag = max(
                float(getattr(self, "_scope_catalog_loaded_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)),
                float(loaded_max_mag),
            )
        except Exception:
            pass
        try:
            ext_path = os.path.join(getattr(self, "_stars_catalog_dir", ""), "stars_catalog_extension.npy")
            if os.path.isfile(ext_path):
                self._gaia_extension_mtime_loaded = float(os.path.getmtime(ext_path))
        except Exception:
            pass
        self._cleanup_scope_catalog_loader()
        if bool(getattr(self, "_scope_preload_pending_activation", False)):
            if not self.canvas.scope_mode_enabled():
                QTimer.singleShot(0, self.activate_scope_mode)
            else:
                self._scope_preload_pending_activation = False
        if self.canvas.scope_mode_enabled():
            QTimer.singleShot(0, self._ensure_scope_spatial_index_warmup)
