"""ArcGIS ImageServer transport for Copernicus raster fragments."""

from __future__ import annotations

import html
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Mapping

import numpy as np
import requests

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.locks import RASTERIO_LOCK
from .constants import IMAGE_SERVER_URL
from .planning import DownloadRequest, Fragment, transform_bounds_to_3035
from .validation import (
    CopernicusDownloadCancelled,
    CopernicusDownloadError,
    NodataEstimate,
    _cancelled,
    validate_fragment_raster,
)


class ArcGISImageServerClient:
    """Streaming, retrying client for the official ``exportImage`` endpoint."""

    def __init__(
        self,
        service_url: str = IMAGE_SERVER_URL,
        *,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (30.0, 180.0),
        max_retries: int = 3,
        retry_sleep: Callable[[float], None] = time.sleep,
        chunk_size: int = 1024 * 1024,
        diagnostic_callback: Callable[[str], None] | None = None,
    ) -> None:
        base = str(service_url).rstrip("/")
        self.export_url = (
            base if base.lower().endswith("/exportimage") else base + "/exportImage"
        )
        self.query_url = (
            base.rsplit("/exportImage", 1)[0] + "/query"
            if base.lower().endswith("/exportimage")
            else base + "/query"
        )
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent", "TerraLab/1.0 (Copernicus orthophoto downloader)"
        )
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.retry_sleep = retry_sleep
        self.chunk_size = max(64 * 1024, int(chunk_size))
        self.diagnostic_callback = diagnostic_callback

    def _diagnostic(self, message: str) -> None:
        callback = self.diagnostic_callback
        if callback is None:
            return
        try:
            callback(str(message))
        except Exception:
            # Diagnostics must never break or mask a download.
            log_suppressed_exception(__name__, "ArcGISImageServerClient._diagnostic")

    @staticmethod
    def export_params(
        fragment: Fragment,
        request: DownloadRequest,
    ) -> dict[str, str]:
        bounds = fragment.bounds_3035
        return {
            "bbox": ",".join(f"{value:.12g}" for value in bounds.as_tuple()),
            "bboxSR": "3035",
            "imageSR": "3035",
            "size": f"{fragment.width_px},{fragment.height_px}",
            "format": "tiff",
            # U8 is deliberately produced locally. Asking this service for
            # U8 clips native U16 samples at 255 instead of applying a
            # display stretch, yielding a nearly all-white raster.
            "pixelType": request.transport_pixel_type,
            "interpolation": "RSP_BilinearInterpolation",
            # This controls transport TIFF compression only.  The user's
            # NONE/LZ77 choice controls the final local mosaic independently.
            "compression": "LZ77",
            "adjustAspectRatio": "false",
            # Request a JSON export descriptor first.  This service reliably
            # prepares larger TIFFs in its ArcGIS output directory and returns
            # a short-lived ``href``; direct ``f=image`` streaming can be
            # rejected by the front-end with an HTML HTTP 500 response.
            "f": "json",
        }

    @staticmethod
    def _response_error(response: requests.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, Mapping):
                error = payload.get("error", payload)
                if isinstance(error, Mapping):
                    message = str(error.get("message", "") or "")
                    details = error.get("details", ())
                    suffix = "; ".join(str(item) for item in details or ())
                    return ": ".join(item for item in (message, suffix) if item)
                return str(error)
        except Exception:
            log_suppressed_exception(__name__, "ArcGISImageServerClient._response_error")
        raw = str(getattr(response, "text", "") or "")
        if not raw:
            return "resposta sense detall"
        # ArcGIS is occasionally fronted by an HTML error page.  Passing that
        # HTML to QMessageBox makes Qt render the provider's page (including
        # broken image icons) instead of showing a useful TerraLab error.
        plain = re.sub(
            r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
            " ",
            raw,
        )
        plain = html.unescape(re.sub(r"(?s)<[^>]+>", " ", plain))
        plain = " ".join(plain.split())
        return plain[:500] or "resposta HTML sense detall"

    def _checked_response(
        self,
        url: str,
        params: Mapping[str, str],
        *,
        stream: bool,
        cancelled: Callable[[], bool] | None,
        expect_json: bool = False,
    ) -> requests.Response:
        if _cancelled(cancelled):
            raise CopernicusDownloadCancelled("Descàrrega cancel·lada.")
        response = self.session.get(
            url,
            params=dict(params),
            stream=stream,
            timeout=self.timeout,
        )
        self._diagnostic(
            "response "
            f"status={response.status_code} "
            f"content_type={response.headers.get('Content-Type', '')!r} "
            f"content_length={response.headers.get('Content-Length', '')!r} "
            f"request_id={response.headers.get('X-ArcGIS-Instance', '')!r}"
        )
        if int(response.status_code) != 200:
            detail = self._response_error(response)
            response.close()
            if int(response.status_code) >= 500:
                detail = (
                    "error temporal del servidor oficial; "
                    f"{detail}"
                )
            raise CopernicusDownloadError(
                f"ImageServer HTTP {response.status_code}: {detail}"
            )
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if not expect_json and ("json" in content_type or "html" in content_type):
            detail = self._response_error(response)
            response.close()
            raise CopernicusDownloadError(
                f"ImageServer no ha retornat un raster: {detail}"
            )
        return response

    def _export_href(
        self,
        params: Mapping[str, str],
        fragment: Fragment,
        *,
        cancelled: Callable[[], bool] | None,
    ) -> str:
        response = self._checked_response(
            self.export_url,
            params,
            stream=False,
            cancelled=cancelled,
            expect_json=True,
        )
        try:
            try:
                payload = response.json()
            except Exception as exc:
                raise CopernicusDownloadError(
                    "ImageServer no ha retornat el descriptor JSON "
                    "de l'exportació."
                ) from exc
        finally:
            response.close()
        if not isinstance(payload, Mapping):
            raise CopernicusDownloadError(
                "Descriptor JSON d'exportació invàlid."
            )
        error = payload.get("error")
        if error:
            raise CopernicusDownloadError(
                f"ImageServer ha rebutjat l'exportació: {error}"
            )
        href = str(payload.get("href", "") or "").strip()
        try:
            width = int(payload.get("width", 0) or 0)
            height = int(payload.get("height", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise CopernicusDownloadError(
                "Dimensions invàlides al descriptor d'exportació."
            ) from exc
        if width != fragment.width_px or height != fragment.height_px:
            raise CopernicusDownloadError(
                "El descriptor ImageServer no conserva les dimensions "
                f"sol·licitades: {width}×{height}, esperat "
                f"{fragment.width_px}×{fragment.height_px}."
            )
        if not href:
            raise CopernicusDownloadError(
                "El descriptor ImageServer no conté cap URL de descàrrega."
            )
        href = urllib.parse.urljoin(self.export_url, href)
        export_parts = urllib.parse.urlsplit(self.export_url)
        href_parts = urllib.parse.urlsplit(href)
        if (
            href_parts.scheme.lower() != export_parts.scheme.lower()
            or href_parts.hostname != export_parts.hostname
            or href_parts.username is not None
            or href_parts.password is not None
        ):
            raise CopernicusDownloadError(
                "ImageServer ha retornat una URL de descàrrega no fiable."
            )
        self._diagnostic(
            f"fragment={fragment.id} export_ready "
            f"width={width} height={height} href={href}"
        )
        return href

    def _request_raster(
        self,
        href: str,
        *,
        stream: bool,
        cancelled: Callable[[], bool] | None,
    ) -> requests.Response:
        return self._checked_response(
            href,
            {},
            stream=stream,
            cancelled=cancelled,
        )

    def download_fragment(
        self,
        fragment: Fragment,
        request: DownloadRequest,
        target_path: str | os.PathLike[str],
        *,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Path:
        target = Path(target_path).resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.part")
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            response: requests.Response | None = None
            try:
                params = self.export_params(fragment, request)
                self._diagnostic(
                    f"fragment={fragment.id} "
                    f"attempt={attempt + 1}/{self.max_retries + 1} "
                    f"bbox={params['bbox']} size={params['size']} "
                    f"pixelType={params['pixelType']}"
                )
                href = self._export_href(
                    params,
                    fragment,
                    cancelled=cancelled,
                )
                response = self._request_raster(
                    href, stream=True, cancelled=cancelled
                )
                try:
                    total = int(response.headers.get("Content-Length", "0") or 0)
                except (TypeError, ValueError):
                    total = 0
                downloaded = 0
                with partial.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=self.chunk_size):
                        if _cancelled(cancelled):
                            raise CopernicusDownloadCancelled(
                                "Descàrrega cancel·lada."
                            )
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if progress is not None:
                            progress(downloaded, total)
                    handle.flush()
                    os.fsync(handle.fileno())
                if downloaded <= 0:
                    raise CopernicusDownloadError(
                        f"El fragment {fragment.id} és buit."
                    )
                if total > 0 and downloaded != total:
                    raise CopernicusDownloadError(
                        f"Resposta truncada al fragment {fragment.id}: "
                        f"{downloaded}/{total} bytes."
                    )
                validate_fragment_raster(partial, fragment, request)
                os.replace(partial, target)
                self._diagnostic(
                    f"fragment={fragment.id} completed bytes={downloaded}"
                )
                return target
            except CopernicusDownloadCancelled:
                partial.unlink(missing_ok=True)
                raise
            except Exception as exc:
                last_error = exc
                self._diagnostic(
                    f"fragment={fragment.id} attempt={attempt + 1} "
                    f"failed={type(exc).__name__}: {exc}"
                )
                partial.unlink(missing_ok=True)
                if attempt >= self.max_retries:
                    break
                remaining = min(30.0, float(2**attempt))
                while remaining > 0.0:
                    if _cancelled(cancelled):
                        raise CopernicusDownloadCancelled(
                            "Descàrrega cancel·lada durant el reintent."
                        )
                    interval = min(0.1, remaining)
                    self.retry_sleep(interval)
                    remaining -= interval
            finally:
                if response is not None:
                    response.close()
        raise CopernicusDownloadError(
            f"No s'ha pogut descarregar {fragment.id}: {last_error}"
        ) from last_error

    def estimate_nodata_fraction(
        self,
        request: DownloadRequest,
        *,
        sample_size: int = 128,
        cancelled: Callable[[], bool] | None = None,
    ) -> NodataEstimate:
        """Estimate black/no-mosaic pixels; the service publishes no mask."""

        projected = transform_bounds_to_3035(request.bbox_wgs84)
        side = max(16, min(512, int(sample_size)))
        sample_fragment = Fragment(
            index=0,
            row_index=0,
            column_index=0,
            row_offset=0,
            column_offset=0,
            width_px=side,
            height_px=side,
            bounds_3035=projected,
        )
        sample_request = DownloadRequest(
            bbox_wgs84=request.bbox_wgs84,
            resolution_m=request.resolution_m,
            pixel_type="U8",
            compression="LZ77",
            output_name=request.output_name,
            tile_width_px=request.tile_width_px,
            tile_height_px=request.tile_height_px,
            max_concurrent_requests=request.max_concurrent_requests,
        )
        params = self.export_params(sample_fragment, sample_request)
        href = self._export_href(
            params, sample_fragment, cancelled=cancelled
        )
        response = self._request_raster(
            href, stream=False, cancelled=cancelled
        )
        try:
            content = bytes(response.content)
        finally:
            response.close()
        if content[:4] not in {
            b"II*\x00",
            b"MM\x00*",
            b"II+\x00",
            b"MM\x00+",
        }:
            raise CopernicusDownloadError(
                "La mostra ImageServer no és un TIFF."
            )
        from rasterio.io import MemoryFile

        with RASTERIO_LOCK:
            with MemoryFile(content) as memory:
                with memory.open() as dataset:
                    if dataset.count < 3:
                        raise CopernicusDownloadError(
                            "La mostra ImageServer no és RGB."
                        )
                    rgb = np.asarray(dataset.read((1, 2, 3)))
        black = np.all(rgb == 0, axis=0)
        total = int(black.size)
        zeros = int(np.count_nonzero(black))
        return NodataEstimate(
            fraction=(zeros / total if total else 0.0),
            sampled_pixels=total,
            zero_rgb_pixels=zeros,
        )



def estimate_nodata_fraction(
    request: DownloadRequest,
    *,
    sample_size: int = 128,
    client: ArcGISImageServerClient | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> NodataEstimate:
    """Convenience preflight wrapper used by the onboarding dialog."""

    return (client or ArcGISImageServerClient()).estimate_nodata_fraction(
        request,
        sample_size=sample_size,
        cancelled=cancelled,
    )
