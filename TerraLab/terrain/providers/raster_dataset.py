"""Bounded raster dataset access and coordinate sampling."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import numpy as np

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.locks import RASTERIO_LOCK
from TerraLab.common.performance.budget import DEFAULT_PERFORMANCE_BUDGET
from TerraLab.terrain.crs import (
    DEFAULT_TRANSFORM_SERVICE,
    CoordinateTransformService,
    normalize_crs,
)
from TerraLab.terrain.providers.common import (
    CATEGORICAL_LOD_REDUCER_ID,
    CATEGORICAL_LOD_TILE_SCHEMA,
    CRS_GEOGRAPHIC,
    RasterMetadata,
    RasterSamplingCancelled,
    _path_fingerprint,
    _terrain_materialized_root,
)

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
        self.lod_rows_read = 0
        self.lod_intervals_read = 0
        self.lod_windows_read = 0
        self.lod_pixels_decoded = 0
        self.lod_bytes_decoded = 0
        self.lod_modal_cells = 0
        self.lod_requested = 0
        self.lod_unique = 0
        self.lod_cache_hits = 0
        self.lod_identity = _path_fingerprint(
            (self.path,),
            namespace=CATEGORICAL_LOD_REDUCER_ID,
            configuration=declared_crs,
        )

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
        self.mask_flags = tuple(
            tuple(
                str(getattr(flag, "name", flag)).lower()
                for flag in band_flags
            )
            for band_flags in dataset.mask_flag_enums
        )
        self._requires_mask_read = any(
            flag not in {"all_valid", "nodata"}
            for band_flags in self.mask_flags
            for flag in band_flags
        )
        self.overviews = tuple(
            tuple(int(level) for level in dataset.overviews(band))
            for band in range(1, self.count + 1)
        )
        try:
            self.dataset_tags = dict(dataset.tags())
        except Exception:
            self.dataset_tags = {}
        self.band_tags = []
        self.colormaps = []
        for band in range(1, self.count + 1):
            try:
                self.band_tags.append(dict(dataset.tags(band)))
            except Exception:
                self.band_tags.append({})
            try:
                color_map = dataset.colormap(band)
            except Exception:
                color_map = {}
            self.colormaps.append(
                {
                    str(int(code)): [int(channel) for channel in color]
                    for code, color in color_map.items()
                }
            )
        self.resolution_m = self.transform_service.resolution_metres(
            self.native_crs, self.transform, self.width, self.height
        )
        block_shapes = tuple(dataset.block_shapes or ())
        self.native_block_height, self.native_block_width = (
            tuple(int(value) for value in block_shapes[0])
            if block_shapes
            else (min(256, self.height), min(256, self.width))
        )
        self.block_height = self.native_block_height
        self.block_width = self.native_block_width
        self.virtual_blocks = bool(
            self.native_block_height <= 4
            and self.native_block_width >= 8192
        )
        if self.virtual_blocks:
            # S2GLC is distributed as one 404k-pixel-wide compressed strip per
            # row.  Treating those native strips as random-access blocks caused
            # one GDAL call per queried row.  Wide local windows amortise that
            # decompression and Python/GDAL overhead across many terrain rows.
            self.block_width = min(self.width, 65_536)
            bytes_per_pixel = max(
                1,
                sum(np.dtype(name).itemsize for name in self.dtypes)
                + (self.count if self._requires_mask_read else 1),
            )
            target_window_bytes = 16 * 1024**2
            self.block_height = min(
                self.height,
                max(
                    32,
                    min(
                        512,
                        target_window_bytes
                        // max(1, self.block_width * bytes_per_pixel),
                    ),
                ),
            )
            print(
                "[TEMPORAL][RasterWindowing] "
                f"path={self.path} "
                f"native={self.native_block_height}x{self.native_block_width} "
                f"sampling={self.block_height}x{self.block_width}"
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
                "block_width": self.native_block_width,
                "block_height": self.native_block_height,
                "sampling_block_width": self.block_width,
                "sampling_block_height": self.block_height,
                "virtual_sampling_blocks": self.virtual_blocks,
                "dtypes": list(self.dtypes),
                "colorinterp": list(self.colorinterp),
                "mask_flags": [list(flags) for flags in self.mask_flags],
                "scales": list(self.scales),
                "offsets": list(self.offsets),
                "transform": [
                    float(self.transform.a),
                    float(self.transform.b),
                    float(self.transform.c),
                    float(self.transform.d),
                    float(self.transform.e),
                    float(self.transform.f),
                ],
                "resolution_x": float(
                    math.hypot(self.transform.a, self.transform.d)
                ),
                "resolution_y": float(
                    math.hypot(self.transform.b, self.transform.e)
                ),
                "overviews": [list(levels) for levels in self.overviews],
                "tags": self.dataset_tags,
                "band_tags": self.band_tags,
                "colormaps": self.colormaps,
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
            if self._requires_mask_read:
                masks = np.asarray(reader.read_masks(bands, window=window)) > 0
                valid = np.all(masks, axis=0)
            else:
                masks = None
                valid = np.ones(data.shape[1:], dtype=bool)
        for local_band, band in enumerate(bands):
            nodata = self.nodatavals[band - 1]
            if nodata is None:
                valid &= np.isfinite(data[local_band])
            elif isinstance(nodata, float) and math.isnan(nodata):
                valid &= np.isfinite(data[local_band])
            else:
                valid &= data[local_band] != nodata
        value = (data, valid, row_off, col_off)
        # Keep the compatibility materialization marker, but write it at most
        # once.  Replacing this file for every sampled continental block caused
        # synchronous disk churn; reusable blocks live in the byte-bounded LRU.
        if not self.materialized_path.exists():
            try:
                self.materialized_dir.mkdir(parents=True, exist_ok=True)
                temporary = self.materialized_path.with_suffix(".tmp.npy")
                np.save(temporary, data, allow_pickle=False)
                os.replace(temporary, self.materialized_path)
            except OSError:
                pass
        self.bytes_read += int(data.nbytes + valid.nbytes)
        self._cache_put(key, value)
        return value

    def sample_native(
        self,
        native_x: Any,
        native_y: Any,
        *,
        bands: tuple[int, ...] = (1,),
        interpolation: str = "bilinear",
        progress_callback=None,
        abort_check=None,
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
        interpolation_mode = str(interpolation).lower()
        nearest = interpolation_mode == "nearest"
        # A nearest categorical read must not round-trip integral class codes
        # through float32.  That conversion is lossy for identifiers above
        # 2**24 and, more importantly, obscures the discrete contract.
        output_dtype = (
            np.result_type(*(np.dtype(self.dtypes[band - 1]) for band in bands))
            if nearest
            else np.float32
        )
        output = np.zeros((len(bands), flat_x.size), dtype=output_dtype)
        valid = np.isfinite(col) & np.isfinite(row)
        safe_col = np.where(valid, col, 0.0)
        safe_row = np.where(valid, row, 0.0)

        linear_light_rgb = interpolation_mode == "bilinear_srgb"
        if nearest:
            col0 = np.floor(safe_col + 0.5).astype(np.int64, copy=False)
            row0 = np.floor(safe_row + 0.5).astype(np.int64, copy=False)
            valid &= (
                (col0 >= 0) & (row0 >= 0) & (col0 < self.width) & (row0 < self.height)
            )
        else:
            col0 = np.floor(safe_col).astype(np.int64, copy=False)
            row0 = np.floor(safe_row).astype(np.int64, copy=False)
            # Bilinear interpolation extends the outermost pixel value to the
            # raster edge.  This keeps valid coverage at the first/last half
            # pixel without ever sampling beyond the dataset.
            valid &= (
                (safe_col >= -0.5)
                & (safe_row >= -0.5)
                & (safe_col <= self.width - 0.5)
                & (safe_row <= self.height - 0.5)
            )
            col0 = np.clip(col0, 0, max(0, self.width - 1))
            row0 = np.clip(row0, 0, max(0, self.height - 1))

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

        if callable(progress_callback):
            progress_callback(0.0, "reading-raster-blocks")

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
            r1 = np.minimum(local_row + 1, self.height - 1 - row_off)
            c1 = np.minimum(local_col + 1, self.width - 1 - col_off)
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
                rr = r1[point_valid]
                cc = c1[point_valid]
                dc = np.clip(col[chosen] - col0[chosen], 0.0, 1.0).astype(
                    np.float32
                )
                dr = np.clip(row[chosen] - row0[chosen], 0.0, 1.0).astype(
                    np.float32
                )
                v00 = data[:, lr, lc].astype(np.float32, copy=False)
                v01 = data[:, lr, cc].astype(np.float32, copy=False)
                v10 = data[:, rr, lc].astype(np.float32, copy=False)
                v11 = data[:, rr, cc].astype(np.float32, copy=False)
                channel_maximum = None
                if linear_light_rgb:
                    maxima = []
                    for band in bands:
                        dtype = np.dtype(self.dtypes[band - 1])
                        maxima.append(
                            float(np.iinfo(dtype).max)
                            if np.issubdtype(dtype, np.integer)
                            else 1.0
                        )
                    channel_maximum = np.asarray(maxima, dtype=np.float32)[:, None]

                    def _decode_srgb(values):
                        encoded = np.clip(values / channel_maximum, 0.0, 1.0)
                        return np.where(
                            encoded <= 0.04045,
                            encoded / 12.92,
                            ((encoded + 0.055) / 1.055) ** 2.4,
                        )

                    v00 = _decode_srgb(v00)
                    v01 = _decode_srgb(v01)
                    v10 = _decode_srgb(v10)
                    v11 = _decode_srgb(v11)
                top = v00 * (1.0 - dc) + v01 * dc
                bottom = v10 * (1.0 - dc) + v11 * dc
                interpolated = top * (1.0 - dr) + bottom * dr
                if linear_light_rgb:
                    encoded = np.where(
                        interpolated <= 0.0031308,
                        interpolated * 12.92,
                        1.055 * np.power(interpolated, 1.0 / 2.4) - 0.055,
                    )
                    interpolated = encoded * channel_maximum
                output[:, chosen] = interpolated
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
            if callable(abort_check) and abort_check():
                raise RasterSamplingCancelled("Raster sampling cancelled")
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
            if callable(progress_callback):
                progress_callback(
                    min(1.0, (window_start + len(window)) / max(1, len(groups))),
                    "reading-raster-blocks",
                )
        if callable(abort_check) and abort_check():
            raise RasterSamplingCancelled("Raster sampling cancelled")
        return output.reshape((len(bands),) + shape), valid.reshape(shape)

    def sample_native_lod(
        self,
        native_x: Any,
        native_y: Any,
        lod_factors: Any,
        *,
        bands: tuple[int, ...] = (1,),
        tile_store: Any = None,
        categorical_decoder: Callable[
            [np.ndarray, tuple[int, ...]], tuple[np.ndarray, np.ndarray]
        ]
        | None = None,
        reducer_identity: str = CATEGORICAL_LOD_REDUCER_ID,
        priority_classes: tuple[int, ...] = (),
        progress_callback=None,
        abort_check=None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Sample categorical cells using nearest at LOD 1 and semantic reduction.

        Each power-of-two cell is aligned to the raster origin.  Invalid pixels
        are ignored.  Ties prefer the cell's central pixel when it is one of
        the modal classes and otherwise prefer the numerically smallest class.
        If a cell contains a configured isolated feature (for example a
        building or water), that class wins in the declared priority order so
        a small but meaningful region is not erased by the surrounding mode.
        Sparse reduced results, never source-centre pixels, are persisted.
        """

        if self.dataset is None:
            raise RuntimeError("Raster dataset is not initialized")
        bands = tuple(int(value) for value in bands)
        if not bands or min(bands) < 1 or max(bands) > self.count:
            raise ValueError("Requested raster band is unavailable")
        x_arr, y_arr, factor_arr = np.broadcast_arrays(
            np.asarray(native_x, dtype=np.float64),
            np.asarray(native_y, dtype=np.float64),
            np.asarray(lod_factors, dtype=np.int16),
        )
        shape = x_arr.shape
        flat_x = x_arr.ravel()
        flat_y = y_arr.ravel()
        factors = np.asarray(factor_arr, dtype=np.int64).ravel()
        allowed = np.asarray((1, 2, 4, 8, 16, 32, 64, 128), dtype=np.int64)
        factors = allowed[
            np.clip(np.searchsorted(allowed, np.maximum(1, factors), side="right") - 1, 0, len(allowed) - 1)
        ]

        inv = self.inverse_transform
        col_float = inv.a * flat_x + inv.b * flat_y + inv.c - 0.5
        row_float = inv.d * flat_x + inv.e * flat_y + inv.f - 0.5
        finite = np.isfinite(col_float) & np.isfinite(row_float)
        native_cols = np.floor(np.where(finite, col_float, 0.0) + 0.5).astype(np.int64)
        native_rows = np.floor(np.where(finite, row_float, 0.0) + 0.5).astype(np.int64)
        valid = finite & (
            (native_cols >= 0)
            & (native_rows >= 0)
            & (native_cols < self.width)
            & (native_rows < self.height)
        )
        output = np.full((1, flat_x.size), -1, dtype=np.int64)
        candidates = np.flatnonzero(valid)
        self.lod_requested += int(candidates.size)
        if not candidates.size:
            return output.reshape((1,) + shape), valid.reshape(shape)

        coarse_rows = native_rows[candidates] // factors[candidates]
        coarse_cols = native_cols[candidates] // factors[candidates]
        query_keys = np.column_stack((factors[candidates], coarse_rows, coarse_cols))
        unique_keys, inverse = np.unique(query_keys, axis=0, return_inverse=True)
        self.lod_unique += int(len(unique_keys))
        unique_values = np.full(len(unique_keys), -1, dtype=np.int64)
        unique_valid = np.zeros(len(unique_keys), dtype=bool)
        identity_digest = hashlib.blake2b(digest_size=20)
        identity_digest.update(self.lod_identity.encode("ascii"))
        identity_digest.update(str(reducer_identity).encode("utf-8"))
        semantic_priority = tuple(
            dict.fromkeys(int(value) for value in priority_classes)
        )
        identity_digest.update(repr(semantic_priority).encode("ascii"))
        reducer_cache_identity = identity_digest.hexdigest()

        # First resolve sparse entries already present in persistent 256x256
        # tiles.  Sort/group in NumPy so Python work scales with the number of
        # touched tiles, not with hundreds of thousands of sample points.
        unique_factors = unique_keys[:, 0].astype(np.int64, copy=False)
        unique_rows = unique_keys[:, 1].astype(np.int64, copy=False)
        unique_cols = unique_keys[:, 2].astype(np.int64, copy=False)
        tile_rows = unique_rows // 256
        tile_cols = unique_cols // 256
        tile_order = np.lexsort((tile_cols, tile_rows, unique_factors))
        ordered_tile_keys = np.column_stack(
            (
                unique_factors[tile_order],
                tile_rows[tile_order],
                tile_cols[tile_order],
            )
        )
        tile_starts = np.r_[
            0,
            np.flatnonzero(
                np.any(ordered_tile_keys[1:] != ordered_tile_keys[:-1], axis=1)
            )
            + 1,
        ]
        tile_stops = np.r_[tile_starts[1:], len(tile_order)]

        tile_state: dict[
            tuple[int, int, int], tuple[np.ndarray, np.ndarray, np.ndarray]
        ] = {}
        missing_parts: list[np.ndarray] = []
        band_token = "-".join(str(value) for value in bands)
        for start, stop in zip(tile_starts, tile_stops):
            unique_indices = tile_order[start:stop]
            factor, tile_row, tile_col = (
                int(value) for value in ordered_tile_keys[start]
            )
            tile_key = (factor, tile_row, tile_col)
            stored_positions = np.asarray([], dtype=np.int32)
            stored_values = np.asarray([], dtype=np.int64)
            stored_valid = np.asarray([], dtype=bool)
            relative = (
                f"tiles/{reducer_cache_identity}/{band_token}/f{factor}/"
                f"r{tile_row}_c{tile_col}"
            )
            loaded = tile_store.load(relative) if tile_store is not None else None
            if loaded is not None:
                metadata, arrays = loaded
                if (
                    int(metadata.get("schema", 0))
                    == CATEGORICAL_LOD_TILE_SCHEMA
                    and str(metadata.get("dataset", ""))
                    == reducer_cache_identity
                    and str(metadata.get("reducer", ""))
                    == str(reducer_identity)
                    and tuple(int(value) for value in metadata.get("bands", ())) == bands
                ):
                    stored_positions = np.asarray(arrays.get("positions", ()), dtype=np.int32)
                    stored_values = np.asarray(
                        arrays.get("class_ids", ()), dtype=np.int64
                    )
                    stored_valid = np.asarray(arrays.get("valid", ()), dtype=bool)
                    if (
                        stored_values.shape != stored_positions.shape
                        or stored_valid.shape != stored_positions.shape
                    ):
                        stored_positions = np.asarray([], dtype=np.int32)
                        stored_values = np.asarray([], dtype=np.int64)
                        stored_valid = np.asarray([], dtype=bool)
            tile_state[tile_key] = (stored_positions, stored_values, stored_valid)
            local_positions = (
                (unique_rows[unique_indices] % 256) * 256
                + unique_cols[unique_indices] % 256
            ).astype(np.int32, copy=False)
            if stored_positions.size:
                stored_indices = np.searchsorted(stored_positions, local_positions)
                bounded = np.minimum(stored_indices, len(stored_positions) - 1)
                hits = (stored_indices < len(stored_positions)) & (
                    stored_positions[bounded] == local_positions
                )
            else:
                stored_indices = np.zeros(len(unique_indices), dtype=np.int64)
                hits = np.zeros(len(unique_indices), dtype=bool)
            if np.any(hits):
                hit_indices = unique_indices[hits]
                hit_stored = stored_indices[hits]
                unique_values[hit_indices] = stored_values[hit_stored]
                unique_valid[hit_indices] = stored_valid[hit_stored]
                self.lod_cache_hits += int(np.count_nonzero(hits))
            if not np.all(hits):
                missing_parts.append(unique_indices[~hits])

        missing = (
            np.concatenate(missing_parts).astype(np.int64, copy=False)
            if missing_parts
            else np.asarray([], dtype=np.int64)
        )

        # Build bounded windows grouped by factor and LOD row.  Nearby columns
        # share a Rasterio read, while sparse continental queries cannot force
        # an enormous min..max interval.
        windows: list[tuple[int, int, np.ndarray]] = []
        if missing.size:
            order = np.lexsort(
                (unique_cols[missing], unique_rows[missing], unique_factors[missing])
            )
            ordered_missing = missing[order]
            keys = np.column_stack(
                (
                    unique_factors[ordered_missing],
                    unique_rows[ordered_missing],
                )
            )
            starts = np.r_[
                0,
                np.flatnonzero(np.any(keys[1:] != keys[:-1], axis=1)) + 1,
            ]
            stops = np.r_[starts[1:], len(ordered_missing)]
            for start, stop in zip(starts, stops):
                row_indices = ordered_missing[start:stop]
                factor = int(unique_factors[row_indices[0]])
                coarse_row = int(unique_rows[row_indices[0]])
                columns = unique_cols[row_indices]
                maximum_cells = max(1, (1024 * 1024) // max(1, factor * factor))
                gap_limit = max(2, min(32, 2048 // max(1, factor)))
                split = np.flatnonzero(
                    (np.diff(columns) > gap_limit)
                    | (
                        np.arange(1, len(columns), dtype=np.int64)
                        % maximum_cells
                        == 0
                    )
                ) + 1
                part_starts = np.r_[0, split]
                part_stops = np.r_[split, len(row_indices)]
                windows.extend(
                    (factor, coarse_row, row_indices[a:b])
                    for a, b in zip(part_starts, part_stops)
                    if b > a
                )

        if callable(progress_callback):
            progress_callback(0.0, "reading-lod-windows")
        progress_last_emit = time.perf_counter()
        from rasterio.windows import Window

        reader = self._reader()
        if reader is None:
            raise RuntimeError("Raster dataset is closed")
        window_count = max(1, len(windows))
        for window_position, (factor, coarse_row, unique_indices) in enumerate(
            windows
        ):
            if callable(abort_check) and abort_check():
                raise RasterSamplingCancelled("Raster LOD sampling cancelled")
            coarse_columns = unique_cols[unique_indices]
            row_off = int(coarse_row * factor)
            col_off = int(np.min(coarse_columns) * factor)
            row_stop = min(self.height, row_off + factor)
            col_stop = min(
                self.width, (int(np.max(coarse_columns)) + 1) * factor
            )
            height = max(0, row_stop - row_off)
            width = max(0, col_stop - col_off)
            if not height or not width:
                continue
            window = Window(col_off, row_off, width, height)
            lock = self._read_lock if os.name == "nt" else threading.Lock()
            with lock:
                data = np.asarray(reader.read(bands, window=window))
                if self._requires_mask_read:
                    data_valid = np.all(
                        np.asarray(reader.read_masks(bands, window=window)) > 0,
                        axis=0,
                    )
                else:
                    data_valid = np.ones((height, width), dtype=bool)
            for local_band, band in enumerate(bands):
                nodata = self.nodatavals[band - 1]
                if nodata is None or (isinstance(nodata, float) and math.isnan(nodata)):
                    data_valid &= np.isfinite(data[local_band])
                else:
                    data_valid &= data[local_band] != nodata
            if categorical_decoder is None:
                if len(bands) != 1:
                    raise ValueError(
                        "Categorical modal LOD requires one integer band or "
                        "an explicit pixel decoder"
                    )
                class_window = np.asarray(data[0], dtype=np.int64)
                decoded_valid = data_valid
            else:
                class_window, decoded_valid = categorical_decoder(data, bands)
                class_window = np.asarray(class_window, dtype=np.int64)
                decoded_valid = np.asarray(decoded_valid, dtype=bool) & data_valid
                if (
                    class_window.shape != (height, width)
                    or decoded_valid.shape != (height, width)
                ):
                    raise ValueError(
                        "Categorical decoder must return one class and validity "
                        "value per source pixel"
                    )

            local_starts = (
                coarse_columns.astype(np.int64, copy=False) * factor - col_off
            )
            column_offsets = (
                local_starts[:, None]
                + np.arange(factor, dtype=np.int64)[None, :]
            )
            column_inside = column_offsets < width
            safe_columns = np.minimum(column_offsets, width - 1)
            cell_values = np.transpose(
                class_window[:, safe_columns], (1, 0, 2)
            )
            cell_valid = np.transpose(
                decoded_valid[:, safe_columns], (1, 0, 2)
            )
            cell_valid &= column_inside[:, None, :]
            cell_count = len(unique_indices)
            flat_cell = np.repeat(
                np.arange(cell_count, dtype=np.int64), height * factor
            )
            flat_values = cell_values.reshape(-1)
            flat_valid = cell_valid.reshape(-1)
            valid_cells = flat_cell[flat_valid]
            valid_values = flat_values[flat_valid]
            modal = np.full(cell_count, -1, dtype=np.int64)
            modal_valid = np.zeros(cell_count, dtype=bool)
            if valid_values.size:
                pair_order = np.lexsort((valid_values, valid_cells))
                pair_cells_sorted = valid_cells[pair_order]
                pair_values_sorted = valid_values[pair_order]
                pair_starts = np.r_[
                    0,
                    np.flatnonzero(
                        (pair_cells_sorted[1:] != pair_cells_sorted[:-1])
                        | (pair_values_sorted[1:] != pair_values_sorted[:-1])
                    )
                    + 1,
                ]
                pair_stops = np.r_[pair_starts[1:], len(pair_order)]
                pair_cells = pair_cells_sorted[pair_starts]
                pair_values = pair_values_sorted[pair_starts]
                pair_counts = pair_stops - pair_starts
                maximum_counts = np.zeros(cell_count, dtype=np.int64)
                np.maximum.at(maximum_counts, pair_cells, pair_counts)
                contenders = pair_counts == maximum_counts[pair_cells]
                smallest = np.full(cell_count, np.iinfo(np.int64).max, dtype=np.int64)
                np.minimum.at(
                    smallest, pair_cells[contenders], pair_values[contenders]
                )
                modal[:] = smallest
                modal_valid[np.unique(pair_cells)] = True

                central_row = min(factor // 2, height - 1)
                central_columns = np.minimum(
                    local_starts + factor // 2, width - 1
                )
                central_values = class_window[central_row, central_columns]
                central_valid = decoded_valid[central_row, central_columns]
                central_is_contender = (
                    contenders
                    & central_valid[pair_cells]
                    & (pair_values == central_values[pair_cells])
                )
                winner_cells = pair_cells[central_is_contender]
                if winner_cells.size:
                    modal[winner_cells] = central_values[winner_cells]
                for priority_class in reversed(semantic_priority):
                    priority_pairs = pair_values == int(priority_class)
                    if np.any(priority_pairs):
                        priority_cells = pair_cells[priority_pairs]
                        modal[priority_cells] = int(priority_class)
                        modal_valid[priority_cells] = True
            unique_values[unique_indices] = modal
            unique_valid[unique_indices] = modal_valid
            read_bytes = int(data.nbytes + data_valid.nbytes)
            self.bytes_read += read_bytes
            self.lod_rows_read += int(height)
            self.lod_intervals_read += 1
            self.lod_windows_read += 1
            self.lod_pixels_decoded += int(height * width)
            self.lod_bytes_decoded += read_bytes
            self.lod_modal_cells += int(len(unique_indices))
            if callable(progress_callback) and (
                window_position + 1 >= window_count
                or time.perf_counter() - progress_last_emit >= 0.10
            ):
                progress_callback(
                    (window_position + 1.0) / window_count,
                    "reading-lod-windows",
                )
                progress_last_emit = time.perf_counter()

        if tile_store is not None and missing.size:
            missing_tile_order = np.lexsort(
                (
                    tile_cols[missing],
                    tile_rows[missing],
                    unique_factors[missing],
                )
            )
            ordered_missing = missing[missing_tile_order]
            ordered_missing_keys = np.column_stack(
                (
                    unique_factors[ordered_missing],
                    tile_rows[ordered_missing],
                    tile_cols[ordered_missing],
                )
            )
            missing_starts = np.r_[
                0,
                np.flatnonzero(
                    np.any(
                        ordered_missing_keys[1:] != ordered_missing_keys[:-1],
                        axis=1,
                    )
                )
                + 1,
            ]
            missing_stops = np.r_[missing_starts[1:], len(ordered_missing)]
            for start, stop in zip(missing_starts, missing_stops):
                new_indices = ordered_missing[start:stop]
                factor, tile_row, tile_col = (
                    int(value) for value in ordered_missing_keys[start]
                )
                tile_key = (factor, tile_row, tile_col)
                positions, values, validity = tile_state[tile_key]
                new_positions = (
                    (unique_rows[new_indices] % 256) * 256
                    + unique_cols[new_indices] % 256
                ).astype(np.int32, copy=False)
                merged_positions = np.concatenate((positions, new_positions))
                merged_values = np.concatenate((values, unique_values[new_indices]))
                merged_valid = np.concatenate((validity, unique_valid[new_indices]))
                order = np.argsort(merged_positions, kind="stable")
                relative = (
                    f"tiles/{reducer_cache_identity}/{band_token}/f{factor}/"
                    f"r{tile_row}_c{tile_col}"
                )
                try:
                    tile_store.save(
                        relative,
                        {
                            "schema": CATEGORICAL_LOD_TILE_SCHEMA,
                            "dataset": reducer_cache_identity,
                            "reducer": str(reducer_identity),
                            "bands": list(bands),
                        },
                        {
                            "positions": merged_positions[order],
                            "class_ids": merged_values[order],
                            "valid": merged_valid[order],
                        },
                        durable=False,
                        prune_after=False,
                    )
                except (OSError, ValueError):
                    # A full or unavailable cache must not make the source
                    # unusable; the freshly sampled values remain valid.
                    pass
            try:
                tile_store.prune()
            except OSError:
                pass

        candidate_values = unique_values[inverse]
        candidate_valid = unique_valid[inverse]
        output[0, candidates] = candidate_values
        valid[candidates] &= candidate_valid
        if callable(progress_callback):
            progress_callback(1.0, "lod-ready")
        return output.reshape((1,) + shape), valid.reshape(shape)

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
                log_suppressed_exception(__name__, "_GeoRasterDataset.close")
        if self.dataset is not None:
            try:
                with RASTERIO_LOCK:
                    self.dataset.close()
            except Exception:
                log_suppressed_exception(__name__, "_GeoRasterDataset.close")
        self.dataset = None
        with self._cache_lock:
            self._cache.clear()
            self._cache_bytes = 0
