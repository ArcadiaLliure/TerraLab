import faulthandler
import os
import sys
import traceback

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from TerraLab.common.timestamped_print import enable_timestamped_print
from TerraLab.ui.data_library_dialog import ensure_data_library_for_gui
from TerraLab.ui.sky_widget import AstronomicalWidget


class StandaloneAstronomicalWidget(AstronomicalWidget):
    def __init__(self):
        # Initialize as standard window (frameless=False)
        super().__init__(parent=None, frameless=False)
        self.setWindowTitle("TerraLab Standalone")
        self.resize(1024, 768)

    def keyPressEvent(self, event):
        """Executa el metode keyPressEvent de la classe StandaloneAstronomicalWidget.

        Par?metres:
        - event (Any): Valor del parametre 'event'.

        Retorna:
        - None.
        """
        if event.key() == Qt.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
        else:
            super().keyPressEvent(event)


def main():
    enable_timestamped_print()

    # Persist native crashes (segfault/abort) to file for post-mortem analysis.
    crash_log = os.path.join(os.getcwd(), "terralab_crash.log")
    try:
        faulthandler.enable(open(crash_log, "a", encoding="utf-8"))
        print(f"[TerraLab] Fault handler enabled: {crash_log}")
    except Exception as e:
        print(f"[TerraLab] Warning: could not enable faulthandler: {e}")

    app = QApplication(sys.argv)

    # Data-heavy services are constructed by the widget, so require the
    # user-controlled library before any coordinator or cache can be created.
    ensure_data_library_for_gui()

    widget = StandaloneAstronomicalWidget()
    widget.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
