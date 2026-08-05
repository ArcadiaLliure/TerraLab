"""Tests for Phase 10: scope, optics, spherical math, and constellations decouple slice."""

import os
import tempfile

from TerraLab.scene.spherical_math import (
    altaz_to_ra_dec,
    destination_point,
    ra_dec_to_alt_az,
    slerp_arc_points,
)
from TerraLab.scene.scope import (
    FAST_STEP_DEG,
    SHAPE_CIRCLE,
    SLOW_STEP_DEG,
    SPEED_FAST,
    SPEED_SLOW,
    compute_scope_fov_deg,
)
from TerraLab.scene.constellations import (
    ConstellationGroup,
    ConstellationNode,
    load_constellation_groups_from_json,
    save_constellation_groups_to_json,
)
from TerraLab.application.scope import (
    ScopeController,
    ScopePipeline,
    resolve_scope_pipeline,
)
from TerraLab.application.constellations import (
    ConstellationController,
    ConstellationPipeline,
    resolve_constellation_pipeline,
)


def test_spherical_math_roundtrip():
    # Test RA/Dec <-> Alt/Az conversions and wrapping
    ra, dec = 180.0, 45.0
    alt, az = ra_dec_to_alt_az(
        ra, dec, ut_hour=12.0, day_of_year=1, latitude_deg=40.0
    )
    ra_res, dec_res = altaz_to_ra_dec(
        alt, az, ut_hour=12.0, day_of_year=1, latitude_deg=40.0
    )
    assert abs(ra - ra_res) < 1e-4 or abs(abs(ra - ra_res) - 360.0) < 1e-4
    assert abs(dec - dec_res) < 1e-4

    # Test destination point geodesic
    dest = destination_point((45.0, 90.0), bearing_deg=0.0, distance_deg=5.0)
    assert abs(dest[0] - 50.0) < 1e-4

    # Test slerp arc
    arc = slerp_arc_points((0.0, 0.0), (10.0, 0.0), n_points=5)
    assert len(arc) == 5
    assert abs(arc[0][0] - 0.0) < 1e-4
    assert abs(arc[-1][0] - 10.0) < 1e-4


def test_scope_fov_and_optics():
    w_tiny, h_tiny = compute_scope_fov_deg(250.0, sensor_key="tiny")
    assert w_tiny > 0.0 and h_tiny > 0.0

    w_ff, h_ff = compute_scope_fov_deg(250.0, sensor_key="full_frame")
    assert w_ff > w_tiny and h_ff > h_tiny

    # Test manual override
    w_m, h_m = compute_scope_fov_deg(250.0, manual_override=(2.5, 1.5))
    assert w_m == 2.5 and h_m == 1.5


def test_scope_controller_nudge_and_tracking():
    ctrl = ScopeController()
    ctrl.activate()
    assert ctrl.state.enabled
    assert ctrl.state.awaiting_center_click

    ctrl.state.center = (45.0, 180.0)
    ctrl.nudge("up", speed_mode=SPEED_SLOW)
    assert abs(ctrl.state.center[0] - (45.0 + SLOW_STEP_DEG)) < 1e-6

    ctrl.nudge("right", speed_mode=SPEED_FAST)
    assert abs(ctrl.state.center[1] - (180.0 + FAST_STEP_DEG)) < 1e-6

    ctrl.deactivate()
    assert not ctrl.state.enabled


def test_scope_plan_building():
    ctrl = ScopeController()
    ctrl.activate()
    ctrl.state.center = (45.0, 180.0)
    ctrl.set_shape(SHAPE_CIRCLE)

    plan = ctrl.build_plan(
        screen_center_px=(400.0, 300.0),
        viewport_w=800.0,
        viewport_h=600.0,
        px_per_deg=100.0,
        hud_metrics={"airmass_x": 1.2},
    )

    assert plan.enabled
    assert plan.shape == SHAPE_CIRCLE
    assert plan.center_px == (400.0, 300.0)
    assert plan.hud_metrics["airmass_x"] == 1.2


def test_constellation_json_persistence_roundtrip():
    groups = [
        ConstellationGroup(
            name="Test Constellation",
            nodes=[
                ConstellationNode(
                    ra_deg=10.0,
                    dec_deg=20.0,
                    star_id="star_1",
                    star_name="Alpha",
                ),
                ConstellationNode(
                    ra_deg=12.0,
                    dec_deg=22.0,
                    star_id="star_2",
                    star_name="Beta",
                    connect_from_prev=True,
                ),
            ],
        )
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = os.path.join(tmpdir, "constellations.json")
        saved = save_constellation_groups_to_json(json_path, groups)
        assert saved

        loaded = load_constellation_groups_from_json(json_path)
        assert len(loaded) == 1
        assert loaded[0].name == "Test Constellation"
        assert len(loaded[0].nodes) == 2
        assert loaded[0].nodes[0].star_name == "Alpha"
        assert loaded[0].nodes[1].connect_from_prev is True


def test_constellation_controller_actions():
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = os.path.join(tmpdir, "constellations.json")
        ctrl = ConstellationController(data_path=json_path)
        ctrl.set_enabled(True)
        ctrl.set_visible(True)

        ctrl.add_node(
            ra_deg=50.0,
            dec_deg=10.0,
            star_id="s1",
            star_name="Star 1",
            group_name="Orion",
        )
        ctrl.add_node(
            ra_deg=52.0,
            dec_deg=12.0,
            star_id="s2",
            star_name="Star 2",
            group_name="Orion",
        )

        assert len(ctrl.state.groups) == 1
        assert len(ctrl.state.groups[0].nodes) == 2

        # Select node and delete
        ctrl.state.selected_group_index = 0
        ctrl.state.selected_node_index = 1
        deleted = ctrl.delete_selected()
        assert deleted
        assert len(ctrl.state.groups[0].nodes) == 1

        # Test undo
        undone = ctrl.undo()
        assert undone
        assert len(ctrl.state.groups[0].nodes) == 2


def test_constellation_plan_building():
    ctrl = ConstellationController()
    ctrl.set_enabled(True)
    ctrl.set_visible(True)
    ctrl.add_node(
        ra_deg=50.0,
        dec_deg=10.0,
        star_id="s1",
        star_name="Star 1",
        group_name="Test",
    )
    ctrl.add_node(
        ra_deg=52.0,
        dec_deg=12.0,
        star_id="s2",
        star_name="Star 2",
        group_name="Test",
    )

    # Simple identity projection mock
    def mock_project(ra, dec):
        return (ra * 10.0, dec * 10.0)

    plan = ctrl.build_plan(mock_project, viewport_w=1000.0, viewport_h=1000.0)
    assert plan.visible
    assert len(plan.nodes) == 2
    assert len(plan.segments) == 1
    assert len(plan.labels) == 1
    assert plan.labels[0].name == "Test"


def test_capability_routes():
    assert resolve_scope_pipeline() == ScopePipeline.SCENE
    assert resolve_constellation_pipeline() == ConstellationPipeline.SCENE
