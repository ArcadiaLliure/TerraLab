"""Cancellable surface sampling application service."""

from __future__ import annotations

import hashlib
import inspect
import math
import os
import threading
import time
from typing import Any, Mapping, Sequence

import numpy as np

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.cache import ByteLRU
from TerraLab.common.perf_events import append_perf_event
from TerraLab.common.performance.budget import DEFAULT_PERFORMANCE_BUDGET
from TerraLab.common.performance.flags import PERFORMANCE_FLAGS
from TerraLab.common.performance.memory import process_memory_bytes
from TerraLab.terrain.crs import (
    CRS_GEOGRAPHIC,
    CRS_TERRAIN_INTERNAL,
    DEFAULT_TRANSFORM_SERVICE,
    CoordinateTransformService,
    normalize_crs,
)
from TerraLab.terrain.surface.cache import (
    SurfaceCacheKey,
    SurfaceSampleCache,
    SurfaceSamplingRequest,
    _surface_cache_from_payload,
    _surface_cache_payload,
    _surface_cache_size,
    geometry_fingerprint,
)
from TerraLab.terrain.surface.categorical import CategoricalSurfaceProvider
from TerraLab.terrain.surface.common import (
    SAMPLE_ORIGIN_EXACT,
    SAMPLE_ORIGIN_SOURCE_FALLBACK,
    SAMPLE_ORIGIN_UNKNOWN,
    SURFACE_CACHE_POLICY_VERSION,
    RgbaSampleBatch,
    SurfaceProvider,
    _normalized_lod_factors,
)
from TerraLab.terrain.surface.geometry import (
    _interpolate_polar_grid,
    _lod_factors_for_polar_grid,
    _sample_indices,
    _subdivided_axis,
    _visible_azimuth_indices,
)
from TerraLab.terrain.surface.rgb import RgbSurfaceProvider
from TerraLab.terrain.surface_store import AtomicNpzStore

