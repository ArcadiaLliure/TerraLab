from pathlib import Path
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
