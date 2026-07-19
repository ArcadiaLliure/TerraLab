"""Tests de regressio per al comportament de la mira en mode scope."""

from __future__ import annotations

from PyQt5.QtCore import Qt

from TerraLab.ui.canvas_input_handler import CanvasInputHandler


class _DummyScopeController:
    """Stub minim de `TelescopeScopeController` per validar interaccions."""

    def __init__(self) -> None:
        self.end_drag_calls = 0
        self.handle_click_calls = 0
        self.start_drag_calls = 0

    def end_drag(self) -> None:
        self.end_drag_calls += 1

    def handle_click(self, sx, sy, unproject_fn) -> bool:
        self.handle_click_calls += 1
        return True

    def start_drag(self, sx, sy) -> None:
        self.start_drag_calls += 1


class _DummyPoint:
    """Representa un punt de click compatible amb els tests."""

    def __init__(self, x: float, y: float) -> None:
        self._x = float(x)
        self._y = float(y)

    def x(self) -> float:
        return self._x

    def y(self) -> float:
        return self._y


class _DummyMouseEvent:
    """Event simplificat per provar `handle_mouse_press`."""

    def __init__(
        self,
        *,
        x: float = 120.0,
        y: float = 80.0,
        button: int = Qt.LeftButton,
        modifiers: int = Qt.NoModifier,
    ) -> None:
        self._x = float(x)
        self._y = float(y)
        self._button = int(button)
        self._modifiers = int(modifiers)
        self.accepted = False

    def button(self) -> int:
        return self._button

    def modifiers(self) -> int:
        return self._modifiers

    def x(self) -> float:
        return self._x

    def y(self) -> float:
        return self._y

    def pos(self) -> _DummyPoint:
        return _DummyPoint(self._x, self._y)

    def accept(self) -> None:
        self.accepted = True


class _DummyCanvas:
    """Stub de canvas amb els atributs minims requerits pel handler."""

    def __init__(self) -> None:
        self.scope_controller = _DummyScopeController()
        self.scope_camera_lock_to_target = True
        self.scope_reticle_lock_to_target = True
        self.dragging = False
        self._scope_camera_pan_started = True
        self._scope_camera_click_pending = True
        self._scope_combined_drag_active = True
        self._cached_star_image = object()
        self.press_pos = None
        self.updated = 0
        self.last_mouse_x = 0
        self.last_mouse_y = 0

    @staticmethod
    def drawing_mode_enabled() -> bool:
        return False

    @staticmethod
    def scope_mode_enabled() -> bool:
        return True

    @staticmethod
    def measurement_tool_active() -> bool:
        return False

    @staticmethod
    def unproject_stereo(sx, sy):
        return 0.0, 0.0

    def update(self) -> None:
        self.updated += 1


def test_scope_manual_press_unlocks_both_target_locks() -> None:
    """Un click/drag manual de mira ha de desactivar lock de reticula i camera."""
    canvas = _DummyCanvas()
    handler = CanvasInputHandler(canvas)

    event = _DummyMouseEvent(button=Qt.LeftButton, modifiers=Qt.NoModifier)
    handler.handle_mouse_press(event)

    assert canvas.scope_reticle_lock_to_target is False
    assert canvas.scope_camera_lock_to_target is False
    assert canvas.scope_controller.handle_click_calls == 1
    assert canvas.scope_controller.start_drag_calls == 1
    assert event.accepted is True
