import math

import numpy as np

from TerraLab.terrain.engine import HorizonBaker, HorizonProfile, generate_bands, limit_profile_radius
from TerraLab.terrain.visibility_range import (
    EARTH_RADIUS_M,
    TerrainRangeSettings,
    horizon_distance_m,
    resolve_visibility_range,
)


def test_auto_range_at_sea_level_includes_target_horizon():
    result = resolve_visibility_range(TerrainRangeSettings(atmospheric_refraction_enabled=False), 0.0)
    assert result.resolved_radius_m == result.target_horizon_m
    assert math.isclose(result.target_horizon_m, horizon_distance_m(8849.0, EARTH_RADIUS_M))


def test_auto_range_from_mountain_adds_both_horizons():
    result = resolve_visibility_range(TerrainRangeSettings(atmospheric_refraction_enabled=False), 3000.0)
    assert math.isclose(result.calculated_radius_m, result.observer_horizon_m + result.target_horizon_m)


def test_manual_minimum_and_maximum_clamps():
    low = resolve_visibility_range(TerrainRangeSettings(mode="manual", manual_radius_km=-1), 0)
    high = resolve_visibility_range(TerrainRangeSettings(mode="manual", manual_radius_km=900), 0)
    assert low.resolved_radius_m == 25_000
    assert high.resolved_radius_m == 530_000


def test_refraction_extends_auto_range():
    plain = resolve_visibility_range(TerrainRangeSettings(atmospheric_refraction_enabled=False), 1000)
    refracted = resolve_visibility_range(TerrainRangeSettings(atmospheric_refraction_enabled=True), 1000)
    assert refracted.resolved_radius_m > plain.resolved_radius_m


def test_legacy_mapping_uses_auto_defaults():
    settings = TerrainRangeSettings.from_mapping({})
    assert settings.mode == "auto"
    assert settings.maximum_radius_km == 530.0


def test_bands_and_mesh_reach_same_resolved_radius_with_bounded_lod():
    for radius in (150_000.0, 300_000.0, 530_000.0):
        bands = generate_bands(20, max_dist_m=radius)
        rings = HorizonBaker._mesh_distance_rings(radius, 30.0)
        assert math.isclose(bands[-1]["max"], radius)
        assert math.isclose(float(rings[-1]), radius)
        assert len(rings) <= 395


def test_profile_radius_metadata_invalidates_legacy_and_short_cache(tmp_path):
    base = dict(azimuths=np.array([0.0]), bands=[])
    legacy_path = tmp_path / "legacy.npz"
    HorizonProfile(**base).save(str(legacy_path))
    assert not HorizonProfile.load(str(legacy_path)).covers_radius(1.0)
    ranged_path = tmp_path / "ranged.npz"
    HorizonProfile(**base, resolved_radius_m=300_000.0).save(str(ranged_path))
    loaded = HorizonProfile.load(str(ranged_path))
    assert loaded.covers_radius(300_000.0)
    assert not loaded.covers_radius(300_001.0)


def test_range_settings_are_serialized_for_the_bake_subprocess():
    import json
    from TerraLab.terrain.worker import HorizonWorker

    worker = HorizonWorker()
    job = {
        "job_id": "test", "lat": 0, "lon": 0, "tiles_dir": ".",
        "bands": 20, "range_settings": TerrainRangeSettings(mode="manual", manual_radius_km=300).to_dict(),
    }
    _cwd, command = worker._build_subprocess_command(job, "out.npz", "preview.npz")
    index = command.index("--range-settings-json")
    transmitted = json.loads(command[index + 1])
    assert transmitted["mode"] == "manual"
    assert transmitted["manual_radius_km"] == 300


def test_reducing_visible_radius_keeps_source_profile_and_slices_mesh():
    band_defs = generate_bands(20, max_dist_m=300_000.0)
    bands = [{"id": item["id"]} for item in band_defs]
    distances = np.array([1_000.0, 25_000.0, 100_000.0, 300_000.0])
    mesh = {
        "distances": distances,
        "altitudes": np.zeros((4, 2)),
        "elevations": np.zeros((4, 2)),
        "valid": np.ones((4, 2), dtype=bool),
        "visible": np.ones((4, 2), dtype=bool),
    }
    source = HorizonProfile(
        azimuths=np.array([0.0, 180.0]), bands=bands,
        terrain_mesh=mesh, resolved_radius_m=300_000.0,
    )
    source._band_defs = band_defs
    limited = limit_profile_radius(source, 100_000.0)
    assert source.resolved_radius_m == 300_000.0
    assert source.terrain_mesh["distances"].size == 4
    assert limited.resolved_radius_m == 100_000.0
    assert limited.terrain_mesh["distances"].tolist() == [1_000.0, 25_000.0, 100_000.0]
    assert all(item["max"] <= 100_000.0 for item in limited._band_defs)
