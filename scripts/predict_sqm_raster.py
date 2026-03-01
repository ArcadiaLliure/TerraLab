"""
predict_sqm_raster.py

CLI utility to apply a calibrated model to a convolved DVNL raster 
and produce a zenith SQM map.
"""

import argparse
import rasterio
import numpy as np
from tqdm import tqdm
from TerraLab.light_pollution.calibration import SQMCalibrationModel

def main():
    parser = argparse.ArgumentParser(description="Predict SQM map from aggregated DVNL.")
    parser.add_argument("input_agg", help="Convolved aggregated DVNL raster (.tif)")
    parser.add_argument("model", help="Path to calibrated .joblib model")
    parser.add_argument("output", help="Output SQM raster (.tif)")
    parser.add_argument("--elevation", type=float, default=0.0, help="Default elevation if no DEM provided")
    
    args = parser.parse_args()

    model = SQMCalibrationModel().load(args.model)
    
    with rasterio.open(args.input_agg) as src:
        meta = src.meta.copy()
        meta.update(dtype='float32', nodata=np.nan, compress='lzw')
        
        with rasterio.open(args.output, 'w', **meta) as dst:
            for stdout, window in tqdm(src.block_windows(1)):
                agg = src.read(1, window=window).astype(np.float32)
                
                # We assume a flat elevation for this batch tool unless we integrate DEM here
                # Predict SQM
                sqm = model.predict(agg, args.elevation)
                
                # Mask where input was nan
                sqm[np.isnan(agg)] = np.nan
                
                dst.write(sqm.astype(np.float32), 1, window=window)

    print(f"SQM prediction complete: {args.output}")

if __name__ == "__main__":
    main()
