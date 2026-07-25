"""Sun, Moon, planet, and eclipse rendering adapters."""

from __future__ import annotations

import math

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QPen

from TerraLab.render.overlays_renderer import (
    draw_moon_skyfield as render_draw_moon_skyfield,
    draw_planet as render_draw_planet,
    get_eclipse_dimming_factor as render_get_eclipse_dimming_factor,
)


class CanvasEphemerisRenderingMixin:
    def draw_moon_skyfield(
        self,
        painter,
        alt,
        az,
        illum,
        rotation_deg,
        radius,
        alpha,
        is_eclipsing=False,
        is_day=False,
        sun_params=None,
        pixels_per_deg=None,
        tint_color=None,
    ):
        return render_draw_moon_skyfield(
            self,
            painter,
            alt,
            az,
            illum,
            rotation_deg,
            radius,
            alpha,
            is_eclipsing=is_eclipsing,
            is_day=is_day,
            sun_params=sun_params,
            pixels_per_deg=pixels_per_deg,
            tint_color=tint_color,
        )

    def draw_planet(self, painter, alt, az, name, col, sz, mag, key=None):
        return render_draw_planet(
            self,
            painter,
            alt,
            az,
            name,
            col,
            sz,
            mag,
            key=key,
        )

    def draw_satellite(self, painter, alt, az, name, mag):
        pt = self.project_universal_stereo(alt, az)
        if not pt:
            return
        x, y = pt
        painter.setPen(QPen(Qt.red, 2))
        painter.drawPoint(QPointF(x, y))
        painter.setPen(Qt.white)
        painter.drawText(int(x) + 5, int(y) - 5, f"{name} {mag:.1f}")

    def calculate_planet_magnitude(self, name, d_au, phase):
        # Revised Base Magnitudes (Normalized to ~1 AU distance + Albedo)
        # Formula uses +5*log10(d), so Base must handle the subtraction of distance modulus.
        # e.g. Saturn at 9AU: 5*log(9) = +4.77. Target Mag ~0.5. Base should be -4.3.
        base = {
            "Mercury": -0.6,
            "Venus": -4.4,
            "Mars": -0.5,  # Adjusted (was -2.0)
            "Jupiter": -5.8,  # Adjusted (was -2.7)
            "Saturn": -4.3,  # Adjusted (was 0.5)
            "Uranus": -0.7,
            "Neptune": 0.5,
            "Pluto": 6.0,
        }.get(name, 0)
        return base + 5 * math.log10(d_au) + 0.01 * phase

    def get_eclipse_dimming_factor(self, ut_hour, day_of_year):
        return render_get_eclipse_dimming_factor(self, ut_hour, day_of_year)
