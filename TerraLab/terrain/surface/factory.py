"""Surface provider construction from data-source declarations."""

from __future__ import annotations

import time
from typing import Any, Mapping, Sequence

import numpy as np

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.terrain.crs import (
    CRS_TERRAIN_INTERNAL,
    DEFAULT_TRANSFORM_SERVICE,
    CoordinateTransformService,
)
from TerraLab.terrain.providers.common import (
    _as_source_sequence,
    _source_declared_crs,
    _source_enabled,
    _source_identifier,
    _source_paths,
    _source_value,
)
from TerraLab.terrain.surface.categorical import (
    CategoricalSurfaceProvider,
    RgbCategoricalSurfaceProvider,
)
from TerraLab.terrain.surface.common import (
    SurfaceProvider,
    _discover_surface_files,
)
from TerraLab.terrain.surface.light_pollution import LightPollutionProvider
from TerraLab.terrain.surface.rgb import RgbSurfaceProvider

def _layer_kind(source: Any) -> str:
    raw = _source_value(source, "layer_type", "type", "kind", default="")
    if hasattr(raw, "value"):
        raw = raw.value
    if not raw and hasattr(raw, "name"):
        raw = raw.name
    normalized = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"land_cover_rgb", "rgb_land_cover"}:
        return "land_cover_rgb"
    if (
        "ortho" in normalized
        or "true_color" in normalized
        or "truecolour" in normalized
        or normalized in {"rgb", "orthophoto_rgb"}
    ):
        return "orthophoto"
    if "categor" in normalized or normalized in {"land_cover", "landcover"}:
        return "categorical"
    if "light" in normalized or "pollution" in normalized or "dvnl" in normalized:
        return "light_pollution"
    return normalized


def _rgb_linear_light_for_source(source: Any) -> bool:
    metadata = _source_value(source, "metadata", default={})
    if isinstance(metadata, Mapping):
        value = metadata.get("rgb_interpolation", "")
        return str(value or "").strip().lower() in {
            "linear_light",
            "linear-light",
            "linear_srgb",
        }
    return False


def _palette_for_source(
    source: Any,
    category_palettes: Mapping[Any, Any] | None,
) -> Mapping[int, Sequence[int]]:
    embedded = _source_value(source, "class_colors", "palette", default=None)
    if not isinstance(embedded, Mapping):
        metadata = _source_value(source, "metadata", default={})
        if isinstance(metadata, Mapping):
            embedded = metadata.get("class_colors", metadata.get("palette"))
    if isinstance(embedded, Mapping):
        return embedded
    palettes = category_palettes or {}
    source_id = _source_identifier(source)
    if source_id and isinstance(palettes.get(source_id), Mapping):
        return palettes[source_id]
    if palettes and all(isinstance(key, (int, np.integer)) for key in palettes):
        return palettes
    return {}


def _legend_for_source(source: Any) -> str:
    value = _source_value(source, "legend_id", default="")
    metadata = _source_value(source, "metadata", default={})
    if not value and isinstance(metadata, Mapping):
        value = metadata.get("legend_id", metadata.get("legend", ""))
    return str(value or "").strip().lower()


# TODO(soil-wms): optional future CLC+ provider (a separate 2023 product,
# explicitly not S2GLC 2017) advertised at
# https://geoserver.geoville.com/geoserver/clcp/ows?service=WMS&version=1.3.0&request=GetCapabilities
# for layer CLMS_CLCplus_RASTER_2023_010m_eu.  Tiles must be cached below
# data_root and composed with the effective DEM.  Do not issue WMS requests
# until that provider and its bounded cache policy are implemented.
def create_surface_providers(
    sources: Any,
    *,
    category_palettes: Mapping[Any, Any] | None = None,
    internal_crs: str = CRS_TERRAIN_INTERNAL,
    initialize: bool = True,
    transform_service: CoordinateTransformService | None = None,
) -> tuple[SurfaceProvider, ...]:
    """Create typed providers from DataSource-like objects by duck typing."""

    service = transform_service or DEFAULT_TRANSFORM_SERVICE
    providers: list[SurfaceProvider] = []
    start = time.perf_counter()
    try:
        for source in _as_source_sequence(sources):
            if not _source_enabled(source):
                continue
            paths = _discover_surface_files(_source_paths(source))
            if not paths:
                continue
            kind = _layer_kind(source)
            common = {
                "source_id": _source_identifier(source),
                "source_name": str(
                    _source_value(
                        source,
                        "display_name",
                        "name",
                        default=_source_identifier(source),
                    )
                    or _source_identifier(source)
                ),
                "internal_crs": internal_crs,
                "declared_crs": _source_declared_crs(source),
                "transform_service": service,
            }
            if kind == "categorical":
                provider: SurfaceProvider = CategoricalSurfaceProvider(
                    paths,
                    class_colors=_palette_for_source(source, category_palettes),
                    legend_id=_legend_for_source(source),
                    **common,
                )
            elif kind == "land_cover_rgb":
                provider = RgbCategoricalSurfaceProvider(
                    paths,
                    class_colors=_palette_for_source(
                        source, category_palettes
                    ),
                    legend_id=_legend_for_source(source),
                    **common,
                )
            elif kind == "light_pollution":
                provider = LightPollutionProvider(paths, **common)
            else:
                provider = RgbSurfaceProvider(
                    paths,
                    linear_light=_rgb_linear_light_for_source(source),
                    **common,
                )
            providers.append(provider)
            if initialize:
                provider.initialize()
    except Exception:
        for provider in providers:
            try:
                provider.close()
            except Exception:
                log_suppressed_exception(__name__, "create_surface_providers")
        raise
    print(
        "[TEMPORAL][SurfaceFactory] "
        f"providers={len(providers)} initialize={initialize} "
        f"elapsed={time.perf_counter() - start:.3f}s"
    )
    return tuple(providers)
