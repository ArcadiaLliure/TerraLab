"""Projection helpers for sky coordinates and stereographic screen mapping."""

from __future__ import annotations

import math
from typing import Optional, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

from TerraLab.scene.camera import Camera


def local_sidereal_angle(day_of_year: int, ut_hour: float, longitude_deg: float) -> float:
    return (100.0 + float(day_of_year) * 0.9856 + float(ut_hour) * 15.0 + float(longitude_deg)) % 360.0


def radec_to_altaz_numpy(
    ra_deg,
    dec_deg,
    latitude_deg: float,
    longitude_deg: float,
    ut_hour: float,
    day_of_year: int,
):
    """Vectorized RA/Dec -> Alt/Az in degrees."""
    if np is None:
        return None, None

    ra = np.asarray(ra_deg, dtype=np.float32)
    dec = np.asarray(dec_deg, dtype=np.float32)

    lat_rad = np.float32(math.radians(float(latitude_deg)))
    lst = np.float32(local_sidereal_angle(day_of_year, ut_hour, longitude_deg))

    ha_rad = np.radians(lst - ra)
    dec_rad = np.radians(dec)

    sin_lat = np.float32(math.sin(float(lat_rad)))
    cos_lat = np.float32(math.cos(float(lat_rad)))

    sin_dec = np.sin(dec_rad)
    cos_dec = np.cos(dec_rad)

    sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * np.cos(ha_rad)
    sin_alt = np.clip(sin_alt, -1.0, 1.0)
    alt_rad = np.arcsin(sin_alt)

    cos_alt = np.cos(alt_rad)
    cos_az = (sin_dec - sin_alt * sin_lat) / (cos_alt * cos_lat + 1e-12)
    cos_az = np.clip(cos_az, -1.0, 1.0)
    az_rad = np.arccos(cos_az)
    az_deg = np.degrees(az_rad)
    az_deg = np.where(np.sin(ha_rad) > 0.0, 360.0 - az_deg, az_deg)

    return np.asarray(np.degrees(alt_rad), dtype=np.float32), np.asarray(az_deg, dtype=np.float32)


def project_universal_stereo_point(
    alt_deg: float,
    az_deg: float,
    width: int,
    height: int,
    camera: Camera,
) -> Optional[Tuple[float, float]]:
    az_rel = math.radians(float(az_deg) - float(camera.azimuth_offset))
    alt_rad = math.radians(float(alt_deg))

    cos_alt = math.cos(alt_rad)
    sin_alt = math.sin(alt_rad)
    cos_az = math.cos(az_rel)
    sin_az = math.sin(az_rel)

    denom = 1.0 + cos_alt * cos_az
    if denom <= 1e-6:
        return None

    k = 2.0 / denom
    x = k * cos_alt * sin_az
    y = k * sin_alt

    scale_h = height * 0.5 * float(camera.zoom_level)
    cx = width * 0.5
    cy_base = (height * 0.5) + (height * float(camera.vertical_offset_ratio))

    y_center_val = 2.0 * math.tan(math.radians(float(camera.elevation_angle)) * 0.5)
    sx = cx + x * scale_h
    sy = cy_base - (y - y_center_val) * scale_h

    return sx, sy


def project_universal_stereo_numpy(
    alt_deg,
    az_deg,
    width: int,
    height: int,
    camera: Camera,
):
    """Vectorized universal stereographic projection.

    Returns `(sx, sy, valid_mask)`.
    """
    if np is None:
        return None, None, None

    alt = np.asarray(alt_deg, dtype=np.float32)
    az = np.asarray(az_deg, dtype=np.float32)

    az_rel = np.radians(az - float(camera.azimuth_offset))
    alt_rad = np.radians(alt)

    cos_alt = np.cos(alt_rad)
    sin_alt = np.sin(alt_rad)
    cos_az = np.cos(az_rel)
    sin_az = np.sin(az_rel)

    denom = 1.0 + cos_alt * cos_az
    valid = denom > 1e-6
    safe = np.where(valid, denom, 1.0)

    k = 2.0 / safe
    x = k * cos_alt * sin_az
    y = k * sin_alt

    scale_h = height * 0.5 * float(camera.zoom_level)
    cx = width * 0.5
    cy_base = (height * 0.5) + (height * float(camera.vertical_offset_ratio))
    y_center_val = 2.0 * math.tan(math.radians(float(camera.elevation_angle)) * 0.5)

    sx = cx + x * scale_h
    sy = cy_base - (y - y_center_val) * scale_h

    return np.asarray(sx, dtype=np.float32), np.asarray(sy, dtype=np.float32), valid
