from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

from TerraLab.common.app_paths import app_root


RUNNER_CODE = r"""
import argparse
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from TerraLab.common.timestamped_print import enable_timestamped_print
from TerraLab.ui.sky_widget import AstronomicalWidget

parser = argparse.ArgumentParser()
parser.add_argument("--activate-after", type=float, default=-1.0)
parser.add_argument("--quit-after", type=float, default=120.0)
args = parser.parse_args()

enable_timestamped_print()
app = QApplication([])
w = AstronomicalWidget(parent=None, frameless=False)
w.show()

def _activate():
    print("[Benchmark] activate_scope_mode()", flush=True)
    try:
        w.activate_scope_mode()
    except Exception as exc:
        print(f"[Benchmark] activate_scope_mode error: {exc}", flush=True)

if float(args.activate_after) >= 0.0:
    QTimer.singleShot(max(0, int(float(args.activate_after) * 1000.0)), _activate)
QTimer.singleShot(max(1000, int(float(args.quit_after) * 1000.0)), app.quit)
rc = app.exec_()
print(
    f"[Benchmark] exit rc={rc} scope_enabled={w.canvas.scope_mode_enabled()} "
    f"preload_ready={bool(getattr(w, '_scope_preload_ready', False))}",
    flush=True,
)
"""


RUNTIME_READ_RE = re.compile(r"\[stars_dataset\]\s+source=runtime_cache")
CATALOG_LOAD_RE = re.compile(r"Runtime dataset loaded: .* in ([0-9]+(?:\.[0-9]+)?)s")
DATASET_LOADED_RE = re.compile(r"Runtime dataset loaded:")


def _read_perf_events_since(path: Path, offset: int) -> tuple[int, list[dict[str, Any]]]:
    if (not path.exists()) or (not path.is_file()):
        return 0, []
    raw = path.read_bytes()
    if offset < 0 or offset > len(raw):
        offset = 0
    segment = raw[offset:]
    events: list[dict[str, Any]] = []
    for line in segment.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return len(raw), events


def _find_event_ms(events: list[dict[str, Any]], event: str, *, stage: str | None = None) -> float | None:
    for item in events:
        if str(item.get("event", "")) != str(event):
            continue
        if stage is not None and str(item.get("stage", "")) != str(stage):
            continue
        try:
            return float(item.get("delta_ms_boot"))
        except Exception:
            return None
    return None


def _find_duration_ms(
    events: list[dict[str, Any]],
    start_event: str,
    end_event: str,
) -> float | None:
    t0 = _find_event_ms(events, start_event)
    t1 = _find_event_ms(events, end_event)
    if t0 is None or t1 is None:
        return None
    return float(t1 - t0)


def _parse_stdout(stdout: str) -> dict[str, Any]:
    runtime_reads = len(RUNTIME_READ_RE.findall(stdout))
    in_mem_preload = ("Scope preload using in-memory catalog" in stdout)
    scopepreload_lines = stdout.count("[ScopePreload]")
    dataset_loaded_count = len(DATASET_LOADED_RE.findall(stdout))

    catalog_load_s = None
    m = CATALOG_LOAD_RE.search(stdout)
    if m:
        try:
            catalog_load_s = float(m.group(1))
        except Exception:
            catalog_load_s = None

    return {
        "runtime_cache_reads": int(runtime_reads),
        "runtime_dataset_load_count": int(dataset_loaded_count),
        "catalog_load_s": catalog_load_s,
        "used_in_memory_preload": bool(in_mem_preload),
        "scopepreload_subprocess_lines": int(scopepreload_lines),
    }


