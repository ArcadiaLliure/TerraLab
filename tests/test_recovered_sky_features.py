from __future__ import annotations

import math
import os
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt5.QtGui import QColor, QGuiApplication, QImage, QPainter

from TerraLab.astro.ephemeris_coordinator import (
    planet_apparent_magnitude,
)
from TerraLab.runtime.offscreen_renderer import (
    OffscreenSceneRenderer,
    standard_refracted_altitude_deg,
    standard_refraction_vertical_scale,
)
from TerraLab.scene.camera import Camera
from TerraLab.scene.render_state import RenderState


@pytest.fixture(scope="module")
def gui_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QGuiApplication.instance() or QGuiApplication([])


def _state(
    *,
    sun_alt=-30.0,
    layers=("stars",),
    camera=None,
) -> RenderState:
    empty = np.empty(0, dtype=np.float32)
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
        np_ra=empty,
        np_dec=empty,
        np_mag=empty,
        np_r=np.empty(0, dtype=np.uint8),
        np_g=np.empty(0, dtype=np.uint8),
        np_b=np.empty(0, dtype=np.uint8),
        np_bp_rp=empty,
        ephemeris_snapshot={},
        bortle=3,
        mag_limit=8.0,
        sun_alt=float(sun_alt),
        sun_az=180.0,
        layers_enabled=frozenset(layers),
    )


def test_standard_refraction_lifts_and_flattens_horizon_discs() -> None:
    assert standard_refracted_altitude_deg(0.0) == pytest.approx(
        0.48194, abs=0.0001
    )
    assert standard_refracted_altitude_deg(45.0) == pytest.approx(
        45.01656, abs=0.0001
    )
    assert standard_refracted_altitude_deg(-2.0) == -2.0
    assert standard_refraction_vertical_scale(0.0) == pytest.approx(
        0.85547, abs=0.001
    )
    assert standard_refraction_vertical_scale(45.0) > 0.999


def test_refracted_disc_geometry_keeps_horizontal_radius_and_flattens_vertical() -> None:
    geometry = OffscreenSceneRenderer._refracted_disc_geometry(
        0.0,
        180.0,
        0.2666,
        800,
        450,
        Camera(
            azimuth_offset=180.0,
            elevation_angle=0.0,
            zoom_level=10.0,
            vertical_offset_ratio=0.0,
        ),
        fallback_px=6.0,
    )
    assert geometry is not None
    assert geometry.apparent_altitude_deg > 0.45
    assert geometry.radius_y < geometry.radius_x
    assert geometry.radius_y / geometry.radius_x == pytest.approx(
        standard_refraction_vertical_scale(0.0)
    )


def test_planet_photometry_uses_distance_and_is_published_to_pick_data(
    gui_app,
) -> None:
    expected = planet_apparent_magnitude("Jupiter", 5.0)
    assert expected == pytest.approx(-2.30515, abs=0.0001)

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
                    "distance_au": 5.0,
                }
            ],
        )
    finally:
        painter.end()
    assert renderer._visible_sky_objects[0]["mag"] == pytest.approx(
        expected
    )
    renderer.close()


def test_city_light_dome_is_rendered_from_horizon_profile(gui_app) -> None:
    renderer = OffscreenSceneRenderer()
    profile = SimpleNamespace(
        light_domes=np.asarray([250.0], dtype=np.float32),
        azimuths=np.asarray([180.0], dtype=np.float32),
        bands=[
            {
                "angles": np.asarray(
                    [math.radians(2.0)], dtype=np.float32
                )
            }
        ],
    )
    state = _state(
        sun_alt=-25.0,
        layers=("terrain",),
        camera=Camera(
            azimuth_offset=180.0,
            elevation_angle=8.0,
            zoom_level=3.0,
            vertical_offset_ratio=0.0,
        ),
    )
    image = QImage(640, 360, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 255))
    painter = QPainter(image)
    try:
        renderer._dome_count = 0
        renderer._draw_single_city_dome(
            painter,
            profile,
            0,
            1000.0,
            1.0,
            image.width(),
            image.height(),
            state,
        )
    finally:
        painter.end()
    pixels = np.frombuffer(
        image.constBits().asstring(image.sizeInBytes()),
        dtype=np.uint8,
    ).reshape((image.height(), image.width(), 4))
    assert renderer._dome_count == 1
    assert np.count_nonzero(pixels[:, :, :3]) > 100
    renderer.close()


def test_constellation_interaction_options_reach_render_controller(
    monkeypatch,
) -> None:
    renderer = OffscreenSceneRenderer()
    renderer._last_pick_state = _state()
    renderer._last_pick_size = (800, 450)
    captured = {}

    def on_left_click(
        _x,
        _y,
        _project,
        _radec_to_sky,
        _pick_star,
        **options,
    ):
        captured.update(options)
        return True

    monkeypatch.setattr(
        renderer.constellations, "on_left_click", on_left_click
    )
    result = renderer.interact(
        20.0,
        30.0,
        "constellation_click",
        options={
            "force_add": True,
            "additive_select": True,
            "allow_when_disabled": True,
        },
    )
    assert result["handled"] is True
    assert captured == {
        "force_add": True,
        "additive_select": True,
        "allow_when_disabled": True,
    }
    renderer.close()
