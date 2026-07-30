"""Renderer-neutral informational-overlay and text-layout plans.

This module owns only deterministic geometry, text content and collision
policy.  Font measurement is deliberately supplied by the application layer:
the Model never guesses metrics from a rendering toolkit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol, Sequence

from TerraLab.scene.projection import project_universal_stereo_point
from TerraLab.scene.render_state import RenderState


RGBA = tuple[int, int, int, int]
ScreenPoint = tuple[float, float]
TextAnchor = Literal["baseline_left", "baseline_center"]


def _camera_cache_key(state: RenderState) -> tuple[float, float, float, float]:
    """Keep layout-cache keys independent of the mutable Camera object."""

    camera = state.camera
    return (
        float(camera.azimuth_offset),
        float(camera.elevation_angle),
        float(camera.zoom_level),
        float(camera.vertical_offset_ratio),
    )


@dataclass(frozen=True, slots=True)
class ScreenRect:
    """A numeric screen-space rectangle independent of any graphics toolkit."""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def expanded(self, horizontal: float, vertical: float) -> "ScreenRect":
        return ScreenRect(
            self.x - horizontal,
            self.y - vertical,
            self.width + 2.0 * horizontal,
            self.height + 2.0 * vertical,
        )

    def intersects(self, other: "ScreenRect") -> bool:
        return (
            self.x < other.right
            and self.right > other.x
            and self.y < other.bottom
            and self.bottom > other.y
        )

    def overlaps(self, other: "ScreenRect") -> bool:
        return self.intersects(other)


@dataclass(frozen=True, slots=True)
class FontStyle:
    """Logical font selection; concrete fallback is a View responsibility."""

    family: str = "Arial"
    pixel_size: float = 12.0
    weight: int = 400
    italic: bool = False


@dataclass(frozen=True, slots=True)
class TextStyle:
    """Presentation values already selected by the Model for one label."""

    font: FontStyle
    foreground_rgba: RGBA
    background_rgba: RGBA | None = None
    background_padding_x: float = 0.0
    background_padding_y: float = 0.0
    corner_radius_px: float = 0.0


@dataclass(frozen=True, slots=True)
class TextMetrics:
    """Measured logical text dimensions returned by the application port."""

    advance_px: float
    ascent_px: float
    descent_px: float
    leading_px: float = 0.0

    @property
    def height_px(self) -> float:
        return max(0.0, self.ascent_px + self.descent_px + self.leading_px)


class TextMetricsProvider(Protocol):
    """Structural dependency supplied by a Controller-owned font-metrics port."""

    @property
    def revision(self) -> str: ...

    def measure_text(self, text: str, style: TextStyle) -> TextMetrics: ...


class PlanetLabelSource(Protocol):
    """Projected planet data required for a label candidate only."""

    key: str
    screen_x: float
    screen_y: float
    radius_px: float
    rgba: RGBA
    label: str


class DeepSkyLabelSource(Protocol):
    """Projected deep-sky glyph data required for a label candidate only."""

    name: str
    screen_x: float
    screen_y: float
    radius_x_px: float
    rgba: RGBA


@dataclass(frozen=True, slots=True)
class TextCandidate:
    """A candidate whose placement is known but whose bounds need measuring."""

    text: str
    position: ScreenPoint
    anchor: TextAnchor
    style: TextStyle
    priority: int
    clip: ScreenRect | None
    z_index: int
    pick_id: str | None = None


@dataclass(frozen=True, slots=True)
class TextLabel:
    """A measured, collision-resolved item ready for a presentation adapter."""

    text: str
    position: ScreenPoint
    anchor: TextAnchor
    style: TextStyle
    priority: int
    clip: ScreenRect | None
    z_index: int
    bounds: ScreenRect
    pick_id: str | None = None


@dataclass(frozen=True, slots=True)
class TextLayoutStats:
    """Data-only observability for the label planner and its cache."""

    candidates: int
    accepted: int
    rejected_collision: int
    rejected_clip: int
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class TextBatch:
    """Resolved labels in paint order with stable hit and clipping metadata."""

    labels: tuple[TextLabel, ...]
    stats: TextLayoutStats
    cache_key: tuple[object, ...]

    def __bool__(self) -> bool:
        return bool(self.labels)


@dataclass(frozen=True, slots=True)
class StrokeStyle:
    rgba: RGBA
    width_px: float = 1.0
    dashed: bool = False


@dataclass(frozen=True, slots=True)
class LineSegment:
    start: ScreenPoint
    end: ScreenPoint
    style: StrokeStyle
    z_index: int = 0


@dataclass(frozen=True, slots=True)
class GridPlan:
    """Projected celestial grid paths and their resolved visual style."""

    paths: tuple[tuple[ScreenPoint, ...], ...]
    style: StrokeStyle

    @property
    def visible(self) -> bool:
        return bool(self.paths)


@dataclass(frozen=True, slots=True)
class CompassPlan:
    """Resolved horizon-direction tick marks and labels."""

    ticks: tuple[LineSegment, ...]
    labels: TextBatch


@dataclass(frozen=True, slots=True)
class HudPlan:
    """A data-only HUD box and its text lines."""

    bounds: ScreenRect
    background_rgba: RGBA
    border: StrokeStyle
    corner_radius_px: float
    labels: TextBatch
    visible: bool = True


class TextLayoutPlanner:
    """Resolve measured label bounds, priority and collision outside the View."""

    def __init__(self) -> None:
        self._cache: dict[tuple[object, ...], TextBatch] = {}

    @staticmethod
    def _text_bounds(
        candidate: TextCandidate, metrics: TextMetrics
    ) -> ScreenRect:
        x, baseline_y = candidate.position
        width = max(0.0, float(metrics.advance_px))
        if candidate.anchor == "baseline_center":
            x -= width * 0.5
        return ScreenRect(
            x - candidate.style.background_padding_x,
            baseline_y
            - float(metrics.ascent_px)
            - candidate.style.background_padding_y,
            width + 2.0 * candidate.style.background_padding_x,
            max(1.0, metrics.height_px)
            + 2.0 * candidate.style.background_padding_y,
        )

    @staticmethod
    def _fits_clip(bounds: ScreenRect, clip: ScreenRect | None) -> bool:
        return clip is None or bounds.intersects(clip)

    def plan(
        self,
        candidates: Sequence[TextCandidate],
        metrics: TextMetricsProvider,
        *,
        cache_key: tuple[object, ...] = (),
        collision_padding: tuple[float, float] = (2.0, 1.0),
    ) -> TextBatch:
        """Select labels deterministically without inventing font metrics."""

        frozen_candidates = tuple(candidates)
        full_key = (
            cache_key,
            metrics.revision,
            frozen_candidates,
            collision_padding,
        )
        cached = self._cache.get(full_key)
        if cached is not None:
            return TextBatch(
                cached.labels,
                TextLayoutStats(
                    cached.stats.candidates,
                    cached.stats.accepted,
                    cached.stats.rejected_collision,
                    cached.stats.rejected_clip,
                    True,
                ),
                cached.cache_key,
            )

        accepted: list[TextLabel] = []
        occupied: list[ScreenRect] = []
        rejected_collision = 0
        rejected_clip = 0
        ordered = sorted(
            enumerate(frozen_candidates),
            key=lambda item: (-item[1].priority, item[1].z_index, item[0]),
        )
        padding_x, padding_y = collision_padding
        for _, candidate in ordered:
            if not candidate.text:
                continue
            bounds = self._text_bounds(
                candidate,
                metrics.measure_text(candidate.text, candidate.style),
            )
            if not self._fits_clip(bounds, candidate.clip):
                rejected_clip += 1
                continue
            collision_bounds = bounds.expanded(padding_x, padding_y)
            if any(
                collision_bounds.intersects(existing) for existing in occupied
            ):
                rejected_collision += 1
                continue
            accepted.append(
                TextLabel(
                    candidate.text,
                    candidate.position,
                    candidate.anchor,
                    candidate.style,
                    candidate.priority,
                    candidate.clip,
                    candidate.z_index,
                    bounds,
                    candidate.pick_id,
                )
            )
            occupied.append(collision_bounds)

        labels = tuple(sorted(accepted, key=lambda label: label.z_index))
        result = TextBatch(
            labels,
            TextLayoutStats(
                len(frozen_candidates),
                len(labels),
                rejected_collision,
                rejected_clip,
                False,
            ),
            full_key,
        )
        if len(self._cache) >= 16:
            self._cache.pop(next(iter(self._cache)))
        self._cache[full_key] = result
        return result


class GridPlanner:
    """Project the existing celestial-equator grid without a View dependency."""

    _STYLE = StrokeStyle((0, 255, 255, 80), 1.0, True)

    @staticmethod
    def equatorial_altitude_azimuth(
        *,
        right_ascension_deg: float,
        declination_deg: float,
        local_sidereal_deg: float,
        latitude_deg: float,
    ) -> tuple[float, float]:
        """Convert an equatorial grid coordinate to horizon coordinates."""

        latitude = math.radians(latitude_deg)
        declination = math.radians(declination_deg)
        hour_angle = math.radians(local_sidereal_deg - right_ascension_deg)
        sin_altitude = math.sin(declination) * math.sin(latitude) + math.cos(
            declination
        ) * math.cos(latitude) * math.cos(hour_angle)
        altitude = math.degrees(math.asin(max(-1.0, min(1.0, sin_altitude))))
        denominator = (
            math.cos(math.radians(altitude)) * math.cos(latitude) + 1e-10
        )
        cos_azimuth = (
            math.sin(declination)
            - math.sin(math.radians(altitude)) * math.sin(latitude)
        ) / denominator
        azimuth = math.degrees(math.acos(max(-1.0, min(1.0, cos_azimuth))))
        if math.sin(hour_angle) > 0.0:
            azimuth = 360.0 - azimuth
        return altitude, azimuth

    @classmethod
    def _project_parallel(
        cls,
        *,
        declination_deg: float,
        local_sidereal_deg: float,
        width: int,
        height: int,
        state: RenderState,
    ) -> tuple[tuple[ScreenPoint, ...], ...]:
        paths: list[list[ScreenPoint]] = []
        path: list[ScreenPoint] = []
        previous: ScreenPoint | None = None
        for index in range(145):
            altitude, azimuth = cls.equatorial_altitude_azimuth(
                right_ascension_deg=index * 2.5,
                declination_deg=declination_deg,
                local_sidereal_deg=local_sidereal_deg,
                latitude_deg=state.latitude,
            )
            point = project_universal_stereo_point(
                altitude, azimuth, width, height, state.camera
            )
            if point is None:
                if path:
                    paths.append(path)
                path = []
                previous = None
                continue
            current = float(point[0]), float(point[1])
            if (
                previous is not None
                and math.dist(current, previous) > max(width, height) * 0.35
            ):
                if path:
                    paths.append(path)
                path = []
            path.append(current)
            previous = current
        if path:
            paths.append(path)
        return tuple(tuple(segment) for segment in paths if len(segment) >= 2)

    def plan(self, state: RenderState, width: int, height: int) -> GridPlan:
        """Build the current visible grid policy: the celestial equator."""

        local_sidereal = (
            100.0
            + float(state.day_of_year_utc) * 0.9856
            + float(state.ut_hour) * 15.0
            + float(state.longitude)
        ) % 360.0
        return GridPlan(
            self._project_parallel(
                declination_deg=0.0,
                local_sidereal_deg=local_sidereal,
                width=width,
                height=height,
                state=state,
            ),
            self._STYLE,
        )


class CompassPlanner:
    """Resolve directional ticks and labels from the camera's pure projection."""

    _DIRECTIONS = (
        (0.0, "N"),
        (45.0, "NE"),
        (90.0, "E"),
        (135.0, "SE"),
        (180.0, "S"),
        (225.0, "SW"),
        (270.0, "W"),
        (315.0, "NW"),
    )
    _TICK_STYLE = StrokeStyle((220, 225, 235, 210), 1.2)
    _TEXT_STYLE = TextStyle(
        FontStyle("Arial", 13.0, 700), (220, 225, 235, 210)
    )

    def __init__(self, text_layout: TextLayoutPlanner) -> None:
        self._text_layout = text_layout

    def plan(
        self,
        state: RenderState,
        width: int,
        height: int,
        metrics: TextMetricsProvider,
    ) -> CompassPlan:
        clip = ScreenRect(0.0, 0.0, float(width), float(height))
        ticks: list[LineSegment] = []
        candidates: list[TextCandidate] = []
        for index, (azimuth, label) in enumerate(self._DIRECTIONS):
            point = project_universal_stereo_point(
                0.0, azimuth, width, height, state.camera
            )
            if point is None:
                continue
            x, y = float(point[0]), float(point[1])
            if x < -50.0 or x > width + 50.0 or y < -50.0 or y > height + 50.0:
                continue
            ticks.append(
                LineSegment((x, y - 5.0), (x, y + 5.0), self._TICK_STYLE, 20)
            )
            candidates.append(
                TextCandidate(
                    label,
                    (x, y - 8.0),
                    "baseline_center",
                    self._TEXT_STYLE,
                    100 if index % 2 == 0 else 90,
                    clip,
                    21,
                    f"compass:{label}",
                )
            )
        labels = self._text_layout.plan(
            candidates,
            metrics,
            cache_key=("compass", width, height, _camera_cache_key(state)),
            collision_padding=(0.0, 0.0),
        )
        return CompassPlan(tuple(ticks), labels)


