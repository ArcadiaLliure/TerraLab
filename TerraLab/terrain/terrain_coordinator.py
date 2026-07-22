"""Coordinador de terreny per horitzo.

Orquestra HorizonWorker i exposa snapshots sense dependencia de UI.
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal

from TerraLab.terrain.worker import HorizonWorker


class TerrainCoordinator(QObject):
    """Coordina bake d'horitzo i perfils de preview/final."""

    horizon_ready = pyqtSignal(object)
    horizon_preview_ready = pyqtSignal(object)
    horizon_progress = pyqtSignal(object)
    horizon_error = pyqtSignal(str)

    # Calling a QObject method directly does not honour its thread affinity.
    # These private signals are therefore the only entry points used for work
    # that must run in the terrain thread.
    _initialize_requested = pyqtSignal()
    _bake_requested = pyqtSignal(object)
    _surface_refresh_requested = pyqtSignal(object)

    def __init__(self, tiles_dir: str | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = HorizonWorker(tiles_dir=tiles_dir)
        self._worker.moveToThread(self._thread)

        self._initialize_requested.connect(
            self._worker.initialize, type=Qt.QueuedConnection
        )
        self._bake_requested.connect(
            self._worker.request_bake, type=Qt.QueuedConnection
        )
        self._surface_refresh_requested.connect(
            self._worker.request_surface_refresh, type=Qt.QueuedConnection
        )

        self._worker.profile_ready.connect(self._on_profile_ready)
        self._worker.preview_ready.connect(self._on_preview_ready)
        self._worker.progress_state.connect(self.horizon_progress)
        self._worker.error_occurred.connect(self.horizon_error)

        self._thread.start()

        self._current_profile: Any = None
        self._surface_request_generation = 0
        self._pending_surface_request: dict[str, float | None] | None = None

    def shutdown(self) -> None:
        """Atura el worker de terreny i allibera recursos."""
        try:
            self._worker.cancel_surface_sampling()
            self._worker.abort_current_job()
        except Exception:
            pass
        try:
            self._thread.requestInterruption()
            self._thread.quit()
            if not self._thread.wait(1500):
                self._thread.terminate()
                self._thread.wait(1500)
        except Exception:
            pass
        # No worker code can still be using these resources after the thread
        # has stopped, so final cleanup is safe even though its event loop is
        # no longer available for a queued invocation.
        try:
            self._worker.shutdown()
        except Exception:
            pass

    @property
    def thread(self) -> QThread:
        return self._thread

    def initialize(self) -> None:
        """Inicialitza proveidors del worker en el seu thread."""
        self._initialize_requested.emit()

    def request_bake(self, job: dict[str, Any]) -> None:
        """Envia un job de bake de terreny al worker."""
        self._bake_requested.emit(job)

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
    ) -> None:
        request_context = {
            "visible_radius_m": visible_radius_m,
            "view_azimuth_deg": float(view_azimuth_deg),
            "view_fov_deg": float(view_fov_deg),
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

    def ingest_profile_payload(self, payload: object) -> None:
        """Ingesta un payload de perfil extern i el publica com a final."""
        self._on_profile_ready(payload)

    def ingest_preview_payload(self, payload: object) -> None:
        """Ingesta un payload de previsualització extern."""
        self._on_preview_ready(payload)

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
