"""QPainter presentation adapter for scope view reticle, mask, and HUD metrics."""

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

from TerraLab.scene.plans.scope import ScopePlan


def render_qpainter_scope_plan(
    painter: QPainter, plan: ScopePlan, viewport_w: float, viewport_h: float
) -> None:
    """Renders ScopePlan onto QPainter without executing domain rules or state updates."""
    if not plan.enabled or plan.center_px is None:
        return

    cx, cy = plan.center_px
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)

    # 1. Scope mask
    path = QPainterPath()
    path.addRect(0.0, 0.0, float(viewport_w), float(viewport_h))

    if plan.shape == "circle" and plan.radius_px > 0.0:
        scope_path = QPainterPath()
        scope_path.addEllipse(QPointF(cx, cy), plan.radius_px, plan.radius_px)
        path = path.subtracted(scope_path)
    elif plan.shape == "rectangle" and plan.rect_size_px is not None:
        rw, rh = plan.rect_size_px
        scope_path = QPainterPath()
        scope_path.addRect(cx - rw * 0.5, cy - rh * 0.5, rw, rh)
        path = path.subtracted(scope_path)

    painter.fillPath(path, QColor(10, 12, 16, 220))

    # 2. Reticle outline
    pen = QPen(QColor(255, 200, 80, 200), 1.5, Qt.SolidLine)  # type: ignore
    painter.setPen(pen)

    if plan.shape == "circle" and plan.radius_px > 0.0:
        painter.drawEllipse(QPointF(cx, cy), plan.radius_px, plan.radius_px)
    elif plan.shape == "rectangle" and plan.rect_size_px is not None:
        rw, rh = plan.rect_size_px
        painter.drawRect(QRectF(cx - rw * 0.5, cy - rh * 0.5, rw, rh))

    # 3. Crosshair
    ch_len = 12.0
    painter.drawLine(QPointF(cx - ch_len, cy), QPointF(cx + ch_len, cy))
    painter.drawLine(QPointF(cx, cy - ch_len), QPointF(cx, cy + ch_len))

    # 4. HUD metrics readout box (if available)
    if plan.hud_metrics:
        font = QFont("SansSerif", 9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(220, 220, 230), 1.0))
        lines = []
        if (
            "exit_pupil_mm" in plan.hud_metrics
            and plan.hud_metrics["exit_pupil_mm"] is not None
        ):
            lines.append(f"Pupil: {plan.hud_metrics['exit_pupil_mm']:.2f} mm")
        if (
            "airmass_x" in plan.hud_metrics
            and plan.hud_metrics["airmass_x"] is not None
        ):
            lines.append(f"Airmass: {plan.hud_metrics['airmass_x']:.2f} X")
        if (
            "loss_mag" in plan.hud_metrics
            and plan.hud_metrics["loss_mag"] is not None
        ):
            lines.append(f"Extinction: {plan.hud_metrics['loss_mag']:.2f} mag")

        if lines:
            text = " | ".join(lines)
            painter.setPen(QColor(240, 240, 255, 220))
            painter.drawText(
                QRectF(
                    10,
                    float(viewport_h) - 30,
                    float(viewport_w) - 20,
                    24,
                ),
                Qt.AlignLeft | Qt.AlignVCenter,  # type: ignore
                text,
            )

    painter.restore()
