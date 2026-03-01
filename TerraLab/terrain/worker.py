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
        self.light_sampler = None
        self._abort_requested = False
        
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
            if self.light_sampler:
                self.light_sampler.close()
                self.light_sampler = None
            
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
            
            # --- Initialize Light Pollution Sampler ---
            try:
                from TerraLab.terrain.light_pollution_sampler import LightPollutionSampler
                from TerraLab.config import ConfigManager
                config = ConfigManager()
                
                # Check config first, then absolute default path
                lp_path = config.get("dvnl_path", "")
                
                if not lp_path or not os.path.exists(lp_path):
                    # Fallback to local data dir
                    base_dir = os.path.dirname(os.path.dirname(__file__))
                    local_default = os.path.join(base_dir, "data", "light_pollution", "C_DVNL 2022.tif")
                    if os.path.exists(local_default):
                        lp_path = local_default
                
                if lp_path and os.path.exists(lp_path):
                    print(f"[HorizonWorker] Loading Light Pollution Sampler with: {lp_path}")
                    self.light_sampler = LightPollutionSampler(lp_path)
                else:
                    print(f"[HorizonWorker] Warning: DVNL file not found. Using dark-sky fallback.")
                    self.light_sampler = LightPollutionSampler(None)
            except Exception as e:
                print(f"[HorizonWorker] Warning: Light Pollution Sampler failed to load: {e}")
                self.light_sampler = None
            
            self.is_initialized = True
            print(f"[HorizonWorker] Init complete in {time.time()-t0:.2f}s")
        except Exception as e:
            self.error_occurred.emit(f"Init Error: {e}")

    def get_bare_elevation(self, lat: float, lon: float) -> Optional[float]:
        """
        Fast lookup for the UI. Returns the bare terrain elevation.
        Uses a non-blocking lock check to avoid hanging the UI thread.
        """
        if not self.is_initialized or not self.provider:
            return None
             
        # Use a short timeout to avoid freezing the UI if the worker is doing a heavy I/O
        # If it's busy, we'll return None and the UI will keep showing the old value 
        # or wait for the next refresh.
        locked = self.provider_lock.acquire(blocking=True, timeout=0.05) # 50ms max wait
        if not locked:
            return None # Worker is busy baking/sampling
            
        try:
            x_utm, y_utm = self.provider.transform_coordinates(lat, lon)
            return self.provider.get_elevation(x_utm, y_utm)
        except Exception as e:
            print(f"[HorizonWorker] get_bare_elevation error: {e}")
            return None
        finally:
            self.provider_lock.release()

    def get_bortle_estimate(self, lat: float, lon: float) -> int:
        """
        Fast synchronous lookup for UI. Uses auto-loaded sampler.
        Returns the Bortle class (1-9).
        """
        if not self.is_initialized:
            try:
                self.initialize()
            except:
                pass
                
        if not self.is_initialized or not self.light_sampler:
            return 4
            
        sqm, bortle = self.light_sampler.estimate_zenith_sqm(lat, lon)
        return bortle
        return bortle

    def get_sqm_estimate(self, lat: float, lon: float) -> float:
        """Returns the estimated zenith SQM."""
        if not self.is_initialized or not self.light_sampler:
            return 21.0
        
        sqm, _ = self.light_sampler.estimate_zenith_sqm(lat, lon)
        return sqm

    @pyqtSlot(float, float)
    def request_bake(self, lat: float, lon: float):
        """Slot to trigger a bake for a specific location."""
        # Check if we are already initialized
        if not self.is_initialized:
             self.initialize()
             
        # If a bake is already in progress, signal it to abort
        # Note: Since this is a queued connection, it only runs when thread is free.
        # But if another bake request arrives while this is WAITING in the queue, 
        # that's fine. If we want to abort DURING execution, we'd need another mechanism.
        # However, for now, we'll reset the flag at start.
        self._abort_requested = False
        
        if not self.is_initialized:
            # If still not initialized, it means config is missing or invalid.
            # We can't bake.
            return 


        try:
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
                self.provider.prepare_region(x_utm, y_utm, vis_radius, progress_callback=region_progress, abort_check=lambda: self._abort_requested)
            
            # Pre-load Light Pollution ROI
            if self.light_sampler:
                print(f"[HorizonWorker] Preparing LP Sampler for {lat}, {lon}...")
                # We need to tell the sampler about the region in KM
                self.light_sampler.prepare_region(lat, lon, vis_radius / 1000.0)
            
            self.progress_message.emit(getTraduction("Horizon.CalculatingHorizonGeneric", "⏳ Calculant horitzó..."))
            
            # Sample ground height for this location
            # If valid, use it. If not, fallback.
            print("[HorizonWorker] Sampling ground height...")
            with self.provider_lock:
                try:
                    ground_h = self.provider.get_elevation(x_utm, y_utm)
                except Exception as e:
                    print(f"[HorizonWorker] Error sampling height: {e}")
                    ground_h = None
            
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
                
                azimuths, bands, light_domes, light_peak_distances = self.baker.bake(
                    obs_x=x_utm,
                    obs_y=y_utm,
                    obs_h_ground=ground_h + self.observer_offset,
                    step_m=50,       # 50m step
                    d_max=vis_radius,    # 150km visibility
                    delta_az_deg=0.5,
                    band_defs=active_band_defs,
                    progress_callback=bake_progress,
                    light_sampler=self.light_sampler,
                    abort_check=lambda: self._abort_requested
                )
            print("[HorizonWorker] self.baker.bake returned.")
            
            # Create profile object
            print("[HorizonWorker] Creating HorizonProfile object...")
            profile = HorizonProfile(
                azimuths=azimuths,
                bands=bands,
                observer_lat=lat,
                observer_lon=lon,
                light_domes=light_domes,
                light_peak_distances=light_peak_distances
            )
            
            print(f"[HorizonWorker] Bake finished in {time.time()-t0:.2f}s")
            # Attach the used band_defs to the profile for the overlay to consume
            profile._band_defs = active_band_defs
            self.profile_ready.emit(profile)

        except InterruptedError:
            print("[HorizonWorker] Bake ABORTED by user request.")
        except Exception as e:
            print(f"[HorizonWorker] CRITICAL ERROR during bake: {e}")
            import traceback
            traceback.print_exc()
            self.error_occurred.emit(f"Bake Error: {e}")
        finally:
            self._abort_requested = False # Reset for safety
            self.progress_message.emit("") # Clear progress message
