"""Immutable, renderer-neutral resources used to draw a resolved scene.

These contracts are intentionally data-only.  A planner owns projection,
scientific selection, terrain preparation and label layout; a renderer only
uploads or paints the values present here.  In particular, no primitive is a
``name``/``count`` manifest: every primitive carries its draw attributes,
material, bounds, versions and frame generation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypeAlias

from TerraLab.scene.contracts import SceneFrame

if TYPE_CHECKING:
    from TerraLab.scene.plans.celestial import CelestialLayerPlans
    from TerraLab.scene.plans.overlays import OverlayLayerPlans
    from TerraLab.scene.picking import PickIndex


Vector2: TypeAlias = tuple[float, float]
Vector3: TypeAlias = tuple[float, float, float]
ColorRGBA: TypeAlias = tuple[float, float, float, float]
PrimitiveKind: TypeAlias = Literal[
    "point_sprite", "star_batch", "triangle_mesh", "text", "terrain_mesh"
]


def _finite(name: str, value: float) -> float:
    resolved = float(value)
    if not math.isfinite(resolved):
        raise ValueError(f"{name} must be finite")
    return resolved


def _vector2(name: str, value: Vector2) -> Vector2:
    if len(value) != 2:
        raise ValueError(f"{name} must contain exactly two coordinates")
    return (_finite(f"{name}[0]", value[0]), _finite(f"{name}[1]", value[1]))


def _vector3(name: str, value: Vector3) -> Vector3:
    if len(value) != 3:
        raise ValueError(f"{name} must contain exactly three coordinates")
    return (
        _finite(f"{name}[0]", value[0]),
        _finite(f"{name}[1]", value[1]),
        _finite(f"{name}[2]", value[2]),
    )


def _rgba(name: str, value: ColorRGBA) -> ColorRGBA:
    if len(value) != 4:
        raise ValueError(f"{name} must contain RGBA components")
    result = tuple(
        _finite(f"{name}[{index}]", component)
        for index, component in enumerate(value)
    )
    if any(component < 0.0 or component > 1.0 for component in result):
        raise ValueError(f"{name} components must be in [0, 1]")
    return result  # type: ignore[return-value]


def _version(name: str, value: str) -> str:
    resolved = str(value)
    if not resolved:
        raise ValueError(f"{name} must not be empty")
    return resolved


def _generation(name: str, value: int) -> int:
    resolved = int(value)
    if resolved < 0:
        raise ValueError(f"{name} cannot be negative")
    return resolved


def _layer_order(name: str, value: int) -> int:
    resolved = int(value)
    if resolved < 0:
        raise ValueError(f"{name} cannot be negative")
    return resolved


def _same_length(name: str, values: tuple[object, ...], expected: int) -> None:
    if len(values) != expected:
        raise ValueError(f"{name} must contain one value per position")


@dataclass(frozen=True, slots=True)
class Bounds:
    """Axis-aligned draw bounds in renderer-neutral scene coordinates."""

    minimum: Vector3
    maximum: Vector3

    def __post_init__(self) -> None:
        minimum = _vector3("bounds.minimum", self.minimum)
        maximum = _vector3("bounds.maximum", self.maximum)
        if any(lower > upper for lower, upper in zip(minimum, maximum)):
            raise ValueError("bounds.minimum cannot exceed bounds.maximum")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    @classmethod
    def enclosing(cls, positions: tuple[Vector3, ...]) -> "Bounds":
        if not positions:
            raise ValueError("Cannot compute bounds for an empty position set")
        normalized = tuple(
            _vector3("position", position) for position in positions
        )
        return cls(
            minimum=tuple(
                min(point[axis] for point in normalized) for axis in range(3)
            ),  # type: ignore[arg-type]
            maximum=tuple(
                max(point[axis] for point in normalized) for axis in range(3)
            ),  # type: ignore[arg-type]
        )

    def contains(self, position: Vector3) -> bool:
        """Return whether a resolved draw coordinate lies within this bounds."""

        return all(
            lower <= coordinate <= upper
            for coordinate, lower, upper in zip(
                _vector3("position", position), self.minimum, self.maximum
            )
        )


@dataclass(frozen=True, slots=True)
class TextureParameters:
    """Sampling parameters for a texture already prepared by the Model/CPU."""

    texture_id: str
    version: str
    wrap_u: Literal["clamp", "repeat", "mirror"] = "clamp"
    wrap_v: Literal["clamp", "repeat", "mirror"] = "clamp"
    min_filter: Literal["nearest", "linear", "mipmap_linear"] = "linear"
    mag_filter: Literal["nearest", "linear"] = "linear"
    uv_offset: Vector2 = (0.0, 0.0)
    uv_scale: Vector2 = (1.0, 1.0)
    flip_y: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "texture_id", _version("texture_id", self.texture_id)
        )
        object.__setattr__(
            self, "version", _version("texture.version", self.version)
        )
        if self.wrap_u not in {"clamp", "repeat", "mirror"}:
            raise ValueError("texture.wrap_u is unsupported")
        if self.wrap_v not in {"clamp", "repeat", "mirror"}:
            raise ValueError("texture.wrap_v is unsupported")
        if self.min_filter not in {"nearest", "linear", "mipmap_linear"}:
            raise ValueError("texture.min_filter is unsupported")
        if self.mag_filter not in {"nearest", "linear"}:
            raise ValueError("texture.mag_filter is unsupported")
        object.__setattr__(
            self, "uv_offset", _vector2("texture.uv_offset", self.uv_offset)
        )
        scale = _vector2("texture.uv_scale", self.uv_scale)
        if scale[0] == 0.0 or scale[1] == 0.0:
            raise ValueError("texture.uv_scale components must be non-zero")
        object.__setattr__(self, "uv_scale", scale)


@dataclass(frozen=True, slots=True)
class TextureResource:
    """A versioned texture source; loading and decoding happened before draw."""

    texture_id: str
    source: str
    version: str
    width: int
    height: int
    parameters: TextureParameters
    color_space: Literal["srgb", "linear"] = "srgb"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "texture_id", _version("texture_id", self.texture_id)
        )
        object.__setattr__(
            self, "source", _version("texture.source", self.source)
        )
        object.__setattr__(
            self, "version", _version("texture.version", self.version)
        )
        if int(self.width) < 1 or int(self.height) < 1:
            raise ValueError("Texture dimensions must be positive")
        if self.parameters.texture_id != self.texture_id:
            raise ValueError(
                "Texture parameters must reference their texture resource"
            )
        if self.parameters.version != self.version:
            raise ValueError(
                "Texture parameters must use the texture resource version"
            )
        if self.color_space not in {"srgb", "linear"}:
            raise ValueError("Texture color space is unsupported")


@dataclass(frozen=True, slots=True)
class ArtifactResource:
    """A CPU-prepared external artifact referenced by a resolved scene."""

    resource_id: str
    kind: str
    source: str
    version: str
    handle: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "resource_id", _version("resource_id", self.resource_id)
        )
        object.__setattr__(self, "kind", _version("resource.kind", self.kind))
        object.__setattr__(
            self, "source", _version("resource.source", self.source)
        )
        object.__setattr__(
            self, "version", _version("resource.version", self.version)
        )


@dataclass(frozen=True, slots=True)
class MaterialParameters:
    """Fully resolved visual material parameters, without renderer objects."""

    material_id: str
    color: ColorRGBA = (1.0, 1.0, 1.0, 1.0)
    opacity: float = 1.0
    point_size: float = 1.0
    blend_mode: Literal["opaque", "alpha", "add", "multiply"] = "alpha"
    depth_test: bool = True
    depth_write: bool = True
    texture: TextureParameters | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "material_id", _version("material_id", self.material_id)
        )
        object.__setattr__(self, "color", _rgba("material.color", self.color))
        opacity = _finite("material.opacity", self.opacity)
        if not 0.0 <= opacity <= 1.0:
            raise ValueError("material.opacity must be in [0, 1]")
        object.__setattr__(self, "opacity", opacity)
        point_size = _finite("material.point_size", self.point_size)
        if point_size <= 0.0:
            raise ValueError("material.point_size must be positive")
        object.__setattr__(self, "point_size", point_size)
        if self.blend_mode not in {"opaque", "alpha", "add", "multiply"}:
            raise ValueError("material.blend_mode is unsupported")


@dataclass(frozen=True, slots=True)
class SpriteBatch:
    """A drawable point/sprite resource with all per-point attributes."""

    primitive_id: str
    positions: tuple[Vector3, ...]
    colors: tuple[ColorRGBA, ...]
    sizes: tuple[float, ...]
    alphas: tuple[float, ...]
    object_ids: tuple[str, ...]
    layer_order: int
    material: MaterialParameters
    bounds: Bounds
    version: str
    frame_generation: int
    uv: tuple[Vector2, ...] = ()
    normals: tuple[Vector3, ...] = ()
    texture: TextureParameters | None = None

    kind: PrimitiveKind = field(default="point_sprite", init=False)

    def __post_init__(self) -> None:
        positions = tuple(
            _vector3("sprite.position", value) for value in self.positions
        )
        if not positions:
            raise ValueError("Sprite batches require at least one position")
        colors = tuple(_rgba("sprite.color", value) for value in self.colors)
        sizes = tuple(_finite("sprite.size", value) for value in self.sizes)
        alphas = tuple(_finite("sprite.alpha", value) for value in self.alphas)
        uv = tuple(_vector2("sprite.uv", value) for value in self.uv)
        normals = tuple(
            _vector3("sprite.normal", value) for value in self.normals
        )
        object_ids = tuple(
            _version("sprite.object_id", value) for value in self.object_ids
        )
        expected = len(positions)
        _same_length("sprite.colors", colors, expected)
        _same_length("sprite.sizes", sizes, expected)
        _same_length("sprite.alphas", alphas, expected)
        _same_length("sprite.object_ids", object_ids, expected)
        if uv:
            _same_length("sprite.uv", uv, expected)
        if normals:
            _same_length("sprite.normals", normals, expected)
        if any(size <= 0.0 for size in sizes):
            raise ValueError("sprite.sizes must be positive")
        if any(alpha < 0.0 or alpha > 1.0 for alpha in alphas):
            raise ValueError("sprite.alphas must be in [0, 1]")
        object.__setattr__(
            self,
            "primitive_id",
            _version("sprite.primitive_id", self.primitive_id),
        )
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "colors", colors)
        object.__setattr__(self, "sizes", sizes)
        object.__setattr__(self, "alphas", alphas)
        object.__setattr__(self, "object_ids", object_ids)
        object.__setattr__(self, "uv", uv)
        object.__setattr__(self, "normals", normals)
        if any(not self.bounds.contains(position) for position in positions):
            raise ValueError(
                "sprite.bounds must enclose every sprite position"
            )
        object.__setattr__(
            self,
            "layer_order",
            _layer_order("sprite.layer_order", self.layer_order),
        )
        object.__setattr__(
            self, "version", _version("sprite.version", self.version)
        )
        object.__setattr__(
            self,
            "frame_generation",
            _generation("sprite.frame_generation", self.frame_generation),
        )


@dataclass(frozen=True, slots=True)
class StarBatch:
    """Resolved star vertices including visual class and stable picking IDs."""

    primitive_id: str
    positions: tuple[Vector3, ...]
    radii: tuple[float, ...]
    colors: tuple[ColorRGBA, ...]
    alphas: tuple[float, ...]
    magnitudes: tuple[float, ...]
    visual_classes: tuple[str, ...]
    picking_ids: tuple[str, ...]
    object_ids: tuple[str, ...]
    layer_order: int
    material: MaterialParameters
    bounds: Bounds
    version: str
    frame_generation: int
    input_coordinates: tuple[Vector2, ...] = ()
    uv: tuple[Vector2, ...] = ()
    normals: tuple[Vector3, ...] = ()
    texture: TextureParameters | None = None

    kind: PrimitiveKind = field(default="star_batch", init=False)

    def __post_init__(self) -> None:
        positions = tuple(
            _vector3("star.position", value) for value in self.positions
        )
        if not positions:
            raise ValueError("Star batches require at least one position")
        radii = tuple(_finite("star.radius", value) for value in self.radii)
        colors = tuple(_rgba("star.color", value) for value in self.colors)
        alphas = tuple(_finite("star.alpha", value) for value in self.alphas)
        magnitudes = tuple(
            _finite("star.magnitude", value) for value in self.magnitudes
        )
        visual_classes = tuple(
            _version("star.visual_class", value)
            for value in self.visual_classes
        )
        picking_ids = tuple(
            _version("star.picking_id", value) for value in self.picking_ids
        )
        object_ids = tuple(
            _version("star.object_id", value) for value in self.object_ids
        )
        input_coordinates = tuple(
            _vector2("star.input_coordinate", value)
            for value in self.input_coordinates
        )
        uv = tuple(_vector2("star.uv", value) for value in self.uv)
        normals = tuple(
            _vector3("star.normal", value) for value in self.normals
        )
        expected = len(positions)
        for name, values in (
            ("star.radii", radii),
            ("star.colors", colors),
            ("star.alphas", alphas),
            ("star.magnitudes", magnitudes),
            ("star.visual_classes", visual_classes),
            ("star.picking_ids", picking_ids),
            ("star.object_ids", object_ids),
        ):
            _same_length(name, values, expected)
        if input_coordinates:
            _same_length("star.input_coordinates", input_coordinates, expected)
        if uv:
            _same_length("star.uv", uv, expected)
        if normals:
            _same_length("star.normals", normals, expected)
        if any(radius <= 0.0 for radius in radii):
            raise ValueError("star.radii must be positive")
        if any(alpha < 0.0 or alpha > 1.0 for alpha in alphas):
            raise ValueError("star.alphas must be in [0, 1]")
        object.__setattr__(
            self,
            "primitive_id",
            _version("star.primitive_id", self.primitive_id),
        )
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "radii", radii)
        object.__setattr__(self, "colors", colors)
        object.__setattr__(self, "alphas", alphas)
        object.__setattr__(self, "magnitudes", magnitudes)
        object.__setattr__(self, "visual_classes", visual_classes)
        object.__setattr__(self, "picking_ids", picking_ids)
        object.__setattr__(self, "object_ids", object_ids)
        object.__setattr__(self, "input_coordinates", input_coordinates)
        object.__setattr__(self, "uv", uv)
        object.__setattr__(self, "normals", normals)
        if any(not self.bounds.contains(position) for position in positions):
            raise ValueError("star.bounds must enclose every star position")
        object.__setattr__(
            self,
            "layer_order",
            _layer_order("star.layer_order", self.layer_order),
        )
        object.__setattr__(
            self, "version", _version("star.version", self.version)
        )
        object.__setattr__(
            self,
            "frame_generation",
            _generation("star.frame_generation", self.frame_generation),
        )


@dataclass(frozen=True, slots=True)
class TriangleMeshResource:
    """A fully resolved indexed triangle mesh."""

    primitive_id: str
    positions: tuple[Vector3, ...]
    indices: tuple[int, ...]
    colors: tuple[ColorRGBA, ...]
    alphas: tuple[float, ...]
    uv: tuple[Vector2, ...]
    normals: tuple[Vector3, ...]
    object_ids: tuple[str, ...]
    layer_order: int
    material: MaterialParameters
    bounds: Bounds
    geometry_version: str
    material_version: str
    frame_generation: int
    texture: TextureParameters | None = None

    kind: PrimitiveKind = field(default="triangle_mesh", init=False)

    def __post_init__(self) -> None:
        positions = tuple(
            _vector3("mesh.position", value) for value in self.positions
        )
        if len(positions) < 3:
            raise ValueError(
                "Triangle meshes require at least three positions"
            )
        indices = tuple(int(value) for value in self.indices)
        if not indices or len(indices) % 3:
            raise ValueError(
                "Triangle mesh indices must form complete triangles"
            )
        if any(index < 0 or index >= len(positions) for index in indices):
            raise ValueError("Triangle mesh indices must reference positions")
        colors = tuple(_rgba("mesh.color", value) for value in self.colors)
        alphas = tuple(_finite("mesh.alpha", value) for value in self.alphas)
        uv = tuple(_vector2("mesh.uv", value) for value in self.uv)
        normals = tuple(
            _vector3("mesh.normal", value) for value in self.normals
        )
        object_ids = tuple(
            _version("mesh.object_id", value) for value in self.object_ids
        )
        expected = len(positions)
        for name, values in (
            ("mesh.colors", colors),
            ("mesh.alphas", alphas),
            ("mesh.uv", uv),
            ("mesh.normals", normals),
            ("mesh.object_ids", object_ids),
        ):
            _same_length(name, values, expected)
        if any(alpha < 0.0 or alpha > 1.0 for alpha in alphas):
            raise ValueError("mesh.alphas must be in [0, 1]")
        object.__setattr__(
            self,
            "primitive_id",
            _version("mesh.primitive_id", self.primitive_id),
        )
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "indices", indices)
        object.__setattr__(self, "colors", colors)
        object.__setattr__(self, "alphas", alphas)
        object.__setattr__(self, "uv", uv)
        object.__setattr__(self, "normals", normals)
        object.__setattr__(self, "object_ids", object_ids)
        if any(not self.bounds.contains(position) for position in positions):
            raise ValueError("mesh.bounds must enclose every mesh position")
        object.__setattr__(
            self,
            "layer_order",
            _layer_order("mesh.layer_order", self.layer_order),
        )
        object.__setattr__(
            self,
            "geometry_version",
            _version("mesh.geometry_version", self.geometry_version),
        )
        object.__setattr__(
            self,
            "material_version",
            _version("mesh.material_version", self.material_version),
        )
        object.__setattr__(
            self,
            "frame_generation",
            _generation("mesh.frame_generation", self.frame_generation),
        )


@dataclass(frozen=True, slots=True)
class TextStyle:
    """Resolved text styling shared by every presentation backend."""

    font_family: str
    size_px: float
    weight: int
    foreground: ColorRGBA
    italic: bool = False
    background: ColorRGBA | None = None
    outline: ColorRGBA | None = None
    outline_width_px: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "font_family", _version("text.font_family", self.font_family)
        )
        size = _finite("text.size_px", self.size_px)
        if size <= 0.0:
            raise ValueError("text.size_px must be positive")
        object.__setattr__(self, "size_px", size)
        object.__setattr__(
            self, "foreground", _rgba("text.foreground", self.foreground)
        )
        if self.background is not None:
            object.__setattr__(
                self, "background", _rgba("text.background", self.background)
            )
        if self.outline is not None:
            object.__setattr__(
                self, "outline", _rgba("text.outline", self.outline)
            )
        outline_width = _finite("text.outline_width_px", self.outline_width_px)
        if outline_width < 0.0:
            raise ValueError("text.outline_width_px cannot be negative")
        object.__setattr__(self, "outline_width_px", outline_width)


@dataclass(frozen=True, slots=True)
class TextLabel:
    """A positioned label after the planner resolved layout and clipping."""

    content: str
    position: Vector3
    anchor: str
    priority: int
    style: TextStyle
    clip: Bounds | None
    object_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "content", _version("text.content", self.content)
        )
        object.__setattr__(
            self, "position", _vector3("text.position", self.position)
        )
        object.__setattr__(
            self, "anchor", _version("text.anchor", self.anchor)
        )
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(
            self, "object_id", _version("text.object_id", self.object_id)
        )


@dataclass(frozen=True, slots=True)
class TextBatch:
    """A drawable text primitive; all labels are already ordered and clipped."""

    primitive_id: str
    labels: tuple[TextLabel, ...]
    layer_order: int
    material: MaterialParameters
    bounds: Bounds
    version: str
    frame_generation: int
    texture: TextureParameters | None = None

    kind: PrimitiveKind = field(default="text", init=False)

    def __post_init__(self) -> None:
        labels = tuple(self.labels)
        if not labels:
            raise ValueError("Text batches require at least one label")
        if any(not self.bounds.contains(label.position) for label in labels):
            raise ValueError("text.bounds must enclose every label position")
        object.__setattr__(
            self,
            "primitive_id",
            _version("text.primitive_id", self.primitive_id),
        )
        object.__setattr__(self, "labels", labels)
        object.__setattr__(
            self,
            "layer_order",
            _layer_order("text.layer_order", self.layer_order),
        )
        object.__setattr__(
            self, "version", _version("text.version", self.version)
        )
        object.__setattr__(
            self,
            "frame_generation",
            _generation("text.frame_generation", self.frame_generation),
        )


@dataclass(frozen=True, slots=True)
class TerrainSurfaceAttributes:
    """Per-vertex visual terrain attributes prepared on CPU."""

    colors: tuple[ColorRGBA, ...]
    alphas: tuple[float, ...]
    uv: tuple[Vector2, ...]
    elevations_m: tuple[float, ...]
    object_ids: tuple[str, ...]
    class_ids: tuple[int, ...] = ()
    categorical_flags: tuple[bool, ...] = ()

    def __post_init__(self) -> None:
        colors = tuple(_rgba("terrain.color", value) for value in self.colors)
        object.__setattr__(self, "colors", colors)
        alphas = tuple(
            _finite("terrain.alpha", value) for value in self.alphas
        )
        if any(alpha < 0.0 or alpha > 1.0 for alpha in alphas):
            raise ValueError("terrain.alphas must be in [0, 1]")
        object.__setattr__(self, "alphas", alphas)
        object.__setattr__(
            self,
            "uv",
            tuple(_vector2("terrain.uv", value) for value in self.uv),
        )
        object.__setattr__(
            self,
            "elevations_m",
            tuple(
                _finite("terrain.elevation_m", value)
                for value in self.elevations_m
            ),
        )
        object.__setattr__(
            self,
            "object_ids",
            tuple(
                _version("terrain.object_id", value)
                for value in self.object_ids
            ),
        )
        class_ids = self.class_ids or (-1,) * len(colors)
        object.__setattr__(
            self, "class_ids", tuple(int(value) for value in class_ids)
        )
        categorical = self.categorical_flags or (False,) * len(colors)
        object.__setattr__(
            self,
            "categorical_flags",
            tuple(bool(value) for value in categorical),
        )


@dataclass(frozen=True, slots=True)
class TerrainTile:
    """The exact index range and bounds for one terrain tile."""

    tile_id: str
    first_index: int
    index_count: int
    bounds: Bounds

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "tile_id", _version("terrain.tile_id", self.tile_id)
        )
        first_index = int(self.first_index)
        index_count = int(self.index_count)
        if first_index < 0 or index_count < 3 or index_count % 3:
            raise ValueError(
                "Terrain tile indices must contain complete triangles"
            )
        object.__setattr__(self, "first_index", first_index)
        object.__setattr__(self, "index_count", index_count)


@dataclass(frozen=True, slots=True)
class TerrainMeshResource:
    """A tiled terrain mesh with CPU-resolved geometry and material inputs."""

    primitive_id: str
    vertices: tuple[Vector3, ...]
    indices: tuple[int, ...]
    normals: tuple[Vector3, ...]
    surface: TerrainSurfaceAttributes
    tiles: tuple[TerrainTile, ...]
    layer_order: int
    material: MaterialParameters
    bounds: Bounds
    geometry_version: str
    material_version: str
    frame_generation: int
    texture: TextureParameters | None = None
    view_vertices: tuple[Vector3, ...] = ()

    kind: PrimitiveKind = field(default="terrain_mesh", init=False)

    def __post_init__(self) -> None:
        vertices = tuple(
            _vector3("terrain.vertex", value) for value in self.vertices
        )
        if len(vertices) < 3:
            raise ValueError("Terrain meshes require at least three vertices")
        indices = tuple(int(value) for value in self.indices)
        if not indices or len(indices) % 3:
            raise ValueError(
                "Terrain mesh indices must form complete triangles"
            )
        if any(index < 0 or index >= len(vertices) for index in indices):
            raise ValueError("Terrain mesh indices must reference vertices")
        normals = tuple(
            _vector3("terrain.normal", value) for value in self.normals
        )
        expected = len(vertices)
        for name, values in (
            ("terrain.normals", normals),
            ("terrain.colors", self.surface.colors),
            ("terrain.alphas", self.surface.alphas),
            ("terrain.uv", self.surface.uv),
            ("terrain.elevations_m", self.surface.elevations_m),
            ("terrain.object_ids", self.surface.object_ids),
            ("terrain.class_ids", self.surface.class_ids),
            ("terrain.categorical_flags", self.surface.categorical_flags),
        ):
            _same_length(name, values, expected)
        view_vertices = tuple(
            _vector3("terrain.view_vertex", value)
            for value in self.view_vertices
        )
        if view_vertices:
            _same_length("terrain.view_vertices", view_vertices, expected)
        tiles = tuple(self.tiles)
        if not tiles:
            raise ValueError("Terrain meshes require at least one tile")
        ordered_tiles = tuple(sorted(tiles, key=lambda tile: tile.first_index))
        if any(
            tile.first_index + tile.index_count > len(indices)
            for tile in ordered_tiles
        ):
            raise ValueError("Terrain tile range exceeds its index buffer")
        next_index = 0
        for tile in ordered_tiles:
            if tile.first_index != next_index:
                raise ValueError(
                    "Terrain tile ranges must cover the index buffer once"
                )
            next_index += tile.index_count
        if next_index != len(indices):
            raise ValueError(
                "Terrain tile ranges must cover the complete index buffer"
            )
        object.__setattr__(
            self,
            "primitive_id",
            _version("terrain.primitive_id", self.primitive_id),
        )
        object.__setattr__(self, "vertices", vertices)
        object.__setattr__(self, "indices", indices)
        object.__setattr__(self, "normals", normals)
        object.__setattr__(self, "view_vertices", view_vertices)
        if any(not self.bounds.contains(vertex) for vertex in vertices):
            raise ValueError(
                "terrain.bounds must enclose every terrain vertex"
            )
        object.__setattr__(self, "tiles", ordered_tiles)
        object.__setattr__(
            self,
            "layer_order",
            _layer_order("terrain.layer_order", self.layer_order),
        )
        object.__setattr__(
            self,
            "geometry_version",
            _version("terrain.geometry_version", self.geometry_version),
        )
        object.__setattr__(
            self,
            "material_version",
            _version("terrain.material_version", self.material_version),
        )
        object.__setattr__(
            self,
            "frame_generation",
            _generation("terrain.frame_generation", self.frame_generation),
        )


DrawablePrimitive: TypeAlias = (
    SpriteBatch
    | StarBatch
    | TriangleMeshResource
    | TextBatch
    | TerrainMeshResource
)


@dataclass(frozen=True, slots=True)
class ResourcePlan:
    """Versioned CPU resources referenced by the frame's typed primitives."""

    frame_generation: int
    artifacts: tuple[ArtifactResource, ...] = ()
    textures: tuple[TextureResource, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "frame_generation",
            _generation("resources.frame_generation", self.frame_generation),
        )
        artifacts = tuple(self.artifacts)
        textures = tuple(self.textures)
        identifiers = tuple(
            resource.resource_id for resource in artifacts
        ) + tuple(resource.texture_id for resource in textures)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Resource identifiers must be unique")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "textures", textures)


