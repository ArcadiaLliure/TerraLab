"""QPainter presentation adapter for constellation polylines, nodes, labels, and preview lines."""

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

from TerraLab.scene.plans.constellations import ConstellationPlan


def render_qpainter_constellation_plan(
    painter: QPainter,
    plan: ConstellationPlan,
    viewport_w: float,
    viewport_h: float,
) -> None:
    """Renders ConstellationPlan onto QPainter without executing domain rules or state updates."""
    if not plan.visible:
        return

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)

    # 1. Render segments
    normal_pen = QPen(QColor(100, 180, 255, 180), 1.5, Qt.SolidLine)  # type: ignore
    selected_pen = QPen(QColor(255, 220, 80, 240), 2.5, Qt.SolidLine)  # type: ignore

    for seg in plan.segments:
        if len(seg.pts_px) < 2:
            continue
        pen = selected_pen if seg.is_selected else normal_pen
        painter.setPen(pen)

        path = QPainterPath()
        path.moveTo(QPointF(*seg.pts_px[0]))
        for pt in seg.pts_px[1:]:
            path.lineTo(QPointF(*pt))
        painter.drawPath(path)

    # 2. Render active drawing preview
    if plan.preview_pts_px and len(plan.preview_pts_px) >= 2:
        prev_pen = QPen(
            QColor(255, 120, 120, 220)
            if not plan.preview_snapped
            else QColor(120, 255, 120, 220),
            1.5,
            Qt.DashLine,  # type: ignore
        )
        painter.setPen(prev_pen)
        painter.drawLine(
            QPointF(*plan.preview_pts_px[0]),
            QPointF(*plan.preview_pts_px[1]),
        )

    # 3. Render nodes
    node_brush = QColor(180, 220, 255, 220)
    node_sel_brush = QColor(255, 230, 100, 255)

    for node in plan.nodes:
        cx, cy = node.pos_px
        r = 4.0 if not node.is_selected else 6.0
        color = node_sel_brush if node.is_selected else node_brush

        if node.is_selected:
            painter.setPen(QPen(QColor(255, 200, 50, 180), 2.0))
            painter.setBrush(Qt.NoBrush)  # type: ignore
            painter.drawEllipse(QPointF(cx, cy), r + 4.0, r + 4.0)

        painter.setPen(Qt.NoPen)  # type: ignore
        painter.setBrush(color)
        painter.drawEllipse(QPointF(cx, cy), r, r)

        if node.star_name:
            font = QFont("SansSerif", 8)
            painter.setFont(font)
            painter.setPen(QPen(QColor(200, 220, 255, 180), 1.0))
            painter.drawText(
                QRectF(cx + 8, cy - 8, 120, 16),
                Qt.AlignLeft | Qt.AlignVCenter,  # type: ignore
                node.star_name,
            )

    # 4. Render labels
    label_font = QFont("SansSerif", 10, QFont.Bold)
    painter.setFont(label_font)

    for lbl in plan.labels:
        rx, ry, rw, rh = lbl.rect_px
        rect = QRectF(rx, ry, rw, rh)
        bg_color = (
            QColor(20, 30, 45, 180)
            if not lbl.is_selected
            else QColor(60, 50, 20, 220)
        )
        border_pen = (
            QPen(QColor(100, 180, 255, 150), 1.0)
            if not lbl.is_selected
            else QPen(QColor(255, 200, 60, 220))
        )

        painter.setPen(border_pen)
        painter.setBrush(bg_color)
        painter.drawRoundedRect(rect, 4.0, 4.0)

        text_color = (
            QColor(220, 240, 255, 230)
            if not lbl.is_selected
            else QColor(255, 240, 160, 255)
        )
        painter.setPen(text_color)
        painter.drawText(rect, Qt.AlignCenter, lbl.name)  # type: ignore

    painter.restore()
