"""Semantic land-cover registries and rendering helpers."""

from TerraLab.terrain.land_cover.legends.s2glc import (
    S2GLC_LEGEND,
    LandCoverStyle,
    get_s2glc_style,
    s2glc_classes_to_rgba,
)

__all__ = [
    "LandCoverStyle",
    "S2GLC_LEGEND",
    "get_s2glc_style",
    "s2glc_classes_to_rgba",
]
