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

try:
    from numba import njit
except Exception:  # pragma: no cover - optional acceleration
    njit = None

from TerraLab.common.performance import (
    ByteLRU,
    DEFAULT_PERFORMANCE_BUDGET,
    PERFORMANCE_FLAGS,
)
from TerraLab.config import ConfigManager
from TerraLab.terrain.render_pipeline import (
    atmospheric_fog_factor,
    compose_vertex_rgba,
    light_direction_enu,
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
    metrics: _TerrainGeometryMetrics

    def __post_init__(self) -> None:
        for name in ("xy", "depth", "vertex_rows", "vertex_columns"):
            value = np.asarray(getattr(self, name))
            value.setflags(write=False)
            object.__setattr__(self, name, value)


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


_rasterize_triangles_fast = (
    njit(cache=True, nogil=True)(_rasterize_triangles_impl) if njit is not None else None
)


def _rasterize_terrain_triangles(xy, depth, width, height, supersample=2):
    rasterizer = _rasterize_triangles_fast or _rasterize_triangles_impl
    return rasterizer(
        np.asarray(xy, dtype=np.float64),
        np.asarray(depth, dtype=np.float64),
        int(width),
        int(height),
        int(supersample),
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


def _calc_t_night(ut_hour: float) -> float:
    """Compute a [0..1] night factor from UTC hour (0=midnight, 12=noon)."""
    val = math.cos((ut_hour / 24.0) * 2 * math.pi)
    t = (val + 1.0) / 2.0
    t = t * t * (3.0 - 2.0 * t)  # smoothstep
    return max(0.0, min(1.0, t))


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
        self._terrain_geometry_cache_key = None
        self._terrain_geometry_cache = None
        self._terrain_polygon_cache_key = None
        self._terrain_polygon_cache = None
        self._terrain_polygon_cache_geometry = None
        self._profile_polygon_cache_view_key = None
        self._profile_polygon_cache = {}
        self._profile_image_cache_key = None
        self._profile_image_cache = None
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
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
        self.terrain_surface_opaque = bool(enabled)
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        self._terrain_raster_cache_key = None
        self._terrain_raster_cache = None
        self.request_update.emit()

    def reload_render_settings(self) -> None:
        """Reload terrain-only settings and invalidate colour-derived caches."""

        self.render_settings = ConfigManager().get_terrain_render_settings()
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_raster_cache_key = None
        self._terrain_raster_cache = None
        self._terrain_shade_cache.clear()
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
        self._profile_image_cache_key = None
        self._profile_image_cache = None
        self._terrain_surface_image_cache_key = None
        self._terrain_surface_image_cache = None
        self._terrain_surface_image_geometry = None
        self._terrain_render_asset = None
        self._terrain_shade_cache.clear()
        self._layers.clear()
        self._loaded = False
        if self.allow_procedural_fallback:
            self._build_procedural_fallback()
        self.request_update.emit()

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

        t_night = _calc_t_night(ut_hour)

        bottom_y = height * 2.0

        # Flat Line Mode
        if draw_flat_line:
            color = _lerp_color(GROUND_DAY, GROUND_NIGHT, t_night)
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
            for band_pts, night_c, day_c in terrain_layers:
                # First: Draw any domes that are behind or within this band (further than band_min)
                while (
                    pending_domes and pending_domes[0]["dist"] >= band_pts.band_min
                ):
                    d_info = pending_domes.pop(0)
                    draw_domes_callback(painter, d_info["idx"], d_info["dist"])

                base_color = _lerp_color(day_c, night_c, t_night)
                color = self._apply_atmospheric_perspective(
                    base_color, sky_ref, band_pts, t_night
                )
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
                    terrain_shading_enabled=False,
                    interaction_active=interaction_active,
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
            ground_c = _lerp_color(GROUND_DAY, GROUND_NIGHT, t_night)
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
        self, az_arr, h_arr, sun_alt, sun_az, band_pts
    ) -> np.ndarray:
        del h_arr
        if sun_alt is None or sun_az is None:
            return np.ones_like(az_arr, dtype=np.float32)

        strength = _solar_shading_strength(float(sun_alt))
        if strength <= 0.001 or len(az_arr) < 2:
            return np.ones_like(az_arr, dtype=np.float32)

        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        distance_contrast = 1.0 - 0.78 * _distance_haze_factor(band_max_m)
        strength *= max(0.18, distance_contrast)

        az = np.asarray(az_arr, dtype=np.float32)
        delta = np.deg2rad(((float(sun_az) - az + 180.0) % 360.0) - 180.0)
        broad_facing = np.cos(delta)

        shade = 1.0 + strength * 0.10 * broad_facing
        return np.clip(shade, 0.91, 1.08).astype(np.float32)

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

    def _configured_light(self) -> tuple[float, float, np.ndarray | None]:
        settings = self.render_settings
        if not settings.terrain_lighting_enabled:
            return (
                settings.terrain_light_elevation_deg,
                settings.terrain_light_azimuth_deg,
                None,
            )
        return (
            settings.terrain_light_elevation_deg,
            settings.terrain_light_azimuth_deg,
            light_direction_enu(
                settings.terrain_light_azimuth_deg,
                settings.terrain_light_elevation_deg,
            ),
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
        return _TerrainRenderAsset(
            mesh_id=id(mesh),
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
    ):
        nx, ny, nz = np.broadcast_arrays(
            np.asarray(normal_x, dtype=np.float32),
            np.asarray(normal_y, dtype=np.float32),
            np.asarray(normal_z, dtype=np.float32),
        )
        settings = self.render_settings
        if (
            not terrain_shading_enabled
            or not settings.terrain_lighting_enabled
            or sun_vec is None
            or not np.all(np.isfinite(sun_vec))
        ):
            return np.ones(nx.shape, dtype=np.float32)

        norm = np.sqrt(nx * nx + ny * ny + nz * nz)
        safe = np.isfinite(norm) & (norm > 1e-6)
        nx = np.where(safe, nx / np.where(safe, norm, 1.0), 0.0)
        ny = np.where(safe, ny / np.where(safe, norm, 1.0), 0.0)
        nz = np.where(safe, nz / np.where(safe, norm, 1.0), 1.0)

        sun_vec = np.asarray(sun_vec, dtype=np.float32)
        lambert = np.clip(
            nx * sun_vec[0] + ny * sun_vec[1] + nz * sun_vec[2],
            0.0,
            1.0,
        )
        if sun_visibility is not None:
            direct_visibility = np.broadcast_to(
                np.asarray(sun_visibility, dtype=np.float32), nx.shape
            )
            lambert *= np.clip(direct_visibility, 0.0, 1.0)
        factor = (
            float(settings.terrain_ambient_strength)
            + float(settings.terrain_diffuse_strength) * lambert
        )
        return np.clip(
            factor,
            settings.terrain_min_brightness,
            settings.terrain_max_brightness,
        ).astype(np.float32)

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
        if sources is not None:
            result_valid = result_valid & (np.asarray(sources)[sampled_row, nearest] >= 0)
        return result_rgba, result_valid

    def _relief_surface_samples(self, row_indices, column_indices, mesh_shape):
        cache = getattr(getattr(self, "profile", None), "surface_samples", None)
        rgba = _sample_cache_value(cache, "relief_rgba")
        valid = _sample_cache_value(cache, "relief_valid")
        rows = np.asarray(row_indices, dtype=np.int32)
        columns = np.asarray(column_indices, dtype=np.int32) % max(1, int(mesh_shape[1]))
        if rgba is None or valid is None:
            return np.zeros(rows.shape + (4,), dtype=np.uint8), np.zeros(rows.shape, dtype=bool)
        rgba = np.asarray(rgba, dtype=np.uint8)
        valid = np.asarray(valid, dtype=bool)
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
        sources = _sample_cache_value(cache, "relief_source_indices")
        if sources is not None:
            result_valid = result_valid & (
                np.asarray(sources)[nearest_rows, nearest_columns] >= 0
            )
        return result_rgba, result_valid

    def _apply_terrain_light(
        self, color: QColor, light_factor: float, sky_color: QColor, t_night: float
    ) -> QColor:
        del sky_color, t_night
        if not self.render_settings.terrain_lighting_enabled:
            return QColor(color)
        factor = max(
            self.render_settings.terrain_min_brightness,
            min(self.render_settings.terrain_max_brightness, float(light_factor)),
        )
        return QColor(
            max(0, min(255, round(color.red() * factor))),
            max(0, min(255, round(color.green() * factor))),
            max(0, min(255, round(color.blue() * factor))),
            color.alpha(),
        )

    def _apply_terrain_atmosphere(
        self, color: QColor, distance_m: float, sky_color: QColor, t_night: float
    ) -> QColor:
        del sky_color, t_night
        base = np.asarray(color.getRgb(), dtype=np.uint8)
        result = compose_vertex_rgba(
            base,
            1.0,
            float(distance_m),
            self.render_settings,
            maximum_distance_m=self._maximum_terrain_distance_m(),
        )
        return _qcolor_from_rgba(result)

    def _compose_terrain_color(
        self,
        base_color: QColor,
        light_factor: float,
        distance_m: float,
        sky_color: QColor,
        t_night: float,
    ) -> QColor:
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
        base = QColor(base_color) if base_color is not None else _lerp_color(day_c, night_c, t_night)

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
                    )
                )
            )

        return self._compose_terrain_color(
            base, light_factor, distance_m, sky_color, t_night
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
        )
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
                )
            )

        x_values = np.asarray(seg_x[finite], dtype=np.float32)
        shade_values = np.asarray(segment_shade[finite], dtype=np.float32)
        x_min = float(np.min(x_values))
        x_max = float(np.max(x_values))
        if x_max - x_min < 1.0 or np.ptp(shade_values) < 0.006:
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
    ):
        self._last_surface2d_quads = 0
        self._last_surface2d_vertices = 0
        self._last_surface2d_max_error_px = 0.0
        self._last_surface2d_geometry_s = 0.0
        self._last_surface2d_paint_s = 0.0
        asset = self._terrain_render_asset
        if asset is None or asset.mesh_id != id(mesh):
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

        sun_alt, sun_az, sun_vec = self._configured_light()
        terrain_shading_enabled = bool(
            terrain_shading_enabled
            and self.render_settings.terrain_lighting_enabled
        )
        shade_key = (
            asset.mesh_id,
            bool(terrain_shading_enabled),
            None if sun_alt is None else round(float(sun_alt) * 4.0) / 4.0,
            None if sun_az is None else round(float(sun_az) * 4.0) / 4.0,
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
            )
            shade_grid = self._smooth_light_grid(
                shade_grid,
                valid & visible,
                min_value=self.render_settings.terrain_min_brightness,
                max_value=self.render_settings.terrain_max_brightness,
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
            "triangles-v1",
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
        cell_rows, cell_columns = np.nonzero(cell_valid)
        if cell_rows.size == 0:
            result = _TerrainTriangleGeometry(
                np.empty((0, 3, 2), dtype=np.float64),
                np.empty((0, 3), dtype=np.float64),
                np.empty((0, 3), dtype=np.int32),
                np.empty((0, 3), dtype=np.int32),
                _TerrainGeometryMetrics(elapsed_s=time.perf_counter() - started),
            )
            return result

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

        # Close the near edge of the sampled mesh against the bottom of the
        # viewport. This is a screen-space cap for the unsampled area between
        # the observer and the first radial ring, not an altitude projection.
        near_pairs = np.flatnonzero(
            vertex_valid[0, :-1] & vertex_valid[0, 1:] & adjacent
        )
        if near_pairs.size:
            cap_xy = np.empty((near_pairs.size * 2, 3, 2), dtype=np.float64)
            cap_rows = np.zeros((near_pairs.size * 2, 3), dtype=np.int32)
            cap_sorted_columns = np.empty_like(cap_rows)
            bottom = float(height) + 1.0
            for index, column in enumerate(near_pairs):
                left = (sx[0, column], sy[0, column])
                right = (sx[0, column + 1], sy[0, column + 1])
                cap_xy[index * 2] = (left, (right[0], bottom), (left[0], bottom))
                cap_xy[index * 2 + 1] = (left, right, (right[0], bottom))
                cap_sorted_columns[index * 2] = (column, column + 1, column)
                cap_sorted_columns[index * 2 + 1] = (column, column + 1, column + 1)
            cap_columns = order[cap_sorted_columns].astype(np.int32)
            cap_depth = np.full(
                (cap_xy.shape[0], 3), float(asset.distances[0]), dtype=np.float64
            )
            xy = np.concatenate((xy, cap_xy), axis=0)
            depth = np.concatenate((depth, cap_depth), axis=0)
            triangle_rows = np.concatenate((triangle_rows, cap_rows), axis=0)
            triangle_columns = np.concatenate(
                (triangle_columns, cap_columns), axis=0
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
        elapsed = float(time.perf_counter() - started)
        metrics = _TerrainGeometryMetrics(
            spans=int(xy.shape[0]),
            source_samples=int(altitudes.size),
            invalid_samples=int(altitudes.size - np.count_nonzero(vertex_valid)),
            output_vertices=int(xy.shape[0] * 3),
            max_error_px=0.0,
            elapsed_s=elapsed,
        )
        result = _TerrainTriangleGeometry(
            xy, depth, triangle_rows, triangle_columns, metrics
        )
        if PERFORMANCE_FLAGS.relief_cached:
            self._terrain_geometry_cache_key = cache_key
            self._terrain_geometry_cache = result
        return result

    def _paint_terrain_triangles(
        self,
        painter,
        geometry,
        shade_grid,
        width,
        height,
        t_night,
        sky_color,
        sun_vec,
        sun_alt,
        terrain_shading_enabled,
        shade_key,
        interaction_active=False,
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
        supersample = 1 if interaction_active else 2
        quantized_night = round(float(t_night) * 256.0) / 256.0
        cache_key = (
            "zbuffer-v2",
            int(width),
            int(height),
            bool(interaction_active),
            shade_key,
            quantized_night,
            int(sky_color.rgba()),
            bool(terrain_shading_enabled),
            bool(self.terrain_surface_opaque),
        )
        if (
            PERFORMANCE_FLAGS.relief_cached
            and self._terrain_surface_image_geometry is geometry
            and self._terrain_surface_image_cache_key == cache_key
            and self._terrain_surface_image_cache is not None
        ):
            painter.drawImage(0, 0, self._terrain_surface_image_cache)
            self._last_surface2d_paint_s = time.perf_counter() - paint_started
            return

        raster_key = (
            id(geometry),
            render_width,
            render_height,
            supersample,
        )
        if (
            PERFORMANCE_FLAGS.relief_cached
            and raster_key == self._terrain_raster_cache_key
            and self._terrain_raster_cache is not None
        ):
            triangle_id, bary_u, bary_v = self._terrain_raster_cache
        else:
            scaled_xy = np.asarray(geometry.xy, dtype=np.float64).copy()
            scaled_xy[:, :, 0] *= float(render_width) / float(width)
            scaled_xy[:, :, 1] *= float(render_height) / float(height)
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
        if np.any(covered):
            ids = triangle_id[covered]
            u = bary_u[covered].astype(np.float64)
            v = bary_v[covered].astype(np.float64)
            w = 1.0 - u - v
            rows = geometry.vertex_rows[ids]
            columns = geometry.vertex_columns[ids]
            vertex_shades = np.asarray(shade_grid[rows, columns], dtype=np.float64)
            interpolated_shade = (
                u * vertex_shades[:, 0]
                + v * vertex_shades[:, 1]
                + w * vertex_shades[:, 2]
            )
            vertex_depth = geometry.depth[ids]
            interpolated_depth = (
                u * vertex_depth[:, 0] + v * vertex_depth[:, 1] + w * vertex_depth[:, 2]
            )
            # Quantized lookup preserves the existing colour model without a
            # Python QColor call for every covered sub-sample.
            distance_bins = np.round(interpolated_depth / 25.0).astype(np.int64)
            shade_bins = np.round(interpolated_shade * 200.0).astype(np.int64)
            keys = np.column_stack((distance_bins, shade_bins))
            unique_keys, inverse = np.unique(keys, axis=0, return_inverse=True)
            palette = np.empty((unique_keys.shape[0], 4), dtype=np.float64)
            for index, (distance_bin, shade_bin) in enumerate(unique_keys):
                color = self._terrain_surface_color(
                    float(distance_bin) * 25.0,
                    float(shade_bin) / 200.0,
                    quantized_night,
                    sky_color,
                    sun_vec,
                    sun_alt,
                    terrain_shading_enabled,
                )
                palette[index] = color.getRgb()
            rgba_high[covered] = np.clip(
                np.rint(palette[inverse]), 0, 255
            ).astype(np.uint8)

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
                int(width), int(height), Qt.IgnoreAspectRatio, Qt.FastTransformation
            )
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
                )
            elif cacheable_fill and self._profile_polygon_cache_view_key is not None:
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

            self._fill_strip_downward_numpy(
                painter, [f_sx], [f_sy], base_color, bottom_y, solid=True
            )

            shade_values = self._terrain_shade_values(
                f_az, f_h, sun_alt, sun_az, band_pts
            )
            if np.nanmax(np.abs(shade_values - 1.0)) < 0.006:
                continue

            min_x = float(np.nanmin(f_sx))
            max_x = float(np.nanmax(f_sx))
            if max_x - min_x < 1.0:
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
                stops.append((pos, float(shade_values[idx])))
            stops.sort(key=lambda item: item[0])

            last_pos = -1.0
            for pos, shade in stops:
                if pos <= last_pos + 0.001:
                    continue
                color = _shade_color(base_color, shade, sky_color)
                color.setAlpha(255)
                gradient.setColorAt(pos, color)
                last_pos = pos
            if last_pos < 1.0:
                color = _shade_color(
                    base_color, float(shade_values[-1]), sky_color
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