def _run_once(
    run_idx: int,
    *,
    repo_root: Path,
    perf_log: Path,
    activate_after: float,
    quit_after: float,
) -> dict[str, Any]:
    if perf_log.exists():
        offset = len(perf_log.read_bytes())
    else:
        offset = 0

    cmd = [
        sys.executable,
        "-u",
        "-c",
        RUNNER_CODE,
        "--activate-after",
        f"{float(activate_after):.3f}",
        "--quit-after",
        f"{float(quit_after):.3f}",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        text=True,
        capture_output=True,
    )
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    new_offset, events = _read_perf_events_since(perf_log, offset)
    del new_offset

    out_metrics = _parse_stdout(stdout)
    scene_ready_ms = _find_event_ms(events, "scene_stage", stage="scene_ready")
    catalog_ready_ms = _find_event_ms(events, "catalog_ready")
    preload_start_ms = _find_event_ms(events, "scope_preload_start")
    preload_ready_ms = _find_event_ms(events, "scope_preload_ready")
    preload_duration_ms = _find_duration_ms(events, "scope_preload_start", "scope_preload_ready")
    scope_req_ms = _find_event_ms(events, "scope_activation_request")
    scope_ready_ms = _find_event_ms(events, "scope_activation_ready")
    scope_enabled_ms = _find_event_ms(events, "scope_mode_enabled")
    scope_wait_ms = _find_duration_ms(events, "scope_activation_wait_start", "scope_activation_ready")
    activation_to_enabled_ms = None
    if scope_req_ms is not None and scope_enabled_ms is not None:
        activation_to_enabled_ms = float(scope_enabled_ms - scope_req_ms)

    result = {
        "run": int(run_idx),
        "exit_code": int(proc.returncode),
        "scene_ready_ms": scene_ready_ms,
        "catalog_ready_ms": catalog_ready_ms,
        "preload_start_ms": preload_start_ms,
        "preload_ready_ms": preload_ready_ms,
        "preload_duration_ms": preload_duration_ms,
        "scope_activation_request_ms": scope_req_ms,
        "scope_activation_ready_ms": scope_ready_ms,
        "scope_mode_enabled_ms": scope_enabled_ms,
        "scope_wait_ms": scope_wait_ms,
        "activation_to_enabled_ms": activation_to_enabled_ms,
        "stdout_metrics": out_metrics,
        "stderr_nonempty": bool(stderr.strip()),
    }
    return result


def _fmt_ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}ms"


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark TerraLab startup/scope pipeline automatically.")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--activate-after", type=float, default=95.0)
    parser.add_argument("--quit-after", type=float, default=145.0)
    parser.add_argument("--clear-scope-cache-first", action="store_true")
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    perf_log = app_root() / "logs" / "terralab_perf.log"
    scope_cache_dir = app_root() / "cache" / "scope"
    scope_cache_npz = scope_cache_dir / "scope_index_full_v1.npz"
    scope_cache_meta = scope_cache_dir / "scope_index_full_v1.meta.json"
    if args.clear_scope_cache_first:
        for p in (scope_cache_npz, scope_cache_meta):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    results: list[dict[str, Any]] = []
    for run_idx in range(1, max(1, int(args.runs)) + 1):
        row = _run_once(
            run_idx,
            repo_root=repo_root,
            perf_log=perf_log,
            activate_after=float(args.activate_after),
            quit_after=float(args.quit_after),
        )
        results.append(row)
        stdout_metrics = row["stdout_metrics"]
        print(
            f"run={row['run']} exit={row['exit_code']} "
            f"reads={stdout_metrics['runtime_cache_reads']} "
            f"dataset_loads={stdout_metrics['runtime_dataset_load_count']} "
            f"in_mem={stdout_metrics['used_in_memory_preload']} "
            f"catalog={stdout_metrics['catalog_load_s']}s "
            f"preload={_fmt_ms(row['preload_duration_ms'])} "
            f"wait={_fmt_ms(row['scope_wait_ms'])} "
            f"activation={_fmt_ms(row['activation_to_enabled_ms'])}"
        )

    read_counts = [int(r["stdout_metrics"]["runtime_cache_reads"]) for r in results]
    dataset_load_counts = [int(r["stdout_metrics"]["runtime_dataset_load_count"]) for r in results]
    preload_durations = [float(r["preload_duration_ms"]) for r in results if r["preload_duration_ms"] is not None]
    activation_durations = [
        float(r["activation_to_enabled_ms"]) for r in results if r["activation_to_enabled_ms"] is not None
    ]
    summary = {
        "runs": len(results),
        "runtime_cache_reads_avg": float(statistics.fmean(read_counts)) if read_counts else None,
        "runtime_cache_reads_max": max(read_counts) if read_counts else None,
        "runtime_dataset_load_count_avg": (
            float(statistics.fmean(dataset_load_counts)) if dataset_load_counts else None
        ),
        "runtime_dataset_load_count_max": max(dataset_load_counts) if dataset_load_counts else None,
        "preload_duration_ms_avg": float(statistics.fmean(preload_durations)) if preload_durations else None,
        "activation_to_enabled_ms_avg": float(statistics.fmean(activation_durations)) if activation_durations else None,
        "objective_double_read_ok": (max(dataset_load_counts) <= 1) if dataset_load_counts else None,
        "objective_warm_activation_ok": (
            float(statistics.fmean(activation_durations)) <= 2000.0 if activation_durations else None
        ),
        "results": results,
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output_json:
        out = Path(args.output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
