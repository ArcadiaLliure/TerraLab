"""
horizon_overlay.py  -  Multi-band terrain/mountain renderer

Renders the real DEM horizon profile as layered silhouettes with
atmospheric perspective, inspired by the Topo Horizon POC viewer.

Separated from village_overlay.py so that terrain rendering and
village-object rendering are independent concerns.
"""

import math
import os
import random
import time
from dataclasses import dataclass

import numpy as np
from PyQt5.QtCore import QObject, QPointF, Qt, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from scipy.ndimage import (
    convolve,
    distance_transform_edt,
    gaussian_filter,
    label as connected_components,
)

try:
    from numba import njit
except Exception:  # pragma: no cover - optional acceleration
    njit = None

from TerraLab.common.performance import (
    ByteLRU,
    DEFAULT_PERFORMANCE_BUDGET,
    PERFORMANCE_FLAGS,
)
from TerraLab.common.perf_events import append_perf_event
from TerraLab.config import ConfigManager
from TerraLab.terrain.render_pipeline import (
    SurfaceVisualStyle,
    TerrainCelestialLightContext,
    TerrainCelestialLightFactors,
    apply_vibrant_color_grade,
    atmospheric_fog_factor,
    compose_vertex_rgba,
    light_direction_enu,
    normalize_surface_visual_style,
    terrain_celestial_light_factors,
    vibrant_depth_haze_factor,
)
from TerraLab.terrain.land_cover.legends.category_info import (
    LandCoverCategoryInfo,
    category_info,
)
from TerraLab.terrain.land_cover.visual_styles import (
    VIBRANT_PALETTE_VERSION,
    preserve_small_region,
    vibrant_land_cover_rgba,
)
from TerraLab.terrain.representation import (
    TerrainGeometrySource,
    TerrainRepresentationMode,
    normalize_terrain_geometry_source,
    normalize_terrain_representation_mode,
)

try:
    from TerraLab.terrain.engine import (
        HorizonProfile,
        compute_polar_mesh_normals,
        load_profile,
    )

    HORIZON_ENGINE_AVAILABLE = True
except ImportError:
    HORIZON_ENGINE_AVAILABLE = False


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


def _rasterize_triangles_impl(xy, depth_values, width, height, supersample):
    """Reference z-buffer rasterizer using deterministic pixel-centre coverage."""

    out_width = int(width) * int(supersample)
    out_height = int(height) * int(supersample)
    depth = np.full((out_height, out_width), np.inf, dtype=np.float64)
    triangle_id = np.full((out_height, out_width), -1, dtype=np.int32)
    bary_u = np.zeros((out_height, out_width), dtype=np.float32)
    bary_v = np.zeros((out_height, out_width), dtype=np.float32)
    scale = float(supersample)
    for triangle in range(xy.shape[0]):
        x0, y0 = xy[triangle, 0, 0] * scale, xy[triangle, 0, 1] * scale
        x1, y1 = xy[triangle, 1, 0] * scale, xy[triangle, 1, 1] * scale
        x2, y2 = xy[triangle, 2, 0] * scale, xy[triangle, 2, 1] * scale
        denominator = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if not np.isfinite(denominator) or abs(denominator) <= 1e-12:
            continue
        min_x = max(0, int(math.ceil(min(x0, x1, x2) - 0.5)))
        max_x = min(out_width - 1, int(math.floor(max(x0, x1, x2) - 0.5)))
        min_y = max(0, int(math.ceil(min(y0, y1, y2) - 0.5)))
        max_y = min(out_height - 1, int(math.floor(max(y0, y1, y2) - 0.5)))
        if min_x > max_x or min_y > max_y:
            continue
        inverse = 1.0 / denominator
        for py in range(min_y, max_y + 1):
            sample_y = py + 0.5
            for px in range(min_x, max_x + 1):
                sample_x = px + 0.5
                u = ((y1 - y2) * (sample_x - x2) + (x2 - x1) * (sample_y - y2)) * inverse
                v = ((y2 - y0) * (sample_x - x2) + (x0 - x2) * (sample_y - y2)) * inverse
                w = 1.0 - u - v
                if u < -1e-10 or v < -1e-10 or w < -1e-10:
                    continue
                candidate = (
                    u * depth_values[triangle, 0]
                    + v * depth_values[triangle, 1]
                    + w * depth_values[triangle, 2]
                )
                if candidate < depth[py, px]:
                    depth[py, px] = candidate
                    triangle_id[py, px] = triangle
                    bary_u[py, px] = u
                    bary_v[py, px] = v
    return depth, triangle_id, bary_u, bary_v


def _rasterize_triangles_numpy(xy, depth_values, width, height, supersample):
    """Portable rasterizer with vectorized bounding boxes and no pixel loop."""

    xy = np.asarray(xy, dtype=np.float64)
    depth_values = np.asarray(depth_values, dtype=np.float64)
    out_width = int(width) * int(supersample)
    out_height = int(height) * int(supersample)
    depth = np.full((out_height, out_width), np.inf, dtype=np.float64)
    triangle_id = np.full((out_height, out_width), -1, dtype=np.int32)
    bary_u = np.zeros((out_height, out_width), dtype=np.float32)
    bary_v = np.zeros((out_height, out_width), dtype=np.float32)
    scale = float(supersample)
    for triangle in range(xy.shape[0]):
        vertices = xy[triangle] * scale
        x0, y0 = vertices[0]
        x1, y1 = vertices[1]
        x2, y2 = vertices[2]
        denominator = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if not np.isfinite(denominator) or abs(denominator) <= 1e-12:
            continue
        min_x = max(0, int(math.ceil(float(np.min(vertices[:, 0])) - 0.5)))
        max_x = min(
            out_width - 1,
            int(math.floor(float(np.max(vertices[:, 0])) - 0.5)),
        )
        min_y = max(0, int(math.ceil(float(np.min(vertices[:, 1])) - 0.5)))
        max_y = min(
            out_height - 1,
            int(math.floor(float(np.max(vertices[:, 1])) - 0.5)),
        )
        if min_x > max_x or min_y > max_y:
            continue
        sample_x = np.arange(min_x, max_x + 1, dtype=np.float64)[None, :] + 0.5
        sample_y = np.arange(min_y, max_y + 1, dtype=np.float64)[:, None] + 0.5
        inverse = 1.0 / denominator
        u = (
            (y1 - y2) * (sample_x - x2)
            + (x2 - x1) * (sample_y - y2)
        ) * inverse
        v = (
            (y2 - y0) * (sample_x - x2)
            + (x0 - x2) * (sample_y - y2)
        ) * inverse
        w = 1.0 - u - v
        covered = (u >= -1e-10) & (v >= -1e-10) & (w >= -1e-10)
        if not np.any(covered):
            continue
        candidate = (
            u * depth_values[triangle, 0]
            + v * depth_values[triangle, 1]
            + w * depth_values[triangle, 2]
        )
        depth_view = depth[min_y : max_y + 1, min_x : max_x + 1]
        nearer = covered & (candidate < depth_view)
        if not np.any(nearer):
            continue
        depth_view[nearer] = candidate[nearer]
        triangle_view = triangle_id[min_y : max_y + 1, min_x : max_x + 1]
        triangle_view[nearer] = triangle
        u_view = bary_u[min_y : max_y + 1, min_x : max_x + 1]
        v_view = bary_v[min_y : max_y + 1, min_x : max_x + 1]
        u_view[nearer] = u[nearer]
        v_view[nearer] = v[nearer]
    return depth, triangle_id, bary_u, bary_v


_rasterize_triangles_fast = (
    njit(cache=True, nogil=True)(_rasterize_triangles_impl) if njit is not None else None
)


def _rasterize_terrain_triangles(xy, depth, width, height, supersample=2):
    rasterizer = _rasterize_triangles_fast or _rasterize_triangles_numpy
    return rasterizer(
        np.asarray(xy, dtype=np.float64),
        np.asarray(depth, dtype=np.float64),
        int(width),
        int(height),
        int(supersample),
    )


def _interpolate_triangle_continuous_values(
    triangle_id,
    bary_u,
    bary_v,
    triangle_values,
    *,
    flat: bool = False,
):
    """Interpolate continuous per-vertex channels on a triangle map."""

    ids_grid = np.asarray(triangle_id, dtype=np.int32)
    values = np.asarray(triangle_values, dtype=np.float64)
    if values.ndim != 3 or values.shape[1] != 3:
        raise ValueError("Triangle values must have shape (triangles, 3, channels)")
    result = np.zeros(ids_grid.shape + (values.shape[2],), dtype=np.float64)
    covered = ids_grid >= 0
    if not np.any(covered):
        return result, covered
    ids = ids_grid[covered]
    selected = values[ids]
    if flat:
        result[covered] = np.mean(selected, axis=1)
        return result, covered
    u = np.asarray(bary_u, dtype=np.float64)[covered]
    v = np.asarray(bary_v, dtype=np.float64)[covered]
    w = 1.0 - u - v
    result[covered] = (
        u[:, None] * selected[:, 0]
        + v[:, None] * selected[:, 1]
        + w[:, None] * selected[:, 2]
    )
    return result, covered


def _interpolate_triangle_values(
    triangle_id,
    bary_u,
    bary_v,
    triangle_values,
    *,
    flat: bool = False,
):
    """Compatibility alias; categorical materials must use their resolver."""

    return _interpolate_triangle_continuous_values(
        triangle_id,
        bary_u,
        bary_v,
        triangle_values,
        flat=flat,
    )


def _resolve_triangle_material(
    triangle_id,
    bary_u,
    bary_v,
    triangle_materials: TerrainMaterialSamples,
    *,
    flat_continuous: bool = False,
) -> TerrainMaterialSamples:
    """Resolve one material per covered pixel without semantic interpolation.

    Equal categorical identities ``(source_index, class_id)`` are constant.
    Three continuous samples from one source retain smooth RGBA interpolation.
    Every mixed/source-boundary case uses the greatest barycentric weight;
    ``argmax`` gives vertex order as the deterministic tie-break.
    """

    ids_grid = np.asarray(triangle_id, dtype=np.int32)
    covered = ids_grid >= 0
    result_rgba = np.zeros(ids_grid.shape + (4,), dtype=np.uint8)
    result_valid = np.zeros(ids_grid.shape, dtype=bool)
    result_classes = np.full(ids_grid.shape, -1, dtype=np.int64)
    result_categorical = np.zeros(ids_grid.shape, dtype=bool)
    result_sources = np.full(ids_grid.shape, -1, dtype=np.int16)
    if not np.any(covered):
        return TerrainMaterialSamples(
            result_rgba,
            result_valid,
            result_classes,
            result_categorical,
            result_sources,
        )

    triangle_rgba = np.asarray(triangle_materials.base_rgba, dtype=np.uint8)
    triangle_valid = np.asarray(triangle_materials.valid, dtype=bool)
    triangle_classes = np.asarray(triangle_materials.class_ids, dtype=np.int64)
    triangle_categorical = np.asarray(
        triangle_materials.categorical, dtype=bool
    )
    triangle_sources = np.asarray(
        triangle_materials.source_indices, dtype=np.int16
    )
    triangle_all_valid = np.all(triangle_valid, axis=1)
    triangle_all_categorical = np.all(triangle_categorical, axis=1)
    triangle_all_continuous = np.all(~triangle_categorical, axis=1)
    triangle_same_source = np.all(
        triangle_sources == triangle_sources[:, :1], axis=1
    )
    triangle_same_class = np.all(
        triangle_classes == triangle_classes[:, :1], axis=1
    )
    triangle_constant_category = (
        triangle_all_valid
        & triangle_all_categorical
        & triangle_same_source
        & triangle_same_class
    )
    triangle_smooth_continuous = (
        triangle_all_valid & triangle_all_continuous & triangle_same_source
    )

    ids = ids_grid[covered]
    u = np.asarray(bary_u, dtype=np.float32)[covered]
    v = np.asarray(bary_v, dtype=np.float32)[covered]
    weights = np.column_stack((u, v, 1.0 - u - v))
    dominant = np.argmax(weights, axis=1)
    constant_category = triangle_constant_category[ids]
    smooth_continuous = triangle_smooth_continuous[ids]

    resolved = triangle_rgba[ids, dominant].copy()
    if np.any(constant_category):
        resolved[constant_category] = triangle_rgba[
            ids[constant_category], 0
        ]
    if np.any(smooth_continuous):
        selected_rgba = triangle_rgba[
            ids[smooth_continuous]
        ].astype(np.float32)
        if flat_continuous:
            continuous_rgba = np.mean(selected_rgba, axis=1)
        else:
            selected_weights = weights[smooth_continuous]
            continuous_rgba = np.sum(
                selected_rgba * selected_weights[..., None], axis=1
            )
        resolved[smooth_continuous] = np.clip(
            np.rint(continuous_rgba), 0, 255
        ).astype(np.uint8)

    selected_valid = triangle_valid[ids, dominant]
    selected_classes = triangle_classes[ids, dominant]
    selected_categorical = triangle_categorical[ids, dominant]
    selected_sources = triangle_sources[ids, dominant]
    selected_valid[constant_category | smooth_continuous] = True
    selected_classes[constant_category] = triangle_classes[
        ids[constant_category], 0
    ]
    selected_categorical[constant_category] = True
    uniform_pixels = constant_category | smooth_continuous
    selected_sources[uniform_pixels] = triangle_sources[
        ids[uniform_pixels], 0
    ]
    selected_classes[smooth_continuous] = -1
    selected_categorical[smooth_continuous] = False

    result_rgba[covered] = resolved
    result_valid[covered] = selected_valid
    result_classes[covered] = np.where(
        selected_categorical, selected_classes, -1
    )
    result_categorical[covered] = selected_categorical
    result_sources[covered] = selected_sources
    return TerrainMaterialSamples(
        result_rgba,
        result_valid,
        result_classes,
        result_categorical,
        result_sources,
    )


def _nearest_axis_indices(axis, values) -> np.ndarray:
    """Return stable nearest-neighbour indices on a one-dimensional axis."""

    coordinates = np.asarray(axis, dtype=np.float64).reshape(-1)
    requested = np.asarray(values, dtype=np.float64)
    if coordinates.size == 0:
        return np.full(requested.shape, -1, dtype=np.int32)
    order = np.argsort(coordinates, kind="stable")
    sorted_coordinates = coordinates[order]
    if sorted_coordinates.size > 1:
        steps = np.diff(sorted_coordinates)
        step = float(steps[0])
        if step > 0.0 and np.allclose(steps, step, rtol=1e-6, atol=1e-9):
            relative = (requested - sorted_coordinates[0]) / step
            rounded = np.floor(
                np.nextafter(relative + 0.5, -np.inf)
            ).astype(np.int64)
            return order[
                np.clip(rounded, 0, sorted_coordinates.size - 1)
            ].astype(np.int32, copy=False)
    insertion = np.searchsorted(sorted_coordinates, requested, side="left")
    left = np.clip(insertion - 1, 0, sorted_coordinates.size - 1)
    right = np.clip(insertion, 0, sorted_coordinates.size - 1)
    choose_right = (
        np.abs(sorted_coordinates[right] - requested)
        < np.abs(requested - sorted_coordinates[left])
    )
    selected = np.where(choose_right, right, left)
    return order[selected].astype(np.int32, copy=False)


def _nearest_circular_axis_indices(axis_degrees, values_degrees) -> np.ndarray:
    """Return stable nearest-neighbour indices on a circular degree axis."""

    coordinates = np.mod(
        np.asarray(axis_degrees, dtype=np.float64).reshape(-1), 360.0
    )
    requested = np.mod(np.asarray(values_degrees, dtype=np.float64), 360.0)
    if coordinates.size == 0:
        return np.full(requested.shape, -1, dtype=np.int32)
    order = np.argsort(coordinates, kind="stable")
    sorted_coordinates = coordinates[order]
    if sorted_coordinates.size > 1:
        wrapped = np.r_[
            np.diff(sorted_coordinates),
            sorted_coordinates[0] + 360.0 - sorted_coordinates[-1],
        ]
        step = float(wrapped[0])
        if step > 0.0 and np.allclose(
            wrapped, step, rtol=1e-6, atol=1e-9
        ):
            relative = (
                (requested - sorted_coordinates[0]) % 360.0
            ) / step
            rounded = np.floor(
                np.nextafter(relative + 0.5, -np.inf)
            ).astype(np.int64)
            return order[rounded % sorted_coordinates.size].astype(
                np.int32, copy=False
            )
    insertion = np.searchsorted(sorted_coordinates, requested, side="left")
    left = (insertion - 1) % sorted_coordinates.size
    right = insertion % sorted_coordinates.size
    left_distance = np.abs(
        (requested - sorted_coordinates[left] + 180.0) % 360.0 - 180.0
    )
    right_distance = np.abs(
        (requested - sorted_coordinates[right] + 180.0) % 360.0 - 180.0
    )
    selected = np.where(right_distance < left_distance, right, left)
    return order[selected].astype(np.int32, copy=False)


def _resolve_surface_material_impl(
    triangle_id,
    bary_u,
    bary_v,
    triangle_rgba,
    triangle_valid,
    triangle_classes,
    triangle_categorical,
    triangle_sources,
    triangle_constant_category,
    triangle_surface_xy,
    triangle_vertex_domain,
    polar_rgba,
    polar_valid,
    polar_classes,
    polar_categorical,
    polar_sources,
    polar_distances,
    polar_azimuths,
    patch_rgba,
    patch_valid,
    patch_classes,
    patch_categorical,
    patch_sources,
    patch_eastings,
    patch_northings,
):
    """Compiled categorical material lookup for the interactive hot path."""

    height, width = triangle_id.shape
    result_rgba = np.zeros((height, width, 4), dtype=np.uint8)
    result_valid = np.zeros((height, width), dtype=np.bool_)
    result_classes = np.full((height, width), -1, dtype=np.int64)
    result_categorical = np.zeros((height, width), dtype=np.bool_)
    result_sources = np.full((height, width), -1, dtype=np.int16)
    degrees_per_radian = 180.0 / math.pi
    for row in range(height):
        for column in range(width):
            triangle = int(triangle_id[row, column])
            if triangle < 0:
                continue
            if triangle_constant_category[triangle]:
                result_rgba[row, column] = triangle_rgba[triangle, 0]
                result_valid[row, column] = True
                result_classes[row, column] = triangle_classes[triangle, 0]
                result_categorical[row, column] = True
                result_sources[row, column] = triangle_sources[triangle, 0]
                continue
            u = float(bary_u[row, column])
            v = float(bary_v[row, column])
            w = 1.0 - u - v
            domain = int(triangle_vertex_domain[triangle, 0])
            uniform_domain = (
                int(triangle_vertex_domain[triangle, 1]) == domain
                and int(triangle_vertex_domain[triangle, 2]) == domain
            )
            east = (
                u * triangle_surface_xy[triangle, 0, 0]
                + v * triangle_surface_xy[triangle, 1, 0]
                + w * triangle_surface_xy[triangle, 2, 0]
            )
            north = (
                u * triangle_surface_xy[triangle, 0, 1]
                + v * triangle_surface_xy[triangle, 1, 1]
                + w * triangle_surface_xy[triangle, 2, 1]
            )
            material_row = -1
            material_column = -1
            if uniform_domain and domain == 0 and polar_distances.size:
                distance = math.sqrt(east * east + north * north)
                right = int(np.searchsorted(polar_distances, distance))
                left = max(0, min(polar_distances.size - 1, right - 1))
                right = max(0, min(polar_distances.size - 1, right))
                material_row = (
                    right
                    if abs(float(polar_distances[right]) - distance)
                    < abs(distance - float(polar_distances[left]))
                    else left
                )
                azimuth = (
                    math.atan2(east, north) * degrees_per_radian
                ) % 360.0
                right = int(np.searchsorted(polar_azimuths, azimuth))
                left = (right - 1) % polar_azimuths.size
                right = right % polar_azimuths.size
                left_distance = abs(
                    (
                        azimuth
                        - float(polar_azimuths[left])
                        + 180.0
                    )
                    % 360.0
                    - 180.0
                )
                right_distance = abs(
                    (
                        azimuth
                        - float(polar_azimuths[right])
                        + 180.0
                    )
                    % 360.0
                    - 180.0
                )
                material_column = (
                    right if right_distance < left_distance else left
                )
                if polar_valid[material_row, material_column]:
                    result_rgba[row, column] = polar_rgba[
                        material_row, material_column
                    ]
                    result_valid[row, column] = True
                    categorical = polar_categorical[
                        material_row, material_column
                    ]
                    result_categorical[row, column] = categorical
                    if categorical:
                        result_classes[row, column] = polar_classes[
                            material_row, material_column
                        ]
                    result_sources[row, column] = polar_sources[
                        material_row, material_column
                    ]
                    continue
            elif (
                uniform_domain
                and domain == 1
                and patch_eastings.size
                and patch_northings.size
            ):
                right = int(np.searchsorted(patch_eastings, east))
                left = max(0, min(patch_eastings.size - 1, right - 1))
                right = max(0, min(patch_eastings.size - 1, right))
                material_column = (
                    right
                    if abs(float(patch_eastings[right]) - east)
                    < abs(east - float(patch_eastings[left]))
                    else left
                )
                right = int(np.searchsorted(patch_northings, north))
                left = max(0, min(patch_northings.size - 1, right - 1))
                right = max(0, min(patch_northings.size - 1, right))
                material_row = (
                    right
                    if abs(float(patch_northings[right]) - north)
                    < abs(north - float(patch_northings[left]))
                    else left
                )
                if patch_valid[material_row, material_column]:
                    result_rgba[row, column] = patch_rgba[
                        material_row, material_column
                    ]
                    result_valid[row, column] = True
                    categorical = patch_categorical[
                        material_row, material_column
                    ]
                    result_categorical[row, column] = categorical
                    if categorical:
                        result_classes[row, column] = patch_classes[
                            material_row, material_column
                        ]
                    result_sources[row, column] = patch_sources[
                        material_row, material_column
                    ]
                    continue

            dominant = 0
            if v > u and v >= w:
                dominant = 1
            elif w > u and w > v:
                dominant = 2
            if triangle_valid[triangle, dominant]:
                result_rgba[row, column] = triangle_rgba[
                    triangle, dominant
                ]
                result_valid[row, column] = True
                categorical = triangle_categorical[triangle, dominant]
                result_categorical[row, column] = categorical
                if categorical:
                    result_classes[row, column] = triangle_classes[
                        triangle, dominant
                    ]
                result_sources[row, column] = triangle_sources[
                    triangle, dominant
                ]
    return (
        result_rgba,
        result_valid,
        result_classes,
        result_categorical,
        result_sources,
    )


_resolve_surface_material_fast = (
    njit(cache=True, nogil=True)(_resolve_surface_material_impl)
    if njit is not None
    else None
)


def _resolve_surface_material(
    triangle_id,
    bary_u,
    bary_v,
    triangle_materials: TerrainMaterialSamples,
    triangle_surface_xy,
    triangle_vertex_domain,
    polar_materials: TerrainMaterialSamples,
    polar_distances,
    polar_azimuths,
    patch_materials: TerrainMaterialSamples,
    patch_eastings,
    patch_northings,
    *,
    flat_continuous: bool = False,
) -> TerrainMaterialSamples:
    """Resolve semantic material in terrain space rather than screen space.

    Continuous RGB samples from one source retain barycentric interpolation.
    Every categorical, mixed, or cross-source pixel first interpolates its ENU
    ground coordinate and then performs a nearest lookup in the already
    prepared material grid.  No source raster is accessed during painting.
    """

    ids_grid = np.asarray(triangle_id, dtype=np.int32)
    covered = ids_grid >= 0
    if not np.any(covered):
        shape = ids_grid.shape
        return TerrainMaterialSamples(
            np.zeros(shape + (4,), dtype=np.uint8),
            np.zeros(shape, dtype=bool),
            np.full(shape, -1, dtype=np.int64),
            np.zeros(shape, dtype=bool),
            np.full(shape, -1, dtype=np.int16),
        )

    triangle_valid = np.asarray(triangle_materials.valid, dtype=bool)
    triangle_categorical = np.asarray(
        triangle_materials.categorical, dtype=bool
    )
    triangle_sources = np.asarray(
        triangle_materials.source_indices, dtype=np.int16
    )
    smooth_continuous = (
        np.all(triangle_valid, axis=1)
        & np.all(~triangle_categorical, axis=1)
        & np.all(triangle_sources == triangle_sources[:, :1], axis=1)
    )
    constant_category = (
        np.all(triangle_valid, axis=1)
        & np.all(triangle_categorical, axis=1)
        & np.all(triangle_sources == triangle_sources[:, :1], axis=1)
        & np.all(
            np.asarray(triangle_materials.class_ids, dtype=np.int64)
            == np.asarray(triangle_materials.class_ids, dtype=np.int64)[:, :1],
            axis=1,
        )
    )
    ids = ids_grid[covered]
    requires_surface_lookup = ~(
        smooth_continuous[ids] | constant_category[ids]
    )
    if not np.any(requires_surface_lookup):
        return _resolve_triangle_material(
            triangle_id,
            bary_u,
            bary_v,
            triangle_materials,
            flat_continuous=flat_continuous,
        )
    has_smooth_pixels = bool(np.any(smooth_continuous[ids]))
    has_direct_pixels = bool(np.any(~requires_surface_lookup))
    polar_distances_array = np.asarray(polar_distances, dtype=np.float64)
    polar_azimuths_array = np.mod(
        np.asarray(polar_azimuths, dtype=np.float64), 360.0
    )
    patch_eastings_array = np.asarray(patch_eastings, dtype=np.float64)
    patch_northings_array = np.asarray(patch_northings, dtype=np.float64)
    fast_axes = (
        np.all(np.diff(polar_distances_array) >= 0.0)
        and np.all(np.diff(polar_azimuths_array) >= 0.0)
        and np.all(np.diff(patch_eastings_array) >= 0.0)
        and np.all(np.diff(patch_northings_array) >= 0.0)
    )
    if (
        _resolve_surface_material_fast is not None
        and not has_smooth_pixels
        and fast_axes
    ):
        return TerrainMaterialSamples(
            *_resolve_surface_material_fast(
                ids_grid,
                np.asarray(bary_u, dtype=np.float32),
                np.asarray(bary_v, dtype=np.float32),
                np.asarray(triangle_materials.base_rgba, dtype=np.uint8),
                triangle_valid,
                np.asarray(triangle_materials.class_ids, dtype=np.int64),
                triangle_categorical,
                triangle_sources,
                constant_category,
                np.asarray(triangle_surface_xy, dtype=np.float64),
                np.asarray(triangle_vertex_domain, dtype=np.uint8),
                np.asarray(polar_materials.base_rgba, dtype=np.uint8),
                np.asarray(polar_materials.valid, dtype=bool),
                np.asarray(polar_materials.class_ids, dtype=np.int64),
                np.asarray(polar_materials.categorical, dtype=bool),
                np.asarray(polar_materials.source_indices, dtype=np.int16),
                polar_distances_array,
                polar_azimuths_array,
                np.asarray(patch_materials.base_rgba, dtype=np.uint8),
                np.asarray(patch_materials.valid, dtype=bool),
                np.asarray(patch_materials.class_ids, dtype=np.int64),
                np.asarray(patch_materials.categorical, dtype=bool),
                np.asarray(patch_materials.source_indices, dtype=np.int16),
                patch_eastings_array,
                patch_northings_array,
            )
        )
    resolved = (
        _resolve_triangle_material(
            triangle_id,
            bary_u,
            bary_v,
            triangle_materials,
            flat_continuous=flat_continuous,
        )
        if has_direct_pixels
        else None
    )

    covered_rows, covered_columns = np.nonzero(covered)
    target_rows = covered_rows[requires_surface_lookup]
    target_columns = covered_columns[requires_surface_lookup]
    target_ids = ids[requires_surface_lookup]
    u = np.asarray(bary_u, dtype=np.float64)[target_rows, target_columns]
    v = np.asarray(bary_v, dtype=np.float64)[target_rows, target_columns]
    w = 1.0 - u - v
    surface_vertices = np.asarray(
        triangle_surface_xy, dtype=np.float64
    )[target_ids]
    surface_xy = (
        u[:, None] * surface_vertices[:, 0]
        + v[:, None] * surface_vertices[:, 1]
        + w[:, None] * surface_vertices[:, 2]
    )

    domains = np.asarray(triangle_vertex_domain, dtype=np.uint8)[target_ids]
    uniform_domain = np.all(domains == domains[:, :1], axis=1)
    selected_domain = domains[:, 0]
    selected_rgba = np.zeros((len(target_ids), 4), dtype=np.uint8)
    selected_valid = np.zeros(len(target_ids), dtype=bool)
    selected_classes = np.full(len(target_ids), -1, dtype=np.int64)
    selected_categorical = np.zeros(len(target_ids), dtype=bool)
    selected_sources = np.full(len(target_ids), -1, dtype=np.int16)

    polar = uniform_domain & (selected_domain == 0)
    if np.any(polar):
        east = surface_xy[polar, 0]
        north = surface_xy[polar, 1]
        distance_indices = _nearest_axis_indices(
            polar_distances, np.hypot(east, north)
        )
        azimuth_indices = _nearest_circular_axis_indices(
            polar_azimuths, np.degrees(np.arctan2(east, north))
        )
        selected_rgba[polar] = polar_materials.base_rgba[
            distance_indices, azimuth_indices
        ]
        selected_valid[polar] = polar_materials.valid[
            distance_indices, azimuth_indices
        ]
        selected_classes[polar] = polar_materials.class_ids[
            distance_indices, azimuth_indices
        ]
        selected_categorical[polar] = polar_materials.categorical[
            distance_indices, azimuth_indices
        ]
        selected_sources[polar] = polar_materials.source_indices[
            distance_indices, azimuth_indices
        ]

    patch = uniform_domain & (selected_domain == 1)
    if np.any(patch):
        easting_indices = _nearest_axis_indices(
            patch_eastings, surface_xy[patch, 0]
        )
        northing_indices = _nearest_axis_indices(
            patch_northings, surface_xy[patch, 1]
        )
        selected_rgba[patch] = patch_materials.base_rgba[
            northing_indices, easting_indices
        ]
        selected_valid[patch] = patch_materials.valid[
            northing_indices, easting_indices
        ]
        selected_classes[patch] = patch_materials.class_ids[
            northing_indices, easting_indices
        ]
        selected_categorical[patch] = patch_materials.categorical[
            northing_indices, easting_indices
        ]
        selected_sources[patch] = patch_materials.source_indices[
            northing_indices, easting_indices
        ]

    replace = uniform_domain & selected_valid
    if not np.any(replace):
        return (
            resolved
            if resolved is not None
            else _resolve_triangle_material(
                triangle_id,
                bary_u,
                bary_v,
                triangle_materials,
                flat_continuous=flat_continuous,
            )
        )
    replace_rows = target_rows[replace]
    replace_columns = target_columns[replace]
    if resolved is None and np.all(replace):
        result_rgba = np.zeros(ids_grid.shape + (4,), dtype=np.uint8)
        result_valid = np.zeros(ids_grid.shape, dtype=bool)
        result_classes = np.full(ids_grid.shape, -1, dtype=np.int64)
        result_categorical = np.zeros(ids_grid.shape, dtype=bool)
        result_sources = np.full(ids_grid.shape, -1, dtype=np.int16)
    else:
        if resolved is None:
            resolved = _resolve_triangle_material(
                triangle_id,
                bary_u,
                bary_v,
                triangle_materials,
                flat_continuous=flat_continuous,
            )
        result_rgba = resolved.base_rgba.copy()
        result_valid = resolved.valid.copy()
        result_classes = resolved.class_ids.copy()
        result_categorical = resolved.categorical.copy()
        result_sources = resolved.source_indices.copy()
    result_rgba[replace_rows, replace_columns] = selected_rgba[replace]
    result_valid[replace_rows, replace_columns] = True
    result_categorical[replace_rows, replace_columns] = (
        selected_categorical[replace]
    )
    result_classes[replace_rows, replace_columns] = np.where(
        selected_categorical[replace], selected_classes[replace], -1
    )
    result_sources[replace_rows, replace_columns] = selected_sources[replace]
    return TerrainMaterialSamples(
        result_rgba,
        result_valid,
        result_classes,
        result_categorical,
        result_sources,
    )


def _triangle_vertical_minimum_y(vertices, sample_x: float) -> float:
    intersections = []
    triangle = np.asarray(vertices, dtype=np.float64)
    for index in range(3):
        x0, y0 = triangle[index]
        x1, y1 = triangle[(index + 1) % 3]
        minimum_x = min(x0, x1) - 1e-9
        maximum_x = max(x0, x1) + 1e-9
        if sample_x < minimum_x or sample_x > maximum_x:
            continue
        delta_x = x1 - x0
        if abs(delta_x) <= 1e-12:
            if abs(sample_x - x0) <= 1e-9:
                intersections.extend((float(y0), float(y1)))
            continue
        t = (sample_x - x0) / delta_x
        if -1e-9 <= t <= 1.0 + 1e-9:
            intersections.append(float(y0 + t * (y1 - y0)))
    return min(intersections) if intersections else float("nan")


def _geometry_horizon_impl(triangles, screen_width):
    """Compiled scalar envelope; work scales with crossed columns, not pixels."""

    horizon = np.full(int(screen_width), np.nan, dtype=np.float64)
    for triangle_index in range(triangles.shape[0]):
        triangle = triangles[triangle_index]
        minimum_x = min(triangle[0, 0], triangle[1, 0], triangle[2, 0])
        maximum_x = max(triangle[0, 0], triangle[1, 0], triangle[2, 0])
        minimum_column = max(0, int(math.ceil(minimum_x - 0.5)))
        maximum_column = min(
            int(screen_width) - 1, int(math.floor(maximum_x - 0.5))
        )
        for column in range(minimum_column, maximum_column + 1):
            sample_x = float(column) + 0.5
            local_minimum = np.inf
            for edge in range(3):
                next_edge = (edge + 1) % 3
                x0 = triangle[edge, 0]
                y0 = triangle[edge, 1]
                x1 = triangle[next_edge, 0]
                y1 = triangle[next_edge, 1]
                delta_x = x1 - x0
                if abs(delta_x) <= 1e-12:
                    continue
                parameter = (sample_x - x0) / delta_x
                if -1e-9 <= parameter <= 1.0 + 1e-9:
                    candidate = y0 + parameter * (y1 - y0)
                    if candidate < local_minimum:
                        local_minimum = candidate
            if local_minimum != np.inf:
                current = horizon[column]
                if not np.isfinite(current) or local_minimum < current:
                    horizon[column] = local_minimum
    return horizon


_geometry_horizon_fast = (
    njit(cache=True, nogil=True)(_geometry_horizon_impl) if njit is not None else None
)


def _geometry_horizon_y(triangle_id, triangle_xy) -> np.ndarray:
    """Resolve one geometric subpixel terrain/sky boundary per image column."""

    ids = np.asarray(triangle_id, dtype=np.int32)
    triangles = np.asarray(triangle_xy, dtype=np.float64)
    if _geometry_horizon_fast is not None:
        return _geometry_horizon_fast(triangles, int(ids.shape[1]))
    horizon = np.full(ids.shape[1], np.nan, dtype=np.float64)
    screen_width = ids.shape[1]
    for triangle in triangles:
        minimum_column = max(
            0, int(math.ceil(float(np.min(triangle[:, 0])) - 0.5))
        )
        maximum_column = min(
            screen_width - 1,
            int(math.floor(float(np.max(triangle[:, 0])) - 0.5)),
        )
        if minimum_column > maximum_column:
            continue
        columns = np.arange(minimum_column, maximum_column + 1, dtype=np.int32)
        sample_x = columns.astype(np.float64) + 0.5
        edge_y = np.full((3, columns.size), np.nan, dtype=np.float64)
        for edge in range(3):
            x0, y0 = triangle[edge]
            x1, y1 = triangle[(edge + 1) % 3]
            delta_x = x1 - x0
            if abs(delta_x) <= 1e-12:
                continue
            t = (sample_x - x0) / delta_x
            on_edge = (t >= -1e-9) & (t <= 1.0 + 1e-9)
            edge_y[edge, on_edge] = y0 + t[on_edge] * (y1 - y0)
        finite = np.any(np.isfinite(edge_y), axis=0)
        if not np.any(finite):
            continue
        local_minimum = np.min(
            np.where(np.isfinite(edge_y), edge_y, np.inf), axis=0
        )
        target_columns = columns[finite]
        current = horizon[target_columns]
        horizon[target_columns] = np.where(
            np.isfinite(current),
            np.minimum(current, local_minimum[finite]),
            local_minimum[finite],
        )
    return horizon


