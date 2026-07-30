"""Renderer-neutral terrain geometry planning, DTOs, and hit query resolution."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from TerraLab.terrain.render.overlay_types import (
    _TerrainGeometryMetrics,
    _TerrainRenderAsset,
    _TerrainSurfaceGeometry,
    _TerrainTriangleGeometry,
)
from TerraLab.terrain.render.triangle_raster import (
    _rasterize_terrain_triangles,
)


@dataclass(frozen=True, slots=True)
class TerrainHitRecord:
    """Immutable spatial query record for a terrain hit at screen coordinates."""

    screen_x: float
    screen_y: float
    hit: bool
    triangle_index: int = -1
    bary_u: float = 0.0
    bary_v: float = 0.0
    bary_w: float = 0.0
    row_index: int = -1
    column_index: int = -1
    domain: int = 0
    distance_m: float = 0.0
    extra_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TerrainGeometryPlan:
    """Immutable renderer-neutral plan describing projected terrain geometry."""

    mesh_id: int
    triangle_geometry: _TerrainTriangleGeometry | None = None
    surface_geometry: _TerrainSurfaceGeometry | None = None
    cache_key: tuple = ()
    metrics: _TerrainGeometryMetrics = field(
        default_factory=_TerrainGeometryMetrics
    )
    raster_cache: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None

    def query_screen_hit(
        self, x: float, y: float, width: int, height: int
    ) -> TerrainHitRecord | None:
        """Perform a renderer-neutral hit query at screen point (x, y)."""
        if self.raster_cache is None or self.triangle_geometry is None:
            return None
        triangle_id, bary_u, bary_v = self.raster_cache
        h, w = triangle_id.shape
        if h <= 0 or w <= 0:
            return None
        px = int(math.floor(float(x) * w / max(1, width)))
        py = int(math.floor(float(y) * h / max(1, height)))
        if not (0 <= px < w and 0 <= py < h):
            return None
        tri_idx = int(triangle_id[py, px])
        if tri_idx < 0:
            return None
        u = float(bary_u[py, px])
        v = float(bary_v[py, px])
        w_bary = 1.0 - u - v
        tri_geom = self.triangle_geometry
        if tri_idx >= tri_geom.vertex_rows.shape[0]:
            return None

        weights = (u, v, w_bary)
        dominant_vertex = int(np.argmax(weights))
        row = int(tri_geom.vertex_rows[tri_idx, dominant_vertex])
        col = int(tri_geom.vertex_columns[tri_idx, dominant_vertex])
        domain = int(tri_geom.vertex_domain[tri_idx, dominant_vertex])
        dist = float(tri_geom.depth[tri_idx, dominant_vertex])

        return TerrainHitRecord(
            screen_x=float(x),
            screen_y=float(y),
            hit=True,
            triangle_index=tri_idx,
            bary_u=u,
            bary_v=v,
            bary_w=w_bary,
            row_index=row,
            column_index=col,
            domain=domain,
            distance_m=dist,
        )


def build_terrain_triangles_geometry(
    asset: _TerrainRenderAsset,
    projection_fn: Callable[[float, float], tuple[float, float] | None],
    width: int,
    height: int,
    cur_az: float,
    az_min: float,
    az_max: float,
    projection_fn_numpy: Callable[[np.ndarray, np.ndarray], Any] | None = None,
    grid_convergence_deg: float = 0.0,
) -> _TerrainTriangleGeometry | None:
    """Project the complete polar mesh and form valid screen triangles."""

    started = time.perf_counter()
    azimuths = np.asarray(asset.azimuths, dtype=np.float64)
    relative = (azimuths - float(cur_az) + 180.0) % 360.0 - 180.0
    full_order = np.argsort(relative, kind="stable")
    full_unwrapped = float(cur_az) + relative[full_order]
    in_view = (full_unwrapped >= float(az_min)) & (
        full_unwrapped <= float(az_max)
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
    vertex_valid = (
        original_valid & projected_valid & np.isfinite(sx) & np.isfinite(sy)
    )
    az_delta = np.diff(unwrapped_az)
    finite_steps = az_delta[np.isfinite(az_delta) & (az_delta > 1e-9)]
    nominal_step = float(np.median(finite_steps)) if finite_steps.size else 1.0
    adjacent = (
        np.isfinite(az_delta)
        & (az_delta > 0.0)
        & (az_delta <= nominal_step * 1.5 + 1e-9)
    )
    cell_valid = (
        vertex_valid[:-1, :-1]
        & vertex_valid[1:, :-1]
        & vertex_valid[1:, 1:]
        & vertex_valid[:-1, 1:]
        & adjacent[None, :]
    )
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

    patch_shape = np.shape(asset.near_patch_altitudes)
    patch_vertex_count = (
        int(np.prod(patch_shape)) if len(patch_shape) == 2 else 0
    )
    if patch_shape[0] >= 2 and patch_shape[1] >= 2:
        patch_east, patch_north = np.meshgrid(
            np.asarray(asset.near_patch_eastings, dtype=np.float64),
            np.asarray(asset.near_patch_northings, dtype=np.float64),
        )
        patch_distance = np.hypot(patch_east, patch_north)
        convergence = float(grid_convergence_deg)
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

    twice_area = (xy[:, 1, 0] - xy[:, 0, 0]) * (xy[:, 2, 1] - xy[:, 0, 1]) - (
        xy[:, 1, 1] - xy[:, 0, 1]
    ) * (xy[:, 2, 0] - xy[:, 0, 0])
    edge_01 = np.hypot(xy[:, 1, 0] - xy[:, 0, 0], xy[:, 1, 1] - xy[:, 0, 1])
    edge_12 = np.hypot(xy[:, 2, 0] - xy[:, 1, 0], xy[:, 2, 1] - xy[:, 1, 1])
    edge_20 = np.hypot(xy[:, 0, 0] - xy[:, 2, 0], xy[:, 0, 1] - xy[:, 2, 1])
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
    return _TerrainTriangleGeometry(
        xy,
        depth,
        triangle_rows,
        triangle_columns,
        vertex_domain,
        metrics,
    )


def build_terrain_geometry_plan(
    asset: _TerrainRenderAsset,
    projection_fn: Callable[[float, float], tuple[float, float] | None],
    width: int,
    height: int,
    cur_az: float,
    az_min: float,
    az_max: float,
    projection_fn_numpy: Callable[[np.ndarray, np.ndarray], Any] | None = None,
    grid_convergence_deg: float = 0.0,
    rasterize: bool = True,
    supersample: int = 2,
    cache_key: tuple = (),
) -> TerrainGeometryPlan:
    """Build a complete, renderer-neutral TerrainGeometryPlan."""

    triangles = build_terrain_triangles_geometry(
        asset,
        projection_fn,
        width,
        height,
        cur_az,
        az_min,
        az_max,
        projection_fn_numpy=projection_fn_numpy,
        grid_convergence_deg=grid_convergence_deg,
    )
    if triangles is None:
        return TerrainGeometryPlan(
            mesh_id=int(asset.mesh_id),
            cache_key=cache_key,
        )

    raster_cache = None
    if rasterize and triangles.xy.shape[0] > 0:
        _, tri_id, bary_u, bary_v = _rasterize_terrain_triangles(
            triangles.xy,
            triangles.depth,
            width,
            height,
            supersample=supersample,
        )
        raster_cache = (tri_id, bary_u, bary_v)

    return TerrainGeometryPlan(
        mesh_id=int(asset.mesh_id),
        triangle_geometry=triangles,
        cache_key=cache_key,
        metrics=triangles.metrics,
        raster_cache=raster_cache,
    )
