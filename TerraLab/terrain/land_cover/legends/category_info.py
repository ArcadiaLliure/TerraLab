"""Localized semantic information for categorical land-cover values."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from TerraLab.terrain.land_cover.legends.s2glc import S2GLC_LEGEND


@dataclass(frozen=True)
class LandCoverCategoryInfo:
    class_id: int
    name: str
    description: str
    product: str


_CLCPLUS_LABELS: Mapping[int, Mapping[str, str]] = MappingProxyType(
    {
        1: {"ca": "Superfícies segellades", "es": "Superficies selladas", "en": "Sealed surfaces"},
        2: {"ca": "Arbres de fulla acicular", "es": "Árboles de hoja acicular", "en": "Needle-leaved trees"},
        3: {"ca": "Arbres caducifolis de fulla ampla", "es": "Árboles caducifolios de hoja ancha", "en": "Broadleaved deciduous trees"},
        4: {"ca": "Arbres perennifolis de fulla ampla", "es": "Árboles perennifolios de hoja ancha", "en": "Broadleaved evergreen trees"},
        5: {"ca": "Vegetació llenyosa baixa", "es": "Vegetación leñosa baja", "en": "Low-growing woody plants"},
        6: {"ca": "Vegetació herbàcia permanent", "es": "Vegetación herbácea permanente", "en": "Permanent herbaceous vegetation"},
        7: {"ca": "Vegetació herbàcia periòdica", "es": "Vegetación herbácea periódica", "en": "Periodically herbaceous vegetation"},
        8: {"ca": "Líquens i molses", "es": "Líquenes y musgos", "en": "Lichens and mosses"},
        9: {"ca": "Superfícies sense vegetació o amb vegetació escassa", "es": "Superficies sin vegetación o con vegetación escasa", "en": "Non- and sparsely-vegetated surfaces"},
        10: {"ca": "Aigua", "es": "Agua", "en": "Water"},
        11: {"ca": "Neu i gel", "es": "Nieve y hielo", "en": "Snow and ice"},
        253: {"ca": "Aigua marina costanera", "es": "Agua marina costera", "en": "Coastal seawater"},
        254: {"ca": "Fora de l’àrea del producte", "es": "Fuera del área del producto", "en": "Outside product area"},
        255: {"ca": "Sense dades", "es": "Sin datos", "en": "No data"},
    }
)


def category_info(
    legend_id: str,
    class_id: int,
    *,
    source_name: str = "",
    locale: str = "ca",
) -> LandCoverCategoryInfo:
    """Resolve a class without accessing its raster source."""

    code = int(class_id)
    legend = str(legend_id or "").strip().lower().replace("-", "_")
    language = locale if locale in {"ca", "es", "en"} else "ca"
    if legend in {"s2glc", "s2glc_2017", "s2glc_europe_2017"}:
        style = S2GLC_LEGEND.get(code)
        if style is not None:
            return LandCoverCategoryInfo(
                class_id=code,
                name=style.labels.get(language, style.labels.get("ca", f"Classe {code}")),
                description=style.descriptions.get(
                    language,
                    style.descriptions.get(
                        "ca", f"Categoria de cobertura del sòl {code}."
                    ),
                ),
                product="S2GLC Europe 2017",
            )
    if legend in {
        "clcplus",
        "clcplus_backbone",
        "clcplus_backbone_2023",
        "clc_plus_backbone",
    }:
        labels = _CLCPLUS_LABELS.get(code, {})
        name = labels.get(language, labels.get("ca", f"Classe {code}"))
        description = {
            "ca": f"Categoria CLC+ Backbone corresponent a «{name}».",
            "es": f"Categoría CLC+ Backbone correspondiente a «{name}».",
            "en": f"CLC+ Backbone category corresponding to “{name}”.",
        }[language]
        return LandCoverCategoryInfo(
            class_id=code,
            name=name,
            description=description,
            product="CLC+ Backbone",
        )
    product = str(source_name or "").strip() or "Font categòrica externa"
    return LandCoverCategoryInfo(
        class_id=code,
        name=f"Classe {code}",
        description="La llegenda externa no inclou cap descripció per a aquesta classe.",
        product=product,
    )


__all__ = ["LandCoverCategoryInfo", "category_info"]
