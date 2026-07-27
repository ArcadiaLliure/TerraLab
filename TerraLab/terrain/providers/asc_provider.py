"""ASC and NPY-backed terrain elevation provider."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.performance.budget import DEFAULT_PERFORMANCE_BUDGET
from TerraLab.terrain.crs import DEFAULT_TRANSFORM_SERVICE, normalize_crs
from TerraLab.terrain.providers.common import (
    CRS_TERRAIN_INTERNAL,
    ElevationBatch,
    RasterMetadata,
    RasterProvider,
    _is_managed_data_path,
    _terrain_materialized_root,
)

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
        from TerraLab.terrain.infrastructure.dem_tiles import (
            DemSampler,
            TileCache,
            TileIndex,
        )

        self.index = TileIndex(
            self.tiles_dir,
            callback=lambda curr, tot, msg: (
                progress_callback(curr / tot * 100, msg)
                if progress_callback
                else None
            ),
        )
        # The 5 m ASC tiles are large; the shared 2.4 GiB DEM allowance caused
        # this subprocess alone to peak near 2.8 GiB.  A 1.5 GiB resident LRU,
        # combined with per-batch LRU prioritisation, keeps the active angular
        # working set resident while preserving the 2 GiB process target.
        self.cache = TileCache(
            capacity=500,
            max_bytes=min(
                DEFAULT_PERFORMANCE_BUDGET.dem_bytes,
                1536 * 1024**2,
            ),
        )
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

        pin = getattr(self.cache, "pin", None)
        if callable(pin):
            pin(tiles_needed)

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
        # Tiles containing the most points are normally the near-observer
        # tiles shared by consecutive azimuth blocks.  Keep those at the MRU
        # end without changing the deterministic overlap sampling order.
        cache_priority = sorted(
            candidate_groups, key=lambda item: int(item[1].size)
        )
        prioritize = getattr(self.cache, "prioritize", None)
        if callable(prioritize):
            prioritize(tile for tile, _indices in cache_priority)
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
        if callable(prioritize):
            prioritize(tile for tile, _indices in cache_priority)
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
                log_suppressed_exception(__name__, "AscRasterProvider.close")
        self.sampler = None
