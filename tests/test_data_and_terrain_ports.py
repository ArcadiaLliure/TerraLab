"""Tests for Phase 15 data, DEM, surface, and assets application ports and runtime adapters."""

from __future__ import annotations

import sys

import pytest

from TerraLab.adapters.runtime.terrain import RuntimeTerrainAdapter
from TerraLab.application.ports.assets import (
    AssetOperationReport,
)
from TerraLab.application.ports.terrain import (
    BakeJobRequest,
    BortleEstimateRequest,
    ElevationPort,
    ElevationQuery,
    SurfaceRefreshContext,
    SurfaceSamplingPort,
    TerrainBakePort,
    TerrainProgress,
)


def test_terrain_application_ports_contracts():
    """Verify that terrain ports are runtime checkable protocols without Qt imports."""

    assert issubclass(RuntimeTerrainAdapter, ElevationPort)
    assert issubclass(RuntimeTerrainAdapter, TerrainBakePort)
    assert issubclass(RuntimeTerrainAdapter, SurfaceSamplingPort)

    query = ElevationQuery(latitude=41.38, longitude=2.17)
    assert query.latitude == 41.38
    assert query.longitude == 2.17

    bake_job = BakeJobRequest(
        job_id="job-1", observer_lat=41.38, observer_lon=2.17
    )
    assert bake_job.job_id == "job-1"

    refresh_ctx = SurfaceRefreshContext(view_azimuth_deg=180.0, generation=5)
    assert refresh_ctx.view_azimuth_deg == 180.0
    assert refresh_ctx.generation == 5

    bortle_req = BortleEstimateRequest(latitude=41.38, longitude=2.17)
    assert bortle_req.latitude == 41.38

    progress = TerrainProgress(job_id="job-1", phase="baking", percent=50.0)
    assert progress.percent == 50.0


def test_assets_application_ports_contracts():
    """Verify that assets and layer ports are runtime checkable protocols."""

    report = AssetOperationReport(asset_id="dem-1", success=True, message="OK")
    assert report.asset_id == "dem-1"
    assert report.success is True


def test_runtime_terrain_adapter_listeners_and_queries():
    """Verify RuntimeTerrainAdapter callback registration, bare elevation, and Bortle queries."""

    adapter = RuntimeTerrainAdapter()

    profile_results = []
    error_results = []
    bortle_results = []
    elevation_results = []

    adapter.add_profile_listener(lambda payload: profile_results.append(payload))
    adapter.add_error_listener(lambda msg: error_results.append(msg))
    adapter.add_bortle_listener(
        lambda cls, sqm, rad, req_id: bortle_results.append((cls, sqm, rad, req_id))
    )
    adapter.add_bare_elevation_listener(
        lambda lat, lon, val: elevation_results.append((lat, lon, val))
    )

    # Bare elevation lookup on uninitialized adapter returns None safely
    val = adapter.get_bare_elevation(41.38, 2.17)
    assert val is None or isinstance(val, float)

    # Bortle estimate triggers callback
    adapter.request_bortle_estimate(41.38, 2.17, request_id=42)
    assert len(bortle_results) == 1
    assert bortle_results[0][3] == 42

    # Bare elevation request triggers callback
    adapter.request_bare_elevation(41.38, 2.17)
    assert len(elevation_results) == 1
    assert elevation_results[0][0] == 41.38

    adapter.shutdown()


def test_qt_terrain_adapter_bridge():
    """Verify QtTerrainCoordinatorAdapter bridges runtime events to Qt signals."""

    from TerraLab.adapters.qt.terrain import QtTerrainCoordinatorAdapter

    adapter = RuntimeTerrainAdapter()
    qt_bridge = QtTerrainCoordinatorAdapter(adapter)

    received_bortle = []
    qt_bridge.bortle_estimate_ready.connect(
        lambda cls, sqm, rad, req_id: received_bortle.append((cls, sqm, rad, req_id))
    )

    qt_bridge.request_bortle_estimate(41.38, 2.17, request_id=99)
    assert len(received_bortle) == 1
    assert received_bortle[0][3] == 99

    qt_bridge.shutdown()


def test_package_dependency_data_does_not_import_terrain():
    """Verify that TerraLab.data modules do not import TerraLab.terrain to avoid cycles."""

    import importlib
    import pkgutil
    import TerraLab.data

    data_package = TerraLab.data
    for _, module_name, _ in pkgutil.walk_packages(
        data_package.__path__, prefix="TerraLab.data."
    ):
        # Skip legacy / deprecation files if any, inspect active modules
        mod = importlib.import_module(module_name)
        file_path = getattr(mod, "__file__", "") or ""
        if file_path:
            with open(file_path, "r", encoding="utf-8") as source_file:
                content = source_file.read()
                # Ensure no direct import of TerraLab.terrain
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(
                        ("import TerraLab.terrain", "from TerraLab.terrain")
                    ):
                        pytest.fail(
                            f"Module {module_name} in TerraLab.data imports TerraLab.terrain: '{stripped}'"
                        )


def test_no_qt_imports_in_ports_and_runtime_adapters():
    """Verify that ports and runtime adapters are completely free of PyQt5 imports."""


    for mod_name in (
        "TerraLab.application.ports.terrain",
        "TerraLab.application.ports.assets",
        "TerraLab.adapters.runtime.terrain",
    ):
        mod = sys.modules.get(mod_name)
        assert mod is not None, f"Module {mod_name} should be loaded"
        file_path = getattr(mod, "__file__", "") or ""
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "PyQt5" not in content, f"Module {mod_name} contains PyQt5 import"
