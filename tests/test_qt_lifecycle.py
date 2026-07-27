from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_astronomical_widget_can_close_and_rebuild_offscreen(
    tmp_path: Path,
) -> None:
    appdata = tmp_path / "appdata"
    data_root = tmp_path / "library"
    config_dir = appdata / "TerraLab" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        """
        {
          "ui_onboarding_done": true,
          "performance": {
            "scope_preload_mode": "disabled",
            "defer_catalog_until_horizon_preview": true
          }
        }
        """,
        encoding="utf-8",
    )

    script = textwrap.dedent(
        """
        import threading
        import time

        from PyQt5.QtWidgets import QApplication
        from TerraLab.ui.astronomical_widget import AstronomicalWidget

        app = QApplication.instance() or QApplication([])
        baseline_threads = {thread.ident for thread in threading.enumerate()}
        closed_widgets = []

        for _ in range(2):
            widget = AstronomicalWidget(parent=None, frameless=False)
            assert widget.terrain_coordinator is not None
            assert widget.ephemeris_coordinator is not None
            assert widget.timer.isActive()
            assert widget._gaia_attach_timer.isActive()
            bake_started = []
            widget._begin_horizon_bake = (
                lambda: bake_started.append(time.monotonic())
            )

            deadline = time.monotonic() + 0.35
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)

            assert widget._deferred_controls_ready
            assert not widget._startup_placeholder_visible
            assert widget._startup_placeholder.isHidden()
            assert bake_started == []

            deadline = time.monotonic() + 0.35
            while time.monotonic() < deadline and not bake_started:
                app.processEvents()
                time.sleep(0.005)
            assert len(bake_started) == 1

            widget.close()
            app.processEvents()
            assert not hasattr(widget.terrain_coordinator, "worker_thread")
            assert not hasattr(widget.canvas, "_thread")
            closed_widgets.append(widget)

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)

        assert all(widget._closing for widget in closed_widgets)
        assert all(
            not hasattr(widget.terrain_coordinator, "worker_thread")
            for widget in closed_widgets
        )
        survivors = [
            thread.name
            for thread in threading.enumerate()
            if thread.ident not in baseline_threads and thread.is_alive()
        ]
        assert survivors == [], survivors
        """
    )
    env = os.environ.copy()
    env.update(
        {
            "APPDATA": str(appdata),
            "QT_QPA_PLATFORM": "offscreen",
            "TERRALAB_DATA_ROOT": str(data_root),
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
    )
