from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from TerraLab.common.app_paths import data_dir as runtime_data_dir_for
from TerraLab.render.stars_renderer import build_scope_spatial_index_payload
from TerraLab.widgets.sky_legacy_components import (
    STAR_CATALOG_NAKED_EYE_MAX_MAG,
)


def _path_signature(path_value: str | None) -> dict[str, Any] | None:
    if not path_value:
        return None
    path = Path(path_value)
    if (not path.exists()) or (not path.is_file()):
        return None
    st = path.stat()
    return {
        "path": str(path.resolve()),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }


def _resolve_no_gaia_path(stars_dir: str | None) -> str:
    candidates: list[str] = []
    if stars_dir:
        candidates.append(str(Path(stars_dir) / "no_gaia_stars.json"))
    try:
        candidates.append(
            str(runtime_data_dir_for("gaia") / "no_gaia_stars.json")
        )
    except Exception:
        pass
    candidates.append(
        str(
            Path(__file__).resolve().parents[1]
            / "data"
            / "stars"
            / "no_gaia_stars.json"
        )
    )
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    return ""


def _dataset_signature(
    runtime_npz: str,
    stars_dir: str,
    schema_version: int,
    max_mag: float | None,
) -> str:
    payload = {
        "runtime_npz": _path_signature(runtime_npz),
        "no_gaia": _path_signature(_resolve_no_gaia_path(stars_dir)),
        "schema_version": int(schema_version),
        "max_mag": None if max_mag is None else float(max_mag),
    }
    raw = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_cached_meta(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {}


def _save_cached_meta(path: Path, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=True, indent=2)
    tmp.replace(path)


class ScopeFullPreloadWorker(QObject):
    progress = pyqtSignal(object)
    ready = pyqtSignal(object)
    error = pyqtSignal(str)

    def _emit_ready_from_cache(self, done_payload: dict[str, Any]) -> None:
        indices_path = str(done_payload.get("indices_path", "") or "")
        offsets_path = str(done_payload.get("offsets_path", "") or "")
        if (not indices_path) or (not os.path.exists(indices_path)):
            raise RuntimeError(
                "Scope preload finished without indices cache file"
            )
        if (not offsets_path) or (not os.path.exists(offsets_path)):
            raise RuntimeError(
                "Scope preload finished without offsets cache file"
            )
        rows = int(done_payload.get("rows", 0) or 0)
        loaded_max_mag = float(done_payload.get("loaded_max_mag", 0.0) or 0.0)
        self.ready.emit(
            {
                "indices_path": indices_path,
                "offsets_path": offsets_path,
                "dataset_signature": str(
                    done_payload.get("dataset_signature", "") or ""
                ),
                "loaded_max_mag": float(loaded_max_mag),
                "rows": int(rows),
                "cached": bool(done_payload.get("cached", False)),
            }
        )

    @pyqtSlot(str, str, str, float, int, bool)
    def run(
        self,
        runtime_npz_path: str,
        stars_dir: str,
        cache_dir: str,
        max_mag: float,
        schema_version: int,
        force_rebuild: bool,
    ) -> None:
        """Executa el metode run de la classe ScopeFullPreloadWorker.

        Par?metres:
        - runtime_npz_path (str): Valor del parametre 'runtime_npz_path'.
        - stars_dir (str): Valor del parametre 'stars_dir'.
        - cache_dir (str): Valor del parametre 'cache_dir'.
        - max_mag (float): Valor del parametre 'max_mag'.
        - schema_version (int): Valor del parametre 'schema_version'.
        - force_rebuild (bool): Valor del parametre 'force_rebuild'.

        Retorna:
        - None.
        """
        project_root = Path(__file__).resolve().parents[2]
        cmd = [
            sys.executable,
            "-m",
            "TerraLab.tools.scope_preload_cache",
            "--runtime-npz",
            str(runtime_npz_path or ""),
            "--stars-dir",
            str(stars_dir or ""),
            "--cache-dir",
            str(cache_dir),
            "--schema-version",
            str(int(schema_version)),
        ]
        if np.isfinite(float(max_mag)):
            cmd.extend(["--max-mag", f"{float(max_mag):.6f}"])
        if bool(force_rebuild):
            cmd.append("--force")

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            self.error.emit(f"Scope preload spawn failed: {exc}")
            return

        done_payload = None
        last_error = ""

        def _drain_stderr(stream):
            if stream is None:
                return
            try:
                for raw_line in iter(stream.readline, ""):
                    text = str(raw_line or "").strip()
                    if text:
                        print(f"[ScopePreload] {text}")
            except Exception:
                pass

        stderr_thread = threading.Thread(
            target=_drain_stderr, args=(proc.stderr,), daemon=True
        )
        stderr_thread.start()

        assert proc.stdout is not None
        for raw_line in iter(proc.stdout.readline, ""):
            line = str(raw_line or "").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                print(f"[ScopePreload] {line}")
                continue

            event_type = str(event.get("type", "")).strip().lower()
            if event_type == "progress":
                self.progress.emit(event)
            elif event_type == "done":
                done_payload = event
            elif event_type == "error":
                last_error = str(
                    event.get("message", "Unknown scope preload error")
                )

        return_code = proc.wait()
        stderr_thread.join(timeout=0.2)

        if return_code != 0:
            if not last_error:
                last_error = f"Scope preload subprocess failed with exit code {return_code}"
            self.error.emit(last_error)
            return

        if not isinstance(done_payload, dict):
            self.error.emit("Scope preload finished without done payload")
            return

        try:
            self._emit_ready_from_cache(done_payload)
        except Exception as exc:
            self.error.emit(f"Scope preload cache read error: {exc}")

    @pyqtSlot(object, object, object, str, str, str, float, int, bool)
    def run_from_arrays(
        self,
        ra_all,
        dec_all,
        mag_all,
        runtime_npz_path: str,
        stars_dir: str,
        cache_dir: str,
        max_mag: float,
        schema_version: int,
        force_rebuild: bool,
    ) -> None:
        """Executa el metode run_from_arrays de la classe ScopeFullPreloadWorker.

        Par?metres:
        - ra_all (Any): Valor del parametre 'ra_all'.
        - dec_all (Any): Valor del parametre 'dec_all'.
        - mag_all (Any): Valor del parametre 'mag_all'.
        - runtime_npz_path (str): Valor del parametre 'runtime_npz_path'.
        - stars_dir (str): Valor del parametre 'stars_dir'.
        - cache_dir (str): Valor del parametre 'cache_dir'.
        - max_mag (float): Valor del parametre 'max_mag'.
        - schema_version (int): Valor del parametre 'schema_version'.
        - force_rebuild (bool): Valor del parametre 'force_rebuild'.

        Retorna:
        - None.
        """
        try:
            ra_arr = np.asarray(ra_all, dtype=np.float32)
            dec_arr = np.asarray(dec_all, dtype=np.float32)
            mag_arr = np.asarray(mag_all, dtype=np.float32)
            if (
                len(ra_arr) <= 0
                or len(ra_arr) != len(dec_arr)
                or len(ra_arr) != len(mag_arr)
            ):
                raise ValueError(
                    "Invalid in-memory catalog arrays for scope preload"
                )

            max_mag_opt = (
                None if (not np.isfinite(float(max_mag))) else float(max_mag)
            )
            schema = int(max(1, int(schema_version)))
            cache_root = Path(cache_dir)
            cache_root.mkdir(parents=True, exist_ok=True)
            cache_idx_npy = (
                cache_root / f"scope_index_full_v{schema}.indices.npy"
            )
            cache_off_npy = (
                cache_root / f"scope_index_full_v{schema}.offsets.npy"
            )
            cache_meta = cache_root / f"scope_index_full_v{schema}.meta.json"
            dataset_sig = _dataset_signature(
                str(runtime_npz_path or ""),
                str(stars_dir or ""),
                schema,
                max_mag_opt,
            )

            meta = _load_cached_meta(cache_meta)
            cache_valid = (
                (not bool(force_rebuild))
                and cache_idx_npy.exists()
                and cache_off_npy.exists()
                and meta.get("dataset_signature") == dataset_sig
                and int(meta.get("schema_version", 0)) == schema
                and (
                    (meta.get("max_mag") is None and max_mag_opt is None)
                    or (
                        meta.get("max_mag") is not None
                        and max_mag_opt is not None
                        and abs(
                            float(meta.get("max_mag")) - float(max_mag_opt)
                        )
                        <= 1e-6
                    )
                )
            )

            if cache_valid:
                self.progress.emit(
                    {
                        "type": "progress",
                        "percent": 100.0,
                        "stage": "cache_hit",
                        "message": "scope preload cache hit",
                    }
                )
                self._emit_ready_from_cache(
                    {
                        "indices_path": str(cache_idx_npy),
                        "offsets_path": str(cache_off_npy),
                        "dataset_signature": dataset_sig,
                        "rows": int(meta.get("rows", 0) or 0),
                        "loaded_max_mag": float(
                            meta.get(
                                "loaded_max_mag",
                                STAR_CATALOG_NAKED_EYE_MAX_MAG,
                            )
                        ),
                        "cached": True,
                    }
                )
                return

            self.progress.emit(
                {
                    "type": "progress",
                    "percent": 12.0,
                    "stage": "build_index",
                    "message": "building scope index from in-memory catalog",
                }
            )
            sorted_indices, offsets = build_scope_spatial_index_payload(
                ra_arr,
                dec_arr,
                mag_all=mag_arr,
                max_mag=max_mag_opt,
            )
            if sorted_indices is None or offsets is None:
                raise RuntimeError(
                    "Scope index payload build returned empty result"
                )

            max_loaded_mag = (
                float(np.nanmax(mag_arr))
                if len(mag_arr) > 0
                else float(STAR_CATALOG_NAKED_EYE_MAX_MAG)
            )
            if max_mag_opt is not None:
                loaded_max_mag = float(min(max_loaded_mag, max_mag_opt))
            else:
                loaded_max_mag = float(max_loaded_mag)
            rows = int(len(ra_arr))

            self.progress.emit(
                {
                    "type": "progress",
                    "percent": 90.0,
                    "stage": "save_cache",
                    "message": "writing persistent scope cache",
                    "rows": rows,
                }
            )
            tmp_idx = cache_idx_npy.with_suffix(".npy.tmp")
            tmp_off = cache_off_npy.with_suffix(".npy.tmp")
            with tmp_idx.open("wb") as fh_idx:
                np.save(
                    fh_idx,
                    np.asarray(sorted_indices, dtype=np.int32),
                    allow_pickle=False,
                )
            with tmp_off.open("wb") as fh_off:
                np.save(
                    fh_off,
                    np.asarray(offsets, dtype=np.int64),
                    allow_pickle=False,
                )
            tmp_idx.replace(cache_idx_npy)
            tmp_off.replace(cache_off_npy)

            meta_payload = {
                "dataset_signature": dataset_sig,
                "schema_version": schema,
                "runtime_npz": str(runtime_npz_path or ""),
                "stars_dir": str(stars_dir or ""),
                "indices_path": str(cache_idx_npy),
                "offsets_path": str(cache_off_npy),
                "rows": rows,
                "loaded_max_mag": loaded_max_mag,
                "max_mag": max_mag_opt,
                "updated_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }
            _save_cached_meta(cache_meta, meta_payload)

            self.progress.emit(
                {
                    "type": "progress",
                    "percent": 100.0,
                    "stage": "done",
                    "message": "scope preload ready",
                    "rows": rows,
                }
            )
            self.ready.emit(
                {
                    "indices_path": str(cache_idx_npy),
                    "offsets_path": str(cache_off_npy),
                    "dataset_signature": dataset_sig,
                    "loaded_max_mag": float(loaded_max_mag),
                    "rows": int(rows),
                    "cached": False,
                }
            )
        except Exception as exc:
            self.error.emit(str(exc))
