"""Encapsulated, framework-neutral application state store for TerraLab."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from TerraLab.application.commands import (
    LayerIntent,
    LightPollutionIntent,
    ModelSnapshots,
    PresentationIntent,
    SceneFrameInputs,
    ScopeIntent,
    TerrainIntent,
    UserViewState,
)
from TerraLab.scene.contracts import (
    CameraState,
    ConstellationState,
    EphemerisSnapshot,
    MeasurementState,
    MilkyWayState,
    Observer,
    ResourceVersions,
    SceneTime,
    SelectionState,
    TrailState,
    WeatherState,
)


@dataclass(frozen=True, slots=True)
class ApplicationState:
    """Immutable snapshot of the entire user view, model data, and application status."""

    user_view: UserViewState
    model: ModelSnapshots = field(default_factory=ModelSnapshots)
    resources: ResourceVersions = field(default_factory=ResourceVersions)
    status: str = "STOPPED"
    error_message: str = ""
    active_workers: Mapping[str, bool] = field(default_factory=dict)

    def to_frame_inputs(self) -> SceneFrameInputs:
        """Convert current state to SceneFrameInputs for scene frame construction."""
        return SceneFrameInputs(
            user_view=self.user_view,
            model=self.model,
            resources=self.resources,
        )

    def with_user_view(self, user_view: UserViewState) -> ApplicationState:
        return replace(self, user_view=user_view)

    def with_time(self, scene_time: SceneTime) -> ApplicationState:
        new_uv = replace(self.user_view, time=scene_time)
        return replace(self, user_view=new_uv)

    def with_observer(self, observer: Observer) -> ApplicationState:
        new_uv = replace(self.user_view, observer=observer)
        return replace(self, user_view=new_uv)

    def with_camera(self, camera: CameraState) -> ApplicationState:
        new_uv = replace(self.user_view, camera=camera)
        return replace(self, user_view=new_uv)

    def with_layers(self, layers: LayerIntent) -> ApplicationState:
        new_uv = replace(self.user_view, layers=layers)
        return replace(self, user_view=new_uv)

    def with_terrain(self, terrain: TerrainIntent) -> ApplicationState:
        new_uv = replace(self.user_view, terrain=terrain)
        return replace(self, user_view=new_uv)

    def with_light_pollution(self, lp: LightPollutionIntent) -> ApplicationState:
        new_uv = replace(self.user_view, light_pollution=lp)
        return replace(self, user_view=new_uv)

    def with_scope(self, scope: ScopeIntent) -> ApplicationState:
        new_uv = replace(self.user_view, scope=scope)
        return replace(self, user_view=new_uv)

    def with_milkyway(self, milkyway: MilkyWayState) -> ApplicationState:
        new_uv = replace(self.user_view, milkyway=milkyway)
        return replace(self, user_view=new_uv)

    def with_trails(self, trails: TrailState) -> ApplicationState:
        new_uv = replace(self.user_view, trails=trails)
        return replace(self, user_view=new_uv)

    def with_presentation(self, presentation: PresentationIntent) -> ApplicationState:
        new_uv = replace(self.user_view, presentation=presentation)
        return replace(self, user_view=new_uv)

    def with_selection(self, selection: SelectionState) -> ApplicationState:
        new_model = replace(self.model, selection=selection)
        return replace(self, model=new_model)

    def with_measurement(self, measurement: MeasurementState) -> ApplicationState:
        new_model = replace(self.model, measurement=measurement)
        return replace(self, model=new_model)

    def with_constellation(self, constellation: ConstellationState) -> ApplicationState:
        new_model = replace(self.model, constellation=constellation)
        return replace(self, model=new_model)

    def with_ephemeris(self, ephemeris: EphemerisSnapshot) -> ApplicationState:
        new_model = replace(self.model, ephemeris=ephemeris)
        return replace(self, model=new_model)

    def with_weather(self, weather: WeatherState) -> ApplicationState:
        new_model = replace(self.model, weather=weather)
        return replace(self, model=new_model)

    def with_status(self, status: str, error_message: str = "") -> ApplicationState:
        return replace(self, status=status, error_message=error_message)

    def with_worker_status(self, worker_name: str, active: bool) -> ApplicationState:
        new_workers = dict(self.active_workers)
        new_workers[worker_name] = active
        return replace(self, active_workers=new_workers)

    def with_frame_inputs(self, inputs: SceneFrameInputs) -> ApplicationState:
        """Replace a typed input snapshot at the application boundary."""

        return replace(
            self,
            user_view=inputs.user_view,
            model=inputs.model,
            resources=inputs.resources,
        )


def create_initial_application_state() -> ApplicationState:
    """Construct a default initial ApplicationState."""
    from datetime import datetime, timezone

    dt = datetime.now(timezone.utc)
    day_of_year = dt.timetuple().tm_yday - 1
    ut_hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    user_view = UserViewState(
        time=SceneTime(
            ut_hour=float(ut_hour),
            day_of_year_utc=int(day_of_year),
            year_utc=int(dt.year),
        ),
        observer=Observer(
            latitude=41.3879,
            longitude=2.1699,
            altitude_m=10.0,
        ),
        camera=CameraState(
            azimuth=180.0,
            elevation=45.0,
            zoom=1.0,
            vertical_ratio=0.0,
        ),
    )
    return ApplicationState(user_view=user_view)
