"""TerraLab desktop application entry point."""

from __future__ import annotations

import argparse
import faulthandler
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terralab",
        description="Start the TerraLab astronomical terrain viewer.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="TerraLab 0.1.0",
    )
    return parser


def run() -> int:
    """Construct and run the GUI after command-line parsing has completed."""

    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    from TerraLab.common.data_library import DataLibrary
    from TerraLab.common.timestamped_print import enable_timestamped_print
    from TerraLab.ui.data_library_dialog import ensure_data_library_for_gui
    from TerraLab.ui.astronomical_widget import AstronomicalWidget

    class StandaloneAstronomicalWidget(AstronomicalWidget):
        def __init__(self) -> None:
            super().__init__(parent=None, frameless=False)
            self.setWindowTitle("TerraLab")
            self.resize(1024, 768)

        def keyPressEvent(self, event) -> None:
            if event.key() == Qt.Key_F11:
                self.showNormal() if self.isFullScreen() else self.showFullScreen()
                return
            super().keyPressEvent(event)

    enable_timestamped_print()
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication([sys.argv[0]])

    ensure_data_library_for_gui()
    logs_dir = DataLibrary.current(create=True).layout(create=True)["logs"]
    crash_log = logs_dir / "terralab_crash.log"
    crash_handle = None
    try:
        crash_handle = crash_log.open("a", encoding="utf-8")
        faulthandler.enable(crash_handle)
        widget = StandaloneAstronomicalWidget()
        widget.show()
        return int(app.exec_())
    finally:
        if crash_handle is not None:
            faulthandler.disable()
            crash_handle.close()


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
