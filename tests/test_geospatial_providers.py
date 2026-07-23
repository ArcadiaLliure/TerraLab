from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin

from TerraLab.terrain.crs import (
    PYPROJ_TRANSFORMER_LOCK,
    CoordinateTransformService,
)
from TerraLab.terrain.data_sources import DataSource, LayerType
from TerraLab.terrain.engine import build_flat_horizon_profile
from TerraLab.terrain.providers import (
    AscRasterProvider,
    ElevationProviderChain,
    GeoTiffElevationProvider,
    RasterSamplingCancelled,
    create_elevation_provider,
)
from TerraLab.terrain.providers import (
    PYPROJ_TRANSFORMER_LOCK as PROVIDER_PYPROJ_TRANSFORMER_LOCK,
)
from TerraLab.terrain.surface import (
    CategoricalSurfaceProvider,
    LightPollutionProvider,
    RgbSurfaceProvider,
    RgbaSampleBatch,
    SurfaceSamplingService,
    create_surface_providers,
)
from TerraLab.terrain.source_inspection import inspect_data_source


def _write_raster(
    path,
    data,
    *,
    transform=None,
    crs="EPSG:25831",
    nodata=None,
    colorinterp=None,
    tiled=False,
):
    array = np.asarray(data)
    if array.ndim == 2:
        array = array[None, ...]
    profile = {
        "driver": "GTiff",
        "width": array.shape[2],
        "height": array.shape[1],
        "count": array.shape[0],
        "dtype": array.dtype,
        "transform": transform or from_origin(0.0, float(array.shape[1]), 1.0, 1.0),
        "crs": crs,
        "nodata": nodata,
    }
    if tiled:
        profile.update(tiled=True, blockxsize=64, blockysize=64)
    with rasterio.open(path, "w", **profile) as target:
        target.write(array)
        if colorinterp is not None:
            target.colorinterp = tuple(colorinterp)
    return path


def _write_ascii(path, data, *, nodata=-9999.0, xll=0.0, yll=0.0):
    rows, cols = np.asarray(data).shape
    header = (
        f"ncols {cols}\n"
        f"nrows {rows}\n"
        f"xllcorner {xll}\n"
        f"yllcorner {yll}\n"
        "cellsize 1\n"
        f"NODATA_value {nodata}\n"
    )
    body = "\n".join(" ".join(str(value) for value in row) for row in data)
    path.write_text(header + body + "\n", encoding="utf-8")
    return path


def test_ascii_txt_and_bounded_npy_are_supported(tmp_path):
    values = np.asarray(
        [
            [0, 10, 20, 30],
            [20, 30, 40, 50],
            [40, 50, 60, 70],
            [60, 70, 80, 90],
        ],
        dtype=np.float32,
    )
    text_path = _write_ascii(tmp_path / "terrain.txt", values)
    text_provider = AscRasterProvider(text_path)
    text_provider.initialize()
    try:
        assert text_provider.get_elevation(1.0, 3.0) == pytest.approx(15.0)
    finally:
        text_provider.close()

    npy_path = tmp_path / "tile_Y_(0_4)X_(0_4).npy"
    np.save(npy_path, np.full((4, 4), 17.0, dtype=np.float32))
    npy_provider = AscRasterProvider(npy_path)
    npy_provider.initialize()
    try:
        assert npy_provider.get_elevation(2.0, 2.0) == pytest.approx(17.0)
    finally:
        npy_provider.close()


def test_ascii_get_elevation_uses_scalar_fast_path(tmp_path, monkeypatch):
    values = np.asarray(
        [
            [0, 10, 20, 30],
            [20, 30, 40, 50],
            [40, 50, 60, 70],
            [60, 70, 80, 90],
        ],
        dtype=np.float32,
    )
    text_path = _write_ascii(tmp_path / "terrain.txt", values)
    provider = AscRasterProvider(text_path)
    provider.initialize()
    try:
        monkeypatch.setattr(
            provider,
            "sample_elevations",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("scalar lookup should not call sample_elevations")
            ),
        )
        assert provider.get_elevation(1.0, 3.0) == pytest.approx(15.0)
        assert provider.get_elevation(1.2, 3.1) is not None
    finally:
        provider.close()


