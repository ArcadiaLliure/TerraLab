"""Immutable terrain overlay geometry and material value objects."""

import math
from dataclasses import dataclass, field

import numpy as np

from TerraLab.terrain.render.lighting import TerrainCelestialLightFactors


@dataclass(frozen=True)
class _TerrainRenderAsset:
    """Immutable, camera-independent arrays prepared outside paint work."""

    mesh_id: int
    azimuths: np.ndarray
    azimuths_closed: np.ndarray
    distances: np.ndarray
    altitudes: np.ndarray
    altitudes_closed: np.ndarray
    elevations: np.ndarray
    valid: np.ndarray
    valid_closed: np.ndarray
    visible: np.ndarray
    visible_closed: np.ndarray
    normal_x: np.ndarray
    normal_y: np.ndarray
    normal_z: np.ndarray
    near_patch_eastings: np.ndarray
    near_patch_northings: np.ndarray
    near_patch_altitudes: np.ndarray
    near_patch_elevations: np.ndarray
    near_patch_valid: np.ndarray
    near_patch_normal_x: np.ndarray
    near_patch_normal_y: np.ndarray
    near_patch_normal_z: np.ndarray

    def __post_init__(self) -> None:
        for name in (
            "azimuths",
            "azimuths_closed",
            "distances",
            "altitudes",
            "altitudes_closed",
            "elevations",
            "valid",
            "valid_closed",
            "visible",
            "visible_closed",
            "normal_x",
            "normal_y",
            "normal_z",
            "near_patch_eastings",
            "near_patch_northings",
            "near_patch_altitudes",
            "near_patch_elevations",
            "near_patch_valid",
            "near_patch_normal_x",
            "near_patch_normal_y",
            "near_patch_normal_z",
        ):
            np.asarray(getattr(self, name)).setflags(write=False)


def _extrema_lod_indices(values, maximum_points: int) -> np.ndarray:
    """Bound a dense angular series while retaining local peaks and valleys."""

    series = np.asarray(values)
    count = int(series.size)
    limit = max(2, int(maximum_points))
    if count <= limit:
        return np.arange(count, dtype=np.int32)

    # Two extrema per bucket preserve narrow ridges and valleys substantially
    # better than a uniform stride while keeping the Qt polygon size bounded.
    bucket_target = max(1, (limit - 2) // 2)
    stride = max(1, int(math.ceil(count / bucket_target)))
    bucket_count = int(math.ceil(count / stride))
    padded_count = bucket_count * stride
    finite = np.isfinite(series)
    low = np.full(padded_count, np.inf, dtype=np.float64)
    high = np.full(padded_count, -np.inf, dtype=np.float64)
    source = np.asarray(series, dtype=np.float64)
    low[:count] = np.where(finite, source, np.inf)
    high[:count] = np.where(finite, source, -np.inf)
    low = low.reshape(bucket_count, stride)
    high = high.reshape(bucket_count, stride)
    has_finite = np.any(np.isfinite(low), axis=1)
    starts = np.arange(bucket_count, dtype=np.int64) * stride
    minima = starts + np.argmin(low, axis=1)
    maxima = starts + np.argmax(high, axis=1)
    pairs = np.sort(np.stack((minima, maxima), axis=1), axis=1)
    selected = pairs[has_finite].reshape(-1)
    selected = np.concatenate(
        (np.asarray([0], dtype=np.int64), selected, np.asarray([count - 1]))
    )
    return np.unique(selected).astype(np.int32)


@dataclass(frozen=True)
class _TerrainSurfaceSpan:
    """One topologically continuous, projected terrain contribution."""

    row_index: int
    distance_m: float
    column_indices: np.ndarray
    x: np.ndarray
    bottom_x: np.ndarray
    top_y: np.ndarray
    bottom_y: np.ndarray
    source_vertex_count: int
    max_error_px: float

    def __post_init__(self) -> None:
        columns = np.asarray(self.column_indices, dtype=np.int32)
        x = np.asarray(self.x, dtype=np.float32)
        bottom_x = np.asarray(self.bottom_x, dtype=np.float32)
        top_y = np.asarray(self.top_y, dtype=np.float32)
        bottom_y = np.asarray(self.bottom_y, dtype=np.float32)
        if not (
            columns.shape
            == x.shape
            == bottom_x.shape
            == top_y.shape
            == bottom_y.shape
        ):
            raise ValueError("Terrain span arrays must have matching shapes")
        for array in (columns, x, bottom_x, top_y, bottom_y):
            array.setflags(write=False)
        object.__setattr__(self, "column_indices", columns)
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "bottom_x", bottom_x)
        object.__setattr__(self, "top_y", top_y)
        object.__setattr__(self, "bottom_y", bottom_y)


@dataclass(frozen=True)
class _TerrainGeometryMetrics:
    spans: int = 0
    source_samples: int = 0
    invalid_samples: int = 0
    occluded_samples: int = 0
    simplified_vertices: int = 0
    output_vertices: int = 0
    max_error_px: float = 0.0
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class _TerrainSurfaceGeometry:
    spans: tuple[_TerrainSurfaceSpan, ...]
    metrics: _TerrainGeometryMetrics


