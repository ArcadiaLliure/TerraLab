"""Typed protocol specifications and schemas for Three.js bridge communication."""

from __future__ import annotations

from array import array
from hashlib import sha256
import json
import math
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Mapping, cast

import numpy as np

from TerraLab.core.rendering_contracts.plans import (
    ArtifactResource,
    Bounds,
    DrawablePrimitive,
    MaterialParameters,
    RenderPlanBundle,
    SpriteBatch,
    StarBatch,
    TerrainMeshResource,
    TextBatch,
    TextureParameters,
    TextureResource,
    TriangleMeshResource,
)
from TerraLab.scene.contracts import JSONValue, freeze_json_mapping
from TerraLab.scene.plans.interaction import (
    ScreenMeasurementItem,
    ScreenMeasurementLabel,
)
from TerraLab.scene.plans.labels import (
    LineSegment,
    ScreenRect,
    StrokeStyle,
    TextLabel,
    TextStyle,
)
from TerraLab.scene.plans.overlays import CirclePrimitive

PROTOCOL_VERSION = 2

OP_START = "start"
OP_READY = "ready"
OP_SUBMIT = "submit"
OP_ACK = "ack"
OP_PICK_REQUEST = "pick_request"
OP_PICK_RESULT = "pick_result"
OP_REGISTER_RESOURCE = "register_resource"
OP_UPDATE_RESOURCE = "update_resource"
OP_DISPOSE_RESOURCE = "dispose_resource"
OP_INVALIDATE_RESOURCE = "invalidate_resource"
OP_ERROR = "error"
OP_CLOSE = "close"
OP_RESTART = "restart"
OP_RESIZE = "resize"
OP_VISIBILITY = "visibility"

ALLOWED_OPERATIONS = frozenset(
    {
        OP_START,
        OP_READY,
        OP_SUBMIT,
        OP_ACK,
        OP_PICK_REQUEST,
        OP_PICK_RESULT,
        OP_REGISTER_RESOURCE,
        OP_UPDATE_RESOURCE,
        OP_DISPOSE_RESOURCE,
        OP_INVALIDATE_RESOURCE,
        OP_ERROR,
        OP_CLOSE,
        OP_RESTART,
        OP_RESIZE,
        OP_VISIBILITY,
    }
)

BinaryDataType = Literal["float32", "uint32", "uint8", "utf8_string_table"]
ResourceCadence = Literal["static", "frame", "delta"]
ResourceOwnership = Literal["bridge"]
ResourceLifetime = Literal["until_dispose"]


@dataclass(frozen=True, slots=True)
class BinaryResource:
    """CPU-owned binary payload that never enters the QWebChannel JSON stream."""

    resource_id: str
    version: str
    kind: str
    dtype: BinaryDataType
    components: int
    element_count: int
    cadence: ResourceCadence
    data: memoryview = field(repr=False, compare=False)
    _owner: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.resource_id or not self.version or not self.kind:
            raise ValueError("Binary resources require id, version, and kind")
        if self.components < 1 or self.element_count < 0:
            raise ValueError("Binary resource shape is invalid")
        view = memoryview(self.data)
        if not view.contiguous:
            raise ValueError("Binary resource data must be contiguous")
        view = view.cast("B")
        component_bytes = {
            "float32": 4,
            "uint32": 4,
            "uint8": 1,
        }.get(self.dtype)
        if component_bytes is not None:
            expected = self.components * self.element_count * component_bytes
            if view.nbytes != expected:
                raise ValueError(
                    "Binary resource byte length does not match its shape"
                )
        object.__setattr__(self, "data", view)

    @property
    def byte_length(self) -> int:
        return self.data.nbytes

    def reference_payload(self) -> Mapping[str, JSONValue]:
        return freeze_json_mapping(
            {
                "resource_id": self.resource_id,
                "version": self.version,
                "dtype": self.dtype,
                "components": self.components,
                "element_count": self.element_count,
                "cadence": self.cadence,
            }
        )


@dataclass(frozen=True, slots=True)
class BinaryResourceDescriptor:
    """Control descriptor for a bridge-owned binary resource URI."""

    resource_id: str
    version: str
    kind: str
    dtype: BinaryDataType
    components: int
    element_count: int
    byte_length: int
    cadence: ResourceCadence
    uri: str
    ownership: ResourceOwnership = "bridge"
    lifetime: ResourceLifetime = "until_dispose"

    def to_payload(self) -> Mapping[str, JSONValue]:
        return freeze_json_mapping(
            {
                "id": self.resource_id,
                "version": self.version,
                "kind": self.kind,
                "dtype": self.dtype,
                "components": self.components,
                "element_count": self.element_count,
                "byte_length": self.byte_length,
                "cadence": self.cadence,
                "uri": self.uri,
                "ownership": self.ownership,
                "lifetime": self.lifetime,
            }
        )


@dataclass(frozen=True, slots=True)
class PreparedBridgeFrame:
    """Resolved control manifest plus binary payloads for one Three.js frame."""

    generation: int
    manifest: Mapping[str, JSONValue]
    resources: tuple[BinaryResource, ...]


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
PRIMITIVE_DEEP_SKY_BATCH = "deep_sky_batch"
PRIMITIVE_GRID_LINES = "grid_lines"
PRIMITIVE_TRAIL_LINES = "trail_lines"
PRIMITIVE_LABEL_TEXT = "label_text"
PRIMITIVE_SCOPE_MASK = "scope_mask"
PRIMITIVE_CONSTELLATION_SEGMENT = "constellation_segment"
PRIMITIVE_MEASUREMENT_PULSE = "measurement_pulse"
PRIMITIVE_TERRAIN_MESH = "terrain_mesh"
PRIMITIVE_TERRAIN_MATERIAL = "terrain_material"
PRIMITIVE_INTERACTION_AFFORDANCE = "interaction_affordance"
PRIMITIVE_SCREEN_LINES = "screen_line_batch"
PRIMITIVE_SCREEN_CIRCLES = "screen_circle_batch"
PRIMITIVE_SCREEN_RECTS = "screen_rect_batch"
PRIMITIVE_TEXT_BATCH = "text_batch"

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
        PRIMITIVE_DEEP_SKY_BATCH,
        PRIMITIVE_GRID_LINES,
        PRIMITIVE_TRAIL_LINES,
        PRIMITIVE_LABEL_TEXT,
        PRIMITIVE_SCOPE_MASK,
        PRIMITIVE_CONSTELLATION_SEGMENT,
        PRIMITIVE_MEASUREMENT_PULSE,
        PRIMITIVE_TERRAIN_MESH,
        PRIMITIVE_TERRAIN_MATERIAL,
        PRIMITIVE_INTERACTION_AFFORDANCE,
        PRIMITIVE_SCREEN_LINES,
        PRIMITIVE_SCREEN_CIRCLES,
        PRIMITIVE_SCREEN_RECTS,
        PRIMITIVE_TEXT_BATCH,
    }
)


@dataclass(frozen=True, slots=True)
class BridgeViewport:
    """Typed CSS viewport sent from Qt to the hosted WebGL surface."""

    width: int
    height: int
    device_pixel_ratio: float

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError("Bridge viewport dimensions must be positive")
        if (
            not math.isfinite(self.device_pixel_ratio)
            or self.device_pixel_ratio <= 0.0
        ):
            raise ValueError("Bridge viewport DPR must be finite and positive")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BridgeViewport":
        return cls(
            width=max(1, int(payload.get("width", 1))),
            height=max(1, int(payload.get("height", 1))),
            device_pixel_ratio=max(0.01, float(payload.get("dpr", 1.0))),
        )

    def to_payload(self) -> Mapping[str, JSONValue]:
        return freeze_json_mapping(
            {
                "width": self.width,
                "height": self.height,
                "dpr": self.device_pixel_ratio,
            }
        )


@dataclass(frozen=True, slots=True)
class BridgeVisibility:
    """Explicitly conveys UI visibility and suspension to WebGL."""

    visible: bool
    suspended: bool

    def to_payload(self) -> Mapping[str, JSONValue]:
        return freeze_json_mapping(
            {"visible": self.visible, "suspended": self.suspended}
        )


def _bounds_payload(bounds: Bounds) -> Mapping[str, JSONValue]:
    return freeze_json_mapping(
        {"minimum": bounds.minimum, "maximum": bounds.maximum}
    )


def _texture_parameters_payload(
    texture: TextureParameters | None,
) -> Mapping[str, JSONValue] | None:
    if texture is None:
        return None
    return freeze_json_mapping(
        {
            "texture_id": texture.texture_id,
            "version": texture.version,
            "wrap_u": texture.wrap_u,
            "wrap_v": texture.wrap_v,
            "min_filter": texture.min_filter,
            "mag_filter": texture.mag_filter,
            "uv_offset": texture.uv_offset,
            "uv_scale": texture.uv_scale,
            "flip_y": texture.flip_y,
        }
    )


