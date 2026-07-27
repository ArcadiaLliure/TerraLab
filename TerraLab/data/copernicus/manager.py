"""Application service coordinating Copernicus preflight, resume, and mosaic."""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .client import ArcGISImageServerClient
from .constants import (
    ADAPTED_ATTRIBUTION,
    DATA_POLICY_URL,
    DOWNLOAD_PIPELINE_VERSION,
    IMAGE_SERVER_URL,
    PRODUCT_NAME,
    PRODUCT_URL,
    SERVICE_PIXEL_TYPE,
    WMS_URL,
)
from .manifest import DownloadManifest, _plan_identity, _utc_now
from .mosaic import _overview_levels, build_mosaic
from .planning import (
    CoverageAssessment,
    DownloadPlan,
    DownloadRequest,
    Fragment,
    SelectionEstimate,
    assess_service_coverage,
    format_bytes_dual,
    plan_fragments,
)
from .validation import (
    CopernicusDownloadCancelled,
    CopernicusDownloadError,
    InsufficientDownloadSpaceError,
    RasterValidation,
    _cancelled,
    read_mosaic_radiometric_metadata,
    validate_final_geotiff,
)
from TerraLab.common.exception_reporting import log_suppressed_exception

@dataclass(frozen=True)
class DownloadResult:
    output_path: str
    manifest_path: str
    request: DownloadRequest
    estimate: SelectionEstimate
    downloaded_fragments: int
    reused_fragments: int
    metadata: dict[str, object]


