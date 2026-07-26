from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PyQt5.QtGui import QColor, QImage, QPainter

from TerraLab.astro.ngc_catalog import (
    NGCObject,
    iter_ngc_aliases,
    load_ngc_catalog,
)
from TerraLab.render.overlays_renderer import (
    draw_skyfield_objects,
    draw_sun_skyfield,
)
from TerraLab.render.sky.milkyway_overlay import MilkyWayOverlay
from TerraLab.render.stars_renderer import StarsRenderer
from TerraLab.scene.camera import Camera
from TerraLab.scene.projection import (
    project_universal_stereo_numpy,
    project_universal_stereo_point,
    radec_to_altaz_numpy,
)
from TerraLab.terrain.domain.profile import HorizonProfile
from TerraLab.terrain.mesh.normals import compute_polar_mesh_normals
from TerraLab.terrain.persistence.profile_npz import load_profile, save_profile
from TerraLab.terrain.overlay import HorizonOverlay
from TerraLab.terrain.render.geometry import (
    _build_terrain_surface_spans,
    _simplify_projected_boundaries,
)
from TerraLab.terrain.render.overlay_types import _extrema_lod_indices
from TerraLab.terrain.render.triangle_raster import (
    _rasterize_terrain_triangles,
)
from TerraLab.terrain.render.config import TerrainRenderSettings
from TerraLab.terrain.worker import HorizonWorker
from TerraLab.ui.astronomical_widget import AstronomicalWidget
from TerraLab.widgets.telescope_runtime import on_telescope_view_enabled


def _workspace_temp_path(name: str) -> Path:
    return Path(__file__).resolve().with_name(name)


def test_terrain_projection_uses_full_altitude_azimuth_coordinates():
    camera = Camera(
        azimuth_offset=90.0,
        elevation_angle=-13.4,
        zoom_level=1.5,
        vertical_offset_ratio=0.0,
    )
    width, height = 1919, 964
    altitudes = np.array([-20.0, 0.0, 5.0, 20.0, 80.0], dtype=np.float64)
    azimuths = np.array([30.0, 75.0, 90.0, 120.0, 145.0], dtype=np.float64)
    expected = [
        project_universal_stereo_point(alt, az, width, height, camera)
        for alt, az in zip(altitudes, azimuths)
    ]
    sx, sy, valid = project_universal_stereo_numpy(
        altitudes, azimuths, width, height, camera
    )
    assert np.all(valid)
    assert (
        np.max(np.abs(sx - np.array([point[0] for point in expected]))) < 1e-4
    )
    assert (
        np.max(np.abs(sy - np.array([point[1] for point in expected]))) < 1e-4
    )

    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    projected_x, projected_y = overlay._project_mesh_column(
        lambda altitude, azimuth: project_universal_stereo_point(
            altitude, azimuth, width, height, camera
        ),
        lambda altitude, azimuth: project_universal_stereo_numpy(
            altitude, azimuth, width, height, camera
        ),
        120.0,
        altitudes,
        height,
        9999.0,
    )
    expected_x, expected_y, _ = project_universal_stereo_numpy(
        altitudes,
        np.full_like(altitudes, 120.0),
        width,
        height,
        camera,
    )
    assert np.max(np.abs(projected_x - expected_x)) < 1e-4
    assert np.max(np.abs(projected_y - expected_y)) < 1e-4


def test_distance_silhouettes_use_the_full_sky_projection():
    class CapturingOverlay(HorizonOverlay):
        def __init__(self):
            super().__init__(
                horizon_profile_path=None, allow_procedural_fallback=False
            )
            self.projected = None

        def _fill_strip_downward_numpy(
            self, _painter, list_sx, list_sy, _color, _bottom_y, solid=False
        ):
            self.projected = (list_sx, list_sy, solid)

    overlay = CapturingOverlay()
    azimuths = np.asarray([179.0, 180.0, 181.0], dtype=np.float64)
    altitudes = np.asarray([1.0, 2.0, 3.0], dtype=np.float64)
    band = SimpleNamespace(
        points=(azimuths, altitudes),
        valid_mask=np.ones(azimuths.shape, dtype=bool),
    )

    def project_np(altitude, azimuth):
        altitude = np.asarray(altitude, dtype=np.float64)
        azimuth = np.asarray(azimuth, dtype=np.float64)
        return azimuth + altitude * 100.0, altitude * 7.0

    overlay._draw_band_linear(
        None,
        band,
        QColor(40, 80, 60),
        None,
        640,
        320,
        99_999.0,
        180.0,
        179.0,
        181.0,
        proj_fn_numpy=project_np,
        terrain_shading_enabled=False,
    )

    assert overlay.projected is not None
    list_sx, list_sy, solid = overlay.projected
    np.testing.assert_allclose(list_sx[0], [279.0, 380.0, 481.0])
    np.testing.assert_allclose(list_sy[0], [7.0, 14.0, 21.0])
    assert solid is True


def test_profile_lod_bounds_dense_series_and_preserves_extrema():
    values = np.zeros(72_000, dtype=np.float32)
    values[12_345] = 18.0
    values[54_321] = -11.0

    indices = _extrema_lod_indices(values, 1024)

    assert indices.size <= 1024
    assert indices[0] == 0
    assert indices[-1] == values.size - 1
    assert 12_345 in indices
    assert 54_321 in indices


def test_profile_interaction_reduces_depth_layers_but_keeps_endpoints():
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    overlay._layers = list(range(80))

    interactive = overlay._profile_layers_for_frame(True)

    assert len(overlay._profile_layers_for_frame(False)) == 80
    assert len(interactive) == 12
    assert interactive[0] == 0
    assert interactive[-1] == 79


def test_settled_profile_reuses_composited_terrain_image():
    azimuths = np.arange(0.0, 360.0, 0.5, dtype=np.float32)
    angles = np.deg2rad(2.0 + np.sin(np.deg2rad(azimuths * 3.0))).astype(
        np.float32
    )
    profile = HorizonProfile(
        azimuths=azimuths,
        bands=[
            {
                "id": "all",
                "angles": angles,
                "dists": np.full(azimuths.shape, 1_000.0, dtype=np.float32),
                "heights": np.full(azimuths.shape, 100.0, dtype=np.float32),
            }
        ],
        observer_lat=42.0,
        observer_lon=1.0,
        resolved_mask=np.ones(azimuths.shape, dtype=bool),
    )
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    overlay.set_profile(
        profile,
        layer_defs=[("all", QColor(10, 20, 30), QColor(70, 90, 70))],
    )
    image = QImage(320, 180, QImage.Format_ARGB32_Premultiplied)
    numpy_calls = {"count": 0}

    def project(altitude, azimuth):
        return (float(azimuth) - 180.0 + 160.0, 120.0 - float(altitude))

    def project_np(altitude, azimuth):
        numpy_calls["count"] += 1
        return (
            np.asarray(azimuth, dtype=np.float32) - 20.0,
            120.0 - np.asarray(altitude, dtype=np.float32),
        )

    def render_once():
        painter = QPainter(image)
        try:
            overlay.draw(
                painter,
                project,
                320,
                180,
                180.0,
                1.0,
                0.0,
                12.0,
                projection_fn_numpy=project_np,
                terrain_3d_enabled=False,
            )
        finally:
            painter.end()

    render_once()
    first_calls = numpy_calls["count"]
    render_once()

    assert first_calls > 0
    assert numpy_calls["count"] == first_calls
    assert overlay._profile_image_cache is not None


