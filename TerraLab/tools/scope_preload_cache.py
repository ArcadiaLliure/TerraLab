from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from TerraLab.common.app_paths import data_dir as runtime_data_dir_for
from TerraLab.data.stars_dataset import ensure_stars_dataset, load_stars_dataset
from TerraLab.render.stars_renderer import build_scope_spatial_index_payload
from TerraLab.widgets.sky_legacy_components import (
    STAR_CATALOG_NAKED_EYE_MAX_MAG,
    _merge_sorted_catalog_with_no_gaia,
)


def _emit_event(event_type: str, **payload: Any) -> None:
    event = {"type": str(event_type), **payload}
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    out = getattr(sys, "__stdout__", sys.stdout)
    out.write(line)
    out.flush()


def _emit_progress(percent: float, message: str, **extra: Any) -> None:
    payload = {
        "percent": round(max(0.0, min(100.0, float(percent))), 1),
        "message": str(message),
    }
    payload.update(extra)
    _emit_event("progress", **payload)


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
        candidates.append(str(runtime_data_dir_for("gaia") / "no_gaia_stars.json"))
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
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
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


def _build_scope_catalog(runtime_npz: str, stars_dir: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    dataset = load_stars_dataset(runtime_npz)
    ra = np.asarray(dataset["ra"], dtype=np.float32)
    dec = np.asarray(dataset["dec"], dtype=np.float32)
    mag = np.asarray(dataset["phot_g_mean_mag"], dtype=np.float32)
    bp_rp = np.asarray(dataset.get("bp_rp"), dtype=np.float32)
    source_id = dataset.get("source_id")

    if len(ra) <= 0:
        raise ValueError("Empty runtime catalog")

    order = np.argsort(mag, kind="mergesort")
    ra = np.asarray(ra[order], dtype=np.float32)
    dec = np.asarray(dec[order], dtype=np.float32)
    mag = np.asarray(mag[order], dtype=np.float32)
    if len(bp_rp) == len(order):
        bp_rp = np.asarray(bp_rp[order], dtype=np.float32)
    else:
        bp_rp = np.full(len(order), 0.8, dtype=np.float32)
    if source_id is not None and len(source_id) == len(order):
        source_id = np.asarray(source_id)[order]
    else:
        source_id = None

    ra, dec, mag, _bp_rp, _sid, _added_no_gaia = _merge_sorted_catalog_with_no_gaia(
        stars_dir,
        ra,
        dec,
        mag,
        bp_rp,
        source_id=source_id,
    )

    if len(mag) <= 0:
        raise ValueError("Catalog became empty after normalization")
    max_loaded_mag = float(np.nanmax(np.asarray(mag, dtype=np.float32)))
    return np.asarray(ra, dtype=np.float32), np.asarray(dec, dtype=np.float32), np.asarray(mag, dtype=np.float32), max_loaded_mag


def _resolve_runtime_npz(path_hint: str | None) -> str:
    if path_hint:
        path = Path(path_hint)
        if path.exists():
            return str(path)
    return str(Path(ensure_stars_dataset()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build persistent full scope spatial index cache.")
    parser.add_argument("--runtime-npz", default="", help="Path to runtime stars_catalog.npz")
    parser.add_argument("--stars-dir", default="", help="Stars directory for no-Gaia supplement resolution")
    parser.add_argument("--cache-dir", required=True, help="Persistent scope cache directory")
    parser.add_argument("--schema-version", type=int, default=1)
    parser.add_argument("--max-mag", type=float, default=float("nan"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    runtime_npz = _resolve_runtime_npz(args.runtime_npz)
    stars_dir = str(Path(args.stars_dir)) if args.stars_dir else str(runtime_data_dir_for("gaia"))
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    max_mag = None if (not np.isfinite(float(args.max_mag))) else float(args.max_mag)
    schema_version = int(max(1, args.schema_version))
    dataset_sig = _dataset_signature(runtime_npz, stars_dir, schema_version, max_mag)

    cache_idx_npy = cache_dir / f"scope_index_full_v{schema_version}.indices.npy"
    cache_off_npy = cache_dir / f"scope_index_full_v{schema_version}.offsets.npy"
    cache_meta = cache_dir / f"scope_index_full_v{schema_version}.meta.json"
    meta = _load_cached_meta(cache_meta)

    cache_valid = (
        (not bool(args.force))
        and cache_idx_npy.exists()
        and cache_off_npy.exists()
        and meta.get("dataset_signature") == dataset_sig
        and int(meta.get("schema_version", 0)) == schema_version
        and (
            (meta.get("max_mag") is None and max_mag is None)
            or (
                meta.get("max_mag") is not None
                and max_mag is not None
                and abs(float(meta.get("max_mag")) - float(max_mag)) <= 1e-6
            )
        )
    )

    if cache_valid:
        _emit_progress(100.0, "scope preload cache hit", stage="cache_hit")
        _emit_event(
            "done",
            indices_path=str(cache_idx_npy),
            offsets_path=str(cache_off_npy),
            dataset_signature=dataset_sig,
            rows=int(meta.get("rows", 0)),
            loaded_max_mag=float(meta.get("loaded_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)),
            cached=True,
        )
        return 0

    _emit_progress(0.0, "scope preload start", stage="start")

    try:
        _emit_progress(10.0, "loading runtime catalog", stage="load_catalog")
        ra_all, dec_all, mag_all, max_loaded_mag = _build_scope_catalog(runtime_npz, stars_dir)
        rows = int(len(ra_all))

        _emit_progress(58.0, "building scope spatial index", stage="build_index", rows=rows)
        sorted_indices, offsets = build_scope_spatial_index_payload(
            ra_all,
            dec_all,
            mag_all=mag_all,
            max_mag=max_mag,
        )
        if sorted_indices is None or offsets is None:
            raise RuntimeError("Scope index payload build returned empty result")

        if max_mag is not None:
            loaded_max_mag = float(min(max_loaded_mag, max_mag))
        else:
            loaded_max_mag = float(max_loaded_mag)

        _emit_progress(92.0, "writing persistent scope cache", stage="save_cache", rows=rows)
        tmp_idx = cache_idx_npy.with_suffix(".npy.tmp")
        tmp_off = cache_off_npy.with_suffix(".npy.tmp")
        with tmp_idx.open("wb") as fh_idx:
            np.save(fh_idx, np.asarray(sorted_indices, dtype=np.int32), allow_pickle=False)
        with tmp_off.open("wb") as fh_off:
            np.save(fh_off, np.asarray(offsets, dtype=np.int64), allow_pickle=False)
        tmp_idx.replace(cache_idx_npy)
        tmp_off.replace(cache_off_npy)

        meta_payload = {
            "dataset_signature": dataset_sig,
            "schema_version": schema_version,
            "runtime_npz": runtime_npz,
            "stars_dir": stars_dir,
            "indices_path": str(cache_idx_npy),
            "offsets_path": str(cache_off_npy),
            "rows": rows,
            "loaded_max_mag": loaded_max_mag,
            "max_mag": max_mag,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        _save_cached_meta(cache_meta, meta_payload)

        _emit_progress(100.0, "scope preload ready", stage="done", rows=rows)
        _emit_event(
            "done",
            indices_path=str(cache_idx_npy),
            offsets_path=str(cache_off_npy),
            dataset_signature=dataset_sig,
            rows=rows,
            loaded_max_mag=loaded_max_mag,
            cached=False,
        )
        return 0
    except Exception as exc:
        message = str(exc)
        _emit_event("error", message=message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
