from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
import requests
from pyproj import Transformer
from rasterio.io import MemoryFile
from rasterio.crs import CRS
from rasterio.transform import from_origin

from TerraLab.data import copernicus_orthophoto as copernicus
from TerraLab.data.copernicus_orthophoto import (
    ArcGISImageServerClient,
    BBoxWgs84,
    CopernicusDownloadCancelled,
    CopernicusDownloadError,
    CopernicusOrthophotoManager,
    DownloadManifest,
    DownloadRequest,
    MAX_IMAGE_HEIGHT,
    MAX_IMAGE_WIDTH,
    ProjectedBounds,
    build_mosaic,
    assess_service_coverage,
    estimate_selection,
    format_bytes_dual,
    plan_fragments,
    transform_bounds_to_3035,
    validate_final_geotiff,
)


def _fixed_projected_bounds(monkeypatch, width_m: float, height_m: float):
    bounds = ProjectedBounds(
        4_000_000.0,
        3_000_000.0,
        4_000_000.0 + width_m,
        3_000_000.0 + height_m,
    )
    monkeypatch.setattr(
        copernicus,
        "transform_bounds_to_3035",
        lambda _bbox: bounds,
    )
    return bounds


def _tiff_bytes(
    fragment,
    *,
    pixel_type="U16",
    value=17,
    nodata=None,
    crs="EPSG:3035",
) -> bytes:
    dtype = "uint16" if pixel_type == "U16" else "uint8"
    data = np.full(
        (3, fragment.height_px, fragment.width_px),
        value,
        dtype=dtype,
    )
    transform = from_origin(
        fragment.bounds_3035.xmin,
        fragment.bounds_3035.ymax,
        fragment.bounds_3035.width / fragment.width_px,
        fragment.bounds_3035.height / fragment.height_px,
    )
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=fragment.width_px,
            height=fragment.height_px,
            count=3,
            dtype=dtype,
            crs=crs,
            transform=transform,
            nodata=nodata,
        ) as dataset:
            dataset.write(data)
        return memory.read()


def _write_fragment(
    path: Path, fragment, request, *, value=17, nodata=None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _tiff_bytes(
            fragment,
            pixel_type=request.transport_pixel_type,
            value=value,
            nodata=nodata,
        )
    )
    return path


def _write_u16_fragment_data(
    path: Path,
    fragment,
    data: np.ndarray,
    *,
    nodata=None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    source = np.asarray(data, dtype=np.uint16)
    assert source.shape == (3, fragment.height_px, fragment.width_px)
    transform = from_origin(
        fragment.bounds_3035.xmin,
        fragment.bounds_3035.ymax,
        fragment.bounds_3035.width / fragment.width_px,
        fragment.bounds_3035.height / fragment.height_px,
    )
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=fragment.width_px,
        height=fragment.height_px,
        count=3,
        dtype="uint16",
        crs="EPSG:3035",
        transform=transform,
        nodata=nodata,
    ) as dataset:
        dataset.write(source)
    return path


_ARCGIS_ETRS89_LAEA_WKT = """
PROJCS["ETRS89-extended / LAEA Europe",
GEOGCS["ETRS89",
DATUM["European_Terrestrial_Reference_System_1989",
SPHEROID["GRS 1980",6378137,298.257222101004,
AUTHORITY["EPSG","7019"]],AUTHORITY["EPSG","6258"]],
PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],
UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],
AUTHORITY["EPSG","4258"]],
PROJECTION["Lambert_Azimuthal_Equal_Area"],
PARAMETER["latitude_of_center",52],
PARAMETER["longitude_of_center",10],
PARAMETER["false_easting",4321000],
PARAMETER["false_northing",3210000],
UNIT["metre",1,AUTHORITY["EPSG","9001"]],
AXIS["Northing",NORTH],AXIS["Easting",EAST],
AUTHORITY["EPSG","3035"]]
""".strip()


class _Response:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        content_length: int | None = None,
        content_type: str = "image/tiff",
    ):
        self.content = content
        self.status_code = status_code
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(
                len(content) if content_length is None else content_length
            ),
        }
        self.text = ""
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.content), max(1, chunk_size)):
            yield self.content[offset : offset + chunk_size]

    def json(self):
        raise ValueError("not json")

    def close(self):
        self.closed = True


class _JsonResponse(_Response):
    def __init__(self, payload, *, status_code=200):
        self.payload = payload
        content = json.dumps(payload).encode("utf-8")
        super().__init__(
            content,
            status_code=status_code,
            content_type="application/json; charset=UTF-8",
        )
        self.text = content.decode("utf-8")

    def json(self):
        return self.payload


def _export_response(fragment, *, host="image.discomap.eea.europa.eu"):
    return _JsonResponse(
        {
            "href": f"https://{host}/arcgisoutput/export.tif",
            "width": fragment.width_px,
            "height": fragment.height_px,
            "extent": {
                "xmin": fragment.bounds_3035.xmin,
                "ymin": fragment.bounds_3035.ymin,
                "xmax": fragment.bounds_3035.xmax,
                "ymax": fragment.bounds_3035.ymax,
                "spatialReference": {"wkid": 3035},
            },
        }
    )


