"""
horizon_overlay.py  -  Multi-band terrain/mountain renderer

Renders the real DEM horizon profile as layered silhouettes with
atmospheric perspective, inspired by the Topo Horizon POC viewer.

Separated from village_overlay.py so that terrain rendering and
village-object rendering are independent concerns.
"""

import math
import os
import random
import numpy as np

from PyQt5.QtCore import Qt, QPointF, QObject, pyqtSignal
from PyQt5.QtGui import (
    QColor, QPainter, QPainterPath, QPen, QBrush,
    QLinearGradient, QPolygonF,
)

try:
    from TerraLab.terrain.engine import load_profile, HorizonProfile
    HORIZON_ENGINE_AVAILABLE = True
except ImportError:
    HORIZON_ENGINE_AVAILABLE = False


# ─── Layer definitions ───────────────────────────────────────────

# Each layer maps to a baked profile band and has its own visual style.
# Drawn back-to-front: farthest first, nearest last.
# Colors match POC (Blue-Grey Atmospheric Gradient).

# Colors match POC (Blue-Grey Atmospheric Gradient).
# 20 Layers for high-definition depth (Organic Palette: Forest -> Haze)
LAYER_DEFS = [
    # (band_id,          parallax, night_color,             day_color)
    
    # --- Deep Horizon (100km+) [Atmospheric Blue] ---
    ("haze_220_plus",    1.02,     QColor(60, 70, 90),      QColor(170, 185, 205)),   # Haze 3
    ("haze_150_220",     1.03,     QColor(58, 68, 88),      QColor(168, 183, 203)),   # Haze 2
    ("haze_100_150",     1.04,     QColor(56, 66, 86),      QColor(165, 180, 200)),   # Haze 1

    # --- Far Range (25-100km) [Blue Haze] ---
    ("far_70_100",       1.05,     QColor(54, 64, 84),      QColor(160, 175, 195)),
    ("far_50_70",        1.06,     QColor(52, 62, 82),      QColor(155, 170, 190)),
    ("far_35_50",        1.08,     QColor(50, 60, 80),      QColor(150, 165, 185)),
    ("far_25_35",        1.10,     QColor(48, 58, 78),      QColor(145, 160, 180)),

    # --- Mid Range (5-25km) [Transition: Green-Blue] ---
    ("mid_20_25",        1.12,     QColor(44, 54, 74),      QColor(135, 150, 170)),
    ("mid_15_20",        1.14,     QColor(40, 50, 70),      QColor(125, 140, 160)),
    ("mid_10_15",        1.16,     QColor(36, 46, 66),      QColor(115, 130, 150)),
    ("mid_7_10",         1.18,     QColor(32, 42, 62),      QColor(105, 120, 135)),
    ("mid_5_7",          1.20,     QColor(28, 38, 58),      QColor(95, 110, 120)),

    # --- Near Hills (1-5km) [Forest Green/Olive] ---
    ("near_4_5",         1.22,     QColor(24, 34, 54),      QColor(90, 105, 100)),
    ("near_3_4",         1.24,     QColor(20, 30, 50),      QColor(85, 100, 90)),
    ("near_2_3",         1.26,     QColor(16, 26, 46),      QColor(80, 95, 80)),
    ("near_1.5_2",       1.28,     QColor(12, 22, 42),      QColor(75, 90, 75)),
    ("near_1_1.5",       1.30,     QColor(10, 18, 38),      QColor(70, 85, 70)),

    # --- Immediate Ground (0-1km) [Dark Forest] ---
    ("gnd_500_1k",       1.35,     QColor(8, 16, 34),       QColor(65, 80, 65)),
    ("gnd_250_500",      1.40,     QColor(6, 14, 30),       QColor(60, 75, 60)),
    ("gnd_0_250",        1.45,     QColor(5, 12, 28),       QColor(55, 70, 55)),      # Closest
]

# Ground fill (solid color below the nearest horizon line)
GROUND_NIGHT = QColor(5, 10, 25)
GROUND_DAY   = QColor(55, 70, 55)   # Matches closest band (Dark Forest Green)


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    """Linear interpolation between two QColors."""
    r = c1.red()   + (c2.red()   - c1.red())   * t
    g = c1.green() + (c2.green() - c1.green()) * t
    b = c1.blue()  + (c2.blue()  - c1.blue())  * t
    a = c1.alpha() + (c2.alpha() - c1.alpha()) * t
    return QColor(int(r), int(g), int(b), int(a))


