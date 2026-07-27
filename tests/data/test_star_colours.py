from __future__ import annotations

import numpy as np

from TerraLab.util.color import _bp_rp_formula_arrays, bp_rp_to_rgb_arrays


def test_star_colour_lut_stays_within_one_channel_level():
    values = np.linspace(-0.5, 2.5, 20_003, dtype=np.float32)
    expected = _bp_rp_formula_arrays(values)
    actual = bp_rp_to_rgb_arrays(values)
    for expected_channel, actual_channel in zip(expected, actual):
        delta = np.abs(
            expected_channel.astype(np.int16) - actual_channel.astype(np.int16)
        )
        assert int(delta.max(initial=0)) <= 1
