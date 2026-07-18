import abc
import hashlib
import json
import math
import os
import threading
import sys
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

import numpy as np
from pyproj import Transformer

from TerraLab.common.locks import RASTERIO_LOCK
from TerraLab.common.data_library import DataLibrary, application_state_root
from TerraLab.common.performance import DEFAULT_PERFORMANCE_BUDGET, PERFORMANCE_FLAGS
from TerraLab.terrain.crs import (
    DEFAULT_TRANSFORM_SERVICE,
    CoordinateTransformService,
    normalize_crs,
)

CRS_GEOGRAPHIC = "EPSG:4326"
CRS_TERRAIN_INTERNAL = "EPSG:25831"
PYPROJ_TRANSFORMER_LOCK = threading.Lock()


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
        pass
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


def create_raster_provider(tiles_dir: str, progress_callback=None):
    """
    Build and initialize the DEM provider from a configured raster source path.

    Input CRS:
        - User coordinates are still expected in `EPSG:4326` by provider APIs.
    Internal CRS:
        - Provider sampling coordinates are in `EPSG:25831` terrain CRS.
    Output CRS:
        - Not applicable; returns an initialized provider instance.

    Selection policy:
        - Uses `TiffRasterWindowProvider` when a GeoTIFF source is present.
        - Falls back to `AscRasterProvider` otherwise.
    """
    tiff_path = resolve_primary_dem_tiff_path(tiles_dir)
    if tiff_path:
        if PERFORMANCE_FLAGS.raster_batch:
            provider = TiffRasterWindowProvider(tiles_dir)
        else:
            provider = LegacyTiffRasterWindowProvider(tiff_path)
    else:
        provider = AscRasterProvider(tiles_dir)
    provider.initialize(progress_callback=progress_callback)
    return provider


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
            with PYPROJ_TRANSFORMER_LOCK:
                self._tr_geo_to_internal = Transformer.from_crs(
                    CRS_GEOGRAPHIC, CRS_TERRAIN_INTERNAL, always_xy=True
                )
        x_internal, y_internal = self._tr_geo_to_internal.transform(lon, lat)
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
                with PYPROJ_TRANSFORMER_LOCK:
                    self._tr_internal_to_geo = Transformer.from_crs(
                        CRS_TERRAIN_INTERNAL, CRS_GEOGRAPHIC, always_xy=True
                    )
            lon, lat = self._tr_internal_to_geo.transform(x, y)
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


