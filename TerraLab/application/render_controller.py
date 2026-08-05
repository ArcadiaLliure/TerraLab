"""Application controller for one renderer-neutral scene presentation."""

from __future__ import annotations

from collections.abc import Callable

from TerraLab.core.rendering_contracts.contracts import (
    PickRequest,
    PickResult,
    RenderFailure,
    RenderOutput,
    RenderOutputPort,
    RenderTarget,
    RendererBackend,
)
from TerraLab.core.rendering_contracts.plans import RenderPlanBundle
from TerraLab.application.render_planning import SceneRenderPlanner
from TerraLab.scene.contracts import SceneFrame


class SceneRenderController(RenderOutputPort):
    """Resolve a frame once and submit its bundle to the selected backend.

    The controller is independent of QPainter, Three.js, Qt and process
    transport.  Presenters supply their neutral target and only display the
    output published through this port.
    """

    def __init__(
        self,
        backend: RendererBackend,
        *,
        frame_ready: Callable[[RenderOutput], None],
        pick_ready: Callable[[PickResult], None],
        backend_failed: Callable[[RenderFailure], None],
    ) -> None:
        self._backend = backend
        self._planner = SceneRenderPlanner()
        self._frame_ready_callback = frame_ready
        self._pick_ready_callback = pick_ready
        self._backend_failed_callback = backend_failed
        self._last_plan: RenderPlanBundle | None = None
        self._closed = False
        self._backend.start(self)

    def submit(self, frame: SceneFrame, target: RenderTarget) -> None:
        if self._closed:
            return
        plan = self._planner.build(frame)
        self._last_plan = plan
        self._backend.render(plan, target)

    def request_pick(self, request: PickRequest) -> None:
        if self._closed:
            return
        if bool(getattr(self._backend, "requires_host_pick", False)):
            self._backend.request_pick(request)
            return
        plan = self._last_plan
        if plan is None or plan.generation != request.generation:
            self.backend_failed(
                RenderFailure(
                    backend_id=self._backend.backend_id,
                    operation="pick_request",
                    message="No matching resolved generation for pick",
                    generation=request.generation,
                    request_id=request.request_id,
                    code="stale_or_unrendered_generation",
                )
            )
            return
        try:
            self.pick_ready(self._planner.process_pick_request(request, plan))
        except (KeyError, TypeError, ValueError) as exc:
            self.backend_failed(
                RenderFailure(
                    backend_id=self._backend.backend_id,
                    operation="pick_request",
                    message=str(exc),
                    generation=request.generation,
                    request_id=request.request_id,
                    code="invalid_resolved_pick",
                )
            )

    def frame_ready(self, output: RenderOutput) -> None:
        self._frame_ready_callback(output)

    def pick_ready(self, result: PickResult) -> None:
        self._pick_ready_callback(result)

    def backend_failed(self, failure: RenderFailure) -> None:
        self._backend_failed_callback(failure)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._planner.close()
        finally:
            self._last_plan = None
            self._backend.close()


__all__ = ("SceneRenderController",)
