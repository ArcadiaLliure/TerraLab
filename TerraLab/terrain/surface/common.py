"""Typed surface and light-pollution providers plus pre-sampling caches.

This module has no Qt dependency.  Raster I/O and CRS transformations happen
while building :class:`SurfaceSampleCache`; the renderer receives immutable
NumPy arrays aligned with profile bands or the relief mesh.
"""

from __future__ import annotations

import abc
import hashlib
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from TerraLab.terrain.crs import (
    CRS_TERRAIN_INTERNAL,
    DEFAULT_TRANSFORM_SERVICE,
    CoordinateTransformService,
    normalize_crs,
)
from TerraLab.terrain.providers import RasterMetadata
from TerraLab.terrain.providers.common import _path_fingerprint
from TerraLab.terrain.providers.raster_dataset import _GeoRasterDataset


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
