from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from TerraLab.common.data_library import DataLibrary
from TerraLab.data.assets_manager import AssetManager, AssetSpec
from TerraLab.terrain.crs import (
    CRS_GEOGRAPHIC,
    CRS_TERRAIN_INTERNAL,
    DEFAULT_TRANSFORM_SERVICE,
)
from TerraLab.terrain.data_sources import (
    DataSourceRegistry,
    LayerSelectionService,
    LayerType,
    SourceHealthStatus,
)


def _write_ascii_grid(
    path: Path,
    *,
    x: float,
    y: float,
    cell_size: float = 100.0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                "ncols 2",
                "nrows 2",
                f"xllcorner {x}",
                f"yllcorner {y}",
                f"cellsize {cell_size}",
                "NODATA_value -9999",
                "1 2",
                "3 4",
            )
        ),
        encoding="utf-8",
    )
    return path


def _union_center(bounds):
    return (
        0.5
        * (min(item[1] for item in bounds) + max(item[3] for item in bounds)),
        0.5
        * (min(item[0] for item in bounds) + max(item[2] for item in bounds)),
    )


def test_dem_autolocation_uses_recursive_geotiff_mosaic_coverage(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    root = tmp_path / "mosaic"
    paths = (root / "west" / "a.tif", root / "east" / "b.tiff")
    transforms = (
        from_origin(1.0, 42.0, 0.1, 0.1),
        from_origin(3.0, 44.0, 0.1, 0.1),
    )
    for path, transform in zip(paths, transforms):
        path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=2,
            width=2,
            count=1,
            dtype="float32",
            crs=CRS_GEOGRAPHIC,
            transform=transform,
        ) as dataset:
            dataset.write(np.ones((1, 2, 2), dtype=np.float32))

    manager = AssetManager.__new__(AssetManager)
    center = manager._estimate_observer_from_dem(root)

    assert center == pytest.approx((42.9, 2.1), abs=1e-8)


def test_dem_autolocation_uses_recursive_ascii_mosaic_coverage(tmp_path):
    root = tmp_path / "ascii-mosaic"
    native_bounds = (
        (400_000.0, 4_500_000.0, 400_200.0, 4_500_200.0),
        (402_000.0, 4_501_000.0, 402_200.0, 4_501_200.0),
    )
    _write_ascii_grid(root / "one" / "a.asc", x=400_000, y=4_500_000)
    _write_ascii_grid(root / "two" / "b.txt", x=402_000, y=4_501_000)
    geographic_bounds = [
        DEFAULT_TRANSFORM_SERVICE.transform_bounds(
            bounds,
            CRS_TERRAIN_INTERNAL,
            CRS_GEOGRAPHIC,
        )
        for bounds in native_bounds
    ]

    manager = AssetManager.__new__(AssetManager)
    center = manager._estimate_observer_from_dem(root)

    assert center == pytest.approx(_union_center(geographic_bounds), abs=1e-8)


class _RecordingRegistry:
    def __init__(self):
        self.calls = []

    def register_path(self, path, layer_type, **fields):
        self.calls.append((Path(path), layer_type, fields))
        return SimpleNamespace(id=f"source-{len(self.calls)}")


def _typed_asset_manager(tmp_path: Path) -> AssetManager:
    manager = AssetManager.__new__(AssetManager)
    manager.layout = {
        "root": str(tmp_path / "runtime"),
        "data_elevation": str(tmp_path / "runtime" / "data" / "elevation"),
        "data_light_pollution": str(
            tmp_path / "runtime" / "data" / "light_pollution"
        ),
    }
    manager.data_sources = _RecordingRegistry()
    manager.specs = {
        asset_id: AssetSpec(
            asset_id=asset_id,
            title=asset_id,
            source_url="",
            accepted_formats="",
            credits=f"{asset_id} credits",
        )
        for asset_id in (
            "elevation_dem",
            "light_pollution",
            "surface_rgb",
            "surface_categorical",
        )
    }
    return manager


