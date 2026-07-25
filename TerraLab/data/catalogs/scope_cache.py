"""Persistent scope runtime cache for memory-mapped star bundles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

import numpy as np
from TerraLab.common.exception_reporting import log_suppressed_exception


class ScopeRuntimeCacheManager:
    """Centralized manager for scope runtime cache lifecycle."""

    _BUNDLE_RE = re.compile(
        r"^scope_runtime_sorted_(catalog|r|g|b)_(\d+)\.npy$",
        re.IGNORECASE,
    )
    _BUNDLE_TMP_RE = re.compile(
        r"^scope_runtime_sorted_(catalog|r|g|b)_(\d+)\.npy\.tmp$",
        re.IGNORECASE,
    )
    _CATALOG_RE = re.compile(
        r"^scope_runtime_sorted_catalog_(\d+)\.npy$",
        re.IGNORECASE,
    )
    _META_RE = re.compile(
        r"^scope_runtime_bundle_meta_(\d+)\.json$",
        re.IGNORECASE,
    )
    _SCHEMA_VERSION = 1

    def __init__(
        self, *, stars_dir: str | None = None, cache_dir: str | None = None
    ):
        resolved_cache = ""
        if cache_dir:
            resolved_cache = os.path.abspath(str(cache_dir))
        elif stars_dir:
            resolved_cache = os.path.join(
                os.path.abspath(str(stars_dir)), "cache", "scope"
            )
        self.cache_dir = resolved_cache

    def prepare(self) -> None:
        """Create the cache directory immediately before an explicit write."""

        if not self.cache_dir:
            raise RuntimeError(
                "Scope runtime cache directory is not configured"
            )
        os.makedirs(self.cache_dir, exist_ok=True)

    @classmethod
    def from_cache_dir(cls, cache_dir: str) -> "ScopeRuntimeCacheManager":
        """Executa el metode from_cache_dir de la classe ScopeRuntimeCacheManager.

        Par?metres:
        - cache_dir (str): Valor del parametre 'cache_dir'.

        Retorna:
        - "ScopeRuntimeCacheManager": Valor retornat pel metode.
        """
        return cls(cache_dir=cache_dir)

    @classmethod
    def build_dataset_signature(
        cls,
        source_paths: Sequence[str] | None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Executa el metode build_dataset_signature de la classe ScopeRuntimeCacheManager.

        Par?metres:
        - source_paths (Sequence[str] | None): Valor del parametre 'source_paths'.
        - extra (dict[str, Any] | None): Valor del parametre 'extra'.

        Retorna:
        - str: Valor retornat pel metode.
        """
        path_signatures: list[dict[str, Any]] = []
        for path in source_paths or ():
            sig = cls._path_signature(path)
            if sig is not None:
                path_signatures.append(sig)
        path_signatures.sort(key=lambda item: str(item.get("path", "")))
        payload = {
            "schema": int(cls._SCHEMA_VERSION),
            "sources": path_signatures,
            "extra": dict(extra or {}),
        }
        raw = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _path_signature(path_value: str | None) -> dict[str, Any] | None:
        path = str(path_value or "").strip()
        if not path or (not os.path.isfile(path)):
            return None
        try:
            st = os.stat(path)
        except Exception:
            return None
        return {
            "path": os.path.abspath(path),
            "size": int(st.st_size),
            "mtime_ns": int(
                getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
            ),
        }

    @staticmethod
    def _safe_remove(path_to_remove: str) -> None:
        for retry in range(3):
            try:
                os.remove(path_to_remove)
                return
            except FileNotFoundError:
                return
            except PermissionError:
                try:
                    os.chmod(path_to_remove, 0o666)
                except Exception:
                    log_suppressed_exception(__name__, "ScopeRuntimeCacheManager._safe_remove")
                time.sleep(0.05 * float(retry + 1))
            except Exception:
                return
        if os.name == "nt":
            try:
                import ctypes

                MOVEFILE_DELAY_UNTIL_REBOOT = 0x00000004
                ctypes.windll.kernel32.MoveFileExW(
                    str(path_to_remove),
                    None,
                    MOVEFILE_DELAY_UNTIL_REBOOT,
                )
            except Exception:
                log_suppressed_exception(__name__, "ScopeRuntimeCacheManager._safe_remove")

    @classmethod
    def extract_stamp(cls, catalog_path: str) -> int | None:
        """Executa el metode extract_stamp de la classe ScopeRuntimeCacheManager.

        Par?metres:
        - catalog_path (str): Valor del parametre 'catalog_path'.

        Retorna:
        - int | None: Valor retornat pel metode.
        """
        base_name = os.path.basename(str(catalog_path or ""))
        match = cls._CATALOG_RE.match(base_name)
        if not match:
            return None
        try:
            return int(match.group(1))
        except Exception:
            return None

    def bundle_paths(self, stamp: int) -> dict[str, str]:
        """Executa el metode bundle_paths de la classe ScopeRuntimeCacheManager.

        Par?metres:
        - stamp (int): Valor del parametre 'stamp'.

        Retorna:
        - dict[str, str]: Valor retornat pel metode.
        """
        base = str(self.cache_dir or "")
        return {
            "catalog_path": os.path.join(
                base, f"scope_runtime_sorted_catalog_{int(stamp)}.npy"
            ),
            "r_path": os.path.join(
                base, f"scope_runtime_sorted_r_{int(stamp)}.npy"
            ),
            "g_path": os.path.join(
                base, f"scope_runtime_sorted_g_{int(stamp)}.npy"
            ),
            "b_path": os.path.join(
                base, f"scope_runtime_sorted_b_{int(stamp)}.npy"
            ),
        }

    def meta_path(self, stamp: int) -> str:
        """Executa el metode meta_path de la classe ScopeRuntimeCacheManager.

        Par?metres:
        - stamp (int): Valor del parametre 'stamp'.

        Retorna:
        - str: Valor retornat pel metode.
        """
        return os.path.join(
            str(self.cache_dir or ""),
            f"scope_runtime_bundle_meta_{int(stamp)}.json",
        )

    def _read_meta(self, stamp: int) -> dict[str, Any]:
        path = self.meta_path(stamp)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                return payload
        except Exception:
            return {}
        return {}

    def _write_meta(
        self,
        stamp: int,
        *,
        dataset_signature: str,
        rows: int,
        loaded_max_mag: float,
    ) -> None:
        path = self.meta_path(stamp)
        tmp_name = f"{path}.{int(os.getpid())}.{int(time.time() * 1000.0)}.tmp"
        payload = {
            "stamp": int(stamp),
            "dataset_signature": str(dataset_signature or ""),
            "rows": int(rows),
            "loaded_max_mag": float(loaded_max_mag),
            "updated_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
        try:
            with open(tmp_name, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=True, indent=2)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                self._safe_remove(tmp_name)

    def _is_complete_bundle(self, paths: dict[str, str]) -> bool:
        for key in ("catalog_path", "r_path", "g_path", "b_path"):
            path = str(paths.get(key, "") or "")
            if (not path) or (not os.path.isfile(path)):
                return False
            try:
                if int(os.path.getsize(path)) <= 0:
                    return False
            except Exception:
                return False
        return True

    @staticmethod
    def _signature_to_stamp(dataset_signature: str | None) -> int:
        sig = str(dataset_signature or "").strip().lower()
        if sig and all(ch in "0123456789abcdef" for ch in sig):
            head = sig[:12] if len(sig) >= 12 else sig
            try:
                stamp = int(head, 16)
                if stamp > 0:
                    return int(stamp)
            except Exception:
                log_suppressed_exception(__name__, "ScopeRuntimeCacheManager._signature_to_stamp")
        return int(time.time() * 1000.0)

    @staticmethod
    def _tmp_path_for(target_path: str) -> str:
        return (
            f"{target_path}."
            f"{int(os.getpid())}."
            f"{int(time.time() * 1000.0)}"
            ".tmp"
        )

    def cleanup(
        self,
        *,
        keep_stamps: int = 1,
        keep_stamp: int | None = None,
        tmp_ttl_seconds: float = 60.0,
    ) -> None:
        """Executa el metode cleanup de la classe ScopeRuntimeCacheManager.

        Par?metres:
        - keep_stamps (int): Valor del parametre 'keep_stamps'.
        - keep_stamp (int | None): Valor del parametre 'keep_stamp'.
        - tmp_ttl_seconds (float): Valor del parametre 'tmp_ttl_seconds'.

        Retorna:
        - None.
        """
        if not self.cache_dir:
            return
        try:
            names = os.listdir(self.cache_dir)
        except Exception:
            return

        grouped: dict[int, list[str]] = {}
        meta_grouped: dict[int, list[str]] = {}
        stamped_tmp: dict[int, list[str]] = {}
        generic_tmp: list[str] = []
        now_ts = float(time.time())
        ttl_seconds = max(1.0, float(tmp_ttl_seconds))
        keep_stamps_i = max(1, int(keep_stamps))

        for name in names:
            path = os.path.join(self.cache_dir, name)
            if not os.path.isfile(path):
                continue
            match = self._BUNDLE_RE.match(name)
            if match:
                try:
                    stamp_i = int(match.group(2))
                except Exception:
                    continue
                grouped.setdefault(stamp_i, []).append(path)
                continue
            meta_match = self._META_RE.match(name)
            if meta_match:
                try:
                    stamp_i = int(meta_match.group(1))
                except Exception:
                    continue
                meta_grouped.setdefault(stamp_i, []).append(path)
                continue
            tmp_match = self._BUNDLE_TMP_RE.match(name)
            if tmp_match:
                try:
                    stamp_i = int(tmp_match.group(2))
                except Exception:
                    generic_tmp.append(path)
                    continue
                stamped_tmp.setdefault(stamp_i, []).append(path)
                continue
            lower_name = str(name).lower()
            if "scope_runtime_sorted_" in lower_name and lower_name.endswith(
                ".tmp"
            ):
                generic_tmp.append(path)
            elif lower_name.startswith(
                "scope_runtime_bundle_meta_"
            ) and lower_name.endswith(".tmp"):
                generic_tmp.append(path)

        keep: set[int] = set()
        newest_complete_stamp = None
        if grouped:
            ordered_stamps = sorted(grouped.keys(), reverse=True)
            keep.update(ordered_stamps[:keep_stamps_i])
            newest_complete_stamp = (
                int(ordered_stamps[0]) if ordered_stamps else None
            )
        if keep_stamp is not None:
            try:
                keep.add(int(keep_stamp))
            except Exception:
                log_suppressed_exception(__name__, "ScopeRuntimeCacheManager.cleanup")

        for stamp_i, paths in grouped.items():
            if stamp_i in keep:
                continue
            for old_path in paths:
                self._safe_remove(old_path)

        for stamp_i, paths in meta_grouped.items():
            if stamp_i in keep:
                continue
            for old_path in paths:
                self._safe_remove(old_path)

        keep_stamp_i = None
        if keep_stamp is not None:
            try:
                keep_stamp_i = int(keep_stamp)
            except Exception:
                keep_stamp_i = None

        for stamp_i, paths in stamped_tmp.items():
            force_prune = False
            if (newest_complete_stamp is not None) and (
                int(stamp_i) < int(newest_complete_stamp)
            ):
                force_prune = True
            if keep_stamp_i is not None and int(stamp_i) == int(keep_stamp_i):
                force_prune = False
            for tmp_path in paths:
                if force_prune and int(stamp_i) not in keep:
                    self._safe_remove(tmp_path)
                    continue
                try:
                    age = now_ts - float(os.path.getmtime(tmp_path))
                except Exception:
                    age = float("inf")
                if age >= ttl_seconds:
                    self._safe_remove(tmp_path)

        for tmp_path in generic_tmp:
            try:
                age = now_ts - float(os.path.getmtime(tmp_path))
            except Exception:
                age = float("inf")
            if age >= ttl_seconds:
                self._safe_remove(tmp_path)

    def resolve_bundle_payload_paths(
        self,
        catalog_path: str,
        r_path: str = "",
        g_path: str = "",
        b_path: str = "",
    ) -> dict[str, Any]:
        """Executa el metode resolve_bundle_payload_paths de la classe ScopeRuntimeCacheManager.

        Par?metres:
        - catalog_path (str): Valor del parametre 'catalog_path'.
        - r_path (str): Valor del parametre 'r_path'.
        - g_path (str): Valor del parametre 'g_path'.
        - b_path (str): Valor del parametre 'b_path'.

        Retorna:
        - dict[str, Any]: Valor retornat pel metode.
        """
        catalog = str(catalog_path or "").strip()
        if (
            (not catalog)
            or catalog.lower().endswith(".tmp")
            or (not os.path.isfile(catalog))
        ):
            raise RuntimeError("Missing runtime mmap catalog path")

        catalog_stamp = self.extract_stamp(catalog)
        if catalog_stamp is None:
            raise RuntimeError("Invalid runtime mmap catalog filename")

        def _resolve_channel(raw_path: str, channel_name: str) -> str:
            candidate = str(raw_path or "").strip()
            expected = os.path.join(
                os.path.dirname(catalog),
                f"scope_runtime_sorted_{channel_name}_{catalog_stamp}.npy",
            )
            if (
                candidate
                and (not candidate.lower().endswith(".tmp"))
                and os.path.isfile(candidate)
            ):
                if (
                    os.path.basename(candidate).strip().lower()
                    == os.path.basename(expected).lower()
                ):
                    return candidate
            if os.path.isfile(expected):
                return expected
            return ""

        out = {
            "stamp": int(catalog_stamp),
            "catalog_path": catalog,
            "r_path": _resolve_channel(r_path, "r"),
            "g_path": _resolve_channel(g_path, "g"),
            "b_path": _resolve_channel(b_path, "b"),
        }
        return out

    def write_bundle_from_arrays(
        self,
        *,
        source_id,
        ra,
        dec,
        mag,
        bp_rp,
        r_arr,
        g_arr,
        b_arr,
        dataset_signature: str | None = None,
    ) -> dict[str, str]:
        """Executa el metode write_bundle_from_arrays de la classe ScopeRuntimeCacheManager.

        Par?metres:
        - source_id (Any): Valor del parametre 'source_id'.
        - ra (Any): Valor del parametre 'ra'.
        - dec (Any): Valor del parametre 'dec'.
        - mag (Any): Valor del parametre 'mag'.
        - bp_rp (Any): Valor del parametre 'bp_rp'.
        - r_arr (Any): Valor del parametre 'r_arr'.
        - g_arr (Any): Valor del parametre 'g_arr'.
        - b_arr (Any): Valor del parametre 'b_arr'.
        - dataset_signature (str | None): Valor del parametre 'dataset_signature'.

        Retorna:
        - dict[str, str]: Valor retornat pel metode.
        """
        self.prepare()
        n = int(len(ra))
        if n <= 0:
            raise ValueError("Cannot write empty scope runtime bundle")
        if not self.cache_dir:
            raise RuntimeError(
                "Scope runtime cache directory is not configured"
            )

        stamp = self._signature_to_stamp(dataset_signature)
        paths = self.bundle_paths(stamp)
        if self._is_complete_bundle(paths):
            self.cleanup(keep_stamps=1, keep_stamp=stamp, tmp_ttl_seconds=1.0)
            return dict(paths)

        self.cleanup(keep_stamps=1, keep_stamp=stamp, tmp_ttl_seconds=1.0)

        dtype = np.dtype(
            [
                ("source_id", np.int64),
                ("ra", np.float64),
                ("dec", np.float64),
                ("phot_g_mean_mag", np.float32),
                ("bp_rp", np.float32),
            ]
        )

        tmp_catalog = self._tmp_path_for(paths["catalog_path"])
        mm_cat = np.lib.format.open_memmap(
            tmp_catalog, mode="w+", dtype=dtype, shape=(n,)
        )
        mm_cat["source_id"] = np.asarray(
            (
                source_id
                if source_id is not None
                else np.full(n, -1, dtype=np.int64)
            ),
            dtype=np.int64,
        )
        mm_cat["ra"] = np.asarray(ra, dtype=np.float64)
        mm_cat["dec"] = np.asarray(dec, dtype=np.float64)
        mm_cat["phot_g_mean_mag"] = np.asarray(mag, dtype=np.float32)
        mm_cat["bp_rp"] = np.asarray(bp_rp, dtype=np.float32)
        del mm_cat
        os.replace(tmp_catalog, paths["catalog_path"])

        for arr, key in (
            (r_arr, "r_path"),
            (g_arr, "g_path"),
            (b_arr, "b_path"),
        ):
            target = paths[key]
            tmp_path = self._tmp_path_for(target)
            mm_c = np.lib.format.open_memmap(
                tmp_path, mode="w+", dtype=np.uint8, shape=(n,)
            )
            mm_c[:] = np.asarray(arr, dtype=np.uint8)
            del mm_c
            os.replace(tmp_path, target)

        mag_arr = np.asarray(mag, dtype=np.float32)
        finite = np.isfinite(mag_arr)
        max_mag = (
            float(np.max(mag_arr[finite])) if np.any(finite) else float("nan")
        )
        self._write_meta(
            stamp,
            dataset_signature=str(dataset_signature or ""),
            rows=int(n),
            loaded_max_mag=float(max_mag),
        )
        self.cleanup(keep_stamps=1, keep_stamp=stamp, tmp_ttl_seconds=1.0)
        return dict(paths)

    def write_bundle_from_structured_npy(
        self,
        *,
        runtime_catalog_path: str,
        chunk_rows: int = 1_000_000,
        progress_callback: Callable[[float, str], None] | None = None,
        bp_to_rgb_fn: Callable[[Any], tuple[Any, Any, Any]] | None = None,
        dataset_signature: str | None = None,
    ) -> dict[str, Any]:
        """Executa el metode write_bundle_from_structured_npy de la classe ScopeRuntimeCacheManager.

        Par?metres:
        - runtime_catalog_path (str): Valor del parametre 'runtime_catalog_path'.
        - chunk_rows (int): Valor del parametre 'chunk_rows'.
        - progress_callback (Callable[[float, str], None] | None): Valor del parametre 'progress_callback'.
        - bp_to_rgb_fn (Callable[[Any], tuple[Any, Any, Any]] | None): Valor del parametre 'bp_to_rgb_fn'.
        - dataset_signature (str | None): Valor del parametre 'dataset_signature'.

        Retorna:
        - dict[str, Any]: Valor retornat pel metode.
        """
        self.prepare()
        if not self.cache_dir:
            raise RuntimeError(
                "Scope runtime cache directory is not configured"
            )

        path = str(runtime_catalog_path or "").strip()
        if not path or (not os.path.isfile(path)):
            raise FileNotFoundError(
                f"Runtime catalog not found: {runtime_catalog_path}"
            )
        if bp_to_rgb_fn is None:
            raise RuntimeError(
                "bp_to_rgb_fn is required to build scope runtime RGB channels"
            )

        signature = str(dataset_signature or "").strip()
        if not signature:
            signature = self.build_dataset_signature(
                [path],
                extra={"mode": "structured_runtime_catalog"},
            )
        stamp = self._signature_to_stamp(signature)
        paths = self.bundle_paths(stamp)
        if self._is_complete_bundle(paths):
            meta = self._read_meta(stamp)
            rows = int(meta.get("rows", 0) or 0)
            loaded_max_mag = float(
                meta.get("loaded_max_mag", float("nan")) or float("nan")
            )
            if rows <= 0:
                try:
                    arr_cached = np.load(
                        paths["catalog_path"],
                        mmap_mode="r",
                        allow_pickle=False,
                    )
                    rows = int(len(arr_cached))
                    del arr_cached
                except Exception:
                    rows = 0
            self.cleanup(keep_stamps=1, keep_stamp=stamp, tmp_ttl_seconds=1.0)
            return {
                **paths,
                "rows": int(rows),
                "loaded_max_mag": float(loaded_max_mag),
            }

        self.cleanup(keep_stamps=1, keep_stamp=stamp, tmp_ttl_seconds=1.0)

        arr = np.load(path, mmap_mode="r", allow_pickle=False)
        if not isinstance(arr, np.ndarray) or arr.dtype.names is None:
            raise ValueError("Expected structured NPY runtime catalog")

        names = set(arr.dtype.names or ())
        if not {"ra", "dec", "phot_g_mean_mag"}.issubset(names):
            raise ValueError(
                "Runtime catalog missing required fields: ra/dec/phot_g_mean_mag"
            )

        total_rows = int(len(arr))
        if total_rows <= 0:
            raise ValueError("Runtime catalog is empty")

        dtype = np.dtype(
            [
                ("source_id", np.int64),
                ("ra", np.float64),
                ("dec", np.float64),
                ("phot_g_mean_mag", np.float32),
                ("bp_rp", np.float32),
            ]
        )

        tmp_catalog = self._tmp_path_for(paths["catalog_path"])
        tmp_r = self._tmp_path_for(paths["r_path"])
        tmp_g = self._tmp_path_for(paths["g_path"])
        tmp_b = self._tmp_path_for(paths["b_path"])

        mm_cat = np.lib.format.open_memmap(
            tmp_catalog, mode="w+", dtype=dtype, shape=(total_rows,)
        )
        mm_r = np.lib.format.open_memmap(
            tmp_r, mode="w+", dtype=np.uint8, shape=(total_rows,)
        )
        mm_g = np.lib.format.open_memmap(
            tmp_g, mode="w+", dtype=np.uint8, shape=(total_rows,)
        )
        mm_b = np.lib.format.open_memmap(
            tmp_b, mode="w+", dtype=np.uint8, shape=(total_rows,)
        )

        sid_key = "source_id" if "source_id" in names else None
        bp_key = "bp_rp" if "bp_rp" in names else None

        max_loaded = float("nan")
        chunk_rows_i = max(100_000, int(chunk_rows))
        for start in range(0, total_rows, chunk_rows_i):
            end = min(total_rows, start + chunk_rows_i)

            ra_chunk = np.asarray(arr["ra"][start:end], dtype=np.float64)
            dec_chunk = np.asarray(arr["dec"][start:end], dtype=np.float64)
            mag_chunk = np.asarray(
                arr["phot_g_mean_mag"][start:end], dtype=np.float32
            )
            if bp_key is not None:
                # Structured runtime catalog is opened as read-only mmap.
                # Keep a writable copy before normalization to avoid
                # "assignment destination is read-only" on some NumPy builds.
                bp_chunk = np.array(
                    arr[bp_key][start:end], dtype=np.float32, copy=True
                )
                bp_chunk = np.nan_to_num(
                    bp_chunk, nan=0.8, posinf=2.5, neginf=-0.5, copy=False
                )
            else:
                bp_chunk = np.full(end - start, 0.8, dtype=np.float32)
            if sid_key is not None:
                sid_chunk = np.asarray(arr[sid_key][start:end], dtype=np.int64)
            else:
                sid_chunk = np.full(end - start, -1, dtype=np.int64)

            mm_cat["source_id"][start:end] = sid_chunk
            mm_cat["ra"][start:end] = ra_chunk
            mm_cat["dec"][start:end] = dec_chunk
            mm_cat["phot_g_mean_mag"][start:end] = mag_chunk
            mm_cat["bp_rp"][start:end] = bp_chunk

            rr, gg, bb = bp_to_rgb_fn(bp_chunk)
            mm_r[start:end] = rr
            mm_g[start:end] = gg
            mm_b[start:end] = bb

            finite_mag = np.isfinite(mag_chunk)
            if np.any(finite_mag):
                chunk_max = float(np.max(mag_chunk[finite_mag]))
                if (not np.isfinite(max_loaded)) or chunk_max > max_loaded:
                    max_loaded = chunk_max

            if progress_callback is not None:
                try:
                    pct = 100.0 * (float(end) / float(max(1, total_rows)))
                    progress_callback(
                        min(99.0, pct),
                        f"Carregant cataleg scope ({int(round(pct))}%)",
                    )
                except Exception:
                    log_suppressed_exception(__name__, "ScopeRuntimeCacheManager.write_bundle_from_structured_npy")

        del mm_cat
        del mm_r
        del mm_g
        del mm_b
        del arr

        os.replace(tmp_catalog, paths["catalog_path"])
        os.replace(tmp_r, paths["r_path"])
        os.replace(tmp_g, paths["g_path"])
        os.replace(tmp_b, paths["b_path"])

        self._write_meta(
            stamp,
            dataset_signature=signature,
            rows=int(total_rows),
            loaded_max_mag=float(
                max_loaded if np.isfinite(max_loaded) else float("nan")
            ),
        )
        self.cleanup(keep_stamps=1, keep_stamp=stamp, tmp_ttl_seconds=1.0)
        return {
            **paths,
            "rows": int(total_rows),
            "loaded_max_mag": float(
                max_loaded if np.isfinite(max_loaded) else float("nan")
            ),
        }
