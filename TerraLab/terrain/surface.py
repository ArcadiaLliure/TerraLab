"""Typed surface and light-pollution providers plus pre-sampling caches.

This module has no Qt dependency.  Raster I/O and CRS transformations happen
while building :class:`SurfaceSampleCache`; the renderer receives immutable
NumPy arrays aligned with profile bands or the relief mesh.
"""

from __future__ import annotations

import abc
import hashlib
import inspect
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from TerraLab.common.locks import RASTERIO_LOCK
from TerraLab.common.performance import (
    ByteLRU,
    DEFAULT_PERFORMANCE_BUDGET,
    PERFORMANCE_FLAGS,
    process_memory_bytes,
)
from TerraLab.common.perf_events import append_perf_event
from TerraLab.terrain.crs import (
    CRS_GEOGRAPHIC,
    CRS_TERRAIN_INTERNAL,
    DEFAULT_TRANSFORM_SERVICE,
    CoordinateTransformService,
    normalize_crs,
)
from TerraLab.terrain.providers import (
    RasterMetadata,
    _GeoRasterDataset,
    _as_source_sequence,
    _path_fingerprint,
    _source_declared_crs,
    _source_enabled,
    _source_identifier,
    _source_paths,
    _source_value,
)
from TerraLab.terrain.land_cover.legends.s2glc import (
    S2GLC_LEGEND,
    s2glc_classes_to_rgba,
)
from TerraLab.terrain.surface_store import AtomicNpzStore


SURFACE_GDAL_SUFFIXES = {
    ".tif",
    ".tiff",
    ".vrt",
    ".img",
    ".jp2",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}
SURFACE_CACHE_POLICY_VERSION = 4
LAND_COVER_PALETTE_VERSION = 3
SAMPLE_ORIGIN_UNKNOWN = np.uint8(0)
SAMPLE_ORIGIN_EXACT = np.uint8(1)
SAMPLE_ORIGIN_MODAL = np.uint8(2)
SAMPLE_ORIGIN_SOURCE_FALLBACK = np.uint8(3)
SAMPLE_ORIGIN_TERRAIN_FALLBACK = np.uint8(4)


# CLC+ Backbone raster classes use a stable 1..11 legend.  Keeping that
# palette here makes a plain CLC+ GeoTIFF useful even when its GDAL color table
# was stripped during download, extraction, or conversion.  Coverage sentinel
# values are transparent so a lower-priority surface source can fill them.
_CLCPLUS_BACKBONE_RGBA: Mapping[int, tuple[int, int, int, int]] = {
    1: (255, 0, 0, 255),       # sealed
    2: (34, 139, 34, 255),     # needle-leaved trees
    3: (128, 255, 0, 255),     # broadleaved deciduous trees
    4: (0, 255, 8, 255),       # broadleaved evergreen trees
    5: (128, 64, 0, 255),      # low-growing woody plants
    6: (204, 242, 77, 255),    # permanent herbaceous
    7: (255, 255, 128, 255),   # periodically herbaceous
    8: (255, 128, 255, 255),   # lichens and mosses
    9: (191, 191, 191, 255),   # non- and sparsely-vegetated
    10: (0, 128, 255, 255),    # water
    11: (0, 255, 255, 255),    # snow and ice
    253: (0, 128, 255, 255),   # coastal seawater buffer
    254: (230, 230, 230, 0),   # outside product area
    255: (0, 0, 0, 0),         # CLC+ NoData sentinel
}


# Unknown categorical schemas cannot be interpreted semantically without
# catalogue metadata.  They still need stable, visibly distinct materials so
# they do not silently fall back to the synthetic elevation palette.
_GENERIC_LAND_COVER_SWATCHES: tuple[tuple[int, int, int], ...] = (
    (109, 96, 82),    # stone / built surface
    (176, 142, 76),   # cultivated soil
    (51, 101, 58),    # forest
    (111, 145, 73),   # grassland
    (100, 119, 71),   # scrub
    (177, 151, 105),  # bare earth
    (65, 119, 117),   # wetland
    (52, 103, 151),   # water
    (145, 111, 91),   # exposed rock / soil
    (145, 139, 93),   # dry vegetation
    (91, 124, 105),   # mixed vegetation
    (126, 113, 143),  # non-vegetated thematic class
)


def _default_land_cover_rgba(class_id: int) -> tuple[int, int, int, int]:
    """Return a stable material color for a valid, otherwise unknown class."""

    known = _CLCPLUS_BACKBONE_RGBA.get(int(class_id))
    if known is not None:
        return known
    digest = hashlib.blake2s(
        str(int(class_id)).encode("ascii"),
        digest_size=2,
        person=b"tl-lc-v1",
    ).digest()
    base = _GENERIC_LAND_COVER_SWATCHES[digest[0] % len(_GENERIC_LAND_COVER_SWATCHES)]
    # A small deterministic luminance shift reduces collisions without turning
    # the qualitative material palette into a continuous height gradient.
    scale = 0.88 + (digest[1] / 255.0) * 0.24
    rgb = tuple(int(np.clip(round(channel * scale), 0, 255)) for channel in base)
    return rgb + (255,)


@dataclass(frozen=True)
class RgbaSampleBatch:
    rgba: np.ndarray
    valid: np.ndarray
    source_indices: np.ndarray | None = None
    class_ids: np.ndarray | None = None
    categorical: np.ndarray | None = None
    raster_rows: np.ndarray | None = None
    raster_columns: np.ndarray | None = None
    lod_factors: np.ndarray | None = None
    sample_origins: np.ndarray | None = None

    def __post_init__(self) -> None:
        rgba = np.asarray(self.rgba, dtype=np.uint8)
        valid = np.asarray(self.valid, dtype=bool)
        if rgba.shape != valid.shape + (4,):
            raise ValueError("RGBA samples must have shape valid.shape + (4,)")
        object.__setattr__(self, "rgba", rgba)
        object.__setattr__(self, "valid", valid)
        if self.source_indices is not None:
            source_indices = np.asarray(self.source_indices, dtype=np.int16)
            if source_indices.shape != valid.shape:
                raise ValueError("Source indices must match the validity mask")
            object.__setattr__(self, "source_indices", source_indices)
        if self.class_ids is not None:
            class_ids = np.asarray(self.class_ids, dtype=np.int64)
            if class_ids.shape != valid.shape:
                raise ValueError("Class identifiers must match the validity mask")
            object.__setattr__(self, "class_ids", class_ids)
        if self.categorical is not None:
            categorical = np.asarray(self.categorical, dtype=bool)
            if categorical.shape != valid.shape:
                raise ValueError("Categorical mask must match the validity mask")
            object.__setattr__(self, "categorical", categorical)
        for name, dtype in (
            ("raster_rows", np.int64),
            ("raster_columns", np.int64),
            ("lod_factors", np.int16),
            ("sample_origins", np.uint8),
        ):
            value = getattr(self, name)
            if value is None:
                continue
            array = np.asarray(value, dtype=dtype)
            if array.shape != valid.shape:
                raise ValueError(f"{name} must match the validity mask")
            object.__setattr__(self, name, array)


@dataclass(frozen=True)
class CategoricalSampleBatch:
    classes: np.ndarray
    valid: np.ndarray
    source_indices: np.ndarray | None = None
    raster_rows: np.ndarray | None = None
    raster_columns: np.ndarray | None = None
    lod_factors: np.ndarray | None = None
    sample_origins: np.ndarray | None = None

    def __post_init__(self) -> None:
        classes = np.asarray(self.classes, dtype=np.int64)
        valid = np.asarray(self.valid, dtype=bool)
        if classes.shape != valid.shape:
            raise ValueError("Categorical values and validity masks must match")
        object.__setattr__(self, "classes", classes)
        object.__setattr__(self, "valid", valid)
        if self.source_indices is not None:
            source_indices = np.asarray(self.source_indices, dtype=np.int16)
            if source_indices.shape != valid.shape:
                raise ValueError("Source indices must match the validity mask")
            object.__setattr__(self, "source_indices", source_indices)
        for name, dtype in (
            ("raster_rows", np.int64),
            ("raster_columns", np.int64),
            ("lod_factors", np.int16),
            ("sample_origins", np.uint8),
        ):
            value = getattr(self, name)
            if value is None:
                continue
            array = np.asarray(value, dtype=dtype)
            if array.shape != valid.shape:
                raise ValueError(f"{name} must match the validity mask")
            object.__setattr__(self, name, array)


class SurfaceProvider(abc.ABC):
    """Base contract for data that determines terrain appearance."""

    surface_kind = "surface"

    def __init__(
        self,
        *,
        source_id: str = "",
        source_name: str = "",
        internal_crs: str = CRS_TERRAIN_INTERNAL,
        transform_service: CoordinateTransformService | None = None,
    ) -> None:
        self.source_id = str(source_id or "")
        self.source_name = str(source_name or source_id or "")
        self.internal_crs = normalize_crs(internal_crs)
        self.transform_service = transform_service or DEFAULT_TRANSFORM_SERVICE

    @property
    @abc.abstractmethod
    def fingerprint(self) -> str:
        """Stable version/fingerprint used in surface-cache keys."""

    @property
    @abc.abstractmethod
    def metadata(self) -> tuple[RasterMetadata, ...]:
        """Native raster metadata."""

    @abc.abstractmethod
    def initialize(self) -> bool:
        """Open metadata/datasets outside the render path."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release open GDAL datasets and block caches."""


def _discover_surface_files(paths: Sequence[str]) -> list[str]:
    start = time.perf_counter()
    result: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_file() and path.suffix.lower() in SURFACE_GDAL_SUFFIXES:
            result.append(str(path))
        elif path.is_dir():
            result.extend(
                str(entry)
                for entry in path.rglob("*")
                if entry.is_file() and entry.suffix.lower() in SURFACE_GDAL_SUFFIXES
            )
    discovered = sorted(set(result), key=str.casefold)
    if paths:
        print(
            "[TEMPORAL][SurfaceDiscovery] "
            f"roots={len(paths)} files={len(discovered)} "
            f"elapsed={time.perf_counter() - start:.3f}s"
        )
    return discovered


def _normalize_channel(
    values: np.ndarray,
    dtype_name: str,
    *,
    scale: float = 1.0,
    offset: float = 0.0,
    alpha: bool = False,
) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32)
    dtype = np.dtype(dtype_name)
    if np.issubdtype(dtype, np.integer):
        maximum = float(np.iinfo(dtype).max)
        finite = values[np.isfinite(values)]
        has_gdal_scaling = not math.isclose(float(scale), 1.0) or not math.isclose(float(offset), 0.0)
        if has_gdal_scaling and finite.size and float(np.min(finite)) >= 0.0 and float(np.max(finite)) <= 1.0 + 1e-6:
            values = values * 255.0
        elif maximum > 255.0 and not has_gdal_scaling:
            values = values * (255.0 / maximum)
    elif np.issubdtype(dtype, np.floating):
        finite = values[np.isfinite(values)]
        if finite.size and float(np.nanmax(finite)) <= 1.0 + 1e-6 and float(np.nanmin(finite)) >= -1e-6:
            values = values * 255.0
    if alpha:
        values = np.where(np.isfinite(values), values, 0.0)
    return np.clip(np.rint(values), 0.0, 255.0).astype(np.uint8)


