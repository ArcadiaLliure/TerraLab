"""Tests for Phase 14: Compute Ports, Runtime Adapters, Qt Bridges, and Import Safety."""

from __future__ import annotations

from unittest.mock import MagicMock


from TerraLab.application.ports.compute import (
    EphemerisPort,
    EphemerisRequest,
    StarCatalogPort,
    StarTileRequest,
    WeatherPort,
    WeatherSampleRequest,
)
from TerraLab.astro.ephemeris_coordinator import EphemerisCoordinator
from TerraLab.adapters.runtime.compute import (
    RuntimeEphemerisAdapter,
    RuntimeStarCatalogAdapter,
    RuntimeWeatherAdapter,
)


def test_compute_ports_and_dto_contracts() -> None:
    req = EphemerisRequest(
        year_utc=2026,
        day_of_year_utc=223,
        ut_hour=18.5,
        latitude=41.6,
        longitude=0.6,
    )
    assert req.year_utc == 2026
    assert req.day_of_year_utc == 223

    tile_req = StarTileRequest(manifest_path="catalog/manifest.json")
    assert tile_req.operation == "catalog_general"

    w_req = WeatherSampleRequest(
        latitude=41.6,
        longitude=0.6,
        year_utc=2026,
        day_of_year_utc=223,
        ut_hour=18.0,
    )
    assert w_req.use_remote is True


def test_ephemeris_coordinator_pure_model_listeners() -> None:
    coordinator = EphemerisCoordinator()
    received_snapshots: list[dict] = []

    def on_ready(snapshot: dict) -> None:
        received_snapshots.append(snapshot)

    coordinator.add_ready_listener(on_ready)
    coordinator.configure_observer(41.60775, 0.60794)

    try:
        snapshot = coordinator._compute_snapshot(
            year_utc=2026,
            day_of_year_utc=223,
            ut_hour=18.5,
            latitude=41.60775,
            longitude=0.60794,
        )
        assert "timestamp_utc" in snapshot
        assert "sun" in snapshot
        assert "moon" in snapshot
    finally:
        coordinator.shutdown()


def test_runtime_ephemeris_adapter_protocol() -> None:
    mock_runtime = MagicMock()
    adapter = RuntimeEphemerisAdapter(mock_runtime)

    assert isinstance(adapter, EphemerisPort)
    adapter.configure_observer(41.6, 0.6)
    adapter.request_snapshot(year_utc=2026, day_of_year_utc=223, ut_hour=18.5)

    assert mock_runtime.send.called
    adapter.shutdown()


def test_runtime_star_catalog_adapter_protocol() -> None:
    mock_runtime = MagicMock()
    adapter = RuntimeStarCatalogAdapter(mock_runtime, "catalog/manifest.json")

    assert isinstance(adapter, StarCatalogPort)
    adapter.load_general_tile()

    assert mock_runtime.send.called
    adapter.shutdown()


def test_runtime_weather_adapter_protocol() -> None:
    adapter = RuntimeWeatherAdapter(
        latitude=41.6, longitude=0.6, use_remote=False, cache_enabled=False
    )
    assert isinstance(adapter, WeatherPort)
    req = WeatherSampleRequest(
        latitude=41.6,
        longitude=0.6,
        year_utc=2026,
        day_of_year_utc=223,
        ut_hour=18.0,
        use_remote=False,
    )
    sample = adapter.get_weather_sample(req)
    assert isinstance(sample, dict)
    adapter.shutdown()


def test_qt_compute_adapters_bridge() -> None:
    from TerraLab.adapters.qt.compute import (
        QtEphemerisAdapter,
        QtStarCatalogAdapter,
    )

    mock_ephemeris_port = MagicMock()
    qt_eph = QtEphemerisAdapter(mock_ephemeris_port)
    qt_eph.configure_observer(41.6, 0.6)
    mock_ephemeris_port.configure_observer.assert_called_with(41.6, 0.6)

    mock_star_port = MagicMock()
    qt_star = QtStarCatalogAdapter(mock_star_port)
    qt_star.load_general_tile()
    mock_star_port.load_general_tile.assert_called_once()
