"""Terrain horizon raycasting and mesh construction."""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.perf_events import append_perf_event
from TerraLab.common.performance.budget import DEFAULT_PERFORMANCE_BUDGET
from TerraLab.common.performance.flags import PERFORMANCE_FLAGS
from TerraLab.common.performance.memory import process_memory_bytes
from TerraLab.terrain.crs import CRS_GEOGRAPHIC, CRS_TERRAIN_INTERNAL
from TerraLab.terrain.domain.curvature import (
    apparent_elevation_degrees,
    apparent_elevation_radians,
)
from TerraLab.terrain.render.sampling import TerrainSamplingSettings
from TerraLab.terrain.sampling import (
    build_adaptive_base_distances,
    evaluate_refinement_intervals,
)

from TerraLab.terrain.domain.bands import generate_bands
from TerraLab.terrain.domain.profile import HorizonProfile
from TerraLab.terrain.infrastructure.dem_tiles import DemSampler, TileCache, TileIndex
from TerraLab.terrain.mesh.elevation_field import PolarElevationField
from TerraLab.terrain.mesh.normals import compute_polar_mesh_normals
from TerraLab.terrain.persistence.profile_npz import save_profile

R_EARTH = 6_371_000.0
MIN_RELIEF_MESH_AZIMUTH_STEP_DEG = 0.05
TERRAIN_MESH_VERSION = 3
NEAR_PATCH_HALF_EXTENT_M = 80.0

