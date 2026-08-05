"""Conformance checks for retained Three.js terrain resources."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from TerraLab.core.rendering_contracts.contracts import (
    RenderCapability,
    RenderTargetKind,
)
from TerraLab.bootstrap.composition import create_backend_registry
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
    TextureParameters,
    TextureResource,
)
from TerraLab.render.conformance import create_minimal_test_frame
from TerraLab.render.threejs.backend import ThreeJSRendererBackend
from TerraLab.render.threejs.bridge import ThreeJSBridge
from TerraLab.render.threejs.protocol import (
    OP_ACK,
    OP_REGISTER_RESOURCE,
    PROTOCOL_VERSION,
)


def _terrain_bundle(generation: int = 20) -> RenderPlanBundle:
    frame = create_minimal_test_frame(generation)
    bounds = Bounds((0.0, 0.0, 0.0), (100.0, 100.0, 20.0))
    texture = TextureParameters("terrain-rgb", "rgb-v3")
    terrain = TerrainMeshResource(
        primitive_id="terrain:near",
        vertices=(
            (0.0, 20.0, 10.0),
            (50.0, 20.0, 12.0),
            (0.0, 75.0, 11.0),
            (50.0, 20.0, 12.0),
            (100.0, 75.0, 20.0),
            (0.0, 75.0, 11.0),
        ),
        view_vertices=(
            (-1.0, 0.1, 10.0),
            (0.0, 0.1, 12.0),
            (-1.0, 0.8, 11.0),
            (0.0, 0.1, 12.0),
            (1.0, 0.8, 20.0),
            (-1.0, 0.8, 11.0),
        ),
        indices=(0, 1, 2, 3, 4, 5),
        normals=((0.0, 0.0, 1.0),) * 6,
        surface=TerrainSurfaceAttributes(
            colors=(
                (0.20, 0.30, 0.18, 1.0),
                (0.25, 0.36, 0.20, 1.0),
                (0.18, 0.28, 0.15, 1.0),
                (0.25, 0.36, 0.20, 1.0),
                (0.48, 0.44, 0.36, 1.0),
                (0.18, 0.28, 0.15, 1.0),
            ),
            alphas=(1.0,) * 6,
            uv=(
                (0.0, 0.0),
                (0.5, 0.0),
                (0.0, 1.0),
                (0.5, 0.0),
                (1.0, 1.0),
                (0.0, 1.0),
            ),
            elevations_m=(10.0, 12.0, 11.0, 12.0, 20.0, 11.0),
            object_ids=tuple(f"terrain:{index}" for index in range(6)),
            class_ids=(7, 7, 7, 7, 12, 7),
            categorical_flags=(False, False, False, False, True, False),
        ),
        tiles=(
            TerrainTile("terrain:near:0", 0, 3, bounds),
            TerrainTile("terrain:near:1", 3, 3, bounds),
        ),
        layer_order=1,
        material=MaterialParameters(
            "terrain-material",
            blend_mode="alpha",
            texture=texture,
        ),
        texture=texture,
        bounds=bounds,
        geometry_version="terrain-geometry-v9",
        material_version="terrain-material-v5",
        frame_generation=generation,
    )
    return RenderPlanBundle(
        generation=generation,
        frame=frame,
        plans=(SceneRenderPlan("terrain", 1, generation, (terrain,)),),
        resources=ResourcePlan(
            frame_generation=generation,
            textures=(
                TextureResource(
                    "terrain-rgb",
                    "file:///cpu-prepared-terrain.png",
                    "rgb-v3",
                    2048,
                    2048,
                    texture,
                ),
            ),
        ),
        picking=PickingPlan(generation),
        interaction=InteractionPlan(generation),
    )


def test_threejs_declares_implemented_terrain_capabilities() -> None:
    backend = ThreeJSRendererBackend()
    assert {
        RenderCapability.TERRAIN_GEOMETRY,
        RenderCapability.TERRAIN_MATERIALS,
    }.issubset(backend.capabilities)
    selected = create_backend_registry().create(
        "threejs",
        target_kind=RenderTargetKind.HOSTED_SURFACE,
        required_capabilities=frozenset(
            {
                RenderCapability.HOSTED_SURFACE,
                RenderCapability.TERRAIN_GEOMETRY,
                RenderCapability.TERRAIN_MATERIALS,
            }
        ),
    )
    assert isinstance(selected, ThreeJSRendererBackend)


def test_threejs_terrain_transport_is_typed_tiled_and_resource_backed() -> (
    None
):
    prepared = ThreeJSRendererBackend.prepare_celestial_plan_for_bridge(
        _terrain_bundle()
    )
    terrain = next(
        item
        for item in prepared.manifest["primitives"]
        if item["kind"] == "terrain_mesh"
    )

    assert terrain["coordinate_space"] == "terrain_direction"
    assert (
        terrain["texture_resource"]["source"]
        == "file:///cpu-prepared-terrain.png"
    )
    assert len(terrain["tiles"]) == 2
    assert terrain["buffers"]["positions"]["cadence"] == "static"
    assert terrain["buffers"]["normals"]["components"] == 3
    assert terrain["buffers"]["class_ids"]["dtype"] == "uint32"
    assert terrain["buffers"]["categorical_flags"]["dtype"] == "uint8"
    assert {resource.resource_id for resource in prepared.resources} >= {
        "terrain:near:positions",
        "terrain:near:indices",
        "terrain:near:normals",
        "terrain:near:colors",
        "terrain:near:class_ids",
    }


def test_threejs_reuses_terrain_gpu_resources_for_camera_only_updates() -> (
    None
):
    first_bundle = _terrain_bundle(20)
    first = ThreeJSRendererBackend.prepare_celestial_plan_for_bridge(
        first_bundle
    )
    moved_frame = replace(
        first_bundle.frame,
        generation=21,
        camera=replace(
            first_bundle.frame.camera,
            azimuth=first_bundle.frame.camera.azimuth + 15.0,
            elevation=first_bundle.frame.camera.elevation + 5.0,
        ),
    )
    terrain = replace(
        first_bundle.plans[0].primitives[0],
        frame_generation=21,
    )
    moved_bundle = replace(
        first_bundle,
        generation=21,
        frame=moved_frame,
        plans=(SceneRenderPlan("terrain", 1, 21, (terrain,)),),
        resources=replace(first_bundle.resources, frame_generation=21),
        picking=PickingPlan(21),
        interaction=InteractionPlan(21),
    )
    second = ThreeJSRendererBackend.prepare_celestial_plan_for_bridge(
        moved_bundle
    )
    sent: list[object] = []
    bridge = ThreeJSBridge(outbound_handler=sent.append)
    bridge.start({"width": 320, "height": 200, "dpr": 1.0})

    bridge.submit_prepared_frame(first)
    registrations = [
        message for message in sent if message["op"] == OP_REGISTER_RESOURCE
    ]
    bridge.receive_inbound(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_ACK,
            "gen": first.generation,
            "seq": 1,
            "payload": {"operation": "render", "rendered": True},
        }
    )
    update = bridge.submit_prepared_frame(second)

    assert update["payload"]["mode"] == "delta"
    assert (
        update["payload"]["patch"]["terrain_view"]["elevation_deg"]
        == moved_frame.camera.elevation
    )
    assert (
        update["payload"]["patch"]["terrain_view"]["azimuth_deg"]
        == moved_frame.camera.azimuth
    )
    assert len(
        [message for message in sent if message["op"] == OP_REGISTER_RESOURCE]
    ) == len(registrations)
    bridge.close()


def test_threejs_host_retains_and_releases_tiled_terrain_resources() -> None:
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
        "terrainGeometryCache",
        "terrainTileCache",
        "replaceIndex",
        "class_ids",
        "categorical_flags",
        "TextureLoader",
        "releaseTerrainPrimitive",
        "advanceTerrainTransitions",
    ):
        assert token in source
    assert "GeoTIFF" not in source
    assert "Raycaster" in source
