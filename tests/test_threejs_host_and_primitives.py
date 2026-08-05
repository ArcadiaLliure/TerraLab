"""Phase 19 test suite for Three.js host, bridge, protocol, and basic primitives."""

from __future__ import annotations

from array import array
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import urlopen
import pytest

from TerraLab.adapters.qt.threejs_host import (
    ThreeJSLoopbackAssetServer,
    ThreeJSWebEngineHostPresenter,
)
from TerraLab.core.rendering_contracts.contracts import (
    HostedSurfaceOutput,
    PresenterIncompatibleError,
    RenderBackendLifecycleError,
    RenderCapability,
    RenderTargetKind,
)
from TerraLab.bootstrap.composition import (
    build_render_backend,
    create_backend_registry,
)
from TerraLab.bootstrap.settings import resolve_render_settings
from TerraLab.render.threejs.backend import ThreeJSRendererBackend
from TerraLab.render.threejs.bridge import ResourceRegistry, ThreeJSBridge
from TerraLab.render.threejs.protocol import (
    OP_ACK,
    OP_CLOSE,
    OP_PICK_REQUEST,
    OP_READY,
    OP_REGISTER_RESOURCE,
    OP_RESTART,
    OP_RESIZE,
    OP_START,
    OP_SUBMIT,
    OP_VISIBILITY,
    PROTOCOL_VERSION,
    BridgeVisibility,
    build_bridge_message,
    BinaryResource,
    parse_bridge_message,
)


class _DummyPort:
    def __init__(self) -> None:
        self.outputs = []
        self.picks = []
        self.failures = []

    def frame_ready(self, output: HostedSurfaceOutput) -> None:
        self.outputs.append(output)

    def pick_ready(self, result) -> None:
        self.picks.append(result)

    def backend_failed(self, failure) -> None:
        self.failures.append(failure)


def test_protocol_message_building_and_parsing() -> None:
    msg = build_bridge_message(
        OP_START,
        {"viewport": {"width": 800, "height": 600}},
        generation=1,
        seq=5,
    )
    assert msg["v"] == PROTOCOL_VERSION
    assert msg["op"] == OP_START
    assert msg["gen"] == 1
    assert msg["seq"] == 5

    parsed = parse_bridge_message(msg)
    assert parsed["op"] == OP_START
    assert parsed["payload"]["viewport"]["width"] == 800

    with pytest.raises(ValueError, match="not allowed"):
        build_bridge_message("invalid_op", {})

    with pytest.raises(
        ValueError, match="Unsupported bridge protocol version"
    ):
        parse_bridge_message({"v": 99, "op": OP_START, "payload": {}})


def _binary_resource(version: str = "v1") -> BinaryResource:
    values = array("f", (1.0, 2.0, 3.0))
    return BinaryResource(
        resource_id="positions",
        version=version,
        kind="positions",
        dtype="float32",
        components=3,
        element_count=1,
        cadence="static",
        data=memoryview(values),
        _owner=values,
    )


def test_resource_registry_versioning_and_disposal(tmp_path: Path) -> None:
    registry = ResourceRegistry(tmp_path)
    assert not registry.is_registered("positions")

    first = registry.register(_binary_resource())
    assert first.operation == "register"
    assert first.descriptor.uri.startswith("resources/")
    assert registry.path_for_uri(first.descriptor.uri) is not None
    assert registry.path_for_uri("file:///C:/untrusted.bin") is None
    assert registry.is_registered("positions", version="v1")
    assert registry.register(_binary_resource()).operation == "reuse"

    updated = registry.register(_binary_resource("v2"))
    assert updated.operation == "update"
    assert registry.acknowledge_ready("positions", "v2")
    assert registry.invalidate("positions") is not None
    assert not registry.is_registered("positions")
    assert registry.dispose("positions")
    assert not registry.dispose("positions")


def test_loopback_host_serves_assets_and_registered_buffers(
    tmp_path: Path,
) -> None:
    registry = ResourceRegistry(tmp_path)
    descriptor = registry.register(_binary_resource()).descriptor
    assets_dir = (
        Path(__file__).resolve().parents[1]
        / "TerraLab"
        / "render"
        / "threejs"
        / "assets"
    )
    channel_script = ThreeJSWebEngineHostPresenter._load_qwebchannel_script()
    server = ThreeJSLoopbackAssetServer(
        registry,
        assets_dir,
        channel_script,
    )
    server.start()
    try:
        with urlopen(server.host_url, timeout=5) as response:
            assert response.headers.get_content_type() == "text/html"
            assert b"canvas-container" in response.read()
        with urlopen(urljoin(server.host_url, descriptor.uri), timeout=5) as response:
            assert response.headers.get_content_type() == "application/octet-stream"
            assert response.read() == _binary_resource().data.tobytes()
        with urlopen(urljoin(server.host_url, "qwebchannel.js"), timeout=5) as response:
            assert response.headers.get_content_type() == "application/javascript"
            assert b"QWebChannel" in response.read()
    finally:
        server.close()
        registry.clear()


