"""Elevation provider chain and construction factory."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from TerraLab.common.performance.flags import PERFORMANCE_FLAGS
from TerraLab.terrain.crs import normalize_crs
from TerraLab.terrain.providers.asc_provider import AscRasterProvider
from TerraLab.terrain.providers.common import (
    CRS_TERRAIN_INTERNAL,
    ElevationBatch,
    RasterMetadata,
    RasterProvider,
    _as_source_sequence,
    _source_declared_crs,
    _source_enabled,
    _source_identifier,
    _source_paths,
    resolve_primary_dem_tiff_path,
)
from TerraLab.terrain.providers.geotiff_provider import (
    GeoTiffElevationProvider,
    LegacyTiffRasterWindowProvider,
    TiffRasterWindowProvider,
)

class ElevationProviderChain(RasterProvider):
    """Ordered, nodata-aware fallback chain with per-point provenance."""

    def __init__(self, providers: Sequence[RasterProvider], *, internal_crs: str = CRS_TERRAIN_INTERNAL):
        self.providers = tuple(providers)
        self.internal_crs = normalize_crs(internal_crs)

    @property
    def metadata(self) -> tuple[RasterMetadata, ...]:
        return tuple(
            item for provider in self.providers for item in getattr(provider, "metadata", ())
        )

    def get_nominal_resolution_m(self) -> Optional[float]:
        values = []
        for provider in self.providers:
            value = provider.get_nominal_resolution_m()
            if value is not None and np.isfinite(value) and value > 0:
                values.append(float(value))
        return min(values) if values else None

    def sample_elevations(self, x: Any, y: Any, *, input_crs: str | None = None) -> ElevationBatch:
        x_arr, y_arr = np.broadcast_arrays(
            np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
        )
        values = np.zeros(x_arr.shape, dtype=np.float32)
        valid = np.zeros(x_arr.shape, dtype=bool)
        sources = np.full(x_arr.shape, -1, dtype=np.int16)
        for provider_index, provider in enumerate(self.providers):
            batch = provider.sample_elevations(x_arr, y_arr, input_crs=input_crs or self.internal_crs)
            chosen = ~valid & batch.valid
            values[chosen] = batch.values[chosen]
            valid[chosen] = True
            sources[chosen] = int(min(provider_index, np.iinfo(np.int16).max))
            if bool(np.all(valid)):
                break
        return ElevationBatch(values, valid, sources)

    def sample_elevation(self, x: Any, y: Any) -> ElevationBatch:
        return self.sample_elevations(x, y, input_crs=self.internal_crs)

    def get_elevation(self, x: float, y: float) -> Optional[float]:
        for provider in self.providers:
            value = provider.get_elevation(x, y)
            if value is not None:
                return value
        return None

    def prepare_region(self, cx, cy, radius, progress_callback=None, abort_check=None):
        for provider in self.providers:
            if abort_check and abort_check():
                raise InterruptedError("Raster warmup aborted")
            try:
                provider.prepare_region(
                    cx, cy, radius, progress_callback=progress_callback, abort_check=abort_check
                )
            except TypeError:
                provider.prepare_region(cx, cy, radius, progress_callback)

    def close(self) -> None:
        for provider in self.providers:
            close = getattr(provider, "close", None)
            if callable(close):
                close()


def create_raster_provider(tiles_dir: str, progress_callback=None):
    """Build and initialize the configured DEM provider."""

    tiff_path = resolve_primary_dem_tiff_path(tiles_dir)
    if tiff_path:
        provider = (
            TiffRasterWindowProvider(tiles_dir)
            if PERFORMANCE_FLAGS.raster_batch
            else LegacyTiffRasterWindowProvider(tiff_path)
        )
    else:
        provider = AscRasterProvider(tiles_dir)
    provider.initialize(progress_callback=progress_callback)
    return provider


def create_elevation_provider(
    sources: Any,
    *,
    internal_crs: str = CRS_TERRAIN_INTERNAL,
    progress_callback=None,
) -> RasterProvider:
    """Create typed elevation providers without loading full raster payloads."""

    providers: list[RasterProvider] = []
    for source in _as_source_sequence(sources):
        if not _source_enabled(source):
            continue
        paths = _source_paths(source)
        if not paths:
            continue
        source_id = _source_identifier(source)
        declared_crs = _source_declared_crs(source)
        has_gdal = any(
            (
                Path(path).is_file()
                and Path(path).suffix.lower() in GeoTiffElevationProvider.GDAL_SUFFIXES
            )
            or (
                Path(path).is_dir()
                and any(
                    item.is_file()
                    and item.suffix.lower() in GeoTiffElevationProvider.GDAL_SUFFIXES
                    for item in Path(path).rglob("*")
                )
            )
            for path in paths
        )
        if has_gdal:
            if PERFORMANCE_FLAGS.raster_batch:
                provider = GeoTiffElevationProvider(
                    paths,
                    source_id=source_id,
                    internal_crs=internal_crs,
                    declared_crs=declared_crs,
                )
            else:
                tiff_path = next(
                    (
                        resolve_primary_dem_tiff_path(path)
                        for path in paths
                        if resolve_primary_dem_tiff_path(path)
                    ),
                    None,
                )
                if tiff_path is None:
                    continue
                provider = LegacyTiffRasterWindowProvider(tiff_path)
        else:
            provider = AscRasterProvider(paths[0])
            provider.source_id = source_id
            provider.internal_crs = normalize_crs(internal_crs)
            provider.native_crs = normalize_crs(declared_crs or internal_crs)
        provider.initialize(progress_callback=progress_callback)
        providers.append(provider)
    if not providers:
        raise FileNotFoundError("No enabled elevation source is readable")
    if len(providers) == 1:
        return providers[0]
    return ElevationProviderChain(providers, internal_crs=internal_crs)
