"""Qt signal adapters bridging compute ports and pure runtime adapters for View components."""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import QObject, pyqtSignal

from TerraLab.application.ports.compute import (
    EphemerisPort,
    StarCatalogPort,
    WeatherPort,
)


class QtEphemerisAdapter(QObject):
    """Qt signal bridge wrapping an EphemerisPort."""

    ephemeris_ready = pyqtSignal(object)
    ephemeris_error = pyqtSignal(str)

    def __init__(
        self,
        port: EphemerisPort,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._port = port
        if hasattr(port, "add_ready_listener"):
            getattr(port, "add_ready_listener")(self._on_ready)
        if hasattr(port, "add_error_listener"):
            getattr(port, "add_error_listener")(self._on_error)

    def configure_observer(self, latitude: float, longitude: float) -> None:
        self._port.configure_observer(latitude, longitude)

    def request_snapshot(
        self,
        *,
        year_utc: int,
        day_of_year_utc: int,
        ut_hour: float,
    ) -> None:
        self._port.request_snapshot(
            year_utc=year_utc,
            day_of_year_utc=day_of_year_utc,
            ut_hour=ut_hour,
        )

    def get_snapshot(self) -> dict[str, Any] | None:
        return self._port.get_snapshot()

    def shutdown(self) -> None:
        self._port.shutdown()

    def _on_ready(self, snapshot: dict[str, Any]) -> None:
        self.ephemeris_ready.emit(snapshot)

    def _on_error(self, message: str) -> None:
        self.ephemeris_error.emit(message)


class QtStarCatalogAdapter(QObject):
    """Qt signal bridge wrapping a StarCatalogPort."""

    general_tile_ready = pyqtSignal(object)
    deep_tile_ready = pyqtSignal(str, object)
    scope_index_ready = pyqtSignal(object)
    extension_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        port: StarCatalogPort,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._port = port
        if hasattr(port, "add_general_tile_ready_listener"):
            getattr(port, "add_general_tile_ready_listener")(
                self._on_general_ready
            )
        if hasattr(port, "add_deep_tile_ready_listener"):
            getattr(port, "add_deep_tile_ready_listener")(self._on_deep_ready)
        if hasattr(port, "add_scope_index_ready_listener"):
            getattr(port, "add_scope_index_ready_listener")(
                self._on_scope_ready
            )
        if hasattr(port, "add_extension_ready_listener"):
            getattr(port, "add_extension_ready_listener")(
                self._on_extension_ready
            )
        if hasattr(port, "add_error_listener"):
            getattr(port, "add_error_listener")(self._on_error)

    def load_general_tile(self) -> None:
        self._port.load_general_tile()

    def request_scope_region(self, **context: Any) -> None:
        self._port.request_scope_region(**context)

    def shutdown(self) -> None:
        self._port.shutdown()

    def _on_general_ready(self, payload: Any) -> None:
        self.general_tile_ready.emit(payload)

    def _on_deep_ready(self, tile_id: str, payload: Any) -> None:
        self.deep_tile_ready.emit(tile_id, payload)

    def _on_scope_ready(self, payload: Any) -> None:
        self.scope_index_ready.emit(payload)

    def _on_extension_ready(self, payload: Any) -> None:
        self.extension_ready.emit(payload)

    def _on_error(self, message: str) -> None:
        self.error_occurred.emit(message)


class QtWeatherAdapter(QObject):
    """Qt signal bridge for weather port notifications."""

    weather_sample_ready = pyqtSignal(object)

    def __init__(
        self,
        port: WeatherPort,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._port = port

    def shutdown(self) -> None:
        self._port.shutdown()
