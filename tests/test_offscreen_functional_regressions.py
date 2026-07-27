from __future__ import annotations

import os
import math
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from PyQt5.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QGuiApplication, QImage, QKeyEvent, QPainter

from TerraLab.runtime.clients import ProcessTerrainClient
from TerraLab.render.qt.context import RenderContext
from TerraLab.render.stars_renderer import StarsRenderer
from TerraLab.runtime.compute_service import (
    _build_search_records,
    _circumpolar_alignment,
    _resolve_search_record,
)
from TerraLab.runtime.offscreen_renderer import (
    OffscreenSceneRenderer,
    daylight_moon_alpha,
    ensure_render_font_available,
    solar_disc_transmission,
)
from TerraLab.runtime.protocol import COMPUTE_REQUEST
from TerraLab.astro.ephemeris_coordinator import EphemerisCoordinator
from TerraLab.scene.camera import Camera
from TerraLab.scene.render_state import RenderState
from TerraLab.terrain.surface import (
    SurfaceCacheKey,
    SurfaceSampleCache,
    _surface_cache_payload,
)
from TerraLab.terrain.surface_store import AtomicNpzStore
from TerraLab.ui.astro_canvas import AstroCanvas
from TerraLab.widgets.spherical_math import altaz_to_ra_dec
from TerraLab.widgets.telescope_scope_mode import TelescopeScopeController


@pytest.fixture(scope="module")
def gui_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication.instance()
    if app is None:
        app = QGuiApplication([])
    return app


def _state(
    *,
    ra=(),
    dec=(),
    mag=(),
    sun_alt=-30.0,
    layers=("stars",),
    camera=None,
) -> RenderState:
    ra_array = np.asarray(ra, dtype=np.float32)
    count = len(ra_array)
    dec_array = np.asarray(dec, dtype=np.float32)
    mag_array = np.asarray(mag, dtype=np.float32)
    return RenderState(
        ut_hour=22.0,
        day_of_year_utc=200,
        year_utc=2026,
        latitude=41.2,
        longitude=0.8,
        altitude_m=0.0,
        camera=camera
        or Camera(
            azimuth_offset=180.0,
            elevation_angle=45.0,
            zoom_level=1.0,
            vertical_offset_ratio=0.0,
        ),
        np_ra=ra_array,
        np_dec=dec_array,
        np_mag=mag_array,
        np_r=np.full(count, 240, dtype=np.uint8),
        np_g=np.full(count, 230, dtype=np.uint8),
        np_b=np.full(count, 210, dtype=np.uint8),
        np_bp_rp=np.full(count, 0.8, dtype=np.float32),
        ephemeris_snapshot={},
        bortle=3,
        mag_limit=8.0,
        sun_alt=float(sun_alt),
        sun_az=180.0,
        layers_enabled=frozenset(layers),
    )


def _earth_snapshot(
    *,
    horizon: bool,
    topography: bool,
    surface: bool,
    terrain_3d: bool,
    light_pollution: bool,
) -> tuple[dict, dict[str, bool]]:
    controls = {
        "chk_enable_horizon": bool(horizon),
        "chk_enable_village": bool(topography),
        "chk_surface_layer": bool(surface),
        "chk_terrain_3d": bool(terrain_3d),
    }
    parent = SimpleNamespace(
        ephemeris_coordinator=None,
        light_pollution_mode="automatic",
        light_pollution_enabled=bool(light_pollution),
        auto_bortle_estimate=8,
        bortle_value=8,
        magnitude_limit=3.8,
        latitude=41.2,
        longitude=0.8,
        weather=None,
    )
    scope = SimpleNamespace(
        center=None,
        current_fov=lambda: (5.0, 5.0),
        shape="circle",
        focal_mm=250.0,
        sensor_key="tiny",
    )
    canvas = SimpleNamespace(
        parent_widget=parent,
        scope_controller=scope,
        measurement_controller=SimpleNamespace(active_tool="none"),
        azimuth_offset=180.0,
        elevation_angle=45.0,
        zoom_level=1.0,
        vertical_offset_ratio=0.0,
        debug_render_metrics=False,
        hud_visible=False,
        trail_start_hour=None,
        _measurement_clear_revision=0,
        _get_current_utc_context=lambda: (22.0, 200, 2026, None),
        _parent_checkbox_checked=lambda name, default: controls.get(
            name, default
        ),
        scope_mode_enabled=lambda: False,
        _camera_interaction_active=lambda **_kwargs: False,
        _scope_motion_active=lambda: False,
        _process_catalog_artifact=lambda: None,
        _process_selection_payload=lambda: {},
        _process_constellation_payload=lambda: {},
    )
    return AstroCanvas._process_scene_snapshot(canvas), controls


