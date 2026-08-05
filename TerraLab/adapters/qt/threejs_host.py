"""Qt WebEngine host for the locally bundled Three.js presentation surface."""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from threading import Thread
from typing import Any
from urllib.parse import unquote, urlsplit

from PyQt5.QtCore import (
    QFile,
    QEvent,
    QIODevice,
    QObject,
    QTimer,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)
from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtWebEngineWidgets import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineView,
)
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from TerraLab.core.rendering_contracts.contracts import HostedSurfaceTarget
from TerraLab.render.threejs.bridge import (
    ResourceRegistry,
    ThreeJSBridge,
)
from TerraLab.render.threejs.protocol import (
    OP_ACK,
    OP_ERROR,
    OP_READY,
    BridgeViewport,
    BridgeVisibility,
)
from TerraLab.scene.contracts import JSONValue, thaw_json_mapping


_HOST_DOCUMENT_NAME = "threejs_host.html"
_HOST_ASSET_MIME_TYPES = {
    _HOST_DOCUMENT_NAME: b"text/html",
    "qwebchannel.js": b"application/javascript",
    "three.min.js": b"application/javascript",
    "threejs_runner.js": b"application/javascript",
}

logger = logging.getLogger(__name__)


class LoggingWebEnginePage(QWebEnginePage):
    """Forward browser-console diagnostics to the Python application log."""

    def javaScriptConsoleMessage(  # noqa: N802 - Qt callback name.
        self,
        level: object,
        message: str,
        line_number: int,
        source_id: str,
    ) -> None:
        try:
            log_level = {
                0: logging.INFO,
                1: logging.WARNING,
            }.get(int(level), logging.ERROR)
        except (TypeError, ValueError):
            log_level = logging.ERROR
        logger.log(
            log_level,
            "Three.js console %s:%s: %s",
            source_id,
            line_number,
            message,
        )


