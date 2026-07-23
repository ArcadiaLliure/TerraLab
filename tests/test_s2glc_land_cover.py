from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin

from TerraLab.terrain.land_cover.legends.s2glc import (
    S2GLC_LEGEND,
    get_s2glc_style,
    s2glc_classes_to_rgba,
)
from TerraLab.terrain.land_cover.legends.category_info import category_info
from TerraLab.data.assets_manager import AssetManager
from TerraLab.data.layer_manager import LayerGroup, LayerId, LayerManager
from TerraLab.common.data_library import DataLibrary
from TerraLab.terrain.providers import RasterMetadata
from TerraLab.terrain.source_inspection import inspect_data_source
from TerraLab.terrain.data_sources import LayerType
from TerraLab.terrain.surface import (
    CategoricalSurfaceProvider,
    RgbCategoricalSurfaceProvider,
    RgbSurfaceProvider,
    SurfaceSamplingService,
    raster_grids_aligned,
)


def _write_raster(
    path,
    data,
    *,
    pixel_size: float,
    left: float = -200.0,
    top: float = 200.0,
    crs: str = "EPSG:25831",
    nodata=None,
    rgb: bool = False,
):
    array = np.asarray(data)
    if array.ndim == 2:
        array = array[None, ...]
    profile = {
        "driver": "GTiff",
        "width": array.shape[2],
        "height": array.shape[1],
        "count": array.shape[0],
        "dtype": array.dtype,
        "crs": crs,
        "transform": from_origin(left, top, pixel_size, pixel_size),
        "nodata": nodata,
    }
    if array.shape[1] >= 16 and array.shape[2] >= 16:
        profile.update(tiled=True, blockxsize=16, blockysize=16)
    with rasterio.open(path, "w", **profile) as target:
        target.write(array)
        if rgb:
            target.colorinterp = (
                ColorInterp.red,
                ColorInterp.green,
                ColorInterp.blue,
            )
    return path


def _profile(*, dem_spacing: float):
    distances = np.asarray(
        [dem_spacing, dem_spacing * 2.0, dem_spacing * 3.0],
        dtype=np.float32,
    )
    azimuths = np.asarray([0.0, 90.0, 180.0, 270.0], dtype=np.float32)
    shape = (len(distances), len(azimuths))
    elevations = np.asarray(
        [[100.0, 101.0, 102.0, 103.0]] * len(distances), dtype=np.float32
    )
    patch_axis = np.asarray([-15.0, 0.0, 15.0], dtype=np.float32)
    patch_shape = (patch_axis.size, patch_axis.size)
    return SimpleNamespace(
        observer_x=0.0,
        observer_y=0.0,
        observer_lat=41.0,
        observer_lon=2.0,
        geometry_crs="EPSG:25831",
        representation_mode="RELIEF",
        azimuths=azimuths,
        bands=[],
        terrain_mesh={
            "version": 3,
            "azimuths": azimuths,
            "distances": distances,
            "altitudes": np.zeros(shape, dtype=np.float32),
            "elevations": elevations,
            "valid": np.ones(shape, dtype=bool),
            "visible": np.ones(shape, dtype=bool),
            "near_patch_eastings": patch_axis,
            "near_patch_northings": patch_axis,
            "near_patch_altitudes": np.full(patch_shape, -10.0, dtype=np.float32),
            "near_patch_elevations": np.full(patch_shape, 100.0, dtype=np.float32),
            "near_patch_valid": np.ones(patch_shape, dtype=bool),
        },
    )


