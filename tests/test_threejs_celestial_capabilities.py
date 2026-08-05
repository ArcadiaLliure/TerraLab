"""Conformance checks for the real Three.js celestial presentation path."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np

from TerraLab.application.controller import ApplicationController
from TerraLab.core.rendering_contracts.contracts import (
    HostedSurfaceOutput,
    HostedSurfaceTarget,
    RenderCapability,
    RenderTargetKind,
)
from TerraLab.application.render_planning import SceneRenderPlanner
from TerraLab.bootstrap.composition import create_backend_registry
from TerraLab.render.threejs.backend import ThreeJSRendererBackend
from TerraLab.render.threejs.bridge import ThreeJSBridge
from TerraLab.render.threejs.protocol import (
    OP_ACK,
    OP_REGISTER_RESOURCE,
    PROTOCOL_VERSION,
)
from TerraLab.scene.contracts import thaw_json_mapping
from TerraLab.scene.plans.stars import (
    StarPickBatch,
    StarScenePlan,
    StarSpriteBatch,
)


class _OutputPort:
    def __init__(self) -> None:
        self.frames: list[HostedSurfaceOutput] = []

    def frame_ready(self, output: HostedSurfaceOutput) -> None:
        self.frames.append(output)

    def pick_ready(self, result: object) -> None:
        del result

    def backend_failed(self, failure: object) -> None:
        del failure


def _plan():
    controller = ApplicationController()
    controller.set_observer(41.38, 2.17, 100.0)
    controller.set_camera(azimuth_deg=190.0, elevation_deg=35.0, zoom=1.5)
    controller.set_viewport(320, 200)
    plan = SceneRenderPlanner().build(controller.build_scene_frame())
    assert plan.celestial is not None
    return plan


def _plan_with_stars():
    plan = _plan()
    assert plan.celestial is not None
    sprites = StarSpriteBatch(
        screen_x=np.asarray((80.0, 160.0, 240.0), dtype=np.float32),
        screen_y=np.asarray((40.0, 100.0, 155.0), dtype=np.float32),
        base_rgba=np.asarray(
            ((210, 225, 255, 170), (255, 240, 180, 230), (170, 205, 255, 255)),
            dtype=np.uint8,
        ),
        medium_rgba=np.asarray(((255, 240, 180, 230),), dtype=np.uint8),
        weak_rgba=np.asarray(((210, 225, 255, 170),), dtype=np.uint8),
        medium_style_key=np.asarray((0, 0, 0), dtype=np.int32),
        weak_style_key=np.asarray((0, 0, 0), dtype=np.int32),
        medium_radius_tenths=np.asarray((5, 15, 24), dtype=np.int16),
        halo_bin=np.asarray((0, 1, 3), dtype=np.uint8),
        size_bin=np.asarray((0, 2, 6), dtype=np.uint8),
        weak_indices=np.asarray((0,), dtype=np.int32),
        medium_indices=np.asarray((1,), dtype=np.int32),
        bright_indices=np.asarray((2,), dtype=np.int32),
        smooth=True,
    )
    stars = StarScenePlan(
        sprites=sprites,
        picks=StarPickBatch(
            catalog_indices=np.asarray((11, 12, 13), dtype=np.int32),
            screen_x=sprites.screen_x,
            screen_y=sprites.screen_y,
        ),
        total_in_view=3,
        after_magnitude_cut=3,
        after_bucket=3,
        average_radius=1.5,
        counters={"selected": 3.0},
    )
    celestial = replace(plan.celestial, stars=stars, pure_colors=False)
    return replace(plan, celestial=celestial)


def test_threejs_declares_only_implemented_celestial_and_overlay_capabilities() -> (
    None
):
    backend = ThreeJSRendererBackend()
    assert backend.capabilities == frozenset(
        {
            RenderCapability.HOSTED_SURFACE,
            RenderCapability.SKY_BACKGROUND,
            RenderCapability.STARS,
            RenderCapability.EPHEMERIS_BODIES,
            RenderCapability.DEEP_SKY,
            RenderCapability.GRID,
            RenderCapability.LABELS,
            RenderCapability.SCOPE,
            RenderCapability.CONSTELLATIONS,
            RenderCapability.PICKING,
            RenderCapability.INTERACTION,
            RenderCapability.MEASUREMENTS,
            RenderCapability.TERRAIN_GEOMETRY,
            RenderCapability.TERRAIN_MATERIALS,
        }
    )
    registry = create_backend_registry()
    selected = registry.create(
        "threejs",
        target_kind=RenderTargetKind.HOSTED_SURFACE,
        required_capabilities=frozenset(
            {
                RenderCapability.SKY_BACKGROUND,
                RenderCapability.STARS,
                RenderCapability.DEEP_SKY,
            }
        ),
    )
    assert isinstance(selected, ThreeJSRendererBackend)


def test_celestial_transport_contains_draw_attributes_not_counter_manifests() -> (
    None
):
    prepared = ThreeJSRendererBackend.prepare_celestial_plan_for_bridge(
        _plan_with_stars()
    )
    primitives = {
        item["kind"]: item for item in prepared.manifest["primitives"]
    }
    sky = primitives["sky_gradient"]
    stars = primitives["star_batch"]

    assert sky["sample_width"] > 0
    assert sky["sample_height"] > 0
    assert sky["buffers"]["rgba"]["dtype"] == "uint8"
    assert {
        "positions",
        "colors",
        "radii",
        "halo_bins",
        "style",
        "pick_catalog_indices",
    }.issubset(stars["buffers"])
    assert stars["buffers"]["positions"]["element_count"] == 3
    assert stars["buffers"]["colors"]["components"] == 4
    assert stars["pure_colors"] is False

    encoded = json.dumps(thaw_json_mapping(prepared.manifest))
    assert '"count"' not in encoded
    assert '"mag"' not in encoded
    assert {resource.resource_id for resource in prepared.resources} >= {
        "celestial:sky-gradient:rgba",
        "celestial:stars:positions",
        "celestial:stars:colors",
    }


def test_celestial_resources_reuse_when_the_resolved_plan_is_unchanged() -> (
    None
):
    prepared = ThreeJSRendererBackend.prepare_celestial_plan_for_bridge(
        _plan_with_stars()
    )
    sent: list[object] = []
    bridge = ThreeJSBridge(outbound_handler=sent.append)
    bridge.start({"width": 320, "height": 200, "dpr": 1.0})

    first = bridge.submit_prepared_frame(prepared)
    registrations = [
        message for message in sent if message["op"] == OP_REGISTER_RESOURCE
    ]
    bridge.receive_inbound(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_ACK,
            "gen": prepared.generation,
            "seq": 1,
            "payload": {"operation": "render", "rendered": True},
        }
    )
    second = bridge.submit_prepared_frame(prepared)

    assert first["payload"]["mode"] == "full"
    assert second["payload"]["mode"] == "delta"
    assert second["payload"]["patch"] == {}
    assert len(
        [message for message in sent if message["op"] == OP_REGISTER_RESOURCE]
    ) == len(registrations)
    bridge.close()


def test_backend_submits_the_same_celestial_plan_to_the_hosted_surface() -> (
    None
):
    bridge = ThreeJSBridge()
    backend = ThreeJSRendererBackend(bridge)
    output_port = _OutputPort()
    plan = _plan_with_stars()

    backend.start(output_port)
    output = backend.render(
        plan,
        HostedSurfaceTarget("celestial-surface", width=320, height=200),
    )

    assert output.generation == plan.generation
    assert output.metadata["celestial_plan_native"] is True
    assert output_port.frames == [output]
    manifest = backend.get_frame_manifest(plan.generation)
    assert manifest is not None
    assert any(item["kind"] == "star_batch" for item in manifest["primitives"])
    backend.close()


def test_threejs_host_uses_persistent_gpu_layers_and_visual_only_shaders() -> (
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
        "BufferGeometry",
        "InstancedBufferGeometry",
        "ShaderMaterial",
        "textureCache",
        "replaceAttribute",
        "horizonAlpha",
        "uPureColors",
        "halo_bins",
    ):
        assert token in source
    assert "clearScene" not in source
    assert "Skyfield" not in source
    assert "Bortle" not in source
    assert "magnitude_limit" not in source
