from __future__ import annotations

import json
from pathlib import Path

import pytest

from TerraLab.common.app_paths import data_source_catalog_path, runtime_layout
from TerraLab.terrain.data_sources import (
    CATALOG_SCHEMA_VERSION,
    LAND_COVER_TYPE_MIGRATION,
    LEGACY_PATHS_MIGRATION,
    DataSourceRegistry,
    LayerRole,
    LayerSelectionService,
    LayerType,
    SelectionMode,
    SourceHealthStatus,
    TerrainRepresentationMode,
    build_source_fingerprint,
)


def _registry(tmp_path: Path, *, legacy_reader=None) -> DataSourceRegistry:
    return DataSourceRegistry(
        tmp_path / "catalog" / "data_sources.json",
        legacy_reader={} if legacy_reader is None else legacy_reader,
    )


def _touch(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test")
    return path


def test_registry_round_trip_preserves_complete_metadata_and_preferences(
    tmp_path,
):
    registry = _registry(tmp_path)
    raster = _touch(tmp_path, "rasters/icgc.tif")

    source = registry.register_path(
        raster,
        LayerType.ELEVATION,
        source_id="icgc-catalunya-5m",
        display_name="ICGC Catalunya",
        data_format="geotiff",
        crs="EPSG:25831",
        resolution_m=5.0,
        bounds=(400_000.0, 4_500_000.0, 500_000.0, 4_700_000.0),
        coverage=(0.0, 40.0, 4.0, 43.5),
        priority=50,
        enabled=True,
        metadata={"year": 2024, "tags": ["dem", "local"]},
        provenance="user_import",
        attribution="Institut Cartografic i Geologic de Catalunya",
        license="CC BY 4.0",
        fingerprint="dataset-version-1",
    )
    registry.set_selection(
        LayerType.ELEVATION,
        source.id,
        mode=SelectionMode.MANUAL,
    )
    registry.representation_mode = TerrainRepresentationMode.PROFILE

    reopened = DataSourceRegistry(registry.path, legacy_reader={})
    loaded = reopened.get(source.id)

    assert loaded == source
    assert loaded is not None
    assert loaded.source_id == "icgc-catalunya-5m"
    assert loaded.path == str(raster.resolve())
    assert loaded.bounds == (
        400_000.0,
        4_500_000.0,
        500_000.0,
        4_700_000.0,
    )
    assert loaded.coverage == (0.0, 40.0, 4.0, 43.5)
    assert reopened.get_selection(LayerRole.ELEVATION).source_id == source.id
    assert reopened.representation_mode is TerrainRepresentationMode.PROFILE

    payload = json.loads(registry.path.read_text(encoding="utf-8"))
    assert payload["version"] == CATALOG_SCHEMA_VERSION
    assert payload["migrations"][LEGACY_PATHS_MIGRATION] is True
    assert not list(registry.path.parent.glob("*.tmp"))


def test_default_registry_uses_runtime_catalog_path(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))

    registry = DataSourceRegistry.default()
    layout = runtime_layout()

    assert registry.path == data_source_catalog_path().resolve()
    assert layout["data_source_catalog"] == data_source_catalog_path()
    assert layout["data_surface"].is_dir()


