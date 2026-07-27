"""DEM tile indexing, loading, and sampling infrastructure."""

from __future__ import annotations

import glob
import math
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.performance.budget import DEFAULT_PERFORMANCE_BUDGET
from TerraLab.terrain.crs import CRS_GEOGRAPHIC, CRS_TERRAIN_INTERNAL


class TileIndex:
    """Indexes DEM .txt/.asc tiles by bounding box in projected coordinates."""

    def __init__(
        self,
        tiles_dir: str,
        patterns: list = ["*.asc", "*.txt", "*.npy"],
        callback=None,
    ):
        self.tiles_dir = tiles_dir
        self.patterns = patterns
        self.tiles: List[Dict] = []
        self.global_bbox = [np.inf, np.inf, -np.inf, -np.inf]
        self._spatial_cell_size = 1.0
        self._spatial_origin = (0.0, 0.0)
        self._spatial_cells: Dict[Tuple[int, int], Tuple[int, ...]] = {}
        self._build_index(callback)

    def _build_index(self, callback=None):
        files = []
        if os.path.isfile(self.tiles_dir):
            files = [self.tiles_dir]
        else:
            for pat in self.patterns:
                search_path = os.path.join(self.tiles_dir, pat)
                files.extend(glob.glob(search_path))

        print(
            f"[HorizonEngine] Indexing {len(files)} tiles from {self.tiles_dir} ({self.patterns})..."
        )

        if callback:
            callback(0, len(files), "Indexing files...")

        for i, fpath in enumerate(files):
            try:
                if fpath.endswith(".npy"):
                    # Parse bbox from filename: Y_(ymin_ymax)X_(xmin_xmax).npy
                    basename = os.path.basename(fpath)
                    bbox = self._parse_npy_filename(basename)
                    if bbox:
                        # For NPY tiles, we still need a cellulsize to sample.
                        # Assuming default 5.0m for ICGC tiles if not encoded in filename.
                        # Real NPY tiles should probably have their own header files.
                        self.tiles.append(
                            {
                                "path": fpath,
                                "header": {
                                    "NPY": True,
                                    "CELLSIZE": 5.0,
                                    "NCOLS": 1,
                                    "NROWS": 1,
                                },  # Minimal
                                "bbox": bbox,
                            }
                        )
                    else:
                        continue
                else:
                    header = self._read_header(fpath)
                    bbox = self._compute_bbox(header)
                    self.tiles.append(
                        {
                            "path": fpath,
                            "header": header,
                            "bbox": bbox,
                        }
                    )

                # Update global bbox
                self.global_bbox[0] = min(self.global_bbox[0], bbox[0])
                self.global_bbox[1] = min(self.global_bbox[1], bbox[1])
                self.global_bbox[2] = max(self.global_bbox[2], bbox[2])
                self.global_bbox[3] = max(self.global_bbox[3], bbox[3])

                # Report progress every 50 files
                if callback and i % 50 == 0:
                    callback(i, len(files), f"Indexing tile {i}/{len(files)}")

            except Exception:
                # print(f"[HorizonEngine] Skipping {fpath}: {e}")
                log_suppressed_exception(__name__, "TileIndex._build_index")

        if callback:
            callback(
                len(files), len(files), f"Indexed {len(self.tiles)} tiles."
            )

        self._build_spatial_index()
        print(f"[HorizonEngine] Indexed {len(self.tiles)} valid tiles.")

    def _build_spatial_index(self) -> None:
        """Build a compact uniform-grid lookup without changing tile precedence."""

        if not self.tiles:
            self._spatial_cells = {}
            return
        widths = [
            max(0.0, float(tile["bbox"][2]) - float(tile["bbox"][0]))
            for tile in self.tiles
        ]
        heights = [
            max(0.0, float(tile["bbox"][3]) - float(tile["bbox"][1]))
            for tile in self.tiles
        ]
        dimensions = [
            value
            for value in widths + heights
            if np.isfinite(value) and value > 0
        ]
        self._spatial_cell_size = max(1.0, float(np.median(dimensions)))
        origin_x = float(self.global_bbox[0])
        origin_y = float(self.global_bbox[1])
        self._spatial_origin = (origin_x, origin_y)
        cells: Dict[Tuple[int, int], List[int]] = {}
        cell_size = self._spatial_cell_size
        for tile_index, tile in enumerate(self.tiles):
            xmin, ymin, xmax, ymax = (float(value) for value in tile["bbox"])
            ix0 = math.floor((xmin - origin_x) / cell_size)
            iy0 = math.floor((ymin - origin_y) / cell_size)
            # Include the cell touching an upper edge; exact bbox checks later
            # discard points that belong to the neighbouring tile.
            ix1 = math.floor((xmax - origin_x) / cell_size)
            iy1 = math.floor((ymax - origin_y) / cell_size)
            for iy in range(iy0, iy1 + 1):
                for ix in range(ix0, ix1 + 1):
                    cells.setdefault((ix, iy), []).append(tile_index)
        self._spatial_cells = {
            key: tuple(indices) for key, indices in cells.items()
        }

    def candidate_point_groups(
        self, x: np.ndarray, y: np.ndarray
    ) -> List[Tuple[Dict, np.ndarray]]:
        """Group flat point indices by possible tile, in index precedence order."""

        flat_x = np.asarray(x, dtype=np.float64).ravel()
        flat_y = np.asarray(y, dtype=np.float64).ravel()
        if flat_x.shape != flat_y.shape:
            raise ValueError("Spatial lookup coordinate shapes must match")
        if not self.tiles or flat_x.size == 0:
            return []

        origin_x, origin_y = self._spatial_origin
        cell_size = self._spatial_cell_size
        cell_x = np.floor((flat_x - origin_x) / cell_size).astype(np.int64)
        cell_y = np.floor((flat_y - origin_y) / cell_size).astype(np.int64)
        order = np.lexsort((cell_x, cell_y))
        sorted_x = cell_x[order]
        sorted_y = cell_y[order]
        boundaries = (
            np.flatnonzero(
                (sorted_x[1:] != sorted_x[:-1])
                | (sorted_y[1:] != sorted_y[:-1])
            )
            + 1
        )
        groups: Dict[int, List[np.ndarray]] = {}
        for positions in np.split(order, boundaries):
            if positions.size == 0:
                continue
            key = (int(cell_x[positions[0]]), int(cell_y[positions[0]]))
            for tile_index in self._spatial_cells.get(key, ()):
                tile = self.tiles[tile_index]
                xmin, ymin, xmax, ymax = tile["bbox"]
                selected = positions[
                    (flat_x[positions] >= xmin)
                    & (flat_x[positions] < xmax)
                    & (flat_y[positions] >= ymin)
                    & (flat_y[positions] < ymax)
                ]
                if selected.size:
                    groups.setdefault(tile_index, []).append(selected)
        return [
            (self.tiles[tile_index], np.concatenate(groups[tile_index]))
            for tile_index in sorted(groups)
        ]

    @staticmethod
    def _parse_npy_filename(
        name: str,
    ) -> Optional[Tuple[float, float, float, float]]:
        # Format: Y_(ymin_ymax)X_(xmin_xmax).npy
        try:
            import re

            base = name.rsplit(".npy", 1)[0]
            match = re.search(
                r"Y_\((-?\d+(?:\.\d+)?)_(-?\d+(?:\.\d+)?)\)"
                r"X_\((-?\d+(?:\.\d+)?)_(-?\d+(?:\.\d+)?)\)",
                base,
                re.IGNORECASE,
            )
            if match is None:
                return None

            y_min, y_max, x_min, x_max = map(float, match.groups())

            return (x_min, y_min, x_max, y_max)
        except Exception:
            return None

    @staticmethod
    def _read_header(path: str) -> Dict:
        header = {}
        try:
            with open(path, "r") as f:
                for _ in range(6):
                    line = f.readline().strip()
                    if not line:
                        break
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].upper()
                        val = parts[1]
                        if key in [
                            "NCOLS",
                            "NROWS",
                            "XLLCORNER",
                            "YLLCORNER",
                            "XLLCENTER",
                            "YLLCENTER",
                            "CELLSIZE",
                            "NODATA_VALUE",
                        ]:
                            header[key] = float(val)

            if "NCOLS" in header:
                header["NCOLS"] = int(header["NCOLS"])
            if "NROWS" in header:
                header["NROWS"] = int(header["NROWS"])
        except Exception:
            log_suppressed_exception(__name__, "TileIndex._read_header")
        return header

    @staticmethod
    def _compute_bbox(h: Dict) -> Tuple[float, float, float, float]:
        s = h.get("CELLSIZE", 5.0)
        half = s / 2.0

        if "XLLCENTER" in h:
            xmin = h["XLLCENTER"] - half
            ymin = h["YLLCENTER"] - half
        elif "XLLCORNER" in h:
            xmin = h["XLLCORNER"]
            ymin = h["YLLCORNER"]
        else:
            raise ValueError(
                f"Header missing XLLCENTER or XLLCORNER: {h.keys()}"
            )

        xmax = xmin + h["NCOLS"] * s
        ymax = ymin + h["NROWS"] * s
        return (xmin, ymin, xmax, ymax)

    def find_tile(self, x: float, y: float) -> Optional[Dict]:
        """Returns the first tile containing (x, y)."""
        # Fast bounds check
        if (
            x < self.global_bbox[0]
            or y < self.global_bbox[1]
            or x > self.global_bbox[2]
            or y > self.global_bbox[3]
        ):
            return None

        # O(N) linear scan (usually fine for ~1000 tiles)
        for t in self.tiles:
            xmin, ymin, xmax, ymax = t["bbox"]
            if xmin <= x < xmax and ymin <= y < ymax:
                return t
        return None

    def get_overlapping_tiles(
        self, cx: float, cy: float, radius: float
    ) -> List[Dict]:
        """Return all tiles within radius of (cx, cy)."""
        r2 = radius * radius
        # Sort by distance (closest first)
        candidates = []

        for t in self.tiles:
            xmin, ymin, xmax, ymax = t["bbox"]
            # Distance from point to rectangle (squared)
            dx = max(xmin - cx, 0, cx - xmax)
            dy = max(ymin - cy, 0, cy - ymax)
            dist_sq = dx * dx + dy * dy
            if dist_sq <= r2:
                candidates.append((dist_sq, t))

        candidates.sort(key=lambda x: x[0])
        return [c[1] for c in candidates]


