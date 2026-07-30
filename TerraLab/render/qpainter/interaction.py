"""QPainter view adapter for selection pulse and interactive measurement plans."""

from __future__ import annotations

import math
from typing import Callable

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen

from TerraLab.scene.plans.interaction import (
    MeasurementItemPlan,
    MeasurementPlan,
    SelectionPlan,
)

SkyCoord = tuple[float, float]
ProjectFn = Callable[[float, float], tuple[float, float] | None]


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
    r = float(plan.pulse_radius_px)
    phase = float(plan.pulse_phase)

    # Dynamic pulsing radius and opacity
    pulse_r = r + 4.0 * math.sin(phase * 4.0)
    alpha = max(40, min(240, int(200 * abs(math.sin(phase * 2.0)))))

    painter.save()
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(getattr(Qt, "NoBrush", Qt.BrushStyle.NoBrush))

        # Outer ring
        painter.setPen(QPen(QColor(216, 178, 106, alpha), 1.5, getattr(Qt, "DashLine", Qt.PenStyle.DashLine)))
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
    plan: MeasurementPlan,
    project_fn: ProjectFn,
) -> None:
    """Render resolved measurement item plans and previews."""
    if not plan.items and plan.preview is None:
        return

    for item in plan.items:
        _draw_measurement_item_plan(painter, project_fn, item)

    if plan.preview is None:
        return
    _draw_measurement_item_plan(
        painter, project_fn, plan.preview, alpha_override=140
    )


def _draw_measurement_item_plan(
    painter: QPainter,
    project_fn: ProjectFn,
    item: MeasurementItemPlan,
    alpha_override: int = 220,
) -> None:
    alpha = alpha_override
    selected = item.selected

    # 1. Draw paths
    for sky_path in item.paths_sky:
        pts: list[QPointF] = []
        for alt, az in sky_path:
            p = project_fn(alt, az)
            if p is not None:
                pts.append(QPointF(float(p[0]), float(p[1])))
        if len(pts) < 2:
            continue

        path = QPainterPath()
        path.moveTo(pts[0])
        for p in pts[1:]:
            path.lineTo(p)

        glow = (
            QColor(255, 255, 180, int(alpha * 0.45))
            if selected
            else QColor(255, 255, 255, int(alpha * 0.35))
        )
        line = (
            QColor(255, 245, 120, alpha)
            if selected
            else QColor(255, 255, 255, alpha)
        )

        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(getattr(Qt, "NoBrush", Qt.BrushStyle.NoBrush))
            painter.setPen(QPen(glow, 3.2 if selected else 3.0))
            painter.drawPath(path)
            painter.setPen(QPen(line, 1.2 if selected else 1.0))
            painter.drawPath(path)
        finally:
            painter.restore()

    # 2. Draw label
    if item.label:
        anc = project_fn(*item.anchor_sky)
        if anc is not None:
            _draw_measurement_label(
                painter, float(anc[0]), float(anc[1]), item.label, alpha
            )

    # 3. Draw handles if selected
    if selected and item.handles_sky:
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            for hk, hsky in item.handles_sky.items():
                hp = project_fn(*hsky)
                if hp is None:
                    continue
                q = QPointF(float(hp[0]), float(hp[1]))
                painter.setPen(QPen(QColor(255, 240, 120, alpha), 1.1))
                painter.setBrush(QColor(0, 0, 0, min(200, alpha)))
                painter.drawEllipse(q, 4.0, 4.0)
        finally:
            painter.restore()


def _draw_measurement_label(
    painter: QPainter,
    x: float,
    y: float,
    txt: str,
    alpha: int,
) -> None:
    lines = [line for line in str(txt).split("\n") if line]
    if not lines:
        return

    painter.save()
    try:
        fm = painter.fontMetrics()
        line_h = float(fm.lineSpacing())
        max_line_w = max(float(fm.horizontalAdvance(line)) for line in lines)

        pad_x, pad_y = 6.0, 4.0
        box_w = min(320.0, max(1.0, max_line_w + 2.0 * pad_x))
        box_h = max(1.0, (line_h * len(lines)) + 2.0 * pad_y)

        rect = QRectF(x + 8.0, y + 8.0, box_w, box_h)
        painter.setPen(getattr(Qt, "NoPen", Qt.PenStyle.NoPen))
        painter.setBrush(QColor(0, 0, 0, min(205, alpha)))
        painter.drawRoundedRect(rect, 5.0, 5.0)

        painter.setPen(QColor(255, 255, 255, alpha))
        text_x = rect.left() + pad_x
        text_y = rect.top() + pad_y
        for i, line in enumerate(lines):
            y_baseline = text_y + line_h * i + fm.ascent()
            painter.drawText(QPointF(text_x, y_baseline), line)
    finally:
        painter.restore()