def test_legacy_migration_is_deduplicated_missing_safe_and_one_time(tmp_path):
    missing_dem = tmp_path / "external-drive" / "dem"
    missing_lp = tmp_path / "external-drive" / "dvnl.tif"
    calls: list[str] = []
    values = {
        "raster_path": str(missing_dem),
        "assets.elevation_dem.path": str(missing_dem / ".." / "dem"),
        "dvnl_path": str(missing_lp),
        "assets.light_pollution.path": str(missing_lp),
    }

    def legacy_reader(key, default=None):
        calls.append(key)
        return values.get(key, default)

    registry = _registry(tmp_path, legacy_reader=legacy_reader)

    elevations = registry.list_sources(LayerType.ELEVATION)
    light_sources = registry.list_sources(LayerType.LIGHT_POLLUTION)
    assert len(elevations) == 1
    assert len(light_sources) == 1
    assert elevations[0].path == str(missing_dem.resolve())
    assert elevations[0].available is False
    assert light_sources[0].available is False
    assert set(elevations[0].metadata["legacy_config_keys"]) == {
        "raster_path",
        "assets.elevation_dem.path",
    }
    assert set(light_sources[0].metadata["legacy_config_keys"]) == {
        "dvnl_path",
        "assets.light_pollution.path",
    }
    assert calls == [
        "raster_path",
        "assets.elevation_dem.path",
        "dvnl_path",
        "assets.light_pollution.path",
    ]

    calls.clear()
    values["raster_path"] = str(tmp_path / "different-dem")
    reopened = DataSourceRegistry(registry.path, legacy_reader=legacy_reader)
    assert len(reopened.list_sources(LayerType.ELEVATION)) == 1
    assert calls == []


def test_legacy_reader_supports_nested_mapping(tmp_path):
    dem = tmp_path / "nested-dem"
    reader = {
        "assets": {
            "elevation_dem": {"path": str(dem)},
            "light_pollution": {"path": ""},
        }
    }

    registry = _registry(tmp_path, legacy_reader=reader)

    assert [source.path for source in registry.list_sources()] == [
        str(dem.resolve())
    ]


def test_legacy_surface_types_are_atomically_rewritten_to_land_cover(tmp_path):
    path = tmp_path / "catalog" / "data_sources.json"
    raster = _touch(tmp_path, "surface/legacy.tif")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {
                        "id": "legacy-rgb",
                        "display_name": "Tipus de sòl",
                        "layer_type": "surface_rgb",
                        "path": str(raster),
                        "format": "geotiff",
                    }
                ],
                "selections": {},
                "migrations": {LEGACY_PATHS_MIGRATION: True},
            }
        ),
        encoding="utf-8",
    )

    registry = DataSourceRegistry(path, legacy_reader={})
    loaded = registry.get("legacy-rgb")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert loaded.layer_type is LayerType.LAND_COVER_RGB
    assert payload["version"] == CATALOG_SCHEMA_VERSION
    assert payload["sources"][0]["layer_type"] == "land_cover_rgb"
    assert payload["migrations"][LAND_COVER_TYPE_MIGRATION] is True


def test_duplicate_registration_returns_existing_source(tmp_path):
    raster = _touch(tmp_path, "same/dem.asc")
    registry = _registry(tmp_path)

    first = registry.register_path(
        raster,
        LayerType.ELEVATION,
        source_id="first",
    )
    duplicate = registry.register_path(
        raster.parent / "." / raster.name,
        LayerType.ELEVATION,
        source_id="second",
    )

    assert duplicate.id == first.id
    assert len(registry.list_sources(LayerType.ELEVATION)) == 1


def test_elevation_selection_uses_coverage_priority_resolution_and_stable_id(
    tmp_path,
):
    registry = _registry(tmp_path)
    broad = registry.register_path(
        _touch(tmp_path, "dem/europe.tif"),
        LayerType.ELEVATION,
        source_id="europe",
        resolution_m=30,
        priority=10,
        coverage=(-15, 30, 45, 72),
    )
    registry.register_path(
        _touch(tmp_path, "dem/catalunya-30.tif"),
        LayerType.ELEVATION,
        source_id="catalunya-30",
        resolution_m=30,
        priority=20,
        coverage=(0, 40, 4, 43.5),
    )
    local_b = registry.register_path(
        _touch(tmp_path, "dem/local-b.tif"),
        LayerType.ELEVATION,
        source_id="local-b",
        resolution_m=5,
        priority=20,
        coverage=(0, 40, 4, 43.5),
    )
    local_a = registry.register_path(
        _touch(tmp_path, "dem/local-a.tif"),
        LayerType.ELEVATION,
        source_id="local-a",
        resolution_m=5,
        priority=20,
        coverage=(0, 40, 4, 43.5),
    )
    selector = LayerSelectionService(registry)

    local_result = selector.select_elevation(41.5, 2.0)
    assert local_result.reason == "automatic"
    assert local_result.effective == local_a
    assert [source.id for source in local_result.chain] == [
        "local-a",
        "local-b",
        "catalunya-30",
        "europe",
    ]

    outside_result = selector.select_elevation(50.0, 8.0)
    assert outside_result.effective == broad
    assert [source.id for source in outside_result.chain] == ["europe"]

    registry.set_selection(LayerRole.ELEVATION, local_b.id)
    manual_fallback = selector.select_elevation(50.0, 8.0)
    assert manual_fallback.configured == local_b
    assert manual_fallback.effective == broad
    assert manual_fallback.reason == "manual_out_of_coverage_fallback"


