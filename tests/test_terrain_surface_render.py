from dataclasses import replace
from types import SimpleNamespace

import numpy as np
from PyQt5.QtCore import QPointF
from PyQt5.QtGui import QColor, QImage, QPainter, QPolygonF

from TerraLab.terrain.overlay import HorizonOverlay
from TerraLab.terrain.render.geometry import (
    _apply_horizon_coverage,
    _geometry_horizon_y,
    _regularize_categorical_regions,
    _soften_categorical_edges,
)
from TerraLab.terrain.render.overlay_types import (
    TerrainMaterialSamples,
    _TerrainGeometryMetrics,
    _TerrainSurfaceGeometry,
    _TerrainSurfaceSpan,
    _TerrainTriangleGeometry,
)
from TerraLab.terrain.render.palette import (
    _apply_categorical_solar_response,
    _apply_categorical_territorial_variation,
    _apply_vibrant_ambient_occlusion,
    _apply_vibrant_bloom,
    _qcolor_from_rgba,
    _resolve_terrain_render_path,
    _surface_cache_has_categorical_material,
    _vibrant_categorical_palette,
    _vibrant_relief_occlusion,
    _vibrant_valley_haze,
)
from TerraLab.terrain.render.triangle_raster import (
    _interpolate_triangle_values,
    _rasterize_terrain_triangles,
    _resolve_surface_material,
    _resolve_triangle_material,
)
from TerraLab.terrain.representation import (
    TerrainGeometrySource,
    TerrainRepresentationMode,
)
from TerraLab.terrain.render.config import (
    TerrainCelestialLightContext,
    TerrainRenderSettings,
)
from TerraLab.terrain.render.lighting import (
    light_direction_enu,
)
from TerraLab.terrain.render.materials import compose_vertex_rgba
from TerraLab.terrain.render.runtime_helpers import (
    _terrain_celestial_light_context,
    _terrain_relief_enabled_for_frame,
)


def _overlay() -> HorizonOverlay:
    return HorizonOverlay(
        horizon_profile_path=None,
        allow_procedural_fallback=False,
    )


def _cached_material_fixture(style="original"):
    overlay = _overlay()
    overlay.render_settings = TerrainRenderSettings(
        surface_visual_style=style
    )
    elevations = np.asarray(
        (
            (120.0, 128.0, 122.0, 116.0),
            (145.0, 154.0, 149.0, 138.0),
        ),
        dtype=np.float32,
    )
    shape = elevations.shape
    mesh = {
        "azimuths": np.asarray((0.0, 90.0, 180.0, 270.0)),
        "distances": np.asarray((100.0, 240.0)),
        "altitudes": np.asarray(
            (
                (-7.0, -6.0, -7.5, -8.0),
                (3.0, 4.0, 2.5, 2.0),
            ),
            dtype=np.float32,
        ),
        "elevations": elevations,
        "valid": np.ones(shape, dtype=bool),
        "visible": np.ones(shape, dtype=bool),
        "near_patch_eastings": np.asarray((-20.0, 20.0)),
        "near_patch_northings": np.asarray((-20.0, 20.0)),
        "near_patch_altitudes": np.asarray(
            ((-8.0, -7.0), (-4.0, -3.0)), dtype=np.float32
        ),
        "near_patch_elevations": np.asarray(
            ((118.0, 119.0), (120.0, 121.0)), dtype=np.float32
        ),
        "near_patch_valid": np.ones((2, 2), dtype=bool),
        "near_patch_normal_x": np.zeros((2, 2), dtype=np.float32),
        "near_patch_normal_y": np.zeros((2, 2), dtype=np.float32),
        "near_patch_normal_z": np.ones((2, 2), dtype=np.float32),
    }
    rgba = np.asarray(
        (
            (
                (55, 118, 70, 255),
                (46, 104, 64, 255),
                (61, 126, 72, 255),
                (58, 112, 69, 255),
            ),
            (
                (74, 138, 80, 255),
                (48, 112, 67, 255),
                (57, 124, 70, 255),
                (68, 132, 77, 255),
            ),
        ),
        dtype=np.uint8,
    )
    classes = np.full(shape, 83, dtype=np.int64)
    classes[0, 0] = 62
    classes[1, 3] = 162
    near_rgba = np.asarray(
        (
            ((42, 105, 64, 255), (82, 96, 108, 255)),
            ((44, 112, 68, 255), (43, 112, 178, 255)),
        ),
        dtype=np.uint8,
    )
    near_classes = np.asarray(((83, 62), (83, 162)), dtype=np.int64)
    surface_cache = {
        "cache_id": "synthetic-surface-v1",
        "completion_state": "complete",
        "source_legend_ids": ("s2glc_europe_2017",),
        "visual_rgba": rgba,
        "visual_valid": np.ones(shape, dtype=bool),
        "visual_source_indices": np.zeros(shape, dtype=np.int16),
        "visual_class_ids": classes,
        "visual_categorical": np.ones(shape, dtype=bool),
        "near_patch_rgba": near_rgba,
        "near_patch_valid": np.ones((2, 2), dtype=bool),
        "near_patch_source_indices": np.zeros((2, 2), dtype=np.int16),
        "near_patch_class_ids": near_classes,
        "near_patch_categorical": np.ones((2, 2), dtype=bool),
    }
    overlay.profile = SimpleNamespace(
        surface_samples=surface_cache,
        resolved_radius_m=240.0,
        terrain_mesh=mesh,
    )
    asset = overlay._prepare_terrain_render_asset(mesh)
    assert asset is not None
    return overlay, asset, surface_cache, rgba, classes


def test_base_material_cache_is_immutable_and_independent_of_time_and_light():
    overlay, asset, surface_cache, rgba, _classes = (
        _cached_material_fixture("original")
    )

    first = overlay._build_terrain_base_material(asset, surface_cache)
    first_key = first.key
    overlay.render_settings = replace(
        overlay.render_settings,
        terrain_light_azimuth_deg=83.0,
        terrain_light_elevation_deg=-22.0,
        vibrant_bloom_strength=0.01,
    )
    second = overlay._build_terrain_base_material(asset, surface_cache)

    assert second is first
    assert second.key == first_key
    assert overlay._terrain_base_material_builds == 1
    assert overlay._last_base_material_cache_hit
    np.testing.assert_array_equal(first.polar.base_rgba, rgba)
    assert np.shares_memory(first.polar.base_rgba, rgba)
    assert first.resident_bytes == (
        first.polar_protected.nbytes
        + first.near_patch_protected.nbytes
    )
    assert not first.polar.base_rgba.flags.writeable
    assert not first.polar.class_ids.flags.writeable


def test_base_material_key_changes_only_for_static_material_inputs():
    overlay, asset, surface_cache, _rgba, _classes = (
        _cached_material_fixture("vibrant")
    )
    initial = overlay._terrain_base_material_key(asset, surface_cache)

    overlay.render_settings = replace(
        overlay.render_settings,
        terrain_moon_diffuse_strength=0.03,
        vibrant_bloom_threshold=0.91,
        vibrant_haze_strength=0.42,
    )
    dynamic_change = overlay._terrain_base_material_key(
        asset, surface_cache
    )
    overlay.render_settings = replace(
        overlay.render_settings,
        vibrant_material_slope_influence=0.09,
    )
    procedural_change = overlay._terrain_base_material_key(
        asset, surface_cache
    )

    assert dynamic_change == initial
    assert procedural_change != initial


def test_vibrant_base_material_preserves_semantic_identity_and_small_classes():
    overlay, asset, surface_cache, _rgba, classes = (
        _cached_material_fixture("vibrant")
    )

    material = overlay._build_terrain_base_material(
        asset, surface_cache
    ).polar

    np.testing.assert_array_equal(material.class_ids, classes)
    assert int(material.class_ids[0, 0]) == 62
    assert int(material.class_ids[1, 3]) == 162
    assert int(
        overlay._build_terrain_base_material(
            asset, surface_cache
        ).near_patch.class_ids[0, 1]
    ) == 62
    assert int(
        overlay._build_terrain_base_material(
            asset, surface_cache
        ).near_patch.class_ids[1, 1]
    ) == 162
    material_cache = overlay._build_terrain_base_material(
        asset, surface_cache
    )
    assert material_cache.polar_protected[0, 0]
    assert material_cache.polar_protected[1, 3]
    assert material_cache.near_patch_protected[0, 1]
    assert material_cache.near_patch_protected[1, 1]
    assert np.all(material.categorical)
    assert np.all(material.valid)


def test_vibrant_orthophoto_base_preserves_source_rgb_and_is_cached():
    overlay, asset, surface_cache, rgba, _classes = (
        _cached_material_fixture("vibrant")
    )
    surface_cache["cache_id"] = "synthetic-orthophoto-v1"
    surface_cache["source_legend_ids"] = ("orthophoto",)
    surface_cache["visual_class_ids"] = np.full(
        rgba.shape[:2], -1, dtype=np.int64
    )
    surface_cache["visual_categorical"] = np.zeros(
        rgba.shape[:2], dtype=bool
    )

    first = overlay._build_terrain_base_material(asset, surface_cache)
    second = overlay._build_terrain_base_material(asset, surface_cache)

    np.testing.assert_array_equal(first.polar.base_rgba, rgba)
    assert not np.any(first.polar.categorical)
    assert second is first
    assert overlay._terrain_base_material_builds == 1


def test_fallback_material_is_cached_without_surface_or_time_dependency():
    overlay, asset, _surface_cache, _rgba, _classes = (
        _cached_material_fixture("original")
    )
    overlay.profile = SimpleNamespace(
        surface_samples=None, resolved_radius_m=240.0
    )

    first = overlay._build_terrain_base_material(asset, None)
    overlay.render_settings = replace(
        overlay.render_settings,
        terrain_light_azimuth_deg=20.0,
        terrain_light_elevation_deg=-35.0,
    )
    second = overlay._build_terrain_base_material(asset, None)

    assert second is first
    assert overlay._terrain_base_material_builds == 1
    assert np.all(first.polar.valid)
    assert not np.any(first.polar.categorical)


