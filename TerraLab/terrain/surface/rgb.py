"""RGB surface raster provider."""

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
    RgbaSampleBatch,
    SurfaceProvider,
    _GdalProviderMixin,
    _GeoRasterDataset,
    _discover_surface_files,
    _normalize_channel,
)

class RgbSurfaceProvider(_GdalProviderMixin, SurfaceProvider):
    """RGB/RGBA/orthophoto provider using nearest-neighbour batch sampling."""

    surface_kind = "surface-rgb"

    def __init__(
        self,
        paths: str | os.PathLike | Sequence[str],
        *,
        source_id: str = "",
        source_name: str = "",
        internal_crs: str = CRS_TERRAIN_INTERNAL,
        declared_crs: str | None = None,
        linear_light: bool = False,
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
        self.linear_light = bool(linear_light)
        self._datasets: list[_GeoRasterDataset] = []

    def initialize(self) -> bool:
        start = time.perf_counter()
        if not self.paths:
            raise FileNotFoundError("No GDAL-compatible RGB surface raster found")
        self._initialize_datasets()
        def is_rgb(dataset: _GeoRasterDataset) -> bool:
            return dataset.count in {3, 4}

        valid_datasets = [dataset for dataset in self._datasets if is_rgb(dataset)]
        invalid = [dataset for dataset in self._datasets if not is_rgb(dataset)]
        for dataset in invalid:
            dataset.close()
        self._datasets = valid_datasets
        if not self._datasets:
            raise ValueError(
                "L'ortofoto ha de tenir tres canals RGB vàlids "
                "(o RGB amb alfa); no s'accepten rasters d'una banda."
            )
        print(
            "[TEMPORAL][RgbSurfaceProvider] "
            f"datasets={len(self._datasets)} elapsed={time.perf_counter() - start:.3f}s"
        )
        return True

    @staticmethod
    def _band_layout(dataset: _GeoRasterDataset) -> tuple[tuple[int, int, int], int | None]:
        names = list(dataset.colorinterp)
        try:
            red = names.index("red") + 1
            green = names.index("green") + 1
            blue = names.index("blue") + 1
            rgb = (red, green, blue)
        except ValueError:
            if dataset.count >= 3:
                rgb = (1, 2, 3)
            else:
                rgb = (1, 1, 1)
        alpha = names.index("alpha") + 1 if "alpha" in names else None
        return rgb, alpha

    def sample_rgba(
        self,
        x: Any,
        y: Any,
        *,
        input_crs: str | None = None,
        progress_callback=None,
        abort_check=None,
    ) -> RgbaSampleBatch:
        x_arr, y_arr = np.broadcast_arrays(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
        shape = x_arr.shape
        flat_x = x_arr.ravel()
        flat_y = y_arr.ravel()
        crs = normalize_crs(input_crs or self.internal_crs)
        rgba = np.zeros((flat_x.size, 4), dtype=np.uint8)
        valid = np.zeros(flat_x.size, dtype=bool)
        provenance = np.full(flat_x.size, -1, dtype=np.int16)
        finite = np.isfinite(flat_x) & np.isfinite(flat_y)

        dataset_count = max(1, len(self._datasets))
        for dataset_index, dataset in enumerate(self._datasets):
            if callable(abort_check) and abort_check():
                from TerraLab.terrain.providers import RasterSamplingCancelled

                raise RasterSamplingCancelled("RGB surface sampling cancelled")
            query_indices = np.flatnonzero(finite & ~valid)
            if query_indices.size == 0:
                break
            native_x, native_y, covered = self._native_query(
                dataset, flat_x, flat_y, query_indices, crs
            )
            if not np.any(covered):
                continue
            covered_indices = query_indices[covered]
            rgb_bands, alpha_band = self._band_layout(dataset)
            rgb_values, sampled_valid = dataset.sample_native(
                native_x[covered],
                native_y[covered],
                bands=tuple(dict.fromkeys(rgb_bands)),
                interpolation="nearest",
                progress_callback=(
                    lambda fraction, message, _index=dataset_index: progress_callback(
                        (_index + 0.85 * float(fraction)) / dataset_count,
                        message,
                    )
                    if callable(progress_callback)
                    else None
                ),
                abort_check=abort_check,
            )
            if not np.any(sampled_valid):
                continue
            rgb_rows = {
                band: index for index, band in enumerate(dict.fromkeys(rgb_bands))
            }
            chosen_local = np.flatnonzero(sampled_valid)
            chosen = covered_indices[chosen_local]
            for channel, band in enumerate(rgb_bands):
                dtype_name = dataset.dtypes[band - 1]
                rgba[chosen, channel] = _normalize_channel(
                    rgb_values[rgb_rows[band], chosen_local],
                    dtype_name,
                    scale=dataset.scales[band - 1],
                    offset=dataset.offsets[band - 1],
                )
            if alpha_band is None:
                rgba[chosen, 3] = 255
                alpha_valid = rgba[chosen, 3] > 0
            else:
                alpha_values, alpha_sampled_valid = dataset.sample_native(
                    native_x[covered],
                    native_y[covered],
                    bands=(alpha_band,),
                    interpolation="nearest",
                    progress_callback=(
                        lambda fraction, message, _index=dataset_index: progress_callback(
                            (_index + 0.85 + 0.15 * float(fraction))
                            / dataset_count,
                            message,
                        )
                        if callable(progress_callback)
                        else None
                    ),
                    abort_check=abort_check,
                )
                rgba[chosen, 3] = _normalize_channel(
                    alpha_values[0, chosen_local],
                    dataset.dtypes[alpha_band - 1],
                    scale=dataset.scales[alpha_band - 1],
                    offset=dataset.offsets[alpha_band - 1],
                    alpha=True,
                )
                alpha_valid = (
                    (rgba[chosen, 3] > 0)
                    & alpha_sampled_valid[chosen_local]
                )
            if np.any(alpha_valid):
                accepted = chosen[alpha_valid]
                valid[accepted] = True
                provenance[accepted] = int(min(dataset_index, np.iinfo(np.int16).max))
            rejected = chosen[~alpha_valid]
            if rejected.size:
                rgba[rejected] = 0
            if callable(progress_callback):
                progress_callback(
                    (dataset_index + 1.0) / dataset_count,
                    "sampling-rgb",
                )
        return RgbaSampleBatch(
            rgba.reshape(shape + (4,)),
            valid.reshape(shape),
            provenance.reshape(shape),
        )
