"""Typed application inputs used to build one renderer-neutral scene frame."""

from __future__ import annotations

from dataclasses import dataclass, field

from TerraLab.scene.contracts import (
    CameraState,
    ConstellationState,
    EphemerisSnapshot,
    MeasurementState,
    MilkyWayState,
    Observer,
    ResourceVersions,
    SceneTime,
    ScopeSettings,
    ScopeShape,
    SelectionState,
    SurfaceVisualStyle,
    TrailState,
    WeatherState,
)


@dataclass(frozen=True, slots=True)
class LayerIntent:
    stars_enabled: bool = True
    milkyway_enabled: bool = True
    solar_system_enabled: bool = True
    sun_moon_enabled: bool = True
    planets_enabled: bool = True
    grid_enabled: bool = False
    deep_sky_enabled: bool = False


@dataclass(frozen=True, slots=True)
class TerrainIntent:
    horizon_enabled: bool = True
    topography_enabled: bool = False
    surface_enabled: bool = True
    terrain_3d_enabled: bool = False
    light_pollution_enabled: bool = True
    surface_visual_style: SurfaceVisualStyle = SurfaceVisualStyle.ORIGINAL


@dataclass(frozen=True, slots=True)
class LightPollutionIntent:
    mode: str = "automatic"
    automatic_bortle: float = 1.0
    bortle_value: float = 1.0
    magnitude_limit: float = 8.0


@dataclass(frozen=True, slots=True)
class ScopeIntent:
    enabled: bool = False
    center_sky: tuple[float, float] | None = None
    fov_deg: tuple[float, float] = (5.0, 5.0)
    shape: ScopeShape = ScopeShape.CIRCLE
    settings: ScopeSettings = field(default_factory=ScopeSettings)


@dataclass(frozen=True, slots=True)
class PresentationIntent:
    hud_visible: bool = True
    debug_render_metrics: bool = False
    pure_colors: bool = False
    spike_magnitude_threshold: float = 2.0
    star_scale: float = 1.0
    auto_star_scale_multiplier: float = 1.0
    scope_k_fallback: float = 0.2
    interaction_active: bool = False
    naked_eye_cap: float = 8.0


@dataclass(frozen=True, slots=True)
class UserViewState:
    """A deliberate UI intent, never an arbitrary QWidget or its namespace."""

    time: SceneTime
    observer: Observer
    camera: CameraState
    layers: LayerIntent = field(default_factory=LayerIntent)
    terrain: TerrainIntent = field(default_factory=TerrainIntent)
    light_pollution: LightPollutionIntent = field(
        default_factory=LightPollutionIntent
    )
    scope: ScopeIntent = field(default_factory=ScopeIntent)
    milkyway: MilkyWayState = field(default_factory=MilkyWayState)
    trails: TrailState = field(default_factory=TrailState)
    presentation: PresentationIntent = field(
        default_factory=PresentationIntent
    )


@dataclass(frozen=True, slots=True)
class ModelSnapshots:
    """Already-published Model data; no Qt object crosses this boundary."""

    ephemeris: EphemerisSnapshot = field(default_factory=EphemerisSnapshot)
    weather: WeatherState = field(default_factory=WeatherState)
    selection: SelectionState = field(default_factory=SelectionState)
    measurement: MeasurementState = field(default_factory=MeasurementState)
    constellation: ConstellationState = field(
        default_factory=ConstellationState
    )


@dataclass(frozen=True, slots=True)
class SceneFrameInputs:
    """Named grouping for tests and future controller entry points."""

    user_view: UserViewState
    model: ModelSnapshots = field(default_factory=ModelSnapshots)
    resources: ResourceVersions = field(default_factory=ResourceVersions)


@dataclass(frozen=True, slots=True)
class PointerPressedCommand:
    x: float
    y: float
    button: str = "left"
    modifiers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PointerMovedCommand:
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class PointerReleasedCommand:
    x: float
    y: float
    button: str = "left"


@dataclass(frozen=True, slots=True)
class PickRequestedCommand:
    x: float
    y: float
    radius: float = 20.0
    purpose: str = "select"


@dataclass(frozen=True, slots=True)
class MeasurementCommand:
    action: str
    tool: str = "none"
    x: float = 0.0
    y: float = 0.0