def test_lighting_cache_key_is_camera_free_but_time_sensitive():
    overlay, asset, _surface_cache, _rgba, _classes = (
        _cached_material_fixture("vibrant")
    )
    noon = TerrainCelestialLightContext(42.0, 155.0)
    later = TerrainCelestialLightContext(18.0, 225.0)

    noon_key = overlay._terrain_lighting_key(
        asset, noon, lighting_enabled=True
    )
    same_noon_key = overlay._terrain_lighting_key(
        asset, noon, lighting_enabled=True
    )
    later_key = overlay._terrain_lighting_key(
        asset, later, lighting_enabled=True
    )

    assert noon_key == same_noon_key
    assert noon_key != later_key


def test_resolved_material_cache_reuses_projected_classes_across_time():
    overlay, asset, surface_cache, _rgba, _classes = (
        _cached_material_fixture("vibrant")
    )
    base = overlay._build_terrain_base_material(asset, surface_cache)
    triangles = np.asarray(
        (((0.0, 0.0), (6.0, 0.0), (0.0, 6.0)),),
        dtype=np.float64,
    )
    depth = np.ones((1, 3), dtype=np.float64)
    _depth, triangle_id, bary_u, bary_v = _rasterize_terrain_triangles(
        triangles, depth, 6, 6, supersample=1
    )
    geometry = _TerrainTriangleGeometry(
        triangles,
        depth,
        np.asarray(((0, 0, 1),), dtype=np.int32),
        np.asarray(((0, 1, 0),), dtype=np.int32),
        np.zeros((1, 3), dtype=np.uint8),
        _TerrainGeometryMetrics(spans=1, output_vertices=3),
    )
    covered = triangle_id >= 0
    raster_key = (geometry.cache_token, 6, 6, 1)

    first = overlay._resolve_screen_material(
        asset,
        geometry,
        triangle_id,
        bary_u,
        bary_v,
        covered,
        base.polar,
        base.near_patch,
        raster_key=raster_key,
        material_key=base.key,
        render_scale=1.0,
    )
    overlay.render_settings = replace(
        overlay.render_settings,
        vibrant_bloom_strength=0.01,
        vibrant_haze_strength=0.2,
    )
    second = overlay._resolve_screen_material(
        asset,
        geometry,
        triangle_id,
        bary_u,
        bary_v,
        covered,
        base.polar,
        base.near_patch,
        raster_key=raster_key,
        material_key=base.key,
        render_scale=1.0,
    )

    assert second is first
    assert overlay._terrain_resolved_material_builds == 1
    assert overlay._last_resolved_material_cache_hit
    assert first.resident_bytes > 0
    diagnostics = overlay.terrain_cache_diagnostics()
    assert diagnostics["base_material"]["builds"] == 1
    assert diagnostics["resolved_material"]["hits"] >= 1
    assert diagnostics["resolved_material"]["resident_bytes"] > 0


def test_resolved_material_cache_rejects_stale_triangle_geometry():
    overlay, asset, surface_cache, _rgba, _classes = (
        _cached_material_fixture("vibrant")
    )
    base = overlay._build_terrain_base_material(asset, surface_cache)
    first_triangles = np.asarray(
        (((0.0, 0.0), (6.0, 0.0), (0.0, 6.0)),),
        dtype=np.float64,
    )
    first_depth = np.ones((1, 3), dtype=np.float64)
    _, triangle_id, bary_u, bary_v = _rasterize_terrain_triangles(
        first_triangles, first_depth, 6, 6, supersample=1
    )
    first_geometry = _TerrainTriangleGeometry(
        first_triangles,
        first_depth,
        np.asarray(((0, 0, 1),), dtype=np.int32),
        np.asarray(((0, 1, 0),), dtype=np.int32),
        np.zeros((1, 3), dtype=np.uint8),
        _TerrainGeometryMetrics(spans=1, output_vertices=3),
    )
    collided_raster_key = (first_geometry.cache_token, 6, 6, 1)
    first = overlay._resolve_screen_material(
        asset,
        first_geometry,
        triangle_id,
        bary_u,
        bary_v,
        triangle_id >= 0,
        base.polar,
        base.near_patch,
        raster_key=collided_raster_key,
        material_key=base.key,
        render_scale=1.0,
    )

    second_triangles = np.asarray(
        (
            ((0.0, 0.0), (6.0, 0.0), (0.0, 6.0)),
            ((6.0, 0.0), (6.0, 6.0), (0.0, 6.0)),
        ),
        dtype=np.float64,
    )
    second_depth = np.ones((2, 3), dtype=np.float64)
    _, triangle_id, bary_u, bary_v = _rasterize_terrain_triangles(
        second_triangles, second_depth, 6, 6, supersample=1
    )
    second_geometry = _TerrainTriangleGeometry(
        second_triangles,
        second_depth,
        np.asarray(((0, 0, 1), (0, 1, 1)), dtype=np.int32),
        np.asarray(((0, 1, 0), (1, 1, 0)), dtype=np.int32),
        np.zeros((2, 3), dtype=np.uint8),
        _TerrainGeometryMetrics(spans=2, output_vertices=6),
    )
    second = overlay._resolve_screen_material(
        asset,
        second_geometry,
        triangle_id,
        bary_u,
        bary_v,
        triangle_id >= 0,
        base.polar,
        base.near_patch,
        raster_key=collided_raster_key,
        material_key=base.key,
        render_scale=1.0,
    )

    assert second is not first
    assert second.triangle_surface_xy.shape == (2, 3, 2)
    assert overlay._terrain_resolved_material_builds == 2
    assert not overlay._last_resolved_material_cache_hit


def test_render_cache_layers_invalidate_independently_for_time_and_camera():
    overlay, asset, _surface_cache, _rgba, _classes = (
        _cached_material_fixture("vibrant")
    )
    overlay._terrain_render_asset = asset
    mesh = overlay.profile.terrain_mesh
    width, height = 120, 80

    def projection(view_azimuth):
        def scalar(altitude, azimuth):
            relative = (
                (float(azimuth) - float(view_azimuth) + 180.0) % 360.0
            ) - 180.0
            return (
                width * 0.5 + relative * 1.25,
                height * 0.70 - float(altitude) * 2.5,
            )

        def vectorized(altitude, azimuth):
            altitude, azimuth = np.broadcast_arrays(
                np.asarray(altitude, dtype=np.float64),
                np.asarray(azimuth, dtype=np.float64),
            )
            relative = (
                (azimuth - float(view_azimuth) + 180.0) % 360.0
            ) - 180.0
            return (
                width * 0.5 + relative * 1.25,
                height * 0.70 - altitude * 2.5,
                np.isfinite(altitude) & np.isfinite(azimuth),
            )

        return scalar, vectorized

    def render(view_azimuth, context, t_night, sky):
        scalar, vectorized = projection(view_azimuth)
        image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            rendered = overlay._draw_terrain_interpolated(
                painter,
                mesh,
                scalar,
                width,
                height,
                view_azimuth,
                view_azimuth - 120.0,
                view_azimuth + 120.0,
                t_night,
                sky,
                projection_fn_numpy=vectorized,
                light_context=context,
            )
        finally:
            painter.end()
        assert rendered

    noon = TerrainCelestialLightContext(35.0, 315.0)
    moon = TerrainCelestialLightContext(
        -24.0, 300.0, 42.0, 120.0, 0.94
    )
    render(45.0, noon, 0.0, QColor(151, 190, 219))
    initial = overlay.terrain_cache_diagnostics()

    render(45.0, moon, 1.0, QColor(8, 12, 24))
    after_time = overlay.terrain_cache_diagnostics()
    assert (
        after_time["base_material"]["builds"]
        == initial["base_material"]["builds"]
    )
    assert (
        after_time["resolved_material"]["builds"]
        == initial["resolved_material"]["builds"]
    )
    assert after_time["raster"]["builds"] == initial["raster"]["builds"]
    assert (
        after_time["lighting"]["builds"]
        == initial["lighting"]["builds"] + 1
    )
    assert after_time["resolved_material"]["last_hit"]
    assert after_time["raster"]["last_hit"]

    render(60.0, moon, 1.0, QColor(8, 12, 24))
    after_camera = overlay.terrain_cache_diagnostics()
    assert (
        after_camera["base_material"]["builds"]
        == after_time["base_material"]["builds"]
    )
    assert (
        after_camera["lighting"]["builds"]
        == after_time["lighting"]["builds"]
    )
    assert (
        after_camera["resolved_material"]["builds"]
        == after_time["resolved_material"]["builds"] + 1
    )
    assert after_camera["raster"]["builds"] == (
        after_time["raster"]["builds"] + 1
    )

    render(60.0, moon, 1.0, QColor(8, 12, 24))
    repaint = overlay.terrain_cache_diagnostics()
    assert repaint["frame"]["last_hit"]
    assert (
        repaint["base_material"]["builds"]
        == after_camera["base_material"]["builds"]
    )
    assert (
        repaint["lighting"]["builds"]
        == after_camera["lighting"]["builds"]
    )
    assert (
        repaint["resolved_material"]["builds"]
        == after_camera["resolved_material"]["builds"]
    )


