"""QPainter presentation adapter for the shared neutral terrain resource."""

from __future__ import annotations

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QPainter, QPolygonF

from TerraLab.core.rendering_contracts.plans import TerrainMeshResource
class QPainterTerrainAdapter:
    """Presentation adapter rendering terrain plans via QPainter only.

    The adapter deliberately accepts values that are already projected and
    shaded.  It owns Qt clipping, blending and polygon submission, never DEM
    loading, raycasting, surface classification or lighting decisions.
    """

    def paint_mesh(self, painter: QPainter, mesh: TerrainMeshResource) -> int:
        """Paint one fully-resolved terrain mesh in its supplied draw order.

        ``TerrainMeshResource`` is shared with the Three.js protocol.  Its
        coordinates are screen-space for QPainter, so this method simply
        turns its indexed buffers into polygons; it does not project or
        otherwise reinterpret them.
        """

        vertices = mesh.vertices
        colors = mesh.surface.colors
        alphas = mesh.surface.alphas
        if not vertices or not mesh.indices:
            return 0

        painter.save()
        try:
            self._apply_composition_mode(painter, mesh.material.blend_mode)
            clip = QRectF(
                float(mesh.bounds.minimum[0]),
                float(mesh.bounds.minimum[1]),
                float(mesh.bounds.maximum[0] - mesh.bounds.minimum[0]),
                float(mesh.bounds.maximum[1] - mesh.bounds.minimum[1]),
            )
            if clip.width() > 0.0 and clip.height() > 0.0:
                painter.setClipRect(clip, Qt.IntersectClip)
            painter.setPen(Qt.NoPen)
            painter.setRenderHint(QPainter.Antialiasing, True)

            drawn = 0
            for start in range(0, len(mesh.indices), 3):
                indices = mesh.indices[start : start + 3]
                if len(indices) != 3:
                    continue
                points = QPolygonF(
                    [
                        QPointF(
                            float(vertices[index][0]), float(vertices[index][1])
                        )
                        for index in indices
                    ]
                )
                rgba = np.mean(
                    np.asarray([colors[index] for index in indices]), axis=0
                )
                alpha = float(np.mean([alphas[index] for index in indices]))
                opacity = max(0.0, min(1.0, alpha * mesh.material.opacity))
                painter.setBrush(
                    QColor(
                        round(float(rgba[0]) * 255.0),
                        round(float(rgba[1]) * 255.0),
                        round(float(rgba[2]) * 255.0),
                        round(float(rgba[3]) * 255.0 * opacity),
                    )
                )
                painter.drawPolygon(points)
                drawn += 1
            return drawn
        finally:
            painter.restore()

    @staticmethod
    def _apply_composition_mode(
        painter: QPainter,
        blend_mode: str,
    ) -> None:
        modes = {
            "opaque": QPainter.CompositionMode_SourceOver,
            "alpha": QPainter.CompositionMode_SourceOver,
            "add": QPainter.CompositionMode_Plus,
            "multiply": QPainter.CompositionMode_Multiply,
        }
        painter.setCompositionMode(
            modes.get(blend_mode, QPainter.CompositionMode_SourceOver)
        )
