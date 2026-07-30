"""Legacy QPainter horizon-layer adapter retained during vertical migration."""

from __future__ import annotations

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QPainterPath


class LegacyHorizonLayerAdapter:
    """Delegate the unmigrated horizon callback at the QPainter View boundary."""

    def paint_layer(self, context, render_state) -> None:
        callback = None
        extras = getattr(render_state, "extras", {}) or {}
        if isinstance(extras, dict):
            callback = extras.get("render_horizon")
        if callable(callback):
            callback(context, render_state)


def paint_legacy_ground_mask(canvas, painter, *, is_day: bool) -> None:
    """Paint the legacy ground silhouette for the inactive canvas route."""

    if canvas is None:
        return

    ground_path = QPainterPath()
    points: list[QPointF | None] = []
    for offset in (-360, 0, 360):
        for azimuth_deg in range(0, 361, 10):
            projected = canvas.project_universal_stereo(
                0, azimuth_deg + offset
            )
            if projected:
                points.append(QPointF(*projected))
            elif points:
                points.append(None)

    if not points:
        return

    bottom_y = canvas.height() * 2.0
    first_in_segment = True
    segment_start: QPointF | None = None
    for point in points:
        if point is None:
            if not first_in_segment and segment_start is not None:
                ground_path.lineTo(ground_path.currentPosition().x(), bottom_y)
                ground_path.lineTo(segment_start.x(), bottom_y)
                ground_path.closeSubpath()
            first_in_segment = True
            continue
        if first_in_segment:
            ground_path.moveTo(point)
            segment_start = point
            first_in_segment = False
        else:
            ground_path.lineTo(point)

    if not first_in_segment and segment_start is not None:
        ground_path.lineTo(ground_path.currentPosition().x(), bottom_y)
        ground_path.lineTo(segment_start.x(), bottom_y)
        ground_path.closeSubpath()

    painter.setBrush(QColor(20, 30, 20) if is_day else QColor(5, 5, 10))
    painter.setPen(Qt.NoPen)
    painter.drawPath(ground_path)
