import os
import time
from typing import Optional
from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from TerraLab.terrain.engine import bake_and_save, HorizonBaker, TileIndex, TileCache, DemSampler, HorizonProfile, DEFAULT_BANDS, generate_bands
from TerraLab.common.utils import getTraduction

class HorizonWorker(QObject):
    """
    Background worker to load DEM tiles and bake horizon profiles 
    without freezing the UI.
    """
    profile_ready = pyqtSignal(object)  # Emits HorizonProfile
    error_occurred = pyqtSignal(str)
    progress_message = pyqtSignal(str)

    def __init__(self, tiles_dir=None, parent=None):
        super().__init__(parent)
        self.tiles_dir = tiles_dir
        self.is_initialized = False
        self.provider = None
        self.observer_offset = 0.0
        self.needs_reload = False
        
        import threading
        self.provider_lock = threading.Lock()

    def set_observer_offset(self, offset: float):
        """Update the observer extra height to use on the next bake."""
        self.observer_offset = offset

    def reload_config(self):
        """Flag the state to force re-initialization on next bake by the worker thread."""
        self.needs_reload = True
        self.tiles_dir = None # Force re-read from ConfigManager

    def initialize(self):
        """Lazy initialization of heavy DEM index."""
        if self.needs_reload:
            if self.provider and hasattr(self.provider, 'close'):
                try:
                    self.provider.close()
                except Exception as e:
                    print(f"[HorizonWorker] Warning: Error closing provider: {e}")
            self.is_initialized = False
            self.provider = None
            self.needs_reload = False
            
        if self.is_initialized:
            return

        # If no explicit path, try config
        if not self.tiles_dir:
             from TerraLab.config import ConfigManager
             self.tiles_dir = ConfigManager().get_raster_path()

        if not self.tiles_dir or not os.path.exists(self.tiles_dir):
            self.error_occurred.emit(f"Tiles directory not configured or found: {self.tiles_dir}")
            return

        try:
            print(f"[HorizonWorker] Initializing RasterProvider from {self.tiles_dir}...")
            t0 = time.time()
            
            def index_callback(percent, msg):
                 self.progress_message.emit(msg)
            
            # Auto-detect provider based on path type or directory contents
            is_tiff = False
            tiff_path = None
            
            if os.path.isfile(self.tiles_dir) and self.tiles_dir.lower().endswith(('.tif', '.tiff')):
                is_tiff = True
                tiff_path = self.tiles_dir
            elif os.path.isdir(self.tiles_dir):
                # Search for GeoTIFFs in the directory
                tifs = [f for f in os.listdir(self.tiles_dir) if f.lower().endswith(('.tif', '.tiff'))]
                if tifs:
                    is_tiff = True
                    # If multiple, just pick the first one for now
                    tiff_path = os.path.join(self.tiles_dir, tifs[0])
                    #TODO: Pick multiple and load them in a single provider (join them)
            
            if is_tiff and tiff_path:
                print(f"[HorizonWorker] Auto-detected GeoTIFF: {tiff_path}")
                from TerraLab.terrain.providers import TiffRasterWindowProvider
                with self.provider_lock:
                    self.provider = TiffRasterWindowProvider(tiff_path)
            else:
                print(f"[HorizonWorker] Using ASC/TXT directory mode")
                from TerraLab.terrain.providers import AscRasterProvider
                with self.provider_lock:
                    self.provider = AscRasterProvider(self.tiles_dir)
                
            with self.provider_lock:
                self.provider.initialize(progress_callback=index_callback)
            
            with self.provider_lock:
                self.baker = HorizonBaker(self.provider)
            self.is_initialized = True
            print(f"[HorizonWorker] Init complete in {time.time()-t0:.2f}s")
        except Exception as e:
            self.error_occurred.emit(f"Init Error: {e}")

    def get_bare_elevation(self, lat: float, lon: float) -> Optional[float]:
        """
        Fast synchronous lookup for the UI. Initializes the provider if needed,
        but does NOT trigger a heavy 150km radius load or baking process.
        Returns the raw DEM altitude for the coordinates, or None.
        """
        if not self.is_initialized:
            # We must not initialize the C-level provider from the main UI thread.
            # Wait for the worker thread to initialize it during a bake request.
            return None
             
        if not self.is_initialized or not self.provider:
            return None
            
        try:
            with self.provider_lock:
                x_utm, y_utm = self.provider.transform_coordinates(lat, lon)
                return self.provider.get_elevation(x_utm, y_utm)
        except Exception as e:
            print(f"[HorizonWorker] get_bare_elevation error: {e}")
            return None

    @pyqtSlot(float, float)
    def request_bake(self, lat: float, lon: float):
        """Slot to trigger a bake for a specific location."""
        if not self.is_initialized:
             self.initialize()
        
        if not self.is_initialized:
            # If still not initialized, it means config is missing or invalid.
            # We can't bake.
            return 


        try:
            print(f"[HorizonWorker] Baking starting for {lat}, {lon}...")
            t0 = time.time()
            
            print(f"[HorizonWorker] Baking starting for {lat}, {lon}...")
            t0 = time.time()
            
            print(f"[HorizonWorker] Transforming {lon}, {lat} to native CRS...", flush=True)
            with self.provider_lock:
                x_utm, y_utm = self.provider.transform_coordinates(lat, lon)
            print(f"[HorizonWorker] UTM Coords: {x_utm}, {y_utm}", flush=True)
            
            # --- Pre-warm Cache with Feedback (Parallel I/O) ---
            vis_radius = 150000.0 # 150km
            print(f"[HorizonWorker] Determining tiles in {vis_radius/1000}km radius...")
            
            def region_progress(pct, msg):
                self.progress_message.emit(msg)
                
            with self.provider_lock:
                self.provider.prepare_region(x_utm, y_utm, vis_radius, progress_callback=region_progress)
            
            self.progress_message.emit(getTraduction("Horizon.CalculatingHorizonGeneric", "⏳ Calculant horitzó..."))
            
            # Sample ground height for this location
            # If valid, use it. If not, fallback.
            print("[HorizonWorker] Sampling ground height...")
            with self.provider_lock:
                ground_h = self.provider.get_elevation(x_utm, y_utm)
            print(f"[HorizonWorker] Ground height: {ground_h}")
            if ground_h is None:
                print("[HorizonWorker] Observer outside DEM coverage. Using default 200m.")
                ground_h = 200.0
            
            # Bake using increased radius and observer offset
            print("[HorizonWorker] Starting self.baker.bake...")
            
            def bake_progress(pct, msg):
                tpl = getTraduction("Horizon.CalculatingHorizon", "⏳ Calculant horitzó: {pct}%")
                self.progress_message.emit(tpl.format(pct=pct))
            
            with self.provider_lock:
                # Read quality from config on each bake (hot-reloadable)
                from TerraLab.config import ConfigManager
                n_bands = ConfigManager().get_horizon_quality()
                active_band_defs = generate_bands(n_bands)
                print(f"[HorizonWorker] Using {n_bands} bands for bake.")
                
                azimuths, bands = self.baker.bake(
                    x_utm, y_utm,
                    obs_h_ground=ground_h + self.observer_offset,
                    step_m=50,       # 50m step
                    d_max=vis_radius,    # 150km visibility
                    delta_az_deg=0.5,
                    band_defs=active_band_defs,
                    progress_callback=bake_progress
                )
            print("[HorizonWorker] self.baker.bake returned.")
            
            # Create profile object
            print("[HorizonWorker] Creating HorizonProfile object...")
            profile = HorizonProfile(
                azimuths=azimuths,
                bands=bands,
                observer_lat=lat,
                observer_lon=lon
            )
            
            print(f"[HorizonWorker] Bake finished in {time.time()-t0:.2f}s")
            # Attach the used band_defs to the profile for the overlay to consume
            profile._band_defs = active_band_defs
            self.profile_ready.emit(profile)

        except Exception as e:
            import traceback
            print(f"[HorizonWorker] Bake Error: {e}")
            traceback.print_exc()
            self.error_occurred.emit(f"Bake Error: {e}")
