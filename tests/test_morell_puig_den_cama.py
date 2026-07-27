"""DEM and apparent-elevation regression for Morell -> Puig d'en Cama."""

from __future__ import annotations

import logging
import math
import os

import pytest
from pyproj import Geod

from TerraLab.common.utils import get_config_value
from TerraLab.terrain.providers import create_elevation_provider
from TerraLab.terrain.crs import meridian_convergence_degrees
from TerraLab.terrain.domain.curvature import apparent_elevation_degrees
from TerraLab.terrain.visibility_range import EARTH_RADIUS_M, TerrainRangeSettings


LOGGER = logging.getLogger(__name__)
OBSERVER = (41.193151965759704, 1.2025659030876152)
PUIG_DEN_CAMA = (41.22024807132576, 1.0940375126367885)
REFERENCE_OBSERVER_DEM_M = 102.5186
REFERENCE_PUIG_DEM_M = 717
EYE_HEIGHT_M = 1.7


def _angular_delta_degrees(a: float, b: float) -> float:
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


def test_morell_puig_den_cama():
    dem_path = str(get_config_value("raster_path", "") or "")
    if not dem_path or not os.path.exists(dem_path):
        pytest.skip("El DEM configurado no está disponible para la regresión de Prades")

    provider = create_elevation_provider(dem_path)
    try:
        observer_x, observer_y = provider.transform_coordinates(*OBSERVER)
        target_x, target_y = provider.transform_coordinates(*PUIG_DEN_CAMA)
        observer_dem_m = provider.get_elevation(observer_x, observer_y)
        target_dem_m = provider.get_elevation(target_x, target_y)
    finally:
        provider.close()

    if observer_dem_m is None or target_dem_m is None:
        pytest.skip("El DEM configurado no cubre Morell y Puig d'en Cama")

    geodetic_azimuth_deg, _back_azimuth, geodetic_distance_m = Geod(
        ellps="WGS84"
    ).inv(
        OBSERVER[1],
        OBSERVER[0],
        PUIG_DEN_CAMA[1],
        PUIG_DEN_CAMA[0],
    )
    geodetic_azimuth_deg %= 360.0
    delta_x = float(target_x) - float(observer_x)
    delta_y = float(target_y) - float(observer_y)
    renderer_distance_m = math.hypot(delta_x, delta_y)
    grid_azimuth_deg = math.degrees(math.atan2(delta_x, delta_y)) % 360.0
    convergence_deg = meridian_convergence_degrees(OBSERVER[1], OBSERVER[0])
    renderer_azimuth_deg = (grid_azimuth_deg + convergence_deg) % 360.0

    range_raw = get_config_value("terrain_visibility_range", {})
    range_settings = TerrainRangeSettings.from_mapping(
        range_raw if isinstance(range_raw, dict) else {}
    )
    effective_radius_m = EARTH_RADIUS_M * (
        range_settings.effective_earth_radius_factor
        if range_settings.atmospheric_refraction_enabled
        else 1.0
    )
    observer_eye_m = float(observer_dem_m) + EYE_HEIGHT_M
    expected_angle_deg = math.degrees(
        math.atan2(
            float(target_dem_m)
            - observer_eye_m
            - geodetic_distance_m**2 / (2.0 * effective_radius_m),
            geodetic_distance_m,
        )
    )
    renderer_angle_deg = float(
        apparent_elevation_degrees(
            target_dem_m,
            renderer_distance_m,
            observer_eye_m,
            effective_radius_m,
        )
    )

    observer_height_deviation_m = (
        float(observer_dem_m) - REFERENCE_OBSERVER_DEM_M
    )
    target_height_deviation_m = float(target_dem_m) - REFERENCE_PUIG_DEM_M
    angle_deviation_deg = renderer_angle_deg - expected_angle_deg
    azimuth_deviation_deg = _angular_delta_degrees(
        renderer_azimuth_deg, geodetic_azimuth_deg
    )
    distance_deviation_m = renderer_distance_m - geodetic_distance_m
    LOGGER.info(
        "Morell -> Puig d'en Cama | DEM observador=%.3f m (desv=%.3f m) | "
        "DEM pico=%.3f m (desv=%.3f m) | distancia geodésica=%.3f m | "
        "distancia renderer=%.3f m (desv=%.3f m) | azimut geodésico=%.6f° | "
        "azimut grid=%.6f° | convergencia=%.6f° | azimut renderer=%.6f° "
        "(desv=%.6f°) | elevación trigonométrica=%.6f° | "
        "elevación renderer=%.6f° (desv=%.6f°) | R efectivo=%.3f m",
        observer_dem_m,
        observer_height_deviation_m,
        target_dem_m,
        target_height_deviation_m,
        geodetic_distance_m,
        renderer_distance_m,
        distance_deviation_m,
        geodetic_azimuth_deg,
        grid_azimuth_deg,
        convergence_deg,
        renderer_azimuth_deg,
        azimuth_deviation_deg,
        expected_angle_deg,
        renderer_angle_deg,
        angle_deviation_deg,
        effective_radius_m,
    )

    assert 0.0 < float(observer_dem_m) < 500.0
    assert 500.0 < float(target_dem_m) < 1_500.0
    assert abs(observer_height_deviation_m) < 60.0
    assert abs(target_height_deviation_m) < 100.0
    assert abs(distance_deviation_m) < 10.0
    assert azimuth_deviation_deg < 0.02
    assert abs(angle_deviation_deg) < 0.01
