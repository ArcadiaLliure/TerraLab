"""Data-adapter loading of immutable terrain artifacts for scene planners."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from TerraLab.scene.contracts import ResourceRef
from TerraLab.terrain.domain.profile import HorizonProfile, build_flat_horizon_profile
from TerraLab.terrain.persistence.profile_npz import load_profile
from TerraLab.terrain.surface import _surface_cache_from_payload


class TerrainPlanResourceRepository:
    """Cache artifact decoding; never projects, shades, classifies or paints."""

    def __init__(self) -> None:
        self._profiles: dict[tuple[str, str], HorizonProfile] = {}
        self._surfaces: dict[tuple[str, str], object | None] = {}

    def profile(
        self, resource: ResourceRef, *, latitude: float, longitude: float
    ) -> HorizonProfile:
        path = Path(resource.path or "")
        key = (str(path), resource.version)
        if path.is_file():
            cached = self._profiles.get(key)
            if cached is not None:
                return cached
            result = load_profile(str(path))
            self._profiles[key] = result
            return result
        return build_flat_horizon_profile(
            observer_lat=latitude,
            observer_lon=longitude,
            geometry_id=f"flat:{latitude:.6f}:{longitude:.6f}",
        )

    def surface(self, resource: ResourceRef) -> object | None:
        path = Path(resource.path or "")
        key = (str(path), resource.version)
        if key in self._surfaces:
            return self._surfaces[key]
        if not path.is_file():
            self._surfaces[key] = None
            return None
        with np.load(path, allow_pickle=False) as archive:
            if "__metadata__" not in archive.files:
                raise ValueError("Surface artifact metadata is missing")
            metadata = json.loads(
                np.asarray(archive["__metadata__"], dtype=np.uint8)
                .tobytes()
                .decode("utf-8")
            )
            arrays = {
                name: np.asarray(archive[name]).copy()
                for name in archive.files
                if name != "__metadata__"
            }
        result = _surface_cache_from_payload(metadata, arrays)
        self._surfaces[key] = result
        return result

    def close(self) -> None:
        self._profiles.clear()
        self._surfaces.clear()


__all__ = ("TerrainPlanResourceRepository",)
