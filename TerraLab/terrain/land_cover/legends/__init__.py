"""Built-in, versioned land-cover legends."""

from TerraLab.terrain.land_cover.legends.s2glc import (
    S2GLC_LEGEND,
    LandCoverStyle,
    get_s2glc_style,
    s2glc_classes_to_rgba,
)
from TerraLab.terrain.land_cover.legends.category_info import (
    LandCoverCategoryInfo,
    category_info,
)

__all__ = [
    "LandCoverStyle",
    "S2GLC_LEGEND",
    "get_s2glc_style",
    "s2glc_classes_to_rgba",
    "LandCoverCategoryInfo",
    "category_info",
]
