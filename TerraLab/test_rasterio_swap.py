import rasterio.warp
from rasterio.crs import CRS

src_crs = CRS.from_string("EPSG:32631") 
dst_crs = CRS.from_string("EPSG:4326")  

utm_x, utm_y = 479417, 4647571 

# Test 1: (E, N) -> (Lon, Lat)?
xs1, ys1 = rasterio.warp.transform(src_crs, dst_crs, [utm_x], [utm_y])
print(f"UTM 31N ({utm_x}, {utm_y}) -> X'={xs1[0]:.6f}, Y'={ys1[0]:.6f}")

# Test 2: (Lon, Lat)? -> (E, N)
xs2, ys2 = rasterio.warp.transform(dst_crs, src_crs, [2.751544], [41.988876])
print(f"GCS (2.75, 41.99) -> X'={xs2[0]:.6f}, Y'={ys2[0]:.6f}")

# Test 3: (Lat, Lon)? -> (E, N)
xs3, ys3 = rasterio.warp.transform(dst_crs, src_crs, [41.988876], [2.751544])
print(f"GCS (41.99, 2.75) -> X'={xs3[0]:.6f}, Y'={ys3[0]:.6f}")
