"""
dvnl_convolve.py

CLI utility to perform radial kernel convolution on DVNL rasters.
Uses a tiled overlap-add approach for memory efficiency on large rasters.
"""

import os
import sys
import argparse
import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.signal import fftconvolve
from tqdm import tqdm

from TerraLab.light_pollution.kernels import create_gaussian_kernel, create_power_law_kernel

def main():
    parser = argparse.ArgumentParser(description="Convolve DVNL raster with a dispersion kernel.")
    parser.add_argument("input", help="Preprocessed DVNL .tif (float32, nan for nodata)")
    parser.add_argument("output", help="Output aggregated .tif")
    parser.add_argument("--kernel", choices=["gaussian", "power-law"], default="gaussian")
    parser.add_argument("--sigma", type=float, default=30.0, help="Sigma for Gaussian (km)")
    parser.add_argument("--rmax", type=float, default=200.0, help="Cutoff radius (km)")
    parser.add_argument("--res", type=float, default=1.0, help="Resolution (km/pixel)")
    
    args = parser.parse_args()

    if args.kernel == "gaussian":
        kernel = create_gaussian_kernel(args.sigma, args.rmax, args.res)
    else:
        # Default power-law parameters
        kernel = create_power_law_kernel(p=2.0, r0_km=1.0, lambda_km=50.0, 
                                        max_radius_km=args.rmax, res_km=args.res)

    halo = kernel.shape[0] // 2
    
    with rasterio.open(args.input) as src:
        meta = src.meta.copy()
        meta.update(compress='lzw', tiled=True)
        
        with rasterio.open(args.output, 'w', **meta) as dst:
            # Process in tiles with halo overlap
            tile_size = 1024
            
            # Simple window generator for the entire raster
            for i in tqdm(range(0, src.height, tile_size)):
                for j in range(0, src.width, tile_size):
                    # Define current tile (writing area)
                    w = min(tile_size, src.width - j)
                    h = min(tile_size, src.height - i)
                    write_window = Window(j, i, w, h)
                    
                    # Define reading area (including halo)
                    read_window = Window(
                        max(0, j - halo),
                        max(0, i - halo),
                        min(src.width, j + w + halo) - max(0, j - halo),
                        min(src.height, i + h + halo) - max(0, i - halo)
                    )
                    
                    data = src.read(1, window=read_window)
                    # Use 0 for NaNs during convolution to stay within spatial support
                    data_filled = np.nan_to_num(data, nan=0.0)
                    
                    # Convolve
                    res = fftconvolve(data_filled, kernel, mode='same')
                    
                    # Crop result to match the write_window exactly
                    # Need to handle edge offsets
                    start_y = i - read_window.row_off
                    start_x = j - read_window.col_off
                    
                    cropped = res[start_y:start_y+h, start_x:start_x+w]
                    dst.write(cropped.astype(np.float32), 1, window=write_window)

    print("Convolution complete.")

if __name__ == "__main__":
    main()
