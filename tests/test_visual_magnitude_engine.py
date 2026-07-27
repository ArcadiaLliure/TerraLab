import math

from TerraLab.widgets.optica_telescopica import calculate_mag_limit
from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    LP_MODE_MAGNITUDE,
)
from TerraLab.widgets.visual_magnitude_engine import (
    VisualMagnitudeEngine,
    VisualMagnitudeInputs,
)


def _base_inputs(**overrides):
    base = dict(
        aperture_mm=70.0,
        telescope_focal_mm=350.0,
        eyepiece_focal_mm=25.0,
        eye_pupil_mm=7.0,
        atmospheric_loss_mag=0.0,
        light_pollution_mode=LP_MODE_MAGNITUDE,
        bortle_class=1.0,
        magnitude_limit=6.0,
        exposure_seconds=1.0,
        iso=100.0,
    )
    base.update(overrides)
    return VisualMagnitudeInputs(**base)


def test_aperture_gain_matches_reference_formula():
    eng = VisualMagnitudeEngine()
    res = eng.compute(_base_inputs())

    expected_gain = 5.0 * math.log10(70.0 / 7.0)
    expected_raw = calculate_mag_limit(
        focal_mm=350.0,
        aperture_mm=70.0,
        iso=100.0,
        exposure_seconds=1.0,
    )
    expected_scope = expected_raw - (7.6 - 6.0)
    assert abs(res.aperture_gain_mag - expected_gain) < 1e-9
    assert abs(res.raw_sensor_limit_mag - expected_raw) < 1e-9
    assert abs(res.scope_limit_mag - expected_scope) < 1e-9


def test_automatic_bortle_drives_eye_limit():
    eng = VisualMagnitudeEngine()
    res = eng.compute(
        _base_inputs(
            light_pollution_mode=LP_MODE_AUTOMATIC,
            bortle_class=9.0,
        )
    )

    assert abs(res.eye_limit_mag - 3.6) < 1e-6
    assert res.ntl_penalty_mag > 3.5


def test_exposure_interdependence_increases_limiting_magnitude():
    eng = VisualMagnitudeEngine()
    base = eng.compute(_base_inputs(exposure_seconds=1.0, iso=100.0))
    deep = eng.compute(_base_inputs(exposure_seconds=60.0, iso=3200.0))

    assert deep.raw_sensor_limit_mag > base.raw_sensor_limit_mag
    assert deep.exposure_gain_mag > base.exposure_gain_mag
    assert deep.scope_limit_mag > base.scope_limit_mag
    assert deep.star_scale_factor >= base.star_scale_factor


def test_atmospheric_loss_penalizes_limit():
    eng = VisualMagnitudeEngine()
    no_loss = eng.compute(_base_inputs(atmospheric_loss_mag=0.0))
    with_loss = eng.compute(_base_inputs(atmospheric_loss_mag=0.9))

    assert with_loss.atmospheric_loss_mag > no_loss.atmospheric_loss_mag
    assert with_loss.scope_limit_mag < no_loss.scope_limit_mag


def test_camera_profiles_apply_different_sensor_bonus():
    eng = VisualMagnitudeEngine()
    apsc = eng.compute(
        _base_inputs(
            instrument_profile="camera_aps_c",
            sensor_profile="aps_c",
            eyepiece_focal_mm=350.0,
        )
    )
    ff = eng.compute(
        _base_inputs(
            instrument_profile="camera_full_frame",
            sensor_profile="full_frame",
            eyepiece_focal_mm=350.0,
        )
    )

    assert ff.scope_limit_mag > apsc.scope_limit_mag
    assert abs(ff.magnification - 1.0) < 1e-12
    assert abs(apsc.magnification - 1.0) < 1e-12