@pytest.mark.parametrize("kind", ["rgb", "categorical"])
def test_coverage_10m_over_dem_30m_subdivides_only_visual_grid(tmp_path, kind):
    if kind == "rgb":
        red = np.tile(np.arange(40, dtype=np.uint8), (40, 1)) * 5
        path = _write_raster(
            tmp_path / "rgb-10m.tif",
            np.stack((red, np.full_like(red, 80), np.full_like(red, 30))),
            pixel_size=10.0,
            rgb=True,
        )
        provider = RgbSurfaceProvider(path, linear_light=True)
    else:
        classes = np.where(
            np.indices((40, 40))[1] % 2,
            np.uint8(82),
            np.uint8(102),
        )
        path = _write_raster(
            tmp_path / "categorical-10m.tif", classes, pixel_size=10.0
        )
        provider = CategoricalSurfaceProvider(path, legend_id="s2glc_europe_2017")
    provider.initialize()
    try:
        cache = SurfaceSamplingService([provider]).sample_profile(
            _profile(dem_spacing=30.0)
        )
        assert cache.relief_rgba.shape[:2] == (3, 4)
        assert cache.visual_rgba is not None
        assert cache.visual_rgba.shape[0] > 3
        assert cache.visual_rgba.shape[1] > 4
        # Geometry values are interpolation of DEM values, not new elevations.
        assert float(np.min(cache.visual_elevations)) >= 100.0
        assert float(np.max(cache.visual_elevations)) <= 103.0
        if kind == "categorical":
            assert set(np.unique(cache.visual_class_ids)) <= {82, 102}
            assert bool(np.all(cache.visual_categorical[cache.visual_valid]))
            assert set(np.unique(cache.near_patch_class_ids)) <= {82, 102}
            assert bool(
                np.all(cache.near_patch_categorical[cache.near_patch_valid])
            )
    finally:
        provider.close()


@pytest.mark.parametrize("pixel_size,dem_spacing", [(10.0, 5.0), (30.0, 30.0)])
def test_rgb_resolution_scenarios_keep_source_pixels_with_nearest_sampling(
    tmp_path, pixel_size, dem_spacing
):
    red = np.tile(np.asarray([0, 64, 128, 255], dtype=np.uint8), (4, 1))
    path = _write_raster(
        tmp_path / f"rgb-{int(pixel_size)}m.tif",
        np.stack((red, red, red)),
        pixel_size=pixel_size,
        left=0.0,
        top=4.0 * pixel_size,
        rgb=True,
    )
    provider = RgbSurfaceProvider(path, linear_light=True)
    provider.initialize()
    try:
        y = 2.5 * pixel_size
        samples = provider.sample_rgba(
            [0.5 * pixel_size, pixel_size, 1.5 * pixel_size],
            [y, y, y],
        )
        assert samples.valid.tolist() == [True, True, True]
        values = samples.rgba[:, 0].astype(int)
        assert values[0] == 0 and values[2] == 64
        assert values[1] in {values[0], values[2]}
        assert set(values) <= {0, 64}
        if pixel_size < dem_spacing:
            pytest.fail("This parameter belongs to finer DEM/equal cases only")
    finally:
        provider.close()


@pytest.mark.parametrize("pixel_size,dem_spacing", [(10.0, 5.0), (30.0, 30.0)])
def test_categorical_resolution_scenarios_never_interpolate_codes(
    tmp_path, pixel_size, dem_spacing
):
    del dem_spacing
    codes = np.tile(np.asarray([82, 82, 102, 102], dtype=np.uint8), (4, 1))
    path = _write_raster(
        tmp_path / f"classes-{int(pixel_size)}m.tif",
        codes,
        pixel_size=pixel_size,
        left=0.0,
        top=4.0 * pixel_size,
    )
    provider = CategoricalSurfaceProvider(path, legend_id="s2glc_europe_2017")
    provider.initialize()
    try:
        x = np.linspace(0.5 * pixel_size, 3.5 * pixel_size, 101)
        sampled = provider.sample_classes(x, np.full_like(x, 2.5 * pixel_size))
        assert set(np.unique(sampled.classes[sampled.valid])) == {82, 102}
        assert 92 not in sampled.classes
        semantic = SurfaceSamplingService([provider]).sample_rgba_points(
            x, np.full_like(x, 2.5 * pixel_size), input_crs="EPSG:25831"
        )
        assert set(np.unique(semantic.class_ids[semantic.valid])) == {82, 102}
        assert bool(np.all(semantic.categorical[semantic.valid]))
    finally:
        provider.close()