class _Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_bbox_is_explicitly_west_south_east_north():
    bbox = BBoxWgs84(west=-9.5, south=36.0, east=3.4, north=44.2)

    assert bbox.as_tuple() == (-9.5, 36.0, 3.4, 44.2)
    assert BBoxWgs84.from_tuple(bbox.as_tuple()) == bbox
    with pytest.raises(ValueError, match="west"):
        BBoxWgs84(3.4, 36.0, -9.5, 44.2)
    with pytest.raises(ValueError, match="south"):
        BBoxWgs84(-9.5, 44.2, 3.4, 36.0)


def test_transform_bounds_uses_wgs84_to_3035_with_densification():
    bbox = BBoxWgs84(-9.0, 36.0, 4.0, 44.0)
    expected = Transformer.from_crs(
        "EPSG:4326", "EPSG:3035", always_xy=True
    ).transform_bounds(*bbox.as_tuple(), densify_pts=21)

    actual = transform_bounds_to_3035(bbox)

    assert actual.as_tuple() == pytest.approx(expected, abs=1e-7)
    assert actual.width > 0.0
    assert actual.height > 0.0


def test_transform_accepts_selections_with_negative_longitudes():
    bounds = transform_bounds_to_3035(BBoxWgs84(-12.0, 38.0, -6.0, 44.0))

    assert all(math.isfinite(value) for value in bounds.as_tuple())
    assert bounds.xmin < bounds.xmax
    assert bounds.ymin < bounds.ymax


def test_estimator_uses_ceil_and_reports_u8_u16_and_fragment_count(
    monkeypatch,
):
    _fixed_projected_bounds(monkeypatch, 40_001.0, 40_009.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        resolution_m=10.0,
        pixel_type="U8",
        compression="LZ77",
    )

    estimate = estimate_selection(request)

    assert estimate.width_px == 4_001
    assert estimate.height_px == 4_001
    assert estimate.pixel_count == 4_001 * 4_001
    assert estimate.raw_u8_bytes == estimate.pixel_count * 3
    assert estimate.raw_u16_bytes == estimate.pixel_count * 3 * 2
    assert estimate.width_m == 40_010.0
    assert estimate.height_m == 40_010.0
    assert estimate.fragment_count == 4
    assert (
        estimate.compressed_estimate_min_bytes
        <= estimate.compressed_estimate_bytes
        <= estimate.compressed_estimate_max_bytes
    )


def test_uncompressed_estimate_equals_selected_raw_size(monkeypatch):
    _fixed_projected_bounds(monkeypatch, 1_000.0, 2_000.0)

    u8 = estimate_selection(
        DownloadRequest(
            BBoxWgs84(0.0, 40.0, 1.0, 41.0),
            pixel_type="U8",
            compression="NONE",
        )
    )
    u16 = estimate_selection(
        DownloadRequest(
            BBoxWgs84(0.0, 40.0, 1.0, 41.0),
            pixel_type="U16",
            compression="NONE",
        )
    )

    assert u8.compressed_estimate_bytes == u8.raw_u8_bytes
    assert u16.compressed_estimate_bytes == u16.raw_u16_bytes


def test_format_bytes_dual_uses_decimal_and_binary_units():
    assert format_bytes_dual(1_000_000).startswith("1.00 MB")
    assert "0.95 MiB" in format_bytes_dual(1_000_000)
    assert format_bytes_dual(1_000_000_000).startswith("1.00 GB")
    assert "0.93 GiB" in format_bytes_dual(1_000_000_000)
    assert format_bytes_dual(1_000_000_000_000).startswith("1.00 TB")
    assert "0.91 TiB" in format_bytes_dual(1_000_000_000_000)


def test_fragment_plan_is_exactly_aligned_by_integer_pixel_offsets(
    monkeypatch,
):
    _fixed_projected_bounds(monkeypatch, 80_010.0, 40_010.0)
    request = DownloadRequest(BBoxWgs84(0.0, 40.0, 1.0, 41.0))

    plan = plan_fragments(request)

    assert plan.fragment_count == 6
    assert [(item.width_px, item.height_px) for item in plan.fragments] == [
        (4_000, 4_000),
        (4_000, 4_000),
        (1, 4_000),
        (4_000, 1),
        (4_000, 1),
        (1, 1),
    ]
    for fragment in plan.fragments:
        assert fragment.bounds_3035.width == pytest.approx(
            fragment.width_px * request.resolution_m
        )
        assert fragment.bounds_3035.height == pytest.approx(
            fragment.height_px * request.resolution_m
        )
        assert fragment.width_px <= 4_000
        assert fragment.height_px <= 4_000

    first, second = plan.fragments[0], plan.fragments[1]
    assert first.bounds_3035.xmax == second.bounds_3035.xmin
    lower_left = plan.fragments[-3]
    assert first.bounds_3035.ymin == lower_left.bounds_3035.ymax


def test_fragment_planner_supports_exact_service_dimension_limits(monkeypatch):
    _fixed_projected_bounds(
        monkeypatch,
        (MAX_IMAGE_WIDTH + 1) * 10.0,
        (MAX_IMAGE_HEIGHT + 1) * 10.0,
    )
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        tile_width_px=MAX_IMAGE_WIDTH,
        tile_height_px=MAX_IMAGE_HEIGHT,
    )

    plan = plan_fragments(request)

    assert plan.fragments[0].width_px == MAX_IMAGE_WIDTH
    assert plan.fragments[0].height_px == MAX_IMAGE_HEIGHT
    assert max(item.width_px for item in plan.fragments) == MAX_IMAGE_WIDTH
    assert max(item.height_px for item in plan.fragments) == MAX_IMAGE_HEIGHT
    assert all(item.width_px <= MAX_IMAGE_WIDTH for item in plan.fragments)
    assert all(item.height_px <= MAX_IMAGE_HEIGHT for item in plan.fragments)


