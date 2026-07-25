"""Environment-selected performance backend switches."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _environment_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class PerformanceFlags:
    """Independent rollback switches for optimized runtime backends."""

    raster_batch: bool = True
    raycast_vectorized: bool = True
    relief_cached: bool = True
    gaia_out_of_core: bool = True
    surface_lod_cache: bool = True

    @classmethod
    def from_environment(cls) -> "PerformanceFlags":
        return cls(
            raster_batch=_environment_flag("TERRALAB_RASTER_BATCH"),
            raycast_vectorized=_environment_flag("TERRALAB_RAYCAST_VECTORIZED"),
            relief_cached=_environment_flag("TERRALAB_RELIEF_CACHED"),
            gaia_out_of_core=_environment_flag("TERRALAB_GAIA_OUT_OF_CORE"),
            surface_lod_cache=_environment_flag("TERRALAB_SURFACE_LOD_CACHE"),
        )


PERFORMANCE_FLAGS = PerformanceFlags.from_environment()
