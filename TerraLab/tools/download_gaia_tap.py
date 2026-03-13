"""Download Gaia DR3 stars via ESA TAP and build TerraLab runtime cache."""

from __future__ import annotations

import argparse
import csv
import faulthandler
import json
import shutil
import traceback
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, TextIO

import numpy as np

# Allow running as a standalone script: `python tools/download_gaia_tap.py ...`
if __package__ in (None, ""):
    _repo_root = Path(__file__).resolve().parents[2]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))

from TerraLab.common.app_paths import ensure_runtime_layout
from TerraLab.common.utils import set_config_value
from TerraLab.util.gaia_importer import build_gaia_catalog_from_tables

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


TAP_BASE_URL = "https://gea.esac.esa.int/tap-server/tap"
VISIBLE_MAG_LIMIT_DEFAULT = 8.0


class _TeeStream:
    """Duplicate writes to both terminal and optional log file."""

    def __init__(self, primary, secondary: Optional[TextIO]):
        self._primary = primary
        self._secondary = secondary

    @property
    def encoding(self):  # pragma: no cover - passthrough for stdlib consumers
        return getattr(self._primary, "encoding", "utf-8")

    def isatty(self) -> bool:  # pragma: no cover - passthrough
        try:
            return bool(self._primary.isatty())
        except Exception:
            return False

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        count = 0
        try:
            count = int(self._primary.write(text))
        except Exception:
            count = len(text)
        if self._secondary is not None:
            try:
                self._secondary.write(text)
            except Exception:
                pass
        return count

    def flush(self) -> None:
        try:
            self._primary.flush()
        except Exception:
            pass
        if self._secondary is not None:
            try:
                self._secondary.flush()
            except Exception:
                pass


def _setup_file_logging(log_file: str) -> tuple[Optional[TextIO], Optional[object], Optional[object], Optional[Path]]:
    path_raw = str(log_file or "").strip()
    if not path_raw:
        return None, None, None, None

    log_path = Path(path_raw).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_stream = log_path.open("w", encoding="utf-8", buffering=1)

    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    sys.stdout = _TeeStream(orig_stdout, log_stream)
    sys.stderr = _TeeStream(orig_stderr, log_stream)
    try:
        faulthandler.enable(file=log_stream, all_threads=True)
    except Exception:
        pass
    print(f"[gaia-tap] log file: {log_path}")
    return log_stream, orig_stdout, orig_stderr, log_path


def _teardown_file_logging(log_stream: Optional[TextIO], orig_stdout, orig_stderr) -> None:
    if orig_stdout is not None:
        sys.stdout = orig_stdout
    if orig_stderr is not None:
        sys.stderr = orig_stderr
    try:
        faulthandler.disable()
    except Exception:
        pass
    if log_stream is not None:
        try:
            log_stream.flush()
        except Exception:
            pass
        try:
            log_stream.close()
        except Exception:
            pass


def _human_bytes(size: float) -> str:
    value = float(max(0.0, size))
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    return f"{value:.2f} {units[idx]}"


def _fmt_int_ca(value: int) -> str:
    try:
        n = int(value)
    except Exception:
        n = 0
    return f"{n:,}".replace(",", ".")


def _default_output_dir() -> Path:
    layout = ensure_runtime_layout()
    return Path(layout["data_gaia"]).resolve()


def _default_state_file() -> Path:
    layout = ensure_runtime_layout()
    root = Path(layout["root"]).resolve()
    return root / "logs" / "gaia_tap_state.json"


def _ensure_no_gaia_supplement(output_dir: Path) -> None:
    """Copy no-Gaia bright-star supplement beside runtime Gaia catalog when available."""
    try:
        dst = Path(output_dir).resolve() / "no_gaia_stars.json"
        if dst.exists() and dst.is_file() and dst.stat().st_size > 0:
            return
        src = Path(__file__).resolve().parents[1] / "data" / "stars" / "no_gaia_stars.json"
        if not src.exists() or (not src.is_file()):
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f"[gaia-tap] no-Gaia supplement copied to runtime: {dst}")
    except Exception as exc:
        print(f"[gaia-tap] WARNING: could not copy no-Gaia supplement: {exc}", file=sys.stderr)


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    payload = dict(state or {})
    payload["updated_at"] = _iso_now()
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    tmp.replace(state_path)


def _load_state(state_path: Path) -> Optional[dict]:
    if not state_path.exists():
        return None
    try:
        with state_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None
    return None


def _state_completed_rows(state: dict) -> int:
    completed = int(state.get("visible_rows_estimate", 0) or 0) if bool(state.get("visible_ready", False)) else 0
    for batch in state.get("batches", []):
        if bool(batch.get("completed", False)):
            completed += int(batch.get("rows_estimate", 0) or 0)
    return int(max(0, completed))


def _update_state_progress(state: dict) -> float:
    total_rows = int(state.get("total_rows_estimate", 0) or 0)
    completed_rows = _state_completed_rows(state)
    state["completed_rows_estimate"] = int(completed_rows)
    if total_rows <= 0:
        pct = 0.0
    else:
        pct = max(0.0, min(100.0, 100.0 * (float(completed_rows) / float(total_rows))))
    state["progress_percent"] = float(round(pct, 3))
    return float(state["progress_percent"])


