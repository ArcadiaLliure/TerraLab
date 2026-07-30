"""Neutral renderer, target, output, and lifecycle contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Protocol, runtime_checkable

from TerraLab.core.rendering_contracts.plans import RenderPlanBundle
from TerraLab.scene.contracts import JSONValue, freeze_json_mapping


class RenderCapability(str, Enum):
    RASTER_SHARED_FRAME = "raster_shared_frame"
    HOSTED_SURFACE = "hosted_surface"
    COMMAND_STREAM = "command_stream"
    PICKING = "picking"
    INTERACTION = "interaction"
    SKY_BACKGROUND = "sky_background"
    STARS = "stars"
    EPHEMERIS_BODIES = "ephemeris_bodies"
    DEEP_SKY = "deep_sky"
    GRID = "grid"
    LABELS = "labels"
    SCOPE = "scope"
    CONSTELLATIONS = "constellations"
    MEASUREMENTS = "measurements"
    TERRAIN_GEOMETRY = "terrain_geometry"
    TERRAIN_MATERIALS = "terrain_materials"
    # Kept as a capability label while old callers migrate to COMMAND_STREAM.
    RECORDING = "recording"


class RenderTargetKind(str, Enum):
    SHARED_RASTER = "shared_raster"
    HOSTED_SURFACE = "hosted_surface"
    COMMAND_STREAM = "command_stream"


class RenderOutputKind(str, Enum):
    RASTER = "raster"
    HOSTED_SURFACE = "hosted_surface"
    COMMAND_STREAM = "command_stream"


@dataclass(frozen=True, slots=True)
class RasterFrameHandle:
    slot: int
    width: int
    height: int
    stride: int
    pool_generation: int
    pixel_format: str = "argb32_premultiplied"


@dataclass(frozen=True, slots=True)
class SharedRasterTarget:
    """A target backed by caller-owned pixels; no image-toolkit type leaks."""

    handle: RasterFrameHandle
    pixels: memoryview
    kind: RenderTargetKind = RenderTargetKind.SHARED_RASTER


@dataclass(frozen=True, slots=True)
class HostedSurfaceTarget:
    """A hosted GPU/window surface identified by an opaque handle."""

    surface_id: str
    width: int
    height: int
    kind: RenderTargetKind = RenderTargetKind.HOSTED_SURFACE


@dataclass(frozen=True, slots=True)
class CommandStreamTarget:
    """A web/remote target represented by an opaque destination ID."""

    stream_id: str
    kind: RenderTargetKind = RenderTargetKind.COMMAND_STREAM


RenderTarget = SharedRasterTarget | HostedSurfaceTarget | CommandStreamTarget


@dataclass(frozen=True, slots=True)
class RasterFrameOutput:
    generation: int
    handle: RasterFrameHandle
    render_ms: float
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    kind: RenderOutputKind = RenderOutputKind.RASTER

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_json_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class HostedSurfaceOutput:
    generation: int
    surface_id: str
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    kind: RenderOutputKind = RenderOutputKind.HOSTED_SURFACE

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_json_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class CommandStreamOutput:
    generation: int
    stream_id: str
    commands: tuple[Mapping[str, JSONValue], ...] = ()
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    kind: RenderOutputKind = RenderOutputKind.COMMAND_STREAM

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "commands",
            tuple(freeze_json_mapping(command) for command in self.commands),
        )
        object.__setattr__(self, "metadata", freeze_json_mapping(self.metadata))

    # Transitional read-only names for the former recording adapter.
    @property
    def recording_id(self) -> str:
        return self.stream_id

    @property
    def manifest(self) -> Mapping[str, JSONValue]:
        return self.metadata


RenderOutput = RasterFrameOutput | HostedSurfaceOutput | CommandStreamOutput


@dataclass(frozen=True, slots=True)
class PickRequest:
    generation: int
    request_id: str
    x: float
    y: float
    radius: float = 20.0
    purpose: str = "select"
    action: str = ""
    options: Mapping[str, JSONValue] | None = None

    def __post_init__(self) -> None:
        if self.options is not None:
            object.__setattr__(self, "options", freeze_json_mapping(self.options))


@dataclass(frozen=True, slots=True)
class PickResult:
    generation: int
    request_id: str
    payload: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json_mapping(self.payload))


@dataclass(frozen=True, slots=True)
class RenderFailure:
    backend_id: str
    operation: str
    message: str


class RenderBackendLifecycleError(RuntimeError):
    pass


class RenderBackendConfigurationError(ValueError):
    pass


class BackendNotFoundError(RenderBackendConfigurationError):
    pass


class DuplicateBackendError(RenderBackendConfigurationError):
    pass


class PresenterIncompatibleError(RenderBackendConfigurationError):
    pass


class RenderOutputPort(Protocol):
    def frame_ready(self, output: RenderOutput) -> None: ...

    def pick_ready(self, result: PickResult) -> None: ...

    def backend_failed(self, failure: RenderFailure) -> None: ...


@runtime_checkable
class RendererBackend(Protocol):
    """Backend contract.  Backends consume plans and neutral targets only."""

    backend_id: str
    capabilities: frozenset[RenderCapability]
    target_kinds: frozenset[RenderTargetKind]

    def start(self, output_port: RenderOutputPort) -> None: ...

    def render(self, plan: RenderPlanBundle, target: RenderTarget) -> RenderOutput: ...

    def request_pick(self, request: PickRequest, plan: RenderPlanBundle | None = None) -> PickResult: ...

    def close(self) -> None: ...
