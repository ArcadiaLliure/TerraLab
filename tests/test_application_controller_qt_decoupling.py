"""Tests for Phase 16: ApplicationController, ApplicationState, Qt Adapter, and AST isolation boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

from PyQt5.QtCore import QCoreApplication

from TerraLab.adapters.qt.controller import QtApplicationControllerAdapter
from TerraLab.application.commands import LayerIntent
from TerraLab.application.controller import ApplicationController
from TerraLab.application.lifecycle import ApplicationLifecycleManager
from TerraLab.application.state import (
    ApplicationState,
    create_initial_application_state,
)


def test_application_state_immutability() -> None:
    state = create_initial_application_state()
    new_state = state.with_status("RUNNING")
    assert state.status == "STOPPED"
    assert new_state.status == "RUNNING"
    assert state is not new_state


def test_application_controller_commands() -> None:
    controller = ApplicationController()
    state_updates: list[ApplicationState] = []
    controller.add_state_listener(state_updates.append)

    controller.set_observer(latitude_deg=40.0, longitude_deg=3.0, elevation_m=100.0)
    assert controller.state.user_view.observer.latitude == 40.0
    assert controller.state.user_view.observer.longitude == 3.0
    assert len(state_updates) == 1

    controller.pan_camera(10.0, -5.0)
    assert controller.state.user_view.camera.azimuth == 190.0
    assert controller.state.user_view.camera.elevation == 40.0
    assert len(state_updates) == 2


def test_application_controller_layer_intents() -> None:
    controller = ApplicationController()
    controller.toggle_layer("grid", True)
    assert controller.state.user_view.layers.grid_enabled is True


def test_application_controller_interaction_measurement_and_picking() -> None:
    controller = ApplicationController()
    requests = []
    controller.add_pick_request_listener(requests.append)

    controller.process_pointer_press(100.0, 100.0, "left", ())
    assert controller.state.user_view.presentation.interaction_active is True
    controller.process_pointer_move(120.0, 90.0)
    assert controller.state.user_view.camera.azimuth != 180.0
    controller.process_pointer_release(120.0, 90.0, "left")
    assert controller.state.user_view.presentation.interaction_active is False
    assert requests and requests[-1].purpose == "select"

    controller.process_measurement_command("start", "ruler", 1.0, 2.0)
    assert controller.state.model.measurement.tool == "ruler"
    controller.process_measurement_command("clear", "ruler", 1.0, 2.0)
    assert controller.state.model.measurement.clear_revision == 1

    intent = LayerIntent(stars_enabled=False, grid_enabled=True)
    controller.set_layer_intent(intent)
    assert controller.state.user_view.layers.stars_enabled is False
    assert controller.state.user_view.layers.grid_enabled is True


def test_application_lifecycle_manager() -> None:
    mgr = ApplicationLifecycleManager()
    events: list[str] = []

    mgr.register_on_start(lambda: events.append("start"))
    mgr.register_on_stop(lambda: events.append("stop"))

    assert not mgr.is_running
    mgr.start()
    assert mgr.is_running
    assert events == ["start"]

    mgr.stop()
    assert not mgr.is_running
    assert events == ["start", "stop"]


def test_qt_adapter_binding() -> None:
    _app = QCoreApplication.instance() or QCoreApplication([])
    controller = ApplicationController()
    adapter = QtApplicationControllerAdapter(controller)

    emitted_states: list[ApplicationState] = []
    adapter.state_changed.connect(emitted_states.append)

    adapter.set_observer_lat_lon(42.0, 1.5)
    assert len(emitted_states) == 1
    assert emitted_states[0].user_view.observer.latitude == 42.0

    adapter.pan_camera(5.0, 5.0)
    assert len(emitted_states) == 2

    adapter.set_time_iso("2026-07-30T18:30:00Z")
    assert controller.state.user_view.time.year_utc == 2026
    assert controller.state.user_view.time.day_of_year_utc == 210
    assert controller.state.user_view.time.ut_hour == 18.5

    adapter.set_viewport_size(640, 480)
    frame = adapter.request_scene_frame()
    assert frame.viewport.width == 640
    assert frame.viewport.height == 480


def test_no_qt_imports_in_application_module() -> None:
    app_dir = Path("TerraLab/application")
    for file_path in app_dir.glob("**/*.py"):
        if "__pycache__" in file_path.parts:
            continue
        source = file_path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("PyQt5"), (
                        f"Forbidden PyQt5 import in {file_path}: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    assert not node.module.startswith("PyQt5"), (
                        f"Forbidden PyQt5 import from {file_path}: {node.module}"
                    )


def test_no_forbidden_imports_in_ui_module() -> None:
    forbidden_prefixes = (
        "numpy",
        "rasterio",
        "skyfield",
        "TerraLab.render",
        "TerraLab.data.catalogs",
    )
    ui_dir = Path("TerraLab/ui")
    for file_path in ui_dir.glob("**/*.py"):
        if "__pycache__" in file_path.parts:
            continue
        source = file_path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(
                        alias.name == p or alias.name.startswith(p + ".")
                        for p in forbidden_prefixes
                    ), f"Forbidden import in {file_path}: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    assert not any(
                        node.module == p or node.module.startswith(p + ".")
                        for p in forbidden_prefixes
                    ), f"Forbidden import in {file_path}: {node.module}"
