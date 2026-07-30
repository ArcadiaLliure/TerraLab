"""Pure Python runtime adapters communicating with the isolated compute service process."""

from __future__ import annotations

from typing import Any, Callable

from TerraLab.application.ports.compute import (
    EphemerisPort,
    StarCatalogPort,
    WeatherPort,
    WeatherSampleRequest,
)
from TerraLab.runtime.protocol import (
    ARTIFACT_READY,
    COMPUTE_REQUEST,
    WORKER_ERROR,
    Envelope,
    envelope,
)
from TerraLab.runtime.supervisor import RuntimeSupervisor
from TerraLab.weather.metno_provider import MetNoWeatherProvider


class RuntimeEphemerisAdapter(EphemerisPort):
    """Pure Python runtime adapter for ephemeris calculation via supervisor IPC."""

    def __init__(self, runtime: RuntimeSupervisor) -> None:
        self._runtime = runtime
        self._latitude = 0.0
        self._longitude = 0.0
        self._generation = 0
        self._snapshot: dict[str, Any] | None = None
        self._shutdown = False
        self._inflight = False
        self._pending: dict[str, Any] | None = None
        self._last_request_key: tuple[int, int, int, int, int] | None = None
        self._ready_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._error_listeners: list[Callable[[str], None]] = []

        runtime.message_received.connect(self._on_message)
        runtime.worker_ready.connect(self._on_worker_ready)

    def add_ready_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> None:
        if listener not in self._ready_listeners:
            self._ready_listeners.append(listener)

    def add_error_listener(self, listener: Callable[[str], None]) -> None:
        if listener not in self._error_listeners:
            self._error_listeners.append(listener)

    def configure_observer(self, latitude: float, longitude: float) -> None:
        self._latitude = float(latitude)
        self._longitude = float(longitude)

    def request_snapshot(
        self,
        *,
        year_utc: int,
        day_of_year_utc: int,
        ut_hour: float,
    ) -> None:
        if self._shutdown:
            return
        payload = {
            "operation": "ephemeris",
            "year_utc": int(year_utc),
            "day_of_year_utc": int(day_of_year_utc),
            "ut_hour": float(ut_hour),
            "latitude": self._latitude,
            "longitude": self._longitude,
        }
        key = (
            payload["year_utc"],
            payload["day_of_year_utc"],
            int(round(payload["ut_hour"] * 3600.0)),
            int(round(self._latitude * 10_000.0)),
            int(round(self._longitude * 10_000.0)),
        )
        if key == self._last_request_key and not self._inflight:
            return
        self._last_request_key = key
        self._pending = payload
        self._dispatch()

    def _dispatch(self) -> None:
        if self._shutdown or self._inflight or self._pending is None:
            return
        payload, self._pending = self._pending, None
        self._generation += 1
        self._inflight = self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                payload,
                request_id="ephemeris",
                generation=self._generation,
            ),
        )
        if not self._inflight:
            self._pending = payload

    def get_snapshot(self) -> dict[str, Any] | None:
        return (
            dict(self._snapshot) if isinstance(self._snapshot, dict) else None
        )

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        for signal, callback in (
            (self._runtime.message_received, self._on_message),
            (self._runtime.worker_ready, self._on_worker_ready),
        ):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass

    def _on_worker_ready(self, role: str) -> None:
        if role != "compute" or self._shutdown:
            return
        self._inflight = False
        self._dispatch()

    def _on_message(self, role: str, message: Envelope) -> None:
        if (
            self._shutdown
            or role != "compute"
            or message.request_id != "ephemeris"
            or message.generation != self._generation
        ):
            return
        if message.kind == ARTIFACT_READY:
            value = message.payload.get("value")
            if isinstance(value, dict):
                self._snapshot = dict(value)
                for ready_listener in list(self._ready_listeners):
                    ready_listener(dict(value))
        elif message.kind == WORKER_ERROR:
            msg = str(message.payload.get("message", "Ephemeris error"))
            for error_listener in list(self._error_listeners):
                error_listener(msg)
        self._inflight = False
        if self._pending is not None:
            self._dispatch()


