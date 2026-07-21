from __future__ import annotations

import math

import numpy as np
import rasterio
from rasterio.transform import from_origin

from TerraLab.common.performance import (
    ByteLRU,
    GenerationController,
    GiB,
    PerformanceBudget,
    PerformanceFlags,
)
from TerraLab.data.star_catalog_store import HealpixStarCatalogStore
from TerraLab.terrain.engine import HorizonBaker, TileCache, TileIndex
from TerraLab.terrain.providers import (
    AscRasterProvider,
    ElevationBatch,
    TiffRasterWindowProvider,
)
from TerraLab.terrain import bake_process
from TerraLab.terrain.asc_cache_builder import materialize_asc_caches_spawned
from TerraLab.util.gaia_importer import build_gaia_catalog_spawned
from TerraLab.util.color import _bp_rp_formula_arrays, bp_rp_to_rgb_arrays


class _ScalarSyntheticProvider:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def _values(x, y):
        distance = np.hypot(x, y)
        values = 120.0 + 0.003 * x - 0.0015 * y + 8.0 * np.sin(distance / 80.0)
        valid = ~((distance >= 91.0) & (distance < 141.0))
        return values, valid

    def get_elevation(self, x: float, y: float):
        self.calls += 1
        values, valid = self._values(float(x), float(y))
        return float(np.float32(values)) if bool(valid) else None

    def get_nominal_resolution_m(self):
        return 5.0


class _BatchSyntheticProvider(_ScalarSyntheticProvider):
    def __init__(self) -> None:
        super().__init__()
        self.batch_calls = 0

    def sample_elevation(self, x, y):
        self.batch_calls += 1
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        values, valid = self._values(x, y)
        return ElevationBatch(values, valid)


def _band_definitions():
    return [
        {"id": "near", "min": 0.0, "max": 100.0},
        {"id": "far", "min": 100.0, "max": 2_000.0},
    ]


def test_performance_budget_uses_shared_40_40_20_split():
    budget = PerformanceBudget.for_machine(16 * GiB)
    assert budget.total_bytes == int(16 * GiB * 0.30)
    assert budget.dem_bytes == int(budget.total_bytes * 0.40)
    assert budget.stars_bytes == int(budget.total_bytes * 0.40)
    assert budget.transient_bytes == budget.total_bytes - budget.dem_bytes - budget.stars_bytes
    assert budget.batch_rows(256) <= 1_000_000
    assert budget.batch_rows(256) * 256 <= 128 * 1024**2


def test_byte_lru_evicts_by_resident_bytes():
    cache = ByteLRU(max_bytes=10)
    cache.put("a", "A", 6)
    cache.put("b", "B", 6)
    assert cache.get("a") is None
    assert cache.get("b") == "B"
    assert cache.resident_bytes == 6
    assert cache.evictions == 1


def test_performance_backends_have_independent_rollback_flags(monkeypatch):
    monkeypatch.setenv("TERRALAB_RASTER_BATCH", "0")
    monkeypatch.setenv("TERRALAB_RAYCAST_VECTORIZED", "false")
    monkeypatch.setenv("TERRALAB_RELIEF_CACHED", "off")
    monkeypatch.setenv("TERRALAB_GAIA_OUT_OF_CORE", "no")
    assert PerformanceFlags.from_environment() == PerformanceFlags(
        raster_batch=False,
        raycast_vectorized=False,
        relief_cached=False,
        gaia_out_of_core=False,
    )


def test_star_colour_lut_stays_within_one_channel_level():
    values = np.linspace(-0.5, 2.5, 20_003, dtype=np.float32)
    expected = _bp_rp_formula_arrays(values)
    actual = bp_rp_to_rgb_arrays(values)
    for expected_channel, actual_channel in zip(expected, actual):
        delta = np.abs(
            expected_channel.astype(np.int16) - actual_channel.astype(np.int16)
        )
        assert int(delta.max(initial=0)) <= 1


def test_vectorized_raycast_matches_scalar_with_nodata_exit():
    kwargs = dict(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=2_000.0,
        delta_az_deg=10.0,
        band_defs=_band_definitions(),
    )
    scalar = HorizonBaker(_ScalarSyntheticProvider()).bake_progressive(**kwargs)
    batch_provider = _BatchSyntheticProvider()
    vector = HorizonBaker(batch_provider).bake_progressive(**kwargs)

    np.testing.assert_array_equal(vector[0], scalar[0])
    np.testing.assert_array_equal(vector[4], scalar[4])
    for scalar_band, vector_band in zip(scalar[1], vector[1]):
        for key in (
            "angles",
            "dists",
            "heights",
            "surface_angles",
            "surface_dists",
            "surface_heights",
        ):
            np.testing.assert_allclose(
                vector_band[key],
                scalar_band[key],
                rtol=0.0,
                atol=1e-7 if "angle" in key or "dist" in key else 1e-5,
            )
    assert batch_provider.batch_calls <= math.ceil(len(vector[0]) / 64)


