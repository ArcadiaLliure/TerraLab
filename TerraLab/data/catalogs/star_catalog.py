"""Star catalogue discovery, loading, merging, and conversion."""

from __future__ import annotations

import json
import math
import os
import re

import numpy as np

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.app_paths import data_dir as runtime_data_dir_for
from TerraLab.util.color import bp_rp_to_rgb_arrays
from TerraLab.data.catalogs.constants import STAR_CATALOG_NAKED_EYE_MAX_MAG
from TerraLab.data.catalogs.scope_cache import ScopeRuntimeCacheManager

NO_GAIA_STARS_JSON_NAME = "no_gaia_stars.json"
STAR_CATALOG_FILE_RE = re.compile(
    r"^MAGNITUD_(-?\d+(?:\.\d+)?)_(-?\d+(?:\.\d+)?)_(\d+)\.npz$",
    re.IGNORECASE,
)


def _parse_star_catalog_npz_name(file_name):
    m = STAR_CATALOG_FILE_RE.match(str(file_name or "").strip())
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2)), int(m.group(3))
    except Exception:
        return None


def _discover_star_catalog_npz_entries(stars_dir):
    out = []
    if not os.path.isdir(stars_dir):
        return out
    for name in os.listdir(stars_dir):
        parsed = _parse_star_catalog_npz_name(name)
        if not parsed:
            continue
        fmin, fmax, rows_hint = parsed
        path = os.path.join(stars_dir, name)
        if not os.path.isfile(path):
            continue
        out.append(
            {
                "path": path,
                "name": name,
                "min_mag": float(fmin),
                "max_mag": float(fmax),
                "rows_hint": int(rows_hint),
            }
        )
    out.sort(key=lambda e: (e["min_mag"], e["max_mag"], e["name"]))
    return out


def _select_base_star_catalog_entry(
    entries, max_mag=STAR_CATALOG_NAKED_EYE_MAX_MAG
):
    if not entries:
        return None
    eps = 1e-6
    exact = [
        e
        for e in entries
        if abs(float(e["max_mag"]) - float(max_mag)) <= eps
        and float(e["min_mag"]) <= float(max_mag) + eps
    ]
    if exact:
        exact.sort(key=lambda e: (e["min_mag"], e["name"]))
        return exact[0]

    bounded = [
        e for e in entries if float(e["max_mag"]) <= float(max_mag) + eps
    ]
    if bounded:
        bounded.sort(key=lambda e: (e["max_mag"], -e["min_mag"], e["name"]))
        return bounded[-1]

    for e in entries:
        if float(e["min_mag"]) <= float(max_mag) <= float(e["max_mag"]):
            return e
    return entries[0]


def _npz_get_first(npz_obj, keys):
    for k in keys:
        if k in npz_obj:
            return npz_obj[k]
    return None


