"""Qt presentation adapter bridging Qt widgets to pure ApplicationController."""

from __future__ import annotations

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from TerraLab.application.commands import (
    LayerIntent,
    LightPollutionIntent,
    PresentationIntent,
    ScopeIntent,
    TerrainIntent,
)
from TerraLab.application.controller import ApplicationController
from TerraLab.application.state import ApplicationState
from TerraLab.scene.contracts import SceneFrame, Viewport


class QtApplicationControllerAdapter(QObject):
    """Bridge adapter exposing Qt signals and slots for ApplicationController."""

    state_changed = pyqtSignal(object)  # ApplicationState
    frame_ready = pyqtSignal(object)  # SceneFrame
    status_changed = pyqtSignal(str, str)  # status, error_message

    def __init__(
        self,
        controller: ApplicationController | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = (
            controller if controller is not None else ApplicationController()
        )
        self._controller.add_state_listener(self._on_controller_state_changed)
        self._controller.add_frame_listener(self._on_controller_frame_ready)

    @property
    def controller(self) -> ApplicationController:
        return self._controller

    @property
    def state(self) -> ApplicationState:
        return self._controller.state

    def _on_controller_state_changed(self, state: ApplicationState) -> None:
        self.state_changed.emit(state)
        self.status_changed.emit(state.status, state.error_message)

    def _on_controller_frame_ready(self, frame: SceneFrame) -> None:
        self.frame_ready.emit(frame)

    # -------------------------------------------------------------------------
    # Qt Slots mapping user intentions to application commands
    # -------------------------------------------------------------------------

    @pyqtSlot(object)
    def dispatch_command(self, command: object) -> None:
        self._controller.handle_command(command)

    @pyqtSlot(str)
    def set_time_iso(self, utc_iso: str) -> None:
        self._controller.set_time_iso(utc_iso)

    @pyqtSlot(float, float)
    def set_observer_lat_lon(self, lat: float, lon: float) -> None:
        self._controller.set_observer(latitude_deg=lat, longitude_deg=lon)

    @pyqtSlot(float, float)
    def pan_camera(self, delta_azimuth: float, delta_elevation: float) -> None:
        self._controller.pan_camera(delta_azimuth, delta_elevation)

    @pyqtSlot(float)
    def zoom_camera(self, wheel_steps: float) -> None:
        self._controller.zoom_camera(wheel_steps)

    @pyqtSlot(int, int)
    def set_viewport_size(self, width: int, height: int) -> None:
        self._controller.set_viewport(width, height)

    @pyqtSlot(str, bool)
    def toggle_layer(self, layer_name: str, enabled: bool) -> None:
        self._controller.toggle_layer(layer_name, enabled)

    @pyqtSlot(object)
    def set_layer_intent(self, intent: LayerIntent) -> None:
        self._controller.set_layer_intent(intent)

    @pyqtSlot(object)
    def set_terrain_intent(self, intent: TerrainIntent) -> None:
        self._controller.set_terrain_intent(intent)

    @pyqtSlot(object)
    def set_light_pollution_intent(self, intent: LightPollutionIntent) -> None:
        self._controller.set_light_pollution_intent(intent)

    @pyqtSlot(object)
    def set_scope_intent(self, intent: ScopeIntent) -> None:
        self._controller.set_scope_intent(intent)

    @pyqtSlot(object)
    def set_presentation_intent(self, intent: PresentationIntent) -> None:
        self._controller.set_presentation_intent(intent)

    def request_scene_frame(self, viewport: Viewport | None = None) -> SceneFrame:
        return self._controller.build_scene_frame(viewport)