def _native_sample_indices(
    dataset: _GeoRasterDataset, native_x: Any, native_y: Any
) -> tuple[np.ndarray, np.ndarray]:
    """Return nearest native pixel indices for transformed sample points."""

    x = np.asarray(native_x, dtype=np.float64)
    y = np.asarray(native_y, dtype=np.float64)
    inverse = dataset.inverse_transform
    columns = np.floor(
        inverse.a * x + inverse.b * y + inverse.c
    ).astype(np.int64)
    rows = np.floor(
        inverse.d * x + inverse.e * y + inverse.f
    ).astype(np.int64)
    return rows, columns


def _normalized_lod_factors(values: Any) -> np.ndarray:
    requested = np.asarray(values, dtype=np.int16)
    levels = np.asarray((1, 2, 4, 8, 16, 32, 64, 128), dtype=np.int16)
    positions = (
        np.searchsorted(levels, np.maximum(1, requested), side="right") - 1
    )
    return levels[np.clip(positions, 0, len(levels) - 1)]


class _GdalProviderMixin:
    paths: list[str]
    declared_crs: str | None
    transform_service: CoordinateTransformService
    _datasets: list[_GeoRasterDataset]

    def _initialize_datasets(self) -> bool:
        start = time.perf_counter()
        opened: list[_GeoRasterDataset] = []
        failures: list[str] = []
        for path in self.paths:
            dataset = _GeoRasterDataset(
                path,
                declared_crs=self.declared_crs,
                transform_service=self.transform_service,
                block_cache_capacity=64,
            )
            try:
                dataset.open()
                opened.append(dataset)
            except Exception as exc:
                dataset.close()
                failures.append(f"{path}: {exc}")
        if not opened:
            raise RuntimeError("No usable surface raster: " + "; ".join(failures))
        opened.sort(
            key=lambda item: (
                item.resolution_m if item.resolution_m is not None else math.inf,
                item.path.casefold(),
            )
        )
        self._datasets = opened
        print(
            "[TEMPORAL][SurfaceProvider] "
            f"opened={len(opened)} failed={len(failures)} "
            f"elapsed={time.perf_counter() - start:.3f}s"
        )
        return True

    @property
    def metadata(self) -> tuple[RasterMetadata, ...]:
        return tuple(dataset.metadata for dataset in self._datasets)

    @property
    def fingerprint(self) -> str:
        return _path_fingerprint(
            self.paths,
            namespace=self.surface_kind,
            configuration=(
                self.declared_crs,
                getattr(self, "band", None),
                self.source_id,
                self.source_name,
            ),
        )

    def close(self) -> None:
        for dataset in self._datasets:
            dataset.close()
        self._datasets.clear()

    def _native_query(
        self,
        dataset: _GeoRasterDataset,
        flat_x: np.ndarray,
        flat_y: np.ndarray,
        query_indices: np.ndarray,
        input_crs: str,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        native_x, native_y = self.transform_service.transform_xy(
            flat_x[query_indices],
            flat_y[query_indices],
            input_crs,
            dataset.native_crs,
        )
        left, bottom, right, top = dataset.bounds
        covered = (
            np.isfinite(native_x)
            & np.isfinite(native_y)
            & (native_x >= left)
            & (native_x <= right)
            & (native_y >= bottom)
            & (native_y <= top)
        )
        return np.asarray(native_x), np.asarray(native_y), covered


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
            sampling_progress = (
                lambda fraction, message, _index=dataset_index: progress_callback(
                    (_index + float(fraction)) / dataset_count,
                    message,
                )
                if callable(progress_callback)
                else None
            )
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


@dataclass(frozen=True)
class SurfaceCacheKey:
    geometry_id: str
    source_fingerprints: tuple[str, ...]
    sampling_policy: str

    @property
    def digest(self) -> str:
        digest = hashlib.blake2b(digest_size=20)
        digest.update(self.geometry_id.encode("utf-8", errors="replace"))
        digest.update(self.sampling_policy.encode("utf-8", errors="replace"))
        for fingerprint in self.source_fingerprints:
            digest.update(fingerprint.encode("ascii", errors="replace"))
        return digest.hexdigest()


@dataclass(frozen=True)
class SurfaceSamplingRequest:
    """Typed, cancellable description of one surface publication stage."""

    profile: Any
    visible_radius_m: float | None = None
    view_azimuth_deg: float = 0.0
    view_fov_deg: float = 360.0
    generation: int = 0
    stage: str = "complete"
    fov_margin_deg: float = 10.0
    surface_mode: str | None = None
    viewport_width_px: int | None = None
    viewport_height_px: int | None = None
    # Deprecated compatibility input.
    surface_layer_type: str | None = None

    def __post_init__(self) -> None:
        stage = str(self.stage or "complete")
        if stage not in {"visible_partial", "complete"}:
            raise ValueError(f"Unsupported surface stage: {stage}")
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "view_azimuth_deg", float(self.view_azimuth_deg) % 360.0)
        object.__setattr__(self, "view_fov_deg", min(360.0, max(0.0, float(self.view_fov_deg))))
        object.__setattr__(self, "fov_margin_deg", max(0.0, float(self.fov_margin_deg)))
        normalized_mode = str(
            self.surface_mode or self.surface_layer_type or ""
        ).strip() or None
        object.__setattr__(self, "surface_mode", normalized_mode)
        object.__setattr__(self, "surface_layer_type", normalized_mode)
        for name in ("viewport_width_px", "viewport_height_px"):
            raw_value = getattr(self, name)
            if raw_value is None:
                continue
            value = int(raw_value)
            object.__setattr__(self, name, value if value > 0 else None)
        if self.visible_radius_m is not None:
            radius = float(self.visible_radius_m)
            object.__setattr__(
                self,
                "visible_radius_m",
                radius if math.isfinite(radius) and radius > 0.0 else None,
            )


def _readonly_array(value: Any, dtype=None) -> np.ndarray | None:
    if value is None:
        return None
    array = np.asarray(value, dtype=dtype)
    array.setflags(write=False)
    return array


@dataclass(frozen=True)
class SurfaceSampleCache:
    """Immutable runtime colors aligned with scientific profile geometry."""

    key: SurfaceCacheKey
    source_ids: tuple[str, ...]
    geometry_crs: str
    source_names: tuple[str, ...] = ()
    source_legend_ids: tuple[str, ...] = ()
    # Exact surface sample below the observer.  It closes the sub-metre hole
    # at the centre of the polar mesh without stretching the first radial ring
    # (and all of its angular land-cover cells) to the screen bottom.
    observer_rgba: np.ndarray | None = None
    observer_valid: bool = False
    observer_source_index: int = -1
    observer_class_id: int = -1
    observer_categorical: bool = False
    observer_raster_row: int = -1
    observer_raster_column: int = -1
    observer_lod_factor: int = 1
    observer_sample_origin: int = int(SAMPLE_ORIGIN_UNKNOWN)
    observer_loaded: bool = True
    completion_state: str = "complete"
    # Surface values for the Cartesian near-field DEM patch. They keep their
    # semantic class ids separate from the derived display colour.
    near_patch_rgba: np.ndarray | None = None
    near_patch_valid: np.ndarray | None = None
    near_patch_loaded: np.ndarray | None = None
    near_patch_source_indices: np.ndarray | None = None
    near_patch_class_ids: np.ndarray | None = None
    near_patch_categorical: np.ndarray | None = None
    near_patch_raster_rows: np.ndarray | None = None
    near_patch_raster_columns: np.ndarray | None = None
    near_patch_lod_factors: np.ndarray | None = None
    near_patch_sample_origins: np.ndarray | None = None
    profile_rgba: np.ndarray | None = None
    profile_valid: np.ndarray | None = None
    profile_loaded: np.ndarray | None = None
    profile_source_indices: np.ndarray | None = None
    profile_class_ids: np.ndarray | None = None
    profile_categorical: np.ndarray | None = None
    profile_raster_rows: np.ndarray | None = None
    profile_raster_columns: np.ndarray | None = None
    profile_lod_factors: np.ndarray | None = None
    profile_sample_origins: np.ndarray | None = None
    profile_band_indices: np.ndarray | None = None
    profile_azimuth_indices: np.ndarray | None = None
    relief_rgba: np.ndarray | None = None
    relief_valid: np.ndarray | None = None
    relief_loaded: np.ndarray | None = None
    relief_source_indices: np.ndarray | None = None
    relief_class_ids: np.ndarray | None = None
    relief_categorical: np.ndarray | None = None
    relief_raster_rows: np.ndarray | None = None
    relief_raster_columns: np.ndarray | None = None
    relief_lod_factors: np.ndarray | None = None
    relief_sample_origins: np.ndarray | None = None
    relief_distance_indices: np.ndarray | None = None
    relief_azimuth_indices: np.ndarray | None = None
    # A visual grid may subdivide DEM cells to preserve land-cover detail. Its
    # elevations are interpolated from the DEM and therefore do not invent new
    # topography; only the appearance is sampled at the denser coordinates.
    visual_distances: np.ndarray | None = None
    visual_azimuths: np.ndarray | None = None
    visual_altitudes: np.ndarray | None = None
    visual_elevations: np.ndarray | None = None
    visual_valid: np.ndarray | None = None
    visual_loaded: np.ndarray | None = None
    visual_visible: np.ndarray | None = None
    visual_rgba: np.ndarray | None = None
    visual_source_indices: np.ndarray | None = None
    visual_class_ids: np.ndarray | None = None
    visual_categorical: np.ndarray | None = None
    visual_raster_rows: np.ndarray | None = None
    visual_raster_columns: np.ndarray | None = None
    visual_lod_factors: np.ndarray | None = None
    visual_sample_origins: np.ndarray | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "geometry_crs", normalize_crs(self.geometry_crs))
        state = str(self.completion_state or "complete")
        if state not in {"visible_partial", "complete"}:
            raise ValueError(f"Unsupported surface cache state: {state}")
        object.__setattr__(self, "completion_state", state)
        if self.profile_loaded is None and self.profile_valid is not None:
            object.__setattr__(
                self, "profile_loaded", np.ones(np.asarray(self.profile_valid).shape, dtype=bool)
            )
        if self.near_patch_loaded is None and self.near_patch_valid is not None:
            object.__setattr__(
                self,
                "near_patch_loaded",
                np.ones(np.asarray(self.near_patch_valid).shape, dtype=bool),
            )
        if self.relief_loaded is None and self.relief_valid is not None:
            object.__setattr__(
                self, "relief_loaded", np.ones(np.asarray(self.relief_valid).shape, dtype=bool)
            )
        if self.visual_loaded is None and self.visual_valid is not None:
            object.__setattr__(
                self, "visual_loaded", np.ones(np.asarray(self.visual_valid).shape, dtype=bool)
            )
        for prefix in ("near_patch", "profile", "relief", "visual"):
            rgba = getattr(self, f"{prefix}_rgba")
            valid = getattr(self, f"{prefix}_valid")
            loaded = getattr(self, f"{prefix}_loaded")
            sources = getattr(self, f"{prefix}_source_indices")
            classes = getattr(self, f"{prefix}_class_ids")
            categorical = getattr(self, f"{prefix}_categorical")
            values = (rgba, valid, loaded, sources, classes, categorical)
            if all(value is None for value in values):
                continue
            if valid is None:
                raise ValueError(f"{prefix} material samples require a validity mask")
            shape = np.shape(valid)
            if rgba is not None and np.shape(rgba) != shape + (4,):
                raise ValueError(f"{prefix} RGBA geometry is not aligned")
            for name, value in (
                ("loaded", loaded),
                ("source indices", sources),
                ("class ids", classes),
                ("categorical mask", categorical),
            ):
                if value is not None and np.shape(value) != shape:
                    raise ValueError(
                        f"{prefix} {name} geometry is not aligned with RGBA"
                    )
            for suffix in (
                "raster_rows",
                "raster_columns",
                "lod_factors",
                "sample_origins",
            ):
                value = getattr(self, f"{prefix}_{suffix}")
                if value is not None and np.shape(value) != shape:
                    raise ValueError(
                        f"{prefix} {suffix} geometry is not aligned with RGBA"
                    )
        for name, dtype in (
            ("observer_rgba", np.uint8),
            ("near_patch_rgba", np.uint8),
            ("near_patch_valid", bool),
            ("near_patch_loaded", bool),
            ("near_patch_source_indices", np.int16),
            ("near_patch_class_ids", np.int64),
            ("near_patch_categorical", bool),
            ("near_patch_raster_rows", np.int64),
            ("near_patch_raster_columns", np.int64),
            ("near_patch_lod_factors", np.int16),
            ("near_patch_sample_origins", np.uint8),
            ("profile_rgba", np.uint8),
            ("profile_valid", bool),
            ("profile_loaded", bool),
            ("profile_source_indices", np.int16),
            ("profile_class_ids", np.int64),
            ("profile_categorical", bool),
            ("profile_raster_rows", np.int64),
            ("profile_raster_columns", np.int64),
            ("profile_lod_factors", np.int16),
            ("profile_sample_origins", np.uint8),
            ("profile_band_indices", np.int32),
            ("profile_azimuth_indices", np.int32),
            ("relief_rgba", np.uint8),
            ("relief_valid", bool),
            ("relief_loaded", bool),
            ("relief_source_indices", np.int16),
            ("relief_class_ids", np.int64),
            ("relief_categorical", bool),
            ("relief_raster_rows", np.int64),
            ("relief_raster_columns", np.int64),
            ("relief_lod_factors", np.int16),
            ("relief_sample_origins", np.uint8),
            ("relief_distance_indices", np.int32),
            ("relief_azimuth_indices", np.int32),
            ("visual_distances", np.float32),
            ("visual_azimuths", np.float32),
            ("visual_altitudes", np.float32),
            ("visual_elevations", np.float32),
            ("visual_valid", bool),
            ("visual_loaded", bool),
            ("visual_visible", bool),
            ("visual_rgba", np.uint8),
            ("visual_source_indices", np.int16),
            ("visual_class_ids", np.int64),
            ("visual_categorical", bool),
            ("visual_raster_rows", np.int64),
            ("visual_raster_columns", np.int64),
            ("visual_lod_factors", np.int16),
            ("visual_sample_origins", np.uint8),
        ):
            object.__setattr__(self, name, _readonly_array(getattr(self, name), dtype))

    @property
    def cache_id(self) -> str:
        return self.key.digest


