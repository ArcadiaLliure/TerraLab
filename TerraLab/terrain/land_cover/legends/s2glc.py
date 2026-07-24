"""Official S2GLC Europe 2017 semantic legend.

The codes, English names and base RGB values below come from
``S2GLC_Europe_2017_v1.2_legend_and_version_info.txt`` embedded in the
official categorical ZIP published by CBK PAN.  They are deliberately kept
outside download and renderer code: a category remains a queryable semantic
value, while this module supplies one optional visual representation.

The official colour map says ``INTERPOLATION:INTERPOLATED`` for QGIS display.
That is *not* a licence to interpolate class identifiers.  TerraLab samples
the source codes with nearest-neighbour only and applies edge antialiasing to
the resulting colours later in the rendering pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np


Color = tuple[int, int, int, int]


@dataclass(frozen=True)
class LandCoverStyle:
    """Extensible material declaration for one categorical source code."""

    code: int
    key: str
    label_key: str
    labels: Mapping[str, str]
    semantic_group: str
    base_color: Color
    procedural_style: str | None = None
    procedural_parameters: Mapping[str, float] = field(default_factory=dict)
    material_id: str | None = None
    texture_id: str | None = None
    object_generator_id: str | None = None
    transition_priority: int = 0
    descriptions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", int(self.code))
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))
        object.__setattr__(
            self, "descriptions", MappingProxyType(dict(self.descriptions))
        )
        object.__setattr__(
            self,
            "procedural_parameters",
            MappingProxyType(dict(self.procedural_parameters)),
        )


def _style(
    code: int,
    key: str,
    en: str,
    ca: str,
    es: str,
    group: str,
    rgb: tuple[int, int, int],
    *,
    alpha: int = 255,
    variation: float = 0.05,
    scale_m: float = 45.0,
    transition_priority: int = 0,
) -> LandCoverStyle:
    return LandCoverStyle(
        code=code,
        key=key,
        label_key=f"LandCover.S2GLC.{key}",
        labels={"en": en, "ca": ca, "es": es},
        descriptions={
            "en": f"Land-cover category corresponding to “{en}”.",
            "ca": f"Categoria de cobertura del sòl corresponent a «{ca}».",
            "es": f"Categoría de cobertura del suelo correspondiente a «{es}».",
        },
        semantic_group=group,
        base_color=rgb + (alpha,),
        procedural_style="coordinate_luminance" if variation > 0.0 else None,
        procedural_parameters={
            "variation": float(variation),
            "scale_m": float(scale_m),
        },
        transition_priority=int(transition_priority),
    )


_STYLES = (
    _style(0, "clouds", "Clouds", "Núvols", "Nubes", "unknown", (255, 255, 255), variation=0.0),
    _style(62, "artificial", "Artificial surfaces and constructions", "Superfícies artificials i construccions", "Superficies artificiales y construcciones", "built", (210, 0, 0), variation=0.025, transition_priority=80),
    _style(73, "cultivated", "Cultivated areas", "Àrees cultivades", "Áreas cultivadas", "agriculture", (253, 211, 39), variation=0.07, scale_m=70.0),
    _style(75, "vineyards", "Vineyards", "Vinyes", "Viñedos", "agriculture", (176, 91, 16), variation=0.06, scale_m=35.0),
    _style(82, "broadleaf_trees", "Broadleaf tree cover", "Coberta d'arbres de fulla ampla", "Cobertura de árboles de hoja ancha", "forest", (35, 152, 0), variation=0.08, scale_m=24.0),
    _style(83, "coniferous_trees", "Coniferous tree cover", "Coberta de coníferes", "Cobertura de coníferas", "forest", (8, 98, 0), variation=0.075, scale_m=22.0),
    _style(102, "herbaceous", "Herbaceous vegetation", "Vegetació herbàcia", "Vegetación herbácea", "low_vegetation", (249, 150, 39), variation=0.075, scale_m=38.0),
    _style(103, "moors_heathland", "Moors and heathland", "Landes i bruguerars", "Brezales y landas", "shrubland", (141, 139, 0), variation=0.065, scale_m=32.0),
    _style(104, "sclerophyllous", "Sclerophyllous vegetation", "Vegetació esclerofil·la", "Vegetación esclerófila", "shrubland", (95, 53, 6), variation=0.055, scale_m=30.0),
    _style(105, "marshes", "Marshes", "Aiguamolls", "Marismas", "wetland", (149, 107, 196), variation=0.035, transition_priority=70),
    _style(106, "peatbogs", "Peatbogs", "Torberes", "Turberas", "wetland", (77, 37, 106), variation=0.035, transition_priority=70),
    _style(121, "natural_material", "Natural material surfaces", "Superfícies de materials naturals", "Superficies de materiales naturales", "bare", (154, 154, 154), variation=0.05, scale_m=28.0),
    _style(123, "permanent_snow", "Permanent snow covered surfaces", "Superfícies cobertes de neu permanent", "Superficies cubiertas de nieve permanente", "snow_ice", (106, 255, 255), variation=0.02, transition_priority=90),
    _style(162, "water", "Water bodies", "Masses d'aigua", "Masas de agua", "water", (20, 69, 249), variation=0.018, scale_m=90.0, transition_priority=100),
    _style(255, "nodata", "No data", "Sense dades", "Sin datos", "nodata", (255, 255, 255), alpha=0, variation=0.0),
)

S2GLC_LEGEND: Mapping[int, LandCoverStyle] = MappingProxyType(
    {style.code: style for style in _STYLES}
)


def get_s2glc_style(code: int) -> LandCoverStyle | None:
    return S2GLC_LEGEND.get(int(code))


def s2glc_classes_to_rgba(
    classes: Any,
    *,
    x: Any | None = None,
    y: Any | None = None,
    altitude_m: Any | None = None,
    slope: Any | None = None,
    aspect_deg: Any | None = None,
    seed: int = 2017,
) -> tuple[np.ndarray, np.ndarray]:
    """Map discrete codes to their canonical, versioned legend colours.

    Coordinate-driven variation is deliberately disabled: lighting and fog
    are applied after per-pixel material resolution in the renderer, so base
    colours remain independent of camera triangulation.
    """

    del x, y, altitude_m, slope, aspect_deg, seed
    codes = np.asarray(classes, dtype=np.int64)
    rgba = np.zeros(codes.shape + (4,), dtype=np.uint8)
    valid = np.zeros(codes.shape, dtype=bool)

    for code in np.unique(codes):
        style = S2GLC_LEGEND.get(int(code))
        if style is None:
            continue
        mask = codes == int(code)
        base = np.asarray(style.base_color, dtype=np.float32)
        pixels = np.broadcast_to(base, (int(np.count_nonzero(mask)), 4)).copy()
        rgba[mask] = np.clip(np.rint(pixels), 0, 255).astype(np.uint8)
        valid[mask] = int(style.base_color[3]) > 0
    return rgba, valid


__all__ = [
    "Color",
    "LandCoverStyle",
    "S2GLC_LEGEND",
    "get_s2glc_style",
    "s2glc_classes_to_rgba",
]
