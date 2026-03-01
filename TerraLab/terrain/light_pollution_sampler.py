"""
light_pollution_sampler.py

Connects the DVNL GeoTIFF data to TerraLab's engine. Automatically extracts 
radiance data around a coordinate, convolves it with a radial kernel, and 
evaluates the SQM and Bortle class.
"""

import os
import numpy as np
import rasterio
from rasterio.windows import from_bounds
import threading
import math

from TerraLab.light_pollution.dvnl_io import read_raster_window_filtered
from TerraLab.light_pollution.kernels import create_gaussian_kernel
from TerraLab.light_pollution.bortle import sqm_to_bortle_class

class LightPollutionSampler:
    """
    Rapid sampler for the UI background worker to get local Bortle/SQM estimates 
    from the given DVNL raster.
    """
    def __init__(self, raster_path: str = None, radius_km: float = 5.0, res_km: float = 0.5):
        self.raster_path = raster_path
        self.radius_km = radius_km
        self.res_km = res_km
        
        # Cache for the current region being baked
        self._cached_data = None
        self._cached_transform = None
        self._cached_bounds = None # (minx, miny, maxx, maxy) in native CRS
        
        # Thread safety lock for cached data and transformers
        self._lock = threading.Lock()
        
        # Zenith kernel: much smaller than the propagation kernel.
        self.kernel = create_gaussian_kernel(sigma_km=1.5, max_radius_km=radius_km, res_km=res_km)
        
        self._transformer = None
        self._src_crs = None
        self._utm_transformer = None
        self._is_geographic = False
        
    def estimate_zenith_sqm(self, lat: float, lon: float) -> tuple[float, int]:
        """
        Estimates the zenith SQM and Bortle class for the given coordinates.
        Uses a quick window extraction from the DVNL file.
        """
        if not self.raster_path or not os.path.exists(self.raster_path):
            return 18.0, 8 # Brighter default if missing file

        try:
            # 1. Try Cache First
            with self._lock:
                if (self._cached_data is not None and 
                    self._transformer is not None and 
                    self._cached_bounds is not None):
                    
                    x, y = self._transformer.transform(lon, lat)
                    b = self._cached_bounds
                    # Check if point is within cached ROI (with a small safety margin)
                    if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                        inv = ~self._cached_transform
                        c_off, r_off = inv * (x, y)
                        
                        # Search radius in pixels
                        px_size = abs(self._cached_transform.a)
                        if self._is_geographic:
                            r_units = self.radius_km / 111.32 # precise deg
                        else:
                            r_units = self.radius_km * 1000.0
                            
                        r_px = int(r_units / px_size)
                        
                        r0 = int(r_off - r_px); r1 = int(r_off + r_px + 1)
                        c0 = int(c_off - r_px); c1 = int(c_off + r_px + 1)
                        
                        # Clip to array
                        h, w = self._cached_data.shape
                        r_start = max(0, r0); r_end = min(h, r1)
                        c_start = max(0, c0); c_end = min(w, c1)
                        
                        if r_end > r_start and c_end > c_start:
                            arr = self._cached_data[r_start:r_end, c_start:c_end].copy()
                            sqm, bortle = self._process_array_to_sqm(arr)
                            # print(f"[LPSampler] Hit: {lat:.3f},{lon:.3f} -> SQM {sqm:.1f} (B{bortle})")
                            return sqm, bortle

            # 2. Fallback: Open file directly
            with rasterio.open(self.raster_path) as src:
                from pyproj import Transformer
                trans = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
                x_cen, y_cen = trans.transform(lon, lat)
                
                if src.crs.is_geographic:
                    r_proj = self.radius_km / 111.32
                else:
                    r_proj = self.radius_km * 1000.0
                    
                window = from_bounds(x_cen - r_proj, y_cen - r_proj, x_cen + r_proj, y_cen + r_proj, transform=src.transform)
                arr = src.read(1, window=window).astype(np.float32)
                sqm, bortle = self._process_array_to_sqm(arr)
                # print(f"[LPSampler] Direct Read: SQM {sqm:.1f} (B{bortle})")
                return sqm, bortle
                
        except Exception as e:
            # If everything fails, don't return 4 (Suburban), return something that looks like the real area
            # Or just return 18.0 (Bortle 8).
            return 18.0, 8

    def _process_array_to_sqm(self, arr: np.ndarray) -> tuple[float, int]:
        try:
            if arr.size == 0:
                return 21.0, 4
                
            # Filter invalid values
            arr_clean = arr.copy()
            arr_clean[arr_clean < 0] = np.nan
            arr_clean[arr_clean > 1e6] = np.nan
            
            # Center of the array (geographic center of the request)
            ah, aw = arr_clean.shape
            cy, cx = ah // 2, aw // 2
            
            kh, kw = self.kernel.shape
            rk = kh // 2
            ck = kw // 2
            
            # Slice bounds in array
            y0 = cy - rk; y1 = cy + rk + 1
            x0 = cx - ck; x1 = cx + ck + 1
            
            # Intersection with array bounds
            ay0 = max(0, y0); ay1 = min(ah, y1)
            ax0 = max(0, x0); ax1 = min(aw, x1)
            
            # Matching slice in kernel
            ky0 = ay0 - y0; ky1 = ky0 + (ay1 - ay0)
            kx0 = ax0 - x0; kx1 = kx0 + (ax1 - ax0)
            
            arr_crop = arr_clean[ay0:ay1, ax0:ax1]
            kernel_crop = self.kernel[ky0:ky1, kx0:kx1]
            
            valid_mask = ~np.isnan(arr_crop)
            if not np.any(valid_mask):
                return 21.0, 4
                
            # Normalize crop
            k_sum = np.nansum(kernel_crop[valid_mask])
            if k_sum < 1e-6:
                 agg_val = np.nanmean(arr_crop)
            else:
                 weighted_sum = np.nansum(arr_crop[valid_mask] * kernel_crop[valid_mask])
                 agg_val = weighted_sum / k_sum
                 
            val = max(float(agg_val), 1e-5) 
            # SQM formula: Standard is 22 - 2.5 * log10(radiance)
            # We use 2.5 to better match terrestrial measurements in the area.
            sqm = 22.0 - 2.5 * np.log10(val + 0.001)
            sqm = np.clip(sqm, 16.0, 22.0)
            
            # print(f"[LPSampler] _process_array_to_sqm: val={val:.4f} -> SQM {sqm:.2f}")
            return float(sqm), sqm_to_bortle_class(sqm)
        except Exception as e:
            print(f"[LPSampler] Internal Error: {e}")
            return 21.0, 4

    def prepare_region(self, lat: float, lon: float, radius_km: float, input_crs: str = "EPSG:25831"):
        """Pre-loads ROI from the DVNL raster into memory."""
        if not self.raster_path or not os.path.exists(self.raster_path):
            return

        try:
            print(f"[LPSampler] Preparing region for {lat:.4f}, {lon:.4f} (r={radius_km}km)...")
            with rasterio.open(self.raster_path) as src:
                from pyproj import Transformer
                self._src_crs = src.crs
                self._is_geographic = src.crs.is_geographic
                
                # Atomically update transformers
                new_trans = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
                new_utm_trans = Transformer.from_crs(input_crs, src.crs, always_xy=True)
                
                x_cen, y_cen = new_trans.transform(lon, lat)
                
                # Correct unit handling: if geographic, convert meters to degrees
                r_m = radius_km * 1000.0 * 2.0
                if self._is_geographic:
                    r_proj = r_m / 111320.0 # Approx meters per degree at equator
                else:
                    r_proj = r_m
                    
                bounds = (x_cen - r_proj, y_cen - r_proj, x_cen + r_proj, y_cen + r_proj)
                window = from_bounds(*bounds, transform=src.transform)
                
                print(f"[LPSampler] Reading window {window} (Geo={self._is_geographic})...")
                data = src.read(1, window=window).astype(np.float32)
                trans_win = src.window_transform(window)
                data[data < 0] = 0.0
                data[data > 1e10] = 0.0
                
                with self._lock:
                    self._transformer = new_trans
                    self._utm_transformer = new_utm_trans
                    self._cached_data = data
                    self._cached_transform = trans_win
                    self._cached_bounds = bounds
                    
            print(f"[LPSampler] ROI Cached: {self._cached_data.shape} px.")
        except Exception as e:
            print(f"[LPSampler] Cache Error: {e}")
            import traceback
            traceback.print_exc()
            with self._lock:
                self._cached_data = None

    def get_radiance(self, lat: float, lon: float) -> float:
        """Fast lookup from the pre-loaded ROI."""
        try:
            with self._lock:
                if self._cached_data is None or self._transformer is None or self._cached_bounds is None:
                    return 0.0
                
                x, y = self._transformer.transform(lon, lat)
                b = self._cached_bounds
                if not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
                    # print(f"[LPSampler] Out of bounds: {x},{y} vs {b}")
                    return 0.0

                inv = ~self._cached_transform
                col, row = inv * (x, y)
                r, c = int(row), int(col)
                if 0 <= r < self._cached_data.shape[0] and 0 <= c < self._cached_data.shape[1]:
                    return float(self._cached_data[r, c])
        except Exception as e:
            pass
        return 0.0

    def get_radiance_utm(self, x_utm: float, y_utm: float) -> float:
        """Fast lookup from UTM coordinates."""
        try:
            with self._lock:
                if self._cached_data is None or self._utm_transformer is None or self._cached_bounds is None:
                    return 0.0
                
                x, y = self._utm_transformer.transform(x_utm, y_utm)
                b = self._cached_bounds
                if not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
                    return 0.0

                inv = ~self._cached_transform
                col, row = inv * (x, y)
                r, c = int(row), int(col)
                if 0 <= r < self._cached_data.shape[0] and 0 <= c < self._cached_data.shape[1]:
                    return float(self._cached_data[r, c])
        except Exception as e:
            pass
        return 0.0