def _apply_horizon_coverage(
    rgba,
    triangle_id,
    triangle_xy,
    *,
    filter_width_px: float = 1.0,
    supersampling_factor: int = 1,
) -> np.ndarray:
    """Apply localized vertical coverage using the original triangle geometry."""

    result = np.asarray(rgba, dtype=np.uint8).copy()
    if result.ndim != 3 or result.shape[2] != 4:
        raise ValueError("Horizon coverage expects an RGBA image")
    horizon = _geometry_horizon_y(triangle_id, triangle_xy)
    width = max(0.25, float(filter_width_px))
    samples = max(1, int(supersampling_factor))
    height = result.shape[0]
    for column in np.flatnonzero(np.isfinite(horizon)):
        boundary = float(horizon[column])
        start = max(0, int(math.floor(boundary)))
        stop = min(height, int(math.ceil(boundary + width)))
        if start >= stop:
            continue
        source_rows = np.flatnonzero(result[:, column, 3] > 0)
        if source_rows.size == 0:
            continue
        source_row = int(source_rows[0])
        base_rgba = result[source_row, column].copy()
        rows = np.arange(start, stop, dtype=np.float64)
        coverage = np.clip((rows + 1.0 - boundary) / width, 0.0, 1.0)
        if samples > 1:
            coverage = np.rint(coverage * samples) / samples
        result[start:stop, column, :3] = base_rgba[:3]
        result[start:stop, column, 3] = np.clip(
            np.rint(float(base_rgba[3]) * coverage), 0.0, 255.0
        ).astype(np.uint8)
    return result


def _soften_categorical_edges(
    rgba,
    materials: TerrainMaterialSamples,
    covered,
    *,
    strength: float = 0.82,
    protected=None,
) -> np.ndarray:
    """Blend only the one-pixel contour between different land-cover classes.

    Material identity remains untouched for hit-testing and tooltips.  The
    display image gets a small Gaussian-like transition that rounds raster
    corners and tones down narrow projected streaks without softening the
    terrain, orthophotos, or the sky-facing silhouette.
    """

    image = np.asarray(rgba, dtype=np.uint8)
    category = (
        np.asarray(covered, dtype=bool)
        & np.asarray(materials.valid, dtype=bool)
        & np.asarray(materials.categorical, dtype=bool)
    )
    protected_mask = (
        np.zeros(category.shape, dtype=bool)
        if protected is None
        else np.asarray(protected, dtype=bool)
    )
    if protected_mask.shape != category.shape:
        raise ValueError("Protected edge mask must match material geometry")
    amount = max(0.0, min(1.0, float(strength)))
    if (
        image.ndim != 3
        or image.shape[2] != 4
        or image.shape[:2] != category.shape
        or amount <= 0.0
        or np.count_nonzero(category) < 2
    ):
        return image.copy()

    classes = np.asarray(materials.class_ids, dtype=np.int64)
    sources = np.asarray(materials.source_indices, dtype=np.int16)
    boundary = np.zeros(category.shape, dtype=bool)

    for dy, dx in (
        (-1, -1),
        (-1, 0),
        (-1, 1),
        (0, -1),
        (0, 1),
        (1, -1),
        (1, 0),
        (1, 1),
    ):
        neighbour_category = np.roll(category, (dy, dx), axis=(0, 1))
        if dy < 0:
            neighbour_category[dy:, :] = False
        elif dy > 0:
            neighbour_category[:dy, :] = False
        if dx < 0:
            neighbour_category[:, dx:] = False
        elif dx > 0:
            neighbour_category[:, :dx] = False

        overlap = category & neighbour_category
        if not np.any(overlap):
            continue
        neighbour_classes = np.roll(classes, (dy, dx), axis=(0, 1))
        neighbour_sources = np.roll(sources, (dy, dx), axis=(0, 1))
        boundary |= overlap & (
            (classes != neighbour_classes) | (sources != neighbour_sources)
        )

    if not np.any(boundary):
        return image.copy()
    boundary &= ~protected_mask
    if not np.any(boundary):
        return image.copy()
    boundary_rows, boundary_columns = np.nonzero(boundary)
    row_slice = slice(
        max(0, int(boundary_rows.min()) - 1),
        min(image.shape[0], int(boundary_rows.max()) + 2),
    )
    column_slice = slice(
        max(0, int(boundary_columns.min()) - 1),
        min(image.shape[1], int(boundary_columns.max()) + 2),
    )
    category_region = category[row_slice, column_slice]
    boundary_region = boundary[row_slice, column_slice]
    rgb = image[row_slice, column_slice, :3].astype(np.float32)
    kernel = np.asarray(
        ((1.0, 2.0, 1.0), (2.0, 4.0, 2.0), (1.0, 2.0, 1.0)),
        dtype=np.float32,
    )
    accumulated = convolve(
        rgb * category_region[..., None],
        kernel[..., None],
        mode="constant",
        cval=0.0,
    )
    weights = convolve(
        category_region.astype(np.float32),
        kernel,
        mode="constant",
        cval=0.0,
    )
    blurred = np.divide(
        accumulated,
        np.maximum(weights[..., None], 1.0),
        out=rgb.copy(),
        where=weights[..., None] > 0.0,
    )
    result = image.copy()
    local_delta = np.max(np.abs(rgb - blurred), axis=-1) / 255.0
    contrast_weight = np.clip(
        (local_delta - 0.06) / 0.28, 0.0, 1.0
    )
    contrast_weight = contrast_weight * contrast_weight * (
        3.0 - 2.0 * contrast_weight
    )
    adaptive_amount = amount * (0.52 + 0.48 * contrast_weight)
    result_rgb = (
        rgb[boundary_region]
        * (1.0 - adaptive_amount[boundary_region, None])
        + blurred[boundary_region]
        * adaptive_amount[boundary_region, None]
    )
    result_region = result[row_slice, column_slice, :3]
    result_region[boundary_region] = np.clip(
        np.rint(result_rgb), 0.0, 255.0
    ).astype(np.uint8)
    return result


def _regularize_categorical_regions(
    materials: TerrainMaterialSamples,
    covered,
    *,
    radius_px: float = 8.0,
    protected=None,
) -> TerrainMaterialSamples:
    """Round categorical regions and absorb narrow screen-space protrusions.

    A Gaussian support field is built for each visible ``(source, class)``
    identity.  Selecting the strongest local field reconstructs the discrete
    regions with rounded corners while preserving broad areas and exact class
    identities.  Continuous imagery and pixels outside terrain coverage are
    never modified.
    """

    valid = np.asarray(materials.valid, dtype=bool)
    categorical = np.asarray(materials.categorical, dtype=bool)
    category = np.asarray(covered, dtype=bool) & valid & categorical
    protected_mask = (
        np.zeros(category.shape, dtype=bool)
        if protected is None
        else np.asarray(protected, dtype=bool)
    )
    if protected_mask.shape != category.shape:
        raise ValueError("Protected category mask must match material geometry")
    protected_mask &= category
    radius = max(0.0, float(radius_px))
    if radius < 0.5 or np.count_nonzero(category) < 2:
        return materials

    classes = np.asarray(materials.class_ids, dtype=np.int64)
    sources = np.asarray(materials.source_indices, dtype=np.int16)
    category_sources = sources[category]
    category_classes = classes[category]
    identity_codes = (
        (category_sources.astype(np.int64) + 32_768) << 32
    ) | (category_classes & np.int64(0xFFFFFFFF))
    unique_codes, first_identity_indices = np.unique(
        identity_codes, return_index=True
    )
    identity_sources = category_sources[first_identity_indices]
    identity_classes = category_classes[first_identity_indices]
    if len(identity_sources) < 2:
        return materials

    category_rows, category_columns = np.nonzero(category)
    padding = max(1, int(math.ceil(radius * 2.5)))
    row_slice = slice(
        max(0, int(category_rows.min()) - padding),
        min(category.shape[0], int(category_rows.max()) + padding + 1),
    )
    column_slice = slice(
        max(0, int(category_columns.min()) - padding),
        min(category.shape[1], int(category_columns.max()) + padding + 1),
    )
    region_category = category[row_slice, column_slice]
    region_classes = classes[row_slice, column_slice]
    region_sources = sources[row_slice, column_slice]
    region_protected = protected_mask[row_slice, column_slice]
    region_rgba = np.asarray(
        materials.base_rgba[row_slice, column_slice], dtype=np.uint8
    )

    best_score = np.full(region_category.shape, -1.0, dtype=np.float32)
    second_score = np.full(region_category.shape, -1.0, dtype=np.float32)
    selected_identity = np.full(
        region_category.shape, -1, dtype=np.int16
    )
    second_identity = np.full(
        region_category.shape, -1, dtype=np.int16
    )
    identity_rgba = np.empty(
        (len(identity_sources), 4), dtype=np.uint8
    )
    for identity_index, (source_index, class_id) in enumerate(
        zip(identity_sources, identity_classes)
    ):
        identity_mask = (
            region_category
            & (region_sources == source_index)
            & (region_classes == class_id)
        )
        first = np.flatnonzero(identity_mask)
        if first.size == 0:
            continue
        identity_rgba[identity_index] = region_rgba.reshape(-1, 4)[
            int(first[0])
        ]
        score = gaussian_filter(
            identity_mask.astype(np.float32),
            sigma=radius,
            mode="constant",
            cval=0.0,
            truncate=2.5,
        )
        stronger = score > best_score
        second_score[stronger] = best_score[stronger]
        second_identity[stronger] = selected_identity[stronger]
        best_score[stronger] = score[stronger]
        selected_identity[stronger] = int(identity_index)
        runner_up = (~stronger) & (score > second_score)
        second_score[runner_up] = score[runner_up]
        second_identity[runner_up] = int(identity_index)

    # Restore the exact source identity on protected pixels before component
    # cleanup.  Thus narrow buildings, waterways, snow and wetlands survive
    # even when their surrounding regions are rounded.
    region_codes = (
        (region_sources.astype(np.int64) + 32_768) << 32
    ) | (region_classes & np.int64(0xFFFFFFFF))
    original_identity = np.searchsorted(unique_codes, region_codes)
    protected_in_range = (
        region_protected
        & (original_identity >= 0)
        & (original_identity < len(unique_codes))
    )
    protected_matches = np.zeros(region_category.shape, dtype=bool)
    protected_matches[protected_in_range] = (
        unique_codes[original_identity[protected_in_range]]
        == region_codes[protected_in_range]
    )
    selected_identity[protected_matches] = original_identity[
        protected_matches
    ].astype(np.int16)

    protected_identities = np.zeros(len(identity_sources), dtype=bool)
    if np.any(protected_matches):
        protected_identities[
            np.unique(selected_identity[protected_matches])
        ] = True

    minimum_region_area = max(
        4, int(round(math.pi * radius * radius * 0.5))
    )
    connectivity = np.ones((3, 3), dtype=np.uint8)
    for identity_index in range(len(identity_sources)):
        if protected_identities[identity_index]:
            continue
        component_map, component_count = connected_components(
            region_category & (selected_identity == identity_index),
            structure=connectivity,
        )
        if component_count == 0:
            continue
        component_sizes = np.bincount(component_map.reshape(-1))
        small_component = component_sizes < minimum_region_area
        small_component[0] = False
        replace = (
            small_component[component_map]
            & (second_identity >= 0)
            & ~region_protected
        )
        selected_identity[replace] = second_identity[replace]

    selected = selected_identity >= 0
    selected_sources = np.full(region_category.shape, -1, dtype=np.int16)
    selected_classes = np.full(region_category.shape, -1, dtype=np.int64)
    selected_sources[selected] = identity_sources[
        selected_identity[selected]
    ]
    selected_classes[selected] = identity_classes[
        selected_identity[selected]
    ]
    changed = (
        region_category
        & selected
        & (
            (region_sources != selected_sources)
            | (region_classes != selected_classes)
        )
    )
    if not np.any(changed):
        return materials

    result_rgba = np.asarray(materials.base_rgba, dtype=np.uint8).copy()
    result_classes = classes.copy()
    result_sources = sources.copy()
    rgba_region = result_rgba[row_slice, column_slice]
    class_region = result_classes[row_slice, column_slice]
    source_region = result_sources[row_slice, column_slice]
    rgba_region[changed] = identity_rgba[selected_identity[changed]]
    class_region[changed] = selected_classes[changed]
    source_region[changed] = selected_sources[changed]
    return TerrainMaterialSamples(
        result_rgba,
        valid.copy(),
        result_classes,
        categorical.copy(),
        result_sources,
    )


def _local_extrema_mask(values: np.ndarray) -> np.ndarray:
    """Return finite local extrema, including plateaus only at their edges."""

    values = np.asarray(values, dtype=np.float64)
    result = np.zeros(values.shape, dtype=bool)
    if values.size < 3:
        return result
    left = values[1:-1] - values[:-2]
    right = values[2:] - values[1:-1]
    finite = np.isfinite(left) & np.isfinite(right)
    result[1:-1] = finite & (
        ((left > 0.0) & (right <= 0.0))
        | ((left < 0.0) & (right >= 0.0))
    )
    return result


def _simplify_projected_boundaries(
    x: np.ndarray,
    top_y: np.ndarray,
    bottom_y: np.ndarray,
    tolerance_px: float = 0.75,
) -> tuple[np.ndarray, float]:
    """Simplify two projected boundaries with a shared, bounded-error index set."""

    x = np.asarray(x, dtype=np.float64)
    top_y = np.asarray(top_y, dtype=np.float64)
    bottom_y = np.asarray(bottom_y, dtype=np.float64)
    count = int(x.size)
    if count <= 2:
        return np.arange(count, dtype=np.int32), 0.0

    reversed_order = bool(x[0] > x[-1])
    if reversed_order:
        work_x = x[::-1]
        work_top = top_y[::-1]
        work_bottom = bottom_y[::-1]
    else:
        work_x = x
        work_top = top_y
        work_bottom = bottom_y

    selected = _local_extrema_mask(work_top) | _local_extrema_mask(work_bottom)
    selected[0] = True
    selected[-1] = True
    tolerance = min(1.0, max(0.0, float(tolerance_px)))
    max_error = float("inf")
    for _iteration in range(count):
        selected_indices = np.flatnonzero(selected)
        reconstructed_top = np.interp(
            work_x, work_x[selected_indices], work_top[selected_indices]
        )
        reconstructed_bottom = np.interp(
            work_x, work_x[selected_indices], work_bottom[selected_indices]
        )
        errors = np.maximum(
            np.abs(work_top - reconstructed_top),
            np.abs(work_bottom - reconstructed_bottom),
        )
        max_error = float(np.max(errors, initial=0.0))
        violations = (errors > tolerance) & ~selected
        if not np.any(violations):
            break
        previous_error = np.r_[-np.inf, errors[:-1]]
        next_error = np.r_[errors[1:], -np.inf]
        additions = (
            violations
            & (errors >= previous_error)
            & (errors >= next_error)
        )
        if not np.any(additions):
            additions[int(np.argmax(np.where(violations, errors, -np.inf)))] = True
        # Add error peaks rather than every violating sample. This retains the
        # bounded-error guarantee without degenerating into an almost full copy.
        selected |= additions

    work_indices = np.flatnonzero(selected).astype(np.int32)
    if reversed_order:
        indices = np.sort((count - 1 - work_indices).astype(np.int32))
    else:
        indices = work_indices
    return indices, max_error


def _contiguous_true_runs(mask: np.ndarray, adjacency: np.ndarray) -> list[tuple[int, int]]:
    """Return half-open true runs without crossing a broken column adjacency."""

    mask = np.asarray(mask, dtype=bool)
    adjacency = np.asarray(adjacency, dtype=bool)
    runs: list[tuple[int, int]] = []
    start = None
    for index, enabled in enumerate(mask):
        if enabled and start is None:
            start = index
        breaks_before = index > 0 and not bool(adjacency[index - 1])
        if start is not None and (not enabled or breaks_before):
            stop = index
            if breaks_before and enabled:
                if stop - start >= 1:
                    runs.append((start, stop))
                start = index
            else:
                if stop - start >= 1:
                    runs.append((start, stop))
                start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