def _emit_progress_line(state: dict, message: str) -> None:
    pct = float(_update_state_progress(state))
    print(f"[gaia-progress] {pct:5.1f}% {message}")


def _mag_where_clause(max_mag: float, min_mag_exclusive: Optional[float] = None) -> str:
    clauses = [
        "phot_g_mean_mag IS NOT NULL",
        f"phot_g_mean_mag <= {float(max_mag):.6f}",
    ]
    if min_mag_exclusive is not None:
        clauses.append(f"phot_g_mean_mag > {float(min_mag_exclusive):.6f}")
    return " AND ".join(clauses)


def _build_count_query(mag_limit: float, min_mag_exclusive: Optional[float] = None) -> str:
    where = _mag_where_clause(mag_limit, min_mag_exclusive=min_mag_exclusive)
    return (
        "SELECT COUNT(*) AS total "
        "FROM gaiadr3.gaia_source "
        f"WHERE {where}"
    )


def _build_data_query(
    mag_limit: float,
    max_rows: int = 0,
    min_mag_exclusive: Optional[float] = None,
) -> str:
    top_clause = f"TOP {int(max_rows)} " if int(max_rows) > 0 else ""
    where = _mag_where_clause(mag_limit, min_mag_exclusive=min_mag_exclusive)
    return (
        f"SELECT {top_clause}"
        "source_id, ra, dec, phot_g_mean_mag, bp_rp, pmra, pmdec, parallax "
        "FROM gaiadr3.gaia_source "
        f"WHERE {where}"
        " ORDER BY phot_g_mean_mag ASC"
    )


def _parse_count_csv(text: str) -> int:
    payload = str(text or "").strip()
    if not payload:
        raise RuntimeError("Empty COUNT response from TAP service.")
    reader = csv.DictReader(payload.splitlines())
    rows = list(reader)
    if not rows:
        raise RuntimeError("COUNT response has no rows.")
    first = rows[0]
    for key in ("total", "count", "COUNT", "TOTAL"):
        if key in first:
            return int(float(first[key]))
    # Fallback: first non-empty cell.
    for value in first.values():
        if value not in ("", None):
            return int(float(value))
    raise RuntimeError("Unable to parse COUNT result.")


def _run_sync_count(session, query: str, timeout_s: float) -> int:
    resp = session.get(
        f"{TAP_BASE_URL}/sync",
        params={
            "REQUEST": "doQuery",
            "LANG": "ADQL",
            "FORMAT": "csv",
            "QUERY": query,
        },
        timeout=float(timeout_s),
    )
    resp.raise_for_status()
    return _parse_count_csv(resp.text)


def _run_async_count(
    session,
    query: str,
    *,
    timeout_s: float,
    poll_seconds: float,
    timeout_total_s: float,
    maxrec: int,
) -> int:
    job_url = _start_async_job(
        session,
        query,
        timeout_s=float(timeout_s),
        maxrec=int(maxrec),
    )
    print(f"[gaia-tap] COUNT async job URL: {job_url}")
    _run_async_job(
        session,
        job_url=job_url,
        poll_seconds=float(poll_seconds),
        timeout_total_s=float(timeout_total_s),
    )
    candidates = (
        f"{job_url}/results/result",
        f"{job_url}/results/result.csv",
    )
    last_error: Optional[str] = None
    for url in candidates:
        try:
            resp = session.get(url, timeout=float(timeout_s))
            if resp.status_code != 200:
                last_error = f"{url} -> HTTP {resp.status_code}"
                continue
            return _parse_count_csv(resp.text)
        except Exception as exc:
            last_error = f"{url} -> {exc}"
    raise RuntimeError(f"Could not read async COUNT result. Last error: {last_error}")


def _run_count_with_retry(
    session,
    query: str,
    *,
    timeout_s: float,
    retries: int,
    backoff_seconds: float,
    poll_seconds: float,
    timeout_total_s: float,
    maxrec: int,
    label: str = "COUNT",
) -> int:
    max_attempts = max(1, int(retries))
    base_backoff = max(0.0, float(backoff_seconds))
    last_sync_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            return _run_sync_count(session, query, timeout_s=float(timeout_s))
        except Exception as exc:
            last_sync_error = exc
            print(
                f"[gaia-tap] {label} sync attempt {attempt}/{max_attempts} failed: {exc}",
                file=sys.stderr,
            )
            if attempt < max_attempts and base_backoff > 0.0:
                wait_s = base_backoff * (2.0 ** float(attempt - 1))
                print(f"[gaia-tap] {label} retrying in {wait_s:.1f}s...")
                time.sleep(wait_s)

    print(f"[gaia-tap] {label} switching to async fallback...")
    try:
        return _run_async_count(
            session,
            query,
            timeout_s=float(timeout_s),
            poll_seconds=float(poll_seconds),
            timeout_total_s=float(timeout_total_s),
            maxrec=int(maxrec),
        )
    except Exception as async_exc:
        if last_sync_error is None:
            raise
        raise RuntimeError(
            f"{label} failed in sync+async paths. "
            f"sync_error={last_sync_error} async_error={async_exc}"
        ) from async_exc


