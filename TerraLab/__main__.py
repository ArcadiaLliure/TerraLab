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
    from PyQt5.QtWidgets import QApplication, QMessageBox

    from TerraLab.common.timestamped_print import enable_timestamped_print

    enable_timestamped_print()
    QApplication.setAttribute(
        Qt.AA_ShareOpenGLContexts,  # type: ignore[reportAttributeAccessIssue]
        True,
    )
    app = QApplication([sys.argv[0]])

    # Qt WebEngine s'ha d'importar després de configurar els contextos OpenGL.
    from TerraLab.ui.application_window import TerraLabMainWindow
    from TerraLab.ui.data_library_dialog import ensure_data_library_for_gui
    from TerraLab.ui.onboarding import FirstRunManager
    from TerraLab.ui.onboarding.onboarding_window import OnboardingWindow

    windows: dict[str, object] = {}
    crash_handle = None

    def show_main_window() -> None:
        nonlocal crash_handle
        if "main" in windows:
            return
        try:
            library = ensure_data_library_for_gui()
        except Exception as exc:
            QMessageBox.critical(
                None,
                "TerraLab",
                "No s'ha pogut preparar la biblioteca de dades.\n\n"
                f"{exc}",
            )
            app.quit()
            return

        if crash_handle is None:
            crash_log = library.layout(create=True)["logs"] / "terralab_crash.log"
            crash_handle = crash_log.open("a", encoding="utf-8")
            faulthandler.enable(crash_handle)

        main_window = TerraLabMainWindow()
        windows["main"] = main_window
        main_window.show()
        app.setQuitOnLastWindowClosed(True)

    first_run_manager = FirstRunManager()
    first_run_manager.prepare_config()

    try:
        if first_run_manager.should_show_on_startup():
            app.setQuitOnLastWindowClosed(False)
            onboarding = OnboardingWindow(
                first_run=True,
                manager=first_run_manager,
            )
            windows["onboarding"] = onboarding
            onboarding.completed.connect(show_main_window)
            onboarding.cancelled.connect(app.quit)

            def continue_after_load_failure(message: str) -> None:
                print(message)
                show_main_window()

            onboarding.load_failed.connect(continue_after_load_failure)
            onboarding.showFullScreen()
        else:
            show_main_window()
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
