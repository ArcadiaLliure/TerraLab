"""Toolkit-neutral render-process orchestration.

The service owns IPC and shared-memory leases only.  A selected view adapter
owns any toolkit application, paint device, surface, or command encoder.
"""

from __future__ import annotations

import os
import threading
import traceback
from collections import deque
from typing import Any

from TerraLab.application.render_planning import SceneRenderPlanner
from TerraLab.application.ports.rendering import (
    PickRequest,
    PickResult,
    RasterFrameHandle,
    RasterFrameOutput,
    RenderFailure,
    RenderOutput,
    RenderTargetKind,
    RendererBackend,
    SharedRasterTarget,
)
from TerraLab.bootstrap.composition import build_render_backend
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
    RESYNC_REQUEST,
    SCENE_DELTA,
    SCENE_SNAPSHOT,
    SHUTDOWN,
    WORKER_ERROR,
    WORKER_READY,
    Envelope,
    decode_scene_frame_v1,
    envelope,
)
from TerraLab.runtime.service_io import (
    read_messages,
    reserve_stdout_for_protocol,
    write_message,
)
from TerraLab.scene.contracts import Viewport, freeze_json_mapping, thaw_json_mapping


class _RenderMailbox:
    """Bounded inbox retaining one newest scene and one newest move per role."""

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
            if message.kind in (SCENE_SNAPSHOT, SCENE_DELTA):
                self._latest_scene = message
            elif message.kind == PICK_REQUEST and str(message.payload.get("purpose", "")) in {"hover", "scope"}:
                self._latest_picks[str(message.payload.get("purpose"))] = message
            elif message.kind == PICK_REQUEST and str(message.payload.get("purpose", "")) == "interaction" and str(message.payload.get("action", "")) in {"move", "constellation_move"}:
                self._latest_picks["interaction"] = message
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
            while not self._closed and not self._control and not self._latest_picks and self._latest_scene is None:
                self._condition.wait()
            if self._closed:
                return None
            if self._control:
                return self._control.popleft()
            for purpose in ("interaction", "scope", "hover"):
                pending = self._latest_picks.pop(purpose, None)
                if pending is not None:
                    return pending
            scene, self._latest_scene = self._latest_scene, None
            return scene


