"""Qt presentation adapter bridging terrain application ports and runtime adapters for Qt View components."""

from __future__ import annotations

from typing import Any, Mapping

from PyQt5.QtCore import QObject, pyqtSignal

from TerraLab.application.ports.terrain import (
    BakeJobRequest,
    SurfaceRefreshContext,
)


class QtTerrainCoordinatorAdapter(QObject):
    """Qt signal bridge wrapping pure terrain ports / runtime adapters."""

    horizon_ready = pyqtSignal(object)
    horizon_preview_ready = pyqtSignal(object)
    horizon_progress = pyqtSignal(object)
    horizon_error = pyqtSignal(str)
    bortle_estimate_ready = pyqtSignal(int, float, float, int)
    bare_elevation_ready = pyqtSignal(float, float, object)
    effective_sources_changed = pyqtSignal(object)

    def __init__(
        self,
        adapter: Any,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._adapter = adapter

        if hasattr(adapter, "add_profile_listener"):
            adapter.add_profile_listener(self._on_profile_ready)
        if hasattr(adapter, "add_preview_listener"):
            adapter.add_preview_listener(self._on_preview_ready)
        if hasattr(adapter, "add_progress_listener"):
            adapter.add_progress_listener(self._on_progress)
        if hasattr(adapter, "add_error_listener"):
            adapter.add_error_listener(self._on_error)
        if hasattr(adapter, "add_bortle_listener"):
            adapter.add_bortle_listener(self._on_bortle_ready)
        if hasattr(adapter, "add_bare_elevation_listener"):
            adapter.add_bare_elevation_listener(self._on_bare_elevation_ready)
        if hasattr(adapter, "add_effective_sources_listener"):
            adapter.add_effective_sources_listener(
                self._on_effective_sources_changed
            )

    def initialize(self) -> None:
        if hasattr(self._adapter, "initialize"):
            self._adapter.initialize()

    def request_bake(self, job: BakeJobRequest | Mapping[str, Any]) -> None:
        if hasattr(self._adapter, "request_bake"):
            self._adapter.request_bake(job)

    def reload_config(self) -> None:
        if hasattr(self._adapter, "reload_config"):
            self._adapter.reload_config()

    def set_observer_offset(self, offset: float) -> None:
        if hasattr(self._adapter, "set_observer_offset"):
            self._adapter.set_observer_offset(offset)

    def get_progress_text(self) -> str:
        if hasattr(self._adapter, "get_progress_text"):
            return str(self._adapter.get_progress_text())
        return ""

    def get_bare_elevation(self, lat: float, lon: float) -> float | None:
        if hasattr(self._adapter, "get_bare_elevation"):
            return self._adapter.get_bare_elevation(lat, lon)
        return None

    def request_bortle_estimate(
        self, lat: float, lon: float, request_id: int = 0
    ) -> None:
        if hasattr(self._adapter, "request_bortle_estimate"):
            self._adapter.request_bortle_estimate(lat, lon, request_id)

    def abort_current_job(self) -> None:
        if hasattr(self._adapter, "abort_current_job"):
            self._adapter.abort_current_job()

    def cancel_surface_refresh(self) -> None:
        if hasattr(self._adapter, "cancel_surface_refresh"):
            self._adapter.cancel_surface_refresh()

    def request_surface_refresh(
        self, context: SurfaceRefreshContext | Mapping[str, Any]
    ) -> None:
        if hasattr(self._adapter, "request_surface_refresh"):
            self._adapter.request_surface_refresh(context)

    def shutdown(self) -> None:
        if hasattr(self._adapter, "shutdown"):
            self._adapter.shutdown()

    # --- Signal Emitters ---
    def _on_profile_ready(self, payload: Any) -> None:
        self.horizon_ready.emit(payload)

    def _on_preview_ready(self, payload: Any) -> None:
        self.horizon_preview_ready.emit(payload)

    def _on_progress(self, payload: Any) -> None:
        self.horizon_progress.emit(payload)

    def _on_error(self, message: str) -> None:
        self.horizon_error.emit(message)

    def _on_bortle_ready(
        self, bortle_class: int, sqm: float, radiance: float, request_id: int
    ) -> None:
        self.bortle_estimate_ready.emit(bortle_class, sqm, radiance, request_id)

    def _on_bare_elevation_ready(
        self, lat: float, lon: float, elevation: Any
    ) -> None:
        self.bare_elevation_ready.emit(lat, lon, elevation)

    def _on_effective_sources_changed(self, payload: Any) -> None:
        self.effective_sources_changed.emit(payload)