def test_threejs_bridge_lifecycle_and_messages() -> None:
    sent_messages = []
    bridge = ThreeJSBridge(outbound_handler=sent_messages.append)
    assert not bridge.is_started

    with pytest.raises(RenderBackendLifecycleError):
        bridge.submit_frame(1, {})

    start_msg = bridge.start({"width": 1024, "height": 768, "dpr": 2.0})
    assert bridge.is_started
    assert start_msg["op"] == OP_START

    frame_msg = bridge.submit_frame(1, {"primitives": []})
    assert frame_msg["op"] == OP_SUBMIT
    assert frame_msg["gen"] == 1

    pick_msg = bridge.request_pick(1, "pick_1", 100.0, 200.0)
    assert pick_msg["op"] == OP_PICK_REQUEST

    registered = bridge.register_resource(_binary_resource())
    assert registered.operation == "register"
    assert registered.message is not None
    assert registered.message["op"] == OP_REGISTER_RESOURCE
    assert bridge.register_resource(_binary_resource()).operation == "reuse"
    updated = bridge.register_resource(_binary_resource("v2"))
    assert updated.operation == "update"

    resize_msg = bridge.resize({"width": 1280, "height": 720, "dpr": 1.5})
    assert resize_msg["op"] == OP_RESIZE
    visibility_msg = bridge.set_visibility(
        BridgeVisibility(visible=False, suspended=True)
    )
    assert visibility_msg["op"] == OP_VISIBILITY

    ack_inbound = {
        "v": PROTOCOL_VERSION,
        "op": OP_ACK,
        "gen": 1,
        "seq": 2,
        "payload": {"status": "ok"},
    }
    bridge.receive_inbound(ack_inbound)
    assert bridge.get_last_ack() == ack_inbound

    bridge.receive_inbound(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_READY,
            "gen": 0,
            "seq": 3,
            "payload": {"status": "channel_ready"},
        }
    )
    assert bridge.is_channel_ready
    restart_msg = bridge.restart()
    assert restart_msg["op"] == OP_RESTART

    bridge.close()
    assert bridge.is_closed
    assert sent_messages[-1]["op"] == OP_CLOSE
    with pytest.raises(RenderBackendLifecycleError):
        bridge.start({"width": 100, "height": 100})


def test_binary_resources_are_reused_delta_submissions_and_recovered() -> None:
    sent_messages = []
    bridge = ThreeJSBridge(outbound_handler=sent_messages.append)
    bridge.start({"width": 640, "height": 360, "dpr": 1.0})

    resource = _binary_resource()
    assert bridge.register_resource(resource).operation == "register"
    first_frame = bridge.submit_frame(1, {"primitives": []})
    second_frame = bridge.submit_frame(2, {"primitives": []})
    assert first_frame["payload"]["mode"] == "full"
    # The host may still be fetching the first frame's buffers.  Until a
    # render ACK confirms a retained base, every replacement is self-contained.
    assert second_frame["payload"]["mode"] == "full"

    bridge.receive_inbound(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_ACK,
            "gen": 2,
            "seq": 2,
            "payload": {"operation": "render", "rendered": True},
        }
    )
    third_frame = bridge.submit_frame(
        3, {"primitives": [], "terrain_view": {"zoom": 2.0}}
    )
    assert third_frame["payload"]["mode"] == "delta"
    assert third_frame["payload"]["base_generation"] == 2
    assert third_frame["payload"]["patch"] == {
        "terrain_view": {"zoom": 2.0}
    }

    # A second frame sent before generation 3 is acknowledged cannot use 3
    # as a base, because the runner may still be loading its new resources.
    fourth_frame = bridge.submit_frame(4, {"primitives": []})
    assert fourth_frame["payload"]["mode"] == "full"
    assert bridge.register_resource(resource).operation == "reuse"

    bridge.receive_inbound(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_ACK,
            "gen": 0,
            "seq": 5,
            "payload": {
                "operation": "resource_ready",
                "resource_id": "positions",
                "version": "v1",
            },
        }
    )
    bridge.restart()
    bridge.receive_inbound(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_READY,
            "gen": 0,
            "seq": 6,
            "payload": {"status": "renderer_ready"},
        }
    )
    assert [message["op"] for message in sent_messages].count(
        OP_REGISTER_RESOURCE
    ) == 2
    assert bridge.invalidate_resource("positions") is not None
    bridge.dispose_resource("positions")
    assert not bridge.resources.is_registered("positions")
    bridge.close()


