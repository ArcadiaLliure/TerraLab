"""Copernicus HRIM 2018 orthophoto download primitives.

The pure models in this module deliberately keep coordinate order explicit:
geographic rectangles are always ``(west, south, east, north)`` and therefore
always longitude/latitude.  Raster planning happens in EPSG:3035 and uses
integer pixel offsets, so every fragment shares one exact grid.
"""

from __future__ import annotations

import math
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

import numpy as np
import requests
from pyproj import CRS as PyprojCRS
from pyproj import Geod, Transformer

from TerraLab.common.locks import RASTERIO_LOCK
from TerraLab.terrain.crs import PYPROJ_TRANSFORMER_LOCK


PRODUCT_URL = (
    "https://land.copernicus.eu/en/products/european-image-mosaic/"
    "high-resolution-image-mosaic-2018-true-colour-10m"
)
IMAGE_SERVER_URL = (
    "https://image.discomap.eea.europa.eu/arcgis/rest/services/"
    "GioLand/HRIM_HR_TrueColour_2018/ImageServer"
)
WMS_URL = (
    "https://image.discomap.eea.europa.eu/arcgis/services/"
    "GioLand/HRIM_HR_TrueColour_2018/ImageServer/WMSServer"
)
DATA_POLICY_URL = "https://land.copernicus.eu/en/data-policy"

PRODUCT_NAME = "High Resolution Image Mosaic 2018 True Colour, 10 m"
ATTRIBUTION = (
    "European Union's Copernicus Land Monitoring Service information. "
    "High Resolution Image Mosaic 2018 True Colour, 10 m."
)
ADAPTED_ATTRIBUTION = (
    "Generated using European Union's Copernicus Land Monitoring Service "
    "information. Data adapted by TerraLab."
)

CRS_WGS84 = "EPSG:4326"
CRS_PRODUCT = "EPSG:3035"
NOMINAL_RESOLUTION_M = 10.0
_SERVICE_COVERAGE_COORDS = (
    -31.385193,
    27.546328,
    44.932709,
    71.274744,
)

MAX_IMAGE_WIDTH = 15_000
MAX_IMAGE_HEIGHT = 4_100
MAX_MOSAIC_IMAGE_COUNT = 20
DEFAULT_FRAGMENT_WIDTH = 4_000
DEFAULT_FRAGMENT_HEIGHT = 4_000
# ``pixelType=U8`` on this ImageServer is a numeric cast, not a display
# rendering: the native U16 values are clipped at 255 and the result is
# effectively white.  TerraLab therefore always transports the native U16
# samples and performs one selection-wide conversion when an U8 output was
# requested.
SERVICE_PIXEL_TYPE = "U16"
U8_STRETCH_LOW_PERCENTILE = 2.0
U8_STRETCH_HIGH_PERCENTILE = 98.0
U8_STRETCH_METHOD = "selection_global_per_band_percentile_2_98"
DOWNLOAD_PIPELINE_VERSION = 2
# The service refuses mosaics above 20 source images.  Real queries against
# this product found 200 km blocks intersecting 25 Sentinel-2 tiles, while
# 150 km blocks remained below the limit (15 in the sampled worst cases) and
# still permit explicit validation at the documented 15,000-pixel width when
# exporting at 10 m.  Keep this physical ceiling in addition to the 4,000-pixel
# default so coarse-resolution exports cannot silently use 200+ km requests.
MAX_FRAGMENT_SPAN_M = 150_000.0