@dataclass(frozen=True, slots=True)
class PickingRecord:
    """Stable, pre-resolved picking area for one drawable object."""

    object_id: str
    picking_id: str
    bounds: Bounds
    layer_order: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "object_id", _version("picking.object_id", self.object_id)
        )
        object.__setattr__(
            self, "picking_id", _version("picking.picking_id", self.picking_id)
        )
        object.__setattr__(
            self,
            "layer_order",
            _layer_order("picking.layer_order", self.layer_order),
        )


@dataclass(frozen=True, slots=True)
class PickingPlan:
    """Resolved hit records and coordinate space for one frame."""

    generation: int
    records: tuple[PickingRecord, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generation",
            _generation("picking.generation", self.generation),
        )
        records = tuple(self.records)
        if len({record.picking_id for record in records}) != len(records):
            raise ValueError("Picking identifiers must be unique per frame")
        object.__setattr__(self, "records", records)


@dataclass(frozen=True, slots=True)
class InteractionAffordance:
    """A resolved interaction target.  Input interpretation remains in control."""

    affordance_id: str
    action: str
    bounds: Bounds
    active: bool
    layer_order: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "affordance_id",
            _version("interaction.affordance_id", self.affordance_id),
        )
        object.__setattr__(
            self, "action", _version("interaction.action", self.action)
        )
        object.__setattr__(
            self,
            "layer_order",
            _layer_order("interaction.layer_order", self.layer_order),
        )


