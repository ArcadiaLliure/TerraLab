"""Unit tests for pure renderer-neutral TerrainGeometryPlan and planner."""

from __future__ import annotations

import sys
import numpy as np

from TerraLab.application.terrain_geometry import (
    TerrainGeometryPipeline,
    resolve_terrain_geometry_pipeline,
)
from TerraLab.scene.plans.terrain_geometry import (
    TerrainGeometryPlan,
    TerrainHitRecord,
    build_terrain_geometry_plan,
)
from TerraLab.terrain.render.overlay_types import _TerrainRenderAsset


def create_dummy_render_asset() -> _TerrainRenderAsset:
    """Create a minimal valid _TerrainRenderAsset for testing."""
    n_dist = 5
    n_az = 8
    azimuths = np.linspace(0, 360, n_az, endpoint=False, dtype=np.float32)
    azimuths_closed = np.linspace(
        0, 360, n_az + 1, endpoint=True, dtype=np.float32
    )
    distances = np.linspace(10, 50, n_dist, dtype=np.float32)
    altitudes = np.broadcast_to(
        np.linspace(-10, 20, n_dist, dtype=np.float32)[:, None], (n_dist, n_az)
    ).copy()
    altitudes_closed = np.broadcast_to(
        np.linspace(-10, 20, n_dist, dtype=np.float32)[:, None],
        (n_dist, n_az + 1),
    ).copy()
    elevations = np.ones((n_dist, n_az), dtype=np.float32) * 100.0
    valid = np.ones((n_dist, n_az), dtype=bool)
    valid_closed = np.ones((n_dist, n_az + 1), dtype=bool)
    visible = np.ones((n_dist, n_az), dtype=bool)
    visible_closed = np.ones((n_dist, n_az + 1), dtype=bool)

    normals = np.zeros((n_dist, n_az), dtype=np.float32)
    normals_z = np.ones((n_dist, n_az), dtype=np.float32)

    near_patch = np.zeros((3, 3), dtype=np.float32)
    near_valid = np.ones((3, 3), dtype=bool)
    near_coords = np.array([-10.0, 0.0, 10.0], dtype=np.float32)

    return _TerrainRenderAsset(
        mesh_id=101,
        azimuths=azimuths,
        azimuths_closed=azimuths_closed,
        distances=distances,
        altitudes=altitudes,
        altitudes_closed=altitudes_closed,
        elevations=elevations,
        valid=valid,
        valid_closed=valid_closed,
        visible=visible,
        visible_closed=visible_closed,
        normal_x=normals,
        normal_y=normals,
        normal_z=normals_z,
        near_patch_eastings=near_coords,
        near_patch_northings=near_coords,
        near_patch_altitudes=near_patch,
        near_patch_elevations=near_patch,
        near_patch_valid=near_valid,
        near_patch_normal_x=near_patch,
        near_patch_normal_y=near_patch,
        near_patch_normal_z=np.ones((3, 3), dtype=np.float32),
    )


def dummy_projection(alt: float, az: float) -> tuple[float, float] | None:
    """Mock projection mapping (alt, az) to screen (x, y)."""
    if not np.isfinite(alt) or not np.isfinite(az):
        return None
    # Standard linear mapping for test
    x = (az % 360.0) * (320.0 / 360.0)
    y = 100.0 - alt * 2.0
    return (x, y)


def dummy_projection_numpy(altitudes: np.ndarray, az_grid: np.ndarray):
    """Mock vector projection mapping."""
    x = (az_grid % 360.0) * (320.0 / 360.0)
    y = 100.0 - altitudes * 2.0
    valid = np.isfinite(altitudes) & np.isfinite(az_grid)
    return (x, y, valid)


def test_terrain_geometry_planner_qt_purity():
    """Verify that TerraLab.scene.plans.terrain_geometry imports no Qt modules."""
    plan_module = sys.modules.get("TerraLab.scene.plans.terrain_geometry")
    assert plan_module is not None
    module_dict = plan_module.__dict__
    for key in module_dict:
        assert "PyQt" not in key and "QPainter" not in key


def test_build_terrain_geometry_plan_basic():
    asset = create_dummy_render_asset()
    plan = build_terrain_geometry_plan(
        asset=asset,
        projection_fn=dummy_projection,
        width=320,
        height=180,
        cur_az=180.0,
        az_min=0.0,
        az_max=360.0,
        projection_fn_numpy=dummy_projection_numpy,
        rasterize=True,
    )
    assert isinstance(plan, TerrainGeometryPlan)
    assert plan.mesh_id == 101
    assert plan.triangle_geometry is not None
    assert plan.triangle_geometry.xy.ndim == 3
    assert plan.raster_cache is not None
    tri_id, bary_u, bary_v = plan.raster_cache
    assert tri_id.shape == (180 * 2, 320 * 2)


def test_terrain_hit_query_resolution():
    asset = create_dummy_render_asset()
    plan = build_terrain_geometry_plan(
        asset=asset,
        projection_fn=dummy_projection,
        width=320,
        height=180,
        cur_az=180.0,
        az_min=0.0,
        az_max=360.0,
        projection_fn_numpy=dummy_projection_numpy,
        rasterize=True,
    )
    hit = plan.query_screen_hit(160.0, 90.0, width=320, height=180)
    if hit is not None:
        assert isinstance(hit, TerrainHitRecord)
        assert hit.hit is True
        assert hit.triangle_index >= 0
        assert 0.0 <= hit.bary_u <= 1.0 + 1e-5
        assert 0.0 <= hit.bary_v <= 1.0 + 1e-5


def test_terrain_pipeline_capability_routing(monkeypatch):
    monkeypatch.setenv("TERRALAB_TERRAIN_GEOMETRY_PIPELINE", "scene")
    assert resolve_terrain_geometry_pipeline() == TerrainGeometryPipeline.SCENE

    monkeypatch.setenv("TERRALAB_TERRAIN_GEOMETRY_PIPELINE", "legacy")
    assert (
        resolve_terrain_geometry_pipeline() == TerrainGeometryPipeline.LEGACY
    )

    monkeypatch.setenv("TERRALAB_TERRAIN_GEOMETRY_PIPELINE", "0")
    assert (
        resolve_terrain_geometry_pipeline() == TerrainGeometryPipeline.LEGACY
    )
