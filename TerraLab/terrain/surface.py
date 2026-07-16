"""Typed surface and light-pollution providers plus pre-sampling caches.

This module has no Qt dependency.  Raster I/O and CRS transformations happen
while building :class:`SurfaceSampleCache`; the renderer receives immutable
NumPy arrays aligned with profile bands or the relief mesh.
"""

from __future__ import annotations

import abc
import hashlib
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from TerraLab.common.locks import RASTERIO_LOCK
from TerraLab.common.performance import ByteLRU, DEFAULT_PERFORMANCE_BUDGET
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
SURFACE_CACHE_POLICY_VERSION = 1
LAND_COVER_PALETTE_VERSION = 1


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


@dataclass(frozen=True)
class CategoricalSampleBatch:
    classes: np.ndarray
    valid: np.ndarray
    source_indices: np.ndarray | None = None

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


class SurfaceProvider(abc.ABC):
    """Base contract for data that determines terrain appearance."""

    surface_kind = "surface"

    def __init__(
        self,
        *,
        source_id: str = "",
        internal_crs: str = CRS_TERRAIN_INTERNAL,
        transform_service: CoordinateTransformService | None = None,
    ) -> None:
        self.source_id = str(source_id or "")
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
            configuration=(self.declared_crs, getattr(self, "band", None)),
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
    """RGB/RGBA/orthophoto provider using bilinear batch sampling."""

    surface_kind = "surface-rgb"

    def __init__(
        self,
        paths: str | os.PathLike | Sequence[str],
        *,
        source_id: str = "",
        internal_crs: str = CRS_TERRAIN_INTERNAL,
        declared_crs: str | None = None,
        transform_service: CoordinateTransformService | None = None,
    ) -> None:
        super().__init__(
            source_id=source_id,
            internal_crs=internal_crs,
            transform_service=transform_service,
        )
        raw_paths = [str(paths)] if isinstance(paths, (str, os.PathLike)) else [str(path) for path in paths]
        self.paths = _discover_surface_files(raw_paths)
        self.declared_crs = declared_crs
        self._datasets: list[_GeoRasterDataset] = []

    def initialize(self) -> bool:
        start = time.perf_counter()
        if not self.paths:
            raise FileNotFoundError("No GDAL-compatible RGB surface raster found")
        self._initialize_datasets()
        valid_datasets = [dataset for dataset in self._datasets if dataset.count >= 1]
        invalid = [dataset for dataset in self._datasets if dataset.count < 1]
        for dataset in invalid:
            dataset.close()
        self._datasets = valid_datasets
        if not self._datasets:
            raise ValueError("RGB surface rasters have no readable bands")
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

        for dataset_index, dataset in enumerate(self._datasets):
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
            requested = list(rgb_bands)
            if alpha_band is not None:
                requested.append(alpha_band)
            unique_bands = tuple(dict.fromkeys(requested))
            values, sampled_valid = dataset.sample_native(
                native_x[covered],
                native_y[covered],
                bands=unique_bands,
                interpolation="bilinear",
            )
            if not np.any(sampled_valid):
                continue
            band_rows = {band: index for index, band in enumerate(unique_bands)}
            chosen_local = np.flatnonzero(sampled_valid)
            chosen = covered_indices[chosen_local]
            for channel, band in enumerate(rgb_bands):
                dtype_name = dataset.dtypes[band - 1]
                rgba[chosen, channel] = _normalize_channel(
                    values[band_rows[band], chosen_local],
                    dtype_name,
                    scale=dataset.scales[band - 1],
                    offset=dataset.offsets[band - 1],
                )
            if alpha_band is None:
                rgba[chosen, 3] = 255
            else:
                rgba[chosen, 3] = _normalize_channel(
                    values[band_rows[alpha_band], chosen_local],
                    dataset.dtypes[alpha_band - 1],
                    scale=dataset.scales[alpha_band - 1],
                    offset=dataset.offsets[alpha_band - 1],
                    alpha=True,
                )
            alpha_valid = rgba[chosen, 3] > 0
            if np.any(alpha_valid):
                accepted = chosen[alpha_valid]
                valid[accepted] = True
                provenance[accepted] = int(min(dataset_index, np.iinfo(np.int16).max))
            rejected = chosen[~alpha_valid]
            if rejected.size:
                rgba[rejected] = 0
        return RgbaSampleBatch(
            rgba.reshape(shape + (4,)),
            valid.reshape(shape),
            provenance.reshape(shape),
        )


