"""Three.js local bridge component handling lifecycle, message allowlisting, and resource tracking."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from TerraLab.core.rendering_contracts.contracts import RenderBackendLifecycleError
from TerraLab.render.threejs.protocol import (
    OP_ACK,
    OP_CLOSE,
    OP_DISPOSE_RESOURCE,
    OP_ERROR,
    OP_PICK_REQUEST,
    OP_PICK_RESULT,
    OP_READY,
    OP_REGISTER_RESOURCE,
    OP_RESTART,
    OP_START,
    OP_SUBMIT,
    build_bridge_message,
    decode_binary_handle,
    encode_binary_handle,
    parse_bridge_message,
)
from TerraLab.scene.contracts import JSONValue, freeze_json_mapping


class ResourceRegistry:
    """Tracks JavaScript host GPU resources with versioning and disposal lifecycle."""

    def __init__(self) -> None:
        self._resources: dict[str, dict[str, Any]] = {}

    def register(self, resource_id: str, kind: str, version: int, payload: Mapping[str, Any]) -> Mapping[str, JSONValue]:
        rid = str(resource_id)
        ver = int(version)
        existing = self._resources.get(rid)
        if existing and existing["version"] == ver:

            return freeze_json_mapping(existing)

        rec = {
            "id": rid,
            "kind": str(kind),
            "version": ver,
            "payload": dict(payload),
        }
        self._resources[rid] = rec
        return freeze_json_mapping(rec)

    def dispose(self, resource_id: str) -> bool:
        rid = str(resource_id)
        if rid in self._resources:
            del self._resources[rid]
            return True
        return False

    def is_registered(self, resource_id: str, version: int | None = None) -> bool:
        rid = str(resource_id)
        if rid not in self._resources:
            return False
        if version is not None and self._resources[rid]["version"] != version:
            return False
        return True

    def clear(self) -> None:
        self._resources.clear()

    def get_registered_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._resources))


class ThreeJSBridge:
    """Local bidirectional bridge for Three.js host process communication."""

    def __init__(
        self,
        *,
        outbound_handler: Callable[[Mapping[str, JSONValue]], None] | None = None,
    ) -> None:
        self._started = False
        self._ready = False
        self._closed = False
        self._seq = 0
        self._outbound_handler = outbound_handler
        self._resources = ResourceRegistry()
        self._history: dict[int, Mapping[str, JSONValue]] = {}
        self._last_ack: Mapping[str, JSONValue] | None = None

    @property
    def is_started(self) -> bool:
        return self._started and not self._closed

    @property
    def is_ready(self) -> bool:
        return self._ready and not self._closed

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def resources(self) -> ResourceRegistry:
        return self._resources

    def set_outbound_handler(
        self, handler: Callable[[Mapping[str, JSONValue]], None]
    ) -> None:
        self._outbound_handler = handler

    def start(self, viewport: Mapping[str, Any]) -> Mapping[str, JSONValue]:
        if self._closed:
            raise RenderBackendLifecycleError("Cannot start a closed Three.js bridge")
        if self._started:
            raise RenderBackendLifecycleError("Three.js bridge is already started")

        self._started = True
        msg = self._send(OP_START, {"viewport": dict(viewport)})
        return msg

    def submit_frame(self, generation: int, frame_manifest: Mapping[str, Any]) -> Mapping[str, JSONValue]:
        self._require_active()
        gen = int(generation)
        msg = self._send(
            OP_SUBMIT,
            {"generation": gen, "manifest": dict(frame_manifest)},
            generation=gen,
        )
        self._history[gen] = msg
        return msg

    def request_pick(
        self, generation: int, request_id: str, x: float, y: float, purpose: str = "select"
    ) -> Mapping[str, JSONValue]:
        self._require_active()
        gen = int(generation)
        payload = {
            "generation": gen,
            "request_id": str(request_id),
            "x": float(x),
            "y": float(y),
            "purpose": str(purpose),
        }
        return self._send(OP_PICK_REQUEST, payload, generation=gen)

    def register_resource(
        self, resource_id: str, kind: str, version: int, payload: Mapping[str, Any]
    ) -> Mapping[str, JSONValue]:
        self._require_active()
        res_record = self._resources.register(resource_id, kind, version, payload)
        return self._send(OP_REGISTER_RESOURCE, dict(res_record))

    def dispose_resource(self, resource_id: str) -> Mapping[str, JSONValue]:
        self._require_active()
        self._resources.dispose(resource_id)
        return self._send(OP_DISPOSE_RESOURCE, {"id": str(resource_id)})

    def encode_buffer_handle(self, buffer: bytes | memoryview) -> str:
        return encode_binary_handle(buffer)

    def decode_buffer_handle(self, handle: str) -> bytes:
        return decode_binary_handle(handle)

    def receive_inbound(self, raw_message: str | bytes | Mapping[str, Any]) -> Mapping[str, JSONValue]:
        msg = parse_bridge_message(raw_message)
        op = str(msg["op"])

        if op == OP_READY:
            self._ready = True
        elif op == OP_ACK:
            self._last_ack = msg
        elif op == OP_PICK_RESULT:
            pass
        elif op == OP_ERROR:
            pass
        elif op == OP_RESTART:
            self._ready = False

        return msg

    def restart(self) -> Mapping[str, JSONValue]:
        if self._closed:
            raise RenderBackendLifecycleError("Cannot restart a closed bridge")
        self._ready = False
        msg = self._send(OP_RESTART, {"action": "reconnect"})
        return msg

    def close(self) -> None:
        if self._closed:
            return
        if self._started:
            try:
                self._send(OP_CLOSE, {"reason": "user_close"})
            except (RenderBackendLifecycleError, ValueError, RuntimeError):
                self._ready = False
        self._closed = True
        self._started = False
        self._ready = False
        self._resources.clear()

    def get_last_ack(self) -> Mapping[str, JSONValue] | None:
        return self._last_ack

    def _send(
        self, op: str, payload: Mapping[str, Any], generation: int = 0
    ) -> Mapping[str, JSONValue]:
        self._seq += 1
        msg = build_bridge_message(
            op, payload, generation=generation, seq=self._seq
        )
        if self._outbound_handler is not None:
            self._outbound_handler(msg)
        return msg

    def _require_active(self) -> None:
        if self._closed:
            raise RenderBackendLifecycleError("Three.js bridge is closed")
        if not self._started:
            raise RenderBackendLifecycleError("Three.js bridge is not started")