@dataclass(frozen=True, slots=True)
class InteractionPlan:
    """Resolved interaction affordances and current interaction state."""

    generation: int
    affordances: tuple[InteractionAffordance, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generation",
            _generation("interaction.generation", self.generation),
        )
        affordances = tuple(self.affordances)
        if len({item.affordance_id for item in affordances}) != len(
            affordances
        ):
            raise ValueError(
                "Interaction affordance identifiers must be unique"
            )
        object.__setattr__(self, "affordances", affordances)


@dataclass(frozen=True, slots=True)
class SceneRenderPlan:
    """Ordered renderer-neutral primitives for one scene capability."""

    capability: str
    layer_order: int
    frame_generation: int
    primitives: tuple[DrawablePrimitive, ...] = ()
    version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capability", _version("plan.capability", self.capability)
        )
        layer_order = _layer_order("plan.layer_order", self.layer_order)
        generation = _generation(
            "plan.frame_generation", self.frame_generation
        )
        primitives = tuple(self.primitives)
        for primitive in primitives:
            if primitive.layer_order != layer_order:
                raise ValueError(
                    "Primitive layer order must match its scene plan"
                )
            if primitive.frame_generation != generation:
                raise ValueError(
                    "Primitive generation must match its scene plan"
                )
        object.__setattr__(self, "layer_order", layer_order)
        object.__setattr__(self, "frame_generation", generation)
        object.__setattr__(self, "primitives", primitives)
        object.__setattr__(
            self, "version", _version("plan.version", self.version)
        )