class CategoricalSurfaceProvider(_GdalProviderMixin, SurfaceProvider):
    """Land-cover provider; classes are sampled exclusively with nearest neighbour."""

    surface_kind = "surface-categorical"

    def __init__(
        self,
        paths: str | os.PathLike | Sequence[str],
        *,
        source_id: str = "",
        class_colors: Mapping[int, Sequence[int]] | None = None,
        band: int = 1,
        internal_crs: str = CRS_TERRAIN_INTERNAL,
        declared_crs: str | None = None,
        transform_service: CoordinateTransformService | None = None,
    ) -> None:
        super().__init__(
            source_id=source_id,
            internal_crs=internal_crs,
            transform_service=transform_service,
        )
        raw_paths = [str(paths)] if isinstance(paths, (str, os.PathLike)) else [str(path) for path in paths]
        self.paths = _discover_surface_files(raw_paths)
        self.declared_crs = declared_crs
        self.band = max(1, int(band))
        self.class_colors = {
            int(class_id): tuple(int(np.clip(value, 0, 255)) for value in color)
            for class_id, color in (class_colors or {}).items()
        }
        self._datasets: list[_GeoRasterDataset] = []

    @property
    def fingerprint(self) -> str:
        digest = hashlib.blake2b(digest_size=20)
        digest.update(
            _path_fingerprint(
                self.paths,
                namespace=self.surface_kind,
                configuration=(self.declared_crs, self.band),
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
        invalid = [dataset for dataset in self._datasets if dataset.count < self.band]
        self._datasets = [dataset for dataset in self._datasets if dataset.count >= self.band]
        for dataset in invalid:
            dataset.close()
        if not self._datasets:
            raise ValueError(f"Categorical band {self.band} is unavailable")
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
    ) -> CategoricalSampleBatch:
        x_arr, y_arr = np.broadcast_arrays(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
        shape = x_arr.shape
        flat_x = x_arr.ravel()
        flat_y = y_arr.ravel()
        crs = normalize_crs(input_crs or self.internal_crs)
        classes = np.zeros(flat_x.size, dtype=np.int64)
        valid = np.zeros(flat_x.size, dtype=bool)
        provenance = np.full(flat_x.size, -1, dtype=np.int16)
        finite = np.isfinite(flat_x) & np.isfinite(flat_y)

        for dataset_index, dataset in enumerate(self._datasets):
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
                native_x[covered],
                native_y[covered],
                bands=(self.band,),
                interpolation="nearest",
            )
            if np.any(sampled_valid):
                chosen = covered_indices[sampled_valid]
                classes[chosen] = np.rint(values[0, sampled_valid]).astype(np.int64)
                valid[chosen] = True
                provenance[chosen] = int(min(dataset_index, np.iinfo(np.int16).max))
        return CategoricalSampleBatch(
            classes.reshape(shape),
            valid.reshape(shape),
            provenance.reshape(shape),
        )

    def classes_to_rgba(self, classes: Any) -> RgbaSampleBatch:
        class_array = np.asarray(classes, dtype=np.int64)
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


class LightPollutionProvider(_GdalProviderMixin, SurfaceProvider):
    """Typed continuous radiance provider, independent from terrain surface."""

    surface_kind = "light-pollution"

    def __init__(
        self,
        paths: str | os.PathLike | Sequence[str],
        *,
        source_id: str = "",
        band: int = 1,
        internal_crs: str = CRS_TERRAIN_INTERNAL,
        declared_crs: str | None = None,
        transform_service: CoordinateTransformService | None = None,
    ) -> None:
        super().__init__(
            source_id=source_id,
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
    profile_rgba: np.ndarray | None = None
    profile_valid: np.ndarray | None = None
    profile_source_indices: np.ndarray | None = None
    profile_band_indices: np.ndarray | None = None
    profile_azimuth_indices: np.ndarray | None = None
    relief_rgba: np.ndarray | None = None
    relief_valid: np.ndarray | None = None
    relief_source_indices: np.ndarray | None = None
    relief_distance_indices: np.ndarray | None = None
    relief_azimuth_indices: np.ndarray | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "geometry_crs", normalize_crs(self.geometry_crs))
        for name, dtype in (
            ("profile_rgba", np.uint8),
            ("profile_valid", bool),
            ("profile_source_indices", np.int16),
            ("profile_band_indices", np.int32),
            ("profile_azimuth_indices", np.int32),
            ("relief_rgba", np.uint8),
            ("relief_valid", bool),
            ("relief_source_indices", np.int16),
            ("relief_distance_indices", np.int32),
            ("relief_azimuth_indices", np.int32),
        ):
            object.__setattr__(self, name, _readonly_array(getattr(self, name), dtype))

    @property
    def cache_id(self) -> str:
        return self.key.digest


def _hash_array(digest, value: Any) -> None:
    array = np.ascontiguousarray(np.asarray(value))
    digest.update(str(array.dtype).encode("ascii"))
    digest.update(str(array.shape).encode("ascii"))
    digest.update(array.tobytes())


def geometry_fingerprint(profile: Any, *, geometry_crs: str) -> str:
    explicit = getattr(profile, "geometry_id", None)
    digest = hashlib.blake2b(digest_size=20)
    digest.update(b"terrain-geometry-v2")
    if explicit:
        digest.update(str(explicit).encode("utf-8", errors="replace"))
    digest.update(normalize_crs(geometry_crs).encode("ascii"))
    digest.update(str(float(getattr(profile, "observer_lat", 0.0))).encode("ascii"))
    digest.update(str(float(getattr(profile, "observer_lon", 0.0))).encode("ascii"))
    digest.update(str(getattr(profile, "representation_mode", "legacy")).encode("utf-8"))
    _hash_array(digest, getattr(profile, "azimuths", np.asarray([], dtype=np.float32)))
    for band in getattr(profile, "bands", ()) or ():
        digest.update(str(band.get("id", "")).encode("utf-8", errors="replace"))
        _hash_array(digest, band.get("dists", np.asarray([], dtype=np.float32)))
        _hash_array(digest, band.get("angles", np.asarray([], dtype=np.float32)))
    mesh = getattr(profile, "terrain_mesh", None)
    if isinstance(mesh, Mapping):
        for name in ("azimuths", "distances", "altitudes", "valid"):
            if name in mesh:
                _hash_array(digest, mesh[name])
    return digest.hexdigest()


def _sample_indices(size: int, other_size: int, max_samples: int) -> np.ndarray:
    if size <= 0:
        return np.asarray([], dtype=np.int32)
    permitted = max(1, int(max_samples) // max(1, int(other_size)))
    count = min(int(size), permitted)
    if count >= int(size):
        return np.arange(size, dtype=np.int32)
    return np.unique(np.rint(np.linspace(0, size - 1, count)).astype(np.int32))


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

    @property
    def source_fingerprints(self) -> tuple[str, ...]:
        return tuple(provider.fingerprint for provider in self.providers)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(provider.source_id or provider.fingerprint for provider in self.providers)

    def close(self) -> None:
        with self._cache_lock:
            self._cache.clear()
        for provider in self.providers:
            provider.close()

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def sample_rgba_points(
        self,
        x: Any,
        y: Any,
        *,
        input_crs: str,
    ) -> RgbaSampleBatch:
        x_arr, y_arr = np.broadcast_arrays(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
        rgba = np.zeros(x_arr.shape + (4,), dtype=np.uint8)
        valid = np.zeros(x_arr.shape, dtype=bool)
        provenance = np.full(x_arr.shape, -1, dtype=np.int16)

        # LayerSelectionService already orders automatic chains as RGB then
        # categorical. Preserve the supplied order here so an explicit manual
        # categorical selection remains the effective source it claims to be.
        ordered = list(self.providers)
        provider_to_index = {id(provider): index for index, provider in enumerate(self.providers)}
        for provider in ordered:
            if np.all(valid):
                break
            if isinstance(provider, RgbSurfaceProvider):
                batch = provider.sample_rgba(x_arr, y_arr, input_crs=input_crs)
                candidate_rgba = batch.rgba
                candidate_valid = batch.valid
            else:
                classes = provider.sample_classes(x_arr, y_arr, input_crs=input_crs)
                mapped = provider.classes_to_rgba(classes.classes)
                candidate_rgba = mapped.rgba
                candidate_valid = classes.valid & mapped.valid
            take = ~valid & candidate_valid
            rgba[take] = candidate_rgba[take]
            valid[take] = True
            provenance[take] = int(provider_to_index[id(provider)])
        return RgbaSampleBatch(rgba, valid, provenance)

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
    ) -> SurfaceSampleCache:
        start = time.perf_counter()
        geometry_crs = normalize_crs(
            getattr(profile, "geometry_crs", None) or self.internal_crs
        )
        profile_geometry_key = geometry_fingerprint(profile, geometry_crs=geometry_crs)
        if geometry_id:
            digest = hashlib.blake2b(digest_size=20)
            digest.update(str(geometry_id).encode("utf-8", errors="replace"))
            digest.update(profile_geometry_key.encode("ascii"))
            geometry_key = digest.hexdigest()
        else:
            geometry_key = profile_geometry_key
        policy = (
            f"surface-v{SURFACE_CACHE_POLICY_VERSION}:profile={self.max_profile_samples}:"
            f"relief={self.max_relief_samples}:rgb-bilinear:categorical-nearest"
        )
        key = SurfaceCacheKey(
            geometry_id=str(geometry_key),
            source_fingerprints=self.source_fingerprints,
            sampling_policy=policy,
        )
        with self._cache_lock:
            cached = self._cache.get(key.digest)
            if cached is not None:
                return cached

        observer_x, observer_y = self._observer_xy(profile, geometry_crs)
        azimuths = np.asarray(getattr(profile, "azimuths", ()), dtype=np.float64)
        bands = list(getattr(profile, "bands", ()) or ())

        band_indices = _sample_indices(
            len(bands), max(1, len(azimuths)), self.max_profile_samples
        )
        profile_azimuth_indices = _sample_indices(
            len(azimuths), max(1, len(band_indices)), self.max_profile_samples
        )
        profile_rgba = np.zeros(
            (len(band_indices), len(profile_azimuth_indices), 4), dtype=np.uint8
        )
        profile_valid = np.zeros(profile_rgba.shape[:-1], dtype=bool)
        profile_sources = np.full(profile_valid.shape, -1, dtype=np.int16)
        if band_indices.size and profile_azimuth_indices.size:
            sampled_bands = [bands[index] for index in band_indices]
            sampled_azimuths = azimuths[profile_azimuth_indices]
            azimuth_radians = np.deg2rad(sampled_azimuths)[None, :]
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
            x = observer_x + distances * np.sin(azimuth_radians)
            y = observer_y + distances * np.cos(azimuth_radians)
            samples = self.sample_rgba_points(x, y, input_crs=geometry_crs)
            profile_valid = samples.valid & position_valid
            profile_rgba = np.where(profile_valid[..., None], samples.rgba, 0).astype(np.uint8)
            profile_sources = np.where(
                profile_valid, samples.source_indices, -1
            ).astype(np.int16)

        relief_rgba = None
        relief_valid = None
        relief_sources = None
        relief_distance_indices = None
        relief_azimuth_indices = None
        mesh = getattr(profile, "terrain_mesh", None)
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
                selected_distances = mesh_distances[relief_distance_indices]
                selected_azimuths = mesh_azimuths[relief_azimuth_indices]
                azimuth_radians = np.deg2rad(selected_azimuths)[None, :]
                x = observer_x + selected_distances[:, None] * np.sin(azimuth_radians)
                y = observer_y + selected_distances[:, None] * np.cos(azimuth_radians)
                samples = self.sample_rgba_points(x, y, input_crs=geometry_crs)
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

        result = SurfaceSampleCache(
            key=key,
            source_ids=self.source_ids,
            geometry_crs=geometry_crs,
            profile_rgba=profile_rgba,
            profile_valid=profile_valid,
            profile_source_indices=profile_sources,
            profile_band_indices=band_indices,
            profile_azimuth_indices=profile_azimuth_indices,
            relief_rgba=relief_rgba,
            relief_valid=relief_valid,
            relief_source_indices=relief_sources,
            relief_distance_indices=relief_distance_indices,
            relief_azimuth_indices=relief_azimuth_indices,
        )
        result_size = sum(
            int(value.nbytes)
            for value in (
                result.profile_rgba,
                result.profile_valid,
                result.profile_source_indices,
                result.profile_band_indices,
                result.profile_azimuth_indices,
                result.relief_rgba,
                result.relief_valid,
                result.relief_source_indices,
                result.relief_distance_indices,
                result.relief_azimuth_indices,
            )
            if value is not None
        )
        with self._cache_lock:
            self._cache.put(result.cache_id, result, result_size)
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
    if "categor" in normalized or "land_cover" in normalized or "landcover" in normalized:
        return "categorical"
    if "light" in normalized or "pollution" in normalized or "dvnl" in normalized:
        return "light_pollution"
    if "rgb" in normalized or "ortho" in normalized or "true_color" in normalized or "truecolour" in normalized:
        return "rgb"
    return normalized


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
                "internal_crs": internal_crs,
                "declared_crs": _source_declared_crs(source),
                "transform_service": service,
            }
            if kind == "categorical":
                provider: SurfaceProvider = CategoricalSurfaceProvider(
                    paths,
                    class_colors=_palette_for_source(source, category_palettes),
                    **common,
                )
            elif kind == "light_pollution":
                provider = LightPollutionProvider(paths, **common)
            else:
                # Multi-band surface datasets default to RGB.  A one-band
                # land-cover dataset should use the categorical type.
                provider = RgbSurfaceProvider(paths, **common)
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
    "LightPollutionProvider",
    "SurfaceCacheKey",
    "SurfaceSampleCache",
    "SurfaceSamplingService",
    "geometry_fingerprint",
    "create_surface_providers",
]
