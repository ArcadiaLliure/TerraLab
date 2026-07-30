"""QPainter View adapter for renderer-neutral sky-background plans."""

from __future__ import annotations

from dataclasses import dataclass

from PyQt5.QtCore import QRectF
from PyQt5.QtGui import QColor, QImage, QPainter

from TerraLab.scene.plans.sky import SkyBackgroundPlan


def qcolor_from_rgba(rgba: tuple[int, int, int, int]) -> QColor:
    """Translate an already resolved Model colour to the Qt colour type."""

    return QColor(rgba[0], rgba[1], rgba[2], rgba[3])


@dataclass(frozen=True, slots=True)
class SkyBackgroundPaintResult:
    """Observable adapter outcome used by render diagnostics and benchmarks."""

    cache_hit: bool
    sample_buffer_bytes: int


class QPainterSkyBackgroundAdapter:
    """Translate already-resolved RGBA samples into a scaled QImage only."""

    def __init__(self) -> None:
        self._cached_token: tuple[object, ...] | None = None
        self._cached_image: QImage | None = None
        self.cache_hits = 0
        self.cache_misses = 0

    def paint(
        self,
        painter: QPainter,
        plan: SkyBackgroundPlan,
        *,
        width: int,
        height: int,
    ) -> SkyBackgroundPaintResult:
        """Create or reuse the small sample image and compose it to the viewport."""

        cache_hit = (
            self._cached_image is not None
            and self._cached_token == plan.cache_token
        )
        if not cache_hit:
            image = QImage(
                plan.sample_width,
                plan.sample_height,
                QImage.Format_RGB32,
            )
            for index, rgba in enumerate(plan.rgba_samples):
                sample_x = index % plan.sample_width
                sample_y = index // plan.sample_width
                alpha = rgba[3] if plan.horizon_mask[index] else 0
                image.setPixelColor(
                    sample_x,
                    sample_y,
                    QColor(rgba[0], rgba[1], rgba[2], alpha),
                )
            self._cached_image = image
            self._cached_token = plan.cache_token
            self.cache_misses += 1
        else:
            self.cache_hits += 1

        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(
            QRectF(0.0, 0.0, float(width), float(height)),
            self._cached_image,
        )
        return SkyBackgroundPaintResult(
            cache_hit=cache_hit,
            sample_buffer_bytes=len(plan.rgba_samples) * 4,
        )

    def clear_cache(self) -> None:
        """Release the adapter-owned QImage on renderer shutdown."""

        self._cached_token = None
        self._cached_image = None
