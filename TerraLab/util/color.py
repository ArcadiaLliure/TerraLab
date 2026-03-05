"""Color utilities for stellar rendering."""

from __future__ import annotations

import math
from typing import Sequence, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

from TerraLab.util.math2d import clamp, lerp, saturate


# Blue -> White -> Yellow -> Orange -> Red
_COLOR_NODES = (
    (-0.5, (155, 187, 255)),
    (0.0, (175, 205, 255)),
    (0.5, (235, 238, 255)),
    (1.0, (255, 244, 214)),
    (1.6, (255, 212, 150)),
    (2.5, (255, 174, 120)),
)


def _mix_rgb(a: Sequence[int], b: Sequence[int], t: float) -> Tuple[int, int, int]:
    return (
        int(clamp(round(lerp(float(a[0]), float(b[0]), t)), 0.0, 255.0)),
        int(clamp(round(lerp(float(a[1]), float(b[1]), t)), 0.0, 255.0)),
        int(clamp(round(lerp(float(a[2]), float(b[2]), t)), 0.0, 255.0)),
    )


def color_from_bp_rp(bp_rp: float, pure_colors: bool = True, desaturate_mix: float = 0.30) -> Tuple[int, int, int]:
    """Convert Gaia BP-RP index into plausible RGB."""
    v = 0.8 if bp_rp is None else float(bp_rp)
    if not math.isfinite(v):
        v = 0.8

    if v <= _COLOR_NODES[0][0]:
        rgb = _COLOR_NODES[0][1]
    elif v >= _COLOR_NODES[-1][0]:
        rgb = _COLOR_NODES[-1][1]
    else:
        rgb = _COLOR_NODES[-1][1]
        for idx in range(len(_COLOR_NODES) - 1):
            x0, c0 = _COLOR_NODES[idx]
            x1, c1 = _COLOR_NODES[idx + 1]
            if x0 <= v <= x1:
                t = saturate((v - x0) / max(1e-9, (x1 - x0)))
                rgb = _mix_rgb(c0, c1, t)
                break

    if pure_colors:
        return rgb

    # Visual desaturation for more natural faint stars.
    wm = saturate(desaturate_mix)
    return _mix_rgb(rgb, (230, 232, 240), wm)


def bp_rp_to_rgb_arrays(bp_rp):
    """Vectorized BP-RP to RGB arrays for bulk catalog preprocessing."""
    if np is None:
        return None, None, None
    arr = np.asarray(bp_rp, dtype=np.float32)
    arr = np.nan_to_num(arr, nan=0.8, posinf=2.5, neginf=-0.5)

    r = np.empty(arr.shape, dtype=np.uint8)
    g = np.empty(arr.shape, dtype=np.uint8)
    b = np.empty(arr.shape, dtype=np.uint8)

    # Use piecewise masks aligned with COLOR_NODES to avoid Python loops.
    m0 = arr <= 0.0
    m1 = (arr > 0.0) & (arr <= 0.5)
    m2 = (arr > 0.5) & (arr <= 1.0)
    m3 = (arr > 1.0) & (arr <= 1.6)
    m4 = arr > 1.6

    r[m0] = 175
    g[m0] = np.clip(205 + (arr[m0] * 36.0), 187, 223).astype(np.uint8)
    b[m0] = 255

    if np.any(m1):
        t = (arr[m1] - 0.0) / 0.5
        r[m1] = np.clip(175 + 60.0 * t, 0, 255).astype(np.uint8)
        g[m1] = np.clip(205 + 33.0 * t, 0, 255).astype(np.uint8)
        b[m1] = 255

    if np.any(m2):
        t = (arr[m2] - 0.5) / 0.5
        r[m2] = 255
        g[m2] = np.clip(238 + 6.0 * t, 0, 255).astype(np.uint8)
        b[m2] = np.clip(255 - 41.0 * t, 0, 255).astype(np.uint8)

    if np.any(m3):
        t = (arr[m3] - 1.0) / 0.6
        r[m3] = 255
        g[m3] = np.clip(244 - 32.0 * t, 0, 255).astype(np.uint8)
        b[m3] = np.clip(214 - 64.0 * t, 0, 255).astype(np.uint8)

    if np.any(m4):
        t = np.clip((arr[m4] - 1.6) / 0.9, 0.0, 1.0)
        r[m4] = 255
        g[m4] = np.clip(212 - 38.0 * t, 0, 255).astype(np.uint8)
        b[m4] = np.clip(150 - 30.0 * t, 0, 255).astype(np.uint8)

    return r, g, b
