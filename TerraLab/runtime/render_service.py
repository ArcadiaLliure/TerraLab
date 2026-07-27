"""Offscreen render process entry point."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from collections import deque

from PyQt5 import sip
from PyQt5.QtGui import (
    QGuiApplication,
    QImage,
    QPainter,
)

from TerraLab.runtime.offscreen_renderer import OffscreenSceneRenderer
from TerraLab.runtime.frame_pool import (
    AttachedFramePool,
    FramePoolDescriptor,
    first_free_slot,
)
from TerraLab.runtime.protocol import (
    FRAME_POOL,
    FRAME_READY,
    FRAME_RELEASED,
    HEARTBEAT,
    PICK_REQUEST,
    PICK_RESULT,
    SCENE_SNAPSHOT,
    SHUTDOWN,
    WORKER_ERROR,
    WORKER_READY,
    Envelope,
    envelope,
)
from TerraLab.runtime.service_io import (
    read_messages,
    reserve_stdout_for_protocol,
    write_message,
)


class _RenderMailbox:
    """Bounded cross-thread inbox with latest-wins scene state."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._control: deque[Envelope] = deque()
        self._latest_scene: Envelope | None = None
        self._latest_picks: dict[str, Envelope] = {}
        self._closed = False

    def put(self, message: Envelope) -> None:
        with self._condition:
            if self._closed:
                return
            if message.kind == SCENE_SNAPSHOT:
                self._latest_scene = message
            elif message.kind == PICK_REQUEST and (
                str(message.payload.get("purpose", "select"))
                in {"hover", "scope"}
                or (
                    str(message.payload.get("purpose", "")) == "interaction"
                    and str(message.payload.get("action", ""))
                    in {"move", "constellation_move"}
                )
            ):
                purpose = str(message.payload.get("purpose", "pick"))
                self._latest_picks[purpose] = message
            elif (
                message.kind == PICK_REQUEST
                and str(message.payload.get("purpose", ""))
                == "interaction"
                and str(message.payload.get("action", ""))
                in {
                    "release",
                    "cancel",
                    "constellation_click",
                    "constellation_right",
                    "constellation_double",
                    "constellation_cancel",
                }
            ):
                pending_move = self._latest_picks.pop(
                    "interaction", None
                )
                if pending_move is not None:
                    self._control.append(pending_move)
                self._control.append(message)
            else:
                self._control.append(message)
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._control.clear()
            self._latest_scene = None
            self._latest_picks.clear()
            self._condition.notify_all()

    def take(self) -> Envelope | None:
        with self._condition:
            while (
                not self._closed
                and not self._control
                and not self._latest_picks
                and self._latest_scene is None
            ):
                self._condition.wait()
            if self._closed:
                return None
            if self._control:
                return self._control.popleft()
            for purpose in ("interaction", "scope", "hover"):
                message = self._latest_picks.pop(purpose, None)
                if message is not None:
                    return message
            message, self._latest_scene = self._latest_scene, None
            return message


class OffscreenService:
    """Latest-frame renderer with explicit shared-slot ownership."""

    def __init__(self) -> None:
        self.pool: AttachedFramePool | None = None
        self.busy_slots: set[int] = set()
        self.last_slot = -1
        self.last_generation = -1
        self.pool_generation = -1
        self.renderer = OffscreenSceneRenderer()

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()
            self.pool = None
        self.renderer.close()

    def attach_pool(self, payload) -> None:
        replacement = AttachedFramePool(
            FramePoolDescriptor.from_payload(payload)
        )
        previous, self.pool = self.pool, replacement
        self.busy_slots.clear()
        self.last_slot = -1
        self.pool_generation = int(payload.get("pool_generation", 0))
        if previous is not None:
            previous.close()

    def release(self, payload) -> None:
        if int(payload.get("pool_generation", -1)) != self.pool_generation:
            return
        self.busy_slots.discard(int(payload.get("slot", -1)))

    def render(self, payload, generation: int) -> None:
        if self.pool is None or int(generation) < self.last_generation:
            return
        descriptor = self.pool.descriptor
        slot = first_free_slot(
            len(descriptor.names),
            self.busy_slots,
            after=self.last_slot,
        )
        if slot is None:
            return
        started = time.perf_counter()
        lease = self.pool.buffer(slot)
        try:
            address = ctypes.addressof(ctypes.c_ubyte.from_buffer(lease))
            image = QImage(
                sip.voidptr(address),
                descriptor.width,
                descriptor.height,
                descriptor.stride,
                QImage.Format_ARGB32_Premultiplied,
            )
            painter = QPainter(image)
            metadata = {}
            try:
                metadata = self.renderer.render(
                    painter,
                    descriptor.width,
                    descriptor.height,
                    payload,
                )
            finally:
                painter.end()
            del image
        finally:
            lease.release()
        self.busy_slots.add(slot)
        self.last_slot = slot
        self.last_generation = int(generation)
        write_message(
            envelope(
                FRAME_READY,
                {
                    "slot": slot,
                    "width": descriptor.width,
                    "height": descriptor.height,
                    "stride": descriptor.stride,
                    "pool_generation": self.pool_generation,
                    "render_ms": round(
                        (time.perf_counter() - started) * 1000.0, 3
                    ),
                    **metadata,
                },
                generation=generation,
            )
        )

    def pick(self, payload, generation: int, request_id: str) -> None:
        purpose = str(payload.get("purpose", "select") or "select")
        if purpose == "hover":
            result = self.renderer.pick_surface(
                float(payload.get("x", 0.0)),
                float(payload.get("y", 0.0)),
            )
        elif purpose == "interaction":
            result = self.renderer.interact(
                float(payload.get("x", 0.0)),
                float(payload.get("y", 0.0)),
                str(payload.get("action", "")),
                options=payload,
            )
        else:
            result = self.renderer.pick(
                float(payload.get("x", 0.0)),
                float(payload.get("y", 0.0)),
                float(payload.get("radius", 20.0)),
            )
        result["purpose"] = purpose
        write_message(
            envelope(
                PICK_RESULT,
                result,
                request_id=request_id,
                generation=generation,
            )
        )


def run() -> int:
    reserve_stdout_for_protocol()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    service = OffscreenService()
    mailbox = _RenderMailbox()

    def receive() -> None:
        try:
            for incoming in read_messages():
                if incoming.kind == SHUTDOWN:
                    return
                mailbox.put(incoming)
        finally:
            mailbox.close()

    reader = threading.Thread(
        target=receive,
        name="terralab-render-ipc",
        daemon=True,
    )
    reader.start()
    write_message(
        envelope(WORKER_READY, {"role": "render", "pid": os.getpid()})
    )
    try:
        while True:
            message = mailbox.take()
            if message is None:
                return 0
            if message.kind == HEARTBEAT:
                write_message(
                    envelope(
                        HEARTBEAT,
                        {"role": "render", "pid": os.getpid()},
                        generation=message.generation,
                    )
                )
            elif message.kind == FRAME_POOL:
                service.attach_pool(message.payload)
            elif message.kind == FRAME_RELEASED:
                service.release(message.payload)
            elif message.kind == SCENE_SNAPSHOT:
                service.render(message.payload, message.generation)
            elif message.kind == PICK_REQUEST:
                service.pick(
                    message.payload,
                    message.generation,
                    message.request_id,
                )
    except Exception as exc:
        write_message(
            envelope(WORKER_ERROR, {"role": "render", "message": str(exc)})
        )
        return 1
    finally:
        service.close()
        del app
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