class CopernicusOrthophotoManager:
    """Coordinate preflight, fragment resume, mosaic and final validation."""

    def __init__(
        self,
        download_root: str | os.PathLike[str],
        dataset_root: str | os.PathLike[str],
        *,
        client: ArcGISImageServerClient | None = None,
        disk_usage: Callable[[str | os.PathLike[str]], object] = shutil.disk_usage,
        diagnostic_log_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.download_root = Path(download_root).resolve(strict=False)
        self.dataset_root = Path(dataset_root).resolve(strict=False)
        self.download_root.mkdir(parents=True, exist_ok=True)
        self.dataset_root.mkdir(parents=True, exist_ok=True)
        self.client = client or ArcGISImageServerClient()
        self.disk_usage = disk_usage
        self.diagnostic_log_path: Path | None = None
        self._diagnostic_lock = threading.Lock()
        if diagnostic_log_path is not None:
            self.set_diagnostic_log_path(diagnostic_log_path)

    def set_diagnostic_log_path(
        self, diagnostic_log_path: str | os.PathLike[str]
    ) -> None:
        self.diagnostic_log_path = Path(diagnostic_log_path).resolve(
            strict=False
        )
        if not bool(
            getattr(self.client, "_terralab_diagnostic_log_attached", False)
        ):
            previous_callback = getattr(
                self.client, "diagnostic_callback", None
            )

            def _combined_diagnostic(message: str) -> None:
                if previous_callback is not None:
                    previous_callback(message)
                self._diagnostic(message)

            self.client.diagnostic_callback = _combined_diagnostic
            self.client._terralab_diagnostic_log_attached = True

    def _diagnostic(self, message: str, *, reset: bool = False) -> None:
        path = self.diagnostic_log_path
        if path is None:
            return
        timestamp = _utc_now()
        line = f"{timestamp} [{threading.current_thread().name}] {message}\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._diagnostic_lock:
                with path.open(
                    "w" if reset else "a",
                    encoding="utf-8",
                    newline="\n",
                ) as handle:
                    handle.write(line)
                    handle.flush()
        except OSError:
            pass

    @staticmethod
    def required_working_space(
        estimate: SelectionEstimate,
        *,
        pending_fragment_bytes: int | None = None,
    ) -> int:
        """Estimate only work that still has to be materialised on disk."""

        fragment_storage = (
            estimate.raw_u16_bytes
            if pending_fragment_bytes is None
            else max(0, int(pending_fragment_bytes))
        )
        overview_ratio = sum(
            1.0 / float(level * level)
            for level in _overview_levels(
                estimate.width_px,
                estimate.height_px,
            )
        )
        final_storage = int(
            math.ceil(
                estimate.compressed_estimate_max_bytes
                * (1.0 + overview_ratio)
            )
        )
        subtotal = fragment_storage + final_storage
        margin = max(256_000_000, int(math.ceil(subtotal * 0.15)))
        return subtotal + margin

    @staticmethod
    def _emit(
        callback: Callable[[float, str], None] | None,
        percent: float,
        message: str,
    ) -> None:
        if callback is not None:
            callback(float(percent), str(message))

    @staticmethod
    def _metadata(
        request: DownloadRequest,
        plan: DownloadPlan,
        coverage: CoverageAssessment,
        validation: RasterValidation,
        *,
        reused_final_output: bool,
    ) -> dict[str, object]:
        radiometric_conversion = read_mosaic_radiometric_metadata(
            validation.path
        )
        return {
            "product_id": "copernicus-hrim-true-colour-2018",
            "product_name": PRODUCT_NAME,
            "product_url": PRODUCT_URL,
            "service_url": IMAGE_SERVER_URL,
            "wms_url": WMS_URL,
            "data_policy_url": DATA_POLICY_URL,
            "reference_year": 2018,
            "download_pipeline_version": DOWNLOAD_PIPELINE_VERSION,
            "bbox_wgs84": request.bbox_wgs84.to_dict(),
            "bounds_3035": plan.grid.bounds.to_dict(),
            "resolution_m": request.resolution_m,
            "format": "GeoTIFF",
            "pixel_type": request.pixel_type,
            "source_pixel_type": SERVICE_PIXEL_TYPE,
            "transport_pixel_type": SERVICE_PIXEL_TYPE,
            "output_pixel_type": request.pixel_type,
            "radiometric_conversion": radiometric_conversion,
            "bands": "RGB",
            "compression": (
                "DEFLATE" if request.compression == "LZ77" else "NONE"
            ),
            "attribution": ADAPTED_ATTRIBUTION,
            "downloaded_utc": _utc_now(),
            "adaptations": {
                "reprojected": True,
                "clipped": True,
                "converted": request.pixel_type == "U8",
            },
            "coverage_relation": coverage.relation,
            "outside_coverage_fraction": coverage.outside_fraction,
            "tiled": validation.tiled,
            "overviews": list(validation.overviews),
            "reused_final_output": bool(reused_final_output),
        }

    def run(
        self,
        request: DownloadRequest,
        progress_callback: Callable[[float, str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> DownloadResult:
        if not isinstance(request, DownloadRequest):
            raise TypeError("request must be a DownloadRequest")
        self._diagnostic(
            "start "
            f"service={IMAGE_SERVER_URL} "
            f"request={json.dumps(request.to_dict(), ensure_ascii=False)}",
            reset=True,
        )
        coverage = assess_service_coverage(request.bbox_wgs84)
        if coverage.relation == "outside":
            raise CopernicusDownloadError(
                "La selecció no intersecta la cobertura oficial del servei."
            )
        plan = plan_fragments(request)
        self._diagnostic(
            f"plan fragments={plan.fragment_count} "
            f"grid={plan.grid.width_px}x{plan.grid.height_px} "
            f"bounds3035={plan.grid.bounds.as_tuple()}"
        )
        identity = _plan_identity(plan)[:20]
        work_root = self.download_root / identity
        fragment_root = work_root / "fragments"
        fragment_root.mkdir(parents=True, exist_ok=True)
        manifest = DownloadManifest(work_root / "manifest.json", plan)
        output_dir = self.dataset_root / f"copernicus_hrim_2018_{identity}"
        output_path = output_dir / request.output_name
        output_dir.mkdir(parents=True, exist_ok=True)

        validation = manifest.validated_final(output_path)
        if validation is None:
            validation = manifest.adopt_final(output_path)
        if validation is not None:
            metadata = self._metadata(
                request,
                plan,
                coverage,
                validation,
                reused_final_output=True,
            )
            self._emit(
                progress_callback,
                100.0,
                "Ortofoto existent verificada i reutilitzada.",
            )
            return DownloadResult(
                output_path=str(output_path),
                manifest_path=str(manifest.path),
                request=request,
                estimate=plan.estimate,
                downloaded_fragments=0,
                reused_fragments=plan.fragment_count,
                metadata=metadata,
            )

        pending: list[Fragment] = []
        reused = 0
        reusable_ids: set[str] = set()
        for fragment in plan.fragments:
            complete = manifest.is_complete(fragment, fragment_root)
            if not complete:
                complete = manifest.adopt_existing(fragment, fragment_root)
            if complete:
                reused += 1
                reusable_ids.add(fragment.id)
            else:
                pending.append(fragment)
        pending_fragment_bytes = sum(
            fragment.width_px * fragment.height_px * 3 * 2
            for fragment in pending
        )
        required = self.required_working_space(
            plan.estimate,
            pending_fragment_bytes=pending_fragment_bytes,
        )
        available = int(self.disk_usage(self.dataset_root).free)
        if available < required:
            raise InsufficientDownloadSpaceError(required, available)

        progress_lock = threading.Lock()
        progress_units = {
            fragment.id: (1.0 if fragment.id in reusable_ids else 0.0)
            for fragment in plan.fragments
        }

        def emit_download_progress(detail: str) -> None:
            with progress_lock:
                units = sum(progress_units.values())
            self._emit(
                progress_callback,
                80.0 * units / max(1, plan.fragment_count),
                detail,
            )

        self._emit(
            progress_callback,
            80.0 * reused / max(1, plan.fragment_count),
            f"Fragments verificats: {reused}/{plan.fragment_count}",
        )
        internal_stop = threading.Event()

        def job_cancelled() -> bool:
            return internal_stop.is_set() or _cancelled(cancelled)

        def download_one(fragment: Fragment) -> Path:
            target = manifest.fragment_path(fragment, fragment_root)

            def byte_progress(downloaded: int, total: int) -> None:
                fraction = (
                    min(0.999, max(0.0, downloaded / total))
                    if total > 0
                    else 0.0
                )
                with progress_lock:
                    progress_units[fragment.id] = fraction
                total_text = format_bytes_dual(total) if total > 0 else "?"
                emit_download_progress(
                    f"{fragment.id}: {format_bytes_dual(downloaded)} / {total_text}"
                )

            return self.client.download_fragment(
                fragment,
                request,
                target,
                progress=byte_progress,
                cancelled=job_cancelled,
            )

        if pending:
            try:
                with ThreadPoolExecutor(
                    max_workers=request.max_concurrent_requests,
                    thread_name_prefix="copernicus-export",
                ) as executor:
                    future_fragments = {
                        executor.submit(download_one, fragment): fragment
                        for fragment in pending
                    }
                    try:
                        for future in as_completed(future_fragments):
                            fragment = future_fragments[future]
                            if _cancelled(cancelled):
                                raise CopernicusDownloadCancelled(
                                    "Descàrrega cancel·lada."
                                )
                            path = future.result()
                            manifest.mark_complete(fragment, path)
                            with progress_lock:
                                progress_units[fragment.id] = 1.0
                                current = sum(
                                    value >= 1.0
                                    for value in progress_units.values()
                                )
                            emit_download_progress(
                                f"Fragment {current}/{plan.fragment_count}"
                            )
                    except Exception:
                        internal_stop.set()
                        for future in future_fragments:
                            future.cancel()
                        raise
            except Exception:
                # All executor jobs have now joined. Persist any TIFF that
                # reached its atomic final name before a sibling failed.
                for fragment in pending:
                    if manifest.is_complete(fragment, fragment_root):
                        continue
                    try:
                        manifest.adopt_existing(fragment, fragment_root)
                    except Exception:
                        # Never mask the original transport/cancellation error.
                        log_suppressed_exception(__name__, "CopernicusOrthophotoManager.run")
                raise

        if _cancelled(cancelled):
            raise CopernicusDownloadCancelled("Descàrrega cancel·lada.")
        self._emit(progress_callback, 80.0, "Generant mosaic GeoTIFF…")
        build_mosaic(
            plan,
            manifest,
            fragment_root,
            output_path,
            progress=(
                lambda fraction, message: self._emit(
                    progress_callback,
                    80.0 + 18.0 * float(fraction),
                    message,
                )
            ),
            cancelled=cancelled,
        )
        validation = validate_final_geotiff(output_path, plan)
        manifest.mark_final(output_path, validation)
        metadata = self._metadata(
            request,
            plan,
            coverage,
            validation,
            reused_final_output=False,
        )
        self._emit(progress_callback, 100.0, "Ortofoto preparada.")
        return DownloadResult(
            output_path=str(output_path),
            manifest_path=str(manifest.path),
            request=request,
            estimate=plan.estimate,
            downloaded_fragments=len(pending),
            reused_fragments=reused,
            metadata=metadata,
        )
