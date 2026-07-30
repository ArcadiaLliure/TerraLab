"""Pickle-free JSON-lines protocol shared by TerraLab processes (v1, active)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

from TerraLab.scene.contracts import JSONValue, SceneFrame, Viewport


PROTOCOL_VERSION = 1

WORKER_READY = "worker_ready"
WORKER_ERROR = "worker_error"
HEARTBEAT = "heartbeat"
SHUTDOWN = "shutdown"
SCENE_SNAPSHOT = "scene_snapshot"
SCENE_DELTA = "scene_delta"
RESYNC_REQUEST = "resync_request"
FRAME_POOL = "frame_pool"
FRAME_READY = "frame_ready"
FRAME_RELEASED = "frame_released"
COMPUTE_REQUEST = "compute_request"
ARTIFACT_READY = "artifact_ready"
PROGRESS = "progress"
PICK_REQUEST = "pick_request"
PICK_RESULT = "pick_result"

MESSAGE_KINDS = frozenset(
    {
        WORKER_READY,
        WORKER_ERROR,
        HEARTBEAT,
        SHUTDOWN,
        SCENE_SNAPSHOT,
        SCENE_DELTA,
        RESYNC_REQUEST,
        FRAME_POOL,
        FRAME_READY,
        FRAME_RELEASED,
        COMPUTE_REQUEST,
        ARTIFACT_READY,
        PROGRESS,
        PICK_REQUEST,
        PICK_RESULT,
    }
)


class ProtocolError(ValueError):
    """Raised when a process message violates the runtime contract."""


def encode_scene_frame_v1(frame: SceneFrame) -> dict[str, JSONValue]:
    """The sole protocol-v1 encoder for a typed scene frame."""

    return frame.to_legacy_snapshot()


def decode_scene_frame_v1(
    payload: Mapping[str, object],
    *,
    generation: int,
    viewport: Viewport,
) -> SceneFrame:
    """Decode a v1 scene payload before it reaches a render backend."""

    return SceneFrame.from_legacy_snapshot(
        generation=int(generation),
        snapshot=payload,
        viewport=viewport,
    )


@dataclass(frozen=True, slots=True)
class Envelope:
    """Versioned JSON message crossing a process boundary."""

    kind: str
    payload: Mapping[str, Any]
    request_id: str = ""
    generation: int = 0
    version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "kind": str(self.kind),
            "request_id": str(self.request_id),
            "generation": int(self.generation),
            "payload": dict(self.payload),
        }


def envelope(
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    request_id: str = "",
    generation: int = 0,
) -> Envelope:
    """Build and validate an outgoing message."""

    message = Envelope(
        kind=str(kind),
        payload=dict(payload or {}),
        request_id=str(request_id),
        generation=int(generation),
    )
    validate(message)
    return message


def _validate_json_value(val: object) -> None:
    if val is None or isinstance(val, (bool, int, str)):
        return
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ProtocolError("Message must contain finite JSON values only")
        return
    if isinstance(val, Mapping):
        for k, v in val.items():
            if not isinstance(k, str):
                raise ProtocolError("Mapping keys in message payload must be strings")
            _validate_json_value(v)
        return
    if isinstance(val, (list, tuple)):
        for item in val:
            _validate_json_value(item)
        return
    raise ProtocolError(f"Unsupported JSON payload value type: {type(val).__name__}")


def validate(message: Envelope) -> None:
    if int(message.version) != PROTOCOL_VERSION:
        raise ProtocolError(f"Unsupported protocol version: {message.version}")
    if message.kind not in MESSAGE_KINDS:
        raise ProtocolError(f"Unknown message kind: {message.kind!r}")
    if not isinstance(message.payload, Mapping):
        raise ProtocolError("Message payload must be a mapping")
    if int(message.generation) < 0:
        raise ProtocolError("Message generation cannot be negative")
    try:
        _validate_json_value(message.payload)
    except ProtocolError:
        raise
    except Exception as exc:
        raise ProtocolError("Message payload validation failed") from exc


def encode(message: Envelope) -> bytes:
    """Encode one compact JSONL message."""

    validate(message)
    return (
        json.dumps(
            message.to_dict(),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def decode(line: bytes | str) -> Envelope:
    """Decode and validate one JSONL message."""

    try:
        raw = (
            bytes(line).decode("utf-8")
            if isinstance(line, (bytes, bytearray))
            else str(line)
        )
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("Invalid JSONL runtime message") from exc
    if not isinstance(value, dict):
        raise ProtocolError("Runtime message must be a JSON object")
    try:
        message = Envelope(
            version=int(value["version"]),
            kind=str(value["kind"]),
            request_id=str(value.get("request_id", "")),
            generation=int(value.get("generation", 0)),
            payload=value.get("payload", {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError("Incomplete runtime message") from exc
    validate(message)
    return message