def test_surface_rgb_precedes_categorical_but_manual_selection_wins(tmp_path):
    registry = _registry(tmp_path)
    categorical = registry.register_path(
        _touch(tmp_path, "surface/clc.tif"),
        LayerType.SURFACE_CATEGORICAL,
        source_id="clc",
        resolution_m=10,
        priority=999,
        coverage=(-15, 30, 45, 72),
    )
    rgb = registry.register_path(
        _touch(tmp_path, "surface/ortho.tif"),
        LayerType.SURFACE_RGB,
        source_id="ortho",
        resolution_m=50,
        priority=-20,
        coverage=(0, 40, 4, 43.5),
    )
    selector = LayerSelectionService(registry)

    automatic = selector.select_surface(41.5, 2.0)
    assert automatic.effective == rgb
    assert [source.id for source in automatic.chain] == ["ortho", "clc"]

    outside_rgb = selector.select_surface(50.0, 8.0)
    assert outside_rgb.effective == categorical

    registry.set_selection(LayerRole.SURFACE, categorical.id)
    manual = selector.select_surface(41.5, 2.0)
    assert manual.reason == "manual"
    assert manual.configured == categorical
    assert manual.effective == categorical
    assert [source.id for source in manual.chain] == ["clc", "ortho"]


def test_disabled_missing_and_removed_sources_fall_back_without_disk_deletion(
    tmp_path,
):
    registry = _registry(tmp_path)
    preferred_path = _touch(tmp_path, "dem/preferred.tif")
    fallback_path = _touch(tmp_path, "dem/fallback.tif")
    preferred = registry.register_path(
        preferred_path,
        LayerType.ELEVATION,
        source_id="preferred",
        priority=20,
    )
    fallback = registry.register_path(
        fallback_path,
        LayerType.ELEVATION,
        source_id="fallback",
        priority=10,
    )
    registry.set_selection(LayerRole.ELEVATION, preferred.id)
    registry.update(preferred.id, enabled=False)
    selector = LayerSelectionService(registry)

    disabled = selector.select_elevation(41.0, 2.0)
    assert disabled.configured is not None
    assert disabled.effective == fallback
    assert disabled.reason == "manual_disabled_fallback"

    removed = registry.remove(preferred.id)
    assert removed is not None
    assert preferred_path.exists()
    assert (
        registry.get_selection(LayerRole.ELEVATION).mode
        is SelectionMode.AUTOMATIC
    )
    assert registry.remove("does-not-exist") is None


def test_light_pollution_selection_and_no_source_reason(tmp_path):
    registry = _registry(tmp_path)
    selector = LayerSelectionService(registry)

    empty = selector.select_light_pollution(41.0, 2.0)
    assert empty.configured is None
    assert empty.effective is None
    assert empty.chain == ()
    assert empty.reason == "no_source_available"

    missing = registry.register_path(
        tmp_path / "missing-dvnl.tif",
        LayerType.LIGHT_POLLUTION,
        source_id="missing",
        priority=100,
    )
    available = registry.register_path(
        _touch(tmp_path, "lp/dvnl.tif"),
        LayerType.LIGHT_POLLUTION,
        source_id="available",
        priority=10,
    )
    automatic = selector.select_light_pollution(41.0, 2.0)
    assert automatic.effective == available
    assert missing not in automatic.chain


