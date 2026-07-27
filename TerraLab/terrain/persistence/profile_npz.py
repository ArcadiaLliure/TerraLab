"""Pickle-free NPZ persistence for terrain horizon profiles."""

from __future__ import annotations


import numpy as np

from TerraLab.terrain.crs import CRS_TERRAIN_INTERNAL
from TerraLab.terrain.representation import (
    TerrainGeometrySource,
    TerrainRepresentationMode,
    normalize_terrain_geometry_source,
    normalize_terrain_representation_mode,
)
TERRAIN_MESH_VERSION = 3


def save_profile(profile, path: str) -> None:
    """Save the profile with an explicit, pickle-free NPZ schema."""
    self = profile
    light_domes = (
        np.asarray(self.light_domes)
        if self.light_domes is not None
        else np.zeros(len(self.azimuths), dtype=np.float32)
    )
    light_peak_distances = (
        np.asarray(self.light_peak_distances)
        if self.light_peak_distances is not None
        else np.zeros(len(self.azimuths), dtype=np.float32)
    )
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
        "light_domes": light_domes,
        "light_peak_distances": light_peak_distances,
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
            "near_patch_eastings",
            "near_patch_northings",
            "near_patch_altitudes",
            "near_patch_elevations",
            "near_patch_normal_x",
            "near_patch_normal_y",
            "near_patch_normal_z",
            "near_patch_valid",
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


def load_profile(path: str):
    from TerraLab.terrain.domain.profile import HorizonProfile

    with np.load(path, allow_pickle=False) as d:
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
            for key, dtype in (
                ("near_patch_eastings", np.float32),
                ("near_patch_northings", np.float32),
                ("near_patch_altitudes", np.float32),
                ("near_patch_elevations", np.float32),
                ("near_patch_normal_x", np.float32),
                ("near_patch_normal_y", np.float32),
                ("near_patch_normal_z", np.float32),
                ("near_patch_valid", bool),
            ):
                stored_key = f"terrain_mesh_{key}"
                if stored_key in d:
                    terrain_mesh[key] = np.asarray(
                        d[stored_key], dtype=dtype
                    ).copy()

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
