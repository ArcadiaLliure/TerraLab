from pathlib import Path
import threading
import time
from types import SimpleNamespace

import numpy as np
from PyQt5.QtCore import QCoreApplication

from TerraLab.terrain.data_sources import (
    DataSourceRegistry,
    LayerSelectionService,
    LayerType,
)
from TerraLab.terrain.engine import build_flat_horizon_profile
from TerraLab.terrain.terrain_coordinator import TerrainCoordinator
from TerraLab.terrain.worker import HorizonWorker


def _raster_placeholder(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_light_pollution_provider_switches_with_observer_coverage(
    monkeypatch,
    tmp_path,
):
    registry = DataSourceRegistry(
        tmp_path / "sources.json", legacy_reader={}
    )
    europe = registry.register_path(
        _raster_placeholder(tmp_path / "europe.tif"),
        LayerType.LIGHT_POLLUTION,
        source_id="europe",
        priority=10,
        coverage=(-15.0, 30.0, 45.0, 72.0),
    )
    local = registry.register_path(
        _raster_placeholder(tmp_path / "local.tif"),
        LayerType.LIGHT_POLLUTION,
        source_id="local",
        priority=100,
        coverage=(0.0, 40.0, 4.0, 43.5),
    )
    monkeypatch.setattr(
        "TerraLab.terrain.worker.get_config_value",
        lambda key, default=None: True
        if key == "light_pollution_enabled"
        else default,
    )
    worker = HorizonWorker()
    worker.data_source_registry = registry
    worker.layer_selection = LayerSelectionService(registry)

    worker._ensure_light_sampler_for_location(41.4, 2.1)
    assert Path(worker.light_sampler.raster_path) == Path(local.path)

    worker._ensure_light_sampler_for_location(50.0, 8.0)
    assert Path(worker.light_sampler.raster_path) == Path(europe.path)
    worker.light_sampler.close()


def test_bake_command_carries_explicit_mode_and_typed_source_snapshot(
    tmp_path,
):
    worker = HorizonWorker()
    manifest = tmp_path / "sources.json"
    manifest.write_text('{"version": 1, "sources": []}', encoding="utf-8")
    light_manifest = tmp_path / "light-sources.json"
    light_manifest.write_text(
        '{"version": 1, "sources": []}', encoding="utf-8"
    )
    job = {
        "job_id": "job-1",
        "lat": 41.4,
        "lon": 2.1,
        "tiles_dir": "",
        "bands": 20,
        "representation_mode": "profile",
        "elevation_source_ids": ("local", "europe"),
        "effective_elevation_source_id": "local",
        "elevation_source_status": "manual",
        "light_pollution_path": str(tmp_path / "dvnl.tif"),
    }

    _cwd, command = worker._build_subprocess_command(
        job,
        str(tmp_path / "final.npz"),
        str(tmp_path / "preview.npz"),
        str(manifest),
        str(light_manifest),
    )

    assert command[command.index("--representation-mode") + 1] == "profile"
    assert command[command.index("--elevation-sources-json") + 1] == str(
        manifest
    )
    assert command.count("--elevation-source-id") == 2
    assert command[command.index("--effective-elevation-source-id") + 1] == (
        "local"
    )
    assert command[command.index("--light-pollution-path") + 1].endswith(
        "dvnl.tif"
    )
    assert command[
        command.index("--light-pollution-sources-json") + 1
    ] == str(light_manifest)


def test_surface_sampling_service_and_cache_survive_identical_refreshes(
    monkeypatch,
    tmp_path,
):
    import TerraLab.terrain.surface as surface_module

    created_services = []
    created_providers = []

    class FakeProvider:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeService:
        def __init__(self, providers):
            self.providers = tuple(providers)
            self.closed = False
            self.cache = None
            self.sample_calls = 0
            created_services.append(self)

        def sample_profile(self, profile, *, geometry_id=""):
            self.sample_calls += 1
            if self.cache is None:
                self.cache = SimpleNamespace(
                    source_ids=("surface",),
                    geometry_id=geometry_id,
                )
            return self.cache

        def close(self):
            self.closed = True
            for provider in self.providers:
                provider.close()

    def create_providers(_sources):
        provider = FakeProvider()
        created_providers.append(provider)
        return (provider,)

    monkeypatch.setattr(
        surface_module, "create_surface_providers", create_providers
    )
    monkeypatch.setattr(surface_module, "SurfaceSamplingService", FakeService)

    source = SimpleNamespace(
        id="surface",
        layer_type=LayerType.SURFACE_RGB,
        path=str(tmp_path / "surface.tif"),
        crs="EPSG:4326",
        fingerprint="version-1",
        metadata={},
    )
    selection = SimpleNamespace(reason="automatic")
    worker = HorizonWorker()
    monkeypatch.setattr(
        worker,
        "_surface_selection",
        lambda _lat, _lon: (selection, [source]),
    )
    profile = build_flat_horizon_profile(
        observer_lat=41.4,
        observer_lon=2.1,
        representation_mode="profile",
        geometry_id="geometry-1",
    )

    worker._prepare_surface_samples(profile)
    first_cache = profile.surface_samples
    worker._prepare_surface_samples(profile)

    assert len(created_services) == 1
    assert created_services[0].sample_calls == 2
    assert profile.surface_samples is first_cache
    assert created_providers[0].closed is False

    source.fingerprint = "version-2"
    worker._prepare_surface_samples(profile)
    assert len(created_services) == 2
    assert created_services[0].closed is True
    assert created_providers[0].closed is True

    worker.shutdown()
    assert created_services[1].closed is True


def test_terrain_coordinator_shutdown_never_leaves_worker_thread_running():
    _app = QCoreApplication.instance() or QCoreApplication([])
    coordinator = TerrainCoordinator()

    coordinator.shutdown()
    coordinator.shutdown()

    assert coordinator.thread.isRunning() is False


def test_terrain_coordinator_never_runs_surface_refresh_on_gui_thread(monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    entered = threading.Event()
    release = threading.Event()
    execution_threads = []

    def slow_surface_refresh(_worker, _payload):
        execution_threads.append(threading.get_ident())
        entered.set()
        release.wait(timeout=2.0)

    monkeypatch.setattr(
        HorizonWorker,
        "request_surface_refresh",
        slow_surface_refresh,
    )
    coordinator = TerrainCoordinator()
    gui_thread_id = threading.get_ident()
    try:
        started = time.perf_counter()
        coordinator.request_surface_refresh(object())
        assert time.perf_counter() - started < 0.1

        deadline = time.monotonic() + 2.0
        while not entered.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)

        assert entered.is_set()
        assert len(execution_threads) == 1
        assert execution_threads[0] != gui_thread_id
    finally:
        release.set()
        coordinator.shutdown()


def test_terrain_coordinator_runs_pending_surface_refresh_when_profile_arrives(
    monkeypatch,
):
    app = QCoreApplication.instance() or QCoreApplication([])
    entered = threading.Event()
    received = []

    def record_surface_refresh(_worker, payload):
        received.append(payload)
        entered.set()

    monkeypatch.setattr(
        HorizonWorker,
        "request_surface_refresh",
        record_surface_refresh,
    )
    coordinator = TerrainCoordinator()
    profile = object()
    try:
        coordinator.request_surface_refresh(
            visible_radius_m=42_000.0,
            view_azimuth_deg=135.0,
            view_fov_deg=80.0,
        )
        assert coordinator._surface_request_generation == 0

        coordinator.ingest_profile_payload(profile)
        deadline = time.monotonic() + 2.0
        while not entered.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)

        assert entered.is_set()
        assert len(received) == 1
        assert received[0]["profile"] is profile
        assert received[0]["visible_radius_m"] == 42_000.0
        assert received[0]["view_azimuth_deg"] == 135.0
        assert received[0]["view_fov_deg"] == 80.0
    finally:
        coordinator.shutdown()


