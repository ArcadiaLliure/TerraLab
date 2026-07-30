"""Application-owned ports for versioned Milky-Way and deep-sky resources."""

from __future__ import annotations

from typing import Protocol

from TerraLab.scene.plans.deep_sky import (
    DeepSkyCatalogHandle,
    TextureResourceHandle,
)


class TextureResourcePort(Protocol):
    """Resolve a texture once for a requested resource identity."""

    def load_texture(
        self, path: str | None, *, version: str = ""
    ) -> TextureResourceHandle: ...


class DeepSkyCatalogPort(Protocol):
    """Resolve a deep-sky catalogue once for a requested resource identity."""

    def load_catalog(
        self, path: str | None, *, version: str = ""
    ) -> DeepSkyCatalogHandle: ...
