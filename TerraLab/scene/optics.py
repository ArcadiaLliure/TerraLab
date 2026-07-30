"""Framework-free telescope and camera photometry helpers."""

from __future__ import annotations

import math


SENSOR_BASE_CONSTANT = 3.4
ISO_KNEE = 800.0
ISO_HIGH_GAIN_EFFICIENCY = 0.25
SHORT_EXPOSURE_REF_S = 5.0
SHORT_EXPOSURE_PENALTY_SLOPE = 1.6
MAG_LIMIT_NAKED_EYE_DARK = 6.5
ISO_EYE_REFERENCE = 800.0
EXPOSURE_EYE_REFERENCE_S = 15.0
EYE_PUPIL_MM = 7.0


def _effective_iso_term(iso: float) -> float:
    iso = max(1.0, float(iso))
    if iso <= 100.0:
        return 1.25 * math.log10(iso)
    knee_stops = math.log2(ISO_KNEE / 100.0)
    iso_stops = math.log2(iso / 100.0)
    effective_stops = (
        min(iso_stops, knee_stops)
        + max(0.0, iso_stops - knee_stops) * ISO_HIGH_GAIN_EFFICIENCY
    )
    return 1.25 * math.log10(100.0) + (
        1.25 * math.log10(2.0) * effective_stops
    )


def _short_exposure_read_noise_penalty(exposure_seconds: float) -> float:
    exposure = max(1e-3, float(exposure_seconds))
    if exposure >= SHORT_EXPOSURE_REF_S:
        return 0.0
    return SHORT_EXPOSURE_PENALTY_SLOPE * math.log10(
        SHORT_EXPOSURE_REF_S / exposure
    )


def calculate_mag_limit(
    focal_mm: float,
    aperture_mm: float,
    iso: float,
    exposure_seconds: float,
    sensor_constant: float = SENSOR_BASE_CONSTANT,
) -> float:
    """Compute the calibrated limiting magnitude for one optical setup."""

    focal = float(focal_mm)
    aperture = float(aperture_mm)
    iso_value = float(iso)
    exposure = float(exposure_seconds)
    if focal <= 0.0 or aperture <= 0.0 or iso_value <= 0.0 or exposure <= 0.0:
        return 0.0
    f_ratio = focal / aperture
    return float(
        sensor_constant
        + 2.5 * math.log10(aperture * aperture)
        + 1.25 * math.log10(exposure)
        + _effective_iso_term(iso_value)
        - 5.0 * math.log10(max(1e-6, f_ratio))
        - _short_exposure_read_noise_penalty(exposure)
    )


def calculate_star_brightness(magnitude: float, mag_limit: float) -> float:
    if float(magnitude) > float(mag_limit):
        return 0.0
    delta_mag = float(mag_limit) - float(magnitude)
    flux_relative = 10.0 ** (delta_mag / 2.5)
    flux_saturation = 10.0 ** (8.0 / 2.5)
    return float(min(1.0, flux_relative / flux_saturation) ** 0.4)


def calculate_star_radius_px(
    magnitude: float, mag_limit: float, mag_saturation: float = -1.5
) -> float:
    if float(magnitude) > float(mag_limit):
        return 0.0
    span = max(1e-6, float(mag_limit) - float(mag_saturation))
    position = (float(mag_limit) - float(magnitude)) / span
    radius = 0.5 + max(0.0, min(1.0, position)) * 3.5
    return float(max(0.5, min(4.0, radius)))


def calculate_telescope_parameters(
    focal_mm: float,
    aperture_mm: float,
    iso: float,
    exposure_seconds: float,
    sensor_constant: float = SENSOR_BASE_CONSTANT,
) -> dict:
    focal = max(1e-6, float(focal_mm))
    aperture = max(1e-6, float(aperture_mm))
    return {
        "mag_limit": calculate_mag_limit(
            focal_mm=focal,
            aperture_mm=aperture,
            iso=iso,
            exposure_seconds=exposure_seconds,
            sensor_constant=sensor_constant,
        ),
        "f_ratio": focal / aperture,
        "fov_graus": math.degrees(2.0 * math.atan(43.3 / (2.0 * focal))),
        "magnification": focal / 25.0,
        "light_power": (aperture / EYE_PUPIL_MM) ** 2,
    }


calcular_mag_limit = calculate_mag_limit
calcular_brillantor_estrella = calculate_star_brightness
calcular_mida_estrella_px = calculate_star_radius_px
calcular_parametres_telescopi = calculate_telescope_parameters
