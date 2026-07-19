from pathlib import Path
import tempfile

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin, rowcol

from TerraLab.terrain.light_pollution_sampler import LightPollutionSampler


TARGET_LAT = 42.52273
TARGET_LON = 1.00398
CRS_8857 = "EPSG:8857"
CRS_25831 = "EPSG:25831"
CRS_32631 = "EPSG:32631"


def _build_test_raster(
    path: Path, *, crs: str | None, pixel_size: float = 1000.0
) -> None:
    transform = from_origin(80000.0, 5204000.0, pixel_size, pixel_size)
    data = np.zeros((12, 12), dtype=np.float32)

    tr = Transformer.from_crs("EPSG:4326", CRS_8857, always_xy=True)
    x, y = tr.transform(TARGET_LON, TARGET_LAT)
    row, col = rowcol(transform, x, y)
    for rr in range(int(row) - 1, int(row) + 2):
        for cc in range(int(col) - 1, int(col) + 2):
            if 0 <= rr < data.shape[0] and 0 <= cc < data.shape[1]:
                data[rr, cc] = 7.0

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=data.shape[1],
        height=data.shape[0],
        count=1,
        dtype="float32",
        transform=transform,
        crs=crs,
        nodata=3.4e38,
    ) as dst:
        dst.write(data, 1)


def test_sampling_matches_same_physical_point_for_latlon_and_terrain_xy() -> None:
    with tempfile.TemporaryDirectory() as td:
        raster_path = Path(td) / "dvnl_8857.tif"
        _build_test_raster(raster_path, crs=CRS_8857)

        tr_to_terrain = Transformer.from_crs(
            "EPSG:4326", CRS_25831, always_xy=True
        )
        x_terrain, y_terrain = tr_to_terrain.transform(TARGET_LON, TARGET_LAT)

        sampler = LightPollutionSampler(str(raster_path), radius_km=1.0)
        sampler.prepare_region_from_terrain_xy(
            x_terrain=x_terrain,
            y_terrain=y_terrain,
            radius_m=4000.0,
            input_crs=CRS_25831,
        )

        rad_xy = sampler.get_radiance_terrain_xy(
            x_terrain=x_terrain,
            y_terrain=y_terrain,
            input_crs=CRS_25831,
        )
        rad_ll = sampler.get_radiance(TARGET_LAT, TARGET_LON)
        assert rad_xy == 7.0
        assert rad_ll == 7.0


def test_missing_crs_uses_explicit_fallback_policy_for_dvnl_extent() -> None:
    with tempfile.TemporaryDirectory() as td:
        raster_path = Path(td) / "light_pollution.tif"
        _build_test_raster(raster_path, crs=None, pixel_size=500000.0)

        tr_to_terrain = Transformer.from_crs(
            "EPSG:4326", CRS_25831, always_xy=True
        )
        x_terrain, y_terrain = tr_to_terrain.transform(TARGET_LON, TARGET_LAT)

        sampler = LightPollutionSampler(str(raster_path), radius_km=1.0)
        sampler.prepare_region_from_terrain_xy(
            x_terrain=x_terrain,
            y_terrain=y_terrain,
            radius_m=4000.0,
            input_crs=CRS_25831,
        )
        rad = sampler.get_radiance_terrain_xy(
            x_terrain=x_terrain,
            y_terrain=y_terrain,
            input_crs=CRS_25831,
        )
        assert rad == 7.0


def test_axis_order_is_not_swapped() -> None:
    with tempfile.TemporaryDirectory() as td:
        raster_path = Path(td) / "dvnl_axis_8857.tif"
        _build_test_raster(raster_path, crs=CRS_8857)

        sampler = LightPollutionSampler(str(raster_path), radius_km=1.0)
        sampler.prepare_region(TARGET_LAT, TARGET_LON, radius_km=4.0)

        rad_ok = sampler.get_radiance(TARGET_LAT, TARGET_LON)
        rad_swapped = sampler.get_radiance(TARGET_LON, TARGET_LAT)
        assert rad_ok == 7.0
        assert rad_swapped == 0.0


def test_out_of_bounds_returns_zero() -> None:
    with tempfile.TemporaryDirectory() as td:
        raster_path = Path(td) / "dvnl_oob_8857.tif"
        _build_test_raster(raster_path, crs=CRS_8857)

        sampler = LightPollutionSampler(str(raster_path), radius_km=1.0)
        sampler.prepare_region(TARGET_LAT, TARGET_LON, radius_km=4.0)

        assert sampler.get_radiance(0.0, 0.0) == 0.0


