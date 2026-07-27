"""Asset manager for per-layer onboarding, downloads and imports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.utils import set_config_value
from TerraLab.data.copernicus import (
    CopernicusOrthophotoManager,
)

ProgressFn = Callable[[float, str], None]


def convert_planck_fits_to_cache(*args, **kwargs):
    """Load the optional FITS stack only when a Planck import is requested."""

    from TerraLab.data.converters.planck import (
        convert_planck_fits_to_cache as convert,
    )

    return convert(*args, **kwargs)


def convert_milkyway_fits_to_png(*args, **kwargs):
    """Load Astropy only when a Milky Way conversion is requested."""

    from TerraLab.util.milkyway_importer import (
        convert_milkyway_fits_to_png as convert,
    )

    return convert(*args, **kwargs)


def build_gaia_catalog_spawned(*args, **kwargs):
    """Load the catalogue import stack only for an explicit Gaia import."""

    from TerraLab.util.gaia_importer import build_gaia_catalog_spawned as build

    return build(*args, **kwargs)


def set_asset_config(key: str, value: object) -> None:
    """Write through the public module so tests and adapters can intercept it."""

    import sys

    public_module = sys.modules.get("TerraLab.data.assets_manager")
    setter = getattr(public_module, "set_config_value", set_config_value)
    setter(key, value)


def copernicus_manager_class():
    """Resolve the public download adapter, allowing controlled substitution."""

    import sys

    public_module = sys.modules.get("TerraLab.data.assets_manager")
    return getattr(
        public_module,
        "CopernicusOrthophotoManager",
        CopernicusOrthophotoManager,
    )


class AssetOperationCancelled(RuntimeError):
    """Raised after a cooperative cancellation, preserving resumable files."""


@dataclass(frozen=True)
class AssetSpec:
    asset_id: str
    title: str
    source_url: str
    accepted_formats: str
    credits: str = ""
    allow_multiple: bool = False
    auto_download_url: Optional[str] = None
    provider: str = ""
    semantic_type: str = ""
    nominal_resolution_m: float | None = None
    nominal_crs: str = ""
    geographic_extent: str = ""
    approximate_download_bytes: int = 0
    expected_download_bytes: int = 0
    expected_extracted_bytes: int = 0
    citation: str = ""
    license_note: str = ""


@dataclass(frozen=True)
class AssetRemovalPreview:
    """Files and catalogue links affected by removing one layer asset."""

    asset_id: str
    managed_paths: tuple[str, ...] = ()
    external_paths: tuple[str, ...] = ()
    retained_paths: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    total_bytes: int = 0
    manifest_registered: bool = False

    @property
    def has_data(self) -> bool:
        return bool(
            self.managed_paths
            or self.external_paths
            or self.source_ids
            or self.manifest_registered
        )


@dataclass(frozen=True)
class AssetRemovalReport:
    """Result of deleting managed data and unlinking external sources."""

    asset_id: str
    deleted_paths: tuple[str, ...] = ()
    detached_paths: tuple[str, ...] = ()
    retained_paths: tuple[str, ...] = ()
    removed_source_ids: tuple[str, ...] = ()
    released_bytes: int = 0


S2GLC_PRODUCT = "S2GLC Land Cover Map of Europe 2017"
S2GLC_PROVIDER = (
    "CBK PAN, Space Research Centre of the Polish Academy of Sciences"
)
S2GLC_SOURCE_URL = "https://s2glc.cbk.waw.pl/extension"
S2GLC_CITATION = (
    "Malinowski et al. (2020), Automated Production of a Land Cover/Use "
    "Map of Europe Based on Sentinel-2 Imagery, Remote Sensing 12(21), "
    "3523. https://doi.org/10.3390/rs12213523"
)
S2GLC_CREDITS = (
    f"{S2GLC_PRODUCT}. {S2GLC_PROVIDER}. Projecte finançat per l'ESA. "
    f"{S2GLC_CITATION}"
)
S2GLC_LICENSE_NOTE = (
    "Descàrrega gratuïta des de la font oficial. La font no declara "
    "expressament una llicència oberta de redistribució; consulteu les "
    "condicions del proveïdor."
)


def _progress(
    callback: Optional[ProgressFn], percent: float, message: str
) -> None:
    if callback is None:
        return
    try:
        callback(float(percent), str(message))
    except Exception:
        log_suppressed_exception(__name__, "_progress")


def _json_payload(value: Any) -> Any:
    """Return a manifest-safe representation of a core result object."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_payload(value.to_dict())
    if is_dataclass(value):
        return _json_payload(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _json_payload(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_payload(item) for item in value]
    raw_value = getattr(value, "value", None)
    if raw_value is not None and raw_value is not value:
        return _json_payload(raw_value)
    return str(value)


