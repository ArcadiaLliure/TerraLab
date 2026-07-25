"""Categorical and RGB-categorical surface providers."""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Mapping, Sequence

import numpy as np

from TerraLab.common.locks import RASTERIO_LOCK
from TerraLab.common.performance.flags import PERFORMANCE_FLAGS
from TerraLab.terrain.crs import (
    CRS_TERRAIN_INTERNAL,
    CoordinateTransformService,
    normalize_crs,
)
from TerraLab.terrain.land_cover.legends.s2glc import (
    S2GLC_LEGEND,
    s2glc_classes_to_rgba,
)
from TerraLab.terrain.surface.common import (
    LAND_COVER_PALETTE_VERSION,
    SAMPLE_ORIGIN_EXACT,
    SAMPLE_ORIGIN_MODAL,
    SAMPLE_ORIGIN_SOURCE_FALLBACK,
    CategoricalSampleBatch,
    RgbaSampleBatch,
    SurfaceProvider,
    _GdalProviderMixin,
    _GeoRasterDataset,
    _default_land_cover_rgba,
    _discover_surface_files,
    _native_sample_indices,
    _normalize_channel,
    _normalized_lod_factors,
    _path_fingerprint,
)
from TerraLab.terrain.surface.rgb import RgbSurfaceProvider
from TerraLab.terrain.surface_store import AtomicNpzStore

def _isolated_lod_classes(legend_id: str) -> tuple[int, ...]:
    """Return semantic classes that must survive categorical LOD reduction."""

    legend = str(legend_id or "").strip().lower().replace("-", "_")
    if legend in {"s2glc", "s2glc_2017", "s2glc_europe_2017"}:
        # Buildings win if one coarse cell also happens to touch shoreline.
        return (62, 162)
    if legend in {
        "clcplus",
        "clcplus_backbone",
        "clcplus_backbone_2023",
        "clc_plus_backbone",
    }:
        return (1, 10, 253)
    return ()


