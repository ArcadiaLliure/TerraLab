import math

import numpy as np

from TerraLab.terrain.engine import compute_polar_mesh_normals
from TerraLab.terrain.render_pipeline import (
    SurfaceVisualStyle,
    TerrainCelestialLightContext,
    TerrainRenderSettings,
    apply_vibrant_color_grade,
    atmospheric_fog_factor,
    compose_vertex_rgba,
    lambert_intensity,
    terrain_celestial_light_factors,
    vibrant_depth_haze_factor,
)


def _polar_grid(distances, azimuths, height_fn):
    distance_grid = np.asarray(distances, dtype=np.float32)[:, None]
    azimuth_grid = np.deg2rad(np.asarray(azimuths, dtype=np.float32))[None, :]
    x = distance_grid * np.sin(azimuth_grid)
    y = distance_grid * np.cos(azimuth_grid)
    return np.asarray(height_fn(x, y), dtype=np.float32)


def test_celestial_light_factors_are_continuous_and_phase_gated():
    settings = TerrainRenderSettings()
    altitudes = (-18.001, -18.0, -17.999, -0.834, -0.833, -0.832, 5.999, 6.0, 6.001)
    ambient = []
    direct = []
    for altitude in altitudes:
        factors = terrain_celestial_light_factors(
            TerrainCelestialLightContext(altitude, 120.0),
            settings,
        )
        ambient.append(factors.solar_ambient)
        direct.append(factors.solar_direct)

    assert np.all(np.diff(ambient) >= 0.0)
    assert np.all(np.diff(direct) >= 0.0)
    assert max(abs(ambient[2] - ambient[1]), abs(direct[5] - direct[4])) < 0.001
    assert ambient[1] == 0.0
    assert direct[4] == 0.0
    assert ambient[7] == 1.0
    assert direct[7] == 1.0

    new_moon = terrain_celestial_light_factors(
        TerrainCelestialLightContext(-30.0, 120.0, 45.0, 210.0, 0.0),
        settings,
    )
    full_moon = terrain_celestial_light_factors(
        TerrainCelestialLightContext(-30.0, 120.0, 45.0, 210.0, 1.0),
        settings,
    )
    hidden_moon = terrain_celestial_light_factors(
        TerrainCelestialLightContext(-30.0, 120.0, -5.0, 210.0, 1.0),
        settings,
    )
    assert new_moon.lunar_strength == 0.0
    assert hidden_moon.lunar_strength == 0.0
    assert full_moon.lunar_strength == 1.0


def test_incomplete_lunar_ephemeris_cannot_invent_moonlight():
    settings = TerrainRenderSettings()
    context = TerrainCelestialLightContext(
        -30.0,
        120.0,
        45.0,
        math.nan,
        1.0,
    ).validated()

    assert context.moon_altitude_deg is None
    assert context.moon_azimuth_deg is None
    assert context.moon_illumination == 0.0
    assert terrain_celestial_light_factors(
        context, settings
    ).lunar_strength == 0.0


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


def test_original_visual_style_is_a_strict_noop():
    source = np.asarray(
        ((20, 80, 140, 255), (230, 220, 200, 255)), dtype=np.uint8
    )
    settings = TerrainRenderSettings(surface_visual_style="original")

    result = apply_vibrant_color_grade(
        source,
        np.ones(2),
        np.asarray((0.0, 150_000.0)),
        settings,
        maximum_distance_m=150_000.0,
    )

    np.testing.assert_array_equal(result, source)


