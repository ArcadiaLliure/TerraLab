"""Metadata-only inspection for typed geospatial catalogue entries."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.data.source_catalog import DataSource, LayerType


@dataclass(frozen=True)
class SourceInspection:
    """Catalogue fields obtained without reading full raster pixel arrays."""

    crs: str | None
    resolution_m: float | None
    bounds: tuple[float, float, float, float] | None
    coverage: tuple[float, float, float, float] | None
    metadata: dict[str, Any]

    def registry_fields(self) -> dict[str, Any]:
        return {
            "crs": self.crs,
            "resolution_m": self.resolution_m,
            "bounds": self.bounds,
            "coverage": self.coverage,
            "metadata": self.metadata,
        }


def _union_bounds(
    bounds: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    finite = [
        item
        for item in bounds
        if len(item) == 4 and all(math.isfinite(float(value)) for value in item)
    ]
    if not finite:
        return None
    return (
        min(float(item[0]) for item in finite),
        min(float(item[1]) for item in finite),
        max(float(item[2]) for item in finite),
        max(float(item[3]) for item in finite),
    )


def _metadata_payload(item: Any) -> dict[str, Any]:
    return {
        "native_crs": getattr(item, "native_crs", None),
        "bounds": list(getattr(item, "bounds", ())) if getattr(item, "bounds", None) is not None else None,
        "resolution_m": getattr(item, "resolution_m", None),
        "nodata": list(getattr(item, "nodata", ())),
        "driver": getattr(item, "driver", None),
        "band_count": int(getattr(item, "band_count", 1)),
        "paths": list(getattr(item, "paths", ())),
        "extra": dict(getattr(item, "extra", {})),
    }


def inspect_data_source(
    path: str,
    layer_type: LayerType | str,
    *,
    source_id: str = "inspection",
    declared_crs: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SourceInspection:
    """Inspect one dataset/mosaic through the same typed provider contracts.

    GeoTIFF/GDAL sources open dataset descriptors and block indexes only. ASC,
    TXT and legacy NPY elevation sources read their headers/descriptors only.
    Pixel windows are not sampled by this function.
    """
    from TerraLab.data.crs import CRS_GEOGRAPHIC, DEFAULT_TRANSFORM_SERVICE
    providers_mod = __import__("TerraLab.terrain.providers", fromlist=["RasterMetadata", "create_elevation_provider"])
    create_elevation_provider = providers_mod.create_elevation_provider
    RasterMetadata = providers_mod.RasterMetadata

    kind = layer_type if isinstance(layer_type, LayerType) else LayerType(layer_type)
    source = {
        "id": str(source_id or "inspection"),
        "path": str(path),
        "layer_type": kind.value,
        "crs": declared_crs,
        "enabled": True,
        "metadata": dict(metadata or {}),
    }
    providers = []
    try:
        if kind is LayerType.ELEVATION:
            providers = [create_elevation_provider([source])]
        else:
            surface_mod = __import__("TerraLab.terrain.surface", fromlist=["create_surface_providers"])
            create_surface_providers = surface_mod.create_surface_providers

            providers = list(create_surface_providers([source]))
        raster_metadata = [
            item
            for provider in providers
            for item in getattr(provider, "metadata", ())
            if isinstance(item, RasterMetadata)
        ]
        if not raster_metadata:
            raise ValueError(f"No readable raster metadata found in {path}")

        native_crs_values = {
            str(item.native_crs) for item in raster_metadata if item.native_crs
        }
        common_crs = (
            next(iter(native_crs_values))
            if len(native_crs_values) == 1
            else None
        )
        native_bounds = None
        if common_crs is not None:
            native_bounds = _union_bounds(
                [item.bounds for item in raster_metadata if item.bounds is not None]
            )

        geographic_bounds = []
        for item in raster_metadata:
            if item.bounds is None or not item.native_crs:
                continue
            try:
                geographic_bounds.append(
                    DEFAULT_TRANSFORM_SERVICE.transform_bounds(
                        item.bounds,
                        item.native_crs,
                        CRS_GEOGRAPHIC,
                    )
                )
            except Exception:
                continue
        coverage = _union_bounds(geographic_bounds)
        resolutions = [
            float(item.resolution_m)
            for item in raster_metadata
            if item.resolution_m is not None
            and math.isfinite(float(item.resolution_m))
            and float(item.resolution_m) > 0.0
        ]
        payload = dict(metadata or {})
        payload.update(
            {
                "inspection_version": 1,
                "raster_count": len(raster_metadata),
                "rasters": [
                    _metadata_payload(item) for item in raster_metadata
                ],
            }
        )
        return SourceInspection(
            crs=common_crs,
            resolution_m=min(resolutions) if resolutions else None,
            bounds=native_bounds,
            coverage=coverage,
            metadata=payload,
        )
    finally:
        for provider in providers:
            try:
                provider.close()
            except Exception:
                log_suppressed_exception(__name__, "inspect_data_source")


def inspect_registered_source(source: DataSource) -> SourceInspection:
    """Inspect an existing catalogue object while preserving its metadata."""

    return inspect_data_source(
        source.path,
        source.layer_type,
        source_id=source.id,
        declared_crs=source.crs,
        metadata=source.metadata,
    )


__all__ = [
    "SourceInspection",
    "inspect_data_source",
    "inspect_registered_source",
]
