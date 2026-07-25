from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import from_origin

from TerraLab.light_pollution.processing import (
    convolve_dvnl,
    iter_tile_windows,
    preprocess_dvnl,
)


def _write_raster(path, data, *, nodata=None):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=data.shape[1],
        height=data.shape[0],
        count=1,
        dtype=data.dtype,
        transform=from_origin(0, data.shape[0], 1, 1),
        crs="EPSG:3857",
        nodata=nodata,
    ) as destination:
        destination.write(data, 1)


def test_preprocess_preserves_geospatial_metadata_and_normalizes_nodata(tmp_path):
    source = tmp_path / "source.tif"
    output = tmp_path / "clean.tif"
    data = np.asarray([[1, -9999], [2, 1e11]], dtype=np.float32)
    _write_raster(source, data, nodata=-9999)
    preprocess_dvnl(source, output)
    with rasterio.open(output) as raster:
        cleaned = raster.read(1)
        assert raster.crs.to_epsg() == 3857
        assert raster.transform == from_origin(0, 2, 1, 1)
        assert np.isnan(raster.nodata)
        assert np.isnan(cleaned[0, 1])
        assert np.isnan(cleaned[1, 1])


def test_convolution_handles_edges_nan_and_partial_windows(tmp_path):
    source = tmp_path / "source.tif"
    output = tmp_path / "convolved.tif"
    data = np.zeros((5, 7), dtype=np.float32)
    data[0, 0] = 1.0
    data[2, 3] = np.nan
    _write_raster(source, data, nodata=np.nan)
    kernel = np.ones((3, 3), dtype=np.float64)
    convolve_dvnl(source, output, kernel, tile_size=4)
    with rasterio.open(output) as raster:
        result = raster.read(1)
    assert result.shape == data.shape
    assert np.isfinite(result).all()
    assert result[0, 0] == 1.0
    assert result[0, 1] == 1.0
    assert result[-1, -1] == 0.0


def test_window_plan_covers_non_multiple_raster_once():
    windows = list(iter_tile_windows(7, 5, tile_size=4, halo=2))
    assert len(windows) == 4
    write_areas = sum(
        int(write.width * write.height) for write, _read in windows
    )
    assert write_areas == 35
    assert all(read.width <= 7 and read.height <= 5 for _write, read in windows)
