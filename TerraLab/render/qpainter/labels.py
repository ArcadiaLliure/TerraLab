"""QPainter adapter for resolved informational-overlay plans and text batches."""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from dataclasses import dataclass

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontInfo,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
)

from TerraLab.scene.plans.labels import (
    CompassPlan,
    FontStyle,
    GridPlan,
    HudPlan,
    ScreenRect,
    TextBatch,
    TextMetrics,
    TextStyle,
)


@dataclass(frozen=True, slots=True)
class FontResolution:
    """Observable concrete-font decision made exclusively by the View."""

    requested_family: str
    resolved_family: str
    registered: bool


class QPainterFontMetrics:
    """Qt implementation of the application-owned ``FontMetricsPort``."""

    def __init__(self) -> None:
        self._families = frozenset(QFontDatabase().families())

    @property
    def revision(self) -> str:
        return "qt-fonts:" + str(len(self._families))

    def resolution_for(self, style: TextStyle) -> FontResolution:
        requested = style.font.family
        registered = requested in self._families
        font = self._font_for(style.font)
        resolved = (
            QFontInfo(font).family() or font.defaultFamily() or font.family()
        )
        return FontResolution(requested, resolved, registered)

    @staticmethod
    def _font_for(style: FontStyle) -> QFont:
        font = QFont(style.family)
        font.setPixelSize(max(1, int(round(style.pixel_size))))
        font.setWeight(int(style.weight))
        font.setItalic(style.italic)
        font.setStyleHint(QFont.SansSerif)
        return font

    def measure_text(self, text: str, style: TextStyle) -> TextMetrics:
        metrics = QFontMetricsF(self._font_for(style.font))
        return TextMetrics(
            max(0.0, float(metrics.horizontalAdvance(text))),
            max(0.0, float(metrics.ascent())),
            max(0.0, float(metrics.descent())),
            max(0.0, float(metrics.leading())),
        )


class QPainterLabelsAdapter:
    """Materialize only pre-resolved paths, strokes, boxes and text."""

    @staticmethod
    def _colour(rgba: tuple[int, int, int, int]) -> QColor:
        return QColor(*rgba)

    @staticmethod
    def _rect(rect: ScreenRect) -> QRectF:
        return QRectF(rect.x, rect.y, rect.width, rect.height)

    @staticmethod
    def _font(style: TextStyle) -> QFont:
        return QPainterFontMetrics._font_for(style.font)

    def paint_grid(self, painter: QPainter, plan: GridPlan) -> int:
        if not plan.visible:
            return 0
        painter.save()
        try:
            pen = QPen(self._colour(plan.style.rgba), plan.style.width_px)
            if plan.style.dashed:
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            count = 0
            for points in plan.paths:
                if len(points) < 2:
                    continue
                path = QPainterPath(QPointF(*points[0]))
                for point in points[1:]:
                    path.lineTo(QPointF(*point))
                painter.drawPath(path)
                count += 1
            return count
        finally:
            painter.restore()

    def paint_compass(self, painter: QPainter, plan: CompassPlan) -> int:
        painter.save()
        try:
            for tick in plan.ticks:
                painter.setPen(
                    QPen(self._colour(tick.style.rgba), tick.style.width_px)
                )
                painter.drawLine(QPointF(*tick.start), QPointF(*tick.end))
        finally:
            painter.restore()
        return len(plan.ticks) + self.paint_text_batch(painter, plan.labels)

    def paint_hud(self, painter: QPainter, plan: HudPlan) -> int:
        if not plan.visible:
            return 0
        painter.save()
        try:
            painter.setPen(
                QPen(self._colour(plan.border.rgba), plan.border.width_px)
            )
            painter.setBrush(self._colour(plan.background_rgba))
            painter.drawRoundedRect(
                self._rect(plan.bounds),
                plan.corner_radius_px,
                plan.corner_radius_px,
            )
        finally:
            painter.restore()
        return 1 + self.paint_text_batch(painter, plan.labels)

    def paint_text_batch(self, painter: QPainter, batch: TextBatch) -> int:
        """Paint resolved labels without inspecting canvas, state or catalogue data."""

        if not batch.labels:
            return 0
        painter.save()
        try:
            active_clip: ScreenRect | None | object = object()
            for label in batch.labels:
                if label.clip != active_clip:
                    painter.setClipping(False)
                    if label.clip is not None:
                        painter.setClipRect(self._rect(label.clip))
                    active_clip = label.clip
                painter.setFont(self._font(label.style))
                if label.style.background_rgba is not None:
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(self._colour(label.style.background_rgba))
                    painter.drawRoundedRect(
                        self._rect(label.bounds),
                        label.style.corner_radius_px,
                        label.style.corner_radius_px,
                    )
                painter.setPen(self._colour(label.style.foreground_rgba))
                x, baseline_y = label.position
                if label.anchor == "baseline_center":
                    x -= (
                        label.bounds.width * 0.5
                        - label.style.background_padding_x
                    )
                painter.drawText(QPointF(x, baseline_y), label.text)
            return len(batch.labels)
        finally:
            painter.restore()
