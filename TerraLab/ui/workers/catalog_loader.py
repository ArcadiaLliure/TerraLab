"""Asynchronous star catalogue loading worker."""

from __future__ import annotations

import os
import re
import time

import numpy as np

from PyQt5.QtCore import (
    QObject,
    pyqtSignal,
    pyqtSlot,
)

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.app_paths import data_dir as runtime_data_dir_for
from TerraLab.common.utils import get_config_value
from TerraLab.data.catalogs.constants import STAR_CATALOG_NAKED_EYE_MAX_MAG
from TerraLab.data.catalogs.scope_cache import ScopeRuntimeCacheManager
from TerraLab.data.catalogs.star_catalog import (
    _bp_rp_to_rgb_arrays,
    _build_celestial_objects_from_arrays,
    _discover_star_catalog_npz_entries,
    _load_star_npz_arrays,
    _load_structured_npy_subset_by_mag,
    _merge_sorted_catalog_with_no_gaia,
    _select_base_star_catalog_entry,
    _write_scope_runtime_mmap_bundle,
    _write_scope_runtime_mmap_bundle_from_structured_npy,
)
from TerraLab.data.stars_dataset import (
    ensure_stars_dataset,
    get_runtime_catalog_source_info,
    load_stars_dataset,
    log_startup_catalog_loaded,
)

NO_GAIA_STARS_JSON_NAME = "no_gaia_stars.json"
STAR_CATALOG_FILE_RE = re.compile(
    r"^MAGNITUD_(-?\d+(?:\.\d+)?)_(-?\d+(?:\.\d+)?)_(\d+)\.npz$",
    re.IGNORECASE,
)

