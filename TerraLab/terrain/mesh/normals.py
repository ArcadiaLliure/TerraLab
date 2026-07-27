"""Vectorized normal generation for polar terrain meshes."""

from typing import Tuple

import numpy as np

def compute_polar_mesh_normals(
    elevations: np.ndarray,
    valid: np.ndarray,
    distances: np.ndarray,
    azimuths: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    elevations = np.asarray(elevations, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    distances = np.asarray(distances, dtype=np.float32)
    azimuths = np.asarray(azimuths, dtype=np.float32)
    if (
        elevations.ndim != 2
        or elevations.shape != valid.shape
        or elevations.shape != (distances.size, azimuths.size)
        or distances.size < 2
        or azimuths.size < 2
    ):
        shape = elevations.shape
        return (
            np.zeros(shape, dtype=np.float32),
            np.zeros(shape, dtype=np.float32),
            np.ones(shape, dtype=np.float32),
        )

    azimuth_radians = np.deg2rad(azimuths.astype(np.float64))[None, :]
    distance_grid = distances.astype(np.float64)[:, None]
    points = np.stack(
        np.broadcast_arrays(
            distance_grid * np.sin(azimuth_radians),
            distance_grid * np.cos(azimuth_radians),
            elevations.astype(np.float64),
        ),
        axis=-1,
    )

    previous_rows = np.maximum(np.arange(distances.size) - 1, 0)
    next_rows = np.minimum(np.arange(distances.size) + 1, distances.size - 1)
    previous_points = points[previous_rows]
    next_points = points[next_rows]
    previous_valid = valid[previous_rows] & valid
    next_valid = valid[next_rows] & valid
    radial = np.where(
        (previous_valid & next_valid)[..., None],
        next_points - previous_points,
        np.where(
            next_valid[..., None],
            next_points - points,
            np.where(previous_valid[..., None], points - previous_points, 0.0),
        ),
    )

    left_points = np.roll(points, 1, axis=1)
    right_points = np.roll(points, -1, axis=1)
    left_valid = np.roll(valid, 1, axis=1) & valid
    right_valid = np.roll(valid, -1, axis=1) & valid
    angular = np.where(
        (left_valid & right_valid)[..., None],
        right_points - left_points,
        np.where(
            right_valid[..., None],
            right_points - points,
            np.where(left_valid[..., None], points - left_points, 0.0),
        ),
    )

    radial_length = np.linalg.norm(radial, axis=-1)
    radial_fallback = np.broadcast_to(
        np.stack(
            (
                np.sin(azimuth_radians[0]),
                np.cos(azimuth_radians[0]),
                np.zeros(azimuths.size),
            ),
            axis=-1,
        )[None, :, :],
        points.shape,
    )
    radial = np.where((radial_length > 1e-9)[..., None], radial, radial_fallback)

    angular_length = np.linalg.norm(angular, axis=-1)
    angular_fallback = np.broadcast_to(
        np.stack(
            (
                np.cos(azimuth_radians[0]),
                -np.sin(azimuth_radians[0]),
                np.zeros(azimuths.size),
            ),
            axis=-1,
        )[None, :, :],
        points.shape,
    )
    angular = np.where(
        (angular_length > 1e-9)[..., None], angular, angular_fallback
    )

    normals = np.cross(angular, radial)
    normals = np.where((normals[..., 2] < 0.0)[..., None], -normals, normals)
    norm = np.linalg.norm(normals, axis=-1, keepdims=True)
    normals = np.divide(
        normals,
        np.maximum(norm, 1e-9),
        out=np.zeros_like(normals),
        where=norm > 1e-9,
    )
    fallback = np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    normals = np.where(
        (valid & np.isfinite(norm[..., 0]) & (norm[..., 0] > 1e-9))[..., None],
        normals,
        fallback,
    )
    return (
        normals[..., 0].astype(np.float32),
        normals[..., 1].astype(np.float32),
        normals[..., 2].astype(np.float32),
    )
