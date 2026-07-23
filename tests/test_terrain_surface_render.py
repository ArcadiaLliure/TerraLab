from types import SimpleNamespace

import numpy as np
from PyQt5.QtCore import QPointF
from PyQt5.QtGui import QColor, QPolygonF

from TerraLab.terrain.overlay import (
    HorizonOverlay,
    _TerrainGeometryMetrics,
    _TerrainSurfaceGeometry,
    _TerrainSurfaceSpan,
    _TerrainTriangleGeometry,
    _apply_horizon_coverage,
    _geometry_horizon_y,
    _interpolate_triangle_values,
    _qcolor_from_rgba,
    _rasterize_terrain_triangles,
    _resolve_terrain_render_path,
)
from TerraLab.terrain.representation import (
    TerrainGeometrySource,
    TerrainRepresentationMode,
)
from TerraLab.ui.canvas_runtime_helpers import (
    _terrain_relief_enabled_for_frame,
)


def _overlay() -> HorizonOverlay:
    return HorizonOverlay(
        horizon_profile_path=None,
        allow_procedural_fallback=False,
    )


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
    overlay._terrain_raster_cache_key = (id(geometry), 1, 1, 1)
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
    overlay._terrain_raster_cache_key = (id(geometry), 1, 1, 1)
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


def test_relief_is_only_suspended_for_the_active_camera_frame():
    assert _terrain_relief_enabled_for_frame(True, False) is True
    assert _terrain_relief_enabled_for_frame(True, True) is False
    assert _terrain_relief_enabled_for_frame(False, False) is False


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