class CategoricalSurfaceProvider(_GdalProviderMixin, SurfaceProvider):
    """Land-cover provider; classes are sampled exclusively with nearest neighbour."""

    surface_kind = "surface-categorical"

    def __init__(
        self,
        paths: str | os.PathLike | Sequence[str],
        *,
        source_id: str = "",
        source_name: str = "",
        class_colors: Mapping[int, Sequence[int]] | None = None,
        legend_id: str | None = None,
        band: int = 1,
        internal_crs: str = CRS_TERRAIN_INTERNAL,
        declared_crs: str | None = None,
        transform_service: CoordinateTransformService | None = None,
        tile_store: AtomicNpzStore | None = None,
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
        self.legend_id = str(legend_id or "").strip().lower()
        self.class_colors = {
            int(class_id): tuple(int(np.clip(value, 0, 255)) for value in color)
            for class_id, color in (class_colors or {}).items()
        }
        self._datasets: list[_GeoRasterDataset] = []
        self.tile_store = tile_store

    @property
    def fingerprint(self) -> str:
        digest = hashlib.blake2b(digest_size=20)
        digest.update(
            _path_fingerprint(
                self.paths,
                namespace=self.surface_kind,
                configuration=(
                    self.declared_crs,
                    self.band,
                    self.legend_id,
                    self.source_id,
                    self.source_name,
                ),
            ).encode("ascii")
        )
        digest.update(f"land-cover-palette-v{LAND_COVER_PALETTE_VERSION}".encode("ascii"))
        for class_id, color in sorted(self.class_colors.items()):
            digest.update(str(class_id).encode("ascii"))
            digest.update(bytes(color))
        return digest.hexdigest()

    def initialize(self) -> bool:
        start = time.perf_counter()
        if not self.paths:
            raise FileNotFoundError("No GDAL-compatible categorical surface raster found")
        self._initialize_datasets()
        def is_discrete_single_band(dataset: _GeoRasterDataset) -> bool:
            if dataset.count != 1 or self.band != 1:
                return False
            return np.issubdtype(np.dtype(dataset.dtypes[0]), np.integer)

        invalid = [
            dataset for dataset in self._datasets if not is_discrete_single_band(dataset)
        ]
        self._datasets = [
            dataset for dataset in self._datasets if is_discrete_single_band(dataset)
        ]
        for dataset in invalid:
            dataset.close()
        if not self._datasets:
            raise ValueError(
                "La cobertura categòrica ha de ser un GeoTIFF d'una sola "
                "banda amb codis enters discrets."
            )
        # Merge embedded GDAL tables for classes not explicitly configured by
        # catalogue metadata.  This is setup-time metadata I/O, never raster
        # access from the paint path.  Earlier (higher-resolution) datasets win
        # when a mosaic happens to contain conflicting tables.
        for dataset in self._datasets:
            try:
                with RASTERIO_LOCK:
                    table = dataset.dataset.colormap(self.band)
            except Exception:
                continue
            for class_id, color in table.items():
                self.class_colors.setdefault(
                    int(class_id),
                    tuple(int(np.clip(value, 0, 255)) for value in color),
                )
        print(
            "[TEMPORAL][CategoricalSurfaceProvider] "
            f"datasets={len(self._datasets)} elapsed={time.perf_counter() - start:.3f}s"
        )
        return True

    def sample_classes(
        self,
        x: Any,
        y: Any,
        *,
        input_crs: str | None = None,
        lod_factors: Any = None,
        progress_callback=None,
        abort_check=None,
    ) -> CategoricalSampleBatch:
        x_arr, y_arr = np.broadcast_arrays(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
        lod_arr = None
        if lod_factors is not None:
            lod_arr = np.broadcast_to(np.asarray(lod_factors, dtype=np.int16), x_arr.shape).ravel()
        shape = x_arr.shape
        flat_x = x_arr.ravel()
        flat_y = y_arr.ravel()
        crs = normalize_crs(input_crs or self.internal_crs)
        classes = np.zeros(flat_x.size, dtype=np.int64)
        valid = np.zeros(flat_x.size, dtype=bool)
        provenance = np.full(flat_x.size, -1, dtype=np.int16)
        raster_rows = np.full(flat_x.size, -1, dtype=np.int64)
        raster_columns = np.full(flat_x.size, -1, dtype=np.int64)
        sampled_lod = np.ones(flat_x.size, dtype=np.int16)
        sample_origins = np.zeros(flat_x.size, dtype=np.uint8)
        finite = np.isfinite(flat_x) & np.isfinite(flat_y)

        dataset_count = max(1, len(self._datasets))
        for dataset_index, dataset in enumerate(self._datasets):
            if callable(abort_check) and abort_check():
                from TerraLab.terrain.providers import RasterSamplingCancelled

                raise RasterSamplingCancelled(
                    "Categorical surface sampling cancelled"
                )
            query_indices = np.flatnonzero(finite & ~valid)
            if query_indices.size == 0:
                break
            native_x, native_y, covered = self._native_query(
                dataset, flat_x, flat_y, query_indices, crs
            )
            if not np.any(covered):
                continue
            covered_indices = query_indices[covered]
            def sampling_progress(fraction, message, _index=dataset_index):
                return (progress_callback(
                                (_index + float(fraction)) / dataset_count,
                                message,
                            )
                            if callable(progress_callback)
                            else None)
            if lod_arr is not None and PERFORMANCE_FLAGS.surface_lod_cache:
                values, sampled_valid = dataset.sample_native_lod(
                    native_x[covered],
                    native_y[covered],
                    lod_arr[covered_indices],
                    bands=(self.band,),
                    tile_store=self.tile_store,
                    reducer_identity=(
                        "categorical-lod-feature-priority-v3:"
                        f"legend={self.legend_id or 'external'}"
                    ),
                    priority_classes=_isolated_lod_classes(self.legend_id),
                    progress_callback=sampling_progress,
                    abort_check=abort_check,
                )
            else:
                values, sampled_valid = dataset.sample_native(
                    native_x[covered],
                    native_y[covered],
                    bands=(self.band,),
                    interpolation="nearest",
                    progress_callback=sampling_progress,
                    abort_check=abort_check,
                )
            if np.any(sampled_valid):
                chosen = covered_indices[sampled_valid]
                classes[chosen] = np.asarray(
                    values[0, sampled_valid], dtype=np.int64
                )
                valid[chosen] = True
                provenance[chosen] = int(min(dataset_index, np.iinfo(np.int16).max))
                selected_rows, selected_columns = _native_sample_indices(
                    dataset,
                    native_x[covered][sampled_valid],
                    native_y[covered][sampled_valid],
                )
                raster_rows[chosen] = selected_rows
                raster_columns[chosen] = selected_columns
                selected_lod = (
                    _normalized_lod_factors(lod_arr[chosen])
                    if lod_arr is not None
                    else np.ones(chosen.shape, dtype=np.int16)
                )
                sampled_lod[chosen] = selected_lod
                sample_origins[chosen] = np.where(
                    dataset_index > 0,
                    SAMPLE_ORIGIN_SOURCE_FALLBACK,
                    np.where(
                        selected_lod > 1,
                        SAMPLE_ORIGIN_MODAL,
                        SAMPLE_ORIGIN_EXACT,
                    ),
                )
            if callable(progress_callback):
                progress_callback(
                    (dataset_index + 1.0) / dataset_count,
                    "sampling-categories",
                )
        return CategoricalSampleBatch(
            classes.reshape(shape),
            valid.reshape(shape),
            provenance.reshape(shape),
        )

    def classes_to_rgba(
        self,
        classes: Any,
        *,
        x: Any | None = None,
        y: Any | None = None,
        altitude_m: Any | None = None,
        slope: Any | None = None,
        aspect_deg: Any | None = None,
    ) -> RgbaSampleBatch:
        class_array = np.asarray(classes, dtype=np.int64)
        if self.legend_id in {"s2glc", "s2glc-2017", "s2glc_europe_2017"}:
            rgba, valid = s2glc_classes_to_rgba(
                class_array,
                altitude_m=altitude_m,
                slope=slope,
                aspect_deg=aspect_deg,
            )
            # Explicit per-source colours remain a supported override.
            for class_id, raw_color in self.class_colors.items():
                mask = class_array == int(class_id)
                if not np.any(mask):
                    continue
                color = tuple(raw_color)
                if len(color) == 3:
                    color += (255,)
                rgba[mask] = np.asarray(color[:4], dtype=np.uint8)
                valid[mask] = int(color[3]) > 0
            return RgbaSampleBatch(rgba, valid)
        rgba = np.zeros(class_array.shape + (4,), dtype=np.uint8)
        valid = np.zeros(class_array.shape, dtype=bool)
        for class_id in np.unique(class_array):
            mask = class_array == int(class_id)
            raw_color = self.class_colors.get(int(class_id))
            color = tuple(raw_color) if raw_color is not None else ()
            if len(color) == 3:
                color = color + (255,)
            elif len(color) < 4:
                color = _default_land_cover_rgba(int(class_id))
            rgba[mask] = np.asarray(color[:4], dtype=np.uint8)
            valid[mask] = int(color[3]) > 0
        return RgbaSampleBatch(rgba, valid)


class RgbCategoricalSurfaceProvider(CategoricalSurfaceProvider):
    """Decode a lossless RGB palette raster into semantic class identifiers."""

    surface_kind = "surface-categorical-rgb"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        mapping: dict[tuple[int, int, int, int], int] = {}
        if self.legend_id in {
            "s2glc",
            "s2glc-2017",
            "s2glc_europe_2017",
        }:
            for class_id, style in S2GLC_LEGEND.items():
                mapping[tuple(int(value) for value in style.base_color)] = int(
                    class_id
                )
        for class_id, raw_color in self.class_colors.items():
            color = tuple(int(value) for value in raw_color)
            if len(color) == 3:
                color += (255,)
            if len(color) >= 4:
                mapping[color[:4]] = int(class_id)
        self._rgba_to_class = mapping
        packed = sorted(
            (
                ((r << 24) | (g << 16) | (b << 8) | a, class_id)
                for (r, g, b, a), class_id in mapping.items()
            ),
            key=lambda item: item[0],
        )
        self._packed_colors = np.asarray(
            [item[0] for item in packed], dtype=np.uint32
        )
        self._packed_classes = np.asarray(
            [item[1] for item in packed], dtype=np.int64
        )

    def initialize(self) -> bool:
        if not self.paths:
            raise FileNotFoundError(
                "No GDAL-compatible RGB categorical raster found"
            )
        if not self._rgba_to_class:
            raise ValueError(
                "La cobertura RGB categòrica necessita una llegenda coneguda "
                "o una paleta explícita."
            )
        self._initialize_datasets()
        valid_datasets = [
            dataset for dataset in self._datasets if dataset.count in {3, 4}
        ]
        invalid = [
            dataset for dataset in self._datasets if dataset.count not in {3, 4}
        ]
        for dataset in invalid:
            dataset.close()
        self._datasets = valid_datasets
        if not self._datasets:
            raise ValueError(
                "La cobertura RGB categòrica ha de tenir tres canals RGB "
                "(o RGB amb alfa)."
            )
        return True

    @staticmethod
    def _decode_rgba(
        rgba: np.ndarray,
        packed_colors: np.ndarray,
        packed_classes: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        values = np.asarray(rgba, dtype=np.uint8)
        packed = (
            values[0].astype(np.uint32) << np.uint32(24)
            | values[1].astype(np.uint32) << np.uint32(16)
            | values[2].astype(np.uint32) << np.uint32(8)
            | values[3].astype(np.uint32)
        )
        positions = np.searchsorted(packed_colors, packed)
        inside = positions < packed_colors.size
        matched = np.zeros(packed.shape, dtype=bool)
        if np.any(inside):
            matched[inside] = (
                packed_colors[positions[inside]] == packed[inside]
            )
        classes = np.full(packed.shape, -1, dtype=np.int64)
        if np.any(matched):
            classes[matched] = packed_classes[positions[matched]]
        return classes, matched & (values[3] > 0)

    def sample_classes(
        self,
        x: Any,
        y: Any,
        *,
        input_crs: str | None = None,
        lod_factors: Any = None,
        progress_callback=None,
        abort_check=None,
    ) -> CategoricalSampleBatch:
        x_arr, y_arr = np.broadcast_arrays(
            np.asarray(x, dtype=np.float64),
            np.asarray(y, dtype=np.float64),
        )
        shape = x_arr.shape
        flat_x = x_arr.ravel()
        flat_y = y_arr.ravel()
        lod_arr = (
            np.broadcast_to(
                np.asarray(lod_factors, dtype=np.int16), shape
            ).ravel()
            if lod_factors is not None
            else None
        )
        crs = normalize_crs(input_crs or self.internal_crs)
        classes = np.full(flat_x.size, -1, dtype=np.int64)
        valid = np.zeros(flat_x.size, dtype=bool)
        provenance = np.full(flat_x.size, -1, dtype=np.int16)
        raster_rows = np.full(flat_x.size, -1, dtype=np.int64)
        raster_columns = np.full(flat_x.size, -1, dtype=np.int64)
        sampled_lod = np.ones(flat_x.size, dtype=np.int16)
        sample_origins = np.zeros(flat_x.size, dtype=np.uint8)
        finite = np.isfinite(flat_x) & np.isfinite(flat_y)
        dataset_count = max(1, len(self._datasets))
        for dataset_index, dataset in enumerate(self._datasets):
            if callable(abort_check) and abort_check():
                from TerraLab.terrain.providers import RasterSamplingCancelled

                raise RasterSamplingCancelled(
                    "RGB categorical sampling cancelled"
                )
            query_indices = np.flatnonzero(finite & ~valid)
            if query_indices.size == 0:
                break
            native_x, native_y, covered = self._native_query(
                dataset, flat_x, flat_y, query_indices, crs
            )
            if not np.any(covered):
                continue
            covered_indices = query_indices[covered]
            rgb_bands, alpha_band = RgbSurfaceProvider._band_layout(dataset)
            bands = tuple(
                dict.fromkeys(
                    (*rgb_bands, *((alpha_band,) if alpha_band else ()))
                )
            )
            if lod_arr is not None and PERFORMANCE_FLAGS.surface_lod_cache:
                rows = {band: index for index, band in enumerate(bands)}

                def decode_window(
                    raw_values: np.ndarray,
                    _bands: tuple[int, ...],
                ) -> tuple[np.ndarray, np.ndarray]:
                    window_shape = raw_values.shape[1:]
                    rgba_window = np.full(
                        (4,) + window_shape, 255, dtype=np.uint8
                    )
                    for channel, band in enumerate(rgb_bands):
                        rgba_window[channel] = _normalize_channel(
                            raw_values[rows[band]],
                            dataset.dtypes[band - 1],
                            scale=dataset.scales[band - 1],
                            offset=dataset.offsets[band - 1],
                        )
                    if alpha_band is not None:
                        rgba_window[3] = _normalize_channel(
                            raw_values[rows[alpha_band]],
                            dataset.dtypes[alpha_band - 1],
                            scale=dataset.scales[alpha_band - 1],
                            offset=dataset.offsets[alpha_band - 1],
                            alpha=True,
                        )
                    return self._decode_rgba(
                        rgba_window,
                        self._packed_colors,
                        self._packed_classes,
                    )

                values, sampled_valid = dataset.sample_native_lod(
                    native_x[covered],
                    native_y[covered],
                    lod_arr[covered_indices],
                    bands=bands,
                    tile_store=self.tile_store,
                    categorical_decoder=decode_window,
                    reducer_identity=(
                        "categorical-lod-feature-priority-v3:"
                        f"palette={self.fingerprint}"
                    ),
                    priority_classes=_isolated_lod_classes(self.legend_id),
                    progress_callback=progress_callback,
                    abort_check=abort_check,
                )
                decoded = np.asarray(values[0], dtype=np.int64)
                decoded_valid = sampled_valid
            else:
                values, sampled_valid = dataset.sample_native(
                    native_x[covered],
                    native_y[covered],
                    bands=bands,
                    interpolation="nearest",
                    progress_callback=progress_callback,
                    abort_check=abort_check,
                )
                rows = {band: index for index, band in enumerate(bands)}
                rgba = np.full(
                    (4, covered_indices.size), 255, dtype=np.uint8
                )
                for channel, band in enumerate(rgb_bands):
                    rgba[channel] = _normalize_channel(
                        values[rows[band]],
                        dataset.dtypes[band - 1],
                        scale=dataset.scales[band - 1],
                        offset=dataset.offsets[band - 1],
                    )
                if alpha_band is not None:
                    rgba[3] = _normalize_channel(
                        values[rows[alpha_band]],
                        dataset.dtypes[alpha_band - 1],
                        scale=dataset.scales[alpha_band - 1],
                        offset=dataset.offsets[alpha_band - 1],
                        alpha=True,
                    )
                decoded, decoded_valid = self._decode_rgba(
                    rgba, self._packed_colors, self._packed_classes
                )
                decoded_valid &= sampled_valid
            if np.any(decoded_valid):
                chosen = covered_indices[decoded_valid]
                classes[chosen] = decoded[decoded_valid]
                valid[chosen] = True
                provenance[chosen] = int(dataset_index)
                selected_rows, selected_columns = _native_sample_indices(
                    dataset,
                    native_x[covered][decoded_valid],
                    native_y[covered][decoded_valid],
                )
                raster_rows[chosen] = selected_rows
                raster_columns[chosen] = selected_columns
                selected_lod = (
                    _normalized_lod_factors(lod_arr[chosen])
                    if lod_arr is not None
                    else np.ones(chosen.shape, dtype=np.int16)
                )
                sampled_lod[chosen] = selected_lod
                sample_origins[chosen] = np.where(
                    dataset_index > 0,
                    SAMPLE_ORIGIN_SOURCE_FALLBACK,
                    np.where(
                        selected_lod > 1,
                        SAMPLE_ORIGIN_MODAL,
                        SAMPLE_ORIGIN_EXACT,
                    ),
                )
            if callable(progress_callback):
                progress_callback(
                    (dataset_index + 1.0) / dataset_count,
                    "sampling-rgb-categories",
                )
        return CategoricalSampleBatch(
            classes.reshape(shape),
            valid.reshape(shape),
            provenance.reshape(shape),
            raster_rows.reshape(shape),
            raster_columns.reshape(shape),
            sampled_lod.reshape(shape),
            sample_origins.reshape(shape),
        )
