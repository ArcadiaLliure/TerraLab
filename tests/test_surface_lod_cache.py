from types import SimpleNamespace

import numpy as np
import pytest
from rasterio.transform import from_origin

from TerraLab.terrain.data_sources import (
    DataSourceRegistry,
    LayerRole,
    LayerSelectionService,
    LayerType,
)
from TerraLab.terrain.surface import (
    CategoricalSurfaceProvider,
    SurfaceSamplingRequest,
    SurfaceSamplingService,
)
from TerraLab.terrain.surface_store import AtomicNpzStore


def _write_categorical(path, data):
    import rasterio

    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=data.dtype,
        crs="EPSG:25831",
        transform=from_origin(0.0, float(data.shape[0]), 1.0, 1.0),
        nodata=255,
        compress="lzw",
    ) as dataset:
        dataset.write(data, 1)
    return path


def _surface_profile():
    azimuths = np.asarray([0.0, 90.0, 180.0, 270.0], dtype=np.float32)
    return SimpleNamespace(
        observer_x=128.5,
        observer_y=128.5,
        observer_lat=41.0,
        observer_lon=2.0,
        geometry_crs="EPSG:25831",
        geometry_id="stable-scene",
        representation_mode="RELIEF",
        resolved_radius_m=25.0,
        grid_convergence_deg=0.0,
        azimuths=azimuths,
        bands=[
            {
                "id": "near",
                "dists": np.full(4, 10.0, dtype=np.float32),
                "angles": np.zeros(4, dtype=np.float32),
            }
        ],
        terrain_mesh={
            "azimuths": azimuths,
            "distances": np.asarray([5.0, 15.0], dtype=np.float32),
            "altitudes": np.zeros((2, 4), dtype=np.float32),
            "valid": np.ones((2, 4), dtype=bool),
        },
    )


def test_atomic_npz_store_recovers_corruption_and_enforces_shared_lru(tmp_path):
    store = AtomicNpzStore(tmp_path / "cache", budget_bytes=4_000)
    rng = np.random.default_rng(7)
    first = rng.integers(0, 256, 2_000, dtype=np.uint8)
    second = rng.integers(0, 256, 2_000, dtype=np.uint8)

    path = store.save("results/first", {"schema": 1}, {"data": first})
    assert store.load("results/first")[1]["data"].tolist() == first.tolist()

    path.write_bytes(b"not-an-npz")
    assert store.load("results/first") is None
    assert not path.exists()
    store.save("results/first", {"schema": 1}, {"data": first})
    store.save("tiles/second", {"schema": 1}, {"data": second})

    assert sum(item.stat().st_size for item in store.root.rglob("*.npz")) <= 4_000
    assert store.stats.corrupt == 1
    assert store.stats.evictions >= 1


def test_atomic_npz_store_can_defer_tile_durability_and_prune_to_one_batch(tmp_path):
    store = AtomicNpzStore(tmp_path / "cache", budget_bytes=32 * 1024**2)
    payload = np.arange(128, dtype=np.uint8)

    store.save(
        "tiles/first", {"schema": 1}, {"data": payload},
        durable=False, prune_after=False,
    )
    store.save(
        "tiles/second", {"schema": 1}, {"data": payload},
        durable=False, prune_after=False,
    )
    assert store.stats.prunes == 0
    assert store.stats.fsync_ns == 0

    store.prune()

    assert store.stats.prunes == 1
    assert store.load("tiles/first") is not None
    assert store.load("tiles/second") is not None


def test_categorical_lod_reads_real_rows_and_reuses_sparse_tiles(tmp_path):
    rows, columns = 128, 10_000
    data = (
        np.arange(rows, dtype=np.uint16)[:, None]
        + np.arange(columns, dtype=np.uint16)[None, :]
    ) % 200
    path = _write_categorical(tmp_path / "striped.tif", data.astype(np.uint8))
    store = AtomicNpzStore(tmp_path / "surface-cache", budget_bytes=32 * 1024**2)
    sample_rows = np.asarray([3, 17, 63, 97], dtype=np.int64)
    sample_cols = np.asarray([4, 500, 4_500, 9_500], dtype=np.int64)
    x = sample_cols.astype(np.float64) + 0.5
    y = rows - sample_rows.astype(np.float64) - 0.5

    first = CategoricalSurfaceProvider(path, tile_store=store)
    first.initialize()
    progress = []
    try:
        batch = first.sample_classes(
            x,
            y,
            input_crs="EPSG:25831",
            lod_factors=np.ones(4, dtype=np.int16),
            progress_callback=lambda fraction, phase: progress.append(
                (fraction, phase)
            ),
        )
        dataset = first._datasets[0]
        assert batch.valid.all()
        assert batch.classes.tolist() == data[sample_rows, sample_cols].tolist()
        assert dataset.lod_rows_read == len(sample_rows)
        assert dataset.bytes_read < 20_000
        assert len(progress) <= 5
        assert progress[-1][0] == 1.0
    finally:
        first.close()

    second_provider = CategoricalSurfaceProvider(path, tile_store=store)
    second_provider.initialize()
    try:
        warm = second_provider.sample_classes(
            x,
            y,
            input_crs="EPSG:25831",
            lod_factors=np.ones(4, dtype=np.int16),
        )
        assert warm.classes.tolist() == batch.classes.tolist()
        assert second_provider._datasets[0].lod_rows_read == 0
        assert second_provider._datasets[0].lod_cache_hits == len(sample_rows)
    finally:
        second_provider.close()


