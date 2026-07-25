from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication, QDialogButtonBox, QWidget

from TerraLab.data.copernicus import (
    BBoxWgs84,
    DownloadRequest,
    WMS_URL,
)
from TerraLab.ui.copernicus_orthophoto_dialog import (
    CopernicusMapBridge,
    CopernicusOrthophotoSelectionDialog,
    build_map_html,
)

_TEST_APP = None


def _application() -> QApplication:
    global _TEST_APP
    _TEST_APP = QApplication.instance() or QApplication([])
    return _TEST_APP


class _FakeSettings:
    def __init__(self) -> None:
        self.attributes = []

    def setAttribute(self, attribute, enabled) -> None:
        self.attributes.append((attribute, bool(enabled)))


class _FakePage(QObject):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.channel = None
        self.scripts: list[str] = []

    def setWebChannel(self, channel) -> None:
        self.channel = channel

    def runJavaScript(self, script: str) -> None:
        self.scripts.append(str(script))


class _FakeWebView(QWidget):
    loadFinished = pyqtSignal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._settings = _FakeSettings()
        self._page = _FakePage(self)
        self.html = ""
        self.base_url = None
        self.stopped = False

    def settings(self):
        return self._settings

    def page(self):
        return self._page

    def setHtml(self, html: str, base_url) -> None:
        self.html = str(html)
        self.base_url = base_url

    def stop(self) -> None:
        self.stopped = True


def _dialog(initial_bbox=None) -> CopernicusOrthophotoSelectionDialog:
    _application()
    return CopernicusOrthophotoSelectionDialog(
        initial_bbox=initial_bbox,
        web_view_factory=_FakeWebView,
    )


def test_bridge_preserves_explicit_west_south_east_north_order():
    bridge = CopernicusMapBridge()
    received = []
    errors = []
    bridge.bboxChanged.connect(
        lambda west, south, east, north: received.append(
            (west, south, east, north)
        )
    )
    bridge.error.connect(errors.append)

    bridge.selectionChanged(-9.25, 36.0, 3.5, 44.75)

    assert received == [(-9.25, 36.0, 3.5, 44.75)]
    assert errors == []


@pytest.mark.parametrize(
    "bounds",
    [
        (1.0, 40.0, 1.0, 41.0),
        (2.0, 41.0, 1.0, 42.0),
        (1.0, 42.0, 2.0, 41.0),
        (float("nan"), 40.0, 2.0, 41.0),
    ],
)
def test_bridge_rejects_empty_inverted_or_non_finite_bounds(bounds):
    bridge = CopernicusMapBridge()
    received = []
    errors = []
    bridge.bboxChanged.connect(lambda *values: received.append(values))
    bridge.error.connect(errors.append)

    bridge.selectionChanged(*bounds)

    assert received == []
    assert errors


def test_map_document_uses_live_official_wms_and_interactive_controls():
    html = build_map_html()

    assert WMS_URL in html
    assert "tileLayer.wms" in html
    assert 'layers: "HRIM_HR_TrueColour_2018"' in html
    assert "tile.openstreetmap.org" in html
    assert "OpenStreetMap contributors" in html
    assert "leaflet.draw.js" in html
    assert "new L.Control.Draw" in html
    assert "draw:editmove" in html
    assert "draw:editresize" in html
    assert "rectangleHandler._startLatLng" in html
    assert "publishBounds(" in html
    assert "resetEuropeView" in html
    assert "selectionCleared" in html
    assert "cursorChanged" in html
    assert "bounds.getWest()" in html
    assert "bounds.getSouth()" in html
    assert "bounds.getEast()" in html
    assert "bounds.getNorth()" in html
    assert "bridge.selectionChanged(" in html
    assert "qrc:///qtwebchannel/qwebchannel.js" in html