class RuntimeStarCatalogAdapter(StarCatalogPort):
    """Pure Python runtime adapter for star catalog loading via supervisor IPC."""

    def __init__(
        self,
        runtime: RuntimeSupervisor,
        manifest_path: str,
    ) -> None:
        self._runtime = runtime
        self._manifest_path = str(manifest_path)
        self._generation = 0
        self._scope_generation = 0
        self._scope_request: dict[str, Any] | None = None
        self._shutdown = False
        self._general_tile_listeners: list[
            Callable[[dict[str, Any]], None]
        ] = []
        self._extension_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._error_listeners: list[Callable[[str], None]] = []

        runtime.message_received.connect(self._on_message)
        runtime.worker_ready.connect(self._on_worker_ready)

    def add_general_tile_ready_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> None:
        if listener not in self._general_tile_listeners:
            self._general_tile_listeners.append(listener)

    def add_extension_ready_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> None:
        if listener not in self._extension_listeners:
            self._extension_listeners.append(listener)

    def add_error_listener(self, listener: Callable[[str], None]) -> None:
        if listener not in self._error_listeners:
            self._error_listeners.append(listener)

    def load_general_tile(self) -> None:
        if self._shutdown:
            return
        self._generation += 1
        self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                {
                    "operation": "catalog_general",
                    "manifest_path": self._manifest_path,
                },
                request_id="catalog",
                generation=self._generation,
            ),
        )

    def request_scope_region(self, **context: Any) -> None:
        if self._shutdown:
            return
        self._scope_generation += 1
        self._scope_request = {
            "operation": "catalog_scope",
            "manifest_path": self._manifest_path,
            **dict(context),
        }
        self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                self._scope_request,
                request_id="catalog_scope",
                generation=self._scope_generation,
            ),
        )

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        for signal, callback in (
            (self._runtime.message_received, self._on_message),
            (self._runtime.worker_ready, self._on_worker_ready),
        ):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass

    def _on_worker_ready(self, role: str) -> None:
        if role == "compute" and not self._shutdown:
            self.load_general_tile()
            if self._scope_request is not None:
                self.request_scope_region(**self._scope_request)

    def _on_message(self, role: str, message: Envelope) -> None:
        if self._shutdown or role != "compute":
            return
        if message.request_id == "catalog":
            expected_generation = self._generation
        elif message.request_id == "catalog_scope":
            expected_generation = self._scope_generation
        else:
            return
        if message.generation != expected_generation:
            return
        if message.kind == ARTIFACT_READY:
            value = message.payload.get("value")
            if isinstance(value, dict):
                if message.request_id == "catalog_scope":
                    for listener in list(self._extension_listeners):
                        listener(dict(value))
                else:
                    for listener in list(self._general_tile_listeners):
                        listener(dict(value))
        elif message.kind == WORKER_ERROR:
            msg = str(message.payload.get("message", "Catalog error"))
            for error_listener in list(self._error_listeners):
                error_listener(msg)


class RuntimeWeatherAdapter(WeatherPort):
    """Pure Python runtime adapter for weather samples."""

    def __init__(
        self,
        latitude: float = 0.0,
        longitude: float = 0.0,
        use_remote: bool = True,
        cache_enabled: bool = True,
    ) -> None:
        self._provider = MetNoWeatherProvider(
            latitude=latitude,
            longitude=longitude,
            use_remote=use_remote,
            cache_enabled=cache_enabled,
        )

    def get_weather_sample(
        self, request: WeatherSampleRequest
    ) -> dict[str, Any]:
        self._provider.set_location(request.latitude, request.longitude)
        if request.user_agent:
            self._provider.set_user_agent(request.user_agent)
        res = self._provider.get_weather(
            request.year_utc, request.day_of_year_utc, int(request.ut_hour)
        )
        return dict(res) if isinstance(res, dict) else {}

    def shutdown(self) -> None:
        self._provider.shutdown()
