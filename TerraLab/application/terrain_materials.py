"""Terrain material application controller and capability resolution."""

from enum import Enum
import os


class TerrainMaterialPipeline(str, Enum):
    SCENE = "scene"
    LEGACY = "legacy"


def resolve_terrain_material_pipeline() -> TerrainMaterialPipeline:
    """Resolve the active terrain material capability flag.

    Defaults to 'scene'. Fallbacks to 'legacy' when TERRALAB_TERRAIN_MATERIAL_PIPELINE
    is set to 'legacy', '0', or 'false'.
    """
    env_val = (
        os.environ.get("TERRALAB_TERRAIN_MATERIAL_PIPELINE", "scene")
        .lower()
        .strip()
    )
    if env_val in ("legacy", "0", "false"):
        return TerrainMaterialPipeline.LEGACY
    return TerrainMaterialPipeline.SCENE