def test_dialog_builds_default_compressed_u8_request_and_all_metrics():
    dialog = _dialog()
    dialog.web_view.loadFinished.emit(True)
    dialog.bridge.mapReady()
    dialog.bridge.selectionChanged(-1.0, 40.0, -0.8, 40.15)

    request = dialog.download_request
    assert request is not None
    assert request.bbox_wgs84.as_tuple() == (-1.0, 40.0, -0.8, 40.15)
    assert request.resolution_m == 10.0
    assert request.pixel_type == "U8"
    assert request.compression == "LZ77"
    assert dialog.metric_width.text() != "—"
    assert dialog.metric_height.text() != "—"
    assert dialog.metric_area.text().endswith("km²")
    assert "×" in dialog.metric_raster.text()
    assert dialog.metric_pixels.text() != "—"
    assert dialog.metric_u16.text() != "—"
    assert dialog.metric_u8.text() != "—"
    assert "–" in dialog.metric_compressed.text()
    assert dialog.metric_fragments.text() != "—"
    assert dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()

    dialog.close()


def test_resolution_and_format_controls_rebuild_the_same_download_request():
    dialog = _dialog(BBoxWgs84(-4.0, 39.0, -3.8, 39.2))

    dialog.resolution_combo.setCurrentIndex(1)
    assert dialog.download_request.resolution_m == 20.0

    dialog.resolution_combo.setCurrentIndex(4)
    assert dialog.custom_resolution.isEnabled()
    assert dialog.custom_resolution.minimum() == 10.0
    dialog.custom_resolution.setValue(35.0)
    assert dialog.download_request.resolution_m == 35.0

    dialog.format_combo.setCurrentIndex(0)
    assert dialog.download_request.pixel_type == "U16"
    assert dialog.download_request.compression == "LZ77"

    dialog.format_combo.setCurrentIndex(1)
    assert dialog.download_request.pixel_type == "U8"
    assert dialog.download_request.compression == "NONE"

    dialog.close()


def test_initial_request_restores_resume_critical_controls_and_grid():
    initial = DownloadRequest(
        bbox_wgs84=BBoxWgs84(-8.0, 37.0, -7.5, 37.4),
        resolution_m=40.0,
        pixel_type="U16",
        compression="LZ77",
        output_name="resume-this.tif",
        tile_width_px=3200,
        tile_height_px=3000,
        max_concurrent_requests=3,
    )

    _application()
    dialog = CopernicusOrthophotoSelectionDialog(
        initial_request=initial,
        web_view_factory=_FakeWebView,
    )

    restored = dialog.download_request
    assert restored is not None
    assert restored.bbox_wgs84.as_tuple() == initial.bbox_wgs84.as_tuple()
    assert restored.resolution_m == 40.0
    assert restored.pixel_type == "U16"
    assert restored.compression == "LZ77"
    assert restored.output_name == "resume-this.tif"
    assert restored.tile_width_px == 3200
    assert restored.tile_height_px == 3000
    assert restored.max_concurrent_requests == 3

    dialog.close()


def test_reset_delete_and_initial_rectangle_are_sent_to_the_web_map():
    bbox = BBoxWgs84(0.5, 41.0, 1.0, 41.5)
    dialog = _dialog(bbox)
    page = dialog.web_view.page()

    dialog.bridge.mapReady()
    assert any("setSelectionBounds(" in script for script in page.scripts)

    dialog.reset_europe_view()
    assert page.scripts[-1] == "resetEuropeView();"

    dialog.clear_selection()
    assert page.scripts[-1] == "clearSelectionFromHost();"
    assert dialog.download_request is None
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()

    dialog.close()


def test_web_load_and_javascript_errors_are_visible():
    dialog = _dialog()

    dialog.web_view.loadFinished.emit(False)
    assert dialog.lbl_map_status.property("error") is True
    assert "No s'ha pogut carregar" in dialog.lbl_map_status.text()

    dialog.bridge.reportError("WMS no disponible")
    assert dialog.lbl_map_status.property("error") is True
    assert dialog.lbl_map_status.text() == "WMS no disponible"

    dialog.close()
