"""Asset manager for per-layer onboarding, downloads and imports."""

from __future__ import annotations

import os
import re
import json
import hashlib
import shutil
import urllib.request
import urllib.parse
import zipfile
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from TerraLab.common.app_paths import ensure_runtime_layout
from TerraLab.common.data_library import DataLibrary
from TerraLab.common.utils import get_config_value, set_config_value
from TerraLab.tools.convert_planck_dust import convert_planck_fits_to_cache
from TerraLab.terrain.asc_cache_builder import materialize_asc_caches_spawned
from TerraLab.terrain.data_sources import (
    DataSourceRegistry,
    LayerType,
    SourceHealthStatus,
)
from TerraLab.terrain.source_inspection import inspect_data_source
from TerraLab.util.gaia_importer import build_gaia_catalog_spawned
from TerraLab.util.milkyway_importer import convert_milkyway_fits_to_png

ProgressFn = Callable[[float, str], None]


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


def _progress(
    callback: Optional[ProgressFn], percent: float, message: str
) -> None:
    if callback is None:
        return
    try:
        callback(float(percent), str(message))
    except Exception:
        pass


class AssetManager:
    def __init__(self, library: DataLibrary | None = None) -> None:
        self.library = library or DataLibrary.current(create=True)
        self.layout = self.library.layout(create=True)
        self.data_sources = DataSourceRegistry(
            self.layout["data_source_catalog"]
        )
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
            "surface_rgb": AssetSpec(
                asset_id="surface_rgb",
                title="Tipus de sol (RGB)",
                source_url="https://www.eea.europa.eu/en/datahub/datahubitem-view/d62ade7a-9c9c-405e-b88e-6ce0410d6a4f",
                accepted_formats="GeoTIFF/VRT/IMG/JP2/PNG/JPEG",
                credits="Copernicus Land Monitoring Service / user supplied data.",
                allow_multiple=True,
                auto_download_url=None,
            ),
            "surface_categorical": AssetSpec(
                asset_id="surface_categorical",
                title="Tipus de sol (categories)",
                source_url="https://geoserver.geoville.com/geoserver/clcp/ows?service=WMS&version=1.3.0&request=GetCapabilities",
                accepted_formats="GeoTIFF/VRT/IMG/JP2 categoric local; WMS pendent",
                credits="Copernicus Land Monitoring Service / user supplied data.",
                allow_multiple=True,
                auto_download_url=None,
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
        set_config_value(f"assets.{asset_id}.ready", bool(ready))
        set_config_value(f"assets.{asset_id}.updated_utc", now_utc)

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
            "surface_categorical",
            "light_pollution",
        ]

    @staticmethod
    def _asset_layer_type(asset_id: str) -> LayerType | None:
        return {
            "elevation_dem": LayerType.ELEVATION,
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
            Path(__file__).resolve().parent
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
            pass

    def get_user_agent(self) -> str:
        return str(
            get_config_value("weather.metno_user_agent", "") or ""
        ).strip()

    def set_user_agent(self, value: str) -> None:
        ua = str(value or "").strip()
        set_config_value("weather.metno_user_agent", ua)
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

    def resolve_ephemeris_path(self) -> Path | None:
        configured = self.library.asset_state("solar_system_ephemeris")
        raw = str(configured.get("path", "") or "").strip()
        candidates = []
        if raw:
            candidates.append(Path(raw))
        candidates.append(Path(self.layout["data_ephemeris"]) / "de421.bsp")
        project = Path(__file__).resolve().parents[1]
        candidates.extend((project / "data" / "stars" / "de421.bsp", project / "de421.bsp"))
        for candidate in candidates:
            try:
                if candidate.exists() and candidate.is_file():
                    return candidate.resolve()
            except OSError:
                continue
        return None

    @staticmethod
    def _copy_file(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _copy_raster_with_sidecars(src: Path, dst: Path) -> None:
        """
        Copy a GeoTIFF plus common sidecar files (aux/xml/ovr/tfw/wld/prj).
        Some rasters store CRS metadata in sidecars instead of the TIFF tags.
        """
        AssetManager._copy_file(src, dst)

        src = Path(src)
        dst = Path(dst)

        # Sidecars named as "<filename>.tif.*" (e.g. .aux.xml/.xml/.ovr).
        prefix = src.name + "."
        for sidecar in src.parent.glob(f"{src.name}.*"):
            if not sidecar.is_file():
                continue
            name = sidecar.name
            if not name.startswith(prefix):
                continue
            suffix = name[len(src.name) :]
            target = dst.parent / f"{dst.name}{suffix}"
            try:
                shutil.copyfile(sidecar, target)
            except Exception:
                pass

        # World/projection files tied to basename (e.g. .tfw/.wld/.prj).
        for ext in (".tfw", ".wld", ".prj"):
            sidecar = src.with_suffix(ext)
            if not sidecar.exists() or not sidecar.is_file():
                continue
            target = dst.with_suffix(ext)
            try:
                shutil.copyfile(sidecar, target)
            except Exception:
                pass

    @staticmethod
    def _valid_lat_lon(lat: float, lon: float) -> bool:
        """
        Validate a geographic coordinate in EPSG:4326.

        Input CRS:
            - `lat`, `lon` in `EPSG:4326`.
        Internal CRS:
            - Not applicable.
        Output CRS:
            - Not applicable (boolean validation result).
        """
        if not (math.isfinite(float(lat)) and math.isfinite(float(lon))):
            return False
        return -90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0

    @staticmethod
    def _resolve_timezone_name(lat: float, lon: float) -> str:
        """
        Resolve observer timezone name from geographic coordinates.

        Input CRS:
            - `lat`, `lon` in `EPSG:4326`.
        Internal CRS:
            - Not applicable.
        Output CRS:
            - Not applicable; returns IANA timezone string or empty string.
        """
        try:
            from timezonefinder import TimezoneFinder

            tz = TimezoneFinder().timezone_at(lat=float(lat), lng=float(lon))
            return str(tz or "").strip()
        except Exception:
            return ""

    def _estimate_observer_from_tiff_dem(
        self, dem_source: Path
    ) -> Optional[Tuple[float, float]]:
        """
        Estimate observer `(lat, lon)` from the center of the selected DEM GeoTIFF.

        Input CRS:
            - Raster center point in raster native CRS.
        Internal CRS:
            - None for this computation.
        Output CRS:
            - Returns `(lat, lon)` in `EPSG:4326`.

        Missing CRS policy:
            - If the GeoTIFF has no CRS, fallback is explicitly `EPSG:4326`
              to stay aligned with `TiffRasterWindowProvider`.
        """
        try:
            import rasterio
            from pyproj import Transformer

            from TerraLab.terrain.providers import (
                CRS_GEOGRAPHIC,
                PYPROJ_TRANSFORMER_LOCK,
            )
        except Exception:
            return None

        try:
            with rasterio.open(str(dem_source)) as src:
                bounds = src.bounds
                center_x = 0.5 * (float(bounds.left) + float(bounds.right))
                center_y = 0.5 * (float(bounds.bottom) + float(bounds.top))
                src_crs = (
                    src.crs.to_string()
                    if getattr(src, "crs", None)
                    else CRS_GEOGRAPHIC
                )
        except Exception:
            return None

        try:
            with PYPROJ_TRANSFORMER_LOCK:
                tr = Transformer.from_crs(
                    src_crs, CRS_GEOGRAPHIC, always_xy=True
                )
            lon, lat = tr.transform(center_x, center_y)
        except Exception:
            return None
        if not self._valid_lat_lon(lat, lon):
            return None
        return float(lat), float(lon)

    def _estimate_observer_from_asc_dem(
        self, dem_dir: Path
    ) -> Optional[Tuple[float, float]]:
        """
        Estimate observer `(lat, lon)` from ASC/TXT DEM coverage.

        Input CRS:
            - ASCII DEM tile headers are interpreted in terrain internal CRS.
        Internal CRS:
            - `EPSG:25831` (`CRS_TERRAIN_INTERNAL`).
        Output CRS:
            - Returns `(lat, lon)` in `EPSG:4326`.
        """
        try:
            from TerraLab.terrain.engine import TileIndex
            from TerraLab.terrain.crs import (
                CRS_GEOGRAPHIC,
                CRS_TERRAIN_INTERNAL,
                DEFAULT_TRANSFORM_SERVICE,
            )
        except Exception:
            return None

        asc_files = sorted(
            list(dem_dir.rglob("*.asc")) + list(dem_dir.rglob("*.txt")),
            key=lambda p: p.name.lower(),
        )
        if not asc_files:
            return None

        west, south = float("inf"), float("inf")
        east, north = float("-inf"), float("-inf")
        for tile_path in asc_files:
            try:
                header = TileIndex._read_header(str(tile_path))
                bbox = TileIndex._compute_bbox(header)
                geographic = DEFAULT_TRANSFORM_SERVICE.transform_bounds(
                    tuple(float(value) for value in bbox),
                    CRS_TERRAIN_INTERNAL,
                    CRS_GEOGRAPHIC,
                )
            except Exception:
                continue
            west = min(west, float(geographic[0]))
            south = min(south, float(geographic[1]))
            east = max(east, float(geographic[2]))
            north = max(north, float(geographic[3]))

        if not (
            math.isfinite(west)
            and math.isfinite(south)
            and math.isfinite(east)
            and math.isfinite(north)
            and east > west
            and north > south
        ):
            return None
        lon = 0.5 * (west + east)
        lat = 0.5 * (south + north)
        if not self._valid_lat_lon(lat, lon):
            return None
        return float(lat), float(lon)

    def _estimate_observer_from_dem(
        self, dem_source: Path
    ) -> Optional[Tuple[float, float]]:
        """Return the centre of the full recursive DEM coverage in WGS84."""

        source = Path(dem_source)
        if source.is_dir() and (
            next(source.rglob("*.asc"), None) is not None
            or next(source.rglob("*.txt"), None) is not None
        ):
            estimated = self._estimate_observer_from_asc_dem(source)
            if estimated is not None:
                return estimated
        try:
            inspected = inspect_data_source(
                str(dem_source), LayerType.ELEVATION, source_id="observer"
            )
            if inspected.coverage is not None:
                west, south, east, north = inspected.coverage
                if west <= east:
                    lon = 0.5 * (float(west) + float(east))
                else:
                    lon = ((float(west) + float(east) + 360.0) * 0.5) % 360.0
                    if lon > 180.0:
                        lon -= 360.0
                lat = 0.5 * (float(south) + float(north))
                if self._valid_lat_lon(lat, lon):
                    return float(lat), float(lon)
        except Exception:
            pass

        if source.is_file() and source.suffix.lower() in {".tif", ".tiff"}:
            return self._estimate_observer_from_tiff_dem(source)
        return self._estimate_observer_from_asc_dem(source)

    def _auto_configure_observer_from_dem(
        self, dem_dir: Path
    ) -> Dict[str, object]:
        """
        Auto-configure observer position from imported DEM assets.

        Input CRS:
            - For GeoTIFF DEM: raster native CRS center.
            - For ASC/TXT DEM: `EPSG:25831` tile header coordinates.
        Internal CRS:
            - Uses `EPSG:25831` only for ASC/TXT conversion.
        Output CRS:
            - Stores observer location in `EPSG:4326` (`observer_lat/lon`).

        Returns:
            - Dict with `applied` plus optional `lat`, `lon`, `timezone`.
              `applied=False` means no valid auto-location could be derived.
        """
        lat_lon = self._estimate_observer_from_dem(Path(dem_dir))

        if lat_lon is None:
            return {"applied": False}

        lat, lon = float(lat_lon[0]), float(lat_lon[1])
        if not self._valid_lat_lon(lat, lon):
            return {"applied": False}

        set_config_value("observer_lat", lat)
        set_config_value("observer_lon", lon)
        tz_name = self._resolve_timezone_name(lat, lon)
        if tz_name:
            set_config_value("observer_timezone", tz_name)
        print(
            f"[AssetManager] Auto observer from DEM: lat={lat:.6f}, lon={lon:.6f}, tz={tz_name or '-'}"
        )
        result = {"applied": True, "lat": lat, "lon": lon}
        if tz_name:
            result["timezone"] = tz_name
        return result

    @staticmethod
    def _dataset_slug(value: str, fallback: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
        normalized = normalized.strip("._-")
        return normalized or fallback

    @classmethod
    def _unique_dataset_dir(cls, parent: Path, name: str, fallback: str) -> Path:
        base = cls._dataset_slug(name, fallback)
        candidate = parent / base
        suffix = 2
        while candidate.exists():
            candidate = parent / f"{base}_{suffix}"
            suffix += 1
        candidate.mkdir(parents=True, exist_ok=False)
        return candidate

    @staticmethod
    def _safe_extract_zip(archive: Path, destination: Path) -> None:
        destination_root = destination.resolve(strict=False)
        with zipfile.ZipFile(archive, "r") as handle:
            for member in handle.infolist():
                target = (destination / member.filename).resolve(strict=False)
                try:
                    target.relative_to(destination_root)
                except ValueError as exc:
                    raise ValueError(
                        f"Unsafe path in ZIP archive: {member.filename}"
                    ) from exc
            handle.extractall(destination)

    @staticmethod
    def _safe_extract_7z(archive: Path, destination: Path) -> None:
        try:
            import py7zr
        except Exception as exc:
            raise RuntimeError(
                "Cal instal-lar py7zr per extreure la descàrrega DVNL."
            ) from exc
        destination_root = destination.resolve(strict=False)
        with py7zr.SevenZipFile(archive, mode="r") as handle:
            names = handle.getnames()
            for name in names:
                target = (destination / name).resolve(strict=False)
                try:
                    target.relative_to(destination_root)
                except ValueError as exc:
                    raise ValueError(f"Unsafe path in 7z archive: {name}") from exc
            handle.extractall(path=destination)

    def _register_data_source(
        self,
        path: str | os.PathLike[str],
        layer_type: LayerType,
        **fields,
    ):
        metadata = dict(fields.pop("metadata", {}) or {})
        metadata["inspection_status"] = SourceHealthStatus.PENDING.value
        source = self.data_sources.register_path(
            path,
            layer_type,
            enabled=False,
            metadata=metadata,
            **fields,
        )
        try:
            inspected = inspect_data_source(
                str(path),
                layer_type,
                source_id=source.id,
                declared_crs=source.crs,
                metadata=metadata,
            )
        except Exception as exc:
            invalid_metadata = dict(metadata)
            invalid_metadata["inspection_status"] = SourceHealthStatus.INVALID.value
            invalid_metadata["inspection_error"] = str(exc)
            self.data_sources.update(
                source.id, metadata=invalid_metadata, enabled=False
            )
            raise ValueError(f"{path} no és un raster vàlid: {exc}") from exc
        inspected_fields = inspected.registry_fields()
        healthy_metadata = dict(inspected_fields.get("metadata", {}) or {})
        healthy_metadata.pop("inspection_error", None)
        healthy_metadata["inspection_status"] = SourceHealthStatus.HEALTHY.value
        inspected_fields["metadata"] = healthy_metadata
        inspected_fields["enabled"] = True
        return self.data_sources.update(source.id, **inspected_fields)

    def register_external_source(
        self,
        asset_id: str,
        path: str | os.PathLike[str],
        *,
        display_name: str | None = None,
        priority: int = 0,
    ):
        """Register a user-owned geospatial source without copying it."""

        layer_type = self._asset_layer_type(asset_id)
        if layer_type is None:
            raise ValueError(f"Asset {asset_id} does not support external sources")
        candidate = Path(path).expanduser().resolve(strict=False)
        if not candidate.exists():
            raise FileNotFoundError(str(candidate))
        source = self._register_data_source(
            candidate,
            layer_type,
            display_name=display_name or candidate.stem or candidate.name,
            priority=int(priority),
            provenance="external",
            metadata={"managed": False},
        )
        self._mark_asset_state(asset_id, True, "")
        return source

    def attach_external_asset(
        self, asset_id: str, path: str | os.PathLike[str]
    ) -> Dict[str, object]:
        """Link an astronomical resource in place whenever runtime supports it.

        Formats that need conversion keep the original external and publish
        only their generated derivative below the library root.
        """

        candidate = Path(path).expanduser().resolve(strict=False)
        if not candidate.is_file():
            raise FileNotFoundError(str(candidate))
        suffix = candidate.suffix.lower()
        if asset_id == "ngc_catalog":
            if suffix != ".csv":
                raise ValueError("El catàleg NGC ha de ser CSV.")
            set_config_value("ngc_catalog_path", str(candidate))
            self._mark_asset_state(asset_id, True, str(candidate))
            self.library.update_asset(
                asset_id,
                managed=False,
                source_path=str(candidate),
                checksum_sha256=self._sha256(candidate),
                size_bytes=int(candidate.stat().st_size),
                attribution="OpenNGC by Mattia Verga and contributors",
                license="CC-BY-SA-4.0",
            )
            return {"ok": True, "linked": True, "stored_in": str(candidate)}
        if asset_id == "solar_system_ephemeris":
            if suffix != ".bsp":
                raise ValueError("L'efemèride ha de ser un fitxer BSP.")
            self._mark_asset_state(asset_id, True, str(candidate))
            return {"ok": True, "linked": True, "stored_in": str(candidate)}
        if asset_id == "milkyway_texture" and suffix in {".png", ".jpg", ".jpeg"}:
            set_config_value("milkyway_overlay_texture_path", str(candidate))
            self._mark_asset_state(asset_id, True, str(candidate))
            return {"ok": True, "linked": True, "stored_in": str(candidate)}
        if asset_id == "planck_dust" and suffix in {".npz", ".zst", ".npy"}:
            set_config_value("dust_map_path", str(candidate))
            self._mark_asset_state(asset_id, True, str(candidate))
            return {"ok": True, "linked": True, "stored_in": str(candidate)}
        if asset_id == "gaia_catalog" and suffix in {".npy", ".npz", ".zst"}:
            set_config_value("gaia_catalog_path", str(candidate))
            self._mark_asset_state(asset_id, True, str(candidate))
            return {"ok": True, "linked": True, "stored_in": str(candidate)}
        if asset_id in {"gaia_catalog", "milkyway_texture", "planck_dust"}:
            self.library.update_asset(asset_id, source_path=str(candidate), managed=False)
            return self.import_files(asset_id, [str(candidate)])
        raise ValueError(f"L'asset {asset_id} no admet aquesta font externa.")

    def _download_file(
        self,
        url: str,
        target_path: Path,
        progress_callback: Optional[ProgressFn] = None,
        *,
        expected_size: int | None = None,
        expected_md5: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        partial_root = Path(self.layout.get("downloads_partial", target_path.parent))
        partial_root.mkdir(parents=True, exist_ok=True)
        partial_path = partial_root / f"{target_path.name}.part"
        downloaded = partial_path.stat().st_size if partial_path.exists() else 0
        headers = {
            "User-Agent": "TerraLab/1.0 (layer library downloader)",
            "Accept": "*/*",
        }
        if downloaded:
            headers["Range"] = f"bytes={downloaded}-"
        if cancelled is not None and cancelled():
            raise AssetOperationCancelled("Descàrrega cancel·lada.")
        _progress(progress_callback, 2.0, f"Descarregant {url}")
        req = urllib.request.Request(
            url,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            resumed = downloaded > 0 and int(response.getcode() or 0) == 206
            if downloaded and not resumed:
                downloaded = 0
            response_size = int(response.headers.get("Content-Length", "0") or "0")
            total = int(expected_size or 0) or (
                downloaded + response_size if response_size else 0
            )
            if total:
                free = shutil.disk_usage(partial_root).free
                remaining = max(0, total - downloaded)
                if free < remaining:
                    raise OSError(
                        f"Espai insuficient: calen {remaining} bytes i només n'hi ha {free}."
                    )
            mode = "ab" if resumed else "wb"
            with partial_path.open(mode) as out:
                while True:
                    if cancelled is not None and cancelled():
                        raise AssetOperationCancelled(
                            "Descàrrega cancel·lada; es conserva el fitxer .part per reprendre-la."
                        )
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = 2.0 + 78.0 * min(1.0, float(downloaded) / float(total))
                        _progress(
                            progress_callback,
                            pct,
                            f"Descarregat {downloaded}/{total} bytes",
                        )
                    else:
                        pct = min(
                            80.0, 2.0 + (downloaded / (1024.0 * 1024.0)) * 0.25
                        )
                        _progress(progress_callback, pct, f"Descarregat {downloaded} bytes")
                out.flush()
                os.fsync(out.fileno())
        if expected_size and partial_path.stat().st_size != int(expected_size):
            raise IOError(
                f"Mida incorrecta: {partial_path.stat().st_size} != {expected_size}."
            )
        if expected_md5:
            digest = hashlib.md5()  # noqa: S324 - provider publishes MD5 metadata.
            with partial_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != str(expected_md5).lower():
                raise IOError("La suma MD5 de la descàrrega no coincideix.")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial_path, target_path)
        _progress(progress_callback, 82.0, "Descarrega completada.")
        return target_path

    @staticmethod
    def _latest_dvnl_download(metadata_url: str) -> Dict[str, object]:
        req = urllib.request.Request(
            metadata_url,
            headers={"User-Agent": "TerraLab/1.0 (layer metadata client)"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        files = payload.get("data", {}).get("latestVersion", {}).get("files", [])
        candidates = []
        for item in files:
            data_file = item.get("dataFile", {}) if isinstance(item, dict) else {}
            name = str(data_file.get("filename", ""))
            match = re.fullmatch(r"C_DVNL\s+(\d{4})\.tif\.7z", name, re.I)
            if match:
                candidates.append((int(match.group(1)), data_file))
        if not candidates:
            raise RuntimeError("Harvard Dataverse no ha retornat cap C_DVNL anual.")
        _year, selected = max(candidates, key=lambda item: item[0])
        file_id = selected.get("id")
        if not file_id:
            raise RuntimeError("La metadata DVNL no conté l'identificador del fitxer.")
        checksum = selected.get("checksum") or {}
        return {
            "url": f"https://dataverse.harvard.edu/api/access/datafile/{file_id}",
            "filename": str(selected.get("filename") or "C_DVNL.tif.7z"),
            "size": int(selected.get("filesize") or 0),
            "md5": str(checksum.get("value") or "")
            if str(checksum.get("type") or "").upper() == "MD5"
            else "",
        }

    def download_and_prepare(
        self,
        asset_id: str,
        progress_callback: Optional[ProgressFn] = None,
        options: Optional[Dict[str, object]] = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Dict[str, object]:
        spec = self.get_spec(asset_id)
        if not spec.auto_download_url:
            raise ValueError(
                f"Asset {asset_id} does not support automatic download."
            )
        download_url = spec.auto_download_url
        expected_size = None
        expected_md5 = None
        if asset_id == "light_pollution":
            resolved = self._latest_dvnl_download(download_url)
            download_url = str(resolved["url"])
            filename = str(resolved["filename"])
            expected_size = int(resolved["size"] or 0) or None
            expected_md5 = str(resolved["md5"] or "") or None
        else:
            filename = Path(urllib.parse.urlsplit(download_url).path).name
            filename = filename or f"{asset_id}.bin"
        download_dir = Path(self.layout.get("downloads", self.layout["tmp"]))
        tmp_path = download_dir / filename
        downloaded = self._download_file(
            download_url,
            tmp_path,
            progress_callback=progress_callback,
            expected_size=expected_size,
            expected_md5=expected_md5,
            cancelled=cancelled,
        )
        if cancelled is not None and cancelled():
            raise AssetOperationCancelled("Preparació cancel·lada.")
        return self.import_files(
            asset_id,
            [str(downloaded)],
            progress_callback=progress_callback,
            options=options,
        )

    def import_files(
        self,
        asset_id: str,
        files: Iterable[str],
        progress_callback: Optional[ProgressFn] = None,
        options: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        opts = dict(options or {})
        paths = [Path(p) for p in files if str(p).strip()]
        if asset_id == "climate_metno":
            ua = self.get_user_agent()
            self._mark_asset_state("climate_metno", bool(ua), "")
            if not ua:
                raise ValueError(
                    "Cal definir un User-Agent per MET Norway abans d'activar el clima."
                )
            return {"ok": True, "message": "User-Agent configurat."}

        if not paths:
            raise ValueError("No s'han seleccionat fitxers.")

        if asset_id == "gaia_catalog":
            out_dir = Path(self.layout["data_gaia"])
            build_healpy_index = bool(opts.get("build_healpy_index", True))
            healpy_nside = int(opts.get("healpy_nside", 512) or 512)
            healpy_chunk_rows = int(
                opts.get("healpy_chunk_rows", 1_000_000) or 1_000_000
            )
            summary = build_gaia_catalog_spawned(
                [str(p) for p in paths],
                str(out_dir),
                output_basename="stars_catalog",
                write_npz=False,
                write_npy=True,
                write_zst=False,
                build_healpy_index=build_healpy_index,
                healpy_nside=healpy_nside,
                healpy_chunk_rows=healpy_chunk_rows,
                progress_callback=progress_callback,
            )
            no_gaia_src = (
                Path(__file__).resolve().parents[1]
                / "data"
                / "stars"
                / "no_gaia_stars.json"
            )
            no_gaia_dst = out_dir / "no_gaia_stars.json"
            try:
                if (not no_gaia_dst.exists()) and no_gaia_src.exists():
                    self._copy_file(no_gaia_src, no_gaia_dst)
            except Exception:
                pass
            npz_path = out_dir / "stars_catalog.npz"
            npy_path = out_dir / "stars_catalog.npy"
            zst_path = out_dir / "stars_catalog.zst"
            selected_path = (
                npy_path
                if npy_path.exists()
                else (npz_path if npz_path.exists() else zst_path)
            )
            set_config_value("gaia_catalog_path", str(selected_path))
            self._mark_asset_state("gaia_catalog", True, str(selected_path))
            return {"ok": True, "summary": summary, "stored_in": str(out_dir)}

        if asset_id == "milkyway_texture":
            out_dir = Path(self.layout["data_milkyway"])
            dst_png = out_dir / "milkyway_overlay.png"
            src = paths[0]
            _progress(progress_callback, 5.0, "Processant Via Lactia...")
            remove_stars = bool(
                opts.get(
                    "remove_stars",
                    get_config_value("milkyway_starless_enabled", True),
                )
            )
            png_compress_level = int(
                get_config_value("milkyway_png_compress_level", 0)
            )
            if src.suffix.lower() == ".fits":
                summary = convert_milkyway_fits_to_png(
                    str(src),
                    str(dst_png),
                    remove_stars=remove_stars,
                    png_compress_level=png_compress_level,
                    progress_callback=progress_callback,
                )
            else:
                self._copy_file(src, dst_png)
                summary = {"output_png": str(dst_png)}
                _progress(progress_callback, 100.0, "Via Lactia importada.")
            set_config_value("milkyway_overlay_texture_path", str(dst_png))
            set_config_value("milkyway_starless_enabled", bool(remove_stars))
            set_config_value(
                "milkyway_png_compress_level",
                int(max(0, min(9, png_compress_level))),
            )
            self._mark_asset_state("milkyway_texture", True, str(dst_png))
            return {"ok": True, "summary": summary, "stored_in": str(out_dir)}

        if asset_id == "planck_dust":
            out_dir = Path(self.layout["data_planck"])
            out_dir.mkdir(parents=True, exist_ok=True)
            src = paths[0]
            _progress(
                progress_callback,
                10.0,
                "Convertint FITS Planck a cache runtime...",
            )
            summary = convert_planck_fits_to_cache(
                fits_path=str(src),
                output_npz=str(out_dir / "planck_dust_opacity_eq_u16.npz"),
                output_zst=str(out_dir / "planck_dust_opacity_eq_u16.npy.zst"),
                write_zst=True,
                workers=max(1, (os.cpu_count() or 2) - 1),
            )
            _progress(progress_callback, 100.0, "Planck convertit.")
            dust_path = out_dir / "planck_dust_opacity_eq_u16.npz"
            set_config_value("dust_map_path", str(dust_path))
            self._mark_asset_state("planck_dust", True, str(dust_path))
            return {"ok": True, "summary": summary, "stored_in": str(out_dir)}

        layer_type = self._asset_layer_type(asset_id)
        if layer_type is not None:
            parent_key = {
                LayerType.ELEVATION: "data_elevation",
                LayerType.SURFACE_RGB: "data_surface",
                LayerType.SURFACE_CATEGORICAL: "data_surface",
                LayerType.LIGHT_POLLUTION: "data_light_pollution",
            }[layer_type]
            fallback_parent = (
                Path(self.layout["root"])
                / "data"
                / "earth"
                / (
                    "surface"
                    if layer_type in {LayerType.SURFACE_RGB, LayerType.SURFACE_CATEGORICAL}
                    else layer_type.value.replace("_", "-")
                )
            )
            parent = Path(self.layout.get(parent_key, fallback_parent))
            display_name = str(opts.get("display_name") or paths[0].stem)
            dataset_dir = self._unique_dataset_dir(
                parent,
                display_name,
                layer_type.value,
            )
            copied = 0
            try:
                total = max(1, len(paths))
                for idx, src in enumerate(paths):
                    if not src.exists():
                        raise FileNotFoundError(str(src))
                    _progress(
                        progress_callback,
                        5.0 + 75.0 * idx / total,
                        f"Important {src.name}...",
                    )
                    if src.is_dir():
                        for child in src.rglob("*"):
                            if child.is_file():
                                destination = dataset_dir / child.relative_to(src)
                                self._copy_file(child, destination)
                    elif src.suffix.lower() == ".zip":
                        self._safe_extract_zip(src, dataset_dir)
                    elif src.suffix.lower() == ".7z":
                        self._safe_extract_7z(src, dataset_dir)
                    elif src.suffix.lower() in {".tif", ".tiff"}:
                        self._copy_raster_with_sidecars(src, dataset_dir / src.name)
                    else:
                        self._copy_file(src, dataset_dir / src.name)
                    copied += 1

                raster_candidates = sorted(
                    (
                        child
                        for child in dataset_dir.rglob("*")
                        if child.is_file()
                        and child.suffix.lower()
                        in {".tif", ".tiff", ".asc", ".txt", ".npy", ".vrt", ".img", ".jp2"}
                    ),
                    key=lambda child: child.name.lower(),
                )
                registered_path: Path = dataset_dir
                if layer_type is LayerType.LIGHT_POLLUTION:
                    tiffs = [
                        child
                        for child in raster_candidates
                        if child.suffix.lower() in {".tif", ".tiff"}
                    ]
                    if not tiffs:
                        raise ValueError("L'arxiu no conté cap GeoTIFF DVNL.")
                    registered_path = tiffs[-1]
                source = self._register_data_source(
                    registered_path,
                    layer_type,
                    display_name=display_name,
                    priority=int(opts.get("priority", 0) or 0),
                    provenance="managed",
                    attribution=self.get_spec(asset_id).credits,
                    metadata={"managed": True},
                )

                asc_caches = []
                observer_auto = {"applied": False}
                if layer_type is LayerType.ELEVATION:
                    _progress(progress_callback, 88.0, "Materialitzant caches ASC...")
                    asc_caches = materialize_asc_caches_spawned(
                        dataset_dir,
                        max_workers=4,
                        progress_callback=(
                            lambda percent, message: _progress(
                                progress_callback,
                                88.0 + float(percent) * 0.06,
                                message,
                            )
                        ),
                    )
                    observer_auto = self._auto_configure_observer_from_dem(dataset_dir)
                self._mark_asset_state(asset_id, True, "")
                _progress(progress_callback, 100.0, "Font registrada a la biblioteca.")
                return {
                    "ok": True,
                    "stored_in": str(
                        registered_path
                        if layer_type is LayerType.LIGHT_POLLUTION
                        else dataset_dir
                    ),
                    "source_id": str(source.id),
                    "files_processed": int(copied),
                    "asc_caches": int(len(asc_caches)),
                    "observer_auto": observer_auto,
                }
            except Exception:
                self._mark_asset_state(asset_id, False, "")
                raise

        if asset_id == "ngc_catalog":
            out_dir = Path(self.layout["data_ngc"])
            dst = out_dir / "openngc_catalog.csv"
            self._copy_file(paths[0], dst)
            _progress(progress_callback, 100.0, "Cataleg NGC importat.")
            set_config_value("ngc_catalog_path", str(dst))
            self._mark_asset_state("ngc_catalog", True, str(dst))
            self.library.update_asset(
                "ngc_catalog",
                managed=True,
                source_path=str(paths[0].resolve(strict=False)),
                checksum_sha256=self._sha256(dst),
                size_bytes=int(dst.stat().st_size),
                attribution="OpenNGC by Mattia Verga and contributors",
                license="CC-BY-SA-4.0",
            )
            return {"ok": True, "stored_in": str(dst)}

        if asset_id == "solar_system_ephemeris":
            src = paths[0]
            if src.suffix.lower() != ".bsp":
                raise ValueError("L'efemèride del sistema solar ha de ser un fitxer .bsp.")
            out_dir = Path(self.layout["data_ephemeris"])
            dst = out_dir / "de421.bsp"
            self._copy_file(src, dst)
            _progress(progress_callback, 100.0, "Efemèride DE421 importada.")
            self._mark_asset_state(asset_id, True, str(dst))
            return {"ok": True, "stored_in": str(dst)}

        raise KeyError(f"Unknown asset id: {asset_id}")
