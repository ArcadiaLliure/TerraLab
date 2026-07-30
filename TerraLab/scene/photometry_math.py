"""Framework-free atmospheric, optical, and visual-photometry equations."""

from __future__ import annotations

import math

from TerraLab.light_pollution.modes import mode_uses_bortle


class AtmosphericMath:
    """Atmospheric extinction and transmission calculations."""

    @staticmethod
    def airmass_from_altitude_deg(h_deg):
        if h_deg is None or h_deg <= 0.0:
            return None
        if h_deg >= 30.0:
            return 1.0 / max(1e-6, math.sin(math.radians(h_deg)))
        return 1.0 / (
            math.sin(math.radians(h_deg))
            + 0.50572 * ((h_deg + 6.07995) ** (-1.6364))
        )

    @staticmethod
    def extinction_k_mag_per_airmass(aod, pressure_hpa, k_fallback=0.20):
        if aod is None or pressure_hpa is None:
            return float(k_fallback)
        tau_aer = max(0.0, float(aod))
        tau_rayleigh = 0.00879 * (float(pressure_hpa) / 1013.25)
        return 1.086 * (tau_aer + tau_rayleigh)

    @staticmethod
    def loss_mag_from_k_airmass(extinction_k_mag_airmass, airmass_x):
        if airmass_x is None:
            return None
        return float(extinction_k_mag_airmass) * float(airmass_x)

    @staticmethod
    def transmission_from_loss_mag(loss_mag):
        if loss_mag is None:
            return None
        return 10.0 ** (-0.4 * float(loss_mag))


class InstrumentOpticsMath:
    """Equations for the telescope or camera optical train."""

    @staticmethod
    def is_camera_profile(instrument_profile: str) -> bool:
        return str(instrument_profile) in (
            "camera_aps_c",
            "camera_full_frame",
        )

    @staticmethod
    def magnification(telescope_focal_mm, eyepiece_focal_mm, is_camera):
        if is_camera:
            return 1.0
        return max(
            0.1,
            float(telescope_focal_mm) / max(0.5, float(eyepiece_focal_mm)),
        )

    @staticmethod
    def exit_pupil_mm(aperture_mm, magnification, is_camera):
        if is_camera:
            return max(1.0, float(aperture_mm))
        return max(
            1e-6,
            float(aperture_mm) / max(1e-6, float(magnification)),
        )

    @staticmethod
    def effective_aperture_mm(
        aperture_mm, exit_pupil_mm, eye_pupil_mm, is_camera
    ):
        aperture = max(1.0, float(aperture_mm))
        if is_camera:
            return aperture
        if exit_pupil_mm > eye_pupil_mm:
            aperture *= float(eye_pupil_mm) / max(1e-6, float(exit_pupil_mm))
        return max(0.1, aperture)

    @staticmethod
    def aperture_gain_mag(effective_aperture_mm, eye_pupil_mm):
        return 5.0 * math.log10(
            max(0.1, float(effective_aperture_mm))
            / max(0.5, float(eye_pupil_mm))
        )


class VisualPhotometryMath:
    """Visibility-limit calculations independent from any presentation layer."""

    @staticmethod
    def clamp(value, low, high):
        return max(low, min(high, float(value)))

    @staticmethod
    def bortle_to_nelm_mag(bortle_class):
        bortle = VisualPhotometryMath.clamp(bortle_class, 1.0, 9.0)
        return 7.6 - 0.5 * (bortle - 1.0)

    @staticmethod
    def eye_limit_mag(light_pollution_mode, bortle_class, magnitude_limit):
        if mode_uses_bortle(light_pollution_mode):
            return VisualPhotometryMath.bortle_to_nelm_mag(float(bortle_class))
        return float(magnitude_limit)

    @staticmethod
    def atmospheric_loss_mag(value):
        return max(0.0, float(value))

    @staticmethod
    def overmagnification_penalty_mag(exit_pupil_mm, is_camera):
        if is_camera:
            return 0.0
        return max(0.0, (0.7 - float(exit_pupil_mm)) * 0.35)

    @staticmethod
    def exposure_gain_mag(exposure_seconds, iso):
        ratio = max(
            1e-4,
            float(exposure_seconds) * max(1.0, float(iso)) / 100.0,
        )
        return VisualPhotometryMath.clamp(1.25 * math.log10(ratio), -3.0, 8.0)

    @staticmethod
    def sensor_bonus_mag(instrument_profile, sensor_profile, is_camera):
        if not is_camera:
            return 0.0
        if (
            instrument_profile == "camera_full_frame"
            or sensor_profile == "full_frame"
        ):
            return 0.35
        return 0.15

    @staticmethod
    def scope_limit_mag(
        eye_limit_mag,
        aperture_gain_mag,
        atmospheric_loss_mag,
        overmagnification_penalty_mag,
        exposure_gain_mag,
        sensor_bonus_mag,
    ):
        return VisualPhotometryMath.clamp(
            float(eye_limit_mag)
            + float(aperture_gain_mag)
            - float(atmospheric_loss_mag)
            - float(overmagnification_penalty_mag)
            + float(exposure_gain_mag)
            + float(sensor_bonus_mag),
            -12.0,
            18.0,
        )

    @staticmethod
    def star_scale_factor(scope_limit_mag, eye_limit_mag, exposure_gain_mag):
        depth_bonus = max(0.0, float(scope_limit_mag) - float(eye_limit_mag))
        factor = (
            0.90
            + 0.08 * depth_bonus
            + 0.08 * max(0.0, float(exposure_gain_mag))
        )
        return VisualPhotometryMath.clamp(factor, 0.70, 3.50)

    @staticmethod
    def general_render_limit_mag(bortle_class, render_compensation_mag=0.0):
        physical_nelm = VisualPhotometryMath.bortle_to_nelm_mag(bortle_class)
        return physical_nelm + float(render_compensation_mag), physical_nelm


__all__ = [
    "AtmosphericMath",
    "InstrumentOpticsMath",
    "VisualPhotometryMath",
]
