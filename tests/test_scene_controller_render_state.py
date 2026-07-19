from __future__ import annotations

from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from TerraLab.scene.scene_controller import SceneController
from TerraLab.light_pollution.modes import LP_MODE_AUTOMATIC


def test_scene_controller_builds_immutable_render_state_with_legacy_aliases():
    controller = SceneController(
        latitude=41.4,
        longitude=2.17,
        manual_year=2026,
        manual_day=80,
        manual_hour=22.5,
        use_real_time=False,
        scope_enabled=True,
        scope_center=(20.0, 130.0),
        scope_fov_deg=(2.0, 1.5),
    )
    star_payload = {
        "ra": np.array([10.0, 12.0], dtype=np.float32),
        "dec": np.array([30.0, 32.0], dtype=np.float32),
        "mag": np.array([1.0, 2.0], dtype=np.float32),
        "color_r": np.array([1.0, 0.8], dtype=np.float32),
        "color_g": np.array([0.9, 0.7], dtype=np.float32),
        "color_b": np.array([0.7, 0.6], dtype=np.float32),
        "bp_rp": np.array([0.3, 0.6], dtype=np.float32),
        "loaded_tile_ids": {"tile_all"},
    }

    state = controller.build_render_state(
        star_data=star_payload,
        horizon={"profile": "ok"},
        ephemeris={"sun": {"alt": -18.0}},
        sun_alt=-12.0,
        sun_az=220.0,
        layers_enabled={"stars"},
        extras={"scope_mask_fn": lambda alt, az: alt > 0},
    )

    assert state.day_of_year == 80
    assert state.day_of_year_utc == 80
    assert state.magnitude_limit == pytest.approx(controller.mag_limit)
    assert state.light_pollution_mode == LP_MODE_AUTOMATIC
    assert state.scope_fov_w == pytest.approx(2.0)
    assert state.scope_fov_h == pytest.approx(1.5)
    assert state.color_r.shape[0] == 2
    assert state.ra.flags.writeable is False
    assert callable(state.scope_mask_fn)

    with pytest.raises(FrozenInstanceError):
        state.mag_limit = 9.0


def test_scene_controller_uses_explicit_utc_context_when_provided():
    controller = SceneController(
        latitude=41.4,
        longitude=2.17,
        manual_year=2026,
        manual_day=80,
        manual_hour=22.5,
        use_real_time=False,
    )
    star_payload = {
        "ra": np.array([10.0], dtype=np.float32),
        "dec": np.array([30.0], dtype=np.float32),
        "mag": np.array([1.0], dtype=np.float32),
        "color_r": np.array([1.0], dtype=np.float32),
        "color_g": np.array([0.9], dtype=np.float32),
        "color_b": np.array([0.7], dtype=np.float32),
        "bp_rp": np.array([0.3], dtype=np.float32),
    }

    state = controller.build_render_state(
        star_data=star_payload,
        horizon=None,
        ephemeris=None,
        ut_hour_utc=23.25,
        day_of_year_utc=81,
        year_utc=2027,
    )

    assert state.ut_hour == pytest.approx(23.25)
    assert state.day_of_year_utc == 81
    assert state.year_utc == 2027
