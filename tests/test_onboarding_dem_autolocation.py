from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from TerraLab.common.utils import _clear_cache_config, get_config_value
from TerraLab.data.assets_manager import AssetManager
from TerraLab.terrain.providers import resolve_primary_dem_tiff_path


def _write_small_tiff(path: Path, *, lat: float, lon: float, crs: str) -> None:
    """Create a tiny GeoTIFF centered on a known geographic point."""
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    cx, cy = tr.transform(float(lon), float(lat))
    pixel = 30.0
    width = 10
    height = 10
    left = cx - (width * pixel) * 0.5
    top = cy + (height * pixel) * 0.5
    transform = from_origin(left, top, pixel, pixel)
    data = np.ones((height, width), dtype=np.float32)
    with rasterio.open(
        str(path),
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        transform=transform,
        crs=crs,
    ) as dst:
        dst.write(data, 1)


def _write_small_asc(path: Path, *, center_x: float, center_y: float) -> None:
    """Create a tiny ESRI ASCII grid around a known EPSG:25831 center."""
    ncols, nrows, cellsize = 2, 2, 100.0
    xllcorner = float(center_x) - (ncols * cellsize) * 0.5
    yllcorner = float(center_y) - (nrows * cellsize) * 0.5
    rows = ["1 2", "3 4"]
    text = (
        f"ncols {ncols}\n"
        f"nrows {nrows}\n"
        f"xllcorner {xllcorner}\n"
        f"yllcorner {yllcorner}\n"
        f"cellsize {cellsize}\n"
        "NODATA_value -9999\n"
        + "\n".join(rows)
        + "\n"
    )
    path.write_text(text, encoding="utf-8")


def test_onboarding_dem_tiff_sets_observer_location(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        monkeypatch.setenv("APPDATA", str(root))
        _clear_cache_config()

        target_lat, target_lon = 42.52273, 1.00398
        source_tiff = root / "source_dem.tif"
        _write_small_tiff(
            source_tiff, lat=target_lat, lon=target_lon, crs="EPSG:32631"
        )

        manager = AssetManager()
        result = manager.import_files("elevation_dem", [str(source_tiff)])
        observer = dict(result.get("observer_auto", {}))

        assert observer.get("applied") is True
        saved_lat = float(get_config_value("observer_lat", 0.0, refresh=True))
        saved_lon = float(get_config_value("observer_lon", 0.0))
        assert abs(saved_lat - target_lat) < 1e-5
        assert abs(saved_lon - target_lon) < 1e-5


def test_onboarding_dem_asc_sets_observer_location(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        monkeypatch.setenv("APPDATA", str(root))
        _clear_cache_config()

        target_lat, target_lon = 41.189795, 1.210058
        tr = Transformer.from_crs("EPSG:4326", "EPSG:25831", always_xy=True)
        center_x, center_y = tr.transform(target_lon, target_lat)
        source_asc = root / "source_dem.asc"
        _write_small_asc(source_asc, center_x=center_x, center_y=center_y)

        manager = AssetManager()
        result = manager.import_files("elevation_dem", [str(source_asc)])
        observer = dict(result.get("observer_auto", {}))

        assert observer.get("applied") is True
        saved_lat = float(get_config_value("observer_lat", 0.0, refresh=True))
        saved_lon = float(get_config_value("observer_lon", 0.0))
        assert abs(saved_lat - target_lat) < 1e-5
        assert abs(saved_lon - target_lon) < 1e-5


def test_resolve_primary_dem_tiff_path_is_deterministic():
    with tempfile.TemporaryDirectory() as td:
        dem_dir = Path(td) / "dem"
        dem_dir.mkdir(parents=True, exist_ok=True)
        (dem_dir / "B_tile.tif").write_bytes(b"")
        (dem_dir / "a_tile.tif").write_bytes(b"")
        selected = resolve_primary_dem_tiff_path(str(dem_dir))
        assert selected is not None
        assert Path(selected).name == "a_tile.tif"
