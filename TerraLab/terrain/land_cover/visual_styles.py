"""Alternative presentation palettes for semantic land-cover products.

The source class identifiers and official colours remain immutable.  These
tables are only consulted by the renderer when the user selects Vibrant.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from TerraLab.terrain.land_cover.legends.s2glc import S2GLC_LEGEND


Color = tuple[int, int, int, int]

VIBRANT_PALETTE_VERSION = 4

_S2GLC_ALIASES = {
    "s2glc",
    "s2glc_2017",
    "s2glc_europe_2017",
}
_CLCPLUS_ALIASES = {
    "clcplus",
    "clcplus_backbone",
    "clcplus_backbone_2023",
    "clc_plus_backbone",
}

S2GLC_VIBRANT_PALETTE: Mapping[int, Color] = MappingProxyType(
    {
        0: (240, 246, 249, 255),
        62: (142, 150, 160, 255),
        73: (245, 194, 66, 255),
        75: (194, 53, 118, 255),
        82: (63, 174, 100, 255),
        83: (29, 122, 82, 255),
        102: (158, 214, 59, 255),
        103: (124, 140, 74, 255),
        104: (95, 191, 142, 255),
        105: (46, 196, 182, 255),
        106: (107, 69, 82, 255),
        121: (199, 190, 160, 255),
        123: (201, 239, 245, 255),
        162: (46, 134, 214, 255),
        255: (255, 255, 255, 0),
    }
)

CLCPLUS_VIBRANT_PALETTE: Mapping[int, Color] = MappingProxyType(
    {
        1: (174, 119, 98, 255),
        2: (41, 111, 75, 255),
        3: (55, 147, 88, 255),
        4: (48, 134, 80, 255),
        5: (148, 146, 76, 255),
        6: (128, 170, 86, 255),
        7: (174, 169, 94, 255),
        8: (121, 150, 91, 255),
        9: (188, 140, 87, 255),
        10: (48, 125, 166, 255),
        11: (241, 248, 252, 255),
        253: (43, 116, 159, 255),
        254: (255, 255, 255, 0),
        255: (255, 255, 255, 0),
    }
)


def _normalized_legend_id(legend_id: str) -> str:
    return str(legend_id or "").strip().lower().replace("-", "_")


def vibrant_land_cover_rgba(
    legend_id: str, class_id: int
) -> Color | None:
    """Return the semantic Vibrant colour for a known product class."""

    legend = _normalized_legend_id(legend_id)
    if legend in _S2GLC_ALIASES:
        return S2GLC_VIBRANT_PALETTE.get(int(class_id))
    if legend in _CLCPLUS_ALIASES:
        return CLCPLUS_VIBRANT_PALETTE.get(int(class_id))
    return None


def preserve_small_region(legend_id: str, class_id: int) -> bool:
    """Protect small, meaningful features from categorical regularization."""

    legend = _normalized_legend_id(legend_id)
    code = int(class_id)
    if legend in _S2GLC_ALIASES:
        style = S2GLC_LEGEND.get(code)
        return bool(style is not None and style.transition_priority >= 70)
    if legend in _CLCPLUS_ALIASES:
        return code in {1, 10, 11, 253}
    return False


__all__ = [
    "CLCPLUS_VIBRANT_PALETTE",
    "S2GLC_VIBRANT_PALETTE",
    "VIBRANT_PALETTE_VERSION",
    "preserve_small_region",
    "vibrant_land_cover_rgba",
]
