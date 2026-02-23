import os
import time
from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from TerraLab.terrain.engine import bake_and_save, HorizonBaker, TileIndex, TileCache, DemSampler, HorizonProfile, DEFAULT_BANDS

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
        self.sampler = None
        self.baker = None
        self.cache = None
        self.index = None

    def reload_config(self):
        """Reset state to force re-initialization on next bake."""
        self.is_initialized = False
        self.tiles_dir = None # Force re-read from ConfigManager

    def initialize(self):
        """Lazy initialization of heavy DEM index."""
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
            print(f"[HorizonWorker] Initializing DEM index from {self.tiles_dir}...")
            t0 = time.time()
            
            def index_callback(current, total, msg):
                 self.progress_message.emit(msg)
            
            self.index = TileIndex(self.tiles_dir, callback=index_callback)
            self.cache = TileCache(capacity=500)
            self.sampler = DemSampler(self.index, self.cache)
            self.baker = HorizonBaker(self.sampler)
            self.is_initialized = True
            print(f"[HorizonWorker] Init complete in {time.time()-t0:.2f}s")
        except Exception as e:
            self.error_occurred.emit(f"Init Error: {e}")

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
            
            # --- UTM CONVERSION (Pure Python to avoid PROJ threading crash) ---
            # Custom implementation for ETRS89 / UTM zone 31N (EPSG:25831)
            # Based on WGS84 ellipsoid
            import math
            
            def latlon_to_utm31(lat, lon):
                # Constants for WGS84
                a = 6378137.0
                f = 1 / 298.257223563
                k0 = 0.9996
                lon0 = 3.0 # Central meridian for Zone 31 (0 to 6 deg E? No, 31 is 0E to 6E. Center is 3E)
                # Correction: Zone 31 is 0E to 6E. Center is 3E.
                
                phi = math.radians(lat)
                lam = math.radians(lon)
                lam0 = math.radians(lon0)
                
                e2 = 2*f - f*f
                ep2 = e2 / (1 - e2)
                
                N = a / math.sqrt(1 - e2 * math.sin(phi)**2)
                T = math.tan(phi)**2
                C = ep2 * math.cos(phi)**2
                A = (lam - lam0) * math.cos(phi)
                
                M = a * ((1 - e2/4 - 3*e2**2/64 - 5*e2**3/256) * phi -
                         (3*e2/8 + 3*e2**2/32 + 45*e2**3/1024) * math.sin(2*phi) +
                         (15*e2**2/256 + 45*e2**3/1024) * math.sin(4*phi) -
                         (35*e2**3/3072) * math.sin(6*phi))
                         
                x = 500000 + k0 * N * (A + (1-T+C)*A**3/6 + (5-18*T+T**2+72*C-58*ep2)*A**5/120)
                y = k0 * (M + N * math.tan(phi) * (A**2/2 + (5-T+9*C+4*C**2)*A**4/24 + (61-58*T+T**2+600*C-330*ep2)*A**6/720))
                
                return x, y

            print(f"[HorizonWorker] Transforming {lon}, {lat} using pure math...", flush=True)
            x_utm, y_utm = latlon_to_utm31(lat, lon)
            print(f"[HorizonWorker] UTM Coords: {x_utm}, {y_utm}", flush=True)
            
            # --- Pre-warm Cache with Feedback ---
            vis_radius = 150000.0 # 150km #TODO: Apujar en un futur quan es distribueixi fora de Catalunya
            print(f"[HorizonWorker] Determining tiles in {vis_radius/1000}km radius...")
            tiles_needed = self.index.get_overlapping_tiles(x_utm, y_utm, vis_radius)
            total_tiles = len(tiles_needed)
            print(f"[HorizonWorker] Need {total_tiles} tiles.")
            
            for i, tile in enumerate(tiles_needed):
                # Only signal every N tiles to avoid UI spam, BUT for new parsing it's slow anyway so every 1 is fine if slow, or every 5 if fast.
                if i % 5 == 0 or i == total_tiles - 1:
                    percent = int((i+1) / total_tiles * 100)
                    msg = f"⏳ Cargando mapas: {i+1}/{total_tiles} ({percent}%)"
                    self.progress_message.emit(msg)
                
                # Force load (will generate NPY if needed)
                self.cache.load(tile)
            
            self.progress_message.emit(f"⏳ Calculando horizonte...")
            
            # Sample ground height for this location
            # If valid, use it. If not, fallback.
            print("[HorizonWorker] Sampling ground height...")
            ground_h = self.sampler.sample(x_utm, y_utm)
            print(f"[HorizonWorker] Ground height: {ground_h}")
            if ground_h is None:
                print("[HorizonWorker] Observer outside DEM coverage. Using default 200m.")
                ground_h = 200.0
            
            # Bake using increased radius for far mountains
            print("[HorizonWorker] Starting self.baker.bake...")
            azimuths, bands = self.baker.bake(
                x_utm, y_utm,
                obs_h_ground=ground_h,
                step_m=50,       # 50m step
                d_max=vis_radius,    # 150km visibility (was 100km)
                delta_az_deg=0.5,
                band_defs=DEFAULT_BANDS
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
            self.profile_ready.emit(profile)

        except Exception as e:
            import traceback
            print(f"[HorizonWorker] Bake Error: {e}")
            traceback.print_exc()
            self.error_occurred.emit(f"Bake Error: {e}")
