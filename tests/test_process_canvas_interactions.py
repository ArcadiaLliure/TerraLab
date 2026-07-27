from __future__ import annotations

from types import SimpleNamespace

from PyQt5.QtCore import QPoint, Qt

from TerraLab.ui.astro_canvas import AstroCanvas


class _PointerEvent:
    def __init__(
        self,
        x: int,
        y: int,
        *,
        button=Qt.LeftButton,
        modifiers=Qt.NoModifier,
    ) -> None:
        self._position = QPoint(x, y)
        self._button = button
        self._modifiers = modifiers
        self.accepted = False

    def x(self) -> int:
        return self._position.x()

    def y(self) -> int:
        return self._position.y()

    def pos(self) -> QPoint:
        return QPoint(self._position)

    def button(self):
        return self._button

    def modifiers(self):
        return self._modifiers

    def accept(self) -> None:
        self.accepted = True


class _KeyEvent:
    def __init__(self, key, modifiers=Qt.NoModifier) -> None:
        self._key = key
        self._modifiers = modifiers
        self.accepted = False

    def key(self):
        return self._key

    def modifiers(self):
        return self._modifiers

    def accept(self) -> None:
        self.accepted = True


class _CanvasHarness:
    _pan_camera_from_pointer = AstroCanvas._pan_camera_from_pointer

    def __init__(
        self,
        *,
        measurement: bool = False,
        drawing: bool = False,
        scope: bool = False,
        constellation_visible: bool = False,
    ) -> None:
        self._measurement = measurement
        self._drawing = drawing
        self._scope = scope
        self._constellation_visible = constellation_visible
        self._remote_pointer_domain = ""
        self._pointer_modifiers = 0
        self._drawing_ctrl_pan_started = False
        self._drawing_ctrl_click_pending = False
        self._suppress_constellation_release_click = False
        self.dragging = False
        self.last_mouse_x = 0.0
        self.last_mouse_y = 0.0
        self.azimuth_offset = 180.0
        self.elevation_angle = 40.0
        self.press_pos = QPoint()
        self.scope_camera_lock_to_target = True
        self.scope_reticle_lock_to_target = True
        self.interactions = []
        self.picks = []
        self.updates = 0
        self.scope_controller = SimpleNamespace()

    def measurement_tool_active(self) -> bool:
        return self._measurement

    def drawing_mode_enabled(self) -> bool:
        return self._drawing

    def scope_mode_enabled(self) -> bool:
        return self._scope

    def constellation_visible(self) -> bool:
        return self._constellation_visible

    def _request_process_interaction(self, action, x=0.0, y=0.0, **options):
        self.interactions.append((action, float(x), float(y), options))

    def _request_process_pick(self, x, y, *, purpose="select"):
        self.picks.append((float(x), float(y), purpose))

    def _mark_camera_interaction(self, *_args) -> None:
        pass

    def _mark_scope_interaction(self, *_args) -> None:
        pass

    def _set_selected_target(self, _target) -> None:
        pass

    def update(self) -> None:
        self.updates += 1


def test_ctrl_drag_pans_camera_while_measuring() -> None:
    canvas = _CanvasHarness(measurement=True)
    press = _PointerEvent(
        100, 100, modifiers=Qt.ControlModifier
    )
    AstroCanvas.mousePressEvent(canvas, press)
    AstroCanvas.mouseMoveEvent(canvas, _PointerEvent(125, 112))
    AstroCanvas.mouseReleaseEvent(
        canvas,
        _PointerEvent(
            125, 112, modifiers=Qt.ControlModifier
        ),
    )

    assert press.accepted
    assert canvas.azimuth_offset == 167.5
    assert canvas.elevation_angle == 46.0
    assert canvas.interactions == []
    assert not canvas.dragging


