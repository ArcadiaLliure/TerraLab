"""Raster signatures, validation, and radiometric metadata."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from pyproj import CRS as PyprojCRS

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.locks import RASTERIO_LOCK
from TerraLab.data.crs import PYPROJ_TRANSFORMER_LOCK
from .constants import (
    SERVICE_PIXEL_TYPE,
    U8_STRETCH_HIGH_PERCENTILE,
    U8_STRETCH_LOW_PERCENTILE,
    U8_STRETCH_METHOD,
)
from .planning import DownloadPlan, DownloadRequest, Fragment, ProjectedBounds, format_bytes_dual


class CopernicusDownloadError(RuntimeError):
    """Base error for an official orthophoto export."""


class CopernicusDownloadCancelled(CopernicusDownloadError):
    """Cooperative cancellation; completed fragments remain resumable."""


class ManifestMismatchError(CopernicusDownloadError):
    """A durable manifest belongs to a different pixel grid or request."""


class InsufficientDownloadSpaceError(CopernicusDownloadError):
    def __init__(self, required_bytes: int, available_bytes: int) -> None:
        self.required_bytes = int(required_bytes)
        self.available_bytes = int(available_bytes)
        super().__init__(
            "Espai insuficient per a l'ortofoto: calen "
            f"{format_bytes_dual(required_bytes)} i hi ha "
            f"{format_bytes_dual(available_bytes)} disponibles."
        )


@dataclass(frozen=True)
class NodataEstimate:
    fraction: float
    sampled_pixels: int
    zero_rgb_pixels: int
    method: str = "all_three_rgb_bands_equal_zero_proxy"


@dataclass(frozen=True)
class RasterValidation:
    path: str
    width_px: int
    height_px: int
    band_count: int
    pixel_type: str
    crs: str
    bounds: ProjectedBounds
    tiled: bool
    nodata: float | int | None = None
    overviews: tuple[int, ...] = ()


@dataclass(frozen=True)
class U8GlobalStretch:
    """One radiometric transform shared by every fragment in a selection."""

    low: tuple[int, int, int]
    high: tuple[int, int, int]
    valid_pixel_count: int
    method: str = U8_STRETCH_METHOD
    low_percentile: float = U8_STRETCH_LOW_PERCENTILE
    high_percentile: float = U8_STRETCH_HIGH_PERCENTILE

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "scope": "selection",
            "per_band": True,
            "low_percentile": self.low_percentile,
            "high_percentile": self.high_percentile,
            "low": list(self.low),
            "high": list(self.high),
            "valid_pixel_count": self.valid_pixel_count,
            "histogram_excludes": "all_zero_rgb_proxy",
            "zero_rgb_preserved": True,
            "source_pixel_type": SERVICE_PIXEL_TYPE,
            "output_pixel_type": "U8",
        }


def _cancelled(predicate: Callable[[], bool] | None) -> bool:
    try:
        return bool(predicate is not None and predicate())
    except Exception:
        return False


def _tiff_signature(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            signature = handle.read(4)
    except OSError:
        return False
    return signature in {
        b"II*\x00",
        b"MM\x00*",
        b"II+\x00",
        b"MM\x00+",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pixel_type_for_dtype(dtype: str) -> str:
    normalized = str(np.dtype(dtype))
    return {"uint8": "U8", "uint16": "U16"}.get(normalized, normalized)


def _is_epsg_3035(crs: object) -> bool:
    """Recognize EPSG:3035 even when GDAL cannot recover its authority code.

    The official ImageServer currently emits an ESRI-flavoured
    ``ETRS89-extended / LAEA Europe`` WKT.  Rasterio can report
    ``to_epsg() is None`` for it even though its Lambert azimuthal equal-area
    parameters are exactly EPSG:3035.
    """

    if crs is None:
        return False
    try:
        if getattr(crs, "to_epsg")() == 3035:
            return True
    except Exception:
        log_suppressed_exception(__name__, "_is_epsg_3035")
    try:
        authority = getattr(crs, "to_authority")()
        if authority and str(authority[0]).upper() == "EPSG":
            if int(authority[1]) == 3035:
                return True
    except Exception:
        log_suppressed_exception(__name__, "_is_epsg_3035")
    try:
        with PYPROJ_TRANSFORMER_LOCK:
            candidate = PyprojCRS.from_user_input(crs)
            official = PyprojCRS.from_epsg(3035)
            if candidate.equals(official, ignore_axis_order=True):
                return True
    except Exception:
        log_suppressed_exception(__name__, "_is_epsg_3035")
    try:
        values = {
            str(key).lower(): value
            for key, value in dict(getattr(crs, "to_dict")()).items()
        }
        projection = str(values.get("proj", "")).lower()
        ellipsoid = str(
            values.get("ellps", values.get("datum", ""))
        ).upper()

        def close(key: str, expected: float, tolerance: float = 1e-6) -> bool:
            return math.isclose(
                float(values.get(key)),
                expected,
                rel_tol=0.0,
                abs_tol=tolerance,
            )

        return (
            projection == "laea"
            and close("lat_0", 52.0)
            and close("lon_0", 10.0)
            and close("x_0", 4_321_000.0, 0.01)
            and close("y_0", 3_210_000.0, 0.01)
            and ellipsoid in {"GRS80", "ETRS89", "ETRS_1989"}
            and str(values.get("units", "m")).lower()
            in {"m", "metre", "meter"}
        )
    except (AttributeError, TypeError, ValueError):
        return False


def validate_fragment_raster(
    path: str | os.PathLike[str],
    fragment: Fragment,
    request: DownloadRequest,
) -> RasterValidation:
    """Validate one downloaded tile without reading its full pixel payload."""

    raster_path = Path(path)
    if not raster_path.is_file() or not _tiff_signature(raster_path):
        raise CopernicusDownloadError(
            f"La resposta de {fragment.id} no és un TIFF."
        )
    import rasterio

    with RASTERIO_LOCK:
        with rasterio.open(raster_path) as dataset:
            crs = dataset.crs
            bounds = ProjectedBounds(*tuple(float(v) for v in dataset.bounds))
            validation = RasterValidation(
                path=str(raster_path),
                width_px=int(dataset.width),
                height_px=int(dataset.height),
                band_count=int(dataset.count),
                pixel_type=_pixel_type_for_dtype(dataset.dtypes[0]),
                crs=crs.to_string() if crs is not None else "",
                bounds=bounds,
                tiled=bool(dataset.profile.get("tiled", False)),
                nodata=dataset.nodata,
                overviews=tuple(int(v) for v in dataset.overviews(1)),
            )
            dtypes = tuple(_pixel_type_for_dtype(item) for item in dataset.dtypes)
            nodatavals = tuple(dataset.nodatavals)
    if not _is_epsg_3035(crs):
        raise CopernicusDownloadError(
            f"El fragment {fragment.id} no és EPSG:3035."
        )
    if validation.width_px != fragment.width_px:
        raise CopernicusDownloadError(
            f"Amplada incorrecta al fragment {fragment.id}."
        )
    if validation.height_px != fragment.height_px:
        raise CopernicusDownloadError(
            f"Alçada incorrecta al fragment {fragment.id}."
        )
    if validation.band_count != 3:
        raise CopernicusDownloadError(
            f"El fragment {fragment.id} no té tres bandes RGB."
        )
    if any(dtype != request.transport_pixel_type for dtype in dtypes):
        raise CopernicusDownloadError(
            f"Tipus de píxel incorrecte al fragment {fragment.id}."
        )
    if any(value != validation.nodata for value in nodatavals):
        raise CopernicusDownloadError(
            f"Les bandes de {fragment.id} no comparteixen NoData."
        )
    tolerance = max(0.05, request.resolution_m * 0.01)
    for actual, expected in zip(
        validation.bounds.as_tuple(),
        fragment.bounds_3035.as_tuple(),
    ):
        if not math.isclose(actual, expected, abs_tol=tolerance):
            raise CopernicusDownloadError(
                f"Extensió desalineada al fragment {fragment.id}."
            )
    return validation



def validate_fragment_partition(plan: DownloadPlan) -> None:
    """Prove that fragment pixel windows partition the output exactly once."""

    by_row: dict[int, list[Fragment]] = {}
    for fragment in plan.fragments:
        by_row.setdefault(fragment.row_index, []).append(fragment)
    expected_row = 0
    row_offset = 0
    for row_index in sorted(by_row):
        if row_index != expected_row:
            raise CopernicusDownloadError("Falta una fila de fragments.")
        row = sorted(by_row[row_index], key=lambda item: item.column_offset)
        if not row:
            raise CopernicusDownloadError("Fila de fragments buida.")
        height = row[0].height_px
        if row[0].row_offset != row_offset:
            raise CopernicusDownloadError("Files de fragments desalineades.")
        column_offset = 0
        for fragment in row:
            if (
                fragment.row_offset != row_offset
                or fragment.height_px != height
                or fragment.column_offset != column_offset
            ):
                raise CopernicusDownloadError(
                    "Buit o solapament entre fragments."
                )
            column_offset += fragment.width_px
        if column_offset != plan.grid.width_px:
            raise CopernicusDownloadError("La fila no cobreix tota l'amplada.")
        row_offset += height
        expected_row += 1
    if row_offset != plan.grid.height_px:
        raise CopernicusDownloadError("Els fragments no cobreixen tota l'alçada.")



def read_mosaic_radiometric_metadata(
    path: str | os.PathLike[str],
) -> dict[str, object]:
    """Read TerraLab's explicit U16-to-U8 conversion metadata."""

    import rasterio

    with RASTERIO_LOCK:
        with rasterio.open(path) as dataset:
            tags = dataset.tags()
    raw = tags.get("radiometric_conversion", "")
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
        except (TypeError, ValueError):
            pass
    return {
        "method": "none",
        "source_pixel_type": tags.get(
            "source_pixel_type", SERVICE_PIXEL_TYPE
        ),
        "output_pixel_type": tags.get(
            "output_pixel_type", SERVICE_PIXEL_TYPE
        ),
    }



