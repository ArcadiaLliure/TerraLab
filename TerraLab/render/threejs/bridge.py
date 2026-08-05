"""Three.js bridge lifecycle and file-backed binary resource transport."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Literal, Mapping

from TerraLab.core.rendering_contracts.contracts import (
    RenderBackendLifecycleError,
)
from TerraLab.render.threejs.protocol import (
    OP_ACK,
    OP_CLOSE,
    OP_DISPOSE_RESOURCE,
    OP_ERROR,
    OP_INVALIDATE_RESOURCE,
    OP_PICK_REQUEST,
    OP_PICK_RESULT,
    OP_READY,
    OP_REGISTER_RESOURCE,
    OP_RESTART,
    OP_RESIZE,
    OP_START,
    OP_SUBMIT,
    OP_UPDATE_RESOURCE,
    OP_VISIBILITY,
    BinaryResource,
    BinaryResourceDescriptor,
    BridgeViewport,
    BridgeVisibility,
    PreparedBridgeFrame,
    build_bridge_message,
    parse_bridge_message,
)
from TerraLab.scene.contracts import JSONValue, freeze_json_mapping


ResourceOperation = Literal["register", "update", "reuse"]
ResourceState = Literal["pending", "ready", "invalid"]
THREEJS_RESOURCE_URL_PATH_PREFIX = "resources/"
_RESOURCE_FILE_NAME = re.compile(r"[0-9a-f]{64}\.bin\Z")


@dataclass(frozen=True, slots=True)
class ResourceRegistration:
    """Typed result of a resource register/update/reuse decision."""

    operation: ResourceOperation
    descriptor: BinaryResourceDescriptor
    message: Mapping[str, JSONValue] | None = None


@dataclass(slots=True)
class _TrackedResource:
    descriptor: BinaryResourceDescriptor
    state: ResourceState
    paths: set[Path]
    retired_paths: set[Path]


class ResourceRegistry:
    """Own file-backed resources exposed by the local Three.js host."""

    def __init__(self, resource_root: Path | None = None) -> None:
        self._temporary_root = (
            TemporaryDirectory(prefix="terralab-threejs-")
            if resource_root is None
            else None
        )
        if self._temporary_root is not None:
            self._root = Path(self._temporary_root.name)
        else:
            assert resource_root is not None
            self._root = Path(resource_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._resources: dict[str, _TrackedResource] = {}

    def register(self, resource: BinaryResource) -> ResourceRegistration:
        existing = self._resources.get(resource.resource_id)
        if existing is not None and existing.state != "invalid":
            if existing.descriptor.version == resource.version:
                return ResourceRegistration("reuse", existing.descriptor)

        descriptor, path = self._materialize(resource)
        operation: ResourceOperation = (
            "update" if existing is not None else "register"
        )
        retired_paths: set[Path] = set()
        if existing is not None:
            retired_paths.update(existing.paths)
            retired_paths.update(existing.retired_paths)
        self._resources[resource.resource_id] = _TrackedResource(
            descriptor=descriptor,
            state="pending",
            paths={path},
            retired_paths=retired_paths,
        )
        return ResourceRegistration(operation, descriptor)

    def invalidate(self, resource_id: str) -> BinaryResourceDescriptor | None:
        tracked = self._resources.get(str(resource_id))
        if tracked is None:
            return None
        tracked.state = "invalid"
        return tracked.descriptor

    def dispose(self, resource_id: str) -> bool:
        tracked = self._resources.pop(str(resource_id), None)
        if tracked is None:
            return False
        for path in tracked.paths | tracked.retired_paths:
            self._unlink(path)
        return True

    def acknowledge_ready(self, resource_id: str, version: str) -> bool:
        tracked = self._resources.get(str(resource_id))
        if tracked is None or tracked.descriptor.version != str(version):
            return False
        tracked.state = "ready"
        for path in tuple(tracked.retired_paths):
            self._unlink(path)
        tracked.retired_paths.clear()
        return True

    def recovery_descriptors(self) -> tuple[BinaryResourceDescriptor, ...]:
        descriptors: list[BinaryResourceDescriptor] = []
        for resource_id in sorted(self._resources):
            tracked = self._resources[resource_id]
            if tracked.state != "invalid":
                tracked.state = "pending"
                descriptors.append(tracked.descriptor)
        return tuple(descriptors)

    def is_registered(
        self, resource_id: str, version: str | None = None
    ) -> bool:
        tracked = self._resources.get(str(resource_id))
        return bool(
            tracked is not None
            and tracked.state != "invalid"
            and (version is None or tracked.descriptor.version == str(version))
        )

    def descriptor_for(
        self, resource_id: str
    ) -> BinaryResourceDescriptor | None:
        tracked = self._resources.get(str(resource_id))
        return None if tracked is None else tracked.descriptor

    def clear(self) -> None:
        for resource_id in tuple(self._resources):
            self.dispose(resource_id)
        if self._temporary_root is not None:
            self._temporary_root.cleanup()

    def get_registered_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._resources))

    def path_for_uri(self, uri: str) -> Path | None:
        """Resolve an opaque, same-origin resource path to its local file."""

        value = str(uri)
        name = value.removeprefix(THREEJS_RESOURCE_URL_PATH_PREFIX)
        if (
            value != f"{THREEJS_RESOURCE_URL_PATH_PREFIX}{name}"
            or _RESOURCE_FILE_NAME.fullmatch(name) is None
        ):
            return None
        path = self._root / name
        return path if path.is_file() else None

    def _materialize(
        self, resource: BinaryResource
    ) -> tuple[BinaryResourceDescriptor, Path]:
        digest = sha256(
            f"{resource.resource_id}\0{resource.version}".encode("utf-8")
        ).hexdigest()
        path = self._root / f"{digest}.bin"
        pending = path.with_suffix(".pending")
        with pending.open("wb") as stream:
            remaining = resource.data
            while remaining:
                written = stream.write(remaining)
                if written is None or written <= 0:
                    raise OSError("Unable to write Three.js binary resource")
                remaining = remaining[written:]
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(path)
        descriptor = BinaryResourceDescriptor(
            resource_id=resource.resource_id,
            version=resource.version,
            kind=resource.kind,
            dtype=resource.dtype,
            components=resource.components,
            element_count=resource.element_count,
            byte_length=resource.byte_length,
            cadence=resource.cadence,
            uri=f"{THREEJS_RESOURCE_URL_PATH_PREFIX}{digest}.bin",
        )
        return descriptor, path

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            # Chromium can hold the old file briefly after a version update.
            # Temporary-directory cleanup retries it when the host is closed.
            return


class ThreeJSBridge:
    """Local control bridge; QWebChannel carries no large resource bytes."""

    def __init__(
        self,
        *,
        outbound_handler: Callable[[Mapping[str, JSONValue]], None]
        | None = None,
        resource_root: Path | None = None,
    ) -> None:
        self._started = False
        self._ready = False
        self._closed = False
        self._seq = 0
        self._outbound_handler = outbound_handler
        self._resources = ResourceRegistry(resource_root)
        self._history: dict[int, Mapping[str, JSONValue]] = {}
        self._last_manifest: Mapping[str, JSONValue] | None = None
        self._last_generation: int | None = None
        self._acknowledged_generation: int | None = None
        self._last_ack: Mapping[str, JSONValue] | None = None
        self._last_error: Mapping[str, JSONValue] | None = None
        self._last_pick_result: Mapping[str, JSONValue] | None = None
        self._viewport: BridgeViewport | None = None
        self._channel_ready = False
        self._inbound_listeners: list[
            Callable[[Mapping[str, JSONValue]], None]
        ] = []

    @property
    def is_started(self) -> bool:
        return self._started and not self._closed

    @property
    def is_ready(self) -> bool:
        return self._ready and not self._closed

    @property
    def is_channel_ready(self) -> bool:
        return self._channel_ready and not self._closed

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

    def add_inbound_listener(
        self, listener: Callable[[Mapping[str, JSONValue]], None]
    ) -> None:
        """Observe validated host replies without coupling to Qt transport."""

        if listener not in self._inbound_listeners:
            self._inbound_listeners.append(listener)

    def remove_inbound_listener(
        self, listener: Callable[[Mapping[str, JSONValue]], None]
    ) -> None:
        if listener in self._inbound_listeners:
            self._inbound_listeners.remove(listener)

    def start(
        self, viewport: BridgeViewport | Mapping[str, Any]
    ) -> Mapping[str, JSONValue]:
        if self._closed:
            raise RenderBackendLifecycleError(
                "Cannot start a closed Three.js bridge"
            )
        if self._started:
            raise RenderBackendLifecycleError(
                "Three.js bridge is already started"
            )
        self._viewport = self._coerce_viewport(viewport)
        self._started = True
        return self._send(OP_START, {"viewport": self._viewport.to_payload()})

    def resize(
        self, viewport: BridgeViewport | Mapping[str, Any]
    ) -> Mapping[str, JSONValue]:
        self._require_active()
        self._viewport = self._coerce_viewport(viewport)
        return self._send(OP_RESIZE, {"viewport": self._viewport.to_payload()})

    def set_visibility(
        self, visibility: BridgeVisibility
    ) -> Mapping[str, JSONValue]:
        self._require_active()
        return self._send(OP_VISIBILITY, visibility.to_payload())

    def register_resource(
        self, resource: BinaryResource
    ) -> ResourceRegistration:
        self._require_active()
        registration = self._resources.register(resource)
        if registration.operation == "reuse":
            return registration
        operation = (
            OP_REGISTER_RESOURCE
            if registration.operation == "register"
            else OP_UPDATE_RESOURCE
        )
        return replace(
            registration,
            message=self._send(
                operation, registration.descriptor.to_payload()
            ),
        )

    def invalidate_resource(
        self, resource_id: str
    ) -> Mapping[str, JSONValue] | None:
        self._require_active()
        descriptor = self._resources.invalidate(resource_id)
        if descriptor is None:
            return None
        return self._send(OP_INVALIDATE_RESOURCE, descriptor.to_payload())

    def dispose_resource(self, resource_id: str) -> Mapping[str, JSONValue]:
        self._require_active()
        self._resources.dispose(resource_id)
        return self._send(OP_DISPOSE_RESOURCE, {"id": str(resource_id)})

    def submit_prepared_frame(
        self, frame: PreparedBridgeFrame
    ) -> Mapping[str, JSONValue]:
        self._require_active()
        for resource in frame.resources:
            self.register_resource(resource)
        return self.submit_frame(frame.generation, frame.manifest)

    def submit_frame(
        self, generation: int, frame_manifest: Mapping[str, JSONValue]
    ) -> Mapping[str, JSONValue]:
        self._require_active()
        gen = int(generation)
        base_is_acknowledged = (
            self._last_manifest is not None
            and self._last_generation is not None
            and self._acknowledged_generation == self._last_generation
        )
        if not base_is_acknowledged:
            payload: Mapping[str, JSONValue] = freeze_json_mapping(
                {"mode": "full", "manifest": frame_manifest}
            )
        else:
            changed = {
                key: value
                for key, value in frame_manifest.items()
                if self._last_manifest.get(key) != value
            }
            removed = tuple(
                key for key in self._last_manifest if key not in frame_manifest
            )
            payload = freeze_json_mapping(
                {
                    "mode": "delta",
                    "base_generation": self._last_generation,
                    "patch": changed,
                    "removed": removed,
                }
            )
        message = self._send(OP_SUBMIT, payload, generation=gen)
        self._history[gen] = message
        self._last_generation = gen
        self._last_manifest = frame_manifest
        return message

    def request_pick(
        self,
        generation: int,
        request_id: str,
        x: float,
        y: float,
        radius: float = 20.0,
        purpose: str = "select",
        action: str = "",
        options: Mapping[str, JSONValue] | None = None,
    ) -> Mapping[str, JSONValue]:
        self._require_active()
        gen = int(generation)
        return self._send(
            OP_PICK_REQUEST,
            {
                "generation": gen,
                "request_id": str(request_id),
                "x": float(x),
                "y": float(y),
                "radius": max(0.0, float(radius)),
                "purpose": str(purpose),
                "action": str(action),
                "options": freeze_json_mapping(options or {}),
            },
            generation=gen,
        )

    def receive_inbound(
        self, raw_message: str | bytes | Mapping[str, Any]
    ) -> Mapping[str, JSONValue]:
        message = parse_bridge_message(raw_message)
        operation = str(message["op"])
        payload = message["payload"]
        if not isinstance(payload, Mapping):
            raise ValueError("Bridge message payload must be an object")
        if operation == OP_READY:
            status = str(payload.get("status", ""))
            if status == "channel_ready":
                self._channel_ready = True
            else:
                # init()/restart() disposes the runner's retained manifest.
                # Resources can be restored, but the next frame must be full.
                self._acknowledged_generation = None
                self._ready = True
                self._restore_resources()
        elif operation == OP_ACK:
            self._last_ack = message
            if payload.get("operation") == "resource_ready":
                self._resources.acknowledge_ready(
                    str(payload.get("resource_id", "")),
                    str(payload.get("version", "")),
                )
            elif payload.get("operation") == "render":
                generation = int(message.get("gen", 0))
                if generation == self._last_generation:
                    self._acknowledged_generation = generation
        elif operation == OP_ERROR:
            self._last_error = message
            if str(payload.get("code", "")) == "resync_required":
                self._acknowledged_generation = None
        elif operation == OP_RESTART:
            self._ready = False
            self._acknowledged_generation = None
        elif operation == OP_PICK_RESULT:
            self._last_pick_result = message
        for listener in tuple(self._inbound_listeners):
            listener(message)
        return message

    def restart(
        self, viewport: BridgeViewport | Mapping[str, Any] | None = None
    ) -> Mapping[str, JSONValue]:
        if self._closed:
            raise RenderBackendLifecycleError("Cannot restart a closed bridge")
        if viewport is not None:
            self._viewport = self._coerce_viewport(viewport)
        if self._viewport is None:
            raise RenderBackendLifecycleError(
                "Cannot restart without a viewport"
            )
        self._ready = False
        self._acknowledged_generation = None
        self._resources.recovery_descriptors()
        return self._send(
            OP_RESTART, {"viewport": self._viewport.to_payload()}
        )

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
        self._channel_ready = False
        self._acknowledged_generation = None
        self._resources.clear()

    def get_last_ack(self) -> Mapping[str, JSONValue] | None:
        return self._last_ack

    def get_last_error(self) -> Mapping[str, JSONValue] | None:
        return self._last_error

    def get_last_pick_result(self) -> Mapping[str, JSONValue] | None:
        return self._last_pick_result

    def _restore_resources(self) -> None:
        for descriptor in self._resources.recovery_descriptors():
            self._send(OP_REGISTER_RESOURCE, descriptor.to_payload())

    def _send(
        self,
        operation: str,
        payload: Mapping[str, JSONValue],
        generation: int = 0,
    ) -> Mapping[str, JSONValue]:
        self._seq += 1
        message = build_bridge_message(
            operation, payload, generation=generation, seq=self._seq
        )
        if self._outbound_handler is not None:
            self._outbound_handler(message)
        return message

    def _require_active(self) -> None:
        if self._closed:
            raise RenderBackendLifecycleError("Three.js bridge is closed")
        if not self._started:
            raise RenderBackendLifecycleError("Three.js bridge is not started")

    @staticmethod
    def _coerce_viewport(
        viewport: BridgeViewport | Mapping[str, Any],
    ) -> BridgeViewport:
        return (
            viewport
            if isinstance(viewport, BridgeViewport)
            else BridgeViewport.from_mapping(viewport)
        )