def test_categorical_lod_sampling_is_cancellable_between_source_rows(tmp_path):
    data = np.full((64, 10_000), 10, dtype=np.uint8)
    path = _write_categorical(tmp_path / "cancel-striped.tif", data)
    provider = CategoricalSurfaceProvider(path)
    provider.initialize()
    cancelled = [False]

    def progress(_fraction, _phase):
        cancelled[0] = True

    rows = np.arange(64, dtype=np.float64)
    try:
        with pytest.raises(InterruptedError):
            provider.sample_classes(
                np.full(64, 4_000.5),
                64.0 - rows - 0.5,
                input_crs="EPSG:25831",
                lod_factors=np.ones(64, dtype=np.int16),
                progress_callback=progress,
                abort_check=lambda: cancelled[0],
            )
    finally:
        provider.close()


def test_surface_result_cache_survives_restart_and_partial_fov_is_typed(tmp_path):
    data = np.full((256, 256), 10, dtype=np.uint8)
    path = _write_categorical(tmp_path / "land-cover.tif", data)
    cache_root = tmp_path / "surface-cache"
    profile = _surface_profile()

    provider = CategoricalSurfaceProvider(
        path,
        source_id="land-cover",
        source_name="Mapa categòric",
        legend_id="external-legend",
        class_colors={10: (20, 90, 30, 255)},
    )
    provider.initialize()
    service = SurfaceSamplingService(
        [provider], persistent_cache_dir=cache_root, persistent_cache_bytes=32 * 1024**2
    )
    partial = service.sample_profile(
        profile,
        request=SurfaceSamplingRequest(
            profile=profile,
            visible_radius_m=20.0,
            view_azimuth_deg=90.0,
            view_fov_deg=30.0,
            generation=3,
            stage="visible_partial",
        ),
    )
    assert partial.completion_state == "visible_partial"
    assert partial.source_names == ("Mapa categòric",)
    assert partial.source_legend_ids == ("external-legend",)
    assert partial.profile_azimuth_indices.tolist() == [1]
    assert partial.profile_loaded.all()
    service.close()

    provider = CategoricalSurfaceProvider(
        path,
        source_id="land-cover",
        source_name="Mapa categòric",
        legend_id="external-legend",
        class_colors={10: (20, 90, 30, 255)},
    )
    provider.initialize()
    restarted = SurfaceSamplingService(
        [provider], persistent_cache_dir=cache_root, persistent_cache_bytes=32 * 1024**2
    )
    same = restarted.sample_profile(
        profile,
        request=SurfaceSamplingRequest(
            profile=profile,
            visible_radius_m=20.0,
            view_azimuth_deg=90.0,
            view_fov_deg=30.0,
            generation=99,
            stage="visible_partial",
        ),
    )
    try:
        assert same.cache_id == partial.cache_id
        assert same.source_names == partial.source_names
        assert same.source_legend_ids == partial.source_legend_ids
        assert restarted._persistent_store.stats.hits >= 1
        assert provider._datasets[0].lod_rows_read == 0
    finally:
        restarted.close()


def test_surface_selection_deduplicates_product_and_prefers_manual_then_managed(tmp_path):
    registry = DataSourceRegistry(tmp_path / "sources.json", legacy_reader={})
    common = {
        "product_id": "s2glc-europe-2017-v1.2",
        "product_version": "1.2",
    }
    external_path = tmp_path / "external.tif"
    managed_path = tmp_path / "managed.tif"
    external_path.touch()
    managed_path.touch()
    external = registry.register_path(
        external_path,
        LayerType.SURFACE_CATEGORICAL,
        source_id="a-external",
        crs="EPSG:3035",
        resolution_m=10.0,
        bounds=(0.0, 0.0, 100.0, 100.0),
        metadata={**common, "managed": False},
    )
    managed = registry.register_path(
        managed_path,
        LayerType.SURFACE_CATEGORICAL,
        source_id="z-managed",
        crs="EPSG:3035",
        resolution_m=10.0,
        bounds=(0.0, 0.0, 100.0, 100.0),
        metadata={**common, "managed": True},
        provenance="managed",
    )
    selector = LayerSelectionService(registry)

    assert selector.select_surface(41.0, 2.0).chain == (managed,)
    registry.set_selection(LayerRole.SURFACE, external.id)
    assert selector.select_surface(41.0, 2.0).chain == (external,)
