from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from TerraLab.terrain import bake_process
from TerraLab.terrain.engine import HorizonProfile, build_flat_horizon_profile
from TerraLab.terrain.representation import (
    TerrainGeometrySource,
    TerrainRepresentationMode,
    normalize_terrain_geometry_source,
    normalize_terrain_representation_mode,
)


def _band(size: int = 2) -> dict:
    return {
        "id": "near_0_100",
        "min": 0.0,
        "max": 100.0,
        "angles": np.linspace(0.0, 0.1, size, dtype=np.float32),
        "dists": np.full(size, 50.0, dtype=np.float32),
        "heights": np.full(size, 120.0, dtype=np.float32),
    }


def test_representation_enums_normalize_to_stable_values() -> None:
    assert str(TerrainRepresentationMode.PROFILE) == "profile"
    assert normalize_terrain_representation_mode("PROFILE") is (
        TerrainRepresentationMode.PROFILE
    )
    assert normalize_terrain_representation_mode("invalid") is (
        TerrainRepresentationMode.RELIEF
    )
    assert normalize_terrain_geometry_source("flat_fallback") is (
        TerrainGeometrySource.FLAT_FALLBACK
    )


def test_horizon_profile_roundtrip_preserves_representation_contract(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profile.npz"
    profile = HorizonProfile(
        azimuths=np.array([0.0, 180.0], dtype=np.float32),
        bands=[_band()],
        observer_lat=41.5,
        observer_lon=1.25,
        representation_mode=TerrainRepresentationMode.PROFILE,
        geometry_source=TerrainGeometrySource.REAL_ELEVATION,
        geometry_id="geometry-123",
        elevation_source_ids=("local-dem", "europe-dem"),
        effective_elevation_source_id="local-dem",
        elevation_source_status="available",
    )

    profile.save(path)
    loaded = HorizonProfile.load(path)

    assert loaded.schema_version >= 2
    assert loaded.representation_mode is TerrainRepresentationMode.PROFILE
    assert loaded.geometry_source is TerrainGeometrySource.REAL_ELEVATION
    assert loaded.geometry_id == "geometry-123"
    assert loaded.elevation_source_ids == ("local-dem", "europe-dem")
    assert loaded.effective_elevation_source_id == "local-dem"
    assert loaded.elevation_source_status == "available"
    assert loaded.bands[0]["min"] == 0.0
    assert loaded.bands[0]["max"] == 100.0


def test_horizon_profile_loads_legacy_npz_as_relief(tmp_path: Path) -> None:
    path = tmp_path / "legacy.npz"
    band = _band()
    np.savez_compressed(
        path,
        azimuths=np.array([0.0, 180.0], dtype=np.float32),
        observer_lat=np.asarray(41.5),
        observer_lon=np.asarray(1.25),
        n_bands=np.asarray(1),
        light_domes=np.zeros(2, dtype=np.float32),
        light_peak_distances=np.zeros(2, dtype=np.float32),
        band_0_id=np.asarray(band["id"]),
        band_0_angles=band["angles"],
        band_0_dists=band["dists"],
        band_0_heights=band["heights"],
    )

    loaded = HorizonProfile.load(path)

    assert loaded.schema_version == 1
    assert loaded.representation_mode is TerrainRepresentationMode.RELIEF
    assert loaded.geometry_source is TerrainGeometrySource.LEGACY_UNKNOWN
    assert loaded.elevation_source_status == "legacy_unknown"


@pytest.mark.parametrize(
    "mode",
    [TerrainRepresentationMode.PROFILE, TerrainRepresentationMode.RELIEF],
)
def test_flat_horizon_preserves_preference_without_topographic_mesh(
    mode,
) -> None:
    profile = build_flat_horizon_profile(
        observer_lat=41.5,
        observer_lon=1.25,
        representation_mode=mode,
        band_defs=[{"id": "flat_0_150k", "min": 0.0, "max": 150_000.0}],
    )

    assert profile.representation_mode is mode
    assert profile.geometry_source is TerrainGeometrySource.FLAT_FALLBACK
    assert profile.terrain_mesh is None
    assert bool(np.all(profile.resolved_mask))
    assert np.allclose(profile.bands[0]["angles"], 0.0)
    assert profile.geometry_id.startswith("flat:")


def test_flat_surface_sampling_distances_survive_profile_round_trip(
    tmp_path: Path,
) -> None:
    profile = build_flat_horizon_profile(
        observer_lat=41.4,
        observer_lon=2.1,
        representation_mode=TerrainRepresentationMode.RELIEF,
    )
    path = tmp_path / "flat_profile.npz"

    profile.save(path)
    loaded = HorizonProfile.load(path)

    assert np.all(loaded.bands[0]["dists"] == 0.0)
    assert np.all(loaded.bands[0]["surface_dists"] > 0.0)


@pytest.mark.parametrize(
    "mode",
    [TerrainRepresentationMode.PROFILE, TerrainRepresentationMode.RELIEF],
)
def test_bake_without_dem_completes_with_explicit_flat_fallback(
    monkeypatch, tmp_path: Path, mode
) -> None:
    events = []
    monkeypatch.setattr(
        bake_process,
        "_emit_event",
        lambda event_type, **payload: events.append(
            {"type": event_type, **payload}
        ),
    )
    monkeypatch.setattr(
        bake_process.HorizonBaker,
        "build_view_mesh",
        lambda *_args, **_kwargs: pytest.fail(
            "flat fallback must not build a topographic mesh"
        ),
    )
    output = tmp_path / f"{mode.value}.npz"
    preview = tmp_path / f"{mode.value}-preview.npz"

    returned = bake_process.main(
        [
            "--job-id",
            f"job-{mode.value}",
            "--lat",
            "41.5",
            "--lon",
            "1.25",
            "--representation-mode",
            mode.value,
            "--elevation-source-id",
            "configured-dem",
            "--effective-elevation-source-id",
            "configured-dem",
            "--elevation-source-status",
            "manual",
            "--output",
            str(output),
            "--preview-path",
            str(preview),
        ]
    )

    loaded = HorizonProfile.load(output)
    assert returned.representation_mode is mode
    assert loaded.representation_mode is mode
    assert loaded.geometry_source is TerrainGeometrySource.FLAT_FALLBACK
    assert loaded.terrain_mesh is None
    assert loaded.elevation_source_ids == ("configured-dem",)
    assert loaded.effective_elevation_source_id is None
    assert returned.effective_elevation_source_id is None
    assert loaded.elevation_source_status == "fallback_no_elevation"
    assert any(event["type"] == "done" for event in events)
    assert not any(event.get("phase") == "terrain_mesh" for event in events)


class _FakeProvider:
    def transform_coordinates(self, _lat, _lon):
        return 0.0, 0.0

    def prepare_region(self, *_args, **_kwargs):
        return None

    def get_elevation(self, _x, _y):
        return 100.0

    def get_nominal_resolution_m(self):
        return 25.0

    def close(self):
        return None


class _FallbackProvenanceProvider(_FakeProvider):
    internal_crs = "EPSG:25831"

    def __init__(self):
        self.providers = (
            SimpleNamespace(source_id="local-dem"),
            SimpleNamespace(source_id="europe-dem"),
        )

    def sample_elevations(self, _x, _y, *, input_crs=None):
        assert input_crs == self.internal_crs
        return SimpleNamespace(
            values=np.asarray(100.0, dtype=np.float32),
            valid=np.asarray(True),
            source_indices=np.asarray(1, dtype=np.int16),
        )

    def get_elevation(self, _x, _y):
        pytest.fail("typed observer sampling must retain source provenance")


class _FakeLightSampler:
    def __init__(self, _path=None):
        pass

    def close(self):
        return None


class _FakeBaker:
    build_calls = 0

    def __init__(self, _provider):
        pass

    def bake_progressive(self, **kwargs):
        azimuths = np.array([0.0, 180.0], dtype=np.float32)
        bands = []
        for definition in kwargs["band_defs"]:
            item = dict(definition)
            item.update(_band(size=2))
            item["id"] = definition["id"]
            item["min"] = definition["min"]
            item["max"] = definition["max"]
            bands.append(item)
        zeros = np.zeros(2, dtype=np.float32)
        resolved = np.ones(2, dtype=bool)
        preview_callback = kwargs.get("preview_callback")
        if preview_callback is not None:
            preview_callback(2, 2, azimuths, bands, zeros, zeros, resolved)
        return azimuths, bands, zeros, zeros, resolved

    def build_view_mesh(self, **_kwargs):
        type(self).build_calls += 1
        return {
            "version": 2,
            "azimuths": np.array([0.0, 180.0], dtype=np.float32),
            "distances": np.array([100.0], dtype=np.float32),
            "altitudes": np.zeros((1, 2), dtype=np.float32),
            "valid": np.ones((1, 2), dtype=bool),
            "visible": np.ones((1, 2), dtype=bool),
        }


@pytest.mark.parametrize(
    ("mode", "expected_build_calls", "expects_mesh"),
    [
        (TerrainRepresentationMode.PROFILE, 0, False),
        (TerrainRepresentationMode.RELIEF, 1, True),
    ],
)
def test_real_dem_builds_mesh_only_for_relief(
    monkeypatch,
    tmp_path: Path,
    mode,
    expected_build_calls: int,
    expects_mesh: bool,
) -> None:
    from TerraLab.terrain import light_pollution_sampler

    events = []
    _FakeBaker.build_calls = 0
    monkeypatch.setattr(
        bake_process, "_path_has_elevation_data", lambda _p: True
    )
    monkeypatch.setattr(
        bake_process, "_create_provider", lambda *_a, **_k: _FakeProvider()
    )
    monkeypatch.setattr(bake_process, "HorizonBaker", _FakeBaker)
    monkeypatch.setattr(
        bake_process, "_resolve_light_pollution_path", lambda: ""
    )
    monkeypatch.setattr(
        light_pollution_sampler, "LightPollutionSampler", _FakeLightSampler
    )
    monkeypatch.setattr(
        bake_process,
        "_emit_event",
        lambda event_type, **payload: events.append(
            {"type": event_type, **payload}
        ),
    )
    output = tmp_path / f"real-{mode.value}.npz"
    preview = tmp_path / f"real-{mode.value}-preview.npz"

    returned = bake_process.main(
        [
            "--job-id",
            f"real-{mode.value}",
            "--lat",
            "41.5",
            "--lon",
            "1.25",
            "--tiles-dir",
            str(tmp_path / "dem"),
            "--representation-mode",
            mode.value,
            "--bands",
            "1",
            "--output",
            str(output),
            "--preview-path",
            str(preview),
        ]
    )

    loaded = HorizonProfile.load(output)
    loaded_preview = HorizonProfile.load(preview)
    assert _FakeBaker.build_calls == expected_build_calls
    assert (returned.terrain_mesh is not None) is expects_mesh
    assert (loaded.terrain_mesh is not None) is expects_mesh
    assert loaded.representation_mode is mode
    assert loaded_preview.representation_mode is mode
    assert loaded_preview.terrain_mesh is None
    assert loaded.geometry_source is TerrainGeometrySource.REAL_ELEVATION
    assert any(event["type"] == "done" for event in events)
    assert (
        any(event.get("phase") == "terrain_mesh" for event in events)
        is expects_mesh
    )


def test_bake_persists_observer_sample_fallback_source_in_preview_and_final(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events = []
    monkeypatch.setattr(
        bake_process, "_path_has_elevation_data", lambda _p: True
    )
    monkeypatch.setattr(
        bake_process,
        "_create_provider",
        lambda *_args, **_kwargs: _FallbackProvenanceProvider(),
    )
    monkeypatch.setattr(bake_process, "HorizonBaker", _FakeBaker)
    monkeypatch.setattr(
        bake_process, "_resolve_light_pollution_path", lambda _path=None: ""
    )
    monkeypatch.setattr(
        bake_process,
        "_emit_event",
        lambda event_type, **payload: events.append(
            {"type": event_type, **payload}
        ),
    )
    output = tmp_path / "fallback-source-final.npz"
    preview = tmp_path / "fallback-source-preview.npz"

    returned = bake_process.main(
        [
            "--job-id",
            "fallback-source",
            "--lat",
            "41.5",
            "--lon",
            "1.25",
            "--tiles-dir",
            str(tmp_path / "dem"),
            "--representation-mode",
            TerrainRepresentationMode.PROFILE.value,
            "--bands",
            "1",
            "--elevation-source-id",
            "local-dem",
            "--elevation-source-id",
            "europe-dem",
            "--effective-elevation-source-id",
            "local-dem",
            "--elevation-source-status",
            "manual",
            "--output",
            str(output),
            "--preview-path",
            str(preview),
        ]
    )

    loaded = HorizonProfile.load(output)
    loaded_preview = HorizonProfile.load(preview)
    for profile in (returned, loaded, loaded_preview):
        assert profile.elevation_source_ids == (
            "local-dem",
            "europe-dem",
        )
        assert profile.effective_elevation_source_id == "europe-dem"
        assert profile.elevation_source_status == "manual_sample_fallback"
    assert any(event["type"] == "preview" for event in events)
    assert any(event["type"] == "done" for event in events)


def test_legacy_observer_provider_keeps_requested_source_compatibility() -> None:
    args = SimpleNamespace(
        elevation_source_id=("legacy-dem",),
        effective_elevation_source_id="legacy-dem",
        elevation_source_status="manual",
    )
    provider = _FakeProvider()

    ground_h, sampled_source_id, has_runtime_provenance = (
        bake_process._observer_elevation_sample(provider, 0.0, 0.0)
    )
    source_ids, effective_source_id, status = (
        bake_process._runtime_source_metadata(
            args,
            provider,
            sampled_source_id,
            has_runtime_provenance,
        )
    )

    assert ground_h == 100.0
    assert source_ids == ("legacy-dem",)
    assert effective_source_id == "legacy-dem"
    assert status == "manual"
