"""Terrain application controller and capability resolution."""

from enum import Enum
import os


class TerrainPipeline(str, Enum):
    SCENE = "scene"
    LEGACY = "legacy"


def resolve_terrain_pipeline() -> TerrainPipeline:
    """Resolve the active terrain capability flag.

    Defaults to 'scene'. Fallbacks to 'legacy' when TERRALAB_TERRAIN_PIPELINE
    is set to 'legacy', '0', or 'false'.
    """
    env_val = (
        os.environ.get("TERRALAB_TERRAIN_PIPELINE", "scene")
        .lower()
        .strip()
    )
    if env_val in ("legacy", "0", "false"):
        return TerrainPipeline.LEGACY
    return TerrainPipeline.SCENE