def test_static_variation_and_dynamic_solar_response_are_separate():
    image = np.full((3, 3, 4), (80, 135, 55, 255), dtype=np.uint8)
    materials = TerrainMaterialSamples(
        image,
        np.ones((3, 3), dtype=bool),
        np.full((3, 3), 102, dtype=np.int64),
        np.ones((3, 3), dtype=bool),
        np.zeros((3, 3), dtype=np.int16),
    )
    world_x, world_y = np.meshgrid(
        np.arange(3, dtype=np.float32) * 500.0,
        np.arange(3, dtype=np.float32) * 500.0,
    )
    common = dict(
        strength=1.0,
        luminance_variation=0.04,
        hue_variation=0.014,
        slope_influence=0.06,
        normal_x=np.full((3, 3), 0.6, dtype=np.float32),
        normal_y=np.zeros((3, 3), dtype=np.float32),
        normal_z=np.full((3, 3), 0.8, dtype=np.float32),
        source_legend_ids=("s2glc_europe_2017",),
        include_solar_response=False,
    )
    static_night = _apply_categorical_territorial_variation(
        image,
        materials,
        world_x,
        world_y,
        materials.valid,
        solar_exposure=np.zeros((3, 3), dtype=np.float32),
        **common,
    )
    static_day = _apply_categorical_territorial_variation(
        image,
        materials,
        world_x,
        world_y,
        materials.valid,
        solar_exposure=np.ones((3, 3), dtype=np.float32),
        **common,
    )
    sunlit = _apply_categorical_solar_response(
        static_day,
        materials,
        np.ones((3, 3), dtype=np.float32),
        materials.valid,
        strength=1.0,
        midscale_variation=0.024,
        microscale_variation=0.007,
        slope_influence=0.06,
        snow_rock_blend=0.28,
        water_shore_variation=0.08,
        normal_x=np.full((3, 3), 0.6, dtype=np.float32),
        normal_y=np.zeros((3, 3), dtype=np.float32),
        normal_z=np.full((3, 3), 0.8, dtype=np.float32),
        source_legend_ids=("s2glc_europe_2017",),
    )

    np.testing.assert_array_equal(static_night, static_day)
    assert not np.array_equal(sunlit[..., :3], static_day[..., :3])
    np.testing.assert_array_equal(sunlit[..., 3], static_day[..., 3])


def test_vibrant_terrain_light_uses_real_astronomical_sun_angles():
    overlay = _overlay()
    overlay.render_settings = TerrainRenderSettings(
        surface_visual_style="vibrant"
    )

    altitude, azimuth, vector = overlay._configured_light(27.5, 118.0)

    assert altitude == 27.5
    assert azimuth == 118.0
    np.testing.assert_allclose(
        vector, light_direction_enu(118.0, 27.5), atol=1e-6
    )


def test_original_terrain_light_uses_real_astronomical_direction():
    overlay = _overlay()
    overlay.render_settings = TerrainRenderSettings(
        surface_visual_style="original",
        terrain_light_elevation_deg=35.0,
        terrain_light_azimuth_deg=315.0,
    )

    altitude, azimuth, vector = overlay._configured_light(27.5, 118.0)

    assert altitude == 27.5
    assert azimuth == 118.0
    np.testing.assert_allclose(
        vector, light_direction_enu(118.0, 27.5), atol=1e-6
    )


def test_configured_direction_remains_the_invalid_ephemeris_fallback():
    overlay = _overlay()
    overlay.render_settings = TerrainRenderSettings(
        surface_visual_style="original",
        terrain_light_elevation_deg=35.0,
        terrain_light_azimuth_deg=315.0,
    )

    altitude, azimuth, vector = overlay._configured_light(None, None)

    assert altitude == 35.0
    assert azimuth == 315.0
    np.testing.assert_allclose(
        vector, light_direction_enu(315.0, 35.0), atol=1e-6
    )


def test_legacy_vibrant_sun_switch_no_longer_disables_astronomical_light():
    overlay = _overlay()
    overlay.render_settings = TerrainRenderSettings(
        surface_visual_style="original",
        vibrant_use_astronomical_sun=False,
    )

    altitude, azimuth, vector = overlay._configured_light(22.0, 87.0)

    assert altitude == 22.0
    assert azimuth == 87.0
    np.testing.assert_allclose(
        vector, light_direction_enu(87.0, 22.0), atol=1e-6
    )


def test_invalid_celestial_context_uses_the_configured_light_fallback():
    overlay = _overlay()
    overlay.render_settings = TerrainRenderSettings(
        terrain_light_elevation_deg=28.0,
        terrain_light_azimuth_deg=301.0,
    )

    altitude, azimuth, vector = overlay._configured_light(
        light_context=TerrainCelestialLightContext(np.nan, np.nan)
    )

    assert altitude == 28.0
    assert azimuth == 301.0
    np.testing.assert_allclose(
        vector, light_direction_enu(301.0, 28.0), atol=1e-6
    )


def test_vibrant_sun_has_stronger_directional_separation_with_coloured_shadows():
    overlay = _overlay()
    overlay.render_settings = TerrainRenderSettings(
        surface_visual_style="vibrant"
    )
    sun_altitude = 30.0
    sun_vector = light_direction_enu(110.0, sun_altitude)
    normals = np.asarray(
        (
            sun_vector,
            (-sun_vector[0], -sun_vector[1], sun_vector[2]),
        ),
        dtype=np.float32,
    )

    light = overlay._terrain_light_factor(
        normals[:, 0],
        normals[:, 1],
        normals[:, 2],
        np.zeros(2),
        sun_vector,
        sun_altitude,
    )

    assert light[0] >= 1.25
    assert light[1] >= 0.60
    assert light[0] - light[1] >= 0.60


def test_daylight_direction_changes_bright_slope_in_every_visual_style():
    normals = np.asarray(((1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)))
    for style in ("original", "vibrant"):
        overlay = _overlay()
        overlay.render_settings = TerrainRenderSettings(
            surface_visual_style=style
        )
        east = overlay._terrain_light_components(
            normals[:, 0],
            normals[:, 1],
            normals[:, 2],
            np.zeros(2),
            light_context=TerrainCelestialLightContext(30.0, 90.0),
        ).intensity
        west = overlay._terrain_light_components(
            normals[:, 0],
            normals[:, 1],
            normals[:, 2],
            np.zeros(2),
            light_context=TerrainCelestialLightContext(30.0, 270.0),
        ).intensity

        assert east[0] > east[1]
        assert west[1] > west[0]


def test_astronomical_night_and_full_moon_have_bounded_visibility():
    overlay = _overlay()
    overlay.render_settings = TerrainRenderSettings(
        surface_visual_style="vibrant"
    )
    normals = np.asarray(((1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)))
    common = (
        normals[:, 0],
        normals[:, 1],
        normals[:, 2],
        np.zeros(2),
    )
    day = overlay._terrain_light_components(
        *common,
        light_context=TerrainCelestialLightContext(30.0, 90.0),
    )
    moonless = overlay._terrain_light_components(
        *common,
        light_context=TerrainCelestialLightContext(-30.0, 90.0),
    )
    full_moon = overlay._terrain_light_components(
        *common,
        light_context=TerrainCelestialLightContext(
            -30.0, 90.0, 45.0, 90.0, 1.0
        ),
    )
    moon_below_horizon = overlay._terrain_light_components(
        *common,
        light_context=TerrainCelestialLightContext(
            -30.0, 90.0, -5.0, 90.0, 1.0
        ),
    )

    assert float(np.max(moonless.intensity)) <= 0.02
    np.testing.assert_allclose(
        moon_below_horizon.intensity, moonless.intensity, atol=1e-6
    )
    assert float(np.max(full_moon.intensity)) > float(
        np.max(moonless.intensity)
    )
    assert float(np.max(full_moon.intensity)) < 0.25 * float(
        np.max(day.intensity)
    )
    assert np.ptp(full_moon.intensity) > 0.05


def test_profile_fallback_uses_the_same_sun_moon_and_night_model():
    overlay = _overlay()
    overlay.render_settings = TerrainRenderSettings(
        surface_visual_style="vibrant",
        atmospheric_perspective_enabled=False,
    )
    band = SimpleNamespace(band_max=10_000.0)
    azimuths = np.asarray((90.0, 270.0), dtype=np.float32)
    heights = np.zeros(2, dtype=np.float32)
    day_context = TerrainCelestialLightContext(30.0, 90.0)
    night_context = TerrainCelestialLightContext(-30.0, 90.0)
    moon_context = TerrainCelestialLightContext(
        -30.0, 90.0, 45.0, 90.0, 1.0
    )

    day = overlay._terrain_profile_light_grid(
        azimuths,
        heights,
        30.0,
        90.0,
        band,
        light_context=day_context,
    )
    night = overlay._terrain_profile_light_grid(
        azimuths,
        heights,
        -30.0,
        90.0,
        band,
        light_context=night_context,
    )
    moon = overlay._terrain_profile_light_grid(
        azimuths,
        heights,
        -30.0,
        90.0,
        band,
        light_context=moon_context,
    )

    assert np.ptp(day.intensity) > 0.20
    assert float(np.max(night.intensity)) <= 0.02
    assert np.ptp(moon.intensity) > 0.05
    assert float(np.max(moon.intensity)) < 0.25

    base = QColor(90, 160, 70)
    sky = QColor(5, 5, 12)
    day_color = overlay._compose_profile_light_color(
        base,
        float(np.max(day.intensity)),
        10_000.0,
        sky,
        day.factors,
        solar_exposure=float(np.max(day.solar_exposure)),
    )
    night_color = overlay._compose_profile_light_color(
        base,
        float(np.max(night.intensity)),
        10_000.0,
        sky,
        night.factors,
    )
    moon_color = overlay._compose_profile_light_color(
        base,
        float(np.max(moon.intensity)),
        10_000.0,
        sky,
        moon.factors,
        lunar_exposure=float(np.max(moon.lunar_exposure)),
    )
    luma = np.asarray((0.2126, 0.7152, 0.0722))
    day_luma = float(np.asarray(day_color.getRgb()[:3]) @ luma)
    night_luma = float(np.asarray(night_color.getRgb()[:3]) @ luma)
    moon_luma = float(np.asarray(moon_color.getRgb()[:3]) @ luma)

    assert night_luma < 0.06 * day_luma
    assert night_luma < moon_luma < 0.25 * day_luma


def test_missing_surface_fallback_keeps_a_neutral_base_for_celestial_lighting():
    overlay = _overlay()
    overlay.profile = SimpleNamespace(surface_samples=None)
    asset = SimpleNamespace(
        elevations=np.zeros((2, 3), dtype=np.float32),
        distances=np.asarray((100.0, 1_000.0), dtype=np.float32),
    )

    daylight_base = overlay._terrain_vertex_materials(
        asset, 0.0
    ).base_rgba
    night_base = overlay._terrain_vertex_materials(asset, 1.0).base_rgba

    np.testing.assert_array_equal(night_base, daylight_base)


