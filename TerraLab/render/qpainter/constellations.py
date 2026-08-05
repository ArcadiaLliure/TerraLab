"""QPainter adapter for resolved constellation overlay primitives."""

# pyright: reportAttributeAccessIssue=false

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

from TerraLab.render.qpainter.labels import QPainterLabelsAdapter
from TerraLab.scene.plans.overlays import ConstellationOverlayPlan
from TerraLab.scene.plans.constellations import ConstellationPlan


def render_qpainter_constellation_plan(
    painter: QPainter, plan: ConstellationOverlayPlan
) -> int:
    """Paint only the plan's lines, circles and already-laid-out text."""

    if not plan.visible:
        return 0
    calls = 0
    painter.save()
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        for segment in plan.segments + ((plan.preview,) if plan.preview else ()):
            painter.setPen(QPen(QColor(*segment.style.rgba), segment.style.width_px, Qt.DashLine if segment.style.dashed else Qt.SolidLine))
            painter.drawLine(QPointF(*segment.start), QPointF(*segment.end))
            calls += 1
        for node in plan.nodes:
            if node.stroke is not None:
                painter.setPen(QPen(QColor(*node.stroke.rgba), node.stroke.width_px))
            else:
                painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(*(node.fill_rgba or (0, 0, 0, 0))))
            painter.drawEllipse(QPointF(*node.center), node.radius_px, node.radius_px)
            calls += 1
    finally:
        painter.restore()
    if plan.labels is not None:
        calls += QPainterLabelsAdapter().paint_text_batch(painter, plan.labels)
    return calls


def render_legacy_constellation_plan(
    painter: QPainter, plan: ConstellationPlan, _viewport_w: float, _viewport_h: float
) -> None:
    """Compatibility painter retained only for the direct v1 renderer path."""

    if not plan.visible:
        return
    painter.save()
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        for segment in plan.segments:
            if len(segment.pts_px) < 2:
                continue
            painter.setPen(QPen(QColor(255, 220, 80, 240) if segment.is_selected else QColor(100, 180, 255, 180), 2.5 if segment.is_selected else 1.5))
            path = QPainterPath(QPointF(*segment.pts_px[0]))
            for point in segment.pts_px[1:]:
                path.lineTo(QPointF(*point))
            painter.drawPath(path)
        for node in plan.nodes:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 230, 100, 255) if node.is_selected else QColor(180, 220, 255, 220))
            painter.drawEllipse(QPointF(*node.pos_px), 6.0 if node.is_selected else 4.0, 6.0 if node.is_selected else 4.0)
        font = QFont("SansSerif", 10, QFont.Bold)
        painter.setFont(font)
        for label in plan.labels:
            rect = QRectF(*label.rect_px)
            painter.setPen(QPen(QColor(255, 200, 60, 220) if label.is_selected else QColor(100, 180, 255, 150)))
            painter.setBrush(QColor(60, 50, 20, 220) if label.is_selected else QColor(20, 30, 45, 180))
            painter.drawRoundedRect(rect, 4.0, 4.0)
            painter.drawText(rect, Qt.AlignCenter, label.name)
    finally:
        painter.restore()
