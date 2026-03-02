import threading

# Global lock for rasterio.open calls to prevent crashes on Windows 
# when accessing multiple GeoTIFFs concurrently or across threads.
# GDAL/Rasterio's global state can be unstable in some environments.
RASTERIO_LOCK = threading.Lock()