def _build_terrain_surface_spans(
    *,
    distances: np.ndarray,
    column_indices: np.ndarray,
    azimuths: np.ndarray,
    projected_x: np.ndarray,
    projected_y: np.ndarray,
    valid: np.ndarray,
    height: float,
    improvement_px: float = 0.25,
    simplify_tolerance_px: float | None = 0.75,
    column_modulus: int | None = None,
) -> _TerrainSurfaceGeometry:
    """Build projected terrain spans without Qt or painter state."""

    started = time.perf_counter()
    distances = np.asarray(distances, dtype=np.float32)
    columns = np.asarray(column_indices, dtype=np.int32)
    azimuths = np.asarray(azimuths, dtype=np.float32)
    projected_x = np.asarray(projected_x, dtype=np.float32)
    projected_y = np.asarray(projected_y, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    shape = (distances.size, columns.size)
    x_is_grid = projected_x.shape == shape
    if (
        columns.size < 2
        or (projected_x.shape != (columns.size,) and not x_is_grid)
        or azimuths.shape != (columns.size,)
        or projected_y.shape != shape
        or valid.shape != shape
    ):
        return _TerrainSurfaceGeometry((), _TerrainGeometryMetrics())

    projected_x_grid = (
        projected_x
        if x_is_grid
        else np.broadcast_to(projected_x[None, :], shape)
    )
    finite = np.isfinite(projected_y) & np.isfinite(projected_x_grid)
    sample_valid = valid & finite
    az_deltas = np.diff(azimuths.astype(np.float64))
    positive_az = np.abs(az_deltas[np.isfinite(az_deltas) & (az_deltas != 0.0)])
    nominal_az_step = float(np.median(positive_az)) if positive_az.size else 1.0
    angular_adjacency = (
        np.isfinite(az_deltas)
        & (np.abs(az_deltas) <= nominal_az_step * 1.5 + 1e-6)
        & (np.diff(columns) == 1)
    )

    painted_limit = np.full(columns.shape, float(height) * 2.0, dtype=np.float32)
    painted_x = np.asarray(projected_x_grid[0], dtype=np.float32).copy()
    spans: list[_TerrainSurfaceSpan] = []
    invalid_samples = 0
    occluded_samples = 0
    simplified_vertices = 0
    output_vertices = 0
    maximum_error = 0.0

    for row_index, distance_m in enumerate(distances):
        row_x = projected_x_grid[row_index]
        row_dx = np.diff(row_x.astype(np.float64))
        finite_dx = row_dx[np.isfinite(row_dx) & (np.abs(row_dx) > 1e-6)]
        direction = float(np.sign(np.median(finite_dx))) if finite_dx.size else 1.0
        if direction == 0.0:
            direction = 1.0
        adjacency = (
            angular_adjacency
            & np.isfinite(row_dx)
            & (row_dx * direction > 0.0)
        )
        row_valid = sample_valid[row_index]
        missing_bottom_x = row_valid & ~np.isfinite(painted_x)
        painted_x[missing_bottom_x] = row_x[missing_bottom_x]
        invalid_samples += int(row_valid.size - np.count_nonzero(row_valid))
        improves = row_valid & (
            projected_y[row_index] < painted_limit - float(improvement_px)
        )
        occluded_samples += int(np.count_nonzero(row_valid & ~improves))
        if not np.any(improves):
            continue

        drawn_support = np.zeros(row_valid.shape, dtype=bool)
        for start, stop in _contiguous_true_runs(row_valid, adjacency):
            if stop - start < 2 or not np.any(improves[start:stop]):
                continue
            top_uses_current = (
                projected_y[row_index, start:stop] <= painted_limit[start:stop]
            )
            source_x = np.where(
                top_uses_current,
                row_x[start:stop],
                painted_x[start:stop],
            )
            source_bottom_x = painted_x[start:stop]
            source_top = np.minimum(
                projected_y[row_index, start:stop], painted_limit[start:stop]
            )
            # Sub-pixel overlap prevents raster cracks between depth layers.
            # Hidden parts remain degenerate apart from this overlap and cannot
            # alter the measured top envelope.
            source_bottom = painted_limit[start:stop] + 0.75
            if not (
                np.all(np.isfinite(source_x))
                and np.all(np.isfinite(source_bottom_x))
                and np.all(np.isfinite(source_top))
                and np.all(np.isfinite(source_bottom))
            ):
                continue
            if simplify_tolerance_px is None:
                keep = np.arange(stop - start, dtype=np.int32)
                error = 0.0
            else:
                keep, error = _simplify_projected_boundaries(
                    source_x,
                    source_top,
                    source_bottom,
                    tolerance_px=simplify_tolerance_px,
                )
            if keep.size < 2:
                continue
            source_count = int(stop - start)
            simplified_vertices += max(0, source_count - int(keep.size))
            output_vertices += int(keep.size) * 2
            maximum_error = max(maximum_error, float(error))
            local_columns = columns[start:stop][keep]
            if column_modulus is not None:
                local_columns = local_columns % int(column_modulus)
            spans.append(
                _TerrainSurfaceSpan(
                    row_index=int(row_index),
                    distance_m=float(distance_m),
                    column_indices=local_columns,
                    x=source_x[keep],
                    bottom_x=source_bottom_x[keep],
                    top_y=source_top[keep],
                    bottom_y=source_bottom[keep],
                    source_vertex_count=source_count,
                    max_error_px=float(error),
                )
            )
            drawn_support[start:stop] = True
        wins = drawn_support & (
            projected_y[row_index] <= painted_limit
        )
        painted_limit[wins] = projected_y[row_index, wins]
        painted_x[wins] = row_x[wins]

    elapsed = float(time.perf_counter() - started)
    metrics = _TerrainGeometryMetrics(
        spans=len(spans),
        source_samples=int(np.prod(shape)),
        invalid_samples=int(invalid_samples),
        occluded_samples=int(occluded_samples),
        simplified_vertices=int(simplified_vertices),
        output_vertices=int(output_vertices),
        max_error_px=float(maximum_error),
        elapsed_s=elapsed,
    )
    return _TerrainSurfaceGeometry(tuple(spans), metrics)


# ─── Layer definitions ───────────────────────────────────────────

# ─── Layer color palette ─────────────────────────────────────────────────────
#
# Color key-stops for the gradient, from index 0 (farthest/deepest) to 1 (nearest/ground).
# (night_rgb, day_rgb) tuples. We interpolate between these stops.
#
_PALETTE_STOPS = [
    # t=0.0  Deepest Haze (farthest)
    ((70, 82, 100), (178, 194, 210)),
    # t=0.25 Mid Haze
    ((58, 70, 88), (152, 172, 186)),
    # t=0.50 Mid-range Green-Blue transition
    ((44, 58, 70), (116, 138, 132)),
    # t=0.75 Near hills
    ((28, 42, 48), (82, 108, 86)),
    # t=1.0  Immediate Foreground (nearest)
    ((18, 32, 34), (62, 86, 64)),
]


def _palette_color(t: float):
    """
    Interpolate a (night_QColor, day_QColor) from the gradient palette at position t in [0, 1].
    t=0 → farthest (Haze Blue), t=1 → nearest (Forest Green).
    """
    stops = _PALETTE_STOPS
    n_seg = len(stops) - 1

    seg_t = t * n_seg
    seg_i = int(seg_t)
    seg_f = seg_t - seg_i

    if seg_i >= n_seg:
        seg_i = n_seg - 1
        seg_f = 1.0

    (nr0, ng0, nb0), (dr0, dg0, db0) = stops[seg_i]
    (nr1, ng1, nb1), (dr1, dg1, db1) = stops[seg_i + 1]

    def lerp(a, b, f):
        return int(a + (b - a) * f)

    night_c = QColor(
        lerp(nr0, nr1, seg_f), lerp(ng0, ng1, seg_f), lerp(nb0, nb1, seg_f)
    )
    day_c = QColor(
        lerp(dr0, dr1, seg_f), lerp(dg0, dg1, seg_f), lerp(db0, db1, seg_f)
    )
    return night_c, day_c


def generate_layer_defs(bands: list) -> list:
    """
    Given a list of band dicts (from engine.generate_bands), produce a LAYER_DEFS-compatible
    list of (band_id, night_QColor, day_QColor) tuples.

    Bands are expected in ASCENDING distance order (nearest first from engine),
    but LAYER_DEFS must be in DESCENDING order (farthest drawn first = painter z-order).
    """
    n = len(bands)
    result = []
    # Reverse so we draw farthest first
    for i, band in enumerate(reversed(bands)):
        # Use non-linear mapping (square root) to stretch near-colors (green) further
        # into the distance, as requested by the user.
        t_linear = i / max(n - 1, 1)
        t = math.sqrt(t_linear)
        # We want t=0 = farthest color, t=1 = nearest color
        # After reversing bands, i=0 is the farthest, so t=0 → farthest → correct.
        night_c, day_c = _palette_color(t)
        result.append((band["id"], night_c, day_c))
    return result


# Static sane default for use before a profile is loaded (20 bands)
LAYER_DEFS = generate_layer_defs(
    __import__(
        "TerraLab.terrain.engine", fromlist=["generate_bands"]
    ).generate_bands(
        20,
        max_dist_m=__import__(
            "TerraLab.terrain.visibility_range", fromlist=["resolve_visibility_range"]
        ).resolve_visibility_range(
            __import__(
                "TerraLab.terrain.visibility_range", fromlist=["TerrainRangeSettings"]
            ).TerrainRangeSettings(), 0.0
        ).resolved_radius_m,
    )
)


# Ground fill (solid color below the nearest horizon line)
GROUND_NIGHT = QColor(5, 10, 25)
GROUND_DAY = QColor(64, 82, 64)  # Matches closest band (muted terrain green)
ATMOSPHERIC_HAZE_NIGHT = QColor(36, 48, 68)
ATMOSPHERIC_HAZE_DAY = QColor(138, 166, 184)
EARTH_RADIUS_M = 6_371_000.0


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    """Linear interpolation between two QColors."""
    r = c1.red() + (c2.red() - c1.red()) * t
    g = c1.green() + (c2.green() - c1.green()) * t
    b = c1.blue() + (c2.blue() - c1.blue()) * t
    a = c1.alpha() + (c2.alpha() - c1.alpha()) * t
    return QColor(int(r), int(g), int(b), int(a))


def _with_alpha(color: QColor, alpha: int) -> QColor:
    result = QColor(color)
    result.setAlpha(max(0, min(255, int(alpha))))
    return result


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _atmospheric_haze_color(sky_color: QColor, t_night: float) -> QColor:
    haze_blue = _lerp_color(
        ATMOSPHERIC_HAZE_DAY,
        ATMOSPHERIC_HAZE_NIGHT,
        _clamp01(t_night),
    )
    return _lerp_color(haze_blue, sky_color, 0.22 + 0.18 * _clamp01(t_night))


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    if edge0 == edge1:
        return 1.0 if value >= edge1 else 0.0
    t = _clamp01((float(value) - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


def _lerp_piecewise(value: float, stops) -> float:
    if not stops:
        return 0.0
    if value <= stops[0][0]:
        return float(stops[0][1])
    for (x0, y0), (x1, y1) in zip(stops, stops[1:]):
        if value <= x1:
            t = _smoothstep(x0, x1, value)
            return float(y0 + (y1 - y0) * t)
    return float(stops[-1][1])


def _distance_haze_factor(distance_m: float) -> float:
    return _clamp01(
        _lerp_piecewise(
            max(0.0, float(distance_m or 0.0)),
            (
                (0.0, 0.00),
                (5_000.0, 0.05),
                (15_000.0, 0.24),
                (40_000.0, 0.46),
                (80_000.0, 0.66),
                (150_000.0, 0.78),
            ),
        )
    )


def _distance_haze_factors(distance_m) -> np.ndarray:
    distance = np.maximum(0.0, np.asarray(distance_m, dtype=np.float32))
    stops = (
        (0.0, 0.00),
        (5_000.0, 0.05),
        (15_000.0, 0.24),
        (40_000.0, 0.46),
        (80_000.0, 0.66),
        (150_000.0, 0.78),
    )
    result = np.full(distance.shape, stops[-1][1], dtype=np.float32)
    result = np.where(distance <= stops[0][0], stops[0][1], result)
    for (x0, y0), (x1, y1) in zip(stops, stops[1:]):
        mask = (distance > x0) & (distance <= x1)
        t = np.clip((distance - x0) / max(x1 - x0, 1e-6), 0.0, 1.0)
        t = t * t * (3.0 - 2.0 * t)
        result = np.where(mask, y0 + (y1 - y0) * t, result)
    return np.clip(result, 0.0, 1.0).astype(np.float32)


def _qcolor_from_rgba(value) -> QColor:
    rgba = np.asarray(value, dtype=np.uint8).reshape(-1)
    if rgba.size < 3:
        return QColor(0, 0, 0, 0)
    alpha = int(rgba[3]) if rgba.size >= 4 else 255
    return QColor(int(rgba[0]), int(rgba[1]), int(rgba[2]), alpha)


def _resolve_terrain_render_path(profile):
    """Resolve renderer from explicit representation and geometry provenance."""

    mode = normalize_terrain_representation_mode(
        getattr(profile, "representation_mode", TerrainRepresentationMode.RELIEF)
    )
    source = normalize_terrain_geometry_source(
        getattr(profile, "geometry_source", TerrainGeometrySource.LEGACY_UNKNOWN)
    )
    mesh = getattr(profile, "terrain_mesh", None)
    if mode is TerrainRepresentationMode.PROFILE or source in {
        TerrainGeometrySource.FLAT_FALLBACK,
        TerrainGeometrySource.PROCEDURAL_FALLBACK,
    }:
        return "profile", None, mode, source
    if mesh is None:
        return "profile_preview", None, mode, source
    return "relief", mesh, mode, source


def _sample_cache_value(cache, name: str, default=None):
    return cache.get(name, default) if isinstance(cache, dict) else getattr(cache, name, default)


def _vibrant_categorical_palette(
    materials: TerrainMaterialSamples, cache
) -> TerrainMaterialSamples:
    """Replace known official category colours with the Vibrant palette."""

    legends = tuple(
        _sample_cache_value(cache, "source_legend_ids", ()) or ()
    )
    categorical = (
        np.asarray(materials.valid, dtype=bool)
        & np.asarray(materials.categorical, dtype=bool)
    )
    if not legends or not np.any(categorical):
        return materials

    result_rgba = np.asarray(materials.base_rgba, dtype=np.uint8).copy()
    classes = np.asarray(materials.class_ids, dtype=np.int64)
    sources = np.asarray(materials.source_indices, dtype=np.int16)
    changed = False
    for source_index in np.unique(sources[categorical]):
        index = int(source_index)
        if not (0 <= index < len(legends)):
            continue
        source_mask = categorical & (sources == index)
        for class_id in np.unique(classes[source_mask]):
            color = vibrant_land_cover_rgba(
                str(legends[index] or ""), int(class_id)
            )
            if color is None:
                continue
            result_rgba[
                source_mask & (classes == int(class_id))
            ] = np.asarray(color, dtype=np.uint8)
            changed = True
    if not changed:
        return materials
    return TerrainMaterialSamples(
        result_rgba,
        np.asarray(materials.valid, dtype=bool).copy(),
        classes.copy(),
        np.asarray(materials.categorical, dtype=bool).copy(),
        sources.copy(),
    )


def _protected_categorical_regions(
    materials: TerrainMaterialSamples, cache
) -> np.ndarray:
    """Return original pixels whose semantic identity must stay intact."""

    legends = tuple(
        _sample_cache_value(cache, "source_legend_ids", ()) or ()
    )
    protected = np.zeros(np.asarray(materials.valid).shape, dtype=bool)
    categorical = (
        np.asarray(materials.valid, dtype=bool)
        & np.asarray(materials.categorical, dtype=bool)
    )
    if not legends or not np.any(categorical):
        return protected
    classes = np.asarray(materials.class_ids, dtype=np.int64)
    sources = np.asarray(materials.source_indices, dtype=np.int16)
    for source_index in np.unique(sources[categorical]):
        index = int(source_index)
        if not (0 <= index < len(legends)):
            continue
        source_mask = categorical & (sources == index)
        for class_id in np.unique(classes[source_mask]):
            if preserve_small_region(
                str(legends[index] or ""), int(class_id)
            ):
                protected |= source_mask & (classes == int(class_id))
    return protected


def _apply_categorical_territorial_variation(
    rgba,
    materials: TerrainMaterialSamples,
    world_x,
    world_y,
    valid_mask,
    *,
    strength: float = 1.0,
    luminance_variation: float = 0.045,
    hue_variation: float = 0.018,
    midscale_variation: float = 0.0,
    microscale_variation: float = 0.0,
    altitude_influence: float = 0.0,
    slope_influence: float = 0.0,
    snow_rock_blend: float = 0.0,
    water_shore_variation: float = 0.0,
    light_intensity=None,
    solar_exposure=None,
    elevation_m=None,
    normal_x=None,
    normal_y=None,
    normal_z=None,
    source_legend_ids=(),
    render_scale: float = 1.0,
    include_solar_response: bool = True,
) -> np.ndarray:
    """Build stable multiscale materials from class identity and DEM geometry."""

    image = np.asarray(rgba, dtype=np.uint8)
    valid = np.asarray(valid_mask, dtype=bool)
    categorical = (
        valid
        & np.asarray(materials.valid, dtype=bool)
        & np.asarray(materials.categorical, dtype=bool)
    )
    amount = max(0.0, min(1.0, float(strength)))
    luminance_amount = max(0.0, min(0.12, float(luminance_variation)))
    hue_amount = max(0.0, min(0.08, float(hue_variation)))
    midscale_amount = max(0.0, min(0.08, float(midscale_variation)))
    microscale_amount = max(0.0, min(0.03, float(microscale_variation)))
    altitude_amount = max(0.0, min(0.2, float(altitude_influence)))
    slope_amount = max(0.0, min(0.2, float(slope_influence)))
    snow_amount = max(0.0, min(0.75, float(snow_rock_blend)))
    water_amount = max(0.0, min(0.3, float(water_shore_variation)))
    if (
        image.shape[:2] != categorical.shape
        or image.shape[-1:] != (4,)
        or amount <= 0.0
        or not np.any(categorical)
    ):
        return image.copy()

    east = np.broadcast_to(
        np.asarray(world_x, dtype=np.float64), categorical.shape
    )
    north = np.broadcast_to(
        np.asarray(world_y, dtype=np.float64), categorical.shape
    )
    phase = (
        np.asarray(materials.class_ids, dtype=np.float64) % 37.0
    ) * 0.31
    territorial = (
        0.5
        + 0.25 * np.sin((east + north * 0.37) / 3_800.0 + phase)
        + 0.25 * np.cos((north - east * 0.21) / 9_100.0 - phase * 0.6)
    )
    territorial = np.clip(territorial, 0.0, 1.0)
    luminance_wave = territorial * 2.0 - 1.0
    chromatic_wave = (
        0.58
        * np.sin((east * 0.42 - north) / 6_700.0 - phase * 0.7)
        + 0.42
        * np.cos((east + north * 0.63) / 12_400.0 + phase * 0.4)
    )
    chromatic_wave = np.clip(chromatic_wave, -1.0, 1.0)
    if midscale_amount > 0.0:
        medium_wave = (
            0.55
            * np.sin((east * 0.76 + north * 0.31) / 620.0 + phase * 1.7)
            + 0.45
            * np.cos((north - east * 0.44) / 1_350.0 - phase * 1.1)
        )
        medium_wave = np.clip(medium_wave, -1.0, 1.0)
    else:
        medium_wave = np.zeros(categorical.shape, dtype=np.float32)
    if microscale_amount > 0.0:
        micro_wave = (
            0.57
            * np.sin((east - north * 0.58) / 145.0 + phase * 2.3)
            + 0.43
            * np.cos((north + east * 0.35) / 280.0 - phase * 1.9)
        )
        micro_wave = np.clip(micro_wave, -1.0, 1.0)
    else:
        micro_wave = np.zeros(categorical.shape, dtype=np.float32)
    material_wave = (
        luminance_amount * luminance_wave
        + midscale_amount * medium_wave
        + microscale_amount * micro_wave
    )
    factor = 1.0 + amount * material_wave

    result = image.copy()
    rgb = np.asarray(image[..., :3], dtype=np.float32) / 255.0
    varied = rgb * factor[..., None]
    channel_max = np.max(rgb, axis=-1)
    channel_min = np.min(rgb, axis=-1)
    saturation = np.divide(
        channel_max - channel_min,
        np.maximum(channel_max, 1e-6),
        out=np.zeros(categorical.shape, dtype=np.float32),
        where=channel_max > 1e-6,
    )
    dominant = np.argmax(rgb, axis=-1)
    green = (dominant == 1) & (saturation > 0.16)
    blue = (dominant == 2) & (saturation > 0.16)
    warm = (
        (dominant == 0)
        & (rgb[..., 1] > rgb[..., 2] * 1.06)
        & (saturation > 0.14)
    )
    hue_shift = amount * hue_amount * chromatic_wave
    # Green territories drift between fresh/cool and dry/warm; water varies
    # between deeper blue and atmospheric blue-green; earth gains a restrained
    # ochre/sienna oscillation.
    varied[..., 0] += hue_shift * 0.52 * green
    varied[..., 1] += hue_shift * 0.06 * green
    varied[..., 2] -= hue_shift * 0.28 * green
    varied[..., 0] -= hue_shift * 0.22 * blue
    varied[..., 1] += hue_shift * 0.30 * blue
    varied[..., 2] += hue_shift * 0.20 * blue
    varied[..., 0] += hue_shift * 0.30 * warm
    varied[..., 1] += hue_shift * 0.16 * warm
    varied[..., 2] -= hue_shift * 0.24 * warm

    if include_solar_response and solar_exposure is not None:
        exposure = np.clip(
            np.broadcast_to(
                np.asarray(solar_exposure, dtype=np.float32),
                categorical.shape,
            ),
            0.0,
            1.0,
        ) * amount
    elif include_solar_response and light_intensity is not None:
        light = np.broadcast_to(
            np.asarray(light_intensity, dtype=np.float32),
            categorical.shape,
        )
        exposure = np.clip((light - 1.02) / 0.27, 0.0, 1.0) * amount
        luminance = np.sum(
            varied
            * np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32),
            axis=-1,
        )
        varied = luminance[..., None] + (
            varied - luminance[..., None]
        ) * (1.0 - 0.025 * exposure[..., None])
        varied *= 1.0 + 0.008 * exposure[..., None]
    else:
        exposure = np.zeros(categorical.shape, dtype=np.float32)

    physical_detail = (
        midscale_amount
        + microscale_amount
        + altitude_amount
        + slope_amount
        + snow_amount
        + water_amount
    )
    if physical_detail <= 0.0:
        result_rgb = result[..., :3]
        result_rgb[categorical] = np.clip(
            np.rint(varied[categorical] * 255.0),
            0.0,
            255.0,
        ).astype(np.uint8)
        return result

    classes = np.asarray(materials.class_ids, dtype=np.int64)
    sources = np.asarray(materials.source_indices, dtype=np.int16)
    legends = tuple(source_legend_ids or ())
    if legends:
        s2glc = np.zeros(categorical.shape, dtype=bool)
        for source_index, legend_id in enumerate(legends):
            normalized = (
                str(legend_id or "").strip().lower().replace("-", "_")
            )
            if normalized in {
                "s2glc",
                "s2glc_2017",
                "s2glc_europe_2017",
            }:
                s2glc |= categorical & (sources == source_index)
    else:
        s2glc = categorical.copy()

    def _geometry_channel(value, default):
        if value is None:
            return np.full(categorical.shape, default, dtype=np.float32)
        return np.broadcast_to(
            np.asarray(value, dtype=np.float32), categorical.shape
        )

    elevation = _geometry_channel(elevation_m, 0.0)
    nx = _geometry_channel(normal_x, 0.0)
    ny = _geometry_channel(normal_y, 0.0)
    nz = np.clip(_geometry_channel(normal_z, 1.0), -1.0, 1.0)
    horizontal_normal = np.hypot(nx, ny)
    normal_length = np.maximum(
        np.sqrt(horizontal_normal * horizontal_normal + nz * nz), 1e-5
    )
    steepness = np.clip(horizontal_normal / normal_length, 0.0, 1.0)
    northness = np.divide(
        ny,
        np.maximum(horizontal_normal, 1e-5),
        out=np.zeros(categorical.shape, dtype=np.float32),
        where=horizontal_normal > 1e-5,
    )
    altitude = np.full(categorical.shape, 0.5, dtype=np.float32)
    elevation_valid = categorical & np.isfinite(elevation)
    if np.count_nonzero(elevation_valid) >= 2:
        low, high = np.nanpercentile(
            elevation[elevation_valid], (10.0, 90.0)
        )
        if float(high) > float(low) + 1e-3:
            altitude = np.clip(
                (elevation - float(low)) / float(high - low), 0.0, 1.0
            ).astype(np.float32)

    forest = s2glc & np.isin(classes, (82, 83))
    conifers = s2glc & (classes == 83)
    herbaceous = s2glc & (classes == 102)
    rock = s2glc & (classes == 121)
    snow = s2glc & (classes == 123)
    water = s2glc & (classes == 162)

    # Forest canopies gain broad density changes, cool shaded north faces and
    # subtle sunlit clearings without introducing pixel-scale grain.
    forest_density = (
        midscale_amount * medium_wave
        + microscale_amount * micro_wave
    ) * amount
    forest_cold = (
        np.clip(northness, 0.0, 1.0) * steepness * slope_amount * amount
    )
    varied *= 1.0 + (forest_density * forest)[..., None]
    varied[..., 0] -= 0.16 * forest_cold * forest
    varied[..., 1] -= 0.06 * forest_cold * forest
    varied[..., 2] += 0.10 * forest_cold * forest
    if include_solar_response:
        varied *= 1.0 - (
            0.8
            * (midscale_amount + microscale_amount)
            * conifers
            * (0.45 + 0.55 * (1.0 - exposure))
        )[..., None]

    # Grass alternates between fresh, sheltered greens and dry sun-facing
    # yellow-greens, using DEM orientation rather than arbitrary patches.
    if include_solar_response:
        grass_dry = (
            exposure * (0.45 + 0.55 * steepness) * slope_amount * amount
        )
        grass_fresh = (
            np.clip(northness, 0.0, 1.0)
            * (1.0 - exposure)
            * slope_amount
            * amount
        )
        varied[..., 0] += 0.28 * grass_dry * herbaceous
        varied[..., 1] += 0.08 * grass_dry * herbaceous
        varied[..., 2] -= 0.18 * grass_dry * herbaceous
        varied[..., 0] -= 0.10 * grass_fresh * herbaceous
        varied[..., 1] += 0.20 * grass_fresh * herbaceous
        varied[..., 2] += 0.06 * grass_fresh * herbaceous

    # Exposed high rock becomes lighter and slightly less chromatic while
    # retaining warm mineral variation in shade.
    mineral_exposure = (
        0.55 * altitude + 0.45 * steepness
    ) * altitude_amount * amount
    varied *= 1.0 + (0.65 * mineral_exposure * rock)[..., None]
    mineral_luminance = np.sum(
        varied
        * np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32),
        axis=-1,
    )
    rock_desaturation = (0.45 * mineral_exposure * rock)[..., None]
    varied = (
        varied * (1.0 - rock_desaturation)
        + mineral_luminance[..., None] * rock_desaturation
    )

    # Steep, low or strongly insolated snow reveals mineral substrate.  The
    # narrow support transition dirties its visual edge without changing the
    # categorical snow identity used by LOD and hit-testing.
    if snow_amount > 0.0 and np.any(snow):
        snow_support = gaussian_filter(
            snow.astype(np.float32),
            sigma=max(0.6, 1.15 * float(render_scale)),
            mode="nearest",
        )
        snow_edge = snow * np.clip(1.0 - snow_support, 0.0, 1.0)
        steep_snow = np.clip((steepness - 0.42) / 0.48, 0.0, 1.0)
        snow_melt = (
            0.46 * steep_snow
            + 0.24 * (1.0 - altitude)
            + (
                0.18 * exposure
                if include_solar_response
                else 0.0
            )
            + 0.12 * snow_edge
        )
        snow_mix = np.clip(
            snow_amount * amount * snow_melt * snow, 0.0, 0.72
        )[..., None]
        mineral = np.asarray((0.78, 0.745, 0.63), dtype=np.float32)
        varied = varied * (1.0 - snow_mix) + mineral * snow_mix
        cold_snow = (
            snow
            * np.clip(northness, 0.0, 1.0)
            * steepness
            * 0.018
            * amount
        )
        varied[..., 0] -= cold_snow
        varied[..., 2] += cold_snow

    # Water receives a shallow bright shoreline, a darker visual centre and
    # a small solar/sky response.  Category geometry remains unchanged.
    if water_amount > 0.0 and np.any(water):
        water_depth = distance_transform_edt(water)
        shoreline = np.exp(
            -np.maximum(water_depth - 1.0, 0.0)
            / max(1.0, 3.5 * float(render_scale))
        ).astype(np.float32)
        centre = np.clip(1.0 - shoreline, 0.0, 1.0)
        varied *= 1.0 - (
            water * centre * water_amount * 0.72
        )[..., None]
        sky_water = np.asarray((0.48, 0.72, 0.88), dtype=np.float32)
        shore_mix = (
            water * shoreline * water_amount * 0.62
        )[..., None]
        varied = varied * (1.0 - shore_mix) + sky_water * shore_mix
        if include_solar_response:
            varied += (
                water * exposure * water_amount * 0.16
            )[..., None]

    result_rgb = result[..., :3]
    result_rgb[categorical] = np.clip(
        np.rint(varied[categorical] * 255.0),
        0.0,
        255.0,
    ).astype(np.uint8)
    return result


def _apply_categorical_solar_response(
    rgba,
    materials: TerrainMaterialSamples,
    solar_exposure,
    valid_mask,
    *,
    strength: float,
    midscale_variation: float,
    microscale_variation: float,
    slope_influence: float,
    snow_rock_blend: float,
    water_shore_variation: float,
    normal_x=None,
    normal_y=None,
    normal_z=None,
    source_legend_ids=(),
) -> np.ndarray:
    """Apply only time-dependent material responses to cached base colours."""

    image = np.asarray(rgba, dtype=np.uint8)
    valid = np.asarray(valid_mask, dtype=bool)
    categorical = (
        valid
        & np.asarray(materials.valid, dtype=bool)
        & np.asarray(materials.categorical, dtype=bool)
    )
    if (
        image.shape[:2] != categorical.shape
        or image.shape[-1:] != (4,)
        or not np.any(categorical)
    ):
        return image.copy()

    amount = max(0.0, min(1.0, float(strength)))
    midscale_amount = max(
        0.0, min(0.08, float(midscale_variation))
    )
    microscale_amount = max(
        0.0, min(0.03, float(microscale_variation))
    )
    slope_amount = max(0.0, min(0.2, float(slope_influence)))
    snow_amount = max(0.0, min(0.75, float(snow_rock_blend)))
    water_amount = max(
        0.0, min(0.3, float(water_shore_variation))
    )
    exposure = np.clip(
        np.broadcast_to(
            np.asarray(solar_exposure, dtype=np.float32),
            categorical.shape,
        ),
        0.0,
        1.0,
    ) * amount

    classes = np.asarray(materials.class_ids, dtype=np.int64)
    sources = np.asarray(materials.source_indices, dtype=np.int16)
    legends = tuple(source_legend_ids or ())
    if legends:
        s2glc = np.zeros(categorical.shape, dtype=bool)
        for source_index, legend_id in enumerate(legends):
            normalized = (
                str(legend_id or "").strip().lower().replace("-", "_")
            )
            if normalized in {
                "s2glc",
                "s2glc_2017",
                "s2glc_europe_2017",
            }:
                s2glc |= categorical & (sources == source_index)
    else:
        s2glc = categorical.copy()

    def _normal_channel(value, default):
        if value is None:
            return np.full(categorical.shape, default, dtype=np.float32)
        return np.broadcast_to(
            np.asarray(value, dtype=np.float32), categorical.shape
        )

    nx = _normal_channel(normal_x, 0.0)
    ny = _normal_channel(normal_y, 0.0)
    nz = np.clip(_normal_channel(normal_z, 1.0), -1.0, 1.0)
    horizontal = np.hypot(nx, ny)
    normal_length = np.maximum(
        np.sqrt(horizontal * horizontal + nz * nz), 1e-5
    )
    steepness = np.clip(horizontal / normal_length, 0.0, 1.0)
    northness = np.divide(
        ny,
        np.maximum(horizontal, 1e-5),
        out=np.zeros(categorical.shape, dtype=np.float32),
        where=horizontal > 1e-5,
    )

    conifers = s2glc & (classes == 83)
    herbaceous = s2glc & (classes == 102)
    snow = s2glc & (classes == 123)
    water = s2glc & (classes == 162)
    result = image.copy()
    varied = np.asarray(image[..., :3], dtype=np.float32) / 255.0

    canopy_response = (
        0.8
        * (midscale_amount + microscale_amount)
        * conifers
        * (0.45 + 0.55 * (1.0 - exposure))
    )
    varied *= 1.0 - canopy_response[..., None]

    grass_dry = (
        exposure * (0.45 + 0.55 * steepness) * slope_amount * amount
    )
    grass_fresh = (
        np.clip(northness, 0.0, 1.0)
        * (1.0 - exposure)
        * slope_amount
        * amount
    )
    varied[..., 0] += 0.28 * grass_dry * herbaceous
    varied[..., 1] += 0.08 * grass_dry * herbaceous
    varied[..., 2] -= 0.18 * grass_dry * herbaceous
    varied[..., 0] -= 0.10 * grass_fresh * herbaceous
    varied[..., 1] += 0.20 * grass_fresh * herbaceous
    varied[..., 2] += 0.06 * grass_fresh * herbaceous

    if snow_amount > 0.0 and np.any(snow):
        solar_snow_mix = np.clip(
            snow_amount * amount * 0.18 * exposure * snow,
            0.0,
            0.24,
        )[..., None]
        mineral = np.asarray((0.78, 0.745, 0.63), dtype=np.float32)
        varied = (
            varied * (1.0 - solar_snow_mix)
            + mineral * solar_snow_mix
        )
    if water_amount > 0.0 and np.any(water):
        varied += (
            water * exposure * water_amount * 0.16
        )[..., None]

    result_rgb = result[..., :3]
    result_rgb[categorical] = np.clip(
        np.rint(varied[categorical] * 255.0),
        0.0,
        255.0,
    ).astype(np.uint8)
    return result


def _vibrant_relief_occlusion(
    elevation_m,
    normal_z,
    valid_mask,
    *,
    radius_px: float = 5.0,
    relief_scale_m: float = 42.0,
) -> np.ndarray:
    """Approximate broad DEM occlusion in valleys and terrain folds."""

    valid = np.asarray(valid_mask, dtype=bool)
    elevation = np.broadcast_to(
        np.asarray(elevation_m, dtype=np.float32), valid.shape
    )
    nz = np.clip(
        np.broadcast_to(np.asarray(normal_z, dtype=np.float32), valid.shape),
        0.0,
        1.0,
    )
    radius = max(0.0, float(radius_px))
    if radius < 0.5 or not np.any(valid):
        return np.zeros(valid.shape, dtype=np.float32)

    weights = gaussian_filter(
        valid.astype(np.float32),
        sigma=radius,
        mode="constant",
        cval=0.0,
        truncate=2.5,
    )
    local_mean = gaussian_filter(
        np.where(valid, elevation, 0.0),
        sigma=radius,
        mode="constant",
        cval=0.0,
        truncate=2.5,
    )
    local_mean = np.divide(
        local_mean,
        np.maximum(weights, 1e-4),
        out=elevation.copy(),
        where=weights > 1e-4,
    )
    depression = np.maximum(local_mean - elevation, 0.0)
    relief_scale = max(1.0, float(relief_scale_m))
    occlusion = np.clip(depression / relief_scale, 0.0, 1.0)
    occlusion = occlusion * occlusion * (3.0 - 2.0 * occlusion)
    fold_weight = 0.42 + 0.58 * np.sqrt(np.clip(1.0 - nz, 0.0, 1.0))
    return np.where(valid, occlusion * fold_weight, 0.0).astype(np.float32)


def _apply_vibrant_ambient_occlusion(
    rgba,
    occlusion,
    valid_mask,
    *,
    strength: float = 0.12,
) -> np.ndarray:
    """Apply subtle colour-preserving occlusion to Vibrant terrain only."""

    image = np.asarray(rgba, dtype=np.uint8)
    valid = np.asarray(valid_mask, dtype=bool)
    ao = np.clip(
        np.broadcast_to(np.asarray(occlusion, dtype=np.float32), valid.shape),
        0.0,
        1.0,
    )
    amount = max(0.0, min(0.4, float(strength)))
    if (
        image.shape[:2] != valid.shape
        or image.shape[-1:] != (4,)
        or amount <= 0.0
        or not np.any(valid & (ao > 0.0))
    ):
        return image.copy()

    result = image.copy()
    rgb = np.asarray(image[..., :3], dtype=np.float32) / 255.0
    shaded = rgb * (1.0 - amount * ao[..., None])
    result[..., :3][valid] = np.clip(
        np.rint(shaded[valid] * 255.0), 0.0, 255.0
    ).astype(np.uint8)
    return result


def _vibrant_valley_haze(
    occlusion,
    distance_m,
    valid_mask,
    *,
    maximum_distance_m: float,
    strength: float = 0.10,
) -> np.ndarray:
    """Return a restrained second atmospheric layer for distant hollows."""

    valid = np.asarray(valid_mask, dtype=bool)
    ao = np.clip(
        np.broadcast_to(np.asarray(occlusion, dtype=np.float32), valid.shape),
        0.0,
        1.0,
    )
    distance = np.maximum(
        np.broadcast_to(
            np.asarray(distance_m, dtype=np.float32), valid.shape
        ),
        0.0,
    )
    depth = np.sqrt(
        np.clip(distance / max(1.0, float(maximum_distance_m)), 0.0, 1.0)
    )
    amount = max(0.0, min(0.4, float(strength)))
    return np.where(
        valid,
        np.clip(ao * (0.25 + 0.75 * depth) * amount, 0.0, 1.0),
        0.0,
    ).astype(np.float32)


def _apply_vibrant_bloom(
    rgba,
    valid_mask,
    settings,
    *,
    render_scale: float = 1.0,
    light_intensity=None,
    distance_m=None,
    maximum_distance_m: float | None = None,
    daylight_factor: float = 1.0,
    moonlight_factor: float = 0.0,
) -> np.ndarray:
    """Add a selective, low-opacity additive glow to surface highlights."""

    image = np.asarray(rgba, dtype=np.uint8)
    valid = np.asarray(valid_mask, dtype=bool)
    if image.shape[:2] != valid.shape or image.shape[-1:] != (4,):
        raise ValueError("Vibrant bloom mask must match the RGBA image")
    strength = (
        float(settings.vibrant_bloom_strength)
        * float(settings.vibrant_intensity)
        * (
            max(0.0, min(1.0, float(daylight_factor)))
            + float(settings.vibrant_moon_bloom_scale)
            * max(0.0, min(1.0, float(moonlight_factor)))
        )
    )
    radius = float(settings.vibrant_bloom_radius_px) * max(
        0.1, float(render_scale)
    )
    if strength <= 0.0 or radius < 0.25 or not np.any(valid):
        return image.copy()

    rgb = np.asarray(image[..., :3], dtype=np.float32) / 255.0
    luminance = np.sum(
        rgb
        * np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32),
        axis=-1,
    )
    base_threshold = max(
        0.0, min(0.999, float(settings.vibrant_bloom_threshold))
    )
    sunlight = np.zeros(valid.shape, dtype=np.float32)
    if light_intensity is not None:
        light = np.broadcast_to(
            np.asarray(light_intensity, dtype=np.float32), valid.shape
        )
        sunlight = np.clip((light - 0.98) / 0.33, 0.0, 1.0)
    distance_haze = np.zeros(valid.shape, dtype=np.float32)
    if distance_m is not None:
        distance_haze = np.broadcast_to(
            vibrant_depth_haze_factor(
                distance_m,
                settings,
                maximum_distance_m=maximum_distance_m,
            ),
            valid.shape,
        ).astype(np.float32)
    threshold = np.clip(
        base_threshold - 0.022 * sunlight - 0.012 * distance_haze,
        0.0,
        0.999,
    )
    bright = np.clip(
        (luminance - threshold) / np.maximum(1e-6, 1.0 - threshold),
        0.0,
        1.0,
    )
    bright = bright * bright * (3.0 - 2.0 * bright)
    if light_intensity is not None:
        bright *= 0.78 + 0.22 * sunlight
    bright *= valid
    if not np.any(bright > 0.0):
        return image.copy()

    glow_source = rgb * bright[..., None]
    blurred = gaussian_filter(
        glow_source,
        sigma=(radius, radius, 0.0),
        mode="constant",
        cval=0.0,
        truncate=2.5,
    )
    support = gaussian_filter(
        valid.astype(np.float32),
        sigma=radius,
        mode="constant",
        cval=0.0,
        truncate=2.5,
    )
    blurred = np.divide(
        blurred,
        np.maximum(support[..., None], 1e-4),
        out=np.zeros_like(blurred),
        where=support[..., None] > 1e-4,
    )
    result = image.copy()
    result_rgb = result[..., :3]
    glow = np.clip(blurred * strength, 0.0, 1.0)
    composed = rgb + glow
    result_rgb[valid] = np.clip(
        np.rint(composed[valid] * 255.0),
        0.0,
        255.0,
    ).astype(np.uint8)
    return result


def _surface_cache_has_categorical_material(cache) -> bool:
    """Return whether a prepared surface cache contains categorical material."""

    for prefix in ("visual", "relief", "near_patch"):
        categorical = _sample_cache_value(cache, f"{prefix}_categorical")
        valid = _sample_cache_value(cache, f"{prefix}_valid")
        if categorical is None:
            continue
        categorical = np.asarray(categorical, dtype=bool)
        if valid is not None and np.shape(valid) == categorical.shape:
            categorical = categorical & np.asarray(valid, dtype=bool)
        if np.any(categorical):
            return True
    return False


def _solar_shading_strength(sun_alt: float) -> float:
    alt = float(sun_alt if sun_alt is not None else -90.0)
    twilight_side_light = 0.34 * _smoothstep(-12.0, 0.0, alt)
    daylight = 0.66 * _smoothstep(0.0, 22.0, alt)
    return _clamp01(twilight_side_light + daylight)


def _terrain_direct_strength(sun_alt: float) -> float:
    alt = float(sun_alt if sun_alt is not None else -90.0)
    if alt <= -6.0:
        return 0.0
    twilight = 0.10 * _smoothstep(-6.0, 0.0, alt)
    daylight = 0.90 * _smoothstep(0.0, 22.0, alt)
    return _clamp01(twilight + daylight)


def _shade_color(color: QColor, factor: float, sky_color: QColor | None = None) -> QColor:
    factor = max(0.55, min(1.35, float(factor)))
    if factor < 1.0:
        result = QColor(
            int(color.red() * factor),
            int(color.green() * factor),
            int(color.blue() * factor),
            color.alpha(),
        )
        return result

    target = sky_color if sky_color is not None else QColor(255, 255, 255)
    return _lerp_color(color, target, min(0.32, (factor - 1.0) * 1.35))


def _parse_band_max_from_id(band_id: str) -> float:
    """
    Extreu la distància màxima en metres de l'ID d'una banda generada per generate_bands().

    Format esperat:  zone_minFmt_maxFmt
    Exemples:
        'gnd_0_71'       → 71.0
        'near_144_208'   → 208.0
        'mid_1.5k_2k'    → 2000.0
        'far_25k_38k'    → 38000.0
        'haze_111k_150k' → 150000.0
    Retorna 9999.0 si no es pot parsejar.
    """

    def _dist_str_to_m(s: str) -> float:
        s = s.strip()
        try:
            if "k" in s:
                return float(s.replace("k", "")) * 1000.0
            return float(s)
        except ValueError:
            return 9999.0

    parts = band_id.rsplit(
        "_", 2
    )  # zona + min + max (el max és l'últim segment)
    if len(parts) >= 3:
        return _dist_str_to_m(parts[1]), _dist_str_to_m(parts[2])
    return 0.0, 9999.0


# ─── Band data wrapper ───────────────────────────────────────────


class _BandPoints:
    """Holds (az_deg, elev_deg) points for one profile band."""

    VOID_THRESHOLD = -80.0  # DEM voids are stored as ≈ -90°

    def __init__(self, profile, band_id, vert_exaggeration=5.0):
        self.band_id = band_id
        # Parsegem min/max de l'ID (ex: "far_25k_38k")
        self.band_min, self.band_max = _parse_band_max_from_id(band_id)
        self.points, self.valid_mask = self._build(
            self._profile_band_points(profile, band_id), profile, vert_exaggeration
        )
        self.surface_points, self.surface_valid_mask = self._build(
            self._profile_band_points(profile, band_id, surface=True),
            profile,
            vert_exaggeration,
        )

    # ── private ──

    @staticmethod
    def _profile_band_points(profile, band_id, *, surface=False):
        """Return NumPy arrays directly, avoiding millions of Python tuples."""

        angle_key = "surface_angles" if surface else "angles"
        azimuths = np.asarray(getattr(profile, "azimuths", ()), dtype=np.float32)
        for band in getattr(profile, "bands", ()) or ():
            if str(band.get("id", "")) != str(band_id) or angle_key not in band:
                continue
            angles = np.asarray(band[angle_key], dtype=np.float32)
            if angles.shape != azimuths.shape:
                break
            elevations = np.where(
                angles <= -np.pi / 2.0,
                -10.0,
                np.rad2deg(angles),
            ).astype(np.float32)
            return azimuths, elevations

        getter_name = "get_band_surface_points" if surface else "get_band_points"
        getter = getattr(profile, getter_name, None)
        return getter(band_id) if callable(getter) else []

    def _build(self, raw, profile, vert_exag):
        if raw is None or len(raw) == 0:
            return (None, None), None

        # Unpack raw data (list of (az, elev))
        # Check if raw is already a numpy array from the engine
        if isinstance(raw, tuple) and len(raw) == 2:
            az = np.asarray(raw[0], dtype=np.float32)
            elev = np.asarray(raw[1], dtype=np.float32)
        elif isinstance(raw, np.ndarray):
            az = raw[:, 0]
            elev = raw[:, 1]
        else:
            az = np.array([pt[0] for pt in raw], dtype=np.float32)
            elev = np.array([pt[1] for pt in raw], dtype=np.float32)
        resolved_mask = getattr(profile, "resolved_mask", None)
        if resolved_mask is not None and len(resolved_mask) == len(az):
            valid_mask = np.asarray(resolved_mask, dtype=bool)
        else:
            valid_mask = np.ones_like(az, dtype=bool)

        # Handle voids
        h = np.where(elev < self.VOID_THRESHOLD, -20.0, elev * vert_exag)

        # Ensure perfect 360-degree closure
        if len(az) > 0 and az[0] == 0 and az[-1] < 360:
            az = np.append(az, 360.0)
            h = np.append(h, h[0])
            valid_mask = np.append(valid_mask, valid_mask[0])

        # Ensure sorting for polygon continuity
        sort_idx = np.argsort(az)
        return (az[sort_idx], h[sort_idx]), valid_mask[sort_idx]


# ─── Main overlay class ──────────────────────────────────────────