def test_terrain_zbuffer_keeps_nearest_triangle_per_subsample():
    triangles = np.array(
        [
            [[1.0, 1.0], [7.0, 1.0], [1.0, 7.0]],
            [[1.0, 1.0], [7.0, 1.0], [1.0, 7.0]],
        ],
        dtype=np.float64,
    )
    depths = np.array([[500.0] * 3, [100.0] * 3], dtype=np.float64)
    depth, triangle_id, _u, _v = _rasterize_terrain_triangles(
        triangles, depths, 8, 8, supersample=2
    )
    covered = triangle_id >= 0
    assert np.any(covered)
    assert np.all(triangle_id[covered] == 1)
    assert np.allclose(depth[covered], 100.0)


def _synthetic_surface_geometry(projected_y, valid=None, *, x=None):
    projected_y = np.asarray(projected_y, dtype=np.float32)
    if x is None:
        x = np.arange(projected_y.shape[1], dtype=np.float32) * 10.0
    if valid is None:
        valid = np.ones(projected_y.shape, dtype=bool)
    columns = np.arange(projected_y.shape[1], dtype=np.int32)
    return _build_terrain_surface_spans(
        distances=np.arange(1, projected_y.shape[0] + 1, dtype=np.float32)
        * 100.0,
        column_indices=columns,
        azimuths=columns.astype(np.float32) * 0.5,
        projected_x=np.asarray(x, dtype=np.float32),
        projected_y=projected_y,
        valid=np.asarray(valid, dtype=bool),
        height=100.0,
    )


def _synthetic_geometry_envelope(geometry, x):
    x = np.asarray(x, dtype=np.float64)
    envelope = np.full(x.shape, 200.0, dtype=np.float64)
    for span in geometry.spans:
        span_x = np.asarray(span.x, dtype=np.float64)
        span_y = np.asarray(span.top_y, dtype=np.float64)
        if span_x[0] > span_x[-1]:
            span_x = span_x[::-1]
            span_y = span_y[::-1]
        covered = (x >= span_x[0]) & (x <= span_x[-1])
        envelope[covered] = np.minimum(
            envelope[covered], np.interp(x[covered], span_x, span_y)
        )
    return envelope


def test_surface_geometry_preserves_peaks_saddles_and_occlusion():
    x = np.arange(9, dtype=np.float32) * 10.0
    near = np.asarray([62, 60, 58, 57, 56, 57, 58, 60, 62], dtype=np.float32)
    far = np.asarray([70, 55, 28, 42, 50, 41, 26, 54, 70], dtype=np.float32)
    hidden = np.full(9, 80.0, dtype=np.float32)
    geometry = _synthetic_surface_geometry(np.vstack((near, far, hidden)), x=x)

    actual = _synthetic_geometry_envelope(geometry, x)
    np.testing.assert_allclose(actual, np.minimum(near, far), atol=0.75)
    assert actual[2] < actual[4] and actual[6] < actual[4]
    assert 2 not in {span.row_index for span in geometry.spans}
    assert geometry.metrics.max_error_px <= 0.75


def test_surface_geometry_splits_nodata_and_reversed_projection():
    x = np.arange(9, dtype=np.float32)[::-1] * 10.0
    y = np.full((1, 9), 40.0, dtype=np.float32)
    valid = np.ones_like(y, dtype=bool)
    valid[0, 4] = False
    geometry = _synthetic_surface_geometry(y, valid, x=x)

    assert len(geometry.spans) == 2
    gap_x = float(x[4])
    assert all(
        not (float(span.x.min()) < gap_x < float(span.x.max()))
        for span in geometry.spans
    )


def test_surface_simplification_has_bounded_projected_error():
    x = np.arange(11, dtype=np.float32)[::-1] * 8.0
    top = np.asarray(
        [50, 48, 44, 30, 43, 52, 42, 27, 43, 49, 51], dtype=np.float32
    )
    bottom = np.full_like(top, 70.0)

    indices, error = _simplify_projected_boundaries(x, top, bottom, 0.75)

    assert {3, 5, 7}.issubset(set(indices.tolist()))
    assert error <= 0.75


def test_surface_geometry_matches_random_exhaustive_envelopes():
    rng = np.random.default_rng(20260717)
    x = np.arange(24, dtype=np.float32) * 5.0
    for _case in range(20):
        projected_y = rng.normal(55.0, 12.0, size=(6, 24)).astype(np.float32)
        valid = rng.random((6, 24)) > 0.08
        valid[:, 1:] |= valid[:, :-1]
        geometry = _synthetic_surface_geometry(projected_y, valid, x=x)
        actual = _synthetic_geometry_envelope(geometry, x)
        expected = np.min(np.where(valid, projected_y, 200.0), axis=0)
        drawable = np.any(valid, axis=0)
        np.testing.assert_allclose(
            actual[drawable], expected[drawable], atol=1.0
        )
        assert geometry.metrics.max_error_px <= 0.75 + 1e-6


def test_legacy_mesh_without_visible_uses_the_common_surface_geometry():
    altitudes = np.asarray(
        [[1.0, 2.0, 1.0], [2.0, 4.0, 2.0]], dtype=np.float32
    )
    mesh = {
        "version": 1,
        "azimuths": np.asarray([179.0, 180.0, 181.0], dtype=np.float32),
        "distances": np.asarray([100.0, 300.0], dtype=np.float32),
        "altitudes": altitudes,
        "elevations": np.full_like(altitudes, 200.0),
        "valid": np.ones_like(altitudes, dtype=bool),
    }
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    asset = overlay._prepare_terrain_render_asset(mesh)

    assert asset is not None
    assert np.all(asset.visible)
    assert asset.visible_closed.shape == (2, 4)

    def project(_alt, azimuth):
        return (float(azimuth) - 178.0, 80.0)

    geometry = overlay._terrain_geometry_for_view(
        asset,
        project,
        240,
        120,
        10.0,
        180.0,
        178.0,
        182.0,
    )
    assert geometry.spans

    explicit_mesh = dict(mesh)
    explicit_mesh["version"] = 2
    explicit_mesh["visible"] = np.ones_like(altitudes, dtype=bool)
    explicit_asset = overlay._prepare_terrain_render_asset(explicit_mesh)
    explicit_geometry = overlay._terrain_geometry_for_view(
        explicit_asset,
        project,
        240,
        120,
        10.0,
        180.0,
        178.0,
        182.0,
    )
    assert len(explicit_geometry.spans) == len(geometry.spans)
    for legacy_span, explicit_span in zip(
        geometry.spans, explicit_geometry.spans
    ):
        np.testing.assert_array_equal(
            legacy_span.column_indices, explicit_span.column_indices
        )
        np.testing.assert_allclose(legacy_span.top_y, explicit_span.top_y)


