import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen

from TerraLab.common.utils import getTraduction
from TerraLab.widgets.spherical_math import (
    angular_distance,
    destination_point,
    screen_to_sky,
    slerp_arc_points,
)


SkyCoord = Tuple[float, float]  # (alt_deg, az_deg)


@dataclass
class SensorPreset:
    key: str
    width_mm: float
    height_mm: float


SENSOR_PRESETS: Dict[str, SensorPreset] = {
    # Hook: phase 2 can be expanded with real devices.
    "tiny": SensorPreset("tiny", 5.37, 4.04),         # ~1/2.8"
    "aps_c": SensorPreset("aps_c", 23.6, 15.7),       # APS-C
    "full_frame": SensorPreset("full_frame", 36.0, 24.0),
}


class TelescopeScopeController:
    SHAPE_CIRCLE = "circle"
    SHAPE_RECT = "rectangle"
    SPEED_SLOW = "slow"
    SPEED_FAST = "fast"

    # Requested movement characteristics
    SLOW_HOLD_DEG_PER_S = 0.5 / 60.0      # 0.5 arcmin / s
    FAST_HOLD_DEG_PER_S = 0.5             # 0.5 deg / s
    SLOW_STEP_DEG = 0.05 / 60.0           # 0.05 arcmin
    FAST_STEP_DEG = 0.05                  # 0.05 deg

    def __init__(self):
        self.enabled = False
        self.awaiting_center_click = False
        self.shape = self.SHAPE_CIRCLE
        self.speed_mode = self.SPEED_SLOW
        self.focal_mm = 250.0
        self.sensor_key = "tiny"
        self.aspect_ratio_override: Optional[float] = None  # width / height for rectangle mode
        self.center: Optional[SkyCoord] = None
        self.dragging = False
        self.last_mouse = QPointF(0.0, 0.0)
        self.manual_override: Optional[Tuple[float, float]] = None

    def activate(self) -> None:
        self.enabled = True
        self.awaiting_center_click = True
        self.dragging = False

    def deactivate(self) -> None:
        self.enabled = False
        self.awaiting_center_click = False
        self.dragging = False

    def set_shape(self, shape: str) -> None:
        if shape in (self.SHAPE_CIRCLE, self.SHAPE_RECT):
            self.shape = shape

    def set_speed_mode(self, mode: str) -> None:
        if mode in (self.SPEED_SLOW, self.SPEED_FAST):
            self.speed_mode = mode

    def set_focal_mm(self, focal_mm: float) -> None:
        self.focal_mm = max(1.0, float(focal_mm))

    def set_sensor_key(self, key: str) -> None:
        if key in SENSOR_PRESETS:
            self.sensor_key = key

    def set_aspect_ratio(self, ratio: Optional[float]) -> None:
        if ratio is None:
            self.aspect_ratio_override = None
            return
        try:
            r = float(ratio)
        except Exception:
            return
        self.aspect_ratio_override = max(0.2, min(5.0, r))

    def set_manual_fov(self, width_deg: float, height_deg: Optional[float] = None) -> None:
        h = width_deg if height_deg is None else height_deg
        self.manual_override = (max(0.01, float(width_deg)), max(0.01, float(h)))

    def clear_manual_fov(self) -> None:
        self.manual_override = None

    def current_fov(self) -> Tuple[float, float]:
        if self.manual_override is not None:
            w, h = self.manual_override
        else:
            sensor = SENSOR_PRESETS.get(self.sensor_key, SENSOR_PRESETS["tiny"])
            f = max(1e-3, self.focal_mm)
            w = math.degrees(2.0 * math.atan(sensor.width_mm / (2.0 * f)))
            h = math.degrees(2.0 * math.atan(sensor.height_mm / (2.0 * f)))

        # Flexible aspect ratio only affects rectangle format.
        if self.shape == self.SHAPE_RECT and self.aspect_ratio_override is not None:
            ar = max(0.2, min(5.0, float(self.aspect_ratio_override)))
            w = max(0.01, float(w))
            h = max(0.01, float(h))
            if w >= h:
                h = max(0.01, w / ar)
            else:
                w = max(0.01, h * ar)
        return max(0.01, w), max(0.01, h)

    def short_step_deg(self) -> float:
        return self.SLOW_STEP_DEG if self.speed_mode == self.SPEED_SLOW else self.FAST_STEP_DEG

    def hold_rate_deg_per_s(self) -> float:
        return self.SLOW_HOLD_DEG_PER_S if self.speed_mode == self.SPEED_SLOW else self.FAST_HOLD_DEG_PER_S

    def handle_click(self, sx: float, sy: float, unproject_fn: Callable) -> bool:
        if not self.enabled:
            return False
        sky = screen_to_sky(sx, sy, unproject_fn)
        if sky is None:
            return True
        self.center = sky
        self.awaiting_center_click = False
        return True

    def set_center(self, sky: SkyCoord) -> None:
        self.center = self._normalized_center(sky)
        self.awaiting_center_click = False

    def start_drag(self, sx: float, sy: float) -> None:
        self.dragging = True
        self.last_mouse = QPointF(float(sx), float(sy))

    def drag_move(self, sx: float, sy: float, unproject_fn: Callable) -> bool:
        if not self.enabled or not self.dragging:
            return False
        if self.center is None:
            return True

        c = self.center
        p0 = self.last_mouse
        sky_prev = screen_to_sky(p0.x(), p0.y(), unproject_fn)
        sky_now = screen_to_sky(float(sx), float(sy), unproject_fn)
        if sky_prev is None or sky_now is None:
            self.last_mouse = QPointF(float(sx), float(sy))
            return True

        # Camera-like drag: movement of mouse displaces target in opposite direction.
        d_alt = sky_now[0] - sky_prev[0]
        d_az = ((sky_now[1] - sky_prev[1] + 180.0) % 360.0) - 180.0
        self.center = self._normalized_center((c[0] - d_alt, c[1] - d_az))
        self.last_mouse = QPointF(float(sx), float(sy))
        return True

    def end_drag(self) -> None:
        self.dragging = False

    def nudge(self, d_alt_deg: float, d_az_deg: float) -> None:
        if not self.enabled:
            return
        if self.center is None:
            self.center = (0.0, 0.0)
        alt, az = self.center
        self.center = self._normalized_center((alt + d_alt_deg, az + d_az_deg))

    def draw(
        self,
        painter: QPainter,
        width: int,
        height: int,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        hud_extra_lines: Optional[List[str]] = None,
    ) -> None:
        if not self.enabled:
            return

        # Dark overlay while waiting for center selection.
        if self.center is None or self.awaiting_center_click:
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 170))
            painter.drawRect(QRectF(0.0, 0.0, float(width), float(height)))
            painter.setPen(QColor(255, 255, 255, 220))
            wait_txt = getTraduction("Scope.ClickCenter", "Click para fijar el centro de la mira")
            painter.drawText(QRectF(10.0, 10.0, width - 20.0, 28.0), Qt.AlignLeft | Qt.AlignVCenter, wait_txt)
            painter.restore()
            return

        boundary_sky = self._boundary_points()
        boundary_screen = self._project_valid(boundary_sky, project_fn)
        if len(boundary_screen) < 8:
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 180))
            painter.drawRect(QRectF(0.0, 0.0, float(width), float(height)))
            painter.restore()
            return

        hole = QPainterPath()
        hole.moveTo(boundary_screen[0])
        for p in boundary_screen[1:]:
            hole.lineTo(p)
        hole.closeSubpath()

        full = QPainterPath()
        full.addRect(QRectF(0.0, 0.0, float(width), float(height)))
        # Keep the interior fully clear: only shade the outside area.
        # Hook for future magnitude simulation should be applied inside this hole.
        outside = full.subtracted(hole)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190))
        painter.drawPath(outside)

        # Border + glow
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 35), 1.2))
        painter.drawPath(hole)
        painter.setPen(QPen(QColor(255, 255, 255, 120), 0.8))
        painter.drawPath(hole)

        # Crosshair
        self._draw_crosshair(painter, project_fn)

        # HUD
        self._draw_hud(
            painter,
            project_fn,
            width,
            height,
            boundary_screen,
            hud_extra_lines=hud_extra_lines,
        )
        painter.restore()

    def _normalized_center(self, c: SkyCoord) -> SkyCoord:
        alt = max(-89.9, min(89.9, c[0]))
        az = c[1] % 360.0
        return alt, az

    def _boundary_points(self) -> List[SkyCoord]:
        if self.center is None:
            return []
        if self.shape == self.SHAPE_CIRCLE:
            return self._circle_boundary()
        return self._rect_boundary()

    def _circle_boundary(self) -> List[SkyCoord]:
        w, h = self.current_fov()
        d = min(w, h)
        r = d * 0.5
        out: List[SkyCoord] = []
        steps = 180
        for i in range(steps + 1):
            b = 360.0 * i / steps
            out.append(destination_point(self.center, b, r))
        return out

    def _rect_boundary(self) -> List[SkyCoord]:
        c_alt, c_az = self.center
        fov_w, fov_h = self.current_fov()
        half_h = 0.5 * fov_h
        half_w = 0.5 * fov_w
        cos_lat = max(0.05, math.cos(math.radians(c_alt)))
        daz = half_w / cos_lat

        p00 = (max(-89.9, min(89.9, c_alt - half_h)), (c_az - daz) % 360.0)
        p10 = (max(-89.9, min(89.9, c_alt - half_h)), (c_az + daz) % 360.0)
        p11 = (max(-89.9, min(89.9, c_alt + half_h)), (c_az + daz) % 360.0)
        p01 = (max(-89.9, min(89.9, c_alt + half_h)), (c_az - daz) % 360.0)

        e1 = slerp_arc_points(p00, p10, 36)
        e2 = slerp_arc_points(p10, p11, 36)
        e3 = slerp_arc_points(p11, p01, 36)
        e4 = slerp_arc_points(p01, p00, 36)
        return e1 + e2[1:] + e3[1:] + e4[1:]

    def _project_valid(
        self,
        pts: List[SkyCoord],
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
    ) -> List[QPointF]:
        out: List[QPointF] = []
        for alt, az in pts:
            p = project_fn(alt, az)
            if p is None:
                continue
            out.append(QPointF(float(p[0]), float(p[1])))
        return out

    def _draw_crosshair(self, painter: QPainter, project_fn: Callable) -> None:
        if self.center is None:
            return
        cpt = project_fn(*self.center)
        if cpt is None:
            return

        cx = float(cpt[0])
        cy = float(cpt[1])
        arm = 8.0

        # Very subtle compact center reticle.
        painter.setPen(QPen(QColor(255, 255, 255, 60), 1.4, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
        painter.drawLine(QPointF(cx, cy - arm), QPointF(cx, cy + arm))

        painter.setPen(QPen(QColor(255, 255, 255, 150), 0.8, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
        painter.drawLine(QPointF(cx, cy - arm), QPointF(cx, cy + arm))

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 170))
        painter.drawEllipse(QPointF(cx, cy), 1.2, 1.2)

    def _draw_hud(
        self,
        painter: QPainter,
        project_fn: Callable,
        width: int,
        height: int,
        boundary_screen: Optional[List[QPointF]] = None,
        hud_extra_lines: Optional[List[str]] = None,
    ) -> None:
        if self.center is None:
            return
        cpt = project_fn(*self.center)
        if cpt is None:
            return

        def fmt_angle(v_deg: float) -> str:
            return f"{v_deg:.3f}° ({v_deg * 60.0:.1f}')"

        fov_w, fov_h = self.current_fov()
        if self.shape == self.SHAPE_CIRCLE:
            text = getTraduction("Scope.HudFovCircle", "FOV: {d}").format(d=fmt_angle(min(fov_w, fov_h)))
        else:
            text = getTraduction("Scope.HudFovRect", "FOV: {w} x {h}").format(w=fmt_angle(fov_w), h=fmt_angle(fov_h))
        speed = getTraduction("Scope.HudSlow", "LENTO") if self.speed_mode == self.SPEED_SLOW else getTraduction("Scope.HudFast", "RAPIDO")
        lines = [f"{text} | {getTraduction('Scope.HudMove', 'Movimiento')}: {speed} (M)"]
        if hud_extra_lines:
            for extra in hud_extra_lines:
                if extra:
                    lines.append(str(extra))

        line_h = 14.0
        hud_w = 320.0
        hud_h = 8.0 + line_h * len(lines)

        if boundary_screen:
            bottom = max(float(p.y()) for p in boundary_screen)
            x = float(cpt[0]) - (hud_w * 0.5)
            y = bottom + 8.0
        else:
            x = float(cpt[0]) + 12.0
            y = float(cpt[1]) + 12.0

        x = max(4.0, min(float(width) - hud_w - 4.0, x))
        y = max(4.0, min(float(height) - hud_h - 4.0, y))
        rect = QRectF(x, y, hud_w, hud_h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRoundedRect(rect, 5.0, 5.0)
        painter.setPen(QColor(255, 255, 255, 230))
        for i, line in enumerate(lines):
            lrect = QRectF(x + 8.0, y + 4.0 + line_h * i, hud_w - 16.0, line_h)
            painter.drawText(lrect, Qt.AlignLeft | Qt.AlignVCenter, line)