class InformationalLabelPlanner:
    """Build label candidates for scene objects; ordering policy stays pure."""

    _PLANET_STYLE = TextStyle(
        FontStyle("Arial", 12.0, 700),
        (245, 248, 255, 255),
        (4, 7, 14, 175),
        4.0,
        2.0,
        3.0,
    )
    _DEEP_SKY_STYLE = TextStyle(
        FontStyle("Arial", 12.0, 700),
        (232, 244, 255, 255),
        (3, 7, 15, 190),
        4.0,
        1.5,
        3.0,
    )

    @classmethod
    def planet_candidates(
        cls, planets: Sequence[PlanetLabelSource], width: int, height: int
    ) -> tuple[TextCandidate, ...]:
        """Create priority candidates from already-resolved planet plans."""

        clip = ScreenRect(0.0, 0.0, float(width), float(height))
        result: list[TextCandidate] = []
        for planet in planets:
            alpha = int(planet.rgba[3])
            style = TextStyle(
                cls._PLANET_STYLE.font,
                (245, 248, 255, alpha),
                (4, 7, 14, int(round(175 * alpha / 255.0))),
                cls._PLANET_STYLE.background_padding_x,
                cls._PLANET_STYLE.background_padding_y,
                cls._PLANET_STYLE.corner_radius_px,
            )
            result.append(
                TextCandidate(
                    str(planet.label),
                    (
                        float(planet.screen_x) + float(planet.radius_px) + 9.0,
                        float(planet.screen_y) + 3.0,
                    ),
                    "baseline_left",
                    style,
                    1_000,
                    clip,
                    50,
                    f"planet:{planet.key}",
                )
            )
        return tuple(result)

    @classmethod
    def deep_sky_candidates(
        cls, glyphs: Sequence[DeepSkyLabelSource], width: int, height: int
    ) -> tuple[TextCandidate, ...]:
        """Create NGC candidates from projected glyphs, not catalogue objects."""

        clip = ScreenRect(0.0, 0.0, float(width), float(height))
        result: list[TextCandidate] = []
        for glyph in glyphs:
            alpha = min(255, int(glyph.rgba[3]) + 28)
            style = TextStyle(
                cls._DEEP_SKY_STYLE.font,
                (232, 244, 255, alpha),
                cls._DEEP_SKY_STYLE.background_rgba,
                cls._DEEP_SKY_STYLE.background_padding_x,
                cls._DEEP_SKY_STYLE.background_padding_y,
                cls._DEEP_SKY_STYLE.corner_radius_px,
            )
            result.append(
                TextCandidate(
                    str(glyph.name),
                    (
                        float(glyph.screen_x) + float(glyph.radius_x_px) + 9.0,
                        float(glyph.screen_y) + 2.0,
                    ),
                    "baseline_left",
                    style,
                    max(1, alpha),
                    clip,
                    40,
                    f"deep_sky:{glyph.name}",
                )
            )
        return tuple(result)