def test_coarse_resolution_caps_fragment_ground_span_for_mosaic_limit(
    monkeypatch,
):
    _fixed_projected_bounds(monkeypatch, 400_000.0, 400_000.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        resolution_m=80.0,
        tile_width_px=4_000,
        tile_height_px=4_000,
    )

    estimate = estimate_selection(request)
    plan = plan_fragments(request)

    expected_side = int(
        copernicus.MAX_FRAGMENT_SPAN_M // request.resolution_m
    )
    assert estimate.fragment_width_px == expected_side
    assert estimate.fragment_height_px == expected_side
    expected_count = math.ceil(estimate.width_px / expected_side) * math.ceil(
        estimate.height_px / expected_side
    )
    assert estimate.fragment_count == expected_count
    assert all(
        item.width_px * request.resolution_m
        <= copernicus.MAX_FRAGMENT_SPAN_M
        and item.height_px * request.resolution_m
        <= copernicus.MAX_FRAGMENT_SPAN_M
        for item in plan.fragments
    )


def test_bbox_intersection_detects_partial_and_outside_service_coverage():
    coverage = copernicus.SERVICE_COVERAGE_WGS84
    partial = BBoxWgs84(40.0, 65.0, 50.0, 75.0)
    outside = BBoxWgs84(80.0, 10.0, 90.0, 20.0)

    clipped = partial.intersection(coverage)

    assert clipped is not None
    assert clipped.west == 40.0
    assert clipped.east == pytest.approx(44.932709)
    assert outside.intersection(coverage) is None


def test_service_coverage_assessment_reports_inside_partial_and_outside():
    inside = assess_service_coverage(BBoxWgs84(0.0, 40.0, 1.0, 41.0))
    partial = assess_service_coverage(BBoxWgs84(40.0, 65.0, 50.0, 75.0))
    outside = assess_service_coverage(BBoxWgs84(80.0, 10.0, 90.0, 20.0))

    assert inside.relation == "inside"
    assert inside.covered_fraction == pytest.approx(1.0)
    assert partial.relation == "partial"
    assert 0.0 < partial.covered_fraction < 1.0
    assert partial.outside_fraction == pytest.approx(
        1.0 - partial.covered_fraction
    )
    assert outside.relation == "outside"
    assert outside.covered_fraction == 0.0


def test_request_json_round_trip_keeps_explicit_bbox_and_all_options():
    request = DownloadRequest(
        BBoxWgs84(-8.0, 38.0, -6.0, 40.0),
        resolution_m=40.0,
        pixel_type="U16",
        compression="NONE",
        output_name="selection.tif",
        tile_width_px=3_000,
        tile_height_px=2_000,
        max_concurrent_requests=3,
    )

    payload = request.to_dict()

    assert payload["bbox_wgs84"] == {
        "west": -8.0,
        "south": 38.0,
        "east": -6.0,
        "north": 40.0,
    }
    assert DownloadRequest.from_dict(payload) == request


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"resolution_m": 5.0}, "at least"),
        ({"pixel_type": "F32"}, "U8 or U16"),
        ({"compression": "JPEG"}, "NONE or LZ77"),
        ({"tile_width_px": 15_001}, "tile_width"),
        ({"tile_height_px": 4_101}, "tile_height"),
    ],
)
def test_request_rejects_values_outside_product_and_service_contract(
    changes, message
):
    values = {
        "bbox_wgs84": BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        **changes,
    }
    with pytest.raises(ValueError, match=message):
        DownloadRequest(**values)


def test_export_image_params_use_native_u16_transport_and_lz77(
    monkeypatch,
):
    _fixed_projected_bounds(monkeypatch, 100.0, 100.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        pixel_type="U8",
        compression="NONE",
    )
    fragment = plan_fragments(request).fragments[0]

    params = ArcGISImageServerClient.export_params(fragment, request)

    assert request.pixel_type == "U8"
    assert request.transport_pixel_type == "U16"
    assert params == {
        "bbox": ",".join(
            f"{value:.12g}" for value in fragment.bounds_3035.as_tuple()
        ),
        "bboxSR": "3035",
        "imageSR": "3035",
        "size": f"{fragment.width_px},{fragment.height_px}",
        "format": "tiff",
        "pixelType": "U16",
        "interpolation": "RSP_BilinearInterpolation",
        "compression": "LZ77",
        "adjustAspectRatio": "false",
        "f": "json",
    }