def _start_async_job(session, query: str, timeout_s: float, maxrec: int = -1) -> str:
    payload = {
        "REQUEST": "doQuery",
        "LANG": "ADQL",
        "FORMAT": "csv",
        "QUERY": query,
    }
    maxrec_i = int(maxrec)
    # Gaia TAP defaults to a server-side row cap when MAXREC is omitted.
    # We send MAXREC explicitly so users do not silently get truncated catalogs.
    payload["MAXREC"] = str(maxrec_i)
    resp = session.post(
        f"{TAP_BASE_URL}/async",
        data=payload,
        timeout=float(timeout_s),
        allow_redirects=False,
    )
    if resp.status_code not in (200, 201, 303):
        raise RuntimeError(f"TAP async job creation failed: HTTP {resp.status_code}")
    job_url = resp.headers.get("Location", "").strip()
    if not job_url:
        # Some TAP services return the final URL in `resp.url`.
        job_url = str(resp.url or "").strip()
    if not job_url:
        raise RuntimeError("Could not resolve TAP async job URL.")
    return job_url.rstrip("/")


def _run_async_job(session, job_url: str, poll_seconds: float, timeout_total_s: float) -> str:
    phase_url = f"{job_url}/phase"
    start_resp = session.post(phase_url, data={"PHASE": "RUN"}, timeout=20.0)
    if start_resp.status_code not in (200, 303):
        raise RuntimeError(f"Could not start TAP async job: HTTP {start_resp.status_code}")

    started_at = time.time()
    last_phase = ""
    while True:
        phase_resp = session.get(phase_url, timeout=20.0)
        phase_resp.raise_for_status()
        phase = str(phase_resp.text or "").strip().upper()
        if phase != last_phase:
            print(f"[gaia-tap] phase={phase}")
            last_phase = phase
        if phase == "COMPLETED":
            return job_url
        if phase in {"ERROR", "ABORTED"}:
            err_msg = ""
            try:
                err_resp = session.get(f"{job_url}/error", timeout=20.0)
                if err_resp.ok:
                    err_msg = err_resp.text.strip()
            except Exception:
                pass
            raise RuntimeError(f"TAP async job failed ({phase}). {err_msg}".strip())
        if (time.time() - started_at) > float(timeout_total_s):
            raise TimeoutError("Timeout waiting for TAP async job completion.")
        time.sleep(max(0.5, float(poll_seconds)))


def _download_result_csv(session, job_url: str, out_csv: Path, timeout_s: float) -> Path:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    candidates = (
        f"{job_url}/results/result",
        f"{job_url}/results/result.csv",
    )
    last_error: Optional[str] = None
    for url in candidates:
        try:
            with session.get(url, stream=True, timeout=float(timeout_s)) as resp:
                if resp.status_code != 200:
                    last_error = f"{url} -> HTTP {resp.status_code}"
                    continue
                total = int(resp.headers.get("Content-Length", "0") or "0")
                downloaded = 0
                with out_csv.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 512):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = 100.0 * (downloaded / float(total))
                            print(f"[gaia-tap] download {pct:5.1f}% ({downloaded}/{total} bytes)")
                if out_csv.exists() and out_csv.stat().st_size > 0:
                    return out_csv
                last_error = f"{url} produced empty file"
        except Exception as exc:
            last_error = f"{url} -> {exc}"
    raise RuntimeError(f"Could not download TAP result CSV. Last error: {last_error}")


def _confirm(question: str) -> bool:
    try:
        answer = input(question).strip().lower()
    except Exception:
        return False
    return answer in {"y", "yes", "s", "si", "sí"}


def _state_has_pending_work(state: Optional[dict]) -> bool:
    if not isinstance(state, dict):
        return False
    status = str(state.get("status", "")).strip().lower()
    if status in {"done", "completed", "success"}:
        return False
    phase = str(state.get("phase", "")).strip().lower()
    if phase in {"done", "completed"}:
        return False
    return True


def _download_query_to_csv(
    session,
    query: str,
    out_csv: Path,
    *,
    timeout_s: float,
    poll_seconds: float,
    timeout_total_s: float,
    maxrec: int,
) -> Path:
    job_url = _start_async_job(
        session,
        query,
        timeout_s=float(timeout_s),
        maxrec=int(maxrec),
    )
    print(f"[gaia-tap] Job URL: {job_url}")
    _run_async_job(
        session,
        job_url=job_url,
        poll_seconds=float(poll_seconds),
        timeout_total_s=float(timeout_total_s),
    )
    return _download_result_csv(session, job_url, out_csv, timeout_s=float(timeout_s))


_STRUCTURED_DTYPE = np.dtype(
    [
        ("source_id", np.int64),
        ("ra", np.float64),
        ("dec", np.float64),
        ("phot_g_mean_mag", np.float32),
        ("bp_rp", np.float32),
        ("pmra", np.float32),
        ("pmdec", np.float32),
        ("parallax", np.float32),
    ]
)


