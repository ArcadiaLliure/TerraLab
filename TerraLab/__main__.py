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

    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtWidgets import (
        QApplication,
        QLabel,
        QMessageBox,
        QVBoxLayout,
        QWidget,
    )

    from TerraLab.common.timestamped_print import enable_timestamped_print
    from TerraLab.runtime.supervisor import RuntimeSupervisor

    enable_timestamped_print()
    QApplication.setAttribute(
        Qt.AA_ShareOpenGLContexts,  # type: ignore[reportAttributeAccessIssue]
        True,
    )
    app = QApplication([sys.argv[0]])

    # Qt WebEngine s'ha d'importar després de configurar els contextos OpenGL.
    from TerraLab.ui.application_window import TerraLabMainWindow
    from TerraLab.ui.data_library_dialog import ensure_data_library_for_gui
    from TerraLab.ui.design_system import apply_onboarding_theme
    from TerraLab.ui.onboarding import FirstRunManager
    from TerraLab.ui.onboarding.onboarding_window import OnboardingWindow

    apply_onboarding_theme(app)
    windows: dict[str, object] = {}
    crash_handle = None
    runtime = RuntimeSupervisor(app)
    setattr(app, "terralab_runtime", runtime)
    runtime.start()
    app.aboutToQuit.connect(runtime.stop)

    def show_startup_shell() -> None:
        if "startup" in windows:
            return
        shell = QWidget()
        shell.setObjectName("startupShell")
        shell.setWindowTitle("TerraLab")
        shell.setStyleSheet(
            "#startupShell { background: #02040a; color: #f1cd88; }"
        )
        layout = QVBoxLayout(shell)
        label = QLabel("TERRALAB")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet(
            "color: #f1cd88; font-size: 28px; font-weight: 700;"
        )
        layout.addWidget(label)
        windows["startup"] = shell
        shell.showFullScreen()

    def show_main_window() -> None:
        nonlocal crash_handle
        if "main" in windows or windows.get("main_loading"):
            return
        windows["main_loading"] = True
        show_startup_shell()

        def finish_main_window() -> None:
            nonlocal crash_handle
            try:
                library = ensure_data_library_for_gui()
            except Exception as exc:
                windows.pop("main_loading", None)
                QMessageBox.critical(
                    None,
                    "TerraLab",
                    "No s'ha pogut preparar la biblioteca de dades.\n\n"
                    f"{exc}",
                )
                app.quit()
                return

            if crash_handle is None:
                crash_log = (
                    library.layout(create=True)["logs"]
                    / "terralab_crash.log"
                )
                crash_handle = crash_log.open("a", encoding="utf-8")
                faulthandler.enable(crash_handle)

            main_window = TerraLabMainWindow()
            windows["main"] = main_window
            windows.pop("main_loading", None)
            main_window.showFullScreen()
            main_window.raise_()
            main_window.activateWindow()
            app.setQuitOnLastWindowClosed(True)

            def retire_startup_shell() -> None:
                shell = windows.pop("startup", None)
                if shell is not None:
                    shell.close()

            QTimer.singleShot(0, retire_startup_shell)

        # Give Qt one event-loop turn to expose the fullscreen shell before
        # any compatibility dialog or library inspection can run.
        QTimer.singleShot(0, finish_main_window)

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
        runtime.finish_shutdown(2_000)
        if crash_handle is not None:
            faulthandler.disable()
            crash_handle.close()


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
