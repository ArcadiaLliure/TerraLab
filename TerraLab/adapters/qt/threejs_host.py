"""Qt WebEngine host presenter for Three.js hosted surface rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from PyQt5.QtCore import QUrl, pyqtSlot
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from TerraLab.application.ports.rendering import HostedSurfaceTarget
from TerraLab.render.threejs.bridge import ThreeJSBridge
from TerraLab.scene.contracts import JSONValue


class ThreeJSWebEngineHostPresenter(QWidget):
    """Qt presentation host embedding WebEngine and binding local Three.js bridge."""

    def __init__(
        self,
        surface_id: str = "threejs-hosted-surface-0",
        bridge: ThreeJSBridge | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._surface_id = surface_id
        self._bridge = bridge or ThreeJSBridge()

        self._view = QWebEngineView(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)
        self.setLayout(layout)

        self._asset_path = (
            Path(__file__).resolve().parents[2]
            / "render"
            / "threejs"
            / "assets"
            / "diagnostic_runner.html"
        )
        if self._asset_path.exists():
            self._view.setUrl(QUrl.fromLocalFile(str(self._asset_path)))

        self._bridge.set_outbound_handler(self._on_bridge_outbound)

    @property
    def surface_id(self) -> str:
        return self._surface_id

    @property
    def bridge(self) -> ThreeJSBridge:
        return self._bridge

    def get_target(self) -> HostedSurfaceTarget:
        w = max(1, self.width())
        h = max(1, self.height())
        return HostedSurfaceTarget(surface_id=self._surface_id, width=w, height=h)

    def _on_bridge_outbound(self, msg: Mapping[str, JSONValue]) -> None:
        # In a full WebChannel setup, message is posted to JS transport
        pass

    @pyqtSlot(str)
    def receive_js_message(self, raw_json: str) -> None:
        self._bridge.receive_inbound(raw_json)

    def closeEvent(self, event: Any) -> None:
        self._bridge.close()
        super().closeEvent(event)
