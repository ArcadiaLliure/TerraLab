import rasterio.warp
from rasterio.crs import CRS

src_crs = CRS.from_string("EPSG:32631") 
dst_crs = CRS.from_string("EPSG:4326")  

utm_x, utm_y = 479417, 4647571 
utms_x = [utm_x, utm_x + 1000]
utms_y = [utm_y, utm_y]

xs, ys = rasterio.warp.transform(src_crs, dst_crs, utms_x, utms_y)
print(f"Moving East 1km: dX={xs[1]-xs[0]:.6f}, dY={ys[1]-ys[0]:.6f}")

utms_x = [utm_x, utm_x]
utms_y = [utm_y, utm_y + 1000]
xs, ys = rasterio.warp.transform(src_crs, dst_crs, utms_x, utms_y)
print(f"Moving North 1km: dX={xs[1]-xs[0]:.6f}, dY={ys[1]-ys[0]:.6f}")
