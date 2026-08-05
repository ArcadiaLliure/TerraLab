"""Typed binary-resource checks for the sole Three.js preparation path."""

from __future__ import annotations

import json

from TerraLab.core.rendering_contracts.plans import (
    Bounds,
    InteractionPlan,
    MaterialParameters,
    PickingPlan,
    RenderPlanBundle,
    ResourcePlan,
    SceneRenderPlan,
    TerrainMeshResource,
    TerrainSurfaceAttributes,
    TerrainTile,
)
from TerraLab.render.conformance import create_minimal_test_frame
from TerraLab.render.threejs.backend import ThreeJSRendererBackend
from TerraLab.render.threejs.bridge import ThreeJSBridge
from TerraLab.render.threejs.protocol import (
    OP_ACK,
    OP_REGISTER_RESOURCE,
    PROTOCOL_VERSION,
)
from TerraLab.scene.contracts import thaw_json_mapping


def _bundle() -> RenderPlanBundle:
    generation = 7
    bounds = Bounds((0.0, 0.0, 0.0), (100.0, 100.0, 20.0))
    terrain = TerrainMeshResource(
        primitive_id="terrain-near",
        vertices=(
            (0.0, 0.0, 10.0),
            (100.0, 0.0, 14.0),
            (0.0, 100.0, 12.0),
        ),
        view_vertices=((-1.0, 0.1, 10.0), (1.0, 0.1, 14.0), (-1.0, 1.0, 12.0)),
        indices=(0, 1, 2),
        normals=((0.0, 0.0, 1.0),) * 3,
        surface=TerrainSurfaceAttributes(
            colors=((0.1, 0.2, 0.3, 1.0),) * 3,
            alphas=(1.0, 1.0, 1.0),
            uv=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
            elevations_m=(10.0, 14.0, 12.0),
            object_ids=("terrain:0", "terrain:1", "terrain:2"),
            class_ids=(4, -1, 4),
            categorical_flags=(True, False, True),
        ),
        tiles=(TerrainTile("tile:near", 0, 3, bounds),),
        layer_order=1,
        material=MaterialParameters("terrain-material", blend_mode="alpha"),
        bounds=bounds,
        geometry_version="terrain-geometry-v9",
        material_version="terrain-material-v5",
        frame_generation=generation,
    )
    return RenderPlanBundle(
        generation=generation,
        frame=create_minimal_test_frame(generation),
        plans=(SceneRenderPlan("terrain", 1, generation, (terrain,)),),
        resources=ResourcePlan(frame_generation=generation),
        picking=PickingPlan(generation),
        interaction=InteractionPlan(generation),
    )


def test_threejs_payload_uses_the_real_typed_terrain_path() -> None:
    prepared = ThreeJSRendererBackend.prepare_celestial_plan_for_bridge(
        _bundle()
    )
    payload = prepared.manifest
    encoded = json.dumps(thaw_json_mapping(payload))
    terrain = next(
        primitive
        for primitive in payload["primitives"]
        if primitive["kind"] == "terrain_mesh"
    )

    assert '"count"' not in encoded
    assert "14.0" not in encoded
    assert (
        terrain["buffers"]["positions"]["resource_id"]
        == "terrain-near:positions"
    )
    assert terrain["buffers"]["positions"]["cadence"] == "static"
    assert terrain["buffers"]["class_ids"]["dtype"] == "uint32"
    assert terrain["buffers"]["categorical_flags"]["dtype"] == "uint8"
    assert terrain["tiles"][0]["tile_id"] == "tile:near"
    assert terrain["geometry_version"] == "terrain-geometry-v9"
    assert terrain["material_version"] == "terrain-material-v5"
    assert {resource.resource_id for resource in prepared.resources} >= {
        "terrain-near:positions",
        "terrain-near:indices",
        "terrain-near:normals",
        "terrain-near:colors",
    }


def test_prepared_resources_register_once_and_frames_send_only_deltas() -> (
    None
):
    prepared = ThreeJSRendererBackend.prepare_celestial_plan_for_bridge(
        _bundle()
    )
    sent_messages = []
    bridge = ThreeJSBridge(outbound_handler=sent_messages.append)
    bridge.start({"width": 1280, "height": 720, "dpr": 1.0})

    first = bridge.submit_prepared_frame(prepared)
    resource_messages = [
        message
        for message in sent_messages
        if message["op"] == OP_REGISTER_RESOURCE
    ]
    assert len(resource_messages) == len(prepared.resources)
    assert first["payload"]["mode"] == "full"
    assert "14.0" not in json.dumps(thaw_json_mapping(first))

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
    assert second["payload"]["mode"] == "delta"
    assert second["payload"]["patch"] == {}
    assert len(
        [
            message
            for message in sent_messages
            if message["op"] == OP_REGISTER_RESOURCE
        ]
    ) == len(resource_messages)
    bridge.close()