def test_canvas_builds_full_moon_context_from_separation_when_needed():
    context = _terrain_celestial_light_context(
        -30.0,
        120.0,
        {
            "moon": {
                "alt": 45.0,
                "az": 210.0,
                "sep_real": 180.0,
            }
        },
        0.8,
    )

    assert context.moon_altitude_deg == 45.0
    assert context.moon_azimuth_deg == 210.0
    assert context.moon_illumination == 1.0
    assert context.eclipse_factor == 0.8


def test_canvas_prefers_skyfield_sun_angles_for_terrain_context():
    context = _terrain_celestial_light_context(
        12.0,
        100.0,
        {
            "sun": {"alt": 13.25, "az": 101.5},
            "moon": {"alt": -10.0, "az": 250.0, "illumination": 0.3},
        },
        1.0,
    )

    assert context.sun_altitude_deg == 13.25
    assert context.sun_azimuth_deg == 101.5


def _rgb(color: QColor) -> tuple[int, int, int, int]:
    return color.red(), color.green(), color.blue(), color.alpha()


def _categorical_cache(classes, *, legend="s2glc_europe_2017"):
    class_grid = np.asarray(classes, dtype=np.int64)
    return SimpleNamespace(
        source_ids=("source-1",),
        source_names=("Cobertura externa",),
        source_legend_ids=(legend,),
        relief_class_ids=class_grid,
        relief_categorical=np.ones(class_grid.shape, dtype=bool),
        relief_source_indices=np.zeros(class_grid.shape, dtype=np.int16),
        relief_distance_indices=np.arange(
            class_grid.shape[0], dtype=np.int32
        ),
        relief_azimuth_indices=np.arange(
            class_grid.shape[1], dtype=np.int32
        ),
    )


def test_relief_category_hit_uses_greatest_barycentric_vertex():
    overlay = _overlay()
    cache = _categorical_cache([[62, 82, 102]])
    overlay.profile = SimpleNamespace(surface_samples=cache)
    overlay._terrain_render_asset = SimpleNamespace(
        elevations=np.zeros((1, 3), dtype=np.float32)
    )
    geometry = _TerrainTriangleGeometry(
        xy=np.asarray([[[0, 0], [1, 0], [0, 1]]], dtype=np.float32),
        depth=np.asarray([[1, 1, 1]], dtype=np.float32),
        vertex_rows=np.asarray([[0, 0, 0]], dtype=np.int32),
        vertex_columns=np.asarray([[0, 1, 2]], dtype=np.int32),
        vertex_domain=np.asarray([[0, 0, 0]], dtype=np.uint8),
        metrics=_TerrainGeometryMetrics(spans=1),
    )
    overlay._terrain_surface_image_geometry = geometry
    overlay._terrain_raster_cache_key = (
        geometry.cache_token,
        1,
        1,
        1,
    )
    overlay._terrain_raster_cache = (
        np.asarray([[0]], dtype=np.int32),
        np.asarray([[0.20]], dtype=np.float32),
        np.asarray([[0.70]], dtype=np.float32),
    )

    info = overlay.category_at_screen(0.0, 0.0)

    assert info is not None
    assert info.class_id == 82
    assert info.product == "S2GLC Europe 2017"
    assert "fulla ampla" in info.name


def test_relief_category_hit_matches_the_regularized_display_material():
    overlay = _overlay()
    cache = _categorical_cache([[62]])
    overlay.profile = SimpleNamespace(surface_samples=cache)
    overlay._terrain_render_asset = SimpleNamespace(
        elevations=np.zeros((1, 1), dtype=np.float32)
    )
    geometry = _TerrainTriangleGeometry(
        xy=np.asarray([[[0, 0], [1, 0], [0, 1]]], dtype=np.float32),
        depth=np.asarray([[1, 1, 1]], dtype=np.float32),
        vertex_rows=np.asarray([[0, 0, 0]], dtype=np.int32),
        vertex_columns=np.asarray([[0, 0, 0]], dtype=np.int32),
        vertex_domain=np.asarray([[0, 0, 0]], dtype=np.uint8),
        metrics=_TerrainGeometryMetrics(spans=1),
    )
    overlay._terrain_surface_image_geometry = geometry
    overlay._terrain_surface_image_cache = SimpleNamespace(
        width=lambda: 1,
        height=lambda: 1,
    )
    overlay._terrain_resolved_materials = TerrainMaterialSamples(
        np.asarray([[[20, 120, 40, 255]]], dtype=np.uint8),
        np.asarray([[True]]),
        np.asarray([[82]], dtype=np.int64),
        np.asarray([[True]]),
        np.asarray([[0]], dtype=np.int16),
    )

    info = overlay.category_at_screen(0.0, 0.0)

    assert info is not None
    assert info.class_id == 82


def test_profile_category_hit_uses_top_visible_polygon_and_nearest_sample():
    overlay = _overlay()
    cache = _categorical_cache(
        [[1, 10]], legend="clcplus_backbone_2023"
    )
    overlay.profile = SimpleNamespace(surface_samples=cache)
    overlay._terrain_render_asset = SimpleNamespace(
        elevations=np.zeros((1, 2), dtype=np.float32)
    )
    span = _TerrainSurfaceSpan(
        row_index=0,
        distance_m=100.0,
        column_indices=np.asarray([0, 1], dtype=np.int32),
        x=np.asarray([0.0, 10.0], dtype=np.float32),
        bottom_x=np.asarray([0.0, 10.0], dtype=np.float32),
        top_y=np.asarray([0.0, 0.0], dtype=np.float32),
        bottom_y=np.asarray([10.0, 10.0], dtype=np.float32),
        source_vertex_count=2,
        max_error_px=0.0,
    )
    geometry = _TerrainSurfaceGeometry(
        (span,), _TerrainGeometryMetrics(spans=1)
    )
    overlay._terrain_surface_image_geometry = geometry

    info = overlay.category_at_screen(8.0, 5.0)

    assert info is not None
    assert info.class_id == 10
    assert info.name == "Aigua"
    assert info.product == "CLC+ Backbone"


def test_profile_representation_uses_cached_polygon_without_raster_io():
    overlay = _overlay()
    cache = SimpleNamespace(
        source_ids=("clc",),
        source_names=("CLC+",),
        source_legend_ids=("clcplus_backbone_2023",),
        profile_class_ids=np.asarray([[1, 10]], dtype=np.int64),
        profile_categorical=np.asarray([[True, True]]),
        profile_source_indices=np.asarray([[0, 0]], dtype=np.int16),
        profile_band_indices=np.asarray([0], dtype=np.int32),
        profile_azimuth_indices=np.asarray([0, 1], dtype=np.int32),
    )
    band = SimpleNamespace(band_id="near")
    overlay.profile = SimpleNamespace(
        surface_samples=cache,
        bands=({"id": "near"},),
        azimuths=np.asarray([0.0, 90.0], dtype=np.float32),
    )
    overlay._layers = [(band, None, None)]
    polygon = QPolygonF(
        [
            QPointF(0.0, 0.0),
            QPointF(10.0, 0.0),
            QPointF(10.0, 10.0),
            QPointF(0.0, 10.0),
        ]
    )
    overlay._profile_category_hit_cache[
        ("band", id(band))
    ] = (
        (
            polygon,
            np.asarray([0.0, 10.0], dtype=np.float32),
            np.asarray([0.0, 90.0], dtype=np.float32),
        ),
    )

    info = overlay.category_at_screen(8.0, 5.0)

    assert info is not None
    assert info.class_id == 10
    assert info.name == "Aigua"


def test_category_hit_ignores_non_categorical_surface_cache():
    overlay = _overlay()
    cache = _categorical_cache([[82]])
    cache.relief_categorical[:] = False
    overlay.profile = SimpleNamespace(surface_samples=cache)
    overlay._terrain_render_asset = SimpleNamespace(
        elevations=np.zeros((1, 1), dtype=np.float32)
    )
    geometry = _TerrainTriangleGeometry(
        xy=np.asarray([[[0, 0], [1, 0], [0, 1]]], dtype=np.float32),
        depth=np.asarray([[1, 1, 1]], dtype=np.float32),
        vertex_rows=np.asarray([[0, 0, 0]], dtype=np.int32),
        vertex_columns=np.asarray([[0, 0, 0]], dtype=np.int32),
        vertex_domain=np.asarray([[0, 0, 0]], dtype=np.uint8),
        metrics=_TerrainGeometryMetrics(spans=1),
    )
    overlay._terrain_surface_image_geometry = geometry
    overlay._terrain_raster_cache_key = (
        geometry.cache_token,
        1,
        1,
        1,
    )
    overlay._terrain_raster_cache = (
        np.asarray([[0]], dtype=np.int32),
        np.asarray([[0.4]], dtype=np.float32),
        np.asarray([[0.3]], dtype=np.float32),
    )

    assert overlay.category_at_screen(0.0, 0.0) is None


def test_explicit_profile_mode_ignores_an_accidental_mesh():
    stale_mesh = {"version": 2, "altitudes": np.ones((2, 2))}
    profile = SimpleNamespace(
        representation_mode=TerrainRepresentationMode.PROFILE,
        geometry_source=TerrainGeometrySource.REAL_ELEVATION,
        terrain_mesh=stale_mesh,
    )

    path, effective_mesh, mode, source = _resolve_terrain_render_path(profile)

    assert path == "profile"
    assert effective_mesh is None
    assert mode is TerrainRepresentationMode.PROFILE
    assert source is TerrainGeometrySource.REAL_ELEVATION


def test_relief_preview_without_mesh_uses_profile_bands():
    profile = SimpleNamespace(
        representation_mode=TerrainRepresentationMode.RELIEF,
        geometry_source=TerrainGeometrySource.REAL_ELEVATION,
        terrain_mesh=None,
    )

    path, effective_mesh, *_ = _resolve_terrain_render_path(profile)

    assert path == "profile_preview"
    assert effective_mesh is None


def test_flat_geometry_source_is_explicit_and_never_uses_relief_mesh():
    profile = SimpleNamespace(
        representation_mode=TerrainRepresentationMode.RELIEF,
        geometry_source=TerrainGeometrySource.FLAT_FALLBACK,
        terrain_mesh={"version": 2},
    )

    path, effective_mesh, *_ = _resolve_terrain_render_path(profile)

    assert path == "profile"
    assert effective_mesh is None


