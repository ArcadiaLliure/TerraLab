import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QObject, QMetaObject, Qt, pyqtSignal, pyqtSlot

from TerraLab.common.utils import (
    getTraduction,
    get_config_value,
    set_config_value,
)
from TerraLab.terrain.engine import HorizonProfile, generate_bands
from TerraLab.terrain.data_sources import DataSourceRegistry, LayerSelectionService


_LP_RESULT_PREFIX = "TERRALAB_LP_RESULT="


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
    bortle_estimate_ready = pyqtSignal(int, float, float, int)
    effective_sources_changed = pyqtSignal(object)

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
        self._initialize_after_reload_queued = False
        self._initialize_running = False
        self._last_reload_pending_log_ts = 0.0
        self._use_isolated_lp_sampler = bool(
            os.name == "nt" and sys.version_info >= (3, 13)
        )
        self._isolated_lp_sampler_logged = False
        self._provider_source_path = ""
        self.data_source_registry = DataSourceRegistry.default()
        self.layer_selection = LayerSelectionService(self.data_source_registry)
        self._light_source_signature = None
        self._surface_service = None
        self._surface_source_signature = None
        self._last_profile = None

    def _ensure_light_sampler_for_location(self, lat: float, lon: float):
        enabled = bool(get_config_value("light_pollution_enabled", True))
        selection = self.layer_selection.select_light_pollution(lat, lon)
        sources = list(selection.chain) if enabled else []
        signature = tuple(
            (source.id, source.path, source.fingerprint) for source in sources
        )
        if signature == self._light_source_signature and self.light_sampler is not None:
            return self.light_sampler
        old = self.light_sampler
        self.light_sampler = None
        if old is not None and hasattr(old, "close"):
            old.close()
        if sources:
            from TerraLab.terrain.light_pollution_sampler import (
                create_light_pollution_sampler,
                snapshot_light_pollution_sources,
            )

            self.light_sampler = create_light_pollution_sampler(
                snapshot_light_pollution_sources(sources)
            )
        self._light_source_signature = signature
        return self.light_sampler

    def _surface_selection(self, lat: float, lon: float):
        selection = self.layer_selection.select_surface(lat, lon)
        visible = bool(get_config_value("ui.visibility.earth.surface", True))
        return selection, list(selection.chain) if visible else []

    @staticmethod
    def _effective_surface_source_id(cache):
        if cache is None:
            return None
        get = cache.get if isinstance(cache, dict) else lambda key, default=None: getattr(cache, key, default)
        source_ids = tuple(get("source_ids", ()) or ())
        for valid_name, index_name in (
            ("profile_valid", "profile_source_indices"),
            ("relief_valid", "relief_source_indices"),
        ):
            valid = get(valid_name, None)
            indices = get(index_name, None)
            if valid is None or indices is None:
                continue
            valid_array = __import__("numpy").asarray(valid, dtype=bool)
            index_array = __import__("numpy").asarray(indices)
            usable = index_array[valid_array & (index_array >= 0)]
            if usable.size:
                index = int(usable.flat[0])
                return source_ids[index] if 0 <= index < len(source_ids) else None
        return None

    def _prepare_surface_samples(self, profile):
        if profile is None:
            return None
        selection, sources = self._surface_selection(
            float(getattr(profile, "observer_lat", 0.0)),
            float(getattr(profile, "observer_lon", 0.0)),
        )
        signature = tuple(
            (
                str(getattr(source, "id", "")),
                str(getattr(source, "path", "")),
                str(getattr(source, "fingerprint", "")),
            )
            for source in sources
        )
        if signature != self._surface_source_signature:
            if self._surface_service is not None:
                self._surface_service.close()
            self._surface_service = None
            if sources:
                from TerraLab.terrain.surface import (
                    SurfaceSamplingService,
                    create_surface_providers,
                )

                self._surface_service = SurfaceSamplingService(
                    create_surface_providers(sources)
                )
            self._surface_source_signature = signature
        if self._surface_service is None:
            profile.surface_samples = None
            return profile
        geometry_id = str(getattr(profile, "geometry_id", "") or "")
        profile.surface_samples = self._surface_service.sample_profile(
            profile, geometry_id=geometry_id
        )
        profile.effective_surface_source_id = self._effective_surface_source_id(
            profile.surface_samples
        )
        profile.surface_source_status = str(getattr(selection, "reason", ""))
        return profile

    def _publish_effective_sources(self, profile=None):
        payload = {
            "surface": {
                "source_id": str(
                    getattr(profile, "effective_surface_source_id", "") or ""
                ),
                "status": str(getattr(profile, "surface_source_status", "") or ""),
            }
        }
        self.effective_sources_changed.emit(payload)
        return payload

    @pyqtSlot(object)
    def request_surface_refresh(self, profile=None):
        target = profile or self._last_profile
        if target is None:
            return
        self._prepare_surface_samples(target)
        self._last_profile = target
        self._publish_effective_sources(target)
        self.profile_ready.emit({"job_id": "surface-refresh", "profile": target})

    def shutdown(self) -> None:
        self.abort_current_job()
        for resource_name in ("_surface_service", "light_sampler", "provider"):
            resource = getattr(self, resource_name, None)
            if resource is not None and hasattr(resource, "close"):
                try:
                    resource.close()
                except Exception:
                    pass
            setattr(self, resource_name, None)

    @staticmethod
    def _paths_equivalent(path_a: object, path_b: object) -> bool:
        try:
            a = Path(str(path_a or "")).expanduser().resolve()
            b = Path(str(path_b or "")).expanduser().resolve()
            return str(a).lower() == str(b).lower()
        except Exception:
            return str(path_a or "").strip().lower() == str(path_b or "").strip().lower()

    def _build_light_sampler(self):
        try:
            from TerraLab.config import ConfigManager
            from TerraLab.terrain.light_pollution_sampler import (
                LightPollutionSampler,
            )

            config = ConfigManager()
            lp_enabled = bool(config.get("light_pollution_enabled", True))
            lp_path = ""
            if lp_enabled:
                lp_path = config.get("dvnl_path", "")
                if not lp_path or not os.path.exists(lp_path):
                    base_dir = os.path.dirname(os.path.dirname(__file__))
                    local_default = os.path.join(
                        base_dir,
                        "data",
                        "light_pollution",
                        "C_DVNL 2022.tif",
                    )
                    if os.path.exists(local_default):
                        lp_path = local_default
            return LightPollutionSampler(
                lp_path if lp_path and os.path.exists(lp_path) else None
            )
        except Exception as exc:
            print(
                f"[HorizonWorker] Warning: Light pollution sampler unavailable: {exc}"
            )
            return None

    def set_observer_offset(self, offset: float):
        """Defineix observer offset a la instancia de HorizonWorker.

        Par?metres:
        - offset (float): Valor del parametre 'offset'.

        Retorna:
        - None.
        """
        self.observer_offset = offset

    def _store_progress(self, state: Optional[dict]) -> None:
        with self._progress_lock:
            self._progress_state = dict(state) if state else None
            self._progress_text = (
                self._format_progress_text(state) if state else ""
            )

    def get_progress_text(self) -> str:
        """Obte progress text de la instancia de HorizonWorker.

        Par?metres:
        - Cap.

        Retorna:
        - str: Valor retornat pel metode.
        """
        with self._progress_lock:
            return self._progress_text

    def get_progress_state(self):
        """Obte progress state de la instancia de HorizonWorker.

        Par?metres:
        - Cap.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        with self._progress_lock:
            return dict(self._progress_state) if self._progress_state else None

    def reload_config(self):
        """Executa el metode reload_config de la classe HorizonWorker.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        self.needs_reload = True
        self.tiles_dir = None
        # Safety: avoid hot re-initialize races on Windows/Python 3.13.
        if not (os.name == "nt" and sys.version_info >= (3, 13)):
            self._queue_initialize_after_reload()

    def _queue_initialize_after_reload(self) -> None:
        """Encola una reinicialitzacio del worker al seu propi thread."""
        if bool(self._initialize_after_reload_queued):
            return
        self._initialize_after_reload_queued = True
        # PyQt5 returns `None` here even when the queued invocation is valid.
        # Avoid interpreting the return value as a boolean success flag.
        QMetaObject.invokeMethod(
            self,
            "_run_initialize_after_reload",
            Qt.QueuedConnection,
        )

    @pyqtSlot()
    def _run_initialize_after_reload(self) -> None:
        """Executa `initialize()` diferit per tancar un `needs_reload` pendent."""
        self._initialize_after_reload_queued = False
        if bool(self._initialize_running):
            return
        if (not bool(self.needs_reload)) and bool(self.is_initialized):
            return
        self.initialize()

    @staticmethod
    def _path_has_dem_data(candidate_path: str) -> bool:
        """Retorna si el path indicat conte dades DEM utilitzables."""
        candidate_text = str(candidate_path or "").strip()
        if not candidate_text:
            return False
        candidate = Path(candidate_text)
        if not candidate.exists():
            return False
        allowed_suffixes = {".tif", ".tiff", ".asc", ".txt", ".npy"}
        if candidate.is_file():
            return candidate.suffix.lower() in allowed_suffixes
        if not candidate.is_dir():
            return False
        for pattern in ("*.tif", "*.tiff", "*.asc", "*.txt", "*.npy"):
            try:
                if any(candidate.glob(pattern)):
                    return True
            except Exception:
                continue
        return False

    def _dem_path_candidates(self) -> list[str]:
        """Construeix candidats de ruta DEM per ordre de prioritat."""
        candidates: list[str] = []

        def append_candidate(value: object) -> None:
            path_text = str(value or "").strip()
            if not path_text:
                return
            if path_text not in candidates:
                candidates.append(path_text)

        append_candidate(self.tiles_dir)

        configured_raster_path = ""
        try:
            from TerraLab.config import ConfigManager

            configured_raster_path = str(
                ConfigManager().get_raster_path() or ""
            ).strip()
        except Exception:
            configured_raster_path = ""
        append_candidate(configured_raster_path)

        append_candidate(get_config_value("assets.elevation_dem.path", ""))

        try:
            from TerraLab.common.app_paths import ensure_runtime_layout

            runtime_paths = ensure_runtime_layout()
            append_candidate(runtime_paths.get("data_elevation"))
        except Exception:
            pass

        project_root = Path(__file__).resolve().parents[1]
        append_candidate(project_root / "data" / "elevation")
        append_candidate(project_root.parent / "data" / "dem")

        return candidates

    def _resolve_tiles_dir(self):
        """Resol la ruta DEM activa, amb autodeteccio quan el config es buit."""
        found_existing_path = None
        for candidate_path in self._dem_path_candidates():
            if not os.path.exists(candidate_path):
                continue
            if found_existing_path is None:
                found_existing_path = candidate_path
            if not self._path_has_dem_data(candidate_path):
                continue
            self.tiles_dir = candidate_path
            try:
                configured_value = str(get_config_value("raster_path", "") or "")
            except Exception:
                configured_value = ""
            if str(configured_value).strip() != str(candidate_path).strip():
                try:
                    set_config_value("raster_path", str(candidate_path))
                    print(
                        "[HorizonWorker] raster_path auto-configured "
                        f"to '{candidate_path}'"
                    )
                except Exception as exc:
                    print(
                        "[HorizonWorker] Warning persisting raster_path "
                        f"('{candidate_path}'): {exc}"
                    )
            return self.tiles_dir
        # Keep a concrete existing path for diagnostics even if no DEM payload was detected.
        self.tiles_dir = found_existing_path
        return self.tiles_dir

    @pyqtSlot()
    def initialize(self):
        """Lazy initialization of light-weight DEM access for quick UI queries."""
        if bool(self._initialize_running):
            return
        self._initialize_running = True
        if self.needs_reload:
            resolved_tiles_dir = self._resolve_tiles_dir()
            same_dem_source = bool(
                self.provider is not None
                and bool(self.is_initialized)
                and self._paths_equivalent(
                    resolved_tiles_dir, self._provider_source_path
                )
            )
            # On Win+Py3.13, avoid rebuilding provider/pyproj pipeline unless DEM source changed.
            if (
                same_dem_source
                and os.name == "nt"
                and sys.version_info >= (3, 13)
            ):
                if self.light_sampler and hasattr(self.light_sampler, "close"):
                    try:
                        self.light_sampler.close()
                    except Exception:
                        pass
                self.light_sampler = self._build_light_sampler()
                self.needs_reload = False
                self._initialize_running = False
                return

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
            self._initialize_running = False
            return

        tiles_dir = self._resolve_tiles_dir()
        if not tiles_dir or not os.path.exists(tiles_dir):
            self.error_occurred.emit(
                f"Tiles directory not configured or found: {tiles_dir}"
            )
            self._initialize_running = False
            return

        try:

            def index_callback(_percent, _msg):
                return None

            from TerraLab.terrain.providers import create_raster_provider

            self.provider = create_raster_provider(
                tiles_dir, progress_callback=index_callback
            )
            self._provider_source_path = str(tiles_dir or "")
            self.light_sampler = self._build_light_sampler()

            self.is_initialized = True
        except Exception as exc:
            self.error_occurred.emit(f"Init Error: {exc}")
        finally:
            self._store_progress(None)
            self.progress_message.emit("")
            self._initialize_running = False

    def get_bare_elevation(self, lat: float, lon: float) -> Optional[float]:
        """Obte bare elevation de la instancia de HorizonWorker.

        Par?metres:
        - lat (float): Valor del parametre 'lat'.
        - lon (float): Valor del parametre 'lon'.

        Retorna:
        - Optional[float]: Valor retornat pel metode.
        """
        if not self.is_initialized or not self.provider:
            return None
        try:
            x_utm, y_utm = self.provider.transform_coordinates(lat, lon)
            return self.provider.get_elevation(x_utm, y_utm)
        except Exception as exc:
            print(f"[HorizonWorker] get_bare_elevation error: {exc}")
            return None

    def _estimate_light_pollution_isolated(
        self, lat: float, lon: float
    ) -> tuple[float, int]:
        configured_paths = tuple(
            str(value).strip()
            for value in tuple(
                getattr(self.light_sampler, "raster_paths", ()) or ()
            )
            if str(value).strip()
        )
        if not configured_paths:
            configured_paths = (
                str(getattr(self.light_sampler, "raster_path", "") or "").strip(),
            )
        raster_paths = tuple(
            path for path in configured_paths if path and os.path.isfile(path)
        )
        if not raster_paths:
            return 21.0, 4

        project_root = Path(__file__).resolve().parents[2]
        command = [
            sys.executable,
            "-m",
            "TerraLab.terrain.light_pollution_query",
        ]
        for raster_path in raster_paths:
            command.extend(["--raster", raster_path])
        command.extend([
            "--lat",
            f"{float(lat):.12f}",
            "--lon",
            f"{float(lon):.12f}",
        ])
        completed = subprocess.run(
            command,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            check=False,
            env=os.environ.copy(),
        )

        result_payload = None
        for raw_line in str(completed.stdout or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(_LP_RESULT_PREFIX):
                result_payload = json.loads(line[len(_LP_RESULT_PREFIX) :])
            else:
                print(line)

        stderr_text = str(completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = stderr_text or f"exit code {completed.returncode}"
            raise RuntimeError(f"isolated light-pollution query failed: {detail}")
        if result_payload is None:
            detail = stderr_text or "missing structured result"
            raise RuntimeError(f"isolated light-pollution query failed: {detail}")

        sqm = float(result_payload["sqm"])
        bortle = int(max(1, min(9, int(result_payload["bortle"]))))
        return sqm, bortle

    def get_light_pollution_estimate(
        self, lat: float, lon: float
    ) -> tuple[float, int]:
        if not self.is_initialized or not self.light_sampler:
            return 21.0, 4

        if self._use_isolated_lp_sampler:
            if not self._isolated_lp_sampler_logged:
                print(
                    "[HorizonWorker] Windows/Python>=3.13 detected: "
                    "using isolated light-pollution sampler."
                )
                self._isolated_lp_sampler_logged = True
            try:
                return self._estimate_light_pollution_isolated(lat, lon)
            except Exception as exc:
                print(f"[HorizonWorker] Isolated Bortle estimate error: {exc}")
                return 21.0, 4

        return self.light_sampler.estimate_zenith_sqm(lat, lon)

    def get_bortle_estimate(self, lat: float, lon: float) -> int:
        """Obte bortle estimate de la instancia de HorizonWorker.

        Par?metres:
        - lat (float): Valor del parametre 'lat'.
        - lon (float): Valor del parametre 'lon'.

        Retorna:
        - int: Valor retornat pel metode.
        """
        _, bortle = self.get_light_pollution_estimate(lat, lon)
        return bortle

    def get_sqm_estimate(self, lat: float, lon: float) -> float:
        """Obte sqm estimate de la instancia de HorizonWorker.

        Par?metres:
        - lat (float): Valor del parametre 'lat'.
        - lon (float): Valor del parametre 'lon'.

        Retorna:
        - float: Valor retornat pel metode.
        """
        sqm, _ = self.get_light_pollution_estimate(lat, lon)
        return sqm

    @pyqtSlot(float, float, int)
    def request_bortle_estimate(
        self, lat: float, lon: float, request_id: int = 0
    ) -> None:
        """Calcula Bortle al thread del worker i publica el resultat.

        Parametres:
        - lat (float): Latitud de consulta.
        - lon (float): Longitud de consulta.
        - request_id (int): Identificador de peticio per descartar respostes antigues.

        Retorna:
        - None.
        """
        latitude_deg = 0.0
        longitude_deg = 0.0
        try:
            latitude_deg = float(lat)
            longitude_deg = float(lon)
            auto_bortle_fallback = int(
                max(
                    1,
                    min(
                        9,
                        int(get_config_value("auto_bortle_estimate", 4) or 4),
                    ),
                )
            )

            if bool(self.needs_reload):
                now_mono = float(time.monotonic())
                if (now_mono - float(self._last_reload_pending_log_ts)) >= 2.0:
                    print(
                        "[HorizonWorker] Bortle request served with fallback "
                        "while reload is pending."
                    )
                    self._last_reload_pending_log_ts = now_mono

            if not bool(get_config_value("light_pollution_enabled", True)):
                bortle_value = auto_bortle_fallback
            elif bool(self.is_initialized) and (self.light_sampler is not None):
                _, bortle_value = self.get_light_pollution_estimate(
                    latitude_deg,
                    longitude_deg,
                )
            else:
                bortle_value = auto_bortle_fallback
        except Exception as exc:
            print(f"[HorizonWorker] request_bortle_estimate error: {exc}")
            bortle_value = 4
        self.bortle_estimate_ready.emit(
            int(request_id),
            float(latitude_deg),
            float(longitude_deg),
            int(max(1, min(9, bortle_value))),
        )

    def abort_current_job(self) -> None:
        """Executa el metode abort_current_job de la classe HorizonWorker.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        with self._process_lock:
            proc = self._current_process
            job_id = self._current_job_id
        if proc is None:
            return
        try:
            if proc.poll() is None:
                print(
                    f"[HorizonWorker] Terminating horizon bake job {job_id}..."
                )
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception as exc:
            print(
                f"[HorizonWorker] Warning terminating bake job {job_id}: {exc}"
            )

    def _cleanup_temp_dir(self, temp_dir: Optional[str]) -> None:
        if not temp_dir:
            return
        try:
            import shutil

            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    def _build_subprocess_command(
        self,
        job: dict,
        output_path: str,
        preview_path: str,
        elevation_sources_json: str | None = None,
        light_pollution_sources_json: str | None = None,
    ):
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
            "--ray-step-deg",
            str(float(job.get("ray_step_deg", 0.5))),
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
            "--range-settings-json",
            json.dumps(job.get("range_settings", {}), sort_keys=True),
        ]
        cmd.extend(
            [
                "--representation-mode",
                str(job.get("representation_mode", "relief")),
            ]
        )
        if elevation_sources_json:
            cmd.extend(["--elevation-sources-json", str(elevation_sources_json)])
        for source_id in tuple(job.get("elevation_source_ids", ()) or ()):
            cmd.extend(["--elevation-source-id", str(source_id)])
        if job.get("effective_elevation_source_id"):
            cmd.extend(
                [
                    "--effective-elevation-source-id",
                    str(job["effective_elevation_source_id"]),
                ]
            )
        if job.get("elevation_source_status"):
            cmd.extend(
                ["--elevation-source-status", str(job["elevation_source_status"])]
            )
        if job.get("light_pollution_path"):
            cmd.extend(
                ["--light-pollution-path", str(job["light_pollution_path"])]
            )
        if light_pollution_sources_json:
            cmd.extend(
                [
                    "--light-pollution-sources-json",
                    str(light_pollution_sources_json),
                ]
            )
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
        base = getTraduction(
            "Horizon.CalculatingHorizon", "Calculating horizon: {pct}%"
        ).format(pct=percent_text)
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
        """Executa el metode request_bake de la classe HorizonWorker.

        Par?metres:
        - job (object): Valor del parametre 'job'.

        Retorna:
        - None.
        """
        try:
            if not isinstance(job, dict):
                raise TypeError("Horizon bake job must be a dict")

            # Apply pending reload at a controlled point (before launching a new bake).
            if bool(self.needs_reload):
                self.initialize()

            tiles_dir = self._resolve_tiles_dir()
            if not tiles_dir or not os.path.exists(tiles_dir):
                self.error_occurred.emit(
                    f"Tiles directory not configured or found: {tiles_dir}"
                )
                return

            job = dict(job)
            job.setdefault("observer_offset", float(self.observer_offset))
            job.setdefault("bands", 20)
            job["tiles_dir"] = tiles_dir

            temp_dir = tempfile.mkdtemp(prefix=f"tl_horizon_{job['job_id']}_")
            output_path = os.path.join(temp_dir, "profile_final.npz")
            preview_path = os.path.join(temp_dir, "profile_preview.npz")
            base_dir, cmd = self._build_subprocess_command(
                job, output_path, preview_path
            )
            print(
                "[HorizonWorker] Launching bake subprocess "
                f"job={job['job_id']} "
                f"cwd={base_dir} "
                f"tiles={job['tiles_dir']}"
            )

            self.abort_current_job()
            self._cleanup_temp_dir(self._current_temp_dir)
            with self._process_lock:
                self._current_job_id = str(job["job_id"])
                self._current_temp_dir = temp_dir

            initial_state = {
                # Keep UI progress aligned with the exact subprocess ray grid.
                "job_id": self._current_job_id,
                "phase": "prepare",
                "percent": 0.0,
                "current": 0,
                "total": __import__(
                    "TerraLab.terrain.ray_precision", fromlist=["ray_count"]
                ).ray_count(job.get("ray_step_deg", 0.5)),
            }
            self._emit_progress_state(initial_state)

            subprocess_env = os.environ.copy()
            subprocess_env.setdefault("PYTHONUNBUFFERED", "1")
            proc = subprocess.Popen(
                cmd,
                cwd=base_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=subprocess_env,
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
            band_defs = None
            final_emitted = False
            received_events = 0
            received_progress_events = 0

            assert proc.stdout is not None
            for raw_line in iter(proc.stdout.readline, ""):
                event = self._parse_json_event(raw_line)
                if not event:
                    continue
                if str(event.get("job_id", "")) != active_job_id:
                    continue

                event_type = str(event.get("type", ""))
                received_events += 1
                if event_type == "progress":
                    received_progress_events += 1

                if received_events <= 3:
                    print(
                        "[HorizonWorker] Event "
                        f"job={active_job_id} idx={received_events} type={event_type}"
                    )
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
                    resolved_radius_m = float(event["resolved_radius_m"])
                    if band_defs is None:
                        band_defs = generate_bands(max(1, int(job["bands"])), max_dist_m=resolved_radius_m)
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
                    resolved_radius_m = float(event["resolved_radius_m"])
                    if band_defs is None:
                        band_defs = generate_bands(max(1, int(job["bands"])), max_dist_m=resolved_radius_m)
                    profile_path = str(event.get("profile_path", "") or "")
                    if not profile_path or not os.path.exists(profile_path):
                        raise RuntimeError(
                            "Horizon bake completed without profile output"
                        )
                    profile = HorizonProfile.load(profile_path)
                    profile._band_defs = band_defs
                    self.profile_ready.emit(
                        {"job_id": active_job_id, "profile": profile}
                    )
                    final_emitted = True
                elif event_type == "error":
                    message = str(event.get("message", "Unknown bake error"))
                    raise RuntimeError(message)

            return_code = proc.wait()
            stderr_thread.join(timeout=0.2)
            if received_events == 0:
                print(
                    "[HorizonWorker] Warning: subprocess exited without JSON events "
                    f"job={active_job_id} rc={return_code}"
                )
            else:
                print(
                    "[HorizonWorker] Subprocess finished "
                    f"job={active_job_id} rc={return_code} "
                    f"events={received_events} progress_events={received_progress_events} "
                    f"final_emitted={final_emitted}"
                )
            if return_code != 0 and not final_emitted:
                raise RuntimeError(
                    f"Horizon bake subprocess failed with exit code {return_code}"
                )
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