def test_ascii_sources_are_materialized_to_runtime_cache_when_external(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    runtime_root = Path(os.environ["APPDATA"]) / "TerraLab"
    path = _write_ascii(
        tmp_path / "terrain.asc",
        np.asarray([[1, 2], [3, 4]], dtype=np.float32),
    )
    provider = create_elevation_provider(path)
    try:
        assert provider.get_elevation(1.0, 1.0) == pytest.approx(2.5)
        cache_path = Path(provider.metadata[0].paths[1])
        assert cache_path.exists()
        assert cache_path.parent == runtime_root / "cache" / "terrain_materialized" / "ascii"
        assert cache_path.with_suffix(".npy.json").exists()
        assert provider.metadata[0].driver == "AAIGrid"
        assert provider.metadata[0].paths[0].endswith("terrain.asc")
    finally:
        provider.close()


def test_ascii_sources_reuse_adjacent_npy_when_installed_in_runtime_data(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    runtime_root = Path(os.environ["APPDATA"]) / "TerraLab"
    runtime_dir = runtime_root / "data" / "elevation" / "installed"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = _write_ascii(
        runtime_dir / "terrain.asc",
        np.asarray([[1, 2], [3, 4]], dtype=np.float32),
    )
    np.save(path.with_suffix(".npy"), np.asarray([[1, 2], [3, 4]], dtype=np.float32))
    provider = create_elevation_provider(path)
    try:
        assert provider.get_elevation(1.0, 1.0) == pytest.approx(2.5)
        assert Path(provider.metadata[0].paths[1]) == path.with_suffix(".npy")
        assert not (runtime_root / "cache" / "terrain_materialized" / "ascii").exists()
    finally:
        provider.close()


def test_ascii_declared_crs_is_separate_from_the_engine_frame(tmp_path):
    native_x, native_y = 500_000.0, 4_600_000.0
    path = _write_ascii(
        tmp_path / "utm32.asc",
        np.full((4, 4), 37.0, dtype=np.float32),
        xll=native_x,
        yll=native_y,
    )
    provider = create_elevation_provider(
        SimpleNamespace(path=path, crs="EPSG:32632", enabled=True)
    )
    try:
        to_internal = Transformer.from_crs("EPSG:32632", "EPSG:25831", always_xy=True)
        x, y = to_internal.transform(native_x + 2.0, native_y + 2.0)
        batch = provider.sample_elevations(x, y, input_crs="EPSG:25831")
        assert bool(batch.valid)
        assert float(batch.values) == pytest.approx(37.0)
        assert provider.metadata[0].native_crs == "EPSG:32632"
        assert provider.get_native_crs() == "EPSG:25831"
    finally:
        provider.close()


def test_geotiff_sources_are_materialized_to_runtime_cache_when_external(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    runtime_root = Path(os.environ["APPDATA"]) / "TerraLab"
    path = _write_raster(
        tmp_path / "dem.tif",
        np.arange(16, dtype=np.float32).reshape(4, 4),
    )
    provider = create_elevation_provider(path)
    try:
        batch = provider.sample_elevations(1.0, 3.0)
        assert bool(batch.valid)
        assert float(batch.values) == pytest.approx(2.5)
        tile_dir = runtime_root / "cache" / "terrain_materialized" / "gdal"
        cache_path = Path(provider.metadata[0].paths[1])
        assert cache_path.parent.parent == tile_dir
        assert tile_dir.exists()
        assert list(tile_dir.glob("*/manifest.json"))
        assert list(tile_dir.glob("*/*.npy"))
        assert provider.metadata[0].driver == "GTiff"
        assert provider.metadata[0].paths[0].endswith("dem.tif")
        assert provider.metadata[0].paths[1].endswith(".npy")
    finally:
        provider.close()


def test_geotiff_directory_is_a_nodata_aware_mosaic_not_first_file(tmp_path):
    first = np.full((4, 4), 10.0, dtype=np.float32)
    first[0, 0] = -9999.0
    _write_raster(tmp_path / "a_first.tif", first, nodata=-9999.0)
    _write_raster(tmp_path / "z_fallback.tif", np.full((4, 4), 55.0, dtype=np.float32))

    provider = create_elevation_provider(tmp_path)
    try:
        batch = provider.sample_elevations([1.0, 2.0], [3.0, 2.0])
        assert batch.valid.tolist() == [True, True]
        assert batch.values.tolist() == pytest.approx([55.0, 10.0])
        assert batch.source_indices.tolist() == [1, 0]
        assert len(provider.metadata) == 2
    finally:
        provider.close()


def test_registered_dataset_root_discovers_nested_archive_layouts(tmp_path):
    nested = tmp_path / "archive" / "tiles" / "part-a"
    nested.mkdir(parents=True)
    _write_raster(
        nested / "nested-dem.tif",
        np.full((4, 4), 42.0, dtype=np.float32),
    )

    elevation = create_elevation_provider(tmp_path / "archive")
    try:
        assert elevation.get_elevation(1.0, 3.0) == pytest.approx(42.0)
    finally:
        elevation.close()

    rgb_nested = tmp_path / "surface-archive" / "nested"
    rgb_nested.mkdir(parents=True)
    _write_raster(
        rgb_nested / "ortho.tif",
        np.full((3, 4, 4), 80, dtype=np.uint8),
        colorinterp=(ColorInterp.red, ColorInterp.green, ColorInterp.blue),
    )
    surfaces = create_surface_providers(
        {
            "id": "nested-rgb",
            "path": tmp_path / "surface-archive",
            "layer_type": "surface_rgb",
        }
    )
    try:
        assert len(surfaces) == 1
        assert bool(surfaces[0].sample_rgba(1.0, 3.0).valid)
    finally:
        for surface in surfaces:
            surface.close()


def test_ordered_elevation_sources_fall_back_per_point(tmp_path):
    local = np.full((4, 4), 100.0, dtype=np.float32)
    local[2, 2] = -9999.0
    local_path = _write_raster(tmp_path / "local.tif", local, nodata=-9999.0)
    base_path = _write_raster(tmp_path / "base.tif", np.full((4, 4), 25.0, dtype=np.float32))

    provider = create_elevation_provider(
        [
            SimpleNamespace(id="local", path=local_path, enabled=True),
            SimpleNamespace(id="base", path=base_path, enabled=True),
        ]
    )
    assert isinstance(provider, ElevationProviderChain)
    try:
        batch = provider.sample_elevations([1.0, 2.0], [3.0, 2.0])
        assert batch.values.tolist() == pytest.approx([100.0, 25.0])
        assert batch.source_indices.tolist() == [0, 1]
    finally:
        provider.close()


def test_geotiff_sampling_transforms_from_arbitrary_crs(tmp_path):
    path = _write_raster(
        tmp_path / "wgs84.tif",
        np.arange(16, dtype=np.float32).reshape(4, 4),
        transform=from_origin(2.0, 41.01, 0.01, 0.01),
        crs="EPSG:4326",
    )
    provider = GeoTiffElevationProvider(path)
    provider.initialize()
    try:
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:25831", always_xy=True)
        x, y = transformer.transform(2.015, 40.995)
        batch = provider.sample_elevations(x, y, input_crs="EPSG:25831")
        assert bool(batch.valid)
        assert float(batch.values) == pytest.approx(5.0, abs=1e-3)
        assert provider.metadata[0].native_crs == "EPSG:4326"
    finally:
        provider.close()


def test_elevation_reads_bounded_blocks_instead_of_whole_raster(tmp_path):
    path = _write_raster(
        tmp_path / "large.tif",
        np.full((512, 512), 8.0, dtype=np.float32),
        tiled=True,
    )
    provider = GeoTiffElevationProvider(path)
    provider.initialize()
    try:
        batch = provider.sample_elevations(20.0, 492.0)
        assert bool(batch.valid)
        cache_entries = list(provider._datasets[0]._cache.values())
        assert cache_entries
        assert all(values.shape[-2] < 512 and values.shape[-1] < 512 for values, *_ in cache_entries)
    finally:
        provider.close()


def test_rgb_and_categorical_are_sampled_with_nearest_neighbour(tmp_path):
    red = np.fromfunction(lambda row, col: row * 10 + col * 20, (4, 4), dtype=int).astype(np.uint8)
    rgb_path = _write_raster(
        tmp_path / "ortho.tif",
        np.stack((red, np.full((4, 4), 40, np.uint8), np.full((4, 4), 80, np.uint8))),
        colorinterp=(ColorInterp.red, ColorInterp.green, ColorInterp.blue),
    )
    rgb = RgbSurfaceProvider(rgb_path)
    rgb.initialize()
    try:
        sample = rgb.sample_rgba(1.0, 3.0)
        assert bool(sample.valid)
        assert sample.rgba.tolist() == [30, 40, 80, 255]
    finally:
        rgb.close()

    classes = np.tile(np.asarray([2, 10, 2, 10], dtype=np.uint8), (4, 1))
    categorical_path = _write_raster(tmp_path / "cover.tif", classes)
    categorical = CategoricalSurfaceProvider(
        categorical_path,
        class_colors={2: (20, 80, 30), 10: (150, 130, 90, 255)},
    )
    categorical.initialize()
    try:
        sample = categorical.sample_classes([0.5, 1.5], [3.5, 3.5])
        assert sample.valid.tolist() == [True, True]
        assert sample.classes.tolist() == [2, 10]
        colors = categorical.classes_to_rgba(sample.classes)
        assert colors.rgba.tolist() == [[20, 80, 30, 255], [150, 130, 90, 255]]
    finally:
        categorical.close()


def test_categorical_without_color_table_uses_stable_land_cover_materials(tmp_path):
    classes = np.tile(np.asarray([1, 10, 137, 254], dtype=np.uint8), (4, 1))
    path = _write_raster(tmp_path / "plain_clcplus.tif", classes)
    provider = CategoricalSurfaceProvider(path)
    provider.initialize()
    try:
        sampled = provider.sample_classes(
            [0.5, 1.5, 2.5, 3.5],
            [3.5, 3.5, 3.5, 3.5],
        )
        mapped = provider.classes_to_rgba(sampled.classes)

        # CLC+ semantic classes retain their documented materials even if the
        # GeoTIFF has lost its color table.
        assert mapped.rgba[0].tolist() == [255, 0, 0, 255]
        assert mapped.rgba[1].tolist() == [0, 128, 255, 255]
        # Unknown schemas remain usable through a deterministic qualitative
        # material, while the CLC+ outside-area sentinel remains transparent.
        assert int(mapped.rgba[2, 3]) == 255
        assert mapped.valid.tolist() == [True, True, True, False]
        assert provider.classes_to_rgba([137]).rgba[0].tolist() == mapped.rgba[2].tolist()
    finally:
        provider.close()


def test_categorical_metadata_overrides_defaults_without_losing_nodata_or_fallback(tmp_path):
    classes = np.tile(np.asarray([1, 7, 255, 42], dtype=np.uint8), (4, 1))
    path = _write_raster(tmp_path / "cover_with_nodata.tif", classes, nodata=255)
    provider = CategoricalSurfaceProvider(
        path,
        class_colors={
            1: (12, 34, 56),
            7: (90, 80, 70, 0),
        },
    )
    provider.initialize()
    try:
        sampled = provider.sample_classes(
            [0.5, 1.5, 2.5, 3.5],
            [3.5, 3.5, 3.5, 3.5],
        )
        mapped = provider.classes_to_rgba(sampled.classes)
        effective_valid = sampled.valid & mapped.valid

        assert mapped.rgba[0].tolist() == [12, 34, 56, 255]
        assert not bool(mapped.valid[1])  # explicit transparent material
        assert not bool(sampled.valid[2])  # GDAL nodata remains missing
        assert bool(mapped.valid[3])  # partial metadata keeps the default fallback
        assert effective_valid.tolist() == [True, False, False, True]
    finally:
        provider.close()


def test_rgba_transparency_is_missing_data_and_can_fall_back(tmp_path):
    alpha = np.full((4, 4), 255, dtype=np.uint8)
    alpha[:2, :2] = 0
    rgba_path = _write_raster(
        tmp_path / "rgba.tif",
        np.stack(
            (
                np.full((4, 4), 200, np.uint8),
                np.full((4, 4), 100, np.uint8),
                np.full((4, 4), 50, np.uint8),
                alpha,
            )
        ),
        colorinterp=(ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha),
    )
    provider = RgbSurfaceProvider(rgba_path)
    provider.initialize()
    try:
        transparent = provider.sample_rgba(1.0, 3.0)
        assert not bool(transparent.valid)
        assert transparent.rgba.tolist() == [0, 0, 0, 0]
    finally:
        provider.close()


def test_rgb_nodata_falls_back_to_categorical_material(tmp_path):
    alpha = np.zeros((4, 4), dtype=np.uint8)
    rgba_path = _write_raster(
        tmp_path / "empty_rgba.tif",
        np.stack(
            (
                np.full((4, 4), 200, np.uint8),
                np.full((4, 4), 100, np.uint8),
                np.full((4, 4), 50, np.uint8),
                alpha,
            )
        ),
        colorinterp=(
            ColorInterp.red,
            ColorInterp.green,
            ColorInterp.blue,
            ColorInterp.alpha,
        ),
    )
    cover_path = _write_raster(
        tmp_path / "cover_fallback.tif",
        np.full((4, 4), 7, dtype=np.uint8),
    )
    sources = [
        DataSource(
            id="rgb",
            display_name="RGB",
            layer_type=LayerType.SURFACE_RGB,
            path=str(rgba_path),
            format="geotiff",
        ),
        DataSource(
            id="cover",
            display_name="Cover",
            layer_type=LayerType.SURFACE_CATEGORICAL,
            path=str(cover_path),
            format="geotiff",
            metadata={"class_colors": {7: (20, 90, 30, 255)}},
        ),
    ]
    providers = create_surface_providers(sources)
    try:
        sampled = SurfaceSamplingService(providers).sample_rgba_points(
            1.0, 3.0, input_crs="EPSG:25831"
        )
        assert bool(sampled.valid)
        assert sampled.rgba.tolist() == [20, 90, 30, 255]
        assert int(sampled.source_indices) == 1
    finally:
        for provider in providers:
            provider.close()


def test_light_pollution_is_a_separate_continuous_provider(tmp_path):
    path = _write_raster(tmp_path / "radiance.tif", np.full((4, 4), 3.5, dtype=np.float32))
    provider = LightPollutionProvider(path)
    provider.initialize()
    try:
        radiance, valid = provider.sample_radiance(1.0, 3.0)
        assert bool(valid)
        assert float(radiance) == pytest.approx(3.5)
    finally:
        provider.close()


def test_surface_factory_uses_typed_data_source_kinds(tmp_path):
    rgb_path = _write_raster(
        tmp_path / "rgb.tif",
        np.full((3, 4, 4), 90, dtype=np.uint8),
        colorinterp=(ColorInterp.red, ColorInterp.green, ColorInterp.blue),
    )
    cover_path = _write_raster(tmp_path / "cover.tif", np.full((4, 4), 4, dtype=np.uint8))
    light_path = _write_raster(tmp_path / "light.tif", np.full((4, 4), 1.0, dtype=np.float32))
    providers = create_surface_providers(
        [
            {"id": "ortho", "path": rgb_path, "layer_type": "true_color"},
            {
                "id": "clc",
                "path": cover_path,
                "layer_type": "categorical_land_cover",
                "class_colors": {4: (40, 80, 20)},
            },
            {"id": "dvnl", "path": light_path, "layer_type": "light_pollution"},
        ]
    )
    try:
        assert [type(provider) for provider in providers] == [
            RgbSurfaceProvider,
            CategoricalSurfaceProvider,
            LightPollutionProvider,
        ]
    finally:
        for provider in providers:
            provider.close()


def test_continental_surface_sampling_reports_blocks_and_is_cancellable(tmp_path):
    path = _write_raster(
        tmp_path / "categorical-tiled.tif",
        np.full((128, 128), 73, dtype=np.uint8),
        tiled=True,
    )
    provider = CategoricalSurfaceProvider(path)
    provider.initialize()
    progress = []
    cancelled = [False]

    def record_progress(fraction, phase):
        progress.append((float(fraction), str(phase)))
        if float(fraction) > 0.0:
            cancelled[0] = True

    grid = np.linspace(0.5, 127.5, 32)
    x, row = np.meshgrid(grid, grid)
    y = 128.0 - row
    try:
        with pytest.raises(RasterSamplingCancelled):
            provider.sample_classes(
                x,
                y,
                input_crs="EPSG:25831",
                progress_callback=record_progress,
                abort_check=lambda: cancelled[0],
            )
        assert progress[0][0] == 0.0
        assert any(fraction > 0.0 for fraction, _phase in progress)
    finally:
        provider.close()


def test_striped_continental_raster_is_batched_into_local_virtual_windows(tmp_path):
    path = _write_raster(
        tmp_path / "wide-striped.tif",
        np.full((128, 10_000), 73, dtype=np.uint8),
    )
    provider = CategoricalSurfaceProvider(path)
    provider.initialize()
    dataset = provider._datasets[0]
    rows = np.arange(0.5, 127.5, dtype=np.float64)
    x = np.full(rows.shape, 5_000.5, dtype=np.float64)
    y = 128.0 - rows
    try:
        assert dataset.native_block_width == 10_000
        assert dataset.native_block_height <= 4
        assert dataset.virtual_blocks is True
        assert dataset.block_height > dataset.native_block_height

        result = provider.sample_classes(x, y, input_crs="EPSG:25831")

        assert result.valid.all()
        assert np.all(result.classes == 73)
        # One grouped local window replaces one GDAL call for every scanline.
        assert dataset.cache_misses < len(rows) // 8
    finally:
        provider.close()


def test_surface_cache_uses_real_profile_and_mesh_coordinates_and_is_immutable():
    class RecordingRgbProvider(RgbSurfaceProvider):
        def __init__(self):
            super().__init__([], source_id="recording")
            self.calls = []

        def sample_rgba(self, x, y, *, input_crs=None):
            x_array, y_array = np.broadcast_arrays(
                np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
            )
            self.calls.append((x_array.copy(), y_array.copy(), input_crs))
            rgba = np.zeros(x_array.shape + (4,), dtype=np.uint8)
            rgba[..., 0] = np.clip(np.rint(x_array + 128.0), 0, 255).astype(np.uint8)
            rgba[..., 1] = 60
            rgba[..., 2] = 30
            rgba[..., 3] = 255
            return RgbaSampleBatch(rgba, np.ones(x_array.shape, dtype=bool))

    provider = RecordingRgbProvider()
    service = SurfaceSamplingService([provider])
    azimuths = np.asarray([0.0, 90.0, 180.0, 270.0], dtype=np.float32)
    profile = SimpleNamespace(
        observer_x=0.0,
        observer_y=0.0,
        observer_lat=41.0,
        observer_lon=2.0,
        geometry_crs="EPSG:25831",
        representation_mode="RELIEF",
        azimuths=azimuths,
        bands=[
            {
                "id": "far",
                "dists": np.asarray([10.0, 20.0, 30.0, 40.0], dtype=np.float32),
                "angles": np.zeros(4, dtype=np.float32),
            }
        ],
        terrain_mesh={
            "version": 3,
            "azimuths": azimuths,
            "distances": np.asarray([5.0, 15.0], dtype=np.float32),
            "altitudes": np.zeros((2, 4), dtype=np.float32),
            "valid": np.ones((2, 4), dtype=bool),
            "near_patch_eastings": np.asarray([-1.0, 0.0, 1.0], dtype=np.float32),
            "near_patch_northings": np.asarray([-1.0, 0.0, 1.0], dtype=np.float32),
            "near_patch_valid": np.ones((3, 3), dtype=bool),
        },
    )

    cache = service.sample_profile(profile, geometry_id="geometry-a")
    assert cache.profile_rgba.shape == (1, 4, 4)
    assert cache.relief_rgba.shape == (2, 4, 4)
    assert cache.profile_valid.all() and cache.relief_valid.all()
    observer_x, observer_y, observer_crs = provider.calls[0]
    assert observer_crs == "EPSG:25831"
    assert float(observer_x) == pytest.approx(0.0)
    assert float(observer_y) == pytest.approx(0.0)
    assert cache.observer_valid is True
    assert cache.observer_rgba.tolist() == [128, 60, 30, 255]
    assert cache.near_patch_rgba.shape == (3, 3, 4)
    assert cache.near_patch_valid.all()
    near_x, near_y, near_crs = provider.calls[1]
    assert near_crs == "EPSG:25831"
    np.testing.assert_allclose(
        near_x,
        [[-1.0, 0.0, 1.0], [-1.0, 0.0, 1.0], [-1.0, 0.0, 1.0]],
    )
    np.testing.assert_allclose(
        near_y,
        [[-1.0, -1.0, -1.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
    )
    profile_x, profile_y, profile_crs = provider.calls[2]
    assert profile_crs == "EPSG:25831"
    assert profile_x[0].tolist() == pytest.approx([0.0, 20.0, 0.0, -40.0], abs=1e-6)
    assert profile_y[0].tolist() == pytest.approx([10.0, 0.0, -30.0, 0.0], abs=1e-6)
    assert not cache.profile_rgba.flags.writeable
    with pytest.raises(ValueError):
        cache.profile_rgba[0, 0, 0] = 0

    calls_before_cache_hit = len(provider.calls)
    same = service.sample_profile(profile, geometry_id="geometry-a")
    different = service.sample_profile(profile, geometry_id="geometry-b")
    assert same is cache
    assert len(provider.calls) == calls_before_cache_hit + 4
    assert same.cache_id == cache.cache_id
    assert different.cache_id != cache.cache_id

    capped = SurfaceSamplingService(
        [RecordingRgbProvider()], max_profile_samples=2, max_relief_samples=3
    ).sample_profile(profile)
    assert int(np.prod(capped.profile_valid.shape)) <= 2
    assert int(np.prod(capped.relief_valid.shape)) <= 3


def test_flat_fallback_can_sample_surface_without_inventing_topography():
    class FlatSurface(RgbSurfaceProvider):
        def __init__(self):
            super().__init__([], source_id="flat-surface")

        def sample_rgba(self, x, y, *, input_crs=None):
            x_values, y_values = np.broadcast_arrays(x, y)
            rgba = np.zeros(x_values.shape + (4,), dtype=np.uint8)
            rgba[...] = (40, 120, 60, 255)
            return RgbaSampleBatch(
                rgba, np.ones(x_values.shape, dtype=bool)
            )

    profile = build_flat_horizon_profile(
        observer_lat=41.4,
        observer_lon=2.1,
        representation_mode="relief",
    )
    assert profile.terrain_mesh is None
    assert np.all(profile.bands[0]["dists"] == 0.0)
    assert np.all(profile.bands[0]["surface_dists"] > 0.0)

    cache = SurfaceSamplingService([FlatSurface()]).sample_profile(profile)
    assert cache.profile_valid is not None
    assert bool(np.all(cache.profile_valid))
    assert cache.relief_rgba is None


def test_metadata_inspection_preserves_crs_resolution_bounds_and_coverage(
    tmp_path,
):
    path = _write_raster(
        tmp_path / "metadata.tif",
        np.ones((4, 4), dtype=np.float32),
        transform=from_origin(500_000.0, 4_600_000.0, 10.0, 10.0),
        crs="EPSG:25831",
    )

    inspected = inspect_data_source(path, LayerType.ELEVATION)

    assert inspected.crs == "EPSG:25831"
    assert inspected.resolution_m == pytest.approx(10.0, rel=0.01)
    assert inspected.bounds == pytest.approx(
        (500_000.0, 4_599_960.0, 500_040.0, 4_600_000.0)
    )
    assert inspected.coverage is not None
    west, south, east, north = inspected.coverage
    assert west < east and south < north
    assert inspected.metadata["raster_count"] == 1


def test_crs_service_vectorizes_without_hard_coding_the_internal_frame():
    service = CoordinateTransformService()
    x, y = service.transform_xy(
        np.asarray([2.17, 2.18]),
        np.asarray([41.38, 41.39]),
        "EPSG:4326",
        "EPSG:3035",
    )
    lon, lat = service.transform_xy(x, y, "EPSG:3035", "EPSG:4326")
    assert lon.tolist() == pytest.approx([2.17, 2.18], abs=1e-8)
    assert lat.tolist() == pytest.approx([41.38, 41.39], abs=1e-8)


def test_all_terrain_providers_share_the_process_wide_pyproj_lock():
    assert PROVIDER_PYPROJ_TRANSFORMER_LOCK is PYPROJ_TRANSFORMER_LOCK


def test_crs_services_serialize_shared_transformers_across_threads(monkeypatch):
    state = {
        "active": 0,
        "maximum": 0,
        "caller_threads": set(),
        "execution_threads": set(),
    }
    state_lock = threading.Lock()
    start_gate = threading.Barrier(4)

    class _InstrumentedTransformer:
        def transform(self, x, y):
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                state["execution_threads"].add(threading.get_ident())
            time.sleep(0.02)
            with state_lock:
                state["active"] -= 1
            return x, y

    transformer = _InstrumentedTransformer()
    monkeypatch.setattr(
        CoordinateTransformService,
        "_build_transformer",
        staticmethod(lambda _source, _target: transformer),
    )

    def run(service):
        start_gate.wait()
        with state_lock:
            state["caller_threads"].add(threading.get_ident())
        return service.transform_xy(1.0, 2.0, "EPSG:4326", "EPSG:3035")

    services = [CoordinateTransformService() for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run, services))

    assert results == [(1.0, 2.0)] * 4
    assert state["maximum"] == 1
    assert len(state["execution_threads"]) == 1
    assert state["execution_threads"].isdisjoint(state["caller_threads"])