# ─────────────────────────────────────────────
#  Tile Cache
# ─────────────────────────────────────────────


class TileCache:
    """LRU cache for loaded DEM grids, with .npy binary caching on disk. Thread-safe."""

    def __init__(self, capacity: int = 16, max_bytes: int | None = None):
        self.capacity = capacity
        self.max_bytes = max(
            0,
            int(
                DEFAULT_PERFORMANCE_BUDGET.dem_bytes
                if max_bytes is None
                else max_bytes
            ),
        )
        self.cache: OrderedDict = OrderedDict()
        self._cache_sizes: dict[str, int] = {}
        self._cache_bytes = 0
        self._pinned_paths: set[str] = set()
        self._lock = threading.Lock()
        self.cache_hits = 0
        self.cache_misses = 0
        self.bytes_read = 0
        # Different tiles can be read in parallel.  Bounded striped locks keep
        # same-path materialisation exclusive without a lock object per tile.
        self._tile_io_lock = threading.Lock()
        self._tile_io_locks = tuple(threading.Lock() for _ in range(64))
        self._packaged_cache_root = (
            Path(__file__).resolve().parents[1] / "data" / "terrain_cache"
        )

    @property
    def resident_bytes(self) -> int:
        with self._lock:
            return int(self._cache_bytes)

    def prioritize(self, tile_infos: Iterable[Dict]) -> None:
        """Make non-candidate tiles the first entries evicted by the LRU."""
        paths = [
            str(tile.get("path", ""))
            for tile in tile_infos
            if tile.get("path")
        ]
        if not paths:
            return
        with self._lock:
            for path in paths:
                if path in self.cache:
                    self.cache.move_to_end(path)

    def pin(self, tile_infos: Iterable[Dict], max_entries: int = 32) -> None:
        """Keep a bounded near-field working set inside the byte budget."""
        paths = [
            str(tile.get("path", ""))
            for tile in tile_infos
            if tile.get("path")
        ][: max(0, int(max_entries))]
        with self._lock:
            self._pinned_paths.update(paths)
            for path in paths:
                if path in self.cache:
                    self.cache.move_to_end(path)

    def _io_lock_for(self, path: str) -> "threading.Lock":
        normalized = os.path.normcase(os.path.abspath(path))
        return self._tile_io_locks[hash(normalized) % len(self._tile_io_locks)]

    def _store(self, path: str, value: tuple[np.ndarray, Dict]) -> None:
        size = int(value[0].nbytes)
        with self._lock:
            old = self.cache.pop(path, None)
            if old is not None:
                self._cache_bytes -= self._cache_sizes.pop(path, 0)
            if self.max_bytes <= 0 or size > self.max_bytes:
                return
            self.cache[path] = value
            self._cache_sizes[path] = size
            self._cache_bytes += size
            while self.cache and self._cache_bytes > self.max_bytes:
                old_path = next(
                    (
                        candidate
                        for candidate in self.cache
                        if candidate not in self._pinned_paths
                    ),
                    None,
                )
                if old_path is None:
                    break
                self.cache.pop(old_path)
                self._cache_bytes -= self._cache_sizes.pop(old_path, 0)

    def clear(self) -> None:
        with self._lock:
            self.cache.clear()
            self._cache_sizes.clear()
            self._cache_bytes = 0
            self._pinned_paths.clear()

    def load(
        self, tile_info: Dict
    ) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        """Executa el metode load de la classe TileCache.

        Par?metres:
        - tile_info (Dict): Valor del parametre 'tile_info'.

        Retorna:
        - Tuple[Optional[np.ndarray], Optional[Dict]]: Valor retornat pel metode.
        """
        path = tile_info["path"]
        with self._lock:
            if path in self.cache:
                self.cache_hits += 1
                self.cache.move_to_end(path)
                return self.cache[path]
            self.cache_misses += 1

        header = tile_info["header"]
        filename = os.path.basename(path)
        base_name, ext = os.path.splitext(filename)

        # 1. Try packaged cache (TerraLab/data/terrain_cache)
        # Use deterministic path computation without runtime mkdir side-effects.
        packaged_npy = str(self._packaged_cache_root / f"{base_name}.npy")

        # 2. Try the library-owned materialized cache.  An already-existing
        # adjacent cache may be consumed even for an external source, but new
        # conversions are only published in the selected library.
        adjacent_npy = os.path.splitext(path)[0] + ".npy"
        materialized_npy = str(tile_info.get("materialized_path", "") or "")

        candidate_npy = None
        if header.get("NPY") and os.path.exists(path):
            candidate_npy = path
        elif materialized_npy and os.path.exists(materialized_npy):
            candidate_npy = materialized_npy
        elif os.path.exists(packaged_npy):
            candidate_npy = packaged_npy
        elif os.path.exists(adjacent_npy):
            candidate_npy = adjacent_npy

        if candidate_npy:
            try:
                with self._io_lock_for(candidate_npy):
                    # Stability-first on Windows/Python 3.13:
                    # avoid memmap-backed arrays loaded from worker threads.
                    data = np.load(
                        candidate_npy,
                        mmap_mode=None,
                        allow_pickle=False,
                    )
                    # Ensure independent in-memory buffer (no file-backed view).
                    data = np.asarray(data, dtype=np.float32)
                self.bytes_read += int(data.nbytes)
                self._store(path, (data, header))
                # print(f"[HorizonEngine] Loaded cached npy: {os.path.basename(candidate_npy)}")
                return data, header
            except Exception as e:
                print(
                    f"[HorizonEngine] Failed to load cached npy {candidate_npy}: {e}"
                )
                pass

        # Parse text/asc grid
        try:
            # Determine header size
            header_lines = 0
            with open(path, "r") as f:
                for _ in range(10):
                    line = f.readline()
                    if not line:
                        break
                    parts = line.split()
                    if not parts:
                        continue
                    if parts[0].upper() in [
                        "NCOLS",
                        "NROWS",
                        "XLLCORNER",
                        "YLLCORNER",
                        "XLLCENTER",
                        "YLLCENTER",
                        "CELLSIZE",
                        "NODATA_VALUE",
                        "DX",
                        "DY",
                    ]:
                        header_lines += 1
                    else:
                        break

            output_npy = materialized_npy or (
                adjacent_npy
                if bool(tile_info.get("allow_adjacent_cache", False))
                else ""
            )

            print(
                f"[HorizonEngine] Parsing with Pandas: {os.path.basename(path)}..."
            )
            try:
                with self._io_lock_for(path):
                    import pandas as pd

                    df = pd.read_csv(
                        path,
                        skiprows=header_lines,
                        sep=r"\s+",
                        header=None,
                        dtype=np.float32,
                        engine="c",
                    )
                    data_raw = df.values.flatten()
            except Exception as e:
                print(f"[HorizonEngine] Error parsing {path} with Pandas: {e}")
                return None, None

            nodata = header.get("NODATA_VALUE", -9999)
            nrows = int(header["NROWS"])
            ncols = int(header["NCOLS"])

            expected = nrows * ncols
            if data_raw.size != expected:
                if data_raw.size > expected:
                    data_raw = data_raw[:expected]
                else:
                    data_raw = np.pad(
                        data_raw,
                        (0, expected - data_raw.size),
                        constant_values=nodata,
                    )

            data = data_raw.reshape((nrows, ncols)).astype(np.float32)

            # Save binary cache to original location if possible
            try:
                if not output_npy:
                    raise OSError("No writable materialization target")
                os.makedirs(os.path.dirname(output_npy), exist_ok=True)
                with self._io_lock_for(output_npy):
                    temp_output_npy = f"{output_npy}.tmp.npy"
                    np.save(temp_output_npy, data)
                    os.replace(temp_output_npy, output_npy)
                sidecar = str(
                    tile_info.get("materialized_metadata_path", "") or ""
                )
                if sidecar:
                    import json

                    with open(sidecar, "w", encoding="utf-8") as handle:
                        json.dump(
                            {
                                "source": path,
                                "cache": output_npy,
                                "header": header,
                            },
                            handle,
                            ensure_ascii=False,
                            indent=2,
                        )
                # print(f"[HorizonEngine] Saved cache: {os.path.basename(output_npy)}")
            except Exception:
                log_suppressed_exception(__name__, "TileCache.load")

            self.bytes_read += int(data.nbytes)
            self._store(path, (data, header))

            return data, header

        except Exception as e:
            print(f"[HorizonEngine] Error loading tile {path}: {e}")
            import traceback

            traceback.print_exc()
            return None, None


