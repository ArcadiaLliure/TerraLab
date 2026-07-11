"""Coordinador de terreny per horitzo.

Orquestra HorizonWorker i exposa snapshots sense dependencia de UI.
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from TerraLab.terrain.worker import HorizonWorker


class TerrainCoordinator(QObject):
    """Coordina bake d'horitzo i perfils de preview/final."""

    horizon_ready = pyqtSignal(object)
    horizon_preview_ready = pyqtSignal(object)
    horizon_progress = pyqtSignal(object)
    horizon_error = pyqtSignal(str)

    def __init__(self, tiles_dir: str | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = HorizonWorker(tiles_dir=tiles_dir)
        self._worker.moveToThread(self._thread)

        self._worker.profile_ready.connect(self._on_profile_ready)
        self._worker.preview_ready.connect(self._on_preview_ready)
        self._worker.progress_state.connect(self.horizon_progress)
        self._worker.error_occurred.connect(self.horizon_error)

        self._thread.start()

        self._current_profile: Any = None

    def shutdown(self) -> None:
        """Atura el worker de terreny i allibera recursos."""
        try:
            self._worker.abort_current_job()
        except Exception:
            pass
        try:
            self._thread.quit()
            self._thread.wait(1500)
        except Exception:
            pass

    def initialize(self) -> None:
        """Inicialitza proveidors del worker en el seu thread."""
        self._worker.initialize()

    def request_bake(self, job: dict[str, Any]) -> None:
        """Envia un job de bake de terreny al worker."""
        self._worker.request_bake(job)

    def abort_current_job(self) -> None:
        """Demana cancelacio del bake en curs."""
        self._worker.abort_current_job()

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

    def _on_preview_ready(self, payload: object) -> None:
        self.horizon_preview_ready.emit(payload)
