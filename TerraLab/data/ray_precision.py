"""Stable data-contract conversions for horizon ray angular precision."""

import math

MIN_RAY_STEP_DEG = 0.005
MAX_RAY_STEP_DEG = 5.0
DEFAULT_RAY_STEP_DEG = 0.5
RAY_STEP_SLIDER_SCALE = 1000


def normalize_ray_step_deg(value) -> float:
    try:
        step = float(value)
    except (TypeError, ValueError):
        step = DEFAULT_RAY_STEP_DEG
    if not math.isfinite(step):
        step = DEFAULT_RAY_STEP_DEG
    return max(MIN_RAY_STEP_DEG, min(MAX_RAY_STEP_DEG, step))


def ray_step_to_slider(value) -> int:
    return int(round(normalize_ray_step_deg(value) * RAY_STEP_SLIDER_SCALE))


def slider_to_ray_step(value: int) -> float:
    return normalize_ray_step_deg(float(value) / RAY_STEP_SLIDER_SCALE)


def ray_count(value) -> int:
    """Match numpy.arange(0, 360, step) for progress reporting."""
    return int(math.ceil(360.0 / normalize_ray_step_deg(value)))