def test_wrapped_antimeridian_coverage(tmp_path):
    registry = _registry(tmp_path)
    source = registry.register_path(
        _touch(tmp_path, "surface/dateline.tif"),
        LayerType.SURFACE_RGB,
        source_id="dateline",
        coverage=(170.0, -20.0, -170.0, 20.0),
    )

    assert source.covers(0.0, 179.0)
    assert source.covers(0.0, -179.0)
    assert not source.covers(0.0, 0.0)


def test_independent_registry_instances_merge_without_lost_updates(tmp_path):
    catalog = tmp_path / "data_sources.json"
    dem = _touch(tmp_path, "dem.tif")
    rgb = _touch(tmp_path, "rgb.tif")
    first = DataSourceRegistry(catalog, legacy_reader={})
    stale_second = DataSourceRegistry(catalog, legacy_reader={})

    first.register_path(dem, LayerType.ELEVATION, display_name="DEM")
    stale_second.register_path(
        rgb, LayerType.SURFACE_RGB, display_name="Ortho"
    )

    reopened = DataSourceRegistry(catalog, legacy_reader={})
    assert [source.display_name for source in reopened.list_sources()] == [
        "DEM",
        "Ortho",
    ]
    assert len(first.list_sources()) == 2


def test_inspection_health_excludes_pending_and_invalid_sources(tmp_path):
    registry = _registry(tmp_path)
    invalid = registry.register_path(
        _touch(tmp_path, "dem/broken.tif"),
        LayerType.ELEVATION,
        source_id="broken",
        priority=100,
        enabled=True,
        metadata={
            "inspection_status": SourceHealthStatus.INVALID.value,
            "inspection_error": "not a raster",
        },
    )
    pending = registry.register_path(
        _touch(tmp_path, "dem/pending.tif"),
        LayerType.ELEVATION,
        source_id="pending",
        priority=90,
        enabled=True,
        metadata={"inspection_status": SourceHealthStatus.PENDING.value},
    )
    fallback = registry.register_path(
        _touch(tmp_path, "dem/legacy-valid.tif"),
        LayerType.ELEVATION,
        source_id="fallback",
        priority=1,
    )

    assert invalid.health_status is SourceHealthStatus.INVALID
    assert invalid.enabled is False
    assert invalid.available is False
    assert pending.health_status is SourceHealthStatus.PENDING
    assert pending.enabled is False
    assert pending.available is False
    assert fallback.health_status is SourceHealthStatus.UNVERIFIED
    assert fallback.available is True

    registry.set_selection(LayerRole.ELEVATION, invalid.id)
    result = LayerSelectionService(registry).select_elevation(41.0, 2.0)
    assert result.effective == fallback
    assert result.reason == "manual_invalid_fallback"
    assert [source.id for source in result.chain] == ["fallback"]

    # Health cannot be bypassed by toggling the generic enabled flag.
    still_invalid = registry.update(invalid.id, enabled=True)
    assert still_invalid.enabled is False
    assert still_invalid.available is False


def test_directory_fingerprint_tracks_nested_raster_tiles_and_is_persisted(
    tmp_path,
):
    mosaic = tmp_path / "mosaic"
    tile = _touch(mosaic, "nested/tile.tif")
    root_stat = mosaic.stat()
    registry = _registry(tmp_path)
    source = registry.register_path(
        mosaic,
        LayerType.ELEVATION,
        source_id="mosaic",
    )
    first = source.fingerprint
    assert first == build_source_fingerprint(mosaic)

    tile.write_bytes(b"changed tile payload")
    # Prove the fingerprint does not depend only on the root directory stat.
    import os

    os.utime(
        mosaic,
        ns=(int(root_stat.st_atime_ns), int(root_stat.st_mtime_ns)),
    )
    second = registry.list_sources(LayerType.ELEVATION)[0]

    assert second.fingerprint != first
    assert second.fingerprint == build_source_fingerprint(mosaic)
    reopened = DataSourceRegistry(registry.path, legacy_reader={})
    assert reopened.get("mosaic").fingerprint == second.fingerprint


