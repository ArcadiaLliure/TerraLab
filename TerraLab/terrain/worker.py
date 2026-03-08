import os
import subprocess
import sys
import tempfile
import time
from typing import Optional

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from TerraLab.common.utils import getTraduction
from TerraLab.terrain.engine import HorizonBaker, HorizonProfile, generate_bands


class HorizonWorker(QObject):
    """
    Background worker to load DEM metadata and delegate heavy horizon bakes
    to a separate Python process.
    """

    profile_ready = pyqtSignal(object)
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
        self.lp_preload_enabled = os.environ.get("TL_LP_PRELOAD", "0") == "1"
        self._abort_requested = False

        import threading

        self.provider_lock = threading.Lock()

    def set_observer_offset(self, offset: float):
        self.observer_offset = offset

    def reload_config(self):
        self.needs_reload = True
        self.tiles_dir = None

    def _resolve_tiles_dir(self):
        if self.tiles_dir and os.path.exists(self.tiles_dir):
            return self.tiles_dir
        from TerraLab.config import ConfigManager

        self.tiles_dir = ConfigManager().get_raster_path()
        return self.tiles_dir

    def initialize(self):
        """Lazy initialization of light-weight DEM access for quick UI queries."""
        if self.needs_reload:
            if self.provider and hasattr(self.provider, "close"):
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

        self._resolve_tiles_dir()
        if not self.tiles_dir or not os.path.exists(self.tiles_dir):
            self.error_occurred.emit(f"Tiles directory not configured or found: {self.tiles_dir}")
            return

        try:
            print(f"[HorizonWorker] Initializing RasterProvider from {self.tiles_dir}...")
            t0 = time.time()

            def index_callback(percent, msg):
                self.progress_message.emit(msg)

            is_tiff = False
            tiff_path = None

            if os.path.isfile(self.tiles_dir) and self.tiles_dir.lower().endswith((".tif", ".tiff")):
                is_tiff = True
                tiff_path = self.tiles_dir
            elif os.path.isdir(self.tiles_dir):
                tifs = [f for f in os.listdir(self.tiles_dir) if f.lower().endswith((".tif", ".tiff"))]
                if tifs:
                    is_tiff = True
                    tiff_path = os.path.join(self.tiles_dir, tifs[0])

            if is_tiff and tiff_path:
                print(f"[HorizonWorker] Auto-detected GeoTIFF: {tiff_path}")
                from TerraLab.terrain.providers import TiffRasterWindowProvider

                with self.provider_lock:
                    self.provider = TiffRasterWindowProvider(tiff_path)
            else:
                print("[HorizonWorker] Using ASC/TXT directory mode")
                from TerraLab.terrain.providers import AscRasterProvider

                with self.provider_lock:
                    self.provider = AscRasterProvider(self.tiles_dir)

            with self.provider_lock:
                self.provider.initialize(progress_callback=index_callback)
                self.baker = HorizonBaker(self.provider)

            try:
                from TerraLab.config import ConfigManager
                from TerraLab.terrain.light_pollution_sampler import LightPollutionSampler

                config = ConfigManager()
                lp_path = config.get("dvnl_path", "")
                if not lp_path or not os.path.exists(lp_path):
                    base_dir = os.path.dirname(os.path.dirname(__file__))
                    local_default = os.path.join(base_dir, "data", "light_pollution", "C_DVNL 2022.tif")
                    if os.path.exists(local_default):
                        lp_path = local_default

                if lp_path and os.path.exists(lp_path):
                    print(f"[HorizonWorker] Loading Light Pollution Sampler with: {lp_path}")
                    self.light_sampler = LightPollutionSampler(lp_path)
                else:
                    print("[HorizonWorker] Warning: DVNL file not found. Using dark-sky fallback.")
                    self.light_sampler = LightPollutionSampler(None)
            except Exception as e:
                print(f"[HorizonWorker] Warning: Light Pollution Sampler failed to load: {e}")
                self.light_sampler = None

            self.is_initialized = True
            print(f"[HorizonWorker] Init complete in {time.time() - t0:.2f}s")
        except Exception as e:
            self.error_occurred.emit(f"Init Error: {e}")

    def get_bare_elevation(self, lat: float, lon: float) -> Optional[float]:
        if not self.is_initialized or not self.provider:
            return None

        locked = self.provider_lock.acquire(blocking=True, timeout=0.05)
        if not locked:
            return None

        try:
            x_utm, y_utm = self.provider.transform_coordinates(lat, lon)
            return self.provider.get_elevation(x_utm, y_utm)
        except Exception as e:
            print(f"[HorizonWorker] get_bare_elevation error: {e}")
            return None
        finally:
            self.provider_lock.release()

    def get_bortle_estimate(self, lat: float, lon: float) -> int:
        if not self.is_initialized or not self.light_sampler:
            return 4
        sqm, bortle = self.light_sampler.estimate_zenith_sqm(lat, lon)
        return bortle

    def get_sqm_estimate(self, lat: float, lon: float) -> float:
        if not self.is_initialized or not self.light_sampler:
            return 21.0
        sqm, _ = self.light_sampler.estimate_zenith_sqm(lat, lon)
        return sqm

    def _build_subprocess_command(self, lat: float, lon: float, tiles_dir: str, output_path: str, n_bands: int):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        return base_dir, [
            sys.executable,
            "-m",
            "TerraLab.terrain.bake_process",
            "--lat",
            str(float(lat)),
            "--lon",
            str(float(lon)),
            "--tiles-dir",
            str(tiles_dir),
            "--observer-offset",
            str(float(self.observer_offset)),
            "--bands",
            str(int(n_bands)),
            "--output",
            str(output_path),
        ]

    @pyqtSlot(float, float)
    def request_bake(self, lat: float, lon: float):
        self._abort_requested = False

        try:
            print(f"[HorizonWorker] Baking starting for {lat}, {lon}...")
            t0 = time.time()

            tiles_dir = self._resolve_tiles_dir()
            if not tiles_dir or not os.path.exists(tiles_dir):
                self.error_occurred.emit(f"Tiles directory not configured or found: {tiles_dir}")
                return

            from TerraLab.config import ConfigManager

            n_bands = int(ConfigManager().get_horizon_quality())
            active_band_defs = generate_bands(n_bands)
            self.progress_message.emit(
                getTraduction("Horizon.CalculatingHorizonGeneric", "Calculating horizon...")
            )

            fd, tmp_path = tempfile.mkstemp(prefix="terralab_horizon_", suffix=".npz")
            os.close(fd)
            base_dir, cmd = self._build_subprocess_command(lat, lon, tiles_dir, tmp_path, n_bands)
            print(f"[HorizonWorker] Launching horizon subprocess with {n_bands} bands...")
            proc = subprocess.Popen(
                cmd,
                cwd=base_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            try:
                while True:
                    try:
                        return_code = proc.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        if self._abort_requested:
                            print("[HorizonWorker] Terminating horizon subprocess...")
                            proc.terminate()
                            try:
                                proc.wait(timeout=2.0)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                            raise InterruptedError("Bake aborted")
                if return_code != 0:
                    raise RuntimeError(
                        f"Horizon bake subprocess failed with exit code {return_code}"
                    )

                profile = HorizonProfile.load(tmp_path)
                profile._band_defs = active_band_defs
                print(f"[HorizonWorker] Bake finished in {time.time() - t0:.2f}s")
                self.profile_ready.emit(profile)
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        except InterruptedError:
            print("[HorizonWorker] Bake ABORTED by user request.")
        except Exception as e:
            print(f"[HorizonWorker] CRITICAL ERROR during bake: {e}")
            import traceback

            traceback.print_exc()
            self.error_occurred.emit(f"Bake Error: {e}")
        finally:
            self._abort_requested = False
            self.progress_message.emit("")
