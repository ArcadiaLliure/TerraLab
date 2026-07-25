"""Radiometric conversion and tiled GeoTIFF mosaic construction."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import numpy as np

from TerraLab.common.locks import RASTERIO_LOCK
from .constants import (
    ADAPTED_ATTRIBUTION,
    CRS_PRODUCT,
    DOWNLOAD_PIPELINE_VERSION,
    IMAGE_SERVER_URL,
    PRODUCT_NAME,
    PRODUCT_URL,
    SERVICE_PIXEL_TYPE,
    U8_STRETCH_HIGH_PERCENTILE,
    U8_STRETCH_LOW_PERCENTILE,
)
from .manifest import DownloadManifest
from .planning import DownloadPlan
from .validation import (
    CopernicusDownloadCancelled,
    CopernicusDownloadError,
    RasterValidation,
    U8GlobalStretch,
    _cancelled,
    validate_fragment_partition,
    validate_fragment_raster,
)


def _overview_levels(width: int, height: int) -> list[int]:
    levels: list[int] = []
    factor = 2
    largest = max(int(width), int(height))
    while factor <= 64 and largest // factor >= 256:
        levels.append(factor)
        factor *= 2
    return levels


def _histogram_percentile(histogram: np.ndarray, percentile: float) -> int:
    total = int(np.asarray(histogram, dtype=np.uint64).sum())
    if total <= 0:
        return 0
    rank = float(percentile) / 100.0 * float(total - 1)
    cumulative = np.cumsum(histogram, dtype=np.uint64)
    return int(np.searchsorted(cumulative, rank, side="right"))


def _calculate_u8_global_stretch(
    plan: DownloadPlan,
    manifest: DownloadManifest,
    fragment_root: Path,
    *,
    nodata: float | int | None,
    progress: Callable[[float, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> U8GlobalStretch:
    """Calculate exact selection-wide per-band percentiles out of core.

    A single transform is subsequently applied to every tile, so adjacent
    fragments cannot acquire different brightness or colour balance.  The
    service currently publishes no useful mask; all-zero RGB pixels are
    excluded from the histogram as the same documented NoData proxy used by
    the preflight estimator, but remain zero in the output.
    """

    import rasterio

    histograms = np.zeros((3, 65_536), dtype=np.uint64)
    valid_pixel_count = 0
    total = max(1, len(plan.fragments))
    for number, fragment in enumerate(plan.fragments, start=1):
        if _cancelled(cancelled):
            raise CopernicusDownloadCancelled(
                "Càlcul radiomètric cancel·lat."
            )
        source_path = manifest.fragment_path(fragment, fragment_root)
        with RASTERIO_LOCK:
            source = rasterio.open(source_path)
        try:
            for _block_index, source_window in source.block_windows(1):
                if _cancelled(cancelled):
                    raise CopernicusDownloadCancelled(
                        "Càlcul radiomètric cancel·lat."
                    )
                data = np.asarray(
                    source.read((1, 2, 3), window=source_window),
                    dtype=np.uint16,
                )
                invalid = np.all(data == 0, axis=0)
                if nodata is not None:
                    invalid |= np.all(data == nodata, axis=0)
                valid = ~invalid
                count = int(np.count_nonzero(valid))
                if count <= 0:
                    continue
                valid_pixel_count += count
                for band_index in range(3):
                    counts = np.bincount(
                        data[band_index][valid],
                        minlength=65_536,
                    )
                    histograms[band_index] += counts.astype(
                        np.uint64, copy=False
                    )
        finally:
            source.close()
        if progress is not None:
            progress(
                number / total,
                f"Analitzant contrast global {number}/{total}",
            )

    lows: list[int] = []
    highs: list[int] = []
    for band_histogram in histograms:
        populated = np.flatnonzero(band_histogram)
        if populated.size == 0:
            low, high = 0, 65_535
        else:
            low = _histogram_percentile(
                band_histogram, U8_STRETCH_LOW_PERCENTILE
            )
            high = _histogram_percentile(
                band_histogram, U8_STRETCH_HIGH_PERCENTILE
            )
            if high <= low:
                minimum = int(populated[0])
                maximum = int(populated[-1])
                if maximum > minimum:
                    low, high = minimum, maximum
                elif maximum > 0:
                    low, high = 0, maximum
                else:
                    low, high = 0, 65_535
        lows.append(low)
        highs.append(high)
    return U8GlobalStretch(
        low=(lows[0], lows[1], lows[2]),
        high=(highs[0], highs[1], highs[2]),
        valid_pixel_count=valid_pixel_count,
    )


def _convert_u16_rgb_to_u8(
    data: np.ndarray,
    stretch: U8GlobalStretch,
    *,
    nodata: float | int | None,
) -> np.ndarray:
    """Apply a deterministic integer transform without per-tile statistics."""

    source = np.asarray(data, dtype=np.uint16)
    if source.ndim != 3 or source.shape[0] != 3:
        raise ValueError("Expected a three-band U16 RGB block")
    converted = np.empty(source.shape, dtype=np.uint8)
    for band_index, (low, high) in enumerate(
        zip(stretch.low, stretch.high)
    ):
        span = int(high) - int(low)
        if span <= 0:
            raise CopernicusDownloadError("Estirament U8 invàlid.")
        clipped = np.clip(
            source[band_index], int(low), int(high)
        ).astype(np.uint32, copy=False)
        numerator = (clipped - int(low)) * 255 + span // 2
        converted[band_index] = (numerator // span).astype(np.uint8)
    if nodata is not None:
        nodata_mask = np.all(source == nodata, axis=0)
        converted[:, nodata_mask] = 0
    return converted



def build_mosaic(
    plan: DownloadPlan,
    manifest: DownloadManifest,
    fragment_root: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    progress: Callable[[float, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Stream fragment blocks into one tiled EPSG:3035 GeoTIFF."""

    validate_fragment_partition(plan)
    import rasterio
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.transform import from_origin
    from rasterio.windows import Window

    root = Path(fragment_root)
    output = Path(output_path).resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.building.tif")
    temporary.unlink(missing_ok=True)
    dtype = "uint16" if plan.request.pixel_type == "U16" else "uint8"
    profile: dict[str, object] = {
        "driver": "GTiff",
        "width": plan.grid.width_px,
        "height": plan.grid.height_px,
        "count": 3,
        "dtype": dtype,
        "crs": CRS_PRODUCT,
        "transform": from_origin(
            plan.grid.bounds.xmin,
            plan.grid.bounds.ymax,
            plan.grid.resolution_m,
            plan.grid.resolution_m,
        ),
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "BIGTIFF": "IF_SAFER",
    }
    fragment_validations: list[RasterValidation] = []
    for fragment in plan.fragments:
        if not manifest.is_complete(fragment, root):
            raise CopernicusDownloadError(
                f"Falta el fragment vàlid {fragment.id}."
            )
        fragment_validations.append(
            validate_fragment_raster(
                manifest.fragment_path(fragment, root),
                fragment,
                plan.request,
            )
        )
    nodata_values = {validation.nodata for validation in fragment_validations}
    if len(nodata_values) != 1:
        raise CopernicusDownloadError(
            "Els fragments no comparteixen el mateix valor NoData."
        )
    common_nodata = next(iter(nodata_values))
    stretch: U8GlobalStretch | None = None
    if plan.request.pixel_type == "U8":
        stretch = _calculate_u8_global_stretch(
            plan,
            manifest,
            root,
            nodata=common_nodata,
            progress=(
                (
                    lambda fraction, message: progress(
                        0.35 * float(fraction), message
                    )
                )
                if progress is not None
                else None
            ),
            cancelled=cancelled,
        )
    if common_nodata is not None:
        # A U16 NoData value cannot necessarily be represented in U8.
        profile["nodata"] = (
            common_nodata if plan.request.pixel_type == "U16" else 0
        )
    if plan.request.compression == "LZ77":
        profile.update({"compress": "DEFLATE", "predictor": 2})
    try:
        with RASTERIO_LOCK:
            destination = rasterio.open(temporary, "w", **profile)
        try:
            destination.colorinterp = (
                ColorInterp.red,
                ColorInterp.green,
                ColorInterp.blue,
            )
            total = max(1, len(plan.fragments))
            for number, fragment in enumerate(plan.fragments, start=1):
                if _cancelled(cancelled):
                    raise CopernicusDownloadCancelled(
                        "Construcció del mosaic cancel·lada."
                    )
                source_path = manifest.fragment_path(fragment, root)
                if not manifest.is_complete(fragment, root):
                    raise CopernicusDownloadError(
                        f"Falta el fragment vàlid {fragment.id}."
                    )
                with RASTERIO_LOCK:
                    source = rasterio.open(source_path)
                try:
                    for _block_index, source_window in source.block_windows(1):
                        if _cancelled(cancelled):
                            raise CopernicusDownloadCancelled(
                                "Construcció del mosaic cancel·lada."
                            )
                        data = source.read((1, 2, 3), window=source_window)
                        if stretch is not None:
                            data = _convert_u16_rgb_to_u8(
                                data,
                                stretch,
                                nodata=common_nodata,
                            )
                        destination_window = Window(
                            fragment.column_offset + int(source_window.col_off),
                            fragment.row_offset + int(source_window.row_off),
                            int(source_window.width),
                            int(source_window.height),
                        )
                        destination.write(data, window=destination_window)
                finally:
                    source.close()
                if progress is not None:
                    fraction = number / total
                    if stretch is not None:
                        fraction = 0.35 + 0.65 * fraction
                    progress(fraction, f"Mosaic {number}/{total}")
            levels = _overview_levels(plan.grid.width_px, plan.grid.height_px)
            if levels:
                destination.build_overviews(levels, Resampling.average)
                destination.update_tags(
                    ns="rio_overview", resampling="average"
                )
            conversion: dict[str, object]
            if stretch is not None:
                conversion = stretch.to_dict()
            else:
                conversion = {
                    "method": "none",
                    "source_pixel_type": SERVICE_PIXEL_TYPE,
                    "output_pixel_type": "U16",
                }
            destination.update_tags(
                product=PRODUCT_NAME,
                attribution=ADAPTED_ATTRIBUTION,
                source=PRODUCT_URL,
                service=IMAGE_SERVER_URL,
                download_pipeline_version=str(DOWNLOAD_PIPELINE_VERSION),
                source_pixel_type=SERVICE_PIXEL_TYPE,
                output_pixel_type=plan.request.pixel_type,
                radiometric_conversion=json.dumps(
                    conversion,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        finally:
            destination.close()
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output
