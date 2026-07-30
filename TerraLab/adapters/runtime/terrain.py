"""Pure Python runtime adapter for DEM, terrain bake, and surface sampling services."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

from TerraLab.application.ports.terrain import (
    BakeJobRequest,
    ElevationPort,
    SurfaceRefreshContext,
    SurfaceSamplingPort,
    TerrainBakePort,
)
from TerraLab.common.app_paths import cache_dir
from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.utils import get_config_value
from TerraLab.data.source_catalog import DataSourceRegistry, LayerSelectionService
from TerraLab.terrain.persistence.profile_npz import load_profile

_LP_RESULT_PREFIX = "TERRALAB_LP_RESULT="


class RuntimeTerrainAdapter(
    ElevationPort, TerrainBakePort, SurfaceSamplingPort
):
    """Pure Python runtime adapter coordinating DEM, bake subprocess, and surface sampling."""

    def __init__(self, tiles_dir: str | None = None) -> None:
        self.tiles_dir = tiles_dir
        self._shutdown_event = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False
        self.is_initialized = False
        self.provider: Any = None
        self.light_sampler: Any = None
        self.observer_offset = 0.0
        self.needs_reload = False
        self._progress_lock = threading.Lock()
        self._progress_text = ""
        self._progress_state: dict[str, Any] | None = None
        self._process_lock = threading.Lock()
        self._current_process: subprocess.Popen[str] | None = None
        self._current_job_id: str | None = None
        self._current_temp_dir: str | None = None

        self._provider_source_path = ""
        self.data_source_registry = DataSourceRegistry.default()
        self.layer_selection = LayerSelectionService(self.data_source_registry)
        self._light_source_signature: tuple[Any, ...] | None = None
        self._surface_service: Any = None
        self._surface_source_signature: tuple[Any, ...] | None = None
        self._last_profile: Any = None
        self._surface_generation_lock = threading.Lock()
        self._surface_request_generation = 0
        self._surface_cancel_event = threading.Event()

        # Callbacks / Listeners
        self._profile_listeners: list[Callable[[Any], None]] = []
        self._preview_listeners: list[Callable[[Any], None]] = []
        self._progress_listeners: list[Callable[[Any], None]] = []
        self._error_listeners: list[Callable[[str], None]] = []
        self._bortle_listeners: list[Callable[[int, float, float, int], None]] = []
        self._bare_elevation_listeners: list[Callable[[float, float, Any], None]] = []
        self._effective_sources_listeners: list[Callable[[Any], None]] = []

    # --- Listener Registration ---
    def add_profile_listener(self, listener: Callable[[Any], None]) -> None:
        if listener not in self._profile_listeners:
            self._profile_listeners.append(listener)

    def add_preview_listener(self, listener: Callable[[Any], None]) -> None:
        if listener not in self._preview_listeners:
            self._preview_listeners.append(listener)

    def add_progress_listener(self, listener: Callable[[Any], None]) -> None:
        if listener not in self._progress_listeners:
            self._progress_listeners.append(listener)

    def add_error_listener(self, listener: Callable[[str], None]) -> None:
        if listener not in self._error_listeners:
            self._error_listeners.append(listener)

    def add_bortle_listener(
        self, listener: Callable[[int, float, float, int], None]
    ) -> None:
        if listener not in self._bortle_listeners:
            self._bortle_listeners.append(listener)

    def add_bare_elevation_listener(
        self, listener: Callable[[float, float, Any], None]
    ) -> None:
        if listener not in self._bare_elevation_listeners:
            self._bare_elevation_listeners.append(listener)

    def add_effective_sources_listener(
        self, listener: Callable[[Any], None]
    ) -> None:
        if listener not in self._effective_sources_listeners:
            self._effective_sources_listeners.append(listener)

    # --- ElevationPort Implementation ---
    def get_bare_elevation(self, lat: float, lon: float) -> float | None:
        if not self.is_initialized or self.provider is None:
            self.initialize()
        if self.provider is None:
            return None
        try:
            val = self.provider.get_elevation(float(lat), float(lon))
            return float(val) if val is not None else None
        except Exception:
            log_suppressed_exception(__name__, "RuntimeTerrainAdapter.get_bare_elevation")
            return None

    def request_bare_elevation(self, lat: float, lon: float) -> float | None:
        val = self.get_bare_elevation(lat, lon)
        for listener in list(self._bare_elevation_listeners):
            listener(float(lat), float(lon), val)
        return val

    def request_bortle_estimate(
        self, lat: float, lon: float, request_id: int = 0
    ) -> None:
        bortle_class = 4
        sqm = 21.0
        radiance = 0.0
        sampler = self._ensure_light_sampler_for_location(lat, lon)
        if sampler is not None and hasattr(sampler, "sample_location"):
            try:
                res = sampler.sample_location(float(lat), float(lon))
                if isinstance(res, tuple) and len(res) >= 3:
                    bortle_class = int(res[0])
                    sqm = float(res[1])
                    radiance = float(res[2])
            except Exception:
                log_suppressed_exception(__name__, "request_bortle_estimate")
        for listener in list(self._bortle_listeners):
            listener(bortle_class, sqm, radiance, int(request_id))

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
            try:
                old.close()
            except Exception:
                log_suppressed_exception(__name__, "RuntimeTerrainAdapter._ensure_light_sampler")
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

    # --- TerrainBakePort Implementation ---
    def set_observer_offset(self, offset: float) -> None:
        self.observer_offset = float(offset)

    def get_progress_text(self) -> str:
        with self._progress_lock:
            return self._progress_text

    def request_bake(self, job: BakeJobRequest | Mapping[str, Any]) -> None:
        if self._shutdown_event.is_set():
            return
        job_dict = job if isinstance(job, dict) else {
            "job_id": getattr(job, "job_id", f"bake-{int(time.time())}"),
            "observer_lat": getattr(job, "observer_lat", 0.0),
            "observer_lon": getattr(job, "observer_lon", 0.0),
            "observer_offset_m": getattr(job, "observer_offset_m", self.observer_offset),
            "radius_m": getattr(job, "radius_m", 50_000.0),
            "tiles_dir": getattr(job, "tiles_dir", self.tiles_dir),
            **(getattr(job, "parameters", {}) or {}),
        }
        thread = threading.Thread(
            target=self._run_bake_process,
            args=(job_dict,),
            daemon=True,
        )
        thread.start()

    def _run_bake_process(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id", "bake"))
        self.abort_current_job()
        temp_dir = tempfile.mkdtemp(prefix=f"terralab_bake_{job_id}_")
        output_npz = os.path.join(temp_dir, "horizon_profile.npz")
        job["output_path"] = output_npz
        if "observer_offset" not in job:
            job["observer_offset"] = self.observer_offset

        cmd = [
            sys.executable,
            "-m",
            "TerraLab.terrain.bake_process",
            json.dumps(job, default=str),
        ]
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            with self._process_lock:
                self._current_process = process
                self._current_job_id = job_id
                self._current_temp_dir = temp_dir

            assert process.stdout is not None
            for line in process.stdout:
                if self._shutdown_event.is_set():
                    process.terminate()
                    break
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        evt = json.loads(line)
                        self._handle_bake_event(evt, output_npz)
                    except Exception:
                        log_suppressed_exception(__name__, "RuntimeTerrainAdapter._handle_json_bake_event")
            process.wait()
            if process.returncode == 0 and os.path.exists(output_npz):
                try:
                    profile = load_profile(output_npz)
                    profile = self._prepare_surface_samples(profile)
                    self._last_profile = profile
                    self._emit_profile_ready({"job_id": job_id, "profile": profile})
                except Exception as exc:
                    self._emit_error(f"Failed to load baked profile: {exc}")
            elif process.returncode not in (0, -15, -9, 1):
                err = process.stderr.read() if process.stderr else ""
                self._emit_error(f"Bake process failed (code {process.returncode}): {err}")
        except Exception as exc:
            self._emit_error(f"Failed to start bake process: {exc}")

    def _handle_bake_event(self, evt: dict[str, Any], default_npz: str) -> None:
        kind = evt.get("event")
        if kind == "progress":
            self._store_progress(evt)
            for listener in list(self._progress_listeners):
                listener(evt)
        elif kind == "preview":
            path = evt.get("path", default_npz)
            if os.path.exists(path):
                try:
                    preview_prof = load_profile(path)
                    for listener in list(self._preview_listeners):
                        listener({"job_id": evt.get("job_id"), "profile": preview_prof})
                except Exception:
                    log_suppressed_exception(__name__, "RuntimeTerrainAdapter._load_preview_profile")

    def abort_current_job(self) -> None:
        with self._process_lock:
            proc = self._current_process
            self._current_process = None
            self._current_job_id = None
            temp_dir = self._current_temp_dir
            self._current_temp_dir = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                log_suppressed_exception(__name__, "RuntimeTerrainAdapter.abort_current_job_terminate")
        if temp_dir and os.path.exists(temp_dir):
            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                log_suppressed_exception(__name__, "RuntimeTerrainAdapter.abort_current_job_rmtree")

    def reload_config(self) -> None:
        self.needs_reload = True
        self.tiles_dir = None
        self.initialize()

    # --- SurfaceSamplingPort Implementation ---
    def set_surface_request_generation(self, generation: int) -> None:
        with self._surface_generation_lock:
            self._surface_request_generation = int(generation)
            self._surface_cancel_event.clear()

    def cancel_surface_sampling(self) -> None:
        self._surface_cancel_event.set()

    def cancel_surface_refresh(self) -> None:
        self.cancel_surface_sampling()

    def _surface_request_cancelled(self, generation: int) -> bool:
        with self._surface_generation_lock:
            current = self._surface_request_generation
        return (
            self._shutdown_event.is_set()
            or self._surface_cancel_event.is_set()
            or int(generation) != current
        )

    def request_surface_refresh(
        self, context: SurfaceRefreshContext | Mapping[str, Any]
    ) -> None:
        if self._shutdown_event.is_set():
            return
        ctx_dict = context if isinstance(context, dict) else {
            "profile": getattr(context, "profile", None),
            "generation": getattr(context, "generation", 0),
            "visible_radius_m": getattr(context, "visible_radius_m", None),
            "view_azimuth_deg": getattr(context, "view_azimuth_deg", 0.0),
            "view_fov_deg": getattr(context, "view_fov_deg", 360.0),
            "viewport_width_px": getattr(context, "viewport_width_px", None),
            "viewport_height_px": getattr(context, "viewport_height_px", None),
            "surface_mode": getattr(context, "surface_mode", None),
            "atomic_surface_swap": getattr(context, "atomic_surface_swap", False),
        }
        thread = threading.Thread(
            target=self._run_surface_refresh,
            args=(ctx_dict,),
            daemon=True,
        )
        thread.start()

    def _run_surface_refresh(self, req: dict[str, Any]) -> None:
        generation = int(req.get("generation", 0))
        target = req.get("profile") or self._last_profile
        if target is None or self._surface_request_cancelled(generation):
            return
        job_id = f"surface-refresh-{generation}"

        def progress(percent: float, phase: str) -> None:
            evt = {
                "job_id": job_id,
                "kind": "surface",
                "phase": str(phase),
                "percent": float(percent),
                "current": int(round(float(percent))),
                "total": 100,
            }
            for listener in list(self._progress_listeners):
                listener(evt)

        try:
            progress(0.0, "queued")
            complete_profile = copy.copy(target) if req.get("atomic_surface_swap") else target
            complete_profile = self._prepare_surface_samples(
                complete_profile,
                surface_request=req,
                progress_callback=lambda p, ph: progress(float(p), ph),
                abort_check=lambda: self._surface_request_cancelled(generation),
            )
            if self._surface_request_cancelled(generation):
                return
            self._last_profile = complete_profile
            progress(100.0, "completed")
            self._emit_profile_ready(
                {
                    "job_id": job_id,
                    "kind": "surface",
                    "stage": "complete",
                    "profile": complete_profile,
                }
            )
        except InterruptedError:
            return
        except Exception as exc:
            self._emit_error(f"Failed to refresh surface samples: {exc}")

    def _prepare_surface_samples(
        self,
        profile: Any,
        *,
        surface_request: Any = None,
        progress_callback: Any = None,
        abort_check: Any = None,
    ) -> Any:
        if profile is None:
            return None
        if callable(progress_callback):
            progress_callback(1.0, "discovering-sources")
        selection, sources = self._surface_selection(
            float(getattr(profile, "observer_lat", 0.0)),
            float(getattr(profile, "observer_lon", 0.0)),
            surface_mode=(
                surface_request.get("surface_mode")
                if isinstance(surface_request, dict)
                else getattr(surface_request, "surface_mode", None)
            ),
        )
        signature = tuple(
            (
                str(getattr(source, "id", "")),
                str(getattr(source, "path", "")),
                str(getattr(source, "fingerprint", "")),
                str(getattr(getattr(source, "layer_type", ""), "value", "")),
                str(getattr(source, "display_name", "")),
            )
            for source in sources
        )
        if signature != self._surface_source_signature:
            if self._surface_service is not None:
                try:
                    self._surface_service.close()
                except Exception:
                    log_suppressed_exception(__name__, "RuntimeTerrainAdapter._close_surface_service")
            self._surface_service = None
            if sources:
                from TerraLab.terrain.surface import (
                    SurfaceSamplingService,
                    create_surface_providers,
                )

                self._surface_service = SurfaceSamplingService(
                    create_surface_providers(sources),
                    persistent_cache_dir=cache_dir("terrain", "surface"),
                )
            self._surface_source_signature = signature

        if self._surface_service is None:
            profile.surface_samples = None
            return profile

        profile.surface_samples = self._surface_service.sample_profile(
            profile,
            progress_callback=progress_callback,
            abort_check=abort_check,
        )
        return profile

    def _surface_selection(
        self, lat: float, lon: float, *, surface_mode: str | None = None
    ):
        selection = self.layer_selection.select_surface(
            lat, lon, mode=surface_mode
        )
        visible = bool(get_config_value("ui.visibility.earth.surface", True))
        if not visible or selection.effective is None:
            return selection, []
        return selection, list(selection.chain)

    def initialize(self) -> None:
        if self.is_initialized and not self.needs_reload:
            return
        tiles_dir = self.tiles_dir or get_config_value("raster_path", "")
        if tiles_dir and os.path.exists(tiles_dir):
            try:
                from TerraLab.terrain.providers import create_raster_provider

                self.provider = create_raster_provider(tiles_dir)
                self.is_initialized = True
                self.needs_reload = False
            except Exception as exc:
                self._emit_error(f"Failed to initialize DEM provider: {exc}")

    def shutdown(self) -> None:
        self.request_shutdown()
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutdown_complete = True
        for resource in (self._surface_service, self.light_sampler, self.provider):
            if resource is not None and hasattr(resource, "close"):
                try:
                    resource.close()
                except Exception:
                    log_suppressed_exception(__name__, "RuntimeTerrainAdapter.shutdown_resource")

    def request_shutdown(self) -> None:
        self._shutdown_event.set()
        self.cancel_surface_sampling()
        self.abort_current_job()

    def _store_progress(self, state: dict[str, Any] | None) -> None:
        with self._progress_lock:
            self._progress_state = dict(state) if state else None
            self._progress_text = str(state.get("message", "")) if state else ""

    def _emit_profile_ready(self, payload: Any) -> None:
        for listener in list(self._profile_listeners):
            listener(payload)

    def _emit_error(self, message: str) -> None:
        for listener in list(self._error_listeners):
            listener(message)