@pytest.mark.parametrize(
    (
        "horizon",
        "topography",
        "surface",
        "terrain_3d",
        "expected",
    ),
    [
        (False, False, False, False, (False, False, False, False)),
        (False, True, True, True, (False, False, False, False)),
        (True, False, True, True, (True, False, False, False)),
        (True, True, False, True, (True, True, False, True)),
        (True, True, True, False, (True, True, True, False)),
    ],
)
def test_horizon_is_the_master_for_effective_earth_visibility(
    horizon,
    topography,
    surface,
    terrain_3d,
    expected,
) -> None:
    snapshot, controls = _earth_snapshot(
        horizon=horizon,
        topography=topography,
        surface=surface,
        terrain_3d=terrain_3d,
        light_pollution=True,
    )
    terrain = snapshot["terrain"]

    assert (
        terrain["horizon_enabled"],
        terrain["topography_enabled"],
        terrain["surface_enabled"],
        terrain["terrain_3d_enabled"],
    ) == expected
    assert ("terrain" in snapshot["layers"]) is bool(horizon)
    assert snapshot["light_pollution_enabled"] is bool(horizon)
    assert controls == {
        "chk_enable_horizon": bool(horizon),
        "chk_enable_village": bool(topography),
        "chk_surface_layer": bool(surface),
        "chk_terrain_3d": bool(terrain_3d),
    }
    if not horizon:
        assert snapshot["bortle"] == 1
        assert snapshot["magnitude_limit"] == pytest.approx(7.6)


def test_eclipse_transmission_uses_real_disc_overlap() -> None:
    assert solar_disc_transmission(1.0, 0.2666, 0.2725) == pytest.approx(
        1.0
    )
    assert solar_disc_transmission(0.0, 0.2666, 0.2725) == pytest.approx(
        0.0
    )
    partial = solar_disc_transmission(0.25, 0.2666, 0.2725)
    assert 0.0 < partial < 1.0


def test_daylight_moon_visibility_depends_on_phase_and_separation() -> None:
    assert (
        daylight_moon_alpha(
            illumination=0.9,
            elongation_deg=140.0,
            moon_alt_deg=35.0,
            sun_alt_deg=25.0,
        )
        > 0.0
    )
    assert (
        daylight_moon_alpha(
            illumination=0.001,
            elongation_deg=2.0,
            moon_alt_deg=35.0,
            sun_alt_deg=25.0,
        )
        == 0.0
    )
    assert (
        daylight_moon_alpha(
            illumination=0.001,
            elongation_deg=0.1,
            moon_alt_deg=35.0,
            sun_alt_deg=25.0,
            eclipsing=True,
        )
        == 1.0
    )


def test_circumpolar_alignment_resolves_current_polaris_position() -> None:
    aligned = _circumpolar_alignment(
        {
            "ut_hour": 22.0,
            "day_of_year_utc": 200,
            "year_utc": 2026,
            "latitude": 41.2,
            "longitude": 0.8,
        }
    )
    assert aligned["name"] == "Polaris"
    assert abs(float(aligned["alt"]) - 41.2) < 1.5
    north_error = abs(
        ((float(aligned["az"]) + 180.0) % 360.0) - 180.0
    )
    assert north_error < 1.5


