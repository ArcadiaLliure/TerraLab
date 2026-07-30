"""Pure spherical geometry and celestial coordinate mathematics."""

import math
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence, Tuple

from TerraLab.scene.projection import local_sidereal_angle

SkyCoord = Tuple[float, float]  # (alt_deg, az_deg)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _wrap_angle_180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def _wrap_angle_360(deg: float) -> float:
    return deg % 360.0


def sky_to_vector(coord: SkyCoord) -> Tuple[float, float, float]:
    """Converts (alt, az) to unit vector in a local horizon frame."""
    alt_deg, az_deg = coord
    alt = math.radians(_clamp(alt_deg, -90.0, 90.0))
    az = math.radians(_wrap_angle_360(az_deg))
    ca = math.cos(alt)
    x = ca * math.cos(az)
    y = ca * math.sin(az)
    z = math.sin(alt)
    return x, y, z


def vector_to_sky(v: Sequence[float]) -> SkyCoord:
    x, y, z = float(v[0]), float(v[1]), float(v[2])
    n = math.sqrt(x * x + y * y + z * z)
    if n < 1e-12:
        return 0.0, 0.0
    x /= n
    y /= n
    z /= n
    alt = math.degrees(math.asin(_clamp(z, -1.0, 1.0)))
    az = math.degrees(math.atan2(y, x)) % 360.0
    return alt, az


def angular_distance(a: SkyCoord, b: SkyCoord) -> float:
    """Great-circle angular distance (degrees) between two sky coordinates."""
    va = sky_to_vector(a)
    vb = sky_to_vector(b)
    dot = _clamp(va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2], -1.0, 1.0)
    return math.degrees(math.acos(dot))


def slerp_arc_points(
    a: SkyCoord, b: SkyCoord, n_points: int = 64
) -> List[SkyCoord]:
    """Returns sampled points along the shortest great-circle arc from a to b."""
    n = max(2, int(n_points))
    va = sky_to_vector(a)
    vb = sky_to_vector(b)
    dot = _clamp(va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2], -1.0, 1.0)
    omega = math.acos(dot)
    if omega < 1e-10:
        return [a for _ in range(n)]

    sin_omega = math.sin(omega)
    out: List[SkyCoord] = []
    for i in range(n):
        t = i / (n - 1)
        w0 = math.sin((1.0 - t) * omega) / sin_omega
        w1 = math.sin(t * omega) / sin_omega
        vx = w0 * va[0] + w1 * vb[0]
        vy = w0 * va[1] + w1 * vb[1]
        vz = w0 * va[2] + w1 * vb[2]
        out.append(vector_to_sky((vx, vy, vz)))
    return out


def destination_point(
    center: SkyCoord, bearing_deg: float, distance_deg: float
) -> SkyCoord:
    """Direct geodesic on unit sphere in local horizon coordinates.

    Uses (alt, az) as (lat, lon).
    """
    alt0_deg, az0_deg = center
    lat1 = math.radians(_clamp(alt0_deg, -89.9999, 89.9999))
    lon1 = math.radians(_wrap_angle_360(az0_deg))
    brng = math.radians(_wrap_angle_360(bearing_deg))
    d = math.radians(max(0.0, float(distance_deg)))

    sin_lat1 = math.sin(lat1)
    cos_lat1 = math.cos(lat1)
    sin_d = math.sin(d)
    cos_d = math.cos(d)

    sin_lat2 = sin_lat1 * cos_d + cos_lat1 * sin_d * math.cos(brng)
    lat2 = math.asin(_clamp(sin_lat2, -1.0, 1.0))

    y = math.sin(brng) * sin_d * cos_lat1
    x = cos_d - sin_lat1 * math.sin(lat2)
    lon2 = lon1 + math.atan2(y, x)

    return math.degrees(lat2), _wrap_angle_360(math.degrees(lon2))


def screen_to_sky(
    sx: float,
    sy: float,
    unproject_fn: Callable[[float, float], Optional[SkyCoord]],
) -> Optional[SkyCoord]:
    """Adapter hook for screen->sky conversion. Keeps this logic centralized for tools."""
    try:
        out = unproject_fn(float(sx), float(sy))
        if out is None:
            return None
        alt, az = out
        if math.isnan(alt) or math.isnan(az):
            return None
        return float(_clamp(alt, -90.0, 90.0)), float(_wrap_angle_360(az))
    except Exception:
        return None


def angular_delta_signed(a_az_deg: float, b_az_deg: float) -> float:
    """Signed shortest delta b-a in degrees within [-180, 180)."""
    return _wrap_angle_180(float(b_az_deg) - float(a_az_deg))


