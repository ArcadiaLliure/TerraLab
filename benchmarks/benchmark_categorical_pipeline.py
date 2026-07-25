"""Reproducible S2GLC sampling and categorical-material benchmark.

The benchmark never reads from a paint callback.  It samples an isolated cold
cache, reopens the provider for the persistent-cache pass, and benchmarks the
old RGBA interpolation primitive against discrete material resolution using
the same rasterized pixels.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import tempfile
import time
from pathlib import Path

import numpy as np
from PyQt5.QtGui import QImage

from TerraLab.common.performance.memory import process_memory_bytes
from TerraLab.terrain.crs import DEFAULT_TRANSFORM_SERVICE
from TerraLab.terrain.render.overlay_types import TerrainMaterialSamples
from TerraLab.terrain.render.triangle_raster import (
    _interpolate_triangle_continuous_values,
    _resolve_surface_material,
)
from TerraLab.terrain.render.config import TerrainRenderSettings
from TerraLab.terrain.render.materials import compose_vertex_rgba
from TerraLab.terrain.surface import CategoricalSurfaceProvider
from TerraLab.terrain.surface_store import AtomicNpzStore


TORROJA_LAT = 41.2135485
TORROJA_LON = 0.8097282
DEFAULT_S2GLC = Path(
    r"I:\TerraLab\data\earth\surface\Cobertura_del_s_l_categ_rica"
    r"\S2GLC_Europe_2017_v1.2.tif"
)


def _save_rgba(path: Path, rgba: np.ndarray) -> None:
    pixels = np.ascontiguousarray(rgba, dtype=np.uint8)
    image = QImage(
        pixels.data,
        int(pixels.shape[1]),
        int(pixels.shape[0]),
        int(pixels.strides[0]),
        QImage.Format_RGBA8888,
    ).copy()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(path)):
        raise OSError(f"Could not write {path}")


def _query_grid(rows: int, columns: int):
    centre_x, centre_y = DEFAULT_TRANSFORM_SERVICE.transform_xy(
        TORROJA_LON, TORROJA_LAT, "EPSG:4326", "EPSG:3035"
    )
    distances = np.geomspace(10.0, 30_000.0, rows)
    azimuths = np.linspace(0.0, 360.0, columns, endpoint=False)
    radians = np.deg2rad(azimuths)
    x = centre_x + distances[:, None] * np.sin(radians)[None, :]
    y = centre_y + distances[:, None] * np.cos(radians)[None, :]
    projected_native_px = 10.0 / np.maximum(distances, 10.0) * 1100.0
    maximum_factor = np.maximum(
        1.0, np.floor(2.0 / np.maximum(projected_native_px, 1e-12))
    )
    levels = np.asarray((1, 2, 4, 8, 16, 32, 64, 128), dtype=np.int16)
    positions = np.searchsorted(levels, maximum_factor, side="right") - 1
    radial_lod = levels[np.clip(positions, 0, len(levels) - 1)]
    radial_lod[projected_native_px >= 0.5] = 1
    lod = np.broadcast_to(radial_lod[:, None], x.shape)
    return x, y, lod, distances


def _sample_pass(path: Path, cache_root: Path, x, y, lod):
    store = AtomicNpzStore(cache_root, budget_bytes=2 * 1024**3)
    provider = CategoricalSurfaceProvider(
        path,
        source_id="s2glc-europe-2017",
        source_name="S2GLC Europe 2017",
        legend_id="s2glc",
        tile_store=store,
    )
    provider.initialize()
    rss_before, peak_before = process_memory_bytes()
    started = time.perf_counter()
    samples = provider.sample_classes(
        x,
        y,
        input_crs="EPSG:3035",
        lod_factors=lod,
    )
    elapsed = time.perf_counter() - started
    rss_after, peak_after = process_memory_bytes()
    dataset = provider._datasets[0]
    metrics = {
        "elapsed_s": elapsed,
        "valid_samples": int(np.count_nonzero(samples.valid)),
        "requested_samples": int(samples.valid.size),
        "raster_bytes": int(dataset.bytes_read),
        "lod_windows": int(dataset.lod_windows_read),
        "lod_rows": int(dataset.lod_rows_read),
        "lod_pixels_decoded": int(dataset.lod_pixels_decoded),
        "lod_unique_cells": int(dataset.lod_unique),
        "lod_modal_cells": int(dataset.lod_modal_cells),
        "lod_cache_hits": int(dataset.lod_cache_hits),
        "rss_delta_bytes": int(rss_after - rss_before),
        "peak_rss_delta_bytes": int(max(0, peak_after - peak_before)),
        "persistent_cache": store.metrics(),
    }
    rgba = provider.classes_to_rgba(samples.classes).rgba
    provider.close()
    return samples, rgba, metrics


def _render_microbenchmark(iterations: int = 12):
    # Interactive camera motion uses the renderer's 0.5 scale for a
    # 960x540 viewport; the final idle frame is cached at full resolution.
    height, width = 270, 480
    triangle_id = np.zeros((height, width), dtype=np.int32)
    x = (np.arange(width, dtype=np.float32)[None, :] + 0.5) / width
    y = (np.arange(height, dtype=np.float32)[:, None] + 0.5) / height
    u = np.broadcast_to(np.clip(1.0 - x - 0.25 * y, 0.0, 1.0), (height, width))
    v = np.broadcast_to(np.clip(x * (1.0 - 0.25 * y), 0.0, 1.0), (height, width))
    total = np.maximum(u + v, 1.0)
    u = u / total
    v = v / total
    triangle_rgba = np.asarray(
        [[[40, 120, 40, 255], [40, 120, 40, 255], [135, 82, 180, 255]]],
        dtype=np.uint8,
    )
    materials = TerrainMaterialSamples(
        triangle_rgba,
        np.ones((1, 3), dtype=bool),
        np.asarray([[83, 83, 105]], dtype=np.int64),
        np.ones((1, 3), dtype=bool),
        np.zeros((1, 3), dtype=np.int16),
    )
    polar_rgba = np.asarray(
        [
            [[40, 120, 40, 255], [40, 120, 40, 255]],
            [[135, 82, 180, 255], [135, 82, 180, 255]],
            [[40, 120, 40, 255], [40, 120, 40, 255]],
        ],
        dtype=np.uint8,
    )
    polar_materials = TerrainMaterialSamples(
        polar_rgba,
        np.ones((3, 2), dtype=bool),
        np.asarray([[83, 83], [105, 105], [83, 83]], dtype=np.int64),
        np.ones((3, 2), dtype=bool),
        np.zeros((3, 2), dtype=np.int16),
    )
    empty_materials = TerrainMaterialSamples(
        np.empty((0, 0, 4), dtype=np.uint8),
        np.empty((0, 0), dtype=bool),
        np.empty((0, 0), dtype=np.int64),
        np.empty((0, 0), dtype=bool),
        np.empty((0, 0), dtype=np.int16),
    )
    triangle_surface_xy = np.asarray(
        [
            [
                (0.0, 2_000.0),
                (
                    20_000.0 * math.sin(math.radians(20.0)),
                    20_000.0 * math.cos(math.radians(20.0)),
                ),
                (0.0, 8_000.0),
            ]
        ],
        dtype=np.float64,
    )
    light = np.asarray([[[0.65], [1.15], [0.9]]], dtype=np.float64)
    distance = np.asarray([[[2_000.0], [20_000.0], [8_000.0]]], dtype=np.float64)
    settings = TerrainRenderSettings()
    old_times = []
    new_times = []
    resolved = None
    final = None
    warmup_started = time.perf_counter()
    _resolve_surface_material(
        triangle_id,
        u,
        v,
        materials,
        triangle_surface_xy,
        np.zeros((1, 3), dtype=np.uint8),
        polar_materials,
        np.asarray([2_000.0, 8_000.0, 20_000.0]),
        np.asarray([0.0, 20.0]),
        empty_materials,
        np.empty(0),
        np.empty(0),
    )
    resolver_warmup_s = time.perf_counter() - warmup_started
    for rotation in range(iterations):
        shifted_u = np.roll(u, rotation * 7, axis=1)
        shifted_v = np.roll(v, rotation * 7, axis=1)
        started = time.perf_counter()
        _interpolate_triangle_continuous_values(
            triangle_id, shifted_u, shifted_v, triangle_rgba.astype(np.float64)
        )
        old_times.append(time.perf_counter() - started)

        started = time.perf_counter()
        resolved = _resolve_surface_material(
            triangle_id,
            shifted_u,
            shifted_v,
            materials,
            triangle_surface_xy,
            np.zeros((1, 3), dtype=np.uint8),
            polar_materials,
            np.asarray([2_000.0, 8_000.0, 20_000.0]),
            np.asarray([0.0, 20.0]),
            empty_materials,
            np.empty(0),
            np.empty(0),
        )
        continuous, _ = _interpolate_triangle_continuous_values(
            triangle_id,
            shifted_u,
            shifted_v,
            np.concatenate((light, distance), axis=2),
        )
        final = compose_vertex_rgba(
            resolved.base_rgba,
            continuous[..., 0],
            continuous[..., 1],
            settings,
            maximum_distance_m=30_000.0,
        )
        new_times.append(time.perf_counter() - started)
    old_sorted = sorted(old_times)
    new_sorted = sorted(new_times)
    percentile_index = max(0, math.ceil(0.95 * iterations) - 1)
    return (
        {
            "iterations": iterations,
            "viewport": [960, 540],
            "interactive_render_size": [width, height],
            "legacy_rgba_interpolation_median_s": statistics.median(old_times),
            "legacy_rgba_interpolation_p95_s": old_sorted[percentile_index],
            "material_resolver_warmup_s": resolver_warmup_s,
            "categorical_pipeline_median_s": statistics.median(new_times),
            "categorical_pipeline_p95_s": new_sorted[percentile_index],
            "categorical_pipeline_median_fps": 1.0
            / max(statistics.median(new_times), 1e-12),
        },
        resolved.base_rgba,
        final,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--surface-path", type=Path, default=DEFAULT_S2GLC)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=48)
    parser.add_argument("--columns", type=int, default=120)
    args = parser.parse_args()
    if not args.surface_path.is_file():
        parser.error(f"S2GLC raster not found: {args.surface_path}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    x, y, lod, distances = _query_grid(args.rows, args.columns)
    with tempfile.TemporaryDirectory(prefix="terralab-s2glc-benchmark-") as cache:
        cold_samples, class_rgba, cold = _sample_pass(
            args.surface_path, Path(cache), x, y, lod
        )
        _warm_samples, _warm_rgba, warm = _sample_pass(
            args.surface_path, Path(cache), x, y, lod
        )
    render, material_rgba, final_rgba = _render_microbenchmark()
    valid_classes, counts = np.unique(
        cold_samples.classes[cold_samples.valid], return_counts=True
    )
    report = {
        "scene": {
            "name": "Torroja del Priorat",
            "latitude": TORROJA_LAT,
            "longitude": TORROJA_LON,
            "radius_m": float(distances[-1]),
            "grid": [int(args.rows), int(args.columns)],
            "surface_path": str(args.surface_path),
        },
        "cold": cold,
        "warm_restart": warm,
        "render_rotation_proxy": render,
        "class_histogram": {
            str(int(class_id)): int(count)
            for class_id, count in zip(valid_classes, counts)
        },
    }
    _save_rgba(args.output_dir / "torroja-class-only.png", class_rgba)
    _save_rgba(args.output_dir / "triangle-material-only.png", material_rgba)
    _save_rgba(args.output_dir / "triangle-final.png", final_rgba)
    output = args.output_dir / "benchmark.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
