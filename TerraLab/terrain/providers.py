import abc
import math
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
from pyproj import Transformer

from TerraLab.common.locks import RASTERIO_LOCK

CRS_GEOGRAPHIC = "EPSG:4326"
CRS_TERRAIN_INTERNAL = "EPSG:25831"


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
            os.path.join(src, name)
            for name in os.listdir(src)
            if str(name).lower().endswith((".tif", ".tiff"))
        ],
        key=lambda path: os.path.basename(path).lower(),
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
        provider = TiffRasterWindowProvider(tiff_path)
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


class AscRasterProvider(RasterProvider):
    """
    Legacy implementation: loads ESRI ASCII / NPY tiled directories dynamically
    using the internal TileIndex, TileCache, and DemSampler.
    """

    def __init__(self, tiles_dir: str):
        self.tiles_dir = tiles_dir
        self.index = None
        self.cache = None
        self.sampler = None

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
        return True

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

        from TerraLab.common.utils import getTraduction

        if not self.index:
            return

        tiles_needed = self.index.get_overlapping_tiles(cx, cy, radius)
        total_tiles = len(tiles_needed)
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
        return self.sampler.sample(x, y)


class TiffRasterWindowProvider(RasterProvider):
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
