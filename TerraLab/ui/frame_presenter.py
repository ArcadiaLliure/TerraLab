"""QPainter presenter for a directly submitted neutral render bundle."""

from __future__ import annotations

import ctypes
import time
from collections import deque

from PyQt5 import sip
from PyQt5.QtCore import QEvent, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter
from PyQt5.QtWidgets import QWidget

from TerraLab.core.rendering_contracts.contracts import (
    PickRequest,
    PickResult,
    RasterFrameHandle,
    RasterFrameOutput,
    RenderFailure,
    RenderOutput,
    SharedRasterTarget,
)
from TerraLab.application.render_controller import SceneRenderController
from TerraLab.runtime.frame_pool import OwnedFramePool, first_free_slot
from TerraLab.runtime.supervisor import RuntimeSupervisor
from TerraLab.scene.contracts import SceneFrame
from TerraLab.view.pyqt.presentation import QtRenderOutputRouter


class SharedFramePresenter(QWidget):
    """Present QPainter output without serialising a ``SceneFrame`` again."""

    frame_presented = pyqtSignal(int, float)
    pick_result = pyqtSignal(object)

    def __init__(
        self, runtime: RuntimeSupervisor, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._pool: OwnedFramePool | None = None
        self._retired_pools: list[tuple[int, OwnedFramePool]] = []
        self._pool_generation = 0
        self._scene_generation = -1
        self._displayed_generation = -1
        self._current_slot: int | None = None
        self._current_pool_generation = -1
        self._pending_scene: SceneFrame | None = None
        self._pending_presentation: tuple[int, float] | None = None
        self._pick_sequence = 0
        self._pick_generations: dict[str, int] = {}
        self._last_frame_metadata: dict[str, object] = {}
        self._paint_times_ms: deque[float] = deque(maxlen=600)
        self._closed = False
        self._output_router = QtRenderOutputRouter(
            raster=self._present_raster_output,
        )
        backend = runtime.render_route.create_backend()
        self._render_controller = SceneRenderController(
            backend,
            frame_ready=self.present_output,
            pick_ready=self._on_backend_pick,
            backend_failed=self._on_backend_failure,
        )
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(40)
        self._resize_timer.timeout.connect(self._replace_pool)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: #000000;")

    @property
    def paint_times_ms(self) -> tuple[float, ...]:
        return tuple(self._paint_times_ms)

    @property
    def last_frame_metadata(self) -> dict[str, object]:
        return dict(self._last_frame_metadata)

    def submit(self, frame: SceneFrame) -> None:
        """Resolve and render the supplied frame in the UI-owned raster pool."""

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
        pools = [self._pool] if self._pool is not None else []
        pools.extend(pool for _generation, pool in self._retired_pools)
        self._pool = None
        self._retired_pools = []
        self._current_slot = None
        self._current_pool_generation = -1
        for pool in pools:
            pool.close()

    def request_pick(
        self,
        x: float,
        y: float,
        radius: float = 20.0,
        *,
        purpose: str = "select",
    ) -> None:
        if self._closed or self._scene_generation < 0:
            return
        self._pick_sequence += 1
        generation = self._scene_generation
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
        if self._closed or self._scene_generation < 0:
            return
        self._pick_sequence += 1
        generation = self._scene_generation
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
        self._resize_timer.start()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        started = time.perf_counter()
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(0, 0, 0))
            pool = self._pool_for_current_frame()
            if pool is not None and self._current_slot is not None:
                descriptor = pool.descriptor
                lease = pool.buffer(self._current_slot)
                try:
                    address = ctypes.addressof(
                        ctypes.c_ubyte.from_buffer(lease)
                    )
                    image = QImage(
                        sip.voidptr(address),
                        descriptor.width,
                        descriptor.height,
                        descriptor.stride,
                        QImage.Format_ARGB32_Premultiplied,
                    )
                    painter.drawImage(self.rect(), image)
                    del image
                finally:
                    lease.release()
        finally:
            painter.end()
            self._paint_times_ms.append(
                (time.perf_counter() - started) * 1000.0
            )
        presented, self._pending_presentation = (
            self._pending_presentation,
            None,
        )
        if presented is not None:
            self.frame_presented.emit(*presented)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.shutdown()
        super().closeEvent(event)

    def event(self, event: QEvent) -> bool:
        if event.type() == getattr(QEvent, "DevicePixelRatioChange", -1):
            self._resize_timer.start()
        return super().event(event)

    def _submit_pending_scene(self) -> None:
        frame = self._pending_scene
        if self._closed or frame is None:
            return
        if self._pool is None:
            self._replace_pool()
            return
        pool = self._pool
        assert pool is not None
        slot = first_free_slot(
            len(pool.descriptor.names),
            () if self._current_slot is None else (self._current_slot,),
            after=self._current_slot if self._current_slot is not None else -1,
        )
        if slot is None:
            return
        handle = RasterFrameHandle(
            slot=slot,
            width=pool.descriptor.width,
            height=pool.descriptor.height,
            stride=pool.descriptor.stride,
            pool_generation=self._pool_generation,
            pixel_format=pool.descriptor.pixel_format,
        )
        lease = pool.buffer(slot)
        try:
            self._render_controller.submit(
                frame, SharedRasterTarget(handle, lease)
            )
        finally:
            lease.release()

    def _present_raster_output(self, output: RenderOutput) -> None:
        if not isinstance(output, RasterFrameOutput):
            raise TypeError("SharedFramePresenter only accepts raster output")
        handle = output.handle
        if handle.pool_generation != self._pool_generation:
            return
        if output.generation <= self._displayed_generation:
            return
        self._displayed_generation = int(output.generation)
        self._last_frame_metadata = {
            "slot": handle.slot,
            "width": handle.width,
            "height": handle.height,
            "stride": handle.stride,
            "pool_generation": handle.pool_generation,
            "render_ms": output.render_ms,
            **dict(output.metadata),
        }
        self._release_current()
        self._current_slot = handle.slot
        self._current_pool_generation = handle.pool_generation
        retired, self._retired_pools = self._retired_pools, []
        for _generation, pool in retired:
            pool.close()
        self._pending_presentation = (int(output.generation), output.render_ms)
        self.update()

    def _on_backend_pick(self, result: PickResult) -> None:
        expected = self._pick_generations.pop(result.request_id, None)
        if expected == result.generation:
            self.pick_result.emit(result)

    def _on_backend_failure(self, failure: RenderFailure) -> None:
        self._last_frame_metadata["renderer_error"] = failure.message
        self._last_frame_metadata["renderer_error_code"] = failure.code or ""

    def _replace_pool(self) -> None:
        if self._closed or self.width() <= 0 or self.height() <= 0:
            return
        ratio = max(1.0, float(self.devicePixelRatioF()))
        width = max(1, int(round(self.width() * ratio)))
        height = max(1, int(round(self.height() * ratio)))
        if (
            self._pool is not None
            and self._pool.descriptor.width == width
            and self._pool.descriptor.height == height
        ):
            return
        if self._pool is not None:
            previous_generation = self._pool_generation
            if self._current_pool_generation == previous_generation:
                for _generation, retired in self._retired_pools:
                    retired.close()
                self._retired_pools = [(previous_generation, self._pool)]
            else:
                self._pool.close()
        self._pool = OwnedFramePool(width, height)
        self._pool_generation += 1
        self._submit_pending_scene()

    def _release_current(self) -> None:
        self._current_slot = None
        self._current_pool_generation = -1

    def _pool_for_current_frame(self) -> OwnedFramePool | None:
        if self._current_pool_generation == self._pool_generation:
            return self._pool
        for generation, pool in self._retired_pools:
            if generation == self._current_pool_generation:
                return pool
        return None


__all__ = ("SharedFramePresenter",)
