"""Renderer-neutral plans for Solar-system bodies and celestial trails."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from TerraLab.astro.apparent import (
    angular_separation_deg,
    daylight_moon_alpha,
    standard_refracted_altitude_deg,
    standard_refraction_vertical_scale,
)
from TerraLab.astro.photometry import (
    PLANET_COLORS,
    apparent_planet_magnitude,
)
from TerraLab.scene.camera import Camera
from TerraLab.scene.projection import (
    project_universal_stereo_numpy,
    project_universal_stereo_point,
    radec_to_altaz_numpy,
)
from TerraLab.scene.render_state import RenderState


RGBA = tuple[int, int, int, int]


def _readonly(values: np.ndarray) -> np.ndarray:
    values.setflags(write=False)
    return values


def _mapping(value: Any) -> Mapping[str, Any]:
    """Normalize an untyped external snapshot node to a read-only interface."""

    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class BodyPickRecord:
    """A renderer-neutral hit target associated with a visible body."""

    body_type: str
    key: str
    name: str
    altitude_deg: float
    azimuth_deg: float
    screen_x: float
    screen_y: float
    radius_px: float
    magnitude: float | None = None


@dataclass(frozen=True, slots=True)
class RefractedDiscPlan:
    """A refracted screen ellipse, computed without a presentation API."""

    screen_x: float
    screen_y: float
    radius_x_px: float
    radius_y_px: float
    apparent_altitude_deg: float


@dataclass(frozen=True, slots=True)
class CoronaPlan:
    """Declarative totality-corona data consumed by visual adapters."""

    screen_x: float
    screen_y: float
    radius_px: float
    strength: float
    orientation_deg: float
    vertical_scale: float
    streamers: tuple[tuple[float, float, float, float], ...]


@dataclass(frozen=True, slots=True)
class SunDiscPlan:
    geometry: RefractedDiscPlan
    core_rgba: RGBA
    edge_rgba: RGBA
    corona: CoronaPlan | None


@dataclass(frozen=True, slots=True)
class MoonDiscPlan:
    geometry: RefractedDiscPlan
    physical_sun_geometry: RefractedDiscPlan | None
    illumination: float
    visibility_alpha: float
    eclipsing: bool
    night: bool
    rotation_deg: float
    lit_outline: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class PlanetDiscPlan:
    key: str
    name: str
    altitude_deg: float
    azimuth_deg: float
    screen_x: float
    screen_y: float
    radius_px: float
    magnitude: float
    rgba: RGBA
    label: str


@dataclass(frozen=True, slots=True)
class CelestialBodiesPlan:
    """Resolved body primitives and pick targets for one scene frame."""

    sun: SunDiscPlan | None
    moon: MoonDiscPlan | None
    planets: tuple[PlanetDiscPlan, ...]
    picks: tuple[BodyPickRecord, ...]


@dataclass(frozen=True, slots=True)
class TrailPlan:
    """Projected, colour-bucketed trails with no raster image ownership."""

    cache_key: tuple[object, ...]
    screen_x: np.ndarray
    screen_y: np.ndarray
    valid: np.ndarray
    rgb: np.ndarray
    alpha: int

    @property
    def empty(self) -> bool:
        return len(self.screen_x) == 0


class CelestialBodiesPlanner:
    """Resolve bodies, eclipse presentation and photometry into pure data."""

    _CORONA_STREAMERS = (
        (6.0, 2.05, 0.55, 1.30),
        (41.0, 1.45, 0.78, 1.16),
        (88.0, 1.75, 0.62, 1.24),
        (137.0, 1.35, 0.88, 1.12),
        (188.0, 2.20, 0.52, 1.34),
        (232.0, 1.50, 0.75, 1.18),
        (281.0, 1.85, 0.58, 1.27),
        (329.0, 1.32, 0.82, 1.10),
    )

    @staticmethod
    def local_angular_radius_px(
        altitude_deg: float,
        azimuth_deg: float,
        angular_radius_deg: float,
        width: int,
        height: int,
        camera: Camera,
        *,
        fallback_px: float,
    ) -> float:
        """Measure an angular radius in the centre's local projection."""

        centre = project_universal_stereo_point(
            altitude_deg, azimuth_deg, width, height, camera
        )
        if centre is None:
            return max(0.05, float(fallback_px))
        angular_radius = max(1e-6, float(angular_radius_deg))
        samples: list[float] = []
        for direction in (-1.0, 1.0):
            sample_altitude = float(altitude_deg) + direction * angular_radius
            if not -89.999 < sample_altitude < 89.999:
                continue
            point = project_universal_stereo_point(
                sample_altitude, azimuth_deg, width, height, camera
            )
            if point is not None:
                samples.append(
                    math.hypot(
                        float(point[0]) - float(centre[0]),
                        float(point[1]) - float(centre[1]),
                    )
                )
        if samples:
            measured = sum(samples) / len(samples)
            if math.isfinite(measured) and measured > 0.0:
                return max(0.05, float(measured))
        return max(0.05, float(fallback_px))

    @classmethod
    def refracted_disc_geometry(
        cls,
        altitude_deg: float,
        azimuth_deg: float,
        angular_radius_deg: float,
        width: int,
        height: int,
        camera: Camera,
        *,
        fallback_px: float,
    ) -> RefractedDiscPlan | None:
        """Project a refracted centre and its vertically compressed limb."""

        apparent_altitude = standard_refracted_altitude_deg(altitude_deg)
        point = project_universal_stereo_point(
            apparent_altitude, azimuth_deg, width, height, camera
        )
        if point is None:
            return None
        radius_x = cls.local_angular_radius_px(
            apparent_altitude,
            azimuth_deg,
            angular_radius_deg,
            width,
            height,
            camera,
            fallback_px=fallback_px,
        )
        return RefractedDiscPlan(
            screen_x=float(point[0]),
            screen_y=float(point[1]),
            radius_x_px=float(radius_x),
            radius_y_px=max(
                0.05,
                float(radius_x)
                * standard_refraction_vertical_scale(altitude_deg),
            ),
            apparent_altitude_deg=float(apparent_altitude),
        )

    @staticmethod
    def celestial_disc_visual_factor(physical_radius_px: float) -> float:
        """Magnify discs uniformly while retaining eclipse contact geometry."""

        radius = max(0.05, float(physical_radius_px))
        if radius <= 36.0:
            scale = 4.0
        elif radius < 96.0:
            scale = 4.0 - 3.0 * ((radius - 36.0) / 60.0)
        else:
            scale = 1.0
        return max(scale, 18.0 / radius)

    @staticmethod
    def _sun_colours(altitude_deg: float) -> tuple[RGBA, RGBA]:
        if altitude_deg < 5.0:
            return (255, 190, 72, 255), (255, 108, 22, 255)
        if altitude_deg < 20.0:
            warmth = (float(altitude_deg) - 5.0) / 15.0
            return (
                (
                    255,
                    int(round(205 + 38 * warmth)),
                    int(round(92 + 105 * warmth)),
                    255,
                ),
                (
                    255,
                    int(round(125 + 78 * warmth)),
                    int(round(28 + 65 * warmth)),
                    255,
                ),
            )
        return (255, 255, 255, 255), (255, 225, 120, 255)

    @staticmethod
    def _moon_lit_outline(
        radius_px: float, illumination: float
    ) -> tuple[tuple[float, float], ...]:
        radius = max(0.01, float(radius_px))
        illumination = max(0.0, min(1.0, float(illumination)))
        samples = 72
        outline: list[tuple[float, float]] = []
        for index in range(samples + 1):
            y = -radius + 2.0 * radius * index / samples
            half_width = math.sqrt(max(0.0, radius**2 - y**2))
            outline.append(((1.0 - 2.0 * illumination) * half_width, y))
        for index in range(samples, -1, -1):
            y = -radius + 2.0 * radius * index / samples
            outline.append((math.sqrt(max(0.0, radius**2 - y**2)), y))
        return tuple(outline)

    @staticmethod
    def _planet_visibility_alpha(
        *,
        altitude_deg: float,
        azimuth_deg: float,
        magnitude: float,
        state: RenderState,
    ) -> float:
        planet_altitude = math.radians(altitude_deg)
        sun_altitude = math.radians(state.sun_alt)
        cosine_solar_distance = math.sin(planet_altitude) * math.sin(
            sun_altitude
        ) + math.cos(planet_altitude) * math.cos(sun_altitude) * math.cos(
            math.radians(azimuth_deg - state.sun_az)
        )
        directional_modifier = -1.5 * max(
            -1.0, min(1.0, cosine_solar_distance)
        )
        extinction_altitude = max(0.1, altitude_deg)
        airmass = 1.0 / (
            math.sin(math.radians(extinction_altitude))
            + 0.15 * (extinction_altitude + 3.885) ** -1.253
        )
        local_limit = (
            state.mag_limit
            + directional_modifier
            - float(state.scope_k_fallback) * (airmass - 1.0)
        )
        return max(0.0, min(1.0, (local_limit - magnitude) * 2.0))

    def plan(
        self, state: RenderState, width: int, height: int
    ) -> CelestialBodiesPlan:
        """Resolve one immutable bodies plan from the complete render state."""

        show_sun_moon = "sun_moon" in state.layers_enabled
        show_planets = bool(
            {"planets", "solar_system"}.intersection(state.layers_enabled)
        )
        if not show_sun_moon and not show_planets:
            return CelestialBodiesPlan(None, None, (), ())
        snapshot: Mapping[str, Any] = (
            state.ephemeris_snapshot
            if isinstance(state.ephemeris_snapshot, Mapping)
            else {}
        )
        sun_data = _mapping(snapshot.get("sun"))
        moon_data = _mapping(snapshot.get("moon"))
        scale = min(width, height) * 0.5 * state.camera.zoom_level / 90.0
        sun_altitude = float(sun_data.get("alt", state.sun_alt))
        sun_azimuth = float(sun_data.get("az", state.sun_az))
        sun_radius = float(sun_data.get("rad_deg", 0.2666))
        moon_altitude = float(moon_data.get("alt", -90.0))
        moon_azimuth = float(moon_data.get("az", 0.0))
        moon_radius = float(moon_data.get("rad_deg", 0.2725))
        separation = float(
            moon_data.get(
                "sep_real",
                angular_separation_deg(
                    sun_altitude, sun_azimuth, moon_altitude, moon_azimuth
                ),
            )
        )
        physical_sun_geometry = self.refracted_disc_geometry(
            sun_altitude,
            sun_azimuth,
            sun_radius,
            width,
            height,
            state.camera,
            fallback_px=sun_radius * scale,
        )
        moon_geometry = self.refracted_disc_geometry(
            moon_altitude,
            moon_azimuth,
            moon_radius,
            width,
            height,
            state.camera,
            fallback_px=moon_radius * scale,
        )
        picks: list[BodyPickRecord] = []
        sun_plan: SunDiscPlan | None = None
        sun_geometry = physical_sun_geometry
        if show_sun_moon and sun_geometry is not None:
            visual_factor = self.celestial_disc_visual_factor(
                sun_geometry.radius_x_px
            )
            sun_geometry = RefractedDiscPlan(
                sun_geometry.screen_x,
                sun_geometry.screen_y,
                sun_geometry.radius_x_px * visual_factor,
                sun_geometry.radius_y_px * visual_factor,
                sun_geometry.apparent_altitude_deg,
            )
            totality = bool(
                moon_radius >= sun_radius
                and separation <= max(0.0, moon_radius - sun_radius)
            )
            corona = (
                CoronaPlan(
                    sun_geometry.screen_x,
                    sun_geometry.screen_y,
                    sun_geometry.radius_x_px,
                    1.0,
                    state.day_of_year_utc * 0.73 + state.ut_hour * 4.0,
                    sun_geometry.radius_y_px
                    / max(0.05, sun_geometry.radius_x_px),
                    self._CORONA_STREAMERS,
                )
                if totality
                else None
            )
            core, edge = self._sun_colours(sun_altitude)
            sun_plan = SunDiscPlan(sun_geometry, core, edge, corona)
            picks.append(
                BodyPickRecord(
                    "sun",
                    "sun",
                    "Sun",
                    sun_altitude,
                    sun_azimuth,
                    sun_geometry.screen_x,
                    sun_geometry.screen_y,
                    max(sun_geometry.radius_x_px, sun_geometry.radius_y_px)
                    + 8.0,
                )
            )

        moon_plan: MoonDiscPlan | None = None
        if show_sun_moon and moon_geometry is not None:
            illumination = max(
                0.0,
                min(
                    1.0,
                    float(
                        moon_data.get(
                            "illumination",
                            (1.0 - math.cos(math.radians(separation))) / 2.0,
                        )
                    ),
                ),
            )
            eclipsing = separation < sun_radius + moon_radius
            eclipse_presentation = separation < 4.0
            if (
                eclipse_presentation
                and sun_plan is not None
                and physical_sun_geometry is not None
            ):
                factor = self.celestial_disc_visual_factor(
                    physical_sun_geometry.radius_x_px
                )
                moon_geometry = RefractedDiscPlan(
                    sun_plan.geometry.screen_x
                    + (moon_geometry.screen_x - sun_plan.geometry.screen_x)
                    * factor,
                    sun_plan.geometry.screen_y
                    + (moon_geometry.screen_y - sun_plan.geometry.screen_y)
                    * factor,
                    moon_geometry.radius_x_px * factor,
                    moon_geometry.radius_y_px * factor,
                    moon_geometry.apparent_altitude_deg,
                )
            else:
                factor = self.celestial_disc_visual_factor(
                    moon_geometry.radius_x_px
                )
                moon_geometry = RefractedDiscPlan(
                    moon_geometry.screen_x,
                    moon_geometry.screen_y,
                    moon_geometry.radius_x_px * factor,
                    moon_geometry.radius_y_px * factor,
                    moon_geometry.apparent_altitude_deg,
                )
            visibility = daylight_moon_alpha(
                illumination=illumination,
                elongation_deg=separation,
                moon_alt_deg=moon_altitude,
                sun_alt_deg=state.sun_alt,
                eclipsing=eclipsing,
            )
            if visibility > 0.0:
                rotation = 0.0
                if sun_plan is not None:
                    rotation = math.degrees(
                        math.atan2(
                            sun_plan.geometry.screen_y
                            - moon_geometry.screen_y,
                            sun_plan.geometry.screen_x
                            - moon_geometry.screen_x,
                        )
                    )
                moon_plan = MoonDiscPlan(
                    moon_geometry,
                    sun_plan.geometry if sun_plan is not None else None,
                    illumination,
                    visibility,
                    eclipsing,
                    state.sun_alt <= -6.0,
                    rotation,
                    self._moon_lit_outline(
                        moon_geometry.radius_x_px, illumination
                    ),
                )
                picks.append(
                    BodyPickRecord(
                        "moon",
                        "moon",
                        "Moon",
                        moon_altitude,
                        moon_azimuth,
                        moon_geometry.screen_x,
                        moon_geometry.screen_y,
                        max(
                            moon_geometry.radius_x_px,
                            moon_geometry.radius_y_px,
                        )
                        + 8.0,
                    )
                )

        planets: list[PlanetDiscPlan] = []
        raw_planets = snapshot.get("planets", ())
        if show_planets and isinstance(raw_planets, (list, tuple)):
            for raw_planet in raw_planets:
                if not isinstance(raw_planet, Mapping):
                    continue
                planet = dict(raw_planet)
                name = str(planet.get("name", "Planet")).strip()
                altitude = float(planet.get("alt", -90.0))
                azimuth = float(planet.get("az", 0.0))
                if altitude < -5.0:
                    continue
                magnitude = apparent_planet_magnitude(planet)
                alpha = self._planet_visibility_alpha(
                    altitude_deg=altitude,
                    azimuth_deg=azimuth,
                    magnitude=magnitude,
                    state=state,
                )
                if alpha <= 0.01:
                    continue
                point = project_universal_stereo_point(
                    altitude, azimuth, width, height, state.camera
                )
                if point is None:
                    continue
                screen_x, screen_y = float(point[0]), float(point[1])
                if not (
                    -30.0 <= screen_x <= width + 30.0
                    and -30.0 <= screen_y <= height + 30.0
                ):
                    continue
                radius = (
                    4.0
                    if name in ("Venus", "Jupiter")
                    else 3.5
                    if name in ("Mars", "Saturn")
                    else 3.0
                )
                colour = PLANET_COLORS.get(name, (230, 220, 200, 255))
                rgba = (
                    colour[0],
                    colour[1],
                    colour[2],
                    int(round(255.0 * alpha)),
                )
                key = str(planet.get("key", name)).lower()
                plan = PlanetDiscPlan(
                    key,
                    name,
                    altitude,
                    azimuth,
                    screen_x,
                    screen_y,
                    radius,
                    magnitude,
                    rgba,
                    f"{name} {magnitude:.1f}",
                )
                planets.append(plan)
                picks.append(
                    BodyPickRecord(
                        "planet",
                        key,
                        name,
                        altitude,
                        azimuth,
                        screen_x,
                        screen_y,
                        radius + 8.0,
                        magnitude,
                    )
                )
        return CelestialBodiesPlan(
            sun_plan, moon_plan, tuple(planets), tuple(picks)
        )


