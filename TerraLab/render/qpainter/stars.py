"""QPainter presentation adapter for renderer-neutral stellar batches."""

from __future__ import annotations

import math

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPixmap,
    QRadialGradient,
)

from TerraLab.scene.plans.stars import StarScenePlan


class QPainterStarAdapter:
    """Cache Qt sprites and paint a star plan without scientific decisions."""

    def __init__(self) -> None:
        self._disc_cache: dict[
            tuple[tuple[int, ...], int], tuple[QImage, float]
        ] = {}
        self._bright_cache: dict[
            tuple[tuple[int, ...], int, int, bool], tuple[QImage, float]
        ] = {}
        self._pixmap_cache: dict[int, QPixmap] = {}

    def clear_cache(self) -> None:
        self._disc_cache.clear()
        self._bright_cache.clear()
        self._pixmap_cache.clear()

    def _disc(
        self, rgba: np.ndarray, radius_px: float
    ) -> tuple[QImage, float]:
        radius = min(6.0, max(0.35, float(radius_px)))
        radius_bin = round(radius * 10.0) / 10.0
        color = tuple(int(value) for value in rgba)
        key = (color, int(round(radius_bin * 10.0)))
        cached = self._disc_cache.get(key)
        if cached is not None:
            return cached
        pad = 1
        size = int(max(3, math.ceil(radius_bin * 2.0) + 2 * pad + 1))
        center = (size - 1) * 0.5
        image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(*color)))
            painter.drawEllipse(
                QPointF(center, center), radius_bin, radius_bin
            )
        finally:
            painter.end()
        result = (image, center)
        self._disc_cache[key] = result
        return result

    def _bright(
        self,
        rgba: np.ndarray,
        core_radius_px: float,
        halo_bin: int,
        *,
        pure_colors: bool,
    ) -> tuple[QImage, float]:
        radius = min(6.2, max(1.0, float(core_radius_px)))
        color = tuple(int(value) for value in rgba)
        halo = max(0, int(halo_bin))
        key = (color, int(round(radius * 10.0)), halo, bool(pure_colors))
        cached = self._bright_cache.get(key)
        if cached is not None:
            return cached
        halo_radius = (
            radius * (2.1 + 0.55 * halo) if halo and not pure_colors else 0.0
        )
        outer_radius = max(radius, halo_radius)
        size = int(max(5, math.ceil(outer_radius * 2.0) + 5))
        center = (size - 1) * 0.5
        image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            if halo_radius:
                gradient = QRadialGradient(center, center, halo_radius)
                gradient.setColorAt(
                    0.0,
                    QColor(
                        color[0],
                        color[1],
                        color[2],
                        min(220, int(color[3] * (0.32 + 0.08 * halo))),
                    ),
                )
                gradient.setColorAt(
                    1.0, QColor(color[0], color[1], color[2], 0)
                )
                painter.setBrush(QBrush(gradient))
                painter.drawEllipse(
                    QPointF(center, center), halo_radius, halo_radius
                )
            painter.setBrush(QBrush(QColor(*color)))
            painter.drawEllipse(QPointF(center, center), radius, radius)
            if not pure_colors:
                painter.setBrush(
                    QBrush(QColor(255, 255, 255, int(color[3] * 0.75)))
                )
                painter.drawEllipse(
                    QPointF(center, center), radius * 0.42, radius * 0.42
                )
        finally:
            painter.end()
        result = (image, center)
        self._bright_cache[key] = result
        return result

    def _paint_sprite_batch(
        self,
        painter: QPainter,
        sprite: QImage,
        center: float,
        screen_x: np.ndarray,
        screen_y: np.ndarray,
        indices: np.ndarray,
    ) -> int:
        if len(indices) == 0:
            return 0
        rounded_center = int(round(center))
        try:
            key = int(sprite.cacheKey())
            pixmap = self._pixmap_cache.get(key)
            if pixmap is None or pixmap.isNull():
                pixmap = QPixmap.fromImage(sprite)
                self._pixmap_cache[key] = pixmap
            source = QRectF(
                0.0, 0.0, float(pixmap.width()), float(pixmap.height())
            )
            half_width = float(pixmap.width()) * 0.5
            half_height = float(pixmap.height()) * 0.5
            calls = 0
            for start in range(0, len(indices), 4096):
                chunk = indices[start : start + 4096]
                fragments = [
                    QPainter.PixmapFragment.create(
                        QPointF(
                            float(
                                int(screen_x[index] - rounded_center)
                                + half_width
                            ),
                            float(
                                int(screen_y[index] - rounded_center)
                                + half_height
                            ),
                        ),
                        source,
                    )
                    for index in chunk
                ]
                painter.drawPixmapFragments(fragments, pixmap)  # type: ignore[arg-type]
                calls += 1
            return calls
        except Exception:
            calls = 0
            for index in indices:
                painter.drawImage(
                    int(screen_x[index] - rounded_center),
                    int(screen_y[index] - rounded_center),
                    sprite,
                )
                calls += 1
            return calls

    @staticmethod
    def _runs(indices: np.ndarray, keys: np.ndarray):
        if len(indices) == 0:
            return
        selected_keys = keys[indices]
        if selected_keys.ndim == 1:
            order = np.argsort(selected_keys, kind="mergesort")
            ordered = indices[order]
            style = selected_keys[order]
            start = 0
            while start < len(ordered):
                end = start + 1
                key = int(style[start])
                while end < len(ordered) and int(style[end]) == key:
                    end += 1
                yield ordered[start:end]
                start = end
            return
        else:
            order = np.lexsort(
                tuple(
                    selected_keys[:, column]
                    for column in range(selected_keys.shape[1] - 1, -1, -1)
                )
            )
        ordered = indices[order]
        style = selected_keys[order]
        start = 0
        while start < len(ordered):
            end = start + 1
            while end < len(ordered) and np.array_equal(
                style[start], style[end]
            ):
                end += 1
            yield ordered[start:end]
            start = end

    def paint(
        self, painter: QPainter, plan: StarScenePlan, *, pure_colors: bool
    ) -> int:
        """Paint a resolved plan and return its QPainter fragment-call count."""

        sprites = plan.sprites
        if plan.after_bucket == 0:
            return 0
        # El renderer heredado redondeaba las coordenadas proyectadas antes de
        # componer cada sprite. Conservamos ese contrato de rasterización para
        # evitar desplazamientos subpíxel al cambiar de adaptador.
        screen_x = np.asarray(np.rint(sprites.screen_x), dtype=np.int32)
        screen_y = np.asarray(np.rint(sprites.screen_y), dtype=np.int32)
        painter.save()
        calls = 0
        try:
            painter.setRenderHint(QPainter.Antialiasing, sprites.smooth)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setPen(Qt.PenStyle.NoPen)
            weak_keys = sprites.weak_rgba
            for run in self._runs(
                sprites.weak_indices, sprites.weak_style_key
            ):
                rgba = weak_keys[int(run[0])]
                radius = 0.42 + 0.38 * (float(rgba[3]) / 255.0)
                sprite, center = self._disc(rgba, radius)
                calls += self._paint_sprite_batch(
                    painter, sprite, center, screen_x, screen_y, run
                )
            for run in self._runs(
                sprites.medium_indices, sprites.medium_style_key
            ):
                index = int(run[0])
                sprite, center = self._disc(
                    sprites.medium_rgba[index],
                    float(sprites.medium_radius_tenths[index]) / 10.0,
                )
                calls += self._paint_sprite_batch(
                    painter, sprite, center, screen_x, screen_y, run
                )
            for index in sprites.bright_indices:
                core_radius = min(
                    5.8, max(1.0, 1.10 + 0.55 * float(sprites.size_bin[index]))
                )
                sprite, center = self._bright(
                    sprites.base_rgba[index],
                    core_radius,
                    int(sprites.halo_bin[index]),
                    pure_colors=pure_colors,
                )
                calls += self._paint_sprite_batch(
                    painter,
                    sprite,
                    center,
                    screen_x,
                    screen_y,
                    np.asarray([index], dtype=np.int32),
                )
        finally:
            painter.restore()
        return calls
