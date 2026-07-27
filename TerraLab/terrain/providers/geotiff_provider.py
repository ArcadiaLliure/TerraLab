"""GeoTIFF terrain elevation providers."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from TerraLab.common.locks import RASTERIO_LOCK
from TerraLab.common.performance.budget import DEFAULT_PERFORMANCE_BUDGET
from TerraLab.terrain.crs import (
    DEFAULT_TRANSFORM_SERVICE,
    CoordinateTransformService,
    normalize_crs,
    transformer_transform,
)
from TerraLab.terrain.providers.common import (
    CRS_TERRAIN_INTERNAL,
    ElevationBatch,
    RasterMetadata,
    RasterProvider,
    _path_fingerprint,
)
from TerraLab.terrain.providers.raster_dataset import _GeoRasterDataset

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
        self._tr_internal_to_native = DEFAULT_TRANSFORM_SERVICE.transformer(
            CRS_TERRAIN_INTERNAL, self.dest_crs_str
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
            self.native_cx, self.native_cy = transformer_transform(
                self._tr_internal_to_native, cx, cy
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