class ThreeJSLoopbackAssetServer:
    """Serve the bundled host and versioned buffers on one loopback origin."""

    def __init__(
        self,
        resources: ResourceRegistry,
        asset_root: Path,
        channel_script: bytes,
    ) -> None:
        self._resources = resources
        self._asset_root = asset_root.resolve()
        self._channel_script = channel_script
        self._token = token_urlsafe(32)
        self._server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            self._request_handler_type(),
        )
        self._server.daemon_threads = True
        self._thread = Thread(
            target=self._server.serve_forever,
            name="terralab-threejs-loopback",
            daemon=True,
        )
        self._started = False
        self._closed = False

    @property
    def host_url(self) -> str:
        port = int(self._server.server_address[1])
        return f"http://127.0.0.1:{port}/{self._token}/{_HOST_DOCUMENT_NAME}"

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            self._server.shutdown()
            self._thread.join(timeout=1.0)
        self._server.server_close()

    def _request_handler_type(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class RequestHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name.
                server._handle_request(self, send_body=True)

            def do_HEAD(self) -> None:  # noqa: N802 - stdlib callback name.
                server._handle_request(self, send_body=False)

            def log_message(self, _format: str, *args: object) -> None:
                return

        return RequestHandler

    def _handle_request(
        self,
        request: BaseHTTPRequestHandler,
        *,
        send_body: bool,
    ) -> None:
        parsed = urlsplit(request.path)
        prefix = f"/{self._token}/"
        if parsed.query or parsed.fragment or not parsed.path.startswith(prefix):
            request.send_error(404)
            return
        relative_path = unquote(parsed.path.removeprefix(prefix))
        resource_path = self._resources.path_for_uri(relative_path)
        if resource_path is not None:
            self._send_file(
                request,
                resource_path,
                b"application/octet-stream",
                "private, max-age=31536000, immutable",
                send_body=send_body,
            )
            return
        if relative_path == "qwebchannel.js":
            self._send_bytes(
                request,
                self._channel_script,
                _HOST_ASSET_MIME_TYPES[relative_path],
                "no-cache",
                send_body=send_body,
            )
            return
        mime_type = _HOST_ASSET_MIME_TYPES.get(relative_path)
        path = self._asset_root / relative_path
        if mime_type is None or not path.is_file():
            request.send_error(404)
            return
        self._send_file(
            request,
            path,
            mime_type,
            "no-cache",
            send_body=send_body,
        )

    @staticmethod
    def _send_file(
        request: BaseHTTPRequestHandler,
        path: Path,
        mime_type: bytes,
        cache_control: str,
        *,
        send_body: bool,
    ) -> None:
        try:
            size = path.stat().st_size
            stream = path.open("rb")
        except OSError:
            request.send_error(404)
            return
        with stream:
            request.send_response(200)
            request.send_header("Content-Type", mime_type.decode("ascii"))
            request.send_header("Content-Length", str(size))
            request.send_header("Cache-Control", cache_control)
            request.send_header("X-Content-Type-Options", "nosniff")
            request.end_headers()
            if send_body:
                shutil.copyfileobj(stream, request.wfile)

    @staticmethod
    def _send_bytes(
        request: BaseHTTPRequestHandler,
        payload: bytes,
        mime_type: bytes,
        cache_control: str,
        *,
        send_body: bool,
    ) -> None:
        request.send_response(200)
        request.send_header("Content-Type", mime_type.decode("ascii"))
        request.send_header("Content-Length", str(len(payload)))
        request.send_header("Cache-Control", cache_control)
        request.send_header("X-Content-Type-Options", "nosniff")
        request.end_headers()
        if send_body:
            request.wfile.write(payload)


class ThreeJSWebChannelBridge(QObject):
    """Typed QWebChannel endpoint; it only transports allowlisted messages."""

    outbound = pyqtSignal(str)
    inbound = pyqtSignal(str)

    @pyqtSlot(str)
    def receive(self, raw_message: str) -> None:
        self.inbound.emit(raw_message)


class ThreeJSWebEngineHostPresenter(QWidget):
    """Own one WebEngine/WebGL surface and its deterministic lifecycle."""

    bridge_message_received = pyqtSignal(object)
    webgl_ready = pyqtSignal()
    webgl_error = pyqtSignal(str)

    def __init__(
        self,
        surface_id: str = "threejs-hosted-surface-0",
        bridge: ThreeJSBridge | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._surface_id = surface_id
        self._bridge = bridge or ThreeJSBridge()
        self._surface_closed = False
        self._screen_connection: object | None = None
        self._window_connection: object | None = None
        self._asset_root = (
            Path(__file__).resolve().parents[2]
            / "render"
            / "threejs"
            / "assets"
        )
        self._asset_path = self._asset_root / _HOST_DOCUMENT_NAME
        if not self._asset_path.is_file():
            raise FileNotFoundError(
                f"Missing local Three.js host asset: {self._asset_path}"
            )
        self._channel_script = self._load_qwebchannel_script()

        self._profile = QWebEngineProfile(self)
        self._page_instance = LoggingWebEnginePage(self._profile, self)
        self._view = QWebEngineView(self)
        self._view.setPage(self._page_instance)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)
        self.setLayout(layout)

        self._web_channel_bridge = ThreeJSWebChannelBridge(self)
        page = self._page()
        self._asset_server = ThreeJSLoopbackAssetServer(
            self._bridge.resources,
            self._asset_root,
            self._channel_script,
        )
        self._asset_server.start()
        self._channel = QWebChannel(page)
        self._channel.registerObject("threeBridge", self._web_channel_bridge)
        page.setWebChannel(self._channel)
        self._web_channel_bridge.inbound.connect(self.receive_js_message)
        self._view.loadStarted.connect(self._on_page_load_started)
        self._view.loadProgress.connect(self._on_page_load_progress)
        self._view.loadFinished.connect(self._on_page_load_finished)
        self._view.renderProcessTerminated.connect(
            self._on_render_process_terminated
        )
        self.webgl_error.connect(self._log_webgl_error)
        self._bridge.set_outbound_handler(self._on_bridge_outbound)

        self._view.setUrl(QUrl(self._asset_server.host_url))

    @property
    def surface_id(self) -> str:
        return self._surface_id

    @property
    def bridge(self) -> ThreeJSBridge:
        return self._bridge

    @property
    def web_view(self) -> QWebEngineView:
        """Expose the actual Qt host to focused integration tests only."""

        return self._view

    def get_target(self) -> HostedSurfaceTarget:
        """Report logical Qt size separately from the physical DPR."""

        return HostedSurfaceTarget(
            surface_id=self._surface_id,
            width=max(1, self.width()),
            height=max(1, self.height()),
            device_pixel_ratio=max(1.0, float(self.devicePixelRatioF())),
        )

    def restart_surface(self) -> None:
        """Recreate renderer/camera after a recoverable WebGL context loss."""

        if self._surface_closed or not self._bridge.is_started:
            return
        self._bridge.restart(self._bridge_viewport())

    @staticmethod
    def is_rendered_ack(message: Mapping[str, object]) -> bool:
        """Recognise the bridge completion ACK without leaking protocol symbols."""

        payload = message.get("payload")
        return (
            str(message.get("op", "")) == OP_ACK
            and isinstance(payload, Mapping)
            and bool(payload.get("rendered"))
        )

    def close_surface(self) -> None:
        """Release the WebGL context and bridge resources exactly once."""

        if self._surface_closed:
            return
        self._surface_closed = True
        # The direct call covers shutdown while the QWebChannel event queue is
        # being drained; CLOSE is still emitted so normal lifecycle is visible.
        page = self._view.page()
        if page is not None:
            page.runJavaScript(
                "window.threeJSRunner && window.threeJSRunner.dispose();"
            )
        self._view.stop()
        self._view.setUrl(QUrl("about:blank"))
        self._asset_server.close()
        self._bridge.close()

    def resizeEvent(  # type: ignore[reportIncompatibleMethodOverride]
        self,
        event: Any,
    ) -> None:
        super().resizeEvent(event)
        self._publish_viewport()

    def showEvent(  # type: ignore[reportIncompatibleMethodOverride]
        self,
        event: Any,
    ) -> None:
        super().showEvent(event)
        self._connect_screen_notifications()
        self._set_surface_visibility(visible=True)

    def hideEvent(  # type: ignore[reportIncompatibleMethodOverride]
        self,
        event: Any,
    ) -> None:
        self._set_surface_visibility(visible=False)
        super().hideEvent(event)

    def event(self, event: QEvent) -> bool:  # type: ignore[override]
        result = super().event(event)
        if event.type() == getattr(QEvent, "ScreenChangeInternal", None):
            self._connect_screen_notifications()
            self._publish_viewport()
        elif event.type() == getattr(QEvent, "WindowStateChange", None):
            self._set_surface_visibility(
                visible=not self.isMinimized() and self.isVisible()
            )
        return result

    @pyqtSlot(str)
    def receive_js_message(self, raw_json: str) -> None:
        """Accept only a validated bridge message from the local web host."""

        try:
            message = self._bridge.receive_inbound(raw_json)
        except (TypeError, ValueError) as exc:
            self.webgl_error.emit(f"Invalid Three.js bridge message: {exc}")
            return
        self.bridge_message_received.emit(message)
        op = str(message["op"])
        payload = message["payload"]
        if not isinstance(payload, Mapping):
            self.webgl_error.emit("Three.js bridge payload must be an object")
            return
        if op == OP_READY:
            status = str(payload.get("status", ""))
            if status == "channel_ready":
                self._start_surface_when_channel_is_ready()
            elif self._bridge.is_ready:
                self.webgl_ready.emit()
        elif op == OP_ERROR:
            detail = str(payload.get("message", "Unknown WebGL error"))
            self.webgl_error.emit(detail)
            if str(payload.get("code", "")) == "webgl_context_lost":
                QTimer.singleShot(0, self.restart_surface)
        elif op == OP_ACK:
            # ACK handling belongs to HostedSurfacePresenter; the host has no
            # scene policy and never decides what should be rendered.
            return

    def closeEvent(  # type: ignore[reportIncompatibleMethodOverride]
        self,
        event: Any,
    ) -> None:
        self.close_surface()
        super().closeEvent(event)

    def _on_page_load_finished(self, ok: bool) -> None:
        if not ok and not self._surface_closed:
            self.webgl_error.emit("The local Three.js host page did not load")
        elif ok:
            logger.debug("Three.js host page loaded")

    @staticmethod
    def _on_page_load_started() -> None:
        logger.debug("Loading local Three.js host page")

    @staticmethod
    def _on_page_load_progress(progress: int) -> None:
        logger.debug("Three.js host page load progress: %s%%", progress)

    def _on_render_process_terminated(
        self,
        status: object,
        exit_code: int,
    ) -> None:
        if not self._surface_closed:
            self.webgl_error.emit(
                "Three.js render process terminated "
                f"(status={status}, exit_code={exit_code})"
            )

    @staticmethod
    def _log_webgl_error(detail: str) -> None:
        """Make host failures observable outside Chromium's dev console."""

        logger.error("Three.js host: %s", detail)

    def _on_bridge_outbound(self, message: Mapping[str, JSONValue]) -> None:
        if self._surface_closed:
            return
        raw_message = json.dumps(
            thaw_json_mapping(message),
            separators=(",", ":"),
        )
        self._web_channel_bridge.outbound.emit(raw_message)

    def _start_surface_when_channel_is_ready(self) -> None:
        if self._surface_closed or self._bridge.is_started:
            return
        self._bridge.start(self._bridge_viewport())
        self._set_surface_visibility(visible=self.isVisible())

    def _publish_viewport(self) -> None:
        if not self._surface_closed and self._bridge.is_started:
            self._bridge.resize(self._bridge_viewport())

    def _set_surface_visibility(self, *, visible: bool) -> None:
        if not self._surface_closed and self._bridge.is_started:
            self._bridge.set_visibility(
                BridgeVisibility(visible=visible, suspended=not visible)
            )

    def _bridge_viewport(self) -> BridgeViewport:
        target = self.get_target()
        return BridgeViewport(
            width=target.width,
            height=target.height,
            device_pixel_ratio=target.device_pixel_ratio,
        )

    def _connect_screen_notifications(self) -> None:
        window = self.window()
        if window is None:
            return
        window_handle = window.windowHandle()
        if window_handle is None:
            return
        if window_handle is not self._window_connection:
            self._window_connection = window_handle
            window_handle.screenChanged.connect(self._on_screen_changed)
        self._on_screen_changed(window_handle.screen())

    def _on_screen_changed(self, screen: object) -> None:
        if screen is self._screen_connection:
            return
        self._screen_connection = screen
        changed = getattr(screen, "logicalDotsPerInchChanged", None)
        if changed is not None:
            changed.connect(self._publish_viewport)
        self._publish_viewport()

    def _page(self) -> QWebEnginePage:
        page = self._view.page()
        if page is None:
            raise RuntimeError("QWebEngineView has no page")
        return page

    @staticmethod
    def _load_qwebchannel_script() -> bytes:
        script = QFile(":/qtwebchannel/qwebchannel.js")
        if not script.open(QIODevice.ReadOnly):
            raise RuntimeError("Qt QWebChannel client script is unavailable")
        try:
            return bytes(script.readAll())
        finally:
            script.close()