def _load_no_gaia_structured(path: Path) -> np.ndarray:
    if (not path.exists()) or (not path.is_file()):
        return np.empty(0, dtype=_STRUCTURED_DTYPE)
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return np.empty(0, dtype=_STRUCTURED_DTYPE)

    rows = []
    names = []
    if isinstance(payload, dict):
        rows = payload.get("data") or []
        metadata = payload.get("metadata") or []
        for item in metadata:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item.get("name")))
    elif isinstance(payload, list):
        rows = payload
    if not rows:
        return np.empty(0, dtype=_STRUCTURED_DTYPE)

    out = np.empty(len(rows), dtype=_STRUCTURED_DTYPE)
    out["source_id"] = -1
    out["ra"] = np.nan
    out["dec"] = np.nan
    out["phot_g_mean_mag"] = np.nan
    out["bp_rp"] = 0.8
    out["pmra"] = np.nan
    out["pmdec"] = np.nan
    out["parallax"] = np.nan

    valid_count = 0
    if isinstance(rows[0], dict):
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ra = float(row.get("ra"))
                dec = float(row.get("dec"))
                mag = float(row.get("phot_g_mean_mag"))
                if (not np.isfinite(ra)) or (not np.isfinite(dec)) or (not np.isfinite(mag)):
                    continue
                out["ra"][valid_count] = ra
                out["dec"][valid_count] = dec
                out["phot_g_mean_mag"][valid_count] = mag
                bp = row.get("bp_rp", 0.8)
                out["bp_rp"][valid_count] = float(bp) if bp not in (None, "") else 0.8
                sid = row.get("source_id", -1)
                try:
                    out["source_id"][valid_count] = int(sid)
                except Exception:
                    out["source_id"][valid_count] = -1
                valid_count += 1
            except Exception:
                continue
    else:
        if (not names) and isinstance(rows[0], (list, tuple)):
            names = list(("source_id", "designation", "ra", "dec", "phot_g_mean_mag", "bp_rp")[: len(rows[0])])
        idx = {str(name): i for i, name in enumerate(names)}
        ra_i = idx.get("ra")
        dec_i = idx.get("dec")
        mag_i = idx.get("phot_g_mean_mag")
        bp_i = idx.get("bp_rp")
        sid_i = idx.get("source_id")
        if ra_i is None or dec_i is None or mag_i is None:
            return np.empty(0, dtype=_STRUCTURED_DTYPE)
        for row in rows:
            if not isinstance(row, (list, tuple)):
                continue
            if max(int(ra_i), int(dec_i), int(mag_i)) >= len(row):
                continue
            try:
                ra = float(row[ra_i])
                dec = float(row[dec_i])
                mag = float(row[mag_i])
                if (not np.isfinite(ra)) or (not np.isfinite(dec)) or (not np.isfinite(mag)):
                    continue
                out["ra"][valid_count] = ra
                out["dec"][valid_count] = dec
                out["phot_g_mean_mag"][valid_count] = mag
                if bp_i is not None and int(bp_i) < len(row):
                    try:
                        out["bp_rp"][valid_count] = float(row[bp_i])
                    except Exception:
                        out["bp_rp"][valid_count] = 0.8
                if sid_i is not None and int(sid_i) < len(row):
                    try:
                        out["source_id"][valid_count] = int(row[sid_i])
                    except Exception:
                        out["source_id"][valid_count] = -1
                valid_count += 1
            except Exception:
                continue

    if valid_count <= 0:
        return np.empty(0, dtype=_STRUCTURED_DTYPE)
    out = out[:valid_count]
    order = np.argsort(out["phot_g_mean_mag"], kind="mergesort")
    return np.asarray(out[order], dtype=_STRUCTURED_DTYPE)


def _fuse_no_gaia_into_visible_catalog(visible_npy: Path, no_gaia_json: Path) -> None:
    """Persist no-Gaia bright stars directly inside the visible-runtime NPY."""
    if (not visible_npy.exists()) or (not visible_npy.is_file()):
        return
    supplement = _load_no_gaia_structured(no_gaia_json)
    if int(len(supplement)) <= 0:
        return

    arr = np.load(visible_npy, allow_pickle=False)
    if not isinstance(arr, np.ndarray) or arr.dtype.names is None:
        return
    names = set(arr.dtype.names or ())
    required = {"ra", "dec", "phot_g_mean_mag", "source_id", "bp_rp", "pmra", "pmdec", "parallax"}
    if not required.issubset(names):
        return
    base = np.empty(len(arr), dtype=_STRUCTURED_DTYPE)
    for key in _STRUCTURED_DTYPE.names:
        base[key] = np.asarray(arr[key], dtype=_STRUCTURED_DTYPE.fields[key][0])

    merged = np.concatenate((base, supplement))
    order = np.argsort(merged["phot_g_mean_mag"], kind="mergesort")
    merged = merged[order]

    seen_sid = set()
    keep = np.ones(len(merged), dtype=bool)
    for i, sid in enumerate(merged["source_id"]):
        sid_i = int(sid)
        if sid_i > 0:
            if sid_i in seen_sid:
                keep[i] = False
            else:
                seen_sid.add(sid_i)
    if not np.all(keep):
        merged = merged[keep]

    tmp = visible_npy.with_suffix(visible_npy.suffix + ".tmp")
    with tmp.open("wb") as fh:
        np.save(fh, np.asarray(merged, dtype=_STRUCTURED_DTYPE), allow_pickle=False)
    tmp.replace(visible_npy)
    try:
        (visible_npy.parent / "stars_catalog_no_gaia_fused.flag").write_text(
            datetime.now().isoformat(timespec="seconds"),
            encoding="utf-8",
        )
    except Exception:
        pass
    print(f"[gaia-tap] no-Gaia stars fused into visible cache: +{len(supplement)}")


