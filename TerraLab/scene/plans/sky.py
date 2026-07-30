"""Pure Model plan for the physical sky background and viewport clip.

This module owns the sky-colour formula and its dependency-based cache token.
It intentionally has no Qt, renderer, runtime, or application imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from TerraLab.scene.camera import Camera
from TerraLab.scene.contracts import Viewport
from TerraLab.scene.projection import unproject_universal_stereo_point


Rgba = tuple[int, int, int, int]


def _finite(name: str, value: float) -> float:
    resolved = float(value)
    if not math.isfinite(resolved):
        raise ValueError(f"{name} must be finite")
    return resolved


def _rgb_lerp(
    first: tuple[int, int, int],
    second: tuple[int, int, int],
    factor: float,
) -> tuple[int, int, int]:
    return tuple(
        int(start + (end - start) * factor)
        for start, end in zip(first, second, strict=True)
    )


def calculate_sky_rgba(
    view_altitude_deg: float,
    view_azimuth_deg: float,
    sun_altitude_deg: float,
    sun_azimuth_deg: float,
    *,
    bortle_class: int = 1,
    twilight_factor: float = 1.0,
) -> Rgba:
    """Return the canonical, Qt-free RGBA colour for one sky direction."""

    view_altitude_deg = _finite("view_altitude_deg", view_altitude_deg)
    view_azimuth_deg = _finite("view_azimuth_deg", view_azimuth_deg)
    sun_altitude_deg = _finite("sun_altitude_deg", sun_altitude_deg)
    sun_azimuth_deg = _finite("sun_azimuth_deg", sun_azimuth_deg)
    twilight_factor = _finite("twilight_factor", twilight_factor)

    keyframes = (
        (20.0, (0, 100, 200), (100, 180, 255), (255, 255, 220)),
        (6.0, (20, 50, 90), (255, 190, 60), (255, 140, 20)),
        (0.0, (25, 30, 70), (255, 70, 10), (255, 60, 0)),
        (-4.0, (10, 15, 50), (120, 60, 20), (60, 20, 5)),
        (-6.0, (5, 5, 25), (40, 15, 20), (10, 2, 0)),
        (-12.0, (2, 2, 10), (10, 10, 25), (0, 0, 0)),
        (-18.0, (0, 0, 5), (5, 5, 15), (0, 0, 0)),
    )
    first = keyframes[0]
    second = keyframes[-1]
    if sun_altitude_deg >= keyframes[0][0]:
        first = keyframes[0]
        second = first
        factor = 0.0
    elif sun_altitude_deg <= keyframes[-1][0]:
        first = keyframes[-1]
        second = first
        factor = 0.0
    else:
        factor = 0.0
        for index in range(len(keyframes) - 1):
            current = keyframes[index]
            following = keyframes[index + 1]
            if current[0] >= sun_altitude_deg >= following[0]:
                first = current
                second = following
                factor = (current[0] - sun_altitude_deg) / (
                    current[0] - following[0] + 1e-9
                )
                break

    zenith_rgb = _rgb_lerp(first[1], second[1], factor)
    horizon_rgb = _rgb_lerp(first[2], second[2], factor)
    sun_rgb = _rgb_lerp(first[3], second[3], factor)

    azimuth_delta_rad = math.radians(abs(view_azimuth_deg - sun_azimuth_deg))
    while azimuth_delta_rad > math.pi:
        azimuth_delta_rad -= 2.0 * math.pi
    azimuth_delta_rad = abs(azimuth_delta_rad)

    view_altitude_rad = math.radians(view_altitude_deg)
    sun_altitude_rad = math.radians(sun_altitude_deg)
    cos_gamma = math.sin(view_altitude_rad) * math.sin(
        sun_altitude_rad
    ) + math.cos(view_altitude_rad) * math.cos(sun_altitude_rad) * math.cos(
        azimuth_delta_rad
    )
    cos_gamma = max(-1.0, min(1.0, cos_gamma))
    gamma_rad = math.acos(cos_gamma)

    if -12.0 < sun_altitude_deg < 15.0:
        azimuth_factor = (math.cos(azimuth_delta_rad) + 1.0) / 2.0
        azimuth_factor = math.pow(azimuth_factor, 1.5)
        sun_extinction = 1.0
        if sun_altitude_deg < 0.0:
            sun_extinction = max(0.0, 1.0 - (abs(sun_altitude_deg) / 12.0))
        anti_sun_horizon_rgb = (
            (100, 130, 170) if sun_altitude_deg > 0.0 else (5, 5, 12)
        )
        target_rgb = tuple(
            int(horizon * azimuth_factor + anti_sun * (1.0 - azimuth_factor))
            for horizon, anti_sun in zip(
                horizon_rgb, anti_sun_horizon_rgb, strict=True
            )
        )
        horizon_rgb = tuple(
            int(horizon * (1.0 - sun_extinction) + target * sun_extinction)
            for horizon, target in zip(horizon_rgb, target_rgb, strict=True)
        )

    # Keep the established branch order exactly: the first branch intentionally
    # covers the legacy <= -18 degree range as well.
    if sun_altitude_deg <= -12.0:
        zenith_rgb = (0, 0, 5)
        horizon_rgb = (5, 5, 12)
        sun_rgb = (0, 0, 0)
    elif sun_altitude_deg <= -18.0:
        zenith_rgb = (0, 0, 2)
        horizon_rgb = (2, 2, 8)
        sun_rgb = (0, 0, 0)

    horizon_mix = 1.0 - (view_altitude_deg / 90.0)
    horizon_mix = max(0.0, min(1.0, horizon_mix))
    horizon_mix = horizon_mix * horizon_mix * (3.0 - 2.0 * horizon_mix)
    red = zenith_rgb[0] * (1.0 - horizon_mix) + horizon_rgb[0] * horizon_mix
    green = zenith_rgb[1] * (1.0 - horizon_mix) + horizon_rgb[1] * horizon_mix
    blue = zenith_rgb[2] * (1.0 - horizon_mix) + horizon_rgb[2] * horizon_mix

    if cos_gamma > 0.0:
        directional_glow = math.pow(cos_gamma, 4.0) * 0.15
        glow_intensity = (
            max(0.0, 1.0 - (abs(sun_altitude_deg) / 12.0))
            if sun_altitude_deg < 0.0
            else 1.0
        )
        directional_glow *= glow_intensity
        red = min(255.0, red + sun_rgb[0] * directional_glow)
        green = min(255.0, green + sun_rgb[1] * directional_glow)
        blue = min(255.0, blue + sun_rgb[2] * directional_glow)

    anti_sun_factor = max(0.0, -math.cos(azimuth_delta_rad))
    if -10.0 <= sun_altitude_deg <= 2.0 and anti_sun_factor > 0.0:
        angle_from_anti_sun = abs(gamma_rad - math.pi)
        if angle_from_anti_sun < 0.5:
            belt_distance = abs(view_altitude_deg - 10.0)
            belt_strength = (
                math.exp(-(belt_distance * belt_distance) / 100.0)
                * 0.2
                * (1.0 - angle_from_anti_sun * 2.0)
                * anti_sun_factor
            )
            red += 60.0 * belt_strength
            green += 30.0 * belt_strength
            blue += 50.0 * belt_strength

    if sun_altitude_deg < 2.0 and anti_sun_factor > 0.0:
        shadow_height = 6.0 + abs(sun_altitude_deg)
        if view_altitude_deg < shadow_height:
            shadow_factor = (shadow_height - view_altitude_deg) / shadow_height
            darken = 1.0 - 0.5 * shadow_factor * anti_sun_factor
            red *= darken
            green *= darken
            blue *= darken

    red = min(255, max(0, int(red)))
    green = min(255, max(0, int(green)))
    blue = min(255, max(0, int(blue)))

    if bortle_class > 2 and twilight_factor > 0.01:
        light_dome_strength = (bortle_class - 2) / 7.0
        light_dome_alpha = light_dome_strength * twilight_factor * 0.15
        elevation = max(0.0, min(1.0, 1.0 - (view_altitude_deg / 90.0)))
        strength = light_dome_alpha * math.pow(elevation, 2.5)
        red = min(255, int(red + 140 * strength))
        green = min(255, int(green + 130 * strength))
        blue = min(255, int(blue + 110 * strength))
    return red, green, blue, 255


def apply_eclipse_transmission(
    rgba: Rgba,
    *,
    sun_altitude_deg: float,
    eclipse_transmission: float,
) -> Rgba:
    """Apply the established daytime eclipse dimming without a graphics API."""

    if sun_altitude_deg <= -1.0 or eclipse_transmission >= 0.999:
        return rgba
    dimming = 0.08 + 0.92 * math.sqrt(max(0.0, eclipse_transmission))
    return (
        int(rgba[0] * dimming),
        int(rgba[1] * dimming),
        int(rgba[2] * dimming),
        rgba[3],
    )


@dataclass(frozen=True, slots=True)
class SkyBackgroundInputs:
    """Typed dependencies of the sky-background Model calculation."""

    viewport: Viewport
    camera: Camera
    sun_altitude_deg: float
    sun_azimuth_deg: float
    bortle_class: int
    eclipse_transmission: float = 1.0
    interaction_active: bool = False


@dataclass(frozen=True, slots=True)
class SkyBackgroundPlan:
    """Immutable, renderer-neutral samples and the basic viewport mask."""

    sample_width: int
    sample_height: int
    rgba_samples: tuple[Rgba, ...]
    horizon_mask: tuple[bool, ...]
    cache_token: tuple[object, ...]
    clip_to_viewport: bool = True

    def __post_init__(self) -> None:
        expected = self.sample_width * self.sample_height
        if self.sample_width <= 0 or self.sample_height <= 0:
            raise ValueError("Sky-background samples must be positive")
        if len(self.rgba_samples) != expected:
            raise ValueError("Sky-background samples do not match dimensions")
        if len(self.horizon_mask) != expected:
            raise ValueError("Horizon mask does not match sample dimensions")


def sky_background_cache_token(
    inputs: SkyBackgroundInputs,
) -> tuple[object, ...]:
    """Return the explicit cache dependencies from the active legacy route."""

    resolution = 10 if inputs.interaction_active else 48
    camera = inputs.camera
    return (
        resolution,
        int(inputs.viewport.width),
        int(inputs.viewport.height),
        round(float(inputs.sun_altitude_deg) * 2.0) / 2.0,
        round(float(inputs.sun_azimuth_deg) / 2.0) * 2.0,
        round(float(camera.azimuth_offset) / 2.0) * 2.0,
        round(float(camera.elevation_angle), 1),
        round(float(camera.zoom_level), 2),
        int(inputs.bortle_class),
        round(float(inputs.eclipse_transmission), 3),
    )


class SkyBackgroundPlanner:
    """Memoized pure Model planner for one sky-background capability."""

    def __init__(self) -> None:
        self._cached_plan: SkyBackgroundPlan | None = None
        self.cache_hits = 0
        self.cache_misses = 0

    def build_plan(self, inputs: SkyBackgroundInputs) -> SkyBackgroundPlan:
        """Resolve adaptive samples, colours, and the basic projection mask."""

        cache_token = sky_background_cache_token(inputs)
        if (
            self._cached_plan is not None
            and self._cached_plan.cache_token == cache_token
        ):
            self.cache_hits += 1
            return self._cached_plan

        resolution = int(cache_token[0])
        width = int(inputs.viewport.width)
        height = int(inputs.viewport.height)
        samples: list[Rgba] = []
        horizon_mask: list[bool] = []
        for sample_y in range(resolution):
            screen_y = sample_y * height / max(1.0, resolution - 1.0)
            for sample_x in range(resolution):
                screen_x = sample_x * width / max(1.0, resolution - 1.0)
                sky_coordinate = unproject_universal_stereo_point(
                    screen_x,
                    screen_y,
                    width,
                    height,
                    inputs.camera,
                )
                visible_sky = sky_coordinate is not None
                altitude_deg, azimuth_deg = sky_coordinate or (-90.0, 0.0)
                rgba = calculate_sky_rgba(
                    altitude_deg,
                    azimuth_deg,
                    inputs.sun_altitude_deg,
                    inputs.sun_azimuth_deg,
                    bortle_class=int(inputs.bortle_class),
                )
                samples.append(
                    apply_eclipse_transmission(
                        rgba,
                        sun_altitude_deg=inputs.sun_altitude_deg,
                        eclipse_transmission=inputs.eclipse_transmission,
                    )
                )
                horizon_mask.append(visible_sky)

        plan = SkyBackgroundPlan(
            sample_width=resolution,
            sample_height=resolution,
            rgba_samples=tuple(samples),
            horizon_mask=tuple(horizon_mask),
            cache_token=cache_token,
        )
        self._cached_plan = plan
        self.cache_misses += 1
        return plan