def test_client_retries_streams_and_validates_real_geotiff(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 40.0, 30.0)
    request = DownloadRequest(BBoxWgs84(0.0, 40.0, 1.0, 41.0))
    fragment = plan_fragments(request).fragments[0]
    content = _tiff_bytes(fragment)
    session = _Session(
        [
            requests.ConnectionError("temporary"),
            _export_response(fragment, host="example.test"),
            _Response(content),
        ]
    )
    sleeps = []
    client = ArcGISImageServerClient(
        "https://example.test/ImageServer",
        session=session,
        max_retries=1,
        retry_sleep=sleeps.append,
        chunk_size=64 * 1024,
    )
    events = []

    result = client.download_fragment(
        fragment,
        request,
        tmp_path / "fragment.tif",
        progress=lambda downloaded, total: events.append(
            (downloaded, total)
        ),
    )

    assert result.is_file()
    assert len(session.calls) == 3
    assert session.calls[1][0].endswith("/exportImage")
    assert session.calls[1][1]["params"]["f"] == "json"
    assert session.calls[2][0] == (
        "https://example.test/arcgisoutput/export.tif"
    )
    assert session.calls[2][1]["params"] == {}
    assert sum(sleeps) == pytest.approx(1.0)
    assert events[-1] == (len(content), len(content))
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize(
    "descriptor, expected_message",
    [
        (
            {
                "href": "https://attacker.example/export.tif",
                "width": 4,
                "height": 3,
            },
            "no fiable",
        ),
        (
            {
                "href": (
                    "https://image.discomap.eea.europa.eu/"
                    "arcgisoutput/export.tif"
                ),
                "width": 5,
                "height": 3,
            },
            "dimensions",
        ),
        (
            {"width": 4, "height": 3},
            "cap URL",
        ),
    ],
)
def test_export_descriptor_rejects_untrusted_or_inconsistent_href(
    tmp_path, monkeypatch, descriptor, expected_message
):
    _fixed_projected_bounds(monkeypatch, 40.0, 30.0)
    request = DownloadRequest(BBoxWgs84(0.0, 40.0, 1.0, 41.0))
    fragment = plan_fragments(request).fragments[0]
    client = ArcGISImageServerClient(
        session=_Session([_JsonResponse(descriptor)]),
        max_retries=0,
    )

    with pytest.raises(CopernicusDownloadError, match=expected_message):
        client.download_fragment(
            fragment, request, tmp_path / "invalid-descriptor.tif"
        )


def test_arcgis_etrs89_extended_laea_wkt_is_accepted_as_epsg_3035(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 40.0, 30.0)
    request = DownloadRequest(BBoxWgs84(0.0, 40.0, 1.0, 41.0))
    fragment = plan_fragments(request).fragments[0]
    arcgis_crs = CRS.from_wkt(_ARCGIS_ETRS89_LAEA_WKT)
    path = tmp_path / "arcgis-wkt.tif"
    path.write_bytes(_tiff_bytes(fragment, crs=arcgis_crs))

    # The fallback remains covered even on GDAL versions that recover EPSG
    # from this WKT automatically.
    class _AuthoritylessArcGISCrs:
        def to_epsg(self):
            return None

        def to_authority(self):
            return None

        def to_dict(self):
            return {
                "proj": "laea",
                "lat_0": 52,
                "lon_0": 10,
                "x_0": 4_321_000,
                "y_0": 3_210_000,
                "ellps": "GRS80",
                "units": "m",
            }

    assert copernicus._is_epsg_3035(_AuthoritylessArcGISCrs())
    validation = copernicus.validate_fragment_raster(
        path, fragment, request
    )
    assert validation.width_px == fragment.width_px
    assert copernicus._is_epsg_3035(arcgis_crs)


def test_client_rejects_truncated_or_non_raster_http_content(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 40.0, 30.0)
    request = DownloadRequest(BBoxWgs84(0.0, 40.0, 1.0, 41.0))
    fragment = plan_fragments(request).fragments[0]
    content = _tiff_bytes(fragment)
    truncated = ArcGISImageServerClient(
        session=_Session(
            [
                _export_response(fragment),
                _Response(content, content_length=len(content) + 1),
            ]
        ),
        max_retries=0,
    )
    with pytest.raises(CopernicusDownloadError, match="truncada"):
        truncated.download_fragment(
            fragment, request, tmp_path / "truncated.tif"
        )

    json_response = _Response(
        b'{"error":{"message":"bad request"}}',
        content_type="application/json",
    )
    json_response.text = '{"error":{"message":"bad request"}}'
    invalid = ArcGISImageServerClient(
        session=_Session([_export_response(fragment), json_response]),
        max_retries=0,
    )
    with pytest.raises(CopernicusDownloadError, match="raster"):
        invalid.download_fragment(
            fragment, request, tmp_path / "invalid.tif"
        )


def test_client_sanitizes_html_500_and_emits_request_diagnostics(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 40.0, 30.0)
    request = DownloadRequest(BBoxWgs84(0.0, 40.0, 1.0, 41.0))
    fragment = plan_fragments(request).fragments[0]
    response = _Response(
        b"<html><body><img src='broken.png'>"
        b"<h1>Our apologies, something went wrong!</h1></body></html>",
        status_code=500,
        content_type="text/html",
    )
    response.text = response.content.decode("utf-8")
    diagnostics = []
    client = ArcGISImageServerClient(
        session=_Session([response]),
        max_retries=0,
        diagnostic_callback=diagnostics.append,
    )

    with pytest.raises(CopernicusDownloadError) as failure:
        client.download_fragment(
            fragment, request, tmp_path / "server-error.tif"
        )

    message = str(failure.value)
    assert "HTTP 500" in message
    assert "error temporal del servidor oficial" in message
    assert "Our apologies" in message
    assert "<html>" not in message
    assert "<img" not in message
    assert any(f"fragment={fragment.id}" in item for item in diagnostics)
    assert any("status=500" in item for item in diagnostics)