def test_relief_mode_with_real_mesh_selects_relief_renderer():
    mesh = {"version": 2}
    profile = SimpleNamespace(
        representation_mode=TerrainRepresentationMode.RELIEF,
        geometry_source=TerrainGeometrySource.REAL_ELEVATION,
        terrain_mesh=mesh,
    )

    path, effective_mesh, *_ = _resolve_terrain_render_path(profile)

    assert path == "relief"
    assert effective_mesh is mesh


def test_relief_remains_enabled_during_interaction_by_default():
    assert _terrain_relief_enabled_for_frame(True, False) is True
    assert _terrain_relief_enabled_for_frame(True, True) is True
    assert _terrain_relief_enabled_for_frame(False, False) is False


def test_visible_surface_always_requires_relief():
    assert (
        _terrain_relief_enabled_for_frame(
            False,
            True,
            surface_enabled=True,
            suspend_during_interaction=True,
        )
        is True
    )


def test_legacy_interaction_fallback_is_reactivated_with_one_flag():
    assert (
        _terrain_relief_enabled_for_frame(
            True,
            True,
            suspend_during_interaction=True,
        )
        is False
    )
    assert (
        _terrain_relief_enabled_for_frame(
            True,
            False,
            suspend_during_interaction=True,
        )
        is True
    )


def test_profile_cache_maps_sparse_band_and_circular_azimuth_indices():
    overlay = _overlay()
    red = (210, 20, 10, 255)
    blue = (12, 40, 220, 255)
    overlay.profile = SimpleNamespace(
        azimuths=np.asarray([0.0, 90.0, 180.0, 270.0]),
        bands=[{"id": "near"}],
        surface_samples=SimpleNamespace(
            profile_rgba=np.asarray([[red, blue]], dtype=np.uint8),
            profile_valid=np.asarray([[True, True]]),
            profile_source_indices=np.asarray([[0, -1]], dtype=np.int16),
            profile_band_indices=np.asarray([0], dtype=np.int32),
            profile_azimuth_indices=np.asarray([0, 2], dtype=np.int32),
        ),
    )
    band = SimpleNamespace(band_id="near", band_index=0)

    rgba, valid = overlay._profile_surface_samples(
        band, np.asarray([359.0, 181.0, 89.0])
    )

    np.testing.assert_array_equal(rgba, np.asarray([red, blue, red]))
    np.testing.assert_array_equal(valid, np.asarray([True, False, True]))


def test_relief_cache_maps_sparse_indices_and_wraps_mesh_columns():
    overlay = _overlay()
    colors = np.asarray(
        [
            [(10, 11, 12, 255), (20, 21, 22, 255)],
            [(30, 31, 32, 255), (40, 41, 42, 255)],
        ],
        dtype=np.uint8,
    )
    overlay.profile = SimpleNamespace(
        surface_samples={
            "relief_rgba": colors,
            "relief_valid": np.asarray([[True, True], [True, True]]),
            "relief_source_indices": np.asarray([[0, 0], [0, -1]]),
            "relief_distance_indices": np.asarray([0, 3]),
            "relief_azimuth_indices": np.asarray([0, 4]),
        }
    )

    rgba, valid = overlay._relief_surface_samples(
        np.asarray([0, 3, 2]),
        np.asarray([8, 4, 7]),
        (4, 8),
    )

    np.testing.assert_array_equal(
        rgba,
        np.asarray([(10, 11, 12, 255), (40, 41, 42, 255), (30, 31, 32, 255)]),
    )
    np.testing.assert_array_equal(valid, np.asarray([True, False, True]))


def test_partial_surface_cache_leaves_unloaded_azimuths_for_topographic_fallback():
    overlay = _overlay()
    color = (40, 120, 60, 255)
    overlay.profile = SimpleNamespace(
        azimuths=np.asarray([0.0, 90.0, 180.0, 270.0]),
        surface_samples=SimpleNamespace(
            completion_state="visible_partial",
            profile_rgba=np.asarray([[color]], dtype=np.uint8),
            profile_valid=np.asarray([[True]]),
            profile_loaded=np.asarray([[True]]),
            profile_source_indices=np.asarray([[0]], dtype=np.int16),
            profile_band_indices=np.asarray([0], dtype=np.int32),
            profile_azimuth_indices=np.asarray([1], dtype=np.int32),
        ),
    )

    _rgba, valid = overlay._profile_surface_samples(
        SimpleNamespace(band_index=0), np.asarray([90.0, 180.0])
    )

    np.testing.assert_array_equal(valid, np.asarray([True, False]))


def test_partial_relief_cache_does_not_stretch_fov_across_pending_mesh():
    overlay = _overlay()
    overlay.profile = SimpleNamespace(
        surface_samples={
            "completion_state": "visible_partial",
            "relief_rgba": np.asarray([[[40, 120, 60, 255]]], dtype=np.uint8),
            "relief_valid": np.asarray([[True]]),
            "relief_loaded": np.asarray([[True]]),
            "relief_source_indices": np.asarray([[0]], dtype=np.int16),
            "relief_distance_indices": np.asarray([1]),
            "relief_azimuth_indices": np.asarray([2]),
        }
    )

    _rgba, valid = overlay._relief_surface_samples(
        np.asarray([1, 1]), np.asarray([2, 3]), (4, 8)
    )

    np.testing.assert_array_equal(valid, np.asarray([True, False]))


def test_cached_rgba_remains_the_base_color_without_light_or_haze():
    overlay = _overlay()
    sampled = _qcolor_from_rgba(np.asarray([17, 91, 203, 255], dtype=np.uint8))

    rendered = overlay._mesh_quad_color(
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        QColor(150, 190, 220),
        None,
        None,
        terrain_shading_enabled=False,
        light_factor=1.0,
        base_color=sampled,
    )

    assert _rgb(rendered) == (17, 91, 203, 255)


def test_color_composition_applies_lighting_before_atmosphere():
    calls = []

    class RecordingOverlay(HorizonOverlay):
        def _apply_terrain_light(self, color, *_args):
            calls.append(("light", _rgb(color)))
            return QColor(33, 44, 55, color.alpha())

        def _apply_terrain_atmosphere(self, color, *_args):
            calls.append(("atmosphere", _rgb(color)))
            return QColor(66, 77, 88, color.alpha())

    overlay = RecordingOverlay(
        horizon_profile_path=None,
        allow_procedural_fallback=False,
    )

    result = overlay._compose_terrain_color(
        QColor(11, 22, 33),
        0.8,
        20_000.0,
        QColor(140, 180, 210),
        0.0,
    )

    assert calls == [
        ("light", (11, 22, 33, 255)),
        ("atmosphere", (33, 44, 55, 255)),
    ]
    assert _rgb(result) == (66, 77, 88, 255)


def test_distance_changes_only_the_post_lighting_atmospheric_stage():
    overlay = _overlay()
    base = QColor(25, 100, 175)
    sky = QColor(170, 200, 225)

    near = overlay._compose_terrain_color(base, 1.0, 0.0, sky, 0.0)
    far = overlay._compose_terrain_color(base, 1.0, 80_000.0, sky, 0.0)
    expected_far = overlay._apply_terrain_atmosphere(base, 80_000.0, sky, 0.0)

    assert _rgb(near) == _rgb(base)
    assert _rgb(far) == _rgb(expected_far)
    assert _rgb(far) != _rgb(base)


def _rasterized_square(reverse_order=False):
    triangles = np.asarray(
        (
            ((0.0, 0.0), (4.0, 0.0), (0.0, 4.0)),
            ((4.0, 0.0), (4.0, 4.0), (0.0, 4.0)),
        ),
        dtype=np.float64,
    )
    values = np.asarray(
        (
            ((0.0,), (4.0,), (4.0,)),
            ((4.0,), (8.0,), (4.0,)),
        ),
        dtype=np.float64,
    )
    if reverse_order:
        triangles = triangles[::-1]
        values = values[::-1]
    depth = np.ones((2, 3), dtype=np.float64)
    _depth, triangle_id, bary_u, bary_v = _rasterize_terrain_triangles(
        triangles, depth, 4, 4, supersample=1
    )
    interpolated, covered = _interpolate_triangle_values(
        triangle_id, bary_u, bary_v, values
    )
    return interpolated[..., 0], covered


def test_adjacent_interpolated_triangles_have_no_coverage_seam():
    _values, covered = _rasterized_square()

    assert np.all(covered)


def test_shared_vertex_values_are_independent_of_triangulation_order():
    forward, forward_covered = _rasterized_square()
    reverse, reverse_covered = _rasterized_square(reverse_order=True)

    np.testing.assert_array_equal(forward_covered, reverse_covered)
    np.testing.assert_allclose(forward, reverse, atol=1e-6)


def test_interpolation_is_continuous_across_the_shared_diagonal():
    values, covered = _rasterized_square()

    expected = np.add.outer(
        np.arange(4, dtype=np.float64) + 0.5,
        np.arange(4, dtype=np.float64) + 0.5,
    )
    assert np.all(covered)
    np.testing.assert_allclose(values, expected, atol=1e-6)


def _material_triangle(base_rgba, class_ids, categorical, source_indices):
    return TerrainMaterialSamples(
        np.asarray([base_rgba], dtype=np.uint8),
        np.ones((1, 3), dtype=bool),
        np.asarray([class_ids], dtype=np.int64),
        np.asarray([categorical], dtype=bool),
        np.asarray([source_indices], dtype=np.int16),
    )


def _material_grid(base_rgba, class_ids, *, categorical=True, source=0):
    rgba = np.asarray(base_rgba, dtype=np.uint8)
    shape = rgba.shape[:-1]
    return TerrainMaterialSamples(
        rgba,
        np.ones(shape, dtype=bool),
        np.asarray(class_ids, dtype=np.int64),
        np.full(shape, categorical, dtype=bool),
        np.full(shape, source, dtype=np.int16),
    )


def _empty_material_grid():
    return TerrainMaterialSamples(
        np.empty((0, 0, 4), dtype=np.uint8),
        np.empty((0, 0), dtype=bool),
        np.empty((0, 0), dtype=np.int64),
        np.empty((0, 0), dtype=bool),
        np.empty((0, 0), dtype=np.int16),
    )