def test_equal_resolution_fast_path_requires_same_crs_origin_and_grid():
    def metadata(transform, crs="EPSG:25831"):
        return RasterMetadata(
            native_crs=crs,
            bounds=(0.0, 0.0, 120.0, 120.0),
            resolution_m=30.0,
            nodata=(None,),
            driver="GTiff",
            band_count=1,
            paths=("synthetic.tif",),
            extra={"transform": list(transform)},
        )

    base = metadata((30.0, 0.0, 0.0, 0.0, -30.0, 120.0))
    shifted_one_pixel = metadata((30.0, 0.0, 30.0, 0.0, -30.0, 90.0))
    half_pixel = metadata((30.0, 0.0, 15.0, 0.0, -30.0, 120.0))
    other_crs = metadata(
        (30.0, 0.0, 0.0, 0.0, -30.0, 120.0), crs="EPSG:3035"
    )
    assert raster_grids_aligned(base, shifted_one_pixel)
    assert not raster_grids_aligned(base, half_pixel)
    assert not raster_grids_aligned(base, other_crs)


def test_crs_transform_nodata_bounds_and_detected_metadata(tmp_path):
    lon, lat = 2.17, 41.38
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform(
        lon, lat
    )
    red = np.full((4, 4), 120, dtype=np.uint8)
    red[0, 0] = 255
    rgb = np.stack((red, np.full_like(red, 80), np.full_like(red, 40)))
    path = _write_raster(
        tmp_path / "rgb-webmercator.tif",
        rgb,
        pixel_size=10.0,
        left=x - 20.0,
        top=y + 20.0,
        crs="EPSG:3857",
        nodata=255,
        rgb=True,
    )
    provider = RgbSurfaceProvider(path)
    provider.initialize()
    try:
        inside = provider.sample_rgba(lon, lat, input_crs="EPSG:4326")
        outside = provider.sample_rgba(lon + 1.0, lat, input_crs="EPSG:4326")
        edge = provider.sample_rgba(x + 19.9, y, input_crs="EPSG:3857")
        nodata = provider.sample_rgba(
            x - 15.0, y + 15.0, input_crs="EPSG:3857"
        )
        assert bool(inside.valid)
        assert not bool(outside.valid)
        assert bool(edge.valid)
        assert not bool(nodata.valid)
        assert provider._datasets[0].bytes_read <= rgb.nbytes * 2
    finally:
        provider.close()

    inspected = inspect_data_source(path, LayerType.ORTHOPHOTO_RGB)
    assert inspected.crs == "EPSG:3857"
    # EPSG:3857 native pixels are 10 projection units; the catalogue stores
    # their detected ground resolution at this latitude.
    assert inspected.resolution_m == pytest.approx(7.49, rel=0.02)
    raster = inspected.metadata["rasters"][0]
    assert raster["band_count"] == 3
    assert raster["extra"]["dtypes"] == ["uint8", "uint8", "uint8"]
    assert raster["extra"]["resolution_x"] == pytest.approx(10.0)
    assert raster["extra"]["resolution_y"] == pytest.approx(10.0)


def test_s2glc_registry_is_complete_deterministic_and_unknown_is_transparent():
    assert set(S2GLC_LEGEND) == {
        0,
        62,
        73,
        75,
        82,
        83,
        102,
        103,
        104,
        105,
        106,
        121,
        123,
        162,
        255,
    }
    assert get_s2glc_style(82).base_color == (35, 152, 0, 255)
    classes = np.asarray([82, 82, 92, 162, 255])
    x = np.asarray([10.0, 10.0, 20.0, 30.0, 40.0])
    y = np.asarray([50.0] * 5)
    first, first_valid = s2glc_classes_to_rgba(classes, x=x, y=y)
    second, second_valid = s2glc_classes_to_rgba(classes, x=x, y=y)
    assert np.array_equal(first, second)
    assert np.array_equal(first_valid, second_valid)
    assert first_valid.tolist() == [True, True, False, True, False]
    assert first[0].tolist() == first[1].tolist()


def test_localized_category_descriptions_cover_builtin_and_external_legends():
    s2glc = category_info("s2glc_europe_2017", 82)
    clcplus = category_info("clcplus_backbone_2023", 10)
    external = category_info("", 17, source_name="Mapa propi")

    assert s2glc.name == "Coberta d'arbres de fulla ampla"
    assert "cobertura del sòl" in s2glc.description
    assert clcplus.name == "Aigua"
    assert clcplus.description
    assert external.name == "Classe 17"
    assert external.product == "Mapa propi"


