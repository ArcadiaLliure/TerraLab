"""Finestra cinematogràfica de primer inici basada en Qt WebEngine."""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QTimer,
    QUrl,
    Qt,
    pyqtSignal,
)
from PyQt5.QtWidgets import QMainWindow
from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtWebEngineWidgets import (
    QWebEnginePage,
    QWebEngineSettings,
    QWebEngineView,
)

from TerraLab.ui.onboarding.first_run_manager import FirstRunManager
from TerraLab.ui.onboarding.onboarding_bridge import OnboardingBridge


def onboarding_document_path() -> Path:
    """Retorna el recurs instal·lat sense dependre del directori de treball."""

    path = Path(__file__).resolve().parent / "assets" / "onboarding.html"
    if not path.is_file():
        raise FileNotFoundError(
            f"No s'ha trobat el document de l'onboarding: {path}"
        )
    return path


class OnboardingWindow(QMainWindow):
    """Experiència independent, sense marc i a pantalla completa."""

    completed = pyqtSignal()
    cancelled = pyqtSignal()
    load_failed = pyqtSignal(str)

    def __init__(
        self,
        parent=None,
        *,
        first_run: bool = False,
        manager: FirstRunManager | None = None,
    ) -> None:
        super().__init__(
            parent,
            Qt.Window  # type: ignore[reportAttributeAccessIssue]
            | Qt.FramelessWindowHint,  # type: ignore[reportAttributeAccessIssue]
        )
        self._is_first_run = bool(first_run)
        self._manager = manager or FirstRunManager()
        self._finishing = False
        self._loaded = False
        self._fade_started = False
        self._fade_animation: QPropertyAnimation | None = None

        self.setObjectName("onboardingWindow")
        self.setWindowTitle("TerraLab · Primer viatge")
        self.setAttribute(
            Qt.WA_DeleteOnClose,  # type: ignore[reportAttributeAccessIssue]
            True,
        )
        self.setStyleSheet("#onboardingWindow { background: #02040a; }")
        self.setWindowOpacity(0.0)

        self.web_view = QWebEngineView(self)
        self.web_view.setContextMenuPolicy(
            Qt.NoContextMenu  # type: ignore[reportAttributeAccessIssue]
        )
        self.web_view.setStyleSheet("background: #02040a;")
        self.setCentralWidget(self.web_view)

        settings = self.web_view.settings()
        assert settings is not None
        settings.setAttribute(
            QWebEngineSettings.LocalContentCanAccessFileUrls,
            True,
        )

        self.bridge = OnboardingBridge(self)
        page = self.web_view.page()
        assert page is not None
        self.page: QWebEnginePage = page
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("onboardingBridge", self.bridge)
        self.page.setWebChannel(self.channel)

        self.bridge.finished.connect(self._finish_from_web)
        self.web_view.loadFinished.connect(self._on_load_finished)
        self.web_view.load(QUrl.fromLocalFile(str(onboarding_document_path())))

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._loaded:
            QTimer.singleShot(0, self._start_fade_in)

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            self._finishing = True
            message = "No s'ha pogut carregar el Primer viatge de TerraLab."
            self.load_failed.emit(message)
            self.close()
            return
        self._loaded = True
        if self.isVisible():
            self._start_fade_in()

    def _start_fade_in(self) -> None:
        if self._fade_started or not self._loaded:
            return
        self._fade_started = True
        # La pàgina pot acabar de carregar mentre la finestra encara té la
        # geometria inicial de Qt. Redibuixem l'escena abans de fer-la visible.
        self.page.runJavaScript(
            "resize(); go(typeof cur === 'number' ? cur : 0);"
        )
        self._fade_animation = QPropertyAnimation(
            self,
            b"windowOpacity",
            self,
        )
        self._fade_animation.setDuration(650)
        self._fade_animation.setStartValue(0.0)
        self._fade_animation.setEndValue(1.0)
        self._fade_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_animation.start()

    def _finish_from_web(self) -> None:
        if self._finishing:
            return
        self._finishing = True
        if self._is_first_run:
            self._manager.mark_completed()
        self.completed.emit()
        self.close()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.page.runJavaScript(
            "window.terralabShutdown && window.terralabShutdown();"
        )
        if self._is_first_run and not self._finishing:
            self.cancelled.emit()
        super().closeEvent(event)
