"""Unit tests for Phase 12 renderer-neutral terrain material plan and capability router."""

import numpy as np
import pytest

from TerraLab.application.terrain_materials import (
    TerrainMaterialPipeline,
    resolve_terrain_material_pipeline,
)
from TerraLab.scene.plans.terrain_materials import (
    TerrainAtmospherePlan,
    build_terrain_material_plan,
)
from TerraLab.terrain.render.config import (
    TerrainRenderSettings,
)


def test_terrain_material_plan_immutability():
    """Verify that material plan RGBA arrays are immutable."""
    base_rgba = np.full((10, 4), 128, dtype=np.uint8)
    intensity = np.ones(10, dtype=np.float32)
    distance_m = np.linspace(100.0, 50000.0, 10, dtype=np.float32)
    settings = TerrainRenderSettings()

    plan = build_terrain_material_plan(
        mesh_id=42,
        base_rgba=base_rgba,
        intensity=intensity,
        distance_m=distance_m,
        settings=settings,
    )

    assert plan.mesh_id == 42
    assert plan.vertex_rgba.shape == (10, 4)
    assert plan.vertex_rgba.dtype == np.uint8
    assert not plan.base_rgba.flags.writeable
    assert not plan.vertex_rgba.flags.writeable

    with pytest.raises(ValueError):
        plan.vertex_rgba[0, 0] = 255


def test_terrain_atmosphere_plan_immutability():
    """Verify that atmosphere plan fog factor arrays are immutable."""
    fog = np.linspace(0.0, 1.0, 10, dtype=np.float32)
    horizon = np.array([120, 140, 180], dtype=np.float32)
    haze = np.array([120, 140, 180], dtype=np.float32)

    atmo = TerrainAtmospherePlan(
        fog_factors=fog,
        horizon_rgb=horizon,
        haze_color=haze,
    )

    assert not atmo.fog_factors.flags.writeable
    with pytest.raises(ValueError):
        atmo.fog_factors[0] = 0.5


def test_vibrant_vs_original_material_plan():
    """Verify that vibrant and original surface visual styles produce distinct plans."""
    base_rgba = np.full((10, 4), 100, dtype=np.uint8)
    intensity = np.ones(10, dtype=np.float32) * 0.8
    distance_m = np.linspace(1000.0, 80000.0, 10, dtype=np.float32)
    settings = TerrainRenderSettings()

    plan_orig = build_terrain_material_plan(
        mesh_id=1,
        base_rgba=base_rgba,
        intensity=intensity,
        distance_m=distance_m,
        settings=settings,
        style_override="original",
    )

    plan_vibrant = build_terrain_material_plan(
        mesh_id=1,
        base_rgba=base_rgba,
        intensity=intensity,
        distance_m=distance_m,
        settings=settings,
        style_override="vibrant",
    )

    assert plan_orig.style == "original"
    assert plan_vibrant.style == "vibrant"
    assert plan_orig.cache_key != plan_vibrant.cache_key


def test_resolve_terrain_material_pipeline(monkeypatch):
    """Verify capability resolution for TERRALAB_TERRAIN_MATERIAL_PIPELINE."""
    monkeypatch.delenv("TERRALAB_TERRAIN_MATERIAL_PIPELINE", raising=False)
    assert resolve_terrain_material_pipeline() == TerrainMaterialPipeline.SCENE

    monkeypatch.setenv("TERRALAB_TERRAIN_MATERIAL_PIPELINE", "legacy")
    assert resolve_terrain_material_pipeline() == TerrainMaterialPipeline.LEGACY

    monkeypatch.setenv("TERRALAB_TERRAIN_MATERIAL_PIPELINE", "scene")
    assert resolve_terrain_material_pipeline() == TerrainMaterialPipeline.SCENE