def test_validation_rejects_grayscale_rgb_and_float_categories(tmp_path):
    grayscale = _write_raster(
        tmp_path / "not-rgb.tif",
        np.ones((4, 4), dtype=np.uint8),
        pixel_size=10.0,
    )
    with pytest.raises(ValueError, match="tres canals RGB"):
        inspect_data_source(
            grayscale,
            LayerType.LAND_COVER_RGB,
            metadata={"legend_id": "s2glc_europe_2017"},
        )

    continuous = _write_raster(
        tmp_path / "not-categorical.tif",
        np.ones((4, 4), dtype=np.float32),
        pixel_size=10.0,
    )
    with pytest.raises(ValueError, match="codis enters"):
        inspect_data_source(continuous, LayerType.LAND_COVER_CATEGORICAL)


def test_s2glc_rgb_and_single_band_decode_to_identical_classes(tmp_path):
    codes = np.asarray(
        [
            [62, 82, 102, 162],
            [82, 102, 162, 62],
            [102, 162, 62, 82],
            [162, 62, 82, 102],
        ],
        dtype=np.uint8,
    )
    rgba = np.asarray(
        [[S2GLC_LEGEND[int(code)].base_color for code in row] for row in codes],
        dtype=np.uint8,
    )
    rgb_path = _write_raster(
        tmp_path / "s2glc-rgb.tif",
        np.moveaxis(rgba[..., :3], -1, 0),
        pixel_size=10.0,
        left=0.0,
        top=40.0,
        rgb=True,
    )
    class_path = _write_raster(
        tmp_path / "s2glc-classes.tif",
        codes,
        pixel_size=10.0,
        left=0.0,
        top=40.0,
    )
    rgb_provider = RgbCategoricalSurfaceProvider(
        rgb_path, legend_id="s2glc_europe_2017"
    )
    class_provider = CategoricalSurfaceProvider(
        class_path, legend_id="s2glc_europe_2017"
    )
    rgb_provider.initialize()
    class_provider.initialize()
    try:
        x, y = np.meshgrid(
            np.asarray([5.0, 15.0, 25.0, 35.0]),
            np.asarray([35.0, 25.0, 15.0, 5.0]),
        )
        decoded = rgb_provider.sample_classes(x, y)
        direct = class_provider.sample_classes(x, y)
        np.testing.assert_array_equal(decoded.valid, direct.valid)
        np.testing.assert_array_equal(decoded.classes, direct.classes)
        np.testing.assert_array_equal(decoded.classes, codes)
    finally:
        rgb_provider.close()
        class_provider.close()


def test_rgb_categorical_unknown_colours_and_transparency_are_nodata():
    palette = np.asarray([0x239800FF], dtype=np.uint32)
    classes = np.asarray([82], dtype=np.int64)
    rgba = np.asarray(
        [
            [35, 152, 0, 255],
            [1, 2, 3, 255],
            [35, 152, 0, 0],
        ],
        dtype=np.uint8,
    ).T

    decoded, valid = RgbCategoricalSurfaceProvider._decode_rgba(
        rgba, palette, classes
    )

    assert decoded.tolist() == [82, -1, -1]
    assert valid.tolist() == [True, False, False]


def test_layer_catalog_exposes_both_official_s2glc_products(tmp_path):
    facade = LayerManager.__new__(LayerManager)
    earth_ids = {item.id for item in facade.list_layers(LayerGroup.EARTH)}
    assert LayerId.EARTH_SURFACE_CATEGORICAL in earth_ids
    assert LayerId.EARTH_SURFACE_RGB in earth_ids

    assets = AssetManager(DataLibrary(tmp_path / "library"))
    categorical = assets.get_spec("surface_categorical")
    rgb = assets.get_spec("surface_rgb")
    assert categorical.semantic_type == "land_cover_categorical"
    assert rgb.semantic_type == "land_cover_rgb"
    assert categorical.auto_download_url.endswith("_grey.zip")
    assert rgb.auto_download_url.endswith("_RGB.zip")
    assert categorical.source_url == rgb.source_url == (
        "https://s2glc.cbk.waw.pl/extension"
    )
    assert "Creative Commons" not in categorical.license_note
