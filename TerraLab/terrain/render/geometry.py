"""Pure projected terrain geometry construction and simplification."""

from __future__ import annotations

import math
import time

import numpy as np
from scipy.ndimage import (
    convolve,
    gaussian_filter,
    label as connected_components,
)

try:
    from numba import njit
except Exception:  # pragma: no cover - optional acceleration
    njit = None


from TerraLab.terrain.render.overlay_types import (
    TerrainMaterialSamples,
    _TerrainGeometryMetrics,
    _TerrainSurfaceGeometry,
    _TerrainSurfaceSpan,
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
