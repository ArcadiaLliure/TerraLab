from types import SimpleNamespace

import numpy as np
from PyQt5.QtGui import QColor

from TerraLab.terrain.overlay import (
    HorizonOverlay,
    _qcolor_from_rgba,
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
