from __future__ import annotations

import json
from pathlib import Path

import pytest

from TerraLab.common.data_library import (
    DataLibrary,
    DataLibraryError,
    configured_data_root,
    data_location_pointer_path,
)


def test_selected_library_creates_versioned_layout_and_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("TERRALAB_DATA_ROOT", raising=False)
    root = tmp_path / "science-data"

    library = DataLibrary(root)
    layout = library.initialize(persist_pointer=True)

    assert configured_data_root() == root.resolve()
    assert layout["data_elevation"].is_dir()
    assert layout["data_surface"].is_dir()
    assert layout["cache_weather"].is_dir()
    manifest = json.loads((root / "terralab-data.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["data_root"] == str(root.resolve())
    assert manifest["catalogs"]["geospatial"] == "config/data_sources.json"
    assert not (tmp_path / "appdata" / "TerraLab" / "data").exists()


def test_environment_root_has_priority_over_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    pointer_root = tmp_path / "pointer"
    DataLibrary(pointer_root).initialize(persist_pointer=True)
    environment_root = tmp_path / "environment"
    monkeypatch.setenv("TERRALAB_DATA_ROOT", str(environment_root))

    assert configured_data_root() == environment_root.resolve()


def test_corrupt_pointer_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("TERRALAB_DATA_ROOT", raising=False)
    pointer = data_location_pointer_path()
    pointer.parent.mkdir(parents=True)
    pointer.write_text("{broken", encoding="utf-8")

    with pytest.raises(DataLibraryError, match="Cannot read"):
        configured_data_root()


def test_legacy_migration_rewrites_only_managed_catalogue_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("TERRALAB_DATA_ROOT", raising=False)
    legacy = tmp_path / "appdata" / "TerraLab"
    managed = legacy / "data" / "elevation" / "local" / "dem.asc"
    managed.parent.mkdir(parents=True)
    managed.write_text("dem", encoding="utf-8")
    external = tmp_path / "external" / "own-dem.asc"
    external.parent.mkdir()
    external.write_text("external", encoding="utf-8")
    catalogue = legacy / "config" / "data_sources.json"
    catalogue.parent.mkdir(parents=True)
    catalogue.write_text(
        json.dumps(
            {
                "sources": [
                    {"id": "managed", "path": str(managed)},
                    {"id": "external", "path": str(external)},
                ]
            }
        ),
        encoding="utf-8",
    )
    destination = DataLibrary(tmp_path / "library")

    report = destination.migrate_from_legacy(legacy)

    assert report.files_copied == 2
    copied = destination.root / "data" / "earth" / "elevation" / "local" / "dem.asc"
    assert copied.read_text(encoding="utf-8") == "dem"
    migrated = json.loads(
        (destination.root / "config" / "data_sources.json").read_text(encoding="utf-8")
    )
    by_id = {item["id"]: item["path"] for item in migrated["sources"]}
    assert Path(by_id["managed"]) == copied
    assert Path(by_id["external"]) == external
    assert managed.exists()


def test_versioned_migration_keeps_external_assets_linked(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("TERRALAB_DATA_ROOT", raising=False)
    old = DataLibrary(tmp_path / "old")
    old.initialize()
    managed = old.layout()["data_ngc"] / "NGC.csv"
    managed.write_text("managed", encoding="utf-8")
    external = tmp_path / "external.csv"
    external.write_text("external", encoding="utf-8")
    old.update_asset("ngc_catalog", ready=True, path=str(managed))
    old.update_asset("milkyway_texture", ready=True, path=str(external))
    new = DataLibrary(tmp_path / "new")

    new.migrate_from_library(old.root)

    assert Path(new.asset_state("ngc_catalog")["path"]).is_relative_to(new.root)
    assert Path(new.asset_state("milkyway_texture")["path"]) == external
    assert managed.exists()

