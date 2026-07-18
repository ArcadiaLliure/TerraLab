import math

SCREEN_WIDTH = 1920
ZOOM = 1.0


def _compute_dome(intensity: float, dist_m: float):
    dist_factor = math.exp(-float(dist_m) / 35000.0)
    log_intensity = math.log10(1.0 + float(intensity))
    visual_intensity = log_intensity * dist_factor

    alpha_base = min(100, int(visual_intensity * 60.0))
    alpha_base = int(alpha_base * 1.5)

    max_rad = SCREEN_WIDTH * 0.4
    rad_x = min(max_rad, log_intensity * 30.0 * ZOOM * dist_factor)
    rad_y = rad_x * 0.35
    return alpha_base, rad_x, rad_y


def test_dome_scale_outputs_are_finite():
    alpha, rx, ry = _compute_dome(200.0, 24000.0)
    assert alpha >= 0
    assert math.isfinite(rx)
    assert math.isfinite(ry)
    assert rx >= 0.0
    assert ry >= 0.0


def test_dome_scale_decreases_with_distance():
    near = _compute_dome(180.0, 24000.0)
    far = _compute_dome(180.0, 88000.0)
    assert near[0] > far[0]
    assert near[1] > far[1]
    assert near[2] > far[2]