def test_vectorized_light_sampling_uses_batch_api_and_matches_scalar():
    class ScalarTerrain(_ScalarSyntheticProvider):
        @staticmethod
        def _values(x, y):
            values, _valid = _ScalarSyntheticProvider._values(x, y)
            return values, np.ones(np.asarray(values).shape, dtype=bool)

    class BatchTerrain(ScalarTerrain):
        def __init__(self):
            super().__init__()
            self.batch_calls = 0

        def sample_elevation(self, x, y):
            self.batch_calls += 1
            values, valid = self._values(
                np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
            )
            return ElevationBatch(values, valid)

    class LightSampler:
        def __init__(self):
            self.scalar_calls = 0
            self.batch_calls = 0

        @staticmethod
        def _value(x, y):
            return np.full(np.broadcast(np.asarray(x), np.asarray(y)).shape, 3.5)

        def get_radiance_terrain_xy(self, x, y, input_crs=None):
            self.scalar_calls += 1
            return float(np.float32(self._value(x, y)))

        def get_radiance_terrain_xy_batch(self, x, y, input_crs=None):
            self.batch_calls += 1
            return self._value(x, y).astype(np.float32)

    kwargs = dict(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=5_000.0,
        delta_az_deg=10.0,
        band_defs=_band_definitions(),
    )
    scalar_light = LightSampler()
    scalar = HorizonBaker(ScalarTerrain()).bake_progressive(
        **kwargs, light_sampler=scalar_light
    )
    batch_light = LightSampler()
    vector = HorizonBaker(BatchTerrain()).bake_progressive(
        **kwargs, light_sampler=batch_light
    )
    np.testing.assert_allclose(vector[2], scalar[2], rtol=1e-6, atol=1e-6)
    np.testing.assert_allclose(vector[3], scalar[3], rtol=0.0, atol=1e-6)
    assert batch_light.batch_calls > 0
    assert batch_light.scalar_calls == 0


def test_tile_spatial_index_matches_brute_force_candidates():
    index = TileIndex.__new__(TileIndex)
    index.tiles = [
        {"path": "first", "bbox": (0.0, 0.0, 10.0, 10.0)},
        {"path": "overlap", "bbox": (5.0, 0.0, 15.0, 10.0)},
        {"path": "north", "bbox": (0.0, 10.0, 10.0, 20.0)},
    ]
    index.global_bbox = [0.0, 0.0, 15.0, 20.0]
    index._spatial_cell_size = 1.0
    index._spatial_origin = (0.0, 0.0)
    index._spatial_cells = {}
    index._build_spatial_index()
    x = np.asarray([-1.0, 0.0, 7.0, 10.0, 14.9, 5.0])
    y = np.asarray([5.0, 0.0, 5.0, 5.0, 9.9, 10.0])

    actual = {
        tile["path"]: set(indices.tolist())
        for tile, indices in index.candidate_point_groups(x, y)
    }
    expected = {}
    for tile in index.tiles:
        xmin, ymin, xmax, ymax = tile["bbox"]
        indices = np.flatnonzero(
            (x >= xmin) & (x < xmax) & (y >= ymin) & (y < ymax)
        )
        if indices.size:
            expected[tile["path"]] = set(indices.tolist())
    assert actual == expected


def test_asc_batch_spatial_sampling_preserves_overlap_nodata_fallback():
    index = TileIndex.__new__(TileIndex)
    first = {
        "path": "first",
        "bbox": (0.0, 0.0, 2.0, 2.0),
        "header": {"NPY": True, "NODATA_VALUE": -9999.0},
    }
    fallback = {
        "path": "fallback",
        "bbox": (1.0, 0.0, 3.0, 2.0),
        "header": {"NPY": True},
    }
    index.tiles = [first, fallback]
    index.global_bbox = [0.0, 0.0, 3.0, 2.0]
    index._spatial_cell_size = 1.0
    index._spatial_origin = (0.0, 0.0)
    index._spatial_cells = {}
    index._build_spatial_index()

    class Cache:
        cache_hits = 0
        cache_misses = 0
        bytes_read = 0

        def load(self, tile):
            if tile["path"] == "first":
                return np.asarray(
                    [[1.0, 1.0, -9999.0, -9999.0]] * 2
                ), tile["header"]
            return np.full((2, 2), 7.0), tile["header"]

    provider = AscRasterProvider("unused")
    provider.index = index
    provider.cache = Cache()
    batch = provider._sample_native_elevation(
        np.asarray([0.25, 1.25, 2.25, 4.0]),
        np.asarray([1.25, 1.25, 1.25, 1.25]),
    )
    np.testing.assert_allclose(batch.values, [1.0, 7.0, 7.0, 0.0])
    assert batch.valid.tolist() == [True, True, True, False]
    assert provider.sample_candidate_tiles == 2
    assert provider.sampled_points == 4


