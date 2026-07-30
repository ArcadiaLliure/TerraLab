"""QPainter presentation adapter for terrain geometry and material plans."""

from __future__ import annotations

import numpy as np
from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QPainter, QPolygonF

from TerraLab.scene.plans.terrain_geometry import TerrainGeometryPlan
from TerraLab.scene.plans.terrain_materials import TerrainMaterialPlan


def render_qpainter_terrain_plan(
    painter: QPainter,
    geometry_plan: TerrainGeometryPlan,
    material_plan: TerrainMaterialPlan | None,
    width: int,
    height: int,
) -> None:
    """Render terrain geometry and material plans onto QPainter.

    Converts numerical arrays into QPainter primitives (polygons, QImage)
    strictly at the presentation boundary without executing domain rules.
    """
    if geometry_plan.triangle_geometry is None:
        return

    tri_geom = geometry_plan.triangle_geometry
    xy = tri_geom.xy
    if xy is None or len(xy) == 0:
        return

    num_triangles = len(xy)

    painter.save()
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)

        vertex_colors = (
            material_plan.vertex_rgba
            if material_plan is not None
            else np.full((num_triangles, 3, 4), 180, dtype=np.uint8)
        )

        # Batch draw triangles using QPolygonF
        for i in range(num_triangles):
            pts = xy[i]
            if pts.ndim == 2 and pts.shape == (3, 2):
                p0 = QPointF(float(pts[0, 0]), float(pts[0, 1]))
                p1 = QPointF(float(pts[1, 0]), float(pts[1, 1]))
                p2 = QPointF(float(pts[2, 0]), float(pts[2, 1]))
            elif pts.size >= 6:
                flat = pts.ravel()
                p0 = QPointF(float(flat[0]), float(flat[1]))
                p1 = QPointF(float(flat[2]), float(flat[3]))
                p2 = QPointF(float(flat[4]), float(flat[5]))
            else:
                continue

            poly = QPolygonF([p0, p1, p2])

            # Resolve triangle average color for QPainter flat fill
            if vertex_colors.ndim == 3:
                avg_color = np.mean(vertex_colors[i], axis=0).astype(np.uint8)
            elif vertex_colors.ndim == 2:
                avg_color = vertex_colors[i]
            else:
                avg_color = np.array([180, 180, 180, 255], dtype=np.uint8)

            qcolor = QColor(
                int(avg_color[0]),
                int(avg_color[1]),
                int(avg_color[2]),
                int(avg_color[3]),
            )

            painter.setBrush(qcolor)
            painter.setPen(Qt.NoPen)
            painter.drawPolygon(poly)
    finally:
        painter.restore()


class QPainterTerrainAdapter:
    """Presentation adapter rendering terrain plans via QPainter."""

    def render(
        self,
        painter: QPainter,
        geometry_plan: TerrainGeometryPlan,
        material_plan: TerrainMaterialPlan | None,
        width: int,
        height: int,
    ) -> None:
        render_qpainter_terrain_plan(
            painter, geometry_plan, material_plan, width, height
        )
