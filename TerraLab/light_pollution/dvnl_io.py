"""
dvnl_io.py

Provides I/O utilities for DVNL (Defense Meteorological Satellite Program / 
VIIRS Nighttime Lights) GeoTIFF files.
"""

import rasterio
import numpy as np
from rasterio.windows import Window

def read_raster_metadata(path: str) -> dict:
    """
    Reads the basic metadata for a raster file without loading its array.
    """
    with rasterio.open(path) as src:
        return src.meta.copy()

def read_raster_window_filtered(path: str, window: Window) -> np.ndarray:
    """
    Reads a specific window from the given raster path and handles NoData
    by replacing it with np.nan.
    
    Args:
        path (str): The file path to the GeoTIFF.
        window (rasterio.windows.Window): The window to extract.
        
    Returns:
        np.ndarray: The array containing the data as float32, with np.nan for NoData.
    """
    with rasterio.open(path) as src:
        arr = src.read(1, window=window).astype(np.float32)
        nodata = src.nodata
        if nodata is not None:
            arr[arr == nodata] = np.nan
        # Sometimes huge values act as nodata in DVNL even without metadata
        arr[arr > 1e10] = np.nan
        return arr
