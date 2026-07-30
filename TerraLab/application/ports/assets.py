"""Qt-free application ports for data assets, layer management, and source catalog."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AssetOperationReport:
    """Typed report summarizing asset import, download or removal."""

    asset_id: str
    success: bool
    message: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class AssetCatalogPort(Protocol):
    """Port contract for asset registry, discovery, download, and installation."""

    def asset_status(self, asset_id: str) -> dict[str, Any]: ...

    def register_external_source(
        self,
        asset_id: str,
        path: str,
        *,
        display_name: str | None = None,
        priority: int = 0,
    ) -> Any: ...

    def attach_external_asset(self, asset_id: str, path: str) -> Any: ...

    def import_files(
        self,
        asset_id: str,
        paths: Iterable[str],
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def remove_asset_data(self, asset_id: str) -> Any: ...


@runtime_checkable
class SourceCatalogPort(Protocol):
    """Port contract for persistent typed geospatial source cataloging."""

    def list_sources(self, layer_type: str | None = None) -> list[Any]: ...

    def get(self, source_id: str) -> Any | None: ...

    def set_selection(
        self, role: str, source_id: str, mode: str = "manual"
    ) -> None: ...

    def set_automatic(self, role: str) -> None: ...


@runtime_checkable
class LayerManagerPort(Protocol):
    """Port contract for layer visibility, status, and configuration façade."""

    def list_layers(self, group: str | None = None) -> tuple[Any, ...]: ...

    def is_visible(self, layer_id: str) -> bool: ...

    def set_visible(self, layer_id: str, visible: bool) -> None: ...

    def status(self, layer_id: str) -> Any: ...

    def set_source(self, layer_id: str, source_id: str | None) -> None: ...
