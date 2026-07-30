"""Pure apparent-position, refraction and eclipse calculations.

This module is the one owner of the renderer-facing ephemeris contract.  It
does not know about Qt, a canvas, or a rendering backend: callers receive an
immutable-context snapshot and use it to build a scene plan.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


EPHEMERIS_VISUAL_MAX_AGE_SECONDS = 1.5


def utc_datetime_from_context(
    year_utc: int, day_of_year_utc: int, ut_hour: float
) -> datetime:
    """Build UTC time from TerraLab's zero-based day-of-year convention."""

    return datetime(int(year_utc), 1, 1, tzinfo=timezone.utc) + timedelta(
        days=int(day_of_year_utc), hours=float(ut_hour)
    )


def snapshot_datetime_utc(payload: Any) -> datetime | None:
    """Return a snapshot timestamp normalized to UTC, if it is valid."""

    if not isinstance(payload, Mapping):
        return None
    raw_timestamp = payload.get("timestamp_utc")
    if not raw_timestamp:
        return None
    try:
        text = str(raw_timestamp).strip()
        timestamp = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except (TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def snapshot_matches_utc_context(
    payload: Any,
    *,
    year_utc: int,
    day_of_year_utc: int,
    ut_hour: float,
    max_age_seconds: float = EPHEMERIS_VISUAL_MAX_AGE_SECONDS,
) -> bool:
    """Whether a published ephemeris belongs to the requested UTC instant."""

    timestamp = snapshot_datetime_utc(payload)
    if timestamp is None:
        return False
    requested = utc_datetime_from_context(year_utc, day_of_year_utc, ut_hour)
    return bool(
        abs((timestamp - requested).total_seconds())
        <= max(0.0, float(max_age_seconds))
    )


def _days_since_j2000(
    ut_hour: float, day_of_year_utc: int, year_utc: int
) -> float:
    try:
        instant = utc_datetime_from_context(year_utc, day_of_year_utc, ut_hour)
    except (OverflowError, ValueError):
        instant = utc_datetime_from_context(2026, day_of_year_utc, ut_hour)
    return (
        instant - datetime(2000, 1, 1, 12, 0, tzinfo=timezone.utc)
    ).total_seconds() / 86400.0


def _horizontal_coordinates(
    *,
    right_ascension_deg: float,
    declination_deg: float,
    days_since_j2000: float,
    latitude: float,
    longitude: float,
) -> tuple[float, float]:
    """Convert equatorial coordinates to geometric horizontal coordinates."""

    lst_deg = (
        280.46061837 + 360.98564736629 * days_since_j2000 + longitude
    ) % 360.0
    hour_angle = math.radians((lst_deg - right_ascension_deg) % 360.0)
    latitude_r = math.radians(latitude)
    declination_r = math.radians(declination_deg)
    sin_altitude = math.sin(latitude_r) * math.sin(declination_r) + math.cos(
        latitude_r
    ) * math.cos(declination_r) * math.cos(hour_angle)
    sin_altitude = max(-1.0, min(1.0, sin_altitude))
    altitude = math.degrees(math.asin(sin_altitude))
    cos_azimuth = (
        math.sin(declination_r) - math.sin(latitude_r) * sin_altitude
    ) / (math.cos(latitude_r) * math.cos(math.radians(altitude)) + 1e-10)
    azimuth = math.degrees(math.acos(max(-1.0, min(1.0, cos_azimuth))))
    if math.sin(hour_angle) > 0.0:
        azimuth = 360.0 - azimuth
    return float(altitude), float(azimuth)


def fast_sun_altaz(
    ut_hour: float,
    day_of_year_utc: int,
    latitude: float,
    longitude: float,
    year_utc: int = 2026,
) -> tuple[float, float]:
    """Return the analytical fallback Sun altitude and azimuth in degrees."""

    days = _days_since_j2000(ut_hour, day_of_year_utc, year_utc)
    mean_longitude = (280.460 + 0.9856474 * days) % 360.0
    mean_anomaly = (357.528 + 0.9856003 * days) % 360.0
    longitude_ecliptic = (
        mean_longitude
        + 1.915 * math.sin(math.radians(mean_anomaly))
        + 0.020 * math.sin(math.radians(2.0 * mean_anomaly))
    ) % 360.0
    obliquity = math.radians(23.439 - 0.0000004 * days)
    longitude_r = math.radians(longitude_ecliptic)
    declination = math.degrees(
        math.asin(
            max(-1.0, min(1.0, math.sin(obliquity) * math.sin(longitude_r)))
        )
    )
    right_ascension = (
        math.degrees(
            math.atan2(
                math.cos(obliquity) * math.sin(longitude_r),
                math.cos(longitude_r),
            )
        )
        % 360.0
    )
    return _horizontal_coordinates(
        right_ascension_deg=right_ascension,
        declination_deg=declination,
        days_since_j2000=days,
        latitude=latitude,
        longitude=longitude,
    )


def fast_moon_altaz(
    ut_hour: float,
    day_of_year_utc: int,
    latitude: float,
    longitude: float,
    year_utc: int = 2026,
) -> tuple[float, float, float]:
    """Return analytical fallback Moon altitude, azimuth and illumination."""

    days = _days_since_j2000(ut_hour, day_of_year_utc, year_utc)
    longitude_mean = (218.3164477 + 13.1763965268 * days) % 360.0
    anomaly_moon = (134.9633964 + 13.0649929509 * days) % 360.0
    argument_latitude = (93.2720950 + 13.2293502605 * days) % 360.0
    elongation_mean = (297.8501921 + 12.1907491174 * days) % 360.0
    anomaly_sun = (357.5291092 + 0.9856002831 * days) % 360.0
    longitude_ecliptic = (
        longitude_mean
        + 6.289 * math.sin(math.radians(anomaly_moon))
        - 1.274 * math.sin(math.radians(2.0 * elongation_mean - anomaly_moon))
        + 0.658 * math.sin(math.radians(2.0 * elongation_mean))
        - 0.186 * math.sin(math.radians(anomaly_sun))
    ) % 360.0
    latitude_ecliptic = (
        5.128 * math.sin(math.radians(argument_latitude))
        + 0.280 * math.sin(math.radians(anomaly_moon + argument_latitude))
        + 0.278 * math.sin(math.radians(argument_latitude - anomaly_moon))
    )
    elongation = (
        elongation_mean
        + 6.289 * math.sin(math.radians(anomaly_moon))
        - 2.100 * math.sin(math.radians(anomaly_sun))
    )
    illumination = max(
        0.0,
        min(1.0, (1.0 - math.cos(math.radians(elongation))) / 2.0),
    )
    obliquity = math.radians(23.439 - 0.0000004 * days)
    longitude_r = math.radians(longitude_ecliptic)
    latitude_r = math.radians(latitude_ecliptic)
    declination = math.degrees(
        math.asin(
            max(
                -1.0,
                min(
                    1.0,
                    math.sin(latitude_r) * math.cos(obliquity)
                    + math.cos(latitude_r)
                    * math.sin(obliquity)
                    * math.sin(longitude_r),
                ),
            )
        )
    )
    right_ascension = (
        math.degrees(
            math.atan2(
                math.sin(longitude_r) * math.cos(obliquity)
                - math.tan(latitude_r) * math.sin(obliquity),
                math.cos(longitude_r),
            )
        )
        % 360.0
    )
    altitude, azimuth = _horizontal_coordinates(
        right_ascension_deg=right_ascension,
        declination_deg=declination,
        days_since_j2000=days,
        latitude=latitude,
        longitude=longitude,
    )
    return altitude, azimuth, float(illumination)


def angular_separation_deg(
    alt_a: float, az_a: float, alt_b: float, az_b: float
) -> float:
    """Great-circle separation for two horizontal coordinates."""

    altitude_a = math.radians(float(alt_a))
    altitude_b = math.radians(float(alt_b))
    delta_azimuth = math.radians(float(az_a) - float(az_b))
    cosine = math.sin(altitude_a) * math.sin(altitude_b) + math.cos(
        altitude_a
    ) * math.cos(altitude_b) * math.cos(delta_azimuth)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def standard_refracted_altitude_deg(true_altitude_deg: float) -> float:
    """Map geometric altitude to apparent altitude in a standard atmosphere."""

    true_altitude = float(true_altitude_deg)
    if not -1.0 <= true_altitude <= 89.9:
        return true_altitude
    apparent_altitude = true_altitude
    atmosphere_scale = 0.28 * 1010.0 / (10.0 + 273.0)
    for _ in range(12):
        if not -1.0 <= apparent_altitude <= 89.9:
            apparent_altitude = true_altitude
            continue
        denominator = apparent_altitude + 4.4
        if abs(denominator) < 1e-9:
            break
        tangent = math.tan(
            math.radians(apparent_altitude + 7.31 / denominator)
        )
        if abs(tangent) < 1e-12:
            break
        next_altitude = true_altitude + 0.016667 / tangent * atmosphere_scale
        if abs(next_altitude - apparent_altitude) <= 3e-5:
            apparent_altitude = next_altitude
            break
        apparent_altitude = next_altitude
    return float(apparent_altitude)


def standard_refraction_vertical_scale(true_altitude_deg: float) -> float:
    """Return local vertical compression caused by differential refraction."""

    altitude = float(true_altitude_deg)
    if not -1.0 <= altitude <= 89.9:
        return 1.0
    epsilon = 0.01
    lower = standard_refracted_altitude_deg(max(-0.999, altitude - epsilon))
    upper = standard_refracted_altitude_deg(min(89.899, altitude + epsilon))
    derivative = (upper - lower) / (2.0 * epsilon)
    return (
        max(0.55, min(1.0, float(derivative)))
        if math.isfinite(derivative)
        else 1.0
    )


def solar_disc_transmission(
    separation_deg: float, sun_radius_deg: float, moon_radius_deg: float
) -> float:
    """Return the unobscured fraction of the physical solar disc."""

    distance = max(0.0, float(separation_deg))
    sun_radius = max(1e-9, float(sun_radius_deg))
    moon_radius = max(1e-9, float(moon_radius_deg))
    if distance >= sun_radius + moon_radius:
        return 1.0
    if distance <= abs(sun_radius - moon_radius):
        covered = math.pi * min(sun_radius, moon_radius) ** 2
    else:
        sun_term = (distance**2 + sun_radius**2 - moon_radius**2) / (
            2.0 * distance * sun_radius
        )
        moon_term = (distance**2 + moon_radius**2 - sun_radius**2) / (
            2.0 * distance * moon_radius
        )
        radical = max(
            0.0,
            (-distance + sun_radius + moon_radius)
            * (distance + sun_radius - moon_radius)
            * (distance - sun_radius + moon_radius)
            * (distance + sun_radius + moon_radius),
        )
        covered = (
            sun_radius**2 * math.acos(max(-1.0, min(1.0, sun_term)))
            + moon_radius**2 * math.acos(max(-1.0, min(1.0, moon_term)))
            - 0.5 * math.sqrt(radical)
        )
    return max(0.0, min(1.0, 1.0 - covered / (math.pi * sun_radius**2)))


def daylight_moon_alpha(
    *,
    illumination: float,
    elongation_deg: float,
    moon_alt_deg: float,
    sun_alt_deg: float,
    eclipsing: bool = False,
) -> float:
    """Approximate naked-eye lunar contrast against a daylight sky."""

    if eclipsing or float(sun_alt_deg) <= -6.0:
        return 1.0
    illumination = max(0.0, min(1.0, float(illumination)))
    elongation = max(0.0, min(180.0, float(elongation_deg)))
    if illumination < 0.003 or elongation < 4.0:
        return 0.0
    phase_contrast = illumination**0.42
    separation_contrast = 0.28 + 0.72 * min(
        1.0, max(0.0, (elongation - 4.0) / 55.0)
    )
    altitude_contrast = 0.32 + 0.68 * min(
        1.0, max(0.0, (float(moon_alt_deg) + 0.8) / 18.0)
    )
    daylight = min(1.0, max(0.0, (float(sun_alt_deg) + 6.0) / 36.0))
    contrast = phase_contrast * separation_contrast * altitude_contrast
    threshold = 0.09 + 0.08 * daylight
    return (
        0.0
        if contrast <= threshold
        else max(0.14, min(0.88, 0.14 + (contrast - threshold) * 1.25))
    )


@dataclass(frozen=True, slots=True)
class ResolvedEphemeris:
    """A coherent snapshot resolved from scientific data or one fallback."""

    snapshot: dict[str, Any]
    sun_altitude_deg: float
    sun_azimuth_deg: float
    eclipse_transmission: float
    source: str


def resolve_ephemeris_snapshot(
    payload: Any,
    *,
    year_utc: int,
    day_of_year_utc: int,
    ut_hour: float,
    latitude: float,
    longitude: float,
) -> ResolvedEphemeris:
    """Resolve one coherent scientific snapshot or a complete analytic fallback.

    A stale but structurally complete scientific snapshot remains coherent while
    the coordinator computes the next one.  We deliberately never combine its
    Moon/eclipse fields with a newer analytical Sun position.
    """

    source = dict(payload) if isinstance(payload, Mapping) else {}
    snapshot: dict[str, Any] = dict(source)
    for body_name in ("sun", "moon"):
        if isinstance(snapshot.get(body_name), Mapping):
            snapshot[body_name] = dict(snapshot[body_name])
    valid = snapshot_matches_utc_context(
        snapshot,
        year_utc=year_utc,
        day_of_year_utc=day_of_year_utc,
        ut_hour=ut_hour,
    )
    scientific = bool(
        snapshot.get("timestamp_utc")
        and all(
            isinstance(snapshot.get(body), Mapping)
            and all(key in snapshot[body] for key in ("alt", "az"))
            for body in ("sun", "moon")
        )
    )
    fallback_sun_alt, fallback_sun_az = fast_sun_altaz(
        ut_hour, day_of_year_utc, latitude, longitude, year_utc
    )
    if valid or scientific:
        sun = dict(snapshot.get("sun", {}))
        moon = dict(snapshot.get("moon", {}))
        source_name = "scientific" if valid else "stale_scientific"
    else:
        sun_alt, sun_az = fallback_sun_alt, fallback_sun_az
        moon_alt, moon_az, illumination = fast_moon_altaz(
            ut_hour, day_of_year_utc, latitude, longitude, year_utc
        )
        sun = {"alt": sun_alt, "az": sun_az, "rad_deg": 0.2666}
        moon = {
            "alt": moon_alt,
            "az": moon_az,
            "rad_deg": 0.2725,
            "illumination": illumination,
        }
        source_name = "analytic_fallback"
    sun_alt = float(sun.get("alt", fallback_sun_alt))
    sun_az = float(sun.get("az", fallback_sun_az))
    moon_alt = float(moon.get("alt", -90.0))
    moon_az = float(moon.get("az", 0.0))
    separation = float(
        moon.get(
            "sep_real",
            angular_separation_deg(sun_alt, sun_az, moon_alt, moon_az),
        )
        if scientific
        else angular_separation_deg(sun_alt, sun_az, moon_alt, moon_az)
    )
    sun_radius = max(1e-6, float(sun.get("rad_deg", 0.2666)))
    moon_radius = max(1e-6, float(moon.get("rad_deg", 0.2725)))
    transmission = (
        solar_disc_transmission(separation, sun_radius, moon_radius)
        if sun_alt > -2.0 and moon_alt > -2.0
        else 1.0
    )
    moon["sep_real"] = separation
    snapshot["sun"] = sun
    snapshot["moon"] = moon
    snapshot["eclipse_factor"] = transmission
    return ResolvedEphemeris(
        snapshot=snapshot,
        sun_altitude_deg=sun_alt,
        sun_azimuth_deg=sun_az,
        eclipse_transmission=transmission,
        source=source_name,
    )