def test_planets_are_registered_with_their_names(gui_app) -> None:
    renderer = OffscreenSceneRenderer()
    image = QImage(480, 270, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        renderer._draw_planets(
            painter,
            image.width(),
            image.height(),
            _state(layers=("planets",)),
            2.0,
            [
                {
                    "name": "Jupiter",
                    "key": "jupiter barycenter",
                    "alt": 45.0,
                    "az": 180.0,
                    "mag": -2.5,
                }
            ],
        )
    finally:
        painter.end()
        renderer.close()
    assert [item["name"] for item in renderer._visible_sky_objects] == [
        "Jupiter"
    ]
    assert np.any(
        np.frombuffer(
            image.constBits().asstring(image.sizeInBytes()),
            dtype=np.uint8,
        )
    )


def test_offscreen_backend_registers_a_font_and_rasterizes_planet_label(
    gui_app,
) -> None:
    assert ensure_render_font_available()
    renderer = OffscreenSceneRenderer()
    image = QImage(480, 270, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        renderer._draw_planets(
            painter,
            image.width(),
            image.height(),
            _state(layers=("planets",)),
            2.0,
            [
                {
                    "name": "Jupiter",
                    "key": "jupiter barycenter",
                    "alt": 45.0,
                    "az": 180.0,
                    "mag": -2.5,
                }
            ],
        )
    finally:
        painter.end()
        renderer.close()
    pixels = np.frombuffer(
        image.constBits().asstring(image.sizeInBytes()),
        dtype=np.uint8,
    ).reshape((image.height(), image.width(), 4))
    # The body is centred at x=240; this region contains only its label.
    label = pixels[115:150, 250:350, :3]
    assert np.count_nonzero(np.max(label, axis=2) > 180) > 20


def test_ngc_markers_keep_catalog_shape_name_and_pick_extent(
    gui_app, monkeypatch
) -> None:
    state = _state(layers=("deep_sky",))
    ra, dec = altaz_to_ra_dec(
        45.0,
        180.0,
        state.ut_hour,
        state.day_of_year_utc,
        state.latitude,
        state.longitude,
        year=state.year_utc,
    )
    catalog = np.asarray(
        [
            (
                ra,
                dec,
                2.0,
                0.5,
                37.0,
                5.0,
                "G",
                "Andromeda",
            )
        ],
        dtype=[
            ("ra", "<f4"),
            ("dec", "<f4"),
            ("maj", "<f4"),
            ("min", "<f4"),
            ("pa", "<f4"),
            ("mag", "<f4"),
            ("kind", "<U16"),
            ("name", "<U80"),
        ],
    )
    renderer = OffscreenSceneRenderer()
    monkeypatch.setattr(renderer.ngc, "load", lambda _artifact: catalog)
    image = QImage(800, 450, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        renderer._draw_ngc(
            painter,
            image.width(),
            image.height(),
            state,
            {"ngc": {}},
        )
    finally:
        painter.end()
        renderer.close()
    assert len(renderer._visible_ngc) == 1
    marker = renderer._visible_ngc[0]
    assert marker["name"] == "Andromeda"
    assert marker["pick_radius"] > 10.0


def test_sun_and_moon_use_larger_visual_discs_but_physical_overlap(
    gui_app,
) -> None:
    renderer = OffscreenSceneRenderer()
    state = replace(
        _state(sun_alt=45.0, layers=("sun_moon",)),
        sun_az=180.0,
        ephemeris_snapshot={
            "sun": {"alt": 45.0, "az": 180.0, "rad_deg": 0.2666},
            "moon": {
                "alt": 45.0,
                "az": 180.0,
                "rad_deg": 0.2725,
                "illumination": 0.0,
                "sep_real": 0.0,
            },
        },
        extras={"eclipse_factor": 0.0},
        camera=Camera(
            azimuth_offset=180.0,
            elevation_angle=45.0,
            zoom_level=20.0,
            vertical_offset_ratio=0.0,
        ),
    )
    image = QImage(480, 270, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    scale = image.height() * 0.5 * state.camera.zoom_level / 90.0
    try:
        renderer._draw_ephemeris(
            painter, image.width(), image.height(), state
        )
    finally:
        painter.end()
    radii = {
        item["type"]: float(item["pick_radius"]) - 8.0
        for item in renderer._visible_sky_objects
    }
    renderer.close()
    assert radii["sun"] >= 0.2666 * scale * 3.99
    assert radii["moon"] >= 0.2725 * scale * 3.99
    assert radii["sun"] >= 18.0
    assert radii["moon"] >= 18.0
    assert OffscreenSceneRenderer._celestial_disc_visual_factor(
        10.0
    ) == 4.0
    assert OffscreenSceneRenderer._celestial_disc_visual_factor(
        120.0
    ) == 1.0
    assert solar_disc_transmission(0.0, 0.2666, 0.2725) == 0.0


def test_stale_ephemeris_keeps_one_coherent_scientific_snapshot(
    gui_app,
) -> None:
    renderer = OffscreenSceneRenderer()
    image = QImage(480, 270, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        renderer.render(
            painter,
            image.width(),
            image.height(),
            {
                "camera": {
                    "azimuth": 180.0,
                    "elevation": 45.0,
                    "zoom": 10.0,
                    "vertical_ratio": 0.0,
                },
                # Deliberately does not match the scientific snapshot below.
                "year_utc": 2026,
                "day_of_year_utc": 0,
                "ut_hour": 12.0,
                "latitude": 41.2,
                "longitude": 0.8,
                "layers": ["sun_moon"],
                "hud_visible": False,
                "ephemeris": {
                    "timestamp_utc": "2026-08-12T18:29:52+00:00",
                    "sun": {
                        "alt": 45.0,
                        "az": 180.0,
                        "rad_deg": 0.26296,
                    },
                    "moon": {
                        "alt": 45.0,
                        "az": 180.0,
                        "rad_deg": 0.27159,
                        "sep_real": 0.00637,
                        "illumination": 0.0,
                    },
                    "planets": [],
                },
            },
        )
    finally:
        painter.end()
    visible = {
        item["type"]: item
        for item in renderer._visible_sky_objects
    }
    renderer.close()
    assert {"sun", "moon"}.issubset(visible)
    assert visible["sun"]["sx"] == pytest.approx(
        visible["moon"]["sx"], abs=0.01
    )
    assert visible["sun"]["sy"] == pytest.approx(
        visible["moon"]["sy"], abs=0.01
    )


def test_torroja_2026_eclipse_progresses_through_all_phases(
    gui_app,
) -> None:
    coordinator = EphemerisCoordinator()
    if coordinator._eph is None:
        coordinator.shutdown()
        pytest.skip("Local DE421 ephemeris is unavailable")

    latitude = 41.0 + 12.0 / 60.0 + 47.8 / 3600.0
    longitude = 48.0 / 60.0 + 37.6 / 3600.0
    samples = (
        ("17:35:19", 0.99, 1.0),
        ("17:36:00", 0.998, 1.0),
        ("18:00:00", 0.60, 0.72),
        ("18:20:00", 0.15, 0.27),
        ("18:29:19", 0.0, 0.001),
        ("18:29:52", 0.0, 0.000001),
        ("18:30:25", 0.0, 0.001),
        ("18:45:00", 0.25, 0.43),
        ("19:00:00", 0.60, 0.75),
        ("19:04:00", 0.70, 0.82),
        ("19:15:00", 0.90, 0.98),
        ("19:20:00", 0.99, 1.0),
    )
    transmissions = []
    visual_transmissions = []
    try:
        for utc_clock, lower, upper in samples:
            instant = datetime.fromisoformat(
                f"2026-08-12T{utc_clock}+00:00"
            )
            day = (
                instant.date()
                - datetime(2026, 1, 1, tzinfo=timezone.utc).date()
            ).days
            hour = (
                instant.hour
                + instant.minute / 60.0
                + instant.second / 3600.0
            )
            snapshot = coordinator._compute_snapshot(
                year_utc=2026,
                day_of_year_utc=day,
                ut_hour=hour,
                latitude=latitude,
                longitude=longitude,
            )
            sun = snapshot["sun"]
            moon = snapshot["moon"]
            transmission = solar_disc_transmission(
                moon["sep_real"],
                sun["rad_deg"],
                moon["rad_deg"],
            )
            transmissions.append(transmission)
            assert lower <= transmission <= upper

            camera = Camera(
                azimuth_offset=float(sun["az"]),
                elevation_angle=float(sun["alt"]),
                zoom_level=100.0 / 8.8,
                vertical_offset_ratio=0.0,
            )
            state = replace(
                _state(
                    sun_alt=float(sun["alt"]),
                    layers=("sun_moon",),
                    camera=camera,
                ),
                ut_hour=hour,
                day_of_year_utc=day,
                year_utc=2026,
                latitude=latitude,
                longitude=longitude,
                sun_az=float(sun["az"]),
                ephemeris_snapshot=snapshot,
            )
            image = QImage(
                960,
                540,
                QImage.Format_ARGB32_Premultiplied,
            )
            image.fill(QColor(0, 0, 0))
            renderer = OffscreenSceneRenderer()
            painter = QPainter(image)
            try:
                renderer._draw_ephemeris(
                    painter,
                    image.width(),
                    image.height(),
                    state,
                )
            finally:
                painter.end()
            visible = {
                item["type"]: item
                for item in renderer._visible_sky_objects
            }
            renderer.close()
            if transmission >= 1.0:
                assert "sun" in visible
                assert "moon" not in visible
                visual_transmissions.append(1.0)
                continue
            assert {"sun", "moon"}.issubset(visible)
            visual_separation = math.hypot(
                visible["sun"]["sx"] - visible["moon"]["sx"],
                visible["sun"]["sy"] - visible["moon"]["sy"],
            )
            visual_transmission = solar_disc_transmission(
                visual_separation,
                visible["sun"]["pick_radius"] - 8.0,
                visible["moon"]["pick_radius"] - 8.0,
            )
            visual_transmissions.append(visual_transmission)
            assert visual_transmission == pytest.approx(
                transmission,
                # The visible discs now include differential atmospheric
                # refraction, so their circular pick bounds are only an
                # approximation of the rendered ellipses near the horizon.
                abs=0.015,
            )
    finally:
        coordinator.shutdown()

    assert transmissions[:6] == sorted(
        transmissions[:6], reverse=True
    )
    assert transmissions[6:] == sorted(transmissions[6:])
    assert visual_transmissions == pytest.approx(
        transmissions,
        abs=0.015,
    )


def test_torroja_local_eclipse_time_is_converted_to_utc() -> None:
    local_hour = 19.0 + 35.0 / 60.0 + 19.0 / 3600.0
    parent = SimpleNamespace(
        observer_timezone="Europe/Madrid",
        manual_year=2026,
        manual_day=(
            datetime(2026, 8, 12).date()
            - datetime(2026, 1, 1).date()
        ).days,
        get_current_hour=lambda: local_hour,
    )
    canvas = SimpleNamespace(
        parent_widget=parent,
        _resolve_observer_tzinfo=lambda: ZoneInfo("Europe/Madrid"),
    )

    hour, day, year, utc = AstroCanvas._get_current_utc_context(
        canvas
    )

    assert year == 2026
    assert day == parent.manual_day
    assert hour == pytest.approx(
        17.0 + 35.0 / 60.0 + 19.0 / 3600.0
    )
    assert utc.isoformat().startswith("2026-08-12T17:35:19")


def test_totality_corona_is_structured_and_white_blue(gui_app) -> None:
    renderer = OffscreenSceneRenderer()
    state = replace(
        _state(sun_alt=45.0, layers=("sun_moon",)),
        sun_az=180.0,
        ephemeris_snapshot={
            "sun": {"alt": 45.0, "az": 180.0, "rad_deg": 0.2666},
            "moon": {
                "alt": 45.0,
                "az": 180.0,
                "rad_deg": 0.2725,
                "sep_real": 0.0,
            },
        },
        extras={"eclipse_factor": 0.0},
    )
    image = QImage(400, 400, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        renderer._draw_sun_body(
            painter,
            image.width(),
            image.height(),
            state,
            45.0,
            180.0,
            36.0,
            state.ephemeris_snapshot["sun"],
        )
    finally:
        painter.end()
        renderer.close()
    pixels = np.frombuffer(
        image.constBits().asstring(image.sizeInBytes()),
        dtype=np.uint8,
    ).reshape((400, 400, 4))
    yy, xx = np.ogrid[:400, :400]
    distance = np.hypot(xx - 200.0, yy - 200.0)
    corona = pixels[(distance > 45.0) & (distance < 180.0), :3]
    visible = corona[np.max(corona, axis=1) > 8]
    assert len(visible) > 1000
    # ARGB32 is BGRA in little-endian memory: channel 0 is blue.
    assert float(np.mean(visible[:, 0])) > float(
        np.mean(visible[:, 2])
    )


def test_circumpolar_trails_produce_a_nonempty_cached_layer(gui_app) -> None:
    ra, dec = altaz_to_ra_dec(
        45.0,
        180.0,
        22.0,
        200,
        41.2,
        0.8,
        year=2026,
    )
    state = _state(ra=(ra,), dec=(dec,), mag=(1.0,))
    renderer = OffscreenSceneRenderer()
    image = QImage(640, 360, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        renderer._draw_trails(
            painter,
            image.width(),
            image.height(),
            state,
            {
                "trails": {
                    "enabled": True,
                    "start_hour": 20.0,
                }
            },
        )
    finally:
        painter.end()
    trail_image = renderer._trail_image
    renderer.close()
    assert trail_image is not None
    data = np.frombuffer(
        trail_image.constBits().asstring(trail_image.sizeInBytes()),
        dtype=np.uint8,
    )
    assert np.any(data[3::4])


def test_circumpolar_includes_faint_visible_stars_and_suppresses_dots(
    gui_app,
) -> None:
    ra, dec = altaz_to_ra_dec(
        45.0,
        180.0,
        22.0,
        200,
        41.2,
        0.8,
        year=2026,
    )
    state = replace(
        _state(ra=(ra,), dec=(dec,), mag=(6.0,)),
        extras={"stars_enabled": True, "suppress_star_points": True},
    )
    renderer = OffscreenSceneRenderer()
    trail_image = QImage(
        640, 360, QImage.Format_ARGB32_Premultiplied
    )
    trail_image.fill(0)
    painter = QPainter(trail_image)
    renderer._draw_trails(
        painter,
        trail_image.width(),
        trail_image.height(),
        state,
        {"trails": {"enabled": True, "start_hour": 20.0}},
    )
    painter.end()
    trail_pixels = np.frombuffer(
        trail_image.constBits().asstring(trail_image.sizeInBytes()),
        dtype=np.uint8,
    )
    assert np.any(trail_pixels[3::4])

    points_image = QImage(
        640, 360, QImage.Format_ARGB32_Premultiplied
    )
    points_image.fill(0)
    points_painter = QPainter(points_image)
    result = StarsRenderer().render(
        RenderContext(
            painter=points_painter,
            width=640,
            height=360,
            diagnostics=None,
        ),
        state,
    )
    points_painter.end()
    renderer.close()
    assert len(result.visible_indices) == 1
    point_pixels = np.frombuffer(
        points_image.constBits().asstring(points_image.sizeInBytes()),
        dtype=np.uint8,
    )
    assert not np.any(point_pixels)


def test_moon_phase_mask_grows_monotonically(gui_app) -> None:
    counts = []
    for illumination in (0.1, 0.5, 0.9):
        image = QImage(100, 100, QImage.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        painter.translate(50.0, 50.0)
        painter.fillPath(
            OffscreenSceneRenderer._moon_lit_path(30.0, illumination),
            QColor(255, 255, 255, 255),
        )
        painter.end()
        pixels = np.frombuffer(
            image.constBits().asstring(image.sizeInBytes()),
            dtype=np.uint8,
        ).reshape((100, 100, 4))
        counts.append(int(np.count_nonzero(pixels[:, :, 3])))
    assert counts[0] < counts[1] < counts[2]


def test_surface_refresh_is_dispatched_to_compute(gui_app) -> None:
    class RuntimeStub(QObject):
        message_received = pyqtSignal(str, object)
        worker_ready = pyqtSignal(str)

        def __init__(self) -> None:
            super().__init__()
            self.sent = []

        def send(self, role, message):
            self.sent.append((role, message))
            return True

    runtime = RuntimeStub()
    client = ProcessTerrainClient(runtime)
    client._profile_path = "C:/immutable/profile.npz"
    client.request_surface_refresh(
        visible_radius_m=12_000.0,
        surface_mode="orthophoto",
    )
    role, request = runtime.sent[-1]
    assert role == "compute"
    assert request.kind == COMPUTE_REQUEST
    assert request.request_id == "terrain-surface"
    assert request.payload["operation"] == "terrain_surface"
    assert request.payload["profile_path"].endswith("profile.npz")
    client.shutdown()


def test_render_can_load_published_surface_artifact(tmp_path) -> None:
    key = SurfaceCacheKey(
        geometry_id="geometry-1",
        source_fingerprints=("source-1",),
        sampling_policy="complete",
    )
    original = SurfaceSampleCache(
        key=key,
        source_ids=("ortho",),
        geometry_crs="EPSG:4326",
        observer_rgba=np.asarray([10, 20, 30, 255], dtype=np.uint8),
        observer_valid=True,
        observer_source_index=0,
    )
    metadata, arrays = _surface_cache_payload(original)
    store = AtomicNpzStore(tmp_path, budget_bytes=10_000_000)
    artifact = store.save("published/surface", metadata, arrays)

    loaded = OffscreenSceneRenderer._load_surface_artifact(str(artifact))

    assert loaded.cache_id == original.cache_id
    assert loaded.observer_valid is True
    np.testing.assert_array_equal(
        loaded.observer_rgba, original.observer_rgba
    )


def test_render_receives_surface_visual_style_from_snapshot(gui_app) -> None:
    class TerrainStub:
        def __init__(self) -> None:
            self.profile = None
            self.styles = []
            self.draw_calls = 0

        def set_surface_visual_style(self, style: str) -> None:
            self.styles.append(style)

        def draw(self, *_args, **_kwargs) -> None:
            self.draw_calls += 1

    renderer = OffscreenSceneRenderer()
    terrain = TerrainStub()
    renderer.terrain = terrain
    renderer._terrain_path = ""
    renderer._terrain_surface_path = ""
    image = QImage(320, 180, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    try:
        renderer._draw_terrain(
            painter,
            image.width(),
            image.height(),
            _state(layers=("terrain",)),
            {
                "terrain": {
                    "profile_path": "",
                    "surface_path": "",
                    "surface_visual_style": "vibrant",
                }
            },
        )
        renderer._draw_terrain(
            painter,
            image.width(),
            image.height(),
            _state(layers=("terrain",)),
            {
                "terrain": {
                    "profile_path": "",
                    "surface_path": "",
                    "surface_visual_style": "original",
                }
            },
        )
    finally:
        painter.end()
        renderer.terrain = None
        renderer.close()
    assert terrain.styles == ["vibrant", "original"]
    assert terrain.draw_calls == 2


def test_render_rejects_incoherent_terrain_when_horizon_is_disabled(
    gui_app,
) -> None:
    class TerrainStub:
        profile = None

        def __init__(self) -> None:
            self.styles = []
            self.draw_calls = 0

        def set_surface_visual_style(self, style: str) -> None:
            self.styles.append(style)

        def draw(self, *_args, **_kwargs) -> None:
            self.draw_calls += 1

    renderer = OffscreenSceneRenderer()
    terrain = TerrainStub()
    renderer.terrain = terrain
    renderer._terrain_path = ""
    renderer._terrain_surface_path = ""
    image = QImage(320, 180, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    try:
        renderer._draw_terrain(
            painter,
            image.width(),
            image.height(),
            _state(layers=("terrain",)),
            {
                "light_pollution_enabled": True,
                "terrain": {
                    "profile_path": "",
                    "surface_path": "",
                    "horizon_enabled": False,
                    "topography_enabled": True,
                    "surface_enabled": True,
                    "terrain_3d_enabled": True,
                    "surface_visual_style": "vibrant",
                },
            },
        )
    finally:
        painter.end()
        renderer.terrain = None
        renderer.close()

    assert terrain.styles == []
    assert terrain.draw_calls == 0


def test_render_neutralizes_stale_earth_state_when_master_is_disabled(
    gui_app,
) -> None:
    renderer = OffscreenSceneRenderer()
    image = QImage(320, 180, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    try:
        renderer.render(
            painter,
            image.width(),
            image.height(),
            {
                "camera": {
                    "azimuth": 180.0,
                    "elevation": 45.0,
                    "zoom": 1.0,
                    "vertical_ratio": 0.0,
                },
                "year_utc": 2026,
                "day_of_year_utc": 0,
                "ut_hour": 0.0,
                "latitude": 0.0,
                "longitude": 0.0,
                "layers": ["terrain"],
                "hud_visible": False,
                "bortle": 9,
                "magnitude_limit": 3.6,
                "light_pollution_enabled": True,
                "terrain": {
                    "horizon_enabled": False,
                    "topography_enabled": True,
                    "surface_enabled": True,
                    "terrain_3d_enabled": True,
                },
            },
        )
        state = renderer._last_pick_state
    finally:
        painter.end()
        renderer.close()

    assert state is not None
    assert "terrain" not in state.layers_enabled
    assert state.bortle == 1
    assert state.mag_limit == pytest.approx(7.6)


def test_search_records_include_all_catalogue_families(tmp_path) -> None:
    gaia_path = tmp_path / "gaia.npy"
    catalog = np.asarray(
        [(123456789, 12.5, -8.25, 2.0)],
        dtype=[
            ("source_id", "<i8"),
            ("ra", "<f4"),
            ("dec", "<f4"),
            ("mag", "<f4"),
        ],
    )
    np.save(gaia_path, catalog, allow_pickle=False)
    root = os.path.dirname(os.path.dirname(__file__))
    result = _build_search_records(
        {
            "named_stars_path": os.path.join(
                root,
                "TerraLab",
                "data",
                "stars",
                "no_gaia_stars.json",
            ),
            "ngc_paths": [
                os.path.join(
                    root,
                    "TerraLab",
                    "data",
                    "sky",
                    "openngc_catalog.csv",
                )
            ],
            "gaia_catalog_path": str(gaia_path),
            "gaia_suggestion_limit": 50,
        }
    )
    aliases = {
        str(alias)
        for record in result["records"]
        for alias in record.get("aliases", ())
    }
    assert {"Sol", "Lluna", "Jupiter"}.issubset(aliases)
    assert "Gaia DR3 123456789" in aliases
    assert any(alias.startswith("M") for alias in aliases)
    assert any(alias.startswith("NGC") for alias in aliases)


def test_selection_marker_has_a_real_pulse(gui_app, monkeypatch) -> None:
    renderer = OffscreenSceneRenderer()
    state = _state()
    selected = {
        "selection": {
            "kind": "sky",
            "type": "planet",
            "key": "jupiter barycenter",
            "name": "Jupiter",
            "alt": 45.0,
            "az": 180.0,
        }
    }
    renderer._visible_sky_objects = [
        {
            "type": "planet",
            "key": "jupiter barycenter",
            "name": "Jupiter",
            "alt": 45.0,
            "az": 180.0,
            "sx": 160.0,
            "sy": 90.0,
            "pick_radius": 12.0,
        }
    ]

    def draw_at(moment: float) -> np.ndarray:
        monkeypatch.setattr(
            "TerraLab.runtime.offscreen_renderer.time.monotonic",
            lambda: moment,
        )
        image = QImage(
            320, 180, QImage.Format_ARGB32_Premultiplied
        )
        image.fill(0)
        painter = QPainter(image)
        renderer._draw_selection(
            painter, 320, 180, state, selected
        )
        painter.end()
        return np.frombuffer(
            image.constBits().asstring(image.sizeInBytes()),
            dtype=np.uint8,
        ).copy()

    first = draw_at(0.0)
    second = draw_at(1.15 / 4.0)
    renderer.close()
    assert not np.array_equal(first, second)


def test_planet_selection_uses_the_requested_body_key(
    gui_app, monkeypatch
) -> None:
    renderer = OffscreenSceneRenderer()
    renderer._visible_sky_objects = [
        {
            "type": "planet",
            "key": "mars barycenter",
            "sx": 60.0,
            "sy": 90.0,
            "pick_radius": 12.0,
        },
        {
            "type": "planet",
            "key": "jupiter barycenter",
            "sx": 260.0,
            "sy": 90.0,
            "pick_radius": 12.0,
        },
    ]
    monkeypatch.setattr(
        "TerraLab.runtime.offscreen_renderer.time.monotonic",
        lambda: 0.0,
    )
    image = QImage(320, 180, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    renderer._draw_selection(
        painter,
        image.width(),
        image.height(),
        _state(),
        {
            "selection": {
                "kind": "sky",
                "type": "planet",
                "key": "jupiter",
                "name": "Jupiter",
                "alt": 45.0,
                "az": 180.0,
            }
        },
    )
    painter.end()
    renderer.close()
    pixels = np.frombuffer(
        image.constBits().asstring(image.sizeInBytes()),
        dtype=np.uint8,
    ).reshape((180, 320, 4))
    assert np.any(pixels[72:109, 242:279, 3])
    assert not np.any(pixels[72:109, 42:79, 3])


def test_search_resolution_returns_the_canonical_planet_key(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "TerraLab.runtime.compute_service._compute_ephemeris",
        lambda _payload, _coordinator: {
            "planets": [
                {
                    "key": "jupiter barycenter",
                    "name": "Jupiter",
                    "alt": 31.0,
                    "az": 205.0,
                }
            ]
        },
    )
    selected = _resolve_search_record(
        {
            "record": {
                "kind": "planet",
                "key": "jupiter",
                "name": "Jupiter",
            },
            "year_utc": 2026,
            "day_of_year_utc": 200,
            "ut_hour": 22.0,
            "latitude": 41.2,
            "longitude": 0.8,
            "client_token": "search-7",
            "view_revision": 3,
        },
        None,
    )
    assert selected["key"] == "jupiter barycenter"
    assert selected["type"] == "planet"
    assert selected["_client_token"] == "search-7"
    assert selected["_view_revision"] == 3


def test_manual_scope_nudge_is_not_overwritten_by_target_tracking() -> None:
    class TimerStub:
        def __init__(self) -> None:
            self.started = False

        def isActive(self) -> bool:
            return self.started

        def start(self) -> None:
            self.started = True

    class CanvasStub:
        def __init__(self) -> None:
            self.scope_controller = TelescopeScopeController()
            self.scope_controller.activate()
            self.scope_controller.set_center((30.0, 120.0), confirmed=True)
            self.scope_camera_lock_to_target = True
            self.scope_reticle_lock_to_target = True
            self._scope_pressed_keys = set()
            self._scope_move_timer = TimerStub()
            self.updated = False

        def measurement_tool_active(self) -> bool:
            return False

        def drawing_mode_enabled(self) -> bool:
            return False

        def scope_mode_enabled(self) -> bool:
            return True

        def _mark_scope_interaction(self, _seconds: float) -> None:
            return None

        def update(self) -> None:
            self.updated = True

    canvas = CanvasStub()
    event = QKeyEvent(
        QEvent.KeyPress,
        Qt.Key_Right,
        Qt.NoModifier,
    )

    AstroCanvas.keyPressEvent(canvas, event)

    assert canvas.scope_camera_lock_to_target is True
    assert canvas.scope_reticle_lock_to_target is False
    assert canvas.scope_controller.center[1] < 120.0
    assert canvas._scope_move_timer.started is True
    assert canvas.updated is True


def test_scope_slow_and_fast_controls_keep_distinct_physical_rates() -> None:
    controller = TelescopeScopeController()
    controller.set_speed_mode(controller.SPEED_SLOW)
    slow_step = controller.short_step_deg()
    slow_rate = controller.hold_rate_deg_per_s()
    controller.set_speed_mode(controller.SPEED_FAST)

    assert controller.short_step_deg() > slow_step * 10.0
    assert controller.hold_rate_deg_per_s() > slow_rate * 10.0


def test_context_goto_enters_scope_and_centers_camera_and_reticle() -> None:
    class ParentStub:
        _scope_ui_manager = None

        def __init__(self) -> None:
            self.tracking_requests = 0

        def request_scope_tracking_update(self) -> None:
            self.tracking_requests += 1

    class CanvasStub:
        def __init__(self) -> None:
            self.parent_widget = ParentStub()
            self.scope_controller = TelescopeScopeController()
            self.scope_camera_lock_to_target = False
            self.scope_reticle_lock_to_target = False
            self.zoom_level = 1.0
            self.dragging = True
            self.enabled = False
            self.selected_target = None
            self.updated = False

        def _set_selected_target(self, target) -> None:
            self.selected_target = target

        def scope_mode_enabled(self) -> bool:
            return self.enabled

        def set_scope_enabled(self, enabled: bool) -> None:
            self.enabled = bool(enabled)
            if enabled:
                self.scope_controller.activate()

        def setFocus(self, _reason) -> None:
            return None

        def update(self) -> None:
            self.updated = True

    canvas = CanvasStub()
    target = {
        "kind": "star",
        "name": "Polaris",
        "alt": 41.2,
        "az": 0.4,
        "ra": 37.95456067,
        "dec": 89.26410897,
    }

    AstroCanvas._goto_process_target(canvas, target)

    assert canvas.enabled is True
    assert canvas.scope_camera_lock_to_target is True
    assert canvas.scope_reticle_lock_to_target is True
    assert canvas.scope_controller.center == pytest.approx((41.2, 0.4))
    assert canvas.azimuth_offset == pytest.approx(0.4)
    assert canvas.elevation_angle == pytest.approx(41.2)
    assert canvas.parent_widget.tracking_requests == 1
    assert canvas.dragging is False
    assert canvas.updated is True