def test_categorical_triangle_never_blends_base_class_colours():
    triangle_id = np.zeros((3, 3), dtype=np.int32)
    u = np.asarray(
        [[0.8, 0.4, 0.1], [0.6, 0.3, 0.1], [0.2, 0.1, 0.0]],
        dtype=np.float32,
    )
    v = np.asarray(
        [[0.1, 0.5, 0.1], [0.2, 0.3, 0.1], [0.7, 0.2, 0.0]],
        dtype=np.float32,
    )
    green = (20, 120, 40, 255)
    violet = (130, 80, 180, 255)
    materials = _material_triangle(
        [green, green, violet],
        [83, 83, 105],
        [True, True, True],
        [0, 0, 0],
    )

    resolved = _resolve_triangle_material(triangle_id, u, v, materials)

    colors = {tuple(value) for value in resolved.base_rgba.reshape(-1, 4)}
    assert colors <= {green, violet}
    assert set(np.unique(resolved.class_ids)) <= {83, 105}


def test_categorical_material_is_selected_in_terrain_space_not_by_screen_vertex():
    green = (20, 120, 40, 255)
    violet = (130, 80, 180, 255)
    triangle_materials = _material_triangle(
        [green, green, violet],
        [83, 83, 105],
        [True, True, True],
        [0, 0, 0],
    )
    polar_materials = _material_grid(
        [[green, green], [violet, violet]],
        [[83, 83], [105, 105]],
    )
    azimuth_ten = np.radians(10.0)
    surface_xy = np.asarray(
        [
            [
                (0.0, 10.0),
                (10.0 * np.sin(azimuth_ten), 10.0 * np.cos(azimuth_ten)),
                (0.0, 20.0),
            ]
        ],
        dtype=np.float64,
    )

    resolved = _resolve_surface_material(
        np.zeros((1, 2), dtype=np.int32),
        np.asarray([[0.3, 0.1]], dtype=np.float32),
        np.asarray([[0.3, 0.1]], dtype=np.float32),
        triangle_materials,
        surface_xy,
        np.zeros((1, 3), dtype=np.uint8),
        polar_materials,
        np.asarray([10.0, 20.0]),
        np.asarray([0.0, 10.0]),
        _empty_material_grid(),
        np.empty(0),
        np.empty(0),
    )

    # At the first pixel the far/violet vertex has the greatest screen weight,
    # but the interpolated terrain point is still closer to the 10 m/green row.
    assert resolved.class_ids.tolist() == [[83, 105]]
    assert resolved.base_rgba.tolist() == [[list(green), list(violet)]]


def test_uniform_categorical_triangle_remains_one_material():
    green = (20, 120, 40, 255)
    violet = (130, 80, 180, 255)
    triangle_materials = _material_triangle(
        [green, green, green],
        [83, 83, 83],
        [True, True, True],
        [0, 0, 0],
    )
    polar_materials = _material_grid(
        [[green, green], [violet, violet]],
        [[83, 83], [105, 105]],
    )

    resolved = _resolve_surface_material(
        np.zeros((1, 1), dtype=np.int32),
        np.asarray([[0.05]], dtype=np.float32),
        np.asarray([[0.05]], dtype=np.float32),
        triangle_materials,
        np.asarray([[[0.0, 10.0], [1.0, 10.0], [0.0, 20.0]]]),
        np.zeros((1, 3), dtype=np.uint8),
        polar_materials,
        np.asarray([10.0, 20.0]),
        np.asarray([0.0, 10.0]),
        _empty_material_grid(),
        np.empty(0),
        np.empty(0),
    )

    assert resolved.class_ids[0, 0] == 83
    assert resolved.base_rgba[0, 0].tolist() == list(green)


def test_surface_material_lookup_is_independent_of_triangle_diagonal():
    green = (20, 120, 40, 255)
    violet = (130, 80, 180, 255)
    patch_materials = _material_grid(
        [[green, violet], [green, violet]],
        [[83, 105], [83, 105]],
    )
    polar_materials = _empty_material_grid()
    square_vertices = np.asarray(
        ((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)),
        dtype=np.float64,
    )

    def render(indices):
        triangle_xy = square_vertices[np.asarray(indices, dtype=np.int32)]
        _depth, triangle_id, bary_u, bary_v = _rasterize_terrain_triangles(
            triangle_xy,
            np.ones((2, 3), dtype=np.float64),
            4,
            4,
            supersample=1,
        )
        vertex_classes = np.where(
            triangle_xy[..., 0] < 2.0, 83, 105
        )
        vertex_rgba = np.where(
            (vertex_classes == 83)[..., None],
            np.asarray(green, dtype=np.uint8),
            np.asarray(violet, dtype=np.uint8),
        )
        triangle_materials = TerrainMaterialSamples(
            vertex_rgba,
            np.ones((2, 3), dtype=bool),
            vertex_classes,
            np.ones((2, 3), dtype=bool),
            np.zeros((2, 3), dtype=np.int16),
        )
        return _resolve_surface_material(
            triangle_id,
            bary_u,
            bary_v,
            triangle_materials,
            triangle_xy,
            np.ones((2, 3), dtype=np.uint8),
            polar_materials,
            np.empty(0),
            np.empty(0),
            patch_materials,
            np.asarray([0.0, 4.0]),
            np.asarray([0.0, 4.0]),
        )

    forward = render(((0, 1, 3), (1, 2, 3)))
    reverse = render(((0, 1, 2), (0, 2, 3)))

    np.testing.assert_array_equal(forward.class_ids, reverse.class_ids)
    np.testing.assert_array_equal(forward.base_rgba, reverse.base_rgba)
    assert forward.class_ids.tolist() == [
        [83, 83, 105, 105],
        [83, 83, 105, 105],
        [83, 83, 105, 105],
        [83, 83, 105, 105],
    ]


def test_categorical_cache_is_detected_for_surface_renderer_routing():
    cache = {
        "visual_valid": np.asarray([[True, False]]),
        "visual_categorical": np.asarray([[True, True]]),
    }

    assert _surface_cache_has_categorical_material(cache)
    cache["visual_valid"][:] = False
    assert not _surface_cache_has_categorical_material(cache)


def test_continuous_triangle_keeps_smooth_rgb_interpolation():
    materials = _material_triangle(
        [(0, 0, 0, 255), (120, 0, 0, 255), (0, 120, 0, 255)],
        [-1, -1, -1],
        [False, False, False],
        [2, 2, 2],
    )

    resolved = _resolve_triangle_material(
        np.zeros((1, 1), dtype=np.int32),
        np.asarray([[0.25]], dtype=np.float32),
        np.asarray([[0.25]], dtype=np.float32),
        materials,
    )

    assert resolved.base_rgba[0, 0].tolist() == [30, 60, 0, 255]
    assert resolved.source_indices[0, 0] == 2
    assert not resolved.categorical[0, 0]


def test_mixed_triangle_selects_one_deterministic_material():
    materials = _material_triangle(
        [(20, 120, 40, 255), (10, 20, 200, 255), (220, 180, 20, 255)],
        [83, -1, 105],
        [True, False, True],
        [0, 1, 2],
    )
    u = np.asarray([[0.5, 0.2]], dtype=np.float32)
    v = np.asarray([[0.5, 0.6]], dtype=np.float32)

    resolved = _resolve_triangle_material(
        np.zeros((1, 2), dtype=np.int32), u, v, materials
    )

    # Equal u/v weights select vertex zero; the second pixel selects vertex one.
    assert resolved.base_rgba[0, 0].tolist() == [20, 120, 40, 255]
    assert resolved.class_ids[0, 0] == 83
    assert resolved.source_indices[0, 0] == 0
    assert resolved.categorical[0, 0]
    assert resolved.base_rgba[0, 1].tolist() == [10, 20, 200, 255]
    assert resolved.class_ids[0, 1] == -1
    assert resolved.source_indices[0, 1] == 1
    assert not resolved.categorical[0, 1]


def test_same_category_keeps_discrete_base_with_continuous_lighting():
    materials = _material_triangle(
        [(20, 120, 40, 255)] * 3,
        [83, 83, 83],
        [True, True, True],
        [0, 0, 0],
    )
    triangle_id = np.zeros((1, 3), dtype=np.int32)
    u = np.asarray([[0.8, 0.3, 0.05]], dtype=np.float32)
    v = np.asarray([[0.1, 0.4, 0.05]], dtype=np.float32)
    resolved = _resolve_triangle_material(triangle_id, u, v, materials)
    light, _ = _interpolate_triangle_values(
        triangle_id,
        u,
        v,
        np.asarray([[[0.5], [1.0], [1.5]]]),
    )
    settings = TerrainRenderSettings(
        atmospheric_perspective_enabled=False
    )

    final = compose_vertex_rgba(
        resolved.base_rgba,
        light[..., 0],
        np.zeros((1, 3)),
        settings,
    )

    assert np.all(resolved.base_rgba == np.asarray((20, 120, 40, 255)))
    assert len(np.unique(final[..., 1])) == 3
    assert np.all(resolved.class_ids == 83)


def test_same_class_code_from_different_sources_is_not_one_material():
    materials = _material_triangle(
        [(20, 120, 40, 255), (180, 120, 20, 255), (20, 120, 40, 255)],
        [83, 83, 83],
        [True, True, True],
        [0, 1, 0],
    )

    resolved = _resolve_triangle_material(
        np.zeros((1, 2), dtype=np.int32),
        np.asarray([[0.2, 0.1]], dtype=np.float32),
        np.asarray([[0.7, 0.1]], dtype=np.float32),
        materials,
    )

    assert resolved.source_indices.tolist() == [[1, 0]]
    assert resolved.base_rgba[0, 0].tolist() == [180, 120, 20, 255]
    assert resolved.base_rgba[0, 1].tolist() == [20, 120, 40, 255]


