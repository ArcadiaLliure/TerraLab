"""Finestra d'aplicació de TerraLab i accions de nivell superior."""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QCloseEvent, QKeyEvent
from PyQt5.QtWidgets import QAction, QApplication, QMainWindow

from TerraLab.ui.astronomical_widget import AstronomicalWidget
from TerraLab.ui.design_system import apply_onboarding_theme
from TerraLab.ui.onboarding.onboarding_window import OnboardingWindow


class TerraLabMainWindow(QMainWindow):
    """Contenidor principal amb el visor i el menú d'aplicació."""

    def __init__(self) -> None:
        super().__init__(parent=None)
        app = QApplication.instance()
        apply_onboarding_theme(app if isinstance(app, QApplication) else None)
        self.setWindowTitle("TerraLab")
        self.resize(1024, 768)
        self._onboarding_window: OnboardingWindow | None = None
        self._viewer_closed = False

        self.viewer = AstronomicalWidget(parent=self, frameless=False)
        self.setCentralWidget(self.viewer)
        self._build_application_menu()

    def _build_application_menu(self) -> None:
        menu_bar = self.menuBar()
        assert menu_bar is not None
        menu_bar.setNativeMenuBar(False)
        menu_bar.setObjectName("mainMenuBar")
        help_menu = menu_bar.addMenu("Ajuda")
        assert help_menu is not None
        onboarding_action = QAction(
            "Primer viatge per TerraLab…",
            self,
        )
        onboarding_action.triggered.connect(self.open_onboarding)
        help_menu.addAction(onboarding_action)
        self.onboarding_action = onboarding_action

    def open_onboarding(self) -> None:
        """Torna a obrir el viatge sense alterar la preferència de primer inici."""

        if self._onboarding_window is not None:
            self._onboarding_window.showFullScreen()
            self._onboarding_window.raise_()
            self._onboarding_window.activateWindow()
            return

        onboarding = OnboardingWindow(self, first_run=False)
        onboarding.destroyed.connect(self._forget_onboarding)
        self._onboarding_window = onboarding
        onboarding.showFullScreen()

    def _forget_onboarding(self, _obj=None) -> None:
        self._onboarding_window = None

    def keyPressEvent(  # type: ignore[reportIncompatibleMethodOverride]
        self,
        event: QKeyEvent,
    ) -> None:
        if event.key() == Qt.Key_F11:  # type: ignore[reportAttributeAccessIssue]
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
            return
        super().keyPressEvent(event)

    def closeEvent(  # type: ignore[reportIncompatibleMethodOverride]
        self,
        event: QCloseEvent,
    ) -> None:
        onboarding = self._onboarding_window
        if onboarding is not None:
            onboarding.close()
        if not self._viewer_closed:
            self._viewer_closed = True
            self.viewer.close()
        super().closeEvent(event)