class SurfaceSamplingService:
    """Pre-sample real surface colors for PROFILE and RELIEF geometry."""

    def __init__(
        self,
        providers: Sequence[SurfaceProvider],
        *,
        internal_crs: str = CRS_TERRAIN_INTERNAL,
        transform_service: CoordinateTransformService | None = None,
        max_profile_samples: int = 120_000,
        max_relief_samples: int = 300_000,
        cache_capacity: int = 4,
        cache_bytes: int | None = None,
        persistent_cache_dir: str | os.PathLike[str] | None = None,
        persistent_cache_bytes: int = 2 * 1024**3,
        performance_logging: bool = False,
    ) -> None:
        self.providers = [
            provider for provider in providers if isinstance(provider, (RgbSurfaceProvider, CategoricalSurfaceProvider))
        ]
        self.internal_crs = normalize_crs(internal_crs)
        self.transform_service = transform_service or DEFAULT_TRANSFORM_SERVICE
        self.max_profile_samples = max(1, int(max_profile_samples))
        self.max_relief_samples = max(1, int(max_relief_samples))
        # Kept as a compatibility attribute for callers that still expose the
        # old setting.  Eviction is deliberately governed by bytes, not items.
        self.cache_capacity = max(1, int(cache_capacity))
        if cache_bytes is None:
            cache_bytes = DEFAULT_PERFORMANCE_BUDGET.transient_bytes // 2
        self.cache_bytes = max(0, int(cache_bytes))
        self._cache: ByteLRU[str, SurfaceSampleCache] = ByteLRU(self.cache_bytes)
        self._cache_lock = threading.RLock()
        self._persistent_store = (
            AtomicNpzStore(
                persistent_cache_dir, budget_bytes=persistent_cache_bytes
            )
            if persistent_cache_dir is not None
            else None
        )
        self.performance_logging = bool(performance_logging)
        for provider in self.providers:
            if isinstance(provider, CategoricalSurfaceProvider):
                provider.tile_store = self._persistent_store

    @property
    def source_fingerprints(self) -> tuple[str, ...]:
        return tuple(provider.fingerprint for provider in self.providers)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(provider.source_id or provider.fingerprint for provider in self.providers)

    @property
    def source_names(self) -> tuple[str, ...]:
        return tuple(
            provider.source_name or provider.source_id or provider.fingerprint
            for provider in self.providers
        )

    @property
    def source_legend_ids(self) -> tuple[str, ...]:
        return tuple(
            str(getattr(provider, "legend_id", "") or "").strip().lower()
            for provider in self.providers
        )

    def close(self) -> None:
        with self._cache_lock:
            self._cache.clear()
        for provider in self.providers:
            provider.close()

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def _nominal_surface_resolution(self) -> float | None:
        values = [
            float(metadata.resolution_m)
            for provider in self.providers
            for metadata in provider.metadata
            if metadata.resolution_m is not None
            and math.isfinite(float(metadata.resolution_m))
            and float(metadata.resolution_m) > 0.0
        ]
        return min(values) if values else None

    def _visual_subdivision(
        self,
        distances: np.ndarray,
        azimuths: np.ndarray,
        request: SurfaceSamplingRequest | None = None,
    ) -> tuple[int, int]:
        resolution = self._nominal_surface_resolution()
        if resolution is None or len(distances) < 2 or len(azimuths) < 2:
            return 1, 1
        radial_steps = np.diff(distances)
        radial_steps = radial_steps[np.isfinite(radial_steps) & (radial_steps > 0)]
        azimuth_steps = np.diff(azimuths)
        azimuth_steps = azimuth_steps[
            np.isfinite(azimuth_steps) & (azimuth_steps > 0)
        ]
        radial_spacing = float(np.median(radial_steps)) if radial_steps.size else resolution
        angular_spacing = (
            float(distances[-1])
            * math.radians(float(np.median(azimuth_steps)))
            if azimuth_steps.size
            else resolution
        )
        radial_factor = min(4, max(1, int(math.ceil(radial_spacing / resolution))))
        angular_factor = min(4, max(1, int(math.ceil(angular_spacing / resolution))))
        categorical = any(
            isinstance(provider, CategoricalSurfaceProvider)
            for provider in self.providers
        )
        if (
            categorical
            and request is not None
            and request.viewport_width_px
            and request.view_fov_deg > 0.0
        ):
            pixels_per_radian = float(request.viewport_width_px) / math.radians(
                max(1e-3, request.view_fov_deg)
            )
            midpoints = np.maximum(
                resolution,
                (
                    np.asarray(distances[:-1], dtype=np.float64)
                    + np.asarray(distances[1:], dtype=np.float64)
                )
                * 0.5,
            )
            radial_edges_px = (
                np.diff(np.asarray(distances, dtype=np.float64))
                / midpoints
                * pixels_per_radian
            )
            radial_factor = max(
                radial_factor,
                int(
                    math.ceil(
                        float(np.max(radial_edges_px, initial=0.0)) / 8.0
                    )
                ),
            )
            angular_step_rad = (
                math.radians(float(np.median(azimuth_steps)))
                if azimuth_steps.size
                else 0.0
            )
            angular_factor = max(
                angular_factor,
                int(math.ceil(angular_step_rad * pixels_per_radian / 8.0)),
            )
            radial_factor = min(32, radial_factor)
            angular_factor = min(32, angular_factor)
        while (
            ((len(distances) - 1) * radial_factor + 1)
            * (len(azimuths) * angular_factor)
            > self.max_relief_samples
            and (radial_factor > 1 or angular_factor > 1)
        ):
            if angular_factor >= radial_factor and angular_factor > 1:
                angular_factor -= 1
            elif radial_factor > 1:
                radial_factor -= 1
        return radial_factor, angular_factor

    def _adaptive_visual_axes(
        self,
        distances: np.ndarray,
        azimuths: np.ndarray,
        request: SurfaceSamplingRequest,
    ) -> tuple[np.ndarray, np.ndarray, bool]:
        """Build a budgeted screen-space grid for categorical materials."""

        radial_factor, angular_factor = self._visual_subdivision(
            distances, azimuths, request
        )
        categorical = any(
            isinstance(provider, CategoricalSurfaceProvider)
            for provider in self.providers
        )
        if (
            not categorical
            or not request.viewport_width_px
            or request.view_fov_deg <= 0.0
        ):
            return (
                _subdivided_axis(distances, radial_factor),
                _subdivided_axis(azimuths, angular_factor, circular=True),
                False,
            )

        resolution = float(self._nominal_surface_resolution() or 1.0)
        pixels_per_radian = float(request.viewport_width_px) / math.radians(
            max(1e-3, request.view_fov_deg)
        )
        distances64 = np.asarray(distances, dtype=np.float64)
        midpoints = np.maximum(
            resolution, (distances64[:-1] + distances64[1:]) * 0.5
        )
        projected = (
            np.diff(distances64) / midpoints * pixels_per_radian
        )
        desired = np.clip(np.ceil(projected / 8.0), 1, 32).astype(np.int32)
        angular_count = max(1, len(azimuths) * angular_factor)
        maximum_radial_points = max(
            2, self.max_relief_samples // angular_count
        )
        permitted_intervals = max(
            len(desired), maximum_radial_points - 1
        )
        factors = np.ones(len(desired), dtype=np.int32)
        extra = max(0, permitted_intervals - len(desired))
        # Allocate scarce subdivisions to the edges with the greatest current
        # screen-space excess.  Dividing by distance gives near-field ties a
        # stable priority without increasing the global vertex count.
        while extra > 0 and np.any(factors < desired):
            score = np.where(
                factors < desired,
                projected / factors / np.maximum(midpoints, resolution),
                -np.inf,
            )
            chosen = int(np.argmax(score))
            factors[chosen] += 1
            extra -= 1
        budget_limited = bool(np.any(factors < desired))
        radial_parts = [
            np.linspace(
                distances64[index],
                distances64[index + 1],
                int(factors[index]),
                endpoint=False,
            )
            for index in range(len(factors))
        ]
        radial_axis = np.concatenate(
            (*radial_parts, distances64[-1:])
        ).astype(np.float32)
        angular_axis = _subdivided_axis(
            azimuths, angular_factor, circular=True
        )
        return radial_axis, angular_axis, budget_limited

    def sample_rgba_points(
        self,
        x: Any,
        y: Any,
        *,
        input_crs: str,
        lod_factors: Any = None,
        progress_callback=None,
        abort_check=None,
    ) -> RgbaSampleBatch:
        x_arr, y_arr = np.broadcast_arrays(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
        requested_shape = x_arr.shape
        dedupe_inverse = None
        lod_arr = None
        if lod_factors is not None:
            lod_arr = np.broadcast_to(
                np.asarray(lod_factors, dtype=np.int16), requested_shape
            )
        if (
            lod_arr is not None
            and PERFORMANCE_FLAGS.surface_lod_cache
            and self.providers
            and all(isinstance(provider, CategoricalSurfaceProvider) for provider in self.providers)
        ):
            flat_x = x_arr.ravel()
            flat_y = y_arr.ravel()
            flat_lod = lod_arr.ravel()
            coordinates = np.empty(
                flat_x.size,
                dtype=[("x", np.float64), ("y", np.float64), ("lod", np.int16)],
            )
            coordinates["x"] = flat_x
            coordinates["y"] = flat_y
            coordinates["lod"] = flat_lod
            _unique, unique_indices, dedupe_inverse = np.unique(
                coordinates, return_index=True, return_inverse=True
            )
            x_arr = flat_x[unique_indices]
            y_arr = flat_y[unique_indices]
            lod_arr = flat_lod[unique_indices]
        rgba = np.zeros(x_arr.shape + (4,), dtype=np.uint8)
        valid = np.zeros(x_arr.shape, dtype=bool)
        provenance = np.full(x_arr.shape, -1, dtype=np.int16)
        class_ids = np.full(x_arr.shape, -1, dtype=np.int64)
        categorical = np.zeros(x_arr.shape, dtype=bool)
        raster_rows = np.full(x_arr.shape, -1, dtype=np.int64)
        raster_columns = np.full(x_arr.shape, -1, dtype=np.int64)
        sampled_lod = np.ones(x_arr.shape, dtype=np.int16)
        sample_origins = np.zeros(x_arr.shape, dtype=np.uint8)

        # LayerSelectionService orders automatic chains as categorical then
        # RGB. Preserve its order so a manual RGB choice remains first while
        # other categorical sources can still fill nodata.
        ordered = list(self.providers)
        provider_to_index = {id(provider): index for index, provider in enumerate(self.providers)}
        provider_count = max(1, len(ordered))
        for provider_position, provider in enumerate(ordered):
            if callable(abort_check) and abort_check():
                from TerraLab.terrain.providers import RasterSamplingCancelled

                raise RasterSamplingCancelled("Surface sampling cancelled")
            if np.all(valid):
                break
            def provider_progress(fraction, message, *, _position=provider_position):
                if callable(progress_callback):
                    progress_callback(
                        (_position + max(0.0, min(1.0, float(fraction))))
                        / provider_count,
                        message,
                    )

            if isinstance(provider, RgbSurfaceProvider):
                sample_method = provider.sample_rgba
                sample_kwargs = {"input_crs": input_crs}
                if "progress_callback" in inspect.signature(sample_method).parameters:
                    sample_kwargs.update(
                        progress_callback=provider_progress,
                        abort_check=abort_check,
                    )
                batch = sample_method(x_arr, y_arr, **sample_kwargs)
                candidate_rgba = batch.rgba
                candidate_valid = batch.valid
            else:
                sample_method = provider.sample_classes
                sample_kwargs = {"input_crs": input_crs}
                if lod_arr is not None and "lod_factors" in inspect.signature(sample_method).parameters:
                    sample_kwargs["lod_factors"] = lod_arr
                if "progress_callback" in inspect.signature(sample_method).parameters:
                    sample_kwargs.update(
                        progress_callback=provider_progress,
                        abort_check=abort_check,
                    )
                classes = sample_method(x_arr, y_arr, **sample_kwargs)
                mapped = provider.classes_to_rgba(
                    classes.classes,
                    x=x_arr,
                    y=y_arr,
                )
                candidate_rgba = mapped.rgba
                candidate_valid = classes.valid & mapped.valid
            take = ~valid & candidate_valid
            rgba[take] = candidate_rgba[take]
            valid[take] = True
            provenance[take] = int(provider_to_index[id(provider)])
            provider_rows = getattr(batch if isinstance(provider, RgbSurfaceProvider) else classes, "raster_rows", None)
            provider_columns = getattr(batch if isinstance(provider, RgbSurfaceProvider) else classes, "raster_columns", None)
            provider_lod = getattr(batch if isinstance(provider, RgbSurfaceProvider) else classes, "lod_factors", None)
            provider_origins = getattr(batch if isinstance(provider, RgbSurfaceProvider) else classes, "sample_origins", None)
            if provider_rows is not None:
                raster_rows[take] = np.asarray(provider_rows)[take]
            if provider_columns is not None:
                raster_columns[take] = np.asarray(provider_columns)[take]
            if provider_lod is not None:
                sampled_lod[take] = np.asarray(provider_lod)[take]
            elif lod_arr is not None:
                sampled_lod[take] = _normalized_lod_factors(lod_arr)[take]
            if provider_position > 0:
                sample_origins[take] = SAMPLE_ORIGIN_SOURCE_FALLBACK
            elif provider_origins is not None:
                sample_origins[take] = np.asarray(provider_origins)[take]
            else:
                sample_origins[take] = SAMPLE_ORIGIN_EXACT
            if isinstance(provider, CategoricalSurfaceProvider):
                class_ids[take] = classes.classes[take]
                categorical[take] = True
            provider_progress(1.0, "sampling-provider")
        if callable(progress_callback):
            progress_callback(1.0, "sampling-complete")
        if dedupe_inverse is not None:
            return RgbaSampleBatch(
                rgba[dedupe_inverse].reshape(requested_shape + (4,)),
                valid[dedupe_inverse].reshape(requested_shape),
                provenance[dedupe_inverse].reshape(requested_shape),
                class_ids[dedupe_inverse].reshape(requested_shape),
                categorical[dedupe_inverse].reshape(requested_shape),
                raster_rows[dedupe_inverse].reshape(requested_shape),
                raster_columns[dedupe_inverse].reshape(requested_shape),
                sampled_lod[dedupe_inverse].reshape(requested_shape),
                sample_origins[dedupe_inverse].reshape(requested_shape),
            )
        return RgbaSampleBatch(
            rgba,
            valid,
            provenance,
            class_ids,
            categorical,
            raster_rows,
            raster_columns,
            sampled_lod,
            sample_origins,
        )

    def _observer_xy(self, profile: Any, geometry_crs: str) -> tuple[float, float]:
        observer_x = getattr(profile, "observer_x", None)
        observer_y = getattr(profile, "observer_y", None)
        if observer_x is not None and observer_y is not None:
            try:
                if math.isfinite(float(observer_x)) and math.isfinite(float(observer_y)):
                    return float(observer_x), float(observer_y)
            except Exception:
                log_suppressed_exception(__name__, "SurfaceSamplingService._observer_xy")
        lon = float(getattr(profile, "observer_lon", 0.0))
        lat = float(getattr(profile, "observer_lat", 0.0))
        x, y = self.transform_service.transform_xy(lon, lat, CRS_GEOGRAPHIC, geometry_crs)
        return float(x), float(y)

    def sample_profile(
        self,
        profile: Any,
        *,
        geometry_id: str | None = None,
        request: SurfaceSamplingRequest | None = None,
        progress_callback=None,
        abort_check=None,
    ) -> SurfaceSampleCache:
        start = time.perf_counter()
        rss_start, peak_start = process_memory_bytes()
        if request is not None:
            profile = request.profile
        else:
            request = SurfaceSamplingRequest(profile=profile)
        metric_datasets = [
            dataset
            for provider in self.providers
            if isinstance(provider, CategoricalSurfaceProvider)
            for dataset in provider._datasets
        ]
        categorical_lod = any(
            isinstance(provider, CategoricalSurfaceProvider)
            for provider in self.providers
        )
        metric_baseline = {
            id(dataset): (
                int(dataset.bytes_read),
                int(dataset.lod_rows_read),
                int(dataset.lod_intervals_read),
                int(dataset.lod_windows_read),
                int(dataset.lod_pixels_decoded),
                int(dataset.lod_modal_cells),
                int(dataset.lod_requested),
                int(dataset.lod_unique),
                int(dataset.lod_cache_hits),
            )
            for dataset in metric_datasets
        }
        visible_radius = request.visible_radius_m
        if visible_radius is None:
            try:
                candidate_radius = float(getattr(profile, "resolved_radius_m", 0.0) or 0.0)
                visible_radius = candidate_radius if candidate_radius > 0.0 else None
            except (TypeError, ValueError):
                visible_radius = None

        def report(percent: float, phase: str) -> None:
            if callable(progress_callback):
                progress_callback(
                    max(0.0, min(100.0, float(percent))), str(phase)
                )

        def check_cancelled() -> None:
            if callable(abort_check) and abort_check():
                from TerraLab.terrain.providers import RasterSamplingCancelled

                raise RasterSamplingCancelled("Surface sampling cancelled")

        report(0.0, "preparing")
        check_cancelled()
        geometry_crs = normalize_crs(
            getattr(profile, "geometry_crs", None) or self.internal_crs
        )
        profile_geometry_key = geometry_fingerprint(profile, geometry_crs=geometry_crs)
        profile_geometry_id = str(getattr(profile, "geometry_id", "") or "")
        if geometry_id and str(geometry_id) != profile_geometry_id:
            digest = hashlib.blake2b(digest_size=20)
            digest.update(str(geometry_id).encode("utf-8", errors="replace"))
            digest.update(profile_geometry_key.encode("ascii"))
            geometry_key = digest.hexdigest()
        else:
            geometry_key = profile_geometry_key
        policy_azimuth = request.view_azimuth_deg if request.stage == "visible_partial" else 0.0
        policy_fov = request.view_fov_deg if request.stage == "visible_partial" else 360.0
        policy_margin = request.fov_margin_deg if request.stage == "visible_partial" else 0.0
        policy = (
            f"surface-v{SURFACE_CACHE_POLICY_VERSION}:profile={self.max_profile_samples}:"
            f"relief={self.max_relief_samples}:rgb-nearest:categorical-nearest-lod:"
            f"mode={request.surface_mode or 'automatic'}:"
            f"stage={request.stage}:radius={visible_radius}:"
            f"azimuth={policy_azimuth:.6f}:fov={policy_fov:.6f}:"
            f"margin={policy_margin:.6f}"
            f":viewport={request.viewport_width_px}x{request.viewport_height_px}"
        )
        key = SurfaceCacheKey(
            geometry_id=str(geometry_key),
            source_fingerprints=self.source_fingerprints,
            sampling_policy=policy,
        )
        with self._cache_lock:
            cached = self._cache.get(key.digest)
            if cached is not None:
                report(100.0, "cache-ready")
                if self.performance_logging:
                    append_perf_event(
                        "surface.sampling",
                        phase="cache-ready",
                        cache="memory",
                        stage=request.stage,
                        generation=int(request.generation),
                        elapsed_ms=int((time.perf_counter() - start) * 1000.0),
                    )
                return cached
        if self._persistent_store is not None:
            loaded = self._persistent_store.load(f"results/{key.digest}")
            if loaded is not None:
                try:
                    cached = _surface_cache_from_payload(*loaded)
                    if cached.key == key:
                        with self._cache_lock:
                            self._cache.put(
                                cached.cache_id, cached, _surface_cache_size(cached)
                            )
                        report(100.0, "persistent-cache-ready")
                        if self.performance_logging:
                            append_perf_event(
                                "surface.sampling",
                                phase="persistent-cache-ready",
                                cache="persistent",
                                stage=request.stage,
                                generation=int(request.generation),
                                elapsed_ms=int(
                                    (time.perf_counter() - start) * 1000.0
                                ),
                                result_cache=self._persistent_store.metrics(),
                            )
                        return cached
                except (KeyError, TypeError, ValueError):
                    pass

        report(4.0, "transforming-coordinates")
        check_cancelled()
        observer_x, observer_y = self._observer_xy(profile, geometry_crs)
        observer_samples = self.sample_rgba_points(
            np.asarray(observer_x, dtype=np.float64),
            np.asarray(observer_y, dtype=np.float64),
            input_crs=geometry_crs,
            lod_factors=np.asarray(1, dtype=np.int16),
            progress_callback=lambda fraction, phase: report(
                4.0 + float(fraction), f"observer:{phase}"
            ),
            abort_check=abort_check,
        )
        observer_valid = bool(np.asarray(observer_samples.valid).item())
        observer_rgba = (
            np.asarray(observer_samples.rgba, dtype=np.uint8).reshape(4)
            if observer_valid
            else None
        )
        observer_source_index = (
            int(np.asarray(observer_samples.source_indices).item())
            if observer_valid and observer_samples.source_indices is not None
            else -1
        )
        observer_class_id = (
            int(np.asarray(observer_samples.class_ids).item())
            if observer_valid and observer_samples.class_ids is not None
            else -1
        )
        observer_categorical = bool(
            observer_valid
            and observer_samples.categorical is not None
            and np.asarray(observer_samples.categorical).item()
        )
        observer_raster_row = (
            int(np.asarray(observer_samples.raster_rows).item())
            if observer_valid and observer_samples.raster_rows is not None
            else -1
        )
        observer_raster_column = (
            int(np.asarray(observer_samples.raster_columns).item())
            if observer_valid and observer_samples.raster_columns is not None
            else -1
        )
        observer_lod_factor = (
            int(np.asarray(observer_samples.lod_factors).item())
            if observer_samples.lod_factors is not None
            else 1
        )
        observer_sample_origin = (
            int(np.asarray(observer_samples.sample_origins).item())
            if observer_samples.sample_origins is not None
            else int(SAMPLE_ORIGIN_UNKNOWN)
        )
        near_patch_rgba = None
        near_patch_valid = None
        near_patch_sources = None
        near_patch_classes = None
        near_patch_categorical = None
        near_patch_raster_rows = None
        near_patch_raster_columns = None
        near_patch_lod_factors = None
        near_patch_sample_origins = None
        mesh = getattr(profile, "terrain_mesh", None)
        if isinstance(mesh, Mapping):
            patch_eastings = np.asarray(
                mesh.get("near_patch_eastings", ()), dtype=np.float64
            )
            patch_northings = np.asarray(
                mesh.get("near_patch_northings", ()), dtype=np.float64
            )
            patch_shape = (patch_northings.size, patch_eastings.size)
            patch_mesh_valid = np.asarray(
                mesh.get("near_patch_valid", ()), dtype=bool
            )
            if (
                patch_shape[0] >= 2
                and patch_shape[1] >= 2
                and patch_mesh_valid.shape == patch_shape
            ):
                patch_east, patch_north = np.meshgrid(
                    patch_eastings, patch_northings
                )
                patch_samples = self.sample_rgba_points(
                    observer_x + patch_east,
                    observer_y + patch_north,
                    input_crs=geometry_crs,
                    lod_factors=np.ones(patch_shape, dtype=np.int16),
                    progress_callback=lambda fraction, phase: report(
                        5.0 + 5.0 * float(fraction), f"near-patch:{phase}"
                    ),
                    abort_check=abort_check,
                )
                near_patch_valid = patch_mesh_valid & patch_samples.valid
                near_patch_rgba = np.where(
                    near_patch_valid[..., None], patch_samples.rgba, 0
                ).astype(np.uint8)
                near_patch_sources = np.where(
                    near_patch_valid, patch_samples.source_indices, -1
                ).astype(np.int16)
                near_patch_classes = np.where(
                    near_patch_valid,
                    patch_samples.class_ids
                    if patch_samples.class_ids is not None
                    else -1,
                    -1,
                ).astype(np.int64)
                near_patch_categorical = near_patch_valid & (
                    patch_samples.categorical
                    if patch_samples.categorical is not None
                    else False
                )
                near_patch_raster_rows = np.where(
                    near_patch_valid, patch_samples.raster_rows, -1
                ).astype(np.int64)
                near_patch_raster_columns = np.where(
                    near_patch_valid, patch_samples.raster_columns, -1
                ).astype(np.int64)
                near_patch_lod_factors = np.asarray(
                    patch_samples.lod_factors, dtype=np.int16
                )
                near_patch_sample_origins = np.where(
                    near_patch_valid,
                    patch_samples.sample_origins,
                    SAMPLE_ORIGIN_UNKNOWN,
                ).astype(np.uint8)
        azimuths = np.asarray(getattr(profile, "azimuths", ()), dtype=np.float64)
        bands = list(getattr(profile, "bands", ()) or ())

        band_indices = _sample_indices(
            len(bands), max(1, len(azimuths)), self.max_profile_samples
        )
        profile_azimuth_indices = _sample_indices(
            len(azimuths), max(1, len(band_indices)), self.max_profile_samples
        )
        profile_azimuth_indices = _visible_azimuth_indices(
            profile_azimuth_indices, azimuths, request
        )
        profile_rgba = np.zeros(
            (len(band_indices), len(profile_azimuth_indices), 4), dtype=np.uint8
        )
        profile_valid = np.zeros(profile_rgba.shape[:-1], dtype=bool)
        profile_sources = np.full(profile_valid.shape, -1, dtype=np.int16)
        profile_classes = np.full(profile_valid.shape, -1, dtype=np.int64)
        profile_categorical = np.zeros(profile_valid.shape, dtype=bool)
        profile_raster_rows = np.full(profile_valid.shape, -1, dtype=np.int64)
        profile_raster_columns = np.full(profile_valid.shape, -1, dtype=np.int64)
        profile_lod_factors = np.ones(profile_valid.shape, dtype=np.int16)
        profile_sample_origins = np.zeros(profile_valid.shape, dtype=np.uint8)
        if band_indices.size and profile_azimuth_indices.size:
            sampled_bands = [bands[index] for index in band_indices]
            sampled_azimuths = azimuths[profile_azimuth_indices]
            convergence = float(
                getattr(profile, "grid_convergence_deg", 0.0) or 0.0
            )
            azimuth_radians = np.deg2rad(
                sampled_azimuths - convergence
            )[None, :]
            distances = np.stack(
                [
                    np.asarray(
                        band.get(
                            "surface_dists",
                            band.get("dists", np.zeros(len(azimuths))),
                        ),
                        dtype=np.float64,
                    )[profile_azimuth_indices]
                    for band in sampled_bands
                ],
                axis=0,
            )
            angle_valid = np.stack(
                [
                    np.asarray(band.get("angles", np.full(len(azimuths), -np.inf)), dtype=np.float64)[
                        profile_azimuth_indices
                    ]
                    for band in sampled_bands
                ],
                axis=0,
            )
            position_valid = (
                np.isfinite(distances)
                & (distances > 0.0)
                & np.isfinite(angle_valid)
                & (angle_valid > -math.pi / 2.0)
            )
            if visible_radius is not None:
                position_valid &= distances <= float(visible_radius)
            x = observer_x + distances * np.sin(azimuth_radians)
            y = observer_y + distances * np.cos(azimuth_radians)
            x = np.where(position_valid, x, np.nan)
            y = np.where(position_valid, y, np.nan)
            samples = self.sample_rgba_points(
                x,
                y,
                input_crs=geometry_crs,
                lod_factors=_lod_factors_for_polar_grid(
                    distances,
                    sampled_azimuths,
                    self._nominal_surface_resolution(),
                    viewport_width_px=request.viewport_width_px,
                    view_fov_deg=request.view_fov_deg,
                    categorical=categorical_lod,
                ),
                progress_callback=lambda fraction, phase: report(
                    10.0 + 20.0 * float(fraction), f"profile:{phase}"
                ),
                abort_check=abort_check,
            )
            profile_valid = samples.valid & position_valid
            profile_rgba = np.where(profile_valid[..., None], samples.rgba, 0).astype(np.uint8)
            profile_sources = np.where(
                profile_valid, samples.source_indices, -1
            ).astype(np.int16)
            if samples.class_ids is not None:
                profile_classes = np.where(
                    profile_valid, samples.class_ids, -1
                ).astype(np.int64)
            if samples.categorical is not None:
                profile_categorical = profile_valid & samples.categorical
            profile_raster_rows = np.where(
                profile_valid, samples.raster_rows, -1
            ).astype(np.int64)
            profile_raster_columns = np.where(
                profile_valid, samples.raster_columns, -1
            ).astype(np.int64)
            profile_lod_factors = np.asarray(
                samples.lod_factors, dtype=np.int16
            )
            profile_sample_origins = np.where(
                profile_valid,
                samples.sample_origins,
                SAMPLE_ORIGIN_UNKNOWN,
            ).astype(np.uint8)

        report(32.0, "profile-ready")
        check_cancelled()

        relief_rgba = None
        relief_valid = None
        relief_sources = None
        relief_classes = None
        relief_categorical = None
        relief_raster_rows = None
        relief_raster_columns = None
        relief_lod_factors = None
        relief_sample_origins = None
        relief_distance_indices = None
        relief_azimuth_indices = None
        if isinstance(mesh, Mapping):
            mesh_azimuths = np.asarray(mesh.get("azimuths", ()), dtype=np.float64)
            mesh_distances = np.asarray(mesh.get("distances", ()), dtype=np.float64)
            if mesh_azimuths.size and mesh_distances.size:
                relief_distance_indices = _sample_indices(
                    len(mesh_distances), len(mesh_azimuths), self.max_relief_samples
                )
                relief_azimuth_indices = _sample_indices(
                    len(mesh_azimuths), len(relief_distance_indices), self.max_relief_samples
                )
                if visible_radius is not None:
                    relief_distance_indices = relief_distance_indices[
                        mesh_distances[relief_distance_indices] <= float(visible_radius)
                    ]
                relief_azimuth_indices = _visible_azimuth_indices(
                    relief_azimuth_indices, mesh_azimuths, request
                )
                selected_distances = mesh_distances[relief_distance_indices]
                selected_azimuths = mesh_azimuths[relief_azimuth_indices]
                convergence = float(
                    getattr(profile, "grid_convergence_deg", 0.0) or 0.0
                )
                azimuth_radians = np.deg2rad(
                    selected_azimuths - convergence
                )[None, :]
                x = observer_x + selected_distances[:, None] * np.sin(azimuth_radians)
                y = observer_y + selected_distances[:, None] * np.cos(azimuth_radians)
                samples = self.sample_rgba_points(
                    x,
                    y,
                    input_crs=geometry_crs,
                    lod_factors=_lod_factors_for_polar_grid(
                        selected_distances,
                        selected_azimuths,
                        self._nominal_surface_resolution(),
                        viewport_width_px=request.viewport_width_px,
                        view_fov_deg=request.view_fov_deg,
                        categorical=categorical_lod,
                    ),
                    progress_callback=lambda fraction, phase: report(
                        34.0 + 28.0 * float(fraction), f"relief:{phase}"
                    ),
                    abort_check=abort_check,
                )
                mesh_valid_raw = np.asarray(
                    mesh.get("valid", np.ones((len(mesh_distances), len(mesh_azimuths)))), dtype=bool
                )
                if mesh_valid_raw.shape == (len(mesh_distances), len(mesh_azimuths)):
                    mesh_valid = mesh_valid_raw[np.ix_(relief_distance_indices, relief_azimuth_indices)]
                else:
                    mesh_valid = np.ones(samples.valid.shape, dtype=bool)
                relief_valid = samples.valid & mesh_valid
                relief_rgba = np.where(relief_valid[..., None], samples.rgba, 0).astype(np.uint8)
                relief_sources = np.where(relief_valid, samples.source_indices, -1).astype(np.int16)
                relief_classes = np.where(
                    relief_valid,
                    samples.class_ids
                    if samples.class_ids is not None
                    else -1,
                    -1,
                ).astype(np.int64)
                relief_categorical = relief_valid & (
                    samples.categorical
                    if samples.categorical is not None
                    else False
                )
                relief_raster_rows = np.where(
                    relief_valid, samples.raster_rows, -1
                ).astype(np.int64)
                relief_raster_columns = np.where(
                    relief_valid, samples.raster_columns, -1
                ).astype(np.int64)
                relief_lod_factors = np.asarray(
                    samples.lod_factors, dtype=np.int16
                )
                relief_sample_origins = np.where(
                    relief_valid,
                    samples.sample_origins,
                    SAMPLE_ORIGIN_UNKNOWN,
                ).astype(np.uint8)

        report(64.0, "relief-ready")
        check_cancelled()

        visual_distances = visual_azimuths = None
        visual_altitudes = visual_elevations = None
        visual_valid = visual_visible = None
        visual_rgba = visual_sources = visual_classes = visual_categorical = None
        visual_raster_rows = visual_raster_columns = None
        visual_lod_factors = visual_sample_origins = None
        if isinstance(mesh, Mapping):
            mesh_azimuths = np.asarray(mesh.get("azimuths", ()), dtype=np.float64)
            mesh_distances = np.asarray(mesh.get("distances", ()), dtype=np.float64)
            mesh_shape = (len(mesh_distances), len(mesh_azimuths))
            mesh_altitudes = np.asarray(mesh.get("altitudes", ()), dtype=np.float32)
            mesh_elevations = np.asarray(mesh.get("elevations", ()), dtype=np.float32)
            mesh_valid = np.asarray(mesh.get("valid", ()), dtype=bool)
            mesh_visible = np.asarray(mesh.get("visible", mesh_valid), dtype=bool)
            if (
                mesh_shape[0] >= 2
                and mesh_shape[1] >= 2
                and mesh_altitudes.shape == mesh_shape
                and mesh_elevations.shape == mesh_shape
                and mesh_valid.shape == mesh_shape
            ):
                (
                    candidate_visual_distances,
                    candidate_visual_azimuths,
                    visual_budget_limited,
                ) = self._adaptive_visual_axes(
                    mesh_distances, mesh_azimuths, request
                )
                if (
                    len(candidate_visual_distances) > len(mesh_distances)
                    or len(candidate_visual_azimuths) > len(mesh_azimuths)
                ):
                    report(68.0, "subdividing-visual-grid")
                    visual_distances = candidate_visual_distances
                    visual_azimuths = candidate_visual_azimuths
                    if visual_budget_limited and self.performance_logging:
                        append_perf_event(
                            "surface.visual_budget",
                            target_edge_px=8.0,
                            requested_samples=int(
                                len(candidate_visual_distances)
                                * len(candidate_visual_azimuths)
                            ),
                            max_relief_samples=int(self.max_relief_samples),
                        )
                    if visible_radius is not None:
                        visual_distances = visual_distances[
                            visual_distances <= float(visible_radius)
                        ]
                    if request.stage == "visible_partial":
                        visible_azimuth_indices = _visible_azimuth_indices(
                            np.arange(len(visual_azimuths), dtype=np.int32),
                            visual_azimuths,
                            request,
                        )
                        visual_azimuths = visual_azimuths[visible_azimuth_indices]
                    visual_altitudes = _interpolate_polar_grid(
                        mesh_altitudes,
                        mesh_distances,
                        mesh_azimuths,
                        visual_distances,
                        visual_azimuths,
                    ).astype(np.float32)
                    visual_elevations = _interpolate_polar_grid(
                        mesh_elevations,
                        mesh_distances,
                        mesh_azimuths,
                        visual_distances,
                        visual_azimuths,
                    ).astype(np.float32)
                    visual_valid = _interpolate_polar_grid(
                        mesh_valid,
                        mesh_distances,
                        mesh_azimuths,
                        visual_distances,
                        visual_azimuths,
                        nearest=True,
                    ).astype(bool)
                    visual_visible = _interpolate_polar_grid(
                        mesh_visible
                        if mesh_visible.shape == mesh_shape
                        else mesh_valid,
                        mesh_distances,
                        mesh_azimuths,
                        visual_distances,
                        visual_azimuths,
                        nearest=True,
                    ).astype(bool)
                    check_cancelled()
                    report(74.0, "sampling-visual-detail")
                    convergence = float(
                        getattr(profile, "grid_convergence_deg", 0.0) or 0.0
                    )
                    radians = np.deg2rad(
                        visual_azimuths - convergence
                    )[None, :]
                    x = observer_x + visual_distances[:, None] * np.sin(radians)
                    y = observer_y + visual_distances[:, None] * np.cos(radians)
                    visual_samples = self.sample_rgba_points(
                        x,
                        y,
                        input_crs=geometry_crs,
                        lod_factors=_lod_factors_for_polar_grid(
                            visual_distances,
                            visual_azimuths,
                            self._nominal_surface_resolution(),
                            viewport_width_px=request.viewport_width_px,
                            view_fov_deg=request.view_fov_deg,
                            categorical=categorical_lod,
                        ),
                        progress_callback=lambda fraction, phase: report(
                            74.0 + 23.0 * float(fraction),
                            f"visual:{phase}",
                        ),
                        abort_check=abort_check,
                    )
                    visual_valid &= visual_samples.valid
                    visual_rgba = np.where(
                        visual_valid[..., None], visual_samples.rgba, 0
                    ).astype(np.uint8)
                    visual_sources = np.where(
                        visual_valid, visual_samples.source_indices, -1
                    ).astype(np.int16)
                    visual_classes = np.where(
                        visual_valid,
                        visual_samples.class_ids
                        if visual_samples.class_ids is not None
                        else -1,
                        -1,
                    ).astype(np.int64)
                    visual_categorical = visual_valid & (
                        visual_samples.categorical
                        if visual_samples.categorical is not None
                        else False
                    )
                    visual_raster_rows = np.where(
                        visual_valid, visual_samples.raster_rows, -1
                    ).astype(np.int64)
                    visual_raster_columns = np.where(
                        visual_valid, visual_samples.raster_columns, -1
                    ).astype(np.int64)
                    visual_lod_factors = np.asarray(
                        visual_samples.lod_factors, dtype=np.int16
                    )
                    visual_sample_origins = np.where(
                        visual_valid,
                        visual_samples.sample_origins,
                        SAMPLE_ORIGIN_UNKNOWN,
                    ).astype(np.uint8)

        report(98.0, "registering-cache")
        check_cancelled()
        result = SurfaceSampleCache(
            key=key,
            source_ids=self.source_ids,
            source_names=self.source_names,
            source_legend_ids=self.source_legend_ids,
            geometry_crs=geometry_crs,
            observer_rgba=observer_rgba,
            observer_valid=observer_valid,
            observer_source_index=observer_source_index,
            observer_class_id=observer_class_id,
            observer_categorical=observer_categorical,
            observer_raster_row=observer_raster_row,
            observer_raster_column=observer_raster_column,
            observer_lod_factor=observer_lod_factor,
            observer_sample_origin=observer_sample_origin,
            observer_loaded=True,
            completion_state=request.stage,
            near_patch_rgba=near_patch_rgba,
            near_patch_valid=near_patch_valid,
            near_patch_source_indices=near_patch_sources,
            near_patch_class_ids=near_patch_classes,
            near_patch_categorical=near_patch_categorical,
            near_patch_raster_rows=near_patch_raster_rows,
            near_patch_raster_columns=near_patch_raster_columns,
            near_patch_lod_factors=near_patch_lod_factors,
            near_patch_sample_origins=near_patch_sample_origins,
            profile_rgba=profile_rgba,
            profile_valid=profile_valid,
            profile_source_indices=profile_sources,
            profile_class_ids=profile_classes,
            profile_categorical=profile_categorical,
            profile_raster_rows=profile_raster_rows,
            profile_raster_columns=profile_raster_columns,
            profile_lod_factors=profile_lod_factors,
            profile_sample_origins=profile_sample_origins,
            profile_band_indices=band_indices,
            profile_azimuth_indices=profile_azimuth_indices,
            relief_rgba=relief_rgba,
            relief_valid=relief_valid,
            relief_source_indices=relief_sources,
            relief_class_ids=relief_classes,
            relief_categorical=relief_categorical,
            relief_raster_rows=relief_raster_rows,
            relief_raster_columns=relief_raster_columns,
            relief_lod_factors=relief_lod_factors,
            relief_sample_origins=relief_sample_origins,
            relief_distance_indices=relief_distance_indices,
            relief_azimuth_indices=relief_azimuth_indices,
            visual_distances=visual_distances,
            visual_azimuths=visual_azimuths,
            visual_altitudes=visual_altitudes,
            visual_elevations=visual_elevations,
            visual_valid=visual_valid,
            visual_visible=visual_visible,
            visual_rgba=visual_rgba,
            visual_source_indices=visual_sources,
            visual_class_ids=visual_classes,
            visual_categorical=visual_categorical,
            visual_raster_rows=visual_raster_rows,
            visual_raster_columns=visual_raster_columns,
            visual_lod_factors=visual_lod_factors,
            visual_sample_origins=visual_sample_origins,
        )
        result_size = _surface_cache_size(result)
        with self._cache_lock:
            self._cache.put(result.cache_id, result, result_size)
        if self._persistent_store is not None:
            metadata, arrays = _surface_cache_payload(result)
            try:
                self._persistent_store.save(
                    f"results/{result.cache_id}", metadata, arrays
                )
            except (OSError, ValueError):
                pass
        report(100.0, "completed")
        rss_end, peak_end = process_memory_bytes()
        if self.performance_logging:
            deltas = []
            for dataset in metric_datasets:
                before = metric_baseline[id(dataset)]
                after = (
                    int(dataset.bytes_read),
                    int(dataset.lod_rows_read),
                    int(dataset.lod_intervals_read),
                    int(dataset.lod_windows_read),
                    int(dataset.lod_pixels_decoded),
                    int(dataset.lod_modal_cells),
                    int(dataset.lod_requested),
                    int(dataset.lod_unique),
                    int(dataset.lod_cache_hits),
                )
                deltas.append(tuple(end - begin for begin, end in zip(before, after)))
            append_perf_event(
                "surface.sampling",
                phase="completed",
                cache="cold",
                stage=request.stage,
                generation=int(request.generation),
                elapsed_ms=int((time.perf_counter() - start) * 1000.0),
                provider_count=len(self.providers),
                raster_bytes=sum(value[0] for value in deltas),
                lod_rows=sum(value[1] for value in deltas),
                lod_intervals=sum(value[2] for value in deltas),
                lod_windows=sum(value[3] for value in deltas),
                lod_pixels_decoded=sum(value[4] for value in deltas),
                lod_modal_cells=sum(value[5] for value in deltas),
                lod_requested=sum(value[6] for value in deltas),
                lod_unique=sum(value[7] for value in deltas),
                lod_cache_hits=sum(value[8] for value in deltas),
                result_cache=self._persistent_store.metrics() if self._persistent_store else None,
                rss_bytes=int(rss_end),
                peak_rss_bytes=int(peak_end),
                rss_delta_bytes=int(rss_end - rss_start),
                peak_delta_bytes=int(peak_end - peak_start),
            )
        print(
            "[TEMPORAL][SurfaceSampling] "
            f"providers={len(self.providers)} "
            f"profile_samples={int(profile_rgba.shape[0] * profile_rgba.shape[1])} "
            f"relief_samples={0 if relief_rgba is None else int(relief_rgba.shape[0] * relief_rgba.shape[1])} "
            f"elapsed={time.perf_counter() - start:.3f}s"
        )
        return result