def test_explicit_source_fingerprint_is_not_overwritten_by_path_refresh(tmp_path):
    raster = _touch(tmp_path, "dem/custom-version.tif")
    registry = _registry(tmp_path)
    registry.register_path(
        raster,
        LayerType.ELEVATION,
        source_id="custom-version",
        fingerprint="vendor-release-2026-07",
    )

    raster.write_bytes(b"changed")
    loaded = registry.get("custom-version")

    assert loaded is not None
    assert loaded.fingerprint == "vendor-release-2026-07"
    assert loaded.fingerprint_auto is False


def test_async_dialog_inspection_transitions_pending_to_healthy_or_invalid(
    tmp_path,
):
    pytest.importorskip("PyQt5")
    from types import SimpleNamespace

    from TerraLab.ui.data_layers_dialog import DataLayersDialog

    registry = _registry(tmp_path)
    pending_metadata = {
        "inspection_status": SourceHealthStatus.PENDING.value,
        "enable_after_inspection": True,
    }
    healthy_source = registry.register_path(
        _touch(tmp_path, "async/healthy.tif"),
        LayerType.ELEVATION,
        source_id="async-healthy",
        enabled=False,
        metadata=pending_metadata,
    )
    invalid_source = registry.register_path(
        _touch(tmp_path, "async/invalid.tif"),
        LayerType.ELEVATION,
        source_id="async-invalid",
        enabled=False,
        metadata=pending_metadata,
    )
    dialog = SimpleNamespace(
        registry=registry,
        _inspection_jobs={healthy_source.id: object(), invalid_source.id: object()},
        _refresh=lambda: None,
    )
    inspection = SimpleNamespace(
        registry_fields=lambda: {
            "crs": "EPSG:4326",
            "resolution_m": 10.0,
            "bounds": (1.0, 2.0, 3.0, 4.0),
            "coverage": (1.0, 2.0, 3.0, 4.0),
            "metadata": {
                **pending_metadata,
                "inspection_version": 1,
            },
        }
    )

    DataLayersDialog._inspection_finished(
        dialog, healthy_source.id, inspection, ""
    )
    DataLayersDialog._inspection_finished(
        dialog, invalid_source.id, None, "cannot open raster"
    )

    healthy = registry.get(healthy_source.id)
    invalid = registry.get(invalid_source.id)
    assert healthy is not None
    assert healthy.health_status is SourceHealthStatus.HEALTHY
    assert healthy.enabled is True
    assert healthy.available is True
    assert "enable_after_inspection" not in healthy.metadata
    assert invalid is not None
    assert invalid.health_status is SourceHealthStatus.INVALID
    assert invalid.enabled is False
    assert invalid.available is False
    assert invalid.metadata["inspection_error"] == "cannot open raster"


def test_dialog_source_signature_tracks_inspection_metadata_and_health(
    tmp_path,
):
    pytest.importorskip("PyQt5")
    from dataclasses import replace

    from TerraLab.ui.data_layers_dialog import _source_signature

    registry = _registry(tmp_path)
    source = registry.register_path(
        _touch(tmp_path, "signatures/surface.tif"),
        LayerType.SURFACE_RGB,
        source_id="signature-surface",
        enabled=False,
        metadata={"inspection_status": SourceHealthStatus.PENDING.value},
    )
    pending = _source_signature(source)
    inspected = replace(
        source,
        crs="EPSG:32631",
        resolution_m=2.5,
        bounds=(400_000.0, 4_600_000.0, 401_000.0, 4_601_000.0),
        coverage=(2.0, 41.0, 2.1, 41.1),
        metadata={"inspection_status": SourceHealthStatus.HEALTHY.value},
    )
    healthy = _source_signature(inspected)

    assert pending != healthy
    assert "EPSG:32631" in healthy
    assert 2.5 in healthy
    assert (400_000.0, 4_600_000.0, 401_000.0, 4_601_000.0) in healthy
    assert (2.0, 41.0, 2.1, 41.1) in healthy
    assert SourceHealthStatus.HEALTHY.value in healthy