def _material_payload(material: MaterialParameters) -> Mapping[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "material_id": material.material_id,
        "color": material.color,
        "opacity": material.opacity,
        "point_size": material.point_size,
        "blend_mode": material.blend_mode,
        "depth_test": material.depth_test,
        "depth_write": material.depth_write,
    }
    texture = _texture_parameters_payload(material.texture)
    if texture is not None:
        payload["texture"] = texture
    return freeze_json_mapping(payload)


def _primitive_header_payload(
    primitive: DrawablePrimitive,
) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "primitive_id": primitive.primitive_id,
        "kind": primitive.kind,
        "layer_order": primitive.layer_order,
        "bounds": _bounds_payload(primitive.bounds),
        "frame_generation": primitive.frame_generation,
        "material": _material_payload(primitive.material),
    }
    texture = _texture_parameters_payload(primitive.texture)
    if texture is not None:
        payload["texture"] = texture
    return payload


def _numeric_resource(
    resource_id: str,
    version: str,
    kind: str,
    values: tuple[object, ...],
    *,
    components: int,
    dtype: Literal["float32", "uint32"],
    cadence: ResourceCadence = "static",
) -> BinaryResource:
    """Pack resolved numeric attributes once into a browser-readable buffer."""

    flattened = (
        component
        for value in values
        for component in (value if isinstance(value, tuple) else (value,))
    )
    if dtype == "float32":
        backing = array("f")
        backing.extend(float(cast(Any, component)) for component in flattened)
    else:
        backing = array("I")
        backing.extend(int(cast(Any, component)) for component in flattened)
    if (
        backing.itemsize != 4
    ):  # pragma: no cover - required by supported CPython.
        raise RuntimeError(
            "Bridge binary transport requires 32-bit array entries"
        )
    if (
        sys.byteorder != "little"
    ):  # Browser typed arrays use little-endian bytes.
        backing.byteswap()
    return BinaryResource(
        resource_id=resource_id,
        version=version,
        kind=kind,
        dtype=dtype,
        components=components,
        element_count=len(values),
        cadence=cadence,
        data=memoryview(backing),
        _owner=backing,
    )


def _string_resource(
    resource_id: str,
    version: str,
    kind: str,
    values: tuple[str, ...],
) -> BinaryResource:
    """Pack string attributes as length-prefixed UTF-8, never as JSON arrays."""

    backing = bytearray()
    for value in values:
        encoded = value.encode("utf-8")
        backing.extend(len(encoded).to_bytes(4, byteorder="little"))
        backing.extend(encoded)
    return BinaryResource(
        resource_id=resource_id,
        version=version,
        kind=kind,
        dtype="utf8_string_table",
        components=1,
        element_count=len(values),
        cadence="static",
        data=memoryview(backing),
        _owner=backing,
    )


def _buffer_reference(
    payload: dict[str, JSONValue], resource: BinaryResource
) -> None:
    buffers = payload.setdefault("buffers", {})
    if not isinstance(buffers, dict):  # pragma: no cover - internal invariant.
        raise RuntimeError("Primitive buffer collection is not mutable")
    buffers[resource.kind] = resource.reference_payload()


def _numeric_buffer(
    payload: dict[str, JSONValue],
    resources: list[BinaryResource],
    primitive_id: str,
    version: str,
    name: str,
    values: tuple[object, ...],
    *,
    components: int,
    dtype: Literal["float32", "uint32"] = "float32",
) -> None:
    if not values:
        return
    resource = _numeric_resource(
        f"{primitive_id}:{name}",
        version,
        name,
        values,
        components=components,
        dtype=dtype,
    )
    resources.append(resource)
    _buffer_reference(payload, resource)


def _string_buffer(
    payload: dict[str, JSONValue],
    resources: list[BinaryResource],
    primitive_id: str,
    version: str,
    name: str,
    values: tuple[str, ...],
) -> None:
    if not values:
        return
    resource = _string_resource(
        f"{primitive_id}:{name}", version, name, values
    )
    resources.append(resource)
    _buffer_reference(payload, resource)


def _mark_frame_resources(
    payload: dict[str, JSONValue],
    resources: list[BinaryResource],
    generation: int,
) -> list[BinaryResource]:
    """Version per-frame batches independently from stable catalog resources."""

    dynamic_resources = [
        replace(
            resource,
            version=f"{resource.version}:frame-{generation}",
            cadence="frame",
        )
        for resource in resources
    ]
    for resource in dynamic_resources:
        _buffer_reference(payload, resource)
    return dynamic_resources


def prepare_drawable_primitive(
    primitive: DrawablePrimitive,
) -> tuple[Mapping[str, JSONValue], tuple[BinaryResource, ...]]:
    """Keep large attributes out of JSON while retaining the full plan semantics."""

    payload = _primitive_header_payload(primitive)
    resources: list[BinaryResource] = []
    if isinstance(primitive, SpriteBatch):
        payload["version"] = primitive.version
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "positions",
            primitive.positions,
            components=3,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "colors",
            primitive.colors,
            components=4,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "sizes",
            primitive.sizes,
            components=1,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "alphas",
            primitive.alphas,
            components=1,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "uv",
            primitive.uv,
            components=2,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "normals",
            primitive.normals,
            components=3,
        )
        _string_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "object_ids",
            primitive.object_ids,
        )
    elif isinstance(primitive, StarBatch):
        payload["version"] = primitive.version
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "positions",
            primitive.positions,
            components=3,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "input_coordinates",
            primitive.input_coordinates,
            components=2,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "radii",
            primitive.radii,
            components=1,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "colors",
            primitive.colors,
            components=4,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "alphas",
            primitive.alphas,
            components=1,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "magnitudes",
            primitive.magnitudes,
            components=1,
        )
        _string_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "visual_classes",
            primitive.visual_classes,
        )
        _string_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "picking_ids",
            primitive.picking_ids,
        )
        _string_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "object_ids",
            primitive.object_ids,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "uv",
            primitive.uv,
            components=2,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.version,
            "normals",
            primitive.normals,
            components=3,
        )
    elif isinstance(primitive, TriangleMeshResource):
        payload["geometry_version"] = primitive.geometry_version
        payload["material_version"] = primitive.material_version
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.geometry_version,
            "positions",
            primitive.positions,
            components=3,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.geometry_version,
            "indices",
            primitive.indices,
            components=1,
            dtype="uint32",
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.material_version,
            "colors",
            primitive.colors,
            components=4,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.material_version,
            "alphas",
            primitive.alphas,
            components=1,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.material_version,
            "uv",
            primitive.uv,
            components=2,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.geometry_version,
            "normals",
            primitive.normals,
            components=3,
        )
        _string_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.geometry_version,
            "object_ids",
            primitive.object_ids,
        )
    elif isinstance(primitive, TextBatch):
        payload.update(
            {
                "version": primitive.version,
                "labels": tuple(
                    freeze_json_mapping(
                        {
                            "content": label.content,
                            "position": label.position,
                            "anchor": label.anchor,
                            "priority": label.priority,
                            "object_id": label.object_id,
                            "clip": _bounds_payload(label.clip)
                            if label.clip is not None
                            else None,
                            "style": freeze_json_mapping(
                                {
                                    "font_family": label.style.font_family,
                                    "size_px": label.style.size_px,
                                    "weight": label.style.weight,
                                    "italic": label.style.italic,
                                    "foreground": label.style.foreground,
                                    "background": label.style.background,
                                    "outline": label.style.outline,
                                    "outline_width_px": label.style.outline_width_px,
                                }
                            ),
                        }
                    )
                    for label in primitive.labels
                ),
            }
        )
    elif isinstance(primitive, TerrainMeshResource):
        payload["geometry_version"] = primitive.geometry_version
        payload["material_version"] = primitive.material_version
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.geometry_version,
            "vertices",
            primitive.vertices,
            components=3,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.geometry_version,
            "indices",
            primitive.indices,
            components=1,
            dtype="uint32",
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.geometry_version,
            "normals",
            primitive.normals,
            components=3,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.material_version,
            "surface_colors",
            primitive.surface.colors,
            components=4,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.material_version,
            "surface_alphas",
            primitive.surface.alphas,
            components=1,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.material_version,
            "surface_uv",
            primitive.surface.uv,
            components=2,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.material_version,
            "surface_elevations_m",
            primitive.surface.elevations_m,
            components=1,
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.material_version,
            "surface_class_ids",
            tuple(
                int(value) if int(value) >= 0 else 0xFFFFFFFF
                for value in primitive.surface.class_ids
            ),
            components=1,
            dtype="uint32",
        )
        _numeric_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.material_version,
            "surface_categorical_flags",
            tuple(int(value) for value in primitive.surface.categorical_flags),
            components=1,
            dtype="uint32",
        )
        if primitive.view_vertices:
            _numeric_buffer(
                payload,
                resources,
                primitive.primitive_id,
                primitive.geometry_version,
                "view_vertices",
                primitive.view_vertices,
                components=3,
            )
        _string_buffer(
            payload,
            resources,
            primitive.primitive_id,
            primitive.material_version,
            "surface_object_ids",
            primitive.surface.object_ids,
        )
        payload["tiles"] = tuple(
            freeze_json_mapping(
                {
                    "tile_id": tile.tile_id,
                    "first_index": tile.first_index,
                    "index_count": tile.index_count,
                    "bounds": _bounds_payload(tile.bounds),
                }
            )
            for tile in primitive.tiles
        )
    else:  # pragma: no cover - the DrawablePrimitive union is exhaustive.
        raise TypeError(
            f"Unsupported Three.js primitive: {type(primitive).__name__}"
        )
    if isinstance(primitive, (SpriteBatch, StarBatch)):
        resources = _mark_frame_resources(
            payload,
            resources,
            primitive.frame_generation,
        )
    return freeze_json_mapping(payload), tuple(resources)


