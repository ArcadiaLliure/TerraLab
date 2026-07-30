"""Qt-free application ports for astronomical ephemeris, star catalogs, and weather computation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class EphemerisRequest:
    """Typed request for an astronomical ephemeris snapshot."""

    year_utc: int
    day_of_year_utc: int
    ut_hour: float
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class StarTileRequest:
    """Typed request for catalog tile or region loading."""

    manifest_path: str
    operation: str = "catalog_general"
    context: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class WeatherSampleRequest:
    """Typed request for weather forecast samples."""

    latitude: float
    longitude: float
    year_utc: int
    day_of_year_utc: int
    ut_hour: float
    use_remote: bool = True
    cache_enabled: bool = True
    user_agent: str = ""


@dataclass(frozen=True, slots=True)
class ComputeFailure:
    """Typed compute process or service error notification."""

    operation: str
    message: str


@runtime_checkable
class EphemerisPort(Protocol):
    """Port contract for ephemeris snapshot calculation."""

    def configure_observer(
        self, latitude: float, longitude: float
    ) -> None: ...

    def request_snapshot(
        self,
        *,
        year_utc: int,
        day_of_year_utc: int,
        ut_hour: float,
    ) -> None: ...

    def get_snapshot(self) -> dict[str, Any] | None: ...

    def shutdown(self) -> None: ...


@runtime_checkable
class StarCatalogPort(Protocol):
    """Port contract for star tile and catalog retrieval."""

    def load_general_tile(self) -> None: ...

    def request_scope_region(self, **context: Any) -> None: ...

    def shutdown(self) -> None: ...


@runtime_checkable
class WeatherPort(Protocol):
    """Port contract for weather system state and forecast data."""

    def get_weather_sample(
        self, request: WeatherSampleRequest
    ) -> dict[str, Any]: ...

    def shutdown(self) -> None: ...
