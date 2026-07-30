"""A QWidget that only presents frames produced by the render process."""

from __future__ import annotations

import ctypes
import time
from collections import deque
from typing import Any

from PyQt5 import sip
from PyQt5.QtCore import QEvent, QRectF, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.core.rendering_contracts.contracts import (
    RasterFrameHandle,
    RasterFrameOutput,
    RenderOutput,
)
from TerraLab.runtime.frame_pool import OwnedFramePool
from TerraLab.runtime.protocol import (
    FRAME_POOL,
    FRAME_READY,
    FRAME_RELEASED,
    PICK_REQUEST,
    PICK_RESULT,
    RESYNC_REQUEST,
    SCENE_DELTA,
    SCENE_SNAPSHOT,
    Envelope,
    encode_scene_frame_v1,
    envelope,
)
from TerraLab.scene.contracts import SceneFrame
from TerraLab.runtime.supervisor import RuntimeSupervisor
from TerraLab.view.pyqt.presentation import QtRenderOutputRouter


class SharedFramePresenter(QWidget):
    """Display the newest completed frame and never wait for its producer."""

    frame_presented = pyqtSignal(int, float)
    pick_result = pyqtSignal(object)

    def __init__(
        self,
        runtime: RuntimeSupervisor,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._pool: OwnedFramePool | None = None
        self._retired_pools: list[tuple[int, OwnedFramePool]] = []
        self._pool_generation = 0
        self._scene_generation = 0
        self._displayed_generation = -1
        self._pick_generations: dict[str, int] = {}
        self._snapshot: dict[str, Any] = {"loading": True}
        self._last_submitted_snapshot: dict[str, Any] | None = None
        self._last_submitted_generation = -1
        self._current_slot: int | None = None
        self._current_pool_generation = -1
        self._pending_presentation: tuple[int, float] | None = None
        self._last_frame_metadata: dict[str, Any] = {}
        self._paint_times_ms: deque[float] = deque(maxlen=600)
        self._closed = False
        self._output_router = QtRenderOutputRouter(
            raster=self._present_raster_output,
        )
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(40)
        self._resize_timer.timeout.connect(self._replace_pool)
        runtime.worker_ready.connect(self._on_worker_ready)
        runtime.message_received.connect(self._on_runtime_message)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: #000000;")

    @property
    def paint_times_ms(self) -> tuple[float, ...]:
        return tuple(self._paint_times_ms)

    @property
    def last_frame_metadata(self) -> dict[str, Any]:
        return dict(self._last_frame_metadata)

    def submit(self, frame: SceneFrame) -> None:
        """Encode one typed scene frame and request one new render."""

        self._scene_generation += 1
        numbered = frame.with_generation(self._scene_generation)
        current_snapshot = encode_scene_frame_v1(numbered)
        if self._pool is None:
            self._replace_pool()

        if (
            self._last_submitted_snapshot is not None
            and self._last_submitted_generation > 0
        ):
            changes = {
                k: v
                for k, v in current_snapshot.items()
                if self._last_submitted_snapshot.get(k) != v
            }
            self._runtime.send(
                "render",
                envelope(
                    SCENE_DELTA,
                    {
                        "base_generation": self._last_submitted_generation,
                        "changes": changes,
                    },
                    generation=self._scene_generation,
                ),
            )
        else:
            self._runtime.send(
                "render",
                envelope(
                    SCENE_SNAPSHOT,
                    current_snapshot,
                    generation=self._scene_generation,
                ),
            )
        self._snapshot = current_snapshot
        self._last_submitted_snapshot = current_snapshot
        self._last_submitted_generation = self._scene_generation

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._release_current()
        pools = [self._pool] if self._pool is not None else []
        pools.extend(pool for _generation, pool in self._retired_pools)
        self._pool = None
        self._retired_pools = []
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
        if self._closed:
            return
        ratio = max(1.0, float(self.devicePixelRatioF()))
        purpose = str(purpose or "select")
        generation = self._pick_generations.get(purpose, 0) + 1
        self._pick_generations[purpose] = generation
        self._runtime.send(
            "render",
            envelope(
                PICK_REQUEST,
                {
                    "x": float(x) * ratio,
                    "y": float(y) * ratio,
                    "radius": float(radius) * ratio,
                    "purpose": purpose,
                },
                request_id=f"pick:{purpose}",
                generation=generation,
            ),
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
        if self._closed:
            return
        ratio = max(1.0, float(self.devicePixelRatioF()))
        purpose = "interaction"
        generation = self._pick_generations.get(purpose, 0) + 1
        self._pick_generations[purpose] = generation
        payload = {
            "purpose": purpose,
            "action": str(action),
            "x": float(x) * ratio,
            "y": float(y) * ratio,
            "force_add": bool(force_add),
            "additive_select": bool(additive_select),
            "allow_when_disabled": bool(allow_when_disabled),
        }
        self._runtime.send(
            "render",
            envelope(
                PICK_REQUEST,
                payload,
                request_id="pick:interaction",
                generation=generation,
            ),
        )

    def present_output(self, output: RenderOutput) -> None:
        """Present only the output family this widget has registered for."""

        self._output_router.present(output)

    def _present_raster_output(self, output: RenderOutput) -> None:
        if not isinstance(output, RasterFrameOutput):
            raise TypeError("SharedFramePresenter only accepts raster output")
        handle = output.handle
        pool_generation = handle.pool_generation
        if pool_generation != self._pool_generation:
            return
        if output.generation <= self._displayed_generation:
            self._runtime.send(
                "render",
                envelope(
                    FRAME_RELEASED,
                    {"slot": handle.slot, "pool_generation": pool_generation},
                ),
            )
            return
        self._displayed_generation = int(output.generation)
        self._last_frame_metadata = {
            "slot": handle.slot,
            "width": handle.width,
            "height": handle.height,
            "stride": handle.stride,
            "pool_generation": pool_generation,
            "render_ms": output.render_ms,
            **dict(output.metadata),
        }
        self._release_current()
        self._current_slot = handle.slot
        self._current_pool_generation = pool_generation
        retired, self._retired_pools = self._retired_pools, []
        for _generation, pool in retired:
            pool.close()
        self._pending_presentation = (int(output.generation), output.render_ms)
        self.update()

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
            elapsed = (time.perf_counter() - started) * 1000.0
            self._paint_times_ms.append(elapsed)
        presented, self._pending_presentation = (
            self._pending_presentation,
            None,
        )
        if presented is not None:
            self.frame_presented.emit(*presented)

    def _draw_hud_overlay(self, painter: QPainter) -> None:
        try:
            snapshot = (
                self._snapshot if isinstance(self._snapshot, dict) else {}
            )
            camera = snapshot.get("camera", {})
            if not isinstance(camera, dict):
                camera = {}
            azimuth = float(camera.get("azimuth", 0.0)) % 360.0
            elevation = float(camera.get("elevation", 40.0))
            zoom = float(camera.get("zoom", 1.0))
            fov_deg = 93.9 / max(0.001, zoom)
            directions = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
            direction = directions[int((azimuth + 22.5) / 45.0) % 8]

            latitude = float(snapshot.get("latitude", 0.0))
            longitude = float(snapshot.get("longitude", 0.0))
            ut_h = float(snapshot.get("ut_hour", 12.0)) % 24.0
            bortle = int(snapshot.get("bortle", 1))

            debug_metrics = bool(snapshot.get("debug_render_metrics", False))
            box_width = min(560.0, max(280.0, float(self.width()) - 30.0))
            box_height = 100.0 if debug_metrics else 64.0

            painter.save()
            try:
                painter.setPen(QPen(QColor(216, 178, 106, 190), 1.5))
                painter.setBrush(QBrush(QColor(5, 8, 17, 235)))
                painter.drawRoundedRect(
                    QRectF(10.0, 10.0, box_width, box_height), 8.0, 8.0
                )

                lat_str = (
                    f"{abs(latitude):.2f}°{'N' if latitude >= 0 else 'S'}"
                )
                lon_str = (
                    f"{abs(longitude):.2f}°{'E' if longitude >= 0 else 'W'}"
                )
                line1 = f"{direction} ({azimuth:.1f}°) · ALT {elevation:.1f}° · FOV {fov_deg:.1f}°"
                time_str = f"UT {int(ut_h):02d}:{int((ut_h % 1) * 60):02d}"
                line2 = f"{time_str} · {lat_str} {lon_str} · Bortle {bortle}"

                font1 = QFont("Arial", 10, QFont.Bold)
                font1.setPixelSize(13)
                painter.setFont(font1)
                painter.setPen(QColor(255, 255, 255, 255))
                painter.drawText(20, 33, line1)

                font2 = QFont("Arial", 9, QFont.Normal)
                font2.setPixelSize(11)
                painter.setFont(font2)
                painter.setPen(QColor(190, 215, 245, 255))
                painter.drawText(20, 55, line2)

                if debug_metrics:
                    terrain = snapshot.get("terrain", {})
                    terrain = terrain if isinstance(terrain, dict) else {}
                    topography_enabled = bool(
                        terrain.get("topography_enabled", False)
                    )
                    surface_enabled = bool(
                        terrain.get("surface_enabled", False)
                    )
                    surface_path = str(terrain.get("surface_path", "") or "")
                    if not topography_enabled:
                        surface_status = "requereix Topografia"
                    elif not surface_enabled:
                        surface_status = "desactivada"
                    elif not surface_path:
                        surface_status = "esperant dades"
                    else:
                        surface_status = "activa"
                    metadata = self._last_frame_metadata
                    sky_pipeline = str(
                        metadata.get("sky_background_pipeline", "pendent")
                    ).upper()
                    stars_pipeline = str(
                        metadata.get("star_rendering_pipeline", "pendent")
                    ).upper()
                    celestial_pipeline = str(
                        metadata.get("celestial_rendering_pipeline", "pendent")
                    ).upper()
                    milkyway_pipeline = str(
                        metadata.get("milkyway_rendering_pipeline", "pendent")
                    ).upper()
                    deep_sky_pipeline = str(
                        metadata.get("deep_sky_rendering_pipeline", "pendent")
                    ).upper()
                    painter.drawText(
                        20,
                        94,
                        "Solar "
                        f"{celestial_pipeline} · Via Làctia {milkyway_pipeline} · "
                        f"NGC {deep_sky_pipeline}",
                    )
                    painter.drawText(
                        20,
                        76,
                        "DEBUG · "
                        f"Cel {sky_pipeline} · Estrelles {stars_pipeline} · "
                        f"Superfície {surface_status}",
                    )
            finally:
                painter.restore()
        except Exception:
            log_suppressed_exception(
                __name__, "SharedFramePresenter._draw_hud_overlay"
            )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.shutdown()
        super().closeEvent(event)

    def event(self, event: QEvent) -> bool:
        if event.type() == getattr(QEvent, "DevicePixelRatioChange", -1):
            self._resize_timer.start()
        return super().event(event)

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
        self._last_submitted_snapshot = None
        self._last_submitted_generation = -1
        payload = self._pool.descriptor.to_payload()
        payload["pool_generation"] = self._pool_generation
        self._runtime.send("render", envelope(FRAME_POOL, payload))
        self._runtime.send(
            "render",
            envelope(
                SCENE_SNAPSHOT,
                self._snapshot,
                generation=self._scene_generation,
            ),
        )

    def _on_worker_ready(self, role: str) -> None:
        if role != "render" or self._closed:
            return
        self._last_submitted_snapshot = None
        self._last_submitted_generation = -1
        # A restarted renderer has lost every shared-memory attachment.
        if self._pool is None:
            self._replace_pool()
            return
        payload = self._pool.descriptor.to_payload()
        payload["pool_generation"] = self._pool_generation
        self._runtime.send("render", envelope(FRAME_POOL, payload))
        self._runtime.send(
            "render",
            envelope(
                SCENE_SNAPSHOT,
                self._snapshot,
                generation=self._scene_generation,
            ),
        )

    def _on_runtime_message(self, role: str, message: Envelope) -> None:
        if role != "render" or self._closed:
            return
        if message.kind == RESYNC_REQUEST:
            self._last_submitted_snapshot = None
            self._last_submitted_generation = -1
            if self._snapshot:
                self._runtime.send(
                    "render",
                    envelope(
                        SCENE_SNAPSHOT,
                        self._snapshot,
                        generation=self._scene_generation,
                    ),
                )
            return
        if (
            message.kind == PICK_RESULT
            and message.request_id.startswith("pick:")
            and message.generation
            == self._pick_generations.get(
                message.request_id.partition(":")[2], -1
            )
        ):
            self.pick_result.emit(dict(message.payload))
            return
        if message.kind != FRAME_READY:
            return
        pool_generation = int(message.payload.get("pool_generation", -1))
        self.present_output(
            RasterFrameOutput(
                generation=int(message.generation),
                handle=RasterFrameHandle(
                    slot=int(message.payload["slot"]),
                    width=int(message.payload["width"]),
                    height=int(message.payload["height"]),
                    stride=int(message.payload["stride"]),
                    pool_generation=pool_generation,
                ),
                render_ms=float(message.payload.get("render_ms", 0.0)),
                metadata={
                    key: value
                    for key, value in message.payload.items()
                    if key
                    not in {
                        "slot",
                        "width",
                        "height",
                        "stride",
                        "pool_generation",
                        "render_ms",
                    }
                },
            )
        )

    def _release_current(self) -> None:
        if self._current_slot is None:
            return
        self._runtime.send(
            "render",
            envelope(
                FRAME_RELEASED,
                {
                    "slot": self._current_slot,
                    "pool_generation": self._current_pool_generation,
                },
            ),
        )
        self._current_slot = None
        self._current_pool_generation = -1

    def _pool_for_current_frame(self) -> OwnedFramePool | None:
        if self._current_pool_generation == self._pool_generation:
            return self._pool
        for generation, pool in self._retired_pools:
            if generation == self._current_pool_generation:
                return pool
        return None
