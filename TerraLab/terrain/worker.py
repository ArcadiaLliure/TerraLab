import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Optional

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from TerraLab.common.utils import getTraduction
from TerraLab.terrain.engine import HorizonProfile, generate_bands


class HorizonWorker(QObject):
    """
    Background coordinator for DEM helpers and subprocess-based horizon bakes.

    The heavy bake runs in a separate Python process. This worker only:
    - keeps light-weight DEM access for quick elevation/Bortle queries
    - launches the bake subprocess
    - parses structured JSONL progress/preview/final events
    """

    profile_ready = pyqtSignal(object)
    preview_ready = pyqtSignal(object)
    progress_state = pyqtSignal(object)
    progress_message = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, tiles_dir=None, parent=None):
        super().__init__(parent)
        self.tiles_dir = tiles_dir
        self.is_initialized = False
        self.provider = None
        self.baker = None
        self.observer_offset = 0.0
        self.needs_reload = False
        self.light_sampler = None
        self._progress_lock = threading.Lock()
        self._progress_text = ""
        self._progress_state = None
        self._process_lock = threading.Lock()
        self._current_process = None
        self._current_job_id = None
        self._current_temp_dir = None

    def set_observer_offset(self, offset: float):
        self.observer_offset = offset

    def _store_progress(self, state: Optional[dict]) -> None:
        with self._progress_lock:
            self._progress_state = dict(state) if state else None
            self._progress_text = self._format_progress_text(state) if state else ""

    def get_progress_text(self) -> str:
        with self._progress_lock:
            return self._progress_text

    def get_progress_state(self):
        with self._progress_lock:
            return dict(self._progress_state) if self._progress_state else None

    def reload_config(self):
        self.needs_reload = True
        self.tiles_dir = None

    def _resolve_tiles_dir(self):
        if self.tiles_dir and os.path.exists(self.tiles_dir):
            return self.tiles_dir
        from TerraLab.config import ConfigManager

        self.tiles_dir = ConfigManager().get_raster_path()
        return self.tiles_dir

    @pyqtSlot()
    def initialize(self):
        """Lazy initialization of light-weight DEM access for quick UI queries."""
        if self.needs_reload:
            if self.provider and hasattr(self.provider, "close"):
                try:
                    self.provider.close()
                except Exception as exc:
                    print(f"[HorizonWorker] Warning closing provider: {exc}")
            if self.light_sampler and hasattr(self.light_sampler, "close"):
                try:
                    self.light_sampler.close()
                except Exception:
                    pass
            self.provider = None
            self.light_sampler = None
            self.is_initialized = False
            self.needs_reload = False

        if self.is_initialized:
            return

        tiles_dir = self._resolve_tiles_dir()
        if not tiles_dir or not os.path.exists(tiles_dir):
            self.error_occurred.emit(f"Tiles directory not configured or found: {tiles_dir}")
            return

        try:
            def index_callback(_percent, _msg):
                return None

            is_tiff = False
            tiff_path = None
            if os.path.isfile(tiles_dir) and tiles_dir.lower().endswith((".tif", ".tiff")):
                is_tiff = True
                tiff_path = tiles_dir
            elif os.path.isdir(tiles_dir):
                tifs = [f for f in os.listdir(tiles_dir) if f.lower().endswith((".tif", ".tiff"))]
                if tifs:
                    is_tiff = True
                    tiff_path = os.path.join(tiles_dir, tifs[0])

            if is_tiff and tiff_path:
                from TerraLab.terrain.providers import TiffRasterWindowProvider

                self.provider = TiffRasterWindowProvider(tiff_path)
            else:
                from TerraLab.terrain.providers import AscRasterProvider

                self.provider = AscRasterProvider(tiles_dir)

            self.provider.initialize(progress_callback=index_callback)
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
                self.light_sampler = LightPollutionSampler(lp_path if lp_path and os.path.exists(lp_path) else None)
            except Exception as exc:
                print(f"[HorizonWorker] Warning: Light pollution sampler unavailable: {exc}")
                self.light_sampler = None

            self.is_initialized = True
        except Exception as exc:
            self.error_occurred.emit(f"Init Error: {exc}")
        finally:
            self._store_progress(None)
            self.progress_message.emit("")

    def get_bare_elevation(self, lat: float, lon: float) -> Optional[float]:
        if not self.is_initialized or not self.provider:
            return None
        try:
            x_utm, y_utm = self.provider.transform_coordinates(lat, lon)
            return self.provider.get_elevation(x_utm, y_utm)
        except Exception as exc:
            print(f"[HorizonWorker] get_bare_elevation error: {exc}")
            return None

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

    def abort_current_job(self) -> None:
        with self._process_lock:
            proc = self._current_process
            job_id = self._current_job_id
        if proc is None:
            return
        try:
            if proc.poll() is None:
                print(f"[HorizonWorker] Terminating horizon bake job {job_id}...")
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception as exc:
            print(f"[HorizonWorker] Warning terminating bake job {job_id}: {exc}")

    def _cleanup_temp_dir(self, temp_dir: Optional[str]) -> None:
        if not temp_dir:
            return
        try:
            import shutil

            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    def _build_subprocess_command(self, job: dict, output_path: str, preview_path: str):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        cmd = [
            sys.executable,
            "-m",
            "TerraLab.terrain.bake_process",
            "--job-id",
            str(job["job_id"]),
            "--lat",
            str(float(job["lat"])),
            "--lon",
            str(float(job["lon"])),
            "--tiles-dir",
            str(job["tiles_dir"]),
            "--observer-offset",
            str(float(job.get("observer_offset", self.observer_offset))),
            "--bands",
            str(int(job["bands"])),
            "--output",
            str(output_path),
            "--preview-path",
            str(preview_path),
            "--view-azimuth",
            str(float(job.get("view_azimuth", 180.0))),
            "--view-fov-deg",
            str(float(job.get("view_fov_deg", 90.0))),
            "--view-elevation",
            str(float(job.get("view_elevation", 0.0))),
        ]
        return base_dir, cmd

    @staticmethod
    def _parse_json_event(line: str):
        raw = str(line or "").strip()
        if not raw or not raw.startswith("{"):
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict) or "type" not in data:
            return None
        return data

    @staticmethod
    def _format_progress_text(state: Optional[dict]) -> str:
        if not state:
            return ""
        percent = max(0.0, min(100.0, float(state.get("percent", 0.0))))
        percent_text = f"{percent:.1f}"
        if percent_text.endswith(".0"):
            percent_text = percent_text[:-2]
        current = state.get("current")
        total = state.get("total")
        base = getTraduction("Horizon.CalculatingHorizon", "Calculating horizon: {pct}%").format(pct=percent_text)
        if current is not None and total:
            return f"{base} · {int(current)}/{int(total)}"
        return base

    @staticmethod
    def _drain_stream_to_stderr(stream, prefix: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                raw = str(line).rstrip()
                if raw:
                    print(f"{prefix}{raw}")
        except Exception:
            pass

    def _emit_progress_state(self, state: dict) -> None:
        state = dict(state)
        self._store_progress(state)
        self.progress_state.emit(state)
        self.progress_message.emit(self._format_progress_text(state))

    @pyqtSlot(object)
    def request_bake(self, job: object):
        try:
            if not isinstance(job, dict):
                raise TypeError("Horizon bake job must be a dict")

            tiles_dir = self._resolve_tiles_dir()
            if not tiles_dir or not os.path.exists(tiles_dir):
                self.error_occurred.emit(f"Tiles directory not configured or found: {tiles_dir}")
                return

            job = dict(job)
            job.setdefault("observer_offset", float(self.observer_offset))
            job.setdefault("bands", 20)
            job["tiles_dir"] = tiles_dir

            temp_dir = tempfile.mkdtemp(prefix=f"tl_horizon_{job['job_id']}_")
            output_path = os.path.join(temp_dir, "profile_final.npz")
            preview_path = os.path.join(temp_dir, "profile_preview.npz")
            base_dir, cmd = self._build_subprocess_command(job, output_path, preview_path)

            self.abort_current_job()
            self._cleanup_temp_dir(self._current_temp_dir)
            with self._process_lock:
                self._current_job_id = str(job["job_id"])
                self._current_temp_dir = temp_dir

            initial_state = {
                "job_id": self._current_job_id,
                "phase": "prepare",
                "percent": 0.0,
                "current": 0,
                "total": int(round(360.0 / 0.5)),
            }
            self._emit_progress_state(initial_state)

            proc = subprocess.Popen(
                cmd,
                cwd=base_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with self._process_lock:
                self._current_process = proc

            stderr_thread = threading.Thread(
                target=self._drain_stream_to_stderr,
                args=(proc.stderr, "[HorizonBakeProcess] "),
                daemon=True,
            )
            stderr_thread.start()

            active_job_id = str(job["job_id"])
            band_defs = generate_bands(max(1, int(job["bands"])))
            final_emitted = False

            assert proc.stdout is not None
            for raw_line in iter(proc.stdout.readline, ""):
                event = self._parse_json_event(raw_line)
                if not event:
                    continue
                if str(event.get("job_id", "")) != active_job_id:
                    continue

                event_type = str(event.get("type", ""))
                if event_type == "progress":
                    state = {
                        "job_id": active_job_id,
                        "phase": str(event.get("phase", "bake")),
                        "percent": float(event.get("percent", 0.0)),
                        "current": event.get("current"),
                        "total": event.get("total"),
                    }
                    self._emit_progress_state(state)
                elif event_type == "preview":
                    snapshot_path = str(event.get("snapshot_path", "") or "")
                    if not snapshot_path or not os.path.exists(snapshot_path):
                        continue
                    try:
                        profile = HorizonProfile.load(snapshot_path)
                        profile._band_defs = band_defs
                        self.preview_ready.emit(
                            {
                                "job_id": active_job_id,
                                "profile": profile,
                                "current": event.get("current"),
                                "total": event.get("total"),
                            }
                        )
                    except Exception as exc:
                        print(f"[HorizonWorker] Preview load failed: {exc}")
                elif event_type == "done":
                    profile_path = str(event.get("profile_path", "") or "")
                    if not profile_path or not os.path.exists(profile_path):
                        raise RuntimeError("Horizon bake completed without profile output")
                    profile = HorizonProfile.load(profile_path)
                    profile._band_defs = band_defs
                    self.profile_ready.emit({"job_id": active_job_id, "profile": profile})
                    final_emitted = True
                elif event_type == "error":
                    message = str(event.get("message", "Unknown bake error"))
                    raise RuntimeError(message)

            return_code = proc.wait()
            stderr_thread.join(timeout=0.2)
            if return_code != 0 and not final_emitted:
                raise RuntimeError(f"Horizon bake subprocess failed with exit code {return_code}")
        except Exception as exc:
            print(f"[HorizonWorker] CRITICAL ERROR during bake: {exc}")
            import traceback

            traceback.print_exc()
            self.error_occurred.emit(f"Bake Error: {exc}")
        finally:
            with self._process_lock:
                proc = self._current_process
                temp_dir = self._current_temp_dir
                self._current_process = None
                self._current_temp_dir = None
                self._current_job_id = None
            if proc is not None:
                try:
                    if proc.stdout:
                        proc.stdout.close()
                    if proc.stderr:
                        proc.stderr.close()
                except Exception:
                    pass
            self._cleanup_temp_dir(temp_dir)
            self._store_progress(None)
            self.progress_message.emit("")
