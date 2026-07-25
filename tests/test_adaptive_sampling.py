import numpy as np
from types import SimpleNamespace

from TerraLab.terrain.raycast.baker import HorizonBaker
from TerraLab.terrain.render.sampling import TerrainSamplingSettings
from TerraLab.terrain.sampling import (
    adaptive_refine_ray,
    build_adaptive_base_distances,
    evaluate_refinement_intervals,
)


def _sampler(function):
    def sample(distances):
        distances = np.asarray(distances, dtype=np.float64)
        values = np.asarray(function(distances), dtype=np.float64)
        return values, np.isfinite(values)

    return sample


def test_flat_terrain_keeps_fewer_samples_than_legacy_progression():
    settings = TerrainSamplingSettings(
        sampling_near_step_m=25.0,
        sampling_far_step_m=400.0,
        sampling_step_growth=1.06,
    )

    result = adaptive_refine_ray(
        _sampler(lambda distance: np.full(distance.shape, 250.0)),
        maximum_distance_m=10_000.0,
        observer_eye_elevation_m=251.7,
        earth_radius_m=6_371_000.0,
        pixels_per_radian=1_000.0,
        settings=settings,
    )
    legacy = HorizonBaker._adaptive_distances(50.0, 10_000.0)

    assert result.distances.size < legacy.size


def test_abrupt_terrain_increases_retained_samples():
    settings = TerrainSamplingSettings(
        sampling_near_step_m=100.0,
        sampling_far_step_m=200.0,
        sampling_step_growth=2.0,
        sampling_max_elevation_error_m=5.0,
        sampling_max_projected_error_px=0.25,
        sampling_max_slope_delta_deg=2.0,
    )
    base = build_adaptive_base_distances(1_000.0, settings)
    result = adaptive_refine_ray(
        _sampler(
            lambda distance: 100.0
            + 300.0 * np.exp(-((distance - 200.5) / 18.0) ** 2)
        ),
        maximum_distance_m=1_000.0,
        observer_eye_elevation_m=101.7,
        earth_radius_m=6_371_000.0,
        pixels_per_radian=1_000.0,
        settings=settings,
    )

    assert result.distances.size > base.size


def test_narrow_crest_midpoint_is_not_lost():
    settings = TerrainSamplingSettings(
        sampling_near_step_m=100.0,
        sampling_far_step_m=200.0,
        sampling_step_growth=2.0,
        sampling_max_elevation_error_m=5.0,
        sampling_max_projected_error_px=0.25,
        sampling_max_slope_delta_deg=2.0,
    )
    result = adaptive_refine_ray(
        _sampler(
            lambda distance: np.where(
                np.abs(distance - 200.5) < 10.0, 600.0, 100.0
            )
        ),
        maximum_distance_m=1_000.0,
        observer_eye_elevation_m=101.7,
        earth_radius_m=6_371_000.0,
        pixels_per_radian=1_000.0,
        settings=settings,
    )

    assert np.max(result.elevations[result.valid]) == 600.0
    assert np.any(np.isclose(result.distances, 200.5))


def test_final_projected_error_is_bounded_above_minimum_step():
    settings = TerrainSamplingSettings(
        sampling_near_step_m=5.0,
        sampling_far_step_m=80.0,
        sampling_step_growth=1.4,
        sampling_max_elevation_error_m=1.0,
        sampling_max_projected_error_px=0.5,
        sampling_max_slope_delta_deg=1.5,
        sampling_max_subdivision_depth=8,
    )
    sample = _sampler(lambda distance: 120.0 + 0.0008 * (distance - 400.0) ** 2)
    result = adaptive_refine_ray(
        sample,
        maximum_distance_m=1_000.0,
        observer_eye_elevation_m=121.7,
        earth_radius_m=6_371_000.0,
        pixels_per_radian=1_000.0,
        settings=settings,
    )
    interval_width = np.diff(result.distances)
    candidates = np.flatnonzero(
        interval_width > settings.sampling_near_step_m + 1e-9
    )
    midpoints = 0.5 * (
        result.distances[candidates] + result.distances[candidates + 1]
    )
    midpoint_elevations, midpoint_valid = sample(midpoints)
    evaluation = evaluate_refinement_intervals(
        result.distances[candidates],
        result.elevations[candidates],
        result.valid[candidates],
        result.distances[candidates + 1],
        result.elevations[candidates + 1],
        result.valid[candidates + 1],
        midpoints,
        midpoint_elevations,
        midpoint_valid,
        observer_eye_elevation_m=121.7,
        earth_radius_m=6_371_000.0,
        pixels_per_radian=1_000.0,
        settings=settings,
    )

    assert np.max(evaluation.projected_error_px, initial=0.0) <= (
        settings.sampling_max_projected_error_px + 1e-6
    )


def test_adaptive_sampling_never_exceeds_configured_sample_cap():
    settings = TerrainSamplingSettings(
        sampling_near_step_m=2.0,
        sampling_far_step_m=100.0,
        sampling_step_growth=1.5,
        sampling_max_elevation_error_m=0.01,
        sampling_max_projected_error_px=0.01,
        sampling_max_slope_delta_deg=0.01,
        sampling_max_subdivision_depth=12,
        sampling_max_samples_per_ray=64,
    )
    result = adaptive_refine_ray(
        _sampler(lambda distance: 100.0 + 50.0 * np.sin(distance / 7.0)),
        maximum_distance_m=1_000.0,
        observer_eye_elevation_m=101.7,
        earth_radius_m=6_371_000.0,
        pixels_per_radian=1_000.0,
        settings=settings,
    )

    assert result.distances.size <= settings.sampling_max_samples_per_ray


def test_progressive_baker_reduces_independent_refined_ray_profiles():
    class RadialProvider:
        def sample_elevation(self, x, y):
            distance = np.hypot(np.asarray(x), np.asarray(y))
            values = np.where(
                np.abs(distance - 200.5) < 10.0, 600.0, 100.0
            ).astype(np.float32)
            return SimpleNamespace(
                values=values,
                valid=np.ones(values.shape, dtype=bool),
            )

        def get_elevation(self, x, y):
            distance = float(np.hypot(x, y))
            return 600.0 if abs(distance - 200.5) < 10.0 else 100.0

        def get_nominal_resolution_m(self):
            return 10.0

    settings = TerrainSamplingSettings(
        adaptive_sampling_enabled=True,
        sampling_near_step_m=100.0,
        sampling_far_step_m=200.0,
        sampling_step_growth=2.0,
        sampling_max_elevation_error_m=5.0,
        sampling_max_projected_error_px=0.25,
        sampling_max_slope_delta_deg=2.0,
    )
    baker = HorizonBaker(RadialProvider(), sampling_settings=settings)

    _azimuths, bands, *_rest = baker.bake_progressive(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=100.0,
        step_m=100.0,
        d_max=1_000.0,
        delta_az_deg=90.0,
        band_defs=[{"id": "all", "min": 0.0, "max": 1_000.0}],
    )

    np.testing.assert_allclose(bands[0]["heights"], 600.0)
    np.testing.assert_allclose(bands[0]["dists"], 200.5)
