"""Qt-free application ports for elevation, terrain bake, and surface sampling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ElevationQuery:
    """Typed query for elevation at a geographic coordinate."""

    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class BakeJobRequest:
    """Typed request for a terrain horizon bake job."""

    job_id: str
    observer_lat: float
    observer_lon: float
    observer_offset_m: float = 0.0
    radius_m: float = 50_000.0
    tiles_dir: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SurfaceRefreshContext:
    """Typed context for surface sampling refresh."""

    profile: object | None = None
    visible_radius_m: float | None = None
    view_azimuth_deg: float = 0.0
    view_fov_deg: float = 360.0
    viewport_width_px: int | None = None
    viewport_height_px: int | None = None
    surface_mode: str | None = None
    atomic_surface_swap: bool = False
    generation: int = 0


@dataclass(frozen=True, slots=True)
class BortleEstimateRequest:
    """Typed request for light pollution / Bortle estimation."""

    latitude: float
    longitude: float
    request_id: int = 0


@dataclass(frozen=True, slots=True)
class TerrainProgress:
    """Typed progress snapshot emitted by terrain operations."""

    job_id: str
    phase: str
    percent: float
    message: str = ""
    current: int = 0
    total: int = 100
    kind: str = "terrain"


@runtime_checkable
class ElevationPort(Protocol):
    """Port contract for bare elevation queries."""

    def get_bare_elevation(self, lat: float, lon: float) -> float | None: ...

    def request_bortle_estimate(
        self, lat: float, lon: float, request_id: int = 0
    ) -> None: ...


@runtime_checkable
class TerrainBakePort(Protocol):
    """Port contract for terrain bake process management."""

    def request_bake(self, job: BakeJobRequest | Mapping[str, Any]) -> None: ...

    def abort_current_job(self) -> None: ...

    def set_observer_offset(self, offset: float) -> None: ...

    def get_progress_text(self) -> str: ...

    def reload_config(self) -> None: ...

    def shutdown(self) -> None: ...


@runtime_checkable
class SurfaceSamplingPort(Protocol):
    """Port contract for terrain surface sampling and raster updates."""

    def request_surface_refresh(
        self, context: SurfaceRefreshContext | Mapping[str, Any]
    ) -> None: ...

    def cancel_surface_refresh(self) -> None: ...
