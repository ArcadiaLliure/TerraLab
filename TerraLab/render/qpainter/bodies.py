"""QPainter presentation adapter for resolved Solar-system and trail plans."""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import math

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)

from TerraLab.scene.plans.bodies import (
    CelestialBodiesPlan,
    MoonDiscPlan,
    SunDiscPlan,
    TrailPlan,
)


class QPainterBodiesAdapter:
    """Paint declarative celestial plans without astronomy or photometry."""

    def __init__(self) -> None:
        self._trail_cache_key: tuple[object, ...] | None = None
        self._trail_image: QImage | None = None

    @property
    def trail_image(self) -> QImage | None:
        """Expose the cached raster for lifecycle diagnostics and compatibility."""

        return self._trail_image

    def clear_cache(self) -> None:
        self._trail_cache_key = None
        self._trail_image = None

    @staticmethod
    def _colour(rgba: tuple[int, int, int, int]) -> QColor:
        return QColor(*rgba)

    def paint_bodies(
        self, painter: QPainter, plan: CelestialBodiesPlan
    ) -> None:
        """Translate a fully resolved bodies plan into QPainter calls."""

        if plan.sun is not None:
            self._paint_sun(painter, plan.sun)
        if plan.moon is not None:
            self._paint_moon(painter, plan.moon)
        if plan.planets:
            self._paint_planets(painter, plan)

    def _paint_sun(self, painter: QPainter, sun: SunDiscPlan) -> None:
        geometry = sun.geometry
        painter.save()
        try:
            if sun.corona is not None:
                self._paint_corona(painter, sun)
            gradient = QRadialGradient(
                geometry.screen_x, geometry.screen_y, geometry.radius_x_px
            )
            gradient.setColorAt(0.0, self._colour(sun.core_rgba))
            gradient.setColorAt(0.7, self._colour(sun.core_rgba))
            gradient.setColorAt(1.0, self._colour(sun.edge_rgba))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(gradient))
            painter.drawEllipse(
                QPointF(geometry.screen_x, geometry.screen_y),
                geometry.radius_x_px,
                geometry.radius_y_px,
            )
        finally:
            painter.restore()

    def _paint_corona(self, painter: QPainter, sun: SunDiscPlan) -> None:
        corona = sun.corona
        if corona is None or corona.strength <= 0.0:
            return
        painter.save()
        try:
            painter.translate(corona.screen_x, corona.screen_y)
            painter.scale(1.0, max(0.55, min(1.0, corona.vertical_scale)))
            painter.rotate(corona.orientation_deg)
            painter.setPen(Qt.NoPen)
            radius = corona.radius_px * (2.4 + 0.8 * corona.strength)
            glow = QRadialGradient(0.0, 0.0, radius)
            glow.setColorAt(
                0.0, QColor(246, 251, 255, int(225 * corona.strength))
            )
            glow.setColorAt(
                0.18, QColor(236, 247, 255, int(190 * corona.strength))
            )
            glow.setColorAt(
                0.42, QColor(196, 220, 246, int(72 * corona.strength))
            )
            glow.setColorAt(
                0.72, QColor(154, 190, 230, int(23 * corona.strength))
            )
            glow.setColorAt(1.0, QColor(130, 175, 220, 0))
            painter.setBrush(QBrush(glow))
            painter.drawEllipse(QPointF(0.0, 0.0), radius, radius)
            for angle, length, width, offset in corona.streamers:
                painter.save()
                painter.rotate(angle)
                painter.translate(corona.radius_px * offset, 0.0)
                painter.scale(length, width)
                lobe_radius = corona.radius_px * 0.95
                lobe = QRadialGradient(0.0, 0.0, lobe_radius)
                lobe.setColorAt(
                    0.0, QColor(244, 250, 255, int(44 * corona.strength))
                )
                lobe.setColorAt(
                    0.35, QColor(226, 240, 255, int(31 * corona.strength))
                )
                lobe.setColorAt(
                    0.72, QColor(190, 217, 245, int(10 * corona.strength))
                )
                lobe.setColorAt(1.0, QColor(160, 198, 236, 0))
                painter.setBrush(QBrush(lobe))
                painter.drawEllipse(
                    QPointF(0.0, 0.0), lobe_radius, lobe_radius
                )
                painter.restore()
        finally:
            painter.restore()

    @staticmethod
    def _moon_path(moon: MoonDiscPlan) -> QPainterPath:
        path = QPainterPath()
        if not moon.lit_outline:
            return path
        first_x, first_y = moon.lit_outline[0]
        path.moveTo(first_x, first_y)
        for x, y in moon.lit_outline[1:]:
            path.lineTo(x, y)
        path.closeSubpath()
        return path

    def _paint_moon(self, painter: QPainter, moon: MoonDiscPlan) -> None:
        geometry = moon.geometry
        painter.save()
        try:
            alpha = int(round(255.0 * moon.visibility_alpha))
            if moon.night and not moon.eclipsing:
                glow_x = geometry.radius_x_px * 4.0
                glow_y = geometry.radius_y_px * 4.0
                glow = QRadialGradient(
                    geometry.screen_x, geometry.screen_y, glow_x
                )
                glow.setColorAt(
                    0.0,
                    QColor(
                        220, 230, 245, int(65 * max(0.1, moon.illumination))
                    ),
                )
                glow.setColorAt(1.0, QColor(200, 215, 235, 0))
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(glow))
                painter.drawEllipse(
                    QPointF(geometry.screen_x, geometry.screen_y),
                    glow_x,
                    glow_y,
                )
            painter.setPen(Qt.NoPen)
            if moon.eclipsing and moon.physical_sun_geometry is not None:
                sun = moon.physical_sun_geometry
                moon_disc = QPainterPath()
                moon_disc.addEllipse(
                    QPointF(geometry.screen_x, geometry.screen_y),
                    geometry.radius_x_px,
                    geometry.radius_y_px,
                )
                sun_disc = QPainterPath()
                sun_disc.addEllipse(
                    QPointF(sun.screen_x, sun.screen_y),
                    sun.radius_x_px,
                    sun.radius_y_px,
                )
                painter.setBrush(QColor(2, 3, 5, 255))
                painter.drawPath(moon_disc.intersected(sun_disc))
            painter.translate(geometry.screen_x, geometry.screen_y)
            painter.scale(
                1.0, geometry.radius_y_px / max(0.05, geometry.radius_x_px)
            )
            if moon.night and not moon.eclipsing:
                painter.setBrush(QColor(18, 21, 28, 105))
                painter.drawEllipse(
                    QPointF(0.0, 0.0),
                    geometry.radius_x_px,
                    geometry.radius_x_px,
                )
            painter.rotate(moon.rotation_deg)
            lit_path = self._moon_path(moon)
            if not moon.eclipsing:
                painter.setBrush(
                    QColor(239, 239, 233, alpha)
                    if moon.night
                    else QColor(218, 226, 232, alpha)
                )
                painter.drawPath(lit_path)
            if (
                not moon.eclipsing
                and geometry.radius_x_px >= 5.0
                and moon.illumination > 0.01
            ):
                painter.setClipPath(lit_path)
                painter.rotate(-moon.rotation_deg)
                painter.setBrush(QColor(175, 180, 188, int(alpha * 0.58)))
                radius = geometry.radius_x_px
                painter.drawEllipse(
                    QPointF(-radius * 0.2, -radius * 0.4),
                    radius * 0.25,
                    radius * 0.25,
                )
                painter.drawEllipse(
                    QPointF(radius * 0.2, -radius * 0.3),
                    radius * 0.2,
                    radius * 0.2,
                )
                painter.drawEllipse(
                    QPointF(radius * 0.3, -radius * 0.1),
                    radius * 0.22,
                    radius * 0.22,
                )
                painter.drawEllipse(
                    QPointF(-radius * 0.5, -radius * 0.1),
                    radius * 0.3,
                    radius * 0.5,
                )
        finally:
            painter.restore()

    def _paint_planets(
        self, painter: QPainter, plan: CelestialBodiesPlan
    ) -> None:
        painter.save()
        try:
            for planet in plan.planets:
                painter.setPen(Qt.NoPen)
                painter.setBrush(self._colour(planet.rgba))
                painter.drawEllipse(
                    QPointF(planet.screen_x, planet.screen_y),
                    planet.radius_px,
                    planet.radius_px,
                )
        finally:
            painter.restore()

    def paint_trails(
        self,
        painter: QPainter,
        width: int,
        height: int,
        plan: TrailPlan | None,
    ) -> None:
        """Rasterize a pure trail plan once and composite its cached image."""

        if plan is None or plan.empty:
            self.clear_cache()
            return
        if (
            self._trail_cache_key != plan.cache_key
            or self._trail_image is None
        ):
            image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
            image.fill(Qt.transparent)
            trail_painter = QPainter(image)
            try:
                trail_painter.setRenderHint(
                    QPainter.Antialiasing, plan.alpha > 100
                )
                trail_painter.setBrush(Qt.NoBrush)
                paths: dict[tuple[int, int, int], QPainterPath] = {}
                jump_limit = max(width, height) * 0.42
                rows, columns = plan.screen_x.shape
                for row in range(rows):
                    colour = (
                        int(plan.rgb[row, 0]),
                        int(plan.rgb[row, 1]),
                        int(plan.rgb[row, 2]),
                    )
                    path = paths.setdefault(colour, QPainterPath())
                    drawing = False
                    previous_x = previous_y = 0.0
                    for column in range(columns):
                        if not bool(plan.valid[row, column]):
                            drawing = False
                            continue
                        x = float(plan.screen_x[row, column])
                        y = float(plan.screen_y[row, column])
                        if (
                            drawing
                            and math.hypot(x - previous_x, y - previous_y)
                            <= jump_limit
                        ):
                            path.lineTo(x, y)
                        else:
                            path.moveTo(x, y)
                        drawing = True
                        previous_x, previous_y = x, y
                for colour, path in paths.items():
                    trail_painter.setPen(
                        QPen(QColor(*colour, plan.alpha), 1.0)
                    )
                    trail_painter.drawPath(path)
            finally:
                trail_painter.end()
            self._trail_image = image
            self._trail_cache_key = plan.cache_key
        painter.drawImage(0, 0, self._trail_image)