def test_tile_cache_prioritizes_next_batch_before_eviction():
    cache = TileCache(capacity=10, max_bytes=12)
    value = (np.zeros(1, dtype=np.float32), {})
    cache._store("b", value)
    cache._store("c", value)
    cache._store("a", value)

    cache.prioritize(
        [{"path": "b"}, {"path": "c"}, {"path": "d"}]
    )
    cache._store("d", value)

    assert tuple(cache.cache) == ("b", "c", "d")


def test_tile_cache_keeps_pinned_near_field_inside_byte_budget():
    cache = TileCache(capacity=10, max_bytes=12)
    value = (np.zeros(1, dtype=np.float32), {})
    cache._store("near", value)
    cache._store("far-a", value)
    cache._store("far-b", value)
    cache.pin([{"path": "near"}])

    cache._store("far-c", value)

    assert "near" in cache.cache
    assert tuple(cache.cache) == ("far-b", "near", "far-c")
    assert cache.resident_bytes <= cache.max_bytes


def test_vectorized_progress_and_previews_are_emitted_at_batch_boundaries():
    provider = _BatchSyntheticProvider()
    progress = []
    previews = []
    order = list(reversed(range(36)))
    result = HorizonBaker(provider).bake_progressive(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=2_000.0,
        delta_az_deg=10.0,
        band_defs=_band_definitions(),
        azimuth_order=order,
        preview_every=8,
        progress_callback=lambda percent, _message: progress.append(percent),
        preview_callback=lambda current, total, *_args: previews.append(
            (current, total, np.flatnonzero(_args[-1]).tolist())
        ),
    )
    assert progress == sorted(progress)
    assert progress[-1] == 100.0
    assert [item[:2] for item in previews] == [(8, 36), (36, 36)]
    assert set(previews[0][2]) == set(order[:8])
    assert np.all(result[4])


def test_vectorized_preview_count_is_bounded_and_finishes():
    provider = _BatchSyntheticProvider()
    previews = []
    total_azimuths = 360
    result = HorizonBaker(provider).bake_progressive(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=2_000.0,
        delta_az_deg=1.0,
        band_defs=_band_definitions(),
        preview_every=math.ceil(total_azimuths / 18),
        preview_callback=lambda current, total, *_args: previews.append(
            (current, total)
        ),
    )
    assert 1 <= len(previews) <= 18
    assert previews[-1] == (total_azimuths, total_azimuths)
    assert np.all(result[4])


def test_vectorized_polar_field_retains_only_mesh_distance_rings():
    provider = _BatchSyntheticProvider()
    baker = HorizonBaker(provider)
    baker.bake_progressive(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=20_000.0,
        delta_az_deg=10.0,
        band_defs=_band_definitions(),
    )
    field = baker._last_polar_field
    assert field is not None
    expected_rings = baker._mesh_distance_rings(20_000.0, 5.0)
    assert field.elevations.shape == (expected_rings.size, 36)
    np.testing.assert_array_equal(field.distances, expected_rings)


def test_prepare_progress_is_throttled_to_meaningful_percent_changes(monkeypatch):
    events = []
    monkeypatch.setattr(
        bake_process,
        "_emit_event",
        lambda event_type, **payload: events.append((event_type, payload)),
    )
    callback = bake_process._phase_progress("job", "prepare", 15.0, 35.0)
    for percent in range(101):
        callback(percent)
    mapped = [payload["percent"] for _event_type, payload in events]
    assert mapped == sorted(mapped)
    assert mapped[-1] == 35.0
    assert len(mapped) <= 22


def test_preview_payload_is_resolved_only_and_capped_to_1440_azimuths():
    count = 72_000
    azimuths = np.arange(count, dtype=np.float32) * np.float32(0.005)
    values = np.arange(count, dtype=np.float32)
    resolved = np.ones(count, dtype=bool)
    reduced = bake_process._reduced_preview_payload(
        azimuths,
        [
            {
                "id": "test",
                "min": 0.0,
                "max": 1.0,
                "angles": values,
                "dists": values,
                "heights": values,
            }
        ],
        values,
        values,
        resolved,
    )
    assert reduced[0].size == 1_440
    assert reduced[1][0]["angles"].size == 1_440
    assert np.all(reduced[4])
    assert np.all(np.diff(reduced[0]) > 0.0)


