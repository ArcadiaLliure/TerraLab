"""Download Gaia DR3 stars via ESA TAP and build TerraLab runtime cache."""

from __future__ import annotations

import argparse
import csv
import faulthandler
import json
import traceback
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, TextIO

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


def _default_output_dir() -> Path:
    layout = ensure_runtime_layout()
    return Path(layout["data_gaia"]).resolve()


def _build_count_query(mag_limit: float) -> str:
    return (
        "SELECT COUNT(*) AS total "
        "FROM gaiadr3.gaia_source "
        f"WHERE phot_g_mean_mag IS NOT NULL AND phot_g_mean_mag <= {float(mag_limit):.6f}"
    )


def _build_data_query(mag_limit: float, max_rows: int = 0) -> str:
    top_clause = f"TOP {int(max_rows)} " if int(max_rows) > 0 else ""
    return (
        f"SELECT {top_clause}"
        "source_id, ra, dec, phot_g_mean_mag, bp_rp, pmra, pmdec, parallax "
        "FROM gaiadr3.gaia_source "
        f"WHERE phot_g_mean_mag IS NOT NULL AND phot_g_mean_mag <= {float(mag_limit):.6f}"
    )


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
    text = resp.text.strip()
    if not text:
        raise RuntimeError("Empty COUNT response from TAP service.")
    reader = csv.DictReader(text.splitlines())
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download Gaia DR3 with a magnitude limit (COUNT first), then build TerraLab cache."
        )
    )
    parser.add_argument("--mag-limit", type=float, required=True, help="Maximum G magnitude (e.g. 15.0)")
    parser.add_argument("--max-rows", type=int, default=0, help="Optional TOP N cap for testing")
    parser.add_argument("--output-dir", default=str(_default_output_dir()), help="Destination folder")
    parser.add_argument("--basename", default="stars_catalog", help="Output base name")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="Async TAP poll cadence")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP request timeout")
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
    try:
        if requests is None:
            print("ERROR: Missing dependency 'requests'. Install with: pip install requests")
            return 2

        mag_limit = float(args.mag_limit)
        if not (mag_limit > 0.0):
            print("ERROR: --mag-limit must be > 0")
            return 2

        session = requests.Session()
        session.headers.update({"User-Agent": "TerraLab/1.0 (gaia tap downloader)"})

        count_query = _build_count_query(mag_limit)
        print(f"[gaia-tap] COUNT query: mag <= {mag_limit:.3f}")
        total_rows = _run_sync_count(session, count_query, timeout_s=float(args.timeout))
        if int(args.max_rows) > 0:
            planned_rows = min(total_rows, int(args.max_rows))
        else:
            planned_rows = total_rows

        approx_npy = planned_rows * 40.0
        approx_csv = planned_rows * 72.0
        print(
            "[gaia-tap] Estimated dataset:\n"
            f"  rows          : {planned_rows:,}\n"
            f"  approx CSV    : {_human_bytes(approx_csv)}\n"
            f"  approx NPY    : {_human_bytes(approx_npy)}"
        )
        print(f"[gaia-tap] TAP MAXREC: {int(args.maxrec)}")

        if (not bool(args.yes)) and (not _confirm("Continue with TAP download? [y/N] ")):
            print("[gaia-tap] Cancelled by user.")
            return 0

        data_query = _build_data_query(mag_limit, max_rows=int(args.max_rows))
        print("[gaia-tap] Creating async TAP job...")
        job_url = _start_async_job(
            session,
            data_query,
            timeout_s=float(args.timeout),
            maxrec=int(args.maxrec),
        )
        print(f"[gaia-tap] Job URL: {job_url}")
        _run_async_job(
            session,
            job_url=job_url,
            poll_seconds=float(args.poll_seconds),
            timeout_total_s=float(args.timeout_total),
        )

        with tempfile.TemporaryDirectory(prefix="terralab_gaia_tap_") as tmp_dir:
            tmp_csv = Path(tmp_dir) / "gaia_tap_result.csv"
            print("[gaia-tap] Downloading result CSV...")
            _download_result_csv(session, job_url, tmp_csv, timeout_s=float(args.timeout))
            print(f"[gaia-tap] CSV ready: {tmp_csv} ({_human_bytes(tmp_csv.stat().st_size)})")

            output_dir = Path(args.output_dir).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)

            def _progress(percent: float, message: str) -> None:
                print(f"[gaia-import] {percent:5.1f}% {message}")

            summary = build_gaia_catalog_from_tables(
                [str(tmp_csv)],
                str(output_dir),
                output_basename=str(args.basename),
                write_npz=bool(args.write_npz),
                write_npy=bool(args.write_npy),
                write_zst=bool(args.write_zst),
                progress_callback=_progress,
            )

            selected_path = (
                summary.get("output_npy")
                or summary.get("output_npz")
                or summary.get("output_zst")
                or ""
            )
            if selected_path:
                set_config_value("gaia_catalog_path", str(selected_path))
                set_config_value("assets.gaia_catalog.ready", True)
                set_config_value("assets.gaia_catalog.path", str(selected_path))

            print("[gaia-tap] Import summary:")
            print(json.dumps(summary, indent=2, ensure_ascii=False))

            imported_rows = int(summary.get("rows", 0) or 0)
            expected_rows = int(planned_rows)
            if expected_rows > 0:
                ratio = float(imported_rows) / float(expected_rows)
            else:
                ratio = 1.0
            if expected_rows > 0 and ratio < 0.98:
                msg = (
                    "[gaia-tap] WARNING: imported rows are much lower than COUNT estimate.\n"
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
                        "[gaia-tap] ERROR: refusing partial Gaia catalog. "
                        "Use --allow-partial to keep this result intentionally.",
                        file=sys.stderr,
                    )
                    return 3

            if bool(args.keep_csv):
                persisted_csv = output_dir / f"{args.basename}_tap_download.csv"
                persisted_csv.write_bytes(tmp_csv.read_bytes())
                print(f"[gaia-tap] Kept source CSV: {persisted_csv}")

        print("[gaia-tap] Done.")
        return 0
    except KeyboardInterrupt:
        print("[gaia-tap] Cancelled by user (KeyboardInterrupt).")
        return 130
    except Exception as exc:
        print(f"[gaia-tap] ERROR: {exc}", file=sys.stderr)
        traceback.print_exc()
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
