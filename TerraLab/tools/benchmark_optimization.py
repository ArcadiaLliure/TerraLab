"""Reproducible CPU/out-of-core benchmarks for the performance backends.

Examples:
    python -m TerraLab.tools.benchmark_optimization --terrain-step 5
    python -m TerraLab.tools.benchmark_optimization --catalog data/gaia
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from TerraLab.common.performance import (
    DEFAULT_PERFORMANCE_BUDGET,
    process_memory_bytes,
)
from TerraLab.data.star_catalog_store import create_star_catalog_store
from TerraLab.terrain.engine import HorizonBaker
from TerraLab.terrain.providers import ElevationBatch, create_elevation_provider


class _SyntheticBatchDem:
    def get_elevation(self, x: float, y: float):
        return float(np.float32(250.0 + 0.002 * x - 0.001 * y))

    def sample_elevation(self, x, y):
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        values = (250.0 + 0.002 * x - 0.001 * y).astype(np.float32)
        return ElevationBatch(values, np.ones(values.shape, dtype=bool))

    def get_nominal_resolution_m(self):
        return 5.0


def _measure(function):
    rss_before, _peak_before = process_memory_bytes()
    tracemalloc.start()
    start = time.perf_counter()
    result = function()
    elapsed = time.perf_counter() - start
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after, rss_peak = process_memory_bytes()
    return result, elapsed, {
        "python_peak_bytes": int(peak),
        "rss_before_bytes": int(rss_before),
        "rss_after_bytes": int(rss_after),
        "rss_peak_bytes": int(rss_peak),
    }


def benchmark_terrain(step_m: float, d_max: float, delta_az: float) -> dict:
    baker = HorizonBaker(_SyntheticBatchDem())
    bands = [
        {"id": "near", "min": 0.0, "max": 3_000.0},
        {"id": "mid", "min": 3_000.0, "max": 15_000.0},
        {"id": "far", "min": 15_000.0, "max": d_max},
    ]
    result, elapsed, memory = _measure(
        lambda: baker.bake_progressive(
            obs_x=0.0,
            obs_y=0.0,
            obs_h_ground=250.0,
            step_m=float(step_m),
            d_max=float(d_max),
            delta_az_deg=float(delta_az),
            band_defs=bands,
        )
    )
    field = baker._last_polar_field
    return {
        "step_m": float(step_m),
        "d_max_m": float(d_max),
        "delta_az_deg": float(delta_az),
        "azimuths": int(len(result[0])),
        "polar_samples": int(field.elevations.size if field is not None else 0),
        "elapsed_s": round(elapsed, 6),
        **memory,
    }


def benchmark_streamed_star_math(rows: int, chunk_rows: int = 1_000_000) -> dict:
    """Measure bounded catalogue filtering without allocating all requested rows."""

    rng = np.random.default_rng(20260716)
    processed = 0
    selected = 0
    peak = 0
    rss_before, _rss_peak_before = process_memory_bytes()
    tracemalloc.start()
    start = time.perf_counter()
    while processed < int(rows):
        size = min(int(chunk_rows), int(rows) - processed)
        ra = rng.uniform(0.0, 360.0, size).astype(np.float32)
        dec = rng.uniform(-90.0, 90.0, size).astype(np.float32)
        mag = rng.uniform(-1.0, 22.0, size).astype(np.float32)
        selected += int(
            np.count_nonzero(
                (mag <= 22.0)
                & (dec >= -30.0)
                & (dec <= 30.0)
                & ((ra <= 45.0) | (ra >= 315.0))
            )
        )
        processed += size
        peak = max(peak, tracemalloc.get_traced_memory()[1])
    elapsed = time.perf_counter() - start
    tracemalloc.stop()
    rss_after, rss_peak = process_memory_bytes()
    return {
        "rows": int(rows),
        "selected": int(selected),
        "elapsed_s": round(elapsed, 6),
        "rows_per_second": round(rows / max(elapsed, 1e-9), 1),
        "python_peak_bytes": int(peak),
        "rss_before_bytes": int(rss_before),
        "rss_after_bytes": int(rss_after),
        "rss_peak_bytes": int(rss_peak),
        "chunk_rows": int(chunk_rows),
    }


def benchmark_catalog_query(path: Path, repeats: int) -> dict:
    store = create_star_catalog_store(path)
    try:
        timings = []
        rows = []
        query_metrics = []
        memory = []
        for _ in range(max(1, int(repeats))):
            def _consume_query():
                return sum(
                    len(batch)
                    for batch in store.query_cone(180.0, 0.0, 5.0, 22.0)
                )

            count, elapsed, query_memory = _measure(_consume_query)
            timings.append(elapsed)
            rows.append(int(count))
            memory.append(query_memory)
            query_metrics.append(dict(getattr(store, "last_query_metrics", {})))
        return {
            "path": str(path),
            "rows": rows,
            "timings_s": [round(value, 6) for value in timings],
            "warm_s": round(timings[-1], 6),
            "query_metrics": query_metrics,
            "memory": memory,
        }
    finally:
        store.close()


def benchmark_real_dem(
    path: Path,
    observer_x: float,
    observer_y: float,
    *,
    step_m: float,
    d_max: float,
    delta_az: float,
    repeats: int,
) -> dict:
    """Measure process-cold and warm block-cache passes over an actual DEM."""

    provider = create_elevation_provider(path)
    try:
        ground = provider.get_elevation(float(observer_x), float(observer_y))
        if ground is None:
            raise ValueError("Observer is outside DEM coverage")
        baker = HorizonBaker(provider)
        passes = []
        for pass_index in range(max(2, int(repeats))):
            before = baker._raster_io_metrics()
            _result, elapsed, memory = _measure(
                lambda: baker.bake_progressive(
                    obs_x=float(observer_x),
                    obs_y=float(observer_y),
                    obs_h_ground=float(ground),
                    step_m=float(step_m),
                    d_max=float(d_max),
                    delta_az_deg=float(delta_az),
                )
            )
            after = baker._raster_io_metrics()
            passes.append(
                {
                    "pass": pass_index + 1,
                    "elapsed_s": round(elapsed, 6),
                    "bytes_read": int(after["bytes_read"] - before["bytes_read"]),
                    "cache_hits": int(after["cache_hits"] - before["cache_hits"]),
                    "cache_misses": int(after["cache_misses"] - before["cache_misses"]),
                    **memory,
                }
            )
        return {
            "path": str(path),
            "observer_x": float(observer_x),
            "observer_y": float(observer_y),
            "step_m": float(step_m),
            "d_max_m": float(d_max),
            "delta_az_deg": float(delta_az),
            "process_cold": passes[0],
            "warm": passes[-1],
            "passes": passes,
        }
    finally:
        provider.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terrain-step", type=float, action="append")
    parser.add_argument("--d-max", type=float, default=150_000.0)
    parser.add_argument("--delta-az", type=float, default=0.5)
    parser.add_argument("--star-rows", type=int, action="append")
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--query-repeats", type=int, default=3)
    parser.add_argument("--dem", type=Path)
    parser.add_argument("--observer-x", type=float)
    parser.add_argument("--observer-y", type=float)
    parser.add_argument("--dem-repeats", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    terrain_steps = args.terrain_step or [30.0, 5.0, 1.0]
    star_rows = args.star_rows or [62_000, 1_000_000, 43_600_000, 157_700_000]
    report = {
        "schema_version": 1,
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "logical_cpu": os.cpu_count(),
            "performance_budget_bytes": DEFAULT_PERFORMANCE_BUDGET.total_bytes,
        },
        "terrain": [
            benchmark_terrain(step, args.d_max, args.delta_az)
            for step in terrain_steps
        ],
        "star_stream": [benchmark_streamed_star_math(rows) for rows in star_rows],
    }
    if args.catalog is not None:
        report["catalog_query"] = benchmark_catalog_query(
            args.catalog, args.query_repeats
        )
    if args.dem is not None:
        if args.observer_x is None or args.observer_y is None:
            parser.error("--dem requires --observer-x and --observer-y")
        report["dem_raycast"] = benchmark_real_dem(
            args.dem,
            args.observer_x,
            args.observer_y,
            step_m=float(terrain_steps[0]),
            d_max=args.d_max,
            delta_az=args.delta_az,
            repeats=args.dem_repeats,
        )
    encoded = json.dumps(report, indent=2, ensure_ascii=False)
    print(encoded)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