def _artifact_payload(resource: ArtifactResource) -> Mapping[str, JSONValue]:
    return freeze_json_mapping(
        {
            "resource_id": resource.resource_id,
            "kind": resource.kind,
            "source": resource.source,
            "version": resource.version,
            "handle": resource.handle,
        }
    )


def _texture_resource_payload(
    resource: TextureResource,
) -> Mapping[str, JSONValue]:
    return freeze_json_mapping(
        {
            "texture_id": resource.texture_id,
            "source": resource.source,
            "version": resource.version,
            "width": resource.width,
            "height": resource.height,
            "color_space": resource.color_space,
            "parameters": _texture_parameters_payload(resource.parameters),
        }
    )


def prepare_render_plan_bundle(
    bundle: RenderPlanBundle,
) -> PreparedBridgeFrame:
    """Produce JSON control and typed binary payloads for one resolved scene."""

    prepared_plans: list[Mapping[str, JSONValue]] = []
    resources: list[BinaryResource] = []
    for plan in bundle.plans:
        primitive_payloads: list[Mapping[str, JSONValue]] = []
        for primitive in plan.primitives:
            payload, primitive_resources = prepare_drawable_primitive(
                primitive
            )
            primitive_payloads.append(payload)
            resources.extend(primitive_resources)
        prepared_plans.append(
            freeze_json_mapping(
                {
                    "capability": plan.capability,
                    "layer_order": plan.layer_order,
                    "frame_generation": plan.frame_generation,
                    "version": plan.version,
                    "primitives": tuple(primitive_payloads),
                }
            )
        )
    manifest = freeze_json_mapping(
        {
            "schema_version": bundle.schema_version,
            "generation": bundle.generation,
            "viewport": freeze_json_mapping(
                {
                    "width": bundle.frame.viewport.width,
                    "height": bundle.frame.viewport.height,
                    "dpr": bundle.frame.viewport.device_pixel_ratio,
                }
            ),
            "resources": freeze_json_mapping(
                {
                    "frame_generation": bundle.resources.frame_generation,
                    "artifacts": tuple(
                        _artifact_payload(resource)
                        for resource in bundle.resources.artifacts
                    ),
                    "textures": tuple(
                        _texture_resource_payload(resource)
                        for resource in bundle.resources.textures
                    ),
                }
            ),
            "plans": tuple(prepared_plans),
            "picking": freeze_json_mapping(
                {
                    "generation": bundle.picking.generation,
                    "records": tuple(
                        freeze_json_mapping(
                            {
                                "object_id": record.object_id,
                                "picking_id": record.picking_id,
                                "bounds": _bounds_payload(record.bounds),
                                "layer_order": record.layer_order,
                            }
                        )
                        for record in bundle.picking.records
                    ),
                }
            ),
            "interaction": freeze_json_mapping(
                {
                    "generation": bundle.interaction.generation,
                    "affordances": tuple(
                        freeze_json_mapping(
                            {
                                "affordance_id": affordance.affordance_id,
                                "action": affordance.action,
                                "bounds": _bounds_payload(affordance.bounds),
                                "active": affordance.active,
                                "layer_order": affordance.layer_order,
                            }
                        )
                        for affordance in bundle.interaction.affordances
                    ),
                }
            ),
        }
    )
    return PreparedBridgeFrame(bundle.generation, manifest, tuple(resources))


