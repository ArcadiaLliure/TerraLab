"""Reproducible cold/warm benchmark for ASC/NPY spatial tile selection.

Run from the repository root:
    python benchmarks/benchmark_horizon_tile_selection.py
    python benchmarks/benchmark_horizon_tile_selection.py --tiles-dir PATH
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import tracemalloc
from pathlib import Path

import numpy as np

from TerraLab.terrain.infrastructure.dem_tiles import TileIndex


def _brute_force(index: TileIndex, x: np.ndarray, y: np.ndarray) -> int:
    matched = 0
    for tile in index.tiles:
        xmin, ymin, xmax, ymax = tile["bbox"]
        matched += int(
            np.count_nonzero(
                (x >= xmin) & (x < xmax) & (y >= ymin) & (y < ymax)
            )
        )
    return matched


def _indexed(index: TileIndex, x: np.ndarray, y: np.ndarray) -> int:
    return sum(
        int(indices.size)
        for _tile, indices in index.candidate_point_groups(x, y)
    )


def _synthetic_index() -> TileIndex:
    index = TileIndex.__new__(TileIndex)
    columns, rows, size = 38, 23, 1_000.0
    index.tiles = [
        {
            "path": f"tile-{row}-{column}",
            "bbox": (
                column * size,
                row * size,
                (column + 1) * size,
                (row + 1) * size,
            ),
        }
        for row in range(rows)
        for column in range(columns)
    ]
    index.global_bbox = [0.0, 0.0, columns * size, rows * size]
    index._spatial_cells = {}
    index._build_spatial_index()
    return index


def _points(index: TileIndex, count: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(20260717)
    xmin, ymin, xmax, ymax = index.global_bbox
    margin = max(xmax - xmin, ymax - ymin) * 0.1
    return (
        rng.uniform(xmin - margin, xmax + margin, count),
        rng.uniform(ymin - margin, ymax + margin, count),
    )


def _measure(function, index, x, y, repeats: int) -> dict:
    durations = []
    matched = None
    for _ in range(repeats):
        started = time.perf_counter()
        current = function(index, x, y)
        durations.append(time.perf_counter() - started)
        if matched is not None and current != matched:
            raise RuntimeError("Benchmark result changed between repetitions")
        matched = current
    tracemalloc.start()
    memory_result = function(index, x, y)
    _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if memory_result != matched:
        raise RuntimeError("Benchmark result changed during memory measurement")
    return {
        "median_s": statistics.median(durations),
        "runs_s": durations,
        "peak_bytes": peak_bytes,
        "matched_points": matched,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tiles-dir", type=Path)
    parser.add_argument("--points", type=int, default=240_000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    index = TileIndex(str(args.tiles_dir)) if args.tiles_dir else _synthetic_index()
    x, y = _points(index, max(1, args.points))
    brute = _measure(_brute_force, index, x, y, max(1, args.repeats))
    indexed = _measure(_indexed, index, x, y, max(1, args.repeats))
    if brute["matched_points"] != indexed["matched_points"]:
        raise RuntimeError("Indexed lookup is not equivalent to brute force")
    output = {
        "dataset": str(args.tiles_dir) if args.tiles_dir else "synthetic-874-tiles",
        "tiles": len(index.tiles),
        "points": int(x.size),
        "cold": {"brute_s": brute["runs_s"][0], "indexed_s": indexed["runs_s"][0]},
        "brute": brute,
        "indexed": indexed,
        "speedup": brute["median_s"] / indexed["median_s"],
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
