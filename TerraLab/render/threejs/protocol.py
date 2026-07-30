"""Typed protocol specifications and schemas for Three.js bridge communication."""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping

from TerraLab.scene.contracts import JSONValue, freeze_json_mapping

PROTOCOL_VERSION = 1

OP_START = "start"
OP_READY = "ready"
OP_SUBMIT = "submit"
OP_ACK = "ack"
OP_PICK_REQUEST = "pick_request"
OP_PICK_RESULT = "pick_result"
OP_REGISTER_RESOURCE = "register_resource"
OP_DISPOSE_RESOURCE = "dispose_resource"
OP_ERROR = "error"
OP_CLOSE = "close"
OP_RESTART = "restart"

ALLOWED_OPERATIONS = frozenset(
    {
        OP_START,
        OP_READY,
        OP_SUBMIT,
        OP_ACK,
        OP_PICK_REQUEST,
        OP_PICK_RESULT,
        OP_REGISTER_RESOURCE,
        OP_DISPOSE_RESOURCE,
        OP_ERROR,
        OP_CLOSE,
        OP_RESTART,
    }
)

PRIMITIVE_COLOR = "color"
PRIMITIVE_POINT = "point_sprite"
PRIMITIVE_LINE = "line_polyline"
PRIMITIVE_MESH = "triangle_mesh"
PRIMITIVE_IMAGE = "image"
PRIMITIVE_TEXT = "text"
PRIMITIVE_CLIP_BLEND = "clip_blend"
PRIMITIVE_SKY_GRADIENT = "sky_gradient"
PRIMITIVE_STAR_BATCH = "star_batch"
PRIMITIVE_CELESTIAL_BODY = "celestial_body"
PRIMITIVE_MILKYWAY_MESH = "milkyway_mesh"
PRIMITIVE_GRID_LINES = "grid_lines"
PRIMITIVE_LABEL_TEXT = "label_text"
PRIMITIVE_SCOPE_MASK = "scope_mask"
PRIMITIVE_CONSTELLATION_SEGMENT = "constellation_segment"
PRIMITIVE_MEASUREMENT_PULSE = "measurement_pulse"
PRIMITIVE_TERRAIN_MESH = "terrain_mesh"
PRIMITIVE_TERRAIN_MATERIAL = "terrain_material"
PRIMITIVE_INTERACTION_AFFORDANCE = "interaction_affordance"

ALLOWED_PRIMITIVES = frozenset(
    {
        PRIMITIVE_COLOR,
        PRIMITIVE_POINT,
        PRIMITIVE_LINE,
        PRIMITIVE_MESH,
        PRIMITIVE_IMAGE,
        PRIMITIVE_TEXT,
        PRIMITIVE_CLIP_BLEND,
        PRIMITIVE_SKY_GRADIENT,
        PRIMITIVE_STAR_BATCH,
        PRIMITIVE_CELESTIAL_BODY,
        PRIMITIVE_MILKYWAY_MESH,
        PRIMITIVE_GRID_LINES,
        PRIMITIVE_LABEL_TEXT,
        PRIMITIVE_SCOPE_MASK,
        PRIMITIVE_CONSTELLATION_SEGMENT,
        PRIMITIVE_MEASUREMENT_PULSE,
        PRIMITIVE_TERRAIN_MESH,
        PRIMITIVE_TERRAIN_MATERIAL,
        PRIMITIVE_INTERACTION_AFFORDANCE,
    }
)


def encode_binary_handle(buffer: bytes | memoryview) -> str:
    """Encode binary buffer to base64 string handle for array buffer transfer."""
    raw = bytes(buffer)
    return base64.b64encode(raw).decode("ascii")


def decode_binary_handle(handle: str) -> bytes:
    """Decode base64 string handle back to raw bytes."""
    return base64.b64decode(handle)


def build_bridge_message(
    op: str,
    payload: Mapping[str, Any],
    *,
    generation: int = 0,
    seq: int = 0,
) -> Mapping[str, JSONValue]:
    """Construct a validated, frozen bridge protocol message."""
    if op not in ALLOWED_OPERATIONS:
        raise ValueError(f"Operation {op!r} is not allowed in Three.js bridge protocol")
    msg: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "op": op,
        "gen": int(generation),
        "seq": int(seq),
        "payload": payload,
    }
    return freeze_json_mapping(msg)


def parse_bridge_message(raw_json: str | bytes | Mapping[str, Any]) -> Mapping[str, JSONValue]:
    """Parse and validate incoming bridge message."""
    if isinstance(raw_json, (str, bytes)):
        try:
            data = json.loads(raw_json)
        except Exception as exc:
            raise ValueError(f"Malformed JSON bridge message: {exc}") from exc
    elif isinstance(raw_json, Mapping):
        data = dict(raw_json)
    else:
        raise TypeError(f"Invalid bridge message type: {type(raw_json)}")

    if not isinstance(data, dict):
        raise ValueError("Bridge message must be a JSON object")

    version = data.get("v")
    if version != PROTOCOL_VERSION:
        raise ValueError(
            f"Unsupported bridge protocol version: {version} (expected {PROTOCOL_VERSION})"
        )

    op = str(data.get("op") or "")
    if op not in ALLOWED_OPERATIONS:
        raise ValueError(f"Disallowed operation in bridge message: {op!r}")

    payload = data.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("Bridge message payload must be an object")

    return freeze_json_mapping(data)
