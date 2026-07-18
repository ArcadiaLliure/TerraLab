"""Grid renderer layer interface.

Current implementation delegates to optional callback for safe migration.
"""

from __future__ import annotations

import math

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QPainterPath, QPen


class GridRenderer:
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
            callback = extras.get("render_grid")
        if callable(callback):
            callback(ctx, state)


def draw_celestial_grid(painter, hour, *, canvas=None, impl=None):
    if callable(impl):
        return impl(painter, hour)
    return draw_celestial_grid_impl(canvas, painter, hour)


def draw_celestial_grid_impl(canvas, painter, hour):
    if canvas is None:
        return

    painter.setPen(QPen(QColor(0, 255, 255, 80), 1, Qt.DashLine))
    day_of_year = canvas.parent_widget.manual_day
    lst = (
        100.0
        + day_of_year * 0.9856
        + float(hour) * 15.0
        + canvas.parent_widget.longitude
    ) % 360

    path = QPainterPath()
    first_pt = True
    lat_rad = math.radians(canvas.parent_widget.latitude)

    steps = 72
    for i in range(steps + 1):
        ra = i * 360.0 / steps
        ha = lst - ra
        ha_rad = math.radians(ha)

        sin_alt = math.cos(lat_rad) * math.cos(ha_rad)
        alt = math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))

        cos_az = (-sin_alt * math.sin(lat_rad)) / (
            math.cos(math.radians(alt)) * math.cos(lat_rad) + 1e-10
        )
        az = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
        if math.sin(ha_rad) > 0:
            az = 360 - az

        pt = canvas.project_universal_stereo(alt, az)
        if pt:
            if first_pt:
                path.moveTo(QPointF(*pt))
                first_pt = False
            else:
                path.lineTo(QPointF(*pt))
        else:
            first_pt = True

    painter.drawPath(path)
