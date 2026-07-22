import math

import numpy as np

from TerraLab.terrain.engine import compute_polar_mesh_normals
from TerraLab.terrain.render_pipeline import (
    TerrainRenderSettings,
    atmospheric_fog_factor,
    compose_vertex_rgba,
    lambert_intensity,
)


def _polar_grid(distances, azimuths, height_fn):
    distance_grid = np.asarray(distances, dtype=np.float32)[:, None]
    azimuth_grid = np.deg2rad(np.asarray(azimuths, dtype=np.float32))[None, :]
    x = distance_grid * np.sin(azimuth_grid)
    y = distance_grid * np.cos(azimuth_grid)
    return np.asarray(height_fn(x, y), dtype=np.float32)


def test_flat_terrain_normals_point_up_without_nan():
    distances = np.asarray([50.0, 100.0, 250.0, 500.0])
    azimuths = np.asarray([0.0, 90.0, 180.0, 270.0])
    elevations = np.full((distances.size, azimuths.size), 240.0)
    valid = np.ones(elevations.shape, dtype=bool)

    normals = np.stack(
        compute_polar_mesh_normals(elevations, valid, distances, azimuths),
        axis=-1,
    )

    assert np.all(np.isfinite(normals))
    np.testing.assert_allclose(
        normals,
        np.broadcast_to((0.0, 0.0, 1.0), normals.shape),
        atol=1e-6,
    )


def test_constant_slope_has_one_shared_normal_including_mesh_edges():
    distances = np.asarray([50.0, 100.0, 250.0, 500.0])
    azimuths = np.arange(0.0, 360.0, 30.0)
    slope_x = 0.2
    elevations = _polar_grid(distances, azimuths, lambda x, _y: 300.0 + slope_x * x)
    valid = np.ones(elevations.shape, dtype=bool)

    normals = np.stack(
        compute_polar_mesh_normals(elevations, valid, distances, azimuths),
        axis=-1,
    )
    expected = np.asarray((-slope_x, 0.0, 1.0))
    expected /= np.linalg.norm(expected)

    assert np.all(np.isfinite(normals))
    np.testing.assert_allclose(
        normals, np.broadcast_to(expected, normals.shape), atol=1.5e-2
    )


def test_ridge_normals_are_smooth_and_never_invert():
    distances = np.asarray([50.0, 100.0, 200.0, 400.0])
    azimuths = np.arange(0.0, 360.0, 15.0)
    elevations = _polar_grid(
        distances,
        azimuths,
        lambda x, y: 450.0 - 0.12 * np.abs(x) + 0.01 * y,
    )
    valid = np.ones(elevations.shape, dtype=bool)

    normals = np.stack(
        compute_polar_mesh_normals(elevations, valid, distances, azimuths),
        axis=-1,
    )

    assert np.all(np.isfinite(normals))
    assert np.all(normals[..., 2] > 0.0)
    assert np.max(np.linalg.norm(np.diff(normals, axis=1), axis=-1)) < 0.3


def test_invalid_neighbours_and_edges_do_not_create_nan_normals():
    distances = np.asarray([25.0, 75.0, 200.0])
    azimuths = np.arange(0.0, 360.0, 45.0)
    elevations = _polar_grid(distances, azimuths, lambda x, y: 100.0 + 0.1 * x - 0.05 * y)
    valid = np.ones(elevations.shape, dtype=bool)
    valid[0, 0] = False
    valid[1, 1:3] = False
    valid[-1, -1] = False

    normals = np.stack(
        compute_polar_mesh_normals(elevations, valid, distances, azimuths),
        axis=-1,
    )

    assert np.all(np.isfinite(normals))
    np.testing.assert_allclose(
        normals[~valid],
        np.broadcast_to((0.0, 0.0, 1.0), normals[~valid].shape),
    )


def test_lambert_uses_configured_light_and_bounds():
    settings = TerrainRenderSettings.from_mapping(
        {
            "terrain_light_azimuth_deg": 90.0,
            "terrain_light_elevation_deg": 0.0,
            "terrain_ambient_strength": 0.25,
            "terrain_diffuse_strength": 0.75,
            "terrain_min_brightness": 0.2,
            "terrain_max_brightness": 1.0,
        }
    )
    normals = np.asarray(((1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)))

    intensity = lambert_intensity(normals, settings)

    np.testing.assert_allclose(intensity, (1.0, 0.25), atol=1e-6)


def test_atmosphere_zero_intermediate_and_maximum_distance():
    settings = TerrainRenderSettings()
    distances = np.asarray((0.0, 75_000.0, 150_000.0))

    fog = atmospheric_fog_factor(
        distances,
        settings,
        maximum_distance_m=150_000.0,
    )

    assert fog[0] == 0.0
    assert 0.0 < fog[1] < 1.0
    assert math.isclose(float(fog[2]), 1.0, abs_tol=1e-6)


def test_atmosphere_progressively_desaturates_and_reaches_horizon_color():
    settings = TerrainRenderSettings()
    base = np.broadcast_to(
        np.asarray((30, 120, 220, 255), dtype=np.uint8), (3, 4)
    )
    distances = np.asarray((0.0, 75_000.0, 150_000.0))

    result = compose_vertex_rgba(
        base,
        np.ones(3),
        distances,
        settings,
        maximum_distance_m=150_000.0,
    )

    np.testing.assert_array_equal(result[0], base[0])
    assert int(np.ptp(result[1, :3])) < int(np.ptp(result[0, :3]))
    np.testing.assert_array_equal(
        result[-1, :3], np.asarray(settings.atmosphere_horizon_color)
    )
    assert result.dtype == np.uint8
    assert np.all((result >= 0) & (result <= 255))


def test_atmosphere_auto_scales_to_short_and_long_visibility_ranges():
    settings = TerrainRenderSettings()

    short = atmospheric_fog_factor(
        np.asarray((0.0, 25_000.0, 50_000.0)),
        settings,
        maximum_distance_m=50_000.0,
    )
    long = atmospheric_fog_factor(
        np.asarray((0.0, 265_000.0, 530_000.0)),
        settings,
        maximum_distance_m=530_000.0,
    )

    np.testing.assert_allclose(short, long, atol=1e-6)
