"""Canonical data-library asset manager."""

from TerraLab.common.utils import set_config_value
from TerraLab.data.assets import (
    AssetRegistryMixin,
    AssetValidationMixin,
    AssetDiscoveryMixin,
    AssetDownloadMixin,
    AssetInstallMixin,
)
from TerraLab.data.assets.runtime import (
    AssetOperationCancelled,
    AssetRemovalPreview,
    AssetRemovalReport,
    AssetSpec,
)
from TerraLab.data.copernicus import CopernicusOrthophotoManager


class AssetManager(
    AssetInstallMixin,
    AssetDownloadMixin,
    AssetDiscoveryMixin,
    AssetValidationMixin,
    AssetRegistryMixin,
):
    """Coordinate asset manifests, validation, downloads, and installation."""


__all__ = [
    "AssetManager",
    "AssetOperationCancelled",
    "AssetRemovalPreview",
    "AssetRemovalReport",
    "AssetSpec",
    "CopernicusOrthophotoManager",
    "set_config_value",
]
