"""End-to-end checks for the neutral planning and target contract."""

from __future__ import annotations

import pytest

from TerraLab.application.controller import ApplicationController
from TerraLab.application.render_controller import SceneRenderController
from TerraLab.application.render_planning import SceneRenderPlanner
from TerraLab.bootstrap.composition import build_render_backend
from TerraLab.core.rendering_contracts.contracts import (
    BackendNotFoundError,
    CommandStreamOutput,
    CommandStreamTarget,
    HostedSurfaceOutput,
    HostedSurfaceTarget,
    PickRequest,
    PickResult,
    RasterFrameHandle,
    RasterFrameOutput,
    RenderCapability,
    RenderOutput,
    RenderTarget,
    RenderTargetKind,
    SharedRasterTarget,
)
from TerraLab.core.rendering_contracts.plans import RenderPlanBundle
from TerraLab.scene.contracts import freeze_json_mapping
from TerraLab.view.pyqt.presentation import QtRenderOutputRouter
from TerraLab.view.pyqt.backend import QPainterRendererBackend


class _OutputPort:
    def frame_ready(self, output: RenderOutput) -> None:
        self.frames.append(output)

    def pick_ready(self, result: PickResult) -> None:
        self.picks.append(result)

    def backend_failed(self, failure) -> None:
        self.failures.append(failure)

    def __init__(self) -> None:
        self.frames: list[RenderOutput] = []
        self.picks: list[PickResult] = []
        self.failures: list[object] = []


class SimulatedBackend:
    """Small conformance backend; it proves no target requires Qt emulation."""

    backend_id = "simulated"
    capabilities = frozenset(
        {
            RenderCapability.RASTER_SHARED_FRAME,
            RenderCapability.HOSTED_SURFACE,
            RenderCapability.COMMAND_STREAM,
            RenderCapability.PICKING,
            RenderCapability.INTERACTION,
        }
    )
    target_kinds = frozenset(RenderTargetKind)

    def __init__(self) -> None:
        self.port: _OutputPort | None = None

    def start(self, output_port: _OutputPort) -> None:
        self.port = output_port

    def render(self, plan: RenderPlanBundle, target: RenderTarget) -> RenderOutput:
        if isinstance(target, SharedRasterTarget):
            output: RenderOutput = RasterFrameOutput(
                plan.generation,
                target.handle,
                0.0,
                freeze_json_mapping({"plan_count": len(plan.plans)}),
            )
        elif isinstance(target, HostedSurfaceTarget):
            output = HostedSurfaceOutput(
                plan.generation,
                target.surface_id,
                freeze_json_mapping({"plan_count": len(plan.plans)}),
            )
        else:
            output = CommandStreamOutput(
                plan.generation,
                target.stream_id,
                commands=(
                    freeze_json_mapping(
                        {"op": "render", "capabilities": len(plan.plans)}
                    ),
                ),
            )
        assert self.port is not None
        self.port.frame_ready(output)
        return output

    def request_pick(
        self, request: PickRequest, plan: RenderPlanBundle | None = None
    ) -> PickResult:
        result = PickResult(
            request.generation,
            request.request_id,
            freeze_json_mapping({"purpose": request.purpose, "kind": "none"}),
        )
        assert self.port is not None
        self.port.pick_ready(result)
        return result

    def close(self) -> None:
        self.port = None


def _plan() -> RenderPlanBundle:
    controller = ApplicationController()
    controller.set_observer(41.38, 2.17, 100.0)
    controller.set_camera(azimuth_deg=190.0, elevation_deg=35.0, zoom=1.5)
    controller.set_viewport(320, 200)
    frame = controller.build_scene_frame()
    return SceneRenderPlanner().build(frame)


def test_controller_frame_plan_and_simulated_backend_are_end_to_end() -> None:
    plan = _plan()
    backend = SimulatedBackend()
    port = _OutputPort()
    backend.start(port)
    handle = RasterFrameHandle(0, 320, 200, 1280, 1)
    raster = backend.render(
        plan,
        SharedRasterTarget(handle, memoryview(bytearray(handle.stride * handle.height))),
    )
    assert isinstance(raster, RasterFrameOutput)
    assert raster.generation == plan.generation
    assert raster.metadata["plan_count"] == len(plan.plans)

    pick = backend.request_pick(PickRequest(plan.generation, "pick-1", 1.0, 2.0))
    assert pick.payload["purpose"] == "select"
    backend.close()


def test_qpainter_paints_the_complete_typed_bundle_directly() -> None:
    backend = QPainterRendererBackend()
    port = _OutputPort()
    plan = _plan()
    handle = RasterFrameHandle(0, 320, 200, 1280, 1)
    backend.start(port)
    try:
        output = backend.render(
            plan,
            SharedRasterTarget(
                handle, memoryview(bytearray(handle.stride * handle.height))
            ),
        )
    finally:
        backend.close()

    assert isinstance(output, RasterFrameOutput)
    assert output.metadata["celestial_plan_native"] is True
    assert output.metadata["terrain_plan_native"] is True
    assert output.metadata["overlay_interaction_plan_native"] is True


def test_qpainter_pick_policy_is_resolved_by_the_shared_controller() -> None:
    backend = QPainterRendererBackend()
    results = []
    failures = []
    controller = SceneRenderController(
        backend,
        frame_ready=lambda _output: None,
        pick_ready=results.append,
        backend_failed=failures.append,
    )
    frame = _plan().frame
    handle = RasterFrameHandle(0, 320, 200, 1280, 1)
    try:
        controller.submit(
            frame,
            SharedRasterTarget(
                handle, memoryview(bytearray(handle.stride * handle.height))
            ),
        )
        controller.request_pick(
            PickRequest(frame.generation, "pick-controller", 8.0, 8.0)
        )
    finally:
        controller.close()

    assert not failures
    assert results[0].generation == frame.generation
    assert results[0].payload["purpose"] == "select"


@pytest.mark.parametrize(
    ("target", "output_type"),
    [
        (HostedSurfaceTarget("surface-1", 800, 600), HostedSurfaceOutput),
        (CommandStreamTarget("web-client-1"), CommandStreamOutput),
    ],
)
def test_hosted_surface_and_command_stream_conformance(target, output_type) -> None:
    backend = SimulatedBackend()
    port = _OutputPort()
    backend.start(port)
    output = backend.render(_plan(), target)
    assert isinstance(output, output_type)
    assert port.frames == [output]
    backend.close()


@pytest.mark.parametrize("backend_id", ["opengl", "vulkan"])
def test_uninstalled_backends_fail_without_qpainter_fallback(backend_id: str) -> None:
    with pytest.raises(BackendNotFoundError, match="Available backends: qpainter, recording, threejs"):
        build_render_backend(
            environment={"TERRALAB_RENDER_BACKEND": backend_id},
            target_kind=RenderTargetKind.COMMAND_STREAM,
        )


def test_presenter_routes_by_output_kind_without_raster_coercion() -> None:
    received: list[RenderOutput] = []
    router = QtRenderOutputRouter(
        raster=received.append,
        hosted_surface=received.append,
        command_stream=received.append,
    )
    handle = RasterFrameHandle(0, 2, 2, 8, 1)
    outputs: tuple[RenderOutput, ...] = (
        RasterFrameOutput(1, handle, 0.0),
        HostedSurfaceOutput(1, "surface-1"),
        CommandStreamOutput(1, "stream-1"),
    )
    for output in outputs:
        router.present(output)
    assert received == list(outputs)
