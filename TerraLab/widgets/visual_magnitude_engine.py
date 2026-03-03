from dataclasses import dataclass

from TerraLab.widgets.physical_math import (
    InstrumentOpticsMath,
    VisualPhotometryMath,
)


@dataclass
class VisualMagnitudeInputs:
    aperture_mm: float
    telescope_focal_mm: float
    eyepiece_focal_mm: float
    eye_pupil_mm: float
    atmospheric_loss_mag: float
    auto_bortle: bool
    bortle_class: float
    manual_eye_limit_mag: float
    exposure_seconds: float
    iso: float
    instrument_profile: str = "telescope"
    sensor_profile: str = "tiny"


@dataclass
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


class VisualMagnitudeEngine:
    def compute(self, inputs: VisualMagnitudeInputs) -> VisualMagnitudeResult:
        aperture_mm = max(1.0, float(inputs.aperture_mm))
        telescope_focal_mm = max(1.0, float(inputs.telescope_focal_mm))
        eyepiece_focal_mm = max(0.5, float(inputs.eyepiece_focal_mm))
        eye_pupil_mm = max(0.5, float(inputs.eye_pupil_mm))
        instrument_profile = str(getattr(inputs, "instrument_profile", "telescope"))
        sensor_profile = str(getattr(inputs, "sensor_profile", "tiny"))

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
            auto_bortle=bool(inputs.auto_bortle),
            bortle_class=float(inputs.bortle_class),
            manual_eye_limit_mag=float(inputs.manual_eye_limit_mag),
        )
        ntl_penalty_mag = max(0.0, 7.6 - eye_limit_mag)

        atmospheric_loss_mag = VisualPhotometryMath.atmospheric_loss_mag(inputs.atmospheric_loss_mag)
        overmagnification_penalty_mag = VisualPhotometryMath.overmagnification_penalty_mag(
            exit_pupil_mm=exit_pupil_mm,
            is_camera=is_camera,
        )
        exposure_gain_mag = VisualPhotometryMath.exposure_gain_mag(
            exposure_seconds=inputs.exposure_seconds,
            iso=inputs.iso,
        )
        sensor_bonus_mag = VisualPhotometryMath.sensor_bonus_mag(
            instrument_profile=instrument_profile,
            sensor_profile=sensor_profile,
            is_camera=is_camera,
        )

        scope_limit_mag = VisualPhotometryMath.scope_limit_mag(
            eye_limit_mag=eye_limit_mag,
            aperture_gain_mag=aperture_gain_mag,
            atmospheric_loss_mag=atmospheric_loss_mag,
            overmagnification_penalty_mag=overmagnification_penalty_mag,
            exposure_gain_mag=exposure_gain_mag,
            sensor_bonus_mag=sensor_bonus_mag,
        )
        star_scale_factor = VisualPhotometryMath.star_scale_factor(
            scope_limit_mag=scope_limit_mag,
            eye_limit_mag=eye_limit_mag,
            exposure_gain_mag=exposure_gain_mag,
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
            star_scale_factor=star_scale_factor,
        )

