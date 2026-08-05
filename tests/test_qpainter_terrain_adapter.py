"""Unit tests for Phase 13 QPainter terrain presentation adapter."""

from PyQt5.QtGui import QImage, QPainter

from TerraLab.application.terrain import (
    TerrainPipeline,
    resolve_terrain_pipeline,
)
from TerraLab.core.rendering_contracts.plans import (
    Bounds,
    MaterialParameters,
    TerrainMeshResource,
    TerrainSurfaceAttributes,
    TerrainTile,
)
from TerraLab.render.qpainter.terrain import (
    QPainterTerrainAdapter,
)
def test_qpainter_terrain_adapter_paints_a_shared_neutral_mesh_resource():
    """The View consumes the same typed mesh resource as the Three.js path."""

    vertices = ((10.0, 10.0, 1.0), (90.0, 10.0, 1.0), (50.0, 90.0, 1.0))
    bounds = Bounds.enclosing(vertices)
    mesh = TerrainMeshResource(
        primitive_id="terrain:test",
        vertices=vertices,
        indices=(0, 1, 2),
        normals=((0.0, 0.0, 1.0),) * 3,
        surface=TerrainSurfaceAttributes(
            colors=((0.8, 0.2, 0.1, 1.0),) * 3,
            alphas=(1.0,) * 3,
            uv=((0.0, 0.0),) * 3,
            elevations_m=(1.0,) * 3,
            object_ids=("terrain:0", "terrain:1", "terrain:2"),
        ),
        tiles=(TerrainTile("terrain:test:0", 0, 3, bounds),),
        layer_order=5,
        material=MaterialParameters("terrain:test", blend_mode="alpha"),
        bounds=bounds,
        geometry_version="test",
        material_version="test",
        frame_generation=1,
    )
    image = QImage(100, 100, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        drawn = QPainterTerrainAdapter().paint_mesh(painter, mesh)
    finally:
        painter.end()

    assert drawn == 1
    assert image.pixelColor(50, 30).alpha() > 0


def test_resolve_terrain_pipeline(monkeypatch):
    """Verify capability resolution for TERRALAB_TERRAIN_PIPELINE."""
    monkeypatch.delenv("TERRALAB_TERRAIN_PIPELINE", raising=False)
    assert resolve_terrain_pipeline() == TerrainPipeline.SCENE

    monkeypatch.setenv("TERRALAB_TERRAIN_PIPELINE", "legacy")
    assert resolve_terrain_pipeline() == TerrainPipeline.LEGACY

    monkeypatch.setenv("TERRALAB_TERRAIN_PIPELINE", "scene")
    assert resolve_terrain_pipeline() == TerrainPipeline.SCENE