def test_ctrl_click_adds_constellation_selection_without_panning() -> None:
    canvas = _CanvasHarness(drawing=True)
    press = _PointerEvent(
        80, 60, modifiers=Qt.ControlModifier
    )
    AstroCanvas.mousePressEvent(canvas, press)
    AstroCanvas.mouseReleaseEvent(
        canvas,
        _PointerEvent(80, 60, modifiers=Qt.ControlModifier),
    )

    action, _x, _y, options = canvas.interactions[-1]
    assert action == "constellation_click"
    assert options["additive_select"] is True
    assert not canvas.dragging


def test_shift_click_forces_constellation_node_addition() -> None:
    canvas = _CanvasHarness(drawing=True)
    AstroCanvas.mousePressEvent(
        canvas,
        _PointerEvent(40, 30, modifiers=Qt.ShiftModifier),
    )
    AstroCanvas.mouseReleaseEvent(
        canvas,
        _PointerEvent(40, 30, modifiers=Qt.ShiftModifier),
    )

    action, _x, _y, options = canvas.interactions[-1]
    assert action == "constellation_click"
    assert options["force_add"] is True


def test_shift_click_selects_object_in_telescope_mode() -> None:
    canvas = _CanvasHarness(scope=True)
    AstroCanvas.mousePressEvent(
        canvas,
        _PointerEvent(140, 90, modifiers=Qt.ShiftModifier),
    )
    AstroCanvas.mouseReleaseEvent(
        canvas,
        _PointerEvent(140, 90, modifiers=Qt.ShiftModifier),
    )

    assert canvas.picks[-1] == (140.0, 90.0, "scope_select")


def test_visible_constellations_remain_editable_outside_draw_mode() -> None:
    canvas = _CanvasHarness(constellation_visible=True)
    AstroCanvas.mousePressEvent(canvas, _PointerEvent(90, 50))
    AstroCanvas.mouseReleaseEvent(canvas, _PointerEvent(90, 50))

    action, _x, _y, options = canvas.interactions[-1]
    assert action == "constellation_click"
    assert options["allow_when_disabled"] is True
    assert canvas._pending_constellation_fallback_pick == (90.0, 50.0)


def test_constellation_right_and_double_click_have_distinct_commands() -> None:
    drawing = _CanvasHarness(drawing=True, constellation_visible=True)
    AstroCanvas.contextMenuEvent(
        drawing,
        _PointerEvent(25, 35, button=Qt.RightButton),
    )
    assert drawing.interactions[-1][0] == "constellation_right"

    visible = _CanvasHarness(constellation_visible=True)
    AstroCanvas.mouseDoubleClickEvent(
        visible,
        _PointerEvent(45, 55, modifiers=Qt.ControlModifier),
    )
    action, _x, _y, options = visible.interactions[-1]
    assert action == "constellation_double"
    assert options["additive_select"] is True
    assert options["allow_when_disabled"] is True


def test_diagnostic_shortcuts_are_restored(monkeypatch) -> None:
    smoke_calls = []
    log_calls = []
    config_writes = []
    canvas = SimpleNamespace(
        debug_render_metrics=False,
        parent_widget=SimpleNamespace(
            run_smoke_scenes=lambda: smoke_calls.append(True)
        ),
        update=lambda: None,
        log_positions=lambda: log_calls.append(True),
    )
    monkeypatch.setattr(
        "TerraLab.ui.astro_canvas.set_config_value",
        lambda key, value: config_writes.append((key, value)),
    )

    f9 = _KeyEvent(Qt.Key_F9)
    AstroCanvas.keyPressEvent(canvas, f9)
    smoke = _KeyEvent(
        Qt.Key_S, Qt.ControlModifier | Qt.ShiftModifier
    )
    AstroCanvas.keyPressEvent(canvas, smoke)
    log = _KeyEvent(Qt.Key_L, Qt.ControlModifier)
    AstroCanvas.keyPressEvent(canvas, log)

    assert f9.accepted and smoke.accepted and log.accepted
    assert canvas.debug_render_metrics is True
    assert config_writes == [("debug_render_metrics", True)]
    assert smoke_calls == [True]
    assert log_calls == [True]