class HudPlanner:
    """Format the HUD as data; the View only materializes the result."""

    _TITLE_STYLE = TextStyle(
        FontStyle("Arial", 13.0, 700), (255, 255, 255, 255)
    )
    _DETAIL_STYLE = TextStyle(
        FontStyle("Arial", 11.0, 400), (190, 215, 245, 255)
    )
    _DEBUG_STYLE = TextStyle(
        FontStyle("Arial", 10.0, 400), (190, 215, 245, 255)
    )

    def __init__(self, text_layout: TextLayoutPlanner) -> None:
        self._text_layout = text_layout

    @staticmethod
    def _coordinate(value: float, positive: str, negative: str) -> str:
        return f"{abs(value):.2f}\N{DEGREE SIGN}{positive if value >= 0 else negative}"

    def plan(
        self,
        state: RenderState,
        width: int,
        visible_stars: int,
        metrics: TextMetricsProvider,
        *,
        debug_lines: Sequence[str] = (),
        visible: bool = True,
    ) -> HudPlan:
        """Resolve complete HUD content from state and declared diagnostic data."""

        if not visible:
            empty = TextBatch(
                (), TextLayoutStats(0, 0, 0, 0, False), ("hud", "hidden")
            )
            return HudPlan(
                ScreenRect(0.0, 0.0, 0.0, 0.0),
                (0, 0, 0, 0),
                StrokeStyle((0, 0, 0, 0), 0.0),
                0.0,
                empty,
                False,
            )
        azimuth = state.camera.azimuth_offset % 360.0
        directions = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
        direction = directions[int((azimuth + 22.5) / 45.0) % 8]
        fov_deg = 93.9 / max(0.001, state.camera.zoom_level)
        separator = " \N{MIDDLE DOT} "
        line1 = (
            f"{direction} ({azimuth:.1f}\N{DEGREE SIGN}){separator}"
            f"ALT {state.camera.elevation_angle:.1f}\N{DEGREE SIGN}{separator}"
            f"FOV {fov_deg:.1f}\N{DEGREE SIGN}"
        )
        ut_hour = state.ut_hour % 24.0
        line2 = (
            f"UT {int(ut_hour):02d}:{int((ut_hour % 1) * 60):02d}{separator}"
            f"{self._coordinate(state.latitude, 'N', 'S')} "
            f"{self._coordinate(state.longitude, 'E', 'W')}{separator}"
            f"STARS {visible_stars}{separator}Bortle {state.bortle}"
        )
        rows = (
            (line1, self._TITLE_STYLE),
            (line2, self._DETAIL_STYLE),
        ) + tuple((line, self._DEBUG_STYLE) for line in debug_lines)
        box_width = min(560.0, max(280.0, float(width) - 30.0))
        box_height = 64.0 + max(0, len(debug_lines)) * 18.0
        bounds = ScreenRect(10.0, 10.0, box_width, box_height)
        candidates = tuple(
            TextCandidate(
                text,
                (20.0, 35.0 + index * 20.0),
                "baseline_left",
                style,
                10_000 - index,
                bounds,
                100,
                f"hud:{index}",
            )
            for index, (text, style) in enumerate(rows)
        )
        labels = self._text_layout.plan(
            candidates,
            metrics,
            cache_key=(
                "hud",
                width,
                state.ut_hour,
                state.latitude,
                state.longitude,
                state.bortle,
                _camera_cache_key(state),
                visible_stars,
                tuple(debug_lines),
            ),
            collision_padding=(0.0, 0.0),
        )
        return HudPlan(
            bounds,
            (5, 8, 17, 235),
            StrokeStyle((216, 178, 106, 190), 1.5),
            8.0,
            labels,
            True,
        )
