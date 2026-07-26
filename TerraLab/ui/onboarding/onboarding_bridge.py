"""Pont mínim entre el document HTML i el cicle de vida de Qt."""

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot


class OnboardingBridge(QObject):
    """Exposa només l'acció de finalitzar l'onboarding."""

    finished = pyqtSignal()

    @pyqtSlot()
    def finish(self) -> None:
        self.finished.emit()

