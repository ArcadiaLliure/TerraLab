"""Tests for Phase 09: Picking resolution, selection policy, and pure measurement plans."""

from __future__ import annotations

import inspect
import numpy as np
import pytest

from TerraLab.application.interaction import (
    ApplicationInteractionController,
    PickingPipeline,
    resolve_picking_pipeline,
)
from TerraLab.core.rendering_contracts.contracts import PickRequest
from TerraLab.scene.picking import PickIndex, PickRecord
from TerraLab.scene.plans.interaction import (
    build_measurement_item_plan,
)


def test_picking_model_has_no_qt_or_runtime_imports() -> None:
    import TerraLab.scene.picking as picking
    import TerraLab.scene.plans.interaction as interaction

    for mod in (picking, interaction):
        source = inspect.getsource(mod)
        assert "PyQt" not in source
        assert "QPainter" not in source
        assert "runtime" not in source


def test_picking_capability_defaults_to_scene_and_has_legacy_rollback() -> (
    None
):
    assert resolve_picking_pipeline() is PickingPipeline.SCENE
    assert resolve_picking_pipeline() == "scene"
    assert resolve_picking_pipeline() != "legacy"


def test_pick_index_priority_solar_over_ngc_and_stars() -> None:
    sun_record = PickRecord(
        kind="sky",
        key="sun",
        name="Sun",
        alt_deg=45.0,
        az_deg=180.0,
        screen_x=100.0,
        screen_y=100.0,
        radius_px=15.0,
    )
    ngc_record = PickRecord(
        kind="ngc",
        key="M31",
        name="Andromeda Galaxy",
        alt_deg=30.0,
        az_deg=45.0,
        screen_x=102.0,
        screen_y=102.0,
        radius_px=15.0,
    )
    star_x = np.array([101.0], dtype=np.float32)
    star_y = np.array([101.0], dtype=np.float32)
    star_ids = np.array([42], dtype=np.int32)
    star_mag = np.array([2.5], dtype=np.float32)

    index = PickIndex(
        generation=10,
        sky_objects=(sun_record,),
        ngc_objects=(ngc_record,),
        star_catalog_indices=star_ids,
        star_screen_x=star_x,
        star_screen_y=star_y,
        star_mag=star_mag,
    )

    # Query at (100, 100) -> Should pick Sun due to higher priority
    res = index.query(100.0, 100.0, radius=20.0)
    assert res["kind"] == "sky"
    assert res["key"] == "sun"
    assert res["name"] == "Sun"

    # Index without Sun -> Should pick NGC over star
    index_no_sun = PickIndex(
        generation=10,
        sky_objects=(),
        ngc_objects=(ngc_record,),
        star_catalog_indices=star_ids,
        star_screen_x=star_x,
        star_screen_y=star_y,
        star_mag=star_mag,
    )
    res_ngc = index_no_sun.query(102.0, 102.0, radius=20.0)
    assert res_ngc["kind"] == "ngc"
    assert res_ngc["name"] == "Andromeda Galaxy"

    # Index with only stars -> Should pick star
    index_only_stars = PickIndex(
        generation=10,
        sky_objects=(),
        ngc_objects=(),
        star_catalog_indices=star_ids,
        star_screen_x=star_x,
        star_screen_y=star_y,
        star_mag=star_mag,
    )
    res_star = index_only_stars.query(101.0, 101.0, radius=20.0)
    assert res_star["kind"] == "star"
    assert res_star["star"]["id"] == 42


def test_pick_index_fallback_to_sky_coords() -> None:
    def dummy_unproject(x: float, y: float) -> tuple[float, float]:
        return (15.5, 270.2)

    index = PickIndex(
        generation=1,
        unproject_fn=dummy_unproject,
    )
    res = index.query(50.0, 50.0, radius=20.0)
    assert res["kind"] == "sky"
    assert pytest.approx(res["alt"], 1e-3) == 15.5
    assert pytest.approx(res["az"], 1e-3) == 270.2


def test_application_controller_discards_stale_requests() -> None:
    controller = ApplicationInteractionController()
    req1 = PickRequest(generation=10, request_id="pick:10", x=10.0, y=10.0)
    index = PickIndex(generation=10)

    res1 = controller.process_pick_request(req1, index)
    assert res1.generation == 10
    assert not res1.payload.get("stale")

    # Stale request generation < 10
    req_stale = PickRequest(generation=5, request_id="pick:5", x=10.0, y=10.0)
    res_stale = controller.process_pick_request(req_stale, index)
    assert res_stale.payload.get("stale") is True


def test_pure_ruler_measurement_plan_geometry() -> None:
    item = build_measurement_item_plan("ruler", a=(0.0, 0.0), b=(0.0, 10.0))
    assert item.tool == "ruler"
    assert "Distance:" in item.label
    assert len(item.paths_sky[0]) == 72
    assert "a" in item.handles_sky and "b" in item.handles_sky


def test_pure_circle_measurement_plan_geometry() -> None:
    item = build_measurement_item_plan("circle", a=(0.0, 0.0), b=(0.0, 5.0))
    assert item.tool == "circle"
    assert "Diameter:" in item.label
    assert "Area:" in item.label
    assert item.hit_polygon_sky is not None
    assert len(item.hit_polygon_sky) == 129