# ─────────────────────────────────────────────
#  DEM Sampler
# ─────────────────────────────────────────────


class DemSampler:
    """Samples elevation from DEM tiles with bilinear interpolation."""

    def __init__(self, tile_index: TileIndex, tile_cache: TileCache):
        self.index = tile_index
        self.cache = tile_cache
        self.last_tile: Optional[Dict] = None
        self._transformer_inv = None  # Lazy loaded if needed

    def get_elevation(self, x: float, y: float) -> Optional[float]:
        """Alias for sample() to maintain compatibility with HorizonBaker."""
        return self.sample(x, y)

    def transform_coordinates_inverse(
        self, x: float, y: float
    ) -> Tuple[float, float]:
        """
        Convert terrain internal coordinates back to geographic lat/lon.

        Input CRS:
            - `x`, `y` in terrain internal CRS (`EPSG:25831`).
        Output CRS:
            - `(lat, lon)` in `EPSG:4326`.
        """
        from TerraLab.terrain.crs import DEFAULT_TRANSFORM_SERVICE

        lon, lat = DEFAULT_TRANSFORM_SERVICE.transform_xy(
            x,
            y,
            CRS_TERRAIN_INTERNAL,
            CRS_GEOGRAPHIC,
        )
        return lat, lon

    def sample(self, x: float, y: float) -> Optional[float]:
        # Spatial coherence optimisation: check last tile first
        """Executa el metode sample de la classe DemSampler.

        Par?metres:
        - x (float): Valor del parametre 'x'.
        - y (float): Valor del parametre 'y'.

        Retorna:
        - Optional[float]: Valor retornat pel metode.
        """
        tile = None
        # Optimization: Track if we are in a "void" area to avoid searching the index
        if self.last_tile == "NONE":
            # We need to know when we exit the void.
            # For now, let's just re-find if it's not the last state.
            pass

        if self.last_tile and self.last_tile != "NONE":
            xmin, ymin, xmax, ymax = self.last_tile["bbox"]
            if xmin <= x < xmax and ymin <= y < ymax:
                tile = self.last_tile

        if tile is None:
            tile = self.index.find_tile(x, y)
            self.last_tile = tile if tile else "NONE"

        if tile is None or tile == "NONE":
            return None

        # Robust unpacking
        res = self.cache.load(tile)
        if res is None:
            # Should not happen if load returns (None, None)
            # print(f"[HorizonEngine] CRITICAL: load returned None for {tile['path']}")
            return None

        data, h = res

        if data is None:
            return None

        # Handle NPY tiles where LL mapping might be different or missing
        if h.get("NPY"):
            # For NPY tiles, we assume bbox is already correct from filename
            # and it's a regular grid.
            xmin, ymin, xmax, ymax = tile["bbox"]
            nrows, ncols = data.shape
            sx = (xmax - xmin) / max(1, ncols)
            sy = (ymax - ymin) / max(1, nrows)
            grid_x = (x - xmin) / sx
            # Y in arrays is usually top-to-bottom
            grid_y_from_top = (ymax - y) / sy
            grid_row = grid_y_from_top
        else:
            # Robust header handling: default to 5.0m if missing (standard for ICGC tiles)
            s = h.get("CELLSIZE", 5.0)
            if "XLLCENTER" in h:
                x0 = h["XLLCENTER"]
                y0 = h["YLLCENTER"]
            else:
                x0 = h.get("XLLCORNER", 0.0) + s / 2
                y0 = h.get("YLLCORNER", 0.0) + s / 2

            grid_x = (x - x0) / s
            grid_y_from_bottom = (y - y0) / s
            grid_row = (h.get("NROWS", data.shape[0]) - 1) - grid_y_from_bottom

        c0 = int(math.floor(grid_x))
        r0 = int(math.floor(grid_row))

        nrows, ncols = data.shape
        c0 = max(0, min(c0, ncols - 1))
        r0 = max(0, min(r0, nrows - 1))

        if 0 <= r0 < nrows - 1 and 0 <= c0 < ncols - 1:
            dx = grid_x - c0
            dy = grid_row - r0

            v00 = float(data[r0, c0])
            v01 = float(data[r0, c0 + 1])
            v10 = float(data[r0 + 1, c0])
            v11 = float(data[r0 + 1, c0 + 1])

            top = v00 * (1 - dx) + v01 * dx
            bot = v10 * (1 - dx) + v11 * dx
            val = top * (1 - dy) + bot * dy
            return val
        else:
            return float(data[r0, c0])