class HorizonBaker:
    """
    Raycasts from observer position to compute horizon elevation angles.
    When a ray exits available DEM coverage, it stops and keeps
    whatever silhouette data was already gathered.
    """

    def __init__(
        self,
        provider,
        eye_height: float = 1.7,
        R: float = R_EARTH,
        grid_convergence_deg: float = 0.0,
        sampling_settings: TerrainSamplingSettings | None = None,
        sampling_pixels_per_radian: float = 1000.0,
    ):
        self.provider = provider
        self.eye_height = eye_height
        self.R = R
        self.grid_convergence_deg = float(grid_convergence_deg)
        self.sampling_settings = sampling_settings or TerrainSamplingSettings(
            adaptive_sampling_enabled=False
        )
        self.sampling_pixels_per_radian = max(
            1.0, float(sampling_pixels_per_radian)
        )
        self.performance_logging_enabled = False
        self.last_sampling_metrics: dict[str, object] = {}
        self.last_mesh_metrics: dict[str, object] = {}
        self._ray_miss_exit_threshold = 8
        self._ray_iteration_guard = 1_000_000
        self._vector_azimuth_batch = 64
        self._last_polar_field: PolarElevationField | None = None

    @staticmethod
    def _requested_raycast_backend() -> str:
        try:
            from TerraLab.common.utils import get_config_value

            requested = str(
                get_config_value("terrain.raycast_backend", "auto") or "auto"
            ).strip().lower()
        except Exception:
            requested = "auto"
        if requested not in {"auto", "single", "threads", "processes"}:
            requested = "auto"
        # The segmented NumPy reducer is substantially faster than IPC on the
        # reference workload. Explicit parallel modes remain opt-in until their
        # measured speedup clears the acceptance threshold.
        return requested

    def _raster_io_metrics(self) -> dict[str, int]:
        """Collect cumulative block-cache counters without coupling to a provider."""

        pending = [self.provider]
        seen: set[int] = set()
        metrics = {
            "bytes_read": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "selection_ns": 0,
            "interpolation_ns": 0,
            "candidate_tiles": 0,
            "loaded_tiles": 0,
            "sampled_points": 0,
        }
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            pending.extend(getattr(item, "providers", ()) or ())
            for child_name in ("cache",):
                child = getattr(item, child_name, None)
                if child is not None:
                    pending.append(child)
            datasets = getattr(item, "_datasets", ()) or ()
            pending.extend(datasets)
            for name in metrics:
                attribute = {
                    "selection_ns": "sample_selection_ns",
                    "interpolation_ns": "sample_interpolation_ns",
                    "candidate_tiles": "sample_candidate_tiles",
                    "loaded_tiles": "sample_loaded_tiles",
                    "sampled_points": "sampled_points",
                }.get(name, name)
                metrics[name] += int(getattr(item, attribute, 0) or 0)
        return metrics

    @staticmethod
    def _next_step_distance(
        d: float, step_m: float, near_factor: float = 1.5
    ) -> float:
        """Return the next ray-march distance using the adaptive stepping policy."""
        if d < step_m:
            return min(d * near_factor, step_m)
        if d < 3_000:
            return d + step_m
        if d < 15_000:
            return d + step_m * 2.0
        if d < 50_000:
            return d + step_m * 4.0
        return d + step_m * 8.0

    @classmethod
    def _adaptive_distances(cls, step_m: float, d_max: float) -> np.ndarray:
        """Materialize the exact legacy distance progression once per bake."""

        distances = []
        distance = 0.5
        guard = 0
        while distance < float(d_max):
            distances.append(float(distance))
            guard += 1
            if guard > 1_000_000:
                raise RuntimeError("Ray distance iteration guard reached")
            next_distance = cls._next_step_distance(distance, float(step_m))
            if not np.isfinite(next_distance) or next_distance <= distance:
                raise ValueError("Ray distance sequence does not progress")
            distance = float(next_distance)
        return np.asarray(distances, dtype=np.float64)

    def _supports_batch_sampling(self) -> bool:
        """Return true only for providers overriding the scalar compatibility loop."""

        if not PERFORMANCE_FLAGS.raycast_vectorized:
            return False
        for provider_type in type(self.provider).__mro__:
            if provider_type.__name__ == "RasterProvider":
                return False
            namespace = getattr(provider_type, "__dict__", {})
            if "sample_elevation" in namespace or "sample_elevations" in namespace:
                return True
        return False

    def _sample_provider_batch(
        self, x: np.ndarray, y: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        sampler = getattr(self.provider, "sample_elevation", None)
        if callable(sampler):
            batch = sampler(x, y)
        else:
            sampler = getattr(self.provider, "sample_elevations", None)
            if not callable(sampler):
                raise TypeError("Provider has no batch elevation method")
            try:
                batch = sampler(x, y, input_crs=CRS_TERRAIN_INTERNAL)
            except TypeError:
                batch = sampler(x, y)
        if hasattr(batch, "values") and hasattr(batch, "valid"):
            raw_values = batch.values
            raw_valid = batch.valid
        else:
            raw_values, raw_valid = batch[:2]
        values = np.asarray(raw_values, dtype=np.float32)
        valid = np.asarray(raw_valid, dtype=bool)
        if values.shape != x.shape or valid.shape != x.shape:
            raise ValueError("Provider batch result shape mismatch")
        valid &= np.isfinite(values)
        return values, valid

    def _refine_azimuth_chunk(
        self,
        *,
        az_indices: np.ndarray,
        obs_x: float,
        obs_y: float,
        h_eye_abs: float,
        base_distances: np.ndarray,
        base_elevations: np.ndarray,
        base_valid: np.ndarray,
        band_defs: List[Dict],
        sin_az: np.ndarray,
        cos_az: np.ndarray,
    ) -> tuple[list[dict], dict[str, float | int]]:
        """Refine independent ray profiles while batching midpoint DEM reads."""

        started_ns = __import__("time").perf_counter_ns()
        settings = self.sampling_settings
        profiles = [
            {
                "distances": np.asarray(base_distances, dtype=np.float64).copy(),
                "elevations": np.asarray(base_elevations[index], dtype=np.float64).copy(),
                "valid": np.asarray(base_valid[index], dtype=bool).copy(),
                "active": np.ones(
                    max(0, np.asarray(base_distances).size - 1), dtype=bool
                ),
            }
            for index in range(len(az_indices))
        ]
        queried_samples = 0
        valid_queries = 0
        maximum_errors = np.zeros(3, dtype=np.float64)
        minimum_step = float(settings.sampling_near_step_m)

        for _depth in range(settings.sampling_max_subdivision_depth):
            candidate_rays = []
            candidate_intervals = []
            candidate_d0 = []
            candidate_d1 = []
            candidate_h0 = []
            candidate_h1 = []
            candidate_v0 = []
            candidate_v1 = []
            for ray_index, profile in enumerate(profiles):
                remaining = (
                    settings.sampling_max_samples_per_ray
                    - profile["distances"].size
                )
                if remaining <= 0:
                    continue
                intervals = np.flatnonzero(
                    profile["active"]
                    & (
                        np.diff(profile["distances"])
                        > minimum_step + 1e-9
                    )
                )
                for interval in intervals:
                    candidate_rays.append(ray_index)
                    candidate_intervals.append(int(interval))
                    candidate_d0.append(profile["distances"][interval])
                    candidate_d1.append(profile["distances"][interval + 1])
                    candidate_h0.append(profile["elevations"][interval])
                    candidate_h1.append(profile["elevations"][interval + 1])
                    candidate_v0.append(profile["valid"][interval])
                    candidate_v1.append(profile["valid"][interval + 1])
            if not candidate_rays:
                break

            candidate_rays = np.asarray(candidate_rays, dtype=np.int32)
            candidate_intervals = np.asarray(candidate_intervals, dtype=np.int32)
            d0 = np.asarray(candidate_d0, dtype=np.float64)
            d1 = np.asarray(candidate_d1, dtype=np.float64)
            midpoint_distances = 0.5 * (d0 + d1)
            global_azimuth_indices = np.asarray(az_indices, dtype=np.int32)[
                candidate_rays
            ]
            x = (
                float(obs_x)
                + sin_az[global_azimuth_indices] * midpoint_distances
            )
            y = (
                float(obs_y)
                + cos_az[global_azimuth_indices] * midpoint_distances
            )
            midpoint_elevations, midpoint_valid = self._sample_provider_batch(x, y)
            midpoint_elevations = np.asarray(midpoint_elevations, dtype=np.float64)
            midpoint_valid = np.asarray(midpoint_valid, dtype=bool)
            queried_samples += int(midpoint_distances.size)
            valid_queries += int(np.count_nonzero(midpoint_valid))

            evaluation = evaluate_refinement_intervals(
                d0,
                np.asarray(candidate_h0),
                np.asarray(candidate_v0),
                d1,
                np.asarray(candidate_h1),
                np.asarray(candidate_v1),
                midpoint_distances,
                midpoint_elevations,
                midpoint_valid,
                observer_eye_elevation_m=float(h_eye_abs),
                earth_radius_m=float(self.R),
                pixels_per_radian=float(self.sampling_pixels_per_radian),
                settings=settings,
            )
            maximum_errors = np.maximum(
                maximum_errors,
                (
                    float(np.max(evaluation.elevation_error_m, initial=0.0)),
                    float(np.max(evaluation.projected_error_px, initial=0.0)),
                    float(np.max(evaluation.slope_delta_deg, initial=0.0)),
                ),
            )
            selected_by_ray: dict[int, np.ndarray] = {}
            for ray_index in range(len(profiles)):
                positions = np.flatnonzero(
                    (candidate_rays == ray_index) & evaluation.should_refine
                )
                remaining = (
                    settings.sampling_max_samples_per_ray
                    - profiles[ray_index]["distances"].size
                )
                if positions.size > remaining:
                    order = np.argsort(
                        evaluation.score[positions], kind="stable"
                    )[::-1]
                    positions = positions[order[:remaining]]
                if positions.size:
                    selected_by_ray[ray_index] = positions
            if not selected_by_ray:
                break

            # Every evaluated interval has reached a terminal state unless it
            # was split.  Rebuild all profiles, including rays with no chosen
            # midpoint, so rejected intervals are not queried again at the
            # next subdivision depth.
            for ray_index in range(len(profiles)):
                positions = selected_by_ray.get(
                    ray_index, np.empty(0, dtype=np.int64)
                )
                profile = profiles[ray_index]
                insertion_by_interval = {
                    int(candidate_intervals[position]): (
                        float(midpoint_distances[position]),
                        float(midpoint_elevations[position]),
                        bool(midpoint_valid[position]),
                    )
                    for position in positions
                }
                next_distances = []
                next_elevations = []
                next_valid = []
                next_active = []
                for interval in range(profile["distances"].size - 1):
                    next_distances.append(profile["distances"][interval])
                    next_elevations.append(profile["elevations"][interval])
                    next_valid.append(profile["valid"][interval])
                    if interval in insertion_by_interval:
                        distance, elevation, is_valid = insertion_by_interval[
                            interval
                        ]
                        next_distances.append(distance)
                        next_elevations.append(elevation)
                        next_valid.append(is_valid)
                        next_active.extend((True, True))
                    else:
                        next_active.append(False)
                next_distances.append(profile["distances"][-1])
                next_elevations.append(profile["elevations"][-1])
                next_valid.append(profile["valid"][-1])
                profile["distances"] = np.asarray(next_distances, dtype=np.float64)
                profile["elevations"] = np.asarray(next_elevations, dtype=np.float64)
                profile["valid"] = np.asarray(next_valid, dtype=bool)
                profile["active"] = np.asarray(next_active, dtype=bool)

        band_results = [
            {
                "angles": np.full(len(profiles), -np.inf, dtype=np.float32),
                "dists": np.zeros(len(profiles), dtype=np.float32),
                "heights": np.zeros(len(profiles), dtype=np.float32),
                "surface_angles": np.full(
                    len(profiles), -np.inf, dtype=np.float32
                ),
                "surface_dists": np.zeros(len(profiles), dtype=np.float32),
                "surface_heights": np.zeros(len(profiles), dtype=np.float32),
            }
            for _definition in band_defs
        ]
        for ray_index, profile in enumerate(profiles):
            distances = profile["distances"]
            elevations = profile["elevations"]
            effective_valid = profile["valid"].copy()
            threshold = int(self._ray_miss_exit_threshold)
            if effective_valid.size >= threshold:
                missing_windows = np.lib.stride_tricks.sliding_window_view(
                    ~effective_valid, threshold
                )
                exits = np.flatnonzero(np.all(missing_windows, axis=1))
                if exits.size:
                    effective_valid[int(exits[0]) + threshold - 1 :] = False
            angles = apparent_elevation_radians(
                elevations,
                distances,
                float(h_eye_abs),
                float(self.R),
            )
            angles = np.where(effective_valid, angles, -np.inf)
            for band_index, definition in enumerate(band_defs):
                in_band = (
                    effective_valid
                    & (distances >= float(definition["min"]))
                    & (distances < float(definition["max"]))
                )
                indices = np.flatnonzero(in_band)
                if indices.size == 0:
                    continue
                best = int(indices[np.argmax(angles[indices])])
                last = int(indices[-1])
                target = band_results[band_index]
                target["angles"][ray_index] = angles[best]
                target["dists"][ray_index] = distances[best]
                target["heights"][ray_index] = elevations[best]
                target["surface_angles"][ray_index] = angles[last]
                target["surface_dists"][ray_index] = distances[last]
                target["surface_heights"][ray_index] = elevations[last]

        elapsed_ns = __import__("time").perf_counter_ns() - started_ns
        return band_results, {
            "refinement_ns": int(elapsed_ns),
            "refinement_queries": int(queried_samples),
            "refinement_valid_queries": int(valid_queries),
            "selected_samples": int(
                sum(profile["distances"].size for profile in profiles)
            ),
            "maximum_elevation_error_m": float(maximum_errors[0]),
            "maximum_projected_error_px": float(maximum_errors[1]),
            "maximum_slope_delta_deg": float(maximum_errors[2]),
        }

    def _sample_azimuth_chunk_vectorized(
        self,
        *,
        az_indices: np.ndarray,
        obs_x: float,
        obs_y: float,
        h_eye_abs: float,
        ray_distances: np.ndarray,
        sample_distances: np.ndarray,
        ray_distance_indices: np.ndarray,
        band_defs: List[Dict],
        band_slices: Optional[List[Tuple[int, int]]] = None,
        sin_az: np.ndarray,
        cos_az: np.ndarray,
        light_sampler=None,
    ) -> dict:
        """Sample and reduce one <=64-azimuth matrix without Python point loops."""

        phase_started_ns = __import__("time").perf_counter_ns()
        az_indices = np.asarray(az_indices, dtype=np.int32)
        local_sin = np.asarray(sin_az[az_indices], dtype=np.float64)[:, None]
        local_cos = np.asarray(cos_az[az_indices], dtype=np.float64)[:, None]
        distances_2d = np.asarray(sample_distances, dtype=np.float64)[None, :]
        x_all = float(obs_x) + local_sin * distances_2d
        y_all = float(obs_y) + local_cos * distances_2d
        coordinate_ns = __import__("time").perf_counter_ns() - phase_started_ns
        provider_started_ns = __import__("time").perf_counter_ns()
        elevations_all, valid_all = self._sample_provider_batch(x_all, y_all)
        provider_ns = __import__("time").perf_counter_ns() - provider_started_ns

        angle_started_ns = __import__("time").perf_counter_ns()
        elevations = elevations_all[:, ray_distance_indices]
        valid = valid_all[:, ray_distance_indices]
        threshold = int(self._ray_miss_exit_threshold)
        if valid.shape[1] >= threshold:
            windows = np.lib.stride_tricks.sliding_window_view(
                ~valid, threshold, axis=1
            )
            miss_runs = np.all(windows, axis=-1)
            has_exit = np.any(miss_runs, axis=1)
            first_start = np.argmax(miss_runs, axis=1)
            # The legacy loop breaks while handling the eighth miss.  No valid
            # point at or after that position may affect a reduction.
            stop = np.where(
                has_exit, first_start + threshold - 1, valid.shape[1]
            )
            before_exit = np.arange(valid.shape[1])[None, :] < stop[:, None]
            effective_valid = valid & before_exit
        else:
            effective_valid = valid

        ray_d = np.asarray(ray_distances, dtype=np.float64)
        angles = apparent_elevation_radians(
            elevations.astype(np.float64),
            ray_d[None, :],
            float(h_eye_abs),
            float(self.R),
        )
        angles = np.where(effective_valid, angles, -np.inf)
        angle_ns = __import__("time").perf_counter_ns() - angle_started_ns

        if band_slices is None:
            band_slices = [
                (
                    int(np.searchsorted(ray_d, float(definition["min"]), side="left")),
                    int(np.searchsorted(ray_d, float(definition["max"]), side="left")),
                )
                for definition in band_defs
            ]

        band_started_ns = __import__("time").perf_counter_ns()
        band_results = []
        rows = np.arange(len(az_indices))
        for start, stop in band_slices:
            segment_valid = effective_valid[:, start:stop]
            segment_angles = angles[:, start:stop]
            segment_elevations = elevations[:, start:stop]
            segment_distances = ray_d[start:stop]
            if stop <= start:
                empty_float = np.zeros(len(az_indices), dtype=np.float32)
                empty_angle = np.full(len(az_indices), -np.inf, dtype=np.float32)
                band_results.append(
                    {
                        "angles": empty_angle,
                        "dists": empty_float.copy(),
                        "heights": empty_float.copy(),
                        "surface_angles": empty_angle.copy(),
                        "surface_dists": empty_float.copy(),
                        "surface_heights": empty_float.copy(),
                    }
                )
                continue
            in_band = segment_valid
            has_value = np.any(in_band, axis=1)
            band_angles = np.where(in_band, segment_angles, -np.inf)
            best_index = np.argmax(band_angles, axis=1)
            best_angle = np.where(
                has_value,
                band_angles[rows, best_index],
                -np.inf,
            )
            reverse_last = np.argmax(in_band[:, ::-1], axis=1)
            last_index = in_band.shape[1] - 1 - reverse_last
            safe_last = np.maximum(last_index, 0)
            band_results.append(
                {
                    "angles": best_angle,
                    "dists": np.where(has_value, segment_distances[best_index], 0.0),
                    "heights": np.where(
                        has_value,
                        segment_elevations[rows, best_index],
                        0.0,
                    ),
                    "surface_angles": np.where(
                        has_value,
                        segment_angles[rows, safe_last],
                        -np.inf,
                    ),
                    "surface_dists": np.where(
                        has_value, segment_distances[safe_last], 0.0
                    ),
                    "surface_heights": np.where(
                        has_value,
                        segment_elevations[rows, safe_last],
                        0.0,
                    ),
                }
            )
        band_ns = __import__("time").perf_counter_ns() - band_started_ns

        light_started_ns = __import__("time").perf_counter_ns()
        light_domes = np.zeros(len(az_indices), dtype=np.float32)
        light_peak_distances = np.zeros(len(az_indices), dtype=np.float32)
        max_radiance = np.zeros(len(az_indices), dtype=np.float32)
        if light_sampler is not None:
            prefix_max = np.maximum.accumulate(angles, axis=1)
            x_ray = x_all[:, ray_distance_indices]
            y_ray = y_all[:, ray_distance_indices]
            candidate_rows = []
            candidate_columns = []
            for local_index in range(len(az_indices)):
                last_light_distance = 0.0
                for distance_index in np.flatnonzero(effective_valid[local_index]):
                    distance = float(ray_d[distance_index])
                    if distance - last_light_distance < 2000.0:
                        continue
                    last_light_distance = distance
                    candidate_rows.append(local_index)
                    candidate_columns.append(distance_index)
            if candidate_rows:
                rows_arr = np.asarray(candidate_rows, dtype=np.int32)
                cols_arr = np.asarray(candidate_columns, dtype=np.int32)
                radiances = self._sample_light_radiance_batch(
                    light_sampler,
                    x_ray[rows_arr, cols_arr],
                    y_ray[rows_arr, cols_arr],
                ).reshape(-1)
                visible_light = (
                    (radiances > 0.1)
                    & (
                        angles[rows_arr, cols_arr]
                        > prefix_max[rows_arr, cols_arr] - 0.17
                    )
                )
                contributions = np.where(
                    visible_light,
                    radiances
                    * (1.0 / np.maximum(1.0, ray_d[cols_arr] / 1000.0))
                    * 20.0,
                    0.0,
                )
                np.add.at(light_domes, rows_arr, contributions.astype(np.float32))
                for local_index in range(len(az_indices)):
                    local_positions = np.flatnonzero(
                        (rows_arr == local_index) & visible_light
                    )
                    if local_positions.size == 0:
                        continue
                    best = local_positions[np.argmax(radiances[local_positions])]
                    max_radiance[local_index] = radiances[best]
                    light_peak_distances[local_index] = ray_d[cols_arr[best]]
        light_ns = __import__("time").perf_counter_ns() - light_started_ns

        return {
            "elevations": elevations_all,
            "valid": valid_all,
            "bands": band_results,
            "light_domes": light_domes,
            "light_peak_distances": light_peak_distances,
            "timings_ns": {
                "coordinates": int(coordinate_ns),
                "provider": int(provider_ns),
                "angles": int(angle_ns),
                "bands": int(band_ns),
                "light": int(light_ns),
            },
        }

    @staticmethod
    def _build_band_buffers(n_az: int, band_defs: List[Dict]) -> List[Dict]:
        bands = []
        for bd in band_defs:
            bands.append(
                {
                    "id": bd["id"],
                    "min": bd["min"],
                    "max": bd["max"],
                    "angles": np.full(n_az, -np.inf, dtype=np.float32),
                    "dists": np.zeros(n_az, dtype=np.float32),
                    "heights": np.zeros(n_az, dtype=np.float32),
                    "surface_angles": np.full(n_az, -np.inf, dtype=np.float32),
                    "surface_dists": np.zeros(n_az, dtype=np.float32),
                    "surface_heights": np.zeros(n_az, dtype=np.float32),
                }
            )
        return bands

    def _sample_single_azimuth(
        self,
        az_index: int,
        obs_x: float,
        obs_y: float,
        h_eye_abs: float,
        step_m: float,
        d_max: float,
        bands: List[Dict],
        sin_az: np.ndarray,
        cos_az: np.ndarray,
        light_domes: np.ndarray,
        light_peak_distances: np.ndarray,
        max_rad_per_az: np.ndarray,
        light_sampler=None,
    ) -> None:
        c = sin_az[az_index]
        s = cos_az[az_index]

        NEAR_START = 0.5
        d = NEAR_START
        max_ang_so_far = -np.pi / 2.0
        last_light_d = 0.0
        miss_streak = 0
        band_idx = 0
        iterations = 0

        while d < d_max:
            iterations += 1
            if iterations > self._ray_iteration_guard:
                print(
                    f"[HorizonEngine] Warning: ray iteration guard reached at az={az_index}, d={d:.1f}"
                )
                break
            x = obs_x + d * c
            y = obs_y + d * s

            h_terr = self.provider.get_elevation(x, y)
            if h_terr is None:
                miss_streak += 1
                if miss_streak >= self._ray_miss_exit_threshold:
                    break
                d_next = self._next_step_distance(d, step_m)
                if not np.isfinite(d_next) or d_next <= d:
                    print(
                        f"[HorizonEngine] Warning: invalid step progression at az={az_index}, d={d:.3f}, next={d_next}"
                    )
                    break
                d = d_next
                continue
            miss_streak = 0

            ang = float(
                apparent_elevation_radians(h_terr, d, h_eye_abs, self.R)
            )

            if ang > max_ang_so_far:
                max_ang_so_far = ang

            if light_sampler is not None and (d - last_light_d) >= 2000.0:
                last_light_d = d
                rad = self._sample_light_radiance(light_sampler, x, y)

                if rad and rad > 0.1:
                    dist_mult = 1.0 / max(1.0, (d / 1000.0))
                    if ang > (max_ang_so_far - 0.17):
                        light_domes[az_index] += float(rad * dist_mult * 20.0)
                        if rad > max_rad_per_az[az_index]:
                            max_rad_per_az[az_index] = float(rad)
                            light_peak_distances[az_index] = float(d)

            while band_idx + 1 < len(bands) and d >= bands[band_idx]["max"]:
                band_idx += 1
            if (
                0 <= band_idx < len(bands)
                and bands[band_idx]["min"] <= d < bands[band_idx]["max"]
            ):
                b = bands[band_idx]
                b["surface_angles"][az_index] = ang
                b["surface_dists"][az_index] = d
                b["surface_heights"][az_index] = h_terr
                if ang > b["angles"][az_index]:
                    b["angles"][az_index] = ang
                    b["dists"][az_index] = d
                    b["heights"][az_index] = h_terr

            d_next = self._next_step_distance(d, step_m)
            if not np.isfinite(d_next) or d_next <= d:
                print(
                    f"[HorizonEngine] Warning: invalid step progression at az={az_index}, d={d:.3f}, next={d_next}"
                )
                break
            d = d_next

    def _sample_light_radiance(
        self, light_sampler, x_internal: float, y_internal: float
    ) -> float:
        """
        Sample light-pollution radiance at a terrain internal point.

        Input CRS:
            - `x_internal`, `y_internal` in terrain internal CRS (`EPSG:25831`).
        Output:
            - Radiance float, `0.0` on controlled failure/out-of-bounds.
        """
        if light_sampler is None:
            return 0.0
        try:
            return float(
                light_sampler.get_radiance_terrain_xy(
                    x_internal,
                    y_internal,
                    input_crs=CRS_TERRAIN_INTERNAL,
                )
            )
        except Exception:
            return 0.0

    def _sample_light_radiance_batch(self, light_sampler, x, y) -> np.ndarray:
        sampler = getattr(light_sampler, "get_radiance_terrain_xy_batch", None)
        if callable(sampler):
            try:
                return np.asarray(
                    sampler(x, y, input_crs=CRS_TERRAIN_INTERNAL),
                    dtype=np.float32,
                )
            except Exception:
                log_suppressed_exception(__name__, "HorizonBaker._sample_light_radiance_batch")
        x_arr, y_arr = np.broadcast_arrays(np.asarray(x), np.asarray(y))
        return np.asarray(
            [
                self._sample_light_radiance(light_sampler, float(px), float(py))
                for px, py in zip(x_arr.ravel(), y_arr.ravel())
            ],
            dtype=np.float32,
        ).reshape(x_arr.shape)

    def _provider_nominal_resolution_m(self) -> float:
        resolution = None
        getter = getattr(self.provider, "get_nominal_resolution_m", None)
        if callable(getter):
            try:
                resolution = float(getter())
            except Exception:
                resolution = None
        if resolution is None or not np.isfinite(resolution) or resolution <= 0:
            resolution = 30.0
        return float(resolution)

    def _normal_sample_step_m(self) -> float:
        resolution = self._provider_nominal_resolution_m()
        return float(max(10.0, min(120.0, resolution * 2.0)))

    def _sample_normal(
        self, x: float, y: float, center_h: float, step_m: float
    ) -> Tuple[float, float, float]:
        h_e = self.provider.get_elevation(x + step_m, y)
        h_w = self.provider.get_elevation(x - step_m, y)
        h_n = self.provider.get_elevation(x, y + step_m)
        h_s = self.provider.get_elevation(x, y - step_m)

        if h_e is None:
            h_e = center_h
        if h_w is None:
            h_w = center_h
        if h_n is None:
            h_n = center_h
        if h_s is None:
            h_s = center_h

        dzdx = (float(h_e) - float(h_w)) / (2.0 * step_m)
        dzdy = (float(h_n) - float(h_s)) / (2.0 * step_m)
        nx = -dzdx
        ny = -dzdy
        nz = 1.0
        norm = math.sqrt(nx * nx + ny * ny + nz * nz)
        if norm <= 0.0 or not np.isfinite(norm):
            return 0.0, 0.0, 1.0
        return nx / norm, ny / norm, nz / norm

    @staticmethod
    def _mesh_azimuth_step(delta_az_deg: float) -> float:
        """Keep relief geometry screen-dense without mirroring ray precision."""

        try:
            requested = float(delta_az_deg)
        except (TypeError, ValueError):
            requested = 1.0
        if not np.isfinite(requested) or requested <= 0.0:
            requested = 1.0
        if requested >= MIN_RELIEF_MESH_AZIMUTH_STEP_DEG:
            return requested

        # Keep retained mesh columns exactly aligned with scientific rays.
        # For arbitrary inputs (for example 0.03 degrees), the first aligned
        # visual step above the floor is 0.06 rather than a synthetic 0.05.
        stride = int(
            math.ceil(MIN_RELIEF_MESH_AZIMUTH_STEP_DEG / requested)
        )
        return requested * max(1, stride)

    @staticmethod
    def _mesh_distance_rings(
        d_max: float, resolution_m: Optional[float] = None
    ) -> np.ndarray:
        visual_max = max(250.0, float(d_max))
        resolution = (
            float(resolution_m)
            if resolution_m is not None
            and np.isfinite(resolution_m)
            and resolution_m > 0
            else 30.0
        )
        # The polar mesh starts where the Cartesian near patch is already
        # present.  This avoids the polar singularity below the observer while
        # retaining a generous overlap for the z-buffer at the transition.
        zones = ((40.0, 5_000.0, 150), (5_000.0, 25_000.0, 100),
                 (25_000.0, 100_000.0, 75), (100_000.0, 250_000.0, 45),
                 (250_000.0, visual_max, 25))
        segments = []
        for start, stop, budget in zones:
            stop = min(stop, visual_max)
            if stop <= start:
                continue
            count = max(2, min(budget, int(math.ceil((stop - start) / max(resolution * 2.0, 1.0))) + 1))
            segments.append(np.geomspace(start, stop, count))

        rings = np.unique(np.round(np.concatenate(segments)).astype(np.float32))
        rings = rings[rings <= visual_max]
        return rings[rings > 0]

    @staticmethod
    def _near_patch_axis(
        half_extent_m: float = NEAR_PATCH_HALF_EXTENT_M,
    ) -> np.ndarray:
        """Return a symmetric ENU axis dense at the eye and coarse outside.

        A uniform DEM-sized cell next to the eye still projects as a giant
        triangle when looking down.  The nested spacings bound that projection
        error without pretending that the DEM contains sub-metre relief: the
        extra vertices only interpolate/query the same continuous DEM surface.
        """

        extent = max(40.0, float(half_extent_m))
        positive = np.unique(
            np.concatenate(
                (
                    np.arange(0.0, min(4.0, extent) + 0.25, 0.5),
                    np.arange(5.0, min(12.0, extent) + 0.5, 1.0),
                    np.arange(14.0, min(24.0, extent) + 1.0, 2.0),
                    np.arange(28.0, min(40.0, extent) + 2.0, 4.0),
                    np.arange(48.0, extent + 4.0, 8.0),
                    np.asarray([extent]),
                )
            )
        )
        positive = positive[(positive >= 0.0) & (positive <= extent)]
        return np.concatenate((-positive[:0:-1], positive)).astype(np.float32)

    def _build_near_patch(
        self,
        obs_x: float,
        obs_y: float,
        h_eye_abs: float,
        *,
        abort_check=None,
    ) -> Dict[str, np.ndarray]:
        """Sample a real Cartesian DEM patch around the observer."""

        eastings = self._near_patch_axis()
        northings = eastings.copy()
        east, north = np.meshgrid(
            eastings.astype(np.float64),
            northings.astype(np.float64),
        )
        x = float(obs_x) + east
        y = float(obs_y) + north
        if abort_check and abort_check():
            raise InterruptedError("Near terrain patch build aborted")
        if self._supports_batch_sampling():
            elevations, valid = self._sample_provider_batch(x, y)
        else:
            elevations = np.zeros(x.shape, dtype=np.float32)
            valid = np.zeros(x.shape, dtype=bool)
            for row, column in np.ndindex(x.shape):
                if abort_check and abort_check():
                    raise InterruptedError("Near terrain patch build aborted")
                value = self.provider.get_elevation(
                    float(x[row, column]), float(y[row, column])
                )
                if value is not None and np.isfinite(value):
                    elevations[row, column] = float(value)
                    valid[row, column] = True

        distance = np.hypot(east, north)
        computed_altitudes = apparent_elevation_degrees(
            elevations.astype(np.float64),
            distance,
            float(h_eye_abs),
            float(self.R),
        ).astype(np.float32)
        altitudes = np.where(valid, computed_altitudes, -90.0).astype(np.float32)

        # Gradients are calculated in the projected Cartesian grid. Invalid
        # vertices never form triangles; using the eye height as a finite fill
        # merely keeps neighbouring normal calculations numerically stable.
        finite_elevations = np.where(
            valid, elevations, float(h_eye_abs) - float(self.eye_height)
        ).astype(np.float64)
        gradient_north, gradient_east = np.gradient(
            finite_elevations,
            northings.astype(np.float64),
            eastings.astype(np.float64),
            edge_order=1,
        )
        nx_grid = -gradient_east
        ny_grid = -gradient_north
        convergence = math.radians(float(self.grid_convergence_deg))
        normal_x = nx_grid * math.cos(convergence) + ny_grid * math.sin(convergence)
        normal_y = -nx_grid * math.sin(convergence) + ny_grid * math.cos(convergence)
        normal_z = np.ones_like(normal_x)
        norm = np.sqrt(normal_x * normal_x + normal_y * normal_y + normal_z * normal_z)
        normal_x = np.where(valid, normal_x / np.maximum(norm, 1e-12), 0.0)
        normal_y = np.where(valid, normal_y / np.maximum(norm, 1e-12), 0.0)
        normal_z = np.where(valid, normal_z / np.maximum(norm, 1e-12), 1.0)

        return {
            "near_patch_eastings": eastings,
            "near_patch_northings": northings,
            "near_patch_altitudes": altitudes,
            "near_patch_elevations": elevations.astype(np.float32),
            "near_patch_normal_x": normal_x.astype(np.float32),
            "near_patch_normal_y": normal_y.astype(np.float32),
            "near_patch_normal_z": normal_z.astype(np.float32),
            "near_patch_valid": valid,
        }

    @staticmethod
    def _compute_mesh_visibility(
        altitudes: np.ndarray,
        valid: np.ndarray,
        margin_deg: float = 0.02,
    ) -> np.ndarray:
        altitudes = np.asarray(altitudes)
        valid = np.asarray(valid, dtype=bool)
        visible = np.zeros_like(valid, dtype=bool)
        if altitudes.shape != valid.shape or altitudes.ndim != 2:
            return visible
        finite_valid = valid & np.isfinite(altitudes)
        candidates = np.where(finite_valid, altitudes, -np.inf)
        running_max = np.maximum.accumulate(candidates, axis=0)
        return finite_valid & (
            altitudes >= running_max - float(margin_deg)
        )

    def build_view_mesh(
        self,
        obs_x: float,
        obs_y: float,
        obs_h_ground: float,
        *,
        d_max: float,
        delta_az_deg: float = 1.0,
        abort_check=None,
    ) -> Dict:
        mesh_started_ns = __import__("time").perf_counter_ns()
        requested_delta_az_deg = float(delta_az_deg)
        mesh_delta_az_deg = self._mesh_azimuth_step(requested_delta_az_deg)
        h_eye_abs = float(obs_h_ground) + float(self.eye_height)
        resolution_m = self._provider_nominal_resolution_m()
        near_patch = self._build_near_patch(
            obs_x,
            obs_y,
            h_eye_abs,
            abort_check=abort_check,
        )
        distances = self._mesh_distance_rings(d_max, resolution_m)
        azimuths = np.arange(
            0.0, 360.0, mesh_delta_az_deg, dtype=np.float32
        )
        n_d = len(distances)
        n_az = len(azimuths)

        altitudes = np.full((n_d, n_az), -90.0, dtype=np.float32)
        elevations = np.zeros((n_d, n_az), dtype=np.float32)
        normal_x = np.zeros((n_d, n_az), dtype=np.float32)
        normal_y = np.zeros((n_d, n_az), dtype=np.float32)
        normal_z = np.ones((n_d, n_az), dtype=np.float32)
        valid = np.zeros((n_d, n_az), dtype=bool)

        az_rads = np.deg2rad(
            azimuths.astype(np.float64) - self.grid_convergence_deg
        )
        sin_az = np.sin(az_rads)
        cos_az = np.cos(az_rads)

        reused_field = False
        field = self._last_polar_field
        if (
            field is not None
            and math.isclose(field.observer_x, float(obs_x), abs_tol=1e-6)
            and math.isclose(field.observer_y, float(obs_y), abs_tol=1e-6)
            and math.isclose(field.observer_ground, float(obs_h_ground), abs_tol=1e-6)
            and math.isclose(
                field.delta_az_deg, mesh_delta_az_deg, abs_tol=1e-9
            )
            and field.azimuths.shape == azimuths.shape
            and np.array_equal(field.azimuths, azimuths)
        ):
            distance_indices = np.searchsorted(field.distances, distances)
            in_bounds = distance_indices < field.distances.size
            if bool(np.all(in_bounds)) and np.array_equal(
                field.distances[distance_indices], distances
            ):
                elevations[:] = field.elevations[distance_indices, :]
                valid[:] = field.valid[distance_indices, :]
                reused_field = True

        sampling_started_ns = __import__("time").perf_counter_ns()
        if not reused_field and self._supports_batch_sampling():
            # Keep each provider request below the shared million-sample cap.
            azimuth_batch = max(
                1,
                min(
                    64,
                    int(self._vector_azimuth_batch),
                    DEFAULT_PERFORMANCE_BUDGET.batch_rows(40)
                    // max(1, int(n_d)),
                ),
            )
            distance_values = distances.astype(np.float64)
            for start in range(0, n_az, azimuth_batch):
                if abort_check and abort_check():
                    raise InterruptedError("Mesh build aborted")
                stop = min(n_az, start + azimuth_batch)
                x = float(obs_x) + sin_az[start:stop, None] * distance_values[None, :]
                y = float(obs_y) + cos_az[start:stop, None] * distance_values[None, :]
                sampled, sampled_valid = self._sample_provider_batch(x, y)
                elevations[:, start:stop] = sampled.T
                valid[:, start:stop] = sampled_valid.T
        elif not reused_field:
            for d_idx, d in enumerate(distances.astype(np.float64)):
                for az_idx in range(n_az):
                    x = obs_x + d * sin_az[az_idx]
                    y = obs_y + d * cos_az[az_idx]
                    h_terr = self.provider.get_elevation(x, y)
                    if h_terr is None:
                        continue
                    elevations[d_idx, az_idx] = float(h_terr)
                    valid[d_idx, az_idx] = True
        sampling_elapsed_s = (
            __import__("time").perf_counter_ns() - sampling_started_ns
        ) / 1e9

        # Bound temporary matrices by processing azimuth columns in chunks.
        generation_started_ns = __import__("time").perf_counter_ns()
        distance64 = distances.astype(np.float64)[:, None]
        mesh_column_batch = 256
        visible = np.zeros_like(valid, dtype=bool)
        for start in range(0, n_az, mesh_column_batch):
            if abort_check and abort_check():
                raise InterruptedError("Mesh build aborted")
            stop = min(n_az, start + mesh_column_batch)
            chunk_valid = valid[:, start:stop]
            computed = apparent_elevation_degrees(
                elevations[:, start:stop].astype(np.float64),
                distance64,
                float(h_eye_abs),
                float(self.R),
            ).astype(np.float32)
            chunk_altitudes = np.where(chunk_valid, computed, -90.0)
            altitudes[:, start:stop] = chunk_altitudes
            visible[:, start:stop] = self._compute_mesh_visibility(
                chunk_altitudes, chunk_valid
            )
        generation_elapsed_s = (
            __import__("time").perf_counter_ns() - generation_started_ns
        ) / 1e9
        normals_started_ns = __import__("time").perf_counter_ns()
        if n_az < 8 or mesh_delta_az_deg >= 30.0:
            normal_step_m = self._normal_sample_step_m()
            for d_idx, az_idx in np.argwhere(valid):
                d = float(distances[d_idx])
                x = obs_x + d * sin_az[az_idx]
                y = obs_y + d * cos_az[az_idx]
                h_terr = float(elevations[d_idx, az_idx])
                nx, ny, nz = self._sample_normal(
                    x, y, h_terr, normal_step_m
                )
                normal_x[d_idx, az_idx] = nx
                normal_y[d_idx, az_idx] = ny
                normal_z[d_idx, az_idx] = nz
        else:
            step_deg = mesh_delta_az_deg
            for start in range(0, n_az, mesh_column_batch):
                if abort_check and abort_check():
                    raise InterruptedError("Mesh normal calculation aborted")
                stop = min(n_az, start + mesh_column_batch)
                expanded_positions = np.arange(start - 1, stop + 1, dtype=np.int64)
                expanded_indices = np.mod(expanded_positions, n_az)
                expanded_azimuths = (
                    expanded_positions.astype(np.float64) * step_deg
                ).astype(np.float32)
                chunk_nx, chunk_ny, chunk_nz = compute_polar_mesh_normals(
                    elevations[:, expanded_indices],
                    valid[:, expanded_indices],
                    distances,
                    expanded_azimuths,
                )
                normal_x[:, start:stop] = chunk_nx[:, 1:-1]
                normal_y[:, start:stop] = chunk_ny[:, 1:-1]
                normal_z[:, start:stop] = chunk_nz[:, 1:-1]
        normals_elapsed_s = (
            __import__("time").perf_counter_ns() - normals_started_ns
        ) / 1e9

        mesh_elapsed_s = (
            __import__("time").perf_counter_ns() - mesh_started_ns
        ) / 1e9
        rss_bytes, peak_rss_bytes = process_memory_bytes()
        valid_cells = (
            valid[:-1, :-1]
            & valid[1:, :-1]
            & valid[:-1, 1:]
            & valid[1:, 1:]
        )
        near_valid = np.asarray(near_patch["near_patch_valid"], dtype=bool)
        near_valid_cells = (
            near_valid[:-1, :-1]
            & near_valid[1:, :-1]
            & near_valid[:-1, 1:]
            & near_valid[1:, 1:]
        )
        near_vertex_count = int(near_valid.size)
        mesh_metrics = {
            "elapsed_s": round(mesh_elapsed_s, 6),
            "sampling_s": round(float(sampling_elapsed_s), 6),
            "mesh_generation_s": round(float(generation_elapsed_s), 6),
            "normals_s": round(float(normals_elapsed_s), 6),
            "azimuths": int(n_az),
            "distance_rings": int(n_d),
            "samples": int(n_az * n_d + near_vertex_count),
            "vertices": int(n_az * n_d + near_vertex_count),
            "near_patch_vertices": near_vertex_count,
            "triangles": int(
                (np.count_nonzero(valid_cells) + np.count_nonzero(near_valid_cells))
                * 2
            ),
            "reused_field": bool(reused_field),
            "requested_delta_az_deg": requested_delta_az_deg,
            "delta_az_deg": mesh_delta_az_deg,
            "d_max_m": float(d_max),
            "rss_bytes": int(rss_bytes),
            "peak_rss_bytes": int(peak_rss_bytes),
        }
        self.last_mesh_metrics = mesh_metrics
        if self.performance_logging_enabled:
            append_perf_event("terrain.mesh", **mesh_metrics)

        return {
            "version": TERRAIN_MESH_VERSION,
            "azimuths": azimuths,
            "distances": distances.astype(np.float32),
            "altitudes": altitudes,
            "elevations": elevations,
            "normal_x": normal_x,
            "normal_y": normal_y,
            "normal_z": normal_z,
            "valid": valid,
            "visible": visible,
            **near_patch,
        }

    def _raycast_chunk(self, args):
        """Process a chunk of azimuths for parallel execution."""
        (
            az_indices,
            sin_az_chunk,
            cos_az_chunk,
            obs_x,
            obs_y,
            h_eye_abs,
            step_m,
            d_max,
            band_defs_simple,
            R,
        ) = args

        n_bands = len(band_defs_simple)
        n_chunk = len(az_indices)

        # Per-chunk band results: list of (angles, dists, heights) arrays
        chunk_angles = [np.full(n_chunk, -np.inf) for _ in range(n_bands)]
        chunk_dists = [np.zeros(n_chunk) for _ in range(n_bands)]
        chunk_heights = [np.zeros(n_chunk) for _ in range(n_bands)]

        for local_i in range(n_chunk):
            c = sin_az_chunk[local_i]
            s = cos_az_chunk[local_i]
            d = step_m
            miss_streak = 0
            b_idx = 0
            iterations = 0

            while d < d_max:
                iterations += 1
                if iterations > self._ray_iteration_guard:
                    break
                x = obs_x + d * c
                y = obs_y + d * s

                h_terr = self.provider.get_elevation(x, y)

                if h_terr is None:
                    miss_streak += 1
                    if miss_streak >= self._ray_miss_exit_threshold:
                        break
                    d_next = self._next_step_distance(d, step_m)
                    if not np.isfinite(d_next) or d_next <= d:
                        break
                    d = d_next
                    continue
                miss_streak = 0

                ang = float(
                    apparent_elevation_radians(h_terr, d, h_eye_abs, R)
                )

                while (
                    b_idx + 1 < n_bands and d >= band_defs_simple[b_idx][1]
                ):
                    b_idx += 1
                b_min, b_max = band_defs_simple[b_idx]
                if b_min <= d < b_max and ang > chunk_angles[b_idx][local_i]:
                    chunk_angles[b_idx][local_i] = ang
                    chunk_dists[b_idx][local_i] = d
                    chunk_heights[b_idx][local_i] = h_terr

                d_next = self._next_step_distance(d, step_m)
                if not np.isfinite(d_next) or d_next <= d:
                    break
                d = d_next

        return az_indices, chunk_angles, chunk_dists, chunk_heights

    def _bake_progressive_vectorized(
        self,
        *,
        obs_x: float,
        obs_y: float,
        obs_h_ground: float,
        h_eye_abs: float,
        step_m: float,
        d_max: float,
        delta_az_deg: float,
        azimuths: np.ndarray,
        ordered_indices: List[int],
        band_defs: List[Dict],
        bands: List[Dict],
        sin_az: np.ndarray,
        cos_az: np.ndarray,
        light_domes: np.ndarray,
        light_peak_distances: np.ndarray,
        resolved_mask: np.ndarray,
        progress_callback=None,
        preview_callback=None,
        preview_every: int = 24,
        light_sampler=None,
        abort_check=None,
    ) -> Tuple[np.ndarray, List[Dict], np.ndarray, np.ndarray, np.ndarray]:
        import time

        adaptive_enabled = bool(
            self.sampling_settings.adaptive_sampling_enabled
        )
        ray_distances = (
            build_adaptive_base_distances(d_max, self.sampling_settings)
            if adaptive_enabled
            else self._adaptive_distances(step_m, d_max)
        )
        mesh_distances = self._mesh_distance_rings(
            d_max, self._provider_nominal_resolution_m()
        ).astype(np.float64)
        sample_distances = np.unique(
            np.concatenate((ray_distances, mesh_distances))
        )
        ray_distance_indices = np.searchsorted(sample_distances, ray_distances)
        if not np.array_equal(sample_distances[ray_distance_indices], ray_distances):
            raise RuntimeError("Ray distances were not preserved in polar union")
        band_slices = [
            (
                int(np.searchsorted(ray_distances, float(definition["min"]), side="left")),
                int(np.searchsorted(ray_distances, float(definition["max"]), side="left")),
            )
            for definition in band_defs
        ]

        mesh_distance_indices = np.searchsorted(sample_distances, mesh_distances)
        if not np.array_equal(
            sample_distances[mesh_distance_indices], mesh_distances
        ):
            raise RuntimeError("Mesh distances were not preserved in polar union")
        mesh_delta_az_deg = self._mesh_azimuth_step(delta_az_deg)
        mesh_azimuths = np.arange(
            0.0, 360.0, mesh_delta_az_deg, dtype=np.float32
        )
        mesh_source_indices = np.rint(
            mesh_azimuths.astype(np.float64) / float(delta_az_deg)
        ).astype(np.int64)
        mesh_source_indices = np.clip(
            mesh_source_indices, 0, max(0, azimuths.size - 1)
        )
        source_to_mesh = np.full(azimuths.size, -1, dtype=np.int32)
        source_to_mesh[mesh_source_indices] = np.arange(
            mesh_source_indices.size, dtype=np.int32
        )
        # Retain only the screen-useful azimuth columns of the comparatively
        # small mesh ring field. Ray samples keep their requested precision.
        field_elevations = np.zeros(
            (mesh_distances.size, mesh_azimuths.size), dtype=np.float32
        )
        field_valid = np.zeros(field_elevations.shape, dtype=bool)
        io_before = self._raster_io_metrics()
        requested_backend = self._requested_raycast_backend()
        effective_backend = "single_vectorized"
        t0 = time.time()
        completed = 0
        reduction_ns = 0
        snapshot_ns = 0
        preview_count = 0
        sampled_valid_count = 0
        phase_totals_ns = {
            "coordinates": 0,
            "provider": 0,
            "angles": 0,
            "bands": 0,
            "light": 0,
            "refinement": 0,
        }
        refinement_queries = 0
        refinement_valid_queries = 0
        selected_profile_samples = (
            0 if adaptive_enabled else int(azimuths.size * ray_distances.size)
        )
        maximum_refinement_errors = np.zeros(3, dtype=np.float64)
        rows_per_batch = DEFAULT_PERFORMANCE_BUDGET.batch_rows(40)
        batch_size = max(
            1,
            min(
                64,
                int(self._vector_azimuth_batch),
                rows_per_batch // max(1, int(sample_distances.size)),
            ),
        )
        chunk_start = 0
        first_batch_size = (
            min(batch_size, max(1, int(preview_every)))
            if preview_callback is not None
            else batch_size
        )
        while chunk_start < len(ordered_indices):
            if abort_check and abort_check():
                raise InterruptedError("Bake aborted")
            current_batch_size = (
                first_batch_size if chunk_start == 0 else batch_size
            )
            chunk_indices = np.asarray(
                ordered_indices[chunk_start : chunk_start + current_batch_size],
                dtype=np.int32,
            )
            chunk_metrics_before = self._raster_io_metrics()
            chunk_t0 = time.perf_counter_ns()
            result = self._sample_azimuth_chunk_vectorized(
                az_indices=chunk_indices,
                obs_x=obs_x,
                obs_y=obs_y,
                h_eye_abs=h_eye_abs,
                ray_distances=ray_distances,
                sample_distances=sample_distances,
                ray_distance_indices=ray_distance_indices,
                band_defs=band_defs,
                band_slices=band_slices,
                sin_az=sin_az,
                cos_az=cos_az,
                light_sampler=light_sampler,
            )
            if adaptive_enabled:
                refined_bands, refinement_metrics = self._refine_azimuth_chunk(
                    az_indices=chunk_indices,
                    obs_x=obs_x,
                    obs_y=obs_y,
                    h_eye_abs=h_eye_abs,
                    base_distances=ray_distances,
                    base_elevations=result["elevations"][:, ray_distance_indices],
                    base_valid=result["valid"][:, ray_distance_indices],
                    band_defs=band_defs,
                    sin_az=sin_az,
                    cos_az=cos_az,
                )
                result["bands"] = refined_bands
                phase_totals_ns["refinement"] += int(
                    refinement_metrics["refinement_ns"]
                )
                refinement_queries += int(
                    refinement_metrics["refinement_queries"]
                )
                refinement_valid_queries += int(
                    refinement_metrics["refinement_valid_queries"]
                )
                selected_profile_samples += int(
                    refinement_metrics["selected_samples"]
                )
                maximum_refinement_errors = np.maximum(
                    maximum_refinement_errors,
                    (
                        refinement_metrics["maximum_elevation_error_m"],
                        refinement_metrics["maximum_projected_error_px"],
                        refinement_metrics["maximum_slope_delta_deg"],
                    ),
                )
            chunk_elapsed_ns = time.perf_counter_ns() - chunk_t0
            chunk_metrics_after = self._raster_io_metrics()
            provider_ns = (
                chunk_metrics_after["selection_ns"]
                - chunk_metrics_before["selection_ns"]
                + chunk_metrics_after["interpolation_ns"]
                - chunk_metrics_before["interpolation_ns"]
            )
            reduction_ns += max(0, int(chunk_elapsed_ns) - int(provider_ns))
            for phase_name, phase_ns in result.get("timings_ns", {}).items():
                phase_totals_ns[phase_name] += int(phase_ns)
            sampled_valid_count += int(np.count_nonzero(result["valid"]))
            mesh_positions = source_to_mesh[chunk_indices]
            retained_rows = np.flatnonzero(mesh_positions >= 0)
            if retained_rows.size:
                retained_columns = mesh_positions[retained_rows]
                field_elevations[:, retained_columns] = np.asarray(
                    result["elevations"][retained_rows][
                        :, mesh_distance_indices
                    ],
                    dtype=np.float32,
                ).T
                field_valid[:, retained_columns] = np.asarray(
                    result["valid"][retained_rows][:, mesh_distance_indices],
                    dtype=bool,
                ).T

            # Commit in the requested priority order so previews never expose
            # unresolved azimuth values from the remainder of a matrix batch.
            for local_index, azimuth_index in enumerate(chunk_indices):
                if abort_check and abort_check():
                    raise InterruptedError("Bake aborted")
                for band_index, band_result in enumerate(result["bands"]):
                    target = bands[band_index]
                    for key in (
                        "angles",
                        "dists",
                        "heights",
                        "surface_angles",
                        "surface_dists",
                        "surface_heights",
                    ):
                        target[key][azimuth_index] = band_result[key][local_index]
                light_domes[azimuth_index] = result["light_domes"][local_index]
                light_peak_distances[azimuth_index] = result[
                    "light_peak_distances"
                ][local_index]
                resolved_mask[azimuth_index] = True
                completed += 1
            if progress_callback:
                progress_pct = (completed / len(azimuths)) * 100.0
                progress_callback(
                    progress_pct, f"Azimuth {completed}/{len(azimuths)}"
                )
            should_preview = bool(preview_callback) and (
                completed >= len(azimuths)
                or completed
                >= min(
                    len(azimuths),
                    max(1, preview_every) * (preview_count + 1),
                )
            )
            if should_preview:
                snapshot_t0 = time.perf_counter_ns()
                preview_callback(
                    completed,
                    len(azimuths),
                    azimuths,
                    bands,
                    light_domes,
                    light_peak_distances,
                    resolved_mask,
                )
                snapshot_ns += time.perf_counter_ns() - snapshot_t0
                preview_count += 1
            chunk_start += len(chunk_indices)

        self._last_polar_field = PolarElevationField(
            azimuths=mesh_azimuths,
            distances=mesh_distances.astype(np.float32),
            elevations=field_elevations,
            valid=field_valid,
            observer_x=float(obs_x),
            observer_y=float(obs_y),
            observer_ground=float(obs_h_ground),
            d_max=float(d_max),
            delta_az_deg=mesh_delta_az_deg,
        )
        print(
            "[HorizonEngine] Vectorized progressive bake complete in "
            f"{time.time() - t0:.2f}s."
        )
        elapsed = time.time() - t0
        io_after = self._raster_io_metrics()
        rss_bytes, peak_rss_bytes = process_memory_bytes()
        sampling_metrics = dict(
            backend=effective_backend,
            requested_backend=requested_backend,
            elapsed_s=round(float(elapsed), 6),
            azimuths=int(azimuths.size),
            ray_distances=int(ray_distances.size),
            polar_distances=int(sample_distances.size),
            retained_mesh_distances=int(mesh_distances.size),
            retained_mesh_azimuths=int(mesh_azimuths.size),
            samples=int(
                azimuths.size * sample_distances.size + refinement_queries
            ),
            base_profile_samples=int(azimuths.size * ray_distances.size),
            refinement_queries=int(refinement_queries),
            refinement_valid_queries=int(refinement_valid_queries),
            selected_profile_samples=int(selected_profile_samples),
            adaptive_sampling_enabled=adaptive_enabled,
            valid_samples=int(sampled_valid_count),
            retained_valid_samples=int(np.count_nonzero(field_valid)),
            step_m=float(step_m),
            d_max_m=float(d_max),
            bytes_read=int(io_after["bytes_read"] - io_before["bytes_read"]),
            cache_hits=int(io_after["cache_hits"] - io_before["cache_hits"]),
            cache_misses=int(io_after["cache_misses"] - io_before["cache_misses"]),
            selection_s=round(
                (io_after["selection_ns"] - io_before["selection_ns"]) / 1e9, 6
            ),
            interpolation_s=round(
                (io_after["interpolation_ns"] - io_before["interpolation_ns"]) / 1e9, 6
            ),
            reduction_s=round(reduction_ns / 1e9, 6),
            snapshots_s=round(snapshot_ns / 1e9, 6),
            preview_count=int(preview_count),
            bands=int(len(band_defs)),
            coordinate_s=round(phase_totals_ns["coordinates"] / 1e9, 6),
            provider_s=round(phase_totals_ns["provider"] / 1e9, 6),
            angle_s=round(phase_totals_ns["angles"] / 1e9, 6),
            band_reduction_s=round(phase_totals_ns["bands"] / 1e9, 6),
            light_pollution_s=round(phase_totals_ns["light"] / 1e9, 6),
            refinement_s=round(phase_totals_ns["refinement"] / 1e9, 6),
            maximum_elevation_error_m=round(
                float(maximum_refinement_errors[0]), 6
            ),
            maximum_projected_error_px=round(
                float(maximum_refinement_errors[1]), 6
            ),
            maximum_slope_delta_deg=round(
                float(maximum_refinement_errors[2]), 6
            ),
            candidate_tiles=int(
                io_after["candidate_tiles"] - io_before["candidate_tiles"]
            ),
            loaded_tiles=int(io_after["loaded_tiles"] - io_before["loaded_tiles"]),
            sampled_points=int(
                io_after["sampled_points"] - io_before["sampled_points"]
            ),
            rss_bytes=int(rss_bytes),
            peak_rss_bytes=int(peak_rss_bytes),
        )
        self.last_sampling_metrics = sampling_metrics
        if self.performance_logging_enabled:
            append_perf_event("terrain.raycast", **sampling_metrics)
        return (
            azimuths,
            bands,
            light_domes,
            light_peak_distances,
            resolved_mask,
        )

    def bake_progressive(
        self,
        obs_x: float,
        obs_y: float,
        obs_h_ground: Optional[float] = None,
        step_m: float = 50,
        d_max: Optional[float] = None,
        delta_az_deg: float = 0.5,
        band_defs: Optional[List[Dict]] = None,
        azimuth_order: Optional[List[int]] = None,
        progress_callback=None,
        preview_callback=None,
        preview_every: int = 24,
        light_sampler=None,
        abort_check=None,
    ) -> Tuple[np.ndarray, List[Dict], np.ndarray, np.ndarray, np.ndarray]:
        """
        Compute a horizon profile incrementally.

        This progressive path is intended for subprocess baking and live previews:
        the worker can request previews after useful azimuth blocks without waiting
        for the full 360° solve to finish.
        """
        import time
        if d_max is None:
            raise ValueError("d_max must be the resolved visibility radius")

        if obs_h_ground is None:
            val = self.provider.get_elevation(obs_x, obs_y)
            if val is None:
                print(
                    "[HorizonEngine] Observer outside DEM coverage. Using 0."
                )
                obs_h_ground = 0
            else:
                obs_h_ground = val

        h_eye_abs = obs_h_ground + self.eye_height
        azimuths = np.arange(0, 360, delta_az_deg, dtype=np.float32)
        n_az = len(azimuths)
        light_domes = np.zeros(n_az, dtype=np.float32)
        light_peak_distances = np.zeros(n_az, dtype=np.float32)
        max_rad_per_az = np.zeros(n_az, dtype=np.float32)
        resolved_mask = np.zeros(n_az, dtype=bool)

        if band_defs is None:
            band_defs = generate_bands(20, max_dist_m=d_max)

        bands = self._build_band_buffers(n_az, band_defs)

        az_rads = np.deg2rad(
            azimuths.astype(np.float64) - self.grid_convergence_deg
        )
        sin_az = np.sin(az_rads)
        cos_az = np.cos(az_rads)

        if azimuth_order is None:
            ordered_indices = list(range(n_az))
        else:
            ordered_indices = []
            seen = set()
            for idx in azimuth_order:
                try:
                    idx_int = int(idx)
                except Exception:
                    continue
                if 0 <= idx_int < n_az and idx_int not in seen:
                    ordered_indices.append(idx_int)
                    seen.add(idx_int)
            if len(ordered_indices) < n_az:
                ordered_indices.extend(i for i in range(n_az) if i not in seen)

        print(
            f"[HorizonEngine] Progressive bake {n_az} azimuths, max_dist={d_max / 1000:.0f}km..."
        )
        if (
            self._supports_batch_sampling()
            or self.sampling_settings.adaptive_sampling_enabled
        ):
            return self._bake_progressive_vectorized(
                obs_x=obs_x,
                obs_y=obs_y,
                obs_h_ground=float(obs_h_ground),
                h_eye_abs=float(h_eye_abs),
                step_m=step_m,
                d_max=d_max,
                delta_az_deg=delta_az_deg,
                azimuths=azimuths,
                ordered_indices=ordered_indices,
                band_defs=band_defs,
                bands=bands,
                sin_az=sin_az,
                cos_az=cos_az,
                light_domes=light_domes,
                light_peak_distances=light_peak_distances,
                resolved_mask=resolved_mask,
                progress_callback=progress_callback,
                preview_callback=preview_callback,
                preview_every=preview_every,
                light_sampler=light_sampler,
                abort_check=abort_check,
            )
        t0 = time.time()
        last_preview_t = t0
        completed = 0

        for az_index in ordered_indices:
            if abort_check and abort_check():
                print("[HorizonEngine] Progressive bake aborted by caller.")
                raise InterruptedError("Bake aborted")

            self._sample_single_azimuth(
                az_index=az_index,
                obs_x=obs_x,
                obs_y=obs_y,
                h_eye_abs=h_eye_abs,
                step_m=step_m,
                d_max=d_max,
                bands=bands,
                sin_az=sin_az,
                cos_az=cos_az,
                light_domes=light_domes,
                light_peak_distances=light_peak_distances,
                max_rad_per_az=max_rad_per_az,
                light_sampler=light_sampler,
            )
            resolved_mask[az_index] = True
            completed += 1

            progress_pct = (completed / n_az) * 100.0
            if progress_callback:
                progress_callback(progress_pct, f"Azimuth {completed}/{n_az}")

            if preview_callback:
                now = time.time()
                enough_samples = completed >= preview_every and (
                    completed % max(1, preview_every) == 0
                )
                enough_time = (now - last_preview_t) >= 0.35
                if completed == n_az or enough_samples or enough_time:
                    preview_callback(
                        completed,
                        n_az,
                        azimuths,
                        bands,
                        light_domes,
                        light_peak_distances,
                        resolved_mask,
                    )
                    last_preview_t = now

        elapsed = time.time() - t0
        print(f"[HorizonEngine] Progressive bake complete in {elapsed:.2f}s.")
        return (
            azimuths,
            bands,
            light_domes,
            light_peak_distances,
            resolved_mask,
        )

    def bake(
        self,
        obs_x: float,
        obs_y: float,
        obs_h_ground: Optional[float] = None,
        step_m: float = 50,
        d_max: Optional[float] = None,
        delta_az_deg: float = 0.5,
        band_defs: Optional[List[Dict]] = None,
        progress_callback=None,
        light_sampler=None,
        abort_check=None,
    ) -> Tuple[np.ndarray, List[Dict], np.ndarray]:
        """
        Compute multi-band horizon profile (sequential — CPU-bound under GIL).

        Args:
            progress_callback: Optional callable(percent: int, msg: str).

        Returns (azimuths, bands) where bands is a list of dicts
        each containing 'id', 'angles', 'dists', 'heights' arrays.
        """
        azimuths, bands, light_domes, light_peak_distances, _resolved = (
            self.bake_progressive(
                obs_x=obs_x,
                obs_y=obs_y,
                obs_h_ground=obs_h_ground,
                step_m=step_m,
                d_max=d_max,
                delta_az_deg=delta_az_deg,
                band_defs=band_defs,
                progress_callback=progress_callback,
                light_sampler=light_sampler,
                abort_check=abort_check,
            )
        )
        return azimuths, bands, light_domes, light_peak_distances

# ─────────────────────────────────────────────
#  Convenience functions
# ─────────────────────────────────────────────


def bake_and_save(
    lat: float,
    lon: float,
    tiles_dir: str,
    output_path: str,
    radius: Optional[float] = None,
    step_m: float = 50,
    resolution_deg: float = 0.5,
    eye_height: float = 1.7,
    band_defs: Optional[List[Dict]] = None,
):
    """
    Full pipeline: transform coords, index tiles, bake horizon, save .npz.
    """
    if radius is None:
        raise ValueError("radius must be a resolved visibility radius")
    from TerraLab.terrain.crs import DEFAULT_TRANSFORM_SERVICE

    # Transform observer from geographic to terrain internal coordinates.
    x_utm, y_utm = DEFAULT_TRANSFORM_SERVICE.transform_xy(
        lon,
        lat,
        CRS_GEOGRAPHIC,
        CRS_TERRAIN_INTERNAL,
    )
    print(f"[HorizonEngine] Observer UTM: {x_utm:.2f}, {y_utm:.2f}")

    # Build system
    idx = TileIndex(tiles_dir)
    cache = TileCache(capacity=100)
    sampler = DemSampler(idx, cache)
    baker = HorizonBaker(sampler, eye_height=eye_height)

    # Sample observer altitude
    ground_h = sampler.sample(x_utm, y_utm)
    if ground_h is None:
        print(
            "[HorizonEngine] Observer outside DEM. Using fallback 200m (Lleida plains)."
        )
        ground_h = 200.0
    else:
        print(f"[HorizonEngine] Observer altitude from DEM: {ground_h:.2f}m")

    # Bake
    azimuths, bands, light_domes, light_peak_distances = baker.bake(
        obs_x=x_utm,
        obs_y=y_utm,
        obs_h_ground=ground_h,
        step_m=step_m,
        d_max=radius,
        delta_az_deg=resolution_deg,
        band_defs=band_defs,
    )

    # Build & save profile
    profile = HorizonProfile(
        azimuths=azimuths,
        bands=bands,
        observer_lat=lat,
        observer_lon=lon,
        light_domes=light_domes,
        light_peak_distances=light_peak_distances,
        resolved_radius_m=float(radius),
    )
    save_profile(profile, output_path)
    print(f"[HorizonEngine] Profile saved to {output_path}")
    return profile
