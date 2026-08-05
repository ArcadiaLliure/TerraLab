"""Renderer-neutral terrain work orders built from prepared CPU artifacts.

This module is intentionally Qt-free.  It turns a baked horizon profile and
an optional, already-sampled surface cache into the exact projected buffers,
colours and surface hits that every presentation backend consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import numpy as np

from TerraLab.core.rendering_contracts.plans import (
    Bounds,
    MaterialParameters,
    SceneRenderPlan,
    TerrainMeshResource,
    TerrainSurfaceAttributes,
    TerrainTile,
)
from TerraLab.scene.projection import (
    project_universal_stereo_numpy,
    project_universal_stereo_point,
)
from TerraLab.scene.plans.terrain_geometry import (
    TerrainGeometryPlan,
    TerrainHitRecord,
    build_terrain_geometry_plan,
)
from TerraLab.scene.plans.terrain_materials import (
    TerrainMaterialPlan,
    build_terrain_material_plan,
)
from TerraLab.terrain.render.config import (
    TerrainCelestialLightContext,
    TerrainRenderSettings,
)
from TerraLab.terrain.mesh.normals import compute_polar_mesh_normals
from TerraLab.terrain.render.overlay_types import _TerrainRenderAsset

if TYPE_CHECKING:
    from TerraLab.scene.contracts import SceneFrame
    from TerraLab.scene.render_state import RenderState
    from TerraLab.terrain.domain.profile import HorizonProfile


@dataclass(frozen=True, slots=True)
class TerrainSurfaceHitIndex:
    """CPU-resolved surface attributes indexed by the terrain raster cache."""

    geometry: TerrainGeometryPlan
    polar_class_ids: np.ndarray
    polar_categorical: np.ndarray
    polar_source_indices: np.ndarray
    near_patch_class_ids: np.ndarray
    near_patch_categorical: np.ndarray
    near_patch_source_indices: np.ndarray
    width: int
    height: int

    def query(self, x: float, y: float) -> dict[str, object] | None:
        hit = self.geometry.query_screen_hit(x, y, self.width, self.height)
        if hit is None:
            return None
        return self._payload(hit)

    def _payload(self, hit: TerrainHitRecord) -> dict[str, object]:
        row = int(hit.row_index)
        column = int(hit.column_index)
        if hit.domain == 1:
            class_ids = self.near_patch_class_ids
            categorical_values = self.near_patch_categorical
            source_indices = self.near_patch_source_indices
        else:
            class_ids = self.polar_class_ids
            categorical_values = self.polar_categorical
            source_indices = self.polar_source_indices
        class_id = self._value(class_ids, row, column, default=-1)
        categorical = bool(
            self._value(categorical_values, row, column, default=False)
        )
        source_index = self._value(source_indices, row, column, default=-1)
        return {
            "kind": "surface",
            "class_id": class_id,
            "categorical": categorical,
            "source_index": source_index,
            "distance_m": float(hit.distance_m),
            "row_index": row,
            "column_index": column,
            "domain": int(hit.domain),
        }

    @staticmethod
    def _value(
        values: np.ndarray, row: int, column: int, *, default: int | bool
    ) -> int | bool:
        array = np.asarray(values)
        if array.ndim != 2 or not (
            0 <= row < array.shape[0] and 0 <= column < array.shape[1]
        ):
            return default
        value = array[row, column]
        return bool(value) if isinstance(default, bool) else int(value)


@dataclass(frozen=True, slots=True)
class TerrainSurfaceSamples:
    """CPU-resolved RGB and categorical samples for both terrain domains."""

    polar_rgba: np.ndarray
    polar_class_ids: np.ndarray
    polar_categorical: np.ndarray
    polar_source_indices: np.ndarray
    near_patch_rgba: np.ndarray
    near_patch_class_ids: np.ndarray
    near_patch_categorical: np.ndarray
    near_patch_source_indices: np.ndarray


@dataclass(frozen=True, slots=True)
class TerrainLayerPlans:
    """One backend-neutral terrain capability plus its CPU pick index."""

    scene_plan: SceneRenderPlan | None
    geometry: TerrainGeometryPlan | None = None
    materials: TerrainMaterialPlan | None = None
    surface_hits: TerrainSurfaceHitIndex | None = None


def build_terrain_layer_plan(
    frame: "SceneFrame",
    state: "RenderState",
    profile: "HorizonProfile | None",
    surface_cache: object | None,
) -> TerrainLayerPlans:
    """Resolve profile/relief terrain once before presentation starts."""

    if "terrain" not in {layer.value for layer in frame.layers.order}:
        return TerrainLayerPlans(None)
    visibility = frame.terrain.visibility
    if not visibility.horizon_enabled:
        return TerrainLayerPlans(None)

    order = tuple(layer.value for layer in frame.layers.order).index("terrain")
    if profile is None:
        return TerrainLayerPlans(None)
    if visibility.topography_enabled and visibility.terrain_3d_enabled:
        resolved = _relief_plan(frame, state, profile, surface_cache, order)
        if resolved.scene_plan is not None:
            return resolved
    return _profile_plan(frame, state, profile, order)


def _relief_plan(
    frame: "SceneFrame",
    state: "RenderState",
    profile: "HorizonProfile",
    surface_cache: object | None,
    order: int,
) -> TerrainLayerPlans:
    asset = _asset_from_profile(profile, surface_cache)
    if asset is None:
        return TerrainLayerPlans(None)
    width, height = int(frame.viewport.width), int(frame.viewport.height)
    fov_deg = np.degrees(
        4.0
        * np.arctan(
            width / (2.0 * height * max(state.camera.zoom_level, 1e-6))
        )
    )
    az_min = state.camera.azimuth_offset - fov_deg / 2.0 - 45.0
    az_max = state.camera.azimuth_offset + fov_deg / 2.0 + 45.0
    grid_convergence_deg = float(getattr(profile, "grid_convergence_deg", 0.0))
    geometry = build_terrain_geometry_plan(
        asset,
        lambda altitude, azimuth: project_universal_stereo_point(
            altitude, azimuth, width, height, state.camera
        ),
        width,
        height,
        state.camera.azimuth_offset,
        az_min,
        az_max,
        projection_fn_numpy=lambda altitude,
        azimuth: project_universal_stereo_numpy(
            altitude, azimuth, width, height, state.camera
        ),
        grid_convergence_deg=grid_convergence_deg,
    )
    triangles = geometry.triangle_geometry
    if triangles is None or triangles.xy.size == 0:
        return TerrainLayerPlans(None, geometry=geometry)

    surface = _surface_values(asset, surface_cache)
    triangle_base = _triangle_values(
        triangles,
        surface.polar_rgba,
        surface.near_patch_rgba,
    )
    normal_z = _triangle_values(
        triangles,
        asset.normal_z,
        asset.near_patch_normal_z,
    )
    intensity = np.clip(0.2 + 0.8 * normal_z, 0.0, 1.0)
    snapshot = state.ephemeris_snapshot or {}
    moon = snapshot.get("moon", {})
    moon = moon if isinstance(moon, dict) else {}
    materials = build_terrain_material_plan(
        mesh_id=asset.mesh_id,
        base_rgba=triangle_base,
        intensity=intensity,
        distance_m=triangles.depth,
        settings=replace(
            TerrainRenderSettings(),
            surface_visual_style=frame.terrain.surface_visual_style.value,
        ),
        light_context=TerrainCelestialLightContext(
            sun_altitude_deg=state.sun_alt,
            sun_azimuth_deg=state.sun_az,
            moon_altitude_deg=_optional_float(moon.get("alt")),
            moon_azimuth_deg=_optional_float(moon.get("az")),
            moon_illumination=float(moon.get("illumination", 0.0) or 0.0),
            eclipse_factor=float(state.extras.get("eclipse_factor", 1.0)),
        ),
        maximum_distance_m=float(np.max(asset.distances)),
    )
    triangle_normals = np.stack(
        (
            _triangle_values(
                triangles,
                asset.normal_x,
                asset.near_patch_normal_x,
            ),
            _triangle_values(
                triangles,
                asset.normal_y,
                asset.near_patch_normal_y,
            ),
            _triangle_values(
                triangles,
                asset.normal_z,
                asset.near_patch_normal_z,
            ),
        ),
        axis=-1,
    )
    mesh = _mesh_resource(
        frame,
        order,
        geometry,
        materials,
        surface,
        triangle_normals,
        asset,
        grid_convergence_deg,
    )
    return TerrainLayerPlans(
        SceneRenderPlan("terrain", order, frame.generation, (mesh,), "1"),
        geometry=geometry,
        materials=materials,
        surface_hits=TerrainSurfaceHitIndex(
            geometry,
            surface.polar_class_ids,
            surface.polar_categorical,
            surface.polar_source_indices,
            surface.near_patch_class_ids,
            surface.near_patch_categorical,
            surface.near_patch_source_indices,
            width,
            height,
        ),
    )


def _profile_plan(
    frame: "SceneFrame",
    state: "RenderState",
    profile: "HorizonProfile",
    order: int,
) -> TerrainLayerPlans:
    """Build the non-3D profile as resolved triangles, not a Qt callback."""

    if not profile.bands:
        return TerrainLayerPlans(None)
    band = profile.bands[-1]
    angles = np.asarray(band.get("angles", ()), dtype=np.float64)
    azimuths = np.asarray(profile.azimuths, dtype=np.float64)
    if angles.size != azimuths.size or angles.size < 2:
        return TerrainLayerPlans(None)
    width, height = int(frame.viewport.width), int(frame.viewport.height)
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    previous: tuple[float, float] | None = None
    for angle, azimuth in zip(angles, azimuths, strict=True):
        point = project_universal_stereo_point(
            float(np.degrees(angle)),
            float(azimuth),
            width,
            height,
            state.camera,
        )
        if point is None:
            # Missing samples split the silhouette.  Joining the two valid
            # neighbours would invent terrain across unavailable data.
            previous = None
            continue
        resolved = (float(point[0]), float(point[1]))
        if previous is not None:
            segments.append((previous, resolved))
        previous = resolved
    if not segments:
        return TerrainLayerPlans(None)
    vertices: list[tuple[float, float, float]] = []
    indices: list[int] = []
    for first, second in segments:
        offset = len(vertices)
        vertices.extend(
            (
                (first[0], first[1], 0.0),
                (second[0], second[1], 0.0),
                (second[0], float(height) * 2.0, 0.0),
                (first[0], float(height) * 2.0, 0.0),
            )
        )
        indices.extend(
            (offset, offset + 1, offset + 2, offset, offset + 2, offset + 3)
        )
    bounds = Bounds.enclosing(tuple(vertices))
    color = (0.29, 0.25, 0.18, 1.0)
    mesh = TerrainMeshResource(
        primitive_id=f"terrain-profile:{frame.generation}",
        vertices=tuple(vertices),
        indices=tuple(indices),
        normals=((0.0, 0.0, 1.0),) * len(vertices),
        surface=TerrainSurfaceAttributes(
            colors=(color,) * len(vertices),
            alphas=(1.0,) * len(vertices),
            uv=((0.0, 0.0),) * len(vertices),
            elevations_m=(0.0,) * len(vertices),
            object_ids=tuple(
                f"terrain-profile:{index}" for index in range(len(vertices))
            ),
        ),
        tiles=(TerrainTile("terrain-profile", 0, len(indices), bounds),),
        layer_order=order,
        material=MaterialParameters(
            "terrain-profile", color=color, blend_mode="opaque"
        ),
        bounds=bounds,
        geometry_version=f"profile:{getattr(profile, 'geometry_id', '') or id(profile)}",
        material_version=frame.terrain.surface_visual_style.value,
        frame_generation=frame.generation,
    )
    return TerrainLayerPlans(
        SceneRenderPlan("terrain", order, frame.generation, (mesh,), "1")
    )


def _asset_from_profile(
    profile: "HorizonProfile", surface_cache: object | None
) -> _TerrainRenderAsset | None:
    mesh = getattr(profile, "terrain_mesh", None)
    if not isinstance(mesh, dict):
        return None
    azimuths = np.asarray(mesh.get("azimuths", ()), dtype=np.float32)
    distances = np.asarray(mesh.get("distances", ()), dtype=np.float32)
    altitudes = np.asarray(mesh.get("altitudes", ()), dtype=np.float32)
    elevations = np.asarray(mesh.get("elevations", ()), dtype=np.float32)
    if azimuths.ndim != 1 or distances.ndim != 1 or altitudes.ndim != 2:
        return None
    shape = altitudes.shape
    if len(azimuths) != shape[1] or len(distances) != shape[0]:
        return None
    if elevations.shape != shape:
        elevations = np.zeros(shape, dtype=np.float32)
    valid = np.asarray(mesh.get("valid", np.isfinite(altitudes)), dtype=bool)
    visible = np.asarray(mesh.get("visible", valid), dtype=bool)
    if valid.shape != shape:
        valid = np.isfinite(altitudes)
    if visible.shape != shape:
        visible = valid.copy()
    computed = compute_polar_mesh_normals(
        elevations, valid, distances, azimuths
    )
    normals = []
    for index, name in enumerate(("normal_x", "normal_y", "normal_z")):
        candidate = np.asarray(mesh.get(name, ()), dtype=np.float32)
        normals.append(
            candidate
            if candidate.shape == shape
            else np.asarray(computed[index], dtype=np.float32)
        )
    near_patch = _near_patch_from_mesh(mesh)
    return _TerrainRenderAsset(
        mesh_id=hash(
            (
                str(getattr(profile, "geometry_id", "")),
                shape,
                float(np.sum(distances)),
            )
        ),
        azimuths=azimuths,
        azimuths_closed=np.concatenate(
            (azimuths, (azimuths[0] + 360.0,))
        ).astype(np.float32),
        distances=distances,
        altitudes=altitudes,
        altitudes_closed=np.concatenate((altitudes, altitudes[:, :1]), axis=1),
        elevations=elevations,
        valid=valid,
        valid_closed=np.concatenate((valid, valid[:, :1]), axis=1),
        visible=visible,
        visible_closed=np.concatenate((visible, visible[:, :1]), axis=1),
        normal_x=normals[0],
        normal_y=normals[1],
        normal_z=normals[2],
        near_patch_eastings=near_patch[0],
        near_patch_northings=near_patch[1],
        near_patch_altitudes=near_patch[2],
        near_patch_elevations=near_patch[3],
        near_patch_valid=near_patch[4],
        near_patch_normal_x=near_patch[5],
        near_patch_normal_y=near_patch[6],
        near_patch_normal_z=near_patch[7],
    )


def _near_patch_from_mesh(
    mesh: dict[str, object],
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
]:
    """Return the baked Cartesian patch intact, or an empty compatible patch."""

    empty = (
        np.empty(0, dtype=np.float32),
        np.empty(0, dtype=np.float32),
        np.empty((0, 0), dtype=np.float32),
        np.empty((0, 0), dtype=np.float32),
        np.empty((0, 0), dtype=bool),
        np.empty((0, 0), dtype=np.float32),
        np.empty((0, 0), dtype=np.float32),
        np.empty((0, 0), dtype=np.float32),
    )
    eastings = np.asarray(
        mesh.get("near_patch_eastings", ()), dtype=np.float32
    )
    northings = np.asarray(
        mesh.get("near_patch_northings", ()), dtype=np.float32
    )
    altitudes = np.asarray(
        mesh.get("near_patch_altitudes", ()), dtype=np.float32
    )
    elevations = np.asarray(
        mesh.get("near_patch_elevations", ()), dtype=np.float32
    )
    shape = altitudes.shape
    if (
        altitudes.ndim != 2
        or shape[0] < 2
        or shape[1] < 2
        or eastings.shape != (shape[1],)
        or northings.shape != (shape[0],)
        or elevations.shape != shape
    ):
        return empty
    valid = np.asarray(
        mesh.get("near_patch_valid", np.isfinite(altitudes)), dtype=bool
    )
    if valid.shape != shape:
        valid = np.isfinite(altitudes)
    normals: list[np.ndarray] = []
    for name, fallback in (
        ("near_patch_normal_x", np.zeros(shape, dtype=np.float32)),
        ("near_patch_normal_y", np.zeros(shape, dtype=np.float32)),
        ("near_patch_normal_z", np.ones(shape, dtype=np.float32)),
    ):
        candidate = np.asarray(mesh.get(name, ()), dtype=np.float32)
        normals.append(candidate if candidate.shape == shape else fallback)
    return (
        eastings,
        northings,
        altitudes,
        elevations,
        valid,
        normals[0],
        normals[1],
        normals[2],
    )


def _surface_values(
    asset: _TerrainRenderAsset, surface_cache: object | None
) -> TerrainSurfaceSamples:
    polar = _surface_grid(
        surface_cache,
        asset.elevations,
        ("visual_rgba", "profile_rgba"),
        ("visual_class_ids", "profile_class_ids"),
        ("visual_categorical", "profile_categorical"),
        ("visual_source_indices", "profile_source_indices"),
    )
    near_patch = _surface_grid(
        surface_cache,
        asset.near_patch_elevations,
        ("near_patch_rgba",),
        ("near_patch_class_ids",),
        ("near_patch_categorical",),
        ("near_patch_source_indices",),
    )
    return TerrainSurfaceSamples(*polar, *near_patch)


def _surface_grid(
    surface_cache: object | None,
    elevations: np.ndarray,
    rgba_names: tuple[str, ...],
    class_names: tuple[str, ...],
    categorical_names: tuple[str, ...],
    source_names: tuple[str, ...],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    shape = elevations.shape
    if elevations.size == 0:
        return (
            np.empty(shape + (4,), dtype=np.uint8),
            np.empty(shape, dtype=np.int64),
            np.empty(shape, dtype=bool),
            np.empty(shape, dtype=np.int16),
        )
    rgba = _matching_array(surface_cache, rgba_names, shape + (4,), np.uint8)
    if rgba is None:
        elevation = elevations.astype(np.float32)
        scale = max(1.0, float(np.nanmax(elevation) - np.nanmin(elevation)))
        value = np.clip((elevation - np.nanmin(elevation)) / scale, 0.0, 1.0)
        rgba = np.stack(
            (
                72 + 55 * value,
                63 + 50 * value,
                45 + 34 * value,
                np.full(shape, 255),
            ),
            axis=-1,
        ).astype(np.uint8)
    class_ids = _matching_array(surface_cache, class_names, shape, np.int64)
    categorical = _matching_array(
        surface_cache, categorical_names, shape, bool
    )
    source_indices = _matching_array(
        surface_cache, source_names, shape, np.int16
    )
    return (
        rgba,
        class_ids
        if class_ids is not None
        else np.full(shape, -1, dtype=np.int64),
        categorical
        if categorical is not None
        else np.zeros(shape, dtype=bool),
        source_indices
        if source_indices is not None
        else np.full(shape, -1, dtype=np.int16),
    )


def _triangle_values(
    triangles: object, polar: np.ndarray, near_patch: np.ndarray
) -> np.ndarray:
    """Gather values by the geometry planner's explicit terrain domain."""

    rows = np.asarray(getattr(triangles, "vertex_rows"), dtype=np.intp)
    columns = np.asarray(getattr(triangles, "vertex_columns"), dtype=np.intp)
    domains = np.asarray(getattr(triangles, "vertex_domain"), dtype=np.uint8)
    value_shape = np.asarray(polar).shape[2:]
    values = np.empty(rows.shape + value_shape, dtype=np.asarray(polar).dtype)
    for domain, source in ((0, polar), (1, near_patch)):
        mask = domains == domain
        if np.any(mask):
            values[mask] = np.asarray(source)[rows[mask], columns[mask]]
    return values


