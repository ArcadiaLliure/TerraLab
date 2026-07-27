"""Terrain raster provider contracts, metadata, and source helpers."""

import abc
import hashlib
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

import numpy as np

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.data_library import DataLibrary, application_state_root
from TerraLab.terrain.crs import (
    DEFAULT_TRANSFORM_SERVICE,
    transformer_transform,
)

CRS_GEOGRAPHIC = "EPSG:4326"
CRS_TERRAIN_INTERNAL = "EPSG:25831"
CATEGORICAL_LOD_REDUCER_ID = "categorical-lod-feature-priority-v3"
CATEGORICAL_LOD_TILE_SCHEMA = 2


class RasterSamplingCancelled(InterruptedError):
    """Raised between bounded GDAL reads when a sampling job is cancelled."""


def _terrain_materialized_root(kind: str) -> Path:
    try:
        root = DataLibrary.current(create=True).root
    except Exception:
        root = application_state_root()
    path = root / "cache" / "terrain_materialized" / str(kind)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_managed_data_path(path: Path) -> bool:
    roots = []
    try:
        roots.append(DataLibrary.current(create=True).root / "data")
    except Exception:
        log_suppressed_exception(__name__, "_is_managed_data_path")
    roots.append(application_state_root() / "data")
    resolved = path.resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


@dataclass(frozen=True)
class ElevationBatch:
    """Vector elevation result aligned with the broadcast input shape."""

    values: np.ndarray
    valid: np.ndarray
    source_indices: np.ndarray | None = None

    def __post_init__(self) -> None:
        values = np.asarray(self.values, dtype=np.float32)
        valid = np.asarray(self.valid, dtype=bool)
        if values.shape != valid.shape:
            raise ValueError("Elevation values and validity masks must match")
        source_indices = self.source_indices
        if source_indices is None:
            source_indices = np.full(valid.shape, -1, dtype=np.int16)
        else:
            source_indices = np.asarray(source_indices, dtype=np.int16)
            if source_indices.shape != valid.shape:
                raise ValueError("Elevation source indices must match values")
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "valid", valid)
        object.__setattr__(self, "source_indices", source_indices)


@dataclass(frozen=True)
class RasterMetadata:
    native_crs: str
    bounds: tuple[float, float, float, float] | None
    resolution_m: float | None
    nodata: tuple[float | None, ...] = ()
    driver: str = ""
    band_count: int = 1
    paths: tuple[str, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)


def _source_value(source: Any, *names: str, default: Any = None) -> Any:
    """Return the first populated attribute/key among compatible source names."""

    for name in names:
        if isinstance(source, Mapping):
            value = source.get(name, None)
        else:
            value = getattr(source, name, None)
        if value is not None:
            return value
    return default


def _source_enabled(source: Any) -> bool:
    return bool(_source_value(source, "enabled", default=True))


def _source_identifier(source: Any) -> str:
    return str(_source_value(source, "id", default="") or "")


def _source_declared_crs(source: Any) -> str | None:
    value = _source_value(source, "crs", default=None)
    return str(value) if value not in (None, "") else None


def _source_paths(source: Any) -> list[str]:
    value = _source_value(source, "path", default=source)
    if isinstance(value, (str, os.PathLike)):
        return [str(Path(value).expanduser().resolve(strict=False))]
    if isinstance(value, Sequence):
        return [str(Path(item).expanduser().resolve(strict=False)) for item in value]
    return []


def _as_source_sequence(sources: Any) -> list[Any]:
    if isinstance(sources, (str, os.PathLike, Mapping)):
        return [sources]
    if isinstance(sources, Sequence):
        return list(sources)
    return [sources]


def _path_fingerprint(
    paths: Sequence[str], *, namespace: str = "raster", configuration: Any = None
) -> str:
    digest = hashlib.blake2b(digest_size=20)
    digest.update(str(namespace).encode("utf-8"))
    digest.update(repr(configuration).encode("utf-8"))
    for raw in sorted((str(item) for item in paths), key=str.casefold):
        path = Path(raw)
        candidates = [path]
        if path.is_dir():
            candidates = sorted(
                (item for item in path.rglob("*") if item.is_file()),
                key=lambda item: str(item).casefold(),
            )
        for item in candidates:
            digest.update(str(item).encode("utf-8", errors="replace"))
            try:
                stat = item.stat()
                digest.update(str(int(stat.st_size)).encode("ascii"))
                digest.update(str(int(stat.st_mtime_ns)).encode("ascii"))
            except OSError:
                digest.update(b"missing")
    return digest.hexdigest()


