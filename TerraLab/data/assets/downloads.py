"""Network download orchestration for supported datasets."""

from __future__ import annotations

import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from TerraLab.data.assets.runtime import (
    AssetOperationCancelled,
    ProgressFn,
    _json_payload,
    _progress,
    copernicus_manager_class,
)
from TerraLab.data.copernicus import (
    ADAPTED_ATTRIBUTION,
    ATTRIBUTION,
    DATA_POLICY_URL,
    DownloadRequest,
    IMAGE_SERVER_URL,
    PRODUCT_NAME as COPERNICUS_ORTHOPHOTO_PRODUCT,
    PRODUCT_URL as COPERNICUS_ORTHOPHOTO_PRODUCT_URL,
    SERVICE_COVERAGE_WGS84,
    WMS_URL,
)
from TerraLab.data.source_catalog import LayerRole, LayerType, SelectionMode


class AssetDownloadMixin:
    def _download_and_prepare_copernicus_orthophoto(
        self,
        *,
        progress_callback: Optional[ProgressFn],
        options: Optional[Dict[str, object]],
        cancelled: Callable[[], bool] | None,
    ) -> Dict[str, object]:
        """Download, validate and register one selected Copernicus mosaic."""

        opts = dict(options or {})
        raw_request = opts.get("copernicus_request")
        if not isinstance(raw_request, Mapping):
            raise ValueError(
                "La descàrrega d'ortofoto necessita una selecció geogràfica "
                "Copernicus vàlida."
            )
        request = DownloadRequest.from_dict(dict(raw_request))
        request_payload = _json_payload(request)
        if not isinstance(request_payload, dict):
            raise ValueError("La petició Copernicus no es pot serialitzar.")

        download_root = (
            Path(self.layout["downloads"]) / "copernicus-hrim-2018"
        )
        dataset_root = Path(self.layout["data_surface"])
        diagnostic_log_path = (
            Path(self.layout["logs"]) / "copernicus_orthophoto_last.log"
        )
        download_root.mkdir(parents=True, exist_ok=True)
        dataset_root.mkdir(parents=True, exist_ok=True)
        self._mark_install_state(
            "orthophoto",
            "downloading",
            copernicus_request=request_payload,
            product_url=COPERNICUS_ORTHOPHOTO_PRODUCT_URL,
            service_url=IMAGE_SERVER_URL,
            download_root=str(download_root),
        )

        try:
            manager = copernicus_manager_class()(
                download_root,
                dataset_root,
            )
            configure_diagnostics = getattr(
                manager, "set_diagnostic_log_path", None
            )
            if callable(configure_diagnostics):
                configure_diagnostics(diagnostic_log_path)
            result = manager.run(
                request,
                progress_callback=progress_callback,
                cancelled=cancelled,
            )
            if cancelled is not None and cancelled():
                raise AssetOperationCancelled(
                    "Descàrrega d'ortofoto pausada; el manifest i els "
                    "fragments vàlids es conserven."
                )

            output_path = Path(result.output_path).expanduser().resolve(
                strict=False
            )
            manifest_path = Path(result.manifest_path).expanduser().resolve(
                strict=False
            )
            if not output_path.is_file():
                raise FileNotFoundError(
                    "El gestor Copernicus no ha publicat el GeoTIFF final: "
                    f"{output_path}"
                )

            result_request = _json_payload(result.request)
            if not isinstance(result_request, dict):
                result_request = request_payload
            estimate_payload = _json_payload(result.estimate)
            if not isinstance(estimate_payload, dict):
                estimate_payload = {}
            result_metadata = _json_payload(
                getattr(result, "metadata", {})
            )
            if not isinstance(result_metadata, dict):
                result_metadata = {}
            adaptations = result_metadata.get("adaptations", {})
            if not isinstance(adaptations, Mapping):
                adaptations = {}

            resolution = result_request.get(
                "resolution_m",
                request_payload.get("resolution_m", 10.0),
            )
            pixel_type = str(
                result_request.get(
                    "pixel_type",
                    request_payload.get("pixel_type", ""),
                )
                or ""
            ).upper()
            output_format = str(
                result_request.get(
                    "output_format",
                    result_request.get(
                        "format",
                        request_payload.get(
                            "output_format",
                            request_payload.get("format", "GeoTIFF"),
                        ),
                    ),
                )
                or "GeoTIFF"
            )
            compression = str(
                result_request.get(
                    "compression",
                    request_payload.get("compression", ""),
                )
                or ""
            )
            downloaded_utc = (
                datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            metadata = self._geospatial_metadata(
                "orthophoto",
                managed=True,
            )
            metadata.update(
                {
                    "product_id": str(
                        result_metadata.get(
                            "product_id",
                            "copernicus-hrim-2018-true-colour",
                        )
                    ),
                    "product_name": str(
                        result_metadata.get(
                            "product_name",
                            COPERNICUS_ORTHOPHOTO_PRODUCT,
                        )
                    ),
                    "reference_year": 2018,
                    "provider": "Copernicus Land Monitoring Service",
                    "official_url": str(
                        result_metadata.get(
                            "product_url",
                            COPERNICUS_ORTHOPHOTO_PRODUCT_URL,
                        )
                    ),
                    "product_url": str(
                        result_metadata.get(
                            "product_url",
                            COPERNICUS_ORTHOPHOTO_PRODUCT_URL,
                        )
                    ),
                    "service_url": str(
                        result_metadata.get(
                            "service_url", IMAGE_SERVER_URL
                        )
                    ),
                    "wms_url": str(
                        result_metadata.get("wms_url", WMS_URL)
                    ),
                    "data_policy_url": str(
                        result_metadata.get(
                            "data_policy_url", DATA_POLICY_URL
                        )
                    ),
                    "service_coverage_wgs84": _json_payload(
                        SERVICE_COVERAGE_WGS84
                    ),
                    "citation": COPERNICUS_ORTHOPHOTO_PRODUCT,
                    "license_note": (
                        f"{ATTRIBUTION} Política de dades: "
                        f"{DATA_POLICY_URL}"
                    ),
                    "source_product": COPERNICUS_ORTHOPHOTO_PRODUCT,
                    "download_service": "ArcGIS ImageServer exportImage",
                    "download_manifest_path": str(manifest_path),
                    "downloaded_utc": str(
                        result_metadata.get(
                            "downloaded_utc", downloaded_utc
                        )
                    ),
                    "download_request": result_request,
                    "download_extent_wgs84": result_request.get(
                        "bbox_wgs84",
                        result_request.get("bbox"),
                    ),
                    "selection_estimate": estimate_payload,
                    "download_result_metadata": result_metadata,
                    "selected_resolution_m": result_metadata.get(
                        "resolution_m", resolution
                    ),
                    "output_format": str(
                        result_metadata.get("format", output_format)
                    ),
                    "pixel_type": str(
                        result_metadata.get(
                            "pixel_type", pixel_type
                        )
                    ).upper(),
                    "compression": str(
                        result_metadata.get(
                            "compression", compression
                        )
                    ),
                    "output_crs": "EPSG:3035",
                    "source_product_crs": "EPSG:3035",
                    "service_published_crs": "EPSG:3857",
                    "reprojected": bool(
                        adaptations.get("reprojected", True)
                    ),
                    "cropped": bool(
                        adaptations.get(
                            "clipped",
                            adaptations.get("cropped", True),
                        )
                    ),
                    "converted": bool(
                        adaptations.get(
                            "converted",
                            pixel_type == "U8"
                            or compression.upper()
                            not in {"", "NONE", "UNCOMPRESSED"},
                        )
                    ),
                    "adapted_by_terralab": True,
                    "attribution": ADAPTED_ATTRIBUTION,
                    "no_official_endorsement": True,
                }
            )
            self._mark_install_state(
                "orthophoto",
                "registering",
                path=str(output_path),
                download_manifest_path=str(manifest_path),
                copernicus_request=result_request,
                selection_estimate=estimate_payload,
            )
            display_name = str(
                opts.get("display_name")
                or (
                    f"Copernicus HRIM 2018 True Colour · "
                    f"{float(resolution):g} m"
                )
            )
            source = self._register_data_source(
                output_path,
                LayerType.ORTHOPHOTO_RGB,
                display_name=display_name,
                priority=int(opts.get("priority", 0) or 0),
                provenance="managed",
                attribution=ADAPTED_ATTRIBUTION,
                license=(
                    "Copernicus Land Monitoring Service data policy: "
                    f"{DATA_POLICY_URL}. Data adaptada per TerraLab; "
                    "sense aval oficial de la Unió Europea, Copernicus "
                    "ni l'European Environment Agency."
                ),
                metadata=metadata,
            )
            # The orthophoto preference is independent from the preferred
            # land-cover encoding and from the Ortofoto/Categòric switch.
            self.data_sources.set_selection(
                LayerRole.ORTHOPHOTO,
                source.id,
                mode=SelectionMode.MANUAL,
            )
            self._mark_asset_state(
                "orthophoto",
                True,
                str(output_path),
            )
            self._mark_install_state(
                "orthophoto",
                "prepared",
                path=str(output_path),
                source_id=str(source.id),
                download_manifest_path=str(manifest_path),
                copernicus_request=result_request,
                selection_estimate=estimate_payload,
            )
            _progress(
                progress_callback,
                100.0,
                "Completat: ortofoto Copernicus registrada.",
            )
            return {
                "ok": True,
                "stored_in": str(output_path),
                "source_id": str(source.id),
                "manifest_path": str(manifest_path),
                "request": result_request,
                "estimate": estimate_payload,
                "downloaded_fragments": int(
                    getattr(result, "downloaded_fragments", 0) or 0
                ),
                "reused_fragments": int(
                    getattr(result, "reused_fragments", 0) or 0
                ),
            }
        except AssetOperationCancelled:
            self._mark_install_state(
                "orthophoto",
                "paused",
                copernicus_request=request_payload,
            )
            raise
        except Exception as exc:
            try:
                diagnostic_log_path.parent.mkdir(
                    parents=True, exist_ok=True
                )
                with diagnostic_log_path.open(
                    "a", encoding="utf-8", newline="\n"
                ) as handle:
                    handle.write(
                        "\nTerraLab asset integration failure\n"
                        f"{traceback.format_exc()}\n"
                    )
            except OSError:
                pass
            if cancelled is not None and cancelled():
                self._mark_install_state(
                    "orthophoto",
                    "paused",
                    copernicus_request=request_payload,
                )
                raise AssetOperationCancelled(
                    "Descàrrega d'ortofoto pausada; el manifest i els "
                    "fragments vàlids es conserven."
                ) from exc
            self._mark_install_state(
                "orthophoto",
                "error",
                error=str(exc),
                diagnostic_log_path=str(diagnostic_log_path),
                copernicus_request=request_payload,
            )
            raise RuntimeError(
                f"{exc}\n\nLog de diagnòstic: {diagnostic_log_path}"
            ) from exc

    def download_and_prepare(
        self,
        asset_id: str,
        progress_callback: Optional[ProgressFn] = None,
        options: Optional[Dict[str, object]] = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Dict[str, object]:
        spec = self.get_spec(asset_id)
        if asset_id == "orthophoto":
            return self._download_and_prepare_copernicus_orthophoto(
                progress_callback=progress_callback,
                options=options,
                cancelled=cancelled,
            )
        if not spec.auto_download_url:
            raise ValueError(
                f"Asset {asset_id} does not support automatic download."
            )
        download_url = spec.auto_download_url
        expected_size = int(spec.expected_download_bytes or 0) or None
        extracted_size = int(spec.expected_extracted_bytes or 0)
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
        self._mark_install_state(asset_id, "downloading")
        try:
            reuse_archive = bool(
                expected_size
                and tmp_path.is_file()
                and int(tmp_path.stat().st_size) == int(expected_size)
            )
            if reuse_archive:
                self.downloader.ensure_space(
                    archive_size=int(expected_size or 0),
                    extracted_size=extracted_size,
                    downloaded_bytes=int(expected_size or 0),
                )
                downloaded = tmp_path
                _progress(
                    progress_callback,
                    72.0,
                    "Verificant l'arxiu descarregat abans de reprendre...",
                )
            else:
                downloaded = self._download_file(
                    download_url,
                    tmp_path,
                    progress_callback=progress_callback,
                    expected_size=expected_size,
                    expected_md5=expected_md5,
                    cancelled=cancelled,
                    extracted_size=extracted_size,
                )
            if cancelled is not None and cancelled():
                raise AssetOperationCancelled("Preparació pausada.")
            result = self.import_files(
                asset_id,
                [str(downloaded)],
                progress_callback=progress_callback,
                options=options,
                cancelled=cancelled,
            )
            if bool(dict(options or {}).get("remove_archive", False)):
                downloaded.unlink(missing_ok=True)
                result["archive_removed"] = True
            self._mark_install_state(asset_id, "prepared")
            return result
        except AssetOperationCancelled:
            self._mark_install_state(asset_id, "paused")
            raise
        except Exception as exc:
            self._mark_install_state(asset_id, "error", error=str(exc))
            raise