class _GeoRasterDataset:
    """One GDAL dataset sampled through native blocks with a byte-bounded LRU."""

    def __init__(
        self,
        path: str,
        *,
        declared_crs: str | None = None,
        transform_service: CoordinateTransformService | None = None,
        block_cache_capacity: int = 64,
        block_cache_bytes: int | None = None,
    ) -> None:
        self.path = str(Path(path).expanduser().resolve(strict=False))
        cache_key = hashlib.blake2b(
            self.path.encode("utf-8", errors="replace"), digest_size=12
        ).hexdigest()
        self.materialized_dir = _terrain_materialized_root("gdal") / cache_key
        self.materialized_path = self.materialized_dir / "materialized.npy"
        self.materialized_manifest_path = self.materialized_dir / "manifest.json"
        self.declared_crs = declared_crs
        self.transform_service = transform_service or DEFAULT_TRANSFORM_SERVICE
        self.block_cache_capacity = max(1, int(block_cache_capacity))
        self.block_cache_bytes = int(
            block_cache_bytes
            if block_cache_bytes is not None
            else max(32 * 1024**2, DEFAULT_PERFORMANCE_BUDGET.dem_bytes // 8)
        )
        self.dataset = None
        self._cache: OrderedDict[tuple, tuple[np.ndarray, np.ndarray, int, int]] = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = threading.RLock()
        self._read_lock = threading.RLock()
        self._thread_local = threading.local()
        self._thread_handles: list[Any] = []
        self._thread_handles_lock = threading.Lock()
        self._io_workers = (
            1 if os.name == "nt" else min(4, max(1, int(os.cpu_count() or 4) // 4))
        )
        self._prefetch_executor: ThreadPoolExecutor | None = None
        self._prefetch_lock = threading.Lock()
        self.cache_hits = 0
        self.cache_misses = 0
        self.bytes_read = 0

    def open(self) -> bool:
        import rasterio

        if self.dataset is not None:
            return True
        with RASTERIO_LOCK:
            self.dataset = rasterio.open(self.path)
        dataset = self.dataset
        raw_crs = self.declared_crs or (
            dataset.crs.to_string() if dataset.crs is not None else CRS_GEOGRAPHIC
        )
        self.native_crs = normalize_crs(raw_crs)
        self.transform = dataset.transform
        self.inverse_transform = ~dataset.transform
        self.width = int(dataset.width)
        self.height = int(dataset.height)
        self.count = int(dataset.count)
        self.bounds = tuple(float(value) for value in dataset.bounds)
        self.driver = str(dataset.driver or "")
        self.nodatavals = tuple(dataset.nodatavals or (None,) * self.count)
        self.dtypes = tuple(dataset.dtypes)
        self.scales = tuple(float(value) for value in dataset.scales)
        self.offsets = tuple(float(value) for value in dataset.offsets)
        self.colorinterp = tuple(
            str(getattr(value, "name", value)).lower() for value in dataset.colorinterp
        )
        self.resolution_m = self.transform_service.resolution_metres(
            self.native_crs, self.transform, self.width, self.height
        )
        block_shapes = tuple(dataset.block_shapes or ())
        self.block_height, self.block_width = (
            tuple(int(value) for value in block_shapes[0])
            if block_shapes
            else (min(256, self.height), min(256, self.width))
        )
        self.metadata = RasterMetadata(
            native_crs=self.native_crs,
            bounds=self.bounds,
            resolution_m=self.resolution_m,
            nodata=self.nodatavals,
            driver=self.driver,
            band_count=self.count,
            paths=(self.path, str(self.materialized_path)),
            extra={
                "width": self.width,
                "height": self.height,
                "block_width": self.block_width,
                "block_height": self.block_height,
            },
        )
        self.materialized_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": 1,
            "source": self.path,
            "cache": str(self.materialized_path),
            "bounded_blocks": True,
            "native_crs": self.native_crs,
        }
        temporary = self.materialized_manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.materialized_manifest_path)
        return True

    def _reader(self):
        """Use one Rasterio handle per worker except in conservative Windows mode."""

        if os.name == "nt":
            return self.dataset
        reader = getattr(self._thread_local, "dataset", None)
        if reader is None:
            import rasterio

            reader = rasterio.open(self.path)
            self._thread_local.dataset = reader
            with self._thread_handles_lock:
                self._thread_handles.append(reader)
        return reader

    def _cache_get(self, key: tuple):
        with self._cache_lock:
            value = self._cache.get(key)
            if value is None:
                self.cache_misses += 1
                return None
            self.cache_hits += 1
            self._cache.move_to_end(key)
            return value

    def _cache_put(self, key: tuple, value) -> None:
        size = int(value[0].nbytes + value[1].nbytes)
        with self._cache_lock:
            previous = self._cache.pop(key, None)
            if previous is not None:
                self._cache_bytes -= int(previous[0].nbytes + previous[1].nbytes)
            if size > self.block_cache_bytes:
                return
            self._cache[key] = value
            self._cache_bytes += size
            while self._cache and self._cache_bytes > self.block_cache_bytes:
                _old_key, old = self._cache.popitem(last=False)
                self._cache_bytes -= int(old[0].nbytes + old[1].nbytes)

    def _read_block(self, block_row: int, block_col: int, bands: tuple[int, ...]):
        key = (bands, int(block_row), int(block_col))
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        from rasterio.windows import Window

        row_off = int(block_row) * self.block_height
        col_off = int(block_col) * self.block_width
        height = min(self.block_height + 1, self.height - row_off)
        width = min(self.block_width + 1, self.width - col_off)
        window = Window(col_off, row_off, width, height)
        reader = self._reader()
        if reader is None:
            raise RuntimeError("Raster dataset is closed")
        # Rasterio/GDAL handles are not shared across worker threads.  Windows
        # deliberately retains a single locked reader for driver stability.
        lock = self._read_lock if os.name == "nt" else threading.Lock()
        with lock:
            data = np.asarray(reader.read(bands, window=window))
            masks = np.asarray(reader.read_masks(bands, window=window)) > 0
        valid = np.all(masks, axis=0)
        for local_band, band in enumerate(bands):
            nodata = self.nodatavals[band - 1]
            if nodata is None:
                valid &= np.isfinite(data[local_band])
            elif isinstance(nodata, float) and math.isnan(nodata):
                valid &= np.isfinite(data[local_band])
            else:
                valid &= data[local_band] != nodata
        value = (data, valid, row_off, col_off)
        try:
            self.materialized_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.materialized_path.with_suffix(".tmp.npy")
            np.save(temporary, data, allow_pickle=False)
            os.replace(temporary, self.materialized_path)
        except OSError:
            pass
        self.bytes_read += int(data.nbytes + masks.nbytes)
        self._cache_put(key, value)
        return value

    def sample_native(
        self,
        native_x: Any,
        native_y: Any,
        *,
        bands: tuple[int, ...] = (1,),
        interpolation: str = "bilinear",
    ) -> tuple[np.ndarray, np.ndarray]:
        if self.dataset is None:
            raise RuntimeError("Raster dataset is not initialized")
        bands = tuple(int(value) for value in bands)
        if not bands or min(bands) < 1 or max(bands) > self.count:
            raise ValueError("Requested raster band is unavailable")
        x_arr, y_arr = np.broadcast_arrays(
            np.asarray(native_x, dtype=np.float64),
            np.asarray(native_y, dtype=np.float64),
        )
        shape = x_arr.shape
        flat_x = x_arr.ravel()
        flat_y = y_arr.ravel()
        inv = self.inverse_transform
        col = inv.a * flat_x + inv.b * flat_y + inv.c - 0.5
        row = inv.d * flat_x + inv.e * flat_y + inv.f - 0.5
        output = np.zeros((len(bands), flat_x.size), dtype=np.float32)
        valid = np.isfinite(col) & np.isfinite(row)
        safe_col = np.where(valid, col, 0.0)
        safe_row = np.where(valid, row, 0.0)

        nearest = str(interpolation).lower() == "nearest"
        if nearest:
            col0 = np.floor(safe_col + 0.5).astype(np.int64, copy=False)
            row0 = np.floor(safe_row + 0.5).astype(np.int64, copy=False)
            valid &= (
                (col0 >= 0) & (row0 >= 0) & (col0 < self.width) & (row0 < self.height)
            )
        else:
            col0 = np.floor(safe_col).astype(np.int64, copy=False)
            row0 = np.floor(safe_row).astype(np.int64, copy=False)
            valid &= (
                (col0 >= 0)
                & (row0 >= 0)
                & (col0 < self.width - 1)
                & (row0 < self.height - 1)
            )

        candidate_indices = np.flatnonzero(valid)
        if candidate_indices.size == 0:
            return output.reshape((len(bands),) + shape), valid.reshape(shape)
        block_row = row0[candidate_indices] // self.block_height
        block_col = col0[candidate_indices] // self.block_width
        block_key = block_row * max(1, math.ceil(self.width / self.block_width)) + block_col
        order = np.argsort(block_key, kind="stable")
        sorted_indices = candidate_indices[order]
        sorted_keys = block_key[order]
        starts = np.r_[0, np.flatnonzero(sorted_keys[1:] != sorted_keys[:-1]) + 1]
        stops = np.r_[starts[1:], len(sorted_indices)]
        groups = []
        for start, stop in zip(starts, stops):
            indices = sorted_indices[start:stop]
            br = int(row0[indices[0]] // self.block_height)
            bc = int(col0[indices[0]] // self.block_width)
            groups.append((indices, br, bc))

        def _apply_group(indices, block) -> None:
            data, data_valid, row_off, col_off = block
            local_row = row0[indices] - row_off
            local_col = col0[indices] - col_off
            if nearest:
                point_valid = data_valid[local_row, local_col]
                chosen = indices[point_valid]
                if chosen.size:
                    output[:, chosen] = data[:, local_row[point_valid], local_col[point_valid]]
                valid[indices] &= point_valid
                return
            r1 = local_row + 1
            c1 = local_col + 1
            point_valid = (
                data_valid[local_row, local_col]
                & data_valid[local_row, c1]
                & data_valid[r1, local_col]
                & data_valid[r1, c1]
            )
            chosen = indices[point_valid]
            if chosen.size:
                lr = local_row[point_valid]
                lc = local_col[point_valid]
                rr = lr + 1
                cc = lc + 1
                dc = (col[chosen] - col0[chosen]).astype(np.float32)
                dr = (row[chosen] - row0[chosen]).astype(np.float32)
                v00 = data[:, lr, lc].astype(np.float32, copy=False)
                v01 = data[:, lr, cc].astype(np.float32, copy=False)
                v10 = data[:, rr, lc].astype(np.float32, copy=False)
                v11 = data[:, rr, cc].astype(np.float32, copy=False)
                top = v00 * (1.0 - dc) + v01 * dc
                bottom = v10 * (1.0 - dc) + v11 * dc
                output[:, chosen] = top * (1.0 - dr) + bottom * dr
            valid[indices] &= point_valid

        window_size = max(1, self._io_workers * 2)
        executor = None
        if self._io_workers > 1 and len(groups) > 1:
            with self._prefetch_lock:
                if self._prefetch_executor is None:
                    self._prefetch_executor = ThreadPoolExecutor(
                        max_workers=self._io_workers,
                        thread_name_prefix="raster-prefetch",
                    )
                executor = self._prefetch_executor
        for window_start in range(0, len(groups), window_size):
            window = groups[window_start : window_start + window_size]
            if executor is None:
                blocks = [self._read_block(br, bc, bands) for _indices, br, bc in window]
            else:
                futures = [
                    executor.submit(self._read_block, br, bc, bands)
                    for _indices, br, bc in window
                ]
                blocks = [future.result() for future in futures]
            for (indices, _br, _bc), block in zip(window, blocks):
                _apply_group(indices, block)
        return output.reshape((len(bands),) + shape), valid.reshape(shape)

    def close(self) -> None:
        with self._prefetch_lock:
            executor = self._prefetch_executor
            self._prefetch_executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        with self._thread_handles_lock:
            handles = tuple(self._thread_handles)
            self._thread_handles.clear()
        for handle in handles:
            try:
                handle.close()
            except Exception:
                pass
        if self.dataset is not None:
            try:
                with RASTERIO_LOCK:
                    self.dataset.close()
            except Exception:
                pass
        self.dataset = None
        with self._cache_lock:
            self._cache.clear()
            self._cache_bytes = 0


class AscRasterProvider(RasterProvider):
    """
    Legacy implementation: loads ESRI ASCII / NPY tiled directories dynamically
    using the internal TileIndex, TileCache, and DemSampler.
    """

    def __init__(self, tiles_dir: str):
        self.tiles_dir = str(tiles_dir)
        self.index = None
        self.cache = None
        self.sampler = None
        self.source_id = ""
        self.internal_crs = CRS_TERRAIN_INTERNAL
        self.native_crs = CRS_TERRAIN_INTERNAL
        self.metadata: tuple[RasterMetadata, ...] = ()
        self.sample_selection_ns = 0
        self.sample_interpolation_ns = 0
        self.sample_candidate_tiles = 0
        self.sample_loaded_tiles = 0
        self.sampled_points = 0

    def initialize(self, progress_callback=None):
        # We must import inside or assure no circular dependency
        """Executa el metode initialize de la classe AscRasterProvider.

        Par?metres:
        - progress_callback (Any): Valor del parametre 'progress_callback'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        from TerraLab.terrain.engine import DemSampler, TileCache, TileIndex

        self.index = TileIndex(
            self.tiles_dir,
            callback=lambda curr, tot, msg: (
                progress_callback(curr / tot * 100, msg)
                if progress_callback
                else None
            ),
        )
        self.cache = TileCache(capacity=500)
        self.sampler = DemSampler(self.index, self.cache)
        metadata = []
        for tile in getattr(self.index, "tiles", ()):
            header = tile.get("header", {})
            nodata = header.get("NODATA_VALUE")
            original_path = Path(str(tile.get("path", ""))).resolve(strict=False)
            metadata_paths = [str(original_path)]
            if original_path.suffix.lower() in {".asc", ".txt"}:
                allow_adjacent = _is_managed_data_path(original_path)
                tile["allow_adjacent_cache"] = allow_adjacent
                if allow_adjacent:
                    cache_path = original_path.with_suffix(".npy")
                else:
                    identity = hashlib.blake2b(
                        str(original_path).encode("utf-8", errors="replace"),
                        digest_size=12,
                    ).hexdigest()
                    cache_path = _terrain_materialized_root("ascii") / f"{identity}.npy"
                tile["materialized_path"] = str(cache_path)
                tile["materialized_metadata_path"] = str(
                    cache_path.with_suffix(".npy.json")
                )
                metadata_paths.append(str(cache_path))
            metadata.append(
                RasterMetadata(
                    native_crs=self.native_crs,
                    bounds=tuple(float(value) for value in tile.get("bbox", ())) or None,
                    resolution_m=float(header.get("CELLSIZE", 5.0)),
                    nodata=(float(nodata) if nodata is not None else None,),
                    driver="NPY" if header.get("NPY") else "AAIGrid",
                    band_count=1,
                    paths=tuple(metadata_paths),
                )
            )
        self.metadata = tuple(metadata)
        return True

    def get_nominal_resolution_m(self) -> Optional[float]:
        if not self.index or not getattr(self.index, "tiles", None):
            return None

        cell_sizes = []
        for tile in self.index.tiles:
            header = tile.get("header", {})
            cell_size = header.get("CELLSIZE")
            try:
                cell_size = float(cell_size)
            except (TypeError, ValueError):
                continue
            if cell_size > 0:
                cell_sizes.append(cell_size)

        return min(cell_sizes) if cell_sizes else None

    def prepare_region(
        self,
        cx: float,
        cy: float,
        radius: float,
        progress_callback=None,
        abort_check=None,
    ):
        """Executa el metode prepare_region de la classe AscRasterProvider.

        Par?metres:
        - cx (float): Valor del parametre 'cx'.
        - cy (float): Valor del parametre 'cy'.
        - radius (float): Valor del parametre 'radius'.
        - progress_callback (Any): Valor del parametre 'progress_callback'.
        - abort_check (Any): Valor del parametre 'abort_check'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from TerraLab.common.utils import getTraduction, get_config_value

        if not self.index:
            return

        tiles_needed = self.index.get_overlapping_tiles(cx, cy, radius)
        total_tiles = len(tiles_needed)
        safe_tile_loading = bool(
            get_config_value("performance.safe_dem_tile_loading", True)
        )
        if safe_tile_loading and os.name == "nt" and sys.version_info >= (3, 13):
            n_tile_workers = 1
        else:
            n_tile_workers = min(8, total_tiles or 1)
        loaded_count = 0
        last_reported_percent = -1

        def _load_tile(tile):
            return self.cache.load(tile)

        with ThreadPoolExecutor(max_workers=n_tile_workers) as executor:
            futures = {
                executor.submit(_load_tile, tile): tile
                for tile in tiles_needed
            }
            for future in as_completed(futures):
                if abort_check and abort_check():
                    # Attempt to cancel pending ones and shutdown immediately
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise InterruptedError("Loading aborted")

                loaded_count += 1
                if progress_callback and total_tiles > 0:
                    percent = int(loaded_count / total_tiles * 100)
                    if percent > last_reported_percent:
                        last_reported_percent = percent
                        msg = getTraduction(
                            "Horizon.LoadingMaps",
                            "Loading maps: {loaded}/{total} ({pct}%)",
                        ).format(
                            loaded=loaded_count, total=total_tiles, pct=percent
                        )
                        progress_callback(percent, msg)
                try:
                    future.result()
                except Exception as e:
                    print(f"[AscRasterProvider] Tile load error: {e}")

    def get_elevation(self, x: float, y: float) -> Optional[float]:
        """Obte elevation de la instancia de AscRasterProvider.

        Par?metres:
        - x (float): Valor del parametre 'x'.
        - y (float): Valor del parametre 'y'.

        Retorna:
        - Optional[float]: Valor retornat pel metode.
        """
        if not self.sampler:
            return None
        native_x, native_y = x, y
        if self.native_crs != self.internal_crs:
            native_x, native_y = DEFAULT_TRANSFORM_SERVICE.transform_xy(
                x, y, self.internal_crs, self.native_crs
            )
        return self.sampler.sample(float(native_x), float(native_y))

    def _sample_native_elevation(self, x: Any, y: Any) -> ElevationBatch:
        if not self.index or not self.cache:
            x_arr, _ = np.broadcast_arrays(np.asarray(x), np.asarray(y))
            return ElevationBatch(
                np.zeros(x_arr.shape, dtype=np.float32),
                np.zeros(x_arr.shape, dtype=bool),
            )
        x_arr, y_arr = np.broadcast_arrays(
            np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
        )
        shape = x_arr.shape
        flat_x = x_arr.ravel()
        flat_y = y_arr.ravel()
        values = np.zeros(flat_x.size, dtype=np.float32)
        valid = np.zeros(flat_x.size, dtype=bool)

        # Tiles are evaluated in deterministic index order.  Each tile load is
        # performed once and all candidate points are interpolated in NumPy.
        selection_start = time.perf_counter_ns()
        candidate_groups = self.index.candidate_point_groups(flat_x, flat_y)
        self.sample_selection_ns += time.perf_counter_ns() - selection_start
        self.sample_candidate_tiles += len(candidate_groups)
        self.sampled_points += int(flat_x.size)
        interpolation_start = time.perf_counter_ns()
        for tile, candidate_indices in candidate_groups:
            indices = candidate_indices[~valid[candidate_indices]]
            if indices.size == 0:
                continue
            xmin, ymin, xmax, ymax = tile["bbox"]
            data, header = self.cache.load(tile)
            if data is None or header is None:
                continue
            self.sample_loaded_tiles += 1
            nrows, ncols = data.shape
            if header.get("NPY"):
                sx = (xmax - xmin) / max(1, ncols)
                sy = (ymax - ymin) / max(1, nrows)
                grid_x = (flat_x[indices] - xmin) / sx
                grid_row = (ymax - flat_y[indices]) / sy
            else:
                cell_size = float(header.get("CELLSIZE", 5.0))
                if "XLLCENTER" in header:
                    x0 = float(header["XLLCENTER"])
                    y0 = float(header["YLLCENTER"])
                else:
                    x0 = float(header.get("XLLCORNER", 0.0)) + cell_size / 2.0
                    y0 = float(header.get("YLLCORNER", 0.0)) + cell_size / 2.0
                grid_x = (flat_x[indices] - x0) / cell_size
                row_bottom = (flat_y[indices] - y0) / cell_size
                grid_row = (int(header.get("NROWS", nrows)) - 1) - row_bottom
            c0 = np.clip(np.floor(grid_x).astype(np.int64), 0, ncols - 1)
            r0 = np.clip(np.floor(grid_row).astype(np.int64), 0, nrows - 1)
            c1 = np.minimum(c0 + 1, ncols - 1)
            r1 = np.minimum(r0 + 1, nrows - 1)
            dc = (grid_x - c0).astype(np.float32)
            dr = (grid_row - r0).astype(np.float32)
            v00 = data[r0, c0]
            v01 = data[r0, c1]
            v10 = data[r1, c0]
            v11 = data[r1, c1]
            nodata = header.get("NODATA_VALUE")
            point_valid = (
                np.isfinite(v00)
                & np.isfinite(v01)
                & np.isfinite(v10)
                & np.isfinite(v11)
            )
            if nodata is not None:
                point_valid &= (
                    (v00 != nodata)
                    & (v01 != nodata)
                    & (v10 != nodata)
                    & (v11 != nodata)
                )
            sampled = (
                (v00 * (1.0 - dc) + v01 * dc) * (1.0 - dr)
                + (v10 * (1.0 - dc) + v11 * dc) * dr
            )
            chosen = indices[point_valid]
            values[chosen] = sampled[point_valid]
            valid[chosen] = True
        self.sample_interpolation_ns += time.perf_counter_ns() - interpolation_start
        sources = np.where(valid, 0, -1).astype(np.int16)
        return ElevationBatch(
            values.reshape(shape), valid.reshape(shape), sources.reshape(shape)
        )

    def sample_elevation(self, x: Any, y: Any) -> ElevationBatch:
        if self.native_crs != self.internal_crs:
            x, y = DEFAULT_TRANSFORM_SERVICE.transform_xy(
                x, y, self.internal_crs, self.native_crs
            )
        return self._sample_native_elevation(x, y)

    def sample_elevations(
        self, x: Any, y: Any, *, input_crs: str | None = None
    ) -> ElevationBatch:
        source_crs = normalize_crs(input_crs or self.internal_crs)
        if source_crs != self.native_crs:
            x, y = DEFAULT_TRANSFORM_SERVICE.transform_xy(
                x, y, source_crs, self.native_crs
            )
        return self._sample_native_elevation(x, y)

    def close(self) -> None:
        if self.cache is not None:
            try:
                self.cache.clear()
            except Exception:
                pass
        self.sampler = None


class LegacyTiffRasterWindowProvider(RasterProvider):
    """
    Implementation for large contiguous GeoTIFFs (like the 18GB European DEM).
    Keeps the dataset open and uses windowed reading to load a Region of Interest
    (ROI) into RAM before heavy sampling.
    """

    def __init__(self, tiff_path: str):
        self.tiff_path = tiff_path
        self.dataset = None
        self.cached_window = None
        self.cached_transform = None
        self.cached_inverse_transform = None
        self.cached_data = None
        self.transformer = None

        # Local flat-earth mapping parameters for fast UTM->Native interpolation
        self.native_cx = 0.0
        self.native_cy = 0.0
        self.center_x_utm = 0.0
        self.center_y_utm = 0.0
        self.m_per_native_x = 1.0
        self.m_per_native_y = 1.0
        self._tr_internal_to_native = None

    def initialize(self, progress_callback=None):
        """Executa el metode initialize de la classe TiffRasterWindowProvider.

        Par?metres:
        - progress_callback (Any): Valor del parametre 'progress_callback'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        import rasterio

        if not os.path.exists(self.tiff_path):
            raise FileNotFoundError(f"GeoTIFF not found: {self.tiff_path}")

        print(f"[TiffRasterWindowProvider] Opening dataset: {self.tiff_path}")
        with RASTERIO_LOCK:
            self.dataset = rasterio.open(self.tiff_path)
        print(
            f"[TiffRasterWindowProvider] CRS: {self.dataset.crs}, Bounds: {self.dataset.bounds}"
        )

        # Setup transformer from terrain internal CRS to dataset native CRS.
        if self.dataset.crs:
            dest_crs = self.dataset.crs.to_string()
            self.is_geo = self.dataset.crs.is_geographic
        else:
            # Default to WGS84 if no CRS is found
            dest_crs = "EPSG:4326"
            self.is_geo = True

        self.dest_crs_str = dest_crs
        with PYPROJ_TRANSFORMER_LOCK:
            self._tr_internal_to_native = Transformer.from_crs(
                CRS_TERRAIN_INTERNAL, self.dest_crs_str, always_xy=True
            )
        self.ds_transform = self.dataset.transform
        self.ds_width = self.dataset.width
        self.ds_height = self.dataset.height

        return True

    def get_native_crs(self) -> str:
        """Obte native crs de la instancia de TiffRasterWindowProvider.

        Par?metres:
        - Cap.

        Retorna:
        - str: Valor retornat pel metode.
        """
        if hasattr(self, "dest_crs_str") and self.dest_crs_str:
            return self.dest_crs_str
        return super().get_native_crs()

    def get_nominal_resolution_m(self) -> Optional[float]:
        if not self.dataset:
            return None
        try:
            res_x, res_y = self.dataset.res
            res_x = abs(float(res_x))
            res_y = abs(float(res_y))
        except Exception:
            return None
        if self.is_geo:
            lat_for_scale = float(getattr(self, "native_cy", 0.0) or 0.0)
            meters_x = res_x * 111320.0 * math.cos(math.radians(lat_for_scale))
            meters_y = res_y * 111320.0
            candidates = [abs(meters_x), abs(meters_y)]
        else:
            candidates = [res_x, res_y]
        candidates = [value for value in candidates if value > 0]
        return min(candidates) if candidates else None

    # The base implementation converts user lat/lon (EPSG:4326) into the terrain
    # internal CRS (EPSG:25831). HorizonBaker operates in that CRS.

    def prepare_region(
        self, cx: float, cy: float, radius: float, progress_callback=None
    ):
        """
        Loads the window covering [cx-radius, cy-radius, cx+radius, cy+radius] into RAM.
        `cx`, `cy` and `radius` are in terrain internal CRS (`EPSG:25831`, meters).
        """
        from rasterio.windows import Window, from_bounds

        if not self.dataset:
            return

        if progress_callback:
            progress_callback(10, "⏳ Preparando ventana de memoria ráster...")

        # Transform terrain internal coordinates to dataset native CRS center.
        try:
            self.native_cx, self.native_cy = self._tr_internal_to_native.transform(
                cx, cy
            )
        except Exception as e:
            print(
                f"[TiffRasterWindowProvider] Transform Error in prepare_region: {e}"
            )
            import traceback

            traceback.print_exc()
            self.cached_data = None
            return

        self.center_x_utm = cx
        self.center_y_utm = cy

        # Local affine scaling (Flat Earth approximation) -> Distances from meters back to degrees
        if self.is_geo:
            self.m_per_native_y = 111320.0
            self.m_per_native_x = 111320.0 * math.cos(
                math.radians(self.native_cy)
            )
        else:
            self.m_per_native_x = 1.0
            self.m_per_native_y = 1.0

        radius_native_x = radius / self.m_per_native_x
        radius_native_y = radius / self.m_per_native_y

        print(
            f"[TiffRasterWindowProvider] Caching window for Native ({self.native_cx:.4f}, {self.native_cy:.4f}) rad_xy=({radius_native_x:.3f}, {radius_native_y:.3f})..."
        )

        # Calculate bounding box
        left = self.native_cx - radius_native_x
        bottom = self.native_cy - radius_native_y
        right = self.native_cx + radius_native_x
        top = self.native_cy + radius_native_y

        # Compute the rasterio window covering this bounding box
        window = from_bounds(
            left, bottom, right, top, transform=self.ds_transform
        )

        # Ensure window is within dataset bounds, round to integers
        dataset_window = Window(
            col_off=0, row_off=0, width=self.ds_width, height=self.ds_height
        )
        window = window.intersection(dataset_window)
        window = window.round_lengths().round_offsets()

        if window.width <= 0 or window.height <= 0:
            print(
                "[TiffRasterWindowProvider] Bounding box outside of GeoTIFF bounds!"
            )
            self.cached_data = None
            return

        # Read the data into memory (Band 1)
        if progress_callback:
            progress_callback(
                50, "⏳ Leyendo porción del GeoTIFF a memoria RAM..."
            )

        with RASTERIO_LOCK:
            self.cached_data = self.dataset.read(1, window=window)
        self.cached_transform = self.dataset.window_transform(window)

        # Cache the inverse affine transform for fast (x,y) -> (r,c) lookups
        self.cached_inverse_transform = ~self.cached_transform

        print(
            f"[TiffRasterWindowProvider] Loaded {self.cached_data.shape} float array into memory."
        )
        if progress_callback:
            progress_callback(100, "✅ Porción de datos lista.")

    def get_elevation(self, x: float, y: float) -> Optional[float]:
        """Obte elevation de la instancia de TiffRasterWindowProvider.

        Par?metres:
        - x (float): Valor del parametre 'x'.
        - y (float): Valor del parametre 'y'.

        Retorna:
        - Optional[float]: Valor retornat pel metode.
        """
        if self.cached_data is None or self.cached_inverse_transform is None:
            return None

        # Convert UTM coordinates back to Native CRS (Fast Flat-Earth projection from center)
        dx = x - self.center_x_utm
        dy = y - self.center_y_utm

        native_x = self.native_cx + (dx / self.m_per_native_x)
        native_y = self.native_cy + (dy / self.m_per_native_y)

        # Transform geographic coordinates to array coordinates (col, row)
        c_float, r_float = self.cached_inverse_transform * (native_x, native_y)

        # Fast bounds check
        rows, cols = self.cached_data.shape
        if not (0 <= r_float < rows - 1 and 0 <= c_float < cols - 1):
            return None

        # Bilinear interpolation
        c0 = int(c_float)
        r0 = int(r_float)

        dc = c_float - c0
        dr = r_float - r0

        v00 = float(self.cached_data[r0, c0])
        v01 = float(self.cached_data[r0, c0 + 1])
        v10 = float(self.cached_data[r0 + 1, c0])
        v11 = float(self.cached_data[r0 + 1, c0 + 1])

        # Handle nodata values roughly via naive zero or dropping
        # In a robust system, we would check for self.dataset.nodata

        top = v00 * (1 - dc) + v01 * dc
        bot = v10 * (1 - dc) + v11 * dc
        val = top * (1 - dr) + bot * dr

        return val

    def close(self):
        """Executa el metode close de la classe TiffRasterWindowProvider.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        if self.dataset:
            with RASTERIO_LOCK:
                self.dataset.close()
            self.dataset = None
            self.cached_data = None


# The out-of-core implementation intentionally replaces the legacy class name
# below at module load time.  Keeping the old implementation above makes old
# serialized references/import traces readable while no runtime path uses its
# whole-ROI allocation strategy.
class GeoTiffElevationProvider(RasterProvider):
    """Out-of-core elevation mosaic backed by native GDAL block reads."""

    GDAL_SUFFIXES = {".tif", ".tiff", ".vrt", ".img", ".jp2"}

    def __init__(
        self,
        paths: str | os.PathLike | Sequence[str],
        *,
        source_id: str = "",
        internal_crs: str = CRS_TERRAIN_INTERNAL,
        declared_crs: str | None = None,
        transform_service: CoordinateTransformService | None = None,
        cache_bytes: int | None = None,
    ) -> None:
        raw_paths = [str(paths)] if isinstance(paths, (str, os.PathLike)) else list(paths)
        discovered: list[str] = []
        for raw in raw_paths:
            path = Path(raw).expanduser().resolve(strict=False)
            if path.is_file() and path.suffix.lower() in self.GDAL_SUFFIXES:
                discovered.append(str(path))
            elif path.is_dir():
                discovered.extend(
                    str(item)
                    for item in path.rglob("*")
                    if item.is_file() and item.suffix.lower() in self.GDAL_SUFFIXES
                )
        self.paths = sorted(set(discovered), key=str.casefold)
        self.tiff_path = self.paths[0] if len(self.paths) == 1 else ""
        self.source_id = str(source_id or "")
        self.internal_crs = normalize_crs(internal_crs)
        self.declared_crs = declared_crs
        self.transform_service = transform_service or DEFAULT_TRANSFORM_SERVICE
        self.cache_bytes = int(cache_bytes or DEFAULT_PERFORMANCE_BUDGET.dem_bytes)
        self._datasets: list[_GeoRasterDataset] = []
        self.metadata: tuple[RasterMetadata, ...] = ()
        self.dataset = None
        self.cached_data = None
        self.cached_window = None

    @property
    def fingerprint(self) -> str:
        return _path_fingerprint(
            self.paths,
            namespace="elevation",
            configuration=(self.declared_crs, self.internal_crs),
        )

    def initialize(self, progress_callback=None):
        if not self.paths:
            raise FileNotFoundError(f"GeoTIFF not found: {self.tiff_path or 'GeoTIFF'}")
        opened: list[_GeoRasterDataset] = []
        per_dataset = max(16 * 1024**2, self.cache_bytes // max(1, len(self.paths)))
        for index, path in enumerate(self.paths):
            dataset = _GeoRasterDataset(
                path,
                declared_crs=self.declared_crs,
                transform_service=self.transform_service,
                block_cache_capacity=512,
                block_cache_bytes=per_dataset,
            )
            try:
                dataset.open()
                opened.append(dataset)
            except Exception:
                dataset.close()
                if len(self.paths) == 1:
                    raise
            if progress_callback:
                progress_callback(
                    ((index + 1) / max(1, len(self.paths))) * 100.0,
                    f"Raster metadata {index + 1}/{len(self.paths)}",
                )
        if not opened:
            raise RuntimeError("No readable elevation raster")
        opened.sort(
            key=lambda item: (
                item.resolution_m if item.resolution_m is not None else math.inf,
                item.path.casefold(),
            )
        )
        self._datasets = opened
        self.metadata = tuple(item.metadata for item in opened)
        self.dataset = opened[0].dataset
        return True

    def get_native_crs(self) -> str:
        return self.internal_crs

    def get_nominal_resolution_m(self) -> Optional[float]:
        values = [
            float(item.resolution_m)
            for item in self._datasets
            if item.resolution_m is not None and float(item.resolution_m) > 0.0
        ]
        return min(values) if values else None

    def _sample_dataset(
        self,
        dataset: _GeoRasterDataset,
        x: np.ndarray,
        y: np.ndarray,
        indices: np.ndarray,
        input_crs: str,
    ) -> tuple[np.ndarray, np.ndarray]:
        native_x, native_y = self.transform_service.transform_xy(
            x[indices], y[indices], input_crs, dataset.native_crs
        )
        native_x = np.asarray(native_x)
        native_y = np.asarray(native_y)
        left, bottom, right, top = dataset.bounds
        covered = (
            np.isfinite(native_x)
            & np.isfinite(native_y)
            & (native_x >= left)
            & (native_x <= right)
            & (native_y >= bottom)
            & (native_y <= top)
        )
        if not np.any(covered):
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float32)
        covered_indices = indices[covered]
        sampled, sampled_valid = dataset.sample_native(
            native_x[covered],
            native_y[covered],
            bands=(1,),
            interpolation="bilinear",
        )
        sampled_valid = np.asarray(sampled_valid, dtype=bool).ravel()
        return covered_indices[sampled_valid], np.asarray(sampled[0]).ravel()[sampled_valid]

    def sample_elevations(
        self, x: Any, y: Any, *, input_crs: str | None = None
    ) -> ElevationBatch:
        x_arr, y_arr = np.broadcast_arrays(
            np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
        )
        shape = x_arr.shape
        flat_x = x_arr.ravel()
        flat_y = y_arr.ravel()
        values = np.zeros(flat_x.size, dtype=np.float32)
        valid = np.zeros(flat_x.size, dtype=bool)
        source_indices = np.full(flat_x.size, -1, dtype=np.int16)
        crs = normalize_crs(input_crs or self.internal_crs)
        for dataset_index, dataset in enumerate(self._datasets):
            pending = np.flatnonzero(~valid & np.isfinite(flat_x) & np.isfinite(flat_y))
            if pending.size == 0:
                break
            chosen, sampled = self._sample_dataset(dataset, flat_x, flat_y, pending, crs)
            if chosen.size:
                values[chosen] = sampled
                valid[chosen] = True
                source_indices[chosen] = int(min(dataset_index, np.iinfo(np.int16).max))
        return ElevationBatch(
            values.reshape(shape), valid.reshape(shape), source_indices.reshape(shape)
        )

    def sample_elevation(self, x: Any, y: Any) -> ElevationBatch:
        return self.sample_elevations(x, y, input_crs=self.internal_crs)

    def get_elevation(self, x: float, y: float) -> Optional[float]:
        batch = self.sample_elevation(float(x), float(y))
        return float(batch.values) if bool(batch.valid) else None

    def prepare_region(
        self,
        cx: float,
        cy: float,
        radius: float,
        progress_callback=None,
        abort_check=None,
    ):
        """Warm only observer-adjacent blocks; the far ROI stays out-of-core."""

        if not self._datasets:
            return
        warm_radius = min(
            max(20.0, self.get_nominal_resolution_m() or 30.0) * 4.0,
            500.0,
        )
        offsets = np.asarray(
            [
                (0.0, 0.0),
                (-warm_radius, 0.0),
                (warm_radius, 0.0),
                (0.0, -warm_radius),
                (0.0, warm_radius),
                (-warm_radius, -warm_radius),
                (warm_radius, -warm_radius),
                (-warm_radius, warm_radius),
                (warm_radius, warm_radius),
            ],
            dtype=np.float64,
        )
        if abort_check and abort_check():
            raise InterruptedError("Raster warmup aborted")
        self.sample_elevation(float(cx) + offsets[:, 0], float(cy) + offsets[:, 1])
        if progress_callback:
            progress_callback(100.0, "Raster metadata and near blocks ready")

    def close(self):
        for dataset in self._datasets:
            dataset.close()
        self._datasets.clear()
        self.metadata = ()
        self.dataset = None
        self.cached_data = None


class TiffRasterWindowProvider(GeoTiffElevationProvider):
    """Compatibility name for the former whole-ROI provider."""


class ElevationProviderChain(RasterProvider):
    """Ordered, nodata-aware fallback chain with per-point provenance."""

    def __init__(self, providers: Sequence[RasterProvider], *, internal_crs: str = CRS_TERRAIN_INTERNAL):
        self.providers = tuple(providers)
        self.internal_crs = normalize_crs(internal_crs)

    @property
    def metadata(self) -> tuple[RasterMetadata, ...]:
        return tuple(
            item for provider in self.providers for item in getattr(provider, "metadata", ())
        )

    def get_nominal_resolution_m(self) -> Optional[float]:
        values = []
        for provider in self.providers:
            value = provider.get_nominal_resolution_m()
            if value is not None and np.isfinite(value) and value > 0:
                values.append(float(value))
        return min(values) if values else None

    def sample_elevations(self, x: Any, y: Any, *, input_crs: str | None = None) -> ElevationBatch:
        x_arr, y_arr = np.broadcast_arrays(
            np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
        )
        values = np.zeros(x_arr.shape, dtype=np.float32)
        valid = np.zeros(x_arr.shape, dtype=bool)
        sources = np.full(x_arr.shape, -1, dtype=np.int16)
        for provider_index, provider in enumerate(self.providers):
            batch = provider.sample_elevations(x_arr, y_arr, input_crs=input_crs or self.internal_crs)
            chosen = ~valid & batch.valid
            values[chosen] = batch.values[chosen]
            valid[chosen] = True
            sources[chosen] = int(min(provider_index, np.iinfo(np.int16).max))
            if bool(np.all(valid)):
                break
        return ElevationBatch(values, valid, sources)

    def sample_elevation(self, x: Any, y: Any) -> ElevationBatch:
        return self.sample_elevations(x, y, input_crs=self.internal_crs)

    def get_elevation(self, x: float, y: float) -> Optional[float]:
        for provider in self.providers:
            value = provider.get_elevation(x, y)
            if value is not None:
                return value
        return None

    def prepare_region(self, cx, cy, radius, progress_callback=None, abort_check=None):
        for provider in self.providers:
            if abort_check and abort_check():
                raise InterruptedError("Raster warmup aborted")
            try:
                provider.prepare_region(
                    cx, cy, radius, progress_callback=progress_callback, abort_check=abort_check
                )
            except TypeError:
                provider.prepare_region(cx, cy, radius, progress_callback)

    def close(self) -> None:
        for provider in self.providers:
            close = getattr(provider, "close", None)
            if callable(close):
                close()


def create_elevation_provider(
    sources: Any,
    *,
    internal_crs: str = CRS_TERRAIN_INTERNAL,
    progress_callback=None,
) -> RasterProvider:
    """Create typed elevation providers without loading full raster payloads."""

    providers: list[RasterProvider] = []
    for source in _as_source_sequence(sources):
        if not _source_enabled(source):
            continue
        paths = _source_paths(source)
        if not paths:
            continue
        source_id = _source_identifier(source)
        declared_crs = _source_declared_crs(source)
        has_gdal = any(
            (
                Path(path).is_file()
                and Path(path).suffix.lower() in GeoTiffElevationProvider.GDAL_SUFFIXES
            )
            or (
                Path(path).is_dir()
                and any(
                    item.is_file()
                    and item.suffix.lower() in GeoTiffElevationProvider.GDAL_SUFFIXES
                    for item in Path(path).rglob("*")
                )
            )
            for path in paths
        )
        if has_gdal:
            if PERFORMANCE_FLAGS.raster_batch:
                provider = GeoTiffElevationProvider(
                    paths,
                    source_id=source_id,
                    internal_crs=internal_crs,
                    declared_crs=declared_crs,
                )
            else:
                tiff_path = next(
                    (
                        resolve_primary_dem_tiff_path(path)
                        for path in paths
                        if resolve_primary_dem_tiff_path(path)
                    ),
                    None,
                )
                if tiff_path is None:
                    continue
                provider = LegacyTiffRasterWindowProvider(tiff_path)
        else:
            provider = AscRasterProvider(paths[0])
            provider.source_id = source_id
            provider.internal_crs = normalize_crs(internal_crs)
            provider.native_crs = normalize_crs(declared_crs or internal_crs)
        provider.initialize(progress_callback=progress_callback)
        providers.append(provider)
    if not providers:
        raise FileNotFoundError("No enabled elevation source is readable")
    if len(providers) == 1:
        return providers[0]
    return ElevationProviderChain(providers, internal_crs=internal_crs)
