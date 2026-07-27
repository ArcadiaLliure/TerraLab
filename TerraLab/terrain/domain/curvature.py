"""Earth-curvature and refraction corrections."""

from __future__ import annotations

from typing import Any

import numpy as np

_REFERENCE_VISIBILITY_RADIUS_KM = 150.0

def apparent_elevation_radians(
    terrain_elevation_m: Any,
    horizontal_distance_m: Any,
    observer_eye_elevation_m: float,
    earth_radius_m: float,
) -> np.ndarray:
    """Calculate the apparent terrain elevation used by raycast and mesh."""

    distance = np.asarray(horizontal_distance_m, dtype=np.float64)
    terrain = np.asarray(terrain_elevation_m, dtype=np.float64)
    safe_distance = np.maximum(distance, 1e-9)
    drop = safe_distance * safe_distance / (2.0 * float(earth_radius_m))
    return np.arctan2(
        terrain - drop - float(observer_eye_elevation_m), safe_distance
    )


def apparent_elevation_degrees(
    terrain_elevation_m: Any,
    horizontal_distance_m: Any,
    observer_eye_elevation_m: float,
    earth_radius_m: float,
) -> np.ndarray:
    """Return :func:`apparent_elevation_radians` in degrees."""

    return np.degrees(
        apparent_elevation_radians(
            terrain_elevation_m,
            horizontal_distance_m,
            observer_eye_elevation_m,
            earth_radius_m,
        )
    )
