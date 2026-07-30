"""Framework-neutral application controller for TerraLab."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any, Callable

from TerraLab.application.commands import (
    LayerIntent,
    LightPollutionIntent,
    MeasurementCommand,
    PickRequestedCommand,
    PointerMovedCommand,
    PointerPressedCommand,
    PointerReleasedCommand,
    PresentationIntent,
    ScopeIntent,
    TerrainIntent,
)
from TerraLab.application.lifecycle import ApplicationLifecycleManager
from TerraLab.application.scene_builder import SceneFrameBuilder
from TerraLab.application.state import (
    ApplicationState,
    create_initial_application_state,
)
from TerraLab.scene.contracts import (
    CameraState,
    ConstellationState,
    EphemerisSnapshot,
    MeasurementState,
    Observer,
    SceneFrame,
    SceneTime,
    SelectionState,
    Viewport,
    WeatherState,
)

logger = logging.getLogger(__name__)


class ApplicationController:
    """Central framework-neutral application controller.

    Governs state mutations, command execution, lifecycle, and scene frame generation.
    Does NOT import QWidget, QObject, pyqtSignal, or renderer-specific graphics code.
    """

    def __init__(self, initial_state: ApplicationState | None = None) -> None:
        self._state: ApplicationState = (
            initial_state if initial_state is not None else create_initial_application_state()
        )
        self._lifecycle = ApplicationLifecycleManager()
        self._scene_frame_builder = SceneFrameBuilder()
        self._state_listeners: list[Callable[[ApplicationState], None]] = []
        self._frame_listeners: list[Callable[[SceneFrame], None]] = []
        self._generation = 0
        self._viewport = Viewport(width=1280, height=720)
        self._pointer_anchor: tuple[float, float, str] | None = None
        self._pick_request_listeners: list[Callable[[object], None]] = []

    @property
    def state(self) -> ApplicationState:
        return self._state

    @property
    def lifecycle(self) -> ApplicationLifecycleManager:
        return self._lifecycle

    def add_state_listener(self, listener: Callable[[ApplicationState], None]) -> None:
        if listener not in self._state_listeners:
            self._state_listeners.append(listener)

    def remove_state_listener(self, listener: Callable[[ApplicationState], None]) -> None:
        if listener in self._state_listeners:
            self._state_listeners.remove(listener)

    def add_frame_listener(self, listener: Callable[[SceneFrame], None]) -> None:
        if listener not in self._frame_listeners:
            self._frame_listeners.append(listener)

    def remove_frame_listener(self, listener: Callable[[SceneFrame], None]) -> None:
        if listener in self._frame_listeners:
            self._frame_listeners.remove(listener)

    def add_pick_request_listener(self, listener: Callable[[object], None]) -> None:
        if listener not in self._pick_request_listeners:
            self._pick_request_listeners.append(listener)

    def remove_pick_request_listener(self, listener: Callable[[object], None]) -> None:
        if listener in self._pick_request_listeners:
            self._pick_request_listeners.remove(listener)

    def _update_state(self, new_state: ApplicationState) -> None:
        if self._state != new_state:
            self._state = new_state
            self._notify_state_listeners()

    def _notify_state_listeners(self) -> None:
        current = self._state
        for listener in list(self._state_listeners):
            try:
                listener(current)
            except Exception as exc:
                logger.error("Error in state listener: %s", exc)

    def _notify_frame_listeners(self, frame: SceneFrame) -> None:
        for listener in list(self._frame_listeners):
            try:
                listener(frame)
            except Exception as exc:
                logger.error("Error in frame listener: %s", exc)

    # -------------------------------------------------------------------------
    # Frame Construction
    # -------------------------------------------------------------------------

    def build_scene_frame(
        self, viewport: Viewport | None = None
    ) -> SceneFrame:
        self._generation += 1
        if viewport is None:
            viewport = self._viewport
        frame = self._scene_frame_builder.build_from_inputs(
            self._state.to_frame_inputs(),
            viewport=viewport,
            generation=self._generation,
        )
        self._notify_frame_listeners(frame)
        return frame

    def set_viewport(self, width: int, height: int, device_pixel_ratio: float = 1.0) -> None:
        """Store presentation dimensions without treating them as camera state."""

        self._viewport = Viewport(
            width=max(1, int(width)),
            height=max(1, int(height)),
            device_pixel_ratio=max(1.0, float(device_pixel_ratio)),
        )

    def replace_scene_inputs(self, inputs) -> None:
        """Accept a typed input-adapter snapshot and retain application ownership."""

        self._update_state(self._state.with_frame_inputs(inputs))

    # -------------------------------------------------------------------------
    # Command Dispatching
    # -------------------------------------------------------------------------

    def handle_command(self, command: Any) -> None:
        """Route typed command to specific use case handler."""
        if isinstance(command, PointerPressedCommand):
            self.process_pointer_press(command.x, command.y, command.button, command.modifiers)
        elif isinstance(command, PointerMovedCommand):
            self.process_pointer_move(command.x, command.y)
        elif isinstance(command, PointerReleasedCommand):
            self.process_pointer_release(command.x, command.y, command.button)
        elif isinstance(command, PickRequestedCommand):
            self.process_pick_request(command.x, command.y, command.radius, command.purpose)
        elif isinstance(command, MeasurementCommand):
            self.process_measurement_command(command.action, command.tool, command.x, command.y)
        else:
            logger.warning("Unhandled command type: %s", type(command).__name__)

    # -------------------------------------------------------------------------
    # Use Cases: Time & Observer Location
    # -------------------------------------------------------------------------

    def set_time(
        self,
        ut_hour: float | None = None,
        day_of_year_utc: int | None = None,
        year_utc: int | None = None,
    ) -> None:
        current_time = self._state.user_view.time
        new_hour = ut_hour if ut_hour is not None else current_time.ut_hour
        new_doy = (
            day_of_year_utc
            if day_of_year_utc is not None
            else current_time.day_of_year_utc
        )
        new_yr = year_utc if year_utc is not None else current_time.year_utc
        new_scene_time = SceneTime(
            ut_hour=float(new_hour),
            day_of_year_utc=int(new_doy),
            year_utc=int(new_yr),
        )
        self._update_state(self._state.with_time(new_scene_time))

    def set_time_iso(self, utc_iso: str) -> None:
        """Parse a UTC ISO-8601 value at the application boundary."""

        value = str(utc_iso).strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        hour = parsed.hour + parsed.minute / 60.0 + parsed.second / 3600.0
        self.set_time(
            ut_hour=hour,
            day_of_year_utc=parsed.timetuple().tm_yday - 1,
            year_utc=parsed.year,
        )

    def set_observer(
        self,
        latitude_deg: float,
        longitude_deg: float,
        elevation_m: float = 0.0,
    ) -> None:
        new_obs = Observer(
            latitude=float(latitude_deg),
            longitude=float(longitude_deg),
            altitude_m=float(elevation_m),
        )
        self._update_state(self._state.with_observer(new_obs))

    # -------------------------------------------------------------------------
    # Use Cases: Camera
    # -------------------------------------------------------------------------

    def set_camera(
        self,
        azimuth_deg: float | None = None,
        elevation_deg: float | None = None,
        zoom: float | None = None,
        vertical_ratio: float | None = None,
    ) -> None:
        curr = self._state.user_view.camera
        new_az = float(azimuth_deg) if azimuth_deg is not None else curr.azimuth
        new_el = float(elevation_deg) if elevation_deg is not None else curr.elevation
        new_z = float(zoom) if zoom is not None else curr.zoom
        new_vr = float(vertical_ratio) if vertical_ratio is not None else curr.vertical_ratio

        new_cam = CameraState(
            azimuth=new_az,
            elevation=new_el,
            zoom=new_z,
            vertical_ratio=new_vr,
        )
        self._update_state(self._state.with_camera(new_cam))

    def pan_camera(self, delta_azimuth_deg: float, delta_elevation_deg: float) -> None:
        curr = self._state.user_view.camera
        new_az = (curr.azimuth + delta_azimuth_deg) % 360.0
        new_el = max(-90.0, min(90.0, curr.elevation + delta_elevation_deg))
        self.set_camera(azimuth_deg=new_az, elevation_deg=new_el)

    def zoom_camera(self, wheel_steps: float, factor_per_step: float = 1.10) -> None:
        curr = self._state.user_view.camera
        factor = factor_per_step ** float(wheel_steps)
        new_zoom = max(0.01, min(100.0, curr.zoom * factor))
        self.set_camera(zoom=new_zoom)

    # -------------------------------------------------------------------------
    # Use Cases: Layer & Terrain Intents
    # -------------------------------------------------------------------------

    def set_layer_intent(self, intent: LayerIntent) -> None:
        self._update_state(self._state.with_layers(intent))

    def toggle_layer(self, layer_name: str, enabled: bool) -> None:
        curr = self._state.user_view.layers
        kw = {
            "stars_enabled": curr.stars_enabled,
            "milkyway_enabled": curr.milkyway_enabled,
            "solar_system_enabled": curr.solar_system_enabled,
            "sun_moon_enabled": curr.sun_moon_enabled,
            "planets_enabled": curr.planets_enabled,
            "grid_enabled": curr.grid_enabled,
            "deep_sky_enabled": curr.deep_sky_enabled,
        }
        attr = f"{layer_name}_enabled"
        if attr in kw:
            kw[attr] = bool(enabled)
            self.set_layer_intent(LayerIntent(**kw))

    def set_terrain_intent(self, intent: TerrainIntent) -> None:
        self._update_state(self._state.with_terrain(intent))

    def set_light_pollution_intent(self, intent: LightPollutionIntent) -> None:
        self._update_state(self._state.with_light_pollution(intent))

    def set_scope_intent(self, intent: ScopeIntent) -> None:
        self._update_state(self._state.with_scope(intent))

    def set_presentation_intent(self, intent: PresentationIntent) -> None:
        self._update_state(self._state.with_presentation(intent))

    # -------------------------------------------------------------------------
    # Use Cases: Model Snapshots (Selection, Measurement, Ephemeris, Weather)
    # -------------------------------------------------------------------------

    def set_selection(self, selection: SelectionState) -> None:
        self._update_state(self._state.with_selection(selection))

    def set_measurement(self, measurement: MeasurementState) -> None:
        self._update_state(self._state.with_measurement(measurement))

    def set_constellation(self, constellation: ConstellationState) -> None:
        self._update_state(self._state.with_constellation(constellation))

    def set_ephemeris(self, ephemeris: EphemerisSnapshot) -> None:
        self._update_state(self._state.with_ephemeris(ephemeris))

    def set_weather(self, weather: WeatherState) -> None:
        self._update_state(self._state.with_weather(weather))

    # -------------------------------------------------------------------------
    # Interaction Handlers
    # -------------------------------------------------------------------------

    def process_pointer_press(
        self, x: float, y: float, button: str, modifiers: tuple[str, ...]
    ) -> None:
        self._pointer_anchor = (float(x), float(y), str(button))
        intent = replace(
            self._state.user_view.presentation,
            interaction_active=True,
        )
        self.set_presentation_intent(intent)

    def process_pointer_move(self, x: float, y: float) -> None:
        anchor = self._pointer_anchor
        if anchor is None:
            return
        previous_x, previous_y, button = anchor
        if button == "left":
            self.pan_camera(
                -(float(x) - previous_x) * 0.18,
                (float(y) - previous_y) * 0.18,
            )
        self._pointer_anchor = (float(x), float(y), button)

    def process_pointer_release(self, x: float, y: float, button: str) -> None:
        active = self._pointer_anchor
        self._pointer_anchor = None
        intent = replace(
            self._state.user_view.presentation,
            interaction_active=False,
        )
        self.set_presentation_intent(intent)
        if active is not None and str(button) == "left":
            self.process_pick_request(float(x), float(y), 20.0, "select")

    def process_pick_request(
        self, x: float, y: float, radius: float, purpose: str
    ) -> None:
        from TerraLab.application.ports.rendering import PickRequest

        request = PickRequest(
            generation=max(0, self._generation),
            request_id=f"pick:{self._generation}:{purpose}",
            x=float(x),
            y=float(y),
            radius=max(0.0, float(radius)),
            purpose=str(purpose or "select"),
        )
        for listener in tuple(self._pick_request_listeners):
            listener(request)

    def process_pick_result(self, result) -> None:
        """Apply a resolved, backend-independent pick result to application state."""

        payload = result.payload
        purpose = str(payload.get("purpose", "select"))
        if purpose not in {"select", "context"}:
            return
        self.set_selection(
            SelectionState(
                kind=str(payload.get("kind", "")),
                object_type=str(payload.get("type", "")),
                key=str(payload.get("key", payload.get("name", ""))),
                name=str(payload.get("name", "")),
                altitude=(float(payload["alt"]) if payload.get("alt") is not None else None),
                azimuth=(float(payload["az"]) if payload.get("az") is not None else None),
            )
        )

    def process_measurement_command(
        self, action: str, tool: str, x: float, y: float
    ) -> None:
        current = self._state.model.measurement
        if str(action) == "clear":
            self.set_measurement(
                MeasurementState(
                    tool="none",
                    clear_revision=current.clear_revision + 1,
                )
            )
            return
        self.set_measurement(
            MeasurementState(
                tool=str(tool or "none"),
                clear_revision=current.clear_revision,
            )
        )

    # -------------------------------------------------------------------------
    # Lifecycle Controls
    # -------------------------------------------------------------------------

    def start(self) -> None:
        self._lifecycle.start()
        self._update_state(self._state.with_status("RUNNING"))

    def stop(self) -> None:
        self._lifecycle.stop()
        self._update_state(self._state.with_status("STOPPED"))