@dataclass(frozen=True)
class _TerrainTriangleGeometry:
    """Fully projected terrain triangles, independent of paint state."""

    xy: np.ndarray
    depth: np.ndarray
    vertex_rows: np.ndarray
    vertex_columns: np.ndarray
    # 0 indexes the polar colour grid; 1 indexes the Cartesian near patch.
    vertex_domain: np.ndarray
    metrics: _TerrainGeometryMetrics
    # Cache keys can outlive the geometry object through the resolved-material
    # LRU.  A retained token cannot be recycled like id(self) can.
    cache_token: object = field(
        default_factory=object,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for name in (
            "xy",
            "depth",
            "vertex_rows",
            "vertex_columns",
            "vertex_domain",
        ):
            value = np.asarray(getattr(self, name))
            value.setflags(write=False)
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class TerrainMaterialSamples:
    """Discrete material identity kept alongside its display-ready base colour."""

    base_rgba: np.ndarray
    valid: np.ndarray
    class_ids: np.ndarray
    categorical: np.ndarray
    source_indices: np.ndarray

    def __post_init__(self) -> None:
        valid = np.asarray(self.valid, dtype=bool)
        base_rgba = np.asarray(self.base_rgba, dtype=np.uint8)
        class_ids = np.asarray(self.class_ids, dtype=np.int64)
        categorical = np.asarray(self.categorical, dtype=bool)
        source_indices = np.asarray(self.source_indices, dtype=np.int16)
        if base_rgba.shape != valid.shape + (4,):
            raise ValueError("Material RGBA must match material geometry")
        for value in (class_ids, categorical, source_indices):
            if value.shape != valid.shape:
                raise ValueError("Material identity arrays must match RGBA geometry")
        for name, value in (
            ("base_rgba", base_rgba),
            ("valid", valid),
            ("class_ids", class_ids),
            ("categorical", categorical),
            ("source_indices", source_indices),
        ):
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class TerrainLightingGrid:
    """Brightness and source-specific exposure over aligned terrain vertices."""

    intensity: np.ndarray
    solar_exposure: np.ndarray
    lunar_exposure: np.ndarray
    factors: TerrainCelestialLightFactors


@dataclass(frozen=True)
class TerrainBaseMaterialCache:
    """Camera- and time-independent colours aligned with terrain geometry."""

    key: tuple
    polar: TerrainMaterialSamples
    near_patch: TerrainMaterialSamples
    polar_protected: np.ndarray
    near_patch_protected: np.ndarray
    owned_bytes: int = -1

    def __post_init__(self) -> None:
        for name in ("polar_protected", "near_patch_protected"):
            value = np.asarray(getattr(self, name), dtype=bool)
            value.setflags(write=False)
            object.__setattr__(self, name, value)

    @property
    def resident_bytes(self) -> int:
        if self.owned_bytes >= 0:
            return int(self.owned_bytes)
        return (
            _material_samples_size(self.polar)
            + _material_samples_size(self.near_patch)
            + int(self.polar_protected.nbytes)
            + int(self.near_patch_protected.nbytes)
        )


@dataclass(frozen=True)
class TerrainResolvedMaterialCache:
    """Screen material lookup reusable while the projected raster is stable."""

    key: tuple
    materials: TerrainMaterialSamples
    triangle_materials: TerrainMaterialSamples
    triangle_surface_xy: np.ndarray
    protected: np.ndarray

    @property
    def resident_bytes(self) -> int:
        return (
            _material_samples_size(self.materials)
            + _material_samples_size(self.triangle_materials)
            + int(np.asarray(self.triangle_surface_xy).nbytes)
            + int(np.asarray(self.protected).nbytes)
        )


def _material_samples_size(materials: TerrainMaterialSamples) -> int:
    """Return owned NumPy storage used by one material grid."""

    return sum(
        int(np.asarray(value).nbytes)
        for value in (
            materials.base_rgba,
            materials.valid,
            materials.class_ids,
            materials.categorical,
            materials.source_indices,
        )
    )


def _freeze_material_samples(
    materials: TerrainMaterialSamples,
) -> TerrainMaterialSamples:
    """Mark one derived material grid immutable without duplicating it."""

    for value in (
        materials.base_rgba,
        materials.valid,
        materials.class_ids,
        materials.categorical,
        materials.source_indices,
    ):
        np.asarray(value).setflags(write=False)
    return materials


def _owned_material_samples_size(
    material_grids: tuple[TerrainMaterialSamples, ...],
    shared_arrays,
) -> int:
    """Count only arrays owned by derived cache entries."""

    shared = tuple(
        np.asarray(value)
        for value in shared_arrays
        if value is not None
    )
    seen = set()
    total = 0
    for materials in material_grids:
        for value in (
            materials.base_rgba,
            materials.valid,
            materials.class_ids,
            materials.categorical,
            materials.source_indices,
        ):
            array = np.asarray(value)
            identity = id(array)
            if identity in seen:
                continue
            seen.add(identity)
            if any(np.shares_memory(array, source) for source in shared):
                continue
            total += int(array.nbytes)
    return total