def test_projected_sampling_supports_25831_and_32631_for_same_point() -> None:
    with tempfile.TemporaryDirectory() as td:
        raster_path = Path(td) / "dvnl_25831_32631_8857.tif"
        _build_test_raster(raster_path, crs=CRS_8857)

        tr_25831 = Transformer.from_crs("EPSG:4326", CRS_25831, always_xy=True)
        tr_32631 = Transformer.from_crs("EPSG:4326", CRS_32631, always_xy=True)
        x_25831, y_25831 = tr_25831.transform(TARGET_LON, TARGET_LAT)
        x_32631, y_32631 = tr_32631.transform(TARGET_LON, TARGET_LAT)

        sampler = LightPollutionSampler(str(raster_path), radius_km=1.0)
        sampler.prepare_region_from_terrain_xy(
            x_terrain=x_25831,
            y_terrain=y_25831,
            radius_m=4000.0,
            input_crs=CRS_25831,
        )

        rad_25831 = sampler.get_radiance_terrain_xy(
            x_terrain=x_25831,
            y_terrain=y_25831,
            input_crs=CRS_25831,
        )
        rad_32631 = sampler.get_radiance_terrain_xy(
            x_terrain=x_32631,
            y_terrain=y_32631,
            input_crs=CRS_32631,
        )
        assert rad_25831 == 7.0
        assert rad_32631 == 7.0


def test_window_sampling_keeps_native_pixel_value_at_query_point() -> None:
    with tempfile.TemporaryDirectory() as td:
        raster_path = Path(td) / "dvnl_window_alignment_8857.tif"
        transform = from_origin(0.0, 10000.0, 1000.0, 1000.0)
        data = np.zeros((20, 20), dtype=np.float32)
        for r in range(data.shape[0]):
            for c in range(data.shape[1]):
                data[r, c] = float(r * 100 + c)

        with rasterio.open(
            raster_path,
            "w",
            driver="GTiff",
            width=data.shape[1],
            height=data.shape[0],
            count=1,
            dtype="float32",
            transform=transform,
            crs=CRS_8857,
            nodata=3.4e38,
        ) as dst:
            dst.write(data, 1)

        x_src = 5500.0
        y_src = 3500.0
        tr_inv = Transformer.from_crs(CRS_8857, "EPSG:4326", always_xy=True)
        lon, lat = tr_inv.transform(x_src, y_src)

        sampler = LightPollutionSampler(str(raster_path), radius_km=3.0)
        with rasterio.open(raster_path) as src:
            context = sampler._build_runtime_context(src)
            x_q, y_q = context["tr_geo_to_src"].transform(lon, lat)
            arr, trans_win, _ = sampler._read_window_around_point(
                src=src,
                x_cen=x_q,
                y_cen=y_q,
                radius_m=3000.0,
                is_geographic=False,
            )
            direct_val = sampler._read_single_pixel_from_source(src, x_q, y_q)
            inv = ~trans_win
            c_float, r_float = inv * (x_q, y_q)
            r = int(r_float)
            c = int(c_float)
            assert 0 <= r < arr.shape[0]
            assert 0 <= c < arr.shape[1]
            assert float(arr[r, c]) == float(direct_val)


def test_sqm_window_is_clipped_to_avoid_pathological_memory_usage() -> None:
    with tempfile.TemporaryDirectory() as td:
        raster_path = Path(td) / "dvnl_clip_3857.tif"
        # Very fine resolution in projected CRS can create huge windows for large radii.
        transform = from_origin(-1000.0, 1000.0, 1.0, 1.0)
        data = np.ones((256, 256), dtype=np.float32)
        with rasterio.open(
            raster_path,
            "w",
            driver="GTiff",
            width=data.shape[1],
            height=data.shape[0],
            count=1,
            dtype="float32",
            transform=transform,
            crs="EPSG:3857",
            nodata=3.4e38,
        ) as dst:
            dst.write(data, 1)

        sampler = LightPollutionSampler(str(raster_path), radius_km=5.0)
        with rasterio.open(raster_path) as src:
            arr, _, _ = sampler._read_window_around_point(
                src=src,
                x_cen=0.0,
                y_cen=0.0,
                radius_m=5000.0,
                is_geographic=False,
            )
        assert arr.shape[0] <= sampler.MAX_SQM_WINDOW_PIXELS
        assert arr.shape[1] <= sampler.MAX_SQM_WINDOW_PIXELS


def test_trace_links_location_sqm_and_bortle(capsys) -> None:
    with tempfile.TemporaryDirectory() as td:
        raster_path = Path(td) / "dvnl_trace_8857.tif"
        _build_test_raster(raster_path, crs=CRS_8857)

        sampler = LightPollutionSampler(str(raster_path), radius_km=1.0)
        sqm, bortle = sampler.estimate_zenith_sqm(TARGET_LAT, TARGET_LON)

    output = capsys.readouterr().out
    expected_trace = (
        "[LPSampler:Estimate] source=raster "
        f"lat={TARGET_LAT:.6f} lon={TARGET_LON:.6f} "
        f"sqm={sqm:.3f} bortle={bortle}"
    )
    assert expected_trace in output
