"""
horizon_engine.py — Real topographic horizon profile engine.

Loads ICGC 5×5 DEM tiles (ESRI ASCII Grid .txt), caches them as .npy,
and computes multi-band horizon silhouettes via raycasting with Earth
curvature correction.

"""

import glob
import math
import os
import threading
import copy
from pathlib import Path
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from TerraLab.common.perf_events import append_perf_event
from TerraLab.common.performance import (
    DEFAULT_PERFORMANCE_BUDGET,
    PERFORMANCE_FLAGS,
    process_memory_bytes,
)
from TerraLab.terrain.providers import (
    CRS_GEOGRAPHIC,
    CRS_TERRAIN_INTERNAL,
    PYPROJ_TRANSFORMER_LOCK,
)
from TerraLab.terrain.representation import (
    TerrainGeometrySource,
    TerrainRepresentationMode,
    normalize_terrain_geometry_source,
    normalize_terrain_representation_mode,
)
from TerraLab.terrain.render_pipeline import (
    TerrainSamplingSettings,
    apparent_elevation_degrees,
    apparent_elevation_radians,
)
from TerraLab.terrain.sampling import (
    build_adaptive_base_distances,
    evaluate_refinement_intervals,
)

# --- Constants ---
R_EARTH = 6_371_000.0
MIN_RELIEF_MESH_AZIMUTH_STEP_DEG = 0.05