def test_resync_and_renderer_restart_force_the_next_frame_to_be_full() -> None:
    bridge = ThreeJSBridge(outbound_handler=lambda _message: None)
    bridge.start({"width": 640, "height": 360, "dpr": 1.0})
    bridge.submit_frame(1, {"primitives": []})
    bridge.receive_inbound(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_ACK,
            "gen": 1,
            "seq": 1,
            "payload": {"operation": "render", "rendered": True},
        }
    )
    assert bridge.submit_frame(2, {"primitives": []})["payload"][
        "mode"
    ] == "delta"

    bridge.receive_inbound(
        {
            "v": PROTOCOL_VERSION,
            "op": "error",
            "gen": 2,
            "seq": 2,
            "payload": {
                "code": "resync_required",
                "message": "base missing",
            },
        }
    )
    assert bridge.submit_frame(3, {"primitives": []})["payload"][
        "mode"
    ] == "full"

    bridge.receive_inbound(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_READY,
            "gen": 0,
            "seq": 3,
            "payload": {"status": "renderer_ready"},
        }
    )
    assert bridge.submit_frame(4, {"primitives": []})["payload"][
        "mode"
    ] == "full"
    bridge.close()


def test_local_assets_exist_and_no_cdn_required() -> None:
    assets_dir = (
        Path(__file__).resolve().parents[1]
        / "TerraLab"
        / "render"
        / "threejs"
        / "assets"
    )
    assert assets_dir.is_dir()
    assert (assets_dir / "three.min.js").is_file()
    assert (assets_dir / "threejs_runner.js").is_file()
    assert (assets_dir / "threejs_host.html").is_file()

    html_content = (assets_dir / "threejs_host.html").read_text(
        encoding="utf-8"
    )
    assert "https://" not in html_content
    assert "http://" not in html_content
    assert "connect-src 'self'" in html_content
    assert "connect-src 'none'" not in html_content
    assert "connect-src 'self' file:" not in html_content
    assert "three.min.js" in html_content
    assert 'src="qwebchannel.js"' in html_content

    three_source = (assets_dir / "three.min.js").read_text(encoding="utf-8")
    assert len(three_source) > 500_000
    assert "WebGLRenderer" in three_source

    runner_source = (assets_dir / "threejs_runner.js").read_text(
        encoding="utf-8"
    )
    assert ".userData.resourceKey" not in runner_source
    assert "._terralabResourceKey" in runner_source


def test_threejs_backend_registration_and_capability_rejection() -> None:
    registry = create_backend_registry()
    assert "threejs" in registry.available_backend_ids()

    # Hosted surface with no extra requirements works
    backend = registry.create(
        "threejs", target_kind=RenderTargetKind.HOSTED_SURFACE
    )
    assert isinstance(backend, ThreeJSRendererBackend)
    assert backend.backend_id == "threejs"

    # Incompatible target kind raises PresenterIncompatibleError
    with pytest.raises(
        PresenterIncompatibleError,
        match="does not support render target 'shared_raster'",
    ):
        registry.create("threejs", target_kind=RenderTargetKind.SHARED_RASTER)

    # Full app selection requiring RASTER_SHARED_FRAME fails with actionable message
    with pytest.raises(
        PresenterIncompatibleError, match="lacks required capabilities"
    ):
        registry.create(
            "threejs",
            target_kind=RenderTargetKind.HOSTED_SURFACE,
            required_capabilities=frozenset(
                {RenderCapability.RASTER_SHARED_FRAME}
            ),
        )

    assert {
        RenderCapability.HOSTED_SURFACE,
        RenderCapability.SKY_BACKGROUND,
        RenderCapability.STARS,
        RenderCapability.EPHEMERIS_BODIES,
        RenderCapability.DEEP_SKY,
        RenderCapability.GRID,
    }.issubset(backend.capabilities)
    assert isinstance(
        registry.create(
            "threejs",
            target_kind=RenderTargetKind.HOSTED_SURFACE,
            required_capabilities=frozenset({RenderCapability.STARS}),
        ),
        ThreeJSRendererBackend,
    )


def test_threejs_backend_has_no_diagnostic_submission_path() -> None:
    assert not hasattr(ThreeJSRendererBackend, "submit_diagnostic_frame")


def test_qpainter_remains_the_default_backend() -> None:
    settings = resolve_render_settings()
    assert settings.backend_id == "qpainter"

    backend = build_render_backend()
    assert backend.backend_id == "qpainter"
