"""Pure photometric models shared by scene planners and application services."""

from __future__ import annotations

import math
from dataclasses import dataclass

from TerraLab.light_pollution.modes import mode_uses_bortle
from TerraLab.scene.optics import (
    EXPOSURE_EYE_REFERENCE_S,
    ISO_EYE_REFERENCE,
    calculate_mag_limit,
)
from TerraLab.scene.photometry_math import (
    InstrumentOpticsMath,
    VisualPhotometryMath,
)


DEFAULT_RENDER_MLIM_COMPENSATION_MAG = 0.0
RENDER_MLIM_COMPENSATION_DESCRIPTION = (
    "Compensa penalitzacions visuals addicionals del pipeline de render; "
    "manteniu-la a 0.0 si es prioritza realisme físic."
)


@dataclass(frozen=True, slots=True)
class VisualMagnitudeInputs:
    aperture_mm: float
    telescope_focal_mm: float
    eyepiece_focal_mm: float
    eye_pupil_mm: float
    atmospheric_loss_mag: float
    light_pollution_mode: str
    bortle_class: float
    magnitude_limit: float
    exposure_seconds: float
    iso: float
    instrument_profile: str = "telescope"
    sensor_profile: str = "tiny"


@dataclass(frozen=True, slots=True)
class VisualMagnitudeResult:
    eye_limit_mag: float
    scope_limit_mag: float
    magnification: float
    exit_pupil_mm: float
    effective_aperture_mm: float
    atmospheric_loss_mag: float
    exposure_gain_mag: float
    aperture_gain_mag: float
    ntl_penalty_mag: float
    star_scale_factor: float
    raw_sensor_limit_mag: float = 0.0
    f_ratio: float = 1.0
    sensor_bonus_mag: float = 0.0


class VisualMagnitudeEngine:
    """Compute scope visibility without Qt or rendering dependencies."""

    def compute(self, inputs: VisualMagnitudeInputs) -> VisualMagnitudeResult:
        aperture_mm = max(1.0, float(inputs.aperture_mm))
        telescope_focal_mm = max(1.0, float(inputs.telescope_focal_mm))
        eyepiece_focal_mm = max(0.5, float(inputs.eyepiece_focal_mm))
        eye_pupil_mm = max(0.5, float(inputs.eye_pupil_mm))
        instrument_profile = str(inputs.instrument_profile)
        sensor_profile = str(inputs.sensor_profile)
        is_camera = InstrumentOpticsMath.is_camera_profile(instrument_profile)
        magnification = InstrumentOpticsMath.magnification(
            telescope_focal_mm=telescope_focal_mm,
            eyepiece_focal_mm=eyepiece_focal_mm,
            is_camera=is_camera,
        )
        exit_pupil_mm = InstrumentOpticsMath.exit_pupil_mm(
            aperture_mm=aperture_mm,
            magnification=magnification,
            is_camera=is_camera,
        )
        effective_aperture_mm = InstrumentOpticsMath.effective_aperture_mm(
            aperture_mm=aperture_mm,
            exit_pupil_mm=exit_pupil_mm,
            eye_pupil_mm=eye_pupil_mm,
            is_camera=is_camera,
        )
        aperture_gain_mag = InstrumentOpticsMath.aperture_gain_mag(
            effective_aperture_mm=effective_aperture_mm,
            eye_pupil_mm=eye_pupil_mm,
        )
        eye_limit_mag = VisualPhotometryMath.eye_limit_mag(
            light_pollution_mode=inputs.light_pollution_mode,
            bortle_class=float(inputs.bortle_class),
            magnitude_limit=float(inputs.magnitude_limit),
        )
        ntl_penalty_mag = max(0.0, 7.6 - eye_limit_mag)
        atmospheric_loss_mag = VisualPhotometryMath.atmospheric_loss_mag(
            inputs.atmospheric_loss_mag
        )
        sensor_bonus_mag = VisualPhotometryMath.sensor_bonus_mag(
            instrument_profile=instrument_profile,
            sensor_profile=sensor_profile,
            is_camera=is_camera,
        )
        raw_sensor_limit_mag = calculate_mag_limit(
            focal_mm=telescope_focal_mm,
            aperture_mm=aperture_mm,
            iso=float(inputs.iso),
            exposure_seconds=float(inputs.exposure_seconds),
        )
        scope_limit_mag = VisualPhotometryMath.clamp(
            raw_sensor_limit_mag
            + float(sensor_bonus_mag)
            - atmospheric_loss_mag
            - ntl_penalty_mag,
            -12.0,
            22.0,
        )
        exposure_ratio = max(
            1e-6,
            (float(inputs.exposure_seconds) * max(1.0, float(inputs.iso)))
            / (EXPOSURE_EYE_REFERENCE_S * ISO_EYE_REFERENCE),
        )
        exposure_gain_mag = VisualPhotometryMath.clamp(
            1.25 * math.log10(exposure_ratio), -8.0, 12.0
        )
        return VisualMagnitudeResult(
            eye_limit_mag=eye_limit_mag,
            scope_limit_mag=scope_limit_mag,
            magnification=magnification,
            exit_pupil_mm=exit_pupil_mm,
            effective_aperture_mm=effective_aperture_mm,
            atmospheric_loss_mag=atmospheric_loss_mag,
            exposure_gain_mag=exposure_gain_mag,
            aperture_gain_mag=aperture_gain_mag,
            ntl_penalty_mag=ntl_penalty_mag,
            star_scale_factor=VisualPhotometryMath.star_scale_factor(
                scope_limit_mag=scope_limit_mag,
                eye_limit_mag=eye_limit_mag,
                exposure_gain_mag=exposure_gain_mag,
            ),
            raw_sensor_limit_mag=float(raw_sensor_limit_mag),
            f_ratio=telescope_focal_mm / max(1.0, aperture_mm),
            sensor_bonus_mag=float(sensor_bonus_mag),
        )


def update_star_rendering_params(state: dict) -> dict:
    """Resolve the already-selected visibility limit for star rendering."""

    scope_enabled = bool(state.get("scope_enabled", False))
    light_pollution_mode = state.get("light_pollution_mode")
    bortle = max(1.0, min(9.0, float(state.get("bortle", 1.0))))
    scope_mlim = float(state.get("scope_mlim", 6.0))
    magnitude_limit = float(state.get("magnitude_limit", 6.0))
    compensation = float(
        state.get(
            "render_compensation_mag", DEFAULT_RENDER_MLIM_COMPENSATION_MAG
        )
    )
    if mode_uses_bortle(light_pollution_mode):
        general_mlim, physical_nelm = (
            VisualPhotometryMath.general_render_limit_mag(
                bortle_class=bortle,
                render_compensation_mag=compensation,
            )
        )
    else:
        general_mlim = magnitude_limit
        physical_nelm = magnitude_limit
    general_mlim = max(-27.0, min(30.0, general_mlim))
    state["general_mlim_physical"] = float(physical_nelm)
    state["general_mlim_compensation_mag"] = float(
        compensation if mode_uses_bortle(light_pollution_mode) else 0.0
    )
    state["general_mlim_compensation_description"] = (
        RENDER_MLIM_COMPENSATION_DESCRIPTION
    )
    state["general_mlim"] = general_mlim
    state["scope_mlim"] = scope_mlim
    state["render_mag_limit"] = scope_mlim if scope_enabled else general_mlim
    return state
