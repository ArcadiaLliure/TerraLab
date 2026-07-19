from TerraLab.widgets.telescope_runtime import (
    compute_airmass,
    compute_extinction_k,
    on_resize,
    update_star_rendering_params,
)
from TerraLab.light_pollution.modes import LP_MODE_AUTOMATIC


def test_compute_airmass_handles_horizon_and_high_altitude():
    assert compute_airmass(-1.0) is None
    x_30 = compute_airmass(30.0)
    assert x_30 is not None
    assert abs(x_30 - 2.0) < 1e-6


def test_compute_airmass_low_altitude_kasten_young():
    x_10 = compute_airmass(10.0)
    assert x_10 is not None
    assert x_10 > 2.0


def test_compute_extinction_k_uses_fallback_without_data():
    k = compute_extinction_k(None, None, k_fallback=0.22)
    assert abs(k - 0.22) < 1e-12


def test_compute_extinction_k_from_aod_and_pressure():
    k = compute_extinction_k(0.12, 1000.0, k_fallback=0.20)
    expected = 1.086 * (0.12 + 0.00879 * (1000.0 / 1013.25))
    assert abs(k - expected) < 1e-9


def test_on_resize_keeps_button_attached_to_panel():
    st = {
        "panel_x": 100,
        "panel_y": 500,
        "panel_w": 800,
        "button_w": 30,
        "button_h": 24,
        "margin_right": 20,
        "overlap_top": 1,
    }
    on_resize(st)
    assert st["collapse_button_pos"] == (850, 477)


def test_update_star_rendering_params_isolates_general_and_scope_modes():
    st_general = {
        "scope_enabled": False,
        "light_pollution_mode": LP_MODE_AUTOMATIC,
        "bortle": 9.0,
        "scope_mlim": 9.5,
        "magnitude_limit": 6.0,
    }
    update_star_rendering_params(st_general)
    assert abs(st_general["general_mlim_physical"] - 3.6) < 1e-12
    assert abs(st_general["general_mlim_compensation_mag"] - 0.0) < 1e-12
    assert abs(st_general["general_mlim"] - 3.6) < 1e-12
    assert (
        abs(st_general["render_mag_limit"] - st_general["general_mlim"])
        < 1e-12
    )

    st_scope = dict(st_general)
    st_scope["scope_enabled"] = True
    update_star_rendering_params(st_scope)
    assert abs(st_scope["render_mag_limit"] - 9.5) < 1e-12
