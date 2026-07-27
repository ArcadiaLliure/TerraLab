"""Stable terrain elevation provider API."""

from TerraLab.terrain.crs import PYPROJ_TRANSFORMER_LOCK

from .asc_provider import AscRasterProvider
from .chain import (
    ElevationProviderChain,
    create_elevation_provider,
    create_raster_provider,
)
from .common import (
    CRS_GEOGRAPHIC,
    CRS_TERRAIN_INTERNAL,
    ElevationBatch,
    RasterMetadata,
    RasterProvider,
    RasterSamplingCancelled,
    resolve_primary_dem_tiff_path,
)
from .geotiff_provider import (
    GeoTiffElevationProvider,
    LegacyTiffRasterWindowProvider,
    TiffRasterWindowProvider,
)
__all__ = [
    "AscRasterProvider",
    "CRS_GEOGRAPHIC",
    "CRS_TERRAIN_INTERNAL",
    "ElevationBatch",
    "ElevationProviderChain",
    "GeoTiffElevationProvider",
    "LegacyTiffRasterWindowProvider",
    "PYPROJ_TRANSFORMER_LOCK",
    "RasterMetadata",
    "RasterProvider",
    "RasterSamplingCancelled",
    "TiffRasterWindowProvider",
    "create_elevation_provider",
    "create_raster_provider",
    "resolve_primary_dem_tiff_path",
]