@dataclass(frozen=True)
class BBoxWgs84:
    """An unambiguous EPSG:4326 rectangle in longitude/latitude order."""

    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        values = tuple(
            float(value)
            for value in (self.west, self.south, self.east, self.north)
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("WGS84 bounds must contain finite coordinates")
        west, south, east, north = values
        if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
            raise ValueError("WGS84 longitudes must be within [-180, 180]")
        if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
            raise ValueError("WGS84 latitudes must be within [-90, 90]")
        if west >= east:
            raise ValueError("west must be smaller than east")
        if south >= north:
            raise ValueError("south must be smaller than north")
        object.__setattr__(self, "west", west)
        object.__setattr__(self, "south", south)
        object.__setattr__(self, "east", east)
        object.__setattr__(self, "north", north)

    @classmethod
    def from_tuple(
        cls, bbox_wgs84: tuple[float, float, float, float]
    ) -> "BBoxWgs84":
        """Build from exactly ``(west, south, east, north)``."""

        if len(bbox_wgs84) != 4:
            raise ValueError("bbox_wgs84 must contain four coordinates")
        return cls(*bbox_wgs84)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.west, self.south, self.east, self.north

    def __iter__(self):
        return iter(self.as_tuple())

    def __len__(self) -> int:
        return 4

    def to_dict(self) -> dict[str, float]:
        return {
            "west": self.west,
            "south": self.south,
            "east": self.east,
            "north": self.north,
        }

    def intersects(self, other: "BBoxWgs84") -> bool:
        return not (
            self.east <= other.west
            or self.west >= other.east
            or self.north <= other.south
            or self.south >= other.north
        )

    def intersection(self, other: "BBoxWgs84") -> "BBoxWgs84 | None":
        west = max(self.west, other.west)
        south = max(self.south, other.south)
        east = min(self.east, other.east)
        north = min(self.north, other.north)
        if west >= east or south >= north:
            return None
        return BBoxWgs84(west, south, east, north)


SERVICE_COVERAGE_WGS84 = BBoxWgs84(*_SERVICE_COVERAGE_COORDS)


@dataclass(frozen=True)
class ProjectedBounds:
    """A projected rectangle ordered as xmin, ymin, xmax, ymax."""

    xmin: float
    ymin: float
    xmax: float
    ymax: float

    def __post_init__(self) -> None:
        values = tuple(
            float(value)
            for value in (self.xmin, self.ymin, self.xmax, self.ymax)
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Projected bounds must be finite")
        xmin, ymin, xmax, ymax = values
        if xmin >= xmax or ymin >= ymax:
            raise ValueError("Projected bounds must have positive dimensions")
        object.__setattr__(self, "xmin", xmin)
        object.__setattr__(self, "ymin", ymin)
        object.__setattr__(self, "xmax", xmax)
        object.__setattr__(self, "ymax", ymax)

    @property
    def width(self) -> float:
        return self.xmax - self.xmin

    @property
    def height(self) -> float:
        return self.ymax - self.ymin

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.xmin, self.ymin, self.xmax, self.ymax

    def to_dict(self) -> dict[str, float]:
        return {
            "xmin": self.xmin,
            "ymin": self.ymin,
            "xmax": self.xmax,
            "ymax": self.ymax,
        }


@dataclass(frozen=True)
class DownloadRequest:
    """One verifiable orthophoto selection and output encoding."""

    bbox_wgs84: BBoxWgs84
    resolution_m: float = NOMINAL_RESOLUTION_M
    pixel_type: str = "U8"
    compression: str = "LZ77"
    output_name: str = "copernicus_hrim_true_colour_2018.tif"
    tile_width_px: int = DEFAULT_FRAGMENT_WIDTH
    tile_height_px: int = DEFAULT_FRAGMENT_HEIGHT
    max_concurrent_requests: int = 2

    def __post_init__(self) -> None:
        bbox = self.bbox_wgs84
        if not isinstance(bbox, BBoxWgs84):
            try:
                bbox = BBoxWgs84.from_tuple(tuple(bbox))  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "bbox_wgs84 must be (west, south, east, north)"
                ) from exc
        resolution = float(self.resolution_m)
        if not math.isfinite(resolution) or resolution < NOMINAL_RESOLUTION_M:
            raise ValueError(
                "resolution_m must be finite and at least the native 10 m"
            )
        if resolution > 10_000.0:
            raise ValueError("resolution_m is disproportionately coarse")
        pixel_type = str(self.pixel_type or "").strip().upper()
        if pixel_type not in {"U8", "U16"}:
            raise ValueError("pixel_type must be U8 or U16")
        compression = str(self.compression or "NONE").strip().upper()
        if compression not in {"NONE", "LZ77"}:
            raise ValueError("compression must be NONE or LZ77")
        output_name = str(self.output_name or "").strip()
        if not output_name or output_name != output_name.split("/")[-1]:
            raise ValueError("output_name must be a plain file name")
        if "\\" in output_name:
            raise ValueError("output_name must be a plain file name")
        if not output_name.lower().endswith((".tif", ".tiff")):
            raise ValueError("The managed mosaic output must be GeoTIFF")
        tile_width = int(self.tile_width_px)
        tile_height = int(self.tile_height_px)
        if not (1 <= tile_width <= MAX_IMAGE_WIDTH):
            raise ValueError(
                f"tile_width_px must be within 1..{MAX_IMAGE_WIDTH}"
            )
        if not (1 <= tile_height <= MAX_IMAGE_HEIGHT):
            raise ValueError(
                f"tile_height_px must be within 1..{MAX_IMAGE_HEIGHT}"
            )
        concurrency = int(self.max_concurrent_requests)
        if not (1 <= concurrency <= 4):
            raise ValueError("max_concurrent_requests must be within 1..4")
        object.__setattr__(self, "bbox_wgs84", bbox)
        object.__setattr__(self, "resolution_m", resolution)
        object.__setattr__(self, "pixel_type", pixel_type)
        object.__setattr__(self, "compression", compression)
        object.__setattr__(self, "output_name", output_name)
        object.__setattr__(self, "tile_width_px", tile_width)
        object.__setattr__(self, "tile_height_px", tile_height)
        object.__setattr__(self, "max_concurrent_requests", concurrency)

    @property
    def bytes_per_pixel(self) -> int:
        return 6 if self.pixel_type == "U16" else 3

    @property
    def transport_pixel_type(self) -> str:
        """Pixel type requested from ArcGIS before local output conversion."""

        return SERVICE_PIXEL_TYPE

    @property
    def transport_bytes_per_pixel(self) -> int:
        return 6

    def to_dict(self) -> dict[str, object]:
        return {
            "bbox_wgs84": self.bbox_wgs84.to_dict(),
            "resolution_m": self.resolution_m,
            "pixel_type": self.pixel_type,
            "compression": self.compression,
            "output_name": self.output_name,
            "tile_width_px": self.tile_width_px,
            "tile_height_px": self.tile_height_px,
            "max_concurrent_requests": self.max_concurrent_requests,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DownloadRequest":
        raw_bbox = payload.get("bbox_wgs84")
        if isinstance(raw_bbox, dict):
            bbox = BBoxWgs84(
                west=float(raw_bbox["west"]),
                south=float(raw_bbox["south"]),
                east=float(raw_bbox["east"]),
                north=float(raw_bbox["north"]),
            )
        else:
            try:
                bbox = BBoxWgs84.from_tuple(tuple(raw_bbox))  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "bbox_wgs84 must contain west/south/east/north"
                ) from exc
        return cls(
            bbox_wgs84=bbox,
            resolution_m=float(
                payload.get("resolution_m", NOMINAL_RESOLUTION_M)
            ),
            pixel_type=str(payload.get("pixel_type", "U8")),
            compression=str(payload.get("compression", "LZ77")),
            output_name=str(
                payload.get(
                    "output_name",
                    "copernicus_hrim_true_colour_2018.tif",
                )
            ),
            tile_width_px=int(
                payload.get("tile_width_px", DEFAULT_FRAGMENT_WIDTH)
            ),
            tile_height_px=int(
                payload.get("tile_height_px", DEFAULT_FRAGMENT_HEIGHT)
            ),
            max_concurrent_requests=int(
                payload.get("max_concurrent_requests", 2)
            ),
        )


@dataclass(frozen=True)
class RasterGrid:
    """North-up EPSG:3035 grid shared by every planned fragment."""

    bounds: ProjectedBounds
    resolution_m: float
    width_px: int
    height_px: int


@dataclass(frozen=True)
class SelectionEstimate:
    """Dynamic storage and pixel metrics derived from one exact raster plan."""

    projected_bounds: ProjectedBounds
    grid_bounds: ProjectedBounds
    width_m: float
    height_m: float
    area_km2: float
    width_px: int
    height_px: int
    pixel_count: int
    raw_u16_bytes: int
    raw_u8_bytes: int
    selected_raw_bytes: int
    compressed_estimate_bytes: int
    compressed_estimate_min_bytes: int
    compressed_estimate_max_bytes: int
    fragment_count: int
    fragment_width_px: int
    fragment_height_px: int
    risk_level: str

    @property
    def width_km(self) -> float:
        return self.width_m / 1_000.0

    @property
    def height_km(self) -> float:
        return self.height_m / 1_000.0

    def to_dict(self) -> dict[str, object]:
        return {
            "projected_bounds": self.projected_bounds.to_dict(),
            "grid_bounds": self.grid_bounds.to_dict(),
            "width_m": self.width_m,
            "height_m": self.height_m,
            "width_km": self.width_km,
            "height_km": self.height_km,
            "area_km2": self.area_km2,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "pixel_count": self.pixel_count,
            "raw_u16_bytes": self.raw_u16_bytes,
            "raw_u8_bytes": self.raw_u8_bytes,
            "selected_raw_bytes": self.selected_raw_bytes,
            "compressed_estimate_bytes": self.compressed_estimate_bytes,
            "compressed_estimate_min_bytes": (
                self.compressed_estimate_min_bytes
            ),
            "compressed_estimate_max_bytes": (
                self.compressed_estimate_max_bytes
            ),
            "fragment_count": self.fragment_count,
            "fragment_width_px": self.fragment_width_px,
            "fragment_height_px": self.fragment_height_px,
            "risk_level": self.risk_level,
        }


@dataclass(frozen=True)
class Fragment:
    """One exportImage request, positioned by integer destination pixels."""

    index: int
    row_index: int
    column_index: int
    row_offset: int
    column_offset: int
    width_px: int
    height_px: int
    bounds_3035: ProjectedBounds

    @property
    def id(self) -> str:
        return f"r{self.row_index:05d}_c{self.column_index:05d}"


@dataclass(frozen=True)
class DownloadPlan:
    request: DownloadRequest
    estimate: SelectionEstimate
    grid: RasterGrid
    fragments: tuple[Fragment, ...]

    @property
    def fragment_count(self) -> int:
        return len(self.fragments)


@dataclass(frozen=True)
class CoverageAssessment:
    intersection: BBoxWgs84 | None
    covered_fraction: float
    outside_fraction: float
    relation: str


def _geodesic_bbox_area(bbox: BBoxWgs84) -> float:
    with PYPROJ_TRANSFORMER_LOCK:
        geod = Geod(ellps="WGS84")
        area, _perimeter = geod.polygon_area_perimeter(
            [bbox.west, bbox.east, bbox.east, bbox.west],
            [bbox.south, bbox.south, bbox.north, bbox.north],
        )
    return abs(float(area))


def assess_service_coverage(
    bbox_wgs84: BBoxWgs84,
) -> CoverageAssessment:
    """Classify a selection against the official advertised WMS coverage."""

    intersection = bbox_wgs84.intersection(SERVICE_COVERAGE_WGS84)
    if intersection is None:
        return CoverageAssessment(None, 0.0, 1.0, "outside")
    selected_area = _geodesic_bbox_area(bbox_wgs84)
    intersection_area = _geodesic_bbox_area(intersection)
    covered = (
        min(1.0, max(0.0, intersection_area / selected_area))
        if selected_area > 0.0
        else 0.0
    )
    relation = "inside" if math.isclose(covered, 1.0, abs_tol=1e-9) else "partial"
    return CoverageAssessment(
        intersection,
        covered,
        max(0.0, 1.0 - covered),
        relation,
    )


def service_coverage_fraction(bbox_wgs84: BBoxWgs84) -> float:
    return assess_service_coverage(bbox_wgs84).covered_fraction


def transform_bounds_to_3035(bbox_wgs84: BBoxWgs84) -> ProjectedBounds:
    """Densify and transform WGS84 bounds using explicit x/y axis order."""

    if not isinstance(bbox_wgs84, BBoxWgs84):
        raise TypeError("bbox_wgs84 must be a BBoxWgs84")
    with PYPROJ_TRANSFORMER_LOCK:
        transformer = Transformer.from_crs(
            CRS_WGS84,
            CRS_PRODUCT,
            always_xy=True,
        )
        result = transformer.transform_bounds(
            *bbox_wgs84.as_tuple(),
            densify_pts=21,
        )
    return ProjectedBounds(*result)


def _effective_fragment_shape(request: DownloadRequest) -> tuple[int, int]:
    physical_limit = max(
        1, int(math.floor(MAX_FRAGMENT_SPAN_M / request.resolution_m))
    )
    return (
        min(request.tile_width_px, MAX_IMAGE_WIDTH, physical_limit),
        min(request.tile_height_px, MAX_IMAGE_HEIGHT, physical_limit),
    )


def _grid_for(
    request: DownloadRequest,
    projected: ProjectedBounds,
) -> RasterGrid:
    resolution = request.resolution_m
    width = int(math.ceil(projected.width / resolution))
    height = int(math.ceil(projected.height / resolution))
    # Anchor on the transformed west/north edges.  East/south may expand by
    # less than one pixel; this makes every pixel exactly the requested size.
    aligned = ProjectedBounds(
        projected.xmin,
        projected.ymax - height * resolution,
        projected.xmin + width * resolution,
        projected.ymax,
    )
    return RasterGrid(aligned, resolution, width, height)


def _risk_level(estimated_bytes: int) -> str:
    if estimated_bytes < 1_000_000_000:
        return "normal"
    if estimated_bytes < 10_000_000_000:
        return "light_warning"
    if estimated_bytes < 50_000_000_000:
        return "important_warning"
    if estimated_bytes < 100_000_000_000:
        return "reinforced_confirmation"
    return "reduce_area_or_resolution"


def estimate_selection(request: DownloadRequest) -> SelectionEstimate:
    """Estimate metrics from the same aligned grid used by the downloader."""

    projected = transform_bounds_to_3035(request.bbox_wgs84)
    grid = _grid_for(request, projected)
    pixel_count = int(grid.width_px) * int(grid.height_px)
    raw_u16 = pixel_count * 3 * 2
    raw_u8 = pixel_count * 3
    selected_raw = raw_u16 if request.pixel_type == "U16" else raw_u8
    if request.compression == "NONE":
        compressed_min = compressed_max = selected_raw
    elif request.pixel_type == "U16":
        compressed_min = int(math.ceil(selected_raw * 0.45))
        compressed_max = int(math.ceil(selected_raw * 0.85))
    else:
        compressed_min = int(math.ceil(selected_raw * 0.55))
        compressed_max = int(math.ceil(selected_raw * 0.90))
    compressed_mid = (compressed_min + compressed_max) // 2
    fragment_width, fragment_height = _effective_fragment_shape(request)
    fragment_count = (
        int(math.ceil(grid.width_px / fragment_width))
        * int(math.ceil(grid.height_px / fragment_height))
    )
    width_m = grid.width_px * request.resolution_m
    height_m = grid.height_px * request.resolution_m
    return SelectionEstimate(
        projected_bounds=projected,
        grid_bounds=grid.bounds,
        width_m=width_m,
        height_m=height_m,
        area_km2=(width_m * height_m) / 1_000_000.0,
        width_px=grid.width_px,
        height_px=grid.height_px,
        pixel_count=pixel_count,
        raw_u16_bytes=raw_u16,
        raw_u8_bytes=raw_u8,
        selected_raw_bytes=selected_raw,
        compressed_estimate_bytes=compressed_mid,
        compressed_estimate_min_bytes=compressed_min,
        compressed_estimate_max_bytes=compressed_max,
        fragment_count=fragment_count,
        fragment_width_px=fragment_width,
        fragment_height_px=fragment_height,
        risk_level=_risk_level(compressed_mid),
    )


def format_bytes_dual(value: int | float) -> str:
    """Format one byte count with matching decimal and binary unit tiers."""

    size = max(0.0, float(value))
    tiers = (
        (1_000_000_000_000.0, 1024.0**4, "TB", "TiB"),
        (1_000_000_000.0, 1024.0**3, "GB", "GiB"),
        (1_000_000.0, 1024.0**2, "MB", "MiB"),
        (1_000.0, 1024.0, "kB", "KiB"),
    )
    for decimal, binary, decimal_name, binary_name in tiers:
        if size >= decimal:
            return (
                f"{size / decimal:.2f} {decimal_name} "
                f"({size / binary:.2f} {binary_name})"
            )
    return f"{int(round(size))} B"


def _iter_fragments(
    request: DownloadRequest,
    grid: RasterGrid,
) -> Iterator[Fragment]:
    tile_width, tile_height = _effective_fragment_shape(request)
    index = 0
    row_index = 0
    for row_offset in range(0, grid.height_px, tile_height):
        height = min(tile_height, grid.height_px - row_offset)
        column_index = 0
        for column_offset in range(0, grid.width_px, tile_width):
            width = min(tile_width, grid.width_px - column_offset)
            xmin = grid.bounds.xmin + column_offset * grid.resolution_m
            xmax = xmin + width * grid.resolution_m
            ymax = grid.bounds.ymax - row_offset * grid.resolution_m
            ymin = ymax - height * grid.resolution_m
            yield Fragment(
                index=index,
                row_index=row_index,
                column_index=column_index,
                row_offset=row_offset,
                column_offset=column_offset,
                width_px=width,
                height_px=height,
                bounds_3035=ProjectedBounds(xmin, ymin, xmax, ymax),
            )
            index += 1
            column_index += 1
        row_index += 1


def plan_fragments(request: DownloadRequest) -> DownloadPlan:
    """Build the exact EPSG:3035 fragment grid for ``request``."""

    estimate = estimate_selection(request)
    grid = RasterGrid(
        estimate.grid_bounds,
        request.resolution_m,
        estimate.width_px,
        estimate.height_px,
    )
    fragments = tuple(_iter_fragments(request, grid))
    if len(fragments) != estimate.fragment_count:
        raise AssertionError("Fragment estimator and planner disagree")
    return DownloadPlan(request, estimate, grid, fragments)


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
        pass
    try:
        authority = getattr(crs, "to_authority")()
        if authority and str(authority[0]).upper() == "EPSG":
            if int(authority[1]) == 3035:
                return True
    except Exception:
        pass
    try:
        with PYPROJ_TRANSFORMER_LOCK:
            candidate = PyprojCRS.from_user_input(crs)
            official = PyprojCRS.from_epsg(3035)
            if candidate.equals(official, ignore_axis_order=True):
                return True
    except Exception:
        pass
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
            pass

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
            pass
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


MANIFEST_SCHEMA_VERSION = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _plan_identity(plan: DownloadPlan) -> str:
    payload = {
        "pipeline_version": DOWNLOAD_PIPELINE_VERSION,
        "request": plan.request.to_dict(),
        "grid": {
            "bounds": plan.grid.bounds.to_dict(),
            "width_px": plan.grid.width_px,
            "height_px": plan.grid.height_px,
            "resolution_m": plan.grid.resolution_m,
        },
        "fragments": [
            {
                "id": fragment.id,
                "row_offset": fragment.row_offset,
                "column_offset": fragment.column_offset,
                "width_px": fragment.width_px,
                "height_px": fragment.height_px,
                "bounds": fragment.bounds_3035.to_dict(),
            }
            for fragment in plan.fragments
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DownloadManifest:
    """Atomic fragment-level state that survives application restarts."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        plan: DownloadPlan,
    ) -> None:
        self.path = Path(path).resolve(strict=False)
        self.plan = plan
        self.identity = _plan_identity(plan)
        self._lock = threading.RLock()
        self._payload: dict[str, object]
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CopernicusDownloadError(
                    f"Manifest de fragments invàlid: {self.path}"
                ) from exc
            if not isinstance(loaded, dict):
                raise CopernicusDownloadError("Manifest de fragments invàlid.")
            if str(loaded.get("plan_identity", "")) != self.identity:
                raise ManifestMismatchError(
                    "El manifest existent correspon a una altra selecció."
                )
            self._payload = loaded
        else:
            self._payload = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "pipeline_version": DOWNLOAD_PIPELINE_VERSION,
                "plan_identity": self.identity,
                "product": PRODUCT_NAME,
                "service_url": IMAGE_SERVER_URL,
                "transport_pixel_type": SERVICE_PIXEL_TYPE,
                "output_pixel_type": plan.request.pixel_type,
                "request": plan.request.to_dict(),
                "estimate": plan.estimate.to_dict(),
                "created_utc": _utc_now(),
                "updated_utc": _utc_now(),
                "fragments": {},
            }
            self._save_locked()

    def _records_locked(self) -> dict[str, object]:
        records = self._payload.setdefault("fragments", {})
        if not isinstance(records, dict):
            records = {}
            self._payload["fragments"] = records
        return records

    def _save_locked(self) -> None:
        self._payload["updated_utc"] = _utc_now()
        _atomic_json(self.path, self._payload)

    def fragment_path(self, fragment: Fragment, fragment_root: Path) -> Path:
        return fragment_root / f"{fragment.id}.tif"

    def is_complete(
        self,
        fragment: Fragment,
        fragment_root: str | os.PathLike[str],
    ) -> bool:
        root = Path(fragment_root)
        path = self.fragment_path(fragment, root)
        with self._lock:
            record = self._records_locked().get(fragment.id)
            if not isinstance(record, Mapping):
                return False
            if str(record.get("status", "")) != "complete":
                return False
            try:
                if int(record.get("size_bytes", -1)) != int(path.stat().st_size):
                    return False
            except OSError:
                return False
            expected_sha256 = str(record.get("sha256", "") or "")
        if expected_sha256:
            try:
                if _sha256_file(path) != expected_sha256:
                    return False
            except OSError:
                return False
        try:
            validate_fragment_raster(path, fragment, self.plan.request)
            return True
        except Exception:
            return False

    def mark_complete(self, fragment: Fragment, path: Path) -> None:
        with self._lock:
            self._records_locked()[fragment.id] = {
                "status": "complete",
                "path": str(path),
                "size_bytes": int(path.stat().st_size),
                "sha256": _sha256_file(path),
                "completed_utc": _utc_now(),
                "row_offset": fragment.row_offset,
                "column_offset": fragment.column_offset,
                "width_px": fragment.width_px,
                "height_px": fragment.height_px,
                "pixel_type": SERVICE_PIXEL_TYPE,
                "bounds_3035": fragment.bounds_3035.to_dict(),
            }
            self._save_locked()

    def adopt_existing(
        self,
        fragment: Fragment,
        fragment_root: str | os.PathLike[str],
    ) -> bool:
        """Durably adopt an unrecorded complete TIFF left by an interrupted run.

        ``download_fragment`` publishes a fragment with an atomic rename.  A
        sibling future can still fail after that rename but before the manager
        processes the successful future and records it.  Only an absent or
        incomplete manifest record is eligible for adoption: a recorded file
        whose size or digest changed must be downloaded again.
        """

        root = Path(fragment_root)
        path = self.fragment_path(fragment, root)
        with self._lock:
            record = self._records_locked().get(fragment.id)
            if (
                isinstance(record, Mapping)
                and str(record.get("status", "")) == "complete"
            ):
                return False
        if not path.is_file():
            return False
        try:
            validate_fragment_raster(path, fragment, self.plan.request)
        except Exception:
            return False
        self.mark_complete(fragment, path)
        return True

    def mark_final(
        self,
        path: Path,
        validation: RasterValidation,
    ) -> None:
        with self._lock:
            self._payload["status"] = "complete"
            self._payload["output"] = {
                "path": str(path),
                "size_bytes": int(path.stat().st_size),
                "sha256": _sha256_file(path),
                "width_px": validation.width_px,
                "height_px": validation.height_px,
                "band_count": validation.band_count,
                "pixel_type": validation.pixel_type,
                "source_pixel_type": SERVICE_PIXEL_TYPE,
                "radiometric_conversion": read_mosaic_radiometric_metadata(
                    path
                ),
                "crs": validation.crs,
                "bounds_3035": validation.bounds.to_dict(),
                "tiled": validation.tiled,
                "nodata": validation.nodata,
                "overviews": list(validation.overviews),
                "completed_utc": _utc_now(),
            }
            self._save_locked()

    def validated_final(
        self,
        expected_path: str | os.PathLike[str],
    ) -> RasterValidation | None:
        """Return a verified completed mosaic, or ``None`` if it is stale."""

        path = Path(expected_path).resolve(strict=False)
        with self._lock:
            if str(self._payload.get("status", "")) != "complete":
                return None
            record = self._payload.get("output")
            if not isinstance(record, Mapping):
                return None
            recorded_path = Path(str(record.get("path", ""))).resolve(
                strict=False
            )
            if recorded_path != path:
                return None
            try:
                if int(record.get("size_bytes", -1)) != int(
                    path.stat().st_size
                ):
                    return None
            except OSError:
                return None
            expected_sha256 = str(record.get("sha256", "") or "")
        if not expected_sha256:
            return None
        try:
            if _sha256_file(path) != expected_sha256:
                return None
            return validate_final_geotiff(path, self.plan)
        except Exception:
            return None

    def adopt_final(
        self,
        expected_path: str | os.PathLike[str],
    ) -> RasterValidation | None:
        """Adopt a valid mosaic published just before an interrupted manifest write."""

        path = Path(expected_path).resolve(strict=False)
        with self._lock:
            # A recorded output that no longer matches its size/digest is
            # stale or corrupted, never an adoption candidate.
            if str(self._payload.get("status", "")) == "complete" or isinstance(
                self._payload.get("output"), Mapping
            ):
                return None
        if not path.is_file():
            return None
        try:
            validation = validate_final_geotiff(path, self.plan)
        except Exception:
            return None
        self.mark_final(path, validation)
        return validation

    @property
    def completed_count(self) -> int:
        with self._lock:
            return sum(
                isinstance(record, Mapping)
                and str(record.get("status", "")) == "complete"
                for record in self._records_locked().values()
            )


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
                        pass
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


__all__ = [
    "ADAPTED_ATTRIBUTION",
    "ATTRIBUTION",
    "BBoxWgs84",
    "ArcGISImageServerClient",
    "CopernicusDownloadCancelled",
    "CopernicusDownloadError",
    "CopernicusOrthophotoManager",
    "CoverageAssessment",
    "CRS_PRODUCT",
    "CRS_WGS84",
    "DATA_POLICY_URL",
    "DEFAULT_FRAGMENT_HEIGHT",
    "DEFAULT_FRAGMENT_WIDTH",
    "DownloadPlan",
    "DownloadManifest",
    "DOWNLOAD_PIPELINE_VERSION",
    "DownloadRequest",
    "DownloadResult",
    "Fragment",
    "IMAGE_SERVER_URL",
    "InsufficientDownloadSpaceError",
    "MAX_FRAGMENT_SPAN_M",
    "MAX_IMAGE_HEIGHT",
    "MAX_IMAGE_WIDTH",
    "MAX_MOSAIC_IMAGE_COUNT",
    "NOMINAL_RESOLUTION_M",
    "NodataEstimate",
    "PRODUCT_NAME",
    "PRODUCT_URL",
    "ProjectedBounds",
    "RasterGrid",
    "RasterValidation",
    "SERVICE_PIXEL_TYPE",
    "SERVICE_COVERAGE_WGS84",
    "SelectionEstimate",
    "U8GlobalStretch",
    "U8_STRETCH_HIGH_PERCENTILE",
    "U8_STRETCH_LOW_PERCENTILE",
    "U8_STRETCH_METHOD",
    "WMS_URL",
    "assess_service_coverage",
    "build_mosaic",
    "estimate_nodata_fraction",
    "estimate_selection",
    "format_bytes_dual",
    "plan_fragments",
    "read_mosaic_radiometric_metadata",
    "service_coverage_fraction",
    "transform_bounds_to_3035",
    "validate_final_geotiff",
    "validate_fragment_partition",
    "validate_fragment_raster",
]