def test_surface_geometry_keeps_the_circular_azimuth_seam_continuous():
    azimuths = np.arange(360, dtype=np.float32)
    altitudes = np.ones((2, 360), dtype=np.float32)
    mesh = {
        "version": 2,
        "azimuths": azimuths,
        "distances": np.asarray([100.0, 300.0], dtype=np.float32),
        "altitudes": altitudes,
        "elevations": np.full_like(altitudes, 200.0),
        "valid": np.ones_like(altitudes, dtype=bool),
        "visible": np.ones_like(altitudes, dtype=bool),
    }
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    asset = overlay._prepare_terrain_render_asset(mesh)

    def project(_alt, azimuth):
        return ((float(azimuth) + 3.0) * 10.0, 80.0)

    geometry = overlay._terrain_geometry_for_view(
        asset,
        project,
        80,
        120,
        10.0,
        0.0,
        -3.0,
        3.0,
    )

    assert geometry.spans
    crossing = [
        span
        for span in geometry.spans
        if np.any(span.column_indices < 10)
        and np.any(span.column_indices > 350)
    ]
    assert crossing
    assert all(np.all(np.diff(span.x) > 0.0) for span in crossing)


def test_ngc_catalog_loader_accepts_openngc_csv():
    csv_path = _workspace_temp_path("_test_openngc_catalog.csv")
    try:
        csv_path.write_text(
            "name,obj_type,raj2000,dej2000,constellation,maj_ax_deg,min_ax_deg,pos_ang,mag_b,mag_v,mag_j,mag_h,mag_k,surf_br_B,hubble_type,messier_nr,other_ngc,ic_cross,comname,notes\n"
            "NGC0001,G,10.0,20.0,And,0.4,0.2,30.0,11.1,10.5,,,,22.1,Sb,31,,,Galaxy One,\n"
            "BAD,G,,20.0,And,0.4,0.2,30.0,11.1,10.5,,,,22.1,Sb,31,,,Galaxy Two,\n",
            encoding="utf-8",
        )
        catalog = load_ngc_catalog(csv_path)
        assert len(catalog) == 1
        assert catalog[0].name in {"NGC0001", "NGC1"}
        assert catalog[0].effective_mag == 10.5
    finally:
        csv_path.unlink(missing_ok=True)


def test_ngc_aliases_include_messier_compact_and_common_name_tokens():
    obj = NGCObject(
        name="NGC 224",
        obj_type="G",
        ra_deg=10.0,
        dec_deg=20.0,
        maj_deg=3.0,
        min_deg=1.0,
        pos_ang_deg=35.0,
        mag_v=3.4,
        mag_b=4.1,
        surf_br_B=None,
        hubble_type="Sb",
        messier_nr=31,
        common_name="Andromeda Galaxy",
        notes=None,
    )
    aliases = iter_ngc_aliases(obj)
    assert "NGC 224" in aliases
    assert "NGC224" in aliases
    assert "M31" in aliases
    assert "M 31" in aliases
    assert "Messier 31" in aliases
    assert "Andromeda Galaxy" in aliases
    assert "Andromeda" in aliases
    assert "Galaxy" in aliases


def test_widget_ngc_search_loader_reads_openngc_catalog():
    widget = AstronomicalWidget.__new__(AstronomicalWidget)
    widget._ngc_search_entries_cache = None
    widget._astro_ngc_catalog_path = str(
        Path(__file__).resolve().parents[1]
        / "TerraLab"
        / "data"
        / "sky"
        / "openngc_catalog.csv"
    )
    entries = AstronomicalWidget._load_ngc_search_entries(widget)
    assert entries
    assert any(
        obj.name.replace(" ", "") in {"NGC224", "NGC0224"}
        or obj.messier_nr == 31
        for obj in entries
    )


def test_search_selection_snaps_object_into_view_and_marks_it_immediately():
    class _ScopeController:
        def set_center(self, center):
            self.center = center

    class _CanvasStub:
        def __init__(self):
            self.selected_target = None
            self.azimuth_offset = 0.0
            self.elevation_angle = 0.0
            self.dragging = True
            self.scope_controller = _ScopeController()

        def _set_selected_target(self, target):
            self.selected_target = target

        def scope_mode_enabled(self):
            return False

        def update(self):
            self.updated = True

    class _TimerStub:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    widget = AstronomicalWidget.__new__(AstronomicalWidget)
    widget.canvas = _CanvasStub()
    widget.anim_timer = _TimerStub()
    widget.target_azimuth = 12.0
    widget.target_elevation = -4.0
    widget.get_horizontal_coords = lambda ra, dec: (201.5, 37.25)

    obj = NGCObject(
        name="NGC1976",
        obj_type="Cl+N",
        ra_deg=83.8187,
        dec_deg=-5.3897,
        maj_deg=1.5,
        min_deg=1.0,
        pos_ang_deg=0.0,
        mag_v=4.0,
        mag_b=4.0,
        surf_br_B=None,
        hubble_type=None,
        messier_nr=42,
        common_name=None,
        notes=None,
    )
    info = {"type": "ngc", "obj": obj, "name": "M42"}
    AstronomicalWidget.center_on_object(widget, info)

    assert widget.canvas.selected_target == {
        "kind": "ngc",
        "obj": obj,
        "info": info,
    }
    assert widget.canvas.azimuth_offset == 201.5
    assert widget.canvas.elevation_angle == 37.25
    assert widget.canvas.dragging is False
    assert widget.target_azimuth is None
    assert widget.target_elevation is None
    assert widget.anim_timer.stopped is True


def test_horizon_worker_parses_json_progress_events_and_formats_user_message():
    event = HorizonWorker._parse_json_event(
        '{"type":"progress","job_id":"abc","phase":"bake","percent":47.3,"current":183,"total":720}'
    )
    assert event == {
        "type": "progress",
        "job_id": "abc",
        "phase": "bake",
        "percent": 47.3,
        "current": 183,
        "total": 720,
    }
    text = HorizonWorker._format_progress_text(event)
    assert "47.3%" in text
    assert "183/720" in text
    assert HorizonWorker._parse_json_event("plain log line") is None