def test_typed_imports_keep_datasets_independent_and_do_not_write_legacy_paths(
    tmp_path,
    monkeypatch,
):
    manager = _typed_asset_manager(tmp_path)
    writes = []
    registrations = []
    monkeypatch.setattr(
        "TerraLab.data.assets_manager.set_config_value",
        lambda key, value: writes.append((key, value)),
    )
    monkeypatch.setattr(
        manager,
        "_auto_configure_observer_from_dem",
        lambda *args, **kwargs: {"applied": False},
    )

    def register(path, layer_type, **fields):
        registrations.append((Path(path), layer_type, fields))
        return SimpleNamespace(id=f"source-{len(registrations)}")

    monkeypatch.setattr(manager, "_register_data_source", register)

    first_input = _write_ascii_grid(
        tmp_path / "first" / "dem.asc", x=400_000, y=4_500_000
    )
    second_input = _write_ascii_grid(
        tmp_path / "second" / "dem.asc", x=410_000, y=4_510_000
    )
    first = manager.import_files(
        "elevation_dem",
        [str(first_input)],
        options={"display_name": "Europe DEM"},
    )
    second = manager.import_files(
        "elevation_dem",
        [str(second_input)],
        options={"display_name": "Europe DEM"},
    )
    light_input = tmp_path / "light.tif"
    light_input.write_bytes(b"light")
    light = manager.import_files(
        "light_pollution", [str(light_input)], options={"display_name": "DVNL"}
    )
    surface_input = tmp_path / "surface.tif"
    surface_input.write_bytes(b"surface")
    surface = manager.import_files(
        "surface_rgb", [str(surface_input)], options={"display_name": "Ortho"}
    )

    first_dir = Path(first["stored_in"])
    second_dir = Path(second["stored_in"])
    assert first_dir != second_dir
    assert first_dir.name == "Europe_DEM"
    assert second_dir.name == "Europe_DEM_2"
    assert (first_dir / "dem.asc").read_text(encoding="utf-8") != (
        second_dir / "dem.asc"
    ).read_text(encoding="utf-8")
    assert Path(light["stored_in"]).parent.name == "DVNL"
    assert Path(surface["stored_in"]).name == "Ortho"
    assert [item[1] for item in registrations] == [
        LayerType.ELEVATION,
        LayerType.ELEVATION,
        LayerType.LIGHT_POLLUTION,
        LayerType.SURFACE_RGB,
    ]

    forbidden = {
        "raster_path",
        "dvnl_path",
        "assets.elevation_dem.path",
        "assets.light_pollution.path",
        "assets.surface_rgb.path",
        "assets.surface_categorical.path",
    }
    assert forbidden.isdisjoint(key for key, _value in writes)
    assert {key for key, _value in writes} == {
        "assets.elevation_dem.ready",
        "assets.elevation_dem.updated_utc",
        "assets.light_pollution.ready",
        "assets.light_pollution.updated_utc",
        "assets.surface_rgb.ready",
        "assets.surface_rgb.updated_utc",
    }


def test_bundled_light_pollution_is_registered_in_typed_catalogue():
    manager = AssetManager.__new__(AssetManager)
    manager.data_sources = _RecordingRegistry()

    manager._ensure_bundled_data_sources()

    assert len(manager.data_sources.calls) == 1
    path, layer_type, fields = manager.data_sources.calls[0]
    assert path.name == "C_DVNL 2022.tif"
    assert layer_type is LayerType.LIGHT_POLLUTION
    assert fields["display_name"] == "DVNL 2022"
    assert fields["priority"] == -100
    assert fields["metadata"] == {"bundled": True}


def test_invalid_typed_import_is_catalogued_disabled_and_never_marked_ready(
    tmp_path,
    monkeypatch,
):
    manager = _typed_asset_manager(tmp_path)
    manager.data_sources = DataSourceRegistry(
        tmp_path / "catalog" / "data_sources.json",
        legacy_reader={},
    )
    writes = []
    monkeypatch.setattr(
        "TerraLab.data.assets_manager.set_config_value",
        lambda key, value: writes.append((key, value)),
    )
    monkeypatch.setattr(
        manager,
        "_auto_configure_observer_from_dem",
        lambda *args, **kwargs: {"applied": False},
    )
    broken = tmp_path / "input" / "broken.txt"
    broken.parent.mkdir(parents=True)
    broken.write_text("this is not an elevation grid", encoding="utf-8")

    with pytest.raises(ValueError, match="no \u00e9s un raster"):
        manager.import_files(
            "elevation_dem",
            [str(broken)],
            options={"display_name": "Broken DEM"},
        )

    sources = manager.data_sources.list_sources(LayerType.ELEVATION)
    assert len(sources) == 1
    source = sources[0]
    assert source.health_status is SourceHealthStatus.INVALID
    assert source.metadata["inspection_error"]
    assert source.enabled is False
    assert source.available is False
    selection = LayerSelectionService(manager.data_sources).select_elevation(
        41.0, 2.0
    )
    assert selection.effective is None
    assert manager.asset_status("elevation_dem")["ready"] is False
    assert manager.asset_status("elevation_dem")["reason"] == "invalid_source"
    assert ("assets.elevation_dem.ready", False) in writes
    assert ("assets.elevation_dem.ready", True) not in writes