def test_relief_mesh_reuses_the_polar_field_without_new_dem_reads():
    provider = _BatchSyntheticProvider()
    baker = HorizonBaker(provider)
    baker.bake_progressive(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=2_000.0,
        delta_az_deg=10.0,
        band_defs=_band_definitions(),
    )
    calls_after_bake = provider.batch_calls
    mesh = baker.build_view_mesh(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        d_max=2_000.0,
        delta_az_deg=10.0,
    )
    assert provider.batch_calls == calls_after_bake
    assert mesh["altitudes"].shape == mesh["valid"].shape


def test_relief_mesh_precision_is_decoupled_from_subpixel_ray_step():
    assert HorizonBaker._mesh_azimuth_step(0.005) == 0.05
    assert HorizonBaker._mesh_azimuth_step(0.03) == 0.06
    assert HorizonBaker._mesh_azimuth_step(0.331) == 0.331


def test_fine_raycast_retains_only_capped_relief_azimuths():
    provider = _BatchSyntheticProvider()
    baker = HorizonBaker(provider)
    baker.bake_progressive(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=200.0,
        delta_az_deg=0.025,
        band_defs=[{"id": "all", "min": 0.0, "max": 200.0}],
    )
    field = baker._last_polar_field
    assert field is not None
    assert field.delta_az_deg == 0.05
    assert field.azimuths.size == 7_200

    calls_after_bake = provider.batch_calls
    mesh = baker.build_view_mesh(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        d_max=200.0,
        delta_az_deg=0.025,
    )
    assert provider.batch_calls == calls_after_bake
    assert mesh["azimuths"].size == 7_200


def test_geotiff_prepare_region_never_materializes_the_full_roi(tmp_path):
    path = tmp_path / "bounded.tif"
    data = np.full((512, 512), 42.0, dtype=np.float32)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=512,
        height=512,
        count=1,
        dtype="float32",
        crs="EPSG:25831",
        transform=from_origin(0.0, 512.0, 1.0, 1.0),
        tiled=True,
        blockxsize=64,
        blockysize=64,
    ) as dataset:
        dataset.write(data, 1)
    provider = TiffRasterWindowProvider(path)
    provider.initialize()
    try:
        provider.prepare_region(128.0, 384.0, 150_000.0)
        batch = provider.sample_elevation([128.0, 129.0], [384.0, 383.0])
        assert batch.valid.tolist() == [True, True]
        assert provider.cached_data is None
        cache_entries = tuple(provider._datasets[0]._cache.values())
        assert cache_entries
        assert all(entry[0].shape[-2:] != (512, 512) for entry in cache_entries)
    finally:
        provider.close()


def test_gaia_import_and_index_pipeline_runs_in_spawn_process(tmp_path):
    source = tmp_path / "gaia.csv"
    source.write_text(
        "ra,dec,phot_g_mean_mag,bp_rp,source_id\n"
        "10.0,20.0,7.0,0.5,101\n"
        "11.0,21.0,8.0,1.0,102\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    progress = []
    summary = build_gaia_catalog_spawned(
        [str(source)],
        str(output),
        output_basename="small",
        write_npz=False,
        write_npy=True,
        write_zst=False,
        build_healpy_index=True,
        healpy_nside=8,
        healpy_chunk_rows=1,
        progress_callback=lambda percent, _message: progress.append(percent),
    )
    assert summary["rows"] == 2
    assert (output / "small.npy").exists()
    assert (output / "small_healpy.npy").exists()
    with np.load(output / "small_healpy.idx.npz", allow_pickle=False) as index:
        assert int(index["format_version"].item()) == 2
    assert progress and progress[-1] == 100.0

    store = HealpixStarCatalogStore(
        output / "small_healpy.npy", output / "small_healpy.idx.npz"
    )
    try:
        batches = list(store.query_cone(10.0, 20.0, 0.25, 7.5))
        assert [int(source_id) for batch in batches for source_id in batch.source_id] == [101]
        assert not batches[0].ra.flags.writeable

        generations = GenerationController()
        stale = generations.next()
        generations.next()
        with np.testing.assert_raises(InterruptedError):
            list(store.query_cone(10.0, 20.0, 5.0, 22.0, token=stale))
    finally:
        store.close()


def test_asc_cache_materialization_uses_spawned_bounded_workers(tmp_path):
    source = tmp_path / "tile.asc"
    source.write_text(
        "ncols 2\n"
        "nrows 2\n"
        "xllcorner 0\n"
        "yllcorner 0\n"
        "cellsize 1\n"
        "NODATA_value -9999\n"
        "1 2\n"
        "3 4\n",
        encoding="utf-8",
    )
    events = []
    results = materialize_asc_caches_spawned(
        tmp_path,
        max_workers=4,
        progress_callback=lambda percent, _message: events.append(percent),
    )
    assert len(results) == 1
    assert np.load(source.with_suffix(".npy"), allow_pickle=False).tolist() == [
        [1.0, 2.0],
        [3.0, 4.0],
    ]
    assert events == [100.0]
