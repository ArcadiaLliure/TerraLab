"""Vectorized terrain triangle rasterization and material lookup."""

from __future__ import annotations

import math

import numpy as np

try:
    from numba import njit
except Exception:  # pragma: no cover - optional acceleration
    njit = None


from TerraLab.terrain.render.overlay_types import TerrainMaterialSamples

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
