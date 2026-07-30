"""Pure geographic models and exact EPSG:3035 fragment planning."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

from pyproj import Geod, Transformer

from TerraLab.data.crs import PYPROJ_TRANSFORMER_LOCK
from .constants import (
    CRS_PRODUCT,
    CRS_WGS84,
    DEFAULT_FRAGMENT_HEIGHT,
    DEFAULT_FRAGMENT_WIDTH,
    MAX_FRAGMENT_SPAN_M,
    MAX_IMAGE_HEIGHT,
    MAX_IMAGE_WIDTH,
    NOMINAL_RESOLUTION_M,
    SERVICE_PIXEL_TYPE,
    _SERVICE_COVERAGE_COORDS,
)

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