class CelestialTrailsPlanner:
    """Build and cache renderer-neutral stellar trail polylines."""

    def __init__(self) -> None:
        self._cache_key: tuple[object, ...] | None = None
        self._cached_plan: TrailPlan | None = None

    def clear_cache(self) -> None:
        self._cache_key = None
        self._cached_plan = None

    def plan(
        self,
        state: RenderState,
        width: int,
        height: int,
        settings: Mapping[str, Any] | None,
    ) -> TrailPlan | None:
        """Resolve trail points once per typed input change, never per paint."""

        settings = settings if isinstance(settings, Mapping) else {}
        if (
            not bool(settings.get("enabled", False))
            or state.sun_alt > -6.0
            or len(state.np_ra) == 0
        ):
            self.clear_cache()
            return None
        try:
            start_hour = float(settings["start_hour"])
        except (KeyError, TypeError, ValueError):
            return None
        difference = ((float(state.ut_hour) - start_hour + 12.0) % 24.0) - 12.0
        if difference <= 1e-4:
            return None
        interaction = bool(state.interaction_active)
        magnitude_limit = float(state.mag_limit)
        mask = (
            np.isfinite(state.np_ra)
            & np.isfinite(state.np_dec)
            & np.isfinite(state.np_mag)
            & (state.np_mag <= magnitude_limit)
        )
        indices = np.flatnonzero(mask)
        maximum = 6000 if interaction else 20_000
        if len(indices) > maximum:
            local = np.argpartition(
                np.asarray(state.np_mag[indices]), maximum - 1
            )[:maximum]
            indices = indices[local]
        if len(indices) == 0:
            return None
        steps = (
            min(18, max(5, int(abs(difference) * 2.0) + 3))
            if interaction
            else min(96, max(12, int(abs(difference) * 8.0) + 4))
        )
        cache_key = (
            width,
            height,
            len(state.np_ra),
            int(round(start_hour * 3600.0)),
            int(
                round(
                    float(state.ut_hour) * (3600.0 if interaction else 1200.0)
                )
            ),
            round(state.latitude, 5),
            round(state.longitude, 5),
            state.day_of_year_utc,
            state.year_utc,
            round(state.camera.azimuth_offset, 3),
            round(state.camera.elevation_angle, 3),
            round(state.camera.zoom_level, 4),
            round(state.camera.vertical_offset_ratio, 4),
            round(magnitude_limit, 2),
            interaction,
        )
        if cache_key == self._cache_key and self._cached_plan is not None:
            return self._cached_plan
        hours = np.linspace(
            start_hour, start_hour + difference, steps, dtype=np.float64
        )
        screen_x = np.full((len(indices), steps), np.nan, dtype=np.float32)
        screen_y = np.full((len(indices), steps), np.nan, dtype=np.float32)
        valid = np.zeros((len(indices), steps), dtype=bool)
        ra = np.asarray(state.np_ra[indices])
        dec = np.asarray(state.np_dec[indices])
        for column, hour in enumerate(hours):
            altitudes, azimuths = radec_to_altaz_numpy(
                ra,
                dec,
                state.latitude,
                state.longitude,
                float(hour),
                state.day_of_year_utc,
                year=state.year_utc,
            )
            if altitudes is None or azimuths is None:
                continue
            sx, sy, projected = project_universal_stereo_numpy(
                altitudes, azimuths, width, height, state.camera
            )
            if sx is None or sy is None or projected is None:
                continue
            screen_x[:, column] = sx
            screen_y[:, column] = sy
            valid[:, column] = (
                np.asarray(projected, dtype=bool)
                & np.isfinite(sx)
                & np.isfinite(sy)
                & (sx >= -width * 0.25)
                & (sx <= width * 1.25)
                & (sy >= -height * 0.25)
                & (sy <= height * 1.25)
            )
        rgb = np.stack(
            (
                np.minimum(
                    255, (np.asarray(state.np_r[indices]) // 24) * 24 + 15
                ),
                np.minimum(
                    255, (np.asarray(state.np_g[indices]) // 24) * 24 + 15
                ),
                np.minimum(
                    255, (np.asarray(state.np_b[indices]) // 24) * 24 + 15
                ),
            ),
            axis=1,
        ).astype(np.uint8, copy=False)
        plan = TrailPlan(
            cache_key,
            _readonly(screen_x),
            _readonly(screen_y),
            _readonly(valid),
            _readonly(rgb),
            68 if interaction else 138,
        )
        self._cache_key = cache_key
        self._cached_plan = plan
        return plan
