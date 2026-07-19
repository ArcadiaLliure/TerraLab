from TerraLab.render.sky.milkyway_overlay import MilkyWayOverlay
from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    LP_MODE_MAGNITUDE,
)
from TerraLab.scene.camera import Camera
from TerraLab.scene.scene_state import SceneState


def _state_with_overlay(**overlay_cfg) -> SceneState:
    state = SceneState(
        camera=Camera(),
        latitude=41.4,
        longitude=2.1,
        ut_hour=22.0,
        day_of_year=120,
        sun_alt=-25.0,
        bortle=2.0,
        light_pollution_mode=LP_MODE_AUTOMATIC,
        magnitude_limit=6.5,
    )
    base_cfg = {
        "enabled": True,
        "texture_path": "data/sky/milkyway_overlay.png",
        "opacity": 1.0,
        "blend_mode": "normal",
        "ra_offset_deg": 180.0,
        "coord_frame": "galactic",
        "lat_flip": True,
        "lon_flip": True,
        "sample_scale": 1.0,
        "auto_opacity": True,
        "light_pollution_mode": LP_MODE_AUTOMATIC,
        "bortle": 2.0,
        "magnitude_limit": 6.5,
        "scope_enabled": False,
        "scope_iso": 800.0,
        "scope_exposure_s": 15.0,
        "scope_aperture_f_number": 2.8,
        "dust_map_enabled": False,
        "dust_map_path": "data/sky/derived/missing.npz",
        "dust_density_strength": 0.0,
        "dust_extinction_strength": 0.0,
    }
    base_cfg.update(overlay_cfg)
    state.extras = {"milkyway_overlay": base_cfg}
    return state


def test_sample_rgba_is_stable_for_same_query():
    overlay = MilkyWayOverlay()
    a = overlay.sample_rgba_at_radec(
        ra_deg=120.0, dec_deg=-15.0, ra_offset_deg=180.0
    )
    b = overlay.sample_rgba_at_radec(
        ra_deg=120.0, dec_deg=-15.0, ra_offset_deg=180.0
    )
    assert a is not None
    assert a == b


def test_sample_rgba_changes_with_ra_offset():
    overlay = MilkyWayOverlay()
    a = overlay.sample_rgba_at_radec(
        ra_deg=75.0, dec_deg=0.0, ra_offset_deg=0.0
    )
    b = overlay.sample_rgba_at_radec(
        ra_deg=75.0, dec_deg=0.0, ra_offset_deg=180.0
    )
    assert a is not None
    assert b is not None
    assert a != b


def test_effective_opacity_is_zero_in_daylight():
    overlay = MilkyWayOverlay()
    state = _state_with_overlay()
    state.sun_alt = 5.0
    cfg = overlay._read_config(state)
    opacity, reason = overlay._compute_effective_opacity(state, cfg)
    assert opacity == 0.0
    assert reason == "daylight_or_civil_twilight"


def test_effective_opacity_fades_with_bortle():
    overlay = MilkyWayOverlay()
    dark = _state_with_overlay(bortle=1.0)
    bright = _state_with_overlay(bortle=6.0)
    cfg_dark = overlay._read_config(dark)
    cfg_bright = overlay._read_config(bright)
    op_dark, _ = overlay._compute_effective_opacity(dark, cfg_dark)
    op_bright, _ = overlay._compute_effective_opacity(bright, cfg_bright)
    assert op_dark > op_bright


def test_effective_opacity_manual_mode_uses_mag_limit():
    overlay = MilkyWayOverlay()
    low = _state_with_overlay(
        light_pollution_mode=LP_MODE_MAGNITUDE,
        auto_opacity=True,
        magnitude_limit=4.0,
    )
    high = _state_with_overlay(
        light_pollution_mode=LP_MODE_MAGNITUDE,
        auto_opacity=True,
        magnitude_limit=8.0,
    )
    cfg_low = overlay._read_config(low)
    cfg_high = overlay._read_config(high)
    op_low, _ = overlay._compute_effective_opacity(low, cfg_low)
    op_high, _ = overlay._compute_effective_opacity(high, cfg_high)
    assert op_high > op_low
