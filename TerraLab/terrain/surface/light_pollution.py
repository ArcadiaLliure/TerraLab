"""Light-pollution raster surface provider."""

from __future__ import annotations

import os
import time
from typing import Any, Sequence

import numpy as np

from TerraLab.terrain.crs import (
    CRS_TERRAIN_INTERNAL,
    CoordinateTransformService,
    normalize_crs,
)
from TerraLab.terrain.surface.common import (
    SurfaceProvider,
    _GdalProviderMixin,
    _GeoRasterDataset,
    _discover_surface_files,
)

class LightPollutionProvider(_GdalProviderMixin, SurfaceProvider):
    """Typed continuous radiance provider, independent from terrain surface."""

    surface_kind = "light-pollution"

    def __init__(
        self,
        paths: str | os.PathLike | Sequence[str],
        *,
        source_id: str = "",
        source_name: str = "",
        band: int = 1,
        internal_crs: str = CRS_TERRAIN_INTERNAL,
        declared_crs: str | None = None,
        transform_service: CoordinateTransformService | None = None,
    ) -> None:
        super().__init__(
            source_id=source_id,
            source_name=source_name,
            internal_crs=internal_crs,
            transform_service=transform_service,
        )
        raw_paths = [str(paths)] if isinstance(paths, (str, os.PathLike)) else [str(path) for path in paths]
        self.paths = _discover_surface_files(raw_paths)
        self.declared_crs = declared_crs
        self.band = max(1, int(band))
        self._datasets: list[_GeoRasterDataset] = []

    def initialize(self) -> bool:
        start = time.perf_counter()
        if not self.paths:
            raise FileNotFoundError("No light-pollution raster found")
        self._initialize_datasets()
        invalid = [dataset for dataset in self._datasets if dataset.count < self.band]
        self._datasets = [dataset for dataset in self._datasets if dataset.count >= self.band]
        for dataset in invalid:
            dataset.close()
        if not self._datasets:
            raise ValueError(f"Radiance band {self.band} is unavailable")
        print(
            "[TEMPORAL][LightPollutionProvider] "
            f"datasets={len(self._datasets)} elapsed={time.perf_counter() - start:.3f}s"
        )
        return True

    def sample_radiance(
        self,
        x: Any,
        y: Any,
        *,
        input_crs: str | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        x_arr, y_arr = np.broadcast_arrays(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
        shape = x_arr.shape
        flat_x = x_arr.ravel()
        flat_y = y_arr.ravel()
        crs = normalize_crs(input_crs or self.internal_crs)
        radiance = np.zeros(flat_x.size, dtype=np.float32)
        valid = np.zeros(flat_x.size, dtype=bool)
        finite = np.isfinite(flat_x) & np.isfinite(flat_y)
        for dataset in self._datasets:
            query_indices = np.flatnonzero(finite & ~valid)
            if query_indices.size == 0:
                break
            native_x, native_y, covered = self._native_query(
                dataset, flat_x, flat_y, query_indices, crs
            )
            if not np.any(covered):
                continue
            covered_indices = query_indices[covered]
            values, sampled_valid = dataset.sample_native(
                native_x[covered], native_y[covered], bands=(self.band,), interpolation="bilinear"
            )
            if np.any(sampled_valid):
                chosen = covered_indices[sampled_valid]
                radiance[chosen] = np.maximum(0.0, values[0, sampled_valid])
                valid[chosen] = True
        return radiance.reshape(shape), valid.reshape(shape)