_SURFACE_ARRAY_FIELDS = (
    "observer_rgba",
    "near_patch_rgba",
    "near_patch_valid",
    "near_patch_loaded",
    "near_patch_source_indices",
    "near_patch_class_ids",
    "near_patch_categorical",
    "near_patch_raster_rows",
    "near_patch_raster_columns",
    "near_patch_lod_factors",
    "near_patch_sample_origins",
    "profile_rgba",
    "profile_valid",
    "profile_loaded",
    "profile_source_indices",
    "profile_class_ids",
    "profile_categorical",
    "profile_raster_rows",
    "profile_raster_columns",
    "profile_lod_factors",
    "profile_sample_origins",
    "profile_band_indices",
    "profile_azimuth_indices",
    "relief_rgba",
    "relief_valid",
    "relief_loaded",
    "relief_source_indices",
    "relief_class_ids",
    "relief_categorical",
    "relief_raster_rows",
    "relief_raster_columns",
    "relief_lod_factors",
    "relief_sample_origins",
    "relief_distance_indices",
    "relief_azimuth_indices",
    "visual_distances",
    "visual_azimuths",
    "visual_altitudes",
    "visual_elevations",
    "visual_valid",
    "visual_loaded",
    "visual_visible",
    "visual_rgba",
    "visual_source_indices",
    "visual_class_ids",
    "visual_categorical",
    "visual_raster_rows",
    "visual_raster_columns",
    "visual_lod_factors",
    "visual_sample_origins",
)


def _surface_cache_size(cache: SurfaceSampleCache) -> int:
    return sum(
        int(value.nbytes)
        for name in _SURFACE_ARRAY_FIELDS
        if (value := getattr(cache, name)) is not None
    )


