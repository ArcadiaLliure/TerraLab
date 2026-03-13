import abc
from typing import Optional, List, Dict, Tuple
import os
import math
import numpy as np
from TerraLab.common.locks import RASTERIO_LOCK

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
        
    def prepare_region(self, cx: float, cy: float, radius: float, progress_callback=None):
        """
        Optional: pre-load or pre-cache data for a region before heavy sampling.
        """
        pass

    def get_native_crs(self) -> str:
        """Return the EPSG code or PROJ string of the native data, defaulting to EPSG:25831."""
        return "EPSG:25831"
        
    def transform_coordinates(self, lat: float, lon: float) -> Tuple[float, float]:
        """Transform lat/lon WGS84 to the provider's native CRS."""
        # By default, use the old math for UTM 31N to keep exact legacy behavior
        a = 6378137.0
        f = 1 / 298.257223563
        k0 = 0.9996
        lon0 = 3.0 # Central meridian for Zone 31
        
        phi = math.radians(lat)
        lam = math.radians(lon)
        lam0 = math.radians(lon0)
        
        e2 = 2*f - f*f
        ep2 = e2 / (1 - e2)
        
        N = a / math.sqrt(1 - e2 * math.sin(phi)**2)
        T = math.tan(phi)**2
        C = ep2 * math.cos(phi)**2
        A = (lam - lam0) * math.cos(phi)
        
        M = a * ((1 - e2/4 - 3*e2**2/64 - 5*e2**3/256) * phi -
                 (3*e2/8 + 3*e2**2/32 + 45*e2**3/1024) * math.sin(2*phi) +
                 (15*e2**2/256 + 45*e2**3/1024) * math.sin(4*phi) -
                 (35*e2**3/3072) * math.sin(6*phi))
                 
        x = 500000 + k0 * N * (A + (1-T+C)*A**3/6 + (5-18*T+T**2+72*C-58*ep2)*A**5/120)
        y = k0 * (M + N * math.tan(phi) * (A**2/2 + (5-T+9*C+4*C**2)*A**4/24 + (61-58*T+T**2+600*C-330*ep2)*A**6/720))
        
        return x, y

    def transform_coordinates_inverse(self, x: float, y: float) -> Tuple[float, float]:
        """Transforms distance meters back to WGS84 Lat/Lon using rasterio.warp."""
        try:
            from rasterio.warp import transform
            # Assuming native is EPSG:25831 (UTM 31N)
            xs, ys = transform("EPSG:25831", "EPSG:4326", [x], [y])
            lat, lon = ys[0], xs[0]
            
            # Additional sanity check for valid Lat/Lon ranges
            if math.isnan(lat) or math.isnan(lon) or abs(lat) > 90 or abs(lon) > 180:
                return 0.0, 0.0
                
            return lat, lon
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
        from TerraLab.terrain.engine import TileIndex, TileCache, DemSampler
        self.index = TileIndex(self.tiles_dir, callback=lambda curr, tot, msg: progress_callback(curr/tot*100, msg) if progress_callback else None)
        self.cache = TileCache(capacity=500)
        self.sampler = DemSampler(self.index, self.cache)
        return True
        
    def prepare_region(self, cx: float, cy: float, radius: float, progress_callback=None, abort_check=None):
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
            futures = {executor.submit(_load_tile, tile): tile for tile in tiles_needed}
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
                        msg = getTraduction("Horizon.LoadingMaps", "Loading maps: {loaded}/{total} ({pct}%)").format(
                            loaded=loaded_count, total=total_tiles, pct=percent
                        )
                        progress_callback(percent, msg)
                try:
                    future.result()
                except Exception as e:
                    print(f"[AscRasterProvider] Tile load error: {e}")

    def get_elevation(self, x: float, y: float) -> Optional[float]:
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
        
    def initialize(self, progress_callback=None):
        import rasterio
        
        if not os.path.exists(self.tiff_path):
            raise FileNotFoundError(f"GeoTIFF not found: {self.tiff_path}")
            
        print(f"[TiffRasterWindowProvider] Opening dataset: {self.tiff_path}")
        with RASTERIO_LOCK:
            self.dataset = rasterio.open(self.tiff_path)
        print(f"[TiffRasterWindowProvider] CRS: {self.dataset.crs}, Bounds: {self.dataset.bounds}")
        
        # Setup PyProj transformer to convert UTM Zone 31N (EPSG:32631) -> Native CRS of GeoTIFF
        # This handles the metric Cartesian translation for HorizonBaker smoothly.
        if self.dataset.crs:
            dest_crs = self.dataset.crs.to_string()
            self.is_geo = self.dataset.crs.is_geographic
        else:
            # Default to WGS84 if no CRS is found
            dest_crs = "EPSG:4326"
            self.is_geo = True
            
        self.dest_crs_str = dest_crs
        self.ds_transform = self.dataset.transform
        self.ds_width = self.dataset.width
        self.ds_height = self.dataset.height
        
        return True
        
    def get_native_crs(self) -> str:
        if hasattr(self, 'dest_crs_str') and self.dest_crs_str:
            return self.dest_crs_str
        return super().get_native_crs()
        
    # Removed transform_coordinates override. We EXPLICITLY WANT to use the base 
    # class implementation, which converts Lat/Lon to UTM Zone 31N meters.
    # The HorizonBaker operates strictly in meters, so cx and cy must be in meters.
        
    def prepare_region(self, cx: float, cy: float, radius: float, progress_callback=None):
        """
        Loads the window covering [cx-radius, cy-radius, cx+radius, cy+radius] into RAM.
        cx, cy, and radius are assumed to be in UTM 31N meters.
        """
        from rasterio.windows import from_bounds, Window
        
        if not self.dataset:
            return
            
        if progress_callback:
            progress_callback(10, "⏳ Preparando ventana de memoria ráster...")
            
        # Transform UTM coordinates to Native CRS center
        try:
            from rasterio.warp import transform
            xs, ys = transform("EPSG:32631", self.dest_crs_str, [cx], [cy])
            self.native_cx = xs[0]
            self.native_cy = ys[0]
        except Exception as e:
            print(f"[TiffRasterWindowProvider] Transform Error in prepare_region: {e}")
            import traceback
            traceback.print_exc()
            self.cached_data = None
            return
            
        self.center_x_utm = cx
        self.center_y_utm = cy
        
        # Local affine scaling (Flat Earth approximation) -> Distances from meters back to degrees
        if self.is_geo:
            self.m_per_native_y = 111320.0
            self.m_per_native_x = 111320.0 * math.cos(math.radians(self.native_cy))
        else:
            self.m_per_native_x = 1.0
            self.m_per_native_y = 1.0
            
        radius_native_x = radius / self.m_per_native_x
        radius_native_y = radius / self.m_per_native_y
        
        print(f"[TiffRasterWindowProvider] Caching window for Native ({self.native_cx:.4f}, {self.native_cy:.4f}) rad_xy=({radius_native_x:.3f}, {radius_native_y:.3f})...")
        
        # Calculate bounding box
        left = self.native_cx - radius_native_x
        bottom = self.native_cy - radius_native_y
        right = self.native_cx + radius_native_x
        top = self.native_cy + radius_native_y
        
        # Compute the rasterio window covering this bounding box
        window = from_bounds(left, bottom, right, top, transform=self.ds_transform)
        
        # Ensure window is within dataset bounds, round to integers
        dataset_window = Window(col_off=0, row_off=0, width=self.ds_width, height=self.ds_height)
        window = window.intersection(dataset_window)
        window = window.round_lengths().round_offsets()
        
        if window.width <= 0 or window.height <= 0:
             print("[TiffRasterWindowProvider] Bounding box outside of GeoTIFF bounds!")
             self.cached_data = None
             return
             
        # Read the data into memory (Band 1)
        if progress_callback:
            progress_callback(50, "⏳ Leyendo porción del GeoTIFF a memoria RAM...")
            
        with RASTERIO_LOCK:
            self.cached_data = self.dataset.read(1, window=window)
        self.cached_transform = self.dataset.window_transform(window)
        
        # Cache the inverse affine transform for fast (x,y) -> (r,c) lookups
        self.cached_inverse_transform = ~self.cached_transform
        
        print(f"[TiffRasterWindowProvider] Loaded {self.cached_data.shape} float array into memory.")
        if progress_callback:
             progress_callback(100, "✅ Porción de datos lista.")

    def get_elevation(self, x: float, y: float) -> Optional[float]:
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
        if self.dataset:
            with RASTERIO_LOCK:
                self.dataset.close()
            self.dataset = None
            self.cached_data = None