def test_region_regularization_rounds_corners_and_absorbs_thin_streaks():
    background = np.asarray((130, 130, 40, 255), dtype=np.uint8)
    forest = np.asarray((20, 120, 40, 255), dtype=np.uint8)
    class_ids = np.full((31, 31), 62, dtype=np.int64)
    class_ids[8:24, 8:24] = 82
    class_ids[2:8, 15:17] = 82
    rgba = np.where(
        (class_ids == 82)[..., None],
        forest,
        background,
    ).astype(np.uint8)
    materials = TerrainMaterialSamples(
        rgba,
        np.ones((31, 31), dtype=bool),
        class_ids,
        np.ones((31, 31), dtype=bool),
        np.zeros((31, 31), dtype=np.int16),
    )

    regularized = _regularize_categorical_regions(
        materials,
        np.ones((31, 31), dtype=bool),
        radius_px=3.0,
    )

    assert regularized.class_ids[4, 15] == 62
    assert regularized.class_ids[8, 8] == 62
    assert regularized.class_ids[15, 15] == 82
    np.testing.assert_array_equal(
        regularized.base_rgba[4, 15], background
    )
    np.testing.assert_array_equal(regularized.valid, materials.valid)
    np.testing.assert_array_equal(
        regularized.categorical, materials.categorical
    )


def test_region_regularization_keeps_protected_small_building():
    background = np.asarray((40, 140, 70, 255), dtype=np.uint8)
    building = np.asarray((185, 105, 75, 255), dtype=np.uint8)
    class_ids = np.full((25, 25), 82, dtype=np.int64)
    class_ids[11:14, 11:14] = 62
    rgba = np.where(
        (class_ids == 62)[..., None], building, background
    ).astype(np.uint8)
    materials = TerrainMaterialSamples(
        rgba,
        np.ones((25, 25), dtype=bool),
        class_ids,
        np.ones((25, 25), dtype=bool),
        np.zeros((25, 25), dtype=np.int16),
    )
    protected = class_ids == 62

    regularized = _regularize_categorical_regions(
        materials,
        np.ones((25, 25), dtype=bool),
        radius_px=4.0,
        protected=protected,
    )

    assert np.all(regularized.class_ids[protected] == 62)
    np.testing.assert_array_equal(
        regularized.base_rgba[protected],
        np.broadcast_to(building, (9, 4)),
    )


def test_vibrant_palette_changes_colour_without_changing_category_identity():
    materials = TerrainMaterialSamples(
        np.asarray([[[210, 0, 0, 255], [20, 69, 249, 255]]], dtype=np.uint8),
        np.ones((1, 2), dtype=bool),
        np.asarray([[62, 162]], dtype=np.int64),
        np.ones((1, 2), dtype=bool),
        np.zeros((1, 2), dtype=np.int16),
    )
    cache = SimpleNamespace(
        source_legend_ids=("s2glc_europe_2017",)
    )

    vibrant = _vibrant_categorical_palette(materials, cache)

    assert not np.array_equal(vibrant.base_rgba, materials.base_rgba)
    np.testing.assert_array_equal(vibrant.class_ids, materials.class_ids)
    np.testing.assert_array_equal(
        vibrant.source_indices, materials.source_indices
    )


def test_categorical_variation_is_stable_and_low_frequency():
    rgba = np.full((1, 7, 4), (90, 160, 80, 255), dtype=np.uint8)
    materials = TerrainMaterialSamples(
        rgba,
        np.ones((1, 7), dtype=bool),
        np.full((1, 7), 82, dtype=np.int64),
        np.ones((1, 7), dtype=bool),
        np.zeros((1, 7), dtype=np.int16),
    )
    world_x = np.asarray([[0, 100, 200, 300, 400, 500, 600]])
    world_y = np.zeros((1, 7))

    first = _apply_categorical_territorial_variation(
        rgba, materials, world_x, world_y, np.ones((1, 7), dtype=bool)
    )
    second = _apply_categorical_territorial_variation(
        rgba, materials, world_x, world_y, np.ones((1, 7), dtype=bool)
    )

    np.testing.assert_array_equal(first, second)
    assert np.max(np.abs(np.diff(first[0, :, 1].astype(int)))) <= 2
    np.testing.assert_array_equal(first[..., 3], rgba[..., 3])
    np.testing.assert_array_equal(materials.class_ids, np.full((1, 7), 82))


def test_categorical_variation_changes_hue_territorially_not_as_pixel_noise():
    rgba = np.full((1, 7, 4), (90, 160, 80, 255), dtype=np.uint8)
    materials = TerrainMaterialSamples(
        rgba,
        np.ones((1, 7), dtype=bool),
        np.full((1, 7), 82, dtype=np.int64),
        np.ones((1, 7), dtype=bool),
        np.zeros((1, 7), dtype=np.int16),
    )
    world_x = np.arange(7, dtype=np.float64)[None, :] * 2_000.0

    varied = _apply_categorical_territorial_variation(
        rgba,
        materials,
        world_x,
        np.zeros((1, 7), dtype=np.float64),
        np.ones((1, 7), dtype=bool),
        luminance_variation=0.0,
        hue_variation=0.05,
    )

    np.testing.assert_array_equal(varied[..., 1], rgba[..., 1])
    assert np.ptp(varied[..., 0]) > 0
    assert np.ptp(varied[..., 2]) > 0
    assert np.max(np.abs(np.diff(varied[0, :, :3].astype(int), axis=0))) <= 1


def test_conifer_material_uses_dem_orientation_for_cool_dense_shade():
    rgba = np.broadcast_to(
        np.asarray((29, 122, 82, 255), dtype=np.uint8), (1, 2, 4)
    ).copy()
    materials = TerrainMaterialSamples(
        rgba,
        np.ones((1, 2), dtype=bool),
        np.full((1, 2), 83, dtype=np.int64),
        np.ones((1, 2), dtype=bool),
        np.zeros((1, 2), dtype=np.int16),
    )

    varied = _apply_categorical_territorial_variation(
        rgba,
        materials,
        np.zeros((1, 2)),
        np.zeros((1, 2)),
        np.ones((1, 2), dtype=bool),
        luminance_variation=0.0,
        hue_variation=0.0,
        slope_influence=0.12,
        light_intensity=np.full((1, 2), 0.72),
        normal_y=np.asarray(((-0.8, 0.8),)),
        normal_z=np.full((1, 2), 0.6),
        source_legend_ids=("s2glc_europe_2017",),
    )

    weights = np.asarray((0.2126, 0.7152, 0.0722))
    south_luminance = float(varied[0, 0, :3] @ weights)
    north_luminance = float(varied[0, 1, :3] @ weights)
    assert north_luminance < south_luminance
    assert (
        varied[0, 1, 2] / max(1, int(varied[0, 1, 0]))
        > varied[0, 0, 2] / max(1, int(varied[0, 0, 0]))
    )


def test_snow_reveals_mineral_colour_on_low_steep_sunlit_slope():
    rgba = np.broadcast_to(
        np.asarray((201, 239, 245, 255), dtype=np.uint8), (1, 2, 4)
    ).copy()
    materials = TerrainMaterialSamples(
        rgba,
        np.ones((1, 2), dtype=bool),
        np.full((1, 2), 123, dtype=np.int64),
        np.ones((1, 2), dtype=bool),
        np.zeros((1, 2), dtype=np.int16),
    )

    varied = _apply_categorical_territorial_variation(
        rgba,
        materials,
        np.zeros((1, 2)),
        np.zeros((1, 2)),
        np.ones((1, 2), dtype=bool),
        luminance_variation=0.0,
        hue_variation=0.0,
        snow_rock_blend=0.4,
        light_intensity=np.asarray(((0.90, 1.31),)),
        elevation_m=np.asarray(((2_600.0, 1_800.0),)),
        normal_x=np.asarray(((0.0, 0.8),)),
        normal_z=np.asarray(((1.0, 0.6),)),
        source_legend_ids=("s2glc_europe_2017",),
    )

    assert varied[0, 1, 2] < varied[0, 0, 2]
    assert varied[0, 1, 0] / varied[0, 1, 2] > (
        varied[0, 0, 0] / varied[0, 0, 2]
    )
    np.testing.assert_array_equal(materials.class_ids, ((123, 123),))


def test_water_material_is_lighter_near_shore_and_deeper_in_centre():
    water = np.zeros((11, 11), dtype=bool)
    water[3:8, 3:8] = True
    classes = np.where(water, 162, 83)
    rgba = np.empty((11, 11, 4), dtype=np.uint8)
    rgba[water] = (46, 134, 214, 255)
    rgba[~water] = (29, 122, 82, 255)
    materials = TerrainMaterialSamples(
        rgba,
        np.ones((11, 11), dtype=bool),
        classes,
        np.ones((11, 11), dtype=bool),
        np.zeros((11, 11), dtype=np.int16),
    )

    varied = _apply_categorical_territorial_variation(
        rgba,
        materials,
        np.zeros((11, 11)),
        np.zeros((11, 11)),
        np.ones((11, 11), dtype=bool),
        luminance_variation=0.0,
        hue_variation=0.0,
        water_shore_variation=0.15,
        source_legend_ids=("s2glc_europe_2017",),
    )

    weights = np.asarray((0.2126, 0.7152, 0.0722))
    shore_luminance = float(varied[3, 5, :3] @ weights)
    centre_luminance = float(varied[5, 5, :3] @ weights)
    assert shore_luminance > centre_luminance
    np.testing.assert_array_equal(materials.class_ids, classes)


def test_relief_occlusion_darkens_valleys_and_drives_layered_haze():
    elevation = np.zeros((9, 9), dtype=np.float32)
    elevation[4, 4] = -60.0
    valid = np.ones((9, 9), dtype=bool)
    occlusion = _vibrant_relief_occlusion(
        elevation,
        np.full((9, 9), 0.72, dtype=np.float32),
        valid,
        radius_px=2.0,
        relief_scale_m=35.0,
    )
    image = np.full((9, 9, 4), (120, 160, 100, 255), dtype=np.uint8)
    shaded = _apply_vibrant_ambient_occlusion(
        image, occlusion, valid, strength=0.15
    )
    distance = np.zeros((9, 9), dtype=np.float32)
    distance[4, 4] = 100_000.0
    haze = _vibrant_valley_haze(
        occlusion,
        distance,
        valid,
        maximum_distance_m=100_000.0,
        strength=0.12,
    )

    assert occlusion[4, 4] > occlusion[4, 3]
    assert np.all(shaded[4, 4, :3] < image[4, 4, :3])
    assert haze[4, 4] > haze[4, 3]
    np.testing.assert_array_equal(shaded[..., 3], image[..., 3])


