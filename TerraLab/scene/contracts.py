"""Qt-free, immutable scene contracts and the legacy-v1 compatibility shape."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType, NoneType
from typing import TypeAlias


JSONScalar: TypeAlias = NoneType | bool | int | float | str
JSONValue: TypeAlias = (
    JSONScalar
    | list["JSONValue"]
    | tuple["JSONValue", ...]
    | Mapping[str, "JSONValue"]
)


class Layer(str, Enum):
    """The stable ordering names accepted by protocol v1."""

    STARS = "stars"
    MILKYWAY = "milkyway"
    SUN_MOON = "sun_moon"
    PLANETS = "planets"
    SOLAR_SYSTEM = "solar_system"
    TERRAIN = "terrain"
    GRID = "grid"
    DEEP_SKY = "deep_sky"


class LightPollutionMode(str, Enum):
    AUTOMATIC = "automatic"
    BORTLE = "bortle"
    MAGNITUDE = "magnitude"


class ScopeShape(str, Enum):
    CIRCLE = "circle"
    RECTANGLE = "rectangle"


class SurfaceVisualStyle(str, Enum):
    ORIGINAL = "original"
    VIBRANT = "vibrant"


def _finite(name: str, value: float) -> float:
    resolved = float(value)
    if not math.isfinite(resolved):
        raise ValueError(f"{name} must be finite")
    return resolved


def _finite_pair(name: str, value: tuple[float, float]) -> tuple[float, float]:
    return (_finite(f"{name}[0]", value[0]), _finite(f"{name}[1]", value[1]))


def freeze_json_mapping(
    payload: Mapping[str, object],
) -> Mapping[str, JSONValue]:
    """Freeze one Model/runtime snapshot and reject non-JSON or nonfinite data."""

    if not all(isinstance(key, str) for key in payload):
        raise ValueError("Scene snapshots require string mapping keys")
    return MappingProxyType(
        {str(key): _freeze_json_value(value) for key, value in payload.items()}
    )


def thaw_json_mapping(
    payload: Mapping[str, JSONValue],
) -> dict[str, JSONValue]:
    """Materialize only at a JSON/process boundary."""

    return {
        str(key): _thaw_json_value(value) for key, value in payload.items()
    }


def _freeze_json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return _finite("JSON value", value)
    if isinstance(value, Mapping):
        return freeze_json_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    raise ValueError(
        "Scene snapshots must contain JSON-compatible values, "
        f"not {type(value).__name__}"
    )


def _thaw_json_value(value: JSONValue) -> JSONValue:
    if isinstance(value, Mapping):
        return thaw_json_mapping(value)
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, (list, tuple)) else ()


def _optional_pair(value: object) -> tuple[float, float] | None:
    values = _sequence(value)
    if len(values) < 2:
        return None
    return (
        _finite("coordinate[0]", float(values[0])),
        _finite("coordinate[1]", float(values[1])),
    )


@dataclass(frozen=True, slots=True)
class Viewport:
    """Pixel dimensions of a renderer output surface without a Qt dependency."""

    width: int
    height: int
    device_pixel_ratio: float = 1.0

    def __post_init__(self) -> None:
        if int(self.width) <= 0 or int(self.height) <= 0:
            raise ValueError("Viewport dimensions must be positive")
        if (
            _finite("Viewport device pixel ratio", self.device_pixel_ratio)
            <= 0.0
        ):
            raise ValueError("Viewport device pixel ratio must be positive")


@dataclass(frozen=True, slots=True)
class SceneTime:
    ut_hour: float
    day_of_year_utc: int
    year_utc: int

    def __post_init__(self) -> None:
        hour = _finite("ut_hour", self.ut_hour)
        if not 0.0 <= hour < 24.0:
            raise ValueError("ut_hour must be in [0, 24)")
        if not 0 <= int(self.day_of_year_utc) <= 365:
            raise ValueError("day_of_year_utc must be in [0, 365]")
        if not 1 <= int(self.year_utc) <= 9999:
            raise ValueError("year_utc must be a valid year")


@dataclass(frozen=True, slots=True)
class Observer:
    latitude: float
    longitude: float
    altitude_m: float = 0.0

    def __post_init__(self) -> None:
        latitude = _finite("latitude", self.latitude)
        longitude = _finite("longitude", self.longitude)
        _finite("altitude_m", self.altitude_m)
        if not -90.0 <= latitude <= 90.0:
            raise ValueError("latitude must be in [-90, 90]")
        if not -360.0 <= longitude <= 360.0:
            raise ValueError("longitude must be in [-360, 360]")


@dataclass(frozen=True, slots=True)
class CameraState:
    azimuth: float
    elevation: float
    zoom: float
    vertical_ratio: float

    def __post_init__(self) -> None:
        _finite("camera.azimuth", self.azimuth)
        elevation = _finite("camera.elevation", self.elevation)
        zoom = _finite("camera.zoom", self.zoom)
        _finite("camera.vertical_ratio", self.vertical_ratio)
        if not -90.0 <= elevation <= 90.0:
            raise ValueError("camera.elevation must be in [-90, 90]")
        if zoom <= 0.0:
            raise ValueError("camera.zoom must be positive")


@dataclass(frozen=True, slots=True)
class EarthLayerState:
    """Effective, coherent Earth-layer visibility for one scene frame."""

    horizon_enabled: bool
    topography_enabled: bool
    surface_enabled: bool
    terrain_3d_enabled: bool
    light_pollution_enabled: bool

    def __post_init__(self) -> None:
        if not self.horizon_enabled and any(
            (
                self.topography_enabled,
                self.surface_enabled,
                self.terrain_3d_enabled,
                self.light_pollution_enabled,
            )
        ):
            raise ValueError("Earth child layers require the horizon layer")
        if not self.topography_enabled and (
            self.surface_enabled or self.terrain_3d_enabled
        ):
            raise ValueError("Surface and 3D terrain require topography")


@dataclass(frozen=True, slots=True)
class LayerState:
    order: tuple[Layer, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.order)) != len(self.order):
            raise ValueError("Scene layers must not contain duplicates")


@dataclass(frozen=True, slots=True)
class ScopeSettings:
    focal_mm: float = 250.0
    sensor_profile: str = "tiny"
    instrument_profile: str = "telescope"
    aperture_input_mode: str = "diameter_mm"
    aperture_mm: float = 80.0
    aperture_f_number: float = 4.0
    eyepiece_mm: float = 20.0
    eye_pupil_dark_mm: float = 6.5
    atmospheric_loss_mag: float = 0.0
    exposure_s: float = 2.0
    iso: int = 800
    dataset_max_mag: float = 8.0
    catalog_mag_sorted: bool = True

    def __post_init__(self) -> None:
        for name in (
            "focal_mm",
            "aperture_mm",
            "aperture_f_number",
            "eyepiece_mm",
            "eye_pupil_dark_mm",
            "atmospheric_loss_mag",
            "exposure_s",
            "dataset_max_mag",
        ):
            _finite(f"scope.{name}", getattr(self, name))
        if (
            min(
                self.focal_mm,
                self.aperture_mm,
                self.aperture_f_number,
                self.eyepiece_mm,
                self.eye_pupil_dark_mm,
                self.exposure_s,
            )
            <= 0.0
        ):
            raise ValueError(
                "Scope optical dimensions and exposure must be positive"
            )
        if int(self.iso) < 1:
            raise ValueError("scope.iso must be positive")
        if self.aperture_input_mode not in {"diameter_mm", "f_number"}:
            raise ValueError("Unsupported scope aperture input mode")
        if self.instrument_profile not in {
            "telescope",
            "camera_aps_c",
            "camera_full_frame",
        }:
            raise ValueError("Unsupported scope instrument profile")
        if not self.sensor_profile:
            raise ValueError("scope.sensor_profile must not be empty")


@dataclass(frozen=True, slots=True)
class ScopeState:
    enabled: bool = False
    center_sky: tuple[float, float] | None = None
    fov_deg: tuple[float, float] = (5.0, 5.0)
    shape: ScopeShape = ScopeShape.CIRCLE
    settings: ScopeSettings = field(default_factory=ScopeSettings)

    def __post_init__(self) -> None:
        fov = _finite_pair("scope.fov_deg", self.fov_deg)
        if fov[0] <= 0.0 or fov[1] <= 0.0:
            raise ValueError("scope.fov_deg must be positive")
        if self.center_sky is not None:
            _finite_pair("scope.center_sky", self.center_sky)


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """A resource identity. Its bytes remain owned by the referenced artifact."""

    path: str | None = None
    version: str = ""
    handle: str = ""


@dataclass(frozen=True, slots=True)
class CatalogResource:
    catalog_path: str = ""
    r_path: str = ""
    g_path: str = ""
    b_path: str = ""
    version: str = ""


@dataclass(frozen=True, slots=True)
class ResourceVersions:
    catalog: CatalogResource = field(default_factory=CatalogResource)
    ngc: ResourceRef = field(default_factory=ResourceRef)
    terrain_profile: ResourceRef = field(default_factory=ResourceRef)
    terrain_surface: ResourceRef = field(default_factory=ResourceRef)
    milkyway_texture: ResourceRef = field(default_factory=ResourceRef)
    dust_map: ResourceRef = field(default_factory=ResourceRef)


@dataclass(frozen=True, slots=True)
class TerrainState:
    visibility: EarthLayerState
    surface_visual_style: SurfaceVisualStyle = SurfaceVisualStyle.ORIGINAL


@dataclass(frozen=True, slots=True)
class WeatherState:
    enabled: bool = False
    latitude: float = 0.0
    longitude: float = 0.0
    use_remote: bool = False
    cache_enabled: bool = False
    user_agent: str = ""
    bortle: int = 1
    sample_artifact: Mapping[str, JSONValue] = field(
        default_factory=lambda: MappingProxyType({})
    )
    sample_revision: int = 0

    def __post_init__(self) -> None:
        _finite("weather.latitude", self.latitude)
        _finite("weather.longitude", self.longitude)
        if not 1 <= int(self.bortle) <= 9:
            raise ValueError("weather.bortle must be in [1, 9]")
        if int(self.sample_revision) < 0:
            raise ValueError("weather.sample_revision cannot be negative")
        object.__setattr__(
            self, "sample_artifact", freeze_json_mapping(self.sample_artifact)
        )


@dataclass(frozen=True, slots=True)
class EphemerisSnapshot:
    """Published Model snapshot; its scientific fields remain process-owned."""

    payload: Mapping[str, JSONValue] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json_mapping(self.payload))


@dataclass(frozen=True, slots=True)
class SelectionStar:
    name: str = ""
    magnitude: float | None = None

    def __post_init__(self) -> None:
        if self.magnitude is not None:
            _finite("selection.star.magnitude", self.magnitude)


@dataclass(frozen=True, slots=True)
class SelectionState:
    kind: str = ""
    object_type: str = ""
    key: str = ""
    name: str = ""
    altitude: float | None = None
    azimuth: float | None = None
    screen_distance: float | None = None
    magnitude: float | None = None
    ra: float | None = None
    dec: float | None = None
    star: SelectionStar | None = None

    def __post_init__(self) -> None:
        for name in (
            "altitude",
            "azimuth",
            "screen_distance",
            "magnitude",
            "ra",
            "dec",
        ):
            value = getattr(self, name)
            if value is not None:
                _finite(f"selection.{name}", value)


@dataclass(frozen=True, slots=True)
class MeasurementState:
    tool: str = "none"
    clear_revision: int = 0

    def __post_init__(self) -> None:
        if int(self.clear_revision) < 0:
            raise ValueError("measurement.clear_revision cannot be negative")


@dataclass(frozen=True, slots=True)
class ConstellationNodeState:
    ra: float
    dec: float
    star_id: str = ""
    star_name: str = ""
    connect: bool = True

    def __post_init__(self) -> None:
        _finite("constellation.node.ra", self.ra)
        _finite("constellation.node.dec", self.dec)


@dataclass(frozen=True, slots=True)
class ConstellationGroupState:
    name: str
    nodes: tuple[ConstellationNodeState, ...] = ()


@dataclass(frozen=True, slots=True)
class ConstellationState:
    data_path: str = ""
    enabled: bool = False
    visible: bool = True
    groups: tuple[ConstellationGroupState, ...] = ()
    active_group_index: int | None = None
    selected_group_index: int | None = None
    selected_node_index: int | None = None
    selected_segment_index: int | None = None
    selected_segments: tuple[tuple[int, int], ...] = ()
    selected_group_indices: tuple[int, ...] = ()
    group_drawing_active: bool = False
    resume_from_node_index: int | None = None
    preview_ra_dec: tuple[float, float] | None = None
    preview_snapped: bool = False

    def __post_init__(self) -> None:
        if self.preview_ra_dec is not None:
            _finite_pair("constellation.preview_ra_dec", self.preview_ra_dec)


@dataclass(frozen=True, slots=True)
class MilkyWayState:
    enabled: bool = True
    opacity: float = 0.65
    blend_mode: str = "add"
    ra_offset_deg: float = 180.0
    coord_frame: str = "galactic"
    lat_flip: bool = True
    lon_flip: bool = True
    sample_scale: float = 1.0
    dust_map_enabled: bool = False
    dust_density_strength: float = 0.0
    dust_extinction_strength: float = 0.65

    def __post_init__(self) -> None:
        for name in (
            "opacity",
            "ra_offset_deg",
            "sample_scale",
            "dust_density_strength",
            "dust_extinction_strength",
        ):
            _finite(f"milkyway.{name}", getattr(self, name))
        if not self.blend_mode or not self.coord_frame:
            raise ValueError(
                "Milky Way blend mode and coordinate frame are required"
            )


@dataclass(frozen=True, slots=True)
class TrailState:
    enabled: bool = False
    start_hour: float | None = None

    def __post_init__(self) -> None:
        if self.start_hour is not None:
            hour = _finite("trails.start_hour", self.start_hour)
            if not 0.0 <= hour < 24.0:
                raise ValueError("trails.start_hour must be in [0, 24)")


@dataclass(frozen=True, slots=True)
class PresentationState:
    hud_visible: bool = True
    debug_render_metrics: bool = False
    pure_colors: bool = False
    spike_magnitude_threshold: float = 2.0
    star_scale: float = 1.0
    auto_star_scale_multiplier: float = 1.0
    scope_k_fallback: float = 0.2
    interaction_active: bool = False
    naked_eye_cap: float = 8.0

    def __post_init__(self) -> None:
        for name in (
            "spike_magnitude_threshold",
            "star_scale",
            "auto_star_scale_multiplier",
            "scope_k_fallback",
            "naked_eye_cap",
        ):
            _finite(f"presentation.{name}", getattr(self, name))


@dataclass(frozen=True, slots=True)
class SceneFrameDelta:
    """Typed delta frame containing only modified fields relative to a base frame generation."""

    generation: int
    base_generation: int
    viewport: Viewport | None = None
    time: SceneTime | None = None
    observer: Observer | None = None
    camera: CameraState | None = None
    layers: LayerState | None = None
    light_pollution_mode: LightPollutionMode | None = None
    bortle: int | None = None
    magnitude_limit: float | None = None
    terrain: TerrainState | None = None
    scope: ScopeState | None = None
    resources: ResourceVersions | None = None
    weather: WeatherState | None = None
    ephemeris: EphemerisSnapshot | None = None
    selection: SelectionState | None = None
    measurement: MeasurementState | None = None
    constellation: ConstellationState | None = None
    milkyway: MilkyWayState | None = None
    trails: TrailState | None = None
    presentation: PresentationState | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if int(self.generation) < 0:
            raise ValueError("Scene frame delta generation cannot be negative")
        if int(self.base_generation) < 0:
            raise ValueError("Scene frame delta base_generation cannot be negative")
        if int(self.schema_version) != 1:
            raise ValueError(f"Unsupported scene frame delta schema: {self.schema_version}")


@dataclass(frozen=True, slots=True)
class SceneFrame:
    """Typed, immutable frame with one deterministic protocol-v1 adapter."""

    generation: int
    viewport: Viewport
    time: SceneTime
    observer: Observer
    camera: CameraState
    layers: LayerState
    light_pollution_mode: LightPollutionMode
    bortle: int
    magnitude_limit: float
    terrain: TerrainState
    scope: ScopeState
    resources: ResourceVersions
    weather: WeatherState
    ephemeris: EphemerisSnapshot
    selection: SelectionState
    measurement: MeasurementState
    constellation: ConstellationState
    milkyway: MilkyWayState
    trails: TrailState
    presentation: PresentationState
    schema_version: int = 1

    def __post_init__(self) -> None:
        if int(self.generation) < 0:
            raise ValueError("Scene frame generation cannot be negative")
        if int(self.schema_version) != 1:
            raise ValueError(
                f"Unsupported scene frame schema: {self.schema_version}"
            )
        if not 1 <= int(self.bortle) <= 9:
            raise ValueError("bortle must be in [1, 9]")
        _finite("magnitude_limit", self.magnitude_limit)

    def with_generation(self, generation: int) -> "SceneFrame":
        return replace(self, generation=int(generation))

    def diff_from(self, base: "SceneFrame") -> "SceneFrameDelta":
        """Construct a delta containing only the fields that differ from base."""
        return SceneFrameDelta(
            generation=self.generation,
            base_generation=base.generation,
            viewport=self.viewport if self.viewport != base.viewport else None,
            time=self.time if self.time != base.time else None,
            observer=self.observer if self.observer != base.observer else None,
            camera=self.camera if self.camera != base.camera else None,
            layers=self.layers if self.layers != base.layers else None,
            light_pollution_mode=self.light_pollution_mode if self.light_pollution_mode != base.light_pollution_mode else None,
            bortle=self.bortle if self.bortle != base.bortle else None,
            magnitude_limit=self.magnitude_limit if self.magnitude_limit != base.magnitude_limit else None,
            terrain=self.terrain if self.terrain != base.terrain else None,
            scope=self.scope if self.scope != base.scope else None,
            resources=self.resources if self.resources != base.resources else None,
            weather=self.weather if self.weather != base.weather else None,
            ephemeris=self.ephemeris if self.ephemeris != base.ephemeris else None,
            selection=self.selection if self.selection != base.selection else None,
            measurement=self.measurement if self.measurement != base.measurement else None,
            constellation=self.constellation if self.constellation != base.constellation else None,
            milkyway=self.milkyway if self.milkyway != base.milkyway else None,
            trails=self.trails if self.trails != base.trails else None,
            presentation=self.presentation if self.presentation != base.presentation else None,
            schema_version=self.schema_version,
        )

    def apply_delta(self, delta: "SceneFrameDelta") -> "SceneFrame":
        """Apply a SceneFrameDelta to construct an updated SceneFrame."""
        if delta.base_generation != self.generation:
            raise ValueError(
                f"Delta base_generation ({delta.base_generation}) does not match current frame generation ({self.generation})"
            )
        return replace(
            self,
            generation=delta.generation,
            viewport=delta.viewport if delta.viewport is not None else self.viewport,
            time=delta.time if delta.time is not None else self.time,
            observer=delta.observer if delta.observer is not None else self.observer,
            camera=delta.camera if delta.camera is not None else self.camera,
            layers=delta.layers if delta.layers is not None else self.layers,
            light_pollution_mode=delta.light_pollution_mode if delta.light_pollution_mode is not None else self.light_pollution_mode,
            bortle=delta.bortle if delta.bortle is not None else self.bortle,
            magnitude_limit=delta.magnitude_limit if delta.magnitude_limit is not None else self.magnitude_limit,
            terrain=delta.terrain if delta.terrain is not None else self.terrain,
            scope=delta.scope if delta.scope is not None else self.scope,
            resources=delta.resources if delta.resources is not None else self.resources,
            weather=delta.weather if delta.weather is not None else self.weather,
            ephemeris=delta.ephemeris if delta.ephemeris is not None else self.ephemeris,
            selection=delta.selection if delta.selection is not None else self.selection,
            measurement=delta.measurement if delta.measurement is not None else self.measurement,
            constellation=delta.constellation if delta.constellation is not None else self.constellation,
            milkyway=delta.milkyway if delta.milkyway is not None else self.milkyway,
            trails=delta.trails if delta.trails is not None else self.trails,
            presentation=delta.presentation if delta.presentation is not None else self.presentation,
        )

    def to_legacy_snapshot(self) -> dict[str, JSONValue]:
        """Encode this DTO graph to the unchanged protocol-v1 payload."""

        catalog = self.resources.catalog
        terrain = self.terrain.visibility
        weather = {
            "enabled": self.weather.enabled,
            "latitude": self.weather.latitude,
            "longitude": self.weather.longitude,
            "use_remote": self.weather.use_remote,
            "cache_enabled": self.weather.cache_enabled,
            "user_agent": self.weather.user_agent,
            "bortle": self.weather.bortle,
            "sample_artifact": thaw_json_mapping(self.weather.sample_artifact),
            "sample_revision": self.weather.sample_revision,
        }
        scope = self.scope
        settings = scope.settings
        extras: dict[str, JSONValue] = {
            "catalog_mag_sorted": settings.catalog_mag_sorted,
            "scope_instrument_profile": settings.instrument_profile,
            "scope_iso": settings.iso,
            "scope_focal_mm": settings.focal_mm,
            "scope_sensor_profile": settings.sensor_profile,
            "scope_aperture_input_mode": settings.aperture_input_mode,
            "scope_aperture_mm": settings.aperture_mm,
            "scope_eyepiece_mm": settings.eyepiece_mm,
            "scope_eye_pupil_dark_mm": settings.eye_pupil_dark_mm,
            "scope_dataset_max_mag": settings.dataset_max_mag,
            "scope_atmospheric_loss_mag": settings.atmospheric_loss_mag,
            "scope_exposure_s": settings.exposure_s,
            "scope_aperture_f_number": settings.aperture_f_number,
        }
        selection = _selection_payload(self.selection)
        constellation = _constellation_payload(self.constellation)
        milkyway: dict[str, JSONValue] = {
            "enabled": self.milkyway.enabled,
            "opacity": self.milkyway.opacity,
            "blend_mode": self.milkyway.blend_mode,
            "ra_offset_deg": self.milkyway.ra_offset_deg,
            "coord_frame": self.milkyway.coord_frame,
            "lat_flip": self.milkyway.lat_flip,
            "lon_flip": self.milkyway.lon_flip,
            "sample_scale": self.milkyway.sample_scale,
            "dust_map_enabled": self.milkyway.dust_map_enabled,
            "dust_density_strength": self.milkyway.dust_density_strength,
            "dust_extinction_strength": self.milkyway.dust_extinction_strength,
        }
        if self.resources.milkyway_texture.path is not None:
            milkyway["texture_path"] = self.resources.milkyway_texture.path
            milkyway["texture_revision"] = (
                self.resources.milkyway_texture.version
            )
        if self.resources.dust_map.path is not None:
            milkyway["dust_map_path"] = self.resources.dust_map.path
            milkyway["dust_map_revision"] = self.resources.dust_map.version
        return {
            "camera": {
                "azimuth": self.camera.azimuth,
                "elevation": self.camera.elevation,
                "zoom": self.camera.zoom,
                "vertical_ratio": self.camera.vertical_ratio,
            },
            "ut_hour": self.time.ut_hour,
            "day_of_year_utc": self.time.day_of_year_utc,
            "year_utc": self.time.year_utc,
            "latitude": self.observer.latitude,
            "longitude": self.observer.longitude,
            "altitude_m": self.observer.altitude_m,
            "magnitude_limit": self.magnitude_limit,
            "naked_eye_cap": self.presentation.naked_eye_cap,
            "bortle": self.bortle,
            "light_pollution_mode": self.light_pollution_mode.value,
            "light_pollution_enabled": terrain.light_pollution_enabled,
            "pure_colors": self.presentation.pure_colors,
            "spike_magnitude_threshold": self.presentation.spike_magnitude_threshold,
            "star_scale": self.presentation.star_scale,
            "auto_star_scale_multiplier": self.presentation.auto_star_scale_multiplier,
            "scope_k_fallback": self.presentation.scope_k_fallback,
            "scope_enabled": scope.enabled,
            "scope_center_sky": list(scope.center_sky)
            if scope.center_sky
            else None,
            "scope_fov_deg": list(scope.fov_deg),
            "scope_shape": scope.shape.value,
            "interaction_active": self.presentation.interaction_active,
            "debug_render_metrics": self.presentation.debug_render_metrics,
            "hud_visible": self.presentation.hud_visible,
            "layers": [layer.value for layer in self.layers.order],
            "trails": {
                "enabled": self.trails.enabled,
                "start_hour": self.trails.start_hour,
            },
            "catalog": {
                "catalog_path": catalog.catalog_path,
                "r_path": catalog.r_path,
                "g_path": catalog.g_path,
                "b_path": catalog.b_path,
            },
            "ngc": {
                "catalog_path": self.resources.ngc.path,
                "revision": self.resources.ngc.version,
            },
            "terrain": {
                "profile_path": self.resources.terrain_profile.path,
                "surface_path": self.resources.terrain_surface.path,
                "terrain_3d_enabled": terrain.terrain_3d_enabled,
                "topography_enabled": terrain.topography_enabled,
                "horizon_enabled": terrain.horizon_enabled,
                "surface_enabled": terrain.surface_enabled,
                "surface_visual_style": self.terrain.surface_visual_style.value,
            },
            "weather": weather,
            "ephemeris": thaw_json_mapping(self.ephemeris.payload),
            "selection": selection,
            "measurement": {
                "tool": self.measurement.tool,
                "clear_revision": self.measurement.clear_revision,
            },
            "constellation": constellation,
            "milkyway": milkyway,
            "extras": extras,
        }

    @classmethod
    def from_legacy_snapshot(
        cls,
        generation: int,
        snapshot: Mapping[str, object],
        *,
        viewport: Viewport | None = None,
    ) -> "SceneFrame":
        """Decode a v1 payload for externally produced legacy messages."""

        raw = _mapping(snapshot)
        camera = _mapping(raw.get("camera"))
        terrain = _mapping(raw.get("terrain"))
        extras = _mapping(raw.get("extras"))
        layers = tuple(
            Layer(str(name)) for name in _sequence(raw.get("layers"))
        )
        horizon = bool(terrain.get("horizon_enabled", True))
        topography = bool(horizon and terrain.get("topography_enabled", True))
        earth = EarthLayerState(
            horizon_enabled=horizon,
            topography_enabled=topography,
            surface_enabled=bool(
                topography and terrain.get("surface_enabled", True)
            ),
            terrain_3d_enabled=bool(
                topography and terrain.get("terrain_3d_enabled", True)
            ),
            light_pollution_enabled=bool(
                horizon and raw.get("light_pollution_enabled", True)
            ),
        )
        scope_fov = _optional_pair(raw.get("scope_fov_deg")) or (5.0, 5.0)
        scope_settings = ScopeSettings(
            focal_mm=float(extras.get("scope_focal_mm", 250.0)),
            sensor_profile=str(extras.get("scope_sensor_profile", "tiny")),
            instrument_profile=str(
                extras.get("scope_instrument_profile", "telescope")
            ),
            aperture_input_mode=str(
                extras.get("scope_aperture_input_mode", "diameter_mm")
            ),
            aperture_mm=float(extras.get("scope_aperture_mm", 80.0)),
            aperture_f_number=float(
                extras.get("scope_aperture_f_number", 4.0)
            ),
            eyepiece_mm=float(extras.get("scope_eyepiece_mm", 20.0)),
            eye_pupil_dark_mm=float(
                extras.get("scope_eye_pupil_dark_mm", 6.5)
            ),
            atmospheric_loss_mag=float(
                extras.get("scope_atmospheric_loss_mag", 0.0)
            ),
            exposure_s=float(extras.get("scope_exposure_s", 2.0)),
            iso=int(extras.get("scope_iso", 800)),
            dataset_max_mag=float(extras.get("scope_dataset_max_mag", 8.0)),
            catalog_mag_sorted=bool(extras.get("catalog_mag_sorted", True)),
        )
        catalog = _mapping(raw.get("catalog"))
        ngc = _mapping(raw.get("ngc"))
        weather = _mapping(raw.get("weather"))
        milkyway = _mapping(raw.get("milkyway"))
        trails = _mapping(raw.get("trails"))
        return cls(
            generation=int(generation),
            viewport=viewport or Viewport(1, 1),
            time=SceneTime(
                float(raw.get("ut_hour", 12.0)),
                int(raw.get("day_of_year_utc", 0)),
                int(raw.get("year_utc", 2026)),
            ),
            observer=Observer(
                float(raw.get("latitude", 0.0)),
                float(raw.get("longitude", 0.0)),
                float(raw.get("altitude_m", 0.0)),
            ),
            camera=CameraState(
                float(camera.get("azimuth", 0.0)),
                float(camera.get("elevation", 40.0)),
                float(camera.get("zoom", 1.0)),
                float(camera.get("vertical_ratio", 0.3)),
            ),
            layers=LayerState(layers),
            light_pollution_mode=LightPollutionMode(
                str(raw.get("light_pollution_mode", "automatic"))
            ),
            bortle=int(raw.get("bortle", 1)),
            magnitude_limit=float(raw.get("magnitude_limit", 8.0)),
            terrain=TerrainState(
                earth,
                SurfaceVisualStyle(
                    str(terrain.get("surface_visual_style", "original"))
                ),
            ),
            scope=ScopeState(
                enabled=bool(raw.get("scope_enabled", False)),
                center_sky=_optional_pair(raw.get("scope_center_sky")),
                fov_deg=scope_fov,
                shape=ScopeShape(str(raw.get("scope_shape", "circle"))),
                settings=scope_settings,
            ),
            resources=ResourceVersions(
                catalog=CatalogResource(
                    catalog_path=str(catalog.get("catalog_path", "")),
                    r_path=str(catalog.get("r_path", "")),
                    g_path=str(catalog.get("g_path", "")),
                    b_path=str(catalog.get("b_path", "")),
                ),
                ngc=ResourceRef(
                    path=str(ngc.get("catalog_path", "")),
                    version=str(ngc.get("revision", "")),
                ),
                terrain_profile=ResourceRef(
                    str(terrain.get("profile_path", ""))
                ),
                terrain_surface=ResourceRef(
                    str(terrain.get("surface_path", ""))
                ),
                milkyway_texture=(
                    ResourceRef(
                        str(milkyway["texture_path"]),
                        str(milkyway.get("texture_revision", "")),
                    )
                    if "texture_path" in milkyway
                    else ResourceRef()
                ),
                dust_map=(
                    ResourceRef(
                        str(milkyway["dust_map_path"]),
                        str(milkyway.get("dust_map_revision", "")),
                    )
                    if "dust_map_path" in milkyway
                    else ResourceRef()
                ),
            ),
            weather=WeatherState(
                enabled=bool(weather.get("enabled", False)),
                latitude=float(weather.get("latitude", 0.0)),
                longitude=float(weather.get("longitude", 0.0)),
                use_remote=bool(weather.get("use_remote", False)),
                cache_enabled=bool(weather.get("cache_enabled", False)),
                user_agent=str(weather.get("user_agent", "")),
                bortle=int(weather.get("bortle", 1)),
                sample_artifact=freeze_json_mapping(
                    _mapping(weather.get("sample_artifact"))
                ),
                sample_revision=int(weather.get("sample_revision", 0)),
            ),
            ephemeris=EphemerisSnapshot(
                freeze_json_mapping(_mapping(raw.get("ephemeris")))
            ),
            selection=_selection_from_legacy(_mapping(raw.get("selection"))),
            measurement=MeasurementState(
                str(_mapping(raw.get("measurement")).get("tool", "none")),
                int(_mapping(raw.get("measurement")).get("clear_revision", 0)),
            ),
            constellation=_constellation_from_legacy(
                _mapping(raw.get("constellation"))
            ),
            milkyway=MilkyWayState(
                enabled=bool(milkyway.get("enabled", True)),
                opacity=float(milkyway.get("opacity", 0.65)),
                blend_mode=str(milkyway.get("blend_mode", "add")),
                ra_offset_deg=float(milkyway.get("ra_offset_deg", 180.0)),
                coord_frame=str(milkyway.get("coord_frame", "galactic")),
                lat_flip=bool(milkyway.get("lat_flip", True)),
                lon_flip=bool(milkyway.get("lon_flip", True)),
                sample_scale=float(milkyway.get("sample_scale", 1.0)),
                dust_map_enabled=bool(milkyway.get("dust_map_enabled", False)),
                dust_density_strength=float(
                    milkyway.get("dust_density_strength", 0.0)
                ),
                dust_extinction_strength=float(
                    milkyway.get("dust_extinction_strength", 0.65)
                ),
            ),
            trails=TrailState(
                enabled=bool(trails.get("enabled", False)),
                start_hour=(
                    float(trails["start_hour"])
                    if trails.get("start_hour") is not None
                    else None
                ),
            ),
            presentation=PresentationState(
                hud_visible=bool(raw.get("hud_visible", True)),
                debug_render_metrics=bool(
                    raw.get("debug_render_metrics", False)
                ),
                pure_colors=bool(raw.get("pure_colors", False)),
                spike_magnitude_threshold=float(
                    raw.get("spike_magnitude_threshold", 2.0)
                ),
                star_scale=float(raw.get("star_scale", 1.0)),
                auto_star_scale_multiplier=float(
                    raw.get("auto_star_scale_multiplier", 1.0)
                ),
                scope_k_fallback=float(raw.get("scope_k_fallback", 0.2)),
                interaction_active=bool(raw.get("interaction_active", False)),
                naked_eye_cap=float(raw.get("naked_eye_cap", 8.0)),
            ),
        )


def _selection_payload(selection: SelectionState) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {
        "kind": selection.kind,
        "type": selection.object_type,
        "key": selection.key,
        "name": selection.name,
    }
    for source, target in (
        ("altitude", "alt"),
        ("azimuth", "az"),
        ("screen_distance", "screen_distance"),
        ("magnitude", "mag"),
        ("ra", "ra"),
        ("dec", "dec"),
    ):
        value = getattr(selection, source)
        if value is not None:
            result[target] = value
    if selection.star is not None:
        star: dict[str, JSONValue] = {"name": selection.star.name}
        if selection.star.magnitude is not None:
            star["mag"] = selection.star.magnitude
        result["star"] = star
    return result


def _selection_from_legacy(raw: Mapping[str, object]) -> SelectionState:
    star = _mapping(raw.get("star"))
    star_mag = star.get("mag")
    return SelectionState(
        kind=str(raw.get("kind", "")),
        object_type=str(raw.get("type", "")),
        key=str(raw.get("key", "")),
        name=str(raw.get("name", "")),
        altitude=_optional_float(raw.get("alt")),
        azimuth=_optional_float(raw.get("az")),
        screen_distance=_optional_float(raw.get("screen_distance")),
        magnitude=_optional_float(raw.get("mag")),
        ra=_optional_float(raw.get("ra")),
        dec=_optional_float(raw.get("dec")),
        star=(
            SelectionStar(str(star.get("name", "")), _optional_float(star_mag))
            if star
            else None
        ),
    )


def _optional_float(value: object) -> float | None:
    return None if value is None else _finite("optional value", float(value))


def _constellation_payload(state: ConstellationState) -> dict[str, JSONValue]:
    return {
        "data_path": state.data_path,
        "enabled": state.enabled,
        "visible": state.visible,
        "groups": [
            {
                "name": group.name,
                "nodes": [
                    {
                        "ra": node.ra,
                        "dec": node.dec,
                        "star_id": node.star_id,
                        "star_name": node.star_name,
                        "connect": node.connect,
                    }
                    for node in group.nodes
                ],
            }
            for group in state.groups
        ],
        "active_group_index": state.active_group_index,
        "selected_group_index": state.selected_group_index,
        "selected_node_index": state.selected_node_index,
        "selected_segment_index": state.selected_segment_index,
        "selected_segments": [
            list(segment) for segment in state.selected_segments
        ],
        "selected_group_indices": list(state.selected_group_indices),
        "group_drawing_active": state.group_drawing_active,
        "resume_from_node_index": state.resume_from_node_index,
        "preview_ra_dec": list(state.preview_ra_dec)
        if state.preview_ra_dec
        else None,
        "preview_snapped": state.preview_snapped,
    }


def _constellation_from_legacy(
    raw: Mapping[str, object],
) -> ConstellationState:
    groups: list[ConstellationGroupState] = []
    for group_raw in _sequence(raw.get("groups")):
        group = _mapping(group_raw)
        nodes: list[ConstellationNodeState] = []
        for node_raw in _sequence(group.get("nodes")):
            node = _mapping(node_raw)
            nodes.append(
                ConstellationNodeState(
                    float(node.get("ra", 0.0)),
                    float(node.get("dec", 0.0)),
                    str(node.get("star_id", "")),
                    str(node.get("star_name", "")),
                    bool(node.get("connect", True)),
                )
            )
        groups.append(
            ConstellationGroupState(str(group.get("name", "")), tuple(nodes))
        )
    segments = tuple(
        (int(values[0]), int(values[1]))
        for value in _sequence(raw.get("selected_segments"))
        if len(values := _sequence(value)) >= 2
    )
    indices = tuple(
        int(value) for value in _sequence(raw.get("selected_group_indices"))
    )
    return ConstellationState(
        data_path=str(raw.get("data_path", "")),
        enabled=bool(raw.get("enabled", False)),
        visible=bool(raw.get("visible", True)),
        groups=tuple(groups),
        active_group_index=_optional_int(raw.get("active_group_index")),
        selected_group_index=_optional_int(raw.get("selected_group_index")),
        selected_node_index=_optional_int(raw.get("selected_node_index")),
        selected_segment_index=_optional_int(
            raw.get("selected_segment_index")
        ),
        selected_segments=segments,
        selected_group_indices=indices,
        group_drawing_active=bool(raw.get("group_drawing_active", False)),
        resume_from_node_index=_optional_int(
            raw.get("resume_from_node_index")
        ),
        preview_ra_dec=_optional_pair(raw.get("preview_ra_dec")),
        preview_snapped=bool(raw.get("preview_snapped", False)),
    )


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)
