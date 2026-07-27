"""Immutable surface sampling cache and persistence payloads."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from TerraLab.terrain.crs import normalize_crs
from TerraLab.terrain.surface.common import (
    SAMPLE_ORIGIN_UNKNOWN,
)

@dataclass(frozen=True)
class SurfaceCacheKey:
    geometry_id: str
    source_fingerprints: tuple[str, ...]
    sampling_policy: str

    @property
    def digest(self) -> str:
        digest = hashlib.blake2b(digest_size=20)
        digest.update(self.geometry_id.encode("utf-8", errors="replace"))
        digest.update(self.sampling_policy.encode("utf-8", errors="replace"))
        for fingerprint in self.source_fingerprints:
            digest.update(fingerprint.encode("ascii", errors="replace"))
        return digest.hexdigest()


@dataclass(frozen=True)
class SurfaceSamplingRequest:
    """Typed, cancellable description of one surface publication stage."""

    profile: Any
    visible_radius_m: float | None = None
    view_azimuth_deg: float = 0.0
    view_fov_deg: float = 360.0
    generation: int = 0
    stage: str = "complete"
    fov_margin_deg: float = 10.0
    surface_mode: str | None = None
    viewport_width_px: int | None = None
    viewport_height_px: int | None = None
    # Deprecated compatibility input.
    surface_layer_type: str | None = None

    def __post_init__(self) -> None:
        stage = str(self.stage or "complete")
        if stage not in {"visible_partial", "complete"}:
            raise ValueError(f"Unsupported surface stage: {stage}")
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "view_azimuth_deg", float(self.view_azimuth_deg) % 360.0)
        object.__setattr__(self, "view_fov_deg", min(360.0, max(0.0, float(self.view_fov_deg))))
        object.__setattr__(self, "fov_margin_deg", max(0.0, float(self.fov_margin_deg)))
        normalized_mode = str(
            self.surface_mode or self.surface_layer_type or ""
        ).strip() or None
        object.__setattr__(self, "surface_mode", normalized_mode)
        object.__setattr__(self, "surface_layer_type", normalized_mode)
        for name in ("viewport_width_px", "viewport_height_px"):
            raw_value = getattr(self, name)
            if raw_value is None:
                continue
            value = int(raw_value)
            object.__setattr__(self, name, value if value > 0 else None)
        if self.visible_radius_m is not None:
            radius = float(self.visible_radius_m)
            object.__setattr__(
                self,
                "visible_radius_m",
                radius if math.isfinite(radius) and radius > 0.0 else None,
            )


def _readonly_array(value: Any, dtype=None) -> np.ndarray | None:
    if value is None:
        return None
    array = np.asarray(value, dtype=dtype)
    array.setflags(write=False)
    return array


@dataclass(frozen=True)
class SurfaceSampleCache:
    """Immutable runtime colors aligned with scientific profile geometry."""

    key: SurfaceCacheKey
    source_ids: tuple[str, ...]
    geometry_crs: str
    source_names: tuple[str, ...] = ()
    source_legend_ids: tuple[str, ...] = ()
    # Exact surface sample below the observer.  It closes the sub-metre hole
    # at the centre of the polar mesh without stretching the first radial ring
    # (and all of its angular land-cover cells) to the screen bottom.
    observer_rgba: np.ndarray | None = None
    observer_valid: bool = False
    observer_source_index: int = -1
    observer_class_id: int = -1
    observer_categorical: bool = False
    observer_raster_row: int = -1
    observer_raster_column: int = -1
    observer_lod_factor: int = 1
    observer_sample_origin: int = int(SAMPLE_ORIGIN_UNKNOWN)
    observer_loaded: bool = True
    completion_state: str = "complete"
    # Surface values for the Cartesian near-field DEM patch. They keep their
    # semantic class ids separate from the derived display colour.
    near_patch_rgba: np.ndarray | None = None
    near_patch_valid: np.ndarray | None = None
    near_patch_loaded: np.ndarray | None = None
    near_patch_source_indices: np.ndarray | None = None
    near_patch_class_ids: np.ndarray | None = None
    near_patch_categorical: np.ndarray | None = None
    near_patch_raster_rows: np.ndarray | None = None
    near_patch_raster_columns: np.ndarray | None = None
    near_patch_lod_factors: np.ndarray | None = None
    near_patch_sample_origins: np.ndarray | None = None
    profile_rgba: np.ndarray | None = None
    profile_valid: np.ndarray | None = None
    profile_loaded: np.ndarray | None = None
    profile_source_indices: np.ndarray | None = None
    profile_class_ids: np.ndarray | None = None
    profile_categorical: np.ndarray | None = None
    profile_raster_rows: np.ndarray | None = None
    profile_raster_columns: np.ndarray | None = None
    profile_lod_factors: np.ndarray | None = None
    profile_sample_origins: np.ndarray | None = None
    profile_band_indices: np.ndarray | None = None
    profile_azimuth_indices: np.ndarray | None = None
    relief_rgba: np.ndarray | None = None
    relief_valid: np.ndarray | None = None
    relief_loaded: np.ndarray | None = None
    relief_source_indices: np.ndarray | None = None
    relief_class_ids: np.ndarray | None = None
    relief_categorical: np.ndarray | None = None
    relief_raster_rows: np.ndarray | None = None
    relief_raster_columns: np.ndarray | None = None
    relief_lod_factors: np.ndarray | None = None
    relief_sample_origins: np.ndarray | None = None
    relief_distance_indices: np.ndarray | None = None
    relief_azimuth_indices: np.ndarray | None = None
    # A visual grid may subdivide DEM cells to preserve land-cover detail. Its
    # elevations are interpolated from the DEM and therefore do not invent new
    # topography; only the appearance is sampled at the denser coordinates.
    visual_distances: np.ndarray | None = None
    visual_azimuths: np.ndarray | None = None
    visual_altitudes: np.ndarray | None = None
    visual_elevations: np.ndarray | None = None
    visual_valid: np.ndarray | None = None
    visual_loaded: np.ndarray | None = None
    visual_visible: np.ndarray | None = None
    visual_rgba: np.ndarray | None = None
    visual_source_indices: np.ndarray | None = None
    visual_class_ids: np.ndarray | None = None
    visual_categorical: np.ndarray | None = None
    visual_raster_rows: np.ndarray | None = None
    visual_raster_columns: np.ndarray | None = None
    visual_lod_factors: np.ndarray | None = None
    visual_sample_origins: np.ndarray | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "geometry_crs", normalize_crs(self.geometry_crs))
        state = str(self.completion_state or "complete")
        if state not in {"visible_partial", "complete"}:
            raise ValueError(f"Unsupported surface cache state: {state}")
        object.__setattr__(self, "completion_state", state)
        if self.profile_loaded is None and self.profile_valid is not None:
            object.__setattr__(
                self, "profile_loaded", np.ones(np.asarray(self.profile_valid).shape, dtype=bool)
            )
        if self.near_patch_loaded is None and self.near_patch_valid is not None:
            object.__setattr__(
                self,
                "near_patch_loaded",
                np.ones(np.asarray(self.near_patch_valid).shape, dtype=bool),
            )
        if self.relief_loaded is None and self.relief_valid is not None:
            object.__setattr__(
                self, "relief_loaded", np.ones(np.asarray(self.relief_valid).shape, dtype=bool)
            )
        if self.visual_loaded is None and self.visual_valid is not None:
            object.__setattr__(
                self, "visual_loaded", np.ones(np.asarray(self.visual_valid).shape, dtype=bool)
            )
        for prefix in ("near_patch", "profile", "relief", "visual"):
            rgba = getattr(self, f"{prefix}_rgba")
            valid = getattr(self, f"{prefix}_valid")
            loaded = getattr(self, f"{prefix}_loaded")
            sources = getattr(self, f"{prefix}_source_indices")
            classes = getattr(self, f"{prefix}_class_ids")
            categorical = getattr(self, f"{prefix}_categorical")
            values = (rgba, valid, loaded, sources, classes, categorical)
            if all(value is None for value in values):
                continue
            if valid is None:
                raise ValueError(f"{prefix} material samples require a validity mask")
            shape = np.shape(valid)
            if rgba is not None and np.shape(rgba) != shape + (4,):
                raise ValueError(f"{prefix} RGBA geometry is not aligned")
            for name, value in (
                ("loaded", loaded),
                ("source indices", sources),
                ("class ids", classes),
                ("categorical mask", categorical),
            ):
                if value is not None and np.shape(value) != shape:
                    raise ValueError(
                        f"{prefix} {name} geometry is not aligned with RGBA"
                    )
            for suffix in (
                "raster_rows",
                "raster_columns",
                "lod_factors",
                "sample_origins",
            ):
                value = getattr(self, f"{prefix}_{suffix}")
                if value is not None and np.shape(value) != shape:
                    raise ValueError(
                        f"{prefix} {suffix} geometry is not aligned with RGBA"
                    )
        for name, dtype in (
            ("observer_rgba", np.uint8),
            ("near_patch_rgba", np.uint8),
            ("near_patch_valid", bool),
            ("near_patch_loaded", bool),
            ("near_patch_source_indices", np.int16),
            ("near_patch_class_ids", np.int64),
            ("near_patch_categorical", bool),
            ("near_patch_raster_rows", np.int64),
            ("near_patch_raster_columns", np.int64),
            ("near_patch_lod_factors", np.int16),
            ("near_patch_sample_origins", np.uint8),
            ("profile_rgba", np.uint8),
            ("profile_valid", bool),
            ("profile_loaded", bool),
            ("profile_source_indices", np.int16),
            ("profile_class_ids", np.int64),
            ("profile_categorical", bool),
            ("profile_raster_rows", np.int64),
            ("profile_raster_columns", np.int64),
            ("profile_lod_factors", np.int16),
            ("profile_sample_origins", np.uint8),
            ("profile_band_indices", np.int32),
            ("profile_azimuth_indices", np.int32),
            ("relief_rgba", np.uint8),
            ("relief_valid", bool),
            ("relief_loaded", bool),
            ("relief_source_indices", np.int16),
            ("relief_class_ids", np.int64),
            ("relief_categorical", bool),
            ("relief_raster_rows", np.int64),
            ("relief_raster_columns", np.int64),
            ("relief_lod_factors", np.int16),
            ("relief_sample_origins", np.uint8),
            ("relief_distance_indices", np.int32),
            ("relief_azimuth_indices", np.int32),
            ("visual_distances", np.float32),
            ("visual_azimuths", np.float32),
            ("visual_altitudes", np.float32),
            ("visual_elevations", np.float32),
            ("visual_valid", bool),
            ("visual_loaded", bool),
            ("visual_visible", bool),
            ("visual_rgba", np.uint8),
            ("visual_source_indices", np.int16),
            ("visual_class_ids", np.int64),
            ("visual_categorical", bool),
            ("visual_raster_rows", np.int64),
            ("visual_raster_columns", np.int64),
            ("visual_lod_factors", np.int16),
            ("visual_sample_origins", np.uint8),
        ):
            object.__setattr__(self, name, _readonly_array(getattr(self, name), dtype))

    @property
    def cache_id(self) -> str:
        return self.key.digest


_SURFACE_ARRAY_FIELDS = (
    "observer_rgba",
    "near_patch_rgba",
    "near_patch_valid",
    "near_patch_loaded",
    "near_patch_source_indices",
    "near_patch_class_ids",
    "near_patch_categorical",
    "near_patch_raster_rows",
    "near_patch_raster_columns",
    "near_patch_lod_factors",
    "near_patch_sample_origins",
    "profile_rgba",
    "profile_valid",
    "profile_loaded",
    "profile_source_indices",
    "profile_class_ids",
    "profile_categorical",
    "profile_raster_rows",
    "profile_raster_columns",
    "profile_lod_factors",
    "profile_sample_origins",
    "profile_band_indices",
    "profile_azimuth_indices",
    "relief_rgba",
    "relief_valid",
    "relief_loaded",
    "relief_source_indices",
    "relief_class_ids",
    "relief_categorical",
    "relief_raster_rows",
    "relief_raster_columns",
    "relief_lod_factors",
    "relief_sample_origins",
    "relief_distance_indices",
    "relief_azimuth_indices",
    "visual_distances",
    "visual_azimuths",
    "visual_altitudes",
    "visual_elevations",
    "visual_valid",
    "visual_loaded",
    "visual_visible",
    "visual_rgba",
    "visual_source_indices",
    "visual_class_ids",
    "visual_categorical",
    "visual_raster_rows",
    "visual_raster_columns",
    "visual_lod_factors",
    "visual_sample_origins",
)


def _surface_cache_size(cache: SurfaceSampleCache) -> int:
    return sum(
        int(value.nbytes)
        for name in _SURFACE_ARRAY_FIELDS
        if (value := getattr(cache, name)) is not None
    )


def _surface_cache_payload(
    cache: SurfaceSampleCache,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    metadata = {
        "schema": 4,
        "digest": cache.cache_id,
        "geometry_id": cache.key.geometry_id,
        "source_fingerprints": list(cache.key.source_fingerprints),
        "sampling_policy": cache.key.sampling_policy,
        "source_ids": list(cache.source_ids),
        "source_names": list(cache.source_names),
        "source_legend_ids": list(cache.source_legend_ids),
        "geometry_crs": cache.geometry_crs,
        "observer_valid": bool(cache.observer_valid),
        "observer_source_index": int(cache.observer_source_index),
        "observer_class_id": int(cache.observer_class_id),
        "observer_categorical": bool(cache.observer_categorical),
        "observer_raster_row": int(cache.observer_raster_row),
        "observer_raster_column": int(cache.observer_raster_column),
        "observer_lod_factor": int(cache.observer_lod_factor),
        "observer_sample_origin": int(cache.observer_sample_origin),
        "observer_loaded": bool(cache.observer_loaded),
        "completion_state": cache.completion_state,
    }
    arrays = {
        name: value
        for name in _SURFACE_ARRAY_FIELDS
        if (value := getattr(cache, name)) is not None
    }
    return metadata, arrays


def _surface_cache_from_payload(
    metadata: Mapping[str, Any], arrays: Mapping[str, np.ndarray]
) -> SurfaceSampleCache:
    if int(metadata.get("schema", 0)) != 4:
        raise ValueError("Unsupported surface cache schema")
    key = SurfaceCacheKey(
        geometry_id=str(metadata["geometry_id"]),
        source_fingerprints=tuple(str(value) for value in metadata["source_fingerprints"]),
        sampling_policy=str(metadata["sampling_policy"]),
    )
    if str(metadata.get("digest", "")) != key.digest:
        raise ValueError("Surface cache digest mismatch")
    kwargs = {name: arrays.get(name) for name in _SURFACE_ARRAY_FIELDS}
    return SurfaceSampleCache(
        key=key,
        source_ids=tuple(str(value) for value in metadata.get("source_ids", ())),
        source_names=tuple(
            str(value) for value in metadata.get("source_names", ())
        ),
        source_legend_ids=tuple(
            str(value) for value in metadata.get("source_legend_ids", ())
        ),
        geometry_crs=str(metadata["geometry_crs"]),
        observer_valid=bool(metadata.get("observer_valid", False)),
        observer_source_index=int(metadata.get("observer_source_index", -1)),
        observer_class_id=int(metadata.get("observer_class_id", -1)),
        observer_categorical=bool(metadata.get("observer_categorical", False)),
        observer_raster_row=int(metadata.get("observer_raster_row", -1)),
        observer_raster_column=int(metadata.get("observer_raster_column", -1)),
        observer_lod_factor=int(metadata.get("observer_lod_factor", 1)),
        observer_sample_origin=int(
            metadata.get("observer_sample_origin", SAMPLE_ORIGIN_UNKNOWN)
        ),
        observer_loaded=bool(metadata.get("observer_loaded", True)),
        completion_state=str(metadata.get("completion_state", "complete")),
        **kwargs,
    )


def _hash_array(digest, value: Any) -> None:
    array = np.ascontiguousarray(np.asarray(value))
    digest.update(str(array.dtype).encode("ascii"))
    digest.update(str(array.shape).encode("ascii"))
    digest.update(array.tobytes())


def geometry_fingerprint(profile: Any, *, geometry_crs: str) -> str:
    explicit = getattr(profile, "geometry_id", None)
    digest = hashlib.blake2b(digest_size=20)
    digest.update(b"terrain-geometry-v4-categorical-materials")
    if explicit:
        digest.update(str(explicit).encode("utf-8", errors="replace"))
    digest.update(normalize_crs(geometry_crs).encode("ascii"))
    digest.update(str(float(getattr(profile, "observer_lat", 0.0))).encode("ascii"))
    digest.update(str(float(getattr(profile, "observer_lon", 0.0))).encode("ascii"))
    digest.update(str(getattr(profile, "representation_mode", "legacy")).encode("utf-8"))
    digest.update(
        repr(tuple(getattr(profile, "elevation_source_ids", ()) or ())).encode(
            "utf-8", errors="replace"
        )
    )
    digest.update(
        str(getattr(profile, "effective_elevation_source_id", "") or "").encode(
            "utf-8", errors="replace"
        )
    )
    _hash_array(digest, getattr(profile, "azimuths", np.asarray([], dtype=np.float32)))
    for band in getattr(profile, "bands", ()) or ():
        digest.update(str(band.get("id", "")).encode("utf-8", errors="replace"))
        _hash_array(digest, band.get("dists", np.asarray([], dtype=np.float32)))
        _hash_array(digest, band.get("angles", np.asarray([], dtype=np.float32)))
    mesh = getattr(profile, "terrain_mesh", None)
    if isinstance(mesh, Mapping):
        digest.update(str(mesh.get("version", 1)).encode("ascii"))
        for name in (
            "azimuths",
            "distances",
            "altitudes",
            "valid",
            "near_patch_eastings",
            "near_patch_northings",
            "near_patch_altitudes",
            "near_patch_valid",
        ):
            if name in mesh:
                _hash_array(digest, mesh[name])
    return digest.hexdigest()
