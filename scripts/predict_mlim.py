"""
predict_mlim.py

CLI utility to convert an SQM map into a Limiting Magnitude map 
for a given altitude and extinction coefficient.
"""

import argparse
import rasterio
import numpy as np
from tqdm import tqdm
from TerraLab.light_pollution.mlim import calculate_mlim_from_sqm

def main():
    parser = argparse.ArgumentParser(description="Convert SQM map to m_lim map.")
    parser.add_argument("sqm_in", help="Input SQM raster (.tif)")
    parser.add_argument("mlim_out", help="Output m_lim raster (.tif)")
    parser.add_argument("--alt", type=float, default=90.0, help="Altitude angle (degrees)")
    parser.add_argument("--k", type=float, default=0.25, help="Extinction coefficient")
    
    args = parser.parse_args()

    # Vectorize the calculation
    v_mlim = np.vectorize(lambda s: calculate_mlim_from_sqm(s, args.alt, args.k) if not np.isnan(s) else np.nan)

    with rasterio.open(args.sqm_in) as src:
        meta = src.meta.copy()
        meta.update(dtype='float32', nodata=np.nan, compress='lzw')
        
        with rasterio.open(args.mlim_out, 'w', **meta) as dst:
            for stdout, window in tqdm(src.block_windows(1)):
                sqm = src.read(1, window=window).astype(np.float32)
                
                mlim = v_mlim(sqm)
                dst.write(mlim.astype(np.float32), 1, window=window)

    print(f"Limiting magnitude map complete: {args.mlim_out}")

if __name__ == "__main__":
    main()
