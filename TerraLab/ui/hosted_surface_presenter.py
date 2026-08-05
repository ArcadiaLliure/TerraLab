"""Qt presenter for a renderer-owned hosted surface.

Unlike :mod:`frame_presenter`, this presenter never allocates shared pixels or
constructs a ``QImage``.  It submits its neutral host target directly and
treats the backend output as presentation completion.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QWidget

from TerraLab.adapters.qt.threejs_host import ThreeJSWebEngineHostPresenter
from TerraLab.application.render_controller import SceneRenderController
from TerraLab.core.rendering_contracts.contracts import (
    HostedSurfaceOutput,
    PickRequest,
    PickResult,
    RenderFailure,
    RenderOutput,
)
from TerraLab.runtime.supervisor import RuntimeSupervisor
from TerraLab.scene.contracts import JSONValue, SceneFrame
from TerraLab.view.pyqt.presentation import QtRenderOutputRouter


logger = logging.getLogger(__name__)


class HostedSurfacePresenter(ThreeJSWebEngineHostPresenter):
    """Present Three.js acknowledgements without rasterizing its surface."""

    frame_presented = pyqtSignal(int, float)
    pick_result = pyqtSignal(object)

    def __init__(
        self,
        runtime: RuntimeSupervisor,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent=parent)
        self._runtime = runtime
        self._scene_generation = 0
        self._displayed_generation = -1
        self._last_submitted_generation = -1
        self._pick_generations: dict[str, int] = {}
        self._last_frame_metadata: dict[str, JSONValue] = {}
        self._closed = False
        self._pending_scene: SceneFrame | None = None
        self._pending_outputs: dict[int, HostedSurfaceOutput] = {}
        self._submitted_at: dict[int, float] = {}
        self._pick_sequence = 0
        self._output_router = QtRenderOutputRouter(
            hosted_surface=self._present_hosted_surface_output,
        )
        backend = runtime.render_route.create_backend()
        bind_host_bridge = getattr(backend, "bind_host_bridge", None)
        if not callable(bind_host_bridge):
            raise TypeError(
                "Hosted surface backend must bind the WebEngine bridge"
            )
        bind_host_bridge(self.bridge)
        self._render_controller = SceneRenderController(
            backend,
            frame_ready=self.present_output,
            pick_ready=self._on_backend_pick,
            backend_failed=self._on_backend_failure,
        )
        self.webgl_ready.connect(self._submit_pending_scene)
        self.bridge_message_received.connect(self._on_webgl_bridge_message)

    @property
    def last_frame_metadata(self) -> dict[str, JSONValue]:
        return dict(self._last_frame_metadata)

    def submit(self, frame: SceneFrame) -> None:
        """Submit the renderer-neutral frame once the live host is ready."""

        if self._closed:
            return
        self._scene_generation = int(frame.generation)
        self._pending_scene = frame
        self._submit_pending_scene()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._render_controller.close()
        self._pending_outputs.clear()
        self._submitted_at.clear()
        self.close_surface()

    def request_pick(
        self,
        x: float,
        y: float,
        radius: float = 20.0,
        *,
        purpose: str = "select",
    ) -> None:
        if self._closed or self._last_submitted_generation < 0:
            return
        self._pick_sequence += 1
        generation = self._last_submitted_generation
        request_id = f"pick:{generation}:{self._pick_sequence}"
        self._pick_generations[request_id] = generation
        self._render_controller.request_pick(
            PickRequest(
                generation=generation,
                request_id=request_id,
                x=float(x),
                y=float(y),
                radius=max(0.0, float(radius)),
                purpose=str(purpose or "select"),
            )
        )

    def request_interaction(
        self,
        action: str,
        x: float = 0.0,
        y: float = 0.0,
        *,
        force_add: bool = False,
        additive_select: bool = False,
        allow_when_disabled: bool = False,
    ) -> None:
        if self._closed or self._last_submitted_generation < 0:
            return
        self._pick_sequence += 1
        generation = self._last_submitted_generation
        request_id = f"interaction:{generation}:{self._pick_sequence}"
        self._pick_generations[request_id] = generation
        self._render_controller.request_pick(
            PickRequest(
                generation=generation,
                request_id=request_id,
                x=float(x),
                y=float(y),
                purpose="interaction",
                action=str(action),
                options={
                    "force_add": bool(force_add),
                    "additive_select": bool(additive_select),
                    "allow_when_disabled": bool(allow_when_disabled),
                },
            )
        )

    def present_output(self, output: RenderOutput) -> None:
        self._output_router.present(output)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)

    def _submit_pending_scene(self) -> None:
        frame = self._pending_scene
        if self._closed or frame is None or not self.bridge.is_ready:
            return
        generation = int(frame.generation)
        try:
            self._submitted_at[generation] = time.perf_counter()
            self._render_controller.submit(frame, self.get_target())
        except Exception as exc:
            self._submitted_at.pop(generation, None)
            # ``webgl_ready`` can invoke this slot directly, outside
            # AstroCanvas' timer callback.  Keep that failure visible too.
            logger.exception("Unable to submit a resolved frame to Three.js")
            self.webgl_error.emit(f"Unable to submit Three.js scene: {exc}")
            return
        self._last_submitted_generation = int(frame.generation)

    def _on_backend_pick(self, result: PickResult) -> None:
        expected = self._pick_generations.pop(result.request_id, None)
        if expected != result.generation:
            return
        self.pick_result.emit(result)

    def _on_backend_failure(self, failure: RenderFailure) -> None:
        context = f" [{failure.code}]" if failure.code else ""
        request = (
            f" request={failure.request_id}" if failure.request_id else ""
        )
        self.webgl_error.emit(
            f"Three.js {failure.operation}{context}{request}: {failure.message}"
        )

    def _on_webgl_bridge_message(self, message: object) -> None:
        if self._closed or not isinstance(message, Mapping):
            return
        if not self.is_rendered_ack(message):
            return
        generation = message.get("gen")
        if not isinstance(generation, int):
            return
        output = self._pending_outputs.pop(generation, None)
        started = self._submitted_at.pop(generation, None)
        if output is None or generation <= self._displayed_generation:
            return
        self._displayed_generation = generation
        self._last_frame_metadata = dict(output.metadata)
        elapsed_ms = (
            0.0
            if started is None
            else (time.perf_counter() - started) * 1_000.0
        )
        self.frame_presented.emit(generation, elapsed_ms)

    def _present_hosted_surface_output(self, output: RenderOutput) -> None:
        if not isinstance(output, HostedSurfaceOutput):
            raise TypeError(
                "HostedSurfacePresenter only accepts hosted output"
            )
        if output.surface_id != self.surface_id:
            raise ValueError(
                f"Hosted output targets {output.surface_id!r}, not {self.surface_id!r}"
            )
        if output.generation <= self._displayed_generation:
            return
        # ``HostedSurfaceOutput`` means the bridge accepted a manifest, not
        # that Chromium has painted it.  Keep the output until the host sends
        # its rendered ACK so ``frame_presented`` retains its pixel-visible
        # contract.
        self._pending_outputs[output.generation] = output


__all__ = ("HostedSurfacePresenter",)