def _matching_array(
    source: object | None,
    names: tuple[str, ...],
    shape: tuple[int, ...],
    dtype: Any,
) -> np.ndarray | None:
    for name in names:
        value = getattr(source, name, None)
        if value is not None and np.shape(value) == shape:
            return np.asarray(value, dtype=dtype)
    return None


def _mesh_resource(
    frame: "SceneFrame",
    order: int,
    geometry: TerrainGeometryPlan,
    materials: TerrainMaterialPlan,
    surface: TerrainSurfaceSamples,
    triangle_normals: np.ndarray,
    asset: _TerrainRenderAsset,
    grid_convergence_deg: float,
) -> TerrainMeshResource:
    """Make tiled CPU resources shared by the raster and GPU presenters."""

    triangles = geometry.triangle_geometry
    assert triangles is not None
    domains = np.asarray(triangles.vertex_domain, dtype=np.uint8)
    rows = np.asarray(triangles.vertex_rows, dtype=np.int32)
    columns = np.asarray(triangles.vertex_columns, dtype=np.int32)
    # The planner owns coarse visibility.  This ordering keeps each resolved
    # source tile contiguous, so a View can retain one GPU range per tile.
    triangle_order = np.lexsort(
        (
            columns[:, 0] // 16,
            rows[:, 0] // 16,
            domains[:, 0],
        )
    )
    screen_vertices = np.concatenate(
        (
            np.asarray(triangles.xy, dtype=np.float32),
            np.asarray(triangles.depth, dtype=np.float32)[..., None],
        ),
        axis=2,
    )[triangle_order]
    view_vertices = _terrain_direction_vertices(
        triangles,
        asset,
        grid_convergence_deg,
    )[triangle_order]
    colors_array = np.asarray(materials.vertex_rgba, dtype=np.uint8)[
        triangle_order
    ]
    class_ids = _triangle_values(
        triangles,
        surface.polar_class_ids,
        surface.near_patch_class_ids,
    )[triangle_order]
    categorical = _triangle_values(
        triangles,
        surface.polar_categorical,
        surface.near_patch_categorical,
    )[triangle_order]
    fog_factors = (
        np.asarray(materials.atmosphere.fog_factors, dtype=np.float32)[
            triangle_order
        ]
        if materials.atmosphere is not None
        else np.zeros(screen_vertices.shape[:2], dtype=np.float32)
    )
    normals_array = np.asarray(triangle_normals, dtype=np.float32)[
        triangle_order
    ]
    vertices = tuple(
        (float(row[0]), float(row[1]), float(row[2]))
        for row in screen_vertices.reshape(-1, 3)
    )
    projection_vertices = tuple(
        (float(row[0]), float(row[1]), float(row[2]))
        for row in view_vertices.reshape(-1, 3)
    )
    colors = tuple(
        (
            float(rgba[0]) / 255.0,
            float(rgba[1]) / 255.0,
            float(rgba[2]) / 255.0,
            float(rgba[3]) / 255.0,
        )
        for rgba in colors_array.reshape(-1, 4)
    )
    bounds = Bounds.enclosing(vertices)
    tile_keys = tuple(
        (
            int(domains[index, 0]),
            int(rows[index, 0] // 16),
            int(columns[index, 0] // 16),
        )
        for index in triangle_order
    )
    tiles: list[TerrainTile] = []
    first_triangle = 0
    while first_triangle < len(tile_keys):
        key = tile_keys[first_triangle]
        final_triangle = first_triangle + 1
        while (
            final_triangle < len(tile_keys)
            and tile_keys[final_triangle] == key
        ):
            final_triangle += 1
        first_index = first_triangle * 3
        index_count = (final_triangle - first_triangle) * 3
        tile_bounds = Bounds.enclosing(
            vertices[first_index : first_index + index_count]
        )
        tiles.append(
            TerrainTile(
                f"terrain:{geometry.mesh_id}:tile:{key[0]}:{key[1]}:{key[2]}",
                first_index,
                index_count,
                tile_bounds,
            )
        )
        first_triangle = final_triangle
    geometry_version = _terrain_resource_version(
        "geometry",
        geometry.mesh_id,
        np.round(view_vertices, decimals=6),
        np.asarray(normals_array, dtype=np.float32),
    )
    material_version = _terrain_resource_version(
        "material",
        geometry.mesh_id,
        colors_array,
        fog_factors,
        class_ids,
        categorical,
        materials.style,
    )
    return TerrainMeshResource(
        primitive_id=f"terrain:{geometry.mesh_id}",
        vertices=vertices,
        indices=tuple(range(len(vertices))),
        normals=tuple(
            (float(row[0]), float(row[1]), float(row[2]))
            for row in normals_array.reshape(-1, 3)
        ),
        surface=TerrainSurfaceAttributes(
            colors=colors,
            alphas=tuple(color[3] for color in colors),
            uv=((0.0, 0.0),) * len(vertices),
            elevations_m=tuple(vertex[2] for vertex in vertices),
            object_ids=tuple(
                f"terrain:{index}" for index in range(len(vertices))
            ),
            class_ids=tuple(int(value) for value in class_ids.reshape(-1)),
            categorical_flags=tuple(
                bool(value) for value in categorical.reshape(-1)
            ),
        ),
        tiles=tuple(tiles),
        layer_order=order,
        material=MaterialParameters(
            f"terrain:{geometry.mesh_id}", blend_mode="alpha"
        ),
        bounds=bounds,
        geometry_version=geometry_version,
        material_version=material_version,
        frame_generation=frame.generation,
        view_vertices=projection_vertices,
    )


def _terrain_direction_vertices(
    triangles: object,
    asset: _TerrainRenderAsset,
    grid_convergence_deg: float,
) -> np.ndarray:
    """Build renderer-neutral local direction vectors for camera transforms.

    The Model resolves the terrain's local geometry here.  The View receives
    only vectors and camera uniforms; it never selects source samples,
    inspects terrain data or performs terrain queries.
    """

    polar_azimuth = np.broadcast_to(
        np.asarray(asset.azimuths, dtype=np.float32)[None, :],
        np.asarray(asset.altitudes).shape,
    )
    patch_east, patch_north = np.meshgrid(
        np.asarray(asset.near_patch_eastings, dtype=np.float32),
        np.asarray(asset.near_patch_northings, dtype=np.float32),
    )
    near_patch_azimuth = (
        np.degrees(np.arctan2(patch_east, patch_north))
        + float(grid_convergence_deg)
    ) % 360.0
    altitude = _triangle_values(
        triangles,
        np.asarray(asset.altitudes, dtype=np.float32),
        np.asarray(asset.near_patch_altitudes, dtype=np.float32),
    )
    azimuth = _triangle_values(
        triangles,
        polar_azimuth,
        near_patch_azimuth,
    )
    altitude_rad = np.radians(altitude)
    azimuth_rad = np.radians(azimuth)
    horizontal = np.cos(altitude_rad)
    return np.stack(
        (
            horizontal * np.sin(azimuth_rad),
            np.sin(altitude_rad),
            horizontal * np.cos(azimuth_rad),
        ),
        axis=-1,
    ).astype(np.float32)


def _terrain_resource_version(
    prefix: str,
    mesh_id: int,
    *values: object,
) -> str:
    """Version only CPU-resolved terrain resources, never frame counters."""

    digest = sha256()
    digest.update(str(mesh_id).encode("utf-8"))
    for value in values:
        if isinstance(value, np.ndarray):
            array = np.ascontiguousarray(value)
            digest.update(str(array.shape).encode("ascii"))
            digest.update(array.dtype.str.encode("ascii"))
            digest.update(memoryview(array))
        else:
            digest.update(repr(value).encode("utf-8"))
    return f"terrain-{prefix}-{digest.hexdigest()[:20]}"


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


__all__ = (
    "TerrainLayerPlans",
    "TerrainSurfaceHitIndex",
    "build_terrain_layer_plan",
)