def validate_final_geotiff(
    path: str | os.PathLike[str],
    plan: DownloadPlan,
) -> RasterValidation:
    import rasterio

    raster_path = Path(path)
    if not _tiff_signature(raster_path):
        raise CopernicusDownloadError("El mosaic final no és un GeoTIFF.")
    with RASTERIO_LOCK:
        with rasterio.open(raster_path) as dataset:
            crs = dataset.crs
            bounds = ProjectedBounds(*tuple(float(v) for v in dataset.bounds))
            validation = RasterValidation(
                path=str(raster_path),
                width_px=int(dataset.width),
                height_px=int(dataset.height),
                band_count=int(dataset.count),
                pixel_type=_pixel_type_for_dtype(dataset.dtypes[0]),
                crs=crs.to_string() if crs is not None else "",
                bounds=bounds,
                tiled=bool(dataset.profile.get("tiled", False)),
                nodata=dataset.nodata,
                overviews=tuple(int(v) for v in dataset.overviews(1)),
            )
            all_types = tuple(
                _pixel_type_for_dtype(dtype) for dtype in dataset.dtypes
            )
            tags = dataset.tags()
    expected = (
        plan.grid.width_px,
        plan.grid.height_px,
        3,
        plan.request.pixel_type,
    )
    actual = (
        validation.width_px,
        validation.height_px,
        validation.band_count,
        validation.pixel_type,
    )
    if plan.request.pixel_type == "U8":
        try:
            radiometry = json.loads(tags.get("radiometric_conversion", ""))
            low = tuple(int(value) for value in radiometry["low"])
            high = tuple(int(value) for value in radiometry["high"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CopernicusDownloadError(
                "El mosaic U8 no documenta la conversió radiomètrica."
            ) from exc
        if (
            radiometry.get("method") != U8_STRETCH_METHOD
            or tags.get("source_pixel_type") != SERVICE_PIXEL_TYPE
            or tags.get("output_pixel_type") != "U8"
            or len(low) != 3
            or len(high) != 3
            or any(upper <= lower for lower, upper in zip(low, high))
        ):
            raise CopernicusDownloadError(
                "La conversió radiomètrica del mosaic U8 és invàlida."
            )
    if actual != expected or any(item != plan.request.pixel_type for item in all_types):
        raise CopernicusDownloadError("Dimensions, bandes o dtype finals invàlids.")
    if not _is_epsg_3035(crs):
        raise CopernicusDownloadError("El mosaic final no és EPSG:3035.")
    if not validation.tiled:
        raise CopernicusDownloadError("El mosaic final no està teselat.")
    tolerance = max(0.05, plan.grid.resolution_m * 0.01)
    if any(
        not math.isclose(actual_value, expected_value, abs_tol=tolerance)
        for actual_value, expected_value in zip(
            validation.bounds.as_tuple(), plan.grid.bounds.as_tuple()
        )
    ):
        raise CopernicusDownloadError("L'extensió final està desalineada.")
    validate_fragment_partition(plan)
    return validation
