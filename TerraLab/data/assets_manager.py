"""Canonical data-library asset manager."""

from TerraLab.common.data_library import DataLibrary
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

    def __init__(self, library: DataLibrary | None = None) -> None:
        """Initialize the registry explicitly; mixin order is not a contract."""

        AssetRegistryMixin.__init__(self, library)


__all__ = [
    "AssetManager",
    "AssetOperationCancelled",
    "AssetRemovalPreview",
    "AssetRemovalReport",
    "AssetSpec",
    "CopernicusOrthophotoManager",
    "set_config_value",
]
