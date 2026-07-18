import pytest
from pyproj import Transformer

from TerraLab.terrain.providers import (
    AscRasterProvider,
    CRS_TERRAIN_INTERNAL,
    TiffRasterWindowProvider,
)


def test_tiff_provider_initialization():
    """Test that a non-existent GeoTIFF raises FileNotFoundError."""
    provider = TiffRasterWindowProvider("invalid/path/to/raster.tif")

    with pytest.raises(FileNotFoundError):
        provider.initialize()


def test_native_crs_default():
    """Test abstract RasterProvider default CRS behavior."""
    from TerraLab.terrain.providers import RasterProvider

    # We can't instantiate ABC directly, so make a dummy subclass
    class DummyProvider(RasterProvider):
        def get_elevation(self, x: float, y: float):
            return 0.0

    dummy = DummyProvider()
    assert dummy.get_native_crs() == "EPSG:25831"


def test_coordinate_transformation():
    """Test geographic->terrain transformation in the configured internal CRS."""
    provider = AscRasterProvider("dummy_dir")

    # Example: Barcelona coordinates
    lat, lon = 41.3851, 2.1734

    x, y = provider.transform_coordinates(lat, lon)

    # Barcelona in UTM 31N should be roughly around X=430000, Y=4580000
    assert 400000 < x < 500000
    assert 4500000 < y < 4600000


def test_internal_crs_matches_epsg_25831_transformer():
    """Provider coordinate transform must match EPSG:25831 exactly."""
    provider = AscRasterProvider("dummy_dir")
    lat, lon = 42.52273, 1.00398

    x_provider, y_provider = provider.transform_coordinates(lat, lon)
    tr = Transformer.from_crs("EPSG:4326", CRS_TERRAIN_INTERNAL, always_xy=True)
    x_ref, y_ref = tr.transform(lon, lat)

    assert abs(x_provider - x_ref) < 1e-6
    assert abs(y_provider - y_ref) < 1e-6
