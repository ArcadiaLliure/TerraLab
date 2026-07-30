"""Discovery and registration of local or external datasets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Dict, Optional

from TerraLab.data.assets.runtime import (
    AssetOperationCancelled,
    ProgressFn,
    S2GLC_PRODUCT,
    _progress,
    set_asset_config,
)
from TerraLab.data.resumable_download import (
    DownloadCancelled,
    DownloadProgress,
    human_bytes,
    progress_message,
)
from TerraLab.data.source_catalog import LayerType, SourceHealthStatus
from TerraLab.data.source_inspection import inspect_data_source


class AssetDiscoveryMixin:
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

        set_asset_config("observer_lat", lat)
        set_asset_config("observer_lon", lon)
        tz_name = self._resolve_timezone_name(lat, lon)
        if tz_name:
            set_asset_config("observer_timezone", tz_name)
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

    @classmethod
    def _unique_dataset_path(cls, parent: Path, name: str, fallback: str) -> Path:
        base = cls._dataset_slug(name, fallback)
        candidate = parent / base
        suffix = 2
        while candidate.exists():
            candidate = parent / f"{base}_{suffix}"
            suffix += 1
        return candidate

    def _geospatial_metadata(
        self,
        asset_id: str,
        *,
        managed: bool,
    ) -> dict[str, object]:
        spec = self.get_spec(asset_id)
        # ``orthophoto`` also accepts arbitrary user rasters.  Product
        # metadata is attached only by the Copernicus download branch so a
        # linked/copied local TIFF is never falsely attributed to Copernicus.
        generic_orthophoto = asset_id == "orthophoto"
        metadata: dict[str, object] = {
            "managed": bool(managed),
            "asset_id": str(asset_id),
            "semantic_type": str(spec.semantic_type or ""),
            "provider": "" if generic_orthophoto else str(spec.provider or ""),
            "official_url": (
                "" if generic_orthophoto else str(spec.source_url or "")
            ),
            "nominal_resolution_m": (
                None if generic_orthophoto else spec.nominal_resolution_m
            ),
            "nominal_crs": (
                "" if generic_orthophoto else str(spec.nominal_crs or "")
            ),
            "geographic_extent_description": (
                ""
                if generic_orthophoto
                else str(spec.geographic_extent or "")
            ),
            "citation": (
                "" if generic_orthophoto else str(spec.citation or "")
            ),
            "license_note": (
                "" if generic_orthophoto else str(spec.license_note or "")
            ),
        }
        if asset_id in {"surface_rgb", "surface_categorical"}:
            metadata.update(
                {
                    "product_id": "s2glc-europe-2017-v1.2",
                    "product_name": S2GLC_PRODUCT,
                    "reference_year": 2017,
                }
            )
        if asset_id == "surface_rgb":
            metadata["legend_id"] = "s2glc_europe_2017"
            metadata["encoding"] = "rgb_palette"
        if asset_id == "surface_categorical":
            metadata["legend_id"] = "s2glc_europe_2017"
        return metadata

    @staticmethod
    def _validate_and_extract_zip(
        archive: Path,
        destination: Path,
        *,
        progress_callback: Optional[ProgressFn] = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """CRC-validate first, then stream a path-safe extraction."""

        destination_root = destination.resolve(strict=False)
        _progress(progress_callback, 73.0, "Verificant l'estructura i els CRC del ZIP…")
        try:
            with zipfile.ZipFile(archive, "r") as probe:
                probe.infolist()
        except zipfile.BadZipFile as exc:
            raise ValueError(f"ZIP corrupte: {exc}") from exc
        with zipfile.ZipFile(archive, "r") as handle:
            members = handle.infolist()
            if not members:
                raise ValueError("El ZIP és buit.")
            for member in members:
                target = (destination / member.filename).resolve(strict=False)
                try:
                    target.relative_to(destination_root)
                except ValueError as exc:
                    raise ValueError(
                        f"Ruta no segura dins del ZIP: {member.filename}"
                    ) from exc
            verify_total = max(
                1, sum(max(0, int(item.file_size)) for item in members)
            )
            verified = 0
            try:
                for member in members:
                    if member.is_dir():
                        continue
                    with handle.open(member, "r") as source:
                        while True:
                            if cancelled is not None and cancelled():
                                raise AssetOperationCancelled(
                                    "Verificació pausada; el ZIP es conserva."
                                )
                            chunk = source.read(4 * 1024 * 1024)
                            if not chunk:
                                break
                            verified += len(chunk)
                            _progress(
                                progress_callback,
                                73.0 + 6.0 * min(1.0, verified / verify_total),
                                "Verificant ZIP: "
                                f"{human_bytes(verified)} / {human_bytes(verify_total)}",
                            )
            except zipfile.BadZipFile as exc:
                raise ValueError(f"ZIP corrupte: {exc}") from exc
        if cancelled is not None and cancelled():
            raise AssetOperationCancelled(
                "Extracció pausada després de verificar; el ZIP es conserva."
            )

        _progress(progress_callback, 80.0, "Extraient el GeoTIFF en segon pla…")
        with zipfile.ZipFile(archive, "r") as handle:
            members = handle.infolist()
            total = max(1, sum(max(0, int(item.file_size)) for item in members))
            extracted = 0
            for member in members:
                if cancelled is not None and cancelled():
                    raise AssetOperationCancelled(
                        "Extracció pausada; el ZIP complet es conserva per reprendre."
                    )
                target = destination / member.filename
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.open(member, "r") as source, target.open("wb") as output:
                    while True:
                        if cancelled is not None and cancelled():
                            raise AssetOperationCancelled(
                                "Extracció pausada; el ZIP complet es conserva per reprendre."
                            )
                        chunk = source.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        extracted += len(chunk)
                        _progress(
                            progress_callback,
                            80.0 + 13.0 * min(1.0, extracted / total),
                            "Extraient: "
                            f"{human_bytes(extracted)} / {human_bytes(total)}",
                        )
                    output.flush()
                    os.fsync(output.fileno())

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
        user_orthophoto = asset_id == "orthophoto"
        source = self._register_data_source(
            candidate,
            layer_type,
            display_name=display_name or candidate.stem or candidate.name,
            priority=int(priority),
            provenance="external",
            metadata=self._geospatial_metadata(asset_id, managed=False),
            attribution=(
                "Font d'ortofoto aportada per l'usuari."
                if user_orthophoto
                else self.get_spec(asset_id).credits
            ),
            license=(
                "" if user_orthophoto else self.get_spec(asset_id).license_note
            ),
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
            set_asset_config("ngc_catalog_path", str(candidate))
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
            set_asset_config("milkyway_overlay_texture_path", str(candidate))
            self._mark_asset_state(asset_id, True, str(candidate))
            return {"ok": True, "linked": True, "stored_in": str(candidate)}
        if asset_id == "planck_dust" and suffix in {".npz", ".zst", ".npy"}:
            set_asset_config("dust_map_path", str(candidate))
            self._mark_asset_state(asset_id, True, str(candidate))
            return {"ok": True, "linked": True, "stored_in": str(candidate)}
        if asset_id == "gaia_catalog" and suffix in {".npy", ".npz", ".zst"}:
            set_asset_config("gaia_catalog_path", str(candidate))
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
        extracted_size: int = 0,
    ) -> Path:
        def report(event: DownloadProgress) -> None:
            if event.phase == "downloading":
                percent = (
                    2.0 + 68.0 * event.downloaded_bytes / event.total_bytes
                    if event.total_bytes > 0
                    else -1.0
                )
            else:
                percent = 1.0 if event.phase == "connecting" else -1.0
            _progress(progress_callback, percent, progress_message(event))

        try:
            result = self.downloader.download(
                url,
                target_path,
                expected_size=int(expected_size or 0),
                extracted_size=int(extracted_size or 0),
                progress=report,
                cancelled=cancelled,
                safety_margin_bytes=(
                    None if int(extracted_size or 0) > 0 else 0
                ),
            )
        except DownloadCancelled as exc:
            raise AssetOperationCancelled(str(exc)) from exc
        if expected_md5:
            digest = hashlib.md5()  # noqa: S324 - provider publishes MD5 metadata.
            with result.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != str(expected_md5).lower():
                raise IOError("La suma MD5 de la descàrrega no coincideix.")
        _progress(progress_callback, 72.0, "Descàrrega completada; verificant arxiu.")
        return result

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