def test_vibrant_settings_are_validated_and_serializable():
    settings = TerrainRenderSettings.from_mapping(
        {
            "surface_visual_style": "VIBRANT",
            "vibrant_bloom_strength": 9.0,
            "vibrant_haze_strength": -1.0,
            "vibrant_saturation_soft_limit": 0.1,
            "vibrant_saturation_compression": 9.0,
            "vibrant_midtone_lift": 9.0,
            "vibrant_distance_desaturation": 9.0,
            "vibrant_distance_brightness_gain": 9.0,
            "vibrant_shadow_sky_mix": 9.0,
            "vibrant_shadow_sky_color": [174, 185, 199],
            "vibrant_territorial_hue_variation": 9.0,
            "vibrant_material_midscale_variation": 9.0,
            "vibrant_material_microscale_variation": 9.0,
            "vibrant_material_altitude_influence": 9.0,
            "vibrant_ambient_occlusion_strength": 9.0,
            "vibrant_ambient_occlusion_radius_px": 99.0,
            "vibrant_snow_rock_blend": 9.0,
            "vibrant_water_shore_variation": 9.0,
            "vibrant_valley_haze_strength": 9.0,
            "vibrant_sun_min_brightness": 1.4,
            "vibrant_sun_max_brightness": 0.2,
            "vibrant_sun_diffuse_boost": 9.0,
            "vibrant_shadow_tint": [0.96, 0.99, 1.05],
        }
    )
    payload = settings.to_dict()

    assert settings.surface_visual_style == "vibrant"
    assert settings.vibrant_bloom_strength == 0.5
    assert settings.vibrant_haze_strength == 0.0
    assert settings.vibrant_saturation_soft_limit == 0.4
    assert settings.vibrant_saturation_compression == 1.0
    assert settings.vibrant_midtone_lift == 0.15
    assert settings.vibrant_distance_desaturation == 0.5
    assert settings.vibrant_distance_brightness_gain == 0.2
    assert settings.vibrant_shadow_sky_mix == 0.5
    assert settings.vibrant_territorial_hue_variation == 0.08
    assert settings.vibrant_material_midscale_variation == 0.08
    assert settings.vibrant_material_microscale_variation == 0.03
    assert settings.vibrant_material_altitude_influence == 0.2
    assert settings.vibrant_ambient_occlusion_strength == 0.4
    assert settings.vibrant_ambient_occlusion_radius_px == 24.0
    assert settings.vibrant_snow_rock_blend == 0.75
    assert settings.vibrant_water_shore_variation == 0.3
    assert settings.vibrant_valley_haze_strength == 0.4
    assert settings.vibrant_sun_diffuse_boost == 3.0
    assert settings.vibrant_sun_min_brightness == 1.4
    assert settings.vibrant_sun_max_brightness == 1.4
    assert payload["vibrant_shadow_sky_color"] == [174, 185, 199]
    assert payload["vibrant_shadow_tint"] == [0.96, 0.99, 1.05]


def test_vibrant_depth_haze_uses_linear_camera_distance():
    settings = TerrainRenderSettings(
        surface_visual_style="vibrant",
        vibrant_linear_depth_haze=True,
    )

    haze = vibrant_depth_haze_factor(
        np.asarray((0.0, 25_000.0, 50_000.0, 100_000.0, 120_000.0)),
        settings,
        maximum_distance_m=100_000.0,
    )

    np.testing.assert_allclose(haze, (0.0, 0.25, 0.5, 1.0, 1.0))


def test_vibrant_farthest_depth_reaches_the_configured_fog_colour():
    source = np.asarray(((60, 150, 80, 255),), dtype=np.uint8)
    settings = TerrainRenderSettings(
        surface_visual_style="vibrant",
        vibrant_haze_strength=1.0,
        vibrant_distance_desaturation=0.0,
        vibrant_distance_contrast_reduction=0.0,
        vibrant_distance_brightness_gain=0.0,
    )

    result = apply_vibrant_color_grade(
        source,
        np.ones(1),
        np.asarray((100_000.0,)),
        settings,
        maximum_distance_m=100_000.0,
    )

    np.testing.assert_array_equal(
        result[0, :3], settings.vibrant_atmosphere_color
    )


def test_vibrant_grade_lifts_blacks_and_applies_bounded_vibrance():
    source = np.asarray(
        (
            (0, 0, 0, 255),
            (70, 105, 72, 255),
            (5, 250, 8, 255),
            (255, 245, 20, 255),
            (255, 255, 255, 255),
        ),
        dtype=np.uint8,
    )
    settings = TerrainRenderSettings(
        surface_visual_style=SurfaceVisualStyle.VIBRANT.value,
        atmospheric_perspective_enabled=False,
    )

    result = apply_vibrant_color_grade(
        source,
        np.ones(5),
        np.zeros(5),
        settings,
    )

    def saturation(pixel):
        rgb = pixel[:3].astype(np.float32) / 255.0
        return float((rgb.max() - rgb.min()) / max(rgb.max(), 1e-6))

    assert np.all(result[0, :3] > 0)
    muted_gain = saturation(result[1]) - saturation(source[1])
    assert muted_gain > 0.0
    assert saturation(result[2]) < saturation(source[2]) - 0.08
    assert saturation(result[3]) < saturation(source[3]) - 0.08
    assert int(np.max(result[4, :3])) < 255
    assert int(np.min(result[3, :3])) > int(np.min(source[3, :3]))
    assert np.all(result[:, 3] == 255)


