
import os
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import from_bounds

def get_bortle(lat, lon, tiff_path):
    print(f"Checking {lat}, {lon} in {tiff_path}")
    if not os.path.exists(tiff_path):
        print("Tiff not found!")
        return
    
    with rasterio.open(tiff_path) as src:
        print(f"CRS: {src.crs}")
        print(f"Transform: {src.transform}")
        trans = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x_cen, y_cen = trans.transform(lon, lat)
        print(f"Transformed coords: {x_cen}, {y_cen}")
        
        r_m = 5000.0
        if src.crs.is_geographic:
            r_proj = r_m / 111320.0
        else:
            r_proj = r_m
        
        print(f"r_proj: {r_proj}")
        window = from_bounds(x_cen - r_proj, y_cen - r_proj, x_cen + r_proj, y_cen + r_proj, transform=src.transform)
        print(f"Window: {window}")
        
        arr = src.read(1, window=window)
        print(f"Array shape: {arr.shape}")
        if arr.size > 0:
            print(f"Mean radiance: {np.nanmean(arr)}")
            val = np.nanmean(arr)
            sqm = 22.0 - 2.4 * np.log10(val + 0.001)
            print(f"Resulting SQM: {sqm}")
        else:
            print("Empty array!")

tiff = r"e:\Desarrollo\TerraLab\TerraLab\data\light_pollution\C_DVNL 2022.tif"
get_bortle(41.189795, 1.210058, tiff)