def test_horizon_profile_roundtrip_preserves_resolved_mask():
    temp_path = _workspace_temp_path("_test_horizon_preview.npz")
    profile = HorizonProfile(
        azimuths=np.array([0.0, 0.5, 1.0], dtype=np.float32),
        bands=[
            {
                "id": "near_0_1",
                "angles": np.array([0.1, 0.2, 0.3], dtype=np.float32),
                "dists": np.array([10.0, 20.0, 30.0], dtype=np.float32),
                "heights": np.array([100.0, 120.0, 140.0], dtype=np.float32),
            }
        ],
        observer_lat=42.58,
        observer_lon=1.0,
        light_domes=np.array([0.0, 1.0, 0.5], dtype=np.float32),
        light_peak_distances=np.array([0.0, 3000.0, 5000.0], dtype=np.float32),
        resolved_mask=np.array([True, False, True], dtype=bool),
    )
    try:
        save_profile(profile, temp_path)
        loaded = load_profile(temp_path)
        assert loaded.resolved_mask.tolist() == [True, False, True]
    finally:
        temp_path.unlink(missing_ok=True)


def test_horizon_profile_roundtrip_preserves_surface_points():
    temp_path = _workspace_temp_path("_test_horizon_surface.npz")
    profile = HorizonProfile(
        azimuths=np.array([0.0, 0.5], dtype=np.float32),
        bands=[
            {
                "id": "near_0_1",
                "angles": np.array([0.3, 0.4], dtype=np.float32),
                "dists": np.array([10.0, 20.0], dtype=np.float32),
                "heights": np.array([100.0, 120.0], dtype=np.float32),
                "surface_angles": np.array([0.1, 0.2], dtype=np.float32),
                "surface_dists": np.array([15.0, 25.0], dtype=np.float32),
                "surface_heights": np.array([90.0, 110.0], dtype=np.float32),
            }
        ],
        observer_lat=42.58,
        observer_lon=1.0,
    )
    try:
        save_profile(profile, temp_path)
        loaded = load_profile(temp_path)
        band = loaded.bands[0]
        assert np.allclose(band["surface_angles"], [0.1, 0.2])
        pts = loaded.get_band_surface_points("near_0_1")
        assert pts[0][0] == 0.0
        assert np.isclose(pts[1][1], np.rad2deg(0.2))
    finally:
        temp_path.unlink(missing_ok=True)


def test_horizon_profile_roundtrip_preserves_terrain_mesh():
    temp_path = _workspace_temp_path("_test_horizon_mesh.npz")
    mesh = {
        "version": 3,
        "azimuths": np.array([0.0, 1.0], dtype=np.float32),
        "distances": np.array([100.0, 200.0], dtype=np.float32),
        "altitudes": np.array([[1.0, 1.1], [1.3, 1.4]], dtype=np.float32),
        "elevations": np.array(
            [[100.0, 102.0], [120.0, 122.0]], dtype=np.float32
        ),
        "normal_x": np.zeros((2, 2), dtype=np.float32),
        "normal_y": np.zeros((2, 2), dtype=np.float32),
        "normal_z": np.ones((2, 2), dtype=np.float32),
        "valid": np.ones((2, 2), dtype=bool),
        "visible": np.array([[True, True], [False, True]], dtype=bool),
        "near_patch_eastings": np.array([-1.0, 0.0, 1.0], dtype=np.float32),
        "near_patch_northings": np.array([-1.0, 0.0, 1.0], dtype=np.float32),
        "near_patch_altitudes": np.full((3, 3), -45.0, dtype=np.float32),
        "near_patch_elevations": np.full((3, 3), 100.0, dtype=np.float32),
        "near_patch_normal_x": np.zeros((3, 3), dtype=np.float32),
        "near_patch_normal_y": np.zeros((3, 3), dtype=np.float32),
        "near_patch_normal_z": np.ones((3, 3), dtype=np.float32),
        "near_patch_valid": np.ones((3, 3), dtype=bool),
    }
    profile = HorizonProfile(
        azimuths=np.array([0.0, 0.5], dtype=np.float32),
        bands=[],
        observer_lat=42.58,
        observer_lon=1.0,
        terrain_mesh=mesh,
    )
    try:
        save_profile(profile, temp_path)
        loaded = load_profile(temp_path)
        assert loaded.terrain_mesh is not None
        assert loaded.terrain_mesh["version"] == 3
        assert np.allclose(loaded.terrain_mesh["distances"], [100.0, 200.0])
        assert loaded.terrain_mesh["valid"].dtype == bool
        assert loaded.terrain_mesh["visible"].dtype == bool
        assert loaded.terrain_mesh["visible"].tolist() == [
            [True, True],
            [False, True],
        ]
        assert loaded.terrain_mesh["near_patch_altitudes"].shape == (3, 3)
        assert loaded.terrain_mesh["near_patch_valid"].all()
    finally:
        temp_path.unlink(missing_ok=True)


def test_horizon_profile_loads_legacy_terrain_mesh_without_visible():
    temp_path = _workspace_temp_path("_test_horizon_mesh_legacy.npz")
    mesh = {
        "azimuths": np.array([0.0, 1.0], dtype=np.float32),
        "distances": np.array([100.0, 200.0], dtype=np.float32),
        "altitudes": np.array([[1.0, 1.1], [1.3, 1.4]], dtype=np.float32),
        "valid": np.ones((2, 2), dtype=bool),
    }
    profile = HorizonProfile(
        azimuths=np.array([0.0, 0.5], dtype=np.float32),
        bands=[],
        observer_lat=42.58,
        observer_lon=1.0,
        terrain_mesh=mesh,
    )
    try:
        save_profile(profile, temp_path)
        loaded = load_profile(temp_path)
        assert loaded.terrain_mesh is not None
        assert loaded.terrain_mesh["version"] == 1
        assert loaded.terrain_mesh["visible"].dtype == bool
        assert loaded.terrain_mesh["visible"].size == 0
    finally:
        temp_path.unlink(missing_ok=True)


def test_horizon_overlay_draws_terrain_mesh_offscreen():
    mesh = {
        "version": 2,
        "azimuths": np.array([179.0, 180.0, 181.0], dtype=np.float32),
        "distances": np.array([100.0, 250.0, 600.0], dtype=np.float32),
        "altitudes": np.array(
            [[-1.0, -0.8, -1.1], [1.0, 1.4, 0.8], [2.0, 2.5, 1.7]],
            dtype=np.float32,
        ),
        "elevations": np.full((3, 3), 200.0, dtype=np.float32),
        "normal_x": np.zeros((3, 3), dtype=np.float32),
        "normal_y": np.zeros((3, 3), dtype=np.float32),
        "normal_z": np.ones((3, 3), dtype=np.float32),
        "valid": np.ones((3, 3), dtype=bool),
        "visible": np.ones((3, 3), dtype=bool),
    }
    profile = HorizonProfile(
        azimuths=np.array([], dtype=np.float32),
        bands=[],
        observer_lat=42.58,
        observer_lon=1.0,
        terrain_mesh=mesh,
    )
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    overlay.set_profile(profile, layer_defs=[])
    image = QImage(240, 120, QImage.Format_ARGB32)
    image.fill(QColor(120, 170, 210))
    painter = QPainter(image)

    def project(altitude, azimuth):
        return ((float(azimuth) - 178.0) * 40.0, 80.0 - float(altitude) * 12.0)

    def project_np(altitude, azimuth):
        azimuth = np.asarray(azimuth, dtype=np.float64)
        altitude = np.asarray(altitude, dtype=np.float64)
        return (azimuth - 178.0) * 40.0, 80.0 - altitude * 12.0

    def sky_color(_alt, _azimuth, _sun_alt, _sun_az):
        return QColor(140, 190, 225)

    try:
        overlay.draw(
            painter,
            project,
            240,
            120,
            180.0,
            1.0,
            0.0,
            12.0,
            projection_fn_numpy=project_np,
            sun_alt=20.0,
            sun_az=220.0,
            sky_color_fn=sky_color,
        )
    finally:
        painter.end()

    assert image.width() == 240
    assert overlay._last_surface2d_quads > 0
    assert overlay._last_surface2d_quads <= overlay._max_terrain_surface_quads


