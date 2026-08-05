"""QPainter adapter for a resolved scope-overlay plan."""

# pyright: reportArgumentType=false

from PyQt5.QtCore import QPointF
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen

from TerraLab.render.qpainter.labels import QPainterLabelsAdapter
from TerraLab.scene.plans.overlays import ScopeOverlayPlan
from TerraLab.scene.plans.scope import ScopePlan


def render_qpainter_scope_plan(painter: QPainter, plan: ScopeOverlayPlan) -> int:
    """Materialise declared scope primitives without reading domain state."""

    if not plan.enabled or plan.center is None:
        return 0
    cx, cy = plan.center
    painter.save()
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        mask = QPainterPath()
        mask.addRect(plan.viewport.x, plan.viewport.y, plan.viewport.width, plan.viewport.height)
        if plan.shape == "circle" and plan.radius_px > 0.0:
            hole = QPainterPath()
            hole.addEllipse(QPointF(cx, cy), plan.radius_px, plan.radius_px)
            mask = mask.subtracted(hole)
            painter.fillPath(mask, QColor(*plan.mask_rgba))
            painter.setPen(QPen(QColor(*plan.outline.rgba), plan.outline.width_px))
            painter.drawEllipse(QPointF(cx, cy), plan.radius_px, plan.radius_px)
        elif plan.shape == "rectangle" and plan.rect_size_px is not None:
            rw, rh = plan.rect_size_px
            hole = QPainterPath()
            hole.addRect(cx - rw * 0.5, cy - rh * 0.5, rw, rh)
            mask = mask.subtracted(hole)
            painter.fillPath(mask, QColor(*plan.mask_rgba))
            painter.setPen(QPen(QColor(*plan.outline.rgba), plan.outline.width_px))
            painter.drawRect(cx - rw * 0.5, cy - rh * 0.5, rw, rh)
        for line in plan.crosshair:
            painter.setPen(QPen(QColor(*line.style.rgba), line.style.width_px))
            painter.drawLine(QPointF(*line.start), QPointF(*line.end))
    finally:
        painter.restore()
    return 1 + len(plan.crosshair) + (
        QPainterLabelsAdapter().paint_text_batch(painter, plan.readout)
        if plan.readout is not None
        else 0
    )


def render_legacy_scope_plan(
    painter: QPainter, plan: ScopePlan, viewport_w: float, viewport_h: float
) -> None:
    """Temporary compatibility paint path for direct v1 renderer callers."""

    if not plan.enabled or plan.center_px is None:
        return
    cx, cy = plan.center_px
    painter.save()
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRect(0.0, 0.0, viewport_w, viewport_h)
        if plan.shape == "circle" and plan.radius_px > 0.0:
            hole = QPainterPath()
            hole.addEllipse(QPointF(cx, cy), plan.radius_px, plan.radius_px)
            path = path.subtracted(hole)
            painter.fillPath(path, QColor(10, 12, 16, 220))
            painter.setPen(QPen(QColor(255, 200, 80, 200), 1.5))
            painter.drawEllipse(QPointF(cx, cy), plan.radius_px, plan.radius_px)
        elif plan.shape == "rectangle" and plan.rect_size_px is not None:
            rw, rh = plan.rect_size_px
            hole = QPainterPath()
            hole.addRect(cx - rw * 0.5, cy - rh * 0.5, rw, rh)
            path = path.subtracted(hole)
            painter.fillPath(path, QColor(10, 12, 16, 220))
            painter.setPen(QPen(QColor(255, 200, 80, 200), 1.5))
            painter.drawRect(cx - rw * 0.5, cy - rh * 0.5, rw, rh)
        painter.drawLine(QPointF(cx - 12.0, cy), QPointF(cx + 12.0, cy))
        painter.drawLine(QPointF(cx, cy - 12.0), QPointF(cx, cy + 12.0))
    finally:
        painter.restore()
