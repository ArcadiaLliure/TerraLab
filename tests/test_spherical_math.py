import math
from datetime import datetime, timezone

from TerraLab.scene.camera import Camera
from TerraLab.scene.projection import (
    local_sidereal_angle,
    project_universal_stereo_point,
    unproject_universal_stereo_point,
)
from TerraLab.widgets.spherical_math import (
    altaz_to_ra_dec,
    angular_distance,
    calculate_sun_times,
    julian_day,
    ra_dec_to_alt_az,
    sky_to_vector,
    slerp_arc_points,
)


def test_angular_distance_known_values():
    # Same point
    assert abs(angular_distance((10.0, 20.0), (10.0, 20.0))) < 1e-9

    # 1 degree separation on equator-like horizontal line
    d1 = angular_distance((0.0, 0.0), (0.0, 1.0))
    assert abs(d1 - 1.0) < 1e-6

    # Orthogonal great-circle points
    d90 = angular_distance((0.0, 0.0), (90.0, 0.0))
    assert abs(d90 - 90.0) < 1e-6


def test_slerp_arc_points_stay_on_unit_sphere():
    pts = slerp_arc_points((0.0, 0.0), (0.0, 60.0), n_points=21)
    assert len(pts) == 21
    for p in pts:
        v = sky_to_vector(p)
        n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
        assert abs(n - 1.0) < 1e-6

    # Monotonic increase in this simple arc
    assert pts[0][1] <= pts[-1][1]


def _ra_delta_deg(a: float, b: float) -> float:
    return abs(((float(a) - float(b) + 180.0) % 360.0) - 180.0)


def test_ra_dec_to_alt_az_polaris_transit():
    lat = 41.0
    lon = 2.0
    ut_hour = 0.0
    day = 40
    ra_polaris = local_sidereal_angle(day, ut_hour, lon)
    dec_polaris = 89.2641
    alt, _az = ra_dec_to_alt_az(
        ra_polaris,
        dec_polaris,
        ut_hour,
        day,
        latitude_deg=lat,
        longitude_deg=lon,
    )
    expected_alt = 90.0 - abs(lat - dec_polaris)
    assert abs(alt - expected_alt) < 0.6


def test_altaz_roundtrip():
    lat = 41.4
    lon = 2.1
    ut_hour = 22.25
    day = 120
    ra = 145.0
    dec = 19.2
    alt, az = ra_dec_to_alt_az(
        ra, dec, ut_hour, day, latitude_deg=lat, longitude_deg=lon
    )
    ra2, dec2 = altaz_to_ra_dec(
        alt, az, ut_hour, day, latitude_deg=lat, longitude_deg=lon
    )
    assert _ra_delta_deg(ra, ra2) < 1e-5
    assert abs(dec - dec2) < 1e-5


def test_project_stereo_zenith():
    cam = Camera(
        azimuth_offset=180.0,
        elevation_angle=40.0,
        zoom_level=1.0,
        vertical_offset_ratio=0.3,
    )
    pt = project_universal_stereo_point(
        90.0, cam.azimuth_offset, 1280, 720, cam
    )
    assert pt is not None
    assert math.isfinite(float(pt[0]))
    assert math.isfinite(float(pt[1]))


def test_unproject_roundtrip():
    cam = Camera(
        azimuth_offset=133.0,
        elevation_angle=25.0,
        zoom_level=1.35,
        vertical_offset_ratio=0.12,
    )
    alt = 27.5
    az = 148.2
    pt = project_universal_stereo_point(alt, az, 1600, 900, cam)
    assert pt is not None
    out = unproject_universal_stereo_point(pt[0], pt[1], 1600, 900, cam)
    assert out is not None
    alt2, az2 = out
    assert abs(alt - alt2) < 1e-6
    assert _ra_delta_deg(az, az2) < 1e-6


def test_calculate_sun_times():
    sunrise, sunset = calculate_sun_times(41.4, 172)
    assert 0.0 <= sunrise <= 24.0
    assert 0.0 <= sunset <= 24.0
    assert sunrise < sunset


def test_julian_day_j2000():
    jd = julian_day(datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    assert abs(jd - 2451545.0) < 1e-9
