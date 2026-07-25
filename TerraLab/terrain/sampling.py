"""Distance-adaptive terrain profile sampling without Qt dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from TerraLab.terrain.domain.curvature import apparent_elevation_radians
from TerraLab.terrain.render.sampling import TerrainSamplingSettings


@dataclass(frozen=True, slots=True)
class RefinementEvaluation:
    should_refine: np.ndarray
    score: np.ndarray
    elevation_error_m: np.ndarray
    projected_error_px: np.ndarray
    slope_delta_deg: np.ndarray


@dataclass(frozen=True, slots=True)
class AdaptiveRayResult:
    distances: np.ndarray
    elevations: np.ndarray
    valid: np.ndarray
    queried_samples: int
    maximum_elevation_error_m: float
    maximum_projected_error_px: float
    maximum_slope_delta_deg: float


def build_adaptive_base_distances(
    maximum_distance_m: float,
    settings: TerrainSamplingSettings,
) -> np.ndarray:
    """Build a near-dense, progressively growing first-pass distance set."""

    maximum = max(0.5, float(maximum_distance_m))
    distances = [0.5]
    step = float(settings.sampling_near_step_m)
    while len(distances) < settings.sampling_max_samples_per_ray:
        candidate = distances[-1] + step
        if candidate >= maximum:
            break
        distances.append(candidate)
        step = min(
            float(settings.sampling_far_step_m),
            max(
                float(settings.sampling_near_step_m),
                step * float(settings.sampling_step_growth),
            ),
        )
    return np.asarray(distances, dtype=np.float64)


def evaluate_refinement_intervals(
    distance_0: Any,
    elevation_0: Any,
    valid_0: Any,
    distance_1: Any,
    elevation_1: Any,
    valid_1: Any,
    midpoint_distance: Any,
    midpoint_elevation: Any,
    midpoint_valid: Any,
    *,
    observer_eye_elevation_m: float,
    earth_radius_m: float,
    pixels_per_radian: float,
    settings: TerrainSamplingSettings,
) -> RefinementEvaluation:
    """Evaluate all required geometric/profile errors for midpoint samples."""

    d0, h0, v0, d1, h1, v1, dm, hm, vm = np.broadcast_arrays(
        np.asarray(distance_0, dtype=np.float64),
        np.asarray(elevation_0, dtype=np.float64),
        np.asarray(valid_0, dtype=bool),
        np.asarray(distance_1, dtype=np.float64),
        np.asarray(elevation_1, dtype=np.float64),
        np.asarray(valid_1, dtype=bool),
        np.asarray(midpoint_distance, dtype=np.float64),
        np.asarray(midpoint_elevation, dtype=np.float64),
        np.asarray(midpoint_valid, dtype=bool),
    )
    span = np.maximum(d1 - d0, 1e-9)
    interpolation_t = np.clip((dm - d0) / span, 0.0, 1.0)
    interpolated_height = h0 + (h1 - h0) * interpolation_t
    all_valid = v0 & v1 & vm
    elevation_error = np.where(
        all_valid, np.abs(hm - interpolated_height), 0.0
    )

    angle_0 = apparent_elevation_radians(
        h0, d0, observer_eye_elevation_m, earth_radius_m
    )
    angle_1 = apparent_elevation_radians(
        h1, d1, observer_eye_elevation_m, earth_radius_m
    )
    angle_midpoint = apparent_elevation_radians(
        hm, dm, observer_eye_elevation_m, earth_radius_m
    )
    interpolated_angle = angle_0 + (angle_1 - angle_0) * interpolation_t
    projected_error = np.where(
        all_valid,
        np.abs(angle_midpoint - interpolated_angle) * float(pixels_per_radian),
        0.0,
    )

    left_slope = np.degrees(
        np.arctan2(hm - h0, np.maximum(dm - d0, 1e-9))
    )
    right_slope = np.degrees(
        np.arctan2(h1 - hm, np.maximum(d1 - dm, 1e-9))
    )
    slope_delta = np.where(all_valid, np.abs(right_slope - left_slope), 0.0)

    validity_transition = vm != (v0 & v1)
    # A midpoint crossing the current angular envelope is an occlusion/silhouette
    # transition even when its absolute pixel error is just below tolerance.
    visibility_transition = all_valid & (
        (angle_midpoint > np.maximum(angle_0, angle_1))
        != (interpolated_angle > np.maximum(angle_0, angle_1))
    )
    should_refine = (
        validity_transition
        | visibility_transition
        | (
            elevation_error
            > float(settings.sampling_max_elevation_error_m)
        )
        | (
            projected_error
            > float(settings.sampling_max_projected_error_px)
        )
        | (
            slope_delta
            > float(settings.sampling_max_slope_delta_deg)
        )
    )
    score = np.maximum.reduce(
        (
            elevation_error
            / max(1e-9, float(settings.sampling_max_elevation_error_m)),
            projected_error
            / max(1e-9, float(settings.sampling_max_projected_error_px)),
            slope_delta
            / max(1e-9, float(settings.sampling_max_slope_delta_deg)),
            validity_transition.astype(np.float64) * 2.0,
            visibility_transition.astype(np.float64) * 1.5,
        )
    )
    return RefinementEvaluation(
        should_refine=np.asarray(should_refine, dtype=bool),
        score=np.asarray(score, dtype=np.float64),
        elevation_error_m=np.asarray(elevation_error, dtype=np.float64),
        projected_error_px=np.asarray(projected_error, dtype=np.float64),
        slope_delta_deg=np.asarray(slope_delta, dtype=np.float64),
    )


def adaptive_refine_ray(
    sample_elevations: Callable[[np.ndarray], tuple[Any, Any]],
    *,
    maximum_distance_m: float,
    observer_eye_elevation_m: float,
    earth_radius_m: float,
    pixels_per_radian: float,
    settings: TerrainSamplingSettings,
) -> AdaptiveRayResult:
    """Reference one-ray implementation used by tests and small providers."""

    distances = build_adaptive_base_distances(maximum_distance_m, settings)
    elevations, valid = sample_elevations(distances)
    elevations = np.asarray(elevations, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    queried = int(distances.size)
    maxima = np.zeros(3, dtype=np.float64)
    minimum_step = float(settings.sampling_near_step_m)
    active_intervals = np.ones(max(0, distances.size - 1), dtype=bool)

    for _depth in range(settings.sampling_max_subdivision_depth):
        candidates = np.flatnonzero(
            active_intervals
            & (np.diff(distances) > minimum_step + 1e-9)
        )
        if candidates.size == 0:
            break
        midpoints = 0.5 * (distances[candidates] + distances[candidates + 1])
        midpoint_elevations, midpoint_valid = sample_elevations(midpoints)
        midpoint_elevations = np.asarray(midpoint_elevations, dtype=np.float64)
        midpoint_valid = np.asarray(midpoint_valid, dtype=bool)
        queried += int(midpoints.size)
        evaluation = evaluate_refinement_intervals(
            distances[candidates],
            elevations[candidates],
            valid[candidates],
            distances[candidates + 1],
            elevations[candidates + 1],
            valid[candidates + 1],
            midpoints,
            midpoint_elevations,
            midpoint_valid,
            observer_eye_elevation_m=observer_eye_elevation_m,
            earth_radius_m=earth_radius_m,
            pixels_per_radian=pixels_per_radian,
            settings=settings,
        )
        maxima = np.maximum(
            maxima,
            (
                float(np.max(evaluation.elevation_error_m, initial=0.0)),
                float(np.max(evaluation.projected_error_px, initial=0.0)),
                float(np.max(evaluation.slope_delta_deg, initial=0.0)),
            ),
        )
        selected_positions = np.flatnonzero(evaluation.should_refine)
        remaining = settings.sampling_max_samples_per_ray - distances.size
        if selected_positions.size > remaining:
            order = np.argsort(
                evaluation.score[selected_positions], kind="stable"
            )[::-1]
            selected_positions = selected_positions[order[:remaining]]
        if selected_positions.size == 0:
            break
        selected_candidates = candidates[selected_positions]
        insertions = {
            int(interval): (
                float(midpoints[position]),
                float(midpoint_elevations[position]),
                bool(midpoint_valid[position]),
            )
            for position, interval in zip(
                selected_positions, selected_candidates
            )
        }
        next_distances = []
        next_elevations = []
        next_valid = []
        next_active = []
        for index in range(distances.size - 1):
            next_distances.append(float(distances[index]))
            next_elevations.append(float(elevations[index]))
            next_valid.append(bool(valid[index]))
            if index in insertions:
                distance, elevation, is_valid = insertions[index]
                next_distances.append(distance)
                next_elevations.append(elevation)
                next_valid.append(is_valid)
                next_active.extend((True, True))
            else:
                next_active.append(False)
        next_distances.append(float(distances[-1]))
        next_elevations.append(float(elevations[-1]))
        next_valid.append(bool(valid[-1]))
        distances = np.asarray(next_distances, dtype=np.float64)
        elevations = np.asarray(next_elevations, dtype=np.float64)
        valid = np.asarray(next_valid, dtype=bool)
        active_intervals = np.asarray(next_active, dtype=bool)
        if distances.size >= settings.sampling_max_samples_per_ray:
            break

    return AdaptiveRayResult(
        distances=distances,
        elevations=elevations,
        valid=valid,
        queried_samples=queried,
        maximum_elevation_error_m=float(maxima[0]),
        maximum_projected_error_px=float(maxima[1]),
        maximum_slope_delta_deg=float(maxima[2]),
    )