def _load_star_npz_arrays(npz_path, max_mag=None, min_mag_exclusive=None):
    if np is None:
        raise RuntimeError("NumPy is required to load star NPZ catalogs.")

    with np.load(npz_path, allow_pickle=False) as data:
        ra_raw = _npz_get_first(data, ("ra", "RA"))
        dec_raw = _npz_get_first(data, ("dec", "DEC"))
        mag_raw = _npz_get_first(data, ("mag", "phot_g_mean_mag", "g_mag"))
        bprp_raw = _npz_get_first(data, ("bp_rp", "bprp"))
        sid_raw = _npz_get_first(
            data, ("source_id", "source_ids", "id", "ids")
        )

        if ra_raw is None or dec_raw is None or mag_raw is None:
            raise ValueError(
                f"NPZ missing required arrays (ra/dec/mag): {npz_path}"
            )

        ra = np.asarray(ra_raw, dtype=np.float32)
        dec = np.asarray(dec_raw, dtype=np.float32)
        mag = np.asarray(mag_raw, dtype=np.float32)
        if not (len(ra) == len(dec) == len(mag)):
            raise ValueError(f"NPZ array length mismatch in {npz_path}")

        if bprp_raw is None:
            bp_rp = np.full(len(mag), 0.8, dtype=np.float32)
        else:
            bp_rp = np.asarray(bprp_raw, dtype=np.float32)
            if len(bp_rp) != len(mag):
                bp_rp = np.full(len(mag), 0.8, dtype=np.float32)
        bp_rp = np.nan_to_num(bp_rp, nan=0.8, posinf=2.0, neginf=-0.5).astype(
            np.float32, copy=False
        )

        source_id = None
        if sid_raw is not None:
            try:
                sid_arr = np.asarray(sid_raw)
                if len(sid_arr) == len(mag):
                    source_id = sid_arr.astype(np.int64, copy=False)
            except Exception:
                source_id = None

    mask = np.isfinite(ra) & np.isfinite(dec) & np.isfinite(mag)
    if max_mag is not None:
        mask &= mag <= float(max_mag) + 1e-6
    if min_mag_exclusive is not None:
        mask &= mag > float(min_mag_exclusive) + 1e-6

    if not np.all(mask):
        ra = ra[mask]
        dec = dec[mask]
        mag = mag[mask]
        bp_rp = bp_rp[mask]
        if source_id is not None and len(source_id) == len(mask):
            source_id = source_id[mask]

    return {
        "ra": ra,
        "dec": dec,
        "mag": mag,
        "bp_rp": bp_rp,
        "source_id": source_id,
    }


def _safe_float_or_nan(value):
    try:
        out = float(value)
    except Exception:
        return float("nan")
    if not math.isfinite(out):
        return float("nan")
    return out


def _safe_int_or_default(value, default=-1):
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return int(default)


