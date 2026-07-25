from __future__ import annotations

import numpy as np

from TerraLab.terrain.infrastructure.dem_tiles import TileCache, TileIndex
from TerraLab.terrain.providers import AscRasterProvider


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
