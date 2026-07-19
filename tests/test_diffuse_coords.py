import numpy as np

from TerraLab.render.sky.milkyway_overlay import MilkyWayOverlay
from TerraLab.scene.camera import Camera
from TerraLab.scene.projection import (
    project_universal_stereo_numpy,
    unproject_universal_stereo_point,
)


def _angular_wrap_deg(value: float) -> float:
    return min(abs(float(value)), abs(float(value) - 360.0))


def test_galactic_center_conversion_is_stable():
    ra = np.asarray([266.4051], dtype=np.float32)
    dec = np.asarray([-28.936175], dtype=np.float32)
    l_deg, b_deg = MilkyWayOverlay._equatorial_to_galactic_deg(ra, dec)
    assert _angular_wrap_deg(float(l_deg[0])) < 0.3
    assert abs(float(b_deg[0])) < 0.3


def test_projection_grid_builds_without_nan():
    cam = Camera(
        azimuth_offset=180.0,
        elevation_angle=35.0,
        zoom_level=1.0,
        vertical_offset_ratio=0.25,
    )
    alts = np.linspace(-5.0, 85.0, 32, dtype=np.float32)
    azs = np.linspace(0.0, 359.0, 48, dtype=np.float32)
    alt_grid, az_grid = np.meshgrid(alts, azs, indexing="ij")

    sx, sy, valid = project_universal_stereo_numpy(
        alt_grid.ravel(),
        az_grid.ravel(),
        width=1280,
        height=720,
        camera=cam,
    )
    assert sx is not None and sy is not None and valid is not None
    assert np.isfinite(sx[valid]).all()
    assert np.isfinite(sy[valid]).all()


def test_projection_roundtrip_sample():
    cam = Camera(
        azimuth_offset=122.0,
        elevation_angle=20.0,
        zoom_level=1.15,
        vertical_offset_ratio=0.18,
    )
    sx, sy, valid = project_universal_stereo_numpy(
        np.asarray([22.5], dtype=np.float32),
        np.asarray([145.0], dtype=np.float32),
        width=1000,
        height=600,
        camera=cam,
    )
    assert bool(valid[0]) is True
    out = unproject_universal_stereo_point(
        float(sx[0]), float(sy[0]), 1000, 600, cam
    )
    assert out is not None
    alt2, az2 = out
    assert abs(alt2 - 22.5) < 1e-4
    assert abs(((az2 - 145.0 + 180.0) % 360.0) - 180.0) < 1e-4