def test_vibrant_shadow_blends_cold_sky_ambient_without_becoming_black():
    source = np.asarray(((18, 24, 20, 255),), dtype=np.uint8)
    settings = TerrainRenderSettings(
        surface_visual_style="vibrant",
        atmospheric_perspective_enabled=False,
        vibrant_black_lift=0.0,
        vibrant_midtone_lift=0.0,
        vibrant_shadow_sky_mix=0.24,
    )

    result = apply_vibrant_color_grade(
        source,
        np.asarray((0.50,)),
        np.zeros(1),
        settings,
    )

    assert np.all(result[0, :3] > source[0, :3])
    assert result[0, 2] > result[0, 0]


def test_vibrant_night_grade_stays_dark_and_reduces_source_chroma():
    base = np.asarray(((80, 170, 65, 255),), dtype=np.uint8)
    settings = TerrainRenderSettings(
        surface_visual_style="vibrant",
        atmospheric_perspective_enabled=False,
    )
    day_lit = compose_vertex_rgba(base, 1.0, 0.0, settings)
    moonless_lit = compose_vertex_rgba(base, 0.015, 0.0, settings)
    full_moon_lit = compose_vertex_rgba(base, 0.15, 0.0, settings)

    day = apply_vibrant_color_grade(
        day_lit,
        1.0,
        0.0,
        settings,
        daylight_factor=1.0,
    )
    moonless = apply_vibrant_color_grade(
        moonless_lit,
        0.015,
        0.0,
        settings,
        daylight_factor=0.0,
        moonlight_factor=0.0,
    )
    full_moon = apply_vibrant_color_grade(
        full_moon_lit,
        0.15,
        0.0,
        settings,
        daylight_factor=0.0,
        moonlight_factor=1.0,
        lunar_exposure=1.0,
    )
    luma = np.asarray((0.2126, 0.7152, 0.0722))
    day_luma = float(day[0, :3] @ luma)
    moonless_luma = float(moonless[0, :3] @ luma)
    full_moon_luma = float(full_moon[0, :3] @ luma)

    assert moonless_luma < day_luma * 0.06
    assert moonless_luma < full_moon_luma < day_luma * 0.25
    assert np.ptp(moonless[0, :3]) < np.ptp(moonless_lit[0, :3])
    assert np.ptp(full_moon[0, :3]) > np.ptp(moonless[0, :3])


def test_vibrant_night_haze_uses_the_dark_sky_instead_of_day_fog():
    source = np.broadcast_to(
        np.asarray((12, 18, 10, 255), dtype=np.uint8),
        (2, 4),
    ).copy()
    settings = TerrainRenderSettings(
        surface_visual_style="vibrant",
        vibrant_haze_strength=1.0,
        vibrant_distance_brightness_gain=0.15,
    )
    night_sky = np.asarray((5, 7, 14), dtype=np.uint8)

    result = apply_vibrant_color_grade(
        source,
        np.full(2, 0.015),
        np.asarray((0.0, 100_000.0)),
        settings,
        maximum_distance_m=100_000.0,
        daylight_factor=0.0,
        atmosphere_rgb=night_sky,
    )

    np.testing.assert_array_equal(result[1, :3], night_sky)
    assert np.max(result[1, :3]) < 20


def test_vibrant_haze_converges_and_desaturates_distant_colour():
    source = np.broadcast_to(
        np.asarray((140, 80, 45, 255), dtype=np.uint8), (2, 4)
    ).copy()
    settings = TerrainRenderSettings(
        surface_visual_style="vibrant",
        vibrant_haze_strength=0.25,
    )

    result = apply_vibrant_color_grade(
        source,
        np.ones(2),
        np.asarray((0.0, 150_000.0)),
        settings,
        maximum_distance_m=150_000.0,
    )
    atmosphere = np.asarray(settings.vibrant_atmosphere_color)

    near_distance = np.linalg.norm(
        result[0, :3].astype(float) - atmosphere
    )
    far_distance = np.linalg.norm(
        result[1, :3].astype(float) - atmosphere
    )
    assert far_distance < near_distance

    def saturation(pixel):
        rgb = pixel[:3].astype(np.float32) / 255.0
        return float((rgb.max() - rgb.min()) / max(rgb.max(), 1e-6))

    assert saturation(result[1]) < saturation(result[0])
    luminance_weights = np.asarray((0.2126, 0.7152, 0.0722))
    assert (
        float(result[1, :3] @ luminance_weights)
        > float(result[0, :3] @ luminance_weights)
    )
