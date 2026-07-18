"""Unified layer model shared by onboarding and the runtime UI.

Visibility and data availability are intentionally independent: enabling a
layer never starts a download and never fails merely because its preferred
resource is absent.  Renderers can use the fallback described by ``status``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from TerraLab.common.utils import get_config_value, set_config_value
from TerraLab.data.assets_manager import AssetManager
from TerraLab.terrain.data_sources import (
    LayerRole,
    LayerType,
    SelectionMode,
    SourceHealthStatus,
)


class _StableStringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class LayerId(_StableStringEnum):
    EARTH_TERRAIN = "earth.terrain"
    EARTH_SURFACE = "earth.surface"
    EARTH_LIGHT_POLLUTION = "earth.light_pollution"
    SKY_STARS = "sky.stars"
    SKY_NGC = "sky.ngc"
    SKY_MILKY_WAY = "sky.milky_way"
    SKY_PLANCK_DUST = "sky.planck_dust"
    SKY_SOLAR_SYSTEM = "sky.solar_system"
    SKY_WEATHER = "sky.weather"


class LayerGroup(_StableStringEnum):
    SKY = "sky"
    EARTH = "earth"


class LayerState(_StableStringEnum):
    READY = "ready"
    PARTIAL = "partial"
    MISSING = "missing"
    INVALID = "invalid"
    PLANNED = "planned"


# Descriptive alias retained for callers that use the longer name.
LayerAvailability = LayerState


@dataclass(frozen=True)
class LayerResource:
    id: str
    name: str
    kind: str
    path: str = ""
    ready: bool = False
    managed: bool = False
    required: bool = False
    details: str = ""


@dataclass(frozen=True)
class LayerDescriptor:
    id: LayerId
    group: LayerGroup
    title: str
    asset_id: str
    description: str
    fallback: str
    default_visible: bool = False
    supports_download: bool = False
    supports_external: bool = True
    children: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayerStatus:
    layer_id: LayerId
    state: LayerState
    visible: bool
    label: str
    message: str
    effective_source: str = ""
    fallback_active: bool = False
    resources: tuple[LayerResource, ...] = field(default_factory=tuple)


_DESCRIPTORS = (
    LayerDescriptor(
        LayerId.SKY_STARS,
        LayerGroup.SKY,
        "Estrelles",
        "gaia_catalog",
        "Gaia i el catàleg propi de les estrelles més brillants.",
        "Catàleg propi inclòs",
        True,
    ),
    LayerDescriptor(
        LayerId.SKY_NGC,
        LayerGroup.SKY,
        "Catàleg NGC",
        "ngc_catalog",
        "Objectes de cel profund d'OpenNGC.",
        "Sense objectes NGC",
        False,
        True,
    ),
    LayerDescriptor(
        LayerId.SKY_MILKY_WAY,
        LayerGroup.SKY,
        "Via Làctia",
        "milkyway_texture",
        "Panorama de la Via Làctia en FITS o PNG.",
        "Fons de cel sense textura",
        True,
        True,
    ),
    LayerDescriptor(
        LayerId.SKY_PLANCK_DUST,
        LayerGroup.SKY,
        "Pols Planck",
        "planck_dust",
        "Opacitat de pols de Planck i derivats de runtime.",
        "Sense atenuació de pols",
        True,
        True,
    ),
    LayerDescriptor(
        LayerId.SKY_SOLAR_SYSTEM,
        LayerGroup.SKY,
        "Sistema solar",
        "solar_system_ephemeris",
        "Sol, Lluna i planetes amb efemèride DE421.",
        "Efemèride geomètrica",
        True,
        True,
        True,
        ("sun", "moon", "planets"),
    ),
    LayerDescriptor(
        LayerId.SKY_WEATHER,
        LayerGroup.SKY,
        "Clima",
        "climate_metno",
        "MET Norway amb User-Agent identificatiu i memòria cau local.",
        "Model atmosfèric offline",
        False,
        False,
        False,
    ),
    LayerDescriptor(
        LayerId.EARTH_TERRAIN,
        LayerGroup.EARTH,
        "Terreny / DEM",
        "elevation_dem",
        "Mosaics d'elevació GeoTIFF, ASC, TXT o NPY.",
        "Horitzó pla",
        True,
        True,
    ),
    LayerDescriptor(
        LayerId.EARTH_SURFACE,
        LayerGroup.EARTH,
        "Tipus de sòl",
        "surface_categorical",
        "Rasters locals RGB o categòrics; el WMS CLC+ encara és pendent.",
        "Paleta sintètica",
        True,
        False,
    ),
    LayerDescriptor(
        LayerId.EARTH_LIGHT_POLLUTION,
        LayerGroup.EARTH,
        "Contaminació lumínica",
        "light_pollution",
        "Raster DVNL o GeoTIFF aportat per l'usuari.",
        "Escala Bortle/manual",
        True,
        True,
    ),
)


_LEGACY_VISIBILITY_KEYS = {
    LayerId.SKY_STARS: "estrelles",
    LayerId.SKY_NGC: "espai_profund",
    LayerId.SKY_MILKY_WAY: "via_lactia",
    LayerId.SKY_PLANCK_DUST: "pols_planck",
    LayerId.SKY_SOLAR_SYSTEM: "sistema_solar",
    LayerId.SKY_WEATHER: "clima",
    LayerId.EARTH_TERRAIN: "topografia",
    LayerId.EARTH_SURFACE: "superficie",
    LayerId.EARTH_LIGHT_POLLUTION: "contaminacio_luminica",
}


_GEO_ROLE = {
    LayerId.EARTH_TERRAIN: LayerRole.ELEVATION,
    LayerId.EARTH_SURFACE: LayerRole.SURFACE,
    LayerId.EARTH_LIGHT_POLLUTION: LayerRole.LIGHT_POLLUTION,
}


def _coerce_layer_id(value: LayerId | str) -> LayerId:
    return value if isinstance(value, LayerId) else LayerId(str(value))


class LayerManager:
    """Single façade used by layer controls and configuration screens."""

    def __init__(self, asset_manager: AssetManager | None = None) -> None:
        self.assets = asset_manager or AssetManager()
        self.library = self.assets.library
        self.data_sources = self.assets.data_sources
        self._by_id = {descriptor.id: descriptor for descriptor in _DESCRIPTORS}
        self._migrate_visibility_once()

    def _migrate_visibility_once(self) -> None:
        marker = "ui.visibility.layer_ids_v1_migrated"
        if bool(get_config_value(marker, False)):
            return
        for descriptor in _DESCRIPTORS:
            legacy = _LEGACY_VISIBILITY_KEYS[descriptor.id]
            value = bool(
                get_config_value(
                    f"ui.visibility.{legacy}", descriptor.default_visible
                )
            )
            set_config_value(f"ui.visibility.{descriptor.id.value}", value)
        set_config_value(marker, True)

    def list_layers(
        self, group: LayerGroup | str | None = None
    ) -> tuple[LayerDescriptor, ...]:
        if group is None:
            return tuple(_DESCRIPTORS)
        normalized = group if isinstance(group, LayerGroup) else LayerGroup(str(group))
        return tuple(item for item in _DESCRIPTORS if item.group is normalized)

    def descriptor(self, layer_id: LayerId | str) -> LayerDescriptor:
        return self._by_id[_coerce_layer_id(layer_id)]

    def is_visible(self, layer_id: LayerId | str) -> bool:
        descriptor = self.descriptor(layer_id)
        return bool(
            get_config_value(
                f"ui.visibility.{descriptor.id.value}",
                descriptor.default_visible,
            )
        )

    def set_visible(self, layer_id: LayerId | str, visible: bool) -> None:
        descriptor = self.descriptor(layer_id)
        checked = bool(visible)
        set_config_value(f"ui.visibility.{descriptor.id.value}", checked)
        # Temporary alias for renderers and older preferences.
        set_config_value(
            f"ui.visibility.{_LEGACY_VISIBILITY_KEYS[descriptor.id]}", checked
        )

    def child_visible(self, child: str, default: bool = True) -> bool:
        normalized = str(child).strip().lower()
        if normalized not in {"sun", "moon", "planets"}:
            raise ValueError(f"Unknown solar-system child: {child}")
        legacy = {"sun": "sol_i_lluna", "moon": "sol_i_lluna", "planets": "planetes"}[normalized]
        return bool(
            get_config_value(
                f"ui.visibility.{LayerId.SKY_SOLAR_SYSTEM.value}.{normalized}",
                get_config_value(f"ui.visibility.{legacy}", default),
            )
        )

    def set_child_visible(self, child: str, visible: bool) -> None:
        normalized = str(child).strip().lower()
        if normalized not in {"sun", "moon", "planets"}:
            raise ValueError(f"Unknown solar-system child: {child}")
        checked = bool(visible)
        set_config_value(
            f"ui.visibility.{LayerId.SKY_SOLAR_SYSTEM.value}.{normalized}", checked
        )
        if normalized == "planets":
            set_config_value("ui.visibility.planetes", checked)
        else:
            # The current renderer exposes Sol/Lluna as one compatibility checkbox.
            set_config_value("ui.visibility.sol_i_lluna", checked)

    @staticmethod
    def _state_label(state: LayerState) -> str:
        return {
            LayerState.READY: "Preparada",
            LayerState.PARTIAL: "Parcial",
            LayerState.MISSING: "Sense dades",
            LayerState.INVALID: "Font no vàlida",
            LayerState.PLANNED: "Pendent",
        }[state]

    def _geospatial_status(self, descriptor: LayerDescriptor) -> LayerStatus:
        role = _GEO_ROLE[descriptor.id]
        layer_types: Iterable[LayerType]
        if role is LayerRole.SURFACE:
            layer_types = (LayerType.SURFACE_RGB, LayerType.SURFACE_CATEGORICAL)
        else:
            layer_types = (LayerType(role.value),)
        sources = [
            source
            for kind in layer_types
            for source in self.data_sources.list_sources(kind)
        ]
        resources = tuple(
            LayerResource(
                id=source.id,
                name=source.display_name,
                kind=source.layer_type.value,
                path=source.path,
                ready=bool(source.available and source.enabled),
                managed=bool(source.metadata.get("managed", False)),
                details=source.attribution or source.format,
            )
            for source in sorted(sources, key=lambda item: (-item.priority, item.display_name))
        )
        usable = [source for source in sources if source.available and source.enabled]
        selected = self.data_sources.get_selection(role)
        effective = None
        if selected.mode is SelectionMode.MANUAL and selected.source_id:
            effective = next(
                (source for source in usable if source.id == selected.source_id), None
            )
        if effective is None and usable:
            effective = sorted(
                usable,
                key=lambda item: (-item.priority, item.resolution_m or float("inf"), item.id),
            )[0]
        if effective is not None:
            state = LayerState.READY
            message = f"Font efectiva: {effective.display_name}"
        elif any(source.health_status is SourceHealthStatus.INVALID for source in sources):
            state = LayerState.INVALID
            message = f"Cap font vàlida; fallback: {descriptor.fallback}."
        elif descriptor.id is LayerId.EARTH_SURFACE:
            state = LayerState.PLANNED
            message = "WMS CLC+ pendent; s'utilitza la paleta sintètica."
        else:
            state = LayerState.PARTIAL
            message = f"Sense font principal; fallback: {descriptor.fallback}."
        return LayerStatus(
            descriptor.id,
            state,
            self.is_visible(descriptor.id),
            self._state_label(state),
            message,
            effective.display_name if effective is not None else "",
            effective is None,
            resources,
        )

    def status(self, layer_id: LayerId | str) -> LayerStatus:
        descriptor = self.descriptor(layer_id)
        if descriptor.id in _GEO_ROLE:
            return self._geospatial_status(descriptor)

        asset = self.assets.asset_status(descriptor.asset_id)
        primary_ready = bool(asset.get("ready", False))
        resource_path = str(asset.get("path", "") or "")
        resources: list[LayerResource] = []
        if descriptor.id is LayerId.SKY_STARS:
            bundled = (
                Path(__file__).resolve().parents[1]
                / "data"
                / "stars"
                / "no_gaia_stars.json"
            )
            resources.append(
                LayerResource(
                    "bright-stars",
                    "Catàleg propi",
                    "json",
                    str(bundled),
                    bundled.exists(),
                    False,
                    True,
                    "Inclòs i no desactivable",
                )
            )
            resources.append(
                LayerResource(
                    "gaia",
                    "Gaia",
                    "catalogue",
                    resource_path,
                    primary_ready,
                    True,
                )
            )
            state = LayerState.READY if primary_ready else LayerState.PARTIAL
            message = (
                "Gaia i catàleg propi disponibles."
                if primary_ready
                else "Gaia no està preparat; es mostra el catàleg propi."
            )
        elif descriptor.id in {LayerId.SKY_SOLAR_SYSTEM, LayerId.SKY_WEATHER}:
            state = LayerState.READY if primary_ready else LayerState.PARTIAL
            message = (
                "Font principal disponible."
                if primary_ready
                else f"Fallback actiu: {descriptor.fallback}."
            )
        else:
            state = LayerState.READY if primary_ready else LayerState.MISSING
            message = (
                "Font principal disponible."
                if primary_ready
                else f"No hi ha dades; {descriptor.fallback.lower()}."
            )
        if descriptor.id is not LayerId.SKY_STARS:
            resources.append(
                LayerResource(
                    descriptor.asset_id,
                    descriptor.title,
                    "asset",
                    resource_path,
                    primary_ready,
                    bool(resource_path and self.library.contains(resource_path)),
                )
            )
        return LayerStatus(
            descriptor.id,
            state,
            self.is_visible(descriptor.id),
            self._state_label(state),
            message,
            Path(resource_path).name if resource_path else "",
            not primary_ready,
            tuple(resources),
        )

    def set_source(self, layer_id: LayerId | str, source_id: str | None) -> None:
        normalized = _coerce_layer_id(layer_id)
        role = _GEO_ROLE.get(normalized)
        if role is None:
            raise ValueError("Manual priorities are only available for geospatial layers")
        if source_id:
            self.data_sources.set_selection(role, str(source_id), mode=SelectionMode.MANUAL)
        else:
            self.data_sources.set_automatic(role)

    def add_external_source(
        self,
        layer_id: LayerId | str,
        path: str,
        *,
        display_name: str | None = None,
        priority: int = 0,
    ):
        descriptor = self.descriptor(layer_id)
        if descriptor.id in _GEO_ROLE:
            asset_id = descriptor.asset_id
            if descriptor.id is LayerId.EARTH_SURFACE:
                suffix = Path(path).suffix.lower()
                asset_id = (
                    "surface_rgb"
                    if suffix in {".png", ".jpg", ".jpeg", ".webp"}
                    else "surface_categorical"
                )
            return self.assets.register_external_source(
                asset_id,
                path,
                display_name=display_name,
                priority=priority,
            )
        return self.assets.attach_external_asset(descriptor.asset_id, path)

    def prepare_managed(
        self,
        layer_id: LayerId | str,
        paths: Iterable[str],
        **options,
    ) -> dict[str, object]:
        descriptor = self.descriptor(layer_id)
        return self.assets.import_files(
            descriptor.asset_id,
            paths,
            options=dict(options),
        )