def _load_no_gaia_star_arrays(stars_dir):
    if np is None:
        return None
    if stars_dir:
        fused_flag = os.path.join(
            stars_dir, "stars_catalog_no_gaia_fused.flag"
        )
        if os.path.isfile(fused_flag):
            return None
    candidate_paths = []
    if stars_dir:
        candidate_paths.append(
            os.path.join(stars_dir, NO_GAIA_STARS_JSON_NAME)
        )
    try:
        candidate_paths.append(
            os.path.join(
                str(runtime_data_dir_for("gaia")), NO_GAIA_STARS_JSON_NAME
            )
        )
    except Exception:
        log_suppressed_exception(__name__, "_load_no_gaia_star_arrays")
    candidate_paths.append(
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

    path = None
    for cand in candidate_paths:
        if cand and os.path.isfile(cand):
            path = cand
            break
    if not path:
        return None

    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        print(f"[CatalogLoader] no_gaia supplement skipped ({path}): {exc}")
        return None

    rows = []
    names = []
    if isinstance(payload, dict):
        rows = payload.get("data") or []
        metadata = payload.get("metadata") or []
        for item in metadata:
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    names.append(str(name))
    elif isinstance(payload, list):
        rows = payload

    if not rows:
        return None

    out_ra = []
    out_dec = []
    out_mag = []
    out_bp = []
    out_sid = []
    have_sid = False

    if isinstance(rows[0], dict):
        for row in rows:
            if not isinstance(row, dict):
                continue
            ra = _safe_float_or_nan(row.get("ra"))
            dec = _safe_float_or_nan(row.get("dec"))
            mag = _safe_float_or_nan(row.get("phot_g_mean_mag"))
            if not (
                math.isfinite(ra) and math.isfinite(dec) and math.isfinite(mag)
            ):
                continue
            bp_rp = _safe_float_or_nan(row.get("bp_rp"))
            if not math.isfinite(bp_rp):
                bp_rp = 0.8
            out_ra.append(ra)
            out_dec.append(dec)
            out_mag.append(mag)
            out_bp.append(bp_rp)
            if "source_id" in row:
                have_sid = True
                out_sid.append(_safe_int_or_default(row.get("source_id"), -1))
            elif have_sid:
                out_sid.append(-1)
    else:
        if not names and isinstance(rows[0], (list, tuple)):
            default = (
                "source_id",
                "designation",
                "ra",
                "dec",
                "phot_g_mean_mag",
                "phot_bp_mean_mag",
                "phot_rp_mean_mag",
                "bp_rp",
                "pmra",
                "pmdec",
                "parallax",
            )
            names = list(default[: len(rows[0])])
        idx = {name: i for i, name in enumerate(names)}
        ra_i = idx.get("ra")
        dec_i = idx.get("dec")
        mag_i = idx.get("phot_g_mean_mag")
        bprp_i = idx.get("bp_rp")
        sid_i = idx.get("source_id")
        have_sid = sid_i is not None
        if ra_i is None or dec_i is None or mag_i is None:
            return None

        for row in rows:
            if not isinstance(row, (list, tuple)):
                continue
            if max(ra_i, dec_i, mag_i) >= len(row):
                continue
            ra = _safe_float_or_nan(row[ra_i])
            dec = _safe_float_or_nan(row[dec_i])
            mag = _safe_float_or_nan(row[mag_i])
            if not (
                math.isfinite(ra) and math.isfinite(dec) and math.isfinite(mag)
            ):
                continue
            bp_rp = (
                _safe_float_or_nan(row[bprp_i])
                if bprp_i is not None and bprp_i < len(row)
                else 0.8
            )
            if not math.isfinite(bp_rp):
                bp_rp = 0.8
            out_ra.append(ra)
            out_dec.append(dec)
            out_mag.append(mag)
            out_bp.append(bp_rp)
            if have_sid:
                sid_val = row[sid_i] if sid_i < len(row) else -1
                out_sid.append(_safe_int_or_default(sid_val, -1))

    if not out_ra:
        return None

    arrays = {
        "ra": np.asarray(out_ra, dtype=np.float32),
        "dec": np.asarray(out_dec, dtype=np.float32),
        "mag": np.asarray(out_mag, dtype=np.float32),
        "bp_rp": np.asarray(out_bp, dtype=np.float32),
    }
    if have_sid and len(out_sid) == len(out_ra):
        arrays["source_id"] = np.asarray(out_sid, dtype=np.int64)
    print(
        f"[CatalogLoader] no-Gaia supplement source: '{path}' "
        f"({len(out_ra)} stars)"
    )
    return arrays


def _merge_sorted_array(base_arr, insert_values, insert_positions):
    out = np.empty(len(base_arr) + len(insert_values), dtype=base_arr.dtype)
    src = 0
    dst = 0
    for i, raw_pos in enumerate(insert_positions):
        pos = int(raw_pos)
        if pos < src:
            pos = src
        if pos > src:
            seg_len = pos - src
            out[dst : dst + seg_len] = base_arr[src:pos]
            dst += seg_len
            src = pos
        out[dst] = insert_values[i]
        dst += 1
    if src < len(base_arr):
        out[dst:] = base_arr[src:]
    return out


def _merge_sorted_catalog_with_no_gaia(
    stars_dir, np_ra, np_dec, np_mag, np_bp_rp, source_id=None
):
    supplement = _load_no_gaia_star_arrays(stars_dir)
    if supplement is None:
        return np_ra, np_dec, np_mag, np_bp_rp, source_id, 0

    s_ra = np.asarray(supplement.get("ra", []), dtype=np.float32)
    s_dec = np.asarray(supplement.get("dec", []), dtype=np.float32)
    s_mag = np.asarray(supplement.get("mag", []), dtype=np.float32)
    s_bp = np.asarray(supplement.get("bp_rp", []), dtype=np.float32)
    s_sid = supplement.get("source_id")
    if s_sid is not None:
        s_sid = np.asarray(s_sid, dtype=np.int64)

    if len(s_mag) <= 0:
        return np_ra, np_dec, np_mag, np_bp_rp, source_id, 0

    order = np.argsort(s_mag, kind="mergesort")
    s_ra = s_ra[order]
    s_dec = s_dec[order]
    s_mag = s_mag[order]
    s_bp = s_bp[order]
    if s_sid is not None and len(s_sid) == len(order):
        s_sid = s_sid[order]

    if s_sid is not None:
        keep = np.ones(len(s_sid), dtype=bool)
        seen = set()
        for i, sid in enumerate(s_sid):
            sid_i = int(sid)
            if sid_i > 0 and sid_i in seen:
                keep[i] = False
            else:
                if sid_i > 0:
                    seen.add(sid_i)
        if not np.all(keep):
            s_ra = s_ra[keep]
            s_dec = s_dec[keep]
            s_mag = s_mag[keep]
            s_bp = s_bp[keep]
            s_sid = s_sid[keep]

    if len(s_mag) <= 0:
        return np_ra, np_dec, np_mag, np_bp_rp, source_id, 0

    insert_at = np.searchsorted(np_mag, s_mag, side="left")
    merged_ra = _merge_sorted_array(np_ra, s_ra, insert_at)
    merged_dec = _merge_sorted_array(np_dec, s_dec, insert_at)
    merged_mag = _merge_sorted_array(np_mag, s_mag, insert_at)
    merged_bp = _merge_sorted_array(np_bp_rp, s_bp, insert_at)

    merged_sid = None
    if source_id is not None or s_sid is not None:
        if source_id is not None and len(source_id) == len(np_mag):
            base_sid = np.asarray(source_id, dtype=np.int64)
        else:
            base_sid = np.full(len(np_mag), -1, dtype=np.int64)

        if s_sid is None or len(s_sid) != len(s_mag):
            s_sid = np.full(len(s_mag), -1, dtype=np.int64)
        else:
            s_sid = np.asarray(s_sid, dtype=np.int64)
        merged_sid = _merge_sorted_array(base_sid, s_sid, insert_at)

    return (
        merged_ra,
        merged_dec,
        merged_mag,
        merged_bp,
        merged_sid,
        int(len(s_mag)),
    )


def _load_structured_npy_subset_by_mag(
    npy_path, max_mag, chunk_rows=2_000_000
):
    """Load only stars up to `max_mag` from structured runtime NPY (chunked scan)."""
    if np is None:
        return None
    try:
        arr = np.load(npy_path, mmap_mode="r", allow_pickle=False)
    except Exception:
        return None

    if not isinstance(arr, np.ndarray) or arr.dtype.names is None:
        return None
    names = set(arr.dtype.names or ())
    if not {"ra", "dec", "phot_g_mean_mag"}.issubset(names):
        return None

    total_rows = int(len(arr))
    if total_rows <= 0:
        return {
            "ra": np.empty(0, dtype=np.float32),
            "dec": np.empty(0, dtype=np.float32),
            "mag": np.empty(0, dtype=np.float32),
            "bp_rp": np.empty(0, dtype=np.float32),
            "source_id": None,
            "total_rows": 0,
            "full_mag_max": float("nan"),
        }

    cap = float(max_mag)
    chunk_rows = max(100_000, int(chunk_rows))
    has_bp = "bp_rp" in names
    has_sid = "source_id" in names

    ra_parts = []
    dec_parts = []
    mag_parts = []
    bp_parts = []
    sid_parts = [] if has_sid else None
    full_mag_max = float("nan")

    for start in range(0, total_rows, chunk_rows):
        end = min(total_rows, start + chunk_rows)
        mag_chunk = np.asarray(
            arr["phot_g_mean_mag"][start:end], dtype=np.float32
        )
        finite_mag = np.isfinite(mag_chunk)
        if np.any(finite_mag):
            chunk_max = float(np.max(mag_chunk[finite_mag]))
            if (not np.isfinite(full_mag_max)) or chunk_max > full_mag_max:
                full_mag_max = chunk_max

        ra_chunk = np.asarray(arr["ra"][start:end], dtype=np.float32)
        dec_chunk = np.asarray(arr["dec"][start:end], dtype=np.float32)
        mask = (
            finite_mag
            & (mag_chunk <= cap + 1e-6)
            & np.isfinite(ra_chunk)
            & np.isfinite(dec_chunk)
        )
        if not np.any(mask):
            continue

        ra_parts.append(ra_chunk[mask])
        dec_parts.append(dec_chunk[mask])
        mag_parts.append(mag_chunk[mask])

        if has_bp:
            bp_chunk = np.asarray(arr["bp_rp"][start:end], dtype=np.float32)
            bp_parts.append(bp_chunk[mask])
        else:
            bp_parts.append(
                np.full(int(np.count_nonzero(mask)), 0.8, dtype=np.float32)
            )

        if has_sid and sid_parts is not None:
            sid_chunk = np.asarray(arr["source_id"][start:end], dtype=np.int64)
            sid_parts.append(sid_chunk[mask])

    if ra_parts:
        ra = np.concatenate(ra_parts)
        dec = np.concatenate(dec_parts)
        mag = np.concatenate(mag_parts)
        bp = np.concatenate(bp_parts)
        source_id = (
            np.concatenate(sid_parts) if has_sid and sid_parts else None
        )
    else:
        ra = np.empty(0, dtype=np.float32)
        dec = np.empty(0, dtype=np.float32)
        mag = np.empty(0, dtype=np.float32)
        bp = np.empty(0, dtype=np.float32)
        source_id = None

    return {
        "ra": np.asarray(ra, dtype=np.float32),
        "dec": np.asarray(dec, dtype=np.float32),
        "mag": np.asarray(mag, dtype=np.float32),
        "bp_rp": np.asarray(bp, dtype=np.float32),
        "source_id": source_id,
        "total_rows": int(total_rows),
        "full_mag_max": float(full_mag_max),
    }


def _bp_rp_to_rgb_arrays(bp_rp):
    return bp_rp_to_rgb_arrays(bp_rp)


def _cleanup_scope_runtime_cache_files(
    out_dir: str,
    *,
    keep_stamps: int = 1,
    keep_stamp: int | None = None,
    tmp_ttl_seconds: float = 60.0,
) -> None:
    ScopeRuntimeCacheManager.from_cache_dir(out_dir).cleanup(
        keep_stamps=keep_stamps,
        keep_stamp=keep_stamp,
        tmp_ttl_seconds=tmp_ttl_seconds,
    )


def _write_scope_runtime_mmap_bundle(
    stars_dir: str,
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
    manager = ScopeRuntimeCacheManager(stars_dir=stars_dir)
    return manager.write_bundle_from_arrays(
        source_id=source_id,
        ra=ra,
        dec=dec,
        mag=mag,
        bp_rp=bp_rp,
        r_arr=r_arr,
        g_arr=g_arr,
        b_arr=b_arr,
        dataset_signature=dataset_signature,
    )


def _write_scope_runtime_mmap_bundle_from_structured_npy(
    stars_dir: str,
    runtime_catalog_path: str,
    *,
    chunk_rows: int = 1_000_000,
    progress_callback=None,
    dataset_signature: str | None = None,
) -> dict[str, object]:
    manager = ScopeRuntimeCacheManager(stars_dir=stars_dir)
    return manager.write_bundle_from_structured_npy(
        runtime_catalog_path=runtime_catalog_path,
        chunk_rows=chunk_rows,
        progress_callback=progress_callback,
        bp_to_rgb_fn=_bp_rp_to_rgb_arrays,
        dataset_signature=dataset_signature,
    )


def _build_celestial_objects_from_arrays(ra, dec, mag, bp_rp, source_id=None):
    out = []
    n = len(ra)
    for i in range(n):
        sid_val = i
        if source_id is not None and i < len(source_id):
            try:
                sid_val = int(source_id[i])
            except Exception:
                sid_val = i
        out.append(
            {
                "id": str(sid_val),
                "name": "",
                "ra": float(ra[i]),
                "dec": float(dec[i]),
                "mag": float(mag[i]),
                "bp_rp": float(bp_rp[i]),
            }
        )
    return out


