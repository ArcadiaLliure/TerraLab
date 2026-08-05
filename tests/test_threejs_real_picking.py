"""Conformance tests for renderer-observed Three.js picking."""

from __future__ import annotations

from pathlib import Path

import pytest

from TerraLab.core.rendering_contracts.contracts import (
    HostedSurfaceTarget,
    PickRequest,
)
from TerraLab.application.render_planning import SceneRenderPlanner
from TerraLab.render.conformance import create_minimal_test_frame
from TerraLab.render.threejs.backend import ThreeJSRendererBackend
from TerraLab.render.threejs.bridge import ThreeJSBridge
from TerraLab.render.threejs.protocol import (
    OP_ERROR,
    OP_PICK_REQUEST,
    OP_PICK_RESULT,
    PROTOCOL_VERSION,
    parse_bridge_message,
)


class _Port:
    def __init__(self) -> None:
        self.frames = []
        self.picks = []
        self.failures = []

    def frame_ready(self, output) -> None:
        self.frames.append(output)

    def pick_ready(self, result) -> None:
        self.picks.append(result)

    def backend_failed(self, failure) -> None:
        self.failures.append(failure)


def _host_message(op: str, generation: int, payload: dict) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "op": op,
        "gen": generation,
        "seq": 1,
        "payload": payload,
    }


def _rendered_backend(generation: int = 11):
    sent: list[object] = []
    bridge = ThreeJSBridge(outbound_handler=sent.append)
    port = _Port()
    backend = ThreeJSRendererBackend(bridge)
    plan = SceneRenderPlanner().build(create_minimal_test_frame(generation))
    backend.start(port)
    backend.render(plan, HostedSurfaceTarget("threejs-test", 320, 200))
    return backend, bridge, plan, port, sent


def test_threejs_pick_is_published_only_after_matching_host_result() -> None:
    backend, bridge, plan, port, sent = _rendered_backend()
    request = PickRequest(plan.generation, "pick:11:1", 100.0, 80.0, 9.0)

    assert backend.request_pick(request) is None
    message = sent[-1]
    assert message["op"] == OP_PICK_REQUEST
    assert message["payload"]["radius"] == 9.0
    assert not port.picks

    bridge.receive_inbound(
        _host_message(
            OP_PICK_RESULT,
            plan.generation,
            {
                "request_id": request.request_id,
                "hit": True,
                "object_id": "star:42",
                "object_kind": "star",
                "distance": 1.5,
                "world_point": None,
                "surface_coordinates": None,
                "metadata": {"catalog_index": 42, "magnitude": 3.2},
                "key": "42",
                "name": "Gaia #42",
                "alt": 12.5,
                "az": 123.0,
                "magnitude": 3.2,
            },
        )
    )

    assert len(port.picks) == 1
    result = port.picks[0]
    assert result.hit and result.object_id == "star:42"
    assert result.object_kind == "star" and result.distance == 1.5
    assert result.metadata["catalog_index"] == 42
    assert result.payload["purpose"] == "select"
    backend.close()


def test_threejs_discards_stale_host_pick_results() -> None:
    backend, bridge, plan, port, _sent = _rendered_backend(12)
    request = PickRequest(plan.generation, "pick:12:1", 50.0, 50.0)
    backend.request_pick(request)
    next_plan = SceneRenderPlanner().build(create_minimal_test_frame(13))
    backend.render(next_plan, HostedSurfaceTarget("threejs-test", 320, 200))

    bridge.receive_inbound(
        _host_message(
            OP_PICK_RESULT,
            plan.generation,
            {
                "request_id": request.request_id,
                "hit": False,
                "object_id": None,
                "object_kind": None,
                "distance": None,
                "world_point": None,
                "surface_coordinates": None,
                "metadata": {},
            },
        )
    )

    assert not port.picks
    backend.close()


def test_threejs_host_errors_reach_the_output_port_with_context() -> None:
    backend, bridge, plan, port, _sent = _rendered_backend(14)
    bridge.receive_inbound(
        _host_message(
            OP_ERROR,
            plan.generation,
            {
                "operation": "pick_request",
                "request_id": "pick:14:1",
                "code": "stale_pick_request",
                "message": "Pick request generation is stale",
            },
        )
    )

    failure = port.failures[-1]
    assert failure.backend_id == "threejs"
    assert failure.generation == plan.generation
    assert failure.request_id == "pick:14:1"
    assert failure.code == "stale_pick_request"
    backend.close()


def test_pick_result_schema_rejects_synthetic_incomplete_hits() -> None:
    with pytest.raises(ValueError, match="object_kind"):
        parse_bridge_message(
            _host_message(
                OP_PICK_RESULT,
                1,
                {
                    "request_id": "invalid",
                    "hit": True,
                    "object_id": "star:1",
                    "distance": 1.0,
                },
            )
        )


def test_pick_result_schema_rejects_incomplete_host_misses() -> None:
    with pytest.raises(ValueError, match="null object_id"):
        parse_bridge_message(
            _host_message(
                OP_PICK_RESULT,
                1,
                {"request_id": "invalid-miss", "hit": False},
            )
        )


def test_host_contains_real_visual_pick_paths_only() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "TerraLab"
        / "render"
        / "threejs"
        / "assets"
        / "threejs_runner.js"
    ).read_text(encoding="utf-8")

    for token in (
        "Raycaster",
        "performPick",
        "terrainCandidate",
        "starCandidate",
        "deep_sky_batch",
        "screen_lines",
        "scope",
        "op: 'pick_result'",
    ):
        assert token in source
    assert "PickIndex" not in source
    assert "Skyfield" not in source