def test_manager_writes_persistent_request_and_transport_log(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 40.0, 30.0)
    request = DownloadRequest(BBoxWgs84(0.0, 40.0, 1.0, 41.0))
    fragment = plan_fragments(request).fragments[0]
    response = _Response(
        b"<html><body>temporary upstream failure</body></html>",
        status_code=503,
        content_type="text/html",
    )
    response.text = response.content.decode("utf-8")
    client = ArcGISImageServerClient(
        session=_Session([response]),
        max_retries=0,
    )
    log_path = tmp_path / "logs" / "copernicus_last.log"
    manager = CopernicusOrthophotoManager(
        tmp_path / "downloads",
        tmp_path / "dataset",
        client=client,
        diagnostic_log_path=log_path,
    )

    with pytest.raises(CopernicusDownloadError):
        manager.run(request)

    log_text = log_path.read_text(encoding="utf-8")
    assert "start service=" in log_text
    assert '"bbox_wgs84"' in log_text
    assert "plan fragments=1" in log_text
    assert f"fragment={fragment.id}" in log_text
    assert "status=503" in log_text


def test_retry_backoff_is_cooperatively_cancellable(tmp_path, monkeypatch):
    _fixed_projected_bounds(monkeypatch, 40.0, 30.0)
    request = DownloadRequest(BBoxWgs84(0.0, 40.0, 1.0, 41.0))
    fragment = plan_fragments(request).fragments[0]
    cancelled = {"value": False}

    def sleep(_seconds):
        cancelled["value"] = True

    client = ArcGISImageServerClient(
        session=_Session([requests.ConnectionError("offline")]),
        max_retries=3,
        retry_sleep=sleep,
    )
    with pytest.raises(CopernicusDownloadCancelled):
        client.download_fragment(
            fragment,
            request,
            tmp_path / "cancelled.tif",
            cancelled=lambda: cancelled["value"],
        )
    assert not (tmp_path / "cancelled.tif").exists()


def test_nodata_preflight_labels_all_zero_rgb_as_an_estimate(monkeypatch):
    _fixed_projected_bounds(monkeypatch, 160.0, 160.0)
    request = DownloadRequest(BBoxWgs84(0.0, 40.0, 1.0, 41.0))
    sample_fragment = copernicus.Fragment(
        index=0,
        row_index=0,
        column_index=0,
        row_offset=0,
        column_offset=0,
        width_px=16,
        height_px=16,
        bounds_3035=ProjectedBounds(
            4_000_000.0, 3_000_000.0, 4_000_160.0, 3_000_160.0
        ),
    )
    client = ArcGISImageServerClient(
        session=_Session(
            [
                _export_response(sample_fragment),
                _Response(_tiff_bytes(sample_fragment, value=0)),
            ]
        ),
        max_retries=0,
    )

    estimate = copernicus.estimate_nodata_fraction(
        request, sample_size=16, client=client
    )

    assert estimate.fraction == 1.0
    assert estimate.sampled_pixels == 256
    assert estimate.zero_rgb_pixels == 256
    assert "proxy" in estimate.method


def test_manifest_persists_valid_fragments_and_rejects_corruption(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 40.0, 30.0)
    request = DownloadRequest(BBoxWgs84(0.0, 40.0, 1.0, 41.0))
    plan = plan_fragments(request)
    fragment = plan.fragments[0]
    root = tmp_path / "fragments"
    manifest_path = tmp_path / "manifest.json"
    manifest = DownloadManifest(manifest_path, plan)
    path = _write_fragment(
        manifest.fragment_path(fragment, root),
        fragment,
        request,
    )
    manifest.mark_complete(fragment, path)

    reopened = DownloadManifest(manifest_path, plan)

    assert reopened.is_complete(fragment, root)
    payload = copernicus.json.loads(manifest_path.read_text(encoding="utf-8"))
    record = payload["fragments"][fragment.id]
    assert len(record["sha256"]) == 64
    assert record["size_bytes"] == path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b"corruption")
    assert not reopened.is_complete(fragment, root)
    assert not list(tmp_path.glob("*.tmp"))


def test_out_of_core_mosaic_preserves_fragment_alignment_and_validates(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 40.0, 40.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        pixel_type="U16",
        tile_width_px=2,
        tile_height_px=2,
    )
    plan = plan_fragments(request)
    root = tmp_path / "fragments"
    manifest = DownloadManifest(tmp_path / "manifest.json", plan)
    for fragment in plan.fragments:
        path = _write_fragment(
            manifest.fragment_path(fragment, root),
            fragment,
            request,
            value=fragment.index + 1,
        )
        manifest.mark_complete(fragment, path)

    output = build_mosaic(
        plan,
        manifest,
        root,
        tmp_path / "mosaic.tif",
    )
    validation = validate_final_geotiff(output, plan)

    assert validation.crs == "EPSG:3035"
    assert validation.tiled is True
    with rasterio.open(output) as dataset:
        data = dataset.read(1)
        assert [item.name for item in dataset.colorinterp] == [
            "red",
            "green",
            "blue",
        ]
        assert data.tolist() == [
            [1, 1, 2, 2],
            [1, 1, 2, 2],
            [3, 3, 4, 4],
            [3, 3, 4, 4],
        ]
        assert dataset.transform.a == 10.0
        assert dataset.transform.e == -10.0


