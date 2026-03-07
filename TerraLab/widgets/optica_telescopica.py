"""Realistic telescope/camera photometry helpers.

This module centralizes the physical model used to derive limiting magnitude
from focal length, aperture, ISO and exposure time.
"""

from __future__ import annotations

import math


# Empirical calibration constant for TerraLab scope rendering.
# Calibrated so camera-like settings (e.g. ~250 mm, f/2.8, ISO 6400, 30 s)
# land in a realistic deep-sky limit range for the current catalog.
SENSOR_BASE_CONSTANT = 3.4
MAG_LIMIT_NAKED_EYE_DARK = 6.5
ISO_EYE_REFERENCE = 800.0
EXPOSURE_EYE_REFERENCE_S = 15.0
EYE_PUPIL_MM = 7.0


def calculate_mag_limit(
    focal_mm: float,
    aperture_mm: float,
    iso: float,
    exposure_seconds: float,
    sensor_constant: float = SENSOR_BASE_CONSTANT,
) -> float:
    """Compute the system limiting magnitude.

    Formula:
      m_lim = C + 2.5*log10(D^2) + 1.25*log10(t) + 1.25*log10(ISO) - 5*log10(f_ratio)
    where f_ratio = focal/aperture.
    """
    focal_mm = float(focal_mm)
    aperture_mm = float(aperture_mm)
    iso = float(iso)
    exposure_seconds = float(exposure_seconds)
    if focal_mm <= 0.0 or aperture_mm <= 0.0 or iso <= 0.0 or exposure_seconds <= 0.0:
        return 0.0

    f_ratio = focal_mm / aperture_mm
    term_aperture = 2.5 * math.log10(aperture_mm * aperture_mm)
    term_exposure = 1.25 * math.log10(exposure_seconds)
    term_iso = 1.25 * math.log10(iso)
    term_f_ratio = -5.0 * math.log10(max(1e-6, f_ratio))
    return float(sensor_constant + term_aperture + term_exposure + term_iso + term_f_ratio)


def calculate_star_brightness(magnitude: float, mag_limit: float) -> float:
    """Map apparent magnitude to normalized brightness [0..1]."""
    magnitude = float(magnitude)
    mag_limit = float(mag_limit)
    if magnitude > mag_limit:
        return 0.0

    delta_mag = mag_limit - magnitude
    flux_relative = 10.0 ** (delta_mag / 2.5)
    flux_sat = 10.0 ** (8.0 / 2.5)
    base = min(1.0, flux_relative / flux_sat)
    return float(base ** 0.4)


def calculate_star_radius_px(magnitude: float, mag_limit: float, mag_saturation: float = -1.5) -> float:
    """Map apparent magnitude to screen radius in pixels."""
    magnitude = float(magnitude)
    mag_limit = float(mag_limit)
    if magnitude > mag_limit:
        return 0.0

    mag_saturation = float(mag_saturation)
    span = max(1e-6, mag_limit - mag_saturation)
    pos = (mag_limit - magnitude) / span
    radius = 0.5 + max(0.0, min(1.0, pos)) * (4.0 - 0.5)
    return float(max(0.5, min(4.0, radius)))


def calculate_telescope_parameters(
    focal_mm: float,
    aperture_mm: float,
    iso: float,
    exposure_seconds: float,
    sensor_constant: float = SENSOR_BASE_CONSTANT,
) -> dict:
    """Return optical/photometric summary values for HUD/debug usage."""
    focal_mm = max(1e-6, float(focal_mm))
    aperture_mm = max(1e-6, float(aperture_mm))
    f_ratio = focal_mm / aperture_mm
    mag_limit = calculate_mag_limit(
        focal_mm=focal_mm,
        aperture_mm=aperture_mm,
        iso=iso,
        exposure_seconds=exposure_seconds,
        sensor_constant=sensor_constant,
    )
    sensor_diag_mm = 43.3
    fov_deg = math.degrees(2.0 * math.atan(sensor_diag_mm / (2.0 * focal_mm)))
    magnification = focal_mm / 25.0
    light_power = (aperture_mm / EYE_PUPIL_MM) ** 2
    return {
        "mag_limit": float(mag_limit),
        "f_ratio": float(f_ratio),
        "fov_graus": float(fov_deg),
        "magnification": float(magnification),
        "light_power": float(light_power),
    }


# Backward-compatible aliases (Catalan naming used in prior docs/snippets).
calcular_mag_limit = calculate_mag_limit
calcular_brillantor_estrella = calculate_star_brightness
calcular_mida_estrella_px = calculate_star_radius_px
calcular_parametres_telescopi = calculate_telescope_parameters