def test_terrain_coordinator_can_cancel_pending_surface_refresh(monkeypatch):
    received = []

    monkeypatch.setattr(
        HorizonWorker,
        "request_surface_refresh",
        lambda _worker, payload: received.append(payload),
    )
    coordinator = TerrainCoordinator()
    try:
        coordinator.request_surface_refresh(view_fov_deg=80.0)
        coordinator.cancel_surface_refresh()
        coordinator.ingest_profile_payload(object())

        assert coordinator._pending_surface_request is None
        assert coordinator._surface_request_generation == 0
        assert received == []
    finally:
        coordinator.shutdown()


def test_surface_runtime_source_uses_first_provider_with_valid_samples():
    cache = SimpleNamespace(
        source_ids=("rgb", "categorical"),
        profile_valid=np.asarray([[True, True]], dtype=bool),
        profile_source_indices=np.asarray([[1, 1]], dtype=np.int16),
        relief_valid=None,
        relief_source_indices=None,
    )
    assert HorizonWorker._effective_surface_source_id(cache) == "categorical"

    cache.profile_valid[:] = False
    assert HorizonWorker._effective_surface_source_id(cache) is None


def test_rgb_and_categorical_are_alternative_persisted_active_products(
    monkeypatch, tmp_path
):
    registry = DataSourceRegistry(tmp_path / "sources.json", legacy_reader={})
    rgb = registry.register_path(
        _raster_placeholder(tmp_path / "s2glc-rgb.tif"),
        LayerType.LAND_COVER_RGB,
        source_id="rgb",
    )
    categorical = registry.register_path(
        _raster_placeholder(tmp_path / "s2glc-codes.tif"),
        LayerType.LAND_COVER_CATEGORICAL,
        source_id="categorical",
    )
    monkeypatch.setattr(
        "TerraLab.terrain.worker.get_config_value",
        lambda key, default=None: True
        if key == "ui.visibility.earth.surface"
        else default,
    )
    worker = HorizonWorker()
    worker.data_source_registry = registry
    worker.layer_selection = LayerSelectionService(registry)

    automatic, automatic_sources = worker._surface_selection(41.0, 2.0)
    assert automatic.effective.id == rgb.id
    assert [source.id for source in automatic_sources] == [rgb.id]

    registry.set_selection("surface", categorical.id)
    reloaded = DataSourceRegistry(tmp_path / "sources.json", legacy_reader={})
    worker.data_source_registry = reloaded
    worker.layer_selection = LayerSelectionService(reloaded)
    manual, manual_sources = worker._surface_selection(41.0, 2.0)
    assert manual.effective.id == categorical.id
    assert [source.id for source in manual_sources] == [categorical.id]
    worker.shutdown()