def test_late_inspection_change_is_replayed_after_modal_close(tmp_path):
    pytest.importorskip("PyQt5")
    from types import SimpleNamespace

    from TerraLab.ui.data_layers_dialog import (
        DataLayerChanges,
        DataLayersDialog,
        _DurableChangeEvent,
    )

    registry = _registry(tmp_path)
    source = registry.register_path(
        _touch(tmp_path, "late/ortho.tif"),
        LayerType.SURFACE_RGB,
        source_id="late-surface",
        enabled=False,
        metadata={
            "inspection_status": SourceHealthStatus.PENDING.value,
            "enable_after_inspection": True,
        },
    )
    durable_event = _DurableChangeEvent()
    dialog = SimpleNamespace(
        registry=registry,
        _inspection_jobs={source.id: object()},
        _dialog_closed=True,
        post_close_changes=durable_event,
        _refresh=lambda: None,
    )
    inspection = SimpleNamespace(
        registry_fields=lambda: {
            "crs": "EPSG:4326",
            "resolution_m": 0.5,
            "bounds": (1.0, 2.0, 3.0, 4.0),
            "coverage": (1.0, 2.0, 3.0, 4.0),
            "metadata": {
                "inspection_status": SourceHealthStatus.PENDING.value,
                "enable_after_inspection": True,
                "inspection_version": 1,
            },
        }
    )

    # Completion happens before the post-exec subscriber is attached.
    DataLayersDialog._inspection_finished(dialog, source.id, inspection, "")
    received = []
    durable_event.connect(received.append)

    assert received == [DataLayerChanges(surface=True)]
    assert registry.get(source.id).health_status is SourceHealthStatus.HEALTHY

    # Once connected, later completions use the normal signal path.
    durable_event.publish(DataLayerChanges(elevation=True))
    assert received[-1] == DataLayerChanges(elevation=True)


def test_inspection_finished_while_open_updates_normal_dialog_changes(tmp_path):
    pytest.importorskip("PyQt5")
    from types import MethodType, SimpleNamespace

    from TerraLab.ui.data_layers_dialog import (
        DataLayerChanges,
        DataLayersDialog,
        _DurableChangeEvent,
    )

    registry = _registry(tmp_path)
    source = registry.register_path(
        _touch(tmp_path, "open/dem.tif"),
        LayerType.ELEVATION,
        source_id="open-elevation",
        enabled=False,
        metadata={
            "inspection_status": SourceHealthStatus.PENDING.value,
            "enable_after_inspection": True,
        },
    )
    dialog = SimpleNamespace(
        registry=registry,
        _inspection_jobs={source.id: object()},
        _dialog_closed=False,
        post_close_changes=_DurableChangeEvent(),
        changes=DataLayerChanges(),
    )
    dialog._snapshot = MethodType(DataLayersDialog._snapshot, dialog)
    dialog._before = dialog._snapshot()
    dialog._refresh = lambda: DataLayersDialog._update_changes(dialog)
    inspection = SimpleNamespace(
        registry_fields=lambda: {
            "crs": "EPSG:25831",
            "resolution_m": 5.0,
            "bounds": (400_000.0, 4_600_000.0, 401_000.0, 4_601_000.0),
            "coverage": (1.0, 41.0, 2.0, 42.0),
            "metadata": {
                "inspection_status": SourceHealthStatus.PENDING.value,
                "enable_after_inspection": True,
                "inspection_version": 1,
            },
        }
    )

    DataLayersDialog._inspection_finished(dialog, source.id, inspection, "")

    assert dialog.changes == DataLayerChanges(elevation=True)
    replayed = []
    dialog.post_close_changes.connect(replayed.append)
    assert replayed == []
