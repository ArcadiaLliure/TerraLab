"""Pure distance fog and atmospheric depth functions."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from TerraLab.terrain.render.config import TerrainRenderSettings

_REFERENCE_VISIBILITY_RADIUS_KM = 150.0

def atmospheric_fog_factor(
    distance_m: Any,
    settings: TerrainRenderSettings,
    *,
    maximum_distance_m: float | None = None,
) -> np.ndarray:
    """Return a smooth normalized exponential fog factor in ``[0, 1]``."""

    distances = np.maximum(np.asarray(distance_m, dtype=np.float32), 0.0)
    if not settings.atmospheric_perspective_enabled:
        return np.zeros(distances.shape, dtype=np.float32)
    scale = 1.0
    if (
        settings.atmosphere_auto_scale
        and maximum_distance_m is not None
        and math.isfinite(float(maximum_distance_m))
        and float(maximum_distance_m) > 0.0
    ):
        scale = float(maximum_distance_m) / (_REFERENCE_VISIBILITY_RADIUS_KM * 1000.0)
    start = settings.atmosphere_start_distance_km * 1000.0 * scale
    end = settings.atmosphere_end_distance_km * 1000.0 * scale
    if maximum_distance_m is not None and math.isfinite(float(maximum_distance_m)):
        end = min(end, max(start + 1.0, float(maximum_distance_m)))
    span = max(1.0, end - start)
    normalized = np.clip((distances - start) / span, 0.0, 1.0)
    density = max(0.0, float(settings.atmosphere_density))
    if density <= 1e-8:
        return normalized.astype(np.float32)
    denominator = max(1e-8, 1.0 - math.exp(-density))
    fog = (1.0 - np.exp(-density * normalized)) / denominator
    return np.clip(fog, 0.0, 1.0).astype(np.float32)


def vibrant_depth_haze_factor(
    distance_m: Any,
    settings: TerrainRenderSettings,
    *,
    maximum_distance_m: float | None = None,
) -> np.ndarray:
    """Return the configurable BOTW-style depth interpolation factor."""

    distances = np.maximum(np.asarray(distance_m, dtype=np.float32), 0.0)
    if not settings.atmospheric_perspective_enabled:
        return np.zeros(distances.shape, dtype=np.float32)
    if not settings.vibrant_linear_depth_haze:
        return atmospheric_fog_factor(
            distances,
            settings,
            maximum_distance_m=maximum_distance_m,
        )
    depth_limit = float(
        maximum_distance_m
        if maximum_distance_m is not None
        and math.isfinite(float(maximum_distance_m))
        and float(maximum_distance_m) > 0.0
        else settings.atmosphere_end_distance_km * 1000.0
    )
    return np.clip(
        distances / max(depth_limit, 1.0), 0.0, 1.0
    ).astype(np.float32)