def _calc_t_night(ut_hour: float) -> float:
    """Compute a [0..1] night factor from UTC hour (0=midnight, 12=noon)."""
    val = math.cos((ut_hour / 24.0) * 2 * math.pi)
    t = (val + 1.0) / 2.0
    t = t * t * (3.0 - 2.0 * t)   # smoothstep
    return max(0.0, min(1.0, t))


# ─── Band data wrapper ───────────────────────────────────────────

class _BandPoints:
    """Holds (az_deg, elev_deg) points for one profile band."""

    VOID_THRESHOLD = -80.0   # DEM voids are stored as ≈ -90°

    def __init__(self, profile, band_id, vert_exaggeration=5.0):
        self.points = self._build(profile, band_id, vert_exaggeration)

    # ── private ──

    def _build(self, profile, band_id, vert_exag):
        raw = profile.get_band_points(band_id)
        if not raw:
            return []

        pts = []
        for az, elev_deg in raw:
            if elev_deg < self.VOID_THRESHOLD:
                # Void — project as way below horizon, not flat (0°)
                h = -20.0
            else:
                h = elev_deg * vert_exag
            pts.append((az, h))
        return pts


# ─── Main overlay class ──────────────────────────────────────────

class HorizonOverlay(QObject):
    """
    Renders terrain silhouettes using a Hybrid Projection:
    - X: Linear mapping based on Azimuth (fixes fisheye 'squeeze')
    - Y: Vertical displacement from the Sky's horizon curve (keeps registration)
    """

    request_update = pyqtSignal()

    def __init__(self, parent=None, horizon_profile_path=None,
                 vert_exaggeration=1.0):
        super().__init__(parent)
        self.vert_exaggeration = vert_exaggeration
        self._layers = []       # list of (_BandPoints, parallax, night_col, day_col)
        self._loaded = False

        if not HORIZON_ENGINE_AVAILABLE:
             print("[HorizonOverlay] Horizon Engine NOT available (ImportError).")

        if horizon_profile_path and HORIZON_ENGINE_AVAILABLE:
            print(f"[HorizonOverlay] Attempting to load profile from: {horizon_profile_path}")
            if not os.path.exists(horizon_profile_path):
                print(f"[HorizonOverlay] ERROR: Profile file not found at {horizon_profile_path}")
            
            try:
                profile = load_profile(horizon_profile_path)
                if profile is not None:
                    print(f"[HorizonOverlay] Profile loaded. Processing layers...")
                    for band_id, parallax, night_c, day_c in LAYER_DEFS:
                        bp = _BandPoints(profile, band_id, vert_exaggeration)
                        if bp.points:
                            self._layers.append((bp, parallax, night_c, day_c))
                        else:
                            print(f"[HorizonOverlay]   Band '{band_id}': no data, skipped")
                    self._loaded = bool(self._layers)
                else:
                    print(f"[HorizonOverlay] load_profile returned None.")
            except Exception as e:
                print(f"[HorizonOverlay] Exception loading profile: {e}")

            print("[HorizonOverlay] No real data loaded — activating procedural fallback.")
            self._build_procedural_fallback()

    # ── public API ──

    def set_profile(self, profile):
        """Update the overlay with a new HorizonProfile object (e.g. from background worker)."""
        if profile is None: return
        
        print(f"[HorizonOverlay] Updating profile for {profile.observer_lat}, {profile.observer_lon}")
        self._layers.clear()
        
        try:
            for band_id, parallax, night_c, day_c in LAYER_DEFS:
                bp = _BandPoints(profile, band_id, self.vert_exaggeration)
                if bp.points:
                    self._layers.append((bp, parallax, night_c, day_c))
            
            self._loaded = bool(self._layers)
            self.request_update.emit()
            
        except Exception as e:
            print(f"[HorizonOverlay] Error setting profile: {e}")

    def draw(self, painter: QPainter, projection_fn,
             width: int, height: int,
             current_azimuth: float, zoom_level: float,
             elevation_angle: float, ut_hour: float,
             draw_flat_line: bool = False):
        """
        Main entry: draw all terrain layers.
        If draw_flat_line is True, ignores loaded data/fallback and draws a simple straight line.
        """
        if elevation_angle > 60.0:
            return   # Looking at zenith — skip terrain

        t_night = _calc_t_night(ut_hour)

        # Flat Line Mode
        if draw_flat_line:
            color = _lerp_color(GROUND_DAY, GROUND_NIGHT, t_night)
            painter.setPen(QPen(color, 2))
            painter.setBrush(QBrush(color))
            
            # Simple horizon line based on elevation angle (pitch)
            # Center (0° elev) is at height/2 if looking straight.
            # px_per_deg approx = height / fov_vertical ? 
            # Better to use projection function if available for consistency.
            pt = projection_fn(0.0, current_azimuth)
            if pt:
                y = pt[1]
                # Fill from horizon down
                painter.drawRect(0, int(y), width, int(height - y))
            return

        # Horizontal scale  (Linear Perspective-like)
        # Using same FOV logic as village, but linear projection
        fov_deg = 100.0 / zoom_level
        px_per_deg_h = width / fov_deg
        center_x = width / 2.0

        # Vertical scale  (independent exaggeration — POC-inspired)
        vert_scale = self.vert_exaggeration * zoom_level  # Use instance exaggerated factor
        px_per_alt_deg = (height / 45.0) * vert_scale

        painter.setRenderHint(QPainter.Antialiasing)

        # --- Draw each band back-to-front ---
        # Wraps azimuth: draws [-360, 0, +360] versions of points
        for band_pts, parallax, night_c, day_c in self._layers:
            color = _lerp_color(day_c, night_c, t_night)
            self._draw_band_linear(painter, band_pts, color, projection_fn,
                                   width, height, center_x, px_per_deg_h, px_per_alt_deg,
                                   current_azimuth, parallax)

        # --- Draw ground fill (solid, below nearest horizon) ---
        if self._layers:
            ground_c = _lerp_color(GROUND_DAY, GROUND_NIGHT, t_night)
            nearest = self._layers[-1]  # last = nearest (band index -1)
            # Overlap slightly (1.0 pixel offset up) to fix blue line gaps
            # Pass nearest layer's parallax to keep ground strictly attached to near mountains
            self._draw_ground_linear(painter, nearest[0], ground_c, projection_fn,
                                     width, height, center_x, px_per_deg_h, px_per_alt_deg,
                                     current_azimuth, parallax_factor=nearest[1],
                                     overlap_px=1.0)

    # ── private rendering (Linear Horizontal) ──

    def _draw_band_linear(self, painter, band_pts, color, proj_fn,
                          w, h, cx, px_h, px_alt,
                          cur_az, parallax):
        """
        Draw one filled silhouette band using Linear X mapping.
        """
        painter.setBrush(QBrush(color))
        painter.setPen(QPen(color, 1))   # thin outline = no hairline gaps

        pts = band_pts.points
        if not pts:
            return

        strip = []
        offsets = [-360, 0, 360]
        
        for offset in offsets:
            for az, h_deg in pts:
                # Apply parallax as an offset to the logical azimuth
                layer_az = az + (parallax - 1.0) * cur_az
                final_az = layer_az + offset
                delta_az = final_az - cur_az
                
                x = cx + delta_az * px_h
                
                # Cull crude offscreen
                if x < -w or x > w * 2:
                    continue

                # Query Y anchor from Sky Projection to match curvature
                # We use 'final_az' (includes parallax) to get the local horizon Y
                anchor = proj_fn(0, final_az)
                if anchor is None:
                    continue
                
                y_base = anchor[1] + 2.0
                y_top = y_base - (h_deg * px_alt)
                
                strip.append(QPointF(x, y_top))
                
        if len(strip) > 1:
             self._fill_strip_downward(painter, strip, h * 2)


    def _draw_ground_linear(self, painter, band_pts, color, proj_fn,
                            w, h, cx, px_h, px_alt,
                            cur_az, parallax_factor, overlap_px=0.0):
        """
        Draw ground fill using the same linear projection as the nearest band.
        Fills from the horizon line (y_base) DOWN to bottom.
        """
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)

        pts = band_pts.points
        strip = []
        offsets = [-360, 0, 360]
        
        for offset in offsets:
            for az, h_deg in pts:
                layer_az = az + (parallax_factor - 1.0) * cur_az
                final_az = layer_az + offset
                delta_az = final_az - cur_az
                x = cx + delta_az * px_h
                
                if x < -w or x > w * 2:
                    continue

                anchor = proj_fn(0, final_az)
                if anchor is None:
                    continue
                
                # Ground starts at y_base (Horizon) - overlap (upward)
                # FIX: Use the actual terrain height (y_top) instead of flat horizon (y_start)
                # to prevent cutting off near terrain features with a straight line.
                
                y_base = anchor[1] + 2.0
                y_top = y_base - (h_deg * px_alt)
                
                # Apply overlap to prevent hairline gaps
                y_fill_start = y_top - overlap_px
                
                strip.append(QPointF(x, y_fill_start))
        
        if len(strip) > 1:
            self._fill_strip_downward(painter, strip, h * 2)

    @staticmethod
    def _fill_strip_downward(painter, top_points, bottom_y):
        """Draw filled polygon from top_points down to bottom_y."""
        if not top_points:
            return
            
        path = QPainterPath()
        # Sort points by X just in case? 
        # No, iterating azimuths + offsets guarantees X order (-inf to +inf).
        
        start_pt = top_points[0]
        path.moveTo(start_pt.x(), bottom_y)
        path.lineTo(start_pt)
        
        for i in range(len(top_points) - 1):
            p0 = top_points[i]
            p1 = top_points[i+1]
            
            # Check for wrap discontinuity/gap
            if abs(p1.x() - p0.x()) > 100:
                # Close current subpath down to bottom
                path.lineTo(p0.x(), bottom_y)
                path.closeSubpath()
                # Start new subpath
                path.moveTo(p1.x(), bottom_y)
                path.lineTo(p1)
                continue
            
            # Sharp, realistic mountain silhouette
            path.lineTo(p1)
            
        last = top_points[-1]
        path.lineTo(last)
        path.lineTo(last.x(), bottom_y)
        path.closeSubpath()
        
        painter.drawPath(path)

    # ── procedural fallback ──

    def _build_procedural_fallback(self):
        print("[HorizonOverlay] WARNING: Using procedural fallback (South Flat / North Mountains).")
        rng = random.Random(42)
        
        # 3 simple layers matching POC colors
        configs = [
             ("far_25_60", 1.10, QColor(38, 48, 68), QColor(140, 155, 175), 0.6, 3.0, 1.5),
             ("mid_3_10",  1.22, QColor(18, 25, 42), QColor(100, 120, 135), 0.8, 2.0, 2.0),
             ("near_0_1",  1.40, QColor(8, 12, 22),  QColor(70, 90, 100),   1.0, 5.0, 1.0),
        ]

        for bid, par, nc, dc, base, freq, amp in configs:
            pts = []
            # Generate 360 degrees
            for step in range(720): # 0.5 deg steps
                az = step * 0.5 
                # Normalize az to 0..360
                norm_az = az % 360.0
                
                # Logic: South is approx 90..270. North is 270..360 + 0..90.
                # Let's define "Flat Zone" as 110 to 250 to have some transition
                
                is_flat = False
                transition = 0.0
                
                if 135 < norm_az < 225:
                    # Pure Flat
                    val = 0.2
                else:
                    # Mountains
                    rad = math.radians(az)
                    
                    # Noise composition
                    n1 = abs(math.sin(rad * freq)) * amp
                    n2 = abs(math.sin(rad * freq * 2.3)) * (amp * 0.5)
                    n3 = abs(math.sin(rad * freq * 5.1)) * (amp * 0.25)
                    
                    val = base + (n1 + n2 + n3) * rng.uniform(0.9, 1.1)
                    
                    # Smooth transition to flat zone?
                    # Simple lerp if near boundaries (90..135 and 225..270)
                    if 90 < norm_az <= 135:
                        t = (norm_az - 90) / 45.0 # 0..1
                        # 1=Flat, 0=Mount
                        val = val * (1.0 - t) + 0.2 * t
                    elif 225 <= norm_az < 270:
                        t = (norm_az - 225) / 45.0 # 0..1
                        # 0=Flat, 1=Mount
                        val = 0.2 * (1.0 - t) + val * t

                pts.append((az, max(0.2, val)))
            
            bp = _BandPoints.__new__(_BandPoints)
            bp.points = pts
            self._layers.append((bp, par, nc, dc))
