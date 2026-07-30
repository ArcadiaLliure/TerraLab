"""Terrain geometry application controller and capability resolution."""

from enum import Enum
import os


class TerrainGeometryPipeline(str, Enum):
    SCENE = "scene"
    LEGACY = "legacy"


def resolve_terrain_geometry_pipeline() -> TerrainGeometryPipeline:
    """Resolve the active terrain geometry capability flag.

    Defaults to 'scene'. Fallbacks to 'legacy' when TERRALAB_TERRAIN_GEOMETRY_PIPELINE
    is set to 'legacy', '0', or 'false'.
    """
    env_val = (
        os.environ.get("TERRALAB_TERRAIN_GEOMETRY_PIPELINE", "scene")
        .lower()
        .strip()
    )
    if env_val in ("legacy", "0", "false"):
        return TerrainGeometryPipeline.LEGACY
    return TerrainGeometryPipeline.SCENE
