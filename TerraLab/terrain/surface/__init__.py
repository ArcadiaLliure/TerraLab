"""Stable terrain surface sampling API."""

from .cache import (
    SurfaceCacheKey,
    SurfaceSampleCache,
    SurfaceSamplingRequest,
    _surface_cache_from_payload,
    _surface_cache_payload,
    geometry_fingerprint,
)
from .categorical import (
    CategoricalSurfaceProvider,
    RgbCategoricalSurfaceProvider,
)
from .common import (
    CategoricalSampleBatch,
    RgbaSampleBatch,
    SurfaceProvider,
)
from .factory import create_surface_providers
from .geometry import _lod_factors_for_polar_grid, raster_grids_aligned
from .light_pollution import LightPollutionProvider
from .rgb import RgbSurfaceProvider
from .service import SurfaceSamplingService

__all__ = [
    "_lod_factors_for_polar_grid",
    "_surface_cache_from_payload",
    "_surface_cache_payload",
    "CategoricalSampleBatch",
    "CategoricalSurfaceProvider",
    "LightPollutionProvider",
    "RgbCategoricalSurfaceProvider",
    "RgbSurfaceProvider",
    "RgbaSampleBatch",
    "SurfaceCacheKey",
    "SurfaceProvider",
    "SurfaceSampleCache",
    "SurfaceSamplingRequest",
    "SurfaceSamplingService",
    "create_surface_providers",
    "geometry_fingerprint",
    "raster_grids_aligned",
]
