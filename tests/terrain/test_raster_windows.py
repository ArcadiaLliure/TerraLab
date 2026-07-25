from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import from_origin

from TerraLab.terrain.providers import TiffRasterWindowProvider


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