def test_u8_output_uses_one_global_stretch_for_u16_values_above_255(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 80.0, 10.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        pixel_type="U8",
        tile_width_px=4,
        tile_height_px=1,
    )
    plan = plan_fragments(request)
    root = tmp_path / "fragments"
    manifest = DownloadManifest(tmp_path / "manifest.json", plan)
    selection_values = np.arange(1_000, 9_000, 1_000, dtype=np.uint16)
    for fragment in plan.fragments:
        start = fragment.column_offset
        values = selection_values[start : start + fragment.width_px]
        data = np.broadcast_to(
            values[np.newaxis, np.newaxis, :],
            (3, fragment.height_px, fragment.width_px),
        ).copy()
        path = _write_u16_fragment_data(
            manifest.fragment_path(fragment, root),
            fragment,
            data,
        )
        manifest.mark_complete(fragment, path)

    output = build_mosaic(
        plan, manifest, root, tmp_path / "global-u8.tif"
    )
    validate_final_geotiff(output, plan)

    with rasterio.open(output) as dataset:
        converted = dataset.read(1)[0]
        assert dataset.dtypes == ("uint8", "uint8", "uint8")
        assert int(converted.min()) == 0
        assert int(converted.max()) == 255
        assert np.all(np.diff(converted.astype(np.int16)) >= 0)
        # The shared transform continues across the fragment boundary. A
        # per-fragment stretch would restart the second fragment at zero.
        assert int(converted[3]) < int(converted[4])
        assert dataset.tags()["source_pixel_type"] == "U16"
        assert dataset.tags()["output_pixel_type"] == "U8"
    radiometry = copernicus.read_mosaic_radiometric_metadata(output)
    assert radiometry["method"] == copernicus.U8_STRETCH_METHOD
    assert radiometry["scope"] == "selection"
    assert radiometry["low"] == [1_000, 1_000, 1_000]
    assert radiometry["high"] == [7_000, 7_000, 7_000]
    with rasterio.open(output, "r+") as dataset:
        dataset.update_tags(radiometric_conversion="")
    with pytest.raises(CopernicusDownloadError, match="radiom"):
        validate_final_geotiff(output, plan)


def test_u16_output_preserves_native_values_without_radiometric_conversion(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 20.0, 10.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        pixel_type="U16",
    )
    plan = plan_fragments(request)
    fragment = plan.fragments[0]
    values = np.array([1_234, 54_321], dtype=np.uint16)
    data = np.broadcast_to(values, (3, 1, 2)).copy()
    root = tmp_path / "fragments"
    manifest = DownloadManifest(tmp_path / "manifest.json", plan)
    path = _write_u16_fragment_data(
        manifest.fragment_path(fragment, root), fragment, data
    )
    manifest.mark_complete(fragment, path)

    output = build_mosaic(
        plan, manifest, root, tmp_path / "native-u16.tif"
    )

    with rasterio.open(output) as dataset:
        assert dataset.dtypes == ("uint16", "uint16", "uint16")
        assert dataset.read(1).tolist() == [[1_234, 54_321]]
    radiometry = copernicus.read_mosaic_radiometric_metadata(output)
    assert radiometry == {
        "method": "none",
        "source_pixel_type": "U16",
        "output_pixel_type": "U16",
    }


def test_mosaic_preserves_common_nodata_and_keeps_none_when_unpublished(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 20.0, 10.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        tile_width_px=1,
        tile_height_px=1,
    )
    plan = plan_fragments(request)
    root = tmp_path / "fragments"
    manifest = DownloadManifest(tmp_path / "manifest.json", plan)
    for fragment in plan.fragments:
        path = _write_fragment(
            manifest.fragment_path(fragment, root),
            fragment,
            request,
            nodata=0,
        )
        manifest.mark_complete(fragment, path)
    with_nodata = build_mosaic(
        plan, manifest, root, tmp_path / "with-nodata.tif"
    )
    with rasterio.open(with_nodata) as dataset:
        assert dataset.nodata == 0

    no_nodata_root = tmp_path / "no-nodata-fragments"
    no_nodata_manifest = DownloadManifest(
        tmp_path / "no-nodata-manifest.json", plan
    )
    for fragment in plan.fragments:
        path = _write_fragment(
            no_nodata_manifest.fragment_path(fragment, no_nodata_root),
            fragment,
            request,
            nodata=None,
        )
        no_nodata_manifest.mark_complete(fragment, path)
    no_nodata = build_mosaic(
        plan,
        no_nodata_manifest,
        no_nodata_root,
        tmp_path / "without-nodata.tif",
    )
    with rasterio.open(no_nodata) as dataset:
        assert dataset.nodata is None


class _SyntheticClient:
    def __init__(self):
        self.calls = []

    def download_fragment(
        self,
        fragment,
        request,
        target_path,
        *,
        progress=None,
        cancelled=None,
    ):
        if cancelled is not None and cancelled():
            raise CopernicusDownloadCancelled("cancelled")
        self.calls.append(fragment.id)
        path = _write_fragment(
            Path(target_path),
            fragment,
            request,
            value=fragment.index + 10,
        )
        if progress is not None:
            size = path.stat().st_size
            progress(size, size)
        return path