def test_queued_surface_refresh_resolves_latest_worker_profile(monkeypatch):
    worker = HorizonWorker()
    latest = build_flat_horizon_profile(
        observer_lat=42.0,
        observer_lon=3.0,
        geometry_id="latest",
    )
    worker._last_profile = latest
    sampled = []
    monkeypatch.setattr(
        worker,
        "_prepare_surface_samples",
        lambda profile: sampled.append(profile) or profile,
    )
    monkeypatch.setattr(worker, "_publish_effective_sources", lambda *_: None)

    worker.request_surface_refresh(None)

    assert sampled == [latest]
    assert worker._last_profile is latest
    worker.shutdown()


def test_surface_refresh_publishes_visible_fov_before_complete_background(monkeypatch):
    worker = HorizonWorker()
    profile = build_flat_horizon_profile(
        observer_lat=42.0,
        observer_lon=3.0,
        geometry_id="progressive",
    )
    requests = []
    published = []

    def prepare(target, *, surface_request=None, progress_callback=None, abort_check=None):
        requests.append(surface_request)
        target.surface_samples = SimpleNamespace(
            completion_state=surface_request.stage,
            source_ids=(),
        )
        if progress_callback is not None:
            progress_callback(100.0, "completed")
        return target

    monkeypatch.setattr(worker, "_prepare_surface_samples", prepare)
    monkeypatch.setattr(worker, "_publish_effective_sources", lambda *_: None)
    worker.profile_ready.connect(published.append)

    worker.request_surface_refresh(
        {
            "profile": profile,
            "generation": 0,
            "view_azimuth_deg": 180.0,
            "view_fov_deg": 90.0,
            "visible_radius_m": 50_000.0,
        }
    )

    assert [request.stage for request in requests] == [
        "visible_partial",
        "complete",
    ]
    assert [payload["stage"] for payload in published] == [
        "visible_partial",
        "complete",
    ]
    assert published[0]["profile"] is not profile
    assert published[1]["profile"] is profile
    worker.shutdown()


def test_surface_row_progress_is_throttled_before_entering_the_qt_queue(monkeypatch):
    worker = HorizonWorker()
    emitted = []
    text_emitted = []
    worker.progress_state.connect(emitted.append)
    worker.progress_message.connect(text_emitted.append)
    clock = iter([0.0, *[0.01 * value for value in range(1, 101)], 2.0])
    monkeypatch.setattr(
        "TerraLab.terrain.worker.time.perf_counter", lambda: next(clock)
    )

    for percent in np.linspace(2.0, 6.0, 101):
        worker._emit_progress_state(
            {
                "kind": "surface",
                "phase": "complete:relief:reading-lod-rows",
                "percent": float(percent),
            }
        )
    worker._emit_progress_state(
        {"kind": "surface", "phase": "completed", "percent": 100.0}
    )

    assert len(emitted) <= 7
    assert emitted[0]["percent"] == 2.0
    assert emitted[-1]["percent"] == 100.0
    assert text_emitted == []
    assert worker.get_progress_state()["percent"] == 100.0
    worker.shutdown()