def julian_day(dt: datetime) -> float:
    """Julian Day from a timezone-aware (or assumed UTC) datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    j2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return 2451545.0 + (dt - j2000).total_seconds() / 86400.0


def gmst_deg(jd: float) -> float:
    """Greenwich Mean Sidereal Time in degrees."""
    T = (float(jd) - 2451545.0) / 36525.0
    st = (
        280.46061837
        + 360.98564736629 * (float(jd) - 2451545.0)
        + 0.000387933 * T * T
        - (T * T * T) / 38710000.0
    )
    return st % 360.0


def lst_deg(jd: float, lon_deg: float) -> float:
    """Local Sidereal Time in degrees."""
    return (gmst_deg(float(jd)) + float(lon_deg)) % 360.0


def ra_dec_to_alt_az(
    ra_deg: float,
    dec_deg: float,
    ut_hour: float,
    day_of_year: int,
    latitude_deg: float,
    longitude_deg: float = 0.0,
    *,
    year: Optional[int] = None,
    default_az_deg: float = 0.0,
) -> SkyCoord:
    """Convert equatorial coordinates (RA/Dec) to local Alt/Az."""
    lat_rad = math.radians(float(latitude_deg))
    lst = local_sidereal_angle(
        int(day_of_year),
        float(ut_hour),
        float(longitude_deg),
        year=year,
    )
    ha_rad = math.radians(lst - float(ra_deg))
    dec_rad = math.radians(float(dec_deg))

    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)
    sin_dec = math.sin(dec_rad)
    cos_dec = math.cos(dec_rad)

    sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * math.cos(ha_rad)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt = math.degrees(math.asin(sin_alt))

    cos_alt = math.cos(math.radians(alt))
    if abs(cos_alt) < 1e-10:
        az = float(default_az_deg) % 360.0
    else:
        cos_az = (sin_dec - sin_alt * sin_lat) / (cos_alt * cos_lat + 1e-10)
        cos_az = max(-1.0, min(1.0, cos_az))
        az = math.degrees(math.acos(cos_az))
        if math.sin(ha_rad) > 0:
            az = 360.0 - az
    return float(alt), float(az) % 360.0


def altaz_to_ra_dec(
    alt_deg: float,
    az_deg: float,
    ut_hour: float,
    day_of_year: int,
    latitude_deg: float,
    longitude_deg: float = 0.0,
    *,
    year: Optional[int] = None,
) -> Tuple[float, float]:
    """Convert local Alt/Az to equatorial coordinates (RA/Dec)."""
    lat = math.radians(float(latitude_deg))
    alt = math.radians(float(alt_deg))
    az = math.radians(float(az_deg) % 360.0)

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    sin_alt = math.sin(alt)
    cos_alt = math.cos(alt)

    sin_dec = sin_alt * sin_lat + cos_alt * cos_lat * math.cos(az)
    sin_dec = max(-1.0, min(1.0, sin_dec))
    dec = math.degrees(math.asin(sin_dec))
    cos_dec = max(1e-10, math.cos(math.radians(dec)))

    cos_ha = (sin_alt - sin_lat * sin_dec) / (cos_lat * cos_dec + 1e-10)
    cos_ha = max(-1.0, min(1.0, cos_ha))
    sin_ha = -math.sin(az) * cos_alt / (cos_dec + 1e-10)
    ha_deg = math.degrees(math.atan2(sin_ha, cos_ha))

    lst = local_sidereal_angle(
        int(day_of_year),
        float(ut_hour),
        float(longitude_deg),
        year=year,
    )
    ra = (lst - ha_deg) % 360.0
    return float(ra), float(dec)


def get_sun_alt_az(
    hour: float, latitude_deg: float, day_of_year: int
) -> SkyCoord:
    """Approximate solar Alt/Az for UI shading and daylight decisions."""
    dec_deg = -23.44 * math.cos(
        math.radians((360.0 / 365.0) * (float(day_of_year) + 10.0))
    )
    dec_rad = math.radians(dec_deg)
    lat_rad = math.radians(float(latitude_deg))

    ha_deg = (float(hour) - 12.0) * 15.0
    ha_rad = math.radians(ha_deg)

    sin_alt = math.sin(dec_rad) * math.sin(lat_rad) + math.cos(
        dec_rad
    ) * math.cos(lat_rad) * math.cos(ha_rad)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt_deg = math.degrees(math.asin(sin_alt))

    cos_alt_val = math.cos(math.radians(alt_deg))
    if abs(cos_alt_val) < 1e-4:
        az_deg = 180.0
    else:
        cos_az = (math.sin(dec_rad) - sin_alt * math.sin(lat_rad)) / (
            cos_alt_val * math.cos(lat_rad)
        )
        cos_az = max(-1.0, min(1.0, cos_az))
        az_deg = math.degrees(math.acos(cos_az))
        if math.sin(ha_rad) > 0:
            az_deg = 360.0 - az_deg
    return float(alt_deg), float(az_deg)


def calculate_sun_times(
    latitude_deg: float, day_of_year: int
) -> Tuple[float, float]:
    """Approximate sunrise/sunset local solar hours."""
    dec = -23.44 * math.cos(
        math.radians((360.0 / 365.0) * (float(day_of_year) + 10.0))
    )
    lat_rad = math.radians(float(latitude_deg))
    dec_rad = math.radians(dec)

    val = -math.tan(lat_rad) * math.tan(dec_rad)
    val = max(-1.0, min(1.0, val))
    ha_rad = math.acos(val)
    half_day = math.degrees(ha_rad) / 15.0

    sunrise = 12.0 - half_day
    sunset = 12.0 + half_day
    return float(sunrise), float(sunset)
