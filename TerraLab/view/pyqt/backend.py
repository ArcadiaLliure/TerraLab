"""QPainter implementation of the neutral renderer contract.

All Qt object creation is contained in this adapter.  Runtime hands it an
opaque shared-raster target and an already resolved render-plan bundle; it
never creates a ``QGuiApplication``, ``QImage`` or ``QPainter`` itself.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from collections.abc import Callable, Mapping
from typing import Protocol, cast

from PyQt5 import sip
from PyQt5.QtGui import QGuiApplication, QImage, QPainter

from TerraLab.application.ports.rendering import PresenterKind
from TerraLab.core.rendering_contracts.contracts import (
    PickRequest,
    PickResult,
    RasterFrameOutput,
    RenderBackendLifecycleError,
    RenderCapability,
    RenderOutput,
    RenderOutputPort,
    RenderTarget,
    RenderTargetKind,
    SharedRasterTarget,
)
from TerraLab.core.rendering_contracts.plans import RenderPlanBundle
from TerraLab.runtime.protocol import encode_scene_frame_v1
from TerraLab.scene.contracts import JSONValue, Viewport, freeze_json_mapping


class _CompatibilityRenderer(Protocol):
    """Private bridge until every capability has a plan-native painter."""

    def close(self) -> None: ...

    def render(
        self,
        painter: QPainter,
        width: int,
        height: int,
        payload: Mapping[str, JSONValue],
    ) -> Mapping[str, JSONValue]: ...

    def pick(self, x: float, y: float, radius: float) -> Mapping[str, object]: ...

    def pick_surface(self, x: float, y: float) -> Mapping[str, object]: ...

    def interact(
        self,
        x: float,
        y: float,
        action: str,
        *,
        options: Mapping[str, JSONValue] | None,
    ) -> Mapping[str, object]: ...


class QPainterRendererBackend:
    """QPainter adapter consuming the neutral target and render-plan contract."""

    backend_id = "qpainter"
    capabilities = frozenset(
        {
            RenderCapability.RASTER_SHARED_FRAME,
            RenderCapability.PICKING,
            RenderCapability.INTERACTION,
            RenderCapability.SKY_BACKGROUND,
        }
    )
    target_kinds = frozenset({RenderTargetKind.SHARED_RASTER})
    # Compatibility metadata only; composition now checks ``target_kinds``.
    presenter_kinds = frozenset({PresenterKind.SHARED_FRAME})

    def __init__(
        self,
        *,
        renderer_factory: Callable[[], _CompatibilityRenderer] | None = None,
    ) -> None:
        self._renderer_factory = renderer_factory
        self._renderer: _CompatibilityRenderer | None = None
        self._output: RenderOutputPort | None = None
        self._latest_plan: RenderPlanBundle | None = None
        self._closed = False
        self._qt_application: QGuiApplication | None = None

    def start(self, output_port: RenderOutputPort) -> None:
        if self._closed:
            raise RenderBackendLifecycleError("Cannot restart a closed backend")
        if self._renderer is not None:
            raise RenderBackendLifecycleError("QPainter backend is already started")
        # This is intentionally inside the selected view adapter, never runtime.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self._qt_application = QGuiApplication.instance() or QGuiApplication(
            [sys.argv[0]]
        )
        factory = self._renderer_factory
        if factory is None:
            from TerraLab.runtime.offscreen_renderer import OffscreenSceneRenderer

            factory = cast(Callable[[], _CompatibilityRenderer], OffscreenSceneRenderer)
        self._renderer = factory()
        self._output = output_port

    def render(self, plan: RenderPlanBundle, target: RenderTarget) -> RenderOutput:
        renderer = self._require_started()
        if not isinstance(target, SharedRasterTarget):
            raise TypeError("QPainter requires a SharedRasterTarget")
        started = time.perf_counter()
        image = None
        painter = None
        try:
            address = ctypes.addressof(ctypes.c_ubyte.from_buffer(target.pixels))
            image = QImage(
                sip.voidptr(address),
                target.handle.width,
                target.handle.height,
                target.handle.stride,
                QImage.Format_ARGB32_Premultiplied,
            )
            painter = QPainter(image)
            metadata = renderer.render(
                painter,
                target.handle.width,
                target.handle.height,
                encode_scene_frame_v1(plan.frame),
            )
        finally:
            if painter is not None and painter.isActive():
                painter.end()
            del painter
            del image
        self._latest_plan = plan
        return RasterFrameOutput(
            generation=plan.generation,
            handle=target.handle,
            render_ms=round((time.perf_counter() - started) * 1000.0, 3),
            metadata=freeze_json_mapping(metadata),
        )

    def request_pick(
        self,
        request: PickRequest,
        plan: RenderPlanBundle | None = None,
    ) -> PickResult:
        renderer = self._require_started()
        purpose = str(request.purpose or "select")
        if purpose == "hover":
            payload = renderer.pick_surface(request.x, request.y)
        elif purpose == "interaction":
            payload = renderer.interact(
                request.x,
                request.y,
                request.action,
                options=request.options,
            )
        else:
            payload = renderer.pick(request.x, request.y, request.radius)
        response = dict(payload)
        response["purpose"] = purpose
        result = PickResult(
            generation=request.generation,
            request_id=request.request_id,
            payload=freeze_json_mapping(response),
        )
        if self._output is not None:
            self._output.pick_ready(result)
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._latest_plan = None
        self._output = None
        renderer, self._renderer = self._renderer, None
        if renderer is not None:
            renderer.close()

    # Compatibility shim for extensions still calling the former adapter API.
    # The selected runtime path calls ``render(plan, target)`` exclusively.
    def render_to_qpainter(
        self,
        painter: object,
        viewport: Viewport,
    ) -> Mapping[str, JSONValue]:
        renderer = self._require_started()
        if self._latest_plan is None:
            raise RenderBackendLifecycleError("No render plan has been submitted")
        if not isinstance(painter, QPainter):
            raise TypeError("QPainter backend requires a QPainter target")
        return renderer.render(
            painter,
            int(viewport.width),
            int(viewport.height),
            encode_scene_frame_v1(self._latest_plan.frame),
        )

    def submit(self, frame) -> None:
        """Compatibility bridge for old direct adapter users.

        The production path creates a ``RenderPlanBundle`` in application
        before this backend is invoked.
        """

        from TerraLab.core.application.planning import SceneRenderPlanner

        self._require_started()
        self._latest_plan = SceneRenderPlanner().build(frame)

    def _require_started(self) -> _CompatibilityRenderer:
        if self._closed or self._renderer is None:
            raise RenderBackendLifecycleError("QPainter backend has not started")
        return self._renderer
