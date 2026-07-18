"""Horizon renderer layer interface.

Current implementation is a no-op placeholder because horizon is still drawn
in the legacy AstroCanvas path. Kept for modular migration compatibility.
"""

from __future__ import annotations

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QPainterPath


class HorizonRenderer:
    def render(self, ctx, state):
        """Renderitza el contingut visual segons l'estat actual.

        Par?metres:
        - ctx (Any): Valor del parametre 'ctx'.
        - state (Any): Valor del parametre 'state'.

        Retorna:
        - None.
        """
        callback = None
        extras = getattr(state, "extras", {}) or {}
        if isinstance(extras, dict):
            callback = extras.get("render_horizon")
        if callable(callback):
            callback(ctx, state)


def draw_ground_mask(painter, is_day, *, canvas=None, impl=None):
    if callable(impl):
        return impl(painter, is_day)
    return draw_ground_mask_impl(canvas, painter, is_day)


def draw_ground_mask_impl(canvas, painter, is_day):
    if canvas is None:
        return

    ground_path = QPainterPath()
    points = []
    for offset in (-360, 0, 360):
        for az in range(0, 361, 10):
            pt = canvas.project_universal_stereo(0, az + offset)
            if pt:
                points.append(QPointF(*pt))
            elif points:
                points.append(None)

    if not points:
        return

    bottom_y = canvas.height() * 2.0
    first = True
    current_block_start = None
    for p in points:
        if p is None:
            if not first and current_block_start:
                ground_path.lineTo(ground_path.currentPosition().x(), bottom_y)
                ground_path.lineTo(current_block_start.x(), bottom_y)
                ground_path.closeSubpath()
            first = True
            continue
        if first:
            ground_path.moveTo(p)
            current_block_start = p
            first = False
        else:
            ground_path.lineTo(p)

    if not first and current_block_start:
        ground_path.lineTo(ground_path.currentPosition().x(), bottom_y)
        ground_path.lineTo(current_block_start.x(), bottom_y)
        ground_path.closeSubpath()

    col = QColor(20, 30, 20) if bool(is_day) else QColor(5, 5, 10)
    painter.setBrush(col)
    painter.setPen(Qt.NoPen)
    painter.drawPath(ground_path)