@dataclass(frozen=True, slots=True)
class RenderPlanBundle:
    """The complete immutable work order consumed by a presentation backend."""

    generation: int
    frame: SceneFrame
    plans: tuple[SceneRenderPlan, ...]
    resources: ResourcePlan
    picking: PickingPlan
    interaction: InteractionPlan
    celestial: "CelestialLayerPlans | None" = None
    overlays: "OverlayLayerPlans | None" = None
    pick_index: "PickIndex | None" = None
    schema_version: int = 2

    def __post_init__(self) -> None:
        generation = _generation("render-plan generation", self.generation)
        if generation != self.frame.generation:
            raise ValueError(
                "Render-plan generation must match its SceneFrame"
            )
        if self.resources.frame_generation != generation:
            raise ValueError(
                "Resource plan generation must match its render bundle"
            )
        if self.picking.generation != generation:
            raise ValueError(
                "Picking plan generation must match its render bundle"
            )
        if self.interaction.generation != generation:
            raise ValueError(
                "Interaction plan generation must match its render bundle"
            )
        if (
            self.celestial is not None
            and self.celestial.generation != generation
        ):
            raise ValueError(
                "Celestial plans must match the render bundle generation"
            )
        if (
            self.overlays is not None
            and self.overlays.generation != generation
        ):
            raise ValueError(
                "Overlay plans must match the render bundle generation"
            )
        if (
            self.pick_index is not None
            and self.pick_index.generation != generation
        ):
            raise ValueError(
                "Pick index must match the render bundle generation"
            )
        plans = tuple(self.plans)
        if any(plan.frame_generation != generation for plan in plans):
            raise ValueError(
                "Scene plan generation must match its render bundle"
            )
        textures = {
            resource.texture_id: resource.version
            for resource in self.resources.textures
        }
        for plan in plans:
            for primitive in plan.primitives:
                for texture in (primitive.texture, primitive.material.texture):
                    if (
                        texture is not None
                        and textures.get(texture.texture_id) != texture.version
                    ):
                        raise ValueError(
                            "Primitive texture parameters must reference a "
                            "registered texture resource version"
                        )
        if int(self.schema_version) != 2:
            raise ValueError("Unsupported render-plan schema version")
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "plans", plans)