class HorizonOverlay(QObject):
    """
    Renders terrain silhouettes using a Hybrid Projection:
    - X: Linear mapping based on Azimuth (fixes fisheye 'squeeze')
    - Y: Vertical displacement from the Sky's horizon curve (keeps registration)
    """

    request_update = pyqtSignal()

    def __init__(
        self,
        parent=None,
        horizon_profile_path=None,
        vert_exaggeration=1.0,
        allow_procedural_fallback=True,
        terrain_surface_opaque=True,
    ):
        super().__init__(parent)
        self.vert_exaggeration = vert_exaggeration
        self.allow_procedural_fallback = bool(allow_procedural_fallback)
        self.terrain_surface_opaque = bool(terrain_surface_opaque)
        self._layers = (
            []
        )  # list of (_BandPoints, night_col, day_col)
        self.profile = None  # Store reference to the current profile
        self._loaded = False
        # Retained as a private compatibility attribute for older diagnostics;
        # it is no longer a hard cap that can truncate visible terrain.
        self._max_terrain_surface_quads = 4500
        self._last_surface2d_quads = 0
        self._last_surface2d_vertices = 0
        self._last_surface2d_max_error_px = 0.0
        self._last_surface2d_geometry_s = 0.0
        self._last_surface2d_paint_s = 0.0
        self._last_terrain_color_s = 0.0
        self._last_terrain_raster_s = 0.0
        self._last_horizon_antialias_s = 0.0
        self._last_terrain_total_s = 0.0
        self._last_material_resolution_s = 0.0
        self._last_base_material_cache_hit = False
        self._last_lighting_cache_hit = False
        self._last_resolved_material_cache_hit = False
        self._last_raster_cache_hit = False
        self._last_frame_cache_hit = False
        self._terrain_base_material_builds = 0
        self._terrain_lighting_builds = 0
        self._terrain_resolved_material_builds = 0
        self._terrain_raster_builds = 0
        self._terrain_frame_cache_hits = 0
        self._terrain_frame_cache_misses = 0
        self._terrain_material_image = None
        self._terrain_surface_diagnostics = {}
        self._terrain_resolved_materials = None
        self._terrain_geometry_cache_key = None
        self._terrain_geometry_cache = None
        self._terrain_polygon_cache_key = None
        self._terrain_polygon_cache = None
        self._terrain_polygon_cache_geometry = None
        self._profile_polygon_cache_view_key = None
        self._profile_polygon_cache = {}
        self._profile_category_hit_cache = {}
        self._profile_image_cache_key = None
        self._profile_image_cache = None
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        self._terrain_material_image = None
        self._terrain_surface_diagnostics = {}
        self._terrain_resolved_materials = None
        self._terrain_surface_image_drawn = 0
        self._terrain_raster_cache_key = None
        self._terrain_raster_cache = None
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._terrain_normal_cache_key = None
        self._terrain_normal_cache = None
        self._terrain_render_asset = None
        self._terrain_shade_cache = ByteLRU(
            max(16 * 1024**2, DEFAULT_PERFORMANCE_BUDGET.transient_bytes // 4)
        )
        self._terrain_base_material_cache = ByteLRU(
            max(16 * 1024**2, DEFAULT_PERFORMANCE_BUDGET.transient_bytes // 6)
        )
        self._terrain_resolved_material_cache = ByteLRU(
            max(32 * 1024**2, DEFAULT_PERFORMANCE_BUDGET.transient_bytes // 4)
        )
        self.render_settings = ConfigManager().get_terrain_render_settings()

        if not HORIZON_ENGINE_AVAILABLE:
            print(
                "[HorizonOverlay] Horizon Engine NOT available (ImportError)."
            )

        if horizon_profile_path and HORIZON_ENGINE_AVAILABLE:
            print(
                f"[HorizonOverlay] Attempting to load profile from: {horizon_profile_path}"
            )
            if not os.path.exists(horizon_profile_path):
                print(
                    f"[HorizonOverlay] ERROR: Profile file not found at {horizon_profile_path}"
                )

            try:
                profile = load_profile(horizon_profile_path)
                if profile is not None:
                    self.profile = profile
                    if PERFORMANCE_FLAGS.relief_cached:
                        self._terrain_render_asset = self._prepare_terrain_render_asset(
                            getattr(profile, "terrain_mesh", None)
                        )
                    print(
                        f"[HorizonOverlay] Profile loaded. Processing layers..."
                    )
                    for band_id, night_c, day_c in LAYER_DEFS:
                        bp = _BandPoints(profile, band_id, vert_exaggeration)
                        if bp.points[0] is not None:
                            self._layers.append((bp, night_c, day_c))
                        else:
                            print(
                                f"[HorizonOverlay]   Band '{band_id}': no data, skipped"
                            )
                    self._loaded = bool(self._layers)
                else:
                    print(f"[HorizonOverlay] load_profile returned None.")
            except Exception as e:
                print(f"[HorizonOverlay] Exception loading profile: {e}")

        if not self._layers and self.allow_procedural_fallback:
            print(
                "[HorizonOverlay] No real data loaded — activating procedural fallback."
            )
            self._build_procedural_fallback()

    # ── public API ──

    def set_terrain_surface_opaque(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.terrain_surface_opaque:
            return
        self.terrain_surface_opaque = enabled
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        self.request_update.emit()

    def reload_render_settings(self) -> None:
        """Reload terrain-only settings and invalidate colour-derived caches."""

        previous_static_key = self._static_material_settings_key()
        previous_lighting_key = self._terrain_lighting_settings_key()
        previous_resolved_key = (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            ),
            str(self.render_settings.terrain_shading_mode),
            round(
                float(
                    self.render_settings
                    .categorical_region_smoothing_radius_px
                ),
                6,
            ),
        )
        self.render_settings = ConfigManager().get_terrain_render_settings()
        next_static_key = self._static_material_settings_key()
        next_lighting_key = self._terrain_lighting_settings_key()
        next_resolved_key = (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            ),
            str(self.render_settings.terrain_shading_mode),
            round(
                float(
                    self.render_settings
                    .categorical_region_smoothing_radius_px
                ),
                6,
            ),
        )
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._profile_image_cache_key = None
        self._profile_image_cache = None
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        if previous_lighting_key != next_lighting_key:
            self._terrain_shade_cache.clear()
        if previous_static_key != next_static_key:
            self._terrain_base_material_cache.clear()
            self._terrain_resolved_material_cache.clear()
        elif previous_resolved_key != next_resolved_key:
            self._terrain_resolved_material_cache.clear()
        self.request_update.emit()

    def set_profile(self, profile, layer_defs=None):
        """Update the overlay with a new HorizonProfile object (e.g. from background worker).

        Args:
            profile: HorizonProfile with baked bands
            layer_defs: Optional list of (band_id, night_QColor, day_QColor).
                        Generated by overlay.generate_layer_defs(bands). If None, uses LAYER_DEFS.
        """
        if profile is None:
            return
        self.profile = profile
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._terrain_normal_cache_key = None
        self._terrain_normal_cache = None
        self._terrain_geometry_cache_key = None
        self._terrain_geometry_cache = None
        self._terrain_polygon_cache_key = None
        self._terrain_polygon_cache = None
        self._terrain_polygon_cache_geometry = None
        self._profile_polygon_cache_view_key = None
        self._profile_polygon_cache.clear()
        self._profile_category_hit_cache.clear()
        self._profile_image_cache_key = None
        self._profile_image_cache = None
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        self._terrain_raster_cache_key = None
        self._terrain_raster_cache = None
        self._terrain_render_asset = (
            self._prepare_terrain_render_asset(getattr(profile, "terrain_mesh", None))
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        self._terrain_shade_cache.clear()
        self._terrain_base_material_cache.clear()
        self._terrain_resolved_material_cache.clear()

        effective_defs = layer_defs if layer_defs is not None else LAYER_DEFS

        now_mono = float(time.monotonic())
        last_log = float(getattr(self, "_last_set_profile_log_mono", 0.0))
        if (now_mono - last_log) >= 2.0:
            print(
                f"[HorizonOverlay] Updating profile for {profile.observer_lat}, {profile.observer_lon} ({len(effective_defs)} layers)"
            )
            self._last_set_profile_log_mono = now_mono
        self._layers.clear()

        try:
            for band_id, night_c, day_c in effective_defs:
                bp = _BandPoints(profile, band_id, self.vert_exaggeration)
                if bp.points[0] is not None:
                    self._layers.append((bp, night_c, day_c))

            self._loaded = bool(self._layers)
            self.request_update.emit()

        except Exception as e:
            print(f"[HorizonOverlay] Error setting profile: {e}")

    def clear_profile(self):
        """Executa el metode clear_profile de la classe HorizonOverlay.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        self.profile = None
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._terrain_normal_cache_key = None
        self._terrain_normal_cache = None
        self._terrain_geometry_cache_key = None
        self._terrain_geometry_cache = None
        self._terrain_polygon_cache_key = None
        self._terrain_polygon_cache = None
        self._terrain_polygon_cache_geometry = None
        self._profile_polygon_cache_view_key = None
        self._profile_polygon_cache.clear()
        self._profile_category_hit_cache.clear()
        self._profile_image_cache_key = None
        self._profile_image_cache = None
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        self._terrain_render_asset = None
        self._terrain_shade_cache.clear()
        self._terrain_base_material_cache.clear()
        self._terrain_resolved_material_cache.clear()
        self._layers.clear()
        self._loaded = False
        if self.allow_procedural_fallback:
            self._build_procedural_fallback()
        self.request_update.emit()

    @staticmethod
    def _category_metadata(
        cache, class_id: int, source_index: int
    ) -> LandCoverCategoryInfo | None:
        if class_id < 0 or source_index < 0:
            return None
        source_ids = tuple(
            _sample_cache_value(cache, "source_ids", ()) or ()
        )
        source_names = tuple(
            _sample_cache_value(cache, "source_names", ()) or ()
        )
        legend_ids = tuple(
            _sample_cache_value(cache, "source_legend_ids", ()) or ()
        )
        if source_index >= len(source_ids):
            return None
        source_name = (
            source_names[source_index]
            if source_index < len(source_names)
            else source_ids[source_index]
        )
        legend_id = (
            legend_ids[source_index]
            if source_index < len(legend_ids)
            else ""
        )
        return category_info(
            legend_id,
            int(class_id),
            source_name=str(source_name or source_ids[source_index]),
            locale="ca",
        )

    @staticmethod
    def _category_grid_value(
        cache,
        prefix: str,
        row: int,
        column: int,
    ) -> tuple[int, int] | None:
        classes = _sample_cache_value(cache, f"{prefix}_class_ids")
        categorical = _sample_cache_value(
            cache, f"{prefix}_categorical"
        )
        sources = _sample_cache_value(
            cache, f"{prefix}_source_indices"
        )
        if classes is None or categorical is None or sources is None:
            return None
        class_grid = np.asarray(classes)
        categorical_grid = np.asarray(categorical, dtype=bool)
        source_grid = np.asarray(sources)
        if (
            class_grid.ndim != 2
            or categorical_grid.shape != class_grid.shape
            or source_grid.shape != class_grid.shape
        ):
            return None
        row = int(row)
        column = int(column) % max(1, int(class_grid.shape[1]))
        if not (0 <= row < class_grid.shape[0]):
            return None
        if not bool(categorical_grid[row, column]):
            return None
        class_id = int(class_grid[row, column])
        source_index = int(source_grid[row, column])
        if class_id < 0 or source_index < 0:
            return None
        return class_id, source_index

    def _relief_category_value(
        self,
        cache,
        row: int,
        column: int,
        mesh_shape: tuple[int, int],
    ) -> tuple[int, int] | None:
        visual_classes = _sample_cache_value(
            cache, "visual_class_ids"
        )
        visual_categorical = _sample_cache_value(
            cache, "visual_categorical"
        )
        visual_sources = _sample_cache_value(
            cache, "visual_source_indices"
        )
        if (
            visual_classes is not None
            and visual_categorical is not None
            and visual_sources is not None
            and np.shape(visual_classes) == tuple(mesh_shape)
            and np.shape(visual_categorical) == tuple(mesh_shape)
            and np.shape(visual_sources) == tuple(mesh_shape)
        ):
            return self._category_grid_value(
                cache, "visual", row, column
            )

        classes = _sample_cache_value(cache, "relief_class_ids")
        categorical = _sample_cache_value(
            cache, "relief_categorical"
        )
        sources = _sample_cache_value(
            cache, "relief_source_indices"
        )
        if classes is None or categorical is None or sources is None:
            return None
        classes = np.asarray(classes)
        categorical = np.asarray(categorical, dtype=bool)
        sources = np.asarray(sources)
        if (
            classes.ndim != 2
            or categorical.shape != classes.shape
            or sources.shape != classes.shape
            or not classes.size
        ):
            return None
        sampled_rows = np.asarray(
            _sample_cache_value(
                cache,
                "relief_distance_indices",
                np.arange(classes.shape[0]),
            ),
            dtype=np.int32,
        )
        sampled_columns = np.asarray(
            _sample_cache_value(
                cache,
                "relief_azimuth_indices",
                np.arange(classes.shape[1]),
            ),
            dtype=np.int32,
        ) % max(1, int(mesh_shape[1]))
        nearest_row = int(np.argmin(np.abs(sampled_rows - int(row))))
        circular = np.abs(
            sampled_columns - (int(column) % max(1, int(mesh_shape[1])))
        )
        circular = np.minimum(
            circular, max(1, int(mesh_shape[1])) - circular
        )
        nearest_column = int(np.argmin(circular))
        if not bool(categorical[nearest_row, nearest_column]):
            return None
        class_id = int(classes[nearest_row, nearest_column])
        source_index = int(sources[nearest_row, nearest_column])
        return (
            (class_id, source_index)
            if class_id >= 0 and source_index >= 0
            else None
        )

    def _profile_category_value(
        self, cache, band_id: str, azimuth_deg: float
    ) -> tuple[int, int] | None:
        classes = _sample_cache_value(cache, "profile_class_ids")
        categorical = _sample_cache_value(
            cache, "profile_categorical"
        )
        sources = _sample_cache_value(
            cache, "profile_source_indices"
        )
        if classes is None or categorical is None or sources is None:
            return None
        classes = np.asarray(classes)
        categorical = np.asarray(categorical, dtype=bool)
        sources = np.asarray(sources)
        if (
            classes.ndim != 2
            or categorical.shape != classes.shape
            or sources.shape != classes.shape
            or not classes.size
        ):
            return None
        profile = getattr(self, "profile", None)
        bands = tuple(getattr(profile, "bands", ()) or ())
        target_band = next(
            (
                index
                for index, band in enumerate(bands)
                if str(band.get("id", "")) == str(band_id)
            ),
            0,
        )
        sampled_bands = np.asarray(
            _sample_cache_value(
                cache,
                "profile_band_indices",
                np.arange(classes.shape[0]),
            ),
            dtype=np.int32,
        )
        matches = np.flatnonzero(sampled_bands == int(target_band))
        if not matches.size:
            return None
        sampled_row = int(matches[0])
        profile_azimuths = np.asarray(
            getattr(profile, "azimuths", ()), dtype=np.float64
        )
        sampled_indices = np.asarray(
            _sample_cache_value(
                cache,
                "profile_azimuth_indices",
                np.arange(classes.shape[1]),
            ),
            dtype=np.int32,
        )
        if (
            profile_azimuths.size == 0
            or sampled_indices.size != classes.shape[1]
            or np.any(sampled_indices < 0)
            or np.any(sampled_indices >= profile_azimuths.size)
        ):
            return None
        sampled_azimuths = profile_azimuths[sampled_indices]
        circular = np.abs(
            (
                (
                    sampled_azimuths
                    - (float(azimuth_deg) % 360.0)
                    + 180.0
                )
                % 360.0
            )
            - 180.0
        )
        column = int(np.argmin(circular))
        if not bool(categorical[sampled_row, column]):
            return None
        class_id = int(classes[sampled_row, column])
        source_index = int(sources[sampled_row, column])
        return (
            (class_id, source_index)
            if class_id >= 0 and source_index >= 0
            else None
        )

    def _profile_category_at_screen(
        self, cache, point_x: float, point_y: float
    ) -> LandCoverCategoryInfo | None:
        point = QPointF(float(point_x), float(point_y))
        # Profile bands are painted far-to-near. Reverse that order to resolve
        # the uppermost visible polygon at the cursor.
        for band_pts, _night, _day in reversed(self._layers):
            entries = self._profile_category_hit_cache.get(
                ("band", id(band_pts)), ()
            )
            for polygon, screen_x, azimuths in reversed(entries):
                if not polygon.containsPoint(point, Qt.OddEvenFill):
                    continue
                nearest = int(
                    np.argmin(
                        np.abs(
                            np.asarray(screen_x, dtype=np.float64)
                            - float(point_x)
                        )
                    )
                )
                value = self._profile_category_value(
                    cache,
                    str(getattr(band_pts, "band_id", "")),
                    float(np.asarray(azimuths)[nearest]),
                )
                if value is not None:
                    return self._category_metadata(
                        cache, value[0], value[1]
                    )
                return None
        return None

    def category_at_screen(
        self, x: float, y: float
    ) -> LandCoverCategoryInfo | None:
        """Return the already-sampled visible category below a screen point.

        This method is deliberately cache-only: it cannot open a GeoTIFF,
        enqueue a worker request or interpolate semantic class identifiers.
        """

        cache = getattr(
            getattr(self, "profile", None), "surface_samples", None
        )
        if cache is None:
            return None
        geometry = self._terrain_surface_image_geometry
        if geometry is None:
            return self._profile_category_at_screen(
                cache, float(x), float(y)
            )
        asset = self._terrain_render_asset
        if asset is None:
            return None
        point_x = float(x)
        point_y = float(y)

        if isinstance(geometry, _TerrainTriangleGeometry):
            display_materials = self._terrain_resolved_materials
            display_image = self._terrain_surface_image_cache
            if display_materials is not None and display_image is not None:
                material_height, material_width = (
                    display_materials.valid.shape
                )
                material_x = int(
                    math.floor(
                        point_x
                        * material_width
                        / max(1, int(display_image.width()))
                    )
                )
                material_y = int(
                    math.floor(
                        point_y
                        * material_height
                        / max(1, int(display_image.height()))
                    )
                )
                if (
                    0 <= material_x < material_width
                    and 0 <= material_y < material_height
                    and bool(
                        display_materials.valid[material_y, material_x]
                    )
                    and bool(
                        display_materials.categorical[
                            material_y, material_x
                        ]
                    )
                ):
                    class_id = int(
                        display_materials.class_ids[
                            material_y, material_x
                        ]
                    )
                    source_index = int(
                        display_materials.source_indices[
                            material_y, material_x
                        ]
                    )
                    if class_id >= 0 and source_index >= 0:
                        return self._category_metadata(
                            cache, class_id, source_index
                        )
            if (
                self._terrain_raster_cache is None
                or not isinstance(self._terrain_raster_cache_key, tuple)
                or len(self._terrain_raster_cache_key) < 4
                or self._terrain_raster_cache_key[0] != id(geometry)
            ):
                return None
            render_width = int(self._terrain_raster_cache_key[1])
            render_height = int(self._terrain_raster_cache_key[2])
            triangle_id, bary_u, bary_v = self._terrain_raster_cache
            image = self._terrain_surface_image_cache
            display_width = int(image.width()) if image is not None else render_width
            display_height = int(image.height()) if image is not None else render_height
            px = int(math.floor(point_x * render_width / max(1, display_width)))
            py = int(math.floor(point_y * render_height / max(1, display_height)))
            if not (0 <= px < render_width and 0 <= py < render_height):
                return None
            triangle = int(triangle_id[py, px])
            if triangle < 0:
                return None
            weights = np.asarray(
                [
                    float(bary_u[py, px]),
                    float(bary_v[py, px]),
                    1.0
                    - float(bary_u[py, px])
                    - float(bary_v[py, px]),
                ],
                dtype=np.float64,
            )
            mesh_shape = tuple(
                np.asarray(asset.elevations).shape
            )
            # At a class boundary, use the categorical vertex with the
            # greatest barycentric contribution. Class codes are never mixed.
            for vertex in np.argsort(-weights):
                row = int(geometry.vertex_rows[triangle, vertex])
                column = int(
                    geometry.vertex_columns[triangle, vertex]
                )
                if int(geometry.vertex_domain[triangle, vertex]) == 1:
                    value = self._category_grid_value(
                        cache, "near_patch", row, column
                    )
                else:
                    value = self._relief_category_value(
                        cache, row, column, mesh_shape
                    )
                if value is not None:
                    return self._category_metadata(
                        cache, value[0], value[1]
                    )
            return None

        if isinstance(geometry, _TerrainSurfaceGeometry):
            point = QPointF(point_x, point_y)
            mesh_shape = tuple(
                np.asarray(asset.elevations).shape
            )
            # Polygons are painted in order; the last containing polygon is
            # the visible upper contribution at this pixel.
            for span, polygon in reversed(
                self._terrain_polygons_for_geometry(geometry)
            ):
                if not polygon.containsPoint(point, Qt.OddEvenFill):
                    continue
                nearest = int(
                    np.argmin(np.abs(np.asarray(span.x) - point_x))
                )
                column = int(span.column_indices[nearest])
                value = self._relief_category_value(
                    cache, int(span.row_index), column, mesh_shape
                )
                if value is not None:
                    return self._category_metadata(
                        cache, value[0], value[1]
                    )
                return None
        return None

    def terrain_surface_diagnostics(self) -> dict:
        """Return cache-only diagnostics from the most recently painted frame."""

        return dict(self._terrain_surface_diagnostics)

    def terrain_cache_diagnostics(self) -> dict:
        """Return hit, build, timing and resident-memory terrain metrics."""

        def _cache_metrics(cache, builds: int, last_hit: bool) -> dict:
            return {
                "hits": int(cache.hits),
                "misses": int(cache.misses),
                "evictions": int(cache.evictions),
                "builds": int(builds),
                "resident_bytes": int(cache.resident_bytes),
                "budget_bytes": int(cache.max_bytes),
                "last_hit": bool(last_hit),
            }

        return {
            "base_material": {
                **_cache_metrics(
                    self._terrain_base_material_cache,
                    self._terrain_base_material_builds,
                    self._last_base_material_cache_hit,
                ),
                "last_time_s": float(self._last_terrain_color_s),
            },
            "lighting": _cache_metrics(
                self._terrain_shade_cache,
                self._terrain_lighting_builds,
                self._last_lighting_cache_hit,
            ),
            "resolved_material": {
                **_cache_metrics(
                    self._terrain_resolved_material_cache,
                    self._terrain_resolved_material_builds,
                    self._last_resolved_material_cache_hit,
                ),
                "last_time_s": float(self._last_material_resolution_s),
            },
            "raster": {
                "builds": int(self._terrain_raster_builds),
                "last_hit": bool(self._last_raster_cache_hit),
                "last_time_s": float(self._last_terrain_raster_s),
            },
            "frame": {
                "hits": int(self._terrain_frame_cache_hits),
                "misses": int(self._terrain_frame_cache_misses),
                "last_hit": bool(self._last_frame_cache_hit),
            },
        }

    def terrain_surface_diagnostic_at_screen(
        self, x: float, y: float
    ) -> dict | None:
        """Inspect the resolved pixel material without touching raster sources."""

        materials = self._terrain_resolved_materials
        image = self._terrain_surface_image_cache
        if materials is None or image is None:
            return None
        height, width = materials.valid.shape
        px = int(math.floor(float(x) * width / max(1, int(image.width()))))
        py = int(math.floor(float(y) * height / max(1, int(image.height()))))
        if not (0 <= px < width and 0 <= py < height):
            return None
        if not bool(materials.valid[py, px]):
            return None
        source_index = int(materials.source_indices[py, px])
        class_id = int(materials.class_ids[py, px])
        categorical = bool(materials.categorical[py, px])
        cache = getattr(
            getattr(self, "profile", None), "surface_samples", None
        )
        legends = tuple(
            _sample_cache_value(cache, "source_legend_ids", ()) or ()
        )
        info = (
            self._category_metadata(cache, class_id, source_index)
            if categorical
            else None
        )
        raster_row = raster_column = None
        lod_factor = None
        sample_origin = None
        geometry = self._terrain_surface_image_geometry
        raster_cache = self._terrain_raster_cache
        if (
            isinstance(geometry, _TerrainTriangleGeometry)
            and raster_cache is not None
        ):
            triangle_id, bary_u, bary_v = raster_cache
            triangle = int(triangle_id[py, px])
            if triangle >= 0:
                weights = np.asarray(
                    (
                        bary_u[py, px],
                        bary_v[py, px],
                        1.0 - bary_u[py, px] - bary_v[py, px],
                    )
                )
                vertex = int(np.argmax(weights))
                row = int(geometry.vertex_rows[triangle, vertex])
                column = int(geometry.vertex_columns[triangle, vertex])
                domain = int(geometry.vertex_domain[triangle, vertex])
                prefix = "near_patch" if domain == 1 else "visual"
                asset = self._terrain_render_asset
                shape = (
                    np.shape(asset.near_patch_elevations)
                    if domain == 1 and asset is not None
                    else np.shape(asset.elevations)
                    if asset is not None
                    else ()
                )
                diagnostic = _sample_cache_value(
                    cache, f"{prefix}_lod_factors"
                )
                if (
                    domain == 0
                    and (
                        diagnostic is None
                        or np.shape(diagnostic) != tuple(shape)
                    )
                ):
                    prefix = "relief"
                    sampled_rows = np.asarray(
                        _sample_cache_value(
                            cache,
                            "relief_distance_indices",
                            (),
                        ),
                        dtype=np.int32,
                    )
                    sampled_columns = np.asarray(
                        _sample_cache_value(
                            cache,
                            "relief_azimuth_indices",
                            (),
                        ),
                        dtype=np.int32,
                    )
                    if sampled_rows.size and sampled_columns.size:
                        row = int(np.argmin(np.abs(sampled_rows - row)))
                        circular = np.abs(
                            sampled_columns
                            - (column % max(1, int(shape[1])))
                        )
                        circular = np.minimum(
                            circular,
                            max(1, int(shape[1])) - circular,
                        )
                        column = int(np.argmin(circular))
                values = {}
                for name in (
                    "raster_rows",
                    "raster_columns",
                    "lod_factors",
                    "sample_origins",
                ):
                    grid = _sample_cache_value(cache, f"{prefix}_{name}")
                    if grid is not None and np.ndim(grid) == 2:
                        grid_array = np.asarray(grid)
                        if (
                            0 <= row < grid_array.shape[0]
                            and 0 <= column < grid_array.shape[1]
                        ):
                            values[name] = int(grid_array[row, column])
                raster_row = values.get("raster_rows")
                raster_column = values.get("raster_columns")
                lod_factor = values.get("lod_factors")
                sample_origin = values.get("sample_origins")
        origin_names = {
            1: "exact",
            2: "modal",
            3: "source_fallback",
            4: "terrain_fallback",
        }
        return {
            "class_id": class_id if categorical else None,
            "category_name": getattr(info, "name", None),
            "source_index": source_index,
            "legend_id": (
                legends[source_index]
                if 0 <= source_index < len(legends)
                else ""
            ),
            "material_type": "categorical" if categorical else "continuous",
            "lod_factor": lod_factor,
            "raster_row": raster_row,
            "raster_column": raster_column,
            "sample_origin": origin_names.get(
                sample_origin,
                "source_fallback"
                if source_index >= 0
                else "terrain_fallback",
            ),
        }

    @staticmethod
    def _profile_point_budget(width: int, interaction_active: bool) -> int:
        width = max(1, int(width))
        if interaction_active:
            return max(256, min(1024, width))
        return max(512, min(2048, width * 2))

    def _profile_layers_for_frame(self, interaction_active: bool):
        layers = self._layers
        if not interaction_active or len(layers) <= 12:
            return layers
        indices = np.unique(
            np.rint(np.linspace(0, len(layers) - 1, 12)).astype(np.int32)
        )
        return [layers[int(index)] for index in indices]

    def _prepare_profile_polygon_cache(
        self,
        profile,
        projection_fn,
        width,
        height,
        current_azimuth,
        az_min,
        az_max,
        interaction_active,
    ) -> None:
        view_key = (
            id(profile),
            int(width),
            int(height),
            float(current_azimuth),
            float(az_min),
            float(az_max),
            bool(interaction_active),
            self._profile_point_budget(width, interaction_active),
            self._projection_geometry_signature(projection_fn, az_min, az_max),
        )
        if view_key != self._profile_polygon_cache_view_key:
            self._profile_polygon_cache_view_key = view_key
            self._profile_polygon_cache.clear()
            self._profile_category_hit_cache.clear()

    def _draw_cached_profile_polygons(self, painter, cache_key, color) -> bool:
        if self._profile_polygon_cache_view_key is None:
            return False
        polygons = self._profile_polygon_cache.get(cache_key)
        if polygons is None:
            return False
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        for polygon in polygons:
            painter.drawPolygon(polygon)
        return True

    def _cache_profile_polygons(self, cache_key, list_sx, list_sy, bottom_y):
        polygons = []
        for sx_arr, sy_arr in zip(list_sx, list_sy):
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if np.count_nonzero(valid) < 2:
                continue
            f_sx = np.asarray(sx_arr[valid], dtype=np.float32)
            f_sy = np.asarray(sy_arr[valid], dtype=np.float32)
            points = [
                QPointF(float(x), float(y)) for x, y in zip(f_sx, f_sy)
            ]
            points.append(QPointF(float(f_sx[-1]), float(bottom_y)))
            points.append(QPointF(float(f_sx[0]), float(bottom_y)))
            polygons.append(QPolygonF(points))
        result = tuple(polygons)
        if self._profile_polygon_cache_view_key is not None:
            self._profile_polygon_cache[cache_key] = result
        return result

    def _cache_profile_category_polygons(
        self,
        cache_key,
        list_sx,
        list_sy,
        list_azimuths,
        bottom_y,
    ) -> None:
        entries = []
        for sx_arr, sy_arr, azimuth_arr in zip(
            list_sx, list_sy, list_azimuths
        ):
            sx_arr = np.asarray(sx_arr)
            sy_arr = np.asarray(sy_arr)
            azimuth_arr = np.asarray(azimuth_arr)
            valid = (
                np.isfinite(sx_arr)
                & np.isfinite(sy_arr)
                & np.isfinite(azimuth_arr)
            )
            if np.count_nonzero(valid) < 2:
                continue
            f_sx = np.asarray(sx_arr[valid], dtype=np.float32)
            f_sy = np.asarray(sy_arr[valid], dtype=np.float32)
            f_azimuths = np.asarray(
                azimuth_arr[valid], dtype=np.float32
            )
            points = [
                QPointF(float(x), float(y))
                for x, y in zip(f_sx, f_sy)
            ]
            points.append(QPointF(float(f_sx[-1]), float(bottom_y)))
            points.append(QPointF(float(f_sx[0]), float(bottom_y)))
            entries.append(
                (QPolygonF(points), f_sx, f_azimuths)
            )
        self._profile_category_hit_cache[cache_key] = tuple(entries)

    def draw(
        self,
        painter: QPainter,
        projection_fn,
        width: int,
        height: int,
        current_azimuth: float,
        zoom_level: float,
        elevation_angle: float,
        ut_hour: float,
        draw_flat_line: bool = False,
        projection_fn_numpy=None,
        draw_domes_callback=None,
        sun_alt: float | None = None,
        sun_az: float | None = None,
        terrain_shading_enabled: bool = True,
        sky_color_fn=None,
        interaction_active: bool = False,
        terrain_3d_enabled: bool | None = None,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        """
        Main entry: draw all terrain layers.
        If draw_flat_line is True, ignores loaded data/fallback and draws a simple straight line.
        """
        if elevation_angle > 60.0:
            return  # Looking at zenith — skip terrain

        # Compatibility: the old control only disabled mesh lighting. The
        # replacement selects between two complete representations instead.
        if terrain_3d_enabled is None:
            terrain_3d_enabled = bool(terrain_shading_enabled)

        light_context = self._resolve_light_context(
            light_context,
            sun_alt=sun_alt,
            sun_az=sun_az,
        )
        sun_alt = light_context.sun_altitude_deg
        sun_az = light_context.sun_azimuth_deg
        light_factors = terrain_celestial_light_factors(
            light_context, self.render_settings
        )
        t_night = light_factors.night

        bottom_y = height * 2.0

        # Flat Line Mode
        if draw_flat_line:
            flat_light = self._terrain_light_components(
                0.0,
                0.0,
                1.0,
                0.0,
                light_context=light_context,
            )
            color = self._compose_profile_light_color(
                GROUND_DAY,
                float(flat_light.intensity),
                0.0,
                self._reference_sky_color(
                    sky_color_fn,
                    sun_alt,
                    sun_az,
                    current_azimuth,
                    t_night,
                ),
                flat_light.factors,
                solar_exposure=float(flat_light.solar_exposure),
                lunar_exposure=float(flat_light.lunar_exposure),
            )
            painter.setPen(QPen(color, 2))
            painter.setBrush(QBrush(color))

            pt = projection_fn(0.0, current_azimuth)
            if pt:
                y = pt[1]
                # If y is off-screen top, drawn from top of screen
                y_draw = int(max(-bottom_y, y))
                painter.drawRect(
                    0, y_draw, width, int(bottom_y * 1.5)
                )  # Big enough to cover
            return

        fov_deg = math.degrees(
            4.0 * math.atan(width / (2.0 * height * max(zoom_level, 1e-6)))
        )
        vert_scale = self.vert_exaggeration * zoom_level
        px_per_alt_deg = (height / 45.0) * vert_scale

        painter.setRenderHint(QPainter.Antialiasing)

        # Pre-calculate Culling range
        # CULLING_MARGIN: degrees outside viewport to keep for smooth transitions
        # Elevated terrain can move substantially inward relative to the
        # altitude-zero horizon under stereographic projection, especially
        # with a steep camera pitch. Keep a broad angular guard band so the
        # projected surface cannot terminate inside a lateral viewport edge.
        culling_margin = 45.0
        az_min = current_azimuth - (fov_deg / 2.0) - culling_margin
        az_max = current_azimuth + (fov_deg / 2.0) + culling_margin

        # ── Pre-process Domes (Darrere cap a davant) ─────────────────────────
        pending_domes = []
        if (
            draw_domes_callback
            and hasattr(self, "profile")
            and hasattr(self.profile, "light_domes")
        ):
            ld = self.profile.light_domes
            lpd = self.profile.light_peak_distances
            n = len(ld)
            # Peak detection to avoid saturation (grouping azimuths)
            for i in range(n):
                val = ld[i]
                if val < 0.2:
                    continue  # Threshold
                prev_val = ld[(i - 1) % n]
                next_val = ld[(i + 1) % n]
                # Local maximum check
                if val >= prev_val and val >= next_val:
                    # Simple plateau handling: only pick the first point
                    if val == next_val:
                        continue
                    pending_domes.append({"idx": i, "dist": lpd[i]})
            pending_domes.sort(key=lambda x: x["dist"], reverse=True)

            # Final step: Azimuthal Clustering to avoid 107 centers
            # We group peaks within 15 degrees to consolidate urban centers.
            if pending_domes:
                clustered = []
                # Sort by intensity to keep the brightest peak as the cluster center
                sorted_by_intensity = sorted(
                    pending_domes, key=lambda x: ld[x["idx"]], reverse=True
                )
                used_indices = set()

                for d in sorted_by_intensity:
                    if d["idx"] in used_indices:
                        continue

                    # New Cluster
                    center_az = self.profile.azimuths[d["idx"]]
                    clustered.append(d)
                    used_indices.add(d["idx"])

                    # Consume neighbors
                    for other in sorted_by_intensity:
                        if other["idx"] in used_indices:
                            continue
                        other_az = self.profile.azimuths[other["idx"]]

                        # Shortest angular distance
                        diff = abs(other_az - center_az) % 360
                        if diff > 180:
                            diff = 360 - diff

                        if diff < 15.0:  # 15 degree cluster radius
                            used_indices.add(other["idx"])

                pending_domes = sorted(
                    clustered, key=lambda x: x["dist"], reverse=True
                )

        # ── Dibuix de cada banda de darrera cap a davant ─────────────────────────
        sky_ref = self._reference_sky_color(
            sky_color_fn, sun_alt, sun_az, current_azimuth, t_night
        )
        terrain_mesh = getattr(self.profile, "terrain_mesh", None)
        has_terrain_mesh = bool(terrain_mesh)
        has_surface2d = self._has_terrain_surface_2d(terrain_mesh)
        use_3d_relief = bool(terrain_3d_enabled and has_surface2d)
        profile_target_painter = painter
        profile_image = None
        profile_image_painter = None
        profile_image_key = None

        if use_3d_relief:
            self._profile_polygon_cache_view_key = None
            self._profile_polygon_cache.clear()
            self._profile_category_hit_cache.clear()
            while pending_domes:
                d_info = pending_domes.pop(0)
                draw_domes_callback(painter, d_info["idx"], d_info["dist"])
        else:
            self._prepare_profile_polygon_cache(
                self.profile,
                projection_fn,
                width,
                height,
                current_azimuth,
                az_min,
                az_max,
                interaction_active,
            )
            if not interaction_active and not pending_domes and self._layers:
                profile_image_key = (
                    self._profile_polygon_cache_view_key,
                    round(float(t_night) * 256.0) / 256.0,
                    int(sky_ref.rgba()),
                    round(float(light_context.sun_altitude_deg) * 4.0) / 4.0,
                    round(float(light_context.sun_azimuth_deg) * 4.0) / 4.0,
                    (
                        None
                        if light_context.moon_altitude_deg is None
                        else round(
                            float(light_context.moon_altitude_deg) * 4.0
                        )
                        / 4.0
                    ),
                    (
                        None
                        if light_context.moon_azimuth_deg is None
                        else round(
                            float(light_context.moon_azimuth_deg) * 4.0
                        )
                        / 4.0
                    ),
                    round(
                        float(light_context.moon_illumination) * 256.0
                    )
                    / 256.0,
                    round(float(light_context.eclipse_factor) * 256.0)
                    / 256.0,
                    repr(self.render_settings),
                )
                if (
                    profile_image_key == self._profile_image_cache_key
                    and self._profile_image_cache is not None
                ):
                    painter.drawImage(0, 0, self._profile_image_cache)
                    return
                profile_image = QImage(
                    int(width), int(height), QImage.Format_ARGB32_Premultiplied
                )
                profile_image.fill(0)
                profile_image_painter = QPainter(profile_image)
                profile_image_painter.setRenderHint(
                    QPainter.Antialiasing,
                    painter.testRenderHint(QPainter.Antialiasing),
                )
                profile_target_painter = profile_image_painter
            # Bands are already ordered far-to-near. In silhouette mode the
            # layer-count control remains authoritative even if a mesh exists.
            terrain_layers = self._profile_layers_for_frame(interaction_active)
            for band_pts, _night_c, day_c in terrain_layers:
                # First: Draw any domes that are behind or within this band (further than band_min)
                while (
                    pending_domes and pending_domes[0]["dist"] >= band_pts.band_min
                ):
                    d_info = pending_domes.pop(0)
                    draw_domes_callback(painter, d_info["idx"], d_info["dist"])

                color = QColor(day_c)
                self._draw_band_linear(
                    profile_target_painter,
                    band_pts,
                    color,
                    projection_fn,
                    width,
                    height,
                    px_per_alt_deg,
                    current_azimuth,
                    az_min,
                    az_max,
                    projection_fn_numpy,
                    sun_alt=sun_alt,
                    sun_az=sun_az,
                    terrain_shading_enabled=True,
                    sky_color=sky_ref,
                    interaction_active=interaction_active,
                    light_context=light_context,
                )

        # ── Farciment del terra amb gradient de perspectiva ───────────────────────
        # Simulem el pla de terra que s'allunya amb un gradient fosc→color terra,
        # evitant el rectangle pla uniforme que trenca el realisme.
        profile_resolved = getattr(self.profile, "resolved_mask", None)
        profile_is_partial = profile_resolved is not None and not bool(
            np.all(profile_resolved)
        )
        if (
            self._layers
            and not use_3d_relief
            and not has_terrain_mesh
            and not profile_is_partial
        ):
            ground_light = self._terrain_light_components(
                0.0,
                0.0,
                1.0,
                0.0,
                light_context=light_context,
            )
            ground_c = self._compose_profile_light_color(
                GROUND_DAY,
                float(ground_light.intensity),
                0.0,
                sky_ref,
                ground_light.factors,
                solar_exposure=float(ground_light.solar_exposure),
                lunar_exposure=float(ground_light.lunar_exposure),
            )
            nearest = self._layers[-1]
            self._draw_ground_linear(
                profile_target_painter,
                nearest[0],
                ground_c,
                projection_fn,
                width,
                height,
                px_per_alt_deg,
                current_azimuth,
                az_min,
                az_max,
                overlap_px=1.0,
                projection_fn_numpy=projection_fn_numpy,
                interaction_active=interaction_active,
            )

        if profile_image_painter is not None and profile_image is not None:
            profile_image_painter.end()
            self._profile_image_cache_key = profile_image_key
            self._profile_image_cache = profile_image
            painter.drawImage(0, 0, profile_image)
            return

        if use_3d_relief:
            rendered = False
            # Preserve subclass hooks and synthetic mesh-only profiles used by
            # integrations: they have no colour-band context from which the
            # enhanced per-vertex base colours can be derived reliably.
            supports_interpolated = (
                bool(self._layers)
                and type(self)._draw_terrain_surface_2d
                is HorizonOverlay._draw_terrain_surface_2d
            )
            categorical_surface = _surface_cache_has_categorical_material(
                getattr(self.profile, "surface_samples", None)
            )
            if (
                (
                    self.render_settings.terrain_shading_mode == "interpolated"
                    or categorical_surface
                )
                and supports_interpolated
            ):
                rendered = self._draw_terrain_interpolated(
                    painter,
                    terrain_mesh,
                    projection_fn,
                    width,
                    height,
                    current_azimuth,
                    az_min,
                    az_max,
                    t_night,
                    sky_ref,
                    sun_alt=sun_alt,
                    sun_az=sun_az,
                    projection_fn_numpy=projection_fn_numpy,
                    interaction_active=interaction_active,
                    light_context=light_context,
                )
            if not rendered:
                self._draw_terrain_surface_2d(
                    painter,
                    terrain_mesh,
                    projection_fn,
                    width,
                    height,
                    px_per_alt_deg,
                    current_azimuth,
                    az_min,
                    az_max,
                    t_night,
                    sky_ref,
                    sun_alt,
                    sun_az,
                    True,
                    projection_fn_numpy=projection_fn_numpy,
                    interaction_active=interaction_active,
                    light_context=light_context,
                )
        elif terrain_3d_enabled and has_terrain_mesh and not self._layers:
            self._draw_terrain_mesh(
                painter,
                terrain_mesh,
                projection_fn,
                width,
                height,
                px_per_alt_deg,
                current_azimuth,
                az_min,
                az_max,
                t_night,
                sky_ref,
                sun_alt,
                sun_az,
                terrain_shading_enabled,
                projection_fn_numpy=projection_fn_numpy,
                light_context=light_context,
            )

    # ── private rendering ──

    def _reference_sky_color(
        self, sky_color_fn, sun_alt, sun_az, current_azimuth, t_night
    ):
        if callable(sky_color_fn) and sun_alt is not None and sun_az is not None:
            try:
                color = sky_color_fn(
                    0.0,
                    float(current_azimuth) % 360.0,
                    float(sun_alt),
                    float(sun_az),
                )
                if isinstance(color, QColor):
                    return color
            except Exception:
                pass
        return _lerp_color(QColor(170, 195, 215), QColor(5, 5, 12), t_night)

    def _terrain_polygon_layers(self, has_terrain_mesh: bool):
        if not has_terrain_mesh or len(self._layers) <= 16:
            return self._layers

        target_layers = 12
        indices = np.linspace(0, len(self._layers) - 1, target_layers)
        indices = np.unique(np.rint(indices).astype(np.int32))
        if indices[-1] != len(self._layers) - 1:
            indices = np.append(indices, len(self._layers) - 1)
        return [self._layers[int(i)] for i in indices]

    def _has_terrain_surface_2d(self, mesh) -> bool:
        if not mesh:
            return False
        try:
            version = int(np.asarray(mesh.get("version", 1)).item())
            altitudes = np.asarray(mesh.get("altitudes"), dtype=np.float32)
            visible = np.asarray(mesh.get("visible"), dtype=bool)
            valid = np.asarray(mesh.get("valid"), dtype=bool)
            azimuths = np.asarray(mesh.get("azimuths"), dtype=np.float32)
            distances = np.asarray(mesh.get("distances"), dtype=np.float32)
        except Exception:
            return False
        return (
            version >= 2
            and altitudes.ndim == 2
            and altitudes.shape == visible.shape
            and altitudes.shape == valid.shape
            and distances.size == altitudes.shape[0]
            and azimuths.size == altitudes.shape[1]
            and bool(np.any(visible & valid))
        )

    def _apply_atmospheric_perspective(
        self, base_color: QColor, sky_color: QColor, band_pts, t_night: float
    ) -> QColor:
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        return self._apply_terrain_atmosphere(
            base_color, band_max_m, sky_color, t_night
        )

    def _terrain_shade_values(
        self,
        az_arr,
        h_arr,
        sun_alt,
        sun_az,
        band_pts,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> np.ndarray:
        return self._terrain_profile_light_grid(
            az_arr,
            h_arr,
            sun_alt,
            sun_az,
            band_pts,
            light_context=light_context,
        ).intensity

    def _terrain_profile_light_grid(
        self,
        az_arr,
        h_arr,
        sun_alt,
        sun_az,
        band_pts,
        *,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> TerrainLightingGrid:
        """Approximate visible profile normals and apply the shared sky light."""

        del h_arr
        context = self._resolve_light_context(
            light_context, sun_alt=sun_alt, sun_az=sun_az
        )
        azimuth = np.deg2rad(np.asarray(az_arr, dtype=np.float32))
        # A silhouette has no complete DEM normal.  Its visible flank faces the
        # observer, with a broad upward component that avoids wall-like bands.
        horizontal = 0.66
        normal_x = -np.sin(azimuth) * horizontal
        normal_y = -np.cos(azimuth) * horizontal
        normal_z = np.full(np.shape(normal_x), 0.75, dtype=np.float32)
        distance = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        return self._terrain_light_components(
            normal_x,
            normal_y,
            normal_z,
            distance,
            self._sun_vector_enu(
                context.sun_altitude_deg, context.sun_azimuth_deg
            ),
            context.sun_altitude_deg,
            terrain_shading_enabled=True,
            light_context=context,
        )

    def _compose_profile_light_color(
        self,
        base_color: QColor,
        intensity: float,
        distance_m: float,
        sky_color: QColor,
        factors: TerrainCelestialLightFactors,
        *,
        solar_exposure: float = 0.0,
        lunar_exposure: float = 0.0,
    ) -> QColor:
        """Run fallback/profile colours through the same surface pipeline."""

        base = np.asarray(base_color.getRgb(), dtype=np.uint8)
        vibrant = (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            )
            == SurfaceVisualStyle.VIBRANT.value
        )
        composed = compose_vertex_rgba(
            base,
            float(intensity),
            float(distance_m),
            self.render_settings,
            maximum_distance_m=self._maximum_terrain_distance_m(),
            horizon_rgb=(
                sky_color.red(),
                sky_color.green(),
                sky_color.blue(),
            ),
            atmosphere_strength=0.0 if vibrant else 1.0,
        )
        if vibrant:
            composed = apply_vibrant_color_grade(
                composed,
                float(intensity),
                float(distance_m),
                self.render_settings,
                maximum_distance_m=self._maximum_terrain_distance_m(),
                daylight_factor=factors.solar_ambient,
                moonlight_factor=factors.lunar_strength,
                solar_exposure=float(solar_exposure),
                lunar_exposure=float(lunar_exposure),
                atmosphere_rgb=(
                    sky_color.red(),
                    sky_color.green(),
                    sky_color.blue(),
                ),
            )
        return _qcolor_from_rgba(composed)

    def _sun_vector_enu(self, sun_alt, sun_az):
        if sun_alt is None or sun_az is None:
            return None
        alt_rad = math.radians(float(sun_alt))
        az_rad = math.radians(float(sun_az))
        cos_alt = math.cos(alt_rad)
        return np.array(
            [
                math.sin(az_rad) * cos_alt,
                math.cos(az_rad) * cos_alt,
                math.sin(alt_rad),
            ],
            dtype=np.float32,
        )

    def _resolve_light_context(
        self,
        light_context: TerrainCelestialLightContext | None = None,
        *,
        sun_alt: float | None = None,
        sun_az: float | None = None,
    ) -> TerrainCelestialLightContext:
        """Resolve shared astronomy, retaining the fixed light as a fallback."""

        if isinstance(light_context, TerrainCelestialLightContext):
            try:
                valid_context_sun = bool(
                    np.isfinite(float(light_context.sun_altitude_deg))
                    and np.isfinite(float(light_context.sun_azimuth_deg))
                )
            except (TypeError, ValueError):
                valid_context_sun = False
            if valid_context_sun:
                return light_context.validated()
        settings = self.render_settings
        try:
            valid_legacy_sun = bool(
                sun_alt is not None
                and sun_az is not None
                and np.isfinite(float(sun_alt))
                and np.isfinite(float(sun_az))
            )
        except (TypeError, ValueError):
            valid_legacy_sun = False
        if valid_legacy_sun:
            return TerrainCelestialLightContext(
                float(sun_alt), float(sun_az)
            ).validated()
        return TerrainCelestialLightContext(
            settings.terrain_light_elevation_deg,
            settings.terrain_light_azimuth_deg,
        ).validated()

    def _configured_light(
        self,
        sun_alt: float | None = None,
        sun_az: float | None = None,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> tuple[float, float, np.ndarray | None]:
        """Compatibility view of the real astronomical sun in every style."""

        context = self._resolve_light_context(
            light_context, sun_alt=sun_alt, sun_az=sun_az
        )
        vector = (
            self._sun_vector_enu(
                context.sun_altitude_deg, context.sun_azimuth_deg
            )
            if self.render_settings.terrain_lighting_enabled
            else None
        )
        return (
            context.sun_altitude_deg,
            context.sun_azimuth_deg,
            vector,
        )

    def _terrain_light_bounds(
        self,
        factors: TerrainCelestialLightFactors | None = None,
    ) -> tuple[float, float]:
        settings = self.render_settings
        if (
            normalize_surface_visual_style(settings.surface_visual_style)
            == SurfaceVisualStyle.VIBRANT.value
        ):
            day_minimum = settings.vibrant_sun_min_brightness
            day_maximum = settings.vibrant_sun_max_brightness
        else:
            day_minimum = settings.terrain_min_brightness
            day_maximum = settings.terrain_max_brightness
        if factors is None:
            return day_minimum, day_maximum
        night_minimum = (
            float(settings.terrain_night_ambient_strength)
            + float(settings.terrain_moon_ambient_strength)
            * factors.lunar_strength
        )
        night_maximum = (
            night_minimum
            + float(settings.terrain_moon_diffuse_strength)
            * factors.lunar_strength
        )
        minimum = night_minimum + (
            day_minimum - night_minimum
        ) * factors.solar_ambient
        maximum = night_maximum + (
            day_maximum - night_maximum
        ) * factors.solar_ambient
        return max(0.0, minimum), max(minimum, maximum)

    def _terrain_lighting_settings_key(self) -> tuple:
        """Describe Lambert/celestial settings without material or camera state."""

        settings = self.render_settings
        return (
            "terrain-lighting-settings-v1",
            bool(settings.terrain_lighting_enabled),
            normalize_surface_visual_style(settings.surface_visual_style),
            round(float(settings.terrain_ambient_strength), 6),
            round(float(settings.terrain_diffuse_strength), 6),
            round(float(settings.terrain_min_brightness), 6),
            round(float(settings.terrain_max_brightness), 6),
            round(
                float(settings.terrain_twilight_dark_altitude_deg), 6
            ),
            round(float(settings.terrain_sun_full_altitude_deg), 6),
            round(float(settings.terrain_night_ambient_strength), 6),
            round(
                float(settings.terrain_moon_horizon_fade_start_deg), 6
            ),
            round(
                float(settings.terrain_moon_horizon_fade_end_deg), 6
            ),
            round(float(settings.terrain_moon_ambient_strength), 6),
            round(float(settings.terrain_moon_diffuse_strength), 6),
            round(float(settings.terrain_moon_phase_exponent), 6),
            round(float(settings.vibrant_sun_ambient_strength), 6),
            round(float(settings.vibrant_sun_diffuse_boost), 6),
            round(float(settings.vibrant_sunlight_exponent), 6),
            round(float(settings.vibrant_sun_min_brightness), 6),
            round(float(settings.vibrant_sun_max_brightness), 6),
        )

    def _terrain_lighting_key(
        self,
        asset,
        light_context: TerrainCelestialLightContext,
        *,
        lighting_enabled: bool,
    ) -> tuple:
        """Key lighting by geometry and quantized celestial state, never camera."""

        return (
            "terrain-lighting-v1",
            int(asset.mesh_id),
            bool(lighting_enabled),
            round(float(light_context.sun_altitude_deg) * 4.0) / 4.0,
            round(float(light_context.sun_azimuth_deg) * 4.0) / 4.0,
            (
                None
                if light_context.moon_altitude_deg is None
                else round(
                    float(light_context.moon_altitude_deg) * 4.0
                )
                / 4.0
            ),
            (
                None
                if light_context.moon_azimuth_deg is None
                else round(
                    float(light_context.moon_azimuth_deg) * 4.0
                )
                / 4.0
            ),
            round(float(light_context.moon_illumination) * 256.0) / 256.0,
            round(float(light_context.eclipse_factor) * 256.0) / 256.0,
            self._terrain_lighting_settings_key(),
        )

    def _maximum_terrain_distance_m(self) -> float | None:
        value = getattr(getattr(self, "profile", None), "resolved_radius_m", None)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if np.isfinite(value) and value > 0.0 else None

    @staticmethod
    def _sample_polar_elevations(
        elevations,
        valid,
        distances,
        azimuths,
        sample_x,
        sample_y,
    ):
        sample_x = np.asarray(sample_x, dtype=np.float32)
        sample_y = np.asarray(sample_y, dtype=np.float32)
        sample_distance = np.hypot(sample_x, sample_y).astype(np.float32)
        sample_azimuth = (
            np.degrees(np.arctan2(sample_x, sample_y)) % 360.0
        ).astype(np.float32)

        distance_hi = np.searchsorted(
            distances, sample_distance, side="right"
        )
        inside = (distance_hi > 0) & (distance_hi < len(distances))
        distance_hi = np.clip(distance_hi, 1, len(distances) - 1)
        distance_lo = distance_hi - 1
        distance_span = np.maximum(
            distances[distance_hi] - distances[distance_lo], 1e-6
        )
        distance_t = np.clip(
            (sample_distance - distances[distance_lo]) / distance_span,
            0.0,
            1.0,
        )

        az_diffs = np.diff(azimuths.astype(np.float32))
        az_diffs = az_diffs[np.isfinite(az_diffs) & (az_diffs > 0.0)]
        az_step = float(np.median(az_diffs)) if az_diffs.size else 360.0
        az_position = ((sample_azimuth - float(azimuths[0])) % 360.0) / max(
            az_step, 1e-6
        )
        az_lo_float = np.floor(az_position)
        az_t = (az_position - az_lo_float).astype(np.float32)
        az_lo = az_lo_float.astype(np.int32) % len(azimuths)
        az_hi = (az_lo + 1) % len(azimuths)

        w00 = (1.0 - distance_t) * (1.0 - az_t)
        w01 = (1.0 - distance_t) * az_t
        w10 = distance_t * (1.0 - az_t)
        w11 = distance_t * az_t
        samples = (
            (distance_lo, az_lo, w00),
            (distance_lo, az_hi, w01),
            (distance_hi, az_lo, w10),
            (distance_hi, az_hi, w11),
        )

        weighted_height = np.zeros(sample_distance.shape, dtype=np.float32)
        weight_sum = np.zeros(sample_distance.shape, dtype=np.float32)
        for distance_idx, azimuth_idx, weight in samples:
            corner_valid = valid[distance_idx, azimuth_idx]
            corner_weight = np.where(corner_valid, weight, 0.0).astype(
                np.float32
            )
            weighted_height += (
                elevations[distance_idx, azimuth_idx] * corner_weight
            )
            weight_sum += corner_weight

        sampled = np.divide(
            weighted_height,
            np.maximum(weight_sum, 1e-6),
            out=np.zeros_like(weighted_height),
            where=weight_sum > 1e-6,
        )
        sampled_valid = inside & (weight_sum >= 0.50)
        return sampled, sample_distance, sampled_valid

    def _prepare_terrain_render_asset(self, mesh):
        if not isinstance(mesh, dict):
            return None
        try:
            azimuths = np.asarray(mesh.get("azimuths"), dtype=np.float32)
            distances = np.asarray(mesh.get("distances"), dtype=np.float32)
            altitudes = np.asarray(mesh.get("altitudes"), dtype=np.float32)
            elevations = np.asarray(mesh.get("elevations"), dtype=np.float32)
            valid = np.asarray(mesh.get("valid"), dtype=bool)
            visible = np.asarray(mesh.get("visible"), dtype=bool)
        except Exception:
            return None
        shape = (distances.size, azimuths.size)
        if azimuths.size < 2 or distances.size < 2 or altitudes.shape != shape:
            return None
        if elevations.shape != shape:
            elevations = np.zeros(shape, dtype=np.float32)
        if valid.shape != shape:
            valid = np.isfinite(altitudes)
        if visible.shape != shape:
            visible = valid.copy()
        surface_cache = getattr(getattr(self, "profile", None), "surface_samples", None)
        visual_distances = _sample_cache_value(surface_cache, "visual_distances")
        visual_azimuths = _sample_cache_value(surface_cache, "visual_azimuths")
        visual_altitudes = _sample_cache_value(surface_cache, "visual_altitudes")
        visual_elevations = _sample_cache_value(surface_cache, "visual_elevations")
        visual_valid = _sample_cache_value(surface_cache, "visual_valid")
        visual_visible = _sample_cache_value(surface_cache, "visual_visible")
        visual_shape = (
            len(visual_distances) if visual_distances is not None else 0,
            len(visual_azimuths) if visual_azimuths is not None else 0,
        )
        if (
            _sample_cache_value(surface_cache, "completion_state", "complete")
            == "complete"
            and
            visual_shape[0] >= 2
            and visual_shape[1] >= 2
            and np.shape(visual_altitudes) == visual_shape
            and np.shape(visual_elevations) == visual_shape
            and np.shape(visual_valid) == visual_shape
        ):
            # Subdivide only the visual mesh. Heights/angles were interpolated
            # from the DEM in the worker, so geometry gains no fictitious
            # topographic information while a finer land-cover raster retains
            # detail between original DEM vertices.
            azimuths = np.asarray(visual_azimuths, dtype=np.float32)
            distances = np.asarray(visual_distances, dtype=np.float32)
            altitudes = np.asarray(visual_altitudes, dtype=np.float32)
            elevations = np.asarray(visual_elevations, dtype=np.float32)
            valid = np.asarray(visual_valid, dtype=bool)
            visible = (
                np.asarray(visual_visible, dtype=bool)
                if np.shape(visual_visible) == visual_shape
                else valid.copy()
            )
            shape = visual_shape
        computed = compute_polar_mesh_normals(
            elevations, valid, distances, azimuths
        )
        saved = tuple(
            np.asarray(mesh.get(name), dtype=np.float32)
            for name in ("normal_x", "normal_y", "normal_z")
        )
        if all(item.shape == shape for item in saved):
            saved_norm = np.sqrt(saved[0] ** 2 + saved[1] ** 2 + saved[2] ** 2)
            use_saved = visible & np.isfinite(saved_norm) & (saved_norm > 1e-5)
            normals = tuple(
                np.where(use_saved, saved[index], computed[index]).astype(np.float32)
                for index in range(3)
            )
        else:
            normals = tuple(np.asarray(item, dtype=np.float32) for item in computed)
        azimuths_closed = np.concatenate(
            [azimuths, [azimuths[0] + 360.0]]
        ).astype(np.float32)
        patch_eastings = np.asarray(
            mesh.get("near_patch_eastings", ()), dtype=np.float32
        )
        patch_northings = np.asarray(
            mesh.get("near_patch_northings", ()), dtype=np.float32
        )
        patch_shape = (patch_northings.size, patch_eastings.size)
        patch_altitudes = np.asarray(
            mesh.get("near_patch_altitudes", ()), dtype=np.float32
        )
        patch_elevations = np.asarray(
            mesh.get("near_patch_elevations", ()), dtype=np.float32
        )
        patch_valid = np.asarray(mesh.get("near_patch_valid", ()), dtype=bool)
        patch_normals = tuple(
            np.asarray(mesh.get(name, ()), dtype=np.float32)
            for name in (
                "near_patch_normal_x",
                "near_patch_normal_y",
                "near_patch_normal_z",
            )
        )
        if (
            patch_shape[0] < 2
            or patch_shape[1] < 2
            or patch_altitudes.shape != patch_shape
            or patch_elevations.shape != patch_shape
            or patch_valid.shape != patch_shape
        ):
            patch_eastings = np.empty(0, dtype=np.float32)
            patch_northings = np.empty(0, dtype=np.float32)
            patch_altitudes = np.empty((0, 0), dtype=np.float32)
            patch_elevations = np.empty((0, 0), dtype=np.float32)
            patch_valid = np.empty((0, 0), dtype=bool)
            patch_normals = (
                np.empty((0, 0), dtype=np.float32),
                np.empty((0, 0), dtype=np.float32),
                np.empty((0, 0), dtype=np.float32),
            )
        elif not all(value.shape == patch_shape for value in patch_normals):
            filled = np.where(patch_valid, patch_elevations, 0.0).astype(np.float64)
            gradient_north, gradient_east = np.gradient(
                filled,
                patch_northings.astype(np.float64),
                patch_eastings.astype(np.float64),
                edge_order=1,
            )
            patch_nx = -gradient_east
            patch_ny = -gradient_north
            patch_nz = np.ones(patch_shape, dtype=np.float64)
            patch_norm = np.sqrt(
                patch_nx * patch_nx + patch_ny * patch_ny + patch_nz * patch_nz
            )
            patch_normals = tuple(
                np.where(patch_valid, value / np.maximum(patch_norm, 1e-12), fallback).astype(np.float32)
                for value, fallback in ((patch_nx, 0.0), (patch_ny, 0.0), (patch_nz, 1.0))
            )
        return _TerrainRenderAsset(
            mesh_id=hash((id(mesh), id(visual_altitudes)))
            if visual_shape == shape and visual_shape[0] >= 2
            else id(mesh),
            azimuths=azimuths,
            azimuths_closed=azimuths_closed,
            distances=distances,
            altitudes=altitudes,
            altitudes_closed=np.concatenate([altitudes, altitudes[:, :1]], axis=1),
            elevations=elevations,
            valid=valid,
            valid_closed=np.concatenate([valid, valid[:, :1]], axis=1),
            visible=visible,
            visible_closed=np.concatenate([visible, visible[:, :1]], axis=1),
            normal_x=normals[0],
            normal_y=normals[1],
            normal_z=normals[2],
            near_patch_eastings=patch_eastings,
            near_patch_northings=patch_northings,
            near_patch_altitudes=patch_altitudes,
            near_patch_elevations=patch_elevations,
            near_patch_valid=patch_valid,
            near_patch_normal_x=patch_normals[0],
            near_patch_normal_y=patch_normals[1],
            near_patch_normal_z=patch_normals[2],
        )

    def _terrain_surface_normals(
        self, mesh, elevations, valid, distances, azimuths
    ):
        cache_key = (id(mesh), elevations.shape)
        if (
            cache_key == self._terrain_normal_cache_key
            and self._terrain_normal_cache is not None
        ):
            return self._terrain_normal_cache

        normals = compute_polar_mesh_normals(
            elevations, valid, distances, azimuths
        )
        self._terrain_normal_cache_key = cache_key
        self._terrain_normal_cache = normals
        return normals

    def _terrain_sun_visibility(
        self,
        mesh,
        elevations,
        valid,
        visible,
        distances,
        azimuths,
        sun_alt,
        sun_az,
        terrain_shading_enabled=True,
    ):
        shape = elevations.shape
        if (
            not terrain_shading_enabled
            or sun_alt is None
            or sun_az is None
            or float(sun_alt) <= -0.5
        ):
            return np.ones(shape, dtype=np.float32)

        cache_key = (
            id(mesh),
            shape,
            round(float(sun_alt) * 4.0) / 4.0,
            round(float(sun_az) * 4.0) / 4.0,
        )
        if (
            cache_key == self._terrain_shadow_cache_key
            and self._terrain_shadow_cache is not None
            and self._terrain_shadow_cache.shape == shape
        ):
            return self._terrain_shadow_cache

        result = np.ones(shape, dtype=np.float32)
        active = valid & visible & np.isfinite(elevations)
        active_rows, active_cols = np.nonzero(active)
        if active_rows.size == 0:
            self._terrain_shadow_cache_key = cache_key
            self._terrain_shadow_cache = result
            return result

        target_distance = distances[active_rows].astype(np.float32)
        target_azimuth = np.deg2rad(
            azimuths[active_cols].astype(np.float32)
        )
        target_x = target_distance * np.sin(target_azimuth)
        target_y = target_distance * np.cos(target_azimuth)
        target_z = elevations[active_rows, active_cols].astype(np.float32)
        target_z -= (target_distance * target_distance) / (
            2.0 * EARTH_RADIUS_M
        )

        sun_az_rad = math.radians(float(sun_az))
        sun_dx = math.sin(sun_az_rad)
        sun_dy = math.cos(sun_az_rad)
        sun_slope = math.tan(math.radians(max(0.15, float(sun_alt))))

        near_steps = np.diff(distances[: min(len(distances), 32)])
        near_steps = near_steps[np.isfinite(near_steps) & (near_steps > 0.0)]
        ray_start = max(
            20.0,
            min(80.0, float(np.median(near_steps)) * 2.0)
            if near_steps.size
            else 40.0,
        )
        ray_limit = max(ray_start * 2.0, float(distances[-1]) * 1.35)
        ray_offsets = np.geomspace(ray_start, ray_limit, 38).astype(
            np.float32
        )
        max_clearance = np.full(active_rows.shape, -np.inf, dtype=np.float32)

        for ray_offset in ray_offsets:
            sample_x = target_x + ray_offset * sun_dx
            sample_y = target_y + ray_offset * sun_dy
            sampled_elevation, sample_distance, sampled_valid = (
                self._sample_polar_elevations(
                    elevations,
                    valid,
                    distances,
                    azimuths,
                    sample_x,
                    sample_y,
                )
            )
            sampled_z = sampled_elevation - (
                sample_distance * sample_distance
            ) / (2.0 * EARTH_RADIUS_M)
            ray_z = target_z + ray_offset * sun_slope
            self_bias = 2.0 + ray_offset * 0.00015
            clearance = sampled_z - ray_z - self_bias
            max_clearance = np.where(
                sampled_valid,
                np.maximum(max_clearance, clearance),
                max_clearance,
            )

        penumbra = np.clip((max_clearance + 2.0) / 14.0, 0.0, 1.0)
        penumbra = penumbra * penumbra * (3.0 - 2.0 * penumbra)
        result[active_rows, active_cols] = 1.0 - penumbra.astype(np.float32)
        result = self._smooth_light_grid(
            result, active, min_value=0.0, max_value=1.0
        )
        result = np.where(active, np.clip(result, 0.0, 1.0), 1.0).astype(
            np.float32
        )

        self._terrain_shadow_cache_key = cache_key
        self._terrain_shadow_cache = result
        return result

    def _terrain_light_components(
        self,
        normal_x,
        normal_y,
        normal_z,
        distance_m,
        sun_vec=None,
        sun_alt=None,
        terrain_shading_enabled=True,
        sun_visibility=None,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> TerrainLightingGrid:
        nx, ny, nz = np.broadcast_arrays(
            np.asarray(normal_x, dtype=np.float32),
            np.asarray(normal_y, dtype=np.float32),
            np.asarray(normal_z, dtype=np.float32),
        )
        settings = self.render_settings
        if light_context is None:
            derived_azimuth = None
            if sun_vec is not None:
                candidate = np.asarray(sun_vec, dtype=np.float32)
                if candidate.shape == (3,) and np.all(np.isfinite(candidate)):
                    derived_azimuth = math.degrees(
                        math.atan2(float(candidate[0]), float(candidate[1]))
                    ) % 360.0
            light_context = self._resolve_light_context(
                None,
                sun_alt=sun_alt,
                sun_az=derived_azimuth,
            )
        else:
            light_context = light_context.validated()
        factors = terrain_celestial_light_factors(light_context, settings)
        zeros = np.zeros(nx.shape, dtype=np.float32)
        if not terrain_shading_enabled or not settings.terrain_lighting_enabled:
            return TerrainLightingGrid(
                np.ones(nx.shape, dtype=np.float32),
                zeros,
                zeros,
                factors,
            )

        norm = np.sqrt(nx * nx + ny * ny + nz * nz)
        safe = np.isfinite(norm) & (norm > 1e-6)
        nx = np.where(safe, nx / np.where(safe, norm, 1.0), 0.0)
        ny = np.where(safe, ny / np.where(safe, norm, 1.0), 0.0)
        nz = np.where(safe, nz / np.where(safe, norm, 1.0), 1.0)

        if sun_vec is None or not np.all(np.isfinite(sun_vec)):
            sun_vec = self._sun_vector_enu(
                light_context.sun_altitude_deg,
                light_context.sun_azimuth_deg,
            )
        sun_vec = np.asarray(sun_vec, dtype=np.float32)
        solar_lambert = np.clip(
            nx * sun_vec[0] + ny * sun_vec[1] + nz * sun_vec[2],
            0.0,
            1.0,
        )
        if sun_visibility is not None:
            direct_visibility = np.broadcast_to(
                np.asarray(sun_visibility, dtype=np.float32), nx.shape
            )
            solar_lambert *= np.clip(direct_visibility, 0.0, 1.0)
        vibrant = (
            normalize_surface_visual_style(settings.surface_visual_style)
            == SurfaceVisualStyle.VIBRANT.value
        )
        if vibrant:
            solar_lambert = np.power(
                solar_lambert,
                float(settings.vibrant_sunlight_exponent),
            )
            day_ambient = float(
                settings.vibrant_sun_ambient_strength
            )
            day_diffuse = (
                float(settings.terrain_diffuse_strength)
                * float(settings.vibrant_sun_diffuse_boost)
            )
        else:
            day_ambient = float(settings.terrain_ambient_strength)
            day_diffuse = float(settings.terrain_diffuse_strength)

        lunar_lambert = zeros
        if (
            factors.lunar_strength > 0.0
            and light_context.moon_altitude_deg is not None
            and light_context.moon_azimuth_deg is not None
        ):
            moon_vec = self._sun_vector_enu(
                light_context.moon_altitude_deg,
                light_context.moon_azimuth_deg,
            )
            lunar_lambert = np.clip(
                nx * moon_vec[0] + ny * moon_vec[1] + nz * moon_vec[2],
                0.0,
                1.0,
            )

        # Distance only removes directional contrast.  Pulling total brightness
        # towards 1.0 here would incorrectly turn night terrain back on.
        haze = _distance_haze_factors(distance_m)
        directional_contrast = np.maximum(0.18, 1.0 - 0.78 * haze)
        solar_exposure = (
            solar_lambert
            * float(factors.solar_direct)
            * directional_contrast
        )
        lunar_exposure = (
            lunar_lambert
            * float(factors.lunar_strength)
            * directional_contrast
        )
        ambient = (
            float(settings.terrain_night_ambient_strength)
            + (
                day_ambient
                - float(settings.terrain_night_ambient_strength)
            )
            * float(factors.solar_ambient)
            + float(settings.terrain_moon_ambient_strength)
            * float(factors.lunar_strength)
        )
        intensity = (
            ambient
            + day_diffuse * solar_exposure
            + float(settings.terrain_moon_diffuse_strength)
            * lunar_exposure
        )
        minimum_brightness, maximum_brightness = self._terrain_light_bounds(
            factors
        )
        intensity = np.clip(
            intensity,
            minimum_brightness,
            maximum_brightness,
        ).astype(np.float32)
        return TerrainLightingGrid(
            intensity,
            np.asarray(solar_exposure, dtype=np.float32),
            np.asarray(lunar_exposure, dtype=np.float32),
            factors,
        )

    def _terrain_light_factor(
        self,
        normal_x,
        normal_y,
        normal_z,
        distance_m,
        sun_vec,
        sun_alt,
        terrain_shading_enabled=True,
        sun_visibility=None,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        """Compatibility brightness view over the celestial light components."""

        return self._terrain_light_components(
            normal_x,
            normal_y,
            normal_z,
            distance_m,
            sun_vec,
            sun_alt,
            terrain_shading_enabled=terrain_shading_enabled,
            sun_visibility=sun_visibility,
            light_context=light_context,
        ).intensity

    @staticmethod
    def _smooth_light_grid(
        light_grid, valid_mask, min_value=0.84, max_value=1.10
    ):
        values = np.asarray(light_grid, dtype=np.float32)
        valid = np.asarray(valid_mask, dtype=bool)
        if values.ndim != 2 or valid.shape != values.shape:
            return values

        source = np.where(valid, values, 1.0).astype(np.float32)
        weights = valid.astype(np.float32)
        source_pad = np.pad(source, ((1, 1), (1, 1)), mode="edge")
        weight_pad = np.pad(weights, ((1, 1), (1, 1)), mode="edge")
        kernel = (
            (1.0, 2.0, 1.0),
            (2.0, 4.0, 2.0),
            (1.0, 2.0, 1.0),
        )

        acc = np.zeros_like(source, dtype=np.float32)
        weight_sum = np.zeros_like(source, dtype=np.float32)
        for row in range(3):
            for col in range(3):
                weight = kernel[row][col]
                sample_weight = weight_pad[
                    row : row + values.shape[0], col : col + values.shape[1]
                ] * weight
                acc += (
                    source_pad[
                        row : row + values.shape[0],
                        col : col + values.shape[1],
                    ]
                    * sample_weight
                )
                weight_sum += sample_weight

        smoothed = np.divide(
            acc,
            np.maximum(weight_sum, 1e-6),
            out=np.ones_like(values, dtype=np.float32),
            where=weight_sum > 1e-6,
        )
        return np.where(
            valid,
            np.clip(smoothed, float(min_value), float(max_value)),
            values,
        ).astype(np.float32)

    def _profile_surface_samples(self, band, azimuths):
        cache = getattr(getattr(self, "profile", None), "surface_samples", None)
        rgba = _sample_cache_value(cache, "profile_rgba")
        valid = _sample_cache_value(cache, "profile_valid")
        if rgba is None or valid is None:
            shape = np.asarray(azimuths).shape
            return np.zeros(shape + (4,), dtype=np.uint8), np.zeros(shape, dtype=bool)
        rgba = np.asarray(rgba, dtype=np.uint8)
        valid = np.asarray(valid, dtype=bool)
        loaded = _sample_cache_value(cache, "profile_loaded")
        sources = _sample_cache_value(cache, "profile_source_indices")
        band_indices = np.asarray(
            _sample_cache_value(cache, "profile_band_indices", np.arange(rgba.shape[0])),
            dtype=np.int32,
        )
        target_band = int(getattr(band, "band_index", 0))
        matches = np.flatnonzero(band_indices == target_band)
        sampled_row = int(matches[0]) if matches.size else min(target_band, rgba.shape[0] - 1)
        original_azimuths = np.asarray(
            getattr(getattr(self, "profile", None), "azimuths", []), dtype=np.float64
        )
        sampled_indices = np.asarray(
            _sample_cache_value(cache, "profile_azimuth_indices", np.arange(rgba.shape[1])),
            dtype=np.int32,
        )
        sampled_angles = original_azimuths[sampled_indices]
        requested = np.asarray(azimuths, dtype=np.float64)
        distance = np.abs(
            ((requested[..., None] - sampled_angles[None, ...] + 180.0) % 360.0) - 180.0
        )
        nearest = np.argmin(distance, axis=-1)
        result_rgba = rgba[sampled_row, nearest]
        result_valid = valid[sampled_row, nearest]
        if loaded is not None:
            result_valid &= np.asarray(loaded, dtype=bool)[sampled_row, nearest]
        if _sample_cache_value(cache, "completion_state", "complete") == "visible_partial":
            result_valid &= np.isin(
                np.mod(np.rint(requested * 1_000_000.0).astype(np.int64), 360_000_000),
                np.mod(
                    np.rint(sampled_angles * 1_000_000.0).astype(np.int64),
                    360_000_000,
                ),
            )
        if sources is not None:
            result_valid = result_valid & (np.asarray(sources)[sampled_row, nearest] >= 0)
        return result_rgba, result_valid

    def _relief_surface_materials(
        self, row_indices, column_indices, mesh_shape
    ) -> TerrainMaterialSamples:
        cache = getattr(getattr(self, "profile", None), "surface_samples", None)
        visual_rgba = _sample_cache_value(cache, "visual_rgba")
        visual_valid = _sample_cache_value(cache, "visual_valid")
        visual_sources = _sample_cache_value(cache, "visual_source_indices")
        visual_classes = _sample_cache_value(cache, "visual_class_ids")
        visual_categorical = _sample_cache_value(cache, "visual_categorical")
        if (
            visual_rgba is not None
            and visual_valid is not None
            and visual_sources is not None
            and visual_classes is not None
            and visual_categorical is not None
            and np.shape(visual_rgba) == tuple(mesh_shape) + (4,)
            and np.shape(visual_valid) == tuple(mesh_shape)
            and np.shape(visual_sources) == tuple(mesh_shape)
            and np.shape(visual_classes) == tuple(mesh_shape)
            and np.shape(visual_categorical) == tuple(mesh_shape)
        ):
            rows = np.asarray(row_indices, dtype=np.int32)
            columns = np.asarray(column_indices, dtype=np.int32) % max(
                1, int(mesh_shape[1])
            )
            return TerrainMaterialSamples(
                np.asarray(visual_rgba, dtype=np.uint8)[rows, columns],
                np.asarray(visual_valid, dtype=bool)[rows, columns],
                np.asarray(visual_classes, dtype=np.int64)[rows, columns],
                np.asarray(visual_categorical, dtype=bool)[rows, columns],
                np.asarray(visual_sources, dtype=np.int16)[rows, columns],
            )
        rgba = _sample_cache_value(cache, "relief_rgba")
        valid = _sample_cache_value(cache, "relief_valid")
        sources = _sample_cache_value(cache, "relief_source_indices")
        classes = _sample_cache_value(cache, "relief_class_ids")
        categorical = _sample_cache_value(cache, "relief_categorical")
        rows = np.asarray(row_indices, dtype=np.int32)
        columns = np.asarray(column_indices, dtype=np.int32) % max(1, int(mesh_shape[1]))
        if rgba is None or valid is None:
            return TerrainMaterialSamples(
                np.zeros(rows.shape + (4,), dtype=np.uint8),
                np.zeros(rows.shape, dtype=bool),
                np.full(rows.shape, -1, dtype=np.int64),
                np.zeros(rows.shape, dtype=bool),
                np.full(rows.shape, -1, dtype=np.int16),
            )
        rgba = np.asarray(rgba, dtype=np.uint8)
        valid = np.asarray(valid, dtype=bool)
        sources = (
            np.asarray(sources, dtype=np.int16)
            if sources is not None
            else np.full(valid.shape, -1, dtype=np.int16)
        )
        classes = (
            np.asarray(classes, dtype=np.int64)
            if classes is not None
            else np.full(valid.shape, -1, dtype=np.int64)
        )
        categorical = (
            np.asarray(categorical, dtype=bool)
            if categorical is not None
            else np.zeros(valid.shape, dtype=bool)
        )
        if not (
            rgba.shape == valid.shape + (4,)
            and sources.shape == valid.shape
            and classes.shape == valid.shape
            and categorical.shape == valid.shape
        ):
            return TerrainMaterialSamples(
                np.zeros(rows.shape + (4,), dtype=np.uint8),
                np.zeros(rows.shape, dtype=bool),
                np.full(rows.shape, -1, dtype=np.int64),
                np.zeros(rows.shape, dtype=bool),
                np.full(rows.shape, -1, dtype=np.int16),
            )
        loaded = _sample_cache_value(cache, "relief_loaded")
        sampled_rows = np.asarray(
            _sample_cache_value(cache, "relief_distance_indices", np.arange(rgba.shape[0])),
            dtype=np.int32,
        )
        sampled_columns = np.asarray(
            _sample_cache_value(cache, "relief_azimuth_indices", np.arange(rgba.shape[1])),
            dtype=np.int32,
        ) % max(1, int(mesh_shape[1]))
        nearest_rows = np.argmin(np.abs(rows[..., None] - sampled_rows), axis=-1)
        circular = np.abs(columns[..., None] - sampled_columns)
        circular = np.minimum(circular, int(mesh_shape[1]) - circular)
        nearest_columns = np.argmin(circular, axis=-1)
        result_rgba = rgba[nearest_rows, nearest_columns]
        result_valid = valid[nearest_rows, nearest_columns]
        if loaded is not None:
            result_valid &= np.asarray(loaded, dtype=bool)[
                nearest_rows, nearest_columns
            ]
        if _sample_cache_value(cache, "completion_state", "complete") == "visible_partial":
            result_valid &= np.isin(rows, sampled_rows) & np.isin(
                columns, sampled_columns
            )
        result_sources = sources[nearest_rows, nearest_columns]
        result_classes = classes[nearest_rows, nearest_columns]
        result_categorical = categorical[nearest_rows, nearest_columns]
        result_valid &= result_sources >= 0
        return TerrainMaterialSamples(
            result_rgba,
            result_valid,
            result_classes,
            result_categorical,
            result_sources,
        )

    def _relief_surface_samples(self, row_indices, column_indices, mesh_shape):
        """Compatibility colour view over the complete material samples."""

        materials = self._relief_surface_materials(
            row_indices, column_indices, mesh_shape
        )
        return materials.base_rgba, materials.valid

    def _terrain_vertex_materials(self, asset, t_night) -> TerrainMaterialSamples:
        """Return aligned material identity and base colour for every vertex."""

        del t_night
        shape = asset.elevations.shape
        cache = getattr(
            getattr(self, "profile", None), "surface_samples", None
        )
        visual_rgba = _sample_cache_value(cache, "visual_rgba")
        visual_valid = _sample_cache_value(cache, "visual_valid")
        visual_loaded = _sample_cache_value(cache, "visual_loaded")
        visual_sources = _sample_cache_value(
            cache, "visual_source_indices"
        )
        visual_classes = _sample_cache_value(cache, "visual_class_ids")
        visual_categorical = _sample_cache_value(
            cache, "visual_categorical"
        )
        if (
            np.shape(visual_rgba) == shape + (4,)
            and np.shape(visual_valid) == shape
            and np.shape(visual_sources) == shape
            and np.shape(visual_classes) == shape
            and np.shape(visual_categorical) == shape
            and np.all(np.asarray(visual_valid, dtype=bool))
            and (
                visual_loaded is None
                or (
                    np.shape(visual_loaded) == shape
                    and np.all(np.asarray(visual_loaded, dtype=bool))
                )
            )
            and (
                not self.terrain_surface_opaque
                or np.all(
                    np.asarray(visual_rgba, dtype=np.uint8)[..., 3] == 255
                )
            )
        ):
            return TerrainMaterialSamples(
                np.asarray(visual_rgba, dtype=np.uint8),
                np.asarray(visual_valid, dtype=bool),
                np.asarray(visual_classes, dtype=np.int64),
                np.asarray(visual_categorical, dtype=bool),
                np.asarray(visual_sources, dtype=np.int16),
            )

        maximum = max(1.0, float(asset.distances[-1]))
        fallback = np.empty(shape + (4,), dtype=np.uint8)
        for row, distance in enumerate(asset.distances):
            palette_position = _clamp01(1.0 - float(distance) / maximum)
            _night_color, day_color = _palette_color(palette_position)
            fallback[row, :, :] = day_color.getRgb()

        rows, columns = np.indices(shape, dtype=np.int32)
        sampled = self._relief_surface_materials(
            rows, columns, shape
        )
        result = np.where(
            sampled.valid[..., None], sampled.base_rgba, fallback
        ).astype(np.uint8)
        if self.terrain_surface_opaque:
            result[..., 3] = 255
        return TerrainMaterialSamples(
            result,
            np.ones(shape, dtype=bool),
            np.where(sampled.valid, sampled.class_ids, -1),
            sampled.valid & sampled.categorical,
            np.where(sampled.valid, sampled.source_indices, -1),
        )

    @staticmethod
    def _surface_material_identity(surface_cache) -> tuple:
        """Return a stable source identity without retaining scientific arrays."""

        if surface_cache is None:
            return ("fallback",)
        completion_state = str(
            _sample_cache_value(
                surface_cache, "completion_state", "complete"
            )
        )
        cache_id = _sample_cache_value(surface_cache, "cache_id")
        if cache_id:
            return (
                "surface-cache",
                str(cache_id),
                completion_state,
            )
        key = _sample_cache_value(surface_cache, "key")
        digest = getattr(key, "digest", None)
        if digest:
            return ("surface-key", str(digest))
        return (
            "runtime-surface",
            id(surface_cache),
            completion_state,
        )

    def _static_material_settings_key(self) -> tuple:
        """Describe only settings that alter immutable terrain material."""

        settings = self.render_settings
        style = normalize_surface_visual_style(
            settings.surface_visual_style
        )
        if style != SurfaceVisualStyle.VIBRANT.value:
            return (
                "base-material-settings-v1",
                style,
                bool(self.terrain_surface_opaque),
            )
        return (
            "base-material-settings-v1",
            style,
            int(VIBRANT_PALETTE_VERSION),
            round(float(settings.vibrant_intensity), 6),
            round(
                float(settings.vibrant_territorial_luminance_variation), 6
            ),
            round(float(settings.vibrant_territorial_hue_variation), 6),
            round(float(settings.vibrant_material_midscale_variation), 6),
            round(float(settings.vibrant_material_microscale_variation), 6),
            round(float(settings.vibrant_material_altitude_influence), 6),
            round(float(settings.vibrant_material_slope_influence), 6),
            round(float(settings.vibrant_snow_rock_blend), 6),
            round(float(settings.vibrant_water_shore_variation), 6),
            bool(self.terrain_surface_opaque),
        )

    def _terrain_base_material_key(
        self, asset, surface_cache
    ) -> tuple:
        """Key a material by source, geometry/LOD and static style only."""

        return (
            "terrain-base-material-v1",
            self._surface_material_identity(surface_cache),
            int(asset.mesh_id),
            tuple(np.asarray(asset.elevations).shape),
            tuple(np.asarray(asset.near_patch_elevations).shape),
            tuple(
                _sample_cache_value(surface_cache, "source_legend_ids", ())
                or ()
            ),
            self._static_material_settings_key(),
        )

    def _build_terrain_base_material(
        self, asset, surface_cache
    ) -> TerrainBaseMaterialCache:
        """Build the camera/time-independent material grids once."""

        key = self._terrain_base_material_key(asset, surface_cache)
        cached = (
            self._terrain_base_material_cache.get(key)
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        if isinstance(cached, TerrainBaseMaterialCache):
            self._last_base_material_cache_hit = True
            return cached

        self._last_base_material_cache_hit = False
        polar = self._terrain_vertex_materials(asset, 0.0)
        near_patch = self._near_patch_vertex_materials(asset, 0.0)
        vibrant = (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            )
            == SurfaceVisualStyle.VIBRANT.value
        )
        if vibrant:
            settings = self.render_settings
            legends = tuple(
                _sample_cache_value(
                    surface_cache, "source_legend_ids", ()
                )
                or ()
            )
            polar = _vibrant_categorical_palette(polar, surface_cache)
            polar_azimuth = np.radians(
                np.asarray(asset.azimuths, dtype=np.float64)
            )
            polar_distance = np.asarray(
                asset.distances, dtype=np.float64
            )
            polar_east = (
                polar_distance[:, None] * np.sin(polar_azimuth)[None, :]
            )
            polar_north = (
                polar_distance[:, None] * np.cos(polar_azimuth)[None, :]
            )
            polar_rgba = _apply_categorical_territorial_variation(
                polar.base_rgba,
                polar,
                polar_east,
                polar_north,
                polar.valid,
                strength=settings.vibrant_intensity,
                luminance_variation=(
                    settings.vibrant_territorial_luminance_variation
                ),
                hue_variation=settings.vibrant_territorial_hue_variation,
                midscale_variation=(
                    settings.vibrant_material_midscale_variation
                ),
                microscale_variation=(
                    settings.vibrant_material_microscale_variation
                ),
                altitude_influence=(
                    settings.vibrant_material_altitude_influence
                ),
                slope_influence=settings.vibrant_material_slope_influence,
                snow_rock_blend=settings.vibrant_snow_rock_blend,
                water_shore_variation=(
                    settings.vibrant_water_shore_variation
                ),
                elevation_m=asset.elevations,
                normal_x=asset.normal_x,
                normal_y=asset.normal_y,
                normal_z=asset.normal_z,
                source_legend_ids=legends,
                render_scale=1.0,
                include_solar_response=False,
            )
            polar = TerrainMaterialSamples(
                polar_rgba,
                polar.valid,
                polar.class_ids,
                polar.categorical,
                polar.source_indices,
            )

            near_patch = _vibrant_categorical_palette(
                near_patch, surface_cache
            )
            if np.asarray(near_patch.valid).size:
                patch_east, patch_north = np.meshgrid(
                    np.asarray(
                        asset.near_patch_eastings, dtype=np.float64
                    ),
                    np.asarray(
                        asset.near_patch_northings, dtype=np.float64
                    ),
                )
                patch_rgba = _apply_categorical_territorial_variation(
                    near_patch.base_rgba,
                    near_patch,
                    patch_east,
                    patch_north,
                    near_patch.valid,
                    strength=settings.vibrant_intensity,
                    luminance_variation=(
                        settings.vibrant_territorial_luminance_variation
                    ),
                    hue_variation=(
                        settings.vibrant_territorial_hue_variation
                    ),
                    midscale_variation=(
                        settings.vibrant_material_midscale_variation
                    ),
                    microscale_variation=(
                        settings.vibrant_material_microscale_variation
                    ),
                    altitude_influence=(
                        settings.vibrant_material_altitude_influence
                    ),
                    slope_influence=(
                        settings.vibrant_material_slope_influence
                    ),
                    snow_rock_blend=settings.vibrant_snow_rock_blend,
                    water_shore_variation=(
                        settings.vibrant_water_shore_variation
                    ),
                    elevation_m=asset.near_patch_elevations,
                    normal_x=asset.near_patch_normal_x,
                    normal_y=asset.near_patch_normal_y,
                    normal_z=asset.near_patch_normal_z,
                    source_legend_ids=legends,
                    render_scale=1.0,
                    include_solar_response=False,
                )
                near_patch = TerrainMaterialSamples(
                    patch_rgba,
                    near_patch.valid,
                    near_patch.class_ids,
                    near_patch.categorical,
                    near_patch.source_indices,
                )

        shared_arrays = tuple(
            _sample_cache_value(surface_cache, name)
            for prefix in ("visual", "relief", "near_patch")
            for name in (
                f"{prefix}_rgba",
                f"{prefix}_valid",
                f"{prefix}_class_ids",
                f"{prefix}_categorical",
                f"{prefix}_source_indices",
            )
        )
        owned_bytes = _owned_material_samples_size(
            (polar, near_patch), shared_arrays
        )
        if vibrant:
            polar_protected = _protected_categorical_regions(
                polar, surface_cache
            )
            near_patch_protected = _protected_categorical_regions(
                near_patch, surface_cache
            )
        else:
            polar_protected = np.zeros(
                np.asarray(polar.valid).shape, dtype=bool
            )
            near_patch_protected = np.zeros(
                np.asarray(near_patch.valid).shape, dtype=bool
            )
        owned_bytes += int(
            polar_protected.nbytes + near_patch_protected.nbytes
        )
        entry = TerrainBaseMaterialCache(
            key,
            _freeze_material_samples(polar),
            _freeze_material_samples(near_patch),
            polar_protected,
            near_patch_protected,
            owned_bytes,
        )
        self._terrain_base_material_builds += 1
        if PERFORMANCE_FLAGS.relief_cached:
            self._terrain_base_material_cache.put(
                key, entry, entry.resident_bytes
            )
        return entry

    def _terrain_vertex_base_rgba(self, asset, t_night):
        """Compatibility view used by legacy callers."""

        return self._terrain_vertex_materials(asset, t_night).base_rgba

    def _near_patch_vertex_materials(
        self, asset, t_night
    ) -> TerrainMaterialSamples:
        """Return aligned near-patch material samples with terrain fallback."""

        del t_night
        shape = asset.near_patch_elevations.shape
        cache = getattr(
            getattr(self, "profile", None), "surface_samples", None
        )
        sampled = _sample_cache_value(cache, "near_patch_rgba")
        sampled_valid = _sample_cache_value(cache, "near_patch_valid")
        sampled_loaded = _sample_cache_value(cache, "near_patch_loaded")
        sampled_sources = _sample_cache_value(
            cache, "near_patch_source_indices"
        )
        sampled_classes = _sample_cache_value(
            cache, "near_patch_class_ids"
        )
        sampled_categorical = _sample_cache_value(
            cache, "near_patch_categorical"
        )
        if (
            np.shape(sampled) == shape + (4,)
            and np.shape(sampled_valid) == shape
            and np.shape(sampled_sources) == shape
            and np.shape(sampled_classes) == shape
            and np.shape(sampled_categorical) == shape
            and np.all(np.asarray(sampled_valid, dtype=bool))
            and (
                sampled_loaded is None
                or (
                    np.shape(sampled_loaded) == shape
                    and np.all(np.asarray(sampled_loaded, dtype=bool))
                )
            )
            and (
                not self.terrain_surface_opaque
                or np.all(
                    np.asarray(sampled, dtype=np.uint8)[..., 3] == 255
                )
            )
        ):
            return TerrainMaterialSamples(
                np.asarray(sampled, dtype=np.uint8),
                np.asarray(sampled_valid, dtype=bool),
                np.asarray(sampled_classes, dtype=np.int64),
                np.asarray(sampled_categorical, dtype=bool),
                np.asarray(sampled_sources, dtype=np.int16),
            )

        _night_color, day_color = _palette_color(1.0)
        fallback_color = np.asarray(
            day_color.getRgb(),
            dtype=np.uint8,
        )
        fallback = np.broadcast_to(fallback_color, shape + (4,)).copy()
        if (
            sampled is not None
            and sampled_valid is not None
            and np.shape(sampled) == shape + (4,)
            and np.shape(sampled_valid) == shape
        ):
            sampled_valid = np.asarray(sampled_valid, dtype=bool)
            result = np.where(
                sampled_valid[..., None],
                np.asarray(sampled, dtype=np.uint8),
                fallback,
            ).astype(np.uint8)
            source_indices = np.where(
                sampled_valid,
                (
                    np.asarray(sampled_sources, dtype=np.int16)
                    if sampled_sources is not None
                    and np.shape(sampled_sources) == shape
                    else -1
                ),
                -1,
            )
            class_ids = np.where(
                sampled_valid,
                (
                    np.asarray(sampled_classes, dtype=np.int64)
                    if sampled_classes is not None
                    and np.shape(sampled_classes) == shape
                    else -1
                ),
                -1,
            )
            categorical = sampled_valid & (
                np.asarray(sampled_categorical, dtype=bool)
                if sampled_categorical is not None
                and np.shape(sampled_categorical) == shape
                else np.zeros(shape, dtype=bool)
            )
        else:
            result = fallback
            source_indices = np.full(shape, -1, dtype=np.int16)
            class_ids = np.full(shape, -1, dtype=np.int64)
            categorical = np.zeros(shape, dtype=bool)
        if self.terrain_surface_opaque:
            result[..., 3] = 255
        return TerrainMaterialSamples(
            result,
            np.ones(shape, dtype=bool),
            class_ids,
            categorical,
            source_indices,
        )

    def _near_patch_vertex_base_rgba(self, asset, t_night):
        """Compatibility view used by legacy callers."""

        return self._near_patch_vertex_materials(asset, t_night).base_rgba

    def _apply_terrain_light(
        self, color: QColor, light_factor: float, sky_color: QColor, t_night: float
    ) -> QColor:
        del sky_color
        if not self.render_settings.terrain_lighting_enabled:
            return QColor(color)
        factor = max(
            0.0,
            min(
                max(
                    self.render_settings.terrain_max_brightness,
                    self.render_settings.vibrant_sun_max_brightness,
                ),
                float(light_factor),
            ),
        )
        del t_night
        rgb = np.asarray(
            (color.red(), color.green(), color.blue()), dtype=np.float32
        ) * factor
        return QColor(
            max(0, min(255, round(float(rgb[0])))),
            max(0, min(255, round(float(rgb[1])))),
            max(0, min(255, round(float(rgb[2])))),
            color.alpha(),
        )

    def _apply_terrain_atmosphere(
        self, color: QColor, distance_m: float, sky_color: QColor, t_night: float
    ) -> QColor:
        del t_night
        base = np.asarray(color.getRgb(), dtype=np.uint8)
        result = compose_vertex_rgba(
            base,
            1.0,
            float(distance_m),
            self.render_settings,
            maximum_distance_m=self._maximum_terrain_distance_m(),
            horizon_rgb=(
                sky_color.red(),
                sky_color.green(),
                sky_color.blue(),
            ),
        )
        return _qcolor_from_rgba(result)

    def _compose_terrain_color(
        self,
        base_color: QColor,
        light_factor: float,
        distance_m: float,
        sky_color: QColor,
        t_night: float,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> QColor:
        if isinstance(light_context, TerrainCelestialLightContext):
            factors = terrain_celestial_light_factors(
                light_context, self.render_settings
            )
            vibrant = (
                normalize_surface_visual_style(
                    self.render_settings.surface_visual_style
                )
                == SurfaceVisualStyle.VIBRANT.value
            )
            day_ambient = (
                self.render_settings.vibrant_sun_ambient_strength
                if vibrant
                else self.render_settings.terrain_ambient_strength
            )
            ambient = (
                self.render_settings.terrain_night_ambient_strength
                + (
                    day_ambient
                    - self.render_settings.terrain_night_ambient_strength
                )
                * factors.solar_ambient
                + self.render_settings.terrain_moon_ambient_strength
                * factors.lunar_strength
            )
            directional = max(0.0, float(light_factor) - float(ambient))
            solar_exposure = 0.0
            lunar_exposure = 0.0
            if factors.solar_direct > 0.0:
                day_diffuse = self.render_settings.terrain_diffuse_strength * (
                    self.render_settings.vibrant_sun_diffuse_boost
                    if vibrant
                    else 1.0
                )
                solar_exposure = _clamp01(
                    directional / max(1e-6, float(day_diffuse))
                )
            elif factors.lunar_strength > 0.0:
                lunar_exposure = _clamp01(
                    directional
                    / max(
                        1e-6,
                        float(self.render_settings.terrain_moon_diffuse_strength),
                    )
                )
            return self._compose_profile_light_color(
                base_color,
                light_factor,
                distance_m,
                sky_color,
                factors,
                solar_exposure=solar_exposure,
                lunar_exposure=lunar_exposure,
            )
        lit = self._apply_terrain_light(
            QColor(base_color), light_factor, sky_color, t_night
        )
        return self._apply_terrain_atmosphere(
            lit, distance_m, sky_color, t_night
        )

    def _mesh_quad_color(
        self,
        distance_m,
        nx,
        ny,
        nz,
        t_night,
        sky_color,
        sun_vec,
        sun_alt,
        terrain_shading_enabled=True,
        light_factor=None,
        base_color=None,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        haze = float(
            atmospheric_fog_factor(
                distance_m,
                self.render_settings,
                maximum_distance_m=self._maximum_terrain_distance_m(),
            )
        )
        palette_t = _clamp01(1.0 - haze)
        night_c, day_c = _palette_color(palette_t)
        base = (
            QColor(base_color)
            if base_color is not None
            else (
                QColor(day_c)
                if light_context is not None
                else _lerp_color(day_c, night_c, t_night)
            )
        )

        if light_factor is None:
            light_factor = float(
                np.asarray(
                    self._terrain_light_factor(
                        nx,
                        ny,
                        nz,
                        distance_m,
                        sun_vec,
                        sun_alt,
                        terrain_shading_enabled=terrain_shading_enabled,
                        light_context=light_context,
                    )
                )
            )

        return self._compose_terrain_color(
            base,
            light_factor,
            distance_m,
            sky_color,
            t_night,
            light_context=light_context,
        )

    def _terrain_surface_color(
        self,
        distance_m,
        light_factor,
        t_night,
        sky_color,
        sun_vec,
        sun_alt,
        terrain_shading_enabled,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        color = self._mesh_quad_color(
            distance_m,
            0.0,
            0.0,
            1.0,
            t_night,
            sky_color,
            sun_vec,
            sun_alt,
            terrain_shading_enabled=terrain_shading_enabled,
            light_factor=light_factor,
            light_context=light_context,
        )
        if light_context is not None:
            if self.terrain_surface_opaque:
                color.setAlpha(255)
            return color
        haze = float(
            atmospheric_fog_factor(
                distance_m,
                self.render_settings,
                maximum_distance_m=self._maximum_terrain_distance_m(),
            )
        )
        calm_night, calm_day = _palette_color(
            _clamp01(0.62 + 0.18 * (1.0 - haze))
        )
        calm_base = _lerp_color(calm_day, calm_night, t_night)
        haze_color = _atmospheric_haze_color(sky_color, t_night)
        calm_base = _lerp_color(calm_base, haze_color, 0.08 + 0.22 * haze)
        color = _lerp_color(calm_base, color, 0.70)
        alpha = int(82 + 58 * (1.0 - haze))
        alpha = int(alpha * (1.0 - 0.30 * _clamp01(t_night)))
        if self.terrain_surface_opaque:
            color.setAlpha(255)
        else:
            color.setAlpha(max(68, min(140, alpha)))
        return color

    def _terrain_span_brush(
        self,
        seg_x,
        segment_shade,
        distance_m,
        t_night,
        sky_color,
        sun_vec,
        sun_alt,
        terrain_shading_enabled,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        finite = np.isfinite(seg_x) & np.isfinite(segment_shade)
        if np.count_nonzero(finite) < 2:
            finite_shade = np.asarray(segment_shade)[
                np.isfinite(segment_shade)
            ]
            light_factor = (
                float(np.mean(finite_shade)) if finite_shade.size else 1.0
            )
            return QBrush(
                self._terrain_surface_color(
                    distance_m,
                    light_factor,
                    t_night,
                    sky_color,
                    sun_vec,
                    sun_alt,
                    terrain_shading_enabled,
                    light_context,
                )
            )

        x_values = np.asarray(seg_x[finite], dtype=np.float32)
        shade_values = np.asarray(segment_shade[finite], dtype=np.float32)
        x_min = float(np.min(x_values))
        x_max = float(np.max(x_values))
        if (
            self.render_settings.terrain_shading_mode == "flat"
            or x_max - x_min < 1.0
            or np.ptp(shade_values) < 0.006
        ):
            light_factor = float(np.mean(shade_values))
            return QBrush(
                self._terrain_surface_color(
                    distance_m,
                    light_factor,
                    t_night,
                    sky_color,
                    sun_vec,
                    sun_alt,
                    terrain_shading_enabled,
                    light_context,
                )
            )

        gradient = QLinearGradient(x_min, 0.0, x_max, 0.0)
        stop_count = min(18, len(x_values))
        stop_indices = np.unique(
            np.linspace(0, len(x_values) - 1, stop_count).astype(np.int32)
        )
        stops = []
        for index in stop_indices:
            position = _clamp01(
                (float(x_values[index]) - x_min) / (x_max - x_min)
            )
            color = self._terrain_surface_color(
                distance_m,
                float(shade_values[index]),
                t_night,
                sky_color,
                sun_vec,
                sun_alt,
                terrain_shading_enabled,
                light_context,
            )
            stops.append((position, color))
        for position, color in sorted(stops, key=lambda item: item[0]):
            gradient.setColorAt(position, color)
        return QBrush(gradient)

    def _project_mesh_column(
        self,
        projection_fn,
        projection_fn_numpy,
        az_value,
        altitudes,
        height,
        px_alt,
    ):
        if projection_fn_numpy:
            az_arr = np.full_like(altitudes, float(az_value), dtype=np.float32)
            projected = projection_fn_numpy(
                np.asarray(altitudes, dtype=np.float64),
                np.asarray(az_arr, dtype=np.float64),
            )
            if projected is None or len(projected) < 2:
                empty = np.full(np.shape(altitudes), np.nan, dtype=np.float64)
                return empty, empty.copy()
            sx, sy = projected[:2]
            sx = np.asarray(sx, dtype=np.float64)
            sy = np.asarray(sy, dtype=np.float64)
            if len(projected) >= 3:
                projected_valid = np.asarray(projected[2], dtype=bool)
                sx = np.where(projected_valid, sx, np.nan)
                sy = np.where(projected_valid, sy, np.nan)
            return sx, sy

        sx = []
        sy = []
        for alt in altitudes:
            point = projection_fn(float(alt), float(az_value))
            if point:
                sx.append(point[0])
                sy.append(point[1])
            else:
                sx.append(np.nan)
                sy.append(np.nan)
        return np.asarray(sx, dtype=np.float64), np.asarray(sy, dtype=np.float64)

    def _profile_horizon_lookup(self):
        if not self._layers:
            return None
        az_ref = None
        max_h = None
        for band_pts, _night_c, _day_c in self._layers:
            az_raw, h_raw = band_pts.points
            if az_raw is None:
                continue
            valid = getattr(band_pts, "valid_mask", None)
            if valid is None:
                valid = np.ones_like(az_raw, dtype=bool)
            az = np.asarray(az_raw, dtype=np.float32)
            h = np.asarray(h_raw, dtype=np.float32)
            valid = np.asarray(valid, dtype=bool)
            if az_ref is None:
                az_ref = az
                max_h = np.full_like(az_ref, -np.inf, dtype=np.float32)
            if len(az) != len(az_ref) or not np.allclose(az, az_ref):
                h = np.interp(az_ref, az, h, left=-np.inf, right=-np.inf)
                valid = np.isfinite(h)
            max_h = np.maximum(
                max_h,
                np.where(valid & np.isfinite(h), h, -np.inf).astype(
                    np.float32
                ),
            )
        if az_ref is None or max_h is None:
            return None
        valid = np.isfinite(max_h) & (max_h > -80.0)
        if not np.any(valid):
            return None
        return az_ref[valid], max_h[valid]

    @staticmethod
    def _quad_area_px(points) -> float:
        area = 0.0
        for i, p0 in enumerate(points):
            p1 = points[(i + 1) % len(points)]
            area += float(p0.x()) * float(p1.y())
            area -= float(p1.x()) * float(p0.y())
        return abs(area) * 0.5

    def _draw_terrain_surface_2d(
        self,
        painter,
        mesh,
        projection_fn,
        width,
        height,
        px_alt,
        cur_az,
        az_min,
        az_max,
        t_night,
        sky_color,
        sun_alt,
        sun_az,
        terrain_shading_enabled=True,
        projection_fn_numpy=None,
        interaction_active=False,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        self._last_surface2d_quads = 0
        self._last_surface2d_vertices = 0
        self._last_surface2d_max_error_px = 0.0
        self._last_surface2d_geometry_s = 0.0
        self._last_surface2d_paint_s = 0.0
        asset = self._terrain_render_asset
        surface_cache = getattr(getattr(self, "profile", None), "surface_samples", None)
        visual_altitudes = _sample_cache_value(surface_cache, "visual_altitudes")
        mesh_token = (
            hash((id(mesh), id(visual_altitudes)))
            if visual_altitudes is not None
            else id(mesh)
        )
        if asset is None or asset.mesh_id != mesh_token:
            asset = self._prepare_terrain_render_asset(mesh)
            if PERFORMANCE_FLAGS.relief_cached:
                self._terrain_render_asset = asset
        if asset is None:
            return
        az_raw = asset.azimuths
        distances = asset.distances
        altitudes = asset.altitudes
        elevations = asset.elevations
        valid = asset.valid
        visible = asset.visible
        normal_x = asset.normal_x
        normal_y = asset.normal_y
        normal_z = asset.normal_z

        light_context = self._resolve_light_context(
            light_context, sun_alt=sun_alt, sun_az=sun_az
        )
        sun_alt, sun_az, sun_vec = self._configured_light(
            sun_alt, sun_az, light_context
        )
        light_factors = terrain_celestial_light_factors(
            light_context, self.render_settings
        )
        terrain_shading_enabled = bool(
            terrain_shading_enabled
            and self.render_settings.terrain_lighting_enabled
        )
        shade_key = (
            asset.mesh_id,
            bool(terrain_shading_enabled),
            None if sun_alt is None else round(float(sun_alt) * 4.0) / 4.0,
            None if sun_az is None else round(float(sun_az) * 4.0) / 4.0,
            (
                None
                if light_context.moon_altitude_deg is None
                else round(float(light_context.moon_altitude_deg) * 4.0) / 4.0
            ),
            (
                None
                if light_context.moon_azimuth_deg is None
                else round(float(light_context.moon_azimuth_deg) * 4.0) / 4.0
            ),
            round(float(light_context.moon_illumination) * 256.0) / 256.0,
            round(float(light_context.eclipse_factor) * 256.0) / 256.0,
            repr(self.render_settings),
        )
        shade_grid = (
            self._terrain_shade_cache.get(shade_key)
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        if shade_grid is None:
            sun_visibility = self._terrain_sun_visibility(
                mesh,
                elevations,
                valid,
                visible,
                distances,
                az_raw,
                sun_alt,
                sun_az,
                terrain_shading_enabled=terrain_shading_enabled,
            )
            shade_grid = self._terrain_light_factor(
                normal_x,
                normal_y,
                normal_z,
                distances[:, None],
                sun_vec,
                sun_alt,
                terrain_shading_enabled=terrain_shading_enabled,
                sun_visibility=sun_visibility,
                light_context=light_context,
            )
            minimum_light, maximum_light = self._terrain_light_bounds(
                light_factors
            )
            shade_grid = self._smooth_light_grid(
                shade_grid,
                valid & visible,
                min_value=minimum_light,
                max_value=maximum_light,
            )
            if PERFORMANCE_FLAGS.relief_cached:
                self._terrain_shade_cache.put(
                    shade_key, shade_grid, int(shade_grid.nbytes)
                )
        geometry = self._terrain_geometry_for_view(
            asset,
            projection_fn,
            width,
            height,
            px_alt,
            cur_az,
            az_min,
            az_max,
            projection_fn_numpy=projection_fn_numpy,
        )
        self._last_surface2d_quads = int(geometry.metrics.spans)
        self._last_surface2d_vertices = int(geometry.metrics.output_vertices)
        self._last_surface2d_max_error_px = float(
            geometry.metrics.max_error_px
        )
        self._last_surface2d_geometry_s = float(geometry.metrics.elapsed_s)

        paint_started = time.perf_counter()
        quantized_night = round(float(t_night) * 256.0) / 256.0
        surface_cache_key = (
            int(width),
            int(height),
            shade_key,
            quantized_night,
            int(sky_color.rgba()),
            bool(terrain_shading_enabled),
            bool(self.terrain_surface_opaque),
        )
        if (
            PERFORMANCE_FLAGS.relief_cached
            and self._terrain_surface_image_geometry is geometry
            and surface_cache_key == self._terrain_surface_image_cache_key
            and self._terrain_surface_image_cache is not None
        ):
            painter.drawImage(0, 0, self._terrain_surface_image_cache)
            self._last_surface2d_quads = int(self._terrain_surface_image_drawn)
            self._last_surface2d_paint_s = float(
                time.perf_counter() - paint_started
            )
            painter.setRenderHint(QPainter.Antialiasing, True)
            return

        surface_image = None
        surface_painter = None
        target_painter = painter
        if PERFORMANCE_FLAGS.relief_cached:
            surface_image = QImage(
                int(width), int(height), QImage.Format_ARGB32_Premultiplied
            )
            surface_image.fill(0)
            surface_painter = QPainter(surface_image)
            target_painter = surface_painter

        # Adjacent depth spans share exact boundaries. Antialiasing each fill
        # independently exposes sub-pixel sky seams between those polygons;
        # the final profile ridge is antialiased separately after the surface.
        target_painter.setRenderHint(QPainter.Antialiasing, False)
        target_painter.setPen(Qt.NoPen)
        drawn = 0
        for span, polygon in self._terrain_polygons_for_geometry(geometry):
            xs = span.x
            ys = np.concatenate((span.top_y, span.bottom_y))
            if float(np.max(xs)) < -64.0 or float(np.min(xs)) > float(width) + 64.0:
                continue
            if float(np.max(ys)) < -64.0 or float(np.min(ys)) > float(height) + 64.0:
                continue
            if self._quad_area_px(polygon) < 0.35:
                continue
            segment_shade = shade_grid[
                int(span.row_index), span.column_indices
            ]
            brush = self._terrain_span_brush(
                span.x,
                segment_shade,
                span.distance_m,
                quantized_night,
                sky_color,
                sun_vec,
                sun_alt,
                terrain_shading_enabled,
                light_context=light_context,
            )
            target_painter.setBrush(brush)
            target_painter.drawPolygon(polygon)
            drawn += 1

        if surface_painter is not None and surface_image is not None:
            surface_painter.end()
            self._terrain_surface_image_cache_key = surface_cache_key
            self._terrain_surface_image_cache = surface_image
            self._terrain_surface_image_geometry = geometry
            self._terrain_surface_image_drawn = int(drawn)
            painter.drawImage(0, 0, surface_image)
        self._last_surface2d_quads = drawn
        self._last_surface2d_paint_s = float(time.perf_counter() - paint_started)
        painter.setRenderHint(QPainter.Antialiasing, True)

    def _terrain_triangles_for_view(
        self,
        asset,
        projection_fn,
        width,
        height,
        cur_az,
        az_min,
        az_max,
        projection_fn_numpy=None,
    ):
        """Project the complete polar mesh and form valid screen triangles."""

        signature = self._projection_geometry_signature(
            projection_fn, float(cur_az) - 90.0, float(cur_az) + 90.0
        )
        cache_key = (
            "triangles-v2-categorical-grid",
            int(asset.mesh_id),
            int(width),
            int(height),
            float(cur_az),
            float(az_min),
            float(az_max),
            signature,
        )
        if (
            PERFORMANCE_FLAGS.relief_cached
            and cache_key == self._terrain_geometry_cache_key
            and isinstance(self._terrain_geometry_cache, _TerrainTriangleGeometry)
        ):
            return self._terrain_geometry_cache

        started = time.perf_counter()
        azimuths = np.asarray(asset.azimuths, dtype=np.float64)
        relative = (azimuths - float(cur_az) + 180.0) % 360.0 - 180.0
        full_order = np.argsort(relative, kind="stable")
        full_unwrapped = float(cur_az) + relative[full_order]
        in_view = (
            (full_unwrapped >= float(az_min))
            & (full_unwrapped <= float(az_max))
        )
        selected = np.flatnonzero(in_view)
        if selected.size:
            selected = np.arange(
                max(0, int(selected[0]) - 1),
                min(full_order.size, int(selected[-1]) + 2),
                dtype=np.int32,
            )
        else:
            selected = np.empty(0, dtype=np.int32)
        order = full_order[selected]
        unwrapped_az = full_unwrapped[selected]
        if order.size < 2:
            return None
        altitudes = np.asarray(asset.altitudes[:, order], dtype=np.float64)
        az_grid = np.broadcast_to(unwrapped_az[None, :], altitudes.shape)

        if projection_fn_numpy is not None:
            projected = projection_fn_numpy(altitudes, az_grid)
            if projected is None or len(projected) < 2:
                return None
            sx = np.asarray(projected[0], dtype=np.float64)
            sy = np.asarray(projected[1], dtype=np.float64)
            projected_valid = np.ones(altitudes.shape, dtype=bool)
            if len(projected) >= 3:
                projected_valid &= np.asarray(projected[2], dtype=bool)
        else:
            sx = np.full(altitudes.shape, np.nan, dtype=np.float64)
            sy = np.full(altitudes.shape, np.nan, dtype=np.float64)
            projected_valid = np.zeros(altitudes.shape, dtype=bool)
            for row in range(altitudes.shape[0]):
                for column in range(altitudes.shape[1]):
                    point = projection_fn(
                        float(altitudes[row, column]),
                        float(unwrapped_az[column]),
                    )
                    if point is not None and np.all(np.isfinite(point[:2])):
                        sx[row, column], sy[row, column] = point[:2]
                        projected_valid[row, column] = True

        original_valid = np.asarray(asset.valid[:, order], dtype=bool)
        original_visible = np.asarray(asset.visible[:, order], dtype=bool)
        vertex_valid = original_valid & projected_valid & np.isfinite(sx) & np.isfinite(sy)
        az_delta = np.diff(unwrapped_az)
        finite_steps = az_delta[np.isfinite(az_delta) & (az_delta > 1e-9)]
        nominal_step = float(np.median(finite_steps)) if finite_steps.size else 1.0
        adjacent = np.isfinite(az_delta) & (az_delta > 0.0) & (
            az_delta <= nominal_step * 1.5 + 1e-9
        )
        cell_valid = (
            vertex_valid[:-1, :-1]
            & vertex_valid[1:, :-1]
            & vertex_valid[1:, 1:]
            & vertex_valid[:-1, 1:]
            & adjacent[None, :]
        )
        # A cell whose four angular samples remain below a nearer running
        # maximum cannot contribute to the final image.  Keeping every cell
        # touching a visible vertex supplies a one-cell transition band while
        # avoiding work the z-buffer would deterministically discard.
        cell_visible = (
            original_visible[:-1, :-1]
            | original_visible[1:, :-1]
            | original_visible[1:, 1:]
            | original_visible[:-1, 1:]
        )
        cell_valid &= cell_visible
        cell_rows, cell_columns = np.nonzero(cell_valid)
        if cell_rows.size:
            triangle_rows = np.empty((cell_rows.size * 2, 3), dtype=np.int32)
            triangle_columns_sorted = np.empty_like(triangle_rows)
            triangle_rows[0::2] = np.column_stack(
                (cell_rows, cell_rows + 1, cell_rows + 1)
            )
            triangle_columns_sorted[0::2] = np.column_stack(
                (cell_columns, cell_columns, cell_columns + 1)
            )
            triangle_rows[1::2] = np.column_stack(
                (cell_rows, cell_rows + 1, cell_rows)
            )
            triangle_columns_sorted[1::2] = np.column_stack(
                (cell_columns, cell_columns + 1, cell_columns + 1)
            )
            triangle_columns = order[triangle_columns_sorted].astype(np.int32)
            triangle_x = sx[triangle_rows, triangle_columns_sorted]
            triangle_y = sy[triangle_rows, triangle_columns_sorted]
            xy = np.stack((triangle_x, triangle_y), axis=2)
            depth = np.asarray(asset.distances, dtype=np.float64)[triangle_rows]
            vertex_domain = np.zeros(triangle_rows.shape, dtype=np.uint8)
        else:
            xy = np.empty((0, 3, 2), dtype=np.float64)
            depth = np.empty((0, 3), dtype=np.float64)
            triangle_rows = np.empty((0, 3), dtype=np.int32)
            triangle_columns = np.empty((0, 3), dtype=np.int32)
            vertex_domain = np.empty((0, 3), dtype=np.uint8)

        # The field immediately below the observer is a genuine Cartesian ENU
        # mesh. Unlike a polar fan it has no collapsed azimuthal edge and thus
        # remains well-conditioned when the camera points at the nadir.
        patch_shape = np.shape(asset.near_patch_altitudes)
        patch_vertex_count = int(np.prod(patch_shape)) if len(patch_shape) == 2 else 0
        if patch_shape[0] >= 2 and patch_shape[1] >= 2:
            patch_east, patch_north = np.meshgrid(
                np.asarray(asset.near_patch_eastings, dtype=np.float64),
                np.asarray(asset.near_patch_northings, dtype=np.float64),
            )
            patch_distance = np.hypot(patch_east, patch_north)
            convergence = float(
                getattr(getattr(self, "profile", None), "grid_convergence_deg", 0.0)
                or 0.0
            )
            patch_azimuth = (
                np.degrees(np.arctan2(patch_east, patch_north)) + convergence
            ) % 360.0
            patch_relative = (
                patch_azimuth - float(cur_az) + 180.0
            ) % 360.0 - 180.0
            patch_unwrapped = float(cur_az) + patch_relative
            patch_unwrapped = np.where(
                patch_distance <= 1e-9, float(cur_az), patch_unwrapped
            )
            patch_altitudes = np.asarray(
                asset.near_patch_altitudes, dtype=np.float64
            )
            if projection_fn_numpy is not None:
                patch_projected = projection_fn_numpy(
                    patch_altitudes, patch_unwrapped
                )
                patch_sx = np.asarray(patch_projected[0], dtype=np.float64)
                patch_sy = np.asarray(patch_projected[1], dtype=np.float64)
                patch_projected_valid = np.ones(patch_shape, dtype=bool)
                if len(patch_projected) >= 3:
                    patch_projected_valid &= np.asarray(
                        patch_projected[2], dtype=bool
                    )
            else:
                patch_sx = np.full(patch_shape, np.nan, dtype=np.float64)
                patch_sy = np.full(patch_shape, np.nan, dtype=np.float64)
                patch_projected_valid = np.zeros(patch_shape, dtype=bool)
                for patch_row, patch_column in np.ndindex(patch_shape):
                    point = projection_fn(
                        float(patch_altitudes[patch_row, patch_column]),
                        float(patch_unwrapped[patch_row, patch_column]),
                    )
                    if point is not None and np.all(np.isfinite(point[:2])):
                        patch_sx[patch_row, patch_column] = point[0]
                        patch_sy[patch_row, patch_column] = point[1]
                        patch_projected_valid[patch_row, patch_column] = True
            patch_vertex_valid = (
                np.asarray(asset.near_patch_valid, dtype=bool)
                & patch_projected_valid
                & np.isfinite(patch_sx)
                & np.isfinite(patch_sy)
            )
            patch_cells = (
                patch_vertex_valid[:-1, :-1]
                & patch_vertex_valid[1:, :-1]
                & patch_vertex_valid[1:, 1:]
                & patch_vertex_valid[:-1, 1:]
            )
            patch_rows, patch_columns = np.nonzero(patch_cells)
            if patch_rows.size:
                patch_triangle_rows = np.empty(
                    (patch_rows.size * 2, 3), dtype=np.int32
                )
                patch_triangle_columns = np.empty_like(patch_triangle_rows)
                patch_triangle_rows[0::2] = np.column_stack(
                    (patch_rows, patch_rows + 1, patch_rows + 1)
                )
                patch_triangle_columns[0::2] = np.column_stack(
                    (patch_columns, patch_columns, patch_columns + 1)
                )
                patch_triangle_rows[1::2] = np.column_stack(
                    (patch_rows, patch_rows + 1, patch_rows)
                )
                patch_triangle_columns[1::2] = np.column_stack(
                    (patch_columns, patch_columns + 1, patch_columns + 1)
                )
                patch_xy = np.stack(
                    (
                        patch_sx[patch_triangle_rows, patch_triangle_columns],
                        patch_sy[patch_triangle_rows, patch_triangle_columns],
                    ),
                    axis=2,
                )
                patch_depth = patch_distance[
                    patch_triangle_rows, patch_triangle_columns
                ]
                xy = np.concatenate((xy, patch_xy), axis=0)
                depth = np.concatenate((depth, patch_depth), axis=0)
                triangle_rows = np.concatenate(
                    (triangle_rows, patch_triangle_rows), axis=0
                )
                triangle_columns = np.concatenate(
                    (triangle_columns, patch_triangle_columns), axis=0
                )
                vertex_domain = np.concatenate(
                    (
                        vertex_domain,
                        np.ones(patch_triangle_rows.shape, dtype=np.uint8),
                    ),
                    axis=0,
                )

        twice_area = (
            (xy[:, 1, 0] - xy[:, 0, 0]) * (xy[:, 2, 1] - xy[:, 0, 1])
            - (xy[:, 1, 1] - xy[:, 0, 1]) * (xy[:, 2, 0] - xy[:, 0, 0])
        )
        edge_01 = np.hypot(
            xy[:, 1, 0] - xy[:, 0, 0], xy[:, 1, 1] - xy[:, 0, 1]
        )
        edge_12 = np.hypot(
            xy[:, 2, 0] - xy[:, 1, 0], xy[:, 2, 1] - xy[:, 1, 1]
        )
        edge_20 = np.hypot(
            xy[:, 0, 0] - xy[:, 2, 0], xy[:, 0, 1] - xy[:, 2, 1]
        )
        maximum_valid_edge = math.hypot(float(width), float(height)) * 2.0
        on_screen = (
            (np.max(xy[:, :, 0], axis=1) >= 0.0)
            & (np.min(xy[:, :, 0], axis=1) < float(width))
            & (np.max(xy[:, :, 1], axis=1) >= 0.0)
            & (np.min(xy[:, :, 1], axis=1) < float(height))
        )
        keep = (
            np.isfinite(twice_area)
            & (np.abs(twice_area) > 1e-9)
            & on_screen
            & (edge_01 <= maximum_valid_edge)
            & (edge_12 <= maximum_valid_edge)
            & (edge_20 <= maximum_valid_edge)
        )
        xy = xy[keep]
        depth = depth[keep]
        triangle_rows = triangle_rows[keep]
        triangle_columns = triangle_columns[keep]
        vertex_domain = vertex_domain[keep]
        elapsed = float(time.perf_counter() - started)
        metrics = _TerrainGeometryMetrics(
            spans=int(xy.shape[0]),
            source_samples=int(altitudes.size + patch_vertex_count),
            invalid_samples=int(altitudes.size - np.count_nonzero(vertex_valid)),
            output_vertices=int(xy.shape[0] * 3),
            max_error_px=0.0,
            elapsed_s=elapsed,
        )
        result = _TerrainTriangleGeometry(
            xy,
            depth,
            triangle_rows,
            triangle_columns,
            vertex_domain,
            metrics,
        )
        if PERFORMANCE_FLAGS.relief_cached:
            self._terrain_geometry_cache_key = cache_key
            self._terrain_geometry_cache = result
        return result

    def _draw_terrain_interpolated(
        self,
        painter,
        mesh,
        projection_fn,
        width,
        height,
        cur_az,
        az_min,
        az_max,
        t_night,
        sky_color,
        *,
        sun_alt=None,
        sun_az=None,
        projection_fn_numpy=None,
        interaction_active=False,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> bool:
        """Render the relief with shared per-vertex colours and a z-buffer."""

        frame_started = time.perf_counter()
        asset = self._terrain_render_asset
        surface_cache = getattr(getattr(self, "profile", None), "surface_samples", None)
        visual_altitudes = _sample_cache_value(surface_cache, "visual_altitudes")
        mesh_token = (
            hash((id(mesh), id(visual_altitudes)))
            if visual_altitudes is not None
            else id(mesh)
        )
        if asset is None or asset.mesh_id != mesh_token:
            asset = self._prepare_terrain_render_asset(mesh)
            if PERFORMANCE_FLAGS.relief_cached:
                self._terrain_render_asset = asset
        if asset is None:
            return False

        light_context = self._resolve_light_context(
            light_context,
            sun_alt=sun_alt,
            sun_az=sun_az,
        )
        light_alt, light_az, light_vector = self._configured_light(
            sun_alt, sun_az, light_context
        )
        light_factors = terrain_celestial_light_factors(
            light_context, self.render_settings
        )
        lighting_enabled = bool(
            self.render_settings.terrain_lighting_enabled
            and light_vector is not None
        )
        shade_key = self._terrain_lighting_key(
            asset,
            light_context,
            lighting_enabled=lighting_enabled,
        )
        lighting_grid = (
            self._terrain_shade_cache.get(shade_key)
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        polar_lighting_hit = isinstance(
            lighting_grid, TerrainLightingGrid
        )
        if not isinstance(lighting_grid, TerrainLightingGrid):
            self._terrain_lighting_builds += 1
            lighting_grid = self._terrain_light_components(
                asset.normal_x,
                asset.normal_y,
                asset.normal_z,
                asset.distances[:, None],
                light_vector,
                light_alt,
                terrain_shading_enabled=lighting_enabled,
                sun_visibility=None,
                light_context=light_context,
            )
            minimum_light, maximum_light = self._terrain_light_bounds(
                lighting_grid.factors
            )
            lighting_grid = TerrainLightingGrid(
                self._smooth_light_grid(
                    lighting_grid.intensity,
                    asset.valid,
                    min_value=minimum_light,
                    max_value=maximum_light,
                ),
                self._smooth_light_grid(
                    lighting_grid.solar_exposure,
                    asset.valid,
                    min_value=0.0,
                    max_value=1.0,
                ),
                self._smooth_light_grid(
                    lighting_grid.lunar_exposure,
                    asset.valid,
                    min_value=0.0,
                    max_value=1.0,
                ),
                lighting_grid.factors,
            )
            if PERFORMANCE_FLAGS.relief_cached:
                light_bytes = (
                    lighting_grid.intensity.nbytes
                    + lighting_grid.solar_exposure.nbytes
                    + lighting_grid.lunar_exposure.nbytes
                )
                self._terrain_shade_cache.put(
                    shade_key, lighting_grid, int(light_bytes)
                )

        patch_east, patch_north = np.meshgrid(
            np.asarray(asset.near_patch_eastings, dtype=np.float32),
            np.asarray(asset.near_patch_northings, dtype=np.float32),
        )
        patch_distances = np.hypot(patch_east, patch_north).astype(np.float32)
        patch_shade_key = shade_key + ("near-patch",)
        patch_lighting = (
            self._terrain_shade_cache.get(patch_shade_key)
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        patch_lighting_hit = isinstance(
            patch_lighting, TerrainLightingGrid
        )
        if not isinstance(patch_lighting, TerrainLightingGrid) and patch_distances.size == 0:
            empty = np.empty(patch_distances.shape, dtype=np.float32)
            patch_lighting = TerrainLightingGrid(
                empty, empty.copy(), empty.copy(), light_factors
            )
            patch_lighting_hit = True
        elif not isinstance(patch_lighting, TerrainLightingGrid):
            patch_lighting = self._terrain_light_components(
                asset.near_patch_normal_x,
                asset.near_patch_normal_y,
                asset.near_patch_normal_z,
                patch_distances,
                light_vector,
                light_alt,
                terrain_shading_enabled=lighting_enabled,
                sun_visibility=None,
                light_context=light_context,
            )
            minimum_light, maximum_light = self._terrain_light_bounds(
                patch_lighting.factors
            )
            patch_lighting = TerrainLightingGrid(
                self._smooth_light_grid(
                    patch_lighting.intensity,
                    asset.near_patch_valid,
                    min_value=minimum_light,
                    max_value=maximum_light,
                ),
                self._smooth_light_grid(
                    patch_lighting.solar_exposure,
                    asset.near_patch_valid,
                    min_value=0.0,
                    max_value=1.0,
                ),
                self._smooth_light_grid(
                    patch_lighting.lunar_exposure,
                    asset.near_patch_valid,
                    min_value=0.0,
                    max_value=1.0,
                ),
                patch_lighting.factors,
            )
            if PERFORMANCE_FLAGS.relief_cached:
                patch_light_bytes = (
                    patch_lighting.intensity.nbytes
                    + patch_lighting.solar_exposure.nbytes
                    + patch_lighting.lunar_exposure.nbytes
                )
                self._terrain_shade_cache.put(
                    patch_shade_key, patch_lighting, int(patch_light_bytes)
                )
        self._last_lighting_cache_hit = bool(
            polar_lighting_hit and patch_lighting_hit
        )

        geometry = self._terrain_triangles_for_view(
            asset,
            projection_fn,
            width,
            height,
            cur_az,
            az_min,
            az_max,
            projection_fn_numpy=projection_fn_numpy,
        )
        if geometry is None or geometry.xy.size == 0:
            return False

        color_started = time.perf_counter()
        base_material = self._build_terrain_base_material(
            asset, surface_cache
        )
        vertex_materials = base_material.polar
        patch_materials = base_material.near_patch
        self._last_terrain_color_s = time.perf_counter() - color_started
        frame_key = (
            "terrain-frame-v1",
            base_material.key,
            shade_key,
            round(float(t_night) * 256.0) / 256.0,
            (
                int(sky_color.rgba())
                if isinstance(sky_color, QColor)
                else None
            ),
            repr(self.render_settings),
        )
        self._paint_terrain_triangles(
            painter,
            asset,
            geometry,
            vertex_materials,
            patch_materials,
            lighting_grid.intensity,
            patch_lighting.intensity,
            width,
            height,
            base_material.key,
            frame_key,
            interaction_active=interaction_active,
            vertex_solar=lighting_grid.solar_exposure,
            patch_vertex_solar=patch_lighting.solar_exposure,
            vertex_lunar=lighting_grid.lunar_exposure,
            patch_vertex_lunar=patch_lighting.lunar_exposure,
            light_factors=lighting_grid.factors,
            sky_color=sky_color,
        )
        self._last_terrain_total_s = time.perf_counter() - frame_started
        if self.render_settings.terrain_performance_logging_enabled:
            append_perf_event(
                "terrain.render",
                geometry_s=round(float(geometry.metrics.elapsed_s), 6),
                colors_s=round(float(self._last_terrain_color_s), 6),
                material_resolution_s=round(
                    float(self._last_material_resolution_s), 6
                ),
                rasterization_s=round(float(self._last_terrain_raster_s), 6),
                horizon_antialias_s=round(
                    float(self._last_horizon_antialias_s), 6
                ),
                total_s=round(float(self._last_terrain_total_s), 6),
                rays=int(asset.azimuths.size),
                samples=int(asset.elevations.size),
                vertices=int(asset.elevations.size),
                triangles=int(geometry.xy.shape[0]),
                width=int(width),
                height=int(height),
                shading_mode=self.render_settings.terrain_shading_mode,
                base_material_cache_hit=bool(
                    self._last_base_material_cache_hit
                ),
                lighting_cache_hit=bool(
                    self._last_lighting_cache_hit
                ),
                resolved_material_cache_hit=bool(
                    self._last_resolved_material_cache_hit
                ),
                base_material_builds=int(
                    self._terrain_base_material_builds
                ),
                lighting_builds=int(self._terrain_lighting_builds),
                resolved_material_builds=int(
                    self._terrain_resolved_material_builds
                ),
                raster_builds=int(self._terrain_raster_builds),
                raster_cache_hit=bool(self._last_raster_cache_hit),
                frame_cache_hit=bool(self._last_frame_cache_hit),
                base_material_cache_bytes=int(
                    self._terrain_base_material_cache.resident_bytes
                ),
                lighting_cache_bytes=int(
                    self._terrain_shade_cache.resident_bytes
                ),
                resolved_material_cache_bytes=int(
                    self._terrain_resolved_material_cache.resident_bytes
                ),
            )
        return True

    def _resolve_screen_material(
        self,
        asset,
        geometry,
        triangle_id,
        bary_u,
        bary_v,
        covered,
        vertex_materials,
        patch_materials,
        *,
        raster_key: tuple,
        material_key: tuple,
        render_scale: float,
    ) -> TerrainResolvedMaterialCache:
        """Resolve projected classes/colours once for a stable camera raster."""

        key = (
            "terrain-resolved-material-v1",
            raster_key,
            material_key,
            str(self.render_settings.terrain_shading_mode),
            round(
                float(
                    self.render_settings
                    .categorical_region_smoothing_radius_px
                )
                * float(render_scale),
                4,
            ),
        )
        cached = (
            self._terrain_resolved_material_cache.get(key)
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        if isinstance(cached, TerrainResolvedMaterialCache):
            self._last_resolved_material_cache_hit = True
            self._last_material_resolution_s = 0.0
            return cached

        self._last_resolved_material_cache_hit = False
        material_started = time.perf_counter()
        vertex_rows = np.asarray(geometry.vertex_rows, dtype=np.int32)
        vertex_domain = np.asarray(geometry.vertex_domain, dtype=np.uint8)
        vertex_columns = np.asarray(
            geometry.vertex_columns, dtype=np.int32
        )
        polar_vertices = vertex_domain == 0
        polar_rows = np.where(polar_vertices, vertex_rows, 0)
        polar_columns = np.where(polar_vertices, vertex_columns, 0)
        base_rgba = np.asarray(
            vertex_materials.base_rgba[polar_rows, polar_columns],
            dtype=np.uint8,
        )
        material_valid = np.asarray(
            vertex_materials.valid[polar_rows, polar_columns], dtype=bool
        )
        class_ids = np.asarray(
            vertex_materials.class_ids[polar_rows, polar_columns],
            dtype=np.int64,
        )
        categorical = np.asarray(
            vertex_materials.categorical[polar_rows, polar_columns],
            dtype=bool,
        )
        source_indices = np.asarray(
            vertex_materials.source_indices[polar_rows, polar_columns],
            dtype=np.int16,
        )
        patch_vertices = vertex_domain == 1
        if np.any(patch_vertices):
            base_rgba = base_rgba.copy()
            material_valid = material_valid.copy()
            class_ids = class_ids.copy()
            categorical = categorical.copy()
            source_indices = source_indices.copy()
            patch_rows = vertex_rows[patch_vertices]
            patch_columns = vertex_columns[patch_vertices]
            base_rgba[patch_vertices] = np.asarray(
                patch_materials.base_rgba[patch_rows, patch_columns],
                dtype=np.uint8,
            )
            material_valid[patch_vertices] = np.asarray(
                patch_materials.valid[patch_rows, patch_columns], dtype=bool
            )
            class_ids[patch_vertices] = np.asarray(
                patch_materials.class_ids[patch_rows, patch_columns],
                dtype=np.int64,
            )
            categorical[patch_vertices] = np.asarray(
                patch_materials.categorical[patch_rows, patch_columns],
                dtype=bool,
            )
            source_indices[patch_vertices] = np.asarray(
                patch_materials.source_indices[patch_rows, patch_columns],
                dtype=np.int16,
            )

        triangle_materials = TerrainMaterialSamples(
            base_rgba,
            material_valid,
            class_ids,
            categorical,
            source_indices,
        )
        triangle_surface_xy = np.zeros(
            vertex_rows.shape + (2,), dtype=np.float64
        )
        polar_distance = np.asarray(
            asset.distances[polar_rows], dtype=np.float64
        )
        polar_azimuth = np.radians(
            np.asarray(
                asset.azimuths[polar_columns], dtype=np.float64
            )
        )
        triangle_surface_xy[..., 0] = (
            polar_distance * np.sin(polar_azimuth)
        )
        triangle_surface_xy[..., 1] = (
            polar_distance * np.cos(polar_azimuth)
        )
        if np.any(patch_vertices):
            patch_rows = vertex_rows[patch_vertices]
            patch_columns = vertex_columns[patch_vertices]
            triangle_surface_xy[..., 0][patch_vertices] = np.asarray(
                asset.near_patch_eastings[patch_columns],
                dtype=np.float64,
            )
            triangle_surface_xy[..., 1][patch_vertices] = np.asarray(
                asset.near_patch_northings[patch_rows],
                dtype=np.float64,
            )

        resolved = _resolve_surface_material(
            triangle_id,
            bary_u,
            bary_v,
            triangle_materials,
            triangle_surface_xy,
            vertex_domain,
            vertex_materials,
            asset.distances,
            asset.azimuths,
            patch_materials,
            asset.near_patch_eastings,
            asset.near_patch_northings,
            flat_continuous=(
                self.render_settings.terrain_shading_mode == "flat"
            ),
        )
        surface_cache = getattr(
            getattr(self, "profile", None), "surface_samples", None
        )
        protected = np.zeros(np.asarray(covered).shape, dtype=bool)
        if (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            )
            == SurfaceVisualStyle.VIBRANT.value
        ):
            protected = _protected_categorical_regions(
                resolved, surface_cache
            )
            resolved = _regularize_categorical_regions(
                resolved,
                covered,
                radius_px=(
                    self.render_settings
                    .categorical_region_smoothing_radius_px
                    * render_scale
                ),
                protected=protected,
            )

        triangle_surface_xy.setflags(write=False)
        protected.setflags(write=False)
        entry = TerrainResolvedMaterialCache(
            key,
            _freeze_material_samples(resolved),
            _freeze_material_samples(triangle_materials),
            triangle_surface_xy,
            protected,
        )
        self._terrain_resolved_material_builds += 1
        self._last_material_resolution_s = (
            time.perf_counter() - material_started
        )
        if PERFORMANCE_FLAGS.relief_cached:
            self._terrain_resolved_material_cache.put(
                key, entry, entry.resident_bytes
            )
        return entry

    def _paint_terrain_triangles(
        self,
        painter,
        asset,
        geometry,
        vertex_materials,
        patch_materials,
        vertex_light,
        patch_vertex_light,
        width,
        height,
        material_key,
        frame_key,
        interaction_active=False,
        vertex_solar=None,
        patch_vertex_solar=None,
        vertex_lunar=None,
        patch_vertex_lunar=None,
        light_factors: TerrainCelestialLightFactors | None = None,
        sky_color: QColor | None = None,
    ):
        """Resolve exact screen visibility and compose the cached RGBA surface."""

        self._last_surface2d_quads = int(geometry.metrics.spans)
        self._last_surface2d_vertices = int(geometry.metrics.output_vertices)
        self._last_surface2d_max_error_px = 0.0
        self._last_surface2d_geometry_s = float(geometry.metrics.elapsed_s)
        paint_started = time.perf_counter()
        render_scale = 0.5 if interaction_active else 1.0
        render_width = max(1, int(math.ceil(float(width) * render_scale)))
        render_height = max(1, int(math.ceil(float(height) * render_scale)))
        supersample = 1
        cache_key = (
            "terrain-frame-image-v1",
            int(width),
            int(height),
            bool(interaction_active),
            frame_key,
        )
        if (
            PERFORMANCE_FLAGS.relief_cached
            and self._terrain_surface_image_geometry is geometry
            and self._terrain_surface_image_cache_key == cache_key
            and self._terrain_surface_image_cache is not None
        ):
            self._last_frame_cache_hit = True
            self._terrain_frame_cache_hits += 1
            painter.drawImage(0, 0, self._terrain_surface_image_cache)
            self._last_surface2d_paint_s = time.perf_counter() - paint_started
            self._last_terrain_raster_s = 0.0
            self._last_horizon_antialias_s = 0.0
            return

        self._last_frame_cache_hit = False
        self._terrain_frame_cache_misses += 1
        raster_started = time.perf_counter()
        raster_key = (
            id(geometry),
            render_width,
            render_height,
            supersample,
        )
        scaled_xy = np.asarray(geometry.xy, dtype=np.float64).copy()
        scaled_xy[:, :, 0] *= float(render_width) / float(width)
        scaled_xy[:, :, 1] *= float(render_height) / float(height)
        if (
            PERFORMANCE_FLAGS.relief_cached
            and raster_key == self._terrain_raster_cache_key
            and self._terrain_raster_cache is not None
        ):
            self._last_raster_cache_hit = True
            triangle_id, bary_u, bary_v = self._terrain_raster_cache
        else:
            self._last_raster_cache_hit = False
            self._terrain_raster_builds += 1
            _depth, triangle_id, bary_u, bary_v = _rasterize_terrain_triangles(
                scaled_xy,
                geometry.depth,
                render_width,
                render_height,
                supersample=supersample,
            )
            if PERFORMANCE_FLAGS.relief_cached:
                self._terrain_raster_cache_key = raster_key
                self._terrain_raster_cache = (triangle_id, bary_u, bary_v)
        covered = triangle_id >= 0
        rgba_high = np.zeros(
            (
                int(render_height) * supersample,
                int(render_width) * supersample,
                4,
            ),
            dtype=np.uint8,
        )
        resolved_material = None
        vibrant_enabled = (
            normalize_surface_visual_style(
                self.render_settings.surface_visual_style
            )
            == SurfaceVisualStyle.VIBRANT.value
        )
        detailed_vibrant = vibrant_enabled and not bool(interaction_active)
        if np.any(covered):
            vertex_rows = np.asarray(geometry.vertex_rows, dtype=np.int32)
            vertex_domain = np.asarray(geometry.vertex_domain, dtype=np.uint8)
            polar_vertices = vertex_domain == 0
            polar_rows = np.where(polar_vertices, vertex_rows, 0)
            polar_columns = np.where(
                polar_vertices, geometry.vertex_columns, 0
            )
            resolved_entry = self._resolve_screen_material(
                asset,
                geometry,
                triangle_id,
                bary_u,
                bary_v,
                covered,
                vertex_materials,
                patch_materials,
                raster_key=raster_key,
                material_key=material_key,
                render_scale=render_scale,
            )
            resolved_material = resolved_entry.materials
            triangle_materials = resolved_entry.triangle_materials
            triangle_surface_xy = resolved_entry.triangle_surface_xy
            protected_categories = resolved_entry.protected
            material_valid = np.asarray(
                triangle_materials.valid, dtype=bool
            )
            categorical = np.asarray(
                triangle_materials.categorical, dtype=bool
            )
            class_ids = np.asarray(
                triangle_materials.class_ids, dtype=np.int64
            )
            source_indices = np.asarray(
                triangle_materials.source_indices, dtype=np.int16
            )
            light_values = np.asarray(
                vertex_light[polar_rows, polar_columns], dtype=np.float64
            )
            solar_values = np.asarray(
                (
                    vertex_solar[polar_rows, polar_columns]
                    if vertex_solar is not None
                    else np.zeros(polar_rows.shape, dtype=np.float32)
                ),
                dtype=np.float64,
            )
            lunar_values = np.asarray(
                (
                    vertex_lunar[polar_rows, polar_columns]
                    if vertex_lunar is not None
                    else np.zeros(polar_rows.shape, dtype=np.float32)
                ),
                dtype=np.float64,
            )
            elevation_values = None
            normal_x_values = None
            normal_y_values = None
            normal_z_values = None
            if detailed_vibrant:
                elevation_values = np.asarray(
                    asset.elevations[polar_rows, polar_columns],
                    dtype=np.float64,
                )
                normal_x_values = np.asarray(
                    asset.normal_x[polar_rows, polar_columns],
                    dtype=np.float64,
                )
                normal_y_values = np.asarray(
                    asset.normal_y[polar_rows, polar_columns],
                    dtype=np.float64,
                )
                normal_z_values = np.asarray(
                    asset.normal_z[polar_rows, polar_columns],
                    dtype=np.float64,
            )
            patch_vertices = vertex_domain == 1
            if np.any(patch_vertices):
                light_values = light_values.copy()
                solar_values = solar_values.copy()
                lunar_values = lunar_values.copy()
                if detailed_vibrant:
                    elevation_values = elevation_values.copy()
                    normal_x_values = normal_x_values.copy()
                    normal_y_values = normal_y_values.copy()
                    normal_z_values = normal_z_values.copy()
                patch_rows = vertex_rows[patch_vertices]
                patch_columns = geometry.vertex_columns[patch_vertices]
                light_values[patch_vertices] = np.asarray(
                    patch_vertex_light[
                        vertex_rows[patch_vertices],
                        geometry.vertex_columns[patch_vertices],
                    ],
                    dtype=np.float64,
                )
                if patch_vertex_solar is not None:
                    solar_values[patch_vertices] = np.asarray(
                        patch_vertex_solar[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
                if patch_vertex_lunar is not None:
                    lunar_values[patch_vertices] = np.asarray(
                        patch_vertex_lunar[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
                if detailed_vibrant:
                    elevation_values[patch_vertices] = np.asarray(
                        asset.near_patch_elevations[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
                    normal_x_values[patch_vertices] = np.asarray(
                        asset.near_patch_normal_x[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
                    normal_y_values[patch_vertices] = np.asarray(
                        asset.near_patch_normal_y[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
                    normal_z_values[patch_vertices] = np.asarray(
                        asset.near_patch_normal_z[
                            vertex_rows[patch_vertices],
                            geometry.vertex_columns[patch_vertices],
                        ],
                        dtype=np.float64,
                    )
            surface_cache = getattr(
                getattr(self, "profile", None), "surface_samples", None
            )
            continuous_channels = [
                light_values[..., None],
                np.asarray(geometry.depth, dtype=np.float64)[..., None],
                triangle_surface_xy,
                solar_values[..., None],
                lunar_values[..., None],
            ]
            if detailed_vibrant:
                continuous_channels.extend(
                    (
                        elevation_values[..., None],
                        normal_x_values[..., None],
                        normal_y_values[..., None],
                        normal_z_values[..., None],
                    )
                )
            continuous_values = np.concatenate(
                tuple(continuous_channels), axis=2
            )
            interpolated_continuous, _ = (
                _interpolate_triangle_continuous_values(
                triangle_id,
                bary_u,
                bary_v,
                continuous_values,
                flat=self.render_settings.terrain_shading_mode == "flat",
                )
            )
            maximum_distance_m = (
                self._maximum_terrain_distance_m()
                or float(np.nanmax(geometry.depth))
            )
            material_pixels = covered & resolved_material.valid
            material_rgba = resolved_material.base_rgba
            if vibrant_enabled:
                detail_scale = 1.0 if detailed_vibrant else 0.0
                material_rgba = _apply_categorical_solar_response(
                    material_rgba,
                    resolved_material,
                    interpolated_continuous[..., 4],
                    material_pixels,
                    strength=self.render_settings.vibrant_intensity,
                    midscale_variation=(
                        self.render_settings.vibrant_material_midscale_variation
                        * detail_scale
                    ),
                    microscale_variation=(
                        self.render_settings
                        .vibrant_material_microscale_variation
                        * detail_scale
                    ),
                    slope_influence=(
                        self.render_settings.vibrant_material_slope_influence
                        * detail_scale
                    ),
                    snow_rock_blend=(
                        self.render_settings.vibrant_snow_rock_blend
                        * detail_scale
                    ),
                    water_shore_variation=(
                        self.render_settings.vibrant_water_shore_variation
                        * detail_scale
                    ),
                    normal_x=(
                        interpolated_continuous[..., 7]
                        if detailed_vibrant
                        else None
                    ),
                    normal_y=(
                        interpolated_continuous[..., 8]
                        if detailed_vibrant
                        else None
                    ),
                    normal_z=(
                        interpolated_continuous[..., 9]
                        if detailed_vibrant
                        else None
                    ),
                    source_legend_ids=tuple(
                        _sample_cache_value(
                            surface_cache, "source_legend_ids", ()
                        )
                        or ()
                    ),
                )
            composed = compose_vertex_rgba(
                material_rgba,
                interpolated_continuous[..., 0],
                interpolated_continuous[..., 1],
                self.render_settings,
                maximum_distance_m=maximum_distance_m,
                horizon_rgb=(
                    (
                        sky_color.red(),
                        sky_color.green(),
                        sky_color.blue(),
                    )
                    if isinstance(sky_color, QColor)
                    else None
                ),
                atmosphere_strength=0.0 if vibrant_enabled else 1.0,
            )
            if vibrant_enabled:
                valley_haze = None
                if detailed_vibrant:
                    relief_occlusion = _vibrant_relief_occlusion(
                        interpolated_continuous[..., 6],
                        interpolated_continuous[..., 9],
                        material_pixels,
                        radius_px=(
                            self.render_settings
                            .vibrant_ambient_occlusion_radius_px
                            * render_scale
                        ),
                        relief_scale_m=(
                            self.render_settings
                            .vibrant_ambient_occlusion_relief_scale_m
                        ),
                    )
                    composed = _apply_vibrant_ambient_occlusion(
                        composed,
                        relief_occlusion,
                        material_pixels,
                        strength=(
                            self.render_settings
                            .vibrant_ambient_occlusion_strength
                        ),
                    )
                    valley_haze = _vibrant_valley_haze(
                        relief_occlusion,
                        interpolated_continuous[..., 1],
                        material_pixels,
                        maximum_distance_m=maximum_distance_m,
                        strength=(
                            self.render_settings.vibrant_valley_haze_strength
                        ),
                    )
                composed = apply_vibrant_color_grade(
                    composed,
                    interpolated_continuous[..., 0],
                    interpolated_continuous[..., 1],
                    self.render_settings,
                    maximum_distance_m=maximum_distance_m,
                    valid_mask=material_pixels,
                    additional_haze=valley_haze,
                    daylight_factor=(
                        light_factors.solar_ambient
                        if light_factors is not None
                        else 1.0
                    ),
                    moonlight_factor=(
                        light_factors.lunar_strength
                        if light_factors is not None
                        else 0.0
                    ),
                    solar_exposure=interpolated_continuous[..., 4],
                    lunar_exposure=interpolated_continuous[..., 5],
                    atmosphere_rgb=(
                        (
                            sky_color.red(),
                            sky_color.green(),
                            sky_color.blue(),
                        )
                        if isinstance(sky_color, QColor)
                        else None
                    ),
                )
            rgba_high[material_pixels] = composed[material_pixels]
            if vibrant_enabled:
                rgba_high = _soften_categorical_edges(
                    rgba_high,
                    resolved_material,
                    covered,
                    strength=(
                        self.render_settings.categorical_edge_smoothing_strength
                    ),
                    protected=protected_categories,
                )
                rgba_high = _apply_vibrant_bloom(
                    rgba_high,
                    material_pixels,
                    self.render_settings,
                    render_scale=render_scale,
                    light_intensity=interpolated_continuous[..., 0],
                    distance_m=interpolated_continuous[..., 1],
                    maximum_distance_m=maximum_distance_m,
                    daylight_factor=(
                        light_factors.solar_ambient
                        if light_factors is not None
                        else 1.0
                    ),
                    moonlight_factor=(
                        light_factors.lunar_strength
                        if light_factors is not None
                        else 0.0
                    ),
                )
        self._last_terrain_raster_s = time.perf_counter() - raster_started

        settings = self.render_settings
        antialias_started = time.perf_counter()
        if (
            settings.horizon_antialiasing_enabled
            and settings.horizon_antialiasing_mode != "off"
            and supersample == 1
        ):
            coverage_samples = (
                settings.horizon_supersampling_factor
                if settings.horizon_antialiasing_mode == "supersample"
                else 1
            )
            rgba_high = _apply_horizon_coverage(
                rgba_high,
                triangle_id,
                scaled_xy,
                filter_width_px=settings.horizon_filter_width_px,
                supersampling_factor=coverage_samples,
            )
        self._last_horizon_antialias_s = (
            time.perf_counter() - antialias_started
        )

        if supersample > 1:
            # Average premultiplied sub-samples, then return to straight RGBA.
            premultiplied = rgba_high.astype(np.float32)
            alpha = premultiplied[:, :, 3:4] / 255.0
            premultiplied[:, :, :3] *= alpha
            reduced = premultiplied.reshape(
                int(render_height),
                supersample,
                int(render_width),
                supersample,
                4,
            ).mean(axis=(1, 3))
            reduced_alpha = reduced[:, :, 3:4] / 255.0
            reduced[:, :, :3] = np.divide(
                reduced[:, :, :3],
                np.maximum(reduced_alpha, 1e-12),
                out=np.zeros_like(reduced[:, :, :3]),
                where=reduced_alpha > 0.0,
            )
            rgba = np.clip(np.rint(reduced), 0, 255).astype(np.uint8)
        else:
            rgba = rgba_high
        image = QImage(
            rgba.data,
            int(render_width),
            int(render_height),
            int(rgba.strides[0]),
            QImage.Format_RGBA8888,
        ).copy()
        if render_width != int(width) or render_height != int(height):
            image = image.scaled(
                int(width),
                int(height),
                Qt.IgnoreAspectRatio,
                Qt.SmoothTransformation,
            )
        visible_material_categories = (
            resolved_material is not None
            and np.any(
                np.asarray(resolved_material.valid, dtype=bool)
                & np.asarray(resolved_material.categorical, dtype=bool)
            )
        )
        self._terrain_resolved_materials = (
            resolved_material if visible_material_categories else None
        )
        if (
            self.render_settings.terrain_surface_diagnostics_enabled
            and resolved_material is not None
        ):
            material_rgba = np.zeros_like(rgba_high)
            diagnostic_pixels = covered & resolved_material.valid
            material_rgba[diagnostic_pixels] = resolved_material.base_rgba[
                diagnostic_pixels
            ]
            material_image = QImage(
                material_rgba.data,
                int(render_width),
                int(render_height),
                int(material_rgba.strides[0]),
                QImage.Format_RGBA8888,
            ).copy()
            if render_width != int(width) or render_height != int(height):
                material_image = material_image.scaled(
                    int(width),
                    int(height),
                    Qt.IgnoreAspectRatio,
                    Qt.SmoothTransformation,
                )
            visible_categories = (
                diagnostic_pixels & resolved_material.categorical
            )
            histogram = {}
            if np.any(visible_categories):
                pairs = np.column_stack(
                    (
                        resolved_material.source_indices[visible_categories],
                        resolved_material.class_ids[visible_categories],
                    )
                )
                unique_pairs, counts = np.unique(
                    pairs, axis=0, return_counts=True
                )
                histogram = {
                    f"{int(source)}:{int(class_id)}": int(count)
                    for (source, class_id), count in zip(unique_pairs, counts)
                }
            categorical_vertices = categorical & material_valid
            mixed_triangles = int(
                np.count_nonzero(
                    np.any(categorical_vertices, axis=1)
                    & np.any(~categorical_vertices, axis=1)
                )
            )
            categorical_counts = {1: 0, 2: 0, 3: 0}
            for triangle_index in np.flatnonzero(
                np.any(categorical_vertices, axis=1)
            ):
                mask = categorical_vertices[triangle_index]
                identities = np.column_stack(
                    (
                        source_indices[triangle_index, mask],
                        class_ids[triangle_index, mask],
                    )
                )
                count = int(len(np.unique(identities, axis=0)))
                categorical_counts[min(3, max(1, count))] += 1
            self._terrain_material_image = material_image
            self._terrain_surface_diagnostics = {
                "class_histogram": histogram,
                "categorical_triangles": {
                    str(key): int(value)
                    for key, value in categorical_counts.items()
                },
                "mixed_triangles": mixed_triangles,
                "material_resolution_s": float(
                    self._last_material_resolution_s
                ),
                "rasterization_s": float(self._last_terrain_raster_s),
            }
        elif not self.render_settings.terrain_surface_diagnostics_enabled:
            self._terrain_material_image = None
            self._terrain_surface_diagnostics = {}
        if PERFORMANCE_FLAGS.relief_cached:
            self._terrain_surface_image_cache_key = cache_key
            self._terrain_surface_image_cache = image
            self._terrain_surface_image_geometry = geometry
        painter.drawImage(0, 0, image)
        self._last_surface2d_paint_s = float(time.perf_counter() - paint_started)

    def _terrain_polygons_for_geometry(self, geometry):
        if (
            self._terrain_polygon_cache_geometry is geometry
            and self._terrain_polygon_cache is not None
        ):
            return self._terrain_polygon_cache
        polygons = []
        for span in geometry.spans:
            points = [
                QPointF(float(x), float(y))
                for x, y in zip(span.x, span.top_y)
            ]
            points.extend(
                QPointF(float(x), float(y))
                for x, y in zip(span.bottom_x[::-1], span.bottom_y[::-1])
            )
            polygons.append((span, QPolygonF(points)))
        result = tuple(polygons)
        self._terrain_polygon_cache_key = id(geometry)
        self._terrain_polygon_cache_geometry = geometry
        self._terrain_polygon_cache = result
        return result

    @staticmethod
    def _projection_geometry_signature(projection_fn, az_min, az_max):
        signature = []
        for altitude in (-30.0, 0.0, 30.0):
            for azimuth in np.linspace(float(az_min), float(az_max), 5):
                try:
                    point = projection_fn(float(altitude), float(azimuth))
                    if point is None:
                        signature.append((None, None))
                    else:
                        signature.append((float(point[0]), float(point[1])))
                except Exception:
                    signature.append((None, None))
        return tuple(signature)

    def _terrain_geometry_for_view(
        self,
        asset,
        projection_fn,
        width,
        height,
        px_alt,
        cur_az,
        az_min,
        az_max,
        projection_fn_numpy=None,
    ):
        signature = self._projection_geometry_signature(
            projection_fn, az_min, az_max
        )
        cache_key = (
            int(asset.mesh_id),
            int(width),
            int(height),
            float(px_alt),
            float(cur_az),
            float(az_min),
            float(az_max),
            signature,
        )
        if (
            PERFORMANCE_FLAGS.relief_cached
            and cache_key == self._terrain_geometry_cache_key
            and self._terrain_geometry_cache is not None
        ):
            return self._terrain_geometry_cache

        started = time.perf_counter()
        azimuths = asset.azimuths
        az_diffs = np.diff(azimuths.astype(np.float64))
        az_diffs = az_diffs[np.isfinite(az_diffs) & (az_diffs > 0.0)]
        margin = float(np.median(az_diffs)) if az_diffs.size else 1.0
        base_offset = round((float(cur_az) - 180.0) / 360.0) * 360.0
        offsets = (base_offset - 360.0, base_offset, base_offset + 360.0)
        selected_azimuth_parts = []
        unwrapped_column_parts = []
        for offset in offsets:
            final_azimuths = azimuths + float(offset)
            columns = np.flatnonzero(
                (final_azimuths >= float(az_min) - margin)
                & (final_azimuths <= float(az_max) + margin)
            ).astype(np.int32)
            if columns.size == 0:
                continue
            wrap_index = int(round(float(offset) / 360.0))
            selected_azimuth_parts.append(final_azimuths[columns])
            unwrapped_column_parts.append(
                columns.astype(np.int64) + wrap_index * int(azimuths.size)
            )

        if not selected_azimuth_parts:
            return _TerrainSurfaceGeometry((), _TerrainGeometryMetrics())
        selected_azimuths = np.concatenate(selected_azimuth_parts).astype(np.float64)
        unwrapped_columns = np.concatenate(unwrapped_column_parts).astype(np.int64)
        sort_order = np.argsort(selected_azimuths, kind="stable")
        selected_azimuths = selected_azimuths[sort_order]
        unwrapped_columns = unwrapped_columns[sort_order]
        if selected_azimuths.size > 1:
            unique = np.r_[True, np.diff(selected_azimuths) > 1e-9]
            selected_azimuths = selected_azimuths[unique]
            unwrapped_columns = unwrapped_columns[unique]
        source_columns = (unwrapped_columns % int(azimuths.size)).astype(np.int32)

        selected_altitudes = np.asarray(
            asset.altitudes[:, source_columns], dtype=np.float64
        )
        if projection_fn_numpy:
            azimuth_grid = np.broadcast_to(
                selected_azimuths[None, :], selected_altitudes.shape
            )
            projected = projection_fn_numpy(selected_altitudes, azimuth_grid)
            projected_x = np.asarray(projected[0], dtype=np.float64)
            projected_y = np.asarray(projected[1], dtype=np.float64)
            if len(projected) >= 3:
                projected_valid = np.asarray(projected[2], dtype=bool)
                projected_x = np.where(projected_valid, projected_x, np.nan)
                projected_y = np.where(projected_valid, projected_y, np.nan)
        else:
            x_values = []
            y_values = []
            for source_column, azimuth in zip(source_columns, selected_azimuths):
                sx, sy = self._project_mesh_column(
                    projection_fn,
                    None,
                    float(azimuth),
                    asset.altitudes[:, int(source_column)],
                    height,
                    px_alt,
                )
                x_values.append(sx)
                y_values.append(sy)
            projected_x = np.asarray(x_values, dtype=np.float64).T
            projected_y = np.asarray(y_values, dtype=np.float64).T
        part = _build_terrain_surface_spans(
            distances=asset.distances,
            column_indices=unwrapped_columns,
            azimuths=selected_azimuths,
            projected_x=projected_x,
            projected_y=projected_y,
            valid=asset.valid[:, source_columns],
            height=float(height),
            simplify_tolerance_px=None,
            column_modulus=int(azimuths.size),
        )

        elapsed = float(time.perf_counter() - started)
        metrics = _TerrainGeometryMetrics(
            spans=len(part.spans),
            source_samples=part.metrics.source_samples,
            invalid_samples=part.metrics.invalid_samples,
            occluded_samples=part.metrics.occluded_samples,
            simplified_vertices=part.metrics.simplified_vertices,
            output_vertices=part.metrics.output_vertices,
            max_error_px=part.metrics.max_error_px,
            elapsed_s=elapsed,
        )
        result = _TerrainSurfaceGeometry(tuple(part.spans), metrics)
        if PERFORMANCE_FLAGS.relief_cached:
            self._terrain_geometry_cache_key = cache_key
            self._terrain_geometry_cache = result
        return result

    def _draw_terrain_mesh(
        self,
        painter,
        mesh,
        projection_fn,
        width,
        height,
        px_alt,
        cur_az,
        az_min,
        az_max,
        t_night,
        sky_color,
        sun_alt,
        sun_az,
        terrain_shading_enabled=True,
        projection_fn_numpy=None,
        interaction_active=False,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        self._draw_terrain_surface_2d(
            painter,
            mesh,
            projection_fn,
            width,
            height,
            px_alt,
            cur_az,
            az_min,
            az_max,
            t_night,
            sky_color,
            sun_alt,
            sun_az,
            terrain_shading_enabled=terrain_shading_enabled,
            projection_fn_numpy=projection_fn_numpy,
            interaction_active=interaction_active,
            light_context=light_context,
        )

    def _draw_profile_horizon_cap(
        self,
        painter,
        projection_fn,
        height,
        px_alt,
        cur_az,
        az_min,
        az_max,
        t_night,
        sky_color,
        projection_fn_numpy=None,
        fill_to_bottom=True,
        draw_ridge=True,
    ):
        lookup = self._profile_horizon_lookup()
        if lookup is None:
            return
        az_raw, h_raw = lookup
        if az_raw.size < 2:
            return

        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        all_sx = []
        all_sy = []
        for offset in offsets:
            final_az = az_raw + offset
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask):
                continue

            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            if projection_fn_numpy:
                projected = projection_fn_numpy(culled_h, culled_az)
                sx, sy = projected[:2]
                if len(projected) >= 3:
                    projected_valid = np.asarray(projected[2], dtype=bool)
                    sx = np.where(projected_valid, sx, np.nan)
                    sy = np.where(projected_valid, sy, np.nan)
            else:
                sx = []
                sy = []
                for az_value, height_value in zip(culled_az, culled_h):
                    point = projection_fn(float(height_value), float(az_value))
                    if point:
                        sx.append(point[0])
                        sy.append(point[1])
                    else:
                        sx.append(np.nan)
                        sy.append(height * 2.0)
                sx = np.asarray(sx, dtype=np.float32)
                sy = np.asarray(sy, dtype=np.float32)

            finite_mask = np.isfinite(sx) & np.isfinite(sy)
            if not np.any(finite_mask):
                continue
            edge_mask = np.diff(
                np.pad(finite_mask.astype(np.int8), (1, 1), constant_values=0)
            )
            starts = np.where(edge_mask == 1)[0]
            stops = np.where(edge_mask == -1)[0]
            for start, stop in zip(starts, stops):
                seg_sx = np.asarray(sx[start:stop], dtype=np.float32)
                seg_sy = np.asarray(sy[start:stop], dtype=np.float32)
                if len(seg_sx) >= 2:
                    all_sx.append(seg_sx)
                    all_sy.append(seg_sy)

        if not all_sx:
            return

        cap_night, cap_day = _palette_color(0.56)
        cap_base = _lerp_color(cap_day, cap_night, t_night)
        cap_fill = _lerp_color(
            cap_base, _atmospheric_haze_color(sky_color, t_night), 0.16
        )
        if fill_to_bottom:
            self._fill_strip_downward_numpy(
                painter, all_sx, all_sy, cap_fill, height * 2.0, solid=True
            )

        if draw_ridge:
            ridge = _with_alpha(QColor(cap_fill).lighter(105), 58)
            shadow = _with_alpha(QColor(cap_fill).darker(110), 36)
            self._stroke_ridge_lines_numpy(
                painter, all_sx, all_sy, shadow, width=0.9, y_offset=0.9
            )
            self._stroke_ridge_lines_numpy(
                painter, all_sx, all_sy, ridge, width=0.65
            )

    def _band_edge_colors(self, fill_color, t_night, band_pts):
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        distance_factor = 1.0 - _distance_haze_factor(band_max_m)
        ridge_alpha = 9 + int(16 * distance_factor)
        shadow_alpha = 12 + int(20 * distance_factor)

        if t_night >= 0.45:
            ridge = QColor(fill_color).lighter(108)
            shadow = QColor(fill_color).darker(106)
            ridge_alpha += int(5 * t_night)
            shadow_alpha += int(5 * t_night)
        else:
            ridge = QColor(fill_color).darker(108)
            shadow = QColor(fill_color).lighter(104)

        return _with_alpha(ridge, ridge_alpha), _with_alpha(shadow, shadow_alpha)

    def _band_surface_color(self, fill_color, t_night, band_pts):
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        distance_factor = 1.0 - _distance_haze_factor(band_max_m)
        alpha = 7 + int(14 * distance_factor)
        if t_night >= 0.45:
            color = QColor(fill_color).lighter(106)
            alpha += int(5 * t_night)
        else:
            color = QColor(fill_color).darker(108)
        return _with_alpha(color, alpha)

    def _draw_band_linear(
        self,
        painter,
        band_pts,
        color,
        proj_fn,
        w,
        h,
        px_alt,
        cur_az,
        az_min,
        az_max,
        proj_fn_numpy=None,
        ridge_color=None,
        shadow_color=None,
        surface_color=None,
        sun_alt=None,
        sun_az=None,
        terrain_shading_enabled=True,
        sky_color=None,
        interaction_active=False,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        """
        Draw one filled silhouette band using the shared sky projection.
        """
        polygon_cache_key = ("band", id(band_pts))
        cacheable_fill = bool(
            not terrain_shading_enabled
            and ridge_color is None
            and shadow_color is None
            and surface_color is None
        )
        if cacheable_fill and self._draw_cached_profile_polygons(
            painter, polygon_cache_key, color
        ):
            return

        az_raw, h_raw = band_pts.points
        if az_raw is None:
            return
        valid_raw = getattr(band_pts, "valid_mask", None)
        if valid_raw is None:
            valid_raw = np.ones_like(az_raw, dtype=bool)

        # Calculate base offset to center around current azimuth
        # az_raw is 0..360, so center is 180.
        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        all_sx = []
        all_sy = []
        all_az = []
        all_h = []

        for offset in offsets:
            final_az = az_raw + offset

            # 1. CULLING: Only keep points within view
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask):
                continue

            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            culled_valid = np.asarray(valid_raw[mask], dtype=bool)
            if not np.any(culled_valid):
                continue

            point_budget = self._profile_point_budget(w, interaction_active)
            if len(culled_az) > point_budget and bool(np.all(culled_valid)):
                lod_indices = _extrema_lod_indices(culled_h, point_budget)
                culled_az = culled_az[lod_indices]
                culled_h = culled_h[lod_indices]
                culled_valid = culled_valid[lod_indices]

            # 2. VECTORIZED PROJECTION
            if proj_fn_numpy:
                projected = proj_fn_numpy(culled_h, culled_az)
                if projected is None or len(projected) < 2:
                    continue
                sx = np.asarray(projected[0], dtype=np.float64)
                sy = np.asarray(projected[1], dtype=np.float64)
                if len(projected) >= 3:
                    projected_valid = np.asarray(projected[2], dtype=bool)
                    sx = np.where(projected_valid, sx, np.nan)
                    sy = np.where(projected_valid, sy, np.nan)
            else:
                # Fallback to scalar (slow)
                sx = []
                sy = []
                for a_val, h_val in zip(culled_az, culled_h):
                    point = proj_fn(float(h_val), float(a_val))
                    if point:
                        sx.append(point[0])
                        sy.append(point[1])
                    else:
                        sx.append(np.nan)
                        sy.append(h * 2)  # Safety
                sx = np.array(sx)
                sy = np.array(sy)

            finite_mask = np.isfinite(sx) & np.isfinite(sy) & culled_valid
            if not np.any(finite_mask):
                continue
            edge_mask = np.diff(
                np.pad(finite_mask.astype(np.int8), (1, 1), constant_values=0)
            )
            starts = np.where(edge_mask == 1)[0]
            stops = np.where(edge_mask == -1)[0]
            for start, stop in zip(starts, stops):
                seg_sx = sx[start:stop]
                seg_sy = sy[start:stop]
                seg_az = culled_az[start:stop]
                seg_h = culled_h[start:stop]
                if len(seg_sx) >= 2:
                    all_sx.append(seg_sx)
                    all_sy.append(seg_sy)
                    all_az.append(seg_az)
                    all_h.append(seg_h)

        if all_sx:
            if terrain_shading_enabled:
                self._fill_shaded_strip_downward_numpy(
                    painter,
                    all_sx,
                    all_sy,
                    all_az,
                    all_h,
                    color,
                    h * 2,
                    sun_alt,
                    sun_az,
                    band_pts,
                    sky_color,
                    light_context=light_context,
                )
            elif cacheable_fill and self._profile_polygon_cache_view_key is not None:
                painter.setBrush(QBrush(color))
                painter.setPen(Qt.NoPen)
                self._cache_profile_category_polygons(
                    polygon_cache_key,
                    all_sx,
                    all_sy,
                    all_az,
                    h * 2,
                )
                for polygon in self._cache_profile_polygons(
                    polygon_cache_key, all_sx, all_sy, h * 2
                ):
                    painter.drawPolygon(polygon)
            else:
                self._fill_strip_downward_numpy(
                    painter, all_sx, all_sy, color, h * 2, solid=True
                )
            if shadow_color is not None:
                self._stroke_ridge_lines_numpy(
                    painter,
                    all_sx,
                    all_sy,
                    shadow_color,
                    width=1.2,
                    y_offset=1.15,
                )
            if ridge_color is not None:
                self._stroke_ridge_lines_numpy(
                    painter, all_sx, all_sy, ridge_color, width=0.8
                )
                self._draw_edge_texture_numpy(
                    painter, all_sx, all_sy, all_az, ridge_color, band_pts
                )
            if surface_color is not None:
                self._draw_band_surface_linear(
                    painter,
                    band_pts,
                    proj_fn,
                    h,
                    px_alt,
                    cur_az,
                    az_min,
                    az_max,
                    proj_fn_numpy,
                    surface_color,
                )

    def _draw_band_surface_linear(
        self,
        painter,
        band_pts,
        proj_fn,
        h,
        px_alt,
        cur_az,
        az_min,
        az_max,
        proj_fn_numpy,
        color,
    ):
        az_raw, h_raw = getattr(band_pts, "surface_points", (None, None))
        if az_raw is None:
            return
        valid_raw = getattr(band_pts, "surface_valid_mask", None)
        if valid_raw is None:
            valid_raw = np.ones_like(az_raw, dtype=bool)

        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]
        all_sx = []
        all_sy = []

        for offset in offsets:
            final_az = az_raw + offset
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask):
                continue

            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            culled_valid = np.asarray(valid_raw[mask], dtype=bool)
            if not np.any(culled_valid):
                continue

            if proj_fn_numpy:
                sx, sy_base = proj_fn_numpy(
                    np.zeros_like(culled_az), culled_az
                )
                sy = sy_base + 2.0 - (culled_h * px_alt)
            else:
                sx = []
                sy = []
                for az_value, height_value in zip(culled_az, culled_h):
                    anchor = proj_fn(0, az_value)
                    if anchor:
                        sx.append(anchor[0])
                        sy.append(anchor[1] + 2.0 - height_value * px_alt)
                    else:
                        sx.append(np.nan)
                        sy.append(h * 2)
                sx = np.array(sx)
                sy = np.array(sy)

            finite_mask = np.isfinite(sx) & np.isfinite(sy) & culled_valid
            if not np.any(finite_mask):
                continue
            edge_mask = np.diff(
                np.pad(finite_mask.astype(np.int8), (1, 1), constant_values=0)
            )
            starts = np.where(edge_mask == 1)[0]
            stops = np.where(edge_mask == -1)[0]
            for start, stop in zip(starts, stops):
                seg_sx = sx[start:stop]
                seg_sy = sy[start:stop]
                if len(seg_sx) >= 2:
                    all_sx.append(seg_sx)
                    all_sy.append(seg_sy)

        if all_sx:
            self._stroke_ridge_lines_numpy(
                painter, all_sx, all_sy, color, width=0.9
            )

    def _fill_shaded_strip_downward_numpy(
        self,
        painter,
        list_sx,
        list_sy,
        list_az,
        list_h,
        base_color,
        bottom_y,
        sun_alt,
        sun_az,
        band_pts,
        sky_color,
        light_context: TerrainCelestialLightContext | None = None,
    ):
        for sx_arr, sy_arr, az_arr, h_arr in zip(
            list_sx, list_sy, list_az, list_h
        ):
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue

            f_sx = np.asarray(sx_arr[valid], dtype=np.float32)
            f_sy = np.asarray(sy_arr[valid], dtype=np.float32)
            f_az = np.asarray(az_arr[valid], dtype=np.float32)
            f_h = np.asarray(h_arr[valid], dtype=np.float32)
            if len(f_sx) < 2:
                continue

            light_grid = self._terrain_profile_light_grid(
                f_az,
                f_h,
                sun_alt,
                sun_az,
                band_pts,
                light_context=light_context,
            )
            shade_values = light_grid.intensity
            band_distance = float(
                getattr(band_pts, "band_max", 0.0) or 0.0
            )
            if np.nanmax(np.abs(shade_values - 1.0)) < 0.006:
                color = self._compose_profile_light_color(
                    base_color,
                    float(np.nanmean(shade_values)),
                    band_distance,
                    sky_color,
                    light_grid.factors,
                    solar_exposure=float(
                        np.nanmean(light_grid.solar_exposure)
                    ),
                    lunar_exposure=float(
                        np.nanmean(light_grid.lunar_exposure)
                    ),
                )
                self._fill_strip_downward_numpy(
                    painter, [f_sx], [f_sy], color, bottom_y, solid=True
                )
                continue

            min_x = float(np.nanmin(f_sx))
            max_x = float(np.nanmax(f_sx))
            if max_x - min_x < 1.0:
                color = self._compose_profile_light_color(
                    base_color,
                    float(np.nanmean(shade_values)),
                    band_distance,
                    sky_color,
                    light_grid.factors,
                    solar_exposure=float(
                        np.nanmean(light_grid.solar_exposure)
                    ),
                    lunar_exposure=float(
                        np.nanmean(light_grid.lunar_exposure)
                    ),
                )
                self._fill_strip_downward_numpy(
                    painter, [f_sx], [f_sy], color, bottom_y, solid=True
                )
                continue

            path = QPainterPath()
            path.moveTo(float(f_sx[0]), float(f_sy[0]))
            for x, y in zip(f_sx[1:], f_sy[1:]):
                path.lineTo(float(x), float(y))
            path.lineTo(float(f_sx[-1]), float(bottom_y))
            path.lineTo(float(f_sx[0]), float(bottom_y))
            path.closeSubpath()

            gradient = QLinearGradient(min_x, 0.0, max_x, 0.0)
            stop_count = min(18, len(f_sx))
            stop_indices = np.linspace(0, len(f_sx) - 1, stop_count).astype(int)
            stops = []
            for idx in stop_indices:
                pos = _clamp01((float(f_sx[idx]) - min_x) / (max_x - min_x))
                stops.append(
                    (
                        pos,
                        float(shade_values[idx]),
                        float(light_grid.solar_exposure[idx]),
                        float(light_grid.lunar_exposure[idx]),
                    )
                )
            stops.sort(key=lambda item: item[0])

            last_pos = -1.0
            for pos, shade, solar_exposure, lunar_exposure in stops:
                if pos <= last_pos + 0.001:
                    continue
                color = self._compose_profile_light_color(
                    base_color,
                    shade,
                    band_distance,
                    sky_color,
                    light_grid.factors,
                    solar_exposure=solar_exposure,
                    lunar_exposure=lunar_exposure,
                )
                color.setAlpha(255)
                gradient.setColorAt(pos, color)
                last_pos = pos
            if last_pos < 1.0:
                color = self._compose_profile_light_color(
                    base_color,
                    float(shade_values[-1]),
                    band_distance,
                    sky_color,
                    light_grid.factors,
                    solar_exposure=float(light_grid.solar_exposure[-1]),
                    lunar_exposure=float(light_grid.lunar_exposure[-1]),
                )
                color.setAlpha(255)
                gradient.setColorAt(1.0, color)

            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(gradient))
            painter.drawPath(path)

    def _draw_edge_texture_numpy(
        self, painter, list_sx, list_sy, list_az, color, band_pts
    ):
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        distance_factor = 1.0 - _distance_haze_factor(band_max_m)
        if distance_factor <= 0.18:
            return

        texture_color = QColor(color)
        texture_color.setAlpha(min(color.alpha(), 5 + int(10 * distance_factor)))
        if texture_color.alpha() <= 0:
            return

        pen = QPen(texture_color)
        pen.setWidthF(0.55)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        for sx_arr, sy_arr, az_arr in zip(list_sx, list_sy, list_az):
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue
            f_sx = np.asarray(sx_arr[valid], dtype=np.float32)
            f_sy = np.asarray(sy_arr[valid], dtype=np.float32)
            f_az = np.asarray(az_arr[valid], dtype=np.float32)
            if len(f_sx) < 2:
                continue

            step = max(2, int(math.ceil(len(f_sx) / 110.0)))
            for i in range(0, len(f_sx), step):
                seed = float(f_az[i]) * 12.9898 + band_max_m * 0.001
                noise = math.sin(seed) * 43758.5453
                noise -= math.floor(noise)
                if noise < 0.52:
                    continue
                length = 0.6 + 2.0 * noise * distance_factor
                x = float(f_sx[i])
                y = float(f_sy[i]) + 0.55
                painter.drawLine(QPointF(x, y), QPointF(x, y + length))

    def _draw_ground_linear(
        self,
        painter,
        band_pts,
        color,
        proj_fn,
        w,
        h,
        px_alt,
        cur_az,
        az_min,
        az_max,
        overlap_px=0.0,
        projection_fn_numpy=None,
        ridge_color=None,
        interaction_active=False,
    ):
        """
        Draw ground fill using the same projection logic as bands.
        """
        polygon_cache_key = ("ground", id(band_pts), float(overlap_px))
        if ridge_color is None and self._draw_cached_profile_polygons(
            painter, polygon_cache_key, color
        ):
            return

        az_raw, h_raw = band_pts.points
        if az_raw is None:
            return

        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        all_sx = []
        all_sy = []

        for offset in offsets:
            final_az = az_raw + offset
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask):
                continue

            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            point_budget = self._profile_point_budget(w, interaction_active)
            if len(culled_az) > point_budget:
                lod_indices = _extrema_lod_indices(culled_h, point_budget)
                culled_az = culled_az[lod_indices]
                culled_h = culled_h[lod_indices]

            if projection_fn_numpy:
                projected = projection_fn_numpy(culled_h, culled_az)
                if projected is None or len(projected) < 2:
                    continue
                sx = np.asarray(projected[0], dtype=np.float64)
                sy = np.asarray(projected[1], dtype=np.float64) - overlap_px
                if len(projected) >= 3:
                    projected_valid = np.asarray(projected[2], dtype=bool)
                    sx = np.where(projected_valid, sx, np.nan)
                    sy = np.where(projected_valid, sy, np.nan)
            else:
                sx = []
                sy = []
                for a_val, h_val in zip(culled_az, culled_h):
                    point = proj_fn(float(h_val), float(a_val))
                    if point:
                        sx.append(point[0])
                        sy.append(point[1] - overlap_px)
                    else:
                        sx.append(np.nan)
                        sy.append(h * 2)
                sx = np.array(sx)
                sy = np.array(sy)

            all_sx.append(sx)
            all_sy.append(sy)

        if all_sx:
            if ridge_color is None and self._profile_polygon_cache_view_key is not None:
                painter.setBrush(QBrush(color))
                painter.setPen(Qt.NoPen)
                for polygon in self._cache_profile_polygons(
                    polygon_cache_key, all_sx, all_sy, h * 2
                ):
                    painter.drawPolygon(polygon)
            else:
                self._fill_strip_downward_numpy(
                    painter, all_sx, all_sy, color, h * 2, solid=True
                )
            if ridge_color is not None:
                self._stroke_ridge_lines_numpy(
                    painter, all_sx, all_sy, ridge_color, width=0.65
                )

    def _stroke_ridge_lines_numpy(
        self, painter, list_sx, list_sy, color, width=1.0, y_offset=0.0
    ):
        pen = QPen(color)
        pen.setWidthF(float(width))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        for sx_arr, sy_arr in zip(list_sx, list_sy):
            if len(sx_arr) < 2:
                continue

            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue

            f_sx = sx_arr[valid]
            f_sy = sy_arr[valid] + float(y_offset)
            if len(f_sx) < 2:
                continue

            path = QPainterPath()
            path.moveTo(float(f_sx[0]), float(f_sy[0]))
            for x, y in zip(f_sx[1:], f_sy[1:]):
                path.lineTo(float(x), float(y))
            painter.drawPath(path)

    def _fill_strip_downward_numpy(
        self, painter, list_sx, list_sy, color, bottom_y, solid=False
    ):
        """Vectorized polygon drawing from NumPy arrays."""
        painter.setBrush(QBrush(color))
        if solid:
            painter.setPen(Qt.NoPen)
        else:
            painter.setPen(QPen(color, 1))

        for sx_arr, sy_arr in zip(list_sx, list_sy):
            if len(sx_arr) < 2:
                continue

            # 1. Filter out NaNs/Infs (projection singularities)
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue

            f_sx = sx_arr[valid]
            f_sy = sy_arr[valid]

            if len(f_sx) < 2:
                continue

            # Constructing QPolygonF from list of QPointF
            # Convert to float explicit for compatibility
            pts = [QPointF(float(x), float(y)) for x, y in zip(f_sx, f_sy)]

            # Close downward
            pts.append(QPointF(float(f_sx[-1]), float(bottom_y)))
            pts.append(QPointF(float(f_sx[0]), float(bottom_y)))

            poly = QPolygonF(pts)
            painter.drawPolygon(poly)

    # ── procedural fallback ──

    def _build_procedural_fallback(self):
        print(
            "[HorizonOverlay] WARNING: Using procedural fallback (South Flat / North Mountains)."
        )
        rng = random.Random(42)

        # 3 simple layers matching POC colors
        configs = [
            (
                "far_25_60",
                QColor(38, 48, 68),
                QColor(140, 155, 175),
                0.6,
                3.0,
                1.5,
            ),
            (
                "mid_3_10",
                QColor(18, 25, 42),
                QColor(100, 120, 135),
                0.8,
                2.0,
                2.0,
            ),
            (
                "near_0_1",
                QColor(8, 12, 22),
                QColor(70, 90, 100),
                1.0,
                5.0,
                1.0,
            ),
        ]

        for bid, nc, dc, base, freq, amp in configs:
            pts_az = []
            pts_h = []
            # Use the same angular sampling selected for real horizon rays.
            from TerraLab.common.utils import get_config_value
            from TerraLab.terrain.ray_precision import (
                normalize_ray_step_deg,
                ray_count,
            )

            ray_step_deg = normalize_ray_step_deg(
                get_config_value("horizon_ray_step_deg", 0.5)
            )
            for step in range(ray_count(ray_step_deg)):
                az = step * ray_step_deg
                # Normalize az to 0..360
                norm_az = az % 360.0

                # Logic: South is approx 90..270. North is 270..360 + 0..90.
                # Let's define "Flat Zone" as 110 to 250 to have some transition

                is_flat = False
                transition = 0.0

                if 135 < norm_az < 225:
                    # Pure Flat
                    val = 0.2
                else:
                    # Mountains
                    rad = math.radians(az)

                    # Noise composition
                    n1 = abs(math.sin(rad * freq)) * amp
                    n2 = abs(math.sin(rad * freq * 2.3)) * (amp * 0.5)
                    n3 = abs(math.sin(rad * freq * 5.1)) * (amp * 0.25)

                    val = base + (n1 + n2 + n3) * rng.uniform(0.9, 1.1)

                    # Smooth transition to flat zone?
                    # Simple lerp if near boundaries (90..135 and 225..270)
                    if 90 < norm_az <= 135:
                        t = (norm_az - 90) / 45.0  # 0..1
                        # 1=Flat, 0=Mount
                        val = val * (1.0 - t) + 0.2 * t
                    elif 225 <= norm_az < 270:
                        t = (norm_az - 225) / 45.0  # 0..1
                        # 0=Flat, 1=Mount
                        val = 0.2 * (1.0 - t) + val * t

                pts_az.append(az)
                pts_h.append(max(0.2, val))

            bp = _BandPoints.__new__(_BandPoints)
            bp.points = (
                np.array(pts_az, dtype=np.float32),
                np.array(pts_h, dtype=np.float32),
            )
            bp.valid_mask = np.ones(len(pts_az), dtype=bool)
            self._layers.append((bp, nc, dc))