class CatalogLoaderWorker(QObject):
    """Background worker to load star catalog without freezing UI."""

    catalog_ready = pyqtSignal(
        list, object, object, object, object, object, object, object
    )
    # (celestial_objects, np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp)
    scope_extension_ready = pyqtSignal(
        object, object, object, object, object, object, object, float
    )
    # (np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp, loaded_max_mag)
    scope_extension_progress = pyqtSignal(float, str)

    @pyqtSlot(str)
    def load(self, stars_dir):

        t0 = time.time()

        self.last_load_mode = "unknown"
        self.last_source_kind = "unknown"
        self.last_source_path = ""
        self.last_reason = ""
        self.last_total_rows = 0
        self.last_catalog_max_mag = float("nan")

        celestial_objects = []
        np_ra = np_dec = np_mag = np_r = np_g = np_b = np_bp_rp = None
        runtime_loaded = False

        # Preferred runtime path: APPDATA NPY.
        if np is not None:
            try:
                runtime_catalog_path = ensure_stars_dataset()

                fast_startup = bool(
                    get_config_value("performance.fast_startup_catalog", True)
                )
                fast_mag_limit = float(
                    get_config_value(
                        "performance.fast_startup_mag_limit",
                        STAR_CATALOG_NAKED_EYE_MAX_MAG,
                    )
                )
                fast_chunk_rows = int(
                    get_config_value(
                        "performance.fast_startup_chunk_rows", 2_000_000
                    )
                )

                mode = "runtime_full"
                source_id = None
                subset_payload = None

                if fast_startup and str(runtime_catalog_path).lower().endswith(
                    ".npy"
                ):
                    subset_payload = _load_structured_npy_subset_by_mag(
                        runtime_catalog_path,
                        max_mag=fast_mag_limit,
                        chunk_rows=fast_chunk_rows,
                    )

                if (
                    subset_payload is not None
                    and len(subset_payload["ra"]) > 0
                ):
                    ra = np.asarray(subset_payload["ra"], dtype=np.float32)
                    dec = np.asarray(subset_payload["dec"], dtype=np.float32)
                    mag = np.asarray(subset_payload["mag"], dtype=np.float32)
                    bp_rp = np.asarray(
                        subset_payload["bp_rp"], dtype=np.float32
                    )
                    source_id = subset_payload.get("source_id")
                    self.last_total_rows = int(
                        subset_payload.get("total_rows", len(ra))
                    )
                    self.last_catalog_max_mag = float(
                        subset_payload.get("full_mag_max", float("nan"))
                    )
                    mode = "runtime_subset"
                else:
                    runtime_ds = load_stars_dataset(runtime_catalog_path)
                    ra = np.asarray(runtime_ds["ra"], dtype=np.float32)
                    dec = np.asarray(runtime_ds["dec"], dtype=np.float32)
                    mag = np.asarray(
                        runtime_ds["phot_g_mean_mag"], dtype=np.float32
                    )
                    bp_rp = np.asarray(
                        runtime_ds.get("bp_rp"), dtype=np.float32
                    )
                    source_id = runtime_ds.get("source_id")
                    self.last_total_rows = int(len(ra))
                    try:
                        self.last_catalog_max_mag = (
                            float(np.nanmax(mag))
                            if len(mag) > 0
                            else float("nan")
                        )
                    except Exception:
                        self.last_catalog_max_mag = float("nan")
                    mode = "runtime_full"

                if len(ra) > 0:
                    order = np.argsort(mag, kind="mergesort")
                    np_ra = ra[order]
                    np_dec = dec[order]
                    np_mag = mag[order]
                    bp_rp = (
                        bp_rp[order]
                        if len(bp_rp) == len(order)
                        else np.full(len(order), 0.8, dtype=np.float32)
                    )
                    np_bp_rp = np.asarray(bp_rp, dtype=np.float32)
                    if source_id is not None and len(source_id) == len(order):
                        source_id = np.asarray(source_id)[order]
                    else:
                        source_id = None

                    (
                        np_ra,
                        np_dec,
                        np_mag,
                        np_bp_rp,
                        source_id,
                        added_no_gaia,
                    ) = _merge_sorted_catalog_with_no_gaia(
                        stars_dir,
                        np.asarray(np_ra, dtype=np.float32),
                        np.asarray(np_dec, dtype=np.float32),
                        np.asarray(np_mag, dtype=np.float32),
                        np.asarray(np_bp_rp, dtype=np.float32),
                        source_id=source_id,
                    )
                    bp_rp = np.asarray(np_bp_rp, dtype=np.float32)
                    np_r, np_g, np_b = _bp_rp_to_rgb_arrays(bp_rp)
                    # Avoid huge dict allocations for large datasets; keep arrays as source of truth.
                    if len(np_ra) <= 500_000:
                        celestial_objects = (
                            _build_celestial_objects_from_arrays(
                                np_ra,
                                np_dec,
                                np_mag,
                                bp_rp,
                                source_id=source_id,
                            )
                        )
                    else:
                        celestial_objects = []
                    runtime_loaded = True
                    self.last_load_mode = str(mode)
                    if mode == "runtime_subset":
                        print(
                            f"[CatalogLoader] Runtime subset loaded: {len(np_ra)} / {self.last_total_rows} stars "
                            f"(<= {fast_mag_limit:.2f} mag) from '{runtime_catalog_path}' in {time.time()-t0:.3f}s"
                        )
                    else:
                        print(
                            f"[CatalogLoader] Runtime dataset loaded: {len(np_ra)} stars "
                            f"from '{runtime_catalog_path}' in {time.time()-t0:.3f}s"
                        )
                    if added_no_gaia > 0:
                        print(
                            f"[CatalogLoader] Added no-Gaia supplement: +{added_no_gaia} bright stars "
                            f"from '{NO_GAIA_STARS_JSON_NAME}'"
                        )
                    try:
                        src_info = get_runtime_catalog_source_info()
                        src_kind = str(src_info.get("source", "unknown"))
                        src_path = str(src_info.get("source_path", "") or "")
                        self.last_source_kind = src_kind
                        self.last_source_path = src_path
                        mag_min = (
                            float(np.min(np_mag))
                            if len(np_mag) > 0
                            else float("nan")
                        )
                        mag_max = (
                            float(np.max(np_mag))
                            if len(np_mag) > 0
                            else float("nan")
                        )
                        if np.isfinite(self.last_catalog_max_mag):
                            mag_max = float(
                                max(mag_max, self.last_catalog_max_mag)
                            )
                        print(
                            "[CatalogLoader] Startup source: "
                            f"source={src_kind} "
                            f"source_path='{src_path}' "
                            f"rows={len(np_ra)} "
                            f"mag_min={mag_min:.6f} "
                            f"mag_max={mag_max:.6f}"
                        )
                        log_startup_catalog_loaded(
                            runtime_catalog_path=str(runtime_catalog_path),
                            rows=int(len(np_ra)),
                            mag_min=mag_min,
                            mag_max=mag_max,
                        )
                    except Exception as log_exc:
                        print(
                            f"[CatalogLoader] Startup source log failed: {log_exc}"
                        )
            except Exception as e:
                print(f"[CatalogLoader] Runtime dataset path unavailable: {e}")

        entries = _discover_star_catalog_npz_entries(stars_dir)
        base_entry = _select_base_star_catalog_entry(
            entries, max_mag=STAR_CATALOG_NAKED_EYE_MAX_MAG
        )

        if np is not None and (not runtime_loaded) and base_entry is not None:
            try:
                base = _load_star_npz_arrays(
                    base_entry["path"],
                    max_mag=STAR_CATALOG_NAKED_EYE_MAX_MAG,
                )
                if len(base["ra"]) > 0:
                    order = np.argsort(base["mag"], kind="mergesort")
                    ra = base["ra"][order]
                    dec = base["dec"][order]
                    mag = base["mag"][order]
                    bp_rp = base["bp_rp"][order]
                    np_bp_rp = np.asarray(bp_rp, dtype=np.float32)
                    source_id = (
                        base["source_id"][order]
                        if base["source_id"] is not None
                        else None
                    )

                    np_ra = np.asarray(ra, dtype=np.float32)
                    np_dec = np.asarray(dec, dtype=np.float32)
                    np_mag = np.asarray(mag, dtype=np.float32)
                    np_ra, np_dec, np_mag, np_bp_rp, source_id, _ = (
                        _merge_sorted_catalog_with_no_gaia(
                            stars_dir,
                            np.asarray(np_ra, dtype=np.float32),
                            np.asarray(np_dec, dtype=np.float32),
                            np.asarray(np_mag, dtype=np.float32),
                            np.asarray(np_bp_rp, dtype=np.float32),
                            source_id=source_id,
                        )
                    )
                    bp_rp = np.asarray(np_bp_rp, dtype=np.float32)
                    np_r, np_g, np_b = _bp_rp_to_rgb_arrays(bp_rp)
                    celestial_objects = _build_celestial_objects_from_arrays(
                        np_ra, np_dec, np_mag, bp_rp, source_id=source_id
                    )
                    runtime_loaded = True
                    self.last_load_mode = "base_npz"
                    self.last_source_kind = "base_npz"
                    self.last_source_path = str(base_entry["path"])
                    print(
                        f"[CatalogLoader] Loaded base NPZ '{os.path.basename(base_entry['path'])}' "
                        f"({len(np_ra)} stars) in {time.time()-t0:.3f}s"
                    )
            except Exception as e:
                print(f"[CatalogLoader] Error loading base NPZ: {e}")
        elif (not runtime_loaded) and base_entry is None:
            print(f"[CatalogLoader] No MAGNITUD_*.npz found in: {stars_dir}")

        if (not runtime_loaded) and not celestial_objects:
            print("[CatalogLoader] Fallback to random stars")
            self.last_load_mode = "random_fallback"
            self.last_source_kind = "random_fallback"
            self.last_reason = "no_catalog_source"
            import random as _rnd

            for _ in range(500):
                celestial_objects.append(
                    {
                        "id": "rnd",
                        "name": "",
                        "ra": _rnd.uniform(0, 360),
                        "dec": _rnd.uniform(-90, 90),
                        "mag": _rnd.uniform(1.0, 6.0),
                        "bp_rp": _rnd.uniform(-0.5, 2.0),
                    }
                )
            celestial_objects.sort(key=lambda x: x["mag"])
            if np is not None:
                np_ra = np.array(
                    [s["ra"] for s in celestial_objects], dtype=np.float32
                )
                np_dec = np.array(
                    [s["dec"] for s in celestial_objects], dtype=np.float32
                )
                np_mag = np.array(
                    [s["mag"] for s in celestial_objects], dtype=np.float32
                )
                np_bp_rp = np.array(
                    [s["bp_rp"] for s in celestial_objects], dtype=np.float32
                )
                np_r, np_g, np_b = _bp_rp_to_rgb_arrays(np_bp_rp)

        self.catalog_ready.emit(
            celestial_objects,
            np_ra,
            np_dec,
            np_mag,
            np_r,
            np_g,
            np_b,
            np_bp_rp,
        )

    @pyqtSlot(
        str,
        float,
        object,
        object,
        object,
        object,
        object,
        object,
        object,
        bool,
    )
    def load_scope_extensions(
        self,
        stars_dir,
        loaded_max_mag,
        base_ra=None,
        base_dec=None,
        base_mag=None,
        base_r=None,
        base_g=None,
        base_b=None,
        base_bp_rp=None,
        force_runtime_full=False,
    ):

        t0 = time.time()
        self.last_scope_load_mode = "unknown"
        self.scope_extension_payload = None

        np_ra = np_dec = np_mag = np_r = np_g = np_b = np_bp_rp = None
        max_loaded = float(loaded_max_mag)
        if np is None:
            self.last_scope_load_mode = "numpy_unavailable"
            self.scope_extension_ready.emit(
                None, None, None, None, None, None, None, max_loaded
            )
            return

        def _scope_cache_signature(
            paths, *, include_no_gaia: bool = False
        ) -> str:
            source_paths = [
                str(p) for p in (paths or []) if str(p or "").strip()
            ]
            if include_no_gaia:
                source_paths.extend(_no_gaia_signature_sources())
            return ScopeRuntimeCacheManager.build_dataset_signature(
                source_paths,
                extra={
                    "cache_kind": "scope_runtime_bundle",
                    "policy_version": 1,
                    "include_no_gaia": bool(include_no_gaia),
                },
            )

        def _no_gaia_signature_sources() -> list[str]:
            candidates: list[str] = []
            if stars_dir:
                candidates.append(
                    os.path.join(stars_dir, NO_GAIA_STARS_JSON_NAME)
                )
                candidates.append(
                    os.path.join(stars_dir, "stars_catalog_no_gaia_fused.flag")
                )
            try:
                candidates.append(
                    os.path.join(
                        str(runtime_data_dir_for("gaia")),
                        NO_GAIA_STARS_JSON_NAME,
                    )
                )
            except Exception:
                log_suppressed_exception(__name__, "CatalogLoaderWorker.load_scope_extensions._no_gaia_signature_sources")
            candidates.append(
                os.path.abspath(
                    os.path.join(
                        os.path.dirname(__file__),
                        "..",
                        "data",
                        "stars",
                        NO_GAIA_STARS_JSON_NAME,
                    )
                )
            )
            out = []
            for candidate in candidates:
                if candidate and os.path.isfile(candidate):
                    out.append(candidate)
            return out

        if bool(force_runtime_full):
            try:
                self.scope_extension_progress.emit(
                    1.0, "Carregant cataleg complet per mode scope..."
                )
                runtime_catalog_path = ensure_stars_dataset()
                runtime_suffix = str(runtime_catalog_path).lower()
                if runtime_suffix.endswith(".npy"):
                    try:
                        full_signature = _scope_cache_signature(
                            [str(runtime_catalog_path)],
                            include_no_gaia=False,
                        )
                        bundle = _write_scope_runtime_mmap_bundle_from_structured_npy(
                            stars_dir=stars_dir,
                            runtime_catalog_path=runtime_catalog_path,
                            chunk_rows=1_000_000,
                            progress_callback=self.scope_extension_progress.emit,
                            dataset_signature=full_signature,
                        )
                        rows = int(bundle.get("rows", 0) or 0)
                        max_hint = float(
                            bundle.get("loaded_max_mag", float("nan"))
                        )
                        if np.isfinite(max_hint):
                            max_loaded = max(max_loaded, max_hint)
                        self.scope_extension_progress.emit(
                            100.0, "Cataleg scope complet carregat"
                        )
                        self.scope_extension_payload = {
                            "mode": "runtime_mmap_bundle",
                            "catalog_sorted": False,
                            "rows": rows,
                            "loaded_max_mag": float(max_loaded),
                            "source_path": str(runtime_catalog_path),
                            **{
                                k: bundle[k]
                                for k in (
                                    "catalog_path",
                                    "r_path",
                                    "g_path",
                                    "b_path",
                                )
                            },
                        }
                        self.last_scope_load_mode = "runtime_mmap_bundle"
                        print(
                            f"[CatalogLoader] Scope full runtime catalog mapped: {rows} stars "
                            f"from '{runtime_catalog_path}' in {time.time()-t0:.3f}s"
                        )
                        self.scope_extension_ready.emit(
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            float(max_loaded),
                        )
                        return
                    except Exception as mmap_exc:
                        print(
                            f"[CatalogLoader] Scope runtime mmap path failed: {mmap_exc}"
                        )
                runtime_ds = load_stars_dataset(runtime_catalog_path)
                ra = np.asarray(runtime_ds["ra"], dtype=np.float32)
                dec = np.asarray(runtime_ds["dec"], dtype=np.float32)
                mag = np.asarray(
                    runtime_ds["phot_g_mean_mag"], dtype=np.float32
                )
                bp_raw = runtime_ds.get("bp_rp")
                if bp_raw is None:
                    bp_rp = np.full(len(mag), 0.8, dtype=np.float32)
                else:
                    bp_rp = np.asarray(bp_raw, dtype=np.float32)
                    if bp_rp.ndim == 0 or len(bp_rp) != len(mag):
                        bp_rp = np.full(len(mag), 0.8, dtype=np.float32)
                source_id = runtime_ds.get("source_id")
                if source_id is not None:
                    source_id = np.asarray(source_id)
                    if source_id.ndim == 0 or len(source_id) != len(mag):
                        source_id = None

                valid = np.isfinite(ra) & np.isfinite(dec) & np.isfinite(mag)
                if not np.all(valid):
                    ra = np.asarray(ra[valid], dtype=np.float32)
                    dec = np.asarray(dec[valid], dtype=np.float32)
                    mag = np.asarray(mag[valid], dtype=np.float32)
                    bp_rp = np.asarray(bp_rp[valid], dtype=np.float32)
                    if source_id is not None:
                        source_id = np.asarray(source_id[valid])

                if len(ra) > 0:
                    order = np.argsort(mag, kind="mergesort")
                    np_ra = np.asarray(ra[order], dtype=np.float32)
                    np_dec = np.asarray(dec[order], dtype=np.float32)
                    np_mag = np.asarray(mag[order], dtype=np.float32)
                    np_bp_rp = np.asarray(bp_rp[order], dtype=np.float32)
                    if source_id is not None and len(source_id) == len(order):
                        source_id = np.asarray(source_id)[order]
                    else:
                        source_id = None

                    (
                        np_ra,
                        np_dec,
                        np_mag,
                        np_bp_rp,
                        source_id,
                        added_no_gaia,
                    ) = _merge_sorted_catalog_with_no_gaia(
                        stars_dir,
                        np.asarray(np_ra, dtype=np.float32),
                        np.asarray(np_dec, dtype=np.float32),
                        np.asarray(np_mag, dtype=np.float32),
                        np.asarray(np_bp_rp, dtype=np.float32),
                        source_id=source_id,
                    )
                    np_r, np_g, np_b = _bp_rp_to_rgb_arrays(np_bp_rp)
                    max_loaded = max(
                        max_loaded,
                        float(np.nanmax(np.asarray(np_mag, dtype=np.float32))),
                    )
                    self.scope_extension_progress.emit(
                        100.0, "Cataleg scope complet carregat"
                    )
                    print(
                        f"[CatalogLoader] Scope full runtime catalog loaded: {len(np_ra)} stars "
                        f"from '{runtime_catalog_path}' in {time.time()-t0:.3f}s"
                    )
                    if added_no_gaia > 0:
                        print(
                            f"[CatalogLoader] Added no-Gaia supplement: +{added_no_gaia} bright stars "
                            f"from '{NO_GAIA_STARS_JSON_NAME}'"
                        )
                    try:
                        full_signature = _scope_cache_signature(
                            [str(runtime_catalog_path)],
                            include_no_gaia=True,
                        )
                        bundle = _write_scope_runtime_mmap_bundle(
                            stars_dir=stars_dir,
                            source_id=source_id,
                            ra=np_ra,
                            dec=np_dec,
                            mag=np_mag,
                            bp_rp=np_bp_rp,
                            r_arr=np_r,
                            g_arr=np_g,
                            b_arr=np_b,
                            dataset_signature=full_signature,
                        )
                        self.scope_extension_payload = {
                            "mode": "runtime_mmap_bundle",
                            "catalog_sorted": True,
                            "rows": int(len(np_ra)),
                            "loaded_max_mag": float(max_loaded),
                            **bundle,
                        }
                        self.last_scope_load_mode = "runtime_mmap_bundle"
                        print(
                            f"[CatalogLoader] Scope runtime mmap bundle written: "
                            f"rows={len(np_ra)} in {time.time()-t0:.3f}s"
                        )
                        self.scope_extension_ready.emit(
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            float(max_loaded),
                        )
                        return
                    except Exception as bundle_exc:
                        print(
                            f"[CatalogLoader] Scope mmap bundle write error: {bundle_exc}"
                        )
                    self.last_scope_load_mode = "sorted_output"
                    self.scope_extension_ready.emit(
                        np_ra,
                        np_dec,
                        np_mag,
                        np_r,
                        np_g,
                        np_b,
                        np_bp_rp,
                        float(max_loaded),
                    )
                    return
            except Exception as e:
                print(f"[CatalogLoader] Scope full runtime load error: {e}")

        runtime_extension_npy = ""
        if stars_dir:
            runtime_extension_npy = os.path.join(
                stars_dir, "stars_catalog_extension.npy"
            )
        if runtime_extension_npy and os.path.isfile(runtime_extension_npy):
            try:
                arr = np.load(
                    runtime_extension_npy, mmap_mode="r", allow_pickle=False
                )
                if not isinstance(arr, np.ndarray) or arr.dtype.names is None:
                    raise ValueError(
                        "stars_catalog_extension.npy is not a structured NPY"
                    )
                names = set(arr.dtype.names or ())
                if not {"ra", "dec", "phot_g_mean_mag"}.issubset(names):
                    raise ValueError(
                        "stars_catalog_extension.npy missing required columns"
                    )

                total_rows = int(len(arr))
                chunk_rows = 1_500_000
                chunks_ra = []
                chunks_dec = []
                chunks_mag = []
                chunks_bp = []

                self.scope_extension_progress.emit(
                    0.0, "Muntant memòria cau d'estrelles..."
                )
                for start in range(0, total_rows, chunk_rows):
                    end = min(total_rows, start + chunk_rows)
                    ra_chunk = np.asarray(
                        arr["ra"][start:end], dtype=np.float32
                    )
                    dec_chunk = np.asarray(
                        arr["dec"][start:end], dtype=np.float32
                    )
                    mag_chunk = np.asarray(
                        arr["phot_g_mean_mag"][start:end], dtype=np.float32
                    )
                    if "bp_rp" in names:
                        bp_chunk = np.asarray(
                            arr["bp_rp"][start:end], dtype=np.float32
                        )
                    else:
                        bp_chunk = np.full(
                            len(mag_chunk), 0.8, dtype=np.float32
                        )

                    mask = (
                        np.isfinite(ra_chunk)
                        & np.isfinite(dec_chunk)
                        & np.isfinite(mag_chunk)
                        & (mag_chunk > float(loaded_max_mag) + 1e-6)
                    )
                    if np.any(mask):
                        chunks_ra.append(ra_chunk[mask])
                        chunks_dec.append(dec_chunk[mask])
                        chunks_mag.append(mag_chunk[mask])
                        chunks_bp.append(bp_chunk[mask])

                    pct = 100.0 * (float(end) / float(max(1, total_rows)))
                    self.scope_extension_progress.emit(
                        min(99.0, pct),
                        f"Carregant extensio ({int(round(pct))}%)",
                    )

                if chunks_ra:
                    np_ra = np.concatenate(chunks_ra)
                    np_dec = np.concatenate(chunks_dec)
                    np_mag = np.concatenate(chunks_mag)
                    np_bp_rp = np.concatenate(chunks_bp)

                    order = np.argsort(np_mag, kind="mergesort")
                    np_ra = np.asarray(np_ra[order], dtype=np.float32)
                    np_dec = np.asarray(np_dec[order], dtype=np.float32)
                    np_mag = np.asarray(np_mag[order], dtype=np.float32)
                    np_bp_rp = np.asarray(np_bp_rp[order], dtype=np.float32)
                    np_r, np_g, np_b = _bp_rp_to_rgb_arrays(np_bp_rp)
                    max_loaded = max(max_loaded, float(np.nanmax(np_mag)))
                self.scope_extension_progress.emit(100.0, "Extensio carregada")
            except Exception as e:
                print(f"[CatalogLoader] Scope extension NPY load error: {e}")

        entries = _discover_star_catalog_npz_entries(stars_dir)
        eps = 1e-6
        targets = [
            e
            for e in entries
            if float(e["max_mag"]) > float(loaded_max_mag) + eps
        ]
        if (np_ra is None or len(np_ra) == 0) and (not targets):
            self.last_scope_load_mode = "up_to_date"
            self.scope_extension_ready.emit(
                None, None, None, None, None, None, None, max_loaded
            )
            return

        chunks_ra = []
        chunks_dec = []
        chunks_mag = []
        chunks_r = []
        chunks_g = []
        chunks_b = []
        chunks_bp_rp = []

        if np_ra is not None and len(np_ra) > 0:
            chunks_ra.append(np.asarray(np_ra, dtype=np.float32))
            chunks_dec.append(np.asarray(np_dec, dtype=np.float32))
            chunks_mag.append(np.asarray(np_mag, dtype=np.float32))
            chunks_r.append(np.asarray(np_r, dtype=np.float32))
            chunks_g.append(np.asarray(np_g, dtype=np.float32))
            chunks_b.append(np.asarray(np_b, dtype=np.float32))
            chunks_bp_rp.append(np.asarray(np_bp_rp, dtype=np.float32))

        for entry in targets:
            try:
                arr = _load_star_npz_arrays(
                    entry["path"],
                    min_mag_exclusive=float(loaded_max_mag),
                )
                if len(arr["ra"]) == 0:
                    continue
                rr, gg, bb = _bp_rp_to_rgb_arrays(arr["bp_rp"])
                chunks_ra.append(np.asarray(arr["ra"], dtype=np.float32))
                chunks_dec.append(np.asarray(arr["dec"], dtype=np.float32))
                chunks_mag.append(np.asarray(arr["mag"], dtype=np.float32))
                chunks_r.append(rr)
                chunks_g.append(gg)
                chunks_b.append(bb)
                chunks_bp_rp.append(np.asarray(arr["bp_rp"], dtype=np.float32))
                max_loaded = max(
                    max_loaded, float(entry.get("max_mag", loaded_max_mag))
                )
                print(
                    f"[CatalogLoader] Scope chunk '{os.path.basename(entry['path'])}' "
                    f"loaded ({len(arr['ra'])} stars)"
                )
            except Exception as e:
                print(
                    f"[CatalogLoader] Scope chunk load error '{entry.get('name', '')}': {e}"
                )

        if chunks_ra:
            np_ra = np.concatenate(chunks_ra)
            np_dec = np.concatenate(chunks_dec)
            np_mag = np.concatenate(chunks_mag)
            np_r = np.concatenate(chunks_r)
            np_g = np.concatenate(chunks_g)
            np_b = np.concatenate(chunks_b)
            np_bp_rp = np.concatenate(chunks_bp_rp)

            if (
                base_ra is not None
                and base_dec is not None
                and base_mag is not None
            ):
                try:
                    b_ra = np.asarray(base_ra, dtype=np.float32)
                    b_dec = np.asarray(base_dec, dtype=np.float32)
                    b_mag = np.asarray(base_mag, dtype=np.float32)
                    b_r = (
                        np.asarray(base_r, dtype=np.float32)
                        if base_r is not None
                        else None
                    )
                    b_g = (
                        np.asarray(base_g, dtype=np.float32)
                        if base_g is not None
                        else None
                    )
                    b_b = (
                        np.asarray(base_b, dtype=np.float32)
                        if base_b is not None
                        else None
                    )
                    b_bp = (
                        np.asarray(base_bp_rp, dtype=np.float32)
                        if base_bp_rp is not None
                        else None
                    )
                    if (
                        b_r is not None
                        and b_g is not None
                        and b_b is not None
                        and len(b_ra)
                        == len(b_dec)
                        == len(b_mag)
                        == len(b_r)
                        == len(b_g)
                        == len(b_b)
                    ):
                        np_ra = np.concatenate((b_ra, np_ra))
                        np_dec = np.concatenate((b_dec, np_dec))
                        np_mag = np.concatenate((b_mag, np_mag))
                        np_r = np.concatenate((b_r, np_r))
                        np_g = np.concatenate((b_g, np_g))
                        np_b = np.concatenate((b_b, np_b))
                        if b_bp is not None and len(b_bp) == len(b_mag):
                            np_bp_rp = np.concatenate((b_bp, np_bp_rp))
                except Exception as merge_exc:
                    print(
                        f"[CatalogLoader] Scope extension merge-with-base warning: {merge_exc}"
                    )

            print(
                f"[CatalogLoader] Scope extension ready: {len(np_ra)} stars "
                f"in {time.time()-t0:.3f}s (max mag {max_loaded:.2f})"
            )
            self.last_scope_load_mode = "sorted_output"
            try:
                extension_sources = []
                if runtime_extension_npy and os.path.isfile(
                    runtime_extension_npy
                ):
                    extension_sources.append(runtime_extension_npy)
                try:
                    base_entry = _select_base_star_catalog_entry(
                        entries,
                        max_mag=STAR_CATALOG_NAKED_EYE_MAX_MAG,
                    )
                except Exception:
                    base_entry = None
                if isinstance(base_entry, dict):
                    base_path = str(base_entry.get("path", "") or "")
                    if base_path and os.path.isfile(base_path):
                        extension_sources.append(base_path)
                for entry in targets:
                    try:
                        p = str(entry.get("path", "") or "")
                    except Exception:
                        p = ""
                    if p and os.path.isfile(p):
                        extension_sources.append(p)
                extension_signature = _scope_cache_signature(
                    extension_sources,
                    include_no_gaia=True,
                )
                bundle = _write_scope_runtime_mmap_bundle(
                    stars_dir=stars_dir,
                    source_id=None,
                    ra=np_ra,
                    dec=np_dec,
                    mag=np_mag,
                    bp_rp=np_bp_rp,
                    r_arr=np_r,
                    g_arr=np_g,
                    b_arr=np_b,
                    dataset_signature=extension_signature,
                )
                self.scope_extension_payload = {
                    "mode": "runtime_mmap_bundle",
                    "catalog_sorted": True,
                    "rows": int(len(np_ra)),
                    "loaded_max_mag": float(max_loaded),
                    **bundle,
                }
                self.last_scope_load_mode = "runtime_mmap_bundle"
                self.scope_extension_ready.emit(
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    float(max_loaded),
                )
                return
            except Exception as bundle_exc:
                print(
                    f"[CatalogLoader] Scope mmap bundle write error: {bundle_exc}"
                )
        else:
            self.last_scope_load_mode = "no_rows"

        self.scope_extension_ready.emit(
            np_ra,
            np_dec,
            np_mag,
            np_r,
            np_g,
            np_b,
            np_bp_rp,
            float(max_loaded),
        )