def _transport_version(prefix: str, value: object) -> str:
    """Return a deterministic View-side version without changing plan data."""

    digest = sha256(repr(value).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _array_resource(
    *,
    resource_id: str,
    version: str,
    kind: str,
    values: np.ndarray,
    components: int,
    dtype: Literal["float32", "uint32", "uint8"],
    cadence: ResourceCadence,
) -> BinaryResource:
    """Expose one already-resolved CPU array as a typed browser resource.

    The conversion is limited to the presentation boundary: it makes the
    array contiguous in the GPU attribute dtype, writes it once through the
    bridge-owned resource registry, and does not select, project, or alter
    scientific content.
    """

    numpy_dtype = {
        "float32": np.dtype(np.float32),
        "uint32": np.dtype(np.uint32),
        "uint8": np.dtype(np.uint8),
    }[dtype]
    array_value = np.ascontiguousarray(np.asarray(values, dtype=numpy_dtype))
    if array_value.size % components:
        raise ValueError(
            f"{kind} buffer size must be divisible by its component count"
        )
    return BinaryResource(
        resource_id=resource_id,
        version=version,
        kind=kind,
        dtype=dtype,
        components=components,
        element_count=int(array_value.size // components),
        cadence=cadence,
        data=memoryview(array_value),
        _owner=array_value,
    )


def _append_array_buffer(
    payload: dict[str, JSONValue],
    resources: list[BinaryResource],
    *,
    name: str,
    resource: BinaryResource,
) -> None:
    buffers = payload.setdefault("buffers", {})
    if not isinstance(buffers, dict):  # pragma: no cover - local invariant.
        raise RuntimeError("Three.js primitive buffers must be mutable")
    buffers[name] = resource.reference_payload()
    resources.append(resource)


def _screen_positions(
    screen_x: np.ndarray,
    screen_y: np.ndarray,
    height: int,
    *,
    z: float = 0.0,
) -> np.ndarray:
    """Change only screen convention (top-left to Three.js y-up)."""

    x_values = np.asarray(screen_x, dtype=np.float32).reshape(-1)
    y_values = np.asarray(screen_y, dtype=np.float32).reshape(-1)
    if len(x_values) != len(y_values):
        raise ValueError("Resolved screen coordinates must have equal lengths")
    result = np.empty((len(x_values), 3), dtype=np.float32)
    result[:, 0] = x_values
    result[:, 1] = float(height) - y_values
    result[:, 2] = float(z)
    return result


def _celestial_sky_primitive(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> Mapping[str, JSONValue] | None:
    celestial = bundle.celestial
    if celestial is None:
        return None
    sky = celestial.sky_background
    samples = (
        np.asarray(sky.rgba_samples, dtype=np.uint8).reshape(-1, 4).copy()
    )
    mask = np.asarray(sky.horizon_mask, dtype=np.bool_).reshape(-1)
    samples[~mask, 3] = 0
    version = _transport_version("sky", sky.cache_token)
    payload: dict[str, JSONValue] = {
        "kind": PRIMITIVE_SKY_GRADIENT,
        "primitive_id": "celestial:sky-gradient",
        "version": version,
        "layer_order": 10,
        "sample_width": sky.sample_width,
        "sample_height": sky.sample_height,
        "clip_to_viewport": sky.clip_to_viewport,
    }
    _append_array_buffer(
        payload,
        resources,
        name="rgba",
        resource=_array_resource(
            resource_id="celestial:sky-gradient:rgba",
            version=version,
            kind="sky_rgba",
            values=samples,
            components=4,
            dtype="uint8",
            cadence="static",
        ),
    )
    return freeze_json_mapping(payload)


def _celestial_star_primitive(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> Mapping[str, JSONValue] | None:
    celestial = bundle.celestial
    if celestial is None:
        return None
    sprites = celestial.stars.sprites
    total = int(len(sprites.screen_x))
    if total == 0:
        return None

    radii = np.full(total, 0.65, dtype=np.float32)
    style = np.zeros(total, dtype=np.uint8)
    weak = np.asarray(sprites.weak_indices, dtype=np.intp)
    medium = np.asarray(sprites.medium_indices, dtype=np.intp)
    bright = np.asarray(sprites.bright_indices, dtype=np.intp)
    base_rgba = np.asarray(sprites.base_rgba, dtype=np.uint8)
    if len(weak):
        radii[weak] = (
            0.42 + 0.38 * base_rgba[weak, 3].astype(np.float32) / 255.0
        )
    if len(medium):
        radii[medium] = (
            np.asarray(sprites.medium_radius_tenths, dtype=np.float32)[medium]
            / 10.0
        )
        style[medium] = 1
    if len(bright):
        radii[bright] = np.clip(
            1.10
            + 0.55 * np.asarray(sprites.size_bin, dtype=np.float32)[bright],
            1.0,
            5.8,
        )
        style[bright] = 2
    halo = np.clip(
        np.asarray(sprites.halo_bin, dtype=np.uint8), 0, 255
    ).reshape(-1)
    picks = celestial.stars.picks
    if len(picks.catalog_indices) != total:
        raise ValueError(
            "Star sprite and pick batches must stay index-aligned"
        )
    version = f"stars-frame-{bundle.generation}"
    payload: dict[str, JSONValue] = {
        "kind": PRIMITIVE_STAR_BATCH,
        "primitive_id": "celestial:stars",
        "version": version,
        "layer_order": 40,
        "pure_colors": celestial.pure_colors,
        "smooth": bool(sprites.smooth),
    }
    buffers = (
        (
            "positions",
            _screen_positions(
                sprites.screen_x,
                sprites.screen_y,
                bundle.frame.viewport.height,
            ),
            3,
            "float32",
        ),
        ("colors", base_rgba, 4, "uint8"),
        ("radii", radii, 1, "float32"),
        ("halo_bins", halo, 1, "uint8"),
        ("style", style, 1, "uint8"),
        (
            "pick_catalog_indices",
            np.asarray(picks.catalog_indices, dtype=np.uint32),
            1,
            "uint32",
        ),
    )
    if len(picks.altitude_deg):
        buffers += (("pick_altitude_deg", picks.altitude_deg, 1, "float32"),)
    if len(picks.azimuth_deg):
        buffers += (("pick_azimuth_deg", picks.azimuth_deg, 1, "float32"),)
    if len(picks.magnitude):
        buffers += (("pick_magnitude", picks.magnitude, 1, "float32"),)
    for name, values, components, dtype in buffers:
        _append_array_buffer(
            payload,
            resources,
            name=name,
            resource=_array_resource(
                resource_id=f"celestial:stars:{name}",
                version=version,
                kind=name,
                values=values,
                components=components,
                dtype=dtype,  # type: ignore[arg-type]
                cadence="frame",
            ),
        )
    return freeze_json_mapping(payload)


def _disc_payload(disc: object, height: int) -> Mapping[str, JSONValue]:
    """Serialize an already-resolved refracted disc without reprojecting it."""

    return freeze_json_mapping(
        {
            "center": (
                float(getattr(disc, "screen_x")),
                float(height) - float(getattr(disc, "screen_y")),
            ),
            "radii": (
                float(getattr(disc, "radius_x_px")),
                float(getattr(disc, "radius_y_px")),
            ),
        }
    )


def _celestial_body_primitive(
    bundle: RenderPlanBundle,
) -> Mapping[str, JSONValue] | None:
    celestial = bundle.celestial
    if celestial is None:
        return None
    bodies = celestial.bodies
    height = bundle.frame.viewport.height

    def pick_for(body_type: str) -> Mapping[str, JSONValue] | None:
        record = next(
            (item for item in bodies.picks if item.body_type == body_type),
            None,
        )
        if record is None:
            return None
        return freeze_json_mapping(
            {
                "object_id": f"body:{record.body_type}",
                "object_kind": "body",
                "key": record.body_type,
                "name": record.body_type.title(),
                "alt": record.altitude_deg,
                "az": record.azimuth_deg,
                "magnitude": record.magnitude,
                "metadata": freeze_json_mapping(
                    {"body_type": record.body_type}
                ),
            }
        )

    payload: dict[str, JSONValue] = {
        "kind": PRIMITIVE_CELESTIAL_BODY,
        "primitive_id": "celestial:bodies",
        "version": f"bodies-frame-{bundle.generation}",
        "layer_order": 50,
        "sun": None,
        "moon": None,
        "planets": (),
    }
    if bodies.sun is not None:
        corona = bodies.sun.corona
        payload["sun"] = freeze_json_mapping(
            {
                "disc": _disc_payload(bodies.sun.geometry, height),
                "core_rgba": bodies.sun.core_rgba,
                "edge_rgba": bodies.sun.edge_rgba,
                "corona": None
                if corona is None
                else freeze_json_mapping(
                    {
                        "center": (
                            corona.screen_x,
                            float(height) - corona.screen_y,
                        ),
                        "radius_px": corona.radius_px,
                        "strength": corona.strength,
                        "orientation_deg": -corona.orientation_deg,
                        "vertical_scale": corona.vertical_scale,
                        "streamers": corona.streamers,
                    }
                ),
                "pick": pick_for("sun"),
            }
        )
    if bodies.moon is not None:
        moon = bodies.moon
        payload["moon"] = freeze_json_mapping(
            {
                "disc": _disc_payload(moon.geometry, height),
                "physical_sun_disc": None
                if moon.physical_sun_geometry is None
                else _disc_payload(moon.physical_sun_geometry, height),
                "illumination": moon.illumination,
                "visibility_alpha": moon.visibility_alpha,
                "eclipsing": moon.eclipsing,
                "night": moon.night,
                "rotation_deg": -moon.rotation_deg,
                "lit_outline": tuple(
                    (float(x), float(height) - float(y))
                    for x, y in moon.lit_outline
                ),
                "pick": pick_for("moon"),
            }
        )
    payload["planets"] = tuple(
        freeze_json_mapping(
            {
                "key": planet.key,
                "center": (
                    planet.screen_x,
                    float(height) - planet.screen_y,
                ),
                "radius_px": planet.radius_px,
                "rgba": planet.rgba,
                "pick": pick_for(planet.key),
            }
        )
        for planet in bodies.planets
    )
    if (
        payload["sun"] is None
        and payload["moon"] is None
        and not payload["planets"]
    ):
        return None
    return freeze_json_mapping(payload)


def _celestial_milkyway_primitive(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> Mapping[str, JSONValue] | None:
    celestial = bundle.celestial
    if celestial is None:
        return None
    plan = celestial.milkyway
    if not plan.visible or plan.rgba is None:
        return None
    rgba = np.asarray(plan.rgba, dtype=np.uint8)
    version = _transport_version(
        "milkyway", (plan.cache_key, plan.texture_version, plan.dust_version)
    )
    payload: dict[str, JSONValue] = {
        "kind": PRIMITIVE_MILKYWAY_MESH,
        "primitive_id": "celestial:milkyway",
        "version": version,
        "layer_order": 20,
        "width": int(rgba.shape[1]),
        "height": int(rgba.shape[0]),
        "opacity": float(plan.effective_opacity),
        "blend_mode": plan.blend_mode,
    }
    _append_array_buffer(
        payload,
        resources,
        name="rgba",
        resource=_array_resource(
            resource_id="celestial:milkyway:rgba",
            version=version,
            kind="milkyway_rgba",
            values=rgba,
            components=4,
            dtype="uint8",
            cadence="static",
        ),
    )
    return freeze_json_mapping(payload)


def _celestial_deep_sky_primitive(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> Mapping[str, JSONValue] | None:
    celestial = bundle.celestial
    if celestial is None or not celestial.deep_sky.glyphs:
        return None
    glyphs = celestial.deep_sky.glyphs
    total = len(glyphs)
    height = bundle.frame.viewport.height
    positions = np.asarray(
        [(glyph.screen_x, float(height) - glyph.screen_y) for glyph in glyphs],
        dtype=np.float32,
    )
    radii = np.asarray(
        [(glyph.radius_x_px, glyph.radius_y_px) for glyph in glyphs],
        dtype=np.float32,
    )
    rotations = np.asarray(
        [-glyph.rotation_deg for glyph in glyphs], dtype=np.float32
    )
    colours = np.asarray([glyph.rgba for glyph in glyphs], dtype=np.uint8)
    flags = np.asarray(
        [(glyph.draw_cross, glyph.draw_ticks) for glyph in glyphs],
        dtype=np.uint8,
    )
    picks_by_name = {
        record.name: record for record in celestial.deep_sky.picks
    }
    version = f"deep-sky-{celestial.deep_sky.catalog_version}-frame-{bundle.generation}"
    payload: dict[str, JSONValue] = {
        "kind": "deep_sky_batch",
        "primitive_id": "celestial:deep-sky",
        "version": version,
        "layer_order": 60,
        "pick_records": tuple(
            freeze_json_mapping(
                {
                    "object_id": f"ngc:{glyph.name}",
                    "object_kind": "ngc",
                    "key": glyph.name,
                    "name": glyph.name,
                    "alt": pick.altitude_deg,
                    "az": pick.azimuth_deg,
                    "metadata": freeze_json_mapping(
                        {
                            "right_ascension_deg": pick.right_ascension_deg,
                            "declination_deg": pick.declination_deg,
                        }
                    ),
                }
            )
            if (pick := picks_by_name.get(glyph.name)) is not None
            else freeze_json_mapping(
                {
                    "object_id": f"ngc:{glyph.name}",
                    "object_kind": "ngc",
                    "key": glyph.name,
                    "name": glyph.name,
                    "metadata": freeze_json_mapping({}),
                }
            )
            for glyph in glyphs
        ),
    }
    for name, values, components, dtype in (
        ("positions", positions, 2, "float32"),
        ("radii", radii, 2, "float32"),
        ("rotations", rotations, 1, "float32"),
        ("colors", colours, 4, "uint8"),
        ("flags", flags, 2, "uint8"),
    ):
        _append_array_buffer(
            payload,
            resources,
            name=name,
            resource=_array_resource(
                resource_id=f"celestial:deep-sky:{name}",
                version=version,
                kind=name,
                values=values,
                components=components,
                dtype=dtype,  # type: ignore[arg-type]
                cadence="frame",
            ),
        )
    if total == 0:  # pragma: no cover - guarded above.
        return None
    return freeze_json_mapping(payload)


def _line_segment_arrays(
    paths: tuple[tuple[tuple[float, float], ...], ...],
    height: int,
) -> np.ndarray:
    segments = [
        (point[0], float(height) - point[1], 0.0)
        for path in paths
        for first, second in zip(path, path[1:])
        for point in (first, second)
    ]
    return np.asarray(segments, dtype=np.float32).reshape(-1, 3)


def _grid_primitive(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> Mapping[str, JSONValue] | None:
    overlays = bundle.overlays
    if overlays is None or not overlays.grid.visible:
        return None
    positions = _line_segment_arrays(
        overlays.grid.paths, bundle.frame.viewport.height
    )
    if not len(positions):
        return None
    version = f"grid-frame-{bundle.generation}"
    payload: dict[str, JSONValue] = {
        "kind": PRIMITIVE_GRID_LINES,
        "primitive_id": "celestial:grid",
        "version": version,
        "layer_order": 70,
        "rgba": overlays.grid.style.rgba,
        "width_px": overlays.grid.style.width_px,
        "dashed": overlays.grid.style.dashed,
    }
    _append_array_buffer(
        payload,
        resources,
        name="positions",
        resource=_array_resource(
            resource_id="celestial:grid:positions",
            version=version,
            kind="positions",
            values=positions,
            components=3,
            dtype="float32",
            cadence="frame",
        ),
    )
    return freeze_json_mapping(payload)


def _trail_primitive(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> Mapping[str, JSONValue] | None:
    celestial = bundle.celestial
    if celestial is None or celestial.trails is None or celestial.trails.empty:
        return None
    trail = celestial.trails
    height = bundle.frame.viewport.height
    jump_limit = max(bundle.frame.viewport.width, height) * 0.42
    positions: list[tuple[float, float, float]] = []
    colours: list[tuple[int, int, int, int]] = []
    for row in range(trail.screen_x.shape[0]):
        previous: tuple[float, float] | None = None
        colour: tuple[int, int, int, int] = (
            int(trail.rgb[row][0]),
            int(trail.rgb[row][1]),
            int(trail.rgb[row][2]),
            int(trail.alpha),
        )
        for column in range(trail.screen_x.shape[1]):
            if not bool(trail.valid[row, column]):
                previous = None
                continue
            current = (
                float(trail.screen_x[row, column]),
                float(trail.screen_y[row, column]),
            )
            if (
                previous is not None
                and math.hypot(
                    current[0] - previous[0], current[1] - previous[1]
                )
                <= jump_limit
            ):
                positions.extend(
                    (
                        (previous[0], float(height) - previous[1], 0.0),
                        (current[0], float(height) - current[1], 0.0),
                    )
                )
                colours.extend((colour, colour))
            previous = current
    if not positions:
        return None
    version = _transport_version("trails", trail.cache_key)
    payload: dict[str, JSONValue] = {
        "kind": "trail_lines",
        "primitive_id": "celestial:trails",
        "version": version,
        "layer_order": 30,
    }
    for name, values, components, dtype in (
        ("positions", np.asarray(positions, dtype=np.float32), 3, "float32"),
        ("colors", np.asarray(colours, dtype=np.uint8), 4, "uint8"),
    ):
        _append_array_buffer(
            payload,
            resources,
            name=name,
            resource=_array_resource(
                resource_id=f"celestial:trails:{name}",
                version=version,
                kind=name,
                values=values,
                components=components,
                dtype=dtype,  # type: ignore[arg-type]
                cadence="static",
            ),
        )
    return freeze_json_mapping(payload)


@dataclass(frozen=True, slots=True)
class _ResolvedTextEntry:
    """Presentation-ready text copied from a resolved overlay plan."""

    text: str
    bounds: ScreenRect
    baseline: tuple[float, float]
    style: TextStyle
    clip: ScreenRect | None


@dataclass(frozen=True, slots=True)
class _ResolvedRectEntry:
    """Presentation-ready filled rectangle copied from a resolved overlay plan."""

    bounds: ScreenRect
    fill_rgba: tuple[int, int, int, int]
    stroke: StrokeStyle | None
    corner_radius_px: float


def _screen_bounds_array(
    bounds: ScreenRect, height: int
) -> tuple[float, float, float, float]:
    """Convert a top-left plan rectangle to the host's bottom-left convention."""

    return (
        float(bounds.x),
        float(height) - float(bounds.y + bounds.height),
        float(bounds.width),
        float(bounds.height),
    )


def _resolved_text_entry(label: TextLabel) -> _ResolvedTextEntry:
    """Preserve QPainter's pre-resolved baseline and anchor exactly."""

    baseline_x, baseline_y = label.position
    if label.anchor == "baseline_center":
        baseline_x -= (
            label.bounds.width * 0.5 - label.style.background_padding_x
        )
    return _ResolvedTextEntry(
        label.text,
        label.bounds,
        (float(baseline_x), float(baseline_y)),
        label.style,
        label.clip,
    )


def _style_payload(style: TextStyle) -> Mapping[str, JSONValue]:
    """Transmit only the already-selected visual font and colour values."""

    return freeze_json_mapping(
        {
            "family": style.font.family,
            "pixel_size": style.font.pixel_size,
            "weight": style.font.weight,
            "italic": style.font.italic,
            "foreground_rgba": style.foreground_rgba,
            "background_rgba": style.background_rgba,
            "corner_radius_px": style.corner_radius_px,
        }
    )


def _text_primitive(
    *,
    semantic: str,
    entries: tuple[_ResolvedTextEntry, ...],
    height: int,
    layer_order: int,
    resources: list[BinaryResource],
) -> Mapping[str, JSONValue] | None:
    """Pack a resolved text batch for one instanced canvas-atlas draw call."""

    if not entries:
        return None
    version = _transport_version(f"text-{semantic}", entries)
    primitive_id = f"overlay:{semantic}:text"
    payload: dict[str, JSONValue] = {
        "kind": PRIMITIVE_TEXT_BATCH,
        "primitive_id": primitive_id,
        "version": version,
        "layer_order": layer_order,
        "styles": tuple(_style_payload(entry.style) for entry in entries),
    }
    bounds = np.asarray(
        [_screen_bounds_array(entry.bounds, height) for entry in entries],
        dtype=np.float32,
    )
    baselines = _screen_positions(
        np.asarray([entry.baseline[0] for entry in entries], dtype=np.float32),
        np.asarray([entry.baseline[1] for entry in entries], dtype=np.float32),
        height,
    )[:, :2]
    clips = np.asarray(
        [
            _screen_bounds_array(entry.clip, height)
            if entry.clip is not None
            else (-1_000_000.0, -1_000_000.0, 2_000_000.0, 2_000_000.0)
            for entry in entries
        ],
        dtype=np.float32,
    )
    for name, values, components, dtype in (
        ("bounds", bounds, 4, "float32"),
        ("baselines", baselines, 2, "float32"),
        ("clips", clips, 4, "float32"),
    ):
        _append_array_buffer(
            payload,
            resources,
            name=name,
            resource=_array_resource(
                resource_id=f"{primitive_id}:{name}",
                version=version,
                kind=name,
                values=values,
                components=components,
                dtype=dtype,  # type: ignore[arg-type]
                cadence="static",
            ),
        )
    string_resource = _string_resource(
        f"{primitive_id}:strings",
        version,
        "strings",
        tuple(entry.text for entry in entries),
    )
    resources.append(string_resource)
    _buffer_reference(payload, string_resource)
    return freeze_json_mapping(payload)


def _screen_line_primitives(
    *,
    semantic: str,
    segments: tuple[LineSegment, ...],
    height: int,
    layer_order: int,
    resources: list[BinaryResource],
) -> tuple[Mapping[str, JSONValue], ...]:
    """Batch plan segments by their already-resolved visual style."""

    grouped: dict[
        tuple[tuple[int, int, int, int], float, bool], list[LineSegment]
    ] = {}
    for segment in segments:
        style = segment.style
        key = (style.rgba, float(style.width_px), bool(style.dashed))
        grouped.setdefault(key, []).append(segment)

    primitives: list[Mapping[str, JSONValue]] = []
    for index, (style_key, group) in enumerate(sorted(grouped.items())):
        rgba, width_px, dashed = style_key
        version = _transport_version(
            f"lines-{semantic}-{index}", (style_key, tuple(group))
        )
        primitive_id = f"overlay:{semantic}:lines:{index}"
        starts = _screen_positions(
            np.asarray(
                [segment.start[0] for segment in group], dtype=np.float32
            ),
            np.asarray(
                [segment.start[1] for segment in group], dtype=np.float32
            ),
            height,
        )[:, :2]
        ends = _screen_positions(
            np.asarray(
                [segment.end[0] for segment in group], dtype=np.float32
            ),
            np.asarray(
                [segment.end[1] for segment in group], dtype=np.float32
            ),
            height,
        )[:, :2]
        payload: dict[str, JSONValue] = {
            "kind": PRIMITIVE_SCREEN_LINES,
            "primitive_id": primitive_id,
            "version": version,
            "layer_order": layer_order,
            "rgba": rgba,
            "width_px": width_px,
            "dashed": dashed,
        }
        if semantic.startswith("measurements"):
            payload["pick_kind"] = "measurement"
            payload["pick_priority"] = 120
        elif semantic.startswith("constellations"):
            payload["pick_kind"] = "constellation"
            payload["pick_priority"] = 110
        elif semantic.startswith("scope"):
            payload["pick_kind"] = "scope"
            payload["pick_priority"] = 100
        for name, values in (("starts", starts), ("ends", ends)):
            _append_array_buffer(
                payload,
                resources,
                name=name,
                resource=_array_resource(
                    resource_id=f"{primitive_id}:{name}",
                    version=version,
                    kind=name,
                    values=values,
                    components=2,
                    dtype="float32",
                    cadence="static",
                ),
            )
        primitives.append(freeze_json_mapping(payload))
    return tuple(primitives)


def _circle_primitive(
    *,
    semantic: str,
    circles: tuple[CirclePrimitive, ...],
    height: int,
    layer_order: int,
    resources: list[BinaryResource],
) -> Mapping[str, JSONValue] | None:
    """Batch plan circles as GPU point sprites with resolved fill and outline."""

    if not circles:
        return None
    version = _transport_version(f"circles-{semantic}", circles)
    primitive_id = f"overlay:{semantic}:circles"
    positions = _screen_positions(
        np.asarray([circle.center[0] for circle in circles], dtype=np.float32),
        np.asarray([circle.center[1] for circle in circles], dtype=np.float32),
        height,
    )
    fills = np.asarray(
        [circle.fill_rgba or (0, 0, 0, 0) for circle in circles],
        dtype=np.uint8,
    )
    strokes = np.asarray(
        [
            circle.stroke.rgba if circle.stroke is not None else (0, 0, 0, 0)
            for circle in circles
        ],
        dtype=np.uint8,
    )
    radii = np.asarray(
        [circle.radius_px for circle in circles], dtype=np.float32
    )
    stroke_widths = np.asarray(
        [
            circle.stroke.width_px if circle.stroke is not None else 0.0
            for circle in circles
        ],
        dtype=np.float32,
    )
    dashed = np.asarray(
        [
            1 if circle.stroke is not None and circle.stroke.dashed else 0
            for circle in circles
        ],
        dtype=np.uint8,
    )
    payload: dict[str, JSONValue] = {
        "kind": PRIMITIVE_SCREEN_CIRCLES,
        "primitive_id": primitive_id,
        "version": version,
        "layer_order": layer_order,
    }
    if semantic.startswith("measurement"):
        payload["pick_kind"] = "measurement"
        payload["pick_priority"] = 120
    elif semantic.startswith("constellations"):
        payload["pick_kind"] = "constellation"
        payload["pick_priority"] = 110
    for name, values, components, dtype in (
        ("positions", positions, 3, "float32"),
        ("fills", fills, 4, "uint8"),
        ("strokes", strokes, 4, "uint8"),
        ("radii", radii, 1, "float32"),
        ("stroke_widths", stroke_widths, 1, "float32"),
        ("dashed", dashed, 1, "uint8"),
    ):
        _append_array_buffer(
            payload,
            resources,
            name=name,
            resource=_array_resource(
                resource_id=f"{primitive_id}:{name}",
                version=version,
                kind=name,
                values=values,
                components=components,
                dtype=dtype,  # type: ignore[arg-type]
                cadence="static",
            ),
        )
    return freeze_json_mapping(payload)


def _rect_primitive(
    *,
    semantic: str,
    rects: tuple[_ResolvedRectEntry, ...],
    height: int,
    layer_order: int,
    resources: list[BinaryResource],
) -> Mapping[str, JSONValue] | None:
    """Batch resolved HUD and measurement boxes without relaying layout to JS."""

    if not rects:
        return None
    version = _transport_version(f"rects-{semantic}", rects)
    primitive_id = f"overlay:{semantic}:rects"
    bounds = np.asarray(
        [_screen_bounds_array(rect.bounds, height) for rect in rects],
        dtype=np.float32,
    )
    fills = np.asarray([rect.fill_rgba for rect in rects], dtype=np.uint8)
    strokes = np.asarray(
        [
            rect.stroke.rgba if rect.stroke is not None else (0, 0, 0, 0)
            for rect in rects
        ],
        dtype=np.uint8,
    )
    stroke_widths = np.asarray(
        [
            rect.stroke.width_px if rect.stroke is not None else 0.0
            for rect in rects
        ],
        dtype=np.float32,
    )
    corner_radii = np.asarray(
        [rect.corner_radius_px for rect in rects], dtype=np.float32
    )
    payload: dict[str, JSONValue] = {
        "kind": PRIMITIVE_SCREEN_RECTS,
        "primitive_id": primitive_id,
        "version": version,
        "layer_order": layer_order,
    }
    for name, values, components, dtype in (
        ("bounds", bounds, 4, "float32"),
        ("fills", fills, 4, "uint8"),
        ("strokes", strokes, 4, "uint8"),
        ("stroke_widths", stroke_widths, 1, "float32"),
        ("corner_radii", corner_radii, 1, "float32"),
    ):
        _append_array_buffer(
            payload,
            resources,
            name=name,
            resource=_array_resource(
                resource_id=f"{primitive_id}:{name}",
                version=version,
                kind=name,
                values=values,
                components=components,
                dtype=dtype,  # type: ignore[arg-type]
                cadence="static",
            ),
        )
    return freeze_json_mapping(payload)


def _text_entries_from_measurement(
    label: ScreenMeasurementLabel,
) -> tuple[_ResolvedTextEntry, ...]:
    """Use the model-provided baselines for every line of a measurement label."""

    line_step = label.bounds.height / max(1, len(label.text))
    return tuple(
        _ResolvedTextEntry(
            line,
            label.bounds,
            (label.baseline[0], label.baseline[1] + index * line_step),
            label.style,
            label.bounds,
        )
        for index, line in enumerate(label.text)
    )


def _measurement_segments(
    item: ScreenMeasurementItem,
    style: StrokeStyle,
) -> tuple[LineSegment, ...]:
    return tuple(
        LineSegment(first, second, style)
        for path in item.paths
        for first, second in zip(path, path[1:])
    )


def _compass_primitives(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> tuple[Mapping[str, JSONValue], ...]:
    overlays = bundle.overlays
    if overlays is None:
        return ()
    height = bundle.frame.viewport.height
    return (
        *_screen_line_primitives(
            semantic="compass",
            segments=overlays.compass.ticks,
            height=height,
            layer_order=80,
            resources=resources,
        ),
        *(
            primitive
            for primitive in (
                _text_primitive(
                    semantic="compass",
                    entries=tuple(
                        _resolved_text_entry(label)
                        for label in overlays.compass.labels.labels
                    ),
                    height=height,
                    layer_order=81,
                    resources=resources,
                ),
            )
            if primitive is not None
        ),
    )


def _label_primitives(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> tuple[Mapping[str, JSONValue], ...]:
    overlays = bundle.overlays
    if overlays is None:
        return ()
    primitive = _text_primitive(
        semantic="labels",
        entries=tuple(
            _resolved_text_entry(label) for label in overlays.labels.labels
        ),
        height=bundle.frame.viewport.height,
        layer_order=85,
        resources=resources,
    )
    return () if primitive is None else (primitive,)


def _scope_primitives(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> tuple[Mapping[str, JSONValue], ...]:
    overlays = bundle.overlays
    if overlays is None:
        return ()
    scope = overlays.scope
    center_value = scope.center
    if not scope.enabled or center_value is None:
        return ()
    height = bundle.frame.viewport.height
    center = _screen_positions(
        np.asarray([center_value[0]], dtype=np.float32),
        np.asarray([center_value[1]], dtype=np.float32),
        height,
    )[0, :2]
    version = _transport_version("scope", scope)
    payload = freeze_json_mapping(
        {
            "kind": PRIMITIVE_SCOPE_MASK,
            "primitive_id": "overlay:scope:mask",
            "version": version,
            "layer_order": 90,
            "shape": scope.shape,
            "center": (float(center[0]), float(center[1])),
            "radius_px": scope.radius_px,
            "rect_size_px": scope.rect_size_px,
            "mask_rgba": scope.mask_rgba,
            "outline_rgba": scope.outline.rgba,
            "outline_width_px": scope.outline.width_px,
            "pick": freeze_json_mapping(
                {
                    "object_id": "scope:mask",
                    "object_kind": "scope",
                    "metadata": freeze_json_mapping({"shape": scope.shape}),
                }
            ),
        }
    )
    primitives: list[Mapping[str, JSONValue]] = [payload]
    primitives.extend(
        _screen_line_primitives(
            semantic="scope-reticle",
            segments=scope.crosshair,
            height=height,
            layer_order=92,
            resources=resources,
        )
    )
    if scope.readout is not None:
        text = _text_primitive(
            semantic="scope-readout",
            entries=tuple(
                _resolved_text_entry(label) for label in scope.readout.labels
            ),
            height=height,
            layer_order=93,
            resources=resources,
        )
        if text is not None:
            primitives.append(text)
    return tuple(primitives)


def _selection_primitives(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> tuple[Mapping[str, JSONValue], ...]:
    overlays = bundle.overlays
    if overlays is None:
        return ()
    selection = overlays.selection
    screen_x, screen_y = selection.screen_x, selection.screen_y
    if selection.selected_kind is None or screen_x is None or screen_y is None:
        return ()
    center = (float(screen_x), float(screen_y))
    alpha = max(40, min(240, int(255.0 * selection.pulse_alpha)))
    circles = (
        CirclePrimitive(
            center,
            float(selection.pulse_radius_px),
            None,
            StrokeStyle((216, 178, 106, alpha), 1.5, True),
        ),
        CirclePrimitive(
            center,
            5.0,
            None,
            StrokeStyle((255, 240, 150, min(255, alpha + 40)), 1.2),
        ),
    )
    radius = float(selection.pulse_radius_px)
    lines = (
        LineSegment(
            (center[0] - radius - 2.0, center[1]),
            (center[0] - 6.0, center[1]),
            StrokeStyle((255, 240, 150, min(255, alpha + 40)), 1.2),
        ),
        LineSegment(
            (center[0] + 6.0, center[1]),
            (center[0] + radius + 2.0, center[1]),
            StrokeStyle((255, 240, 150, min(255, alpha + 40)), 1.2),
        ),
        LineSegment(
            (center[0], center[1] - radius - 2.0),
            (center[0], center[1] - 6.0),
            StrokeStyle((255, 240, 150, min(255, alpha + 40)), 1.2),
        ),
        LineSegment(
            (center[0], center[1] + 6.0),
            (center[0], center[1] + radius + 2.0),
            StrokeStyle((255, 240, 150, min(255, alpha + 40)), 1.2),
        ),
    )
    primitives: list[Mapping[str, JSONValue]] = list(
        _screen_line_primitives(
            semantic="selection",
            segments=lines,
            height=bundle.frame.viewport.height,
            layer_order=100,
            resources=resources,
        )
    )
    circles_primitive = _circle_primitive(
        semantic="selection",
        circles=circles,
        height=bundle.frame.viewport.height,
        layer_order=100,
        resources=resources,
    )
    if circles_primitive is not None:
        primitives.append(circles_primitive)
    return tuple(primitives)


def _measurement_primitives(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> tuple[Mapping[str, JSONValue], ...]:
    overlays = bundle.overlays
    if overlays is None:
        return ()
    items = overlays.measurements.items + (
        (overlays.measurements.preview,)
        if overlays.measurements.preview is not None
        else ()
    )
    if not items:
        return ()
    height = bundle.frame.viewport.height
    glow_segments = tuple(
        segment
        for item in items
        for segment in _measurement_segments(item, item.glow_style)
    )
    line_segments = tuple(
        segment
        for item in items
        for segment in _measurement_segments(item, item.line_style)
    )
    circles = tuple(
        CirclePrimitive(
            point,
            item.handle_radius_px,
            (0, 0, 0, 200),
            StrokeStyle((255, 240, 120, 220), 1.1),
        )
        for item in items
        for point in item.handles
    )
    boxes = tuple(
        _ResolvedRectEntry(
            item.label.bounds,
            item.label.background_rgba,
            None,
            item.label.corner_radius_px,
        )
        for item in items
        if item.label is not None
    )
    text_entries = tuple(
        entry
        for item in items
        if item.label is not None
        for entry in _text_entries_from_measurement(item.label)
    )
    primitives: list[Mapping[str, JSONValue]] = []
    primitives.extend(
        _screen_line_primitives(
            semantic="measurements-glow",
            segments=glow_segments,
            height=height,
            layer_order=104,
            resources=resources,
        )
    )
    primitives.extend(
        _screen_line_primitives(
            semantic="measurements",
            segments=line_segments,
            height=height,
            layer_order=105,
            resources=resources,
        )
    )
    circle_primitive = _circle_primitive(
        semantic="measurement-handles",
        circles=circles,
        height=height,
        layer_order=106,
        resources=resources,
    )
    if circle_primitive is not None:
        primitives.append(circle_primitive)
    rect_primitive = _rect_primitive(
        semantic="measurement-labels",
        rects=boxes,
        height=height,
        layer_order=107,
        resources=resources,
    )
    if rect_primitive is not None:
        primitives.append(rect_primitive)
    text_primitive = _text_primitive(
        semantic="measurement-labels",
        entries=text_entries,
        height=height,
        layer_order=108,
        resources=resources,
    )
    if text_primitive is not None:
        primitives.append(text_primitive)
    return tuple(primitives)


def _constellation_primitives(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> tuple[Mapping[str, JSONValue], ...]:
    overlays = bundle.overlays
    if overlays is None or not overlays.constellations.visible:
        return ()
    constellation = overlays.constellations
    height = bundle.frame.viewport.height
    segments = constellation.segments + (
        (constellation.preview,) if constellation.preview is not None else ()
    )
    primitives: list[Mapping[str, JSONValue]] = list(
        _screen_line_primitives(
            semantic="constellations",
            segments=segments,
            height=height,
            layer_order=110,
            resources=resources,
        )
    )
    circles = _circle_primitive(
        semantic="constellations",
        circles=constellation.nodes,
        height=height,
        layer_order=111,
        resources=resources,
    )
    if circles is not None:
        primitives.append(circles)
    if constellation.labels is not None:
        text = _text_primitive(
            semantic="constellations",
            entries=tuple(
                _resolved_text_entry(label)
                for label in constellation.labels.labels
            ),
            height=height,
            layer_order=112,
            resources=resources,
        )
        if text is not None:
            primitives.append(text)
    return tuple(primitives)


def _hud_primitives(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> tuple[Mapping[str, JSONValue], ...]:
    overlays = bundle.overlays
    if overlays is None or not overlays.hud.visible:
        return ()
    hud = overlays.hud
    height = bundle.frame.viewport.height
    primitives: list[Mapping[str, JSONValue]] = []
    rect = _rect_primitive(
        semantic="hud",
        rects=(
            _ResolvedRectEntry(
                hud.bounds,
                hud.background_rgba,
                hud.border,
                hud.corner_radius_px,
            ),
        ),
        height=height,
        layer_order=130,
        resources=resources,
    )
    if rect is not None:
        primitives.append(rect)
    text = _text_primitive(
        semantic="hud",
        entries=tuple(
            _resolved_text_entry(label) for label in hud.labels.labels
        ),
        height=height,
        layer_order=131,
        resources=resources,
    )
    if text is not None:
        primitives.append(text)
    return tuple(primitives)


def _interaction_affordance_primitive(
    bundle: RenderPlanBundle,
) -> Mapping[str, JSONValue] | None:
    """Expose model-resolved hit affordances without inventing interaction policy."""

    affordances = bundle.interaction.affordances
    if not affordances:
        return None
    return freeze_json_mapping(
        {
            "kind": PRIMITIVE_INTERACTION_AFFORDANCE,
            "primitive_id": "overlay:interaction-affordances",
            "version": _transport_version("interaction", affordances),
            "layer_order": 120,
            "affordances": tuple(
                freeze_json_mapping(
                    {
                        "affordance_id": affordance.affordance_id,
                        "action": affordance.action,
                        "bounds": (
                            affordance.bounds.minimum,
                            affordance.bounds.maximum,
                        ),
                        "active": affordance.active,
                    }
                )
                for affordance in affordances
            ),
        }
    )


def _terrain_mesh_primitive(
    mesh: TerrainMeshResource,
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> Mapping[str, JSONValue]:
    """Pack one CPU-resolved terrain mesh as persistent GPU attributes.

    The helper copies buffers only at the presentation boundary.  Tile
    membership, colours, category identities, normals and texture versions
    are supplied by the neutral plan; this module does not inspect terrain
    sources or derive any scientific state.
    """

    texture = mesh.texture or mesh.material.texture
    texture_resource = next(
        (
            resource
            for resource in bundle.resources.textures
            if texture is not None
            and resource.texture_id == texture.texture_id
            and resource.version == texture.version
        ),
        None,
    )
    coordinate_space = (
        "terrain_direction" if mesh.view_vertices else "screen_resolved"
    )
    positions = (
        np.asarray(mesh.view_vertices, dtype=np.float32)
        if mesh.view_vertices
        else _terrain_screen_positions(
            mesh.vertices, bundle.frame.viewport.height
        )
    )
    rgba = np.clip(
        np.rint(np.asarray(mesh.surface.colors, dtype=np.float32) * 255.0),
        0.0,
        255.0,
    ).astype(np.uint8)
    class_ids = np.asarray(mesh.surface.class_ids, dtype=np.int64)
    class_ids = np.where(
        class_ids < 0,
        np.iinfo(np.uint32).max,
        class_ids,
    ).astype(np.uint32)
    payload: dict[str, JSONValue] = {
        "kind": PRIMITIVE_TERRAIN_MESH,
        "primitive_id": mesh.primitive_id,
        "version": f"{mesh.geometry_version}:{mesh.material_version}",
        # QPainter renders terrain above the celestial scene and below the
        # model-resolved overlays.  The Three.js scene uses the same order.
        "layer_order": 60,
        "geometry_version": mesh.geometry_version,
        "material_version": mesh.material_version,
        "coordinate_space": coordinate_space,
        "material": _material_payload(mesh.material),
        "texture": _texture_parameters_payload(texture),
        "texture_resource": (
            _texture_resource_payload(texture_resource)
            if texture_resource is not None
            else None
        ),
        "tiles": tuple(
            freeze_json_mapping(
                {
                    "tile_id": tile.tile_id,
                    "first_index": tile.first_index,
                    "index_count": tile.index_count,
                    "bounds": _bounds_payload(tile.bounds),
                }
            )
            for tile in mesh.tiles
        ),
        "pick": freeze_json_mapping(
            {
                "object_kind": "terrain",
                "primitive_id": mesh.primitive_id,
            }
        ),
    }
    for name, values, components, dtype, version in (
        ("positions", positions, 3, "float32", mesh.geometry_version),
        (
            "pick_positions",
            _terrain_screen_positions(
                mesh.vertices, bundle.frame.viewport.height
            ),
            3,
            "float32",
            mesh.geometry_version,
        ),
        (
            "indices",
            np.asarray(mesh.indices, dtype=np.uint32),
            1,
            "uint32",
            mesh.geometry_version,
        ),
        (
            "normals",
            np.asarray(mesh.normals, dtype=np.float32),
            3,
            "float32",
            mesh.geometry_version,
        ),
        ("colors", rgba, 4, "uint8", mesh.material_version),
        (
            "alphas",
            np.asarray(mesh.surface.alphas, dtype=np.float32),
            1,
            "float32",
            mesh.material_version,
        ),
        (
            "uv",
            np.asarray(mesh.surface.uv, dtype=np.float32),
            2,
            "float32",
            mesh.material_version,
        ),
        (
            "elevations_m",
            np.asarray(mesh.surface.elevations_m, dtype=np.float32),
            1,
            "float32",
            mesh.material_version,
        ),
        ("class_ids", class_ids, 1, "uint32", mesh.material_version),
        (
            "categorical_flags",
            np.asarray(mesh.surface.categorical_flags, dtype=np.uint8),
            1,
            "uint8",
            mesh.material_version,
        ),
    ):
        _append_array_buffer(
            payload,
            resources,
            name=name,
            resource=_array_resource(
                resource_id=f"{mesh.primitive_id}:{name}",
                version=version,
                kind=name,
                values=values,
                components=components,
                dtype=dtype,  # type: ignore[arg-type]
                cadence="static",
            ),
        )
    return freeze_json_mapping(payload)


def _terrain_screen_positions(
    vertices: tuple[tuple[float, float, float], ...], height: int
) -> np.ndarray:
    """Convert the already-resolved QPainter convention to WebGL y-up."""

    positions = np.asarray(vertices, dtype=np.float32).copy()
    positions[:, 1] = float(height) - positions[:, 1]
    return positions


def _terrain_primitives(
    bundle: RenderPlanBundle,
    resources: list[BinaryResource],
) -> tuple[Mapping[str, JSONValue], ...]:
    return tuple(
        _terrain_mesh_primitive(primitive, bundle, resources)
        for plan in bundle.plans
        for primitive in plan.primitives
        if isinstance(primitive, TerrainMeshResource)
    )


def prepare_celestial_plan_bundle(
    bundle: RenderPlanBundle,
) -> PreparedBridgeFrame:
    """Prepare the already-resolved celestial, overlay and terrain scene.

    This adapter intentionally reads only immutable Model plans.  It packs
    resolved screen-space attributes, terrain tiles, masks and pre-sampled
    imagery into typed bridge resources; it does not consult Skyfield,
    catalogues, Bortle, magnitude rules, GeoTIFF data, or terrain-selection
    kernels.
    """

    resources: list[BinaryResource] = []
    primitive_builders = (
        lambda: (_celestial_sky_primitive(bundle, resources),),
        lambda: (_celestial_milkyway_primitive(bundle, resources),),
        lambda: (_trail_primitive(bundle, resources),),
        lambda: (_celestial_star_primitive(bundle, resources),),
        lambda: (_celestial_body_primitive(bundle),),
        lambda: (_celestial_deep_sky_primitive(bundle, resources),),
        lambda: _terrain_primitives(bundle, resources),
        lambda: (_grid_primitive(bundle, resources),),
        lambda: _compass_primitives(bundle, resources),
        lambda: _label_primitives(bundle, resources),
        lambda: _scope_primitives(bundle, resources),
        lambda: _selection_primitives(bundle, resources),
        lambda: _measurement_primitives(bundle, resources),
        lambda: _constellation_primitives(bundle, resources),
        lambda: (_interaction_affordance_primitive(bundle),),
        lambda: _hud_primitives(bundle, resources),
    )
    primitives = tuple(
        primitive
        for build in primitive_builders
        for primitive in build()
        if primitive is not None
    )
    manifest = freeze_json_mapping(
        {
            "schema_version": bundle.schema_version,
            "generation": bundle.generation,
            "viewport": freeze_json_mapping(
                {
                    "width": bundle.frame.viewport.width,
                    "height": bundle.frame.viewport.height,
                    "dpr": bundle.frame.viewport.device_pixel_ratio,
                }
            ),
            "terrain_view": freeze_json_mapping(
                {
                    "azimuth_deg": bundle.frame.camera.azimuth,
                    "zoom": bundle.frame.camera.zoom,
                    "elevation_deg": bundle.frame.camera.elevation,
                    "vertical_ratio": bundle.frame.camera.vertical_ratio,
                }
            ),
            "primitives": primitives,
        }
    )
    return PreparedBridgeFrame(bundle.generation, manifest, tuple(resources))


def build_bridge_message(
    op: str,
    payload: Mapping[str, Any],
    *,
    generation: int = 0,
    seq: int = 0,
) -> Mapping[str, JSONValue]:
    """Construct a validated, frozen bridge protocol message."""
    if op not in ALLOWED_OPERATIONS:
        raise ValueError(
            f"Operation {op!r} is not allowed in Three.js bridge protocol"
        )
    msg: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "op": op,
        "gen": int(generation),
        "seq": int(seq),
        "payload": payload,
    }
    return freeze_json_mapping(msg)


def parse_bridge_message(
    raw_json: str | bytes | Mapping[str, Any],
) -> Mapping[str, JSONValue]:
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

    if op == OP_PICK_RESULT:
        _validate_pick_result_message(data, payload)

    return freeze_json_mapping(data)


def _validate_pick_result_message(
    message: Mapping[str, Any], payload: Mapping[str, Any]
) -> None:
    """Reject incomplete host picks before they can reach application state."""

    if not isinstance(message.get("gen"), int) or int(message["gen"]) < 0:
        raise ValueError(
            "Pick result generation must be a non-negative integer"
        )
    if (
        not isinstance(payload.get("request_id"), str)
        or not payload["request_id"]
    ):
        raise ValueError("Pick result requires a request_id")
    if not isinstance(payload.get("hit"), bool):
        raise ValueError("Pick result requires a boolean hit field")
    if not payload["hit"]:
        for name in (
            "object_id",
            "object_kind",
            "distance",
            "world_point",
            "surface_coordinates",
        ):
            if name not in payload or payload[name] is not None:
                raise ValueError(f"Miss pick result requires null {name}")
        if not isinstance(payload.get("metadata"), Mapping):
            raise ValueError("Pick result metadata must be an object")
        return
    for name in ("object_id", "object_kind"):
        if not isinstance(payload.get(name), str) or not payload[name]:
            raise ValueError(f"Hit pick result requires {name}")
    distance = payload.get("distance")
    if not isinstance(distance, (int, float)) or not math.isfinite(distance):
        raise ValueError("Hit pick result requires a finite distance")
    for name in ("world_point", "surface_coordinates", "metadata"):
        if name not in payload:
            raise ValueError(f"Hit pick result requires {name}")
    world_point = payload.get("world_point")
    if world_point is not None:
        if (
            not isinstance(world_point, (tuple, list))
            or len(world_point) != 3
            or not all(
                isinstance(value, (int, float)) and math.isfinite(value)
                for value in world_point
            )
        ):
            raise ValueError(
                "Pick result world_point must contain three finite values"
            )
    surface = payload.get("surface_coordinates")
    if surface is not None and not isinstance(surface, Mapping):
        raise ValueError("Pick result surface_coordinates must be an object")
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError("Pick result metadata must be an object")