class OffscreenService:
    """Render-plan executor with shared-slot ownership, independent of Qt."""

    def __init__(self, backend: RendererBackend) -> None:
        self.pool: AttachedFramePool | None = None
        self.busy_slots: set[int] = set()
        self.last_slot = -1
        self.last_generation = -1
        self.pool_generation = -1
        self.last_snapshot: dict[str, Any] | None = None
        self.last_plan = None
        self.backend = backend
        self.planner = SceneRenderPlanner()
        self.backend.start(self)

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()
            self.pool = None
        self.backend.close()

    def attach_pool(self, payload: dict[str, Any]) -> None:
        replacement = AttachedFramePool(FramePoolDescriptor.from_payload(payload))
        previous, self.pool = self.pool, replacement
        self.busy_slots.clear()
        self.last_slot = -1
        self.last_snapshot = None
        self.last_plan = None
        self.pool_generation = int(payload.get("pool_generation", 0))
        if previous is not None:
            previous.close()

    def release(self, payload: dict[str, Any]) -> None:
        if int(payload.get("pool_generation", -1)) == self.pool_generation:
            self.busy_slots.discard(int(payload.get("slot", -1)))

    def render_snapshot(self, payload: dict[str, Any], generation: int) -> None:
        self.last_snapshot = dict(payload)
        # Advance before dispatch so an intentionally lightweight/test
        # executor still establishes the base required by the next delta.
        self.last_generation = int(generation)
        self.render(payload, generation)

    def render_delta(self, payload: dict[str, Any], generation: int) -> None:
        base = int(payload.get("base_generation", -1))
        if self.last_snapshot is None or self.last_generation != base:
            write_message(envelope(RESYNC_REQUEST, {"generation": generation, "reason": "missing_base_generation"}, generation=generation))
            return
        merged = dict(self.last_snapshot)
        merged.update(payload.get("changes", {}))
        self.last_snapshot = merged
        self.render(merged, generation)

    def render(self, payload: dict[str, Any], generation: int) -> None:
        if self.pool is None or generation < self.last_generation:
            return
        descriptor = self.pool.descriptor
        slot = first_free_slot(len(descriptor.names), self.busy_slots, after=self.last_slot)
        if slot is None:
            return
        frame = decode_scene_frame_v1(
            payload,
            generation=generation,
            viewport=Viewport(descriptor.width, descriptor.height),
        )
        plan = self.planner.build(frame)
        lease = self.pool.buffer(slot)
        handle = RasterFrameHandle(
            slot=slot,
            width=descriptor.width,
            height=descriptor.height,
            stride=descriptor.stride,
            pool_generation=self.pool_generation,
            pixel_format=descriptor.pixel_format,
        )
        try:
            output = self.backend.render(plan, SharedRasterTarget(handle, lease))
        finally:
            lease.release()
        if not isinstance(output, RasterFrameOutput):
            raise TypeError(
                f"Backend {self.backend.backend_id!r} returned {output.kind.value!r}; "
                "the render worker owns a shared-raster target"
            )
        self.busy_slots.add(slot)
        self.last_slot = slot
        self.last_generation = int(generation)
        self.last_plan = plan
        self.frame_ready(output)

    def pick(self, payload: dict[str, Any], generation: int, request_id: str) -> None:
        request = PickRequest(
            generation=int(generation),
            request_id=str(request_id),
            x=float(payload.get("x", 0.0)),
            y=float(payload.get("y", 0.0)),
            radius=float(payload.get("radius", 20.0)),
            purpose=str(payload.get("purpose", "select") or "select"),
            action=str(payload.get("action", "")),
            options=freeze_json_mapping(payload),
        )
        self.backend.request_pick(request, self.last_plan)

    def frame_ready(self, output: RenderOutput) -> None:
        if not isinstance(output, RasterFrameOutput):
            self.backend_failed(
                RenderFailure(
                    self.backend.backend_id,
                    "render",
                    f"Runtime cannot publish {output.kind.value} through FRAME_READY",
                )
            )
            return
        handle = output.handle
        write_message(
            envelope(
                FRAME_READY,
                {
                    "slot": handle.slot,
                    "width": handle.width,
                    "height": handle.height,
                    "stride": handle.stride,
                    "pool_generation": handle.pool_generation,
                    "render_ms": output.render_ms,
                    **thaw_json_mapping(output.metadata),
                },
                generation=output.generation,
            )
        )

    def pick_ready(self, result: PickResult) -> None:
        write_message(envelope(PICK_RESULT, thaw_json_mapping(result.payload), request_id=result.request_id, generation=result.generation))

    def backend_failed(self, failure: RenderFailure) -> None:
        write_message(envelope(WORKER_ERROR, {"role": "render", "backend": failure.backend_id, "operation": failure.operation, "message": failure.message}))


def run() -> int:
    reserve_stdout_for_protocol()
    try:
        service = OffscreenService(
            build_render_backend(target_kind=RenderTargetKind.SHARED_RASTER)
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        write_message(envelope(WORKER_ERROR, {"role": "render", "message": str(exc)}))
        return 2
    mailbox = _RenderMailbox()

    def receive() -> None:
        try:
            for incoming in read_messages():
                if incoming.kind == SHUTDOWN:
                    return
                mailbox.put(incoming)
        finally:
            mailbox.close()

    reader = threading.Thread(target=receive, name="terralab-render-ipc", daemon=True)
    reader.start()
    write_message(envelope(WORKER_READY, {"role": "render", "pid": os.getpid(), "backend": service.backend.backend_id}))
    try:
        while (message := mailbox.take()) is not None:
            try:
                if message.kind == HEARTBEAT:
                    write_message(envelope(HEARTBEAT, {"role": "render", "pid": os.getpid()}, generation=message.generation))
                elif message.kind == FRAME_POOL:
                    service.attach_pool(dict(message.payload))
                elif message.kind == FRAME_RELEASED:
                    service.release(dict(message.payload))
                elif message.kind == SCENE_SNAPSHOT:
                    service.render_snapshot(dict(message.payload), message.generation)
                elif message.kind == SCENE_DELTA:
                    service.render_delta(dict(message.payload), message.generation)
                elif message.kind == PICK_REQUEST:
                    service.pick(dict(message.payload), message.generation, message.request_id)
            except Exception as exc:
                write_message(envelope(WORKER_ERROR, {"role": "render", "message": str(exc), "traceback": traceback.format_exc(limit=8)}, generation=message.generation))
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