def resolve_primary_dem_tiff_path(tiles_dir: str) -> Optional[str]:
    """
    Resolve the GeoTIFF DEM path that TerraLab should use from a raster source.

    Input CRS:
        - Not applicable (path resolution only).
    Internal CRS:
        - Not applicable.
    Output CRS:
        - Not applicable.

    Policy:
        - If `tiles_dir` is a `.tif/.tiff` file, return that file.
        - If `tiles_dir` is a directory, return the first GeoTIFF in
          deterministic lexical order.
        - Returns `None` when no GeoTIFF is available.
    """
    src = str(tiles_dir or "").strip()
    if not src:
        return None
    if os.path.isfile(src) and src.lower().endswith((".tif", ".tiff")):
        return src
    if not os.path.isdir(src):
        return None
    tifs = sorted(
        [
            str(path)
            for path in Path(src).rglob("*")
            if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}
        ],
        key=str.casefold,
    )
    return tifs[0] if tifs else None


class RasterProvider(abc.ABC):
    """
    Interface for providing elevation data from various raster sources (ASC tiles, large GeoTIFFs, etc).
    """

    @abc.abstractmethod
    def get_elevation(self, x: float, y: float) -> Optional[float]:
        """
        Return elevation at projected coordinates (x,y), or None if outside coverage.
        Coordinates are expected to match the provider's internal CRS (usually UTM).
        """
        pass

    def sample_elevation(self, x: Any, y: Any) -> ElevationBatch:
        """Sample broadcast x/y arrays; legacy providers get a bounded fallback."""

        x_arr, y_arr = np.broadcast_arrays(
            np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
        )
        values = np.zeros(x_arr.shape, dtype=np.float32)
        valid = np.zeros(x_arr.shape, dtype=bool)
        flat_values = values.ravel()
        flat_valid = valid.ravel()
        for index, (x_value, y_value) in enumerate(
            zip(x_arr.ravel(), y_arr.ravel())
        ):
            value = self.get_elevation(float(x_value), float(y_value))
            if value is not None and np.isfinite(value):
                flat_values[index] = float(value)
                flat_valid[index] = True
        return ElevationBatch(values, valid)

    def sample_elevations(
        self, x: Any, y: Any, *, input_crs: str | None = None
    ) -> ElevationBatch:
        """Compatibility alias used by typed geospatial providers."""

        if input_crs not in (None, "", CRS_TERRAIN_INTERNAL):
            transformer = DEFAULT_TRANSFORM_SERVICE
            x, y = transformer.transform_xy(
                x, y, input_crs, CRS_TERRAIN_INTERNAL
            )
        return self.sample_elevation(x, y)

    def prepare_region(
        self, cx: float, cy: float, radius: float, progress_callback=None
    ):
        """
        Optional: pre-load or pre-cache data for a region before heavy sampling.
        """
        pass

    def get_native_crs(self) -> str:
        """
        Return the terrain internal CRS used by HorizonBaker coordinates.

        Returns:
            str: CRS identifier. TerraLab uses `EPSG:25831` as internal terrain CRS.
        """
        return CRS_TERRAIN_INTERNAL

    def get_nominal_resolution_m(self) -> Optional[float]:
        return None

    def transform_coordinates(
        self, lat: float, lon: float
    ) -> Tuple[float, float]:
        """
        Transform a user geographic coordinate into the terrain internal CRS.

        Input CRS:
            - `lat`, `lon` in `EPSG:4326` (degrees).
        Internal CRS:
            - Terrain/Horizon coordinates in `EPSG:25831` (meters).
        Returns:
            Tuple[float, float]: `(x_internal, y_internal)` in `EPSG:25831`.
        """
        if not hasattr(self, "_tr_geo_to_internal"):
            self._tr_geo_to_internal = DEFAULT_TRANSFORM_SERVICE.transformer(
                CRS_GEOGRAPHIC, CRS_TERRAIN_INTERNAL
            )
        x_internal, y_internal = transformer_transform(
            self._tr_geo_to_internal, lon, lat
        )
        return float(x_internal), float(y_internal)

    def transform_coordinates_inverse(
        self, x: float, y: float
    ) -> Tuple[float, float]:
        """
        Transform terrain internal coordinates back to geographic lat/lon.

        Input CRS:
            - `x`, `y` in terrain internal `EPSG:25831` (meters).
        Output CRS:
            - Returns `(lat, lon)` in `EPSG:4326` (degrees).
        """
        try:
            if not hasattr(self, "_tr_internal_to_geo"):
                self._tr_internal_to_geo = DEFAULT_TRANSFORM_SERVICE.transformer(
                    CRS_TERRAIN_INTERNAL, CRS_GEOGRAPHIC
                )
            lon, lat = transformer_transform(
                self._tr_internal_to_geo, x, y
            )
            if (
                math.isnan(lat)
                or math.isnan(lon)
                or abs(lat) > 90
                or abs(lon) > 180
            ):
                return 0.0, 0.0

            return float(lat), float(lon)
        except Exception:
            return 0.0, 0.0
