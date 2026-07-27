"""Terrain horizon profile domain model and invariants."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from TerraLab.terrain.crs import CRS_GEOGRAPHIC, CRS_TERRAIN_INTERNAL
from TerraLab.terrain.representation import (
    TerrainGeometrySource,
    TerrainRepresentationMode,
    normalize_terrain_geometry_source,
    normalize_terrain_representation_mode,
)
TERRAIN_MESH_VERSION = 3

@dataclass
class HorizonProfile:
    """Serializable horizon profile result."""

    azimuths: np.ndarray  # shape (N,), degrees 0‥360
    bands: List[Dict]  # each band: {id, angles, dists, heights}
    observer_lat: float = 0.0
    observer_lon: float = 0.0
    light_domes: Optional[np.ndarray] = None
    light_peak_distances: Optional[np.ndarray] = (
        None  # Distances of max light source per azimuth
    )
    resolved_mask: Optional[np.ndarray] = None
    terrain_mesh: Optional[Dict] = None
    resolved_radius_m: Optional[float] = None
    schema_version: int = 2
    representation_mode: TerrainRepresentationMode = TerrainRepresentationMode.RELIEF
    geometry_source: TerrainGeometrySource = TerrainGeometrySource.LEGACY_UNKNOWN
    geometry_id: str = ""
    elevation_source_ids: tuple[str, ...] = ()
    effective_elevation_source_id: Optional[str] = None
    elevation_source_status: str = "legacy_unknown"
    geometry_crs: str = CRS_TERRAIN_INTERNAL
    observer_x: Optional[float] = None
    observer_y: Optional[float] = None
    grid_convergence_deg: float = 0.0
    surface_samples: object = None

    def __post_init__(self) -> None:
        self.representation_mode = normalize_terrain_representation_mode(
            self.representation_mode
        )
        self.geometry_source = normalize_terrain_geometry_source(
            self.geometry_source
        )
        self.elevation_source_ids = tuple(
            str(value) for value in (self.elevation_source_ids or ()) if str(value)
        )

    def get_band_points(self, band_id: str):
        """Return list of (az_deg, elevation_deg) for a given band."""
        for b in self.bands:
            if b["id"] == band_id:
                pts = []
                for i, az in enumerate(self.azimuths):
                    ang_rad = b["angles"][i]
                    if ang_rad <= -np.pi / 2:
                        # No valid data for this azimuth — use -10.0 deg (hidden)
                        pts.append((float(az), -10.0))
                    else:
                        pts.append((float(az), float(np.rad2deg(ang_rad))))
                return pts
        return []

    def get_band_surface_points(self, band_id: str):
        """Return far-edge surface points for a band when available."""
        for b in self.bands:
            if b["id"] == band_id:
                angles = b.get("surface_angles")
                if angles is None:
                    return []
                pts = []
                for i, az in enumerate(self.azimuths):
                    ang_rad = angles[i]
                    if ang_rad <= -np.pi / 2:
                        pts.append((float(az), -10.0))
                    else:
                        pts.append((float(az), float(np.rad2deg(ang_rad))))
                return pts
        return []

    def covers_radius(self, requested_radius_m: float) -> bool:
        """Return false for legacy profiles whose coverage is unknown."""
        if self.terrain_mesh:
            try:
                if int(self.terrain_mesh.get("version", 1)) < TERRAIN_MESH_VERSION:
                    return False
            except (TypeError, ValueError):
                return False
        return self.resolved_radius_m is not None and self.resolved_radius_m + 0.5 >= float(requested_radius_m)
def build_flat_horizon_profile(
    *,
    observer_lat: float,
    observer_lon: float,
    representation_mode: TerrainRepresentationMode | str = TerrainRepresentationMode.RELIEF,
    band_defs: Optional[List[Dict]] = None,
    geometry_id: str = "",
    delta_az_deg: float = 0.5,
) -> HorizonProfile:
    """Build an explicit no-DEM profile while retaining surface sample points."""

    mode = normalize_terrain_representation_mode(representation_mode)
    definitions = list(
        band_defs
        or [{"id": "flat_0_150k", "min": 0.0, "max": 150_000.0}]
    )
    from TerraLab.terrain.ray_precision import normalize_ray_step_deg

    azimuths = np.arange(
        0.0, 360.0, normalize_ray_step_deg(delta_az_deg), dtype=np.float32
    )
    bands = []
    for definition in definitions:
        maximum = max(1.0, float(definition.get("max", 150_000.0)))
        zeros = np.zeros(azimuths.shape, dtype=np.float32)
        bands.append(
            {
                "id": str(definition.get("id", "flat")),
                "min": float(definition.get("min", 0.0)),
                "max": maximum,
                "angles": zeros.copy(),
                "dists": zeros.copy(),
                "heights": zeros.copy(),
                "surface_angles": zeros.copy(),
                "surface_dists": np.full(azimuths.shape, maximum, dtype=np.float32),
                "surface_heights": zeros.copy(),
            }
        )
    observer_x = observer_y = None
    try:
        from TerraLab.terrain.crs import DEFAULT_TRANSFORM_SERVICE

        observer_x, observer_y = DEFAULT_TRANSFORM_SERVICE.transform_xy(
            float(observer_lon),
            float(observer_lat),
            CRS_GEOGRAPHIC,
            CRS_TERRAIN_INTERNAL,
        )
    except (TypeError, ValueError, RuntimeError):
        observer_x = observer_y = None
    resolved_radius = max(float(item["max"]) for item in bands)
    identity = geometry_id or (
        f"flat:{float(observer_lat):.6f}:{float(observer_lon):.6f}:"
        f"{mode.value}:{resolved_radius:.1f}"
    )
    profile = HorizonProfile(
        azimuths=azimuths,
        bands=bands,
        observer_lat=float(observer_lat),
        observer_lon=float(observer_lon),
        resolved_mask=np.ones(azimuths.shape, dtype=bool),
        terrain_mesh=None,
        resolved_radius_m=resolved_radius,
        representation_mode=mode,
        geometry_source=TerrainGeometrySource.FLAT_FALLBACK,
        geometry_id=identity,
        elevation_source_status="fallback_no_elevation",
        geometry_crs=CRS_TERRAIN_INTERNAL,
        observer_x=float(observer_x) if observer_x is not None else None,
        observer_y=float(observer_y) if observer_y is not None else None,
    )
    profile._band_defs = definitions
    return profile


def limit_profile_radius(profile: HorizonProfile, radius_m: float) -> HorizonProfile:
    """Create a cheap, non-destructive view of a baked profile up to ``radius_m``."""
    radius = max(0.0, float(radius_m))
    band_defs = list(getattr(profile, "_band_defs", []) or [])
    allowed_ids = {
        str(item.get("id")) for item in band_defs
        if float(item.get("max", float("inf"))) <= radius + 0.5
    }
    if band_defs:
        bands = [band for band in profile.bands if str(band.get("id")) in allowed_ids]
        limited_defs = [item for item in band_defs if str(item.get("id")) in allowed_ids]
    else:
        bands = list(profile.bands)
        limited_defs = []

    mesh = profile.terrain_mesh
    limited_mesh = mesh
    if mesh and "distances" in mesh:
        distances = np.asarray(mesh["distances"])
        rows = int(np.searchsorted(distances, radius, side="right"))
        if rows < distances.size:
            limited_mesh = dict(mesh)
            for key in ("distances", "altitudes", "elevations", "normal_x", "normal_y", "normal_z", "valid", "visible"):
                if key in limited_mesh:
                    limited_mesh[key] = np.asarray(limited_mesh[key])[:rows]

    result = copy.copy(profile)
    result.bands = bands
    result.terrain_mesh = limited_mesh
    result._source_resolved_radius_m = profile.resolved_radius_m
    result.resolved_radius_m = min(radius, float(profile.resolved_radius_m or radius))
    if limited_defs:
        result._band_defs = limited_defs
    return result
