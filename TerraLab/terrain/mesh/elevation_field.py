"""Immutable polar elevation field used by terrain raycasting."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

@dataclass(frozen=True)
class PolarElevationField:
    """Bounded polar samples shared by horizon and relief mesh."""

    azimuths: np.ndarray
    distances: np.ndarray
    elevations: np.ndarray
    valid: np.ndarray
    observer_x: float
    observer_y: float
    observer_ground: float
    d_max: float
    delta_az_deg: float

    def __post_init__(self) -> None:
        azimuths = np.asarray(self.azimuths, dtype=np.float32)
        distances = np.asarray(self.distances, dtype=np.float32)
        elevations = np.asarray(self.elevations, dtype=np.float32)
        valid = np.asarray(self.valid, dtype=bool)
        if elevations.shape != (distances.size, azimuths.size):
            raise ValueError("Polar elevation dimensions are inconsistent")
        if valid.shape != elevations.shape:
            raise ValueError("Polar validity mask is inconsistent")
        for array in (azimuths, distances, elevations, valid):
            array.setflags(write=False)
        object.__setattr__(self, "azimuths", azimuths)
        object.__setattr__(self, "distances", distances)
        object.__setattr__(self, "elevations", elevations)
        object.__setattr__(self, "valid", valid)
