"""
dvnl_preprocess.py

CLI utility to validate and clean DVNL GeoTIFF files, ensuring NoData 
is correctly masked and metadata is preserved.
"""

import os
import sys
import argparse
import rasterio
import numpy as np
from tqdm import tqdm

def main():
    parser = argparse.ArgumentParser(description="Preprocess DVNL GeoTIFF for TerraLab.")
    parser.add_argument("input", help="Path to input DVNL .tif")
    parser.add_argument("output", help="Path to output cleaned .tif")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: Input file {args.input} does not exist.")
        sys.exit(1)

    print(f"Preprocessing {args.input} -> {args.output}")
    
    with rasterio.open(args.input) as src:
        meta = src.meta.copy()
        # We ensure it is float32 for downstream processing
        meta.update(dtype='float32', nodata=np.nan, compress='lzw')
        
        with rasterio.open(args.output, 'w', **meta) as dst:
            # We process in blocks to save memory
            for stdout, window in tqdm(src.block_windows(1)):
                data = src.read(1, window=window).astype(np.float32)
                
                # Handle NoData
                if src.nodata is not None:
                    data[data == src.nodata] = np.nan
                
                # Handle outlier large values typical in some DVNL versions
                data[data > 1e10] = np.nan
                
                dst.write(data, 1, window=window)

    print("Preprocess complete.")

if __name__ == "__main__":
    main()
