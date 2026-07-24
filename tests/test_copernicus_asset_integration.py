from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from PyQt5.QtWidgets import QDialog, QMessageBox
from rasterio.transform import from_origin

from TerraLab.common.data_library import DataLibrary
from TerraLab.data import assets_manager as assets_module
from TerraLab.data.assets_manager import (
    AssetManager,
    AssetOperationCancelled,
)
from TerraLab.data.copernicus_orthophoto import (
    ADAPTED_ATTRIBUTION,
    BBoxWgs84,
    DownloadRequest,
    estimate_selection,
)
from TerraLab.data.layer_manager import LayerId, LayerManager
from TerraLab.terrain.data_sources import (
    DataSourceRegistry,
    LayerRole,
    LayerType,
    SelectionMode,
    SurfaceMode,
)
from TerraLab.ui import copernicus_orthophoto_dialog as dialog_module
from TerraLab.ui.onboarding_dialogs import AssetOnboardingDialog


def _write_rgb(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=3,
        dtype="uint8",
        crs="EPSG:3035",
        transform=from_origin(4_000_000.0, 3_000_000.0, 80.0, 80.0),
    ) as dataset:
        dataset.write(np.full((3, 4, 4), 96, dtype=np.uint8))
    return path


def _manager(tmp_path: Path, monkeypatch) -> AssetManager:
    monkeypatch.setattr(
        "TerraLab.data.assets_manager.set_config_value",
        lambda *_args: None,
    )
    manager = AssetManager(DataLibrary(tmp_path / "library"))
    manager.data_sources = DataSourceRegistry(
        tmp_path / "library" / "config" / "data_sources.json",
        legacy_reader={},
    )
    return manager


def _request() -> DownloadRequest:
    return DownloadRequest(
        bbox_wgs84=BBoxWgs84(
            west=5.0,
            south=45.0,
            east=5.01,
            north=45.01,
        ),
        resolution_m=80.0,
        pixel_type="U8",
        compression="LZ77",
    )


def test_copernicus_result_is_registered_without_changing_surface_mode(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path, monkeypatch)
    manager.data_sources.set_surface_mode(SurfaceMode.LAND_COVER)
    request = _request()
    estimate = estimate_selection(request)
    calls = {}

    class FakeCopernicusManager:
        def __init__(self, download_root, dataset_root):
            calls["download_root"] = Path(download_root)
            calls["dataset_root"] = Path(dataset_root)

        def run(self, received, *, progress_callback=None, cancelled=None):
            calls["request"] = received
            output = _write_rgb(
                calls["dataset_root"] / "copernicus-fixture.tif"
            )
            manifest = (
                calls["download_root"] / "fixture-job" / "manifest.json"
            )
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text('{"status": "complete"}\n', encoding="utf-8")
            if progress_callback is not None:
                progress_callback(75.0, "Mosaic de prova")
            return SimpleNamespace(
                output_path=output,
                manifest_path=manifest,
                request=received,
                estimate=estimate,
            )

    monkeypatch.setattr(
        assets_module,
        "CopernicusOrthophotoManager",
        FakeCopernicusManager,
    )
    events = []
    result = manager.download_and_prepare(
        "orthophoto",
        progress_callback=lambda percent, message: events.append(
            (percent, message)
        ),
        options={
            "copernicus_request": request.to_dict(),
            "display_name": "Copernicus fixture",
        },
    )

    assert result["ok"] is True
    assert Path(result["stored_in"]).is_file()
    assert calls["request"].to_dict() == request.to_dict()
    sources = manager.data_sources.list_sources(
        LayerType.ORTHOPHOTO_RGB
    )
    assert len(sources) == 1
    source = sources[0]
    assert source.path == result["stored_in"]
    assert source.provenance == "managed"
    assert source.attribution == ADAPTED_ATTRIBUTION
    assert source.metadata["managed"] is True
    assert source.metadata["product_id"] == (
        "copernicus-hrim-2018-true-colour"
    )
    assert source.metadata["download_request"] == request.to_dict()
    assert source.metadata["cropped"] is True
    assert source.metadata["adapted_by_terralab"] is True
    selection = manager.data_sources.get_selection(
        LayerRole.ORTHOPHOTO
    )
    assert selection.mode is SelectionMode.MANUAL
    assert selection.source_id == source.id
    assert manager.data_sources.surface_mode is SurfaceMode.LAND_COVER
    state = manager.library.asset_state("orthophoto")
    assert state["install_state"] == "prepared"
    assert state["source_id"] == source.id
    assert state["download_manifest_path"] == result["manifest_path"]
    assert events[-1][0] == 100.0


def test_user_orthophoto_is_not_falsely_attributed_to_copernicus(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path, monkeypatch)
    local = _write_rgb(tmp_path / "external" / "user-ortho.tif")

    source = manager.register_external_source("orthophoto", local)

    assert source.layer_type is LayerType.ORTHOPHOTO_RGB
    assert "Copernicus" not in source.attribution
    assert "product_id" not in source.metadata
    assert source.metadata["official_url"] == ""


