"""Regression tests for ownership of temporary Qt paint devices."""

from __future__ import annotations

import os

import pytest
from PyQt5.QtGui import QColor, QGuiApplication

from TerraLab.render import stars_renderer
from TerraLab.weather import system as weather_system


@pytest.fixture(scope="module")
def gui_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QGuiApplication.instance() or QGuiApplication([])


class _FailingPainter:
    """Paints nothing, but records whether the protected cleanup ran."""

    Antialiasing = 1
    instances: list["_FailingPainter"] = []

    def __init__(self, _device) -> None:
        self.ended = False
        self.instances.append(self)

    def setRenderHint(self, *_args) -> None:
        raise RuntimeError("synthetic painter failure")

    def isActive(self) -> bool:
        return not self.ended

    def end(self) -> None:
        self.ended = True


@pytest.fixture(autouse=True)
def reset_failing_painters():
    _FailingPainter.instances = []


def test_cloud_texture_closes_painter_after_failure(gui_app, monkeypatch):
    monkeypatch.setattr(weather_system, "QPainter", _FailingPainter)

    with pytest.raises(RuntimeError, match="synthetic painter failure"):
        weather_system.Cloud(180.0, 40.0, 8.0)

    assert len(_FailingPainter.instances) == 1
    assert _FailingPainter.instances[0].ended


@pytest.mark.parametrize(
    "build_sprite",
    (
        lambda renderer: renderer._cached_disc_sprite(QColor(240, 230, 210), 1.5),
        lambda renderer: renderer._cached_bright_sprite(
            QColor(240, 230, 210), 220, 2.0, 2, False
        ),
    ),
)
def test_star_sprite_closes_painter_after_failure(monkeypatch, build_sprite):
    monkeypatch.setattr(stars_renderer, "QPainter", _FailingPainter)

    with pytest.raises(RuntimeError, match="synthetic painter failure"):
        build_sprite(stars_renderer.StarsRenderer())

    assert len(_FailingPainter.instances) == 1
    assert _FailingPainter.instances[0].ended
