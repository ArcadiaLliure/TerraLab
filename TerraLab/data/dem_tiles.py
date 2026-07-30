"""DEM tile indexing, loading, and sampling infrastructure."""

from __future__ import annotations

import glob
import math
import os
from typing import Dict, List, Optional, Tuple

import numpy as np

from TerraLab.common.exception_reporting import log_suppressed_exception


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
                    basename = os.path.basename(fpath)
                    bbox = self._parse_npy_filename(basename)
                    if bbox:
                        self.tiles.append(
                            {
                                "path": fpath,
                                "header": {
                                    "NPY": True,
                                    "CELLSIZE": 5.0,
                                    "NCOLS": 1,
                                    "NROWS": 1,
                                },
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

                self.global_bbox[0] = min(self.global_bbox[0], bbox[0])
                self.global_bbox[1] = min(self.global_bbox[1], bbox[1])
                self.global_bbox[2] = max(self.global_bbox[2], bbox[2])
                self.global_bbox[3] = max(self.global_bbox[3], bbox[3])

                if callback and i % 50 == 0:
                    callback(i, len(files), f"Indexing tile {i}/{len(files)}")

            except Exception:
                log_suppressed_exception(__name__, "TileIndex._build_index")

        if callback:
            callback(
                len(files), len(files), f"Indexed {len(self.tiles)} tiles."
            )

        self._build_spatial_index()
        print(f"[HorizonEngine] Indexed {len(self.tiles)} valid tiles.")

    def _build_spatial_index(self) -> None:
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
            ix1 = math.floor((xmax - origin_x) / cell_size)
            iy1 = math.floor((ymax - origin_y) / cell_size)
            for iy in range(iy0, iy1 + 1):
                for ix in range(ix0, ix1 + 1):
                    cells.setdefault((ix, iy), []).append(tile_index)
        self._spatial_cells = {
            key: tuple(indices) for key, indices in cells.items()
        }

    @staticmethod
    def _parse_npy_filename(
        name: str,
    ) -> Optional[Tuple[float, float, float, float]]:
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
        if (
            x < self.global_bbox[0]
            or y < self.global_bbox[1]
            or x > self.global_bbox[2]
            or y > self.global_bbox[3]
        ):
            return None

        for t in self.tiles:
            xmin, ymin, xmax, ymax = t["bbox"]
            if xmin <= x < xmax and ymin <= y < ymax:
                return t
        return None


__all__ = ["TileIndex"]