def test_manager_run_resumes_completed_fragments_and_records_final_output(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 40.0, 40.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        tile_width_px=2,
        tile_height_px=2,
        max_concurrent_requests=1,
    )
    client = _SyntheticClient()
    manager = CopernicusOrthophotoManager(
        tmp_path / "downloads",
        tmp_path / "datasets",
        client=client,
        disk_usage=lambda _path: SimpleNamespace(free=10**12),
    )
    progress = []

    first = manager.run(request, progress_callback=lambda p, m: progress.append((p, m)))
    calls_after_first = list(client.calls)
    second = manager.run(request)

    assert Path(first.output_path).is_file()
    assert len(calls_after_first) == 4
    assert client.calls == calls_after_first
    assert first.downloaded_fragments == 4
    assert first.reused_fragments == 0
    assert second.downloaded_fragments == 0
    assert second.reused_fragments == 4
    assert progress[-1][0] == 100.0
    manifest = copernicus.json.loads(
        Path(second.manifest_path).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "complete"
    assert len(manifest["output"]["sha256"]) == 64
    assert manifest["output"]["crs"] == "EPSG:3035"
    assert manifest["transport_pixel_type"] == "U16"
    assert manifest["output_pixel_type"] == "U8"
    assert (
        manifest["output"]["radiometric_conversion"]["method"]
        == copernicus.U8_STRETCH_METHOD
    )
    assert first.metadata["source_pixel_type"] == "U16"
    assert (
        first.metadata["radiometric_conversion"]["method"]
        == copernicus.U8_STRETCH_METHOD
    )


class _InterruptingClient(_SyntheticClient):
    def download_fragment(self, fragment, request, target_path, **kwargs):
        if self.calls:
            # Let the manager durably mark the first completed future before
            # the second one reports the intentional interruption.
            copernicus.time.sleep(0.05)
            raise CopernicusDownloadCancelled("intentional pause")
        return super().download_fragment(
            fragment, request, target_path, **kwargs
        )


def test_cancelled_manager_run_resumes_only_completed_fragments(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 40.0, 10.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        tile_width_px=2,
        tile_height_px=1,
        max_concurrent_requests=1,
    )
    first_client = _InterruptingClient()
    first_manager = CopernicusOrthophotoManager(
        tmp_path / "downloads",
        tmp_path / "datasets",
        client=first_client,
        disk_usage=lambda _path: SimpleNamespace(free=10**12),
    )

    with pytest.raises(CopernicusDownloadCancelled):
        first_manager.run(request)

    resumed_client = _SyntheticClient()
    resumed_manager = CopernicusOrthophotoManager(
        tmp_path / "downloads",
        tmp_path / "datasets",
        client=resumed_client,
        disk_usage=lambda _path: SimpleNamespace(free=10**12),
    )
    result = resumed_manager.run(request)

    assert result.reused_fragments == 1
    assert result.downloaded_fragments == 1
    assert len(resumed_client.calls) == 1
    assert Path(result.output_path).is_file()


def test_large_mosaic_generates_internal_overviews(tmp_path, monkeypatch):
    _fixed_projected_bounds(monkeypatch, 6_000.0, 6_000.0)
    request = DownloadRequest(BBoxWgs84(0.0, 40.0, 1.0, 41.0))
    plan = plan_fragments(request)
    root = tmp_path / "fragments"
    manifest = DownloadManifest(tmp_path / "manifest.json", plan)
    fragment = plan.fragments[0]
    path = _write_fragment(
        manifest.fragment_path(fragment, root),
        fragment,
        request,
    )
    manifest.mark_complete(fragment, path)

    output = build_mosaic(
        plan, manifest, root, tmp_path / "overview-mosaic.tif"
    )
    validation = validate_final_geotiff(output, plan)

    assert validation.overviews == (2,)


def test_manager_reuses_verified_final_without_disk_check_or_mosaic(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 20.0, 20.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        tile_width_px=1,
        tile_height_px=1,
        max_concurrent_requests=1,
    )
    client = _SyntheticClient()
    manager = CopernicusOrthophotoManager(
        tmp_path / "downloads",
        tmp_path / "datasets",
        client=client,
        disk_usage=lambda _path: SimpleNamespace(free=10**12),
    )
    first = manager.run(request)
    calls_after_first = list(client.calls)

    def unexpected(*_args, **_kwargs):
        pytest.fail("a valid final mosaic must bypass preflight and rebuilding")

    manager.disk_usage = unexpected
    monkeypatch.setattr(copernicus, "build_mosaic", unexpected)
    second = manager.run(request)

    assert second.output_path == first.output_path
    assert second.downloaded_fragments == 0
    assert second.reused_fragments == 4
    assert second.metadata["reused_final_output"] is True
    assert client.calls == calls_after_first


def test_manager_adopts_valid_final_missing_only_manifest_commit(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 20.0, 10.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        tile_width_px=1,
        tile_height_px=1,
        max_concurrent_requests=1,
    )
    manager = CopernicusOrthophotoManager(
        tmp_path / "downloads",
        tmp_path / "datasets",
        client=_SyntheticClient(),
        disk_usage=lambda _path: SimpleNamespace(free=10**12),
    )
    first = manager.run(request)
    manifest_path = Path(first.manifest_path)
    payload = copernicus.json.loads(
        manifest_path.read_text(encoding="utf-8")
    )
    payload.pop("status", None)
    payload.pop("output", None)
    manifest_path.write_text(
        copernicus.json.dumps(payload),
        encoding="utf-8",
    )

    def unexpected(*_args, **_kwargs):
        pytest.fail("a valid orphan final must be adopted without rebuilding")

    manager.disk_usage = unexpected
    monkeypatch.setattr(copernicus, "build_mosaic", unexpected)
    second = manager.run(request)

    assert second.output_path == first.output_path
    assert second.metadata["reused_final_output"] is True
    adopted = copernicus.json.loads(
        manifest_path.read_text(encoding="utf-8")
    )
    assert adopted["status"] == "complete"
    assert len(adopted["output"]["sha256"]) == 64


def test_resume_adopts_unrecorded_fragments_before_space_preflight(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 40.0, 40.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        tile_width_px=2,
        tile_height_px=2,
        max_concurrent_requests=1,
    )
    client = _SyntheticClient()
    first_manager = CopernicusOrthophotoManager(
        tmp_path / "downloads",
        tmp_path / "datasets",
        client=client,
        disk_usage=lambda _path: SimpleNamespace(free=10**12),
    )
    first = first_manager.run(request)
    Path(first.output_path).unlink()
    manifest_path = Path(first.manifest_path)
    payload = copernicus.json.loads(
        manifest_path.read_text(encoding="utf-8")
    )
    payload["fragments"] = {}
    manifest_path.write_text(
        copernicus.json.dumps(payload),
        encoding="utf-8",
    )

    plan = plan_fragments(request)
    resumed_requirement = CopernicusOrthophotoManager.required_working_space(
        plan.estimate,
        pending_fragment_bytes=0,
    )
    full_requirement = CopernicusOrthophotoManager.required_working_space(
        plan.estimate
    )
    assert resumed_requirement < full_requirement

    calls_before_resume = list(client.calls)

    def resumed_disk_usage(_path):
        durable = copernicus.json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
        assert len(durable["fragments"]) == plan.fragment_count
        return SimpleNamespace(free=resumed_requirement)

    resumed_manager = CopernicusOrthophotoManager(
        tmp_path / "downloads",
        tmp_path / "datasets",
        client=client,
        disk_usage=resumed_disk_usage,
    )
    result = resumed_manager.run(request)

    assert result.downloaded_fragments == 0
    assert result.reused_fragments == plan.fragment_count
    assert client.calls == calls_before_resume
    assert Path(result.output_path).is_file()


class _SiblingFailureClient:
    def __init__(self):
        self.fragment_published = copernicus.threading.Event()

    def download_fragment(
        self,
        fragment,
        request,
        target_path,
        *,
        progress=None,
        cancelled=None,
    ):
        if fragment.index == 0:
            path = _write_fragment(
                Path(target_path),
                fragment,
                request,
                value=31,
            )
            self.fragment_published.set()
            copernicus.time.sleep(0.1)
            return path
        assert self.fragment_published.wait(timeout=1.0)
        raise CopernicusDownloadError("intentional sibling failure")


def test_failed_concurrent_run_adopts_tiff_published_by_sibling(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 40.0, 10.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        tile_width_px=2,
        tile_height_px=1,
        max_concurrent_requests=2,
    )
    failing_manager = CopernicusOrthophotoManager(
        tmp_path / "downloads",
        tmp_path / "datasets",
        client=_SiblingFailureClient(),
        disk_usage=lambda _path: SimpleNamespace(free=10**12),
    )

    with pytest.raises(CopernicusDownloadError, match="sibling failure"):
        failing_manager.run(request)

    manifest_path = next((tmp_path / "downloads").glob("*/manifest.json"))
    payload = copernicus.json.loads(
        manifest_path.read_text(encoding="utf-8")
    )
    assert payload["fragments"]["r00000_c00000"]["status"] == "complete"

    resumed_client = _SyntheticClient()
    resumed_manager = CopernicusOrthophotoManager(
        tmp_path / "downloads",
        tmp_path / "datasets",
        client=resumed_client,
        disk_usage=lambda _path: SimpleNamespace(free=10**12),
    )
    result = resumed_manager.run(request)

    assert result.reused_fragments == 1
    assert result.downloaded_fragments == 1
    assert resumed_client.calls == ["r00000_c00001"]


def test_stale_final_manifest_rebuilds_from_verified_fragments(
    tmp_path, monkeypatch
):
    _fixed_projected_bounds(monkeypatch, 20.0, 10.0)
    request = DownloadRequest(
        BBoxWgs84(0.0, 40.0, 1.0, 41.0),
        tile_width_px=1,
        tile_height_px=1,
        max_concurrent_requests=1,
    )
    client = _SyntheticClient()
    manager = CopernicusOrthophotoManager(
        tmp_path / "downloads",
        tmp_path / "datasets",
        client=client,
        disk_usage=lambda _path: SimpleNamespace(free=10**12),
    )
    first = manager.run(request)
    with Path(first.output_path).open("ab") as handle:
        handle.write(b"stale")

    original_build_mosaic = copernicus.build_mosaic
    rebuilt = []

    def recording_build(*args, **kwargs):
        rebuilt.append(True)
        return original_build_mosaic(*args, **kwargs)

    monkeypatch.setattr(copernicus, "build_mosaic", recording_build)
    result = manager.run(request)

    assert rebuilt == [True]
    assert result.downloaded_fragments == 0
    assert result.reused_fragments == 2
    assert result.metadata["reused_final_output"] is False
    validate_final_geotiff(result.output_path, plan_fragments(request))