def test_cartesian_near_patch_covers_nadir_without_a_polar_cap_or_hole():
    azimuths = np.asarray([0.0, 90.0, 180.0, 270.0], dtype=np.float32)
    distances = np.asarray([40.0, 100.0], dtype=np.float32)
    altitudes = np.degrees(np.arctan2(-1.7, distances[:, None])).astype(
        np.float32
    )
    altitudes = np.broadcast_to(altitudes, (2, 4)).copy()
    patch_axis = np.asarray(
        [-4.0, -2.0, -1.0, 0.0, 1.0, 2.0, 4.0], dtype=np.float32
    )
    patch_east, patch_north = np.meshgrid(patch_axis, patch_axis)
    patch_distance = np.hypot(patch_east, patch_north)
    patch_altitudes = np.degrees(
        np.arctan2(-1.7, np.maximum(patch_distance, 1e-9))
    ).astype(np.float32)
    mesh = {
        "version": 3,
        "azimuths": azimuths,
        "distances": distances,
        "altitudes": altitudes,
        "elevations": np.full_like(altitudes, 200.0),
        "valid": np.ones_like(altitudes, dtype=bool),
        "visible": np.ones_like(altitudes, dtype=bool),
        "near_patch_eastings": patch_axis,
        "near_patch_northings": patch_axis,
        "near_patch_altitudes": patch_altitudes,
        "near_patch_elevations": np.full_like(patch_altitudes, 200.0),
        "near_patch_normal_x": np.zeros_like(patch_altitudes),
        "near_patch_normal_y": np.zeros_like(patch_altitudes),
        "near_patch_normal_z": np.ones_like(patch_altitudes),
        "near_patch_valid": np.ones_like(patch_altitudes, dtype=bool),
    }
    relief_rgba = np.full((2, 4, 4), (230, 120, 20, 255), dtype=np.uint8)
    near_rgba = np.full(
        patch_altitudes.shape + (4,), (24, 48, 72, 255), dtype=np.uint8
    )
    surface_cache = SimpleNamespace(
        near_patch_rgba=near_rgba,
        near_patch_valid=np.ones_like(patch_altitudes, dtype=bool),
        relief_rgba=relief_rgba,
        relief_valid=np.ones((2, 4), dtype=bool),
        relief_source_indices=np.zeros((2, 4), dtype=np.int16),
        relief_distance_indices=np.arange(2, dtype=np.int32),
        relief_azimuth_indices=np.arange(4, dtype=np.int32),
        visual_altitudes=None,
        visual_rgba=None,
    )
    profile = HorizonProfile(
        azimuths=np.asarray([], dtype=np.float32),
        bands=[],
        observer_lat=42.58,
        observer_lon=1.0,
        terrain_mesh=mesh,
        surface_samples=surface_cache,
    )
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    overlay.render_settings = TerrainRenderSettings.from_mapping(
        {
            "terrain_lighting_enabled": False,
            "atmospheric_perspective_enabled": False,
            "horizon_antialiasing_enabled": False,
        }
    )
    overlay.set_profile(profile, layer_defs=[])
    width, height = 320, 180
    camera = Camera(
        azimuth_offset=180.0,
        elevation_angle=-51.0,
        zoom_level=1.0,
        vertical_offset_ratio=0.0,
    )
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)

    def project(altitude, azimuth):
        return project_universal_stereo_point(
            altitude, azimuth, width, height, camera
        )

    def project_np(altitude, azimuth):
        return project_universal_stereo_numpy(
            altitude, azimuth, width, height, camera
        )

    try:
        rendered = overlay._draw_terrain_interpolated(
            painter,
            mesh,
            project,
            width,
            height,
            180.0,
            0.0,
            360.0,
            0.0,
            QColor(120, 170, 210),
            projection_fn_numpy=project_np,
        )
    finally:
        painter.end()

    assert rendered is True
    geometry = overlay._terrain_geometry_cache
    assert not np.any(geometry.vertex_rows < 0)
    assert np.any(geometry.vertex_domain == 1)
    centre = QColor.fromRgba(image.pixel(width // 2, height - 2))
    expected = np.asarray((24, 48, 72), dtype=np.int16)
    np.testing.assert_allclose(
        np.asarray(centre.getRgb()[:3]), expected, atol=5
    )


def test_horizon_overlay_does_not_truncate_visible_surface_spans_offscreen():
    azimuths = np.linspace(150.0, 210.0, 61, dtype=np.float32)
    distances = np.linspace(50.0, 5_000.0, 36, dtype=np.float32)
    altitudes = np.zeros((distances.size, azimuths.size), dtype=np.float32)
    for d_idx, _distance in enumerate(distances):
        altitudes[d_idx, :] = 1.0 + float(d_idx) * 0.2
    mesh = {
        "version": 2,
        "azimuths": azimuths,
        "distances": distances,
        "altitudes": altitudes,
        "elevations": np.full_like(altitudes, 200.0, dtype=np.float32),
        "normal_x": np.zeros_like(altitudes, dtype=np.float32),
        "normal_y": np.zeros_like(altitudes, dtype=np.float32),
        "normal_z": np.ones_like(altitudes, dtype=np.float32),
        "valid": np.ones_like(altitudes, dtype=bool),
        "visible": np.ones_like(altitudes, dtype=bool),
    }
    profile = HorizonProfile(
        azimuths=np.array([], dtype=np.float32),
        bands=[],
        observer_lat=42.58,
        observer_lon=1.0,
        terrain_mesh=mesh,
    )
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    overlay._max_terrain_surface_quads = 12
    overlay.set_profile(profile, layer_defs=[])
    image = QImage(240, 120, QImage.Format_ARGB32)
    image.fill(QColor(120, 170, 210))
    painter = QPainter(image)

    def project(altitude, azimuth):
        return ((float(azimuth) - 150.0) * 4.0, 80.0 - float(altitude) * 12.0)

    def project_np(altitude, azimuth):
        azimuth = np.asarray(azimuth, dtype=np.float32)
        altitude = np.asarray(altitude, dtype=np.float32)
        return (azimuth - 150.0) * 4.0, 80.0 - altitude * 12.0

    try:
        overlay.draw(
            painter,
            project,
            240,
            120,
            180.0,
            1.0,
            0.0,
            12.0,
            projection_fn_numpy=project_np,
            sun_alt=20.0,
            sun_az=220.0,
        )
    finally:
        painter.end()

    assert overlay._last_surface2d_quads > 12
    assert overlay._last_surface2d_max_error_px <= 0.75


def test_terrain_3d_toggle_switches_between_mesh_and_all_distance_silhouettes():
    class RecordingOverlay(HorizonOverlay):
        def __init__(self):
            super().__init__(
                horizon_profile_path=None, allow_procedural_fallback=False
            )
            self.band_colors = []
            self.surface_calls = 0
            self.ground_calls = 0

        def _draw_band_linear(self, _painter, _band, color, *_args, **_kwargs):
            self.band_colors.append(QColor(color))

        def _draw_ground_linear(self, *_args, **_kwargs):
            self.ground_calls += 1

        def _draw_terrain_surface_2d(self, *_args, **_kwargs):
            self.surface_calls += 1

    azimuths = np.asarray([179.0, 180.0, 181.0], dtype=np.float32)
    altitudes = np.asarray(
        [[0.5, 0.8, 0.5], [1.0, 1.5, 1.0]], dtype=np.float32
    )
    mesh = {
        "version": 2,
        "azimuths": azimuths,
        "distances": np.asarray([100.0, 1_000.0], dtype=np.float32),
        "altitudes": altitudes,
        "elevations": np.full_like(altitudes, 200.0),
        "valid": np.ones_like(altitudes, dtype=bool),
        "visible": np.ones_like(altitudes, dtype=bool),
    }
    overlay = RecordingOverlay()
    overlay.profile = SimpleNamespace(
        terrain_mesh=mesh,
        resolved_mask=np.ones(azimuths.shape, dtype=bool),
    )
    for index in range(20):
        distance_factor = index / 19.0
        band = SimpleNamespace(
            band_min=float((19 - index) * 5_000.0),
            band_max=float((20 - index) * 5_000.0),
            points=(azimuths, np.full(azimuths.shape, 0.5 + index * 0.02)),
            valid_mask=np.ones(azimuths.shape, dtype=bool),
        )
        day = QColor(
            int(178 - 116 * distance_factor),
            int(194 - 108 * distance_factor),
            int(210 - 146 * distance_factor),
        )
        overlay._layers.append((band, QColor(day).darker(180), day))

    image = QImage(240, 120, QImage.Format_ARGB32)
    painter = QPainter(image)

    def project(altitude, azimuth):
        return ((float(azimuth) - 178.0) * 40.0, 80.0 - float(altitude) * 12.0)

    def project_np(altitude, azimuth):
        altitude = np.asarray(altitude, dtype=np.float64)
        azimuth = np.asarray(azimuth, dtype=np.float64)
        return (azimuth - 178.0) * 40.0, 80.0 - altitude * 12.0

    try:
        overlay.draw(
            painter,
            project,
            240,
            120,
            180.0,
            1.0,
            0.0,
            12.0,
            projection_fn_numpy=project_np,
            terrain_3d_enabled=False,
        )
        assert len(overlay.band_colors) == 20
        assert overlay.surface_calls == 0
        assert overlay.ground_calls == 0
        assert overlay._terrain_surface_image_geometry is None
        far_color = overlay.band_colors[0]
        near_color = overlay.band_colors[-1]
        assert (
            far_color.blue() - far_color.red()
            > near_color.blue() - near_color.red()
        )
        assert near_color.green() > near_color.blue()

        overlay.band_colors.clear()
        overlay.ground_calls = 0
        overlay.draw(
            painter,
            project,
            240,
            120,
            180.0,
            1.0,
            0.0,
            12.0,
            projection_fn_numpy=project_np,
            terrain_3d_enabled=True,
        )
        assert overlay.band_colors == []
        assert overlay.surface_calls == 1
        assert overlay.ground_calls == 0
    finally:
        painter.end()


def test_profile_mode_paints_sampled_surface_material():
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    overlay._profile_surface_samples = lambda _band, azimuths: (
        np.tile(
            np.asarray([210, 35, 25, 255], dtype=np.uint8),
            (len(azimuths), 1),
        ),
        np.ones(len(azimuths), dtype=bool),
    )
    light_grid = SimpleNamespace(
        intensity=np.ones(2, dtype=np.float32),
        solar_exposure=np.ones(2, dtype=np.float32),
        lunar_exposure=np.zeros(2, dtype=np.float32),
        factors=SimpleNamespace(),
    )
    overlay._terrain_profile_light_grid = lambda *_args, **_kwargs: light_grid
    overlay._compose_profile_light_color = (
        lambda color, *_args, **_kwargs: QColor(color)
    )
    image = QImage(120, 100, QImage.Format_ARGB32)
    image.fill(QColor(0, 0, 0))
    painter = QPainter(image)
    try:
        overlay._fill_shaded_strip_downward_numpy(
            painter,
            [np.asarray([10.0, 110.0])],
            [np.asarray([40.0, 40.0])],
            [np.asarray([179.0, 181.0])],
            [np.asarray([1.0, 1.0])],
            QColor(40, 80, 120),
            100.0,
            25.0,
            180.0,
            SimpleNamespace(band_max=1_000.0),
            QColor(100, 140, 180),
        )
    finally:
        painter.end()

    painted = image.pixelColor(60, 60)
    assert painted.red() > 180
    assert painted.green() < 60
    assert painted.blue() < 60


def test_terrain_light_factor_tracks_solar_azimuth_and_haze():
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    east_sun = overlay._sun_vector_enu(30.0, 90.0)
    west_sun = overlay._sun_vector_enu(30.0, 270.0)
    below_east_sun = overlay._sun_vector_enu(-8.0, 90.0)

    east_facing = float(
        np.asarray(
            overlay._terrain_light_factor(
                1.0, 0.0, 0.0, 1_000.0, east_sun, 30.0, True
            )
        )
    )
    west_facing = float(
        np.asarray(
            overlay._terrain_light_factor(
                -1.0, 0.0, 0.0, 1_000.0, east_sun, 30.0, True
            )
        )
    )
    flipped = float(
        np.asarray(
            overlay._terrain_light_factor(
                -1.0, 0.0, 0.0, 1_000.0, west_sun, 30.0, True
            )
        )
    )
    below_horizon = float(
        np.asarray(
            overlay._terrain_light_factor(
                1.0, 0.0, 0.0, 1_000.0, below_east_sun, -8.0, True
            )
        )
    )
    near_shadow = float(
        np.asarray(
            overlay._terrain_light_factor(
                -1.0, 0.0, 0.0, 1_000.0, east_sun, 30.0, True
            )
        )
    )
    far_shadow = float(
        np.asarray(
            overlay._terrain_light_factor(
                -1.0, 0.0, 0.0, 80_000.0, east_sun, 30.0, True
            )
        )
    )

    assert east_facing > west_facing + 0.08
    assert flipped > west_facing + 0.08
    assert 0.0 < below_horizon < west_facing
    assert np.isclose(far_shadow, near_shadow)


def test_terrain_sun_visibility_casts_shadow_behind_ridge():
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    azimuths = np.arange(0.0, 360.0, 10.0, dtype=np.float32)
    distances = np.array(
        [100.0, 180.0, 240.0, 340.0, 520.0, 800.0], dtype=np.float32
    )
    elevations = np.zeros((len(distances), len(azimuths)), dtype=np.float32)
    elevations[1:3, 0] = 120.0
    elevations[1:3, 1] = 80.0
    elevations[1:3, -1] = 80.0
    valid = np.ones_like(elevations, dtype=bool)
    visible = np.ones_like(elevations, dtype=bool)
    mesh = {
        "version": 2,
        "azimuths": azimuths,
        "distances": distances,
        "elevations": elevations,
    }

    sun_visibility = overlay._terrain_sun_visibility(
        mesh,
        elevations,
        valid,
        visible,
        distances,
        azimuths,
        sun_alt=10.0,
        sun_az=0.0,
    )

    assert sun_visibility[0, 0] < 0.35
    assert sun_visibility[0, 9] > 0.85


def test_polar_mesh_normals_cover_cells_between_visible_ridges():
    azimuths = np.arange(0.0, 360.0, 10.0, dtype=np.float32)
    distances = np.array([100.0, 200.0, 300.0], dtype=np.float32)
    azimuth_radians = np.deg2rad(azimuths)[None, :]
    elevations = 0.10 * distances[:, None] * np.sin(azimuth_radians)
    valid = np.ones_like(elevations, dtype=bool)

    normal_x, normal_y, normal_z = compute_polar_mesh_normals(
        elevations, valid, distances, azimuths
    )

    east_index = int(np.where(azimuths == 90.0)[0][0])
    assert normal_x[1, east_index] < -0.09
    assert abs(float(normal_y[1, east_index])) < 0.02
    assert normal_z[1, east_index] > 0.99
    assert np.all(np.isfinite(normal_x[valid]))


def test_terrain_span_brush_preserves_lateral_light_variation():
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    brush = overlay._terrain_span_brush(
        np.array([0.0, 50.0, 100.0], dtype=np.float32),
        np.array([0.84, 1.0, 1.10], dtype=np.float32),
        2_000.0,
        0.0,
        QColor(140, 190, 225),
        overlay._sun_vector_enu(30.0, 180.0),
        30.0,
        True,
    )

    gradient = brush.gradient()
    assert gradient is not None
    stops = gradient.stops()
    assert len(stops) >= 3
    assert stops[0][1] != stops[-1][1]
    first_rgb = sum(stops[0][1].getRgb()[:3])
    last_rgb = sum(stops[-1][1].getRgb()[:3])
    assert abs(first_rgb - last_rgb) >= 18


def test_terrain_surface_opacity_flag_is_reversible():
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    sun_vec = overlay._sun_vector_enu(30.0, 180.0)

    opaque = overlay._terrain_surface_color(
        2_000.0,
        0.92,
        0.0,
        QColor(140, 190, 225),
        sun_vec,
        30.0,
        True,
    )
    overlay.set_terrain_surface_opaque(False)
    translucent = overlay._terrain_surface_color(
        2_000.0,
        0.92,
        0.0,
        QColor(140, 190, 225),
        sun_vec,
        30.0,
        True,
    )
    overlay.set_terrain_surface_opaque(True)
    opaque_again = overlay._terrain_surface_color(
        2_000.0,
        0.92,
        0.0,
        QColor(140, 190, 225),
        sun_vec,
        30.0,
        True,
    )

    assert opaque.alpha() == 255
    assert translucent.alpha() < 255
    assert opaque_again.alpha() == 255


def test_horizon_overlay_solar_shading_changes_with_sun_az_offscreen():
    mesh = {
        "version": 2,
        "azimuths": np.array([179.0, 180.0, 181.0], dtype=np.float32),
        "distances": np.array([100.0, 250.0, 600.0], dtype=np.float32),
        "altitudes": np.array(
            [[1.0, 1.0, 1.0], [2.0, 2.0, 2.0], [3.0, 3.0, 3.0]],
            dtype=np.float32,
        ),
        "elevations": np.full((3, 3), 200.0, dtype=np.float32),
        "normal_x": np.ones((3, 3), dtype=np.float32),
        "normal_y": np.zeros((3, 3), dtype=np.float32),
        "normal_z": np.zeros((3, 3), dtype=np.float32),
        "valid": np.ones((3, 3), dtype=bool),
        "visible": np.ones((3, 3), dtype=bool),
    }

    def render(sun_az):
        profile = HorizonProfile(
            azimuths=np.array([], dtype=np.float32),
            bands=[],
            observer_lat=42.58,
            observer_lon=1.0,
            terrain_mesh=mesh,
        )
        overlay = HorizonOverlay(
            horizon_profile_path=None, allow_procedural_fallback=False
        )
        overlay.set_profile(profile, layer_defs=[])
        image = QImage(240, 120, QImage.Format_ARGB32)
        image.fill(QColor(140, 190, 225))
        painter = QPainter(image)

        def project(altitude, azimuth):
            return (
                (float(azimuth) - 178.0) * 40.0,
                82.0 - float(altitude) * 12.0,
            )

        def project_np(altitude, azimuth):
            azimuth = np.asarray(azimuth, dtype=np.float32)
            altitude = np.asarray(altitude, dtype=np.float32)
            return (azimuth - 178.0) * 40.0, 82.0 - altitude * 12.0

        def sky_color(_alt, _azimuth, _sun_alt, _sun_az):
            return QColor(140, 190, 225)

        try:
            overlay.draw(
                painter,
                project,
                240,
                120,
                180.0,
                1.0,
                0.0,
                12.0,
                projection_fn_numpy=project_np,
                sun_alt=35.0,
                sun_az=sun_az,
                sky_color_fn=sky_color,
            )
        finally:
            painter.end()
        return image, overlay._last_surface2d_quads

    def mean_rgb(image):
        values = []
        for x in range(48, 124, 8):
            for y in range(90, 114, 4):
                color = image.pixelColor(x, y)
                values.append(color.red() + color.green() + color.blue())
        return float(np.mean(values))

    east_lit, drawn = render(90.0)
    west_lit, _ = render(270.0)

    assert drawn > 0
    assert mean_rgb(east_lit) > mean_rgb(west_lit) + 3.0


def test_horizon_overlay_can_stay_empty_without_procedural_fallback():
    overlay = HorizonOverlay(
        horizon_profile_path=None, allow_procedural_fallback=False
    )
    assert overlay._layers == []


def test_scope_activation_can_skip_remote_weather_fetch(monkeypatch):
    calls = {"fetch": 0}

    def _fake_fetch(*_args, **_kwargs):
        calls["fetch"] += 1
        return 0.15, 1013.0

    monkeypatch.setattr(
        "TerraLab.widgets.telescope_runtime.fetch_copernicus_aod_pressure",
        _fake_fetch,
    )
    state = {
        "scope_enabled": True,
        "weather_enabled": True,
        "lat": 42.58,
        "lon": 1.0,
        "h_deg": 30.0,
        "focal_mm": 280.0,
        "aperture_mm": 80.0,
        "ocular_mm": 20.0,
    }
    on_telescope_view_enabled(state, allow_remote_fetch=False)
    assert calls["fetch"] == 0
    assert state["hud_metrics"]["k_fallback_used"] is True


def test_scope_prefilter_skips_sync_index_build_while_async_warmup_is_pending():
    renderer = StarsRenderer()
    ra = np.array([10.0, 11.0, 12.0], dtype=np.float32)
    dec = np.array([5.0, 5.5, 6.0], dtype=np.float32)
    mag = np.array([4.0, 5.0, 6.0], dtype=np.float32)
    renderer.begin_scope_index_warmup(ra, dec)

    idx, tile_count, candidate_count = renderer._scope_spatial_prefilter(
        ra_all=ra,
        dec_all=dec,
        mag_all=mag,
        center_ra=11.0,
        center_dec=5.5,
        ra_pad=1.0,
        dec_pad=1.0,
        pre_limit=7.0,
        interaction_active=False,
    )
    assert idx is None
    assert tile_count == 0
    assert candidate_count == 0


def test_scope_prefilter_can_disable_sync_index_build_without_pending():
    renderer = StarsRenderer()
    ra = np.array([10.0, 11.0, 12.0], dtype=np.float32)
    dec = np.array([5.0, 5.5, 6.0], dtype=np.float32)
    mag = np.array([4.0, 5.0, 6.0], dtype=np.float32)

    idx, tile_count, candidate_count = renderer._scope_spatial_prefilter(
        ra_all=ra,
        dec_all=dec,
        mag_all=mag,
        center_ra=11.0,
        center_dec=5.5,
        ra_pad=1.0,
        dec_pad=1.0,
        pre_limit=7.0,
        interaction_active=False,
        allow_sync_index_build=False,
    )
    assert idx is None
    assert tile_count == 0
    assert candidate_count == 0


def test_stars_renderer_cached_altaz_uses_year_when_available():
    renderer = StarsRenderer()
    ra = np.array([56.75], dtype=np.float32)
    dec = np.array([24.12], dtype=np.float32)
    idx = np.array([0], dtype=np.int32)

    class _StateWithYear:
        ut_hour = 22.62
        latitude = 42.51
        longitude = 0.51
        day_of_year = 80
        year_utc = 2026

    alt_deg, az_deg = renderer._cached_altaz(
        ra_all=ra,
        dec_all=dec,
        catalog_idx=idx,
        state=_StateWithYear(),
        interaction_active=False,
    )
    exp_alt, exp_az = radec_to_altaz_numpy(
        ra,
        dec,
        latitude_deg=42.51,
        longitude_deg=0.51,
        ut_hour=22.62,
        day_of_year=80,
        year=2026,
    )
    assert alt_deg is not None and az_deg is not None
    assert float(abs(alt_deg[0] - exp_alt[0])) < 1e-5
    assert float(abs(az_deg[0] - exp_az[0])) < 1e-5


def test_non_scope_prefilter_respects_max_count_when_catalog_sorted():
    renderer = StarsRenderer()
    ra = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
    dec = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
    mag = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)

    idx = renderer._non_scope_prefilter(
        ra_all=ra,
        dec_all=dec,
        mag_all=mag,
        pre_limit=10.0,
        assume_sorted=True,
        max_count=2,
    )
    assert idx is not None
    assert idx.tolist() == [0, 1]


def test_equatorial_to_galactic_conversion_for_galactic_center():
    ra = np.asarray([266.4051], dtype=np.float32)
    dec = np.asarray([-28.936175], dtype=np.float32)
    l_deg, b_deg = MilkyWayOverlay._equatorial_to_galactic_deg(ra, dec)
    galactic_longitude = float(l_deg[0] % 360.0)
    b = float(b_deg[0])
    l_err = min(
        abs(galactic_longitude),
        abs(galactic_longitude - 360.0),
    )
    assert l_err < 0.2
    assert abs(b) < 0.2


def test_galactic_centre_lands_in_southern_sky_on_northern_summer_night():
    alt_deg, az_deg = radec_to_altaz_numpy(
        np.array([266.4051], dtype=np.float32),
        np.array([-28.936175], dtype=np.float32),
        latitude_deg=40.42,
        longitude_deg=-3.70,
        ut_hour=1.95,
        day_of_year=195,
        year=2026,
    )
    assert float(alt_deg[0]) > 0.0
    assert 180.0 <= float(az_deg[0]) <= 260.0


def test_eclipse_lock_uses_real_moon_position_for_overlap_geometry():
    class _Parent:
        auto_bortle_estimate = 3
        scope_k_fallback = 0.2

    class _Canvas:
        def __init__(self):
            self.parent_widget = _Parent()
            self.zoom_level = 1.0
            self.eclipse_lock_mode = True
            self._sf_cache = {
                "data": {
                    "sun": {
                        "alt": 30.0,
                        "az": 100.0,
                        "dist_km": 149_600_000.0,
                        "rad_deg": 0.266,
                    },
                    "moon": {
                        "alt": 30.2,
                        "az": 100.2,
                        "dist_km": 384_400.0,
                        "rad_deg": 0.272,
                        "sep_real": 0.5,
                        "illumination": 0.01,
                        "elongation": 5.0,
                    },
                    "planets": [],
                }
            }
            self.last_moon_draw = None
            self.last_corona_opacity = None

        def width(self):
            return 800

        def height(self):
            return 600

        def scope_mode_enabled(self):
            return False

        def _parent_checkbox_checked(self, key, default=True):
            if key == "chk_planets":
                return False
            return default

        def perceived_disc_scale(self, _alt):
            return 1.0

        def _sun_weather_dim_factor(self):
            return 0.0

        def draw_sun_skyfield(
            self,
            _painter,
            _alt,
            _az,
            _radius,
            _color,
            corona_opacity,
            _pixels_per_deg,
        ):
            self.last_corona_opacity = float(corona_opacity)

        def draw_moon_skyfield(self, _painter, alt, az, *_args, **_kwargs):
            self.last_moon_draw = (float(alt), float(az))

        def draw_planet(self, *_args, **_kwargs):
            return None

    canvas = _Canvas()
    image = QImage(800, 600, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        draw_skyfield_objects(
            canvas,
            painter,
            ut_hour=12.0,
            day_of_year=100,
            ambient_light=1.0,
            mag_limit=6.0,
        )
    finally:
        painter.end()

    assert canvas.last_moon_draw is not None
    alt_draw, az_draw = canvas.last_moon_draw
    assert abs(alt_draw - 30.2) < 1e-3
    assert abs(az_draw - 100.2) < 1e-3
    assert canvas.last_corona_opacity == 0.0
