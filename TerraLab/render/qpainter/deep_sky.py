"""QPainter-only presentation adapter for resolved Milky-Way and NGC plans."""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false

from __future__ import annotations

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QImage, QPainter, QPen

from TerraLab.scene.plans.deep_sky import DeepSkyBatch, MilkyWayPlan


class QPainterDeepSkyAdapter:
    """Paint already resolved plans; this adapter performs neither I/O nor astronomy."""

    def __init__(self) -> None:
        self._milkyway_image_key: tuple[object, ...] | None = None
        self._milkyway_image: QImage | None = None

    @staticmethod
    def _composition_mode(blend_mode: str):
        if blend_mode == "add":
            return QPainter.CompositionMode_Plus
        if blend_mode == "screen":
            return QPainter.CompositionMode_Screen
        return QPainter.CompositionMode_SourceOver

    def paint_milkyway(
        self, painter: QPainter, plan: MilkyWayPlan, width: int, height: int
    ) -> None:
        if not plan.visible or plan.rgba is None:
            return
        if (
            self._milkyway_image_key != plan.cache_key
            or self._milkyway_image is None
        ):
            rgba = plan.rgba
            image = QImage(
                rgba.data,
                rgba.shape[1],
                rgba.shape[0],
                rgba.shape[1] * 4,
                QImage.Format_RGBA8888,
            ).copy()
            if image.width() != width or image.height() != height:
                image = image.scaled(
                    width,
                    height,
                    Qt.IgnoreAspectRatio,
                    Qt.SmoothTransformation,
                )
            self._milkyway_image_key = plan.cache_key
            self._milkyway_image = image
        painter.save()
        try:
            painter.setCompositionMode(self._composition_mode(plan.blend_mode))
            painter.drawImage(0, 0, self._milkyway_image)
        finally:
            painter.restore()

    def paint_deep_sky(self, painter: QPainter, batch: DeepSkyBatch) -> None:
        if not batch.glyphs:
            return
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(Qt.NoBrush)
            for glyph in batch.glyphs:
                painter.save()
                try:
                    painter.translate(glyph.screen_x, glyph.screen_y)
                    painter.rotate(glyph.rotation_deg)
                    painter.setPen(QPen(QColor(*glyph.rgba), 1.2))
                    painter.drawEllipse(
                        QPointF(0.0, 0.0), glyph.radius_x_px, glyph.radius_y_px
                    )
                    if glyph.draw_cross:
                        painter.drawLine(
                            QPointF(-glyph.radius_x_px, 0.0),
                            QPointF(glyph.radius_x_px, 0.0),
                        )
                        painter.drawLine(
                            QPointF(0.0, -glyph.radius_y_px),
                            QPointF(0.0, glyph.radius_y_px),
                        )
                    elif glyph.draw_ticks:
                        painter.drawLine(
                            QPointF(-glyph.radius_x_px - 3.0, 0.0),
                            QPointF(-glyph.radius_x_px, 0.0),
                        )
                        painter.drawLine(
                            QPointF(glyph.radius_x_px, 0.0),
                            QPointF(glyph.radius_x_px + 3.0, 0.0),
                        )
                finally:
                    painter.restore()
        finally:
            painter.restore()