def _surface_cache_payload(
    cache: SurfaceSampleCache,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    metadata = {
        "schema": 4,
        "digest": cache.cache_id,
        "geometry_id": cache.key.geometry_id,
        "source_fingerprints": list(cache.key.source_fingerprints),
        "sampling_policy": cache.key.sampling_policy,
        "source_ids": list(cache.source_ids),
        "source_names": list(cache.source_names),
        "source_legend_ids": list(cache.source_legend_ids),
        "geometry_crs": cache.geometry_crs,
        "observer_valid": bool(cache.observer_valid),
        "observer_source_index": int(cache.observer_source_index),
        "observer_class_id": int(cache.observer_class_id),
        "observer_categorical": bool(cache.observer_categorical),
        "observer_raster_row": int(cache.observer_raster_row),
        "observer_raster_column": int(cache.observer_raster_column),
        "observer_lod_factor": int(cache.observer_lod_factor),
        "observer_sample_origin": int(cache.observer_sample_origin),
        "observer_loaded": bool(cache.observer_loaded),
        "completion_state": cache.completion_state,
    }
    arrays = {
        name: value
        for name in _SURFACE_ARRAY_FIELDS
        if (value := getattr(cache, name)) is not None
    }
    return metadata, arrays


def _surface_cache_from_payload(
    metadata: Mapping[str, Any], arrays: Mapping[str, np.ndarray]
) -> SurfaceSampleCache:
    if int(metadata.get("schema", 0)) != 4:
        raise ValueError("Unsupported surface cache schema")
    key = SurfaceCacheKey(
        geometry_id=str(metadata["geometry_id"]),
        source_fingerprints=tuple(str(value) for value in metadata["source_fingerprints"]),
        sampling_policy=str(metadata["sampling_policy"]),
    )
    if str(metadata.get("digest", "")) != key.digest:
        raise ValueError("Surface cache digest mismatch")
    kwargs = {name: arrays.get(name) for name in _SURFACE_ARRAY_FIELDS}
    return SurfaceSampleCache(
        key=key,
        source_ids=tuple(str(value) for value in metadata.get("source_ids", ())),
        source_names=tuple(
            str(value) for value in metadata.get("source_names", ())
        ),
        source_legend_ids=tuple(
            str(value) for value in metadata.get("source_legend_ids", ())
        ),
        geometry_crs=str(metadata["geometry_crs"]),
        observer_valid=bool(metadata.get("observer_valid", False)),
        observer_source_index=int(metadata.get("observer_source_index", -1)),
        observer_class_id=int(metadata.get("observer_class_id", -1)),
        observer_categorical=bool(metadata.get("observer_categorical", False)),
        observer_raster_row=int(metadata.get("observer_raster_row", -1)),
        observer_raster_column=int(metadata.get("observer_raster_column", -1)),
        observer_lod_factor=int(metadata.get("observer_lod_factor", 1)),
        observer_sample_origin=int(
            metadata.get("observer_sample_origin", SAMPLE_ORIGIN_UNKNOWN)
        ),
        observer_loaded=bool(metadata.get("observer_loaded", True)),
        completion_state=str(metadata.get("completion_state", "complete")),
        **kwargs,
    )


def _hash_array(digest, value: Any) -> None:
    array = np.ascontiguousarray(np.asarray(value))
    digest.update(str(array.dtype).encode("ascii"))
    digest.update(str(array.shape).encode("ascii"))
    digest.update(array.tobytes())


def geometry_fingerprint(profile: Any, *, geometry_crs: str) -> str:
    explicit = getattr(profile, "geometry_id", None)
    digest = hashlib.blake2b(digest_size=20)
    digest.update(b"terrain-geometry-v4-categorical-materials")
    if explicit:
        digest.update(str(explicit).encode("utf-8", errors="replace"))
    digest.update(normalize_crs(geometry_crs).encode("ascii"))
    digest.update(str(float(getattr(profile, "observer_lat", 0.0))).encode("ascii"))
    digest.update(str(float(getattr(profile, "observer_lon", 0.0))).encode("ascii"))
    digest.update(str(getattr(profile, "representation_mode", "legacy")).encode("utf-8"))
    digest.update(
        repr(tuple(getattr(profile, "elevation_source_ids", ()) or ())).encode(
            "utf-8", errors="replace"
        )
    )
    digest.update(
        str(getattr(profile, "effective_elevation_source_id", "") or "").encode(
            "utf-8", errors="replace"
        )
    )
    _hash_array(digest, getattr(profile, "azimuths", np.asarray([], dtype=np.float32)))
    for band in getattr(profile, "bands", ()) or ():
        digest.update(str(band.get("id", "")).encode("utf-8", errors="replace"))
        _hash_array(digest, band.get("dists", np.asarray([], dtype=np.float32)))
        _hash_array(digest, band.get("angles", np.asarray([], dtype=np.float32)))
    mesh = getattr(profile, "terrain_mesh", None)
    if isinstance(mesh, Mapping):
        digest.update(str(mesh.get("version", 1)).encode("ascii"))
        for name in (
            "azimuths",
            "distances",
            "altitudes",
            "valid",
            "near_patch_eastings",
            "near_patch_northings",
            "near_patch_altitudes",
            "near_patch_valid",
        ):
            if name in mesh:
                _hash_array(digest, mesh[name])
    return digest.hexdigest()


def raster_grids_aligned(
    first: RasterMetadata,
    second: RasterMetadata,
    *,
    tolerance: float = 1e-7,
) -> bool:
    """Return whether two rasters share CRS, pixel vectors and pixel centres.

    Equal nominal resolution is insufficient: the affine origins must differ
    by an integer number of pixels.  Callers may use this result for a direct
    index fast path; all other cases continue through world coordinates and
    the normal CRS-aware sampler.
    """

    if normalize_crs(first.native_crs) != normalize_crs(second.native_crs):
        return False
    first_transform = np.asarray(first.extra.get("transform", ()), dtype=np.float64)
    second_transform = np.asarray(second.extra.get("transform", ()), dtype=np.float64)
    if first_transform.shape != (6,) or second_transform.shape != (6,):
        return False
    first_basis = np.asarray(
        ((first_transform[0], first_transform[1]), (first_transform[3], first_transform[4])),
        dtype=np.float64,
    )
    second_basis = np.asarray(
        ((second_transform[0], second_transform[1]), (second_transform[3], second_transform[4])),
        dtype=np.float64,
    )
    scale = max(1.0, float(np.max(np.abs(first_basis))))
    if not np.allclose(first_basis, second_basis, rtol=tolerance, atol=tolerance * scale):
        return False
    try:
        offset = np.linalg.solve(
            first_basis,
            np.asarray(
                (
                    second_transform[2] - first_transform[2],
                    second_transform[5] - first_transform[5],
                ),
                dtype=np.float64,
            ),
        )
    except np.linalg.LinAlgError:
        return False
    return bool(np.allclose(offset, np.rint(offset), atol=tolerance, rtol=0.0))


def _sample_indices(size: int, other_size: int, max_samples: int) -> np.ndarray:
    if size <= 0:
        return np.asarray([], dtype=np.int32)
    permitted = max(1, int(max_samples) // max(1, int(other_size)))
    count = min(int(size), permitted)
    if count >= int(size):
        return np.arange(size, dtype=np.int32)
    return np.unique(np.rint(np.linspace(0, size - 1, count)).astype(np.int32))


def _visible_azimuth_indices(
    indices: np.ndarray,
    azimuths: np.ndarray,
    request: SurfaceSamplingRequest,
) -> np.ndarray:
    if request.stage == "complete":
        return indices
    width = min(360.0, request.view_fov_deg + 2.0 * request.fov_margin_deg)
    if width >= 360.0:
        return indices
    values = np.asarray(azimuths, dtype=np.float64)[indices]
    delta = (values - request.view_azimuth_deg + 180.0) % 360.0 - 180.0
    return indices[np.abs(delta) <= width * 0.5 + 1e-9]


def _lod_factors_for_polar_grid(
    distances: Any,
    azimuths: Any,
    resolution_m: float | None,
    *,
    viewport_width_px: int | None = None,
    view_fov_deg: float | None = None,
    categorical: bool = False,
) -> np.ndarray:
    """Choose the coarsest power-of-two level no larger than half a cell."""

    distance_grid = np.asarray(distances, dtype=np.float64)
    azimuth_axis = np.asarray(azimuths, dtype=np.float64)
    if distance_grid.ndim == 1:
        distance_grid = distance_grid[:, None]
    if distance_grid.ndim != 2:
        return np.ones(distance_grid.shape, dtype=np.int16)
    if distance_grid.shape[1] == 1 and azimuth_axis.size > 1:
        distance_grid = np.broadcast_to(
            distance_grid, (distance_grid.shape[0], azimuth_axis.size)
        )
    resolution = float(resolution_m or 0.0)
    if not math.isfinite(resolution) or resolution <= 0.0:
        return np.ones(distance_grid.shape, dtype=np.int16)
    if distance_grid.shape[0] > 1:
        radial_spacing = np.abs(np.gradient(distance_grid, axis=0))
    else:
        radial_spacing = np.full(distance_grid.shape, np.inf, dtype=np.float64)
    if azimuth_axis.size > 1:
        sorted_azimuths = np.sort(azimuth_axis % 360.0)
        steps = np.diff(np.r_[sorted_azimuths, sorted_azimuths[0] + 360.0])
        positive = steps[steps > 1e-9]
        angular_step = math.radians(
            float(np.median(positive)) if positive.size else 360.0
        )
    else:
        angular_step = math.inf
    angular_spacing = np.abs(distance_grid) * angular_step
    spacing = np.minimum(radial_spacing, angular_spacing)
    maximum = np.floor(spacing / (2.0 * resolution))
    maximum = np.where(np.isfinite(maximum), maximum, 128.0)
    levels = np.asarray((1, 2, 4, 8, 16, 32, 64, 128), dtype=np.int16)
    positions = np.searchsorted(levels, np.maximum(1.0, maximum), side="right") - 1
    result = levels[np.clip(positions, 0, len(levels) - 1)]
    if categorical and viewport_width_px and view_fov_deg:
        fov_radians = math.radians(
            min(360.0, max(1e-3, float(view_fov_deg)))
        )
        pixels_per_radian = float(viewport_width_px) / fov_radians
        projected_native = (
            resolution
            / np.maximum(np.abs(distance_grid), resolution)
            * pixels_per_radian
        )
        maximum_factor = np.maximum(
            1.0, np.floor(2.0 / np.maximum(projected_native, 1e-12))
        )
        cap_positions = (
            np.searchsorted(levels, maximum_factor, side="right") - 1
        )
        screen_cap = levels[
            np.clip(cap_positions, 0, len(levels) - 1)
        ]
        result = np.minimum(result, screen_cap)
        result = np.where(projected_native >= 0.5, 1, result)
    return np.asarray(result, dtype=np.int16)


def _subdivided_axis(values: np.ndarray, factor: int, *, circular: bool = False) -> np.ndarray:
    source = np.asarray(values, dtype=np.float64)
    factor = max(1, int(factor))
    if source.size < 2 or factor == 1:
        return source.astype(np.float32)
    if circular:
        extended = np.concatenate((source, [float(source[0]) + 360.0]))
        parts = [
            np.linspace(extended[index], extended[index + 1], factor, endpoint=False)
            for index in range(source.size)
        ]
        return np.concatenate(parts).astype(np.float32)
    parts = [
        np.linspace(source[index], source[index + 1], factor, endpoint=False)
        for index in range(source.size - 1)
    ]
    return np.concatenate((*parts, source[-1:])).astype(np.float32)


def _interpolate_polar_grid(
    values: Any,
    distances: np.ndarray,
    azimuths: np.ndarray,
    new_distances: np.ndarray,
    new_azimuths: np.ndarray,
    *,
    nearest: bool = False,
) -> np.ndarray:
    """Interpolate a DEM-derived polar grid, wrapping azimuth at 360 degrees."""

    source = np.asarray(values)
    if source.shape != (len(distances), len(azimuths)):
        raise ValueError("Polar grid shape does not match its axes")
    d_hi = np.searchsorted(distances, new_distances, side="right")
    d_hi = np.clip(d_hi, 1, len(distances) - 1)
    d_lo = d_hi - 1
    d_span = np.maximum(distances[d_hi] - distances[d_lo], 1e-9)
    d_t = (new_distances - distances[d_lo]) / d_span

    az = np.asarray(azimuths, dtype=np.float64)
    az_step_values = np.diff(az)
    az_step_values = az_step_values[az_step_values > 0]
    az_step = float(np.median(az_step_values)) if az_step_values.size else 360.0
    a_position = ((new_azimuths - float(az[0])) % 360.0) / max(az_step, 1e-9)
    a_floor = np.floor(a_position)
    a_lo = a_floor.astype(np.int64) % len(az)
    a_hi = (a_lo + 1) % len(az)
    a_t = a_position - a_floor
    if nearest:
        d_index = np.where(d_t < 0.5, d_lo, d_hi)
        a_index = np.where(a_t < 0.5, a_lo, a_hi)
        return source[np.ix_(d_index, a_index)]

    v00 = source[np.ix_(d_lo, a_lo)].astype(np.float64)
    v01 = source[np.ix_(d_lo, a_hi)].astype(np.float64)
    v10 = source[np.ix_(d_hi, a_lo)].astype(np.float64)
    v11 = source[np.ix_(d_hi, a_hi)].astype(np.float64)
    radial_low = v00 * (1.0 - a_t[None, :]) + v01 * a_t[None, :]
    radial_high = v10 * (1.0 - a_t[None, :]) + v11 * a_t[None, :]
    return radial_low * (1.0 - d_t[:, None]) + radial_high * d_t[:, None]


class SurfaceSamplingService:
    """Pre-sample real surface colors for PROFILE and RELIEF geometry."""

    def __init__(
        self,
        providers: Sequence[SurfaceProvider],
        *,
        internal_crs: str = CRS_TERRAIN_INTERNAL,
        transform_service: CoordinateTransformService | None = None,
        max_profile_samples: int = 120_000,
        max_relief_samples: int = 300_000,
        cache_capacity: int = 4,
        cache_bytes: int | None = None,
        persistent_cache_dir: str | os.PathLike[str] | None = None,
        persistent_cache_bytes: int = 2 * 1024**3,
        performance_logging: bool = False,
    ) -> None:
        self.providers = [
            provider for provider in providers if isinstance(provider, (RgbSurfaceProvider, CategoricalSurfaceProvider))
        ]
        self.internal_crs = normalize_crs(internal_crs)
        self.transform_service = transform_service or DEFAULT_TRANSFORM_SERVICE
        self.max_profile_samples = max(1, int(max_profile_samples))
        self.max_relief_samples = max(1, int(max_relief_samples))
        # Kept as a compatibility attribute for callers that still expose the
        # old setting.  Eviction is deliberately governed by bytes, not items.
        self.cache_capacity = max(1, int(cache_capacity))
        if cache_bytes is None:
            cache_bytes = DEFAULT_PERFORMANCE_BUDGET.transient_bytes // 2
        self.cache_bytes = max(0, int(cache_bytes))
        self._cache: ByteLRU[str, SurfaceSampleCache] = ByteLRU(self.cache_bytes)
        self._cache_lock = threading.RLock()
        self._persistent_store = (
            AtomicNpzStore(
                persistent_cache_dir, budget_bytes=persistent_cache_bytes
            )
            if persistent_cache_dir is not None
            else None
        )
        self.performance_logging = bool(performance_logging)
        for provider in self.providers:
            if isinstance(provider, CategoricalSurfaceProvider):
                provider.tile_store = self._persistent_store

    @property
    def source_fingerprints(self) -> tuple[str, ...]:
        return tuple(provider.fingerprint for provider in self.providers)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(provider.source_id or provider.fingerprint for provider in self.providers)

    @property
    def source_names(self) -> tuple[str, ...]:
        return tuple(
            provider.source_name or provider.source_id or provider.fingerprint
            for provider in self.providers
        )

    @property
    def source_legend_ids(self) -> tuple[str, ...]:
        return tuple(
            str(getattr(provider, "legend_id", "") or "").strip().lower()
            for provider in self.providers
        )

    def close(self) -> None:
        with self._cache_lock:
            self._cache.clear()
        for provider in self.providers:
            provider.close()

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def _nominal_surface_resolution(self) -> float | None:
        values = [
            float(metadata.resolution_m)
            for provider in self.providers
            for metadata in provider.metadata
            if metadata.resolution_m is not None
            and math.isfinite(float(metadata.resolution_m))
            and float(metadata.resolution_m) > 0.0
        ]
        return min(values) if values else None

    def _visual_subdivision(
        self,
        distances: np.ndarray,
        azimuths: np.ndarray,
        request: SurfaceSamplingRequest | None = None,
    ) -> tuple[int, int]:
        resolution = self._nominal_surface_resolution()
        if resolution is None or len(distances) < 2 or len(azimuths) < 2:
            return 1, 1
        radial_steps = np.diff(distances)
        radial_steps = radial_steps[np.isfinite(radial_steps) & (radial_steps > 0)]
        azimuth_steps = np.diff(azimuths)
        azimuth_steps = azimuth_steps[
            np.isfinite(azimuth_steps) & (azimuth_steps > 0)
        ]
        radial_spacing = float(np.median(radial_steps)) if radial_steps.size else resolution
        angular_spacing = (
            float(distances[-1])
            * math.radians(float(np.median(azimuth_steps)))
            if azimuth_steps.size
            else resolution
        )
        radial_factor = min(4, max(1, int(math.ceil(radial_spacing / resolution))))
        angular_factor = min(4, max(1, int(math.ceil(angular_spacing / resolution))))
        categorical = any(
            isinstance(provider, CategoricalSurfaceProvider)
            for provider in self.providers
        )
        if (
            categorical
            and request is not None
            and request.viewport_width_px
            and request.view_fov_deg > 0.0
        ):
            pixels_per_radian = float(request.viewport_width_px) / math.radians(
                max(1e-3, request.view_fov_deg)
            )
            midpoints = np.maximum(
                resolution,
                (
                    np.asarray(distances[:-1], dtype=np.float64)
                    + np.asarray(distances[1:], dtype=np.float64)
                )
                * 0.5,
            )
            radial_edges_px = (
                np.diff(np.asarray(distances, dtype=np.float64))
                / midpoints
                * pixels_per_radian
            )
            radial_factor = max(
                radial_factor,
                int(
                    math.ceil(
                        float(np.max(radial_edges_px, initial=0.0)) / 8.0
                    )
                ),
            )
            angular_step_rad = (
                math.radians(float(np.median(azimuth_steps)))
                if azimuth_steps.size
                else 0.0
            )
            angular_factor = max(
                angular_factor,
                int(math.ceil(angular_step_rad * pixels_per_radian / 8.0)),
            )
            radial_factor = min(32, radial_factor)
            angular_factor = min(32, angular_factor)
        while (
            ((len(distances) - 1) * radial_factor + 1)
            * (len(azimuths) * angular_factor)
            > self.max_relief_samples
            and (radial_factor > 1 or angular_factor > 1)
        ):
            if angular_factor >= radial_factor and angular_factor > 1:
                angular_factor -= 1
            elif radial_factor > 1:
                radial_factor -= 1
        return radial_factor, angular_factor

    def _adaptive_visual_axes(
        self,
        distances: np.ndarray,
        azimuths: np.ndarray,
        request: SurfaceSamplingRequest,
    ) -> tuple[np.ndarray, np.ndarray, bool]:
        """Build a budgeted screen-space grid for categorical materials."""

        radial_factor, angular_factor = self._visual_subdivision(
            distances, azimuths, request
        )
        categorical = any(
            isinstance(provider, CategoricalSurfaceProvider)
            for provider in self.providers
        )
        if (
            not categorical
            or not request.viewport_width_px
            or request.view_fov_deg <= 0.0
        ):
            return (
                _subdivided_axis(distances, radial_factor),
                _subdivided_axis(azimuths, angular_factor, circular=True),
                False,
            )

        resolution = float(self._nominal_surface_resolution() or 1.0)
        pixels_per_radian = float(request.viewport_width_px) / math.radians(
            max(1e-3, request.view_fov_deg)
        )
        distances64 = np.asarray(distances, dtype=np.float64)
        midpoints = np.maximum(
            resolution, (distances64[:-1] + distances64[1:]) * 0.5
        )
        projected = (
            np.diff(distances64) / midpoints * pixels_per_radian
        )
        desired = np.clip(np.ceil(projected / 8.0), 1, 32).astype(np.int32)
        angular_count = max(1, len(azimuths) * angular_factor)
        maximum_radial_points = max(
            2, self.max_relief_samples // angular_count
        )
        permitted_intervals = max(
            len(desired), maximum_radial_points - 1
        )
        factors = np.ones(len(desired), dtype=np.int32)
        extra = max(0, permitted_intervals - len(desired))
        # Allocate scarce subdivisions to the edges with the greatest current
        # screen-space excess.  Dividing by distance gives near-field ties a
        # stable priority without increasing the global vertex count.
        while extra > 0 and np.any(factors < desired):
            score = np.where(
                factors < desired,
                projected / factors / np.maximum(midpoints, resolution),
                -np.inf,
            )
            chosen = int(np.argmax(score))
            factors[chosen] += 1
            extra -= 1
        budget_limited = bool(np.any(factors < desired))
        radial_parts = [
            np.linspace(
                distances64[index],
                distances64[index + 1],
                int(factors[index]),
                endpoint=False,
            )
            for index in range(len(factors))
        ]
        radial_axis = np.concatenate(
            (*radial_parts, distances64[-1:])
        ).astype(np.float32)
        angular_axis = _subdivided_axis(
            azimuths, angular_factor, circular=True
        )
        return radial_axis, angular_axis, budget_limited

    def sample_rgba_points(
        self,
        x: Any,
        y: Any,
        *,
        input_crs: str,
        lod_factors: Any = None,
        progress_callback=None,
        abort_check=None,
    ) -> RgbaSampleBatch:
        x_arr, y_arr = np.broadcast_arrays(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
        requested_shape = x_arr.shape
        dedupe_inverse = None
        lod_arr = None
        if lod_factors is not None:
            lod_arr = np.broadcast_to(
                np.asarray(lod_factors, dtype=np.int16), requested_shape
            )
        if (
            lod_arr is not None
            and PERFORMANCE_FLAGS.surface_lod_cache
            and self.providers
            and all(isinstance(provider, CategoricalSurfaceProvider) for provider in self.providers)
        ):
            flat_x = x_arr.ravel()
            flat_y = y_arr.ravel()
            flat_lod = lod_arr.ravel()
            coordinates = np.empty(
                flat_x.size,
                dtype=[("x", np.float64), ("y", np.float64), ("lod", np.int16)],
            )
            coordinates["x"] = flat_x
            coordinates["y"] = flat_y
            coordinates["lod"] = flat_lod
            _unique, unique_indices, dedupe_inverse = np.unique(
                coordinates, return_index=True, return_inverse=True
            )
            x_arr = flat_x[unique_indices]
            y_arr = flat_y[unique_indices]
            lod_arr = flat_lod[unique_indices]
        rgba = np.zeros(x_arr.shape + (4,), dtype=np.uint8)
        valid = np.zeros(x_arr.shape, dtype=bool)
        provenance = np.full(x_arr.shape, -1, dtype=np.int16)
        class_ids = np.full(x_arr.shape, -1, dtype=np.int64)
        categorical = np.zeros(x_arr.shape, dtype=bool)
        raster_rows = np.full(x_arr.shape, -1, dtype=np.int64)
        raster_columns = np.full(x_arr.shape, -1, dtype=np.int64)
        sampled_lod = np.ones(x_arr.shape, dtype=np.int16)
        sample_origins = np.zeros(x_arr.shape, dtype=np.uint8)

        # LayerSelectionService orders automatic chains as categorical then
        # RGB. Preserve its order so a manual RGB choice remains first while
        # other categorical sources can still fill nodata.
        ordered = list(self.providers)
        provider_to_index = {id(provider): index for index, provider in enumerate(self.providers)}
        provider_count = max(1, len(ordered))
        for provider_position, provider in enumerate(ordered):
            if callable(abort_check) and abort_check():
                from TerraLab.terrain.providers import RasterSamplingCancelled

                raise RasterSamplingCancelled("Surface sampling cancelled")
            if np.all(valid):
                break
            def provider_progress(fraction, message, *, _position=provider_position):
                if callable(progress_callback):
                    progress_callback(
                        (_position + max(0.0, min(1.0, float(fraction))))
                        / provider_count,
                        message,
                    )

            if isinstance(provider, RgbSurfaceProvider):
                sample_method = provider.sample_rgba
                sample_kwargs = {"input_crs": input_crs}
                if "progress_callback" in inspect.signature(sample_method).parameters:
                    sample_kwargs.update(
                        progress_callback=provider_progress,
                        abort_check=abort_check,
                    )
                batch = sample_method(x_arr, y_arr, **sample_kwargs)
                candidate_rgba = batch.rgba
                candidate_valid = batch.valid
            else:
                sample_method = provider.sample_classes
                sample_kwargs = {"input_crs": input_crs}
                if lod_arr is not None and "lod_factors" in inspect.signature(sample_method).parameters:
                    sample_kwargs["lod_factors"] = lod_arr
                if "progress_callback" in inspect.signature(sample_method).parameters:
                    sample_kwargs.update(
                        progress_callback=provider_progress,
                        abort_check=abort_check,
                    )
                classes = sample_method(x_arr, y_arr, **sample_kwargs)
                mapped = provider.classes_to_rgba(
                    classes.classes,
                    x=x_arr,
                    y=y_arr,
                )
                candidate_rgba = mapped.rgba
                candidate_valid = classes.valid & mapped.valid
            take = ~valid & candidate_valid
            rgba[take] = candidate_rgba[take]
            valid[take] = True
            provenance[take] = int(provider_to_index[id(provider)])
            provider_rows = getattr(batch if isinstance(provider, RgbSurfaceProvider) else classes, "raster_rows", None)
            provider_columns = getattr(batch if isinstance(provider, RgbSurfaceProvider) else classes, "raster_columns", None)
            provider_lod = getattr(batch if isinstance(provider, RgbSurfaceProvider) else classes, "lod_factors", None)
            provider_origins = getattr(batch if isinstance(provider, RgbSurfaceProvider) else classes, "sample_origins", None)
            if provider_rows is not None:
                raster_rows[take] = np.asarray(provider_rows)[take]
            if provider_columns is not None:
                raster_columns[take] = np.asarray(provider_columns)[take]
            if provider_lod is not None:
                sampled_lod[take] = np.asarray(provider_lod)[take]
            elif lod_arr is not None:
                sampled_lod[take] = _normalized_lod_factors(lod_arr)[take]
            if provider_position > 0:
                sample_origins[take] = SAMPLE_ORIGIN_SOURCE_FALLBACK
            elif provider_origins is not None:
                sample_origins[take] = np.asarray(provider_origins)[take]
            else:
                sample_origins[take] = SAMPLE_ORIGIN_EXACT
            if isinstance(provider, CategoricalSurfaceProvider):
                class_ids[take] = classes.classes[take]
                categorical[take] = True
            provider_progress(1.0, "sampling-provider")
        if callable(progress_callback):
            progress_callback(1.0, "sampling-complete")
        if dedupe_inverse is not None:
            return RgbaSampleBatch(
                rgba[dedupe_inverse].reshape(requested_shape + (4,)),
                valid[dedupe_inverse].reshape(requested_shape),
                provenance[dedupe_inverse].reshape(requested_shape),
                class_ids[dedupe_inverse].reshape(requested_shape),
                categorical[dedupe_inverse].reshape(requested_shape),
                raster_rows[dedupe_inverse].reshape(requested_shape),
                raster_columns[dedupe_inverse].reshape(requested_shape),
                sampled_lod[dedupe_inverse].reshape(requested_shape),
                sample_origins[dedupe_inverse].reshape(requested_shape),
            )
        return RgbaSampleBatch(
            rgba,
            valid,
            provenance,
            class_ids,
            categorical,
            raster_rows,
            raster_columns,
            sampled_lod,
            sample_origins,
        )

    def _observer_xy(self, profile: Any, geometry_crs: str) -> tuple[float, float]:
        observer_x = getattr(profile, "observer_x", None)
        observer_y = getattr(profile, "observer_y", None)
        if observer_x is not None and observer_y is not None:
            try:
                if math.isfinite(float(observer_x)) and math.isfinite(float(observer_y)):
                    return float(observer_x), float(observer_y)
            except Exception:
                pass
        lon = float(getattr(profile, "observer_lon", 0.0))
        lat = float(getattr(profile, "observer_lat", 0.0))
        x, y = self.transform_service.transform_xy(lon, lat, CRS_GEOGRAPHIC, geometry_crs)
        return float(x), float(y)

    def sample_profile(
        self,
        profile: Any,
        *,
        geometry_id: str | None = None,
        request: SurfaceSamplingRequest | None = None,
        progress_callback=None,
        abort_check=None,
    ) -> SurfaceSampleCache:
        start = time.perf_counter()
        rss_start, peak_start = process_memory_bytes()
        if request is not None:
            profile = request.profile
        else:
            request = SurfaceSamplingRequest(profile=profile)
        metric_datasets = [
            dataset
            for provider in self.providers
            if isinstance(provider, CategoricalSurfaceProvider)
            for dataset in provider._datasets
        ]
        categorical_lod = any(
            isinstance(provider, CategoricalSurfaceProvider)
            for provider in self.providers
        )
        metric_baseline = {
            id(dataset): (
                int(dataset.bytes_read),
                int(dataset.lod_rows_read),
                int(dataset.lod_intervals_read),
                int(dataset.lod_windows_read),
                int(dataset.lod_pixels_decoded),
                int(dataset.lod_modal_cells),
                int(dataset.lod_requested),
                int(dataset.lod_unique),
                int(dataset.lod_cache_hits),
            )
            for dataset in metric_datasets
        }
        visible_radius = request.visible_radius_m
        if visible_radius is None:
            try:
                candidate_radius = float(getattr(profile, "resolved_radius_m", 0.0) or 0.0)
                visible_radius = candidate_radius if candidate_radius > 0.0 else None
            except (TypeError, ValueError):
                visible_radius = None

        def report(percent: float, phase: str) -> None:
            if callable(progress_callback):
                progress_callback(
                    max(0.0, min(100.0, float(percent))), str(phase)
                )

        def check_cancelled() -> None:
            if callable(abort_check) and abort_check():
                from TerraLab.terrain.providers import RasterSamplingCancelled

                raise RasterSamplingCancelled("Surface sampling cancelled")

        report(0.0, "preparing")
        check_cancelled()
        geometry_crs = normalize_crs(
            getattr(profile, "geometry_crs", None) or self.internal_crs
        )
        profile_geometry_key = geometry_fingerprint(profile, geometry_crs=geometry_crs)
        profile_geometry_id = str(getattr(profile, "geometry_id", "") or "")
        if geometry_id and str(geometry_id) != profile_geometry_id:
            digest = hashlib.blake2b(digest_size=20)
            digest.update(str(geometry_id).encode("utf-8", errors="replace"))
            digest.update(profile_geometry_key.encode("ascii"))
            geometry_key = digest.hexdigest()
        else:
            geometry_key = profile_geometry_key
        policy_azimuth = request.view_azimuth_deg if request.stage == "visible_partial" else 0.0
        policy_fov = request.view_fov_deg if request.stage == "visible_partial" else 360.0
        policy_margin = request.fov_margin_deg if request.stage == "visible_partial" else 0.0
        policy = (
            f"surface-v{SURFACE_CACHE_POLICY_VERSION}:profile={self.max_profile_samples}:"
            f"relief={self.max_relief_samples}:rgb-nearest:categorical-nearest-lod:"
            f"mode={request.surface_mode or 'automatic'}:"
            f"stage={request.stage}:radius={visible_radius}:"
            f"azimuth={policy_azimuth:.6f}:fov={policy_fov:.6f}:"
            f"margin={policy_margin:.6f}"
            f":viewport={request.viewport_width_px}x{request.viewport_height_px}"
        )
        key = SurfaceCacheKey(
            geometry_id=str(geometry_key),
            source_fingerprints=self.source_fingerprints,
            sampling_policy=policy,
        )
        with self._cache_lock:
            cached = self._cache.get(key.digest)
            if cached is not None:
                report(100.0, "cache-ready")
                if self.performance_logging:
                    append_perf_event(
                        "surface.sampling",
                        phase="cache-ready",
                        cache="memory",
                        stage=request.stage,
                        generation=int(request.generation),
                        elapsed_ms=int((time.perf_counter() - start) * 1000.0),
                    )
                return cached
        if self._persistent_store is not None:
            loaded = self._persistent_store.load(f"results/{key.digest}")
            if loaded is not None:
                try:
                    cached = _surface_cache_from_payload(*loaded)
                    if cached.key == key:
                        with self._cache_lock:
                            self._cache.put(
                                cached.cache_id, cached, _surface_cache_size(cached)
                            )
                        report(100.0, "persistent-cache-ready")
                        if self.performance_logging:
                            append_perf_event(
                                "surface.sampling",
                                phase="persistent-cache-ready",
                                cache="persistent",
                                stage=request.stage,
                                generation=int(request.generation),
                                elapsed_ms=int(
                                    (time.perf_counter() - start) * 1000.0
                                ),
                                result_cache=self._persistent_store.metrics(),
                            )
                        return cached
                except (KeyError, TypeError, ValueError):
                    pass

        report(4.0, "transforming-coordinates")
        check_cancelled()
        observer_x, observer_y = self._observer_xy(profile, geometry_crs)
        observer_samples = self.sample_rgba_points(
            np.asarray(observer_x, dtype=np.float64),
            np.asarray(observer_y, dtype=np.float64),
            input_crs=geometry_crs,
            lod_factors=np.asarray(1, dtype=np.int16),
            progress_callback=lambda fraction, phase: report(
                4.0 + float(fraction), f"observer:{phase}"
            ),
            abort_check=abort_check,
        )
        observer_valid = bool(np.asarray(observer_samples.valid).item())
        observer_rgba = (
            np.asarray(observer_samples.rgba, dtype=np.uint8).reshape(4)
            if observer_valid
            else None
        )
        observer_source_index = (
            int(np.asarray(observer_samples.source_indices).item())
            if observer_valid and observer_samples.source_indices is not None
            else -1
        )
        observer_class_id = (
            int(np.asarray(observer_samples.class_ids).item())
            if observer_valid and observer_samples.class_ids is not None
            else -1
        )
        observer_categorical = bool(
            observer_valid
            and observer_samples.categorical is not None
            and np.asarray(observer_samples.categorical).item()
        )
        observer_raster_row = (
            int(np.asarray(observer_samples.raster_rows).item())
            if observer_valid and observer_samples.raster_rows is not None
            else -1
        )
        observer_raster_column = (
            int(np.asarray(observer_samples.raster_columns).item())
            if observer_valid and observer_samples.raster_columns is not None
            else -1
        )
        observer_lod_factor = (
            int(np.asarray(observer_samples.lod_factors).item())
            if observer_samples.lod_factors is not None
            else 1
        )
        observer_sample_origin = (
            int(np.asarray(observer_samples.sample_origins).item())
            if observer_samples.sample_origins is not None
            else int(SAMPLE_ORIGIN_UNKNOWN)
        )
        near_patch_rgba = None
        near_patch_valid = None
        near_patch_sources = None
        near_patch_classes = None
        near_patch_categorical = None
        near_patch_raster_rows = None
        near_patch_raster_columns = None
        near_patch_lod_factors = None
        near_patch_sample_origins = None
        mesh = getattr(profile, "terrain_mesh", None)
        if isinstance(mesh, Mapping):
            patch_eastings = np.asarray(
                mesh.get("near_patch_eastings", ()), dtype=np.float64
            )
            patch_northings = np.asarray(
                mesh.get("near_patch_northings", ()), dtype=np.float64
            )
            patch_shape = (patch_northings.size, patch_eastings.size)
            patch_mesh_valid = np.asarray(
                mesh.get("near_patch_valid", ()), dtype=bool
            )
            if (
                patch_shape[0] >= 2
                and patch_shape[1] >= 2
                and patch_mesh_valid.shape == patch_shape
            ):
                patch_east, patch_north = np.meshgrid(
                    patch_eastings, patch_northings
                )
                patch_samples = self.sample_rgba_points(
                    observer_x + patch_east,
                    observer_y + patch_north,
                    input_crs=geometry_crs,
                    lod_factors=np.ones(patch_shape, dtype=np.int16),
                    progress_callback=lambda fraction, phase: report(
                        5.0 + 5.0 * float(fraction), f"near-patch:{phase}"
                    ),
                    abort_check=abort_check,
                )
                near_patch_valid = patch_mesh_valid & patch_samples.valid
                near_patch_rgba = np.where(
                    near_patch_valid[..., None], patch_samples.rgba, 0
                ).astype(np.uint8)
                near_patch_sources = np.where(
                    near_patch_valid, patch_samples.source_indices, -1
                ).astype(np.int16)
                near_patch_classes = np.where(
                    near_patch_valid,
                    patch_samples.class_ids
                    if patch_samples.class_ids is not None
                    else -1,
                    -1,
                ).astype(np.int64)
                near_patch_categorical = near_patch_valid & (
                    patch_samples.categorical
                    if patch_samples.categorical is not None
                    else False
                )
                near_patch_raster_rows = np.where(
                    near_patch_valid, patch_samples.raster_rows, -1
                ).astype(np.int64)
                near_patch_raster_columns = np.where(
                    near_patch_valid, patch_samples.raster_columns, -1
                ).astype(np.int64)
                near_patch_lod_factors = np.asarray(
                    patch_samples.lod_factors, dtype=np.int16
                )
                near_patch_sample_origins = np.where(
                    near_patch_valid,
                    patch_samples.sample_origins,
                    SAMPLE_ORIGIN_UNKNOWN,
                ).astype(np.uint8)
        azimuths = np.asarray(getattr(profile, "azimuths", ()), dtype=np.float64)
        bands = list(getattr(profile, "bands", ()) or ())

        band_indices = _sample_indices(
            len(bands), max(1, len(azimuths)), self.max_profile_samples
        )
        profile_azimuth_indices = _sample_indices(
            len(azimuths), max(1, len(band_indices)), self.max_profile_samples
        )
        profile_azimuth_indices = _visible_azimuth_indices(
            profile_azimuth_indices, azimuths, request
        )
        profile_rgba = np.zeros(
            (len(band_indices), len(profile_azimuth_indices), 4), dtype=np.uint8
        )
        profile_valid = np.zeros(profile_rgba.shape[:-1], dtype=bool)
        profile_sources = np.full(profile_valid.shape, -1, dtype=np.int16)
        profile_classes = np.full(profile_valid.shape, -1, dtype=np.int64)
        profile_categorical = np.zeros(profile_valid.shape, dtype=bool)
        profile_raster_rows = np.full(profile_valid.shape, -1, dtype=np.int64)
        profile_raster_columns = np.full(profile_valid.shape, -1, dtype=np.int64)
        profile_lod_factors = np.ones(profile_valid.shape, dtype=np.int16)
        profile_sample_origins = np.zeros(profile_valid.shape, dtype=np.uint8)
        if band_indices.size and profile_azimuth_indices.size:
            sampled_bands = [bands[index] for index in band_indices]
            sampled_azimuths = azimuths[profile_azimuth_indices]
            convergence = float(
                getattr(profile, "grid_convergence_deg", 0.0) or 0.0
            )
            azimuth_radians = np.deg2rad(
                sampled_azimuths - convergence
            )[None, :]
            distances = np.stack(
                [
                    np.asarray(
                        band.get(
                            "surface_dists",
                            band.get("dists", np.zeros(len(azimuths))),
                        ),
                        dtype=np.float64,
                    )[profile_azimuth_indices]
                    for band in sampled_bands
                ],
                axis=0,
            )
            angle_valid = np.stack(
                [
                    np.asarray(band.get("angles", np.full(len(azimuths), -np.inf)), dtype=np.float64)[
                        profile_azimuth_indices
                    ]
                    for band in sampled_bands
                ],
                axis=0,
            )
            position_valid = (
                np.isfinite(distances)
                & (distances > 0.0)
                & np.isfinite(angle_valid)
                & (angle_valid > -math.pi / 2.0)
            )
            if visible_radius is not None:
                position_valid &= distances <= float(visible_radius)
            x = observer_x + distances * np.sin(azimuth_radians)
            y = observer_y + distances * np.cos(azimuth_radians)
            x = np.where(position_valid, x, np.nan)
            y = np.where(position_valid, y, np.nan)
            samples = self.sample_rgba_points(
                x,
                y,
                input_crs=geometry_crs,
                lod_factors=_lod_factors_for_polar_grid(
                    distances,
                    sampled_azimuths,
                    self._nominal_surface_resolution(),
                    viewport_width_px=request.viewport_width_px,
                    view_fov_deg=request.view_fov_deg,
                    categorical=categorical_lod,
                ),
                progress_callback=lambda fraction, phase: report(
                    10.0 + 20.0 * float(fraction), f"profile:{phase}"
                ),
                abort_check=abort_check,
            )
            profile_valid = samples.valid & position_valid
            profile_rgba = np.where(profile_valid[..., None], samples.rgba, 0).astype(np.uint8)
            profile_sources = np.where(
                profile_valid, samples.source_indices, -1
            ).astype(np.int16)
            if samples.class_ids is not None:
                profile_classes = np.where(
                    profile_valid, samples.class_ids, -1
                ).astype(np.int64)
            if samples.categorical is not None:
                profile_categorical = profile_valid & samples.categorical
            profile_raster_rows = np.where(
                profile_valid, samples.raster_rows, -1
            ).astype(np.int64)
            profile_raster_columns = np.where(
                profile_valid, samples.raster_columns, -1
            ).astype(np.int64)
            profile_lod_factors = np.asarray(
                samples.lod_factors, dtype=np.int16
            )
            profile_sample_origins = np.where(
                profile_valid,
                samples.sample_origins,
                SAMPLE_ORIGIN_UNKNOWN,
            ).astype(np.uint8)

        report(32.0, "profile-ready")
        check_cancelled()

        relief_rgba = None
        relief_valid = None
        relief_sources = None
        relief_classes = None
        relief_categorical = None
        relief_raster_rows = None
        relief_raster_columns = None
        relief_lod_factors = None
        relief_sample_origins = None
        relief_distance_indices = None
        relief_azimuth_indices = None
        if isinstance(mesh, Mapping):
            mesh_azimuths = np.asarray(mesh.get("azimuths", ()), dtype=np.float64)
            mesh_distances = np.asarray(mesh.get("distances", ()), dtype=np.float64)
            if mesh_azimuths.size and mesh_distances.size:
                relief_distance_indices = _sample_indices(
                    len(mesh_distances), len(mesh_azimuths), self.max_relief_samples
                )
                relief_azimuth_indices = _sample_indices(
                    len(mesh_azimuths), len(relief_distance_indices), self.max_relief_samples
                )
                if visible_radius is not None:
                    relief_distance_indices = relief_distance_indices[
                        mesh_distances[relief_distance_indices] <= float(visible_radius)
                    ]
                relief_azimuth_indices = _visible_azimuth_indices(
                    relief_azimuth_indices, mesh_azimuths, request
                )
                selected_distances = mesh_distances[relief_distance_indices]
                selected_azimuths = mesh_azimuths[relief_azimuth_indices]
                convergence = float(
                    getattr(profile, "grid_convergence_deg", 0.0) or 0.0
                )
                azimuth_radians = np.deg2rad(
                    selected_azimuths - convergence
                )[None, :]
                x = observer_x + selected_distances[:, None] * np.sin(azimuth_radians)
                y = observer_y + selected_distances[:, None] * np.cos(azimuth_radians)
                samples = self.sample_rgba_points(
                    x,
                    y,
                    input_crs=geometry_crs,
                    lod_factors=_lod_factors_for_polar_grid(
                        selected_distances,
                        selected_azimuths,
                        self._nominal_surface_resolution(),
                        viewport_width_px=request.viewport_width_px,
                        view_fov_deg=request.view_fov_deg,
                        categorical=categorical_lod,
                    ),
                    progress_callback=lambda fraction, phase: report(
                        34.0 + 28.0 * float(fraction), f"relief:{phase}"
                    ),
                    abort_check=abort_check,
                )
                mesh_valid_raw = np.asarray(
                    mesh.get("valid", np.ones((len(mesh_distances), len(mesh_azimuths)))), dtype=bool
                )
                if mesh_valid_raw.shape == (len(mesh_distances), len(mesh_azimuths)):
                    mesh_valid = mesh_valid_raw[np.ix_(relief_distance_indices, relief_azimuth_indices)]
                else:
                    mesh_valid = np.ones(samples.valid.shape, dtype=bool)
                relief_valid = samples.valid & mesh_valid
                relief_rgba = np.where(relief_valid[..., None], samples.rgba, 0).astype(np.uint8)
                relief_sources = np.where(relief_valid, samples.source_indices, -1).astype(np.int16)
                relief_classes = np.where(
                    relief_valid,
                    samples.class_ids
                    if samples.class_ids is not None
                    else -1,
                    -1,
                ).astype(np.int64)
                relief_categorical = relief_valid & (
                    samples.categorical
                    if samples.categorical is not None
                    else False
                )
                relief_raster_rows = np.where(
                    relief_valid, samples.raster_rows, -1
                ).astype(np.int64)
                relief_raster_columns = np.where(
                    relief_valid, samples.raster_columns, -1
                ).astype(np.int64)
                relief_lod_factors = np.asarray(
                    samples.lod_factors, dtype=np.int16
                )
                relief_sample_origins = np.where(
                    relief_valid,
                    samples.sample_origins,
                    SAMPLE_ORIGIN_UNKNOWN,
                ).astype(np.uint8)

        report(64.0, "relief-ready")
        check_cancelled()

        visual_distances = visual_azimuths = None
        visual_altitudes = visual_elevations = None
        visual_valid = visual_visible = None
        visual_rgba = visual_sources = visual_classes = visual_categorical = None
        visual_raster_rows = visual_raster_columns = None
        visual_lod_factors = visual_sample_origins = None
        if isinstance(mesh, Mapping):
            mesh_azimuths = np.asarray(mesh.get("azimuths", ()), dtype=np.float64)
            mesh_distances = np.asarray(mesh.get("distances", ()), dtype=np.float64)
            mesh_shape = (len(mesh_distances), len(mesh_azimuths))
            mesh_altitudes = np.asarray(mesh.get("altitudes", ()), dtype=np.float32)
            mesh_elevations = np.asarray(mesh.get("elevations", ()), dtype=np.float32)
            mesh_valid = np.asarray(mesh.get("valid", ()), dtype=bool)
            mesh_visible = np.asarray(mesh.get("visible", mesh_valid), dtype=bool)
            if (
                mesh_shape[0] >= 2
                and mesh_shape[1] >= 2
                and mesh_altitudes.shape == mesh_shape
                and mesh_elevations.shape == mesh_shape
                and mesh_valid.shape == mesh_shape
            ):
                (
                    candidate_visual_distances,
                    candidate_visual_azimuths,
                    visual_budget_limited,
                ) = self._adaptive_visual_axes(
                    mesh_distances, mesh_azimuths, request
                )
                if (
                    len(candidate_visual_distances) > len(mesh_distances)
                    or len(candidate_visual_azimuths) > len(mesh_azimuths)
                ):
                    report(68.0, "subdividing-visual-grid")
                    visual_distances = candidate_visual_distances
                    visual_azimuths = candidate_visual_azimuths
                    if visual_budget_limited and self.performance_logging:
                        append_perf_event(
                            "surface.visual_budget",
                            target_edge_px=8.0,
                            requested_samples=int(
                                len(candidate_visual_distances)
                                * len(candidate_visual_azimuths)
                            ),
                            max_relief_samples=int(self.max_relief_samples),
                        )
                    if visible_radius is not None:
                        visual_distances = visual_distances[
                            visual_distances <= float(visible_radius)
                        ]
                    if request.stage == "visible_partial":
                        visible_azimuth_indices = _visible_azimuth_indices(
                            np.arange(len(visual_azimuths), dtype=np.int32),
                            visual_azimuths,
                            request,
                        )
                        visual_azimuths = visual_azimuths[visible_azimuth_indices]
                    visual_altitudes = _interpolate_polar_grid(
                        mesh_altitudes,
                        mesh_distances,
                        mesh_azimuths,
                        visual_distances,
                        visual_azimuths,
                    ).astype(np.float32)
                    visual_elevations = _interpolate_polar_grid(
                        mesh_elevations,
                        mesh_distances,
                        mesh_azimuths,
                        visual_distances,
                        visual_azimuths,
                    ).astype(np.float32)
                    visual_valid = _interpolate_polar_grid(
                        mesh_valid,
                        mesh_distances,
                        mesh_azimuths,
                        visual_distances,
                        visual_azimuths,
                        nearest=True,
                    ).astype(bool)
                    visual_visible = _interpolate_polar_grid(
                        mesh_visible
                        if mesh_visible.shape == mesh_shape
                        else mesh_valid,
                        mesh_distances,
                        mesh_azimuths,
                        visual_distances,
                        visual_azimuths,
                        nearest=True,
                    ).astype(bool)
                    check_cancelled()
                    report(74.0, "sampling-visual-detail")
                    convergence = float(
                        getattr(profile, "grid_convergence_deg", 0.0) or 0.0
                    )
                    radians = np.deg2rad(
                        visual_azimuths - convergence
                    )[None, :]
                    x = observer_x + visual_distances[:, None] * np.sin(radians)
                    y = observer_y + visual_distances[:, None] * np.cos(radians)
                    visual_samples = self.sample_rgba_points(
                        x,
                        y,
                        input_crs=geometry_crs,
                        lod_factors=_lod_factors_for_polar_grid(
                            visual_distances,
                            visual_azimuths,
                            self._nominal_surface_resolution(),
                            viewport_width_px=request.viewport_width_px,
                            view_fov_deg=request.view_fov_deg,
                            categorical=categorical_lod,
                        ),
                        progress_callback=lambda fraction, phase: report(
                            74.0 + 23.0 * float(fraction),
                            f"visual:{phase}",
                        ),
                        abort_check=abort_check,
                    )
                    visual_valid &= visual_samples.valid
                    visual_rgba = np.where(
                        visual_valid[..., None], visual_samples.rgba, 0
                    ).astype(np.uint8)
                    visual_sources = np.where(
                        visual_valid, visual_samples.source_indices, -1
                    ).astype(np.int16)
                    visual_classes = np.where(
                        visual_valid,
                        visual_samples.class_ids
                        if visual_samples.class_ids is not None
                        else -1,
                        -1,
                    ).astype(np.int64)
                    visual_categorical = visual_valid & (
                        visual_samples.categorical
                        if visual_samples.categorical is not None
                        else False
                    )
                    visual_raster_rows = np.where(
                        visual_valid, visual_samples.raster_rows, -1
                    ).astype(np.int64)
                    visual_raster_columns = np.where(
                        visual_valid, visual_samples.raster_columns, -1
                    ).astype(np.int64)
                    visual_lod_factors = np.asarray(
                        visual_samples.lod_factors, dtype=np.int16
                    )
                    visual_sample_origins = np.where(
                        visual_valid,
                        visual_samples.sample_origins,
                        SAMPLE_ORIGIN_UNKNOWN,
                    ).astype(np.uint8)

        report(98.0, "registering-cache")
        check_cancelled()
        result = SurfaceSampleCache(
            key=key,
            source_ids=self.source_ids,
            source_names=self.source_names,
            source_legend_ids=self.source_legend_ids,
            geometry_crs=geometry_crs,
            observer_rgba=observer_rgba,
            observer_valid=observer_valid,
            observer_source_index=observer_source_index,
            observer_class_id=observer_class_id,
            observer_categorical=observer_categorical,
            observer_raster_row=observer_raster_row,
            observer_raster_column=observer_raster_column,
            observer_lod_factor=observer_lod_factor,
            observer_sample_origin=observer_sample_origin,
            observer_loaded=True,
            completion_state=request.stage,
            near_patch_rgba=near_patch_rgba,
            near_patch_valid=near_patch_valid,
            near_patch_source_indices=near_patch_sources,
            near_patch_class_ids=near_patch_classes,
            near_patch_categorical=near_patch_categorical,
            near_patch_raster_rows=near_patch_raster_rows,
            near_patch_raster_columns=near_patch_raster_columns,
            near_patch_lod_factors=near_patch_lod_factors,
            near_patch_sample_origins=near_patch_sample_origins,
            profile_rgba=profile_rgba,
            profile_valid=profile_valid,
            profile_source_indices=profile_sources,
            profile_class_ids=profile_classes,
            profile_categorical=profile_categorical,
            profile_raster_rows=profile_raster_rows,
            profile_raster_columns=profile_raster_columns,
            profile_lod_factors=profile_lod_factors,
            profile_sample_origins=profile_sample_origins,
            profile_band_indices=band_indices,
            profile_azimuth_indices=profile_azimuth_indices,
            relief_rgba=relief_rgba,
            relief_valid=relief_valid,
            relief_source_indices=relief_sources,
            relief_class_ids=relief_classes,
            relief_categorical=relief_categorical,
            relief_raster_rows=relief_raster_rows,
            relief_raster_columns=relief_raster_columns,
            relief_lod_factors=relief_lod_factors,
            relief_sample_origins=relief_sample_origins,
            relief_distance_indices=relief_distance_indices,
            relief_azimuth_indices=relief_azimuth_indices,
            visual_distances=visual_distances,
            visual_azimuths=visual_azimuths,
            visual_altitudes=visual_altitudes,
            visual_elevations=visual_elevations,
            visual_valid=visual_valid,
            visual_visible=visual_visible,
            visual_rgba=visual_rgba,
            visual_source_indices=visual_sources,
            visual_class_ids=visual_classes,
            visual_categorical=visual_categorical,
            visual_raster_rows=visual_raster_rows,
            visual_raster_columns=visual_raster_columns,
            visual_lod_factors=visual_lod_factors,
            visual_sample_origins=visual_sample_origins,
        )
        result_size = _surface_cache_size(result)
        with self._cache_lock:
            self._cache.put(result.cache_id, result, result_size)
        if self._persistent_store is not None:
            metadata, arrays = _surface_cache_payload(result)
            try:
                self._persistent_store.save(
                    f"results/{result.cache_id}", metadata, arrays
                )
            except (OSError, ValueError):
                pass
        report(100.0, "completed")
        rss_end, peak_end = process_memory_bytes()
        if self.performance_logging:
            deltas = []
            for dataset in metric_datasets:
                before = metric_baseline[id(dataset)]
                after = (
                    int(dataset.bytes_read),
                    int(dataset.lod_rows_read),
                    int(dataset.lod_intervals_read),
                    int(dataset.lod_windows_read),
                    int(dataset.lod_pixels_decoded),
                    int(dataset.lod_modal_cells),
                    int(dataset.lod_requested),
                    int(dataset.lod_unique),
                    int(dataset.lod_cache_hits),
                )
                deltas.append(tuple(end - begin for begin, end in zip(before, after)))
            append_perf_event(
                "surface.sampling",
                phase="completed",
                cache="cold",
                stage=request.stage,
                generation=int(request.generation),
                elapsed_ms=int((time.perf_counter() - start) * 1000.0),
                provider_count=len(self.providers),
                raster_bytes=sum(value[0] for value in deltas),
                lod_rows=sum(value[1] for value in deltas),
                lod_intervals=sum(value[2] for value in deltas),
                lod_windows=sum(value[3] for value in deltas),
                lod_pixels_decoded=sum(value[4] for value in deltas),
                lod_modal_cells=sum(value[5] for value in deltas),
                lod_requested=sum(value[6] for value in deltas),
                lod_unique=sum(value[7] for value in deltas),
                lod_cache_hits=sum(value[8] for value in deltas),
                result_cache=self._persistent_store.metrics() if self._persistent_store else None,
                rss_bytes=int(rss_end),
                peak_rss_bytes=int(peak_end),
                rss_delta_bytes=int(rss_end - rss_start),
                peak_delta_bytes=int(peak_end - peak_start),
            )
        print(
            "[TEMPORAL][SurfaceSampling] "
            f"providers={len(self.providers)} "
            f"profile_samples={int(profile_rgba.shape[0] * profile_rgba.shape[1])} "
            f"relief_samples={0 if relief_rgba is None else int(relief_rgba.shape[0] * relief_rgba.shape[1])} "
            f"elapsed={time.perf_counter() - start:.3f}s"
        )
        return result


def _layer_kind(source: Any) -> str:
    raw = _source_value(source, "layer_type", "type", "kind", default="")
    if hasattr(raw, "value"):
        raw = raw.value
    if not raw and hasattr(raw, "name"):
        raw = raw.name
    normalized = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"land_cover_rgb", "rgb_land_cover"}:
        return "land_cover_rgb"
    if (
        "ortho" in normalized
        or "true_color" in normalized
        or "truecolour" in normalized
        or normalized in {"rgb", "orthophoto_rgb"}
    ):
        return "orthophoto"
    if "categor" in normalized or normalized in {"land_cover", "landcover"}:
        return "categorical"
    if "light" in normalized or "pollution" in normalized or "dvnl" in normalized:
        return "light_pollution"
    return normalized


def _rgb_linear_light_for_source(source: Any) -> bool:
    metadata = _source_value(source, "metadata", default={})
    if isinstance(metadata, Mapping):
        value = metadata.get("rgb_interpolation", "")
        return str(value or "").strip().lower() in {
            "linear_light",
            "linear-light",
            "linear_srgb",
        }
    return False


def _palette_for_source(
    source: Any,
    category_palettes: Mapping[Any, Any] | None,
) -> Mapping[int, Sequence[int]]:
    embedded = _source_value(source, "class_colors", "palette", default=None)
    if not isinstance(embedded, Mapping):
        metadata = _source_value(source, "metadata", default={})
        if isinstance(metadata, Mapping):
            embedded = metadata.get("class_colors", metadata.get("palette"))
    if isinstance(embedded, Mapping):
        return embedded
    palettes = category_palettes or {}
    source_id = _source_identifier(source)
    if source_id and isinstance(palettes.get(source_id), Mapping):
        return palettes[source_id]
    if palettes and all(isinstance(key, (int, np.integer)) for key in palettes):
        return palettes
    return {}


def _legend_for_source(source: Any) -> str:
    value = _source_value(source, "legend_id", default="")
    metadata = _source_value(source, "metadata", default={})
    if not value and isinstance(metadata, Mapping):
        value = metadata.get("legend_id", metadata.get("legend", ""))
    return str(value or "").strip().lower()


# TODO(soil-wms): optional future CLC+ provider (a separate 2023 product,
# explicitly not S2GLC 2017) advertised at
# https://geoserver.geoville.com/geoserver/clcp/ows?service=WMS&version=1.3.0&request=GetCapabilities
# for layer CLMS_CLCplus_RASTER_2023_010m_eu.  Tiles must be cached below
# data_root and composed with the effective DEM.  Do not issue WMS requests
# until that provider and its bounded cache policy are implemented.
def create_surface_providers(
    sources: Any,
    *,
    category_palettes: Mapping[Any, Any] | None = None,
    internal_crs: str = CRS_TERRAIN_INTERNAL,
    initialize: bool = True,
    transform_service: CoordinateTransformService | None = None,
) -> tuple[SurfaceProvider, ...]:
    """Create typed providers from DataSource-like objects by duck typing."""

    service = transform_service or DEFAULT_TRANSFORM_SERVICE
    providers: list[SurfaceProvider] = []
    start = time.perf_counter()
    try:
        for source in _as_source_sequence(sources):
            if not _source_enabled(source):
                continue
            paths = _discover_surface_files(_source_paths(source))
            if not paths:
                continue
            kind = _layer_kind(source)
            common = {
                "source_id": _source_identifier(source),
                "source_name": str(
                    _source_value(
                        source,
                        "display_name",
                        "name",
                        default=_source_identifier(source),
                    )
                    or _source_identifier(source)
                ),
                "internal_crs": internal_crs,
                "declared_crs": _source_declared_crs(source),
                "transform_service": service,
            }
            if kind == "categorical":
                provider: SurfaceProvider = CategoricalSurfaceProvider(
                    paths,
                    class_colors=_palette_for_source(source, category_palettes),
                    legend_id=_legend_for_source(source),
                    **common,
                )
            elif kind == "land_cover_rgb":
                provider = RgbCategoricalSurfaceProvider(
                    paths,
                    class_colors=_palette_for_source(
                        source, category_palettes
                    ),
                    legend_id=_legend_for_source(source),
                    **common,
                )
            elif kind == "light_pollution":
                provider = LightPollutionProvider(paths, **common)
            else:
                provider = RgbSurfaceProvider(
                    paths,
                    linear_light=_rgb_linear_light_for_source(source),
                    **common,
                )
            providers.append(provider)
            if initialize:
                provider.initialize()
    except Exception:
        for provider in providers:
            try:
                provider.close()
            except Exception:
                pass
        raise
    print(
        "[TEMPORAL][SurfaceFactory] "
        f"providers={len(providers)} initialize={initialize} "
        f"elapsed={time.perf_counter() - start:.3f}s"
    )
    return tuple(providers)


__all__ = [
    "RgbaSampleBatch",
    "CategoricalSampleBatch",
    "SurfaceProvider",
    "RgbSurfaceProvider",
    "CategoricalSurfaceProvider",
    "RgbCategoricalSurfaceProvider",
    "LightPollutionProvider",
    "SurfaceCacheKey",
    "SurfaceSampleCache",
    "SurfaceSamplingService",
    "geometry_fingerprint",
    "raster_grids_aligned",
    "_interpolate_polar_grid",
    "create_surface_providers",
]