def test_cancelled_copernicus_job_is_persisted_as_paused(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path, monkeypatch)
    request = _request()

    class CancelledManager:
        def __init__(self, _download_root, _dataset_root):
            pass

        def run(self, _request, *, progress_callback=None, cancelled=None):
            assert cancelled is not None and cancelled()
            raise RuntimeError("core stopped")

    monkeypatch.setattr(
        assets_module,
        "CopernicusOrthophotoManager",
        CancelledManager,
    )

    with pytest.raises(AssetOperationCancelled):
        manager.download_and_prepare(
            "orthophoto",
            options={"copernicus_request": request.to_dict()},
            cancelled=lambda: True,
        )

    state = manager.library.asset_state("orthophoto")
    assert state["install_state"] == "paused"
    assert state["copernicus_request"] == request.to_dict()
    assert manager.asset_status("orthophoto")["resumable"] is True
    assert manager.data_sources.list_sources(
        LayerType.ORTHOPHOTO_RGB
    ) == []


def test_orthophoto_descriptor_advertises_automatic_download(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path, monkeypatch)

    descriptor = LayerManager(manager).descriptor(
        LayerId.EARTH_ORTHOPHOTO
    )

    assert descriptor.supports_download is True
    assert manager.get_spec("orthophoto").auto_download_url is None


def test_removing_orthophoto_includes_resumable_copernicus_fragments(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path, monkeypatch)
    download_root = (
        Path(manager.layout["downloads"]) / "copernicus-hrim-2018"
    )
    fragment = download_root / "job-id" / "fragments" / "r00000_c00000.tif"
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_bytes(b"resumable-fragment")

    preview = manager.removal_preview("orthophoto")

    assert str(download_root.resolve()) in preview.managed_paths
    report = manager.remove_asset_data("orthophoto")
    assert str(download_root.resolve()) in report.deleted_paths
    assert not download_root.exists()


def test_onboarding_passes_complete_previous_request_to_async_preflight(
    monkeypatch,
):
    previous = _request()
    captured = {}

    class FakeSelectionDialog:
        def __init__(
            self,
            parent=None,
            *,
            initial_request=None,
            **_kwargs,
        ):
            captured["initial_request"] = initial_request
            self.download_request = initial_request or previous
            self._estimate = estimate_selection(self.download_request)

        def exec_(self):
            return QDialog.Accepted

    monkeypatch.setattr(
        dialog_module,
        "CopernicusOrthophotoSelectionDialog",
        FakeSelectionDialog,
    )
    preflights = []
    harness = SimpleNamespace(
        manager=SimpleNamespace(
            library=SimpleNamespace(
                asset_state=lambda _asset_id: {
                    "copernicus_request": previous.to_dict()
                }
            )
        ),
        _start_copernicus_preflight=lambda request, estimate: (
            preflights.append((request, estimate))
        ),
    )

    AssetOnboardingDialog._auto_download_copernicus_orthophoto(
        harness
    )

    assert captured["initial_request"].to_dict() == previous.to_dict()
    assert len(preflights) == 1
    assert preflights[0][0].to_dict() == previous.to_dict()


def test_confirmed_preflight_launches_existing_background_job():
    request = _request()
    starts = []
    harness = SimpleNamespace(
        _start_job=lambda **kwargs: starts.append(kwargs),
    )

    AssetOnboardingDialog._launch_copernicus_download(harness, request)

    assert len(starts) == 1
    assert starts[0]["mode"] == "download"
    assert starts[0]["files"] == []
    assert (
        starts[0]["options"]["copernicus_request"]
        == request.to_dict()
    )


def test_confirmation_blocks_bbox_outside_service_coverage(monkeypatch):
    errors = []
    monkeypatch.setattr(
        "TerraLab.ui.onboarding_dialogs.QMessageBox.critical",
        lambda _parent, title, message: errors.append((title, message)),
    )
    request = DownloadRequest(
        bbox_wgs84=BBoxWgs84(
            west=-170.0,
            south=-80.0,
            east=-169.0,
            north=-79.0,
        ),
        resolution_m=80.0,
        pixel_type="U8",
        compression="LZ77",
    )
    harness = SimpleNamespace(
        manager=SimpleNamespace(
            library=SimpleNamespace(root=Path.cwd())
        )
    )

    accepted = AssetOnboardingDialog._confirm_copernicus_download(
        harness,
        request,
        estimate_selection(request),
    )

    assert accepted is False
    assert errors
    assert "cobertura" in errors[0][1].lower()


def test_confirmation_uses_async_nodata_result_without_network(
    tmp_path,
    monkeypatch,
):
    request = _request()
    questions = []
    monkeypatch.setattr(
        "TerraLab.data.copernicus_orthophoto.estimate_nodata_fraction",
        lambda *_args, **_kwargs: pytest.fail(
            "NoData network probe must not run in the UI confirmation"
        ),
    )
    monkeypatch.setattr(
        "TerraLab.ui.onboarding_dialogs.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=10**12),
    )
    monkeypatch.setattr(
        "TerraLab.ui.onboarding_dialogs.QMessageBox.question",
        lambda _parent, title, message, *_args: (
            questions.append((title, message)) or QMessageBox.Yes
        ),
    )
    harness = SimpleNamespace(
        manager=SimpleNamespace(
            library=SimpleNamespace(root=tmp_path)
        ),
        _copernicus_estimate_value=(
            AssetOnboardingDialog._copernicus_estimate_value
        ),
    )

    accepted = AssetOnboardingDialog._confirm_copernicus_download(
        harness,
        request,
        estimate_selection(request),
        nodata_fraction=0.75,
    )

    assert accepted is True
    assert questions
    assert "75.0% de mar o NoData" in questions[0][1]
    assert "RGB U16 natiu" in questions[0][1]