def _seed_mag_ranges(visible_mag: float, target_mag: float, mag_step: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    lo = float(visible_mag)
    hi_target = float(target_mag)
    step = max(0.05, float(mag_step))
    while lo + 1e-9 < hi_target:
        hi = min(hi_target, lo + step)
        out.append((float(lo), float(hi)))
        lo = hi
    return out


def _plan_extension_batches(
    session,
    *,
    visible_mag: float,
    target_mag: float,
    mag_step: float,
    max_batch_rows: int,
    min_mag_span: float,
    timeout_s: float,
    count_retries: int,
    count_backoff: float,
    poll_seconds: float,
    count_timeout_total_s: float,
    maxrec: int,
) -> list[dict]:
    pending = list(_seed_mag_ranges(visible_mag, target_mag, mag_step))
    planned: list[dict] = []
    min_span = max(0.02, float(min_mag_span))
    max_rows = max(50_000, int(max_batch_rows))

    while pending:
        lo, hi = pending.pop(0)
        span = float(hi - lo)
        if span <= 1e-6:
            continue

        q_count = _build_count_query(float(hi), min_mag_exclusive=float(lo))
        rows = _run_count_with_retry(
            session,
            q_count,
            timeout_s=float(timeout_s),
            retries=int(count_retries),
            backoff_seconds=float(count_backoff),
            poll_seconds=float(poll_seconds),
            timeout_total_s=float(count_timeout_total_s),
            maxrec=int(maxrec),
            label=f"COUNT ({lo:.3f}, {hi:.3f}]",
        )
        if rows <= 0:
            continue

        if rows > max_rows and span > (2.0 * min_span):
            mid = float((lo + hi) * 0.5)
            pending.insert(0, (mid, hi))
            pending.insert(0, (lo, mid))
            continue

        planned.append(
            {
                "mag_min_exclusive": float(lo),
                "mag_max": float(hi),
                "rows_estimate": int(rows),
                "completed": False,
            }
        )

    planned.sort(key=lambda b: (float(b.get("mag_min_exclusive", 0.0)), float(b.get("mag_max", 0.0))))
    return planned


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download Gaia DR3 progressively: visible stars first, then background extension by batches."
        )
    )
    parser.add_argument("--mag-limit", type=float, default=None, help="Maximum G magnitude (e.g. 15.0)")
    parser.add_argument("--resume", action="store_true", help="Resume unfinished background download from state file.")
    parser.add_argument("--state-file", default=str(_default_state_file()), help="Persistent state JSON path.")
    parser.add_argument(
        "--visible-mag",
        type=float,
        default=VISIBLE_MAG_LIMIT_DEFAULT,
        help="First phase magnitude (visible stars, default 8.0).",
    )
    parser.add_argument("--batch-mag-step", type=float, default=0.5, help="Magnitude span per extension batch.")
    parser.add_argument(
        "--max-batch-rows",
        type=int,
        default=2_000_000,
        help="Split batches when estimated rows exceed this threshold.",
    )
    parser.add_argument(
        "--min-mag-span",
        type=float,
        default=0.05,
        help="Minimum magnitude span allowed when auto-splitting heavy batches.",
    )
    parser.add_argument("--max-rows", type=int, default=0, help="Optional TOP N cap for testing")
    parser.add_argument("--output-dir", default=str(_default_output_dir()), help="Destination folder")
    parser.add_argument("--basename", default="stars_catalog", help="Output base name")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="Async TAP poll cadence")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP request timeout")
    parser.add_argument("--count-retries", type=int, default=4, help="Sync COUNT retries before async fallback.")
    parser.add_argument(
        "--count-backoff",
        type=float,
        default=3.0,
        help="Base backoff (seconds) between COUNT retries; exponential per attempt.",
    )
    parser.add_argument(
        "--count-timeout-total",
        type=float,
        default=20 * 60,
        help="Max wait for async COUNT fallback completion (seconds).",
    )
    parser.add_argument(
        "--timeout-total",
        type=float,
        default=8 * 3600,
        help="Max wait for async TAP job completion (seconds)",
    )
    parser.add_argument("--keep-csv", action="store_true", help="Keep downloaded CSV in temp folder")
    parser.add_argument(
        "--maxrec",
        type=int,
        default=-1,
        help="TAP MAXREC parameter (-1 requests no service-side row cap).",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Return success even when imported rows are far below COUNT estimate.",
    )
    parser.add_argument("--write-npy", dest="write_npy", action="store_true", default=True)
    parser.add_argument("--no-write-npy", dest="write_npy", action="store_false")
    parser.add_argument("--write-npz", dest="write_npz", action="store_true", default=False)
    parser.add_argument("--no-write-npz", dest="write_npz", action="store_false")
    parser.add_argument("--write-zst", dest="write_zst", action="store_true", default=False)
    parser.add_argument("--no-write-zst", dest="write_zst", action="store_false")
    parser.add_argument("--log-file", default="", help="Optional path to write a persistent execution log")
    args = parser.parse_args()

    log_stream, orig_stdout, orig_stderr, _ = _setup_file_logging(str(args.log_file or ""))
    session = None
    state: Optional[dict] = None
    state_path = Path(str(args.state_file)).expanduser().resolve()

    def _sig_abort(_signum, _frame):
        raise KeyboardInterrupt()

    try:
        signal.signal(signal.SIGTERM, _sig_abort)
    except Exception:
        pass
    try:
        signal.signal(signal.SIGINT, _sig_abort)
    except Exception:
        pass

    try:
        if requests is None:
            print("ERROR: Missing dependency 'requests'. Install with: pip install requests")
            return 2

        session = requests.Session()
        session.headers.update({"User-Agent": "TerraLab/1.0 (gaia tap downloader)"})

        if bool(args.resume):
            state = _load_state(state_path)
            if not _state_has_pending_work(state):
                print(f"[gaia-tap] No pending state to resume: {state_path}")
                return 0
            print(f"[gaia-tap] Resuming from state: {state_path}")
            state["status"] = "running"
        else:
            mag_limit = args.mag_limit
            if mag_limit is None:
                print("ERROR: --mag-limit is required unless --resume is used.")
                return 2
            mag_limit = float(mag_limit)
            if not (mag_limit > 0.0):
                print("ERROR: --mag-limit must be > 0")
                return 2

            visible_mag = min(float(args.visible_mag), float(mag_limit))
            target_mag = float(mag_limit)
            output_dir = Path(args.output_dir).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            stage_dir = output_dir / f"{args.basename}_tap_batches"
            stage_dir.mkdir(parents=True, exist_ok=True)

            if (not bool(args.yes)) and (not _confirm("Continue with TAP download? [y/N] ")):
                print("[gaia-tap] Cancelled by user.")
                return 0

            print(f"[gaia-tap] COUNT query: mag <= {target_mag:.3f}")
            total_rows = _run_count_with_retry(
                session,
                _build_count_query(target_mag),
                timeout_s=float(args.timeout),
                retries=int(args.count_retries),
                backoff_seconds=float(args.count_backoff),
                poll_seconds=float(args.poll_seconds),
                timeout_total_s=float(args.count_timeout_total),
                maxrec=int(args.maxrec),
                label=f"COUNT mag <= {target_mag:.3f}",
            )
            visible_rows = _run_count_with_retry(
                session,
                _build_count_query(visible_mag),
                timeout_s=float(args.timeout),
                retries=int(args.count_retries),
                backoff_seconds=float(args.count_backoff),
                poll_seconds=float(args.poll_seconds),
                timeout_total_s=float(args.count_timeout_total),
                maxrec=int(args.maxrec),
                label=f"COUNT mag <= {visible_mag:.3f}",
            )
            if int(args.max_rows) > 0:
                total_rows = min(total_rows, int(args.max_rows))
                visible_rows = min(visible_rows, int(args.max_rows))

            batches = []
            if target_mag > visible_mag + 1e-9:
                print(
                    f"[gaia-tap] Planning extension batches: "
                    f"{visible_mag:.2f} < mag <= {target_mag:.2f}"
                )
                batches = _plan_extension_batches(
                    session,
                    visible_mag=visible_mag,
                    target_mag=target_mag,
                    mag_step=float(args.batch_mag_step),
                    max_batch_rows=int(args.max_batch_rows),
                    min_mag_span=float(args.min_mag_span),
                    timeout_s=float(args.timeout),
                    count_retries=int(args.count_retries),
                    count_backoff=float(args.count_backoff),
                    poll_seconds=float(args.poll_seconds),
                    count_timeout_total_s=float(args.count_timeout_total),
                    maxrec=int(args.maxrec),
                )

            for i, batch in enumerate(batches, start=1):
                lo = float(batch["mag_min_exclusive"])
                hi = float(batch["mag_max"])
                batch["csv_path"] = str(stage_dir / f"batch_{i:03d}_{lo:.3f}_{hi:.3f}.csv")

            state = {
                "version": 2,
                "status": "running",
                "phase": "init",
                "started_at": _iso_now(),
                "target_mag": float(target_mag),
                "visible_mag": float(visible_mag),
                "basename": str(args.basename),
                "output_dir": str(output_dir),
                "stage_dir": str(stage_dir),
                "visible_csv_path": str(stage_dir / f"batch_visible_le_{visible_mag:.3f}.csv"),
                "visible_ready": False,
                "merge_ready": False,
                "total_rows_estimate": int(total_rows),
                "visible_rows_estimate": int(visible_rows),
                "batches": batches,
                "completed_rows_estimate": 0,
                "progress_percent": 0.0,
            }
            _update_state_progress(state)
            _save_state(state_path, state)

        assert isinstance(state, dict)
        target_mag = float(state.get("target_mag", 0.0))
        visible_mag = float(state.get("visible_mag", min(VISIBLE_MAG_LIMIT_DEFAULT, target_mag)))
        basename = str(state.get("basename", args.basename))
        output_dir = Path(str(state.get("output_dir", args.output_dir))).resolve()
        stage_dir = Path(str(state.get("stage_dir", output_dir / f"{basename}_tap_batches"))).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        stage_dir.mkdir(parents=True, exist_ok=True)

        if not state.get("visible_csv_path"):
            state["visible_csv_path"] = str(stage_dir / f"batch_visible_le_{visible_mag:.3f}.csv")
        for i, batch in enumerate(state.get("batches", []), start=1):
            if not batch.get("csv_path"):
                lo = float(batch.get("mag_min_exclusive", visible_mag))
                hi = float(batch.get("mag_max", target_mag))
                batch["csv_path"] = str(stage_dir / f"batch_{i:03d}_{lo:.3f}_{hi:.3f}.csv")

        approx_npy = float(state.get("total_rows_estimate", 0)) * 40.0
        approx_csv = float(state.get("total_rows_estimate", 0)) * 72.0
        print(
            "[gaia-tap] Planned dataset:\n"
            f"  target mag     : {target_mag:.3f}\n"
            f"  visible mag    : {visible_mag:.3f}\n"
            f"  rows (COUNT)   : {int(state.get('total_rows_estimate', 0)):,}\n"
            f"  approx CSV     : {_human_bytes(approx_csv)}\n"
            f"  approx NPY     : {_human_bytes(approx_npy)}\n"
            f"  batches        : {len(state.get('batches', []))}"
        )
        print(f"[gaia-tap] TAP MAXREC: {int(args.maxrec)}")
        _ensure_no_gaia_supplement(output_dir)
        _emit_progress_line(state, "descarregant estrelles")
        _save_state(state_path, state)

        # Phase 1: visible stars first (<= visible_mag), so the app can become usable quickly.
        if not bool(state.get("visible_ready", False)):
            state["phase"] = "visible"
            state["status_message"] = "Iniciant TAP"
            _save_state(state_path, state)
            visible_csv = Path(str(state["visible_csv_path"])).resolve()
            print("[gaia-ui] Iniciant TAP")
            print(f"[gaia-tap] Phase 1/3: visible stars <= {visible_mag:.2f}")
            query_visible = _build_data_query(
                visible_mag,
                max_rows=int(args.max_rows),
                min_mag_exclusive=None,
            )
            _download_query_to_csv(
                session,
                query_visible,
                visible_csv,
                timeout_s=float(args.timeout),
                poll_seconds=float(args.poll_seconds),
                timeout_total_s=float(args.timeout_total),
                maxrec=int(args.maxrec),
            )
            print(f"[gaia-tap] Visible CSV ready: {visible_csv} ({_human_bytes(visible_csv.stat().st_size)})")
            state["status_message"] = f"Muntant memòria cau d'estrelles a {output_dir}..."
            _save_state(state_path, state)
            print(f"[gaia-ui] {state['status_message']}")

            def _progress_visible(percent: float, message: str) -> None:
                print(f"[gaia-import] {percent:5.1f}% {message}")

            visible_summary = build_gaia_catalog_from_tables(
                [str(visible_csv)],
                str(output_dir),
                output_basename=str(basename),
                write_npz=bool(args.write_npz),
                write_npy=bool(args.write_npy),
                write_zst=bool(args.write_zst),
                progress_callback=_progress_visible,
            )
            selected_path_visible = (
                visible_summary.get("output_npy")
                or visible_summary.get("output_npz")
                or visible_summary.get("output_zst")
                or ""
            )
            if selected_path_visible:
                set_config_value("gaia_catalog_path", str(selected_path_visible))
                set_config_value("assets.gaia_catalog.ready", True)
                set_config_value("assets.gaia_catalog.path", str(selected_path_visible))
                try:
                    _fuse_no_gaia_into_visible_catalog(
                        Path(str(selected_path_visible)).resolve(),
                        Path(output_dir) / "no_gaia_stars.json",
                    )
                except Exception as fuse_exc:
                    print(f"[gaia-tap] WARNING: no-Gaia fusion skipped: {fuse_exc}", file=sys.stderr)

            state["visible_ready"] = True
            state["status_message"] = "Cataleg visible preparat"
            _emit_progress_line(state, "cataleg visible preparat")
            _save_state(state_path, state)
            print("[gaia-ui] Cataleg visible preparat")

        # Phase 2: extension downloads by batches in background-friendly chunks.
        if target_mag > visible_mag + 1e-9:
            state["phase"] = "extension_download"
            state["status_message"] = "Descarregant extensio Gaia en segon pla"
            _save_state(state_path, state)
            total_batches = len(state.get("batches", []))
            for idx, batch in enumerate(state.get("batches", []), start=1):
                if bool(batch.get("completed", False)):
                    continue
                lo = float(batch.get("mag_min_exclusive", visible_mag))
                hi = float(batch.get("mag_max", target_mag))
                csv_path = Path(str(batch.get("csv_path", stage_dir / f"batch_{idx:03d}.csv"))).resolve()
                csv_path.parent.mkdir(parents=True, exist_ok=True)
                print(
                    f"[gaia-tap] Phase 2/3: batch {idx}/{total_batches} "
                    f"({lo:.3f}, {hi:.3f}] est_rows={int(batch.get('rows_estimate', 0)):,}"
                )
                est_rows = int(batch.get("rows_estimate", 0) or 0)
                state["status_message"] = (
                    f"Descarregant...({idx}/{total_batches}) "
                    f"[0/{_fmt_int_ca(est_rows)} estrelles]"
                )
                _save_state(state_path, state)
                print(f"[gaia-ui] {state['status_message']}")
                query_batch = _build_data_query(
                    hi,
                    max_rows=int(args.max_rows),
                    min_mag_exclusive=lo,
                )
                _download_query_to_csv(
                    session,
                    query_batch,
                    csv_path,
                    timeout_s=float(args.timeout),
                    poll_seconds=float(args.poll_seconds),
                    timeout_total_s=float(args.timeout_total),
                    maxrec=int(args.maxrec),
                )
                batch["completed"] = True
                batch["csv_path"] = str(csv_path)
                _emit_progress_line(state, "descarregant estrelles")
                _save_state(state_path, state)

        # Phase 3: build extension cache (> visible_mag) in a separate runtime NPY.
        if target_mag > visible_mag + 1e-9 and (not bool(state.get("merge_ready", False))):
            state["phase"] = "merge"
            state["status_message"] = f"Muntant memòria cau d'estrelles a {output_dir}..."
            _save_state(state_path, state)
            print(f"[gaia-ui] {state['status_message']}")

            source_paths = []
            for batch in state.get("batches", []):
                if bool(batch.get("completed", False)) and batch.get("csv_path"):
                    source_paths.append(str(Path(str(batch["csv_path"])).resolve()))
            source_paths = [p for p in source_paths if Path(p).exists()]
            if not source_paths:
                state["merge_ready"] = True
                _save_state(state_path, state)
                _save_state(state_path, state)
            else:
                ext_basename = f"{basename}_extension"
                print(f"[gaia-tap] Phase 3/3: building extension cache from {len(source_paths)} CSV batches...")

                def _progress_merge(percent: float, message: str) -> None:
                    print(f"[gaia-import] {percent:5.1f}% {message}")

                extension_summary = build_gaia_catalog_from_tables(
                    source_paths,
                    str(output_dir),
                    output_basename=str(ext_basename),
                    write_npz=bool(args.write_npz),
                    write_npy=bool(args.write_npy),
                    write_zst=bool(args.write_zst),
                    progress_callback=_progress_merge,
                )

                ext_path = (
                    extension_summary.get("output_npy")
                    or extension_summary.get("output_npz")
                    or extension_summary.get("output_zst")
                    or ""
                )
                if ext_path:
                    set_config_value("gaia_catalog_extension_path", str(ext_path))
                    set_config_value("assets.gaia_catalog.extension_path", str(ext_path))

                print("[gaia-tap] Extension import summary:")
                print(json.dumps(extension_summary, indent=2, ensure_ascii=False))

                imported_rows = int(extension_summary.get("rows", 0) or 0)
                expected_rows = int(state.get("total_rows_estimate", 0) or 0) - int(state.get("visible_rows_estimate", 0) or 0)
                ratio = (float(imported_rows) / float(expected_rows)) if expected_rows > 0 else 1.0
                if expected_rows > 0 and ratio < 0.98:
                    msg = (
                        "[gaia-tap] WARNING: extension rows are lower than COUNT estimate.\n"
                        f"  imported_rows : {imported_rows:,}\n"
                        f"  expected_rows : {expected_rows:,}\n"
                        f"  completion    : {ratio * 100.0:.2f}%\n"
                        "Likely causes: TAP server-side truncation (MAXREC), query timeout, or backend limits."
                    )
                    print(msg, file=sys.stderr)
                    if int(args.maxrec) >= 0:
                        print(
                            "[gaia-tap] Hint: use --maxrec -1 to request unbounded rows.",
                            file=sys.stderr,
                        )
                    if not bool(args.allow_partial):
                        print(
                            "[gaia-tap] ERROR: refusing partial extension catalog. "
                            "Use --allow-partial to keep this result intentionally.",
                            file=sys.stderr,
                        )
                        state["status"] = "error"
                        state["phase"] = "merge"
                        state["last_error"] = "partial_catalog_rejected"
                        _save_state(state_path, state)
                        return 3

                state["merge_ready"] = True

        if bool(args.keep_csv):
            print(f"[gaia-tap] Keeping CSV batches in: {stage_dir}")
        else:
            try:
                shutil.rmtree(stage_dir, ignore_errors=True)
            except Exception:
                pass

        state["status"] = "done"
        state["phase"] = "done"
        state["status_message"] = "Finalitzat"
        state["progress_percent"] = 100.0
        state["completed_rows_estimate"] = int(state.get("total_rows_estimate", 0) or 0)
        _save_state(state_path, state)
        print("[gaia-ui] Finalitzat")
        print("[gaia-tap] Done.")
        return 0
    except KeyboardInterrupt:
        print("[gaia-tap] Cancelled by user (KeyboardInterrupt).")
        if isinstance(state, dict):
            state["status"] = "paused"
            state["status_message"] = "Pausat per l'usuari"
            _save_state(state_path, state)
        return 130
    except Exception as exc:
        print(f"[gaia-tap] ERROR: {exc}", file=sys.stderr)
        traceback.print_exc()
        if isinstance(state, dict):
            state["status"] = "error"
            state["last_error"] = str(exc)
            state["status_message"] = f"Error: {exc}"
            _save_state(state_path, state)
        return 1
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        _teardown_file_logging(log_stream, orig_stdout, orig_stderr)


if __name__ == "__main__":
    raise SystemExit(main())
