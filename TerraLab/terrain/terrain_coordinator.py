"""Coordinador de terreny per horitzo.

Orquestra HorizonWorker i exposa snapshots sense dependencia de UI.
"""

from __future__ import annotations

import time
from typing import Any

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from TerraLab.terrain.worker import HorizonWorker


def create_process_horizon_worker(
    tiles_dir: str | None = None,
    *,
    quit_thread_on_shutdown: bool = False,
) -> HorizonWorker:
    """Create the domain worker for a process-owned execution context."""

    return HorizonWorker(
        tiles_dir=tiles_dir,
        quit_thread_on_shutdown=quit_thread_on_shutdown,
    )


class TerrainCoordinator(QObject):
    """Coordina bake d'horitzo i perfils de preview/final."""

    horizon_ready = pyqtSignal(object)
    horizon_preview_ready = pyqtSignal(object)
    horizon_progress = pyqtSignal(object)
    horizon_error = pyqtSignal(str)
    bortle_estimate_ready = pyqtSignal(int, float, float, int)
    bare_elevation_ready = pyqtSignal(float, float, object)

    # Calling a QObject method directly does not honour its thread affinity.
    # These private signals are therefore the only entry points used for work
    # that must run in the terrain thread.
    _initialize_requested = pyqtSignal()
    _bake_requested = pyqtSignal(object)
    _surface_refresh_requested = pyqtSignal(object)
    _reload_requested = pyqtSignal()
    _observer_offset_requested = pyqtSignal(float)
    _bortle_requested = pyqtSignal(float, float, int)
    _bare_elevation_requested = pyqtSignal(float, float)
    _shutdown_cleanup_requested = pyqtSignal()

    def __init__(self, tiles_dir: str | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._current_profile: Any = None
        self._surface_request_generation = 0
        self._pending_surface_request: dict[str, Any] | None = None
        self._observer_offset = 0.0
        self._progress_text = ""
        self._elevation_cache: dict[tuple[float, float], float | None] = {}
        self._pending_elevation_queries: set[tuple[float, float]] = set()
        self._shutdown_started = False
        self._shutdown_complete = False

        self._thread = QThread(self)
        self._worker = create_process_horizon_worker(
            tiles_dir,
            quit_thread_on_shutdown=True,
        )
        self._worker.moveToThread(self._thread)

        # AutoConnection becomes queued because the receiver has worker-thread
        # affinity. Keeping one-argument ``connect`` calls also matches PyQt's
        # published type stubs.
        self._initialize_requested.connect(self._worker.initialize)
        self._bake_requested.connect(self._worker.request_bake)
        self._surface_refresh_requested.connect(
            self._worker.request_surface_refresh
        )
        self._reload_requested.connect(self._worker.reload_config)
        self._observer_offset_requested.connect(
            self._worker.set_observer_offset
        )
        self._bortle_requested.connect(
            self._worker.request_bortle_estimate
        )
        self._bare_elevation_requested.connect(
            self._worker.request_bare_elevation
        )
        self._shutdown_cleanup_requested.connect(self._worker.shutdown)

        self._worker.profile_ready.connect(self._on_profile_ready)
        self._worker.preview_ready.connect(self._on_preview_ready)
        self._worker.progress_state.connect(self.horizon_progress)
        self._worker.progress_message.connect(self._on_progress_message)
        self._worker.error_occurred.connect(self.horizon_error)
        self._worker.bortle_estimate_ready.connect(self.bortle_estimate_ready)
        self._worker.bare_elevation_ready.connect(
            self._on_bare_elevation_ready
        )

        self._thread.start()

    def shutdown(self, timeout_ms: int = 15_000) -> None:
        """Cooperatively stop work, close resources, and join the Qt thread."""

        if self._shutdown_complete:
            return
        if not self._shutdown_started:
            self._shutdown_started = True
            # ``request_shutdown`` is an explicitly synchronized cancellation
            # interface: it only sets Events and controls the lock-protected
            # subprocess. Resource cleanup itself remains queued.
            self._worker.request_shutdown()
            self._thread.requestInterruption()
            self._shutdown_cleanup_requested.emit()

        deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
        next_diagnostic = time.monotonic() + 1.0
        while self._thread.isRunning():
            remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise RuntimeError(
                    "Terrain worker did not stop cooperatively within "
                    f"{int(timeout_ms)} ms; progress={self._progress_text!r}"
                )
            self._thread.wait(min(250, remaining_ms))
            now = time.monotonic()
            if self._thread.isRunning() and now >= next_diagnostic:
                print(
                    "[TerrainCoordinator] Waiting for cooperative shutdown: "
                    f"progress={self._progress_text!r}, "
                    f"remaining_ms={remaining_ms}"
                )
                next_diagnostic = now + 1.0
        self._shutdown_complete = True

    @property
    def worker_thread(self) -> QThread:
        """Return the dedicated terrain thread for diagnostics and tests."""

        return self._thread

    def initialize(self) -> None:
        """Inicialitza proveidors del worker en el seu thread."""
        self._initialize_requested.emit()

    def request_bake(self, job: dict[str, Any]) -> None:
        """Envia un job de bake de terreny al worker."""
        self._bake_requested.emit(job)

    def reload_config(self) -> None:
        """Queue provider reconfiguration on the terrain thread."""

        self._reload_requested.emit()

    def set_observer_offset(self, offset: float) -> None:
        """Queue an observer-height update on the terrain thread."""

        self._observer_offset = float(offset)
        self._observer_offset_requested.emit(self._observer_offset)

    @property
    def observer_offset(self) -> float:
        return self._observer_offset

    def get_progress_text(self) -> str:
        """Return the latest progress snapshot published to the coordinator."""

        return self._progress_text

    def get_bare_elevation(self, lat: float, lon: float) -> float | None:
        """Return a cached elevation and queue any missing lookup."""

        key = (float(lat), float(lon))
        if key not in self._elevation_cache:
            if key not in self._pending_elevation_queries:
                self._pending_elevation_queries.add(key)
                self._bare_elevation_requested.emit(*key)
            return None
        return self._elevation_cache[key]

    def request_bortle_estimate(
        self, lat: float, lon: float, request_id: int = 0
    ) -> None:
        """Queue one light-pollution estimate on the terrain thread."""

        self._bortle_requested.emit(
            float(lat),
            float(lon),
            int(request_id),
        )

    def abort_current_job(self) -> None:
        """Demana cancelacio del bake en curs."""
        self._worker.cancel_surface_sampling()
        self._worker.abort_current_job()

    def cancel_surface_refresh(self) -> None:
        """Cancel any queued or in-flight surface sampling request."""
        self._pending_surface_request = None
        self._worker.cancel_surface_sampling()

    def request_surface_refresh(
        self,
        profile: object | None = None,
        *,
        visible_radius_m: float | None = None,
        view_azimuth_deg: float = 0.0,
        view_fov_deg: float = 360.0,
        viewport_width_px: int | None = None,
        viewport_height_px: int | None = None,
        surface_mode: str | None = None,
        surface_layer_type: str | None = None,
        atomic_surface_swap: bool = False,
    ) -> None:
        request_context = {
            "visible_radius_m": visible_radius_m,
            "view_azimuth_deg": float(view_azimuth_deg),
            "view_fov_deg": float(view_fov_deg),
            "viewport_width_px": viewport_width_px,
            "viewport_height_px": viewport_height_px,
            "surface_mode": str(
                surface_mode or surface_layer_type or ""
            ).strip()
            or None,
            "atomic_surface_swap": bool(atomic_surface_swap),
        }
        target = profile or self._current_profile
        if target is None:
            self._pending_surface_request = request_context
            return
        self._pending_surface_request = None
        self._surface_request_generation += 1
        generation = self._surface_request_generation
        # This setter only changes a lock-protected generation counter.  It is
        # intentionally immediate so an in-flight raster loop can observe the
        # superseding request without waiting for the worker event queue.
        self._worker.set_surface_request_generation(generation)
        self._surface_refresh_requested.emit(
            {
                "profile": target,
                "generation": generation,
                **request_context,
            }
        )

    def get_profile(self) -> object | None:
        """Retorna l'ultim perfil final disponible."""
        return self._current_profile

    def _on_profile_ready(self, payload: object) -> None:
        profile_obj = payload
        if isinstance(payload, dict):
            profile_obj = payload.get("profile")
        self._current_profile = profile_obj
        self.horizon_ready.emit(payload)
        pending = self._pending_surface_request
        if profile_obj is not None and pending is not None:
            self._pending_surface_request = None
            self.request_surface_refresh(profile_obj, **pending)

    def _on_preview_ready(self, payload: object) -> None:
        self.horizon_preview_ready.emit(payload)

    def _on_progress_message(self, message: str) -> None:
        self._progress_text = str(message)

    def _on_bare_elevation_ready(
        self,
        lat: float,
        lon: float,
        elevation: object,
    ) -> None:
        key = (float(lat), float(lon))
        if elevation is None:
            value = None
        elif isinstance(elevation, (int, float)):
            value = float(elevation)
        else:
            raise TypeError(
                "TerrainWorker returned a non-numeric bare elevation: "
                f"{type(elevation).__name__}"
            )
        self._pending_elevation_queries.discard(key)
        self._elevation_cache[key] = value
        self.bare_elevation_ready.emit(key[0], key[1], value)