def test_remove_layer_data_deletes_managed_sources_and_preserves_external_files(
    tmp_path,
    monkeypatch,
):
    library = DataLibrary(tmp_path / "library")
    library.initialize()
    manager = AssetManager(library)
    manager.data_sources = DataSourceRegistry(
        tmp_path / "catalog" / "removal_sources.json",
        legacy_reader={},
    )
    writes = []
    monkeypatch.setattr(
        "TerraLab.data.assets_manager.set_config_value",
        lambda key, value: writes.append((key, value)),
    )

    managed_dir = manager.layout["data_elevation"] / "owned-dem"
    managed_file = _write_ascii_grid(
        managed_dir / "dem.asc",
        x=400_000,
        y=4_500_000,
    )
    external_file = _write_ascii_grid(
        tmp_path / "external" / "dem.asc",
        x=410_000,
        y=4_510_000,
    )
    managed_source = manager.data_sources.register_path(
        managed_dir,
        LayerType.ELEVATION,
        metadata={"managed": True, "asset_id": "elevation_dem"},
        provenance="managed",
    )
    external_source = manager.data_sources.register_path(
        external_file,
        LayerType.ELEVATION,
        metadata={"managed": False, "asset_id": "elevation_dem"},
        provenance="external",
    )
    library.update_asset("elevation_dem", ready=True)
    managed_size = managed_file.stat().st_size

    preview = manager.removal_preview("elevation_dem", include_size=True)
    report = manager.remove_asset_data("elevation_dem")

    assert preview.has_data is True
    assert preview.total_bytes == managed_size
    assert managed_dir.exists() is False
    assert external_file.is_file()
    assert manager.data_sources.get(managed_source.id) is None
    assert manager.data_sources.get(external_source.id) is None
    assert set(report.removed_source_ids) == {
        managed_source.id,
        external_source.id,
    }
    assert str(external_file.resolve()) in report.detached_paths
    assert library.asset_state("elevation_dem") == {}
    assert ("assets.elevation_dem.ready", False) in writes


def test_remove_sky_layer_clears_owned_directory_but_not_linked_source(
    tmp_path,
    monkeypatch,
):
    library = DataLibrary(tmp_path / "library")
    library.initialize()
    manager = AssetManager(library)
    monkeypatch.setattr(
        "TerraLab.data.assets_manager.set_config_value",
        lambda *_args: None,
    )
    managed_root = manager.layout["data_ngc"]
    managed_file = managed_root / "openngc_catalog.csv"
    managed_file.write_text("Name\nM31\n", encoding="utf-8")
    external_source = library.root / "imports" / "OpenNGC.csv"
    external_source.parent.mkdir()
    external_source.write_text("Name\nM42\n", encoding="utf-8")
    library.update_asset(
        "ngc_catalog",
        ready=True,
        path=str(managed_file),
        source_path=str(external_source),
        managed=True,
    )

    report = manager.remove_asset_data("ngc_catalog")

    assert managed_root.is_dir()
    assert list(managed_root.iterdir()) == []
    assert external_source.read_text(encoding="utf-8") == "Name\nM42\n"
    assert str(external_source.resolve()) in report.detached_paths
    assert library.asset_state("ngc_catalog") == {}


def test_remove_layer_keeps_managed_path_shared_with_another_layer(
    tmp_path,
    monkeypatch,
):
    library = DataLibrary(tmp_path / "library")
    library.initialize()
    manager = AssetManager(library)
    manager.data_sources = DataSourceRegistry(
        tmp_path / "catalog" / "shared_sources.json",
        legacy_reader={},
    )
    monkeypatch.setattr(
        "TerraLab.data.assets_manager.set_config_value",
        lambda *_args: None,
    )
    shared = manager.layout["data_surface"] / "shared"
    raster = shared / "surface.tif"
    raster.parent.mkdir()
    raster.write_bytes(b"surface")
    rgb = manager.data_sources.register_path(
        shared,
        LayerType.SURFACE_RGB,
        metadata={"managed": True, "asset_id": "surface_rgb"},
        provenance="managed",
    )
    categorical = manager.data_sources.register_path(
        shared,
        LayerType.SURFACE_CATEGORICAL,
        metadata={"managed": False, "asset_id": "surface_categorical"},
        provenance="external",
    )

    report = manager.remove_asset_data("surface_rgb")

    assert raster.read_bytes() == b"surface"
    assert manager.data_sources.get(rgb.id) is None
    assert manager.data_sources.get(categorical.id) is not None
    assert str(shared.resolve()) in report.retained_paths
