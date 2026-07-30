"""Offscreen lifecycle smoke test for the wheel-installed TerraLab package."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

isolation_root = Path(os.environ["TERRALAB_SMOKE_ROOT"]).resolve()
isolated_appdata = isolation_root / "appdata"
isolated_local_appdata = isolation_root / "localappdata"
isolated_data_root = isolation_root / "data"
for directory in (
    isolated_appdata,
    isolated_local_appdata,
    isolated_data_root,
):
    directory.mkdir(parents=True, exist_ok=True)

# Set every application-owned location before importing Qt or TerraLab.  This
# keeps the installed-wheel smoke test away from the user's real preferences,
# data library and caches.
os.environ["APPDATA"] = str(isolated_appdata)
os.environ["LOCALAPPDATA"] = str(isolated_local_appdata)
os.environ["TERRALAB_DATA_ROOT"] = str(isolated_data_root)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

assert QApplication.instance() is None

import TerraLab  # noqa: E402
from TerraLab.ui.astronomical_widget import AstronomicalWidget  # noqa: E402


def main() -> None:
    config_dir = isolated_appdata / "TerraLab" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
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

    app = QApplication([])
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    close_times: list[float] = []
    closed_widgets: list[AstronomicalWidget] = []
    resolved_dem_paths: list[str | None] = []

    for _ in range(2):
        widget = AstronomicalWidget(parent=None, frameless=False)
        assert widget.timer.isActive()
        assert widget._gaia_attach_timer.isActive()

        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)

        started = time.perf_counter()
        widget.close()
        app.processEvents()
        close_times.append(time.perf_counter() - started)
        assert not widget.terrain_coordinator.worker_thread.isRunning()
        assert not widget.canvas._thread.isRunning()
        resolved_path = widget.terrain_coordinator._worker.tiles_dir
        if resolved_path is not None:
            resolved = Path(resolved_path).resolve()
            resolved.relative_to(isolation_root)
            resolved_dem_paths.append(str(resolved))
        else:
            resolved_dem_paths.append(None)
        closed_widgets.append(widget)

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    survivors = [
        thread.name
        for thread in threading.enumerate()
        if thread.ident not in baseline_threads and thread.is_alive()
    ]
    assert survivors == [], survivors
    assert all(widget._closing for widget in closed_widgets)

    print(
        json.dumps(
            {
                "package_file": str(Path(TerraLab.__file__).resolve()),
                "instances": 2,
                "close_seconds": close_times,
                "python_thread_survivors": survivors,
                "terrain_qthreads_running": [
                    widget.terrain_coordinator.worker_thread.isRunning()
                    for widget in closed_widgets
                ],
                "canvas_qthreads_running": [
                    widget.canvas._thread.isRunning()
                    for widget in closed_widgets
                ],
                "isolation_root": str(isolation_root),
                "resolved_dem_paths": resolved_dem_paths,
                "result": "PASS",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