def compute_polar_mesh_normals(
    elevations: np.ndarray,
    valid: np.ndarray,
    distances: np.ndarray,
    azimuths: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    elevations = np.asarray(elevations, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    distances = np.asarray(distances, dtype=np.float32)
    azimuths = np.asarray(azimuths, dtype=np.float32)
    if (
        elevations.ndim != 2
        or elevations.shape != valid.shape
        or elevations.shape != (distances.size, azimuths.size)
        or distances.size < 2
        or azimuths.size < 2
    ):
        shape = elevations.shape
        return (
            np.zeros(shape, dtype=np.float32),
            np.zeros(shape, dtype=np.float32),
            np.ones(shape, dtype=np.float32),
        )

    azimuth_radians = np.deg2rad(azimuths.astype(np.float64))[None, :]
    distance_grid = distances.astype(np.float64)[:, None]
    points = np.stack(
        np.broadcast_arrays(
            distance_grid * np.sin(azimuth_radians),
            distance_grid * np.cos(azimuth_radians),
            elevations.astype(np.float64),
        ),
        axis=-1,
    )

    previous_rows = np.maximum(np.arange(distances.size) - 1, 0)
    next_rows = np.minimum(np.arange(distances.size) + 1, distances.size - 1)
    previous_points = points[previous_rows]
    next_points = points[next_rows]
    previous_valid = valid[previous_rows] & valid
    next_valid = valid[next_rows] & valid
    radial = np.where(
        (previous_valid & next_valid)[..., None],
        next_points - previous_points,
        np.where(
            next_valid[..., None],
            next_points - points,
            np.where(previous_valid[..., None], points - previous_points, 0.0),
        ),
    )

    left_points = np.roll(points, 1, axis=1)
    right_points = np.roll(points, -1, axis=1)
    left_valid = np.roll(valid, 1, axis=1) & valid
    right_valid = np.roll(valid, -1, axis=1) & valid
    angular = np.where(
        (left_valid & right_valid)[..., None],
        right_points - left_points,
        np.where(
            right_valid[..., None],
            right_points - points,
            np.where(left_valid[..., None], points - left_points, 0.0),
        ),
    )

    radial_length = np.linalg.norm(radial, axis=-1)
    radial_fallback = np.broadcast_to(
        np.stack(
            (
                np.sin(azimuth_radians[0]),
                np.cos(azimuth_radians[0]),
                np.zeros(azimuths.size),
            ),
            axis=-1,
        )[None, :, :],
        points.shape,
    )
    radial = np.where((radial_length > 1e-9)[..., None], radial, radial_fallback)

    angular_length = np.linalg.norm(angular, axis=-1)
    angular_fallback = np.broadcast_to(
        np.stack(
            (
                np.cos(azimuth_radians[0]),
                -np.sin(azimuth_radians[0]),
                np.zeros(azimuths.size),
            ),
            axis=-1,
        )[None, :, :],
        points.shape,
    )
    angular = np.where(
        (angular_length > 1e-9)[..., None], angular, angular_fallback
    )

    normals = np.cross(angular, radial)
    normals = np.where((normals[..., 2] < 0.0)[..., None], -normals, normals)
    norm = np.linalg.norm(normals, axis=-1, keepdims=True)
    normals = np.divide(
        normals,
        np.maximum(norm, 1e-9),
        out=np.zeros_like(normals),
        where=norm > 1e-9,
    )
    fallback = np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    normals = np.where(
        (valid & np.isfinite(norm[..., 0]) & (norm[..., 0] > 1e-9))[..., None],
        normals,
        fallback,
    )
    return (
        normals[..., 0].astype(np.float32),
        normals[..., 1].astype(np.float32),
        normals[..., 2].astype(np.float32),
    )


def generate_bands(n: int = 20, max_dist_m: float | None = None) -> list:
    """
    Genera N bandes d'horitzó amb distribució logarítmica per zones (piecewise bilog).

    Zones:
      · Zona propera  (0 → near_km):  2/3 de les N bandes.
        Recull detall de primer pla (colls, turons, cingleres).
      · Zona llunyana (near_km → max): 1/3 de les N bandes.
        Representa la boira atmosfèrica amb menys bandes (l'ull no n'aprecia el detall).

    La distància mínima de la primera banda és step_min_m (≥ resolució del baker)
    per evitar artefactes "paret" quan l'observador és a la vora d'un penya-segat.

    Args:
        n: Nombre total de bandes (10=Baix, 20=Normal, 40=Alt, 60=Ultra, 80=Extrem)
        max_dist_m: Distància màxima de càlcul en metres
    Returns:
        Llista de dicts amb 'id', 'min', 'max' en metres
    """
    import math as _math
    if max_dist_m is None:
        raise ValueError("max_dist_m must be the resolved visibility radius")
    max_dist_m = max(1.0, float(max_dist_m))

    # ── Paramètres de partició ────────────────────────────────────────────────
    # Distància de tall entre zona propera i zona llunyana
    near_km = 5_000.0  # metres
    # Fracció de bandes dedicades a la zona propera (2/3)
    near_frac = 2.0 / 3.0
    # Distància mínima del primer extrem de banda
    # El baker ara comença a 0.5m, però les bandes < 1m no aporten informació visual extra
    step_min_m = 1.0

    n_near = max(2, round(n * near_frac))
    n_far = max(1, n - n_near)

    # ── Zona propera: log entre step_min_m i near_km ─────────────────────────
    log_n_min = _math.log(step_min_m)
    log_n_max = _math.log(near_km)

    near_inner = []
    for i in range(1, n_near + 1):
        t = i / n_near
        v = _math.exp(log_n_min + (log_n_max - log_n_min) * t)
        near_inner.append(min(v, near_km))

    # ── Zona llunyana: log entre near_km i max_dist_m ────────────────────────
    log_f_min = _math.log(near_km)
    log_f_max = _math.log(max_dist_m)

    far_inner = []
    for i in range(1, n_far + 1):
        t = i / n_far
        v = _math.exp(log_f_min + (log_f_max - log_f_min) * t)
        far_inner.append(min(v, max_dist_m))

    # ── Punts de tall combinats ───────────────────────────────────────────────
    # Sempre comencem des de 0 i garantim near_km com a punt de transició
    breakpoints = [0.0] + near_inner + far_inner

    # ── Etiquetes de zona i format de noms ───────────────────────────────────
    _zone_labels = [
        (0, 750, "gnd"),
        (750, 5_000, "near"),
        (5_000, 25_000, "mid"),
        (25_000, 100_000, "far"),
        (100_000, 999_999, "haze"),
    ]

    def _zone_for(m):
        for lo, hi, label in _zone_labels:
            if m < hi:
                return label
        return "haze"

    def _fmt(m):
        """Etiqueta de distància llegible: 250→'250', 1500→'1.5k', 35000→'35k'."""
        if m < 1000:
            return str(int(m))
        elif m < 10_000:
            v = m / 1_000
            return (
                f"{v:.1f}k".rstrip("0").rstrip(".") + "k"
                if v != int(v)
                else f"{int(v)}k"
            )
        else:
            return f"{int(round(m / 1000))}k"

    # ── Construcció de la llista de bandes ────────────────────────────────────
    bands = []
    total_bps = len(breakpoints)
    for i in range(total_bps - 1):
        lo = breakpoints[i]
        hi = breakpoints[i + 1]
        if hi <= lo:
            continue  # Salta bandes buides (pot passar per arrodoniments)
        zone = _zone_for(lo)
        band_id = f"{zone}_{_fmt(lo)}_{_fmt(hi)}"
        bands.append({"id": band_id, "min": lo, "max": hi})

    return bands


# Àlies retrocompatible — qualitat per defecte = 20 bandes
DEFAULT_BANDS = None


# ─────────────────────────────────────────────
#  Data classes
# ─────────────────────────────────────────────


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

    def save(self, path: str):
        """Save profile to .npy file."""
        data = {
            "schema_version": np.asarray(int(self.schema_version)),
            "representation_mode": np.asarray(self.representation_mode.value),
            "geometry_source": np.asarray(self.geometry_source.value),
            "geometry_id": np.asarray(str(self.geometry_id or "")),
            "elevation_source_ids": np.asarray(self.elevation_source_ids, dtype=str),
            "effective_elevation_source_id": np.asarray(
                str(self.effective_elevation_source_id or "")
            ),
            "elevation_source_status": np.asarray(
                str(self.elevation_source_status or "")
            ),
            "geometry_crs": np.asarray(str(self.geometry_crs or CRS_TERRAIN_INTERNAL)),
            "azimuths": self.azimuths,
            "observer_lat": self.observer_lat,
            "observer_lon": self.observer_lon,
            "grid_convergence_deg": np.asarray(float(self.grid_convergence_deg)),
            "n_bands": len(self.bands),
            "light_domes": self.light_domes,
            "light_peak_distances": self.light_peak_distances,
        }
        if self.observer_x is not None:
            data["observer_x"] = np.asarray(float(self.observer_x))
        if self.observer_y is not None:
            data["observer_y"] = np.asarray(float(self.observer_y))
        if self.resolved_radius_m is not None:
            data["resolved_radius_m"] = np.asarray(float(self.resolved_radius_m))
        if self.resolved_mask is not None:
            data["resolved_mask"] = np.asarray(
                self.resolved_mask, dtype=np.uint8
            )
        if self.terrain_mesh:
            mesh = self.terrain_mesh
            data["terrain_mesh_present"] = np.asarray(1, dtype=np.uint8)
            data["terrain_mesh_version"] = np.asarray(
                int(mesh.get("version", 1)), dtype=np.uint8
            )
            for key in (
                "azimuths",
                "distances",
                "altitudes",
                "elevations",
                "normal_x",
                "normal_y",
                "normal_z",
                "valid",
                "visible",
            ):
                if key in mesh:
                    data[f"terrain_mesh_{key}"] = np.asarray(mesh[key])
        for i, b in enumerate(self.bands):
            data[f"band_{i}_id"] = b["id"]
            if "min" in b:
                data[f"band_{i}_min"] = np.asarray(float(b["min"]))
            if "max" in b:
                data[f"band_{i}_max"] = np.asarray(float(b["max"]))
            data[f"band_{i}_angles"] = b["angles"]
            data[f"band_{i}_dists"] = b["dists"]
            data[f"band_{i}_heights"] = b["heights"]
            if "surface_angles" in b:
                data[f"band_{i}_surface_angles"] = b["surface_angles"]
                data[f"band_{i}_surface_dists"] = b.get(
                    "surface_dists", np.zeros_like(b["dists"])
                )
                data[f"band_{i}_surface_heights"] = b.get(
                    "surface_heights", np.zeros_like(b["heights"])
                )
        np.savez_compressed(path, **data)

    @staticmethod
    def load(path: str) -> "HorizonProfile":
        """Load profile from .npz file."""
        with np.load(path, allow_pickle=True) as d:
            azimuths = np.asarray(d["azimuths"]).copy()
            n_bands = int(np.asarray(d["n_bands"]).item())
            bands = []
            for i in range(n_bands):
                band = {
                    "id": str(d[f"band_{i}_id"]),
                    "angles": np.asarray(d[f"band_{i}_angles"]).copy(),
                    "dists": np.asarray(d[f"band_{i}_dists"]).copy(),
                    "heights": np.asarray(d[f"band_{i}_heights"]).copy(),
                }
                if f"band_{i}_min" in d:
                    band["min"] = float(np.asarray(d[f"band_{i}_min"]).item())
                if f"band_{i}_max" in d:
                    band["max"] = float(np.asarray(d[f"band_{i}_max"]).item())
                if f"band_{i}_surface_angles" in d:
                    band["surface_angles"] = np.asarray(
                        d[f"band_{i}_surface_angles"]
                    ).copy()
                    band["surface_dists"] = np.asarray(
                        d[f"band_{i}_surface_dists"]
                    ).copy()
                    band["surface_heights"] = np.asarray(
                        d[f"band_{i}_surface_heights"]
                    ).copy()
                bands.append(band)

            if "light_domes" in d:
                light_domes = np.asarray(d["light_domes"]).copy()
            else:
                light_domes = np.zeros(len(azimuths))
            if "light_peak_distances" in d:
                light_peak_distances = np.asarray(
                    d["light_peak_distances"]
                ).copy()
            else:
                light_peak_distances = np.zeros(len(azimuths))

            resolved_mask = None
            if "resolved_mask" in d:
                resolved_mask = np.asarray(
                    d["resolved_mask"], dtype=np.uint8
                ).astype(bool)

            terrain_mesh = None
            if (
                "terrain_mesh_present" in d
                and "terrain_mesh_azimuths" in d
                and "terrain_mesh_distances" in d
                and "terrain_mesh_altitudes" in d
            ):
                mesh_version = (
                    int(np.asarray(d["terrain_mesh_version"]).item())
                    if "terrain_mesh_version" in d
                    else 1
                )
                terrain_mesh = {
                    "version": mesh_version,
                    "azimuths": np.asarray(d["terrain_mesh_azimuths"]).copy(),
                    "distances": np.asarray(
                        d["terrain_mesh_distances"]
                    ).copy(),
                    "altitudes": np.asarray(
                        d["terrain_mesh_altitudes"]
                    ).copy(),
                    "elevations": np.asarray(
                        d["terrain_mesh_elevations"]
                        if "terrain_mesh_elevations" in d
                        else np.array([])
                    ).copy(),
                    "normal_x": np.asarray(
                        d["terrain_mesh_normal_x"]
                        if "terrain_mesh_normal_x" in d
                        else np.array([])
                    ).copy(),
                    "normal_y": np.asarray(
                        d["terrain_mesh_normal_y"]
                        if "terrain_mesh_normal_y" in d
                        else np.array([])
                    ).copy(),
                    "normal_z": np.asarray(
                        d["terrain_mesh_normal_z"]
                        if "terrain_mesh_normal_z" in d
                        else np.array([])
                    ).copy(),
                    "valid": np.asarray(
                        d["terrain_mesh_valid"]
                        if "terrain_mesh_valid" in d
                        else np.array([]),
                        dtype=np.uint8,
                    )
                    .astype(bool)
                    .copy(),
                    "visible": np.asarray(
                        d["terrain_mesh_visible"]
                        if "terrain_mesh_visible" in d
                        else np.array([]),
                        dtype=np.uint8,
                    )
                    .astype(bool)
                    .copy(),
                }

            observer_lat = (
                float(np.asarray(d["observer_lat"]).item())
                if "observer_lat" in d
                else 0.0
            )
            observer_lon = (
                float(np.asarray(d["observer_lon"]).item())
                if "observer_lon" in d
                else 0.0
            )
            resolved_radius_m = (
                float(np.asarray(d["resolved_radius_m"]).item())
                if "resolved_radius_m" in d
                else None
            )
            schema_version = (
                int(np.asarray(d["schema_version"]).item())
                if "schema_version" in d
                else 1
            )
            representation_mode = normalize_terrain_representation_mode(
                np.asarray(d["representation_mode"]).item()
                if "representation_mode" in d
                else TerrainRepresentationMode.RELIEF
            )
            geometry_source = normalize_terrain_geometry_source(
                np.asarray(d["geometry_source"]).item()
                if "geometry_source" in d
                else TerrainGeometrySource.LEGACY_UNKNOWN
            )
            geometry_id = (
                str(np.asarray(d["geometry_id"]).item())
                if "geometry_id" in d
                else ""
            )
            elevation_source_ids = (
                tuple(str(value) for value in np.asarray(d["elevation_source_ids"]).tolist())
                if "elevation_source_ids" in d
                else ()
            )
            effective_elevation_source_id = (
                str(np.asarray(d["effective_elevation_source_id"]).item())
                if "effective_elevation_source_id" in d
                else ""
            ) or None
            elevation_source_status = (
                str(np.asarray(d["elevation_source_status"]).item())
                if "elevation_source_status" in d
                else "legacy_unknown"
            )
            geometry_crs = (
                str(np.asarray(d["geometry_crs"]).item())
                if "geometry_crs" in d
                else CRS_TERRAIN_INTERNAL
            )
            observer_x = (
                float(np.asarray(d["observer_x"]).item()) if "observer_x" in d else None
            )
            observer_y = (
                float(np.asarray(d["observer_y"]).item()) if "observer_y" in d else None
            )
            grid_convergence_deg = (
                float(np.asarray(d["grid_convergence_deg"]).item())
                if "grid_convergence_deg" in d
                else 0.0
            )

        return HorizonProfile(
            azimuths=azimuths,
            bands=bands,
            observer_lat=observer_lat,
            observer_lon=observer_lon,
            light_domes=light_domes,
            light_peak_distances=light_peak_distances,
            resolved_mask=resolved_mask,
            terrain_mesh=terrain_mesh,
            resolved_radius_m=resolved_radius_m,
            schema_version=schema_version,
            representation_mode=representation_mode,
            geometry_source=geometry_source,
            geometry_id=geometry_id,
            elevation_source_ids=elevation_source_ids,
            effective_elevation_source_id=effective_elevation_source_id,
            elevation_source_status=elevation_source_status,
            geometry_crs=geometry_crs,
            observer_x=observer_x,
            observer_y=observer_y,
            grid_convergence_deg=grid_convergence_deg,
        )

    def covers_radius(self, requested_radius_m: float) -> bool:
        """Return false for legacy profiles whose coverage is unknown."""
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
        from pyproj import Transformer

        with PYPROJ_TRANSFORMER_LOCK:
            transformer = Transformer.from_crs(
                CRS_GEOGRAPHIC, CRS_TERRAIN_INTERNAL, always_xy=True
            )
        observer_x, observer_y = transformer.transform(
            float(observer_lon), float(observer_lat)
        )
    except Exception:
        pass
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


# ─────────────────────────────────────────────
#  Tile Index
# ─────────────────────────────────────────────


class TileIndex:
    """Indexes DEM .txt/.asc tiles by bounding box in projected coordinates."""

    def __init__(
        self,
        tiles_dir: str,
        patterns: list = ["*.asc", "*.txt", "*.npy"],
        callback=None,
    ):
        self.tiles_dir = tiles_dir
        self.patterns = patterns
        self.tiles: List[Dict] = []
        self.global_bbox = [np.inf, np.inf, -np.inf, -np.inf]
        self._spatial_cell_size = 1.0
        self._spatial_origin = (0.0, 0.0)
        self._spatial_cells: Dict[Tuple[int, int], Tuple[int, ...]] = {}
        self._build_index(callback)

    def _build_index(self, callback=None):
        files = []
        if os.path.isfile(self.tiles_dir):
            files = [self.tiles_dir]
        else:
            for pat in self.patterns:
                search_path = os.path.join(self.tiles_dir, pat)
                files.extend(glob.glob(search_path))

        print(
            f"[HorizonEngine] Indexing {len(files)} tiles from {self.tiles_dir} ({self.patterns})..."
        )

        if callback:
            callback(0, len(files), "Indexing files...")

        for i, fpath in enumerate(files):
            try:
                if fpath.endswith(".npy"):
                    # Parse bbox from filename: Y_(ymin_ymax)X_(xmin_xmax).npy
                    basename = os.path.basename(fpath)
                    bbox = self._parse_npy_filename(basename)
                    if bbox:
                        # For NPY tiles, we still need a cellulsize to sample.
                        # Assuming default 5.0m for ICGC tiles if not encoded in filename.
                        # Real NPY tiles should probably have their own header files.
                        self.tiles.append(
                            {
                                "path": fpath,
                                "header": {
                                    "NPY": True,
                                    "CELLSIZE": 5.0,
                                    "NCOLS": 1,
                                    "NROWS": 1,
                                },  # Minimal
                                "bbox": bbox,
                            }
                        )
                    else:
                        continue
                else:
                    header = self._read_header(fpath)
                    bbox = self._compute_bbox(header)
                    self.tiles.append(
                        {
                            "path": fpath,
                            "header": header,
                            "bbox": bbox,
                        }
                    )

                # Update global bbox
                self.global_bbox[0] = min(self.global_bbox[0], bbox[0])
                self.global_bbox[1] = min(self.global_bbox[1], bbox[1])
                self.global_bbox[2] = max(self.global_bbox[2], bbox[2])
                self.global_bbox[3] = max(self.global_bbox[3], bbox[3])

                # Report progress every 50 files
                if callback and i % 50 == 0:
                    callback(i, len(files), f"Indexing tile {i}/{len(files)}")

            except Exception as e:
                # print(f"[HorizonEngine] Skipping {fpath}: {e}")
                pass

        if callback:
            callback(
                len(files), len(files), f"Indexed {len(self.tiles)} tiles."
            )

        self._build_spatial_index()
        print(f"[HorizonEngine] Indexed {len(self.tiles)} valid tiles.")

    def _build_spatial_index(self) -> None:
        """Build a compact uniform-grid lookup without changing tile precedence."""

        if not self.tiles:
            self._spatial_cells = {}
            return
        widths = [
            max(0.0, float(tile["bbox"][2]) - float(tile["bbox"][0]))
            for tile in self.tiles
        ]
        heights = [
            max(0.0, float(tile["bbox"][3]) - float(tile["bbox"][1]))
            for tile in self.tiles
        ]
        dimensions = [
            value
            for value in widths + heights
            if np.isfinite(value) and value > 0
        ]
        self._spatial_cell_size = max(1.0, float(np.median(dimensions)))
        origin_x = float(self.global_bbox[0])
        origin_y = float(self.global_bbox[1])
        self._spatial_origin = (origin_x, origin_y)
        cells: Dict[Tuple[int, int], List[int]] = {}
        cell_size = self._spatial_cell_size
        for tile_index, tile in enumerate(self.tiles):
            xmin, ymin, xmax, ymax = (float(value) for value in tile["bbox"])
            ix0 = math.floor((xmin - origin_x) / cell_size)
            iy0 = math.floor((ymin - origin_y) / cell_size)
            # Include the cell touching an upper edge; exact bbox checks later
            # discard points that belong to the neighbouring tile.
            ix1 = math.floor((xmax - origin_x) / cell_size)
            iy1 = math.floor((ymax - origin_y) / cell_size)
            for iy in range(iy0, iy1 + 1):
                for ix in range(ix0, ix1 + 1):
                    cells.setdefault((ix, iy), []).append(tile_index)
        self._spatial_cells = {
            key: tuple(indices) for key, indices in cells.items()
        }

    def candidate_point_groups(
        self, x: np.ndarray, y: np.ndarray
    ) -> List[Tuple[Dict, np.ndarray]]:
        """Group flat point indices by possible tile, in index precedence order."""

        flat_x = np.asarray(x, dtype=np.float64).ravel()
        flat_y = np.asarray(y, dtype=np.float64).ravel()
        if flat_x.shape != flat_y.shape:
            raise ValueError("Spatial lookup coordinate shapes must match")
        if not self.tiles or flat_x.size == 0:
            return []

        origin_x, origin_y = self._spatial_origin
        cell_size = self._spatial_cell_size
        cell_x = np.floor((flat_x - origin_x) / cell_size).astype(np.int64)
        cell_y = np.floor((flat_y - origin_y) / cell_size).astype(np.int64)
        order = np.lexsort((cell_x, cell_y))
        sorted_x = cell_x[order]
        sorted_y = cell_y[order]
        boundaries = np.flatnonzero(
            (sorted_x[1:] != sorted_x[:-1]) | (sorted_y[1:] != sorted_y[:-1])
        ) + 1
        groups: Dict[int, List[np.ndarray]] = {}
        for positions in np.split(order, boundaries):
            if positions.size == 0:
                continue
            key = (int(cell_x[positions[0]]), int(cell_y[positions[0]]))
            for tile_index in self._spatial_cells.get(key, ()):
                tile = self.tiles[tile_index]
                xmin, ymin, xmax, ymax = tile["bbox"]
                selected = positions[
                    (flat_x[positions] >= xmin)
                    & (flat_x[positions] < xmax)
                    & (flat_y[positions] >= ymin)
                    & (flat_y[positions] < ymax)
                ]
                if selected.size:
                    groups.setdefault(tile_index, []).append(selected)
        return [
            (self.tiles[tile_index], np.concatenate(groups[tile_index]))
            for tile_index in sorted(groups)
        ]

    @staticmethod
    def _parse_npy_filename(
        name: str,
    ) -> Optional[Tuple[float, float, float, float]]:
        # Format: Y_(ymin_ymax)X_(xmin_xmax).npy
        try:
            import re

            base = name.rsplit(".npy", 1)[0]
            match = re.search(
                r"Y_\((-?\d+(?:\.\d+)?)_(-?\d+(?:\.\d+)?)\)"
                r"X_\((-?\d+(?:\.\d+)?)_(-?\d+(?:\.\d+)?)\)",
                base,
                re.IGNORECASE,
            )
            if match is None:
                return None

            y_min, y_max, x_min, x_max = map(float, match.groups())

            return (x_min, y_min, x_max, y_max)
        except:
            return None

    @staticmethod
    def _read_header(path: str) -> Dict:
        header = {}
        try:
            with open(path, "r") as f:
                for _ in range(6):
                    line = f.readline().strip()
                    if not line:
                        break
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].upper()
                        val = parts[1]
                        if key in [
                            "NCOLS",
                            "NROWS",
                            "XLLCORNER",
                            "YLLCORNER",
                            "XLLCENTER",
                            "YLLCENTER",
                            "CELLSIZE",
                            "NODATA_VALUE",
                        ]:
                            header[key] = float(val)

            if "NCOLS" in header:
                header["NCOLS"] = int(header["NCOLS"])
            if "NROWS" in header:
                header["NROWS"] = int(header["NROWS"])
        except:
            pass
        return header

    @staticmethod
    def _compute_bbox(h: Dict) -> Tuple[float, float, float, float]:
        s = h.get("CELLSIZE", 5.0)
        half = s / 2.0

        if "XLLCENTER" in h:
            xmin = h["XLLCENTER"] - half
            ymin = h["YLLCENTER"] - half
        elif "XLLCORNER" in h:
            xmin = h["XLLCORNER"]
            ymin = h["YLLCORNER"]
        else:
            raise ValueError(
                f"Header missing XLLCENTER or XLLCORNER: {h.keys()}"
            )

        xmax = xmin + h["NCOLS"] * s
        ymax = ymin + h["NROWS"] * s
        return (xmin, ymin, xmax, ymax)

    def find_tile(self, x: float, y: float) -> Optional[Dict]:
        """Returns the first tile containing (x, y)."""
        # Fast bounds check
        if (
            x < self.global_bbox[0]
            or y < self.global_bbox[1]
            or x > self.global_bbox[2]
            or y > self.global_bbox[3]
        ):
            return None

        # O(N) linear scan (usually fine for ~1000 tiles)
        for t in self.tiles:
            xmin, ymin, xmax, ymax = t["bbox"]
            if xmin <= x < xmax and ymin <= y < ymax:
                return t
        return None

    def get_overlapping_tiles(
        self, cx: float, cy: float, radius: float
    ) -> List[Dict]:
        """Return all tiles within radius of (cx, cy)."""
        matches = []
        r2 = radius * radius
        # Sort by distance (closest first)
        candidates = []

        for t in self.tiles:
            xmin, ymin, xmax, ymax = t["bbox"]
            # Distance from point to rectangle (squared)
            dx = max(xmin - cx, 0, cx - xmax)
            dy = max(ymin - cy, 0, cy - ymax)
            dist_sq = dx * dx + dy * dy
            if dist_sq <= r2:
                candidates.append((dist_sq, t))

        candidates.sort(key=lambda x: x[0])
        return [c[1] for c in candidates]


# ─────────────────────────────────────────────
#  Tile Cache
# ─────────────────────────────────────────────


class TileCache:
    """LRU cache for loaded DEM grids, with .npy binary caching on disk. Thread-safe."""

    def __init__(self, capacity: int = 16, max_bytes: int | None = None):
        self.capacity = capacity
        self.max_bytes = max(
            0,
            int(
                DEFAULT_PERFORMANCE_BUDGET.dem_bytes
                if max_bytes is None
                else max_bytes
            ),
        )
        self.cache: OrderedDict = OrderedDict()
        self._cache_sizes: dict[str, int] = {}
        self._cache_bytes = 0
        self._pinned_paths: set[str] = set()
        self._lock = threading.Lock()
        self.cache_hits = 0
        self.cache_misses = 0
        self.bytes_read = 0
        # Different tiles can be read in parallel.  Bounded striped locks keep
        # same-path materialisation exclusive without a lock object per tile.
        self._tile_io_lock = threading.Lock()
        self._tile_io_locks = tuple(threading.Lock() for _ in range(64))
        self._packaged_cache_root = (
            Path(__file__).resolve().parents[1] / "data" / "terrain_cache"
        )

    @property
    def resident_bytes(self) -> int:
        with self._lock:
            return int(self._cache_bytes)

    def prioritize(self, tile_infos: Iterable[Dict]) -> None:
        """Make non-candidate tiles the first entries evicted by the LRU."""
        paths = [
            str(tile.get("path", ""))
            for tile in tile_infos
            if tile.get("path")
        ]
        if not paths:
            return
        with self._lock:
            for path in paths:
                if path in self.cache:
                    self.cache.move_to_end(path)

    def pin(self, tile_infos: Iterable[Dict], max_entries: int = 32) -> None:
        """Keep a bounded near-field working set inside the byte budget."""
        paths = [
            str(tile.get("path", ""))
            for tile in tile_infos
            if tile.get("path")
        ][: max(0, int(max_entries))]
        with self._lock:
            self._pinned_paths.update(paths)
            for path in paths:
                if path in self.cache:
                    self.cache.move_to_end(path)

    def _io_lock_for(self, path: str) -> "threading.Lock":
        normalized = os.path.normcase(os.path.abspath(path))
        return self._tile_io_locks[hash(normalized) % len(self._tile_io_locks)]

    def _store(self, path: str, value: tuple[np.ndarray, Dict]) -> None:
        size = int(value[0].nbytes)
        with self._lock:
            old = self.cache.pop(path, None)
            if old is not None:
                self._cache_bytes -= self._cache_sizes.pop(path, 0)
            if self.max_bytes <= 0 or size > self.max_bytes:
                return
            self.cache[path] = value
            self._cache_sizes[path] = size
            self._cache_bytes += size
            while self.cache and self._cache_bytes > self.max_bytes:
                old_path = next(
                    (
                        candidate
                        for candidate in self.cache
                        if candidate not in self._pinned_paths
                    ),
                    None,
                )
                if old_path is None:
                    break
                self.cache.pop(old_path)
                self._cache_bytes -= self._cache_sizes.pop(old_path, 0)

    def clear(self) -> None:
        with self._lock:
            self.cache.clear()
            self._cache_sizes.clear()
            self._cache_bytes = 0
            self._pinned_paths.clear()

    def load(
        self, tile_info: Dict
    ) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        """Executa el metode load de la classe TileCache.

        Par?metres:
        - tile_info (Dict): Valor del parametre 'tile_info'.

        Retorna:
        - Tuple[Optional[np.ndarray], Optional[Dict]]: Valor retornat pel metode.
        """
        path = tile_info["path"]
        with self._lock:
            if path in self.cache:
                self.cache_hits += 1
                self.cache.move_to_end(path)
                return self.cache[path]
            self.cache_misses += 1

        header = tile_info["header"]
        filename = os.path.basename(path)
        base_name, ext = os.path.splitext(filename)

        # 1. Try packaged cache (TerraLab/data/terrain_cache)
        # Use deterministic path computation without runtime mkdir side-effects.
        packaged_npy = str(
            self._packaged_cache_root / f"{base_name}.npy"
        )

        # 2. Try the library-owned materialized cache.  An already-existing
        # adjacent cache may be consumed even for an external source, but new
        # conversions are only published in the selected library.
        adjacent_npy = os.path.splitext(path)[0] + ".npy"
        materialized_npy = str(tile_info.get("materialized_path", "") or "")

        candidate_npy = None
        if header.get("NPY") and os.path.exists(path):
            candidate_npy = path
        elif materialized_npy and os.path.exists(materialized_npy):
            candidate_npy = materialized_npy
        elif os.path.exists(packaged_npy):
            candidate_npy = packaged_npy
        elif os.path.exists(adjacent_npy):
            candidate_npy = adjacent_npy

        if candidate_npy:
            try:
                with self._io_lock_for(candidate_npy):
                    # Stability-first on Windows/Python 3.13:
                    # avoid memmap-backed arrays loaded from worker threads.
                    data = np.load(
                        candidate_npy,
                        mmap_mode=None,
                        allow_pickle=False,
                    )
                    # Ensure independent in-memory buffer (no file-backed view).
                    data = np.asarray(data, dtype=np.float32)
                self.bytes_read += int(data.nbytes)
                self._store(path, (data, header))
                # print(f"[HorizonEngine] Loaded cached npy: {os.path.basename(candidate_npy)}")
                return data, header
            except Exception as e:
                print(
                    f"[HorizonEngine] Failed to load cached npy {candidate_npy}: {e}"
                )
                pass

        # Parse text/asc grid
        try:
            # Determine header size
            header_lines = 0
            with open(path, "r") as f:
                for _ in range(10):
                    line = f.readline()
                    if not line:
                        break
                    parts = line.split()
                    if not parts:
                        continue
                    if parts[0].upper() in [
                        "NCOLS",
                        "NROWS",
                        "XLLCORNER",
                        "YLLCORNER",
                        "XLLCENTER",
                        "YLLCENTER",
                        "CELLSIZE",
                        "NODATA_VALUE",
                        "DX",
                        "DY",
                    ]:
                        header_lines += 1
                    else:
                        break

            output_npy = materialized_npy or (
                adjacent_npy if bool(tile_info.get("allow_adjacent_cache", False)) else ""
            )

            print(
                f"[HorizonEngine] Parsing with Pandas: {os.path.basename(path)}..."
            )
            try:
                with self._io_lock_for(path):
                    import pandas as pd

                    df = pd.read_csv(
                        path,
                        skiprows=header_lines,
                        sep=r"\s+",
                        header=None,
                        dtype=np.float32,
                        engine="c",
                    )
                    data_raw = df.values.flatten()
            except Exception as e:
                print(f"[HorizonEngine] Error parsing {path} with Pandas: {e}")
                return None, None

            nodata = header.get("NODATA_VALUE", -9999)
            nrows = int(header["NROWS"])
            ncols = int(header["NCOLS"])

            expected = nrows * ncols
            if data_raw.size != expected:
                if data_raw.size > expected:
                    data_raw = data_raw[:expected]
                else:
                    data_raw = np.pad(
                        data_raw,
                        (0, expected - data_raw.size),
                        constant_values=nodata,
                    )

            data = data_raw.reshape((nrows, ncols)).astype(np.float32)

            # Save binary cache to original location if possible
            try:
                if not output_npy:
                    raise OSError("No writable materialization target")
                os.makedirs(os.path.dirname(output_npy), exist_ok=True)
                with self._io_lock_for(output_npy):
                    temp_output_npy = f"{output_npy}.tmp.npy"
                    np.save(temp_output_npy, data)
                    os.replace(temp_output_npy, output_npy)
                sidecar = str(tile_info.get("materialized_metadata_path", "") or "")
                if sidecar:
                    import json

                    with open(sidecar, "w", encoding="utf-8") as handle:
                        json.dump(
                            {"source": path, "cache": output_npy, "header": header},
                            handle,
                            ensure_ascii=False,
                            indent=2,
                        )
                # print(f"[HorizonEngine] Saved cache: {os.path.basename(output_npy)}")
            except:
                pass

            self.bytes_read += int(data.nbytes)
            self._store(path, (data, header))

            return data, header

        except Exception as e:
            print(f"[HorizonEngine] Error loading tile {path}: {e}")
            import traceback

            traceback.print_exc()
            return None, None


# ─────────────────────────────────────────────
#  DEM Sampler
# ─────────────────────────────────────────────


class DemSampler:
    """Samples elevation from DEM tiles with bilinear interpolation."""

    def __init__(self, tile_index: TileIndex, tile_cache: TileCache):
        self.index = tile_index
        self.cache = tile_cache
        self.last_tile: Optional[Dict] = None
        self._transformer_inv = None  # Lazy loaded if needed

    def get_elevation(self, x: float, y: float) -> Optional[float]:
        """Alias for sample() to maintain compatibility with HorizonBaker."""
        return self.sample(x, y)

    def transform_coordinates_inverse(
        self, x: float, y: float
    ) -> Tuple[float, float]:
        """
        Convert terrain internal coordinates back to geographic lat/lon.

        Input CRS:
            - `x`, `y` in terrain internal CRS (`EPSG:25831`).
        Output CRS:
            - `(lat, lon)` in `EPSG:4326`.
        """
        from pyproj import Transformer

        if self._transformer_inv is None:
            with PYPROJ_TRANSFORMER_LOCK:
                self._transformer_inv = Transformer.from_crs(
                    CRS_TERRAIN_INTERNAL, CRS_GEOGRAPHIC, always_xy=True
                )
        lon, lat = self._transformer_inv.transform(x, y)
        return lat, lon

    def sample(self, x: float, y: float) -> Optional[float]:
        # Spatial coherence optimisation: check last tile first
        """Executa el metode sample de la classe DemSampler.

        Par?metres:
        - x (float): Valor del parametre 'x'.
        - y (float): Valor del parametre 'y'.

        Retorna:
        - Optional[float]: Valor retornat pel metode.
        """
        tile = None
        # Optimization: Track if we are in a "void" area to avoid searching the index
        if self.last_tile == "NONE":
            # We need to know when we exit the void.
            # For now, let's just re-find if it's not the last state.
            pass

        if self.last_tile and self.last_tile != "NONE":
            xmin, ymin, xmax, ymax = self.last_tile["bbox"]
            if xmin <= x < xmax and ymin <= y < ymax:
                tile = self.last_tile

        if tile is None:
            tile = self.index.find_tile(x, y)
            self.last_tile = tile if tile else "NONE"

        if tile is None or tile == "NONE":
            return None

        # Robust unpacking
        res = self.cache.load(tile)
        if res is None:
            # Should not happen if load returns (None, None)
            # print(f"[HorizonEngine] CRITICAL: load returned None for {tile['path']}")
            return None

        data, h = res

        if data is None:
            return None

        # Handle NPY tiles where LL mapping might be different or missing
        if h.get("NPY"):
            # For NPY tiles, we assume bbox is already correct from filename
            # and it's a regular grid.
            xmin, ymin, xmax, ymax = tile["bbox"]
            nrows, ncols = data.shape
            sx = (xmax - xmin) / max(1, ncols)
            sy = (ymax - ymin) / max(1, nrows)
            grid_x = (x - xmin) / sx
            # Y in arrays is usually top-to-bottom
            grid_y_from_top = (ymax - y) / sy
            grid_row = grid_y_from_top
        else:
            # Robust header handling: default to 5.0m if missing (standard for ICGC tiles)
            s = h.get("CELLSIZE", 5.0)
            if "XLLCENTER" in h:
                x0 = h["XLLCENTER"]
                y0 = h["YLLCENTER"]
            else:
                x0 = h.get("XLLCORNER", 0.0) + s / 2
                y0 = h.get("YLLCORNER", 0.0) + s / 2

            grid_x = (x - x0) / s
            grid_y_from_bottom = (y - y0) / s
            grid_row = (h.get("NROWS", data.shape[0]) - 1) - grid_y_from_bottom

        c0 = int(math.floor(grid_x))
        r0 = int(math.floor(grid_row))

        nrows, ncols = data.shape
        c0 = max(0, min(c0, ncols - 1))
        r0 = max(0, min(r0, nrows - 1))

        if 0 <= r0 < nrows - 1 and 0 <= c0 < ncols - 1:
            dx = grid_x - c0
            dy = grid_row - r0

            v00 = float(data[r0, c0])
            v01 = float(data[r0, c0 + 1])
            v10 = float(data[r0 + 1, c0])
            v11 = float(data[r0 + 1, c0 + 1])

            top = v00 * (1 - dx) + v01 * dx
            bot = v10 * (1 - dx) + v11 * dx
            val = top * (1 - dy) + bot * dy
            return val
        else:
            return float(data[r0, c0])


# ─────────────────────────────────────────────
#  Horizon Baker
# ─────────────────────────────────────────────


@dataclass(frozen=True)
class PolarElevationField:
    """Bounded polar samples shared by horizon and relief mesh."""

    azimuths: np.ndarray
    distances: np.ndarray
    elevations: np.ndarray
    valid: np.ndarray
    observer_x: float
    observer_y: float
    observer_ground: float
    d_max: float
    delta_az_deg: float

    def __post_init__(self) -> None:
        azimuths = np.asarray(self.azimuths, dtype=np.float32)
        distances = np.asarray(self.distances, dtype=np.float32)
        elevations = np.asarray(self.elevations, dtype=np.float32)
        valid = np.asarray(self.valid, dtype=bool)
        if elevations.shape != (distances.size, azimuths.size):
            raise ValueError("Polar elevation dimensions are inconsistent")
        if valid.shape != elevations.shape:
            raise ValueError("Polar validity mask is inconsistent")
        for array in (azimuths, distances, elevations, valid):
            array.setflags(write=False)
        object.__setattr__(self, "azimuths", azimuths)
        object.__setattr__(self, "distances", distances)
        object.__setattr__(self, "elevations", elevations)
        object.__setattr__(self, "valid", valid)


class HorizonBaker:
    """
    Raycasts from observer position to compute horizon elevation angles.
    When a ray exits available DEM coverage, it stops and keeps
    whatever silhouette data was already gathered.
    """

    def __init__(
        self,
        provider,
        eye_height: float = 1.7,
        R: float = R_EARTH,
        grid_convergence_deg: float = 0.0,
        sampling_settings: TerrainSamplingSettings | None = None,
        sampling_pixels_per_radian: float = 1000.0,
    ):
        self.provider = provider
        self.eye_height = eye_height
        self.R = R
        self.grid_convergence_deg = float(grid_convergence_deg)
        self.sampling_settings = sampling_settings or TerrainSamplingSettings(
            adaptive_sampling_enabled=False
        )
        self.sampling_pixels_per_radian = max(
            1.0, float(sampling_pixels_per_radian)
        )
        self._ray_miss_exit_threshold = 8
        self._ray_iteration_guard = 1_000_000
        self._vector_azimuth_batch = 64
        self._last_polar_field: PolarElevationField | None = None

    @staticmethod
    def _requested_raycast_backend(azimuth_count: int) -> str:
        try:
            from TerraLab.common.utils import get_config_value

            requested = str(
                get_config_value("terrain.raycast_backend", "auto") or "auto"
            ).strip().lower()
        except Exception:
            requested = "auto"
        if requested not in {"auto", "single", "threads", "processes"}:
            requested = "auto"
        # The segmented NumPy reducer is substantially faster than IPC on the
        # reference workload. Explicit parallel modes remain opt-in until their
        # measured speedup clears the acceptance threshold.
        return requested

    def _raster_io_metrics(self) -> dict[str, int]:
        """Collect cumulative block-cache counters without coupling to a provider."""

        pending = [self.provider]
        seen: set[int] = set()
        metrics = {
            "bytes_read": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "selection_ns": 0,
            "interpolation_ns": 0,
            "candidate_tiles": 0,
            "loaded_tiles": 0,
            "sampled_points": 0,
        }
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            pending.extend(getattr(item, "providers", ()) or ())
            for child_name in ("cache",):
                child = getattr(item, child_name, None)
                if child is not None:
                    pending.append(child)
            datasets = getattr(item, "_datasets", ()) or ()
            pending.extend(datasets)
            for name in metrics:
                attribute = {
                    "selection_ns": "sample_selection_ns",
                    "interpolation_ns": "sample_interpolation_ns",
                    "candidate_tiles": "sample_candidate_tiles",
                    "loaded_tiles": "sample_loaded_tiles",
                    "sampled_points": "sampled_points",
                }.get(name, name)
                metrics[name] += int(getattr(item, attribute, 0) or 0)
        return metrics

    @staticmethod
    def _next_step_distance(
        d: float, step_m: float, near_factor: float = 1.5
    ) -> float:
        """Return the next ray-march distance using the adaptive stepping policy."""
        if d < step_m:
            return min(d * near_factor, step_m)
        if d < 3_000:
            return d + step_m
        if d < 15_000:
            return d + step_m * 2.0
        if d < 50_000:
            return d + step_m * 4.0
        return d + step_m * 8.0

    @classmethod
    def _adaptive_distances(cls, step_m: float, d_max: float) -> np.ndarray:
        """Materialize the exact legacy distance progression once per bake."""

        distances = []
        distance = 0.5
        guard = 0
        while distance < float(d_max):
            distances.append(float(distance))
            guard += 1
            if guard > 1_000_000:
                raise RuntimeError("Ray distance iteration guard reached")
            next_distance = cls._next_step_distance(distance, float(step_m))
            if not np.isfinite(next_distance) or next_distance <= distance:
                raise ValueError("Ray distance sequence does not progress")
            distance = float(next_distance)
        return np.asarray(distances, dtype=np.float64)

    def _supports_batch_sampling(self) -> bool:
        """Return true only for providers overriding the scalar compatibility loop."""

        if not PERFORMANCE_FLAGS.raycast_vectorized:
            return False
        for provider_type in type(self.provider).__mro__:
            if provider_type.__name__ == "RasterProvider":
                return False
            namespace = getattr(provider_type, "__dict__", {})
            if "sample_elevation" in namespace or "sample_elevations" in namespace:
                return True
        return False

    def _sample_provider_batch(
        self, x: np.ndarray, y: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        sampler = getattr(self.provider, "sample_elevation", None)
        if callable(sampler):
            batch = sampler(x, y)
        else:
            sampler = getattr(self.provider, "sample_elevations", None)
            if not callable(sampler):
                raise TypeError("Provider has no batch elevation method")
            try:
                batch = sampler(x, y, input_crs=CRS_TERRAIN_INTERNAL)
            except TypeError:
                batch = sampler(x, y)
        if hasattr(batch, "values") and hasattr(batch, "valid"):
            raw_values = batch.values
            raw_valid = batch.valid
        else:
            raw_values, raw_valid = batch[:2]
        values = np.asarray(raw_values, dtype=np.float32)
        valid = np.asarray(raw_valid, dtype=bool)
        if values.shape != x.shape or valid.shape != x.shape:
            raise ValueError("Provider batch result shape mismatch")
        valid &= np.isfinite(values)
        return values, valid

    def _refine_azimuth_chunk(
        self,
        *,
        az_indices: np.ndarray,
        obs_x: float,
        obs_y: float,
        h_eye_abs: float,
        base_distances: np.ndarray,
        base_elevations: np.ndarray,
        base_valid: np.ndarray,
        band_defs: List[Dict],
        sin_az: np.ndarray,
        cos_az: np.ndarray,
    ) -> tuple[list[dict], dict[str, float | int]]:
        """Refine independent ray profiles while batching midpoint DEM reads."""

        started_ns = __import__("time").perf_counter_ns()
        settings = self.sampling_settings
        profiles = [
            {
                "distances": np.asarray(base_distances, dtype=np.float64).copy(),
                "elevations": np.asarray(base_elevations[index], dtype=np.float64).copy(),
                "valid": np.asarray(base_valid[index], dtype=bool).copy(),
            }
            for index in range(len(az_indices))
        ]
        queried_samples = 0
        valid_queries = 0
        maximum_errors = np.zeros(3, dtype=np.float64)
        minimum_step = float(settings.sampling_near_step_m)

        for _depth in range(settings.sampling_max_subdivision_depth):
            candidate_rays = []
            candidate_intervals = []
            candidate_d0 = []
            candidate_d1 = []
            candidate_h0 = []
            candidate_h1 = []
            candidate_v0 = []
            candidate_v1 = []
            for ray_index, profile in enumerate(profiles):
                remaining = (
                    settings.sampling_max_samples_per_ray
                    - profile["distances"].size
                )
                if remaining <= 0:
                    continue
                intervals = np.flatnonzero(
                    np.diff(profile["distances"]) > minimum_step + 1e-9
                )
                for interval in intervals:
                    candidate_rays.append(ray_index)
                    candidate_intervals.append(int(interval))
                    candidate_d0.append(profile["distances"][interval])
                    candidate_d1.append(profile["distances"][interval + 1])
                    candidate_h0.append(profile["elevations"][interval])
                    candidate_h1.append(profile["elevations"][interval + 1])
                    candidate_v0.append(profile["valid"][interval])
                    candidate_v1.append(profile["valid"][interval + 1])
            if not candidate_rays:
                break

            candidate_rays = np.asarray(candidate_rays, dtype=np.int32)
            candidate_intervals = np.asarray(candidate_intervals, dtype=np.int32)
            d0 = np.asarray(candidate_d0, dtype=np.float64)
            d1 = np.asarray(candidate_d1, dtype=np.float64)
            midpoint_distances = 0.5 * (d0 + d1)
            global_azimuth_indices = np.asarray(az_indices, dtype=np.int32)[
                candidate_rays
            ]
            x = (
                float(obs_x)
                + sin_az[global_azimuth_indices] * midpoint_distances
            )
            y = (
                float(obs_y)
                + cos_az[global_azimuth_indices] * midpoint_distances
            )
            midpoint_elevations, midpoint_valid = self._sample_provider_batch(x, y)
            midpoint_elevations = np.asarray(midpoint_elevations, dtype=np.float64)
            midpoint_valid = np.asarray(midpoint_valid, dtype=bool)
            queried_samples += int(midpoint_distances.size)
            valid_queries += int(np.count_nonzero(midpoint_valid))

            evaluation = evaluate_refinement_intervals(
                d0,
                np.asarray(candidate_h0),
                np.asarray(candidate_v0),
                d1,
                np.asarray(candidate_h1),
                np.asarray(candidate_v1),
                midpoint_distances,
                midpoint_elevations,
                midpoint_valid,
                observer_eye_elevation_m=float(h_eye_abs),
                earth_radius_m=float(self.R),
                pixels_per_radian=float(self.sampling_pixels_per_radian),
                settings=settings,
            )
            maximum_errors = np.maximum(
                maximum_errors,
                (
                    float(np.max(evaluation.elevation_error_m, initial=0.0)),
                    float(np.max(evaluation.projected_error_px, initial=0.0)),
                    float(np.max(evaluation.slope_delta_deg, initial=0.0)),
                ),
            )
            selected_by_ray: dict[int, np.ndarray] = {}
            for ray_index in range(len(profiles)):
                positions = np.flatnonzero(
                    (candidate_rays == ray_index) & evaluation.should_refine
                )
                remaining = (
                    settings.sampling_max_samples_per_ray
                    - profiles[ray_index]["distances"].size
                )
                if positions.size > remaining:
                    order = np.argsort(
                        evaluation.score[positions], kind="stable"
                    )[::-1]
                    positions = positions[order[:remaining]]
                if positions.size:
                    selected_by_ray[ray_index] = positions
            if not selected_by_ray:
                break

            for ray_index, positions in selected_by_ray.items():
                profile = profiles[ray_index]
                insertion_by_interval = {
                    int(candidate_intervals[position]): (
                        float(midpoint_distances[position]),
                        float(midpoint_elevations[position]),
                        bool(midpoint_valid[position]),
                    )
                    for position in positions
                }
                next_distances = []
                next_elevations = []
                next_valid = []
                for interval in range(profile["distances"].size - 1):
                    next_distances.append(profile["distances"][interval])
                    next_elevations.append(profile["elevations"][interval])
                    next_valid.append(profile["valid"][interval])
                    if interval in insertion_by_interval:
                        distance, elevation, is_valid = insertion_by_interval[
                            interval
                        ]
                        next_distances.append(distance)
                        next_elevations.append(elevation)
                        next_valid.append(is_valid)
                next_distances.append(profile["distances"][-1])
                next_elevations.append(profile["elevations"][-1])
                next_valid.append(profile["valid"][-1])
                profile["distances"] = np.asarray(next_distances, dtype=np.float64)
                profile["elevations"] = np.asarray(next_elevations, dtype=np.float64)
                profile["valid"] = np.asarray(next_valid, dtype=bool)

        band_results = [
            {
                "angles": np.full(len(profiles), -np.inf, dtype=np.float32),
                "dists": np.zeros(len(profiles), dtype=np.float32),
                "heights": np.zeros(len(profiles), dtype=np.float32),
                "surface_angles": np.full(
                    len(profiles), -np.inf, dtype=np.float32
                ),
                "surface_dists": np.zeros(len(profiles), dtype=np.float32),
                "surface_heights": np.zeros(len(profiles), dtype=np.float32),
            }
            for _definition in band_defs
        ]
        for ray_index, profile in enumerate(profiles):
            distances = profile["distances"]
            elevations = profile["elevations"]
            effective_valid = profile["valid"].copy()
            threshold = int(self._ray_miss_exit_threshold)
            if effective_valid.size >= threshold:
                missing_windows = np.lib.stride_tricks.sliding_window_view(
                    ~effective_valid, threshold
                )
                exits = np.flatnonzero(np.all(missing_windows, axis=1))
                if exits.size:
                    effective_valid[int(exits[0]) + threshold - 1 :] = False
            angles = apparent_elevation_radians(
                elevations,
                distances,
                float(h_eye_abs),
                float(self.R),
            )
            angles = np.where(effective_valid, angles, -np.inf)
            for band_index, definition in enumerate(band_defs):
                in_band = (
                    effective_valid
                    & (distances >= float(definition["min"]))
                    & (distances < float(definition["max"]))
                )
                indices = np.flatnonzero(in_band)
                if indices.size == 0:
                    continue
                best = int(indices[np.argmax(angles[indices])])
                last = int(indices[-1])
                target = band_results[band_index]
                target["angles"][ray_index] = angles[best]
                target["dists"][ray_index] = distances[best]
                target["heights"][ray_index] = elevations[best]
                target["surface_angles"][ray_index] = angles[last]
                target["surface_dists"][ray_index] = distances[last]
                target["surface_heights"][ray_index] = elevations[last]

        elapsed_ns = __import__("time").perf_counter_ns() - started_ns
        return band_results, {
            "refinement_ns": int(elapsed_ns),
            "refinement_queries": int(queried_samples),
            "refinement_valid_queries": int(valid_queries),
            "selected_samples": int(
                sum(profile["distances"].size for profile in profiles)
            ),
            "maximum_elevation_error_m": float(maximum_errors[0]),
            "maximum_projected_error_px": float(maximum_errors[1]),
            "maximum_slope_delta_deg": float(maximum_errors[2]),
        }

    def _sample_azimuth_chunk_vectorized(
        self,
        *,
        az_indices: np.ndarray,
        obs_x: float,
        obs_y: float,
        h_eye_abs: float,
        ray_distances: np.ndarray,
        sample_distances: np.ndarray,
        ray_distance_indices: np.ndarray,
        band_defs: List[Dict],
        band_slices: Optional[List[Tuple[int, int]]] = None,
        sin_az: np.ndarray,
        cos_az: np.ndarray,
        light_sampler=None,
    ) -> dict:
        """Sample and reduce one <=64-azimuth matrix without Python point loops."""

        phase_started_ns = __import__("time").perf_counter_ns()
        az_indices = np.asarray(az_indices, dtype=np.int32)
        local_sin = np.asarray(sin_az[az_indices], dtype=np.float64)[:, None]
        local_cos = np.asarray(cos_az[az_indices], dtype=np.float64)[:, None]
        distances_2d = np.asarray(sample_distances, dtype=np.float64)[None, :]
        x_all = float(obs_x) + local_sin * distances_2d
        y_all = float(obs_y) + local_cos * distances_2d
        coordinate_ns = __import__("time").perf_counter_ns() - phase_started_ns
        provider_started_ns = __import__("time").perf_counter_ns()
        elevations_all, valid_all = self._sample_provider_batch(x_all, y_all)
        provider_ns = __import__("time").perf_counter_ns() - provider_started_ns

        angle_started_ns = __import__("time").perf_counter_ns()
        elevations = elevations_all[:, ray_distance_indices]
        valid = valid_all[:, ray_distance_indices]
        threshold = int(self._ray_miss_exit_threshold)
        if valid.shape[1] >= threshold:
            windows = np.lib.stride_tricks.sliding_window_view(
                ~valid, threshold, axis=1
            )
            miss_runs = np.all(windows, axis=-1)
            has_exit = np.any(miss_runs, axis=1)
            first_start = np.argmax(miss_runs, axis=1)
            # The legacy loop breaks while handling the eighth miss.  No valid
            # point at or after that position may affect a reduction.
            stop = np.where(
                has_exit, first_start + threshold - 1, valid.shape[1]
            )
            before_exit = np.arange(valid.shape[1])[None, :] < stop[:, None]
            effective_valid = valid & before_exit
        else:
            effective_valid = valid

        ray_d = np.asarray(ray_distances, dtype=np.float64)
        angles = apparent_elevation_radians(
            elevations.astype(np.float64),
            ray_d[None, :],
            float(h_eye_abs),
            float(self.R),
        )
        angles = np.where(effective_valid, angles, -np.inf)
        angle_ns = __import__("time").perf_counter_ns() - angle_started_ns

        if band_slices is None:
            band_slices = [
                (
                    int(np.searchsorted(ray_d, float(definition["min"]), side="left")),
                    int(np.searchsorted(ray_d, float(definition["max"]), side="left")),
                )
                for definition in band_defs
            ]

        band_started_ns = __import__("time").perf_counter_ns()
        band_results = []
        rows = np.arange(len(az_indices))
        for start, stop in band_slices:
            segment_valid = effective_valid[:, start:stop]
            segment_angles = angles[:, start:stop]
            segment_elevations = elevations[:, start:stop]
            segment_distances = ray_d[start:stop]
            if stop <= start:
                empty_float = np.zeros(len(az_indices), dtype=np.float32)
                empty_angle = np.full(len(az_indices), -np.inf, dtype=np.float32)
                band_results.append(
                    {
                        "angles": empty_angle,
                        "dists": empty_float.copy(),
                        "heights": empty_float.copy(),
                        "surface_angles": empty_angle.copy(),
                        "surface_dists": empty_float.copy(),
                        "surface_heights": empty_float.copy(),
                    }
                )
                continue
            in_band = segment_valid
            has_value = np.any(in_band, axis=1)
            band_angles = np.where(in_band, segment_angles, -np.inf)
            best_index = np.argmax(band_angles, axis=1)
            best_angle = np.where(
                has_value,
                band_angles[rows, best_index],
                -np.inf,
            )
            reverse_last = np.argmax(in_band[:, ::-1], axis=1)
            last_index = in_band.shape[1] - 1 - reverse_last
            safe_last = np.maximum(last_index, 0)
            band_results.append(
                {
                    "angles": best_angle,
                    "dists": np.where(has_value, segment_distances[best_index], 0.0),
                    "heights": np.where(
                        has_value,
                        segment_elevations[rows, best_index],
                        0.0,
                    ),
                    "surface_angles": np.where(
                        has_value,
                        segment_angles[rows, safe_last],
                        -np.inf,
                    ),
                    "surface_dists": np.where(
                        has_value, segment_distances[safe_last], 0.0
                    ),
                    "surface_heights": np.where(
                        has_value,
                        segment_elevations[rows, safe_last],
                        0.0,
                    ),
                }
            )
        band_ns = __import__("time").perf_counter_ns() - band_started_ns

        light_started_ns = __import__("time").perf_counter_ns()
        light_domes = np.zeros(len(az_indices), dtype=np.float32)
        light_peak_distances = np.zeros(len(az_indices), dtype=np.float32)
        max_radiance = np.zeros(len(az_indices), dtype=np.float32)
        if light_sampler is not None:
            prefix_max = np.maximum.accumulate(angles, axis=1)
            x_ray = x_all[:, ray_distance_indices]
            y_ray = y_all[:, ray_distance_indices]
            candidate_rows = []
            candidate_columns = []
            for local_index in range(len(az_indices)):
                last_light_distance = 0.0
                for distance_index in np.flatnonzero(effective_valid[local_index]):
                    distance = float(ray_d[distance_index])
                    if distance - last_light_distance < 2000.0:
                        continue
                    last_light_distance = distance
                    candidate_rows.append(local_index)
                    candidate_columns.append(distance_index)
            if candidate_rows:
                rows_arr = np.asarray(candidate_rows, dtype=np.int32)
                cols_arr = np.asarray(candidate_columns, dtype=np.int32)
                radiances = self._sample_light_radiance_batch(
                    light_sampler,
                    x_ray[rows_arr, cols_arr],
                    y_ray[rows_arr, cols_arr],
                ).reshape(-1)
                visible_light = (
                    (radiances > 0.1)
                    & (
                        angles[rows_arr, cols_arr]
                        > prefix_max[rows_arr, cols_arr] - 0.17
                    )
                )
                contributions = np.where(
                    visible_light,
                    radiances
                    * (1.0 / np.maximum(1.0, ray_d[cols_arr] / 1000.0))
                    * 20.0,
                    0.0,
                )
                np.add.at(light_domes, rows_arr, contributions.astype(np.float32))
                for local_index in range(len(az_indices)):
                    local_positions = np.flatnonzero(
                        (rows_arr == local_index) & visible_light
                    )
                    if local_positions.size == 0:
                        continue
                    best = local_positions[np.argmax(radiances[local_positions])]
                    max_radiance[local_index] = radiances[best]
                    light_peak_distances[local_index] = ray_d[cols_arr[best]]
        light_ns = __import__("time").perf_counter_ns() - light_started_ns

        return {
            "elevations": elevations_all,
            "valid": valid_all,
            "bands": band_results,
            "light_domes": light_domes,
            "light_peak_distances": light_peak_distances,
            "timings_ns": {
                "coordinates": int(coordinate_ns),
                "provider": int(provider_ns),
                "angles": int(angle_ns),
                "bands": int(band_ns),
                "light": int(light_ns),
            },
        }

    @staticmethod
    def _build_band_buffers(n_az: int, band_defs: List[Dict]) -> List[Dict]:
        bands = []
        for bd in band_defs:
            bands.append(
                {
                    "id": bd["id"],
                    "min": bd["min"],
                    "max": bd["max"],
                    "angles": np.full(n_az, -np.inf, dtype=np.float32),
                    "dists": np.zeros(n_az, dtype=np.float32),
                    "heights": np.zeros(n_az, dtype=np.float32),
                    "surface_angles": np.full(n_az, -np.inf, dtype=np.float32),
                    "surface_dists": np.zeros(n_az, dtype=np.float32),
                    "surface_heights": np.zeros(n_az, dtype=np.float32),
                }
            )
        return bands

    def _sample_single_azimuth(
        self,
        az_index: int,
        obs_x: float,
        obs_y: float,
        h_eye_abs: float,
        step_m: float,
        d_max: float,
        bands: List[Dict],
        sin_az: np.ndarray,
        cos_az: np.ndarray,
        light_domes: np.ndarray,
        light_peak_distances: np.ndarray,
        max_rad_per_az: np.ndarray,
        light_sampler=None,
    ) -> None:
        c = sin_az[az_index]
        s = cos_az[az_index]

        NEAR_START = 0.5
        d = NEAR_START
        max_ang_so_far = -np.pi / 2.0
        last_light_d = 0.0
        miss_streak = 0
        band_idx = 0
        iterations = 0

        while d < d_max:
            iterations += 1
            if iterations > self._ray_iteration_guard:
                print(
                    f"[HorizonEngine] Warning: ray iteration guard reached at az={az_index}, d={d:.1f}"
                )
                break
            x = obs_x + d * c
            y = obs_y + d * s

            h_terr = self.provider.get_elevation(x, y)
            if h_terr is None:
                miss_streak += 1
                if miss_streak >= self._ray_miss_exit_threshold:
                    break
                d_next = self._next_step_distance(d, step_m)
                if not np.isfinite(d_next) or d_next <= d:
                    print(
                        f"[HorizonEngine] Warning: invalid step progression at az={az_index}, d={d:.3f}, next={d_next}"
                    )
                    break
                d = d_next
                continue
            miss_streak = 0

            ang = float(
                apparent_elevation_radians(h_terr, d, h_eye_abs, self.R)
            )

            if ang > max_ang_so_far:
                max_ang_so_far = ang

            if light_sampler is not None and (d - last_light_d) >= 2000.0:
                last_light_d = d
                rad = self._sample_light_radiance(light_sampler, x, y)

                if rad and rad > 0.1:
                    dist_mult = 1.0 / max(1.0, (d / 1000.0))
                    if ang > (max_ang_so_far - 0.17):
                        light_domes[az_index] += float(rad * dist_mult * 20.0)
                        if rad > max_rad_per_az[az_index]:
                            max_rad_per_az[az_index] = float(rad)
                            light_peak_distances[az_index] = float(d)

            while band_idx + 1 < len(bands) and d >= bands[band_idx]["max"]:
                band_idx += 1
            if (
                0 <= band_idx < len(bands)
                and bands[band_idx]["min"] <= d < bands[band_idx]["max"]
            ):
                b = bands[band_idx]
                b["surface_angles"][az_index] = ang
                b["surface_dists"][az_index] = d
                b["surface_heights"][az_index] = h_terr
                if ang > b["angles"][az_index]:
                    b["angles"][az_index] = ang
                    b["dists"][az_index] = d
                    b["heights"][az_index] = h_terr

            d_next = self._next_step_distance(d, step_m)
            if not np.isfinite(d_next) or d_next <= d:
                print(
                    f"[HorizonEngine] Warning: invalid step progression at az={az_index}, d={d:.3f}, next={d_next}"
                )
                break
            d = d_next

    def _sample_light_radiance(
        self, light_sampler, x_internal: float, y_internal: float
    ) -> float:
        """
        Sample light-pollution radiance at a terrain internal point.

        Input CRS:
            - `x_internal`, `y_internal` in terrain internal CRS (`EPSG:25831`).
        Output:
            - Radiance float, `0.0` on controlled failure/out-of-bounds.
        """
        if light_sampler is None:
            return 0.0
        try:
            return float(
                light_sampler.get_radiance_terrain_xy(
                    x_internal,
                    y_internal,
                    input_crs=CRS_TERRAIN_INTERNAL,
                )
            )
        except Exception:
            return 0.0

    def _sample_light_radiance_batch(self, light_sampler, x, y) -> np.ndarray:
        sampler = getattr(light_sampler, "get_radiance_terrain_xy_batch", None)
        if callable(sampler):
            try:
                return np.asarray(
                    sampler(x, y, input_crs=CRS_TERRAIN_INTERNAL),
                    dtype=np.float32,
                )
            except Exception:
                pass
        x_arr, y_arr = np.broadcast_arrays(np.asarray(x), np.asarray(y))
        return np.asarray(
            [
                self._sample_light_radiance(light_sampler, float(px), float(py))
                for px, py in zip(x_arr.ravel(), y_arr.ravel())
            ],
            dtype=np.float32,
        ).reshape(x_arr.shape)

    def _provider_nominal_resolution_m(self) -> float:
        resolution = None
        getter = getattr(self.provider, "get_nominal_resolution_m", None)
        if callable(getter):
            try:
                resolution = float(getter())
            except Exception:
                resolution = None
        if resolution is None or not np.isfinite(resolution) or resolution <= 0:
            resolution = 30.0
        return float(resolution)

    def _normal_sample_step_m(self) -> float:
        resolution = self._provider_nominal_resolution_m()
        return float(max(10.0, min(120.0, resolution * 2.0)))

    def _sample_normal(
        self, x: float, y: float, center_h: float, step_m: float
    ) -> Tuple[float, float, float]:
        h_e = self.provider.get_elevation(x + step_m, y)
        h_w = self.provider.get_elevation(x - step_m, y)
        h_n = self.provider.get_elevation(x, y + step_m)
        h_s = self.provider.get_elevation(x, y - step_m)

        if h_e is None:
            h_e = center_h
        if h_w is None:
            h_w = center_h
        if h_n is None:
            h_n = center_h
        if h_s is None:
            h_s = center_h

        dzdx = (float(h_e) - float(h_w)) / (2.0 * step_m)
        dzdy = (float(h_n) - float(h_s)) / (2.0 * step_m)
        nx = -dzdx
        ny = -dzdy
        nz = 1.0
        norm = math.sqrt(nx * nx + ny * ny + nz * nz)
        if norm <= 0.0 or not np.isfinite(norm):
            return 0.0, 0.0, 1.0
        return nx / norm, ny / norm, nz / norm

    @staticmethod
    def _mesh_azimuth_step(delta_az_deg: float) -> float:
        """Keep relief geometry screen-dense without mirroring ray precision."""

        try:
            requested = float(delta_az_deg)
        except (TypeError, ValueError):
            requested = 1.0
        if not np.isfinite(requested) or requested <= 0.0:
            requested = 1.0
        if requested >= MIN_RELIEF_MESH_AZIMUTH_STEP_DEG:
            return requested

        # Keep retained mesh columns exactly aligned with scientific rays.
        # For arbitrary inputs (for example 0.03 degrees), the first aligned
        # visual step above the floor is 0.06 rather than a synthetic 0.05.
        stride = int(
            math.ceil(MIN_RELIEF_MESH_AZIMUTH_STEP_DEG / requested)
        )
        return requested * max(1, stride)

    @staticmethod
    def _mesh_distance_rings(
        d_max: float, resolution_m: Optional[float] = None
    ) -> np.ndarray:
        visual_max = max(250.0, float(d_max))
        resolution = (
            float(resolution_m)
            if resolution_m is not None
            and np.isfinite(resolution_m)
            and resolution_m > 0
            else 30.0
        )
        # At most 395 rings: high detail to 5 km, then progressively sparser
        # medium/low/silhouette LOD out to the resolved radius.
        zones = ((40.0, 5_000.0, 150), (5_000.0, 25_000.0, 100),
                 (25_000.0, 100_000.0, 75), (100_000.0, 250_000.0, 45),
                 (250_000.0, visual_max, 25))
        segments = []
        for start, stop, budget in zones:
            stop = min(stop, visual_max)
            if stop <= start:
                continue
            count = max(2, min(budget, int(math.ceil((stop - start) / max(resolution * 2.0, 1.0))) + 1))
            segments.append(np.geomspace(start, stop, count))

        rings = np.unique(np.round(np.concatenate(segments)).astype(np.float32))
        rings = rings[rings <= visual_max]
        return rings[rings > 0]

    @staticmethod
    def _compute_mesh_visibility(
        altitudes: np.ndarray,
        valid: np.ndarray,
        margin_deg: float = 0.02,
    ) -> np.ndarray:
        altitudes = np.asarray(altitudes)
        valid = np.asarray(valid, dtype=bool)
        visible = np.zeros_like(valid, dtype=bool)
        if altitudes.shape != valid.shape or altitudes.ndim != 2:
            return visible
        finite_valid = valid & np.isfinite(altitudes)
        candidates = np.where(finite_valid, altitudes, -np.inf)
        running_max = np.maximum.accumulate(candidates, axis=0)
        return finite_valid & (
            altitudes >= running_max - float(margin_deg)
        )

    def build_view_mesh(
        self,
        obs_x: float,
        obs_y: float,
        obs_h_ground: float,
        *,
        d_max: float,
        delta_az_deg: float = 1.0,
        abort_check=None,
    ) -> Dict:
        mesh_started_ns = __import__("time").perf_counter_ns()
        requested_delta_az_deg = float(delta_az_deg)
        mesh_delta_az_deg = self._mesh_azimuth_step(requested_delta_az_deg)
        h_eye_abs = float(obs_h_ground) + float(self.eye_height)
        resolution_m = self._provider_nominal_resolution_m()
        distances = self._mesh_distance_rings(d_max, resolution_m)
        azimuths = np.arange(
            0.0, 360.0, mesh_delta_az_deg, dtype=np.float32
        )
        n_d = len(distances)
        n_az = len(azimuths)

        altitudes = np.full((n_d, n_az), -90.0, dtype=np.float32)
        elevations = np.zeros((n_d, n_az), dtype=np.float32)
        normal_x = np.zeros((n_d, n_az), dtype=np.float32)
        normal_y = np.zeros((n_d, n_az), dtype=np.float32)
        normal_z = np.ones((n_d, n_az), dtype=np.float32)
        valid = np.zeros((n_d, n_az), dtype=bool)

        az_rads = np.deg2rad(
            azimuths.astype(np.float64) - self.grid_convergence_deg
        )
        sin_az = np.sin(az_rads)
        cos_az = np.cos(az_rads)

        reused_field = False
        field = self._last_polar_field
        if (
            field is not None
            and math.isclose(field.observer_x, float(obs_x), abs_tol=1e-6)
            and math.isclose(field.observer_y, float(obs_y), abs_tol=1e-6)
            and math.isclose(field.observer_ground, float(obs_h_ground), abs_tol=1e-6)
            and math.isclose(
                field.delta_az_deg, mesh_delta_az_deg, abs_tol=1e-9
            )
            and field.azimuths.shape == azimuths.shape
            and np.array_equal(field.azimuths, azimuths)
        ):
            distance_indices = np.searchsorted(field.distances, distances)
            in_bounds = distance_indices < field.distances.size
            if bool(np.all(in_bounds)) and np.array_equal(
                field.distances[distance_indices], distances
            ):
                elevations[:] = field.elevations[distance_indices, :]
                valid[:] = field.valid[distance_indices, :]
                reused_field = True

        if not reused_field and self._supports_batch_sampling():
            # Keep each provider request below the shared million-sample cap.
            azimuth_batch = max(
                1,
                min(
                    64,
                    int(self._vector_azimuth_batch),
                    DEFAULT_PERFORMANCE_BUDGET.batch_rows(40)
                    // max(1, int(n_d)),
                ),
            )
            distance_values = distances.astype(np.float64)
            for start in range(0, n_az, azimuth_batch):
                if abort_check and abort_check():
                    raise InterruptedError("Mesh build aborted")
                stop = min(n_az, start + azimuth_batch)
                x = float(obs_x) + sin_az[start:stop, None] * distance_values[None, :]
                y = float(obs_y) + cos_az[start:stop, None] * distance_values[None, :]
                sampled, sampled_valid = self._sample_provider_batch(x, y)
                elevations[:, start:stop] = sampled.T
                valid[:, start:stop] = sampled_valid.T
        elif not reused_field:
            for d_idx, d in enumerate(distances.astype(np.float64)):
                for az_idx in range(n_az):
                    x = obs_x + d * sin_az[az_idx]
                    y = obs_y + d * cos_az[az_idx]
                    h_terr = self.provider.get_elevation(x, y)
                    if h_terr is None:
                        continue
                    elevations[d_idx, az_idx] = float(h_terr)
                    valid[d_idx, az_idx] = True

        # Bound temporary matrices by processing azimuth columns in chunks.
        distance64 = distances.astype(np.float64)[:, None]
        mesh_column_batch = 256
        visible = np.zeros_like(valid, dtype=bool)
        for start in range(0, n_az, mesh_column_batch):
            if abort_check and abort_check():
                raise InterruptedError("Mesh build aborted")
            stop = min(n_az, start + mesh_column_batch)
            chunk_valid = valid[:, start:stop]
            computed = apparent_elevation_degrees(
                elevations[:, start:stop].astype(np.float64),
                distance64,
                float(h_eye_abs),
                float(self.R),
            ).astype(np.float32)
            chunk_altitudes = np.where(chunk_valid, computed, -90.0)
            altitudes[:, start:stop] = chunk_altitudes
            visible[:, start:stop] = self._compute_mesh_visibility(
                chunk_altitudes, chunk_valid
            )
        if n_az < 8 or mesh_delta_az_deg >= 30.0:
            normal_step_m = self._normal_sample_step_m()
            for d_idx, az_idx in np.argwhere(valid):
                d = float(distances[d_idx])
                x = obs_x + d * sin_az[az_idx]
                y = obs_y + d * cos_az[az_idx]
                h_terr = float(elevations[d_idx, az_idx])
                nx, ny, nz = self._sample_normal(
                    x, y, h_terr, normal_step_m
                )
                normal_x[d_idx, az_idx] = nx
                normal_y[d_idx, az_idx] = ny
                normal_z[d_idx, az_idx] = nz
        else:
            step_deg = mesh_delta_az_deg
            for start in range(0, n_az, mesh_column_batch):
                if abort_check and abort_check():
                    raise InterruptedError("Mesh normal calculation aborted")
                stop = min(n_az, start + mesh_column_batch)
                expanded_positions = np.arange(start - 1, stop + 1, dtype=np.int64)
                expanded_indices = np.mod(expanded_positions, n_az)
                expanded_azimuths = (
                    expanded_positions.astype(np.float64) * step_deg
                ).astype(np.float32)
                chunk_nx, chunk_ny, chunk_nz = compute_polar_mesh_normals(
                    elevations[:, expanded_indices],
                    valid[:, expanded_indices],
                    distances,
                    expanded_azimuths,
                )
                normal_x[:, start:stop] = chunk_nx[:, 1:-1]
                normal_y[:, start:stop] = chunk_ny[:, 1:-1]
                normal_z[:, start:stop] = chunk_nz[:, 1:-1]

        mesh_elapsed_s = (
            __import__("time").perf_counter_ns() - mesh_started_ns
        ) / 1e9
        rss_bytes, peak_rss_bytes = process_memory_bytes()
        append_perf_event(
            "terrain.mesh",
            elapsed_s=round(mesh_elapsed_s, 6),
            azimuths=int(n_az),
            distance_rings=int(n_d),
            samples=int(n_az * n_d),
            reused_field=bool(reused_field),
            requested_delta_az_deg=requested_delta_az_deg,
            delta_az_deg=mesh_delta_az_deg,
            d_max_m=float(d_max),
            rss_bytes=int(rss_bytes),
            peak_rss_bytes=int(peak_rss_bytes),
        )

        return {
            "version": 2,
            "azimuths": azimuths,
            "distances": distances.astype(np.float32),
            "altitudes": altitudes,
            "elevations": elevations,
            "normal_x": normal_x,
            "normal_y": normal_y,
            "normal_z": normal_z,
            "valid": valid,
            "visible": visible,
        }

    def _raycast_chunk(self, args):
        """Process a chunk of azimuths for parallel execution."""
        (
            az_indices,
            sin_az_chunk,
            cos_az_chunk,
            obs_x,
            obs_y,
            h_eye_abs,
            step_m,
            d_max,
            band_defs_simple,
            R,
        ) = args

        n_bands = len(band_defs_simple)
        n_chunk = len(az_indices)

        # Per-chunk band results: list of (angles, dists, heights) arrays
        chunk_angles = [np.full(n_chunk, -np.inf) for _ in range(n_bands)]
        chunk_dists = [np.zeros(n_chunk) for _ in range(n_bands)]
        chunk_heights = [np.zeros(n_chunk) for _ in range(n_bands)]

        for local_i in range(n_chunk):
            c = sin_az_chunk[local_i]
            s = cos_az_chunk[local_i]
            d = step_m
            miss_streak = 0
            b_idx = 0
            iterations = 0

            while d < d_max:
                iterations += 1
                if iterations > self._ray_iteration_guard:
                    break
                x = obs_x + d * c
                y = obs_y + d * s

                h_terr = self.provider.get_elevation(x, y)

                if h_terr is None:
                    miss_streak += 1
                    if miss_streak >= self._ray_miss_exit_threshold:
                        break
                    d_next = self._next_step_distance(d, step_m)
                    if not np.isfinite(d_next) or d_next <= d:
                        break
                    d = d_next
                    continue
                miss_streak = 0

                ang = float(
                    apparent_elevation_radians(h_terr, d, h_eye_abs, R)
                )

                while (
                    b_idx + 1 < n_bands and d >= band_defs_simple[b_idx][1]
                ):
                    b_idx += 1
                b_min, b_max = band_defs_simple[b_idx]
                if b_min <= d < b_max and ang > chunk_angles[b_idx][local_i]:
                    chunk_angles[b_idx][local_i] = ang
                    chunk_dists[b_idx][local_i] = d
                    chunk_heights[b_idx][local_i] = h_terr

                d_next = self._next_step_distance(d, step_m)
                if not np.isfinite(d_next) or d_next <= d:
                    break
                d = d_next

        return az_indices, chunk_angles, chunk_dists, chunk_heights

    def _bake_progressive_vectorized(
        self,
        *,
        obs_x: float,
        obs_y: float,
        obs_h_ground: float,
        h_eye_abs: float,
        step_m: float,
        d_max: float,
        delta_az_deg: float,
        azimuths: np.ndarray,
        ordered_indices: List[int],
        band_defs: List[Dict],
        bands: List[Dict],
        sin_az: np.ndarray,
        cos_az: np.ndarray,
        light_domes: np.ndarray,
        light_peak_distances: np.ndarray,
        resolved_mask: np.ndarray,
        progress_callback=None,
        preview_callback=None,
        preview_every: int = 24,
        light_sampler=None,
        abort_check=None,
    ) -> Tuple[np.ndarray, List[Dict], np.ndarray, np.ndarray, np.ndarray]:
        import time

        adaptive_enabled = bool(
            self.sampling_settings.adaptive_sampling_enabled
        )
        ray_distances = (
            build_adaptive_base_distances(d_max, self.sampling_settings)
            if adaptive_enabled
            else self._adaptive_distances(step_m, d_max)
        )
        mesh_distances = self._mesh_distance_rings(
            d_max, self._provider_nominal_resolution_m()
        ).astype(np.float64)
        sample_distances = np.unique(
            np.concatenate((ray_distances, mesh_distances))
        )
        ray_distance_indices = np.searchsorted(sample_distances, ray_distances)
        if not np.array_equal(sample_distances[ray_distance_indices], ray_distances):
            raise RuntimeError("Ray distances were not preserved in polar union")
        band_slices = [
            (
                int(np.searchsorted(ray_distances, float(definition["min"]), side="left")),
                int(np.searchsorted(ray_distances, float(definition["max"]), side="left")),
            )
            for definition in band_defs
        ]

        mesh_distance_indices = np.searchsorted(sample_distances, mesh_distances)
        if not np.array_equal(
            sample_distances[mesh_distance_indices], mesh_distances
        ):
            raise RuntimeError("Mesh distances were not preserved in polar union")
        mesh_delta_az_deg = self._mesh_azimuth_step(delta_az_deg)
        mesh_azimuths = np.arange(
            0.0, 360.0, mesh_delta_az_deg, dtype=np.float32
        )
        mesh_source_indices = np.rint(
            mesh_azimuths.astype(np.float64) / float(delta_az_deg)
        ).astype(np.int64)
        mesh_source_indices = np.clip(
            mesh_source_indices, 0, max(0, azimuths.size - 1)
        )
        source_to_mesh = np.full(azimuths.size, -1, dtype=np.int32)
        source_to_mesh[mesh_source_indices] = np.arange(
            mesh_source_indices.size, dtype=np.int32
        )
        # Retain only the screen-useful azimuth columns of the comparatively
        # small mesh ring field. Ray samples keep their requested precision.
        field_elevations = np.zeros(
            (mesh_distances.size, mesh_azimuths.size), dtype=np.float32
        )
        field_valid = np.zeros(field_elevations.shape, dtype=bool)
        io_before = self._raster_io_metrics()
        requested_backend = self._requested_raycast_backend(len(azimuths))
        effective_backend = "single_vectorized"
        t0 = time.time()
        completed = 0
        reduction_ns = 0
        snapshot_ns = 0
        preview_count = 0
        sampled_valid_count = 0
        phase_totals_ns = {
            "coordinates": 0,
            "provider": 0,
            "angles": 0,
            "bands": 0,
            "light": 0,
            "refinement": 0,
        }
        refinement_queries = 0
        refinement_valid_queries = 0
        selected_profile_samples = (
            0 if adaptive_enabled else int(azimuths.size * ray_distances.size)
        )
        maximum_refinement_errors = np.zeros(3, dtype=np.float64)
        rows_per_batch = DEFAULT_PERFORMANCE_BUDGET.batch_rows(40)
        batch_size = max(
            1,
            min(
                64,
                int(self._vector_azimuth_batch),
                rows_per_batch // max(1, int(sample_distances.size)),
            ),
        )
        chunk_start = 0
        first_batch_size = (
            min(batch_size, max(1, int(preview_every)))
            if preview_callback is not None
            else batch_size
        )
        while chunk_start < len(ordered_indices):
            if abort_check and abort_check():
                raise InterruptedError("Bake aborted")
            current_batch_size = (
                first_batch_size if chunk_start == 0 else batch_size
            )
            chunk_indices = np.asarray(
                ordered_indices[chunk_start : chunk_start + current_batch_size],
                dtype=np.int32,
            )
            chunk_metrics_before = self._raster_io_metrics()
            chunk_t0 = time.perf_counter_ns()
            result = self._sample_azimuth_chunk_vectorized(
                az_indices=chunk_indices,
                obs_x=obs_x,
                obs_y=obs_y,
                h_eye_abs=h_eye_abs,
                ray_distances=ray_distances,
                sample_distances=sample_distances,
                ray_distance_indices=ray_distance_indices,
                band_defs=band_defs,
                band_slices=band_slices,
                sin_az=sin_az,
                cos_az=cos_az,
                light_sampler=light_sampler,
            )
            if adaptive_enabled:
                refined_bands, refinement_metrics = self._refine_azimuth_chunk(
                    az_indices=chunk_indices,
                    obs_x=obs_x,
                    obs_y=obs_y,
                    h_eye_abs=h_eye_abs,
                    base_distances=ray_distances,
                    base_elevations=result["elevations"][:, ray_distance_indices],
                    base_valid=result["valid"][:, ray_distance_indices],
                    band_defs=band_defs,
                    sin_az=sin_az,
                    cos_az=cos_az,
                )
                result["bands"] = refined_bands
                phase_totals_ns["refinement"] += int(
                    refinement_metrics["refinement_ns"]
                )
                refinement_queries += int(
                    refinement_metrics["refinement_queries"]
                )
                refinement_valid_queries += int(
                    refinement_metrics["refinement_valid_queries"]
                )
                selected_profile_samples += int(
                    refinement_metrics["selected_samples"]
                )
                maximum_refinement_errors = np.maximum(
                    maximum_refinement_errors,
                    (
                        refinement_metrics["maximum_elevation_error_m"],
                        refinement_metrics["maximum_projected_error_px"],
                        refinement_metrics["maximum_slope_delta_deg"],
                    ),
                )
            chunk_elapsed_ns = time.perf_counter_ns() - chunk_t0
            chunk_metrics_after = self._raster_io_metrics()
            provider_ns = (
                chunk_metrics_after["selection_ns"]
                - chunk_metrics_before["selection_ns"]
                + chunk_metrics_after["interpolation_ns"]
                - chunk_metrics_before["interpolation_ns"]
            )
            reduction_ns += max(0, int(chunk_elapsed_ns) - int(provider_ns))
            for phase_name, phase_ns in result.get("timings_ns", {}).items():
                phase_totals_ns[phase_name] += int(phase_ns)
            sampled_valid_count += int(np.count_nonzero(result["valid"]))
            mesh_positions = source_to_mesh[chunk_indices]
            retained_rows = np.flatnonzero(mesh_positions >= 0)
            if retained_rows.size:
                retained_columns = mesh_positions[retained_rows]
                field_elevations[:, retained_columns] = np.asarray(
                    result["elevations"][retained_rows][
                        :, mesh_distance_indices
                    ],
                    dtype=np.float32,
                ).T
                field_valid[:, retained_columns] = np.asarray(
                    result["valid"][retained_rows][:, mesh_distance_indices],
                    dtype=bool,
                ).T

            # Commit in the requested priority order so previews never expose
            # unresolved azimuth values from the remainder of a matrix batch.
            for local_index, azimuth_index in enumerate(chunk_indices):
                if abort_check and abort_check():
                    raise InterruptedError("Bake aborted")
                for band_index, band_result in enumerate(result["bands"]):
                    target = bands[band_index]
                    for key in (
                        "angles",
                        "dists",
                        "heights",
                        "surface_angles",
                        "surface_dists",
                        "surface_heights",
                    ):
                        target[key][azimuth_index] = band_result[key][local_index]
                light_domes[azimuth_index] = result["light_domes"][local_index]
                light_peak_distances[azimuth_index] = result[
                    "light_peak_distances"
                ][local_index]
                resolved_mask[azimuth_index] = True
                completed += 1
            if progress_callback:
                progress_pct = (completed / len(azimuths)) * 100.0
                progress_callback(
                    progress_pct, f"Azimuth {completed}/{len(azimuths)}"
                )
            should_preview = bool(preview_callback) and (
                completed >= len(azimuths)
                or completed
                >= min(
                    len(azimuths),
                    max(1, preview_every) * (preview_count + 1),
                )
            )
            if should_preview:
                snapshot_t0 = time.perf_counter_ns()
                preview_callback(
                    completed,
                    len(azimuths),
                    azimuths,
                    bands,
                    light_domes,
                    light_peak_distances,
                    resolved_mask,
                )
                snapshot_ns += time.perf_counter_ns() - snapshot_t0
                preview_count += 1
            chunk_start += len(chunk_indices)

        self._last_polar_field = PolarElevationField(
            azimuths=mesh_azimuths,
            distances=mesh_distances.astype(np.float32),
            elevations=field_elevations,
            valid=field_valid,
            observer_x=float(obs_x),
            observer_y=float(obs_y),
            observer_ground=float(obs_h_ground),
            d_max=float(d_max),
            delta_az_deg=mesh_delta_az_deg,
        )
        print(
            "[HorizonEngine] Vectorized progressive bake complete in "
            f"{time.time() - t0:.2f}s."
        )
        elapsed = time.time() - t0
        io_after = self._raster_io_metrics()
        rss_bytes, peak_rss_bytes = process_memory_bytes()
        append_perf_event(
            "terrain.raycast",
            backend=effective_backend,
            requested_backend=requested_backend,
            elapsed_s=round(float(elapsed), 6),
            azimuths=int(azimuths.size),
            ray_distances=int(ray_distances.size),
            polar_distances=int(sample_distances.size),
            retained_mesh_distances=int(mesh_distances.size),
            retained_mesh_azimuths=int(mesh_azimuths.size),
            samples=int(
                azimuths.size * sample_distances.size + refinement_queries
            ),
            base_profile_samples=int(azimuths.size * ray_distances.size),
            refinement_queries=int(refinement_queries),
            refinement_valid_queries=int(refinement_valid_queries),
            selected_profile_samples=int(selected_profile_samples),
            adaptive_sampling_enabled=adaptive_enabled,
            valid_samples=int(sampled_valid_count),
            retained_valid_samples=int(np.count_nonzero(field_valid)),
            step_m=float(step_m),
            d_max_m=float(d_max),
            bytes_read=int(io_after["bytes_read"] - io_before["bytes_read"]),
            cache_hits=int(io_after["cache_hits"] - io_before["cache_hits"]),
            cache_misses=int(io_after["cache_misses"] - io_before["cache_misses"]),
            selection_s=round(
                (io_after["selection_ns"] - io_before["selection_ns"]) / 1e9, 6
            ),
            interpolation_s=round(
                (io_after["interpolation_ns"] - io_before["interpolation_ns"]) / 1e9, 6
            ),
            reduction_s=round(reduction_ns / 1e9, 6),
            snapshots_s=round(snapshot_ns / 1e9, 6),
            preview_count=int(preview_count),
            bands=int(len(band_defs)),
            coordinate_s=round(phase_totals_ns["coordinates"] / 1e9, 6),
            provider_s=round(phase_totals_ns["provider"] / 1e9, 6),
            angle_s=round(phase_totals_ns["angles"] / 1e9, 6),
            band_reduction_s=round(phase_totals_ns["bands"] / 1e9, 6),
            light_pollution_s=round(phase_totals_ns["light"] / 1e9, 6),
            refinement_s=round(phase_totals_ns["refinement"] / 1e9, 6),
            maximum_elevation_error_m=round(
                float(maximum_refinement_errors[0]), 6
            ),
            maximum_projected_error_px=round(
                float(maximum_refinement_errors[1]), 6
            ),
            maximum_slope_delta_deg=round(
                float(maximum_refinement_errors[2]), 6
            ),
            candidate_tiles=int(
                io_after["candidate_tiles"] - io_before["candidate_tiles"]
            ),
            loaded_tiles=int(io_after["loaded_tiles"] - io_before["loaded_tiles"]),
            sampled_points=int(
                io_after["sampled_points"] - io_before["sampled_points"]
            ),
            rss_bytes=int(rss_bytes),
            peak_rss_bytes=int(peak_rss_bytes),
        )
        return (
            azimuths,
            bands,
            light_domes,
            light_peak_distances,
            resolved_mask,
        )

    def bake_progressive(
        self,
        obs_x: float,
        obs_y: float,
        obs_h_ground: Optional[float] = None,
        step_m: float = 50,
        d_max: Optional[float] = None,
        delta_az_deg: float = 0.5,
        band_defs: Optional[List[Dict]] = None,
        azimuth_order: Optional[List[int]] = None,
        progress_callback=None,
        preview_callback=None,
        preview_every: int = 24,
        light_sampler=None,
        abort_check=None,
    ) -> Tuple[np.ndarray, List[Dict], np.ndarray, np.ndarray, np.ndarray]:
        """
        Compute a horizon profile incrementally.

        This progressive path is intended for subprocess baking and live previews:
        the worker can request previews after useful azimuth blocks without waiting
        for the full 360° solve to finish.
        """
        import time
        if d_max is None:
            raise ValueError("d_max must be the resolved visibility radius")

        if obs_h_ground is None:
            val = self.provider.get_elevation(obs_x, obs_y)
            if val is None:
                print(
                    "[HorizonEngine] Observer outside DEM coverage. Using 0."
                )
                obs_h_ground = 0
            else:
                obs_h_ground = val

        h_eye_abs = obs_h_ground + self.eye_height
        azimuths = np.arange(0, 360, delta_az_deg, dtype=np.float32)
        n_az = len(azimuths)
        light_domes = np.zeros(n_az, dtype=np.float32)
        light_peak_distances = np.zeros(n_az, dtype=np.float32)
        max_rad_per_az = np.zeros(n_az, dtype=np.float32)
        resolved_mask = np.zeros(n_az, dtype=bool)

        if band_defs is None:
            band_defs = generate_bands(20, max_dist_m=d_max)

        bands = self._build_band_buffers(n_az, band_defs)

        az_rads = np.deg2rad(
            azimuths.astype(np.float64) - self.grid_convergence_deg
        )
        sin_az = np.sin(az_rads)
        cos_az = np.cos(az_rads)

        if azimuth_order is None:
            ordered_indices = list(range(n_az))
        else:
            ordered_indices = []
            seen = set()
            for idx in azimuth_order:
                try:
                    idx_int = int(idx)
                except Exception:
                    continue
                if 0 <= idx_int < n_az and idx_int not in seen:
                    ordered_indices.append(idx_int)
                    seen.add(idx_int)
            if len(ordered_indices) < n_az:
                ordered_indices.extend(i for i in range(n_az) if i not in seen)

        print(
            f"[HorizonEngine] Progressive bake {n_az} azimuths, max_dist={d_max / 1000:.0f}km..."
        )
        if (
            self._supports_batch_sampling()
            or self.sampling_settings.adaptive_sampling_enabled
        ):
            return self._bake_progressive_vectorized(
                obs_x=obs_x,
                obs_y=obs_y,
                obs_h_ground=float(obs_h_ground),
                h_eye_abs=float(h_eye_abs),
                step_m=step_m,
                d_max=d_max,
                delta_az_deg=delta_az_deg,
                azimuths=azimuths,
                ordered_indices=ordered_indices,
                band_defs=band_defs,
                bands=bands,
                sin_az=sin_az,
                cos_az=cos_az,
                light_domes=light_domes,
                light_peak_distances=light_peak_distances,
                resolved_mask=resolved_mask,
                progress_callback=progress_callback,
                preview_callback=preview_callback,
                preview_every=preview_every,
                light_sampler=light_sampler,
                abort_check=abort_check,
            )
        t0 = time.time()
        last_preview_t = t0
        completed = 0

        for az_index in ordered_indices:
            if abort_check and abort_check():
                print("[HorizonEngine] Progressive bake aborted by caller.")
                raise InterruptedError("Bake aborted")

            self._sample_single_azimuth(
                az_index=az_index,
                obs_x=obs_x,
                obs_y=obs_y,
                h_eye_abs=h_eye_abs,
                step_m=step_m,
                d_max=d_max,
                bands=bands,
                sin_az=sin_az,
                cos_az=cos_az,
                light_domes=light_domes,
                light_peak_distances=light_peak_distances,
                max_rad_per_az=max_rad_per_az,
                light_sampler=light_sampler,
            )
            resolved_mask[az_index] = True
            completed += 1

            progress_pct = (completed / n_az) * 100.0
            if progress_callback:
                progress_callback(progress_pct, f"Azimuth {completed}/{n_az}")

            if preview_callback:
                now = time.time()
                enough_samples = completed >= preview_every and (
                    completed % max(1, preview_every) == 0
                )
                enough_time = (now - last_preview_t) >= 0.35
                if completed == n_az or enough_samples or enough_time:
                    preview_callback(
                        completed,
                        n_az,
                        azimuths,
                        bands,
                        light_domes,
                        light_peak_distances,
                        resolved_mask,
                    )
                    last_preview_t = now

        elapsed = time.time() - t0
        print(f"[HorizonEngine] Progressive bake complete in {elapsed:.2f}s.")
        return (
            azimuths,
            bands,
            light_domes,
            light_peak_distances,
            resolved_mask,
        )

    def bake(
        self,
        obs_x: float,
        obs_y: float,
        obs_h_ground: Optional[float] = None,
        step_m: float = 50,
        d_max: Optional[float] = None,
        delta_az_deg: float = 0.5,
        band_defs: Optional[List[Dict]] = None,
        progress_callback=None,
        light_sampler=None,
        abort_check=None,
    ) -> Tuple[np.ndarray, List[Dict], np.ndarray]:
        """
        Compute multi-band horizon profile (sequential — CPU-bound under GIL).

        Args:
            progress_callback: Optional callable(percent: int, msg: str).

        Returns (azimuths, bands) where bands is a list of dicts
        each containing 'id', 'angles', 'dists', 'heights' arrays.
        """
        azimuths, bands, light_domes, light_peak_distances, _resolved = (
            self.bake_progressive(
                obs_x=obs_x,
                obs_y=obs_y,
                obs_h_ground=obs_h_ground,
                step_m=step_m,
                d_max=d_max,
                delta_az_deg=delta_az_deg,
                band_defs=band_defs,
                progress_callback=progress_callback,
                light_sampler=light_sampler,
                abort_check=abort_check,
            )
        )
        return azimuths, bands, light_domes, light_peak_distances

        # Retained temporarily as a reference implementation for parity
        # profiling; normal execution returns through bake_progressive above.
        import time

        if obs_h_ground is None:
            val = self.provider.get_elevation(obs_x, obs_y)
            if val is None:
                print(
                    "[HorizonEngine] Observer outside DEM coverage. Using 0."
                )
                obs_h_ground = 0
            else:
                obs_h_ground = val

        h_eye_abs = obs_h_ground + self.eye_height

        azimuths = np.arange(0, 360, delta_az_deg)
        n_az = len(azimuths)
        light_domes = np.zeros(n_az, dtype=np.float32)
        light_peak_distances = np.zeros(n_az, dtype=np.float32)
        max_rad_per_az = np.zeros(n_az, dtype=np.float32)

        if band_defs is None:
            if d_max is None:
                raise ValueError("d_max must be the resolved visibility radius")
            band_defs = generate_bands(20, max_dist_m=d_max)

        bands = []
        for bd in band_defs:
            bands.append(
                {
                    "id": bd["id"],
                    "min": bd["min"],
                    "max": bd["max"],
                    "angles": np.full(n_az, -np.inf),
                    "dists": np.zeros(n_az),
                    "heights": np.zeros(n_az),
                    "surface_angles": np.full(n_az, -np.inf),
                    "surface_dists": np.zeros(n_az),
                    "surface_heights": np.zeros(n_az),
                }
            )

        az_rads = np.deg2rad(
            azimuths.astype(np.float64) - self.grid_convergence_deg
        )
        sin_az = np.sin(az_rads)
        cos_az = np.cos(az_rads)

        print(
            f"[HorizonEngine] Baking {n_az} azimuths, max_dist={d_max / 1000:.0f}km..."
        )
        t0 = time.time()

        for i in range(n_az):
            if i % 10 == 0:
                print(
                    f"[HorizonEngine Debug] Azimuth {i}/{n_az} (ang={azimuths[i]:.1f})",
                    flush=True,
                )

            if abort_check and abort_check():
                print("[HorizonEngine] Bake ABORTED by caller request.")
                raise InterruptedError("Bake aborted")

            try:
                c = sin_az[i]
                s = cos_az[i]

                # ── Escombrat adaptatiu: molt fi a l'acostament, groller a la llunyania ──
                NEAR_START = (
                    0.5  # metres — primera mostra (resolució sub-metre)
                )
                d = NEAR_START
                max_ang_so_far = -np.pi / 2.0

                last_light_d = 0.0
                miss_streak = 0
                band_idx = 0
                iterations = 0

                while d < d_max:
                    iterations += 1
                    if iterations > self._ray_iteration_guard:
                        print(
                            f"[HorizonEngine] Warning: ray iteration guard reached at az={i}, d={d:.1f}"
                        )
                        break
                    x = obs_x + d * c
                    y = obs_y + d * s

                    h_terr = self.provider.get_elevation(x, y)
                    if h_terr is None:
                        miss_streak += 1
                        if miss_streak >= self._ray_miss_exit_threshold:
                            break
                        d_next = self._next_step_distance(d, step_m)
                        if not np.isfinite(d_next) or d_next <= d:
                            print(
                                f"[HorizonEngine] Warning: invalid step progression at az={i}, d={d:.3f}, next={d_next}"
                            )
                            break
                        d = d_next
                        continue
                    miss_streak = 0

                    ang = float(
                        apparent_elevation_radians(
                            h_terr, d, h_eye_abs, self.R
                        )
                    )

                    if ang > max_ang_so_far:
                        max_ang_so_far = ang

                    # Light pollution sampling with occlusion checks
                    # Optimization: To avoid stalling the baker with thousands of small reads,
                    # we sample light domes sparsely, at most every 2000m.
                    # We only sample if the point is somewhat visible (within 1.5 deg of max horizon)
                    if (
                        light_sampler is not None
                        and (d - last_light_d) >= 2000.0
                    ):
                        last_light_d = d
                        rad = self._sample_light_radiance(
                            light_sampler, x, y
                        )

                        if rad and rad > 0.1:  # Catch everything that glows
                            # Distance multiplier: Inverse linear law (slower decay for atmospheric glow)
                            dist_mult = 1.0 / max(1.0, (d / 1000.0))
                            # Liberal occlusion check: allow up to 10 degrees (-0.17 rad) below horizon
                            if ang > (max_ang_so_far - 0.17):
                                # Multiply by 20.0 (balanced gain)
                                light_domes[i] += float(rad * dist_mult * 20.0)
                                if rad > max_rad_per_az[i]:
                                    max_rad_per_az[i] = float(rad)
                                    light_peak_distances[i] = float(d)

                    while (
                        band_idx + 1 < len(bands)
                        and d >= bands[band_idx]["max"]
                    ):
                        band_idx += 1
                    if (
                        0 <= band_idx < len(bands)
                        and bands[band_idx]["min"] <= d < bands[band_idx]["max"]
                    ):
                        b = bands[band_idx]
                        b["surface_angles"][i] = ang
                        b["surface_dists"][i] = d
                        b["surface_heights"][i] = h_terr
                        if ang > b["angles"][i]:
                            b["angles"][i] = ang
                            b["dists"][i] = d
                            b["heights"][i] = h_terr

                    d_next = self._next_step_distance(d, step_m)
                    if not np.isfinite(d_next) or d_next <= d:
                        print(
                            f"[HorizonEngine] Warning: invalid step progression at az={i}, d={d:.3f}, next={d_next}"
                        )
                        break
                    d = d_next
            except Exception as e:
                print(
                    f"[HorizonEngine] CRITICAL Error at Azimuth {i} (d={d:.1f}): {e}"
                )
                import traceback

                traceback.print_exc()
                raise e

            # Progress reporting (every azimuth).
            progress_pct = ((i + 1) / n_az) * 100.0
            if progress_callback:
                progress_callback(progress_pct, f"Azimuth {i + 1}/{n_az}")
            pct_bucket = int(progress_pct)
            if pct_bucket % 25 == 0 and abs(progress_pct - pct_bucket) < 1e-9:
                elapsed = time.time() - t0
                print(
                    f"[HorizonEngine]   {pct_bucket}% ({i+1}/{n_az} azimuths, {elapsed:.1f}s)"
                )

        elapsed = time.time() - t0
        print(f"[HorizonEngine] Bake complete in {elapsed:.2f}s.")
        return azimuths, bands, light_domes, light_peak_distances


# ─────────────────────────────────────────────
#  Convenience functions
# ─────────────────────────────────────────────


def bake_and_save(
    lat: float,
    lon: float,
    tiles_dir: str,
    output_path: str,
    radius: Optional[float] = None,
    step_m: float = 50,
    resolution_deg: float = 0.5,
    eye_height: float = 1.7,
    band_defs: Optional[List[Dict]] = None,
):
    """
    Full pipeline: transform coords, index tiles, bake horizon, save .npz.
    """
    if radius is None:
        raise ValueError("radius must be a resolved visibility radius")
    from pyproj import Transformer

    # Transform observer from geographic to terrain internal coordinates.
    with PYPROJ_TRANSFORMER_LOCK:
        transformer = Transformer.from_crs(
            CRS_GEOGRAPHIC, CRS_TERRAIN_INTERNAL, always_xy=True
        )
    x_utm, y_utm = transformer.transform(lon, lat)
    print(f"[HorizonEngine] Observer UTM: {x_utm:.2f}, {y_utm:.2f}")

    # Build system
    idx = TileIndex(tiles_dir)
    cache = TileCache(capacity=100)
    sampler = DemSampler(idx, cache)
    baker = HorizonBaker(sampler, eye_height=eye_height)

    # Sample observer altitude
    ground_h = sampler.sample(x_utm, y_utm)
    if ground_h is None:
        print(
            "[HorizonEngine] Observer outside DEM. Using fallback 200m (Lleida plains)."
        )
        ground_h = 200.0
    else:
        print(f"[HorizonEngine] Observer altitude from DEM: {ground_h:.2f}m")

    # Bake
    azimuths, bands, light_domes, light_peak_distances = baker.bake(
        obs_x=x_utm,
        obs_y=y_utm,
        obs_h_ground=ground_h,
        step_m=step_m,
        d_max=radius,
        delta_az_deg=resolution_deg,
        band_defs=band_defs,
    )

    # Build & save profile
    profile = HorizonProfile(
        azimuths=azimuths,
        bands=bands,
        observer_lat=lat,
        observer_lon=lon,
        light_domes=light_domes,
        light_peak_distances=light_peak_distances,
        resolved_radius_m=float(radius),
    )
    profile.save(output_path)
    print(f"[HorizonEngine] Profile saved to {output_path}")
    return profile


def load_profile(path: str) -> Optional[HorizonProfile]:
    """Load a pre-baked horizon profile. Returns None if file is missing."""
    if not os.path.exists(path):
        return None
    try:
        return HorizonProfile.load(path)
    except Exception as e:
        print(f"[HorizonEngine] Error loading profile {path}: {e}")
        return None
