import math
from typing import Callable, List, Optional, Sequence, Tuple


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
    """
    Great-circle angular distance (degrees) between two sky coordinates.
    """
    va = sky_to_vector(a)
    vb = sky_to_vector(b)
    dot = _clamp(va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2], -1.0, 1.0)
    return math.degrees(math.acos(dot))


def slerp_arc_points(a: SkyCoord, b: SkyCoord, n_points: int = 64) -> List[SkyCoord]:
    """
    Returns sampled points along the shortest great-circle arc from a to b.
    """
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


def destination_point(center: SkyCoord, bearing_deg: float, distance_deg: float) -> SkyCoord:
    """
    Direct geodesic on unit sphere in local horizon coordinates.
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
    """
    Adapter hook for screen->sky conversion. Keeps this logic centralized for tools.
    """
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
