"""Compatibility exports for the data-layer terrain contracts."""

from TerraLab.data.terrain_contracts import (
    TerrainGeometrySource,
    TerrainRepresentationMode,
    normalize_terrain_geometry_source,
    normalize_terrain_representation_mode,
)

__all__ = [
    "TerrainGeometrySource",
    "TerrainRepresentationMode",
    "normalize_terrain_geometry_source",
    "normalize_terrain_representation_mode",
]
