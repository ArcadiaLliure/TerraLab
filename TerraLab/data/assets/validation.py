"""Validation and metadata inspection for managed assets."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Tuple

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.data.assets.registry import AssetRegistryMixin
from TerraLab.data.assets.runtime import (
    AssetRemovalPreview,
    AssetRemovalReport,
    set_asset_config,
)
from TerraLab.data.source_catalog import LayerType
from TerraLab.data.source_inspection import inspect_data_source


class AssetValidationMixin:
    @staticmethod
    def _path_size(path: Path) -> int:
        try:
            if path.is_symlink() or path.is_file():
                return int(path.stat().st_size)
        except OSError:
            return 0
        total = 0
        try:
            for folder, _directories, files in os.walk(path, followlinks=False):
                for name in files:
                    try:
                        total += int(
                            (Path(folder) / name).stat(follow_symlinks=False).st_size
                        )
                    except OSError:
                        continue
        except OSError:
            return total
        return total

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        try:
            a = first.resolve(strict=False)
            b = second.resolve(strict=False)
            return a == b or a.is_relative_to(b) or b.is_relative_to(a)
        except OSError:
            return False

    def _geospatial_sources_for_asset(self, asset_id: str):
        layer_type = self._asset_layer_type(asset_id)
        if layer_type is None:
            return []
        return [
            source
            for source in self.data_sources.list_sources(layer_type)
            if not bool(source.metadata.get("bundled", False))
            and str(source.provenance or "").strip().lower() != "bundled"
        ]

    def _managed_source_target(
        self,
        asset_id: str,
        source,
        *,
        remaining_sources,
    ) -> Path | None:
        managed = bool(source.metadata.get("managed", False)) or (
            str(source.provenance or "").strip().lower() == "managed"
        )
        if not managed:
            return None
        candidate = Path(source.path).resolve(strict=False)
        if not self.library.contains(candidate):
            return None
        data_root = self._asset_data_root(asset_id)
        if data_root is None:
            return None
        try:
            candidate.relative_to(data_root)
        except ValueError:
            return candidate if candidate.is_file() else None
        if candidate == data_root:
            return None

        target = candidate
        if (
            candidate.is_file()
            and candidate.parent != data_root
            and candidate.parent.parent == data_root
        ):
            # Managed single-file rasters are stored with sidecars in their
            # own dataset directory.
            target = candidate.parent
        if any(
            self._paths_overlap(target, Path(other.path))
            for other in remaining_sources
        ):
            return None
        return target

    @staticmethod
    def _deduplicate_parent_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
        unique: dict[str, Path] = {}
        for path in paths:
            resolved = path.resolve(strict=False)
            unique.setdefault(AssetRegistryMixin._path_key(resolved), resolved)
        ordered = sorted(unique.values(), key=lambda item: (len(item.parts), str(item)))
        selected: list[Path] = []
        for candidate in ordered:
            if any(
                candidate == parent or candidate.is_relative_to(parent)
                for parent in selected
            ):
                continue
            selected.append(candidate)
        return tuple(selected)

    def removal_preview(
        self,
        asset_id: str,
        *,
        include_size: bool = False,
    ) -> AssetRemovalPreview:
        """Describe removable library data without mutating disk or catalogues."""

        normalized_id = str(asset_id or "").strip()
        self.get_spec(normalized_id)
        state = self.library.asset_state(normalized_id)
        sources = self._geospatial_sources_for_asset(normalized_id)
        source_ids = tuple(sorted(str(source.id) for source in sources))
        source_id_set = set(source_ids)
        remaining_sources = (
            [
                source
                for source in self.data_sources.list_sources()
                if str(source.id) not in source_id_set
            ]
            if sources
            else []
        )

        managed_paths: list[Path] = []
        external_paths: list[Path] = []
        retained_paths: list[Path] = []
        for source in sources:
            target = self._managed_source_target(
                normalized_id,
                source,
                remaining_sources=remaining_sources,
            )
            managed = bool(source.metadata.get("managed", False)) or (
                str(source.provenance or "").strip().lower() == "managed"
            )
            if target is not None:
                if self._path_has_content(target):
                    managed_paths.append(target)
            elif managed and self.library.contains(source.path):
                retained_paths.append(Path(source.path).resolve(strict=False))
            else:
                external_paths.append(Path(source.path).resolve(strict=False))

        data_root = self._asset_data_root(normalized_id)
        fixed_root_assets = {
            "gaia_catalog",
            "ngc_catalog",
            "milkyway_texture",
            "planck_dust",
            "solar_system_ephemeris",
            "climate_metno",
        }
        if (
            normalized_id in fixed_root_assets
            and data_root is not None
            and self._path_has_content(data_root)
        ):
            managed_paths.append(data_root)

        for field in ("path", "source_path"):
            raw = str(state.get(field, "") or "").strip()
            if not raw:
                continue
            candidate = Path(raw).expanduser().resolve(strict=False)
            if any(self._paths_overlap(candidate, item) for item in managed_paths):
                continue
            # ``source_path`` records provenance and always remains
            # user-owned, even when the imported derivative is managed.
            is_managed = (
                field == "path"
                and bool(state.get("managed", False))
                and self.library.contains(candidate)
            )
            if is_managed and candidate.is_file():
                managed_paths.append(candidate)
            else:
                external_paths.append(candidate)

        download_target = self._download_target(normalized_id)
        if download_target is not None:
            partial_path, partial_metadata = self.downloader.paths_for(download_target)
            for candidate in (download_target, partial_path, partial_metadata):
                resolved = candidate.resolve(strict=False)
                if self.library.contains(resolved) and self._path_has_content(resolved):
                    managed_paths.append(resolved)
        if normalized_id == "orthophoto":
            # Dynamic Copernicus exports do not have one static
            # ``auto_download_url``, so ``_download_target`` cannot discover
            # their resumable fragment manifests.  They are nevertheless
            # TerraLab-owned data and must be included in an explicit
            # "Eliminar de la biblioteca" operation.
            copernicus_download_root = (
                Path(self.layout["downloads"]) / "copernicus-hrim-2018"
            ).resolve(strict=False)
            if (
                self.library.contains(copernicus_download_root)
                and self._path_has_content(copernicus_download_root)
            ):
                managed_paths.append(copernicus_download_root)

        managed = self._deduplicate_parent_paths(managed_paths)
        external = self._deduplicate_parent_paths(external_paths)
        retained = self._deduplicate_parent_paths(retained_paths)
        total_bytes = (
            sum(self._path_size(path) for path in managed) if include_size else 0
        )
        return AssetRemovalPreview(
            asset_id=normalized_id,
            managed_paths=tuple(str(path) for path in managed),
            external_paths=tuple(str(path) for path in external),
            retained_paths=tuple(str(path) for path in retained),
            source_ids=source_ids,
            total_bytes=int(total_bytes),
            manifest_registered=bool(state),
        )

    @staticmethod
    def _remove_path(path: Path, *, keep_directory: bool = False) -> None:
        def remove_readonly(function, raw_path, _error_info):
            os.chmod(raw_path, 0o700)
            function(raw_path)

        if keep_directory and path.is_dir() and not path.is_symlink():
            for child in tuple(path.iterdir()):
                AssetValidationMixin._remove_path(child)
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, onerror=remove_readonly)
            return
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            os.chmod(path, 0o600)
            path.unlink(missing_ok=True)

    def remove_asset_data(self, asset_id: str) -> AssetRemovalReport:
        """Delete TerraLab-owned data and unlink user-owned sources."""

        preview = self.removal_preview(asset_id, include_size=True)
        data_root = self._asset_data_root(preview.asset_id)
        fixed_root_assets = {
            "gaia_catalog",
            "ngc_catalog",
            "milkyway_texture",
            "planck_dust",
            "solar_system_ephemeris",
            "climate_metno",
        }
        deleted: list[str] = []
        for raw in preview.managed_paths:
            path = Path(raw).resolve(strict=False)
            if not self.library.contains(path) or path == self.library.root:
                raise ValueError(f"Refusing to remove unsafe library path: {path}")
            keep_directory = (
                preview.asset_id in fixed_root_assets
                and data_root is not None
                and path == data_root
            )
            self._remove_path(path, keep_directory=keep_directory)
            deleted.append(str(path))

        removed_source_ids: list[str] = []
        for source_id in preview.source_ids:
            if self.data_sources.remove(source_id) is not None:
                removed_source_ids.append(source_id)

        self.library.remove_asset(preview.asset_id)
        now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        set_asset_config(f"assets.{preview.asset_id}.ready", False)
        set_asset_config(f"assets.{preview.asset_id}.updated_utc", now_utc)
        path_preferences = {
            "gaia_catalog": "gaia_catalog_path",
            "ngc_catalog": "ngc_catalog_path",
            "milkyway_texture": "milkyway_overlay_texture_path",
            "planck_dust": "dust_map_path",
        }
        preference = path_preferences.get(preview.asset_id)
        if preference:
            set_asset_config(preference, "")

        return AssetRemovalReport(
            asset_id=preview.asset_id,
            deleted_paths=tuple(deleted),
            detached_paths=preview.external_paths,
            retained_paths=preview.retained_paths,
            removed_source_ids=tuple(removed_source_ids),
            released_bytes=preview.total_bytes,
        )

    def resolve_ephemeris_path(self) -> Path | None:
        configured = self.library.asset_state("solar_system_ephemeris")
        raw = str(configured.get("path", "") or "").strip()
        candidates = []
        if raw:
            candidates.append(Path(raw))
        candidates.append(Path(self.layout["data_ephemeris"]) / "de421.bsp")
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
        AssetValidationMixin._copy_file(src, dst)

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
                log_suppressed_exception(__name__, "AssetValidationMixin._copy_raster_with_sidecars")

        # World/projection files tied to basename (e.g. .tfw/.wld/.prj).
        for ext in (".tfw", ".wld", ".prj"):
            sidecar = src.with_suffix(ext)
            if not sidecar.exists() or not sidecar.is_file():
                continue
            target = dst.with_suffix(ext)
            try:
                shutil.copyfile(sidecar, target)
            except Exception:
                log_suppressed_exception(__name__, "AssetValidationMixin._copy_raster_with_sidecars")

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
            from TerraLab.data.crs import (
                CRS_GEOGRAPHIC,
                DEFAULT_TRANSFORM_SERVICE,
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
            lon, lat = DEFAULT_TRANSFORM_SERVICE.transform_xy(
                center_x,
                center_y,
                src_crs,
                CRS_GEOGRAPHIC,
            )
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
            from TerraLab.data.dem_tiles import TileIndex
            from TerraLab.data.crs import (
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
            log_suppressed_exception(__name__, "AssetValidationMixin._estimate_observer_from_dem")

        if source.is_file() and source.suffix.lower() in {".tif", ".tiff"}:
            return self._estimate_observer_from_tiff_dem(source)
        return self._estimate_observer_from_asc_dem(source)

