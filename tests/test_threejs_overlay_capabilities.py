"""Conformance checks for Three.js presentation of resolved overlay plans."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from TerraLab.application.controller import ApplicationController
from TerraLab.core.rendering_contracts.contracts import (
    HostedSurfaceOutput,
    HostedSurfaceTarget,
    PickRequest,
)
from TerraLab.application.render_planning import SceneRenderPlanner
from TerraLab.core.rendering_contracts.plans import (
    Bounds,
    InteractionAffordance,
    InteractionPlan,
)
from TerraLab.render.threejs.backend import ThreeJSRendererBackend
from TerraLab.render.threejs.bridge import ThreeJSBridge
from TerraLab.render.threejs.protocol import OP_PICK_RESULT, PROTOCOL_VERSION
from TerraLab.scene.contracts import thaw_json_mapping
from TerraLab.scene.plans.interaction import (
    ScreenMeasurementItem,
    ScreenMeasurementLabel,
    ScreenMeasurementPlan,
    SelectionPlan,
)
from TerraLab.scene.plans.labels import (
    CompassPlan,
    FontStyle,
    GridPlan,
    HudPlan,
    LineSegment,
    ScreenRect,
    StrokeStyle,
    TextBatch,
    TextLabel,
    TextLayoutStats,
    TextStyle,
)
from TerraLab.scene.plans.overlays import (
    CirclePrimitive,
    ConstellationOverlayPlan,
    ScopeOverlayPlan,
)


class _OutputPort:
    def __init__(self) -> None:
        self.frames: list[HostedSurfaceOutput] = []
        self.picks: list[object] = []

    def frame_ready(self, output: HostedSurfaceOutput) -> None:
        self.frames.append(output)

    def pick_ready(self, result: object) -> None:
        self.picks.append(result)

    def backend_failed(self, failure: object) -> None:
        del failure


def _text_batch(
    text: str,
    *,
    x: float,
    y: float,
    style: TextStyle,
    bounds: ScreenRect | None = None,
) -> TextBatch:
    label_bounds = bounds or ScreenRect(x, y - 12.0, 100.0, 16.0)
    return TextBatch(
        labels=(
            TextLabel(
                text,
                (x, y),
                "baseline_left",
                style,
                100,
                ScreenRect(0.0, 0.0, 320.0, 200.0),
                1,
                label_bounds,
            ),
        ),
        stats=TextLayoutStats(1, 1, 0, 0, False),
        cache_key=("threejs-overlay", text, x, y),
    )


def _resolved_overlay_plan():
    controller = ApplicationController()
    controller.set_observer(41.38, 2.17, 100.0)
    controller.set_camera(azimuth_deg=190.0, elevation_deg=35.0, zoom=1.5)
    controller.set_viewport(320, 200)
    plan = SceneRenderPlanner().build(controller.build_scene_frame())
    assert plan.overlays is not None

    text_style = TextStyle(
        FontStyle("SansSerif", 11.0, 700),
        (220, 240, 255, 230),
        (20, 30, 45, 180),
        3.0,
        2.0,
        4.0,
    )
    labels = _text_batch("Resolved label", x=36.0, y=46.0, style=text_style)
    compass_labels = _text_batch("N", x=160.0, y=26.0, style=text_style)
    readout = _text_batch(
        "FOV 2.00° × 1.20°", x=12.0, y=188.0, style=text_style
    )
    constellation_labels = _text_batch(
        "Orion", x=190.0, y=92.0, style=text_style
    )
    hud_labels = _text_batch("HUD resolved", x=14.0, y=22.0, style=text_style)
    measurement_label = ScreenMeasurementLabel(
        ("Distance: 2.400deg",),
        ScreenRect(112.0, 114.0, 128.0, 24.0),
        (118.0, 131.0),
        text_style,
        (0, 0, 0, 190),
        5.0,
    )
    measurement = ScreenMeasurementItem(
        paths=(((48.0, 150.0), (116.0, 118.0), (204.0, 142.0)),),
        line_style=StrokeStyle((255, 245, 120, 220), 1.2),
        glow_style=StrokeStyle((255, 255, 180, 100), 3.2),
        handles=((48.0, 150.0), (204.0, 142.0)),
        handle_radius_px=4.0,
        label=measurement_label,
        selected=True,
    )
    overlays = replace(
        plan.overlays,
        grid=GridPlan(
            (((0.0, 100.0), (320.0, 100.0)),),
            StrokeStyle((0, 255, 255, 80), 1.0, True),
        ),
        compass=CompassPlan(
            (
                LineSegment(
                    (160.0, 6.0),
                    (160.0, 18.0),
                    StrokeStyle((255, 255, 255, 220), 1.0),
                ),
            ),
            compass_labels,
        ),
        labels=labels,
        hud=HudPlan(
            ScreenRect(8.0, 6.0, 128.0, 28.0),
            (12, 18, 28, 210),
            StrokeStyle((180, 210, 240, 180), 1.0),
            5.0,
            hud_labels,
            True,
        ),
        scope=ScopeOverlayPlan(
            True,
            ScreenRect(0.0, 0.0, 320.0, 200.0),
            "circle",
            (160.0, 100.0),
            76.0,
            None,
            (10, 12, 16, 220),
            StrokeStyle((255, 200, 80, 200), 1.5),
            (
                LineSegment(
                    (148.0, 100.0),
                    (172.0, 100.0),
                    StrokeStyle((255, 200, 80, 200), 1.5),
                ),
                LineSegment(
                    (160.0, 88.0),
                    (160.0, 112.0),
                    StrokeStyle((255, 200, 80, 200), 1.5),
                ),
            ),
            readout,
        ),
        constellations=ConstellationOverlayPlan(
            True,
            True,
            (
                LineSegment(
                    (176.0, 74.0),
                    (218.0, 106.0),
                    StrokeStyle((100, 180, 255, 180), 1.5),
                ),
            ),
            (
                CirclePrimitive((176.0, 74.0), 4.0, (180, 220, 255, 220)),
                CirclePrimitive(
                    (218.0, 106.0),
                    6.0,
                    (255, 230, 100, 255),
                    StrokeStyle((255, 200, 50, 180), 2.0),
                ),
            ),
            constellation_labels,
            LineSegment(
                (218.0, 106.0),
                (248.0, 84.0),
                StrokeStyle((255, 120, 120, 220), 1.5, True),
            ),
        ),
        selection=SelectionPlan(
            plan.generation,
            "star",
            "12",
            "Gaia #12",
            84.0,
            78.0,
            0.5,
            18.0,
            0.75,
        ),
        measurements=ScreenMeasurementPlan(plan.generation, (measurement,)),
    )
    interaction = InteractionPlan(
        plan.generation,
        (
            InteractionAffordance(
                "measurement:handle:0",
                "drag",
                bounds=Bounds((44.0, 146.0, 0.0), (52.0, 154.0, 0.0)),
                active=True,
                layer_order=120,
            ),
        ),
    )
    return replace(plan, overlays=overlays, interaction=interaction)


def test_overlay_transport_uses_resolved_geometry_and_batched_resources() -> (
    None
):
    prepared = ThreeJSRendererBackend.prepare_celestial_plan_for_bridge(
        _resolved_overlay_plan()
    )
    primitives = {item["kind"] for item in prepared.manifest["primitives"]}
    assert {
        "grid_lines",
        "screen_line_batch",
        "screen_circle_batch",
        "screen_rect_batch",
        "text_batch",
        "scope_mask",
        "interaction_affordance",
    }.issubset(primitives)
    text = next(
        item
        for item in prepared.manifest["primitives"]
        if item["kind"] == "text_batch"
    )
    assert set(text["buffers"]) == {"bounds", "baselines", "clips", "strings"}
    assert text["buffers"]["strings"]["dtype"] == "utf8_string_table"
    assert text["buffers"]["bounds"]["element_count"] > 0
    assert any(
        resource.resource_id.endswith(":strings")
        for resource in prepared.resources
    )
    assert '"count"' not in json.dumps(thaw_json_mapping(prepared.manifest))


def test_threejs_renders_overlay_plan_and_returns_model_pick_result() -> None:
    plan = _resolved_overlay_plan()
    output_port = _OutputPort()
    backend = ThreeJSRendererBackend(ThreeJSBridge())
    backend.start(output_port)
    output = backend.render(
        plan, HostedSurfaceTarget("overlay-surface", 320, 200)
    )

    assert output.metadata["overlay_plan_native"] is True
    assert output_port.frames == [output]
    assert backend.get_frame_manifest(plan.generation) is not None

    assert (
        backend.request_pick(
            PickRequest(plan.generation, "overlay-pick", 160.0, 100.0)
        )
        is None
    )
    backend.bridge.receive_inbound(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_PICK_RESULT,
            "gen": plan.generation,
            "seq": 1,
            "payload": {
                "request_id": "overlay-pick",
                "hit": True,
                "object_id": "scope:mask",
                "object_kind": "scope",
                "distance": 2.0,
                "world_point": None,
                "surface_coordinates": None,
                "metadata": {"shape": "circle"},
            },
        }
    )
    assert output_port.picks[0].generation == plan.generation
    assert output_port.picks[0].payload["purpose"] == "select"
    assert output_port.picks[0].object_kind == "scope"
    assert (
        backend.request_pick(
            PickRequest(
                plan.generation,
                "overlay-drag",
                160.0,
                100.0,
                purpose="interaction",
            )
        )
        is None
    )
    backend.close()


def test_host_separates_world_screen_text_and_interaction_overlay_groups() -> (
    None
):
    source = (
        Path(__file__).resolve().parents[1]
        / "TerraLab"
        / "render"
        / "threejs"
        / "assets"
        / "threejs_runner.js"
    ).read_text(encoding="utf-8")

    for token in (
        "worldGroup",
        "screenOverlayGroup",
        "textOverlayGroup",
        "interactionOverlayGroup",
        "InstancedBufferGeometry",
        "CanvasTexture",
        "upsertScreenLines",
        "upsertTextBatch",
        "upsertScopeMask",
    ):
        assert token in source
    assert "widgets/" not in source
    assert "Skyfield" not in source
    assert "Bortle" not in source
