from pathlib import Path
from types import SimpleNamespace

from TerraLab.ui.astronomical_widget import (
    AstronomicalWidget,
    GAIA_CATALOG_AVAILABLE,
    GAIA_CATALOG_NOT_CONFIGURED,
    GAIA_CATALOG_UNAVAILABLE,
)


def _widget_stub(runtime_layout: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        runtime_layout=runtime_layout,
        gaia_catalog_status=GAIA_CATALOG_NOT_CONFIGURED,
    )


def test_gaia_manifest_is_never_discovered_from_the_working_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    accidental = tmp_path / "accidental"
    accidental.mkdir()
    (accidental / "tile_manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(accidental)
    widget = _widget_stub({})

    resolved = AstronomicalWidget._resolve_gaia_manifest_path(widget)

    assert resolved is None
    assert widget.gaia_catalog_status == GAIA_CATALOG_NOT_CONFIGURED


def test_gaia_manifest_resolution_is_identical_from_different_cwds(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    gaia_dir = library / "data" / "gaia"
    gaia_dir.mkdir(parents=True)
    manifest = gaia_dir / "tile_manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    widget = _widget_stub(
        {
            "root": str(library),
            "data_gaia": str(Path("data") / "gaia"),
        }
    )

    monkeypatch.chdir(first_cwd)
    first = AstronomicalWidget._resolve_gaia_manifest_path(widget)
    monkeypatch.chdir(second_cwd)
    second = AstronomicalWidget._resolve_gaia_manifest_path(widget)

    assert first == manifest
    assert second == manifest
    assert widget.gaia_catalog_status == GAIA_CATALOG_AVAILABLE


def test_relative_gaia_path_requires_an_absolute_library_root(
    tmp_path: Path,
) -> None:
    widget = _widget_stub(
        {
            "root": "relative-library",
            "data_gaia": str(Path("data") / "gaia"),
        }
    )

    resolved = AstronomicalWidget._resolve_gaia_manifest_path(widget)

    assert resolved is None
    assert widget.gaia_catalog_status == GAIA_CATALOG_UNAVAILABLE
