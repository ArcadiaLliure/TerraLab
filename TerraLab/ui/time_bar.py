"""Interactive astronomical time bar widgets."""

from __future__ import annotations

import math
from datetime import datetime

from PyQt5.QtCore import (
    Qt,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt5.QtWidgets import (
    QLabel,
    QWidget,
)

class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


# --- ASTRO ENGINE CORE (ELP 2000-82 / VSOP87) ---
class RusticTimeBar(QWidget):
    valueChanged = pyqtSignal(float)
    dragStateChanged = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self.current_hour = 12.0
        self.setCursor(Qt.PointingHandCursor)
        self.hours_text = [0, 6, 12, 18, 24]

        # Defaults
        self.lat = 0.0
        self.lon = 0.0
        self.day_of_year = 1
        self._bg_cache_image = None
        self._bg_cache_key = None
        self._dragging = False

    def set_time(self, hour):
        self.current_hour = hour % 24.0
        self.update()

    def update_params(self, lat, lon, day_of_year):
        new_lat = float(lat)
        new_lon = float(lon)
        new_day = int(day_of_year)
        changed = (
            abs(new_lat - float(self.lat)) > 1e-6
            or abs(new_lon - float(self.lon)) > 1e-6
            or new_day != int(self.day_of_year)
        )
        self.lat = new_lat
        self.lon = new_lon
        self.day_of_year = new_day
        if changed:
            self._bg_cache_image = None
            self._bg_cache_key = None
        self.update()

    def resizeEvent(self, event):
        self._bg_cache_image = None
        self._bg_cache_key = None
        super().resizeEvent(event)

    def _ensure_background_cache(self):
        rect = self.rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return

        key = (
            int(rect.width()),
            int(rect.height()),
            round(float(self.lat), 4),
            round(float(self.lon), 4),
            int(self.day_of_year),
        )
        if self._bg_cache_image is not None and self._bg_cache_key == key:
            return

        img = QImage(rect.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(QColor(2, 4, 10))
        p = QPainter(img)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            clip = QPainterPath()
            clip.addRoundedRect(
                0.5,
                0.5,
                max(0.0, rect.width() - 1.0),
                max(0.0, rect.height() - 1.0),
                5.0,
                5.0,
            )
            p.setClipPath(clip)
            # 1. Background Gradient (Sampled every 15 mins)
            grad = QLinearGradient(0, 0, rect.width(), 0)

            step = 0.25
            steps = int(24.0 / step)
            for i in range(steps + 1):
                h = i * step
                if h > 24.0:
                    h = 24.0
                alt = self.get_sun_alt_fast(
                    h, self.lat, self.lon, self.day_of_year
                )
                grad.setColorAt(h / 24.0, self.get_color_for_alt(alt))

            p.fillRect(rect, grad)

            # 2. Ticks & Labels (static with cache key)
            p.setPen(QColor(170, 177, 194, 150))
            font = p.font()
            font.setFamily("Consolas")
            font.setPointSize(8)
            p.setFont(font)
            for h in self.hours_text:
                x = (h / 24.0) * rect.width()
                p.drawLine(int(x), 0, int(x), 5)
                p.drawLine(int(x), rect.height(), int(x), rect.height() - 5)
                if h < 24:
                    p.drawText(int(x) + 2, rect.height() - 2, f"{h}h")

            # 3. Border
            p.setClipping(False)
            p.setPen(QPen(QColor(59, 69, 89), 1))
            p.drawRoundedRect(
                0,
                0,
                rect.width() - 1,
                rect.height() - 1,
                5,
                5,
            )
        finally:
            p.end()

        self._bg_cache_image = img
        self._bg_cache_key = key

    def get_sun_alt_fast(self, hour, lat, lon, day):
        # Quick calculation of sun altitude for the bar
        # 1. UTC Estimate

        now = datetime.now().astimezone()
        tz_offset = now.utcoffset().total_seconds() / 3600.0
        ut_hour = (hour - tz_offset) % 24.0

        # 2. Declination
        dec_deg = -23.44 * math.cos(math.radians(360 / 365 * (day + 10)))
        dec_rad = math.radians(dec_deg)
        lat_rad = math.radians(lat)

        # 3. Hour Angle
        # Solar Time = UT + Lon/15
        solar_time = ut_hour + lon / 15.0
        ha_deg = (solar_time - 12.0) * 15.0
        ha_rad = math.radians(ha_deg)

        # 4. Altitude
        sin_alt = math.sin(dec_rad) * math.sin(lat_rad) + math.cos(
            dec_rad
        ) * math.cos(lat_rad) * math.cos(ha_rad)
        return math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))

    def get_color_for_alt(self, alt):
        # Smooth interpolation for bar background
        # Night -> Astro -> Naut -> Civil -> Golden -> Day

        # Keyframes: (Alt, Color)
        # Deep Night: < -18
        # Day: > 6

        # QColor helper
        def c(r, g, b):
            return QColor(r, g, b)

        # Colors matches AstroCanvas logic but flattened
        k_night = c(2, 4, 10)
        k_astro = c(8, 12, 22)
        k_naut = c(33, 29, 52)
        c(80, 50, 30)  # Brownish dark
        k_gold = c(179, 116, 63)
        k_day = c(53, 118, 151)

        if alt < -18:
            return k_night
        if alt < -12:
            t = (alt + 18) / 6.0
            return self.lerp_color(k_night, k_astro, t)
        if alt < -6:
            t = (alt + 12) / 6.0
            return self.lerp_color(k_astro, k_naut, t)
        if alt < 0:
            t = (alt + 6) / 6.0
            return self.lerp_color(
                k_naut, k_gold, t
            )  # Civil twilight is colorful
        if alt < 6:
            t = alt / 6.0
            return self.lerp_color(k_gold, k_day, t)

        return k_day

    def lerp_color(self, c1, c2, t):
        r = c1.red() + (c2.red() - c1.red()) * t
        g = c1.green() + (c2.green() - c1.green()) * t
        b = c1.blue() + (c2.blue() - c1.blue()) * t
        return QColor(int(r), int(g), int(b))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        self._ensure_background_cache()
        if self._bg_cache_image is not None:
            painter.drawImage(0, 0, self._bg_cache_image)

        # Time Marker (dynamic)
        pos_x = (self.current_hour / 24.0) * rect.width()

        # Rustic Indicator Line
        painter.setPen(QPen(QColor(241, 205, 136), 2))
        painter.drawLine(int(pos_x), 0, int(pos_x), rect.height())

        # Label for specific time
        time_str = (
            f"{int(self.current_hour):02}:{int((self.current_hour%1)*60):02}"
        )
        painter.setPen(QColor(243, 245, 250))
        # Check bounds to keep text inside
        text_x = pos_x + 5
        if text_x + 30 > rect.width():
            text_x = pos_x - 35
        painter.drawText(int(text_x), 15, time_str)

        painter.end()

    def mousePressEvent(self, event):
        if not self._dragging:
            self._dragging = True
            self.dragStateChanged.emit(True)
        self._update_from_mouse(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._update_from_mouse(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._update_from_mouse(event)
            self._dragging = False
            self.dragStateChanged.emit(False)
        super().mouseReleaseEvent(event)

    def _update_from_mouse(self, event):
        x = max(0, min(self.width(), event.x()))
        ratio = x / self.width()
        self.current_hour = ratio * 24.0
        self.update()
        self.valueChanged.emit(self.current_hour)


# --- ASYNC LOADING WORKERS ---