def test_vibrant_bloom_is_soft_thresholded_and_stays_inside_surface():
    image = np.zeros((21, 21, 4), dtype=np.uint8)
    image[..., :3] = 30
    image[..., 3] = 255
    image[9:12, 9:12, :3] = 245
    valid = np.ones((21, 21), dtype=bool)
    valid[:, :2] = False
    settings = TerrainRenderSettings(
        surface_visual_style="vibrant",
        vibrant_bloom_strength=0.12,
        vibrant_bloom_threshold=0.72,
        vibrant_bloom_radius_px=3.0,
    )

    bloomed = _apply_vibrant_bloom(image, valid, settings)

    assert np.any(bloomed[7:14, 7:14, :3] > image[7:14, 7:14, :3])
    np.testing.assert_array_equal(bloomed[:, :2], image[:, :2])
    np.testing.assert_array_equal(bloomed[..., 3], image[..., 3])


def test_vibrant_bloom_does_not_glow_dark_terrain():
    image = np.full((7, 7, 4), (35, 45, 40, 255), dtype=np.uint8)
    settings = TerrainRenderSettings(surface_visual_style="vibrant")

    bloomed = _apply_vibrant_bloom(
        image, np.ones((7, 7), dtype=bool), settings
    )

    np.testing.assert_array_equal(bloomed, image)


def test_vibrant_bloom_is_disabled_without_moon_and_capped_under_full_moon():
    image = np.full((17, 17, 4), (220, 220, 220, 255), dtype=np.uint8)
    valid = np.ones((17, 17), dtype=bool)
    settings = TerrainRenderSettings(
        surface_visual_style="vibrant",
        vibrant_bloom_strength=0.15,
        vibrant_bloom_threshold=0.70,
        vibrant_bloom_radius_px=2.0,
        vibrant_moon_bloom_scale=0.12,
    )

    day = _apply_vibrant_bloom(
        image,
        valid,
        settings,
        daylight_factor=1.0,
        moonlight_factor=0.0,
    )
    moonless = _apply_vibrant_bloom(
        image,
        valid,
        settings,
        daylight_factor=0.0,
        moonlight_factor=0.0,
    )
    full_moon = _apply_vibrant_bloom(
        image,
        valid,
        settings,
        daylight_factor=0.0,
        moonlight_factor=1.0,
    )
    day_gain = day[..., :3].astype(int) - image[..., :3].astype(int)
    moon_gain = full_moon[..., :3].astype(int) - image[..., :3].astype(int)

    np.testing.assert_array_equal(moonless, image)
    assert np.max(moon_gain) > 0
    assert np.max(moon_gain) <= np.max(day_gain) * 0.12 + 1


def test_vibrant_bloom_favours_sunlit_highlights():
    image = np.full((31, 61, 4), (45, 55, 50, 255), dtype=np.uint8)
    image[12:19, 8:15, :3] = 225
    image[12:19, 46:53, :3] = 225
    light = np.full((31, 61), 0.68, dtype=np.float32)
    light[12:19, 46:53] = 1.31
    valid = np.ones((31, 61), dtype=bool)
    settings = TerrainRenderSettings(
        surface_visual_style="vibrant",
        vibrant_bloom_strength=0.16,
        vibrant_bloom_threshold=0.74,
        vibrant_bloom_radius_px=3.0,
    )

    bloomed = _apply_vibrant_bloom(
        image,
        valid,
        settings,
        light_intensity=light,
        distance_m=np.zeros((31, 61), dtype=np.float32),
    )
    delta = bloomed[..., :3].astype(int) - image[..., :3].astype(int)

    shadow_halo = int(np.max(delta[9:22, 5:18]))
    sunlit_halo = int(np.max(delta[9:22, 43:56]))
    assert sunlit_halo > shadow_halo
    np.testing.assert_array_equal(bloomed[..., 3], image[..., 3])


def test_vibrant_bloom_uses_low_opacity_additive_composition():
    image = np.full((17, 17, 4), (220, 220, 220, 255), dtype=np.uint8)
    valid = np.ones((17, 17), dtype=bool)
    settings = TerrainRenderSettings(
        surface_visual_style="vibrant",
        vibrant_bloom_strength=0.15,
        vibrant_bloom_threshold=0.70,
        vibrant_bloom_radius_px=2.0,
    )

    bloomed = _apply_vibrant_bloom(image, valid, settings)

    rgb = 220.0 / 255.0
    threshold_value = (rgb - 0.70) / 0.30
    bright = threshold_value * threshold_value * (
        3.0 - 2.0 * threshold_value
    )
    expected = round((rgb + rgb * bright * 0.15) * 255.0)
    assert np.all(bloomed[8, 8, :3] == expected)


def test_categorical_edges_are_softened_without_changing_material_identity():
    green = np.asarray((20, 120, 40, 255), dtype=np.uint8)
    violet = np.asarray((130, 80, 180, 255), dtype=np.uint8)
    image = np.empty((3, 6, 4), dtype=np.uint8)
    image[:, :3] = green
    image[:, 3:] = violet
    class_ids = np.broadcast_to(
        np.asarray((83, 83, 83, 105, 105, 105), dtype=np.int64),
        (3, 6),
    ).copy()
    materials = TerrainMaterialSamples(
        image.copy(),
        np.ones((3, 6), dtype=bool),
        class_ids,
        np.ones((3, 6), dtype=bool),
        np.zeros((3, 6), dtype=np.int16),
    )

    softened = _soften_categorical_edges(
        image, materials, np.ones((3, 6), dtype=bool)
    )

    np.testing.assert_array_equal(softened[:, 0], image[:, 0])
    np.testing.assert_array_equal(softened[:, -1], image[:, -1])
    assert not np.array_equal(softened[1, 2, :3], green[:3])
    assert not np.array_equal(softened[1, 3, :3], violet[:3])
    np.testing.assert_array_equal(softened[..., 3], image[..., 3])
    np.testing.assert_array_equal(materials.class_ids, class_ids)


def test_protected_small_category_is_not_diluted_by_edge_softening():
    forest = np.asarray((25, 125, 70, 255), dtype=np.uint8)
    building = np.asarray((185, 105, 75, 255), dtype=np.uint8)
    image = np.broadcast_to(forest, (5, 5, 4)).copy()
    image[2, 2] = building
    class_ids = np.full((5, 5), 82, dtype=np.int64)
    class_ids[2, 2] = 62
    materials = TerrainMaterialSamples(
        image.copy(),
        np.ones((5, 5), dtype=bool),
        class_ids,
        np.ones((5, 5), dtype=bool),
        np.zeros((5, 5), dtype=np.int16),
    )
    protected = class_ids == 62

    softened = _soften_categorical_edges(
        image,
        materials,
        np.ones((5, 5), dtype=bool),
        protected=protected,
    )

    np.testing.assert_array_equal(softened[2, 2], building)
    assert not np.array_equal(softened[2, 1, :3], forest[:3])


def test_categorical_smoothing_does_not_cross_the_terrain_silhouette():
    green = np.asarray((20, 120, 40, 255), dtype=np.uint8)
    image = np.zeros((3, 3, 4), dtype=np.uint8)
    image[1:] = green
    covered = np.zeros((3, 3), dtype=bool)
    covered[1:] = True
    materials = TerrainMaterialSamples(
        image.copy(),
        covered.copy(),
        np.where(covered, 83, -1),
        covered.copy(),
        np.where(covered, 0, -1),
    )

    softened = _soften_categorical_edges(image, materials, covered)

    np.testing.assert_array_equal(softened, image)


def _diagonal_horizon_fixture():
    triangles = np.asarray(
        (
            ((0.0, 1.0), (8.0, 5.0), (0.0, 8.0)),
            ((8.0, 5.0), (8.0, 8.0), (0.0, 8.0)),
        ),
        dtype=np.float64,
    )
    depth = np.ones((2, 3), dtype=np.float64)
    _depth, triangle_id, _u, _v = _rasterize_terrain_triangles(
        triangles, depth, 8, 8, supersample=1
    )
    rgba = np.zeros((8, 8, 4), dtype=np.uint8)
    rgba[triangle_id >= 0] = (40, 90, 150, 255)
    return triangles, triangle_id, rgba


def test_horizon_geometry_retains_float_precision_for_diagonal_line():
    triangles, triangle_id, _rgba = _diagonal_horizon_fixture()

    horizon = _geometry_horizon_y(triangle_id, triangles)
    expected = 1.0 + 0.5 * (np.arange(8) + 0.5)

    np.testing.assert_allclose(horizon, expected, atol=1e-6)


def test_horizon_coverage_antialiases_only_configured_edge_band():
    triangles, triangle_id, rgba = _diagonal_horizon_fixture()

    antialiased = _apply_horizon_coverage(
        rgba,
        triangle_id,
        triangles,
        filter_width_px=1.0,
        supersampling_factor=1,
    )

    partial = (antialiased[..., 3] > 0) & (antialiased[..., 3] < 255)
    assert np.count_nonzero(partial) >= 6
    for column, boundary in enumerate(1.0 + 0.5 * (np.arange(8) + 0.5)):
        interior_start = min(8, int(np.ceil(boundary + 1.0)))
        np.testing.assert_array_equal(
            antialiased[interior_start:, column],
            rgba[interior_start:, column],
        )


def test_horizon_coverage_uses_terrain_rgb_without_light_or_dark_halo():
    triangles, triangle_id, rgba = _diagonal_horizon_fixture()

    antialiased = _apply_horizon_coverage(
        rgba,
        triangle_id,
        triangles,
        filter_width_px=1.25,
        supersampling_factor=4,
    )
    affected = antialiased[..., 3] > 0

    np.testing.assert_array_equal(
        antialiased[..., :3][affected],
        np.broadcast_to((40, 90, 150), antialiased[..., :3][affected].shape),
    )
