from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from TerraLab.terrain.light_pollution_sampler import (
    LightPollutionSamplerChain,
    create_light_pollution_sampler,
    snapshot_light_pollution_sources,
)


TARGET_LAT = 0.005
TARGET_LON = 0.005


def _write_raster(path: Path, value: float, *, nodata: float = -9999.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=1,
        height=1,
        count=1,
        dtype="float32",
        transform=from_origin(0.0, 0.01, 0.01, 0.01),
        crs="EPSG:4326",
        nodata=nodata,
    ) as dst:
        dst.write(np.asarray([[value]], dtype=np.float32), 1)
    return path


def test_folder_mosaic_expands_files_and_falls_back_from_nodata(tmp_path):
    mosaic = tmp_path / "regional-mosaic"
    _write_raster(mosaic / "nested" / "a_nodata.tif", -9999.0)
    valid = _write_raster(mosaic / "nested" / "b_valid.tif", 7.0)

    snapshot = snapshot_light_pollution_sources(
        {"id": "regional", "path": str(mosaic), "enabled": True}
    )
    assert snapshot == [
        {
            "id": "regional",
            "paths": [
                str((mosaic / "nested" / "a_nodata.tif").resolve()),
                str(valid.resolve()),
            ],
            "fingerprint": "",
        }
    ]

    sampler = create_light_pollution_sampler(snapshot)
    try:
        assert isinstance(sampler, LightPollutionSamplerChain)
        assert sampler.get_radiance(TARGET_LAT, TARGET_LON) == 7.0
        sqm, _bortle = sampler.estimate_zenith_sqm(TARGET_LAT, TARGET_LON)
        assert sqm < 21.0

        x, y = Transformer.from_crs(
            "EPSG:4326", "EPSG:25831", always_xy=True
        ).transform(TARGET_LON, TARGET_LAT)
        assert sampler.get_radiance_terrain_xy(x, y) == 7.0
    finally:
        sampler.close()


def test_typed_source_chain_preserves_valid_zero_instead_of_falling_through(
    tmp_path,
):
    dark = _write_raster(tmp_path / "dark.tif", 0.0)
    bright = _write_raster(tmp_path / "bright.tif", 9.0)
    sampler = create_light_pollution_sampler(
        [
            {"id": "priority", "path": str(dark)},
            {"id": "fallback", "path": str(bright)},
        ]
    )
    try:
        assert sampler.get_radiance(TARGET_LAT, TARGET_LON) == 0.0
    finally:
        sampler.close()
