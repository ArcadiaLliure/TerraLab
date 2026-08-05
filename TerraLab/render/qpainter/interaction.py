"""QPainter view adapter for selection pulse and interactive measurement plans."""

# pyright: reportAttributeAccessIssue=false, reportArgumentType=false

from __future__ import annotations

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

from TerraLab.scene.plans.interaction import (
    ScreenMeasurementItem,
    ScreenMeasurementPlan,
    SelectionPlan,
)


def render_selection_plan(
    painter: QPainter,
    plan: SelectionPlan,
) -> None:
    """Render selection pulse indicator on a QPainter surface."""
    if (
        plan.selected_kind is None
        or plan.screen_x is None
        or plan.screen_y is None
    ):
        return

    x = float(plan.screen_x)
    y = float(plan.screen_y)
    pulse_r = float(plan.pulse_radius_px)
    alpha = max(40, min(240, int(255.0 * plan.pulse_alpha)))

    painter.save()
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(Qt.NoBrush)

        # Outer ring
        painter.setPen(QPen(QColor(216, 178, 106, alpha), 1.5, Qt.DashLine))
        painter.drawEllipse(QPointF(x, y), pulse_r, pulse_r)


        # Inner crosshair/circle
        painter.setPen(QPen(QColor(255, 240, 150, min(255, alpha + 40)), 1.2))
        painter.drawEllipse(QPointF(x, y), 5.0, 5.0)

        painter.drawLine(QPointF(x - pulse_r - 2, y), QPointF(x - 6.0, y))
        painter.drawLine(QPointF(x + 6.0, y), QPointF(x + pulse_r + 2, y))
        painter.drawLine(QPointF(x, y - pulse_r - 2), QPointF(x, y - 6.0))
        painter.drawLine(QPointF(x, y + 6.0), QPointF(x, y + pulse_r + 2))
    finally:
        painter.restore()


def render_measurement_plan(
    painter: QPainter,
    plan: ScreenMeasurementPlan,
) -> None:
    """Paint resolved measurement primitives without projection or layout."""
    if not plan.items and plan.preview is None:
        return

    for item in plan.items:
        _draw_screen_measurement_item(painter, item)

    if plan.preview is None:
        return
    _draw_screen_measurement_item(painter, plan.preview)


def _draw_screen_measurement_item(
    painter: QPainter,
    item: ScreenMeasurementItem,
) -> None:
    for points in item.paths:
        if len(points) < 2:
            continue
        path = QPainterPath(QPointF(*points[0]))
        for point in points[1:]:
            path.lineTo(QPointF(*point))
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(*item.glow_style.rgba), item.glow_style.width_px))
            painter.drawPath(path)
            painter.setPen(QPen(QColor(*item.line_style.rgba), item.line_style.width_px))
            painter.drawPath(path)
        finally:
            painter.restore()
    if item.handles:
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            for point in item.handles:
                painter.setPen(QPen(QColor(255, 240, 120, 220), 1.1))
                painter.setBrush(QColor(0, 0, 0, 200))
                painter.drawEllipse(QPointF(*point), item.handle_radius_px, item.handle_radius_px)
        finally:
            painter.restore()
    label = item.label
    if label is None:
        return
    painter.save()
    try:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*label.background_rgba))
        painter.drawRoundedRect(
            label.bounds.x, label.bounds.y, label.bounds.width, label.bounds.height,
            label.corner_radius_px, label.corner_radius_px,
        )
        font = QFont(label.style.font.family)
        font.setPixelSize(max(1, int(round(label.style.font.pixel_size))))
        painter.setFont(font)
        painter.setPen(QColor(*label.style.foreground_rgba))
        line_step = label.bounds.height / max(1, len(label.text))
        for index, line in enumerate(label.text):
            painter.drawText(QPointF(label.baseline[0], label.baseline[1] + index * line_step), line)
    finally:
        painter.restore()
