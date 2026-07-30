"""Asset catalogue, manifest state, and removal operations."""

from __future__ import annotations

import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.data_library import DataLibrary
from TerraLab.common.utils import get_config_value
from TerraLab.data.assets.runtime import (
    AssetSpec,
    S2GLC_CITATION,
    S2GLC_CREDITS,
    S2GLC_LICENSE_NOTE,
    S2GLC_PROVIDER,
    S2GLC_SOURCE_URL,
    set_asset_config,
)
from TerraLab.data.copernicus import (
    ATTRIBUTION,
    DATA_POLICY_URL,
    PRODUCT_NAME as COPERNICUS_ORTHOPHOTO_PRODUCT,
    PRODUCT_URL as COPERNICUS_ORTHOPHOTO_PRODUCT_URL,
)
from TerraLab.data.resumable_download import PartialDownload, ResumableDownloader
from TerraLab.data.source_catalog import (
    DataSourceRegistry,
    LayerType,
    SourceHealthStatus,
)


class AssetRegistryMixin:
    def __init__(self, library: DataLibrary | None = None) -> None:
        self.library = library or DataLibrary.current(create=True)
        self.layout = self.library.layout(create=True)
        self.data_sources = DataSourceRegistry(
            self.layout["data_source_catalog"]
        )
        self.downloader = ResumableDownloader(self.layout["downloads_partial"])
        self.specs: Dict[str, AssetSpec] = {
            "climate_metno": AssetSpec(
                asset_id="climate_metno",
                title="Clima MET Norway",
                source_url="https://api.met.no/weatherapi/locationforecast/2.0/compact",
                accepted_formats="Configuracio (User-Agent obligatori)",
                credits="MET Norway (api.met.no) - Weather data under provider terms.",
                allow_multiple=False,
                auto_download_url=None,
            ),
            "gaia_catalog": AssetSpec(
                asset_id="gaia_catalog",
                title="Cataleg Gaia",
                source_url="https://gea.esac.esa.int/archive/",
                accepted_formats="ECSV (.ecsv), CSV (.csv) o ZST (.zst)",
                credits="ESA Gaia Archive / DPAC - Gaia DR3 terms apply.",
                allow_multiple=True,
                auto_download_url=None,
            ),
            "milkyway_texture": AssetSpec(
                asset_id="milkyway_texture",
                title="Via Lactia",
                source_url="https://galaxy.phy.cmich.edu/~axel/mwpan2/mwpan2_RGB_3600.fits",
                accepted_formats="FITS (.fits) o PNG RGBA (.png)",
                credits="Milky Way panorama source by Axel Mellinger (mwpan2).",
                allow_multiple=False,
                auto_download_url="https://galaxy.phy.cmich.edu/~axel/mwpan2/mwpan2_RGB_3600.fits",
            ),
            "planck_dust": AssetSpec(
                asset_id="planck_dust",
                title="Pols Planck",
                source_url="https://irsa.ipac.caltech.edu/data/Planck/release_2/all-sky-maps/maps/component-maps/foregrounds/COM_CompMap_Dust-GNILC-Model-Opacity_2048_R2.01.fits",
                accepted_formats="FITS (.fits)",
                credits="ESA / Planck Collaboration / Planck Legacy Archive.",
                allow_multiple=False,
                auto_download_url="https://irsa.ipac.caltech.edu/data/Planck/release_2/all-sky-maps/maps/component-maps/foregrounds/COM_CompMap_Dust-GNILC-Model-Opacity_2048_R2.01.fits",
            ),
            "elevation_dem": AssetSpec(
                asset_id="elevation_dem",
                title="Elevacions",
                source_url="https://gisco-services.ec.europa.eu/dem/5degree/mosaic/EU_DEM_mosaic_5deg.ZIP",
                accepted_formats="GeoTIFF (.tif/.tiff) o TXT/ASC (.txt/.asc), tambe ZIP",
                credits="EU-DEM (Copernicus/EEA via GISCO services).",
                allow_multiple=True,
                auto_download_url="https://gisco-services.ec.europa.eu/dem/5degree/mosaic/EU_DEM_mosaic_5deg.ZIP",
            ),
            "light_pollution": AssetSpec(
                asset_id="light_pollution",
                title="Contaminacio luminica",
                source_url="https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/15IKI5",
                accepted_formats="GeoTIFF (.tif/.tiff)",
                credits="VIIRS/DMSP composite via Harvard Dataverse DOI:10.7910/DVN/15IKI5.",
                allow_multiple=False,
                auto_download_url="https://dataverse.harvard.edu/api/datasets/:persistentId/?persistentId=doi:10.7910/DVN/15IKI5",
            ),
            "orthophoto": AssetSpec(
                asset_id="orthophoto",
                title="Ortofoto Copernicus",
                source_url=COPERNICUS_ORTHOPHOTO_PRODUCT_URL,
                accepted_formats=(
                    "GeoTIFF RGB U16/U8, GeoTIFF RGB U8 comprimit "
                    "o raster RGB/RGBA georeferenciat"
                ),
                credits=ATTRIBUTION,
                allow_multiple=True,
                # This download needs a geographic request, not one static
                # file URL.  The onboarding dialog enables it explicitly and
                # routes it through CopernicusOrthophotoManager.
                auto_download_url=None,
                provider="Copernicus Land Monitoring Service",
                semantic_type=LayerType.ORTHOPHOTO_RGB.value,
                nominal_resolution_m=10.0,
                nominal_crs="EPSG:3035",
                geographic_extent=(
                    "Europa; cobertura efectiva consultada al servei "
                    "ArcGIS ImageServer"
                ),
                citation=COPERNICUS_ORTHOPHOTO_PRODUCT,
                license_note=(
                    f"{ATTRIBUTION} Política de dades: {DATA_POLICY_URL}"
                ),
            ),
            "surface_rgb": AssetSpec(
                asset_id="surface_rgb",
                title="Cobertura del sòl — RGB",
                source_url=S2GLC_SOURCE_URL,
                accepted_formats="GeoTIFF RGB de tres bandes (o RGB amb alfa), també ZIP oficial",
                credits=S2GLC_CREDITS,
                allow_multiple=True,
                auto_download_url="https://users.cbk.waw.pl/~mkrupinski/S2GLC_Europe_2017_v1.2_RGB.zip",
                provider=S2GLC_PROVIDER,
                semantic_type=LayerType.LAND_COVER_RGB.value,
                nominal_resolution_m=10.0,
                nominal_crs="EPSG:3035 (verificat en instal·lar)",
                geographic_extent="Europa (extensió detectada del GeoTIFF)",
                approximate_download_bytes=16_200_000_000,
                expected_download_bytes=16_992_946_811,
                expected_extracted_bytes=17_423_097_171,
                citation=S2GLC_CITATION,
                license_note=S2GLC_LICENSE_NOTE,
            ),
            "surface_categorical": AssetSpec(
                asset_id="surface_categorical",
                title="Cobertura del sòl — categòrica",
                source_url=S2GLC_SOURCE_URL,
                accepted_formats="GeoTIFF d'una banda de codis enters, també ZIP oficial",
                credits=S2GLC_CREDITS,
                allow_multiple=True,
                auto_download_url="https://users.cbk.waw.pl/~mkrupinski/S2GLC_Europe_2017_v1.2_grey.zip",
                provider=S2GLC_PROVIDER,
                semantic_type=LayerType.LAND_COVER_CATEGORICAL.value,
                nominal_resolution_m=10.0,
                nominal_crs="EPSG:3035 (verificat en instal·lar)",
                geographic_extent="Europa (extensió detectada del GeoTIFF)",
                approximate_download_bytes=8_000_000_000,
                expected_download_bytes=7_950_798_367,
                expected_extracted_bytes=8_445_672_037,
                citation=S2GLC_CITATION,
                license_note=S2GLC_LICENSE_NOTE,
            ),
            "ngc_catalog": AssetSpec(
                asset_id="ngc_catalog",
                title="Cataleg NGC",
                source_url="https://github.com/mattiaverga/OpenNGC/blob/master/database_files/NGC.csv",
                accepted_formats="CSV (.csv)",
                credits="OpenNGC catalogue by Mattia Verga and contributors.",
                allow_multiple=False,
                auto_download_url="https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv",
            ),
            "solar_system_ephemeris": AssetSpec(
                asset_id="solar_system_ephemeris",
                title="Sol, Lluna i planetes",
                source_url="https://github.com/skyfielders/python-skyfield/blob/master/ci/de421.bsp",
                accepted_formats="Efemeride JPL BSP (.bsp)",
                credits="JPL Development Ephemeris DE421 / Skyfield.",
                allow_multiple=False,
                auto_download_url="https://raw.githubusercontent.com/skyfielders/python-skyfield/master/ci/de421.bsp",
            ),
        }
        self._ensure_bundled_data_sources()

    def _mark_asset_state(
        self, asset_id: str, ready: bool, path: str = ""
    ) -> None:
        now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        stored_path = str(path or "")
        if hasattr(self, "library"):
            self.library.update_asset(
                asset_id,
                ready=bool(ready),
                path=stored_path,
                updated_utc=now_utc,
            )
        # Compatibility flags remain in preferences for one release.  Paths are
        # deliberately owned by the library manifest, not by APPDATA config.
        set_asset_config(f"assets.{asset_id}.ready", bool(ready))
        set_asset_config(f"assets.{asset_id}.updated_utc", now_utc)

    def _mark_install_state(
        self,
        asset_id: str,
        state: str,
        *,
        error: str = "",
        **details,
    ) -> None:
        library = getattr(self, "library", None)
        if library is None:
            return
        library.update_asset(
            asset_id,
            install_state=str(state),
            install_error=str(error or ""),
            install_updated_utc=datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            **details,
        )

    def _download_target(self, asset_id: str) -> Path | None:
        spec = self.get_spec(asset_id)
        if not spec.auto_download_url:
            return None
        name = Path(urllib.parse.urlsplit(spec.auto_download_url).path).name
        download_dir = Path(
            self.layout.get(
                "downloads",
                Path(self.layout.get("tmp", Path.cwd())) / "downloads",
            )
        )
        return download_dir / (name or f"{asset_id}.bin")

    def partial_download(self, asset_id: str):
        target = self._download_target(asset_id)
        downloader = getattr(self, "downloader", None)
        partial = (
            downloader.partial_for(target)
            if target is not None and downloader is not None
            else None
        )
        if partial is not None:
            return partial
        # Cancelling verification/extraction happens after the atomic .part ->
        # ZIP promotion. Treat that archive as resumable preparation so an
        # application restart does not needlessly download another 8/17 GB.
        if target is not None and target.is_file():
            spec = self.get_spec(asset_id)
            expected = int(spec.expected_download_bytes or 0)
            actual = int(target.stat().st_size)
            if expected > 0 and actual == expected:
                return PartialDownload(
                    url=str(spec.auto_download_url or ""),
                    target_path=str(target),
                    partial_path=str(target),
                    metadata_path=str(
                        target.with_suffix(target.suffix + ".part.json")
                    ),
                    downloaded_bytes=actual,
                    expected_size=expected,
                    etag="",
                    last_modified="",
                    status="downloaded",
                )
        return None

    def get_specs(self) -> List[AssetSpec]:
        return list(self.specs.values())

    def get_spec(self, asset_id: str) -> AssetSpec:
        if asset_id not in self.specs:
            raise KeyError(f"Unknown asset id: {asset_id}")
        return self.specs[asset_id]

    def onboarding_asset_order(self) -> List[str]:
        return [
            "gaia_catalog",
            "ngc_catalog",
            "milkyway_texture",
            "planck_dust",
            "solar_system_ephemeris",
            "climate_metno",
            "elevation_dem",
            "orthophoto",
            "surface_categorical",
            "surface_rgb",
            "light_pollution",
        ]

    @staticmethod
    def _asset_layer_type(asset_id: str) -> LayerType | None:
        return {
            "elevation_dem": LayerType.ELEVATION,
            "orthophoto": LayerType.ORTHOPHOTO_RGB,
            "surface_rgb": LayerType.SURFACE_RGB,
            "surface_categorical": LayerType.SURFACE_CATEGORICAL,
            "light_pollution": LayerType.LIGHT_POLLUTION,
        }.get(str(asset_id))

    def _ensure_bundled_data_sources(self) -> None:
        """Register distributable fallbacks without copying them to user data."""

        registry = getattr(self, "data_sources", None)
        if registry is None:
            return
        bundled = (
            Path(__file__).resolve().parents[1]
            / "light_pollution"
            / "C_DVNL 2022.tif"
        )
        if not bundled.exists():
            return
        try:
            registry.register_path(
                bundled,
                LayerType.LIGHT_POLLUTION,
                display_name="DVNL 2022",
                priority=-100,
                enabled=True,
                metadata={"bundled": True},
                provenance="bundled",
                attribution="VIIRS/DMSP DVNL 2022",
            )
        except Exception:
            log_suppressed_exception(__name__, "AssetRegistryMixin._ensure_bundled_data_sources")

    def get_user_agent(self) -> str:
        return str(
            get_config_value("weather.metno_user_agent", "") or ""
        ).strip()

    def set_user_agent(self, value: str) -> None:
        ua = str(value or "").strip()
        set_asset_config("weather.metno_user_agent", ua)
        self._mark_asset_state("climate_metno", bool(ua), "")

    def asset_ready(self, asset_id: str) -> bool:
        return bool(self.asset_status(asset_id).get("ready", False))

    def _manifest_asset_path(self, asset_id: str) -> Path | None:
        library = getattr(self, "library", None)
        if library is None:
            return None
        raw = str(library.asset_state(asset_id).get("path", "") or "").strip()
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        try:
            return candidate.resolve() if candidate.is_file() else None
        except OSError:
            return None

    def asset_status(self, asset_id: str) -> Dict[str, object]:
        layout = self.layout
        if asset_id == "climate_metno":
            ua = self.get_user_agent()
            return {
                "ready": bool(ua),
                "reason": "ok" if ua else "missing_user_agent",
                "path": "",
            }
        if asset_id == "gaia_catalog":
            configured = self._manifest_asset_path(asset_id)
            if configured is not None:
                return {"ready": True, "reason": "ok", "path": str(configured)}
            p_npz = Path(layout["data_gaia"]) / "stars_catalog.npz"
            p_zst = Path(layout["data_gaia"]) / "stars_catalog.zst"
            p_npy = Path(layout["data_gaia"]) / "stars_catalog.npy"
            p_tile_manifest = Path(layout["data_gaia"]) / "tile_manifest.json"
            p_tile_all = Path(layout["data_gaia"]) / "tile_all.npz"
            for candidate in (p_npy, p_npz, p_zst):
                if candidate.exists():
                    return {
                        "ready": True,
                        "reason": "ok",
                        "path": str(candidate),
                    }
            if p_tile_manifest.exists() and p_tile_all.exists():
                return {
                    "ready": True,
                    "reason": "ok_tile_manifest",
                    "path": str(p_tile_manifest),
                }
            return {
                "ready": False,
                "reason": "missing_catalog",
                "path": str(p_npy),
            }
        if asset_id == "milkyway_texture":
            p = self._manifest_asset_path(asset_id) or (
                Path(layout["data_milkyway"]) / "milkyway_overlay.png"
            )
            exists = p.exists()
            return {
                "ready": exists,
                "reason": "ok" if exists else "missing_texture",
                "path": str(p),
            }
        if asset_id == "planck_dust":
            p = self._manifest_asset_path(asset_id) or (
                Path(layout["data_planck"]) / "planck_dust_opacity_eq_u16.npz"
            )
            exists = p.exists()
            return {
                "ready": exists,
                "reason": "ok" if exists else "missing_dust_map",
                "path": str(p),
            }
        layer_type = self._asset_layer_type(asset_id)
        if layer_type is not None:
            sources = self.data_sources.list_sources(layer_type)
            available = [source for source in sources if source.available and source.enabled]
            invalid = [
                source
                for source in sources
                if source.health_status is SourceHealthStatus.INVALID
            ]
            effective = available[0] if available else None
            partial = self.partial_download(asset_id)
            library = getattr(self, "library", None)
            manifest_state = (
                library.asset_state(asset_id) if library is not None else {}
            )
            install_state = str(
                manifest_state.get("install_state", "") or ""
            )
            download_manifest_path = str(
                manifest_state.get("download_manifest_path", "") or ""
            )
            estimate_state = manifest_state.get("selection_estimate", {})
            if not isinstance(estimate_state, Mapping):
                estimate_state = {}
            estimated_download_bytes = 0
            for estimate_key in (
                "compressed_estimate_bytes",
                "estimated_compressed_bytes",
                "raw_u16_bytes",
                "raw_u8_bytes",
            ):
                try:
                    estimated_download_bytes = int(
                        estimate_state.get(estimate_key, 0) or 0
                    )
                except (TypeError, ValueError):
                    estimated_download_bytes = 0
                if estimated_download_bytes > 0:
                    break
            active_orthophoto_install = (
                asset_id == "orthophoto"
                and install_state
                in {
                    "downloading",
                    "partial",
                    "paused",
                    "registering",
                    "error",
                }
            )
            if effective is not None and not active_orthophoto_install:
                install_state = "prepared"
            elif partial is not None and partial.downloaded_bytes > 0:
                install_state = {
                    "downloading": "downloading",
                    "paused": "paused",
                    "error": "error",
                }.get(partial.status, "partial")
            elif not install_state:
                install_state = "not_configured"
            return {
                "ready": bool(effective),
                "reason": (
                    "ok"
                    if effective is not None
                    else ("invalid_source" if invalid else "missing_source")
                ),
                "path": str(effective.path) if effective is not None else "",
                "source_id": str(effective.id) if effective is not None else "",
                "source_count": len(sources),
                "install_state": install_state,
                "install_error": str(
                    manifest_state.get("install_error", "") or ""
                ),
                "partial_bytes": int(
                    partial.downloaded_bytes if partial is not None else 0
                ),
                "expected_bytes": int(
                    partial.expected_size
                    if partial is not None
                    else (
                        estimated_download_bytes
                        or self.get_spec(asset_id).expected_download_bytes
                    )
                ),
                "resumable": bool(
                    (partial is not None and partial.resumable)
                    or (
                        download_manifest_path
                        and Path(download_manifest_path).is_file()
                        and install_state
                        in {
                            "downloading",
                            "partial",
                            "paused",
                            "error",
                        }
                    )
                    or (
                        asset_id == "orthophoto"
                        and install_state
                        in {
                            "downloading",
                            "partial",
                            "paused",
                            "error",
                        }
                        and isinstance(
                            manifest_state.get("copernicus_request"),
                            Mapping,
                        )
                    )
                ),
                "download_manifest_path": download_manifest_path,
            }
        if asset_id == "ngc_catalog":
            p = self._manifest_asset_path(asset_id) or (
                Path(layout["data_ngc"]) / "openngc_catalog.csv"
            )
            exists = p.exists()
            return {
                "ready": exists,
                "reason": "ok" if exists else "missing_catalog",
                "path": str(p),
            }
        if asset_id == "solar_system_ephemeris":
            p = self.resolve_ephemeris_path()
            return {
                "ready": p is not None,
                "reason": "ok" if p is not None else "missing_ephemeris",
                "path": str(p or ""),
            }
        return {"ready": False, "reason": "unknown_asset", "path": ""}

    def _asset_data_root(self, asset_id: str) -> Path | None:
        layout_key = {
            "gaia_catalog": "data_gaia",
            "ngc_catalog": "data_ngc",
            "milkyway_texture": "data_milkyway",
            "planck_dust": "data_planck",
            "solar_system_ephemeris": "data_ephemeris",
            "climate_metno": "cache_weather",
            "elevation_dem": "data_elevation",
            "orthophoto": "data_surface",
            "surface_rgb": "data_surface",
            "surface_categorical": "data_surface",
            "light_pollution": "data_light_pollution",
        }.get(str(asset_id))
        raw = self.layout.get(layout_key) if layout_key else None
        return Path(raw).resolve(strict=False) if raw is not None else None

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))

    @staticmethod
    def _path_has_content(path: Path) -> bool:
        try:
            if path.is_symlink() or path.is_file():
                return True
            return path.is_dir() and next(path.iterdir(), None) is not None
        except OSError:
            return False

