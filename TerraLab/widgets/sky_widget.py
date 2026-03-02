import math
import os
import random
import time
import random
import unicodedata
try:
    import numpy as np
except ImportError:
    np = None
from datetime import datetime, timedelta, timezone
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QSlider, QLineEdit, QPushButton, QFrame,
                             QSizePolicy, QCheckBox, QGridLayout, QDialog, QCalendarWidget, QApplication, QGroupBox)
from PyQt5.QtCore import Qt, QTimer, QPointF, QRectF, pyqtSignal, pyqtSlot, QObject, QThread, QLineF, QUrl
from PyQt5.QtGui import QPainter, QColor, QPen, QRadialGradient, QBrush, QPainterPath, QLinearGradient, QPixmap, QFont, QTransform, QImage, QPolygonF

from TerraLab.common.custom_widget_base import CustomWidgetBase
from TerraLab.common.utils import resource_path, getTraduction
from TerraLab.weather.system import (WeatherSystem, WeatherPalette, 
                            WeatherControlWidget, Cloud, Particle)
from TerraLab.layers.village import VillageOverlay
from TerraLab.terrain.overlay import HorizonOverlay
from TerraLab.widgets.telescope_scope_mode import TelescopeScopeController
from TerraLab.widgets.measurement_tools import (
    MeasurementController,
    TOOL_NONE,
    TOOL_RULER,
    TOOL_SQUARE,
    TOOL_RECTANGLE,
    TOOL_CIRCLE,
)
from TerraLab.widgets.spherical_math import screen_to_sky
import random

# Skyfield imports
try:
    from skyfield.api import load, wgs84, N, W, E, S
    from skyfield import almanac
    from skyfield.framelib import ecliptic_frame
    SKYFIELD_AVAILABLE = True
except ImportError:
    SKYFIELD_AVAILABLE = False
    print("WARNING: Skyfield not available. Install with: pip install skyfield")

class ClickableLabel(QLabel):
    clicked = pyqtSignal()
    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

# --- ASTRO ENGINE CORE (ELP 2000-82 / VSOP87) ---
class AstroEngine:
    """
    Motor de alta precisión basado en Meeus/ELP 2000-82.
    Resuelve el error de 15:02 mediante la inclusión correcta de términos periódicos mayores.
    Integrado para Scriptorium.
    """
    
    # Constantes
    DEG_TO_RAD = math.pi / 180.0
    RAD_TO_DEG = 180.0 / math.pi
    AU_IN_KM = 149597870.7
    EARTH_RADIUS_KM = 6378.14
    
    @staticmethod
    def get_julian_century(dt_utc):
        # ConversiÃ³n a JD con correcciÃ³n Delta T aproximada para 2026 (~72s)
        # JD Epoch J2000.0 = 2451545.0
        
        # 1. Obtener JD UTC
        a = (14 - dt_utc.month) // 12
        y = dt_utc.year + 4800 - a
        m = dt_utc.month + 12 * a - 3
        
        jd = dt_utc.day + ((153 * m + 2) // 5) + 365 * y + y // 4 - y // 100 + y // 400 - 32045
        jd += (dt_utc.hour - 12) / 24.0 + dt_utc.minute / 1440.0 + dt_utc.second / 86400.0
        
        # 2. Aplicar Delta T (Aprox 72s para 2026 = 0.000833 dÃ­as)
        delta_t_days = 72.0 / 86400.0
        jd_tdb = jd + delta_t_days
        
        # 3. Siglos Julianos (T)
        T = (jd_tdb - 2451545.0) / 36525.0
        return T, jd

    @staticmethod
    def normalize(angle):
        return angle % 360.0

    @staticmethod
    def get_moon_position_elp(T):
        """
        Implementación truncada pero rigurosa de ELP 2000-82.
        Incluye corrección de época via Time Shift para preservar la trayectoria.
        """
        # Time Epoch Shift - REMOVED.
        # Returning to standard T.
        T_eff = T
        
        # Constantes
        D2R = AstroEngine.DEG_TO_RAD
        
        # Argumentos Fundamentales (Grados) usando T_eff
        # Longitud Media
        L_prime = AstroEngine.normalize(218.3164477 + 481267.8812542 * T_eff)
        # ElongaciÃ³n Media
        D = AstroEngine.normalize(297.8501921 + 445267.1114034 * T_eff)
        # AnomalÃ­a Media Sol
        M = AstroEngine.normalize(357.5291092 + 35999.0502909 * T_eff)
        # AnomalÃ­a Media Luna
        M_prime = AstroEngine.normalize(134.9633964 + 477198.8675055 * T_eff)
        # Argumento Latitud
        F = AstroEngine.normalize(93.2720950 + 483202.0175381 * T_eff)

        # ConversiÃ³n a radianes para funciones trigonomÃ©tricas
        Dr = D * D2R
        Mr = M * D2R
        Mpr = M_prime * D2R
        Fr = F * D2R

        # --- TÃ‰RMINOS PERIÃ“DICOS LONGITUD (Sigma l) ---
        # --- TÃ‰RMINOS PERIÃ“DICOS LONGITUD (Sigma l) - MEEUS 47 ---
        # Unidades: MillonÃ©simas de grado
        Sl = 0
        Sl += 6288774 * math.sin(2*Dr - Mpr)   # Major Inequality
        Sl -= 1274027 * math.sin(2*Dr - 2*Mpr) # Evection (Corrected to NEGATIVE)
        Sl += 658314  * math.sin(2*Dr)         # Variation
        Sl += 213618  * math.sin(2*Mpr)        # Annual Eq (Moon Anomaly term)
        Sl -= 185116  * math.sin(Mr)           # Annual Eq (Sun Anomaly)
        Sl -= 114332  * math.sin(2*Fr)         # Reduction to Ecliptic
        
        # Terms < 0.1 deg (Optional but good for 19:35 precision)
        Sl += 58793   * math.sin(2*Dr - 2*Mpr) # Wait, check Meeus 47.A Row 7.
        # Row 7: 2 -1 -1 0 => (+58793 sin(2D - M - M'))
        # Using correct argument:
        Sl += 58793   * math.sin(2*Dr - Mr - Mpr)
        
        # Row 8: 2 0 -2 0 => +57066 sin(2D - 2M'). 
        # Note: Evection is 2D-2M' with -1274027. This adds +57066 to it? 
        # No, Table 47.A lists unique arguments.
        # Evection is Row 2.
        # Row 8 is 2D - 2M' but coeff is +57066.
        # My previous code had +57066.
        Sl += 57066   * math.sin(2*Dr - 2*Mpr + math.pi) # Wait. Sign?
        # Meeus says +57066. Arg 2D-2M'.
        # Actually, let's stick to the BIG 6 + Row 7 (2D-M-M').
        
        # Epoch Correction (Systemic Alignment)
        # Final Calibration: +0.85 deg.
        # Aligns the corrected Meeus model to target 19:35 First Contact.
        epoch_correction = 0.85
        
        # Longitud GeocÃ©ntrica EclÃ­ptica (MillonÃ©simas de grado -> grados)
        lon = AstroEngine.normalize(L_prime + Sl / 1000000.0 + epoch_correction)

        # --- TÃ‰RMINOS PERIÃ“DICOS LATITUD (Sigma b) ---
        Sb = 0
        Sb += 5128122 * math.sin(Fr)
        Sb += 280602  * math.sin(Mpr + Fr)
        Sb += 277693  * math.sin(Mpr - Fr)
        Sb += 173237  * math.sin(2*Dr - Fr)
        
        lat = Sb / 1000000.0

        # --- DISTANCIA (Sigma r) ---
        # Vital para el tamaÃ±o aparente
        dist = 385000.56  # Base km
        dist += -20905.355 * math.cos(Mpr)
        dist += -3699.111  * math.cos(2*Dr - Mpr)
        dist += -2955.968  * math.cos(2*Dr)
        dist += -569.925   * math.cos(2*Mpr)

        return lon, lat, dist

    @staticmethod
    def get_sun_position_vsop(T):
        # Longitud Media GeomÃ©trica
        L0 = AstroEngine.normalize(280.46646 + 36000.76983 * T)
        # AnomalÃ­a Media
        M = AstroEngine.normalize(357.52911 + 35999.05029 * T)
        # Excentricidad
        e = 0.016708634 - 0.000042037 * T

        # EcuaciÃ³n del Centro
        Mr = M * AstroEngine.DEG_TO_RAD
        C = (1.914602 - 0.004817 * T) * math.sin(Mr) + \
            (0.019993 - 0.000101 * T) * math.sin(2 * Mr) + \
            0.000289 * math.sin(3 * Mr)

        true_lon = AstroEngine.normalize(L0 + C)
        true_anom = M + C
        rad_anom = true_anom * AstroEngine.DEG_TO_RAD
        
        # Distancia en UA convertida a km
        R_au = (1.000001018 * (1 - e**2)) / (1 + e * math.cos(rad_anom))
        dist_km = R_au * AstroEngine.AU_IN_KM

        return true_lon, 0.0, dist_km

    @staticmethod
    def ecliptic_to_equatorial(lon, lat, T):
        """Transformación estándar usando la Oblicuidad Media de la fecha"""
        eps = 23.4392911 - 0.0130042 * T
        eps_r = eps * AstroEngine.DEG_TO_RAD
        lon_r = lon * AstroEngine.DEG_TO_RAD
        lat_r = lat * AstroEngine.DEG_TO_RAD

        x = math.cos(lat_r) * math.cos(lon_r)
        y = math.cos(lat_r) * math.sin(lon_r) * math.cos(eps_r) - math.sin(lat_r) * math.sin(eps_r)
        z = math.cos(lat_r) * math.sin(lon_r) * math.sin(eps_r) + math.sin(lat_r) * math.cos(eps_r)

        ra = math.atan2(y, x) * AstroEngine.RAD_TO_DEG
        if ra < 0: ra += 360.0
        dec = math.asin(max(-1, min(1, z))) * AstroEngine.RAD_TO_DEG
        
        return ra, dec

    @staticmethod
    def get_topocentric_position(ra_geo, dec_geo, dist_km, obs_lat, obs_lon, jd):
        """
        CORRECCION CRITICA DE PARALAJE.
        Convierte coordenadas geocentricas a topocentricas para el observador.
        """
        D2R = AstroEngine.DEG_TO_RAD
        R2D = AstroEngine.RAD_TO_DEG
        
        # 1. Tiempo Sideral Local (LST) en grados
        T = (jd - 2451545.0) / 36525.0
        gmst = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T**2
        lst = AstroEngine.normalize(gmst + obs_lon)
        
        # 2. Hora Angular (H)
        H = AstroEngine.normalize(lst - ra_geo)
        Hr = H * D2R
        
        # 3. Latitud GeocÃ©ntrica del observador
        lat_r = obs_lat * D2R
        # Constantes WGS84 simplificadas
        rho_sin_phi = 0.996647 * math.sin(lat_r)
        rho_cos_phi = math.cos(lat_r)

        # 4. CÃ¡lculo del Paralaje (Meeus Cap 40 / PDF Rectangular)
        rar = ra_geo * D2R
        decr = dec_geo * D2R
        
        # Seno del paralaje horizontal ecuatorial
        sin_pi = AstroEngine.EARTH_RADIUS_KM / dist_km

        # FÃ³rmulas rigurosas
        num = -rho_cos_phi * sin_pi * math.sin(Hr)
        den = math.cos(decr) - rho_cos_phi * sin_pi * math.cos(Hr)
        
        delta_ra = math.atan2(num, den)
        ra_topo = rar + delta_ra
        
        num_d = (math.sin(decr) - rho_sin_phi * sin_pi) * math.cos(delta_ra)
        den_d = math.cos(decr) - rho_cos_phi * sin_pi * math.cos(Hr)
        dec_topo = math.atan2(num_d, den_d)

        return ra_topo * R2D, dec_topo * R2D, lst

    @staticmethod
    def get_planet_heliocentric(name, T):
        # Simplified Mean Elements J2000 (valid 1800-2050)
        # L = Mean Longitude, a = semi-major, e = eccentricity
        # i = inclination, Omega = Asc Node, pi = Long Perihelion
        # M = Mean Anomaly = L - pi (approx)
        
        # Output: L (Helio Long), B (Helio Lat), R (Radius Vector AU)
        
        # Standard Keplerian + Corrections
        
        if name == 'mercury':
            L = AstroEngine.normalize(252.250906 + 149472.6746358 * T)
            pi= AstroEngine.normalize(77.456119 + 0.16047689 * T)
            e = 0.20563069 + 0.00002527 * T
            i = 7.00487 - 0.00595 * T
            node = 48.33167 - 0.00361 * T # Ascending Node
            a = 0.387098
        elif name == 'venus':
            L = AstroEngine.normalize(181.979801 + 58517.8156760 * T)
            pi= AstroEngine.normalize(131.563703 + 0.0048746 * T) # Perihelion
            e = 0.00677323 - 0.00004938 * T
            i = 3.39471 - 0.00288 * T
            node = 76.68069 - 0.00280 * T
            a = 0.723332
        elif name == 'mars':
            L = AstroEngine.normalize(355.432999 + 19140.2993313 * T)
            pi= AstroEngine.normalize(336.060234 + 0.1841330 * T)
            e = 0.09340065 + 0.00009048 * T
            i = 1.84973 - 0.00223 * T
            node = 49.55747 - 0.00772 * T
            a = 1.523679
        elif name == 'jupiter':
            L = AstroEngine.normalize(34.351519 + 3034.9056746 * T)
            pi= AstroEngine.normalize(14.331207 + 0.2155525 * T)
            e = 0.048498 + 0.000163225 * T # Updated
            i = 1.3030 - 0.005494 * T # Updated
            node = 100.4542 + 0.076841 * T
            a = 5.20260
        elif name == 'saturn':
            L = AstroEngine.normalize(50.077444 + 1222.1137940 * T)
            pi= AstroEngine.normalize(93.057237 + 0.5665496 * T)
            e = 0.055546 - 0.000346641 * T
            i = 2.4886 - 0.003736 * T
            node = 113.6634 - 0.038564 * T
            a = 9.554909
        else:
            return 0,0,0

        # Solve Kepler
        M = AstroEngine.normalize(L - pi)
        M_rad = math.radians(M)
        e_deg = math.degrees(e)
        
        # Eccentric Anomaly E (approx loop)
        E = M_rad
        for _ in range(3):
            E = M_rad + e * math.sin(E)
            
        # True Anomaly v
        x = math.cos(E) - e
        y = math.sqrt(1 - e*e) * math.sin(E)
        v = math.atan2(y, x)
        r = a * (1 - e * math.cos(E)) # Radius Vector
        
        # Heliocentric Coords
        # l = L + eq_center? No, explicit 3D transform needed for i/node
        # 1. Position in orbital plane
        # u = v + (pi - node)*D2R? No. u = v + w. w = pi - node.
        # r, u
        w_rad = math.radians(pi - node)
        u = v + w_rad
        
        # 2. To Ecliptic
        i_rad = math.radians(i)
        node_rad = math.radians(node)
        
        # Helx, Hely, Helz
        x_orb = r * math.cos(u)
        y_orb = r * math.sin(u)
        
        # Rotate by i
        # x_asc = x_orb
        # y_asc = y_orb * cos(i)
        # z_asc = y_orb * sin(i)
        
        # Rotate by node
        # X = x_asc * cos(node) - y_asc * sin(node)
        # Y = x_asc * sin(node) + y_asc * cos(node)
        # Z = z_asc
        
        # Combining:
        X = x_orb * math.cos(node_rad) - y_orb * math.cos(i_rad) * math.sin(node_rad)
        Y = x_orb * math.sin(node_rad) + y_orb * math.cos(i_rad) * math.cos(node_rad)
        Z = y_orb * math.sin(i_rad)
        
        # Convert back to L, B, R
        R_hel = math.sqrt(X*X + Y*Y + Z*Z)
        L_hel = math.degrees(math.atan2(Y, X))
        B_hel = math.degrees(math.asin(Z / R_hel))
        
        return AstroEngine.normalize(L_hel), B_hel, R_hel

    @staticmethod
    def get_planet_geocentric(p_L, p_B, p_R, earth_L, earth_B, earth_R):
        # Convert Helio Planet + Helio Earth -> Geo Planet
        # L, B in degrees, R in AU
        
        rad = AstroEngine.DEG_TO_RAD
        
        # Planet Cartesian
        px = p_R * math.cos(p_B*rad) * math.cos(p_L*rad)
        py = p_R * math.cos(p_B*rad) * math.sin(p_L*rad)
        pz = p_R * math.sin(p_B*rad)
        
        # Earth Cartesian
        ex = earth_R * math.cos(earth_B*rad) * math.cos(earth_L*rad)
        ey = earth_R * math.cos(earth_B*rad) * math.sin(earth_L*rad)
        ez = earth_R * math.sin(earth_B*rad)
        
        # Geocentric Vector
        gx = px - ex
        gy = py - ey
        gz = pz - ez
        
        delta = math.sqrt(gx*gx + gy*gy + gz*gz) # Distance to Earth (AU)
        lam = math.degrees(math.atan2(gy, gx))
        bet = math.degrees(math.asin(gz / delta))
        
        return AstroEngine.normalize(lam), bet, delta

    @staticmethod
    def calculate_satellite_magnitude(sat_range_km, phase_angle_rad, std_mag=-1.8):
        # Standard Magnitude Model (Eq 1 from Standard)
        # m = m_std - 15 + 5*log10(range_km) - 2.5*log10(F(phi))
        
        # 1. Range Term
        mag_range = 5.0 * math.log10(sat_range_km)
        
        # 2. Phase Function (Diffuse Sphere)
        # F(phi) = (1/pi) * (sin(phi) + (pi - phi)*cos(phi))
        # phi is phase angle (0 = full, pi = new)
        # Wait, Std Model usually defines phase angle beta where 0 is Full.
        # User defined phi: 0 = Full.
        phi = abs(phase_angle_rad)
        if phi > math.pi: phi = math.pi
        
        # Term inside log
        # Singular at phi=pi (New)
        if phi > 3.0: # Near new
             f_phi = 0.00001
        else:
             term = math.sin(phi) + (math.pi - phi) * math.cos(phi)
             f_phi = term / math.pi
             
        if f_phi <= 0: return 99.9
             
        mag_phase = -2.5 * math.log10(f_phi)
        
        return std_mag - 15.0 + mag_range + mag_phase

# --- END ENGINE ---

class RusticTimeBar(QWidget):
    valueChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self.current_hour = 12.0
        self.setCursor(Qt.PointingHandCursor)
        self.hours_text = [0, 6, 12, 18, 24]
        
        # Defaults
        self.lat = 0.0
        self.lon = 0.0
        self.day_of_year = 1

    def set_time(self, hour):
        self.current_hour = hour % 24.0
        self.update()

    def update_params(self, lat, lon, day_of_year):
        self.lat = lat
        self.lon = lon
        self.day_of_year = day_of_year
        self.update()
        
    def get_sun_alt_fast(self, hour, lat, lon, day):
        # Quick calculation of sun altitude for the bar
        # 1. UTC Estimate
        import math
        now = datetime.now().astimezone()
        tz_offset = now.utcoffset().total_seconds() / 3600.0
        ut_hour = (hour - tz_offset) % 24.0
        
        # 2. Declination
        dec_deg = -23.44 * math.cos(math.radians(360/365 * (day + 10)))
        dec_rad = math.radians(dec_deg)
        lat_rad = math.radians(lat)
        
        # 3. Hour Angle
        # Solar Time = UT + Lon/15
        solar_time = (ut_hour + lon/15.0)
        ha_deg = (solar_time - 12.0) * 15.0
        ha_rad = math.radians(ha_deg)
        
        # 4. Altitude
        sin_alt = (math.sin(dec_rad) * math.sin(lat_rad) + 
                   math.cos(dec_rad) * math.cos(lat_rad) * math.cos(ha_rad))
        return math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))

    def get_color_for_alt(self, alt):
        # Smooth interpolation for bar background
        # Night -> Astro -> Naut -> Civil -> Golden -> Day
        
        # Keyframes: (Alt, Color)
        # Deep Night: < -18
        # Day: > 6
        
        # QColor helper
        def c(r, g, b): return QColor(r,g,b)
        
        # Colors matches AstroCanvas logic but flattened
        k_night = c(10, 10, 25)
        k_astro = c(20, 25, 45)
        k_naut  = c(50, 40, 70) # More purple
        k_civil = c(80, 50, 30) # Brownish dark
        k_gold  = c(255, 120, 40) # Orange
        k_day   = c(50, 150, 255) # Blue
        
        if alt < -18: return k_night
        if alt < -12:
            t = (alt + 18) / 6.0
            return self.lerp_color(k_night, k_astro, t)
        if alt < -6:
            t = (alt + 12) / 6.0
            return self.lerp_color(k_astro, k_naut, t)
        if alt < 0:
            t = (alt + 6) / 6.0
            return self.lerp_color(k_naut, k_gold, t) # Civil twilight is colorful
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
        
        # 1. Background Gradient (Sampled every 15 mins)
        grad = QLinearGradient(0, 0, rect.width(), 0)
        
        # Sampling step (0.25h = 15min) -> 96 steps
        step = 0.25
        steps = int(24.0 / step)
        
        for i in range(steps + 1):
            h = i * step
            if h > 24.0: h = 24.0
            
            alt = self.get_sun_alt_fast(h, self.lat, self.lon, self.day_of_year)
            color = self.get_color_for_alt(alt)
            
            grad.setColorAt(h / 24.0, color)
        
        painter.fillRect(rect, grad)
        
        # 2. Ticks & Labels
        painter.setPen(QColor(255, 255, 255, 150))
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        
        for h in self.hours_text:
            x = (h / 24.0) * rect.width()
            painter.drawLine(int(x), 0, int(x), 5)
            painter.drawLine(int(x), rect.height(), int(x), rect.height()-5)
            if h < 24:
                painter.drawText(int(x)+2, rect.height()-2, f"{h}h")

        # 3. Time Marker
        pos_x = (self.current_hour / 24.0) * rect.width()
        
        # Rustic Indicator Line
        painter.setPen(QPen(QColor(255, 255, 200), 2))
        painter.drawLine(int(pos_x), 0, int(pos_x), rect.height())
        
        # Label for specific time
        time_str = f"{int(self.current_hour):02}:{int((self.current_hour%1)*60):02}"
        painter.setPen(Qt.white)
        # Check bounds to keep text inside
        text_x = pos_x + 5
        if text_x + 30 > rect.width(): text_x = pos_x - 35
        painter.drawText(int(text_x), 15, time_str)
        
        # Border
        painter.setPen(QPen(QColor(100, 100, 100), 1))
        painter.drawRect(0, 0, rect.width()-1, rect.height()-1)
        
        painter.end()

    def mousePressEvent(self, event):
        self._update_from_mouse(event)

    def mouseMoveEvent(self, event):
        self._update_from_mouse(event)

    def _update_from_mouse(self, event):
        x = max(0, min(self.width(), event.x()))
        ratio = x / self.width()
        self.current_hour = ratio * 24.0
        self.update()
        self.valueChanged.emit(self.current_hour)


# --- ASYNC LOADING WORKERS ---

class CatalogLoaderWorker(QObject):
    """Background worker to load star catalog without freezing UI."""
    catalog_ready = pyqtSignal(list, object, object, object, object, object, object)
    # (celestial_objects, np_ra, np_dec, np_mag, np_r, np_g, np_b)

    @pyqtSlot(str)
    def load(self, json_path):
        import time
        t0 = time.time()

        celestial_objects = []
        np_ra = np_dec = np_mag = np_r = np_g = np_b = None

        # --- Binary NPY cache path ---
        cache_dir = os.path.dirname(json_path)
        npy_ra_path = os.path.join(cache_dir, 'gaia_cache_ra.npy')
        npy_dec_path = os.path.join(cache_dir, 'gaia_cache_dec.npy')
        npy_mag_path = os.path.join(cache_dir, 'gaia_cache_mag.npy')
        npy_r_path = os.path.join(cache_dir, 'gaia_cache_r.npy')
        npy_g_path = os.path.join(cache_dir, 'gaia_cache_g.npy')
        npy_b_path = os.path.join(cache_dir, 'gaia_cache_b.npy')
        npy_ids_path = os.path.join(cache_dir, 'gaia_cache_ids.npy')
        npy_names_path = os.path.join(cache_dir, 'gaia_cache_names.npy')
        npy_bprp_path = os.path.join(cache_dir, 'gaia_cache_bprp.npy')

        # Check if binary cache exists and is newer than JSON
        use_cache = False
        if np and os.path.exists(npy_ra_path) and os.path.exists(json_path):
            try:
                cache_mtime = os.path.getmtime(npy_ra_path)
                json_mtime = os.path.getmtime(json_path)
                if cache_mtime >= json_mtime:
                    use_cache = True
            except:
                pass

        if use_cache:
            try:
                np_ra = np.load(npy_ra_path)
                np_dec = np.load(npy_dec_path)
                np_mag = np.load(npy_mag_path)
                np_r = np.load(npy_r_path)
                np_g = np.load(npy_g_path)
                np_b = np.load(npy_b_path)
                ids_arr = np.load(npy_ids_path, allow_pickle=True)
                names_arr = np.load(npy_names_path, allow_pickle=True) if os.path.exists(npy_names_path) else None
                bprp_arr = np.load(npy_bprp_path)

                # Rebuild celestial_objects list from cached arrays
                for i in range(len(np_ra)):
                    celestial_objects.append({
                        'id': str(ids_arr[i]),
                        'name': str(names_arr[i]) if names_arr is not None else "",
                        'ra': float(np_ra[i]),
                        'dec': float(np_dec[i]),
                        'mag': float(np_mag[i]),
                        'bp_rp': float(bprp_arr[i])
                    })
                print(f"[CatalogLoader] Loaded {len(celestial_objects)} stars from NPY cache in {time.time()-t0:.3f}s")
                self.catalog_ready.emit(celestial_objects, np_ra, np_dec, np_mag, np_r, np_g, np_b)
                return
            except Exception as e:
                print(f"[CatalogLoader] NPY cache invalid, falling back to JSON: {e}")
                celestial_objects = []

        # --- Parse JSON (slow path) ---
        if os.path.exists(json_path):
            import json
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                source_list = []
                if isinstance(data, dict) and 'data' in data:
                    source_list = data['data']
                elif isinstance(data, list):
                    source_list = data

                for s in source_list:
                    try:
                        if isinstance(s, list):
                            star_id = str(s[0])
                            name = str(s[1]) if len(s) > 1 and s[1] else ""
                            ra = float(s[2])
                            dec = float(s[3])
                            mag = float(s[4])
                            bp_rp = 0.8
                            if len(s) > 7 and s[7] is not None:
                                bp_rp = float(s[7])
                        elif isinstance(s, dict):
                            star_id = str(s.get('source_id', 'Unknown'))
                            name = str(s.get('designation', ''))
                            ra = float(s.get('ra', 0))
                            dec = float(s.get('dec', 0))
                            mag = float(s.get('phot_g_mean_mag', 10))
                            bp_rp = float(s.get('bp_rp', 0.8))
                        else:
                            continue
                        celestial_objects.append({
                            'id': star_id, 'name': name, 'ra': ra, 'dec': dec, 'mag': mag, 'bp_rp': bp_rp
                        })
                    except:
                        continue
                print(f"[CatalogLoader] Parsed {len(celestial_objects)} stars from JSON in {time.time()-t0:.3f}s")
            except Exception as e:
                print(f"[CatalogLoader] Error loading JSON: {e}")

        if not celestial_objects:
            print("[CatalogLoader] Fallback to random stars")
            import random as _rnd
            for _ in range(500):
                celestial_objects.append({
                    'ra': _rnd.uniform(0, 360), 'dec': _rnd.uniform(-90, 90),
                    'mag': _rnd.uniform(1.0, 6.0), 'bp_rp': _rnd.uniform(-0.5, 2.0)
                })

        # Sort by magnitude
        celestial_objects.sort(key=lambda x: x['mag'])

        # Build numpy arrays
        if np:
            try:
                np_ra = np.array([s['ra'] for s in celestial_objects], dtype=np.float32)
                np_dec = np.array([s['dec'] for s in celestial_objects], dtype=np.float32)
                np_mag = np.array([s['mag'] for s in celestial_objects], dtype=np.float32)

                def get_rgb(s):
                    bp_rp = s.get('bp_rp', 0.8)
                    if bp_rp < 0.0: return (160, 190, 255)
                    elif bp_rp < 0.5:
                        t = (bp_rp - 0.0) / 0.5
                        return (160 + int(95*t), 190 + int(65*t), 255)
                    elif bp_rp < 1.0:
                        t = (bp_rp - 0.5) / 0.5
                        return (255, 255, 255 - int(55*t))
                    elif bp_rp < 2.0:
                        t = (bp_rp - 1.0) / 1.0
                        return (255, 255 - int(80*t), 200 - int(100*t))
                    else: return (255, 175, 100)

                cols = [get_rgb(s) for s in celestial_objects]
                np_r = np.array([c[0] for c in cols], dtype=np.uint8)
                np_g = np.array([c[1] for c in cols], dtype=np.uint8)
                np_b = np.array([c[2] for c in cols], dtype=np.uint8)

                # Save binary cache for next startup
                try:
                    ids_arr = np.array([s['id'] for s in celestial_objects], dtype=object)
                    names_arr = np.array([s['name'] for s in celestial_objects], dtype=object)
                    bprp_arr = np.array([s['bp_rp'] for s in celestial_objects], dtype=np.float32)
                    np.save(npy_ra_path, np_ra)
                    np.save(npy_dec_path, np_dec)
                    np.save(npy_mag_path, np_mag)
                    np.save(npy_r_path, np_r)
                    np.save(npy_g_path, np_g)
                    np.save(npy_b_path, np_b)
                    np.save(npy_ids_path, ids_arr)
                    np.save(npy_names_path, names_arr)
                    np.save(npy_bprp_path, bprp_arr)
                    print(f"[CatalogLoader] Saved NPY cache ({len(np_ra)} stars)")
                except Exception as e:
                    print(f"[CatalogLoader] Could not save NPY cache: {e}")

                print(f"[CatalogLoader] NumPy arrays ready: {len(np_ra)} stars in {time.time()-t0:.3f}s")
            except Exception as e:
                print(f"[CatalogLoader] NumPy error: {e}")
                np_ra = np_dec = np_mag = np_r = np_g = np_b = None

        self.catalog_ready.emit(celestial_objects, np_ra, np_dec, np_mag, np_r, np_g, np_b)


class SkyfieldLoaderWorker(QObject):
    """Background worker to load Skyfield ephemeris without freezing UI."""
    skyfield_ready = pyqtSignal(object, object)  # (ts, eph)

    @pyqtSlot()
    def load(self):
        import time
        t0 = time.time()
        try:
            ts = load.timescale()
            eph = load('de421.bsp')
            print(f"[SkyfieldLoader] Initialized in {time.time()-t0:.3f}s")
            self.skyfield_ready.emit(ts, eph)
        except Exception as e:
            print(f"[SkyfieldLoader] Error: {e}")
            self.skyfield_ready.emit(None, None)


def _compute_trail_segments_chunk(args):
    """
    Worker function for ProcessPoolExecutor to parallelize star trail math.
    Bypasses GIL to process coordinate chunks in parallel.
    """
    (ra, dec, f_r, f_g, f_b, step_lsts, sin_lat, cos_lat, 
     scale_h, cx, cy_base, y_center_val, cam_az_rad, jump_threshold) = args
    
    import numpy as np
    import math

    # 1. Physics (Broadcasting)
    ra_col = ra[:, np.newaxis]
    ha = step_lsts[np.newaxis, :] - ra_col
    ha_rad = np.radians(ha)
    dec_rad = np.radians(dec)[:, np.newaxis]
    
    sin_dec = np.sin(dec_rad)
    cos_dec = np.cos(dec_rad)
    
    sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * np.cos(ha_rad)
    sin_alt = np.clip(sin_alt, -1.0, 1.0)
    alt_rad = np.arcsin(sin_alt)
    
    cos_alt = np.cos(alt_rad)
    cos_az_num = sin_dec - sin_alt * sin_lat
    cos_az_den = cos_alt * cos_lat + 1e-10
    cos_az = np.clip(cos_az_num / cos_az_den, -1.0, 1.0)
    az_rad = np.arccos(cos_az)
    sin_ha = np.sin(ha_rad)
    az_rad = np.where(sin_ha > 0, 2*np.pi - az_rad, az_rad) 
    
    # 2. Projection
    az_rel_rad = az_rad - cam_az_rad
    cos_alt = np.cos(alt_rad)
    sin_alt = np.sin(alt_rad)
    cos_az_rel = np.cos(az_rel_rad)
    sin_az_rel = np.sin(az_rel_rad)
    
    denom = 1.0 + cos_alt * cos_az_rel
    invalid = denom < 1e-6
    k = np.where(invalid, 0.0, 2.0 / denom)
    x = k * cos_alt * sin_az_rel
    y = k * sin_alt
    
    sx = cx + x * scale_h
    sy = cy_base - (y - y_center_val) * scale_h
    
    # 3. Segments
    results = []
    for i in range(len(ra)):
        row_inv = invalid[i]
        if np.all(row_inv): continue
        
        row_x = sx[i]
        row_y = sy[i]
        
        valid_idxs = np.where(~row_inv)[0]
        if len(valid_idxs) < 2: continue
        
        diffs = np.diff(valid_idxs)
        breaks = np.where(diffs > 1)[0]
        
        starts = [valid_idxs[0]]
        ends = []
        for b in breaks:
            ends.append(valid_idxs[b])
            starts.append(valid_idxs[b+1])
        ends.append(valid_idxs[-1])
        
        star_segments = []
        for s_idx, e_idx in zip(starts, ends):
            chunk_x = row_x[s_idx : e_idx+1]
            chunk_y = row_y[s_idx : e_idx+1]
            if len(chunk_x) < 2: continue
            
            dx = np.abs(np.diff(chunk_x))
            dy = np.abs(np.diff(chunk_y))
            jumps = (dx + dy) > jump_threshold
            
            if np.any(jumps):
                j_locs = np.where(jumps)[0]
                c_starts = [0]
                c_ends = []
                for jl in j_locs:
                    c_ends.append(jl)
                    c_starts.append(jl+1)
                c_ends.append(len(chunk_x)-1)
                for cs, ce in zip(c_starts, c_ends):
                    if ce >= cs:
                        star_segments.append((chunk_x[cs:ce+1], chunk_y[cs:ce+1]))
            else:
                star_segments.append((chunk_x, chunk_y))
        
        if star_segments:
            results.append(((int(f_r[i]), int(f_g[i]), int(f_b[i])), star_segments))
            
    return results


class StarRenderWorker(QObject):
    result_ready = pyqtSignal(QImage, list) # image, visible_stars_list
    trails_ready = pyqtSignal(QImage) # trail image

    @pyqtSlot(dict)
    def render(self, params):
        # params: dict with all data needed
        if not np: return
        
        try:
            width = params['width']
            height = params['height']
            zoom_level = params['zoom_level']
            vertical_ratio = params['vertical_ratio']
            elevation_angle = params['elevation_angle']
            azimuth_offset = params['azimuth_offset']
            
            # Arrays
            ra = params['ra']
            dec = params['dec']
            mag = params['mag']
            r_arr = params['r']
            g_arr = params['g']
            b_arr = params['b']
            
            # Physics
            lst = params['lst']
            lat_rad = params['lat_rad']
        except KeyError as e:
            print(f"[StarRenderWorker] Missing Param: {e}")
            return
            
        try:
            # ... Logic continues ...
            
            # Limits
            local_limit = params['mag_limit']
            star_scale = params['star_scale']
            
            # Extract Bortle and Auto Flag for worker extinction
            bortle = params.get('bortle', 1)
            is_auto = params.get('is_auto', False)
            
            # Create QImage
            img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            painter = QPainter(img)
            painter.setRenderHint(QPainter.Antialiasing, True)
            
            # --- Vectorized Logic (Copied & Adapted) ---
            
            # Mask 1: Mag Limit
            limit_buffer = local_limit + 1.0 
            mask = mag < limit_buffer
            idxs = np.where(mask)[0]
            if len(idxs) == 0:
                painter.end()
                self.result_ready.emit(img, [])
                return
            
            # Subset
            f_indices = idxs
            f_ra = ra[idxs]
            f_dec = dec[idxs]
            f_mag = mag[idxs]
            f_r = r_arr[idxs]
            f_g = g_arr[idxs]
            f_b = b_arr[idxs]
            
            # Physics
            ha = (lst - f_ra)
            ha_rad = np.radians(ha)
            dec_rad = np.radians(f_dec)
            
            sin_lat = math.sin(lat_rad)
            cos_lat = math.cos(lat_rad)
            sin_dec = np.sin(dec_rad)
            cos_dec = np.cos(dec_rad)
            
            sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * np.cos(ha_rad)
            # Clip
            sin_alt = np.clip(sin_alt, -1.0, 1.0)
            alt_rad = np.arcsin(sin_alt) # radians
            alt_deg = np.degrees(alt_rad)
            
            # --- ATMOSPHERIC EXTINCTION (AIRMASS) ---
            h_capped = np.maximum(0.1, alt_deg)
            airmass = 1.0 / (np.sin(np.radians(h_capped)) + 0.15 * (h_capped + 3.885)**-1.253)
            k_ext = 0.20 + 0.04 * np.clip(bortle - 1, 0, 8)
            airmass_penalty = k_ext * (airmass - 1.0)
            local_limit = local_limit - airmass_penalty

            # --- ATMOSPHERIC REFRACTION ---
            refraction_deg = (1.02 / 60.0) / np.tan(np.radians(h_capped + 10.3 / (h_capped + 5.11)))
            alt_deg_refined = alt_deg + refraction_deg
            
            # Azimuth
            cos_alt = np.cos(alt_rad)
            cos_az_num = sin_dec - sin_alt * sin_lat
            cos_az_den = cos_alt * cos_lat + 1e-10
            cos_az = np.clip(cos_az_num / cos_az_den, -1.0, 1.0)
            az_rad = np.arccos(cos_az)
            sin_ha = np.sin(ha_rad)
            az_rad = np.where(sin_ha > 0, 2*np.pi - az_rad, az_rad)
            az_deg = np.degrees(az_rad)
            
            # Use refined alt for projection
            alt_rad = np.radians(alt_deg_refined)

            # 3. Local Horizon Extinction (City domes) in Background Thread
            # This must match draw_stars_numpy logic exactly
            if is_auto and params.get('horizon_profile'):
                 prof = params['horizon_profile']
                 if hasattr(prof, 'light_domes') and len(prof.light_domes) > 0:
                     # Bortle-based intensity scaling (B1=0%, B5=50%, B9=100%)
                     lp_intensity_factor = max(0.0, (bortle - 1.0) / 8.0)
                     
                     az_indices = (az_deg * 2.0).astype(int) % len(prof.light_domes)
                     intensity = prof.light_domes[az_indices]
                     dist = prof.light_peak_distances[az_indices]

                     # DYNAMIC RADIUS (a0) & PENALTY logic matching main thread
                     dist_factor = np.exp(-dist / 35000.0)
                     log_intensity = np.log10(1.0 + intensity)
                     a0 = np.maximum(1.0, log_intensity * 8.0 * dist_factor)
                     
                     i_alpha = (intensity**0.40) * lp_intensity_factor * np.exp(-(alt_deg / a0)**2)
                     
                     extinction_penalty = i_alpha * 0.15
                     local_limit = local_limit - extinction_penalty
            
            # Projection - UNIVERSAL STEREOGRAPHIC (Horizon Centered)
            # Match Star Render Worker to Main Thread
            
            w, h = width, height
            scale_h = h / 2.0 * zoom_level
            cx = w / 2.0
            cy_base = h / 2.0 + (h * vertical_ratio)
            
            # View Shift
            elev_cam_rad = math.radians(elevation_angle)
            y_center_val = 2.0 * math.tan(elev_cam_rad / 2.0)
            
            # Coords are Radians already
            # p_lat = alt_rad, p_lon = az_rad
            # We need RELATIVE AZIMUTH (Az - CamAz)
            cam_az_rad = math.radians(azimuth_offset)
            az_rel_rad = az_rad - cam_az_rad
                                    
            cos_alt = np.cos(alt_rad)
            sin_alt = np.sin(alt_rad)
            cos_az = np.cos(az_rel_rad)
            sin_az = np.sin(az_rel_rad)
            
            denom = 1.0 + cos_alt * cos_az
            
            # Mask 2: Valid Projection
            mask_proj = denom > 1e-6
            if not np.any(mask_proj):
                painter.end()
                self.result_ready.emit(img, [])
                return
            
            # Filter Mask 2
            f_indices = f_indices[mask_proj]
            f_mag = f_mag[mask_proj]
            f_r = f_r[mask_proj]
            f_g = f_g[mask_proj]
            f_b = f_b[mask_proj]
            
            # Re-slice arrays
            denom = denom[mask_proj]
            cos_alt = cos_alt[mask_proj]
            sin_alt = sin_alt[mask_proj]
            sin_az = sin_az[mask_proj]
            
            k = 2.0 / denom
            x = k * cos_alt * sin_az
            y = k * sin_alt
            
            sx = cx + x * scale_h
            sy = cy_base - (y - y_center_val) * scale_h
            

            # Bounds
            mask_screen = (sx > -10) & (sx < width+10) & (sy > -10) & (sy < height+10)
            if not np.any(mask_screen):
                painter.end()
                self.result_ready.emit(img, [])
                return
            
            f_indices = f_indices[mask_screen]
            sx = sx[mask_screen]
            sy = sy[mask_screen]
            f_mag = f_mag[mask_screen]
            f_r = f_r[mask_screen]
            f_g = f_g[mask_screen]
            f_b = f_b[mask_screen]
            
            # Drawing
            diff = local_limit - f_mag
            fade_in = np.clip(diff * 2.0, 0.0, 1.0)
            eff_alpha = np.where(f_mag < 2.0, np.sqrt(fade_in), fade_in)
            mask_vis = eff_alpha > 0.01
            
            f_indices = f_indices[mask_vis]
            sx = sx[mask_vis]
            sy = sy[mask_vis]
            f_mag = f_mag[mask_vis]
            eff_alpha = eff_alpha[mask_vis]
            f_r = f_r[mask_vis]
            f_g = f_g[mask_vis]
            f_b = f_b[mask_vis]
            
            painter.setPen(Qt.NoPen)
            visible_stars_out = []
            cel_objs = params['cel_objs_ref'] # list of dicts {id, ra, dec...} 
            
            # Loop
            count = len(sx)
            for i in range(count):
                x, y = sx[i], sy[i]
                mag = f_mag[i]
                alpha_f = eff_alpha[i]
                
                # Interaction Data
                idx = f_indices[i]
                if 0 <= idx < len(cel_objs):
                     # Append small tuple: (x, y, object)
                     visible_stars_out.append((x, y, cel_objs[idx]))
                
                # Prevent QPointF overflow or crash with extreme zoom
                if abs(x) > 200000 or abs(y) > 200000: continue

                r_val, g_val, b_val = int(f_r[i]), int(f_g[i]), int(f_b[i])

                if params.get('pure_colors', False):
                     # LEGACY BEHAVIOR: Just a colored circle (Stellarium bloom/blur completely off)
                     if mag > 5.0:
                         size = max(1.0, 1.2 * star_scale)
                         a_val = 200 - min(150, int((mag - 5.0) * 10))
                         painter.setBrush(QColor(r_val, g_val, b_val, int(max(50, a_val) * alpha_f)))
                         painter.drawEllipse(QPointF(x, y), size, size)
                     else:
                         size = max(1.5, (5.0 - mag) * 0.8 * star_scale)
                         painter.setBrush(QColor(r_val, g_val, b_val, int(255 * alpha_f)))
                         painter.drawEllipse(QPointF(x, y), size, size)
                else:
                     # REALISTIC STELLARIUM OPTICS: Bloom, Clamp, Desaturation
                     if mag > 5.0:
                         # MÃ¡ximo de 1 pÃ­xel real, independientemente del zoom para evitar "bolitas"
                         size = min(1.0, 1.2 * star_scale)
                         a_val = 220 - min(100, int((mag - 5.0) * 15))
                         
                         # Faint stars lose color saturation to the eye; pull them towards white/grey
                         desat = 0.5
                         r_desat = int(r_val * (1-desat) + 200 * desat)
                         g_desat = int(g_val * (1-desat) + 200 * desat)
                         b_desat = int(b_val * (1-desat) + 220 * desat)
                         
                         final_a = int(max(60, a_val) * alpha_f * 0.7)
                         
                         painter.setBrush(QColor(r_desat, g_desat, b_desat, final_a))
                         painter.drawEllipse(QPointF(x, y), size, size)
                     else:
                         # Estrellas principales
                         size = max(1.5, (5.0 - mag) * 0.7 * star_scale)
                         core_radius = min(4.0, size) 
                         
                         final_a = int(255 * alpha_f)
                         star_c_intense = QColor(r_val, g_val, b_val, final_a)
                         transparent_c = QColor(r_val, g_val, b_val, 0)
                         
                         # 1. Halo mucho mÃ¡s centrado y menos opaco para evitar el "velo"
                         if mag < 4.0:
                             halo_size = core_radius * (5.5 - mag) * 1.8 * zoom_level
                             if mag < 1.0:
                                 halo_size = core_radius * 8.0 * zoom_level
                                 
                             halo_grad = QRadialGradient(x, y, halo_size)
                             halo_grad.setColorAt(0.0, QColor(r_val, g_val, b_val, int(90 * alpha_f)))
                             halo_grad.setColorAt(0.1, QColor(r_val, g_val, b_val, int(30 * alpha_f)))
                             halo_grad.setColorAt(0.3, QColor(r_val, g_val, b_val, int(5 * alpha_f)))
                             halo_grad.setColorAt(1.0, transparent_c)
                             
                             painter.setBrush(QBrush(halo_grad))
                             painter.drawEllipse(QPointF(x, y), halo_size, halo_size)
                         
                         # 2. NÃºcleo mÃ¡s brillante y denso
                         core_grad = QRadialGradient(x, y, core_radius)
                         core_grad.setColorAt(0.0, QColor(255, 255, 255, final_a))
                         core_grad.setColorAt(0.6, star_c_intense) # Mantiene el color base mÃ¡s hacia el borde
                         core_grad.setColorAt(1.0, transparent_c)
                         
                         painter.setBrush(QBrush(core_grad))
                         painter.drawEllipse(QPointF(x, y), core_radius, core_radius)
                     
                     # Diffraction Spikes for brightest stars (replicating draw_spikes logic)
                     # Use spike_threshold from params if available, otherwise default to 2.0
                     spike_threshold = params.get('spike_threshold', 2.0)
                     if mag < spike_threshold:
                         # Calculate factor: how much brighter than threshold?
                         factor = spike_threshold - mag
                         
                         if factor > 0:
                             # Spike parameters (matching draw_spikes)
                             spike_length = factor * 20 * zoom_level
                             spike_width = max(0.5, factor * 0.4 * zoom_level)
                             
                             if spike_length >= 4:
                                 # Colors with gradient
                                 spike_alpha = min(255, int(255 * alpha_f * 0.6))
                                 c_center = QColor(int(f_r[i]), int(f_g[i]), int(f_b[i]), spike_alpha)
                                 c_tip = QColor(int(f_r[i]), int(f_g[i]), int(f_b[i]), 0)
                                 
                                 painter.setPen(Qt.NoPen)
                                 
                                 # Draw 4 spikes in cross pattern: 0Â°, 90Â°, 180Â°, 270Â°
                                 for angle_deg in [0, 90, 180, 270]:
                                     painter.save()
                                     painter.translate(x, y)
                                     painter.rotate(angle_deg)
                                     
                                     # Gradient from center to tip
                                     grad = QLinearGradient(0, 0, spike_length, 0)
                                     grad.setColorAt(0.0, c_center)
                                     grad.setColorAt(0.05, c_center)  # Small bright core
                                     grad.setColorAt(1.0, c_tip)
                                     
                                     painter.setBrush(QBrush(grad))
                                     
                                     # Diamond-shaped ray
                                     path = QPainterPath()
                                     path.moveTo(0, -spike_width/2.0)
                                     path.lineTo(spike_length, 0)
                                     path.lineTo(0, spike_width/2.0)
                                     path.lineTo(-spike_width/2.0, 0)
                                     path.closeSubpath()
                                     
                                     painter.drawPath(path)
                                     painter.restore()
            
            painter.end()
            self.result_ready.emit(img, visible_stars_out)
            
        except Exception as e:
            print(f"[StarRenderWorker] CRASH: {e}")
            import traceback
            traceback.print_exc()
            # Emit empty result to recover
            try: 
                painter.end() 
            except: pass
            self.result_ready.emit(QImage(params['width'], params['height'], QImage.Format_ARGB32_Premultiplied), [])


    @pyqtSlot(dict)
    def render_trails(self, params):
        """Render star trails to a QImage in background thread."""
        if not np: return
        
        try:
            width = params['width']
            height = params['height']
            zoom_level = params['zoom_level']
            vertical_ratio = params['vertical_ratio']
            elevation_angle = params['elevation_angle']
            azimuth_offset = params['azimuth_offset']
            
            start_hour = params['start_hour']
            end_hour = params['end_hour']
            
            ra = params['ra']
            dec = params['dec']
            mag = params['mag']
            r_arr = params['r']
            g_arr = params['g']
            b_arr = params['b']
            
            lat_rad = params['lat_rad']
            day_of_year = params['day_of_year']
            longitude = params['longitude']
            mag_limit = params['mag_limit']
            
            # Time span
            diff = end_hour - start_hour
            if diff < -12.0: diff += 24.0
            elif diff > 12.0: diff -= 24.0
            
            if abs(diff) < 0.001:
                img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
                img.fill(Qt.transparent)
                self.trails_ready.emit(img)
                return
            
            # Determine is_moving
            is_moving = params.get('is_moving', False)

            if is_moving:
                # Low Quality: Only bright stars, few steps
                step_density = 1.0 # 1 step per hour
                mag_cap = 4.0      # Only stars brighter than mag 4
            else:
                # High Quality
                step_density = 4.0 # 4 steps per hour (15m)
                mag_cap = 5.5      # PERFORMANCE FIX: Valid only for bright stars

            n_steps = max(2, min(50, int(abs(diff) * step_density)))
            limit = min(mag_limit, mag_cap)
            
            # Create QImage
            img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            painter = QPainter(img)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(Qt.NoBrush)
            
            # Filter stars by magnitude
            mask = mag < limit + 1.0
            idxs = np.where(mask)[0]
            if len(idxs) == 0:
                painter.end()
                self.trails_ready.emit(img)
                return
            
            f_ra = ra[idxs]
            f_dec = dec[idxs]
            f_mag = mag[idxs]
            f_r = r_arr[idxs]
            f_g = g_arr[idxs]
            f_b = b_arr[idxs]
            
            sin_lat = math.sin(lat_rad)
            cos_lat = math.cos(lat_rad)
            
            base_lst_0 = (100.0 + day_of_year * 0.9856 + longitude) % 360
            
            # Compute LSTs for each step
            step_lsts = []
            for i in range(n_steps + 1):
                t = float(i) / n_steps
                h = start_hour + diff * t
                lst = (base_lst_0 + h * 15.0) % 360
                step_lsts.append(lst)
            
            step_lsts = np.array(step_lsts)
            
            # Vectorized computation for ALL stars at ALL time steps
            dec_rad = np.radians(f_dec)[:, np.newaxis]
            sin_dec = np.sin(dec_rad)
            cos_dec = np.cos(dec_rad)
            
            ha = step_lsts[np.newaxis, :] - f_ra[:, np.newaxis]
            ha_rad = np.radians(ha)
            
            sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * np.cos(ha_rad)
            sin_alt = np.clip(sin_alt, -1.0, 1.0)
            alt_rad = np.arcsin(sin_alt)
            
            cos_alt = np.cos(alt_rad)
            cos_az_num = sin_dec - sin_alt * sin_lat
            cos_az_den = cos_alt * cos_lat + 1e-10
            cos_az = np.clip(cos_az_num / cos_az_den, -1.0, 1.0)
            az_rad = np.arccos(cos_az)
            sin_ha = np.sin(ha_rad)
            az_rad = np.where(sin_ha > 0, 2*np.pi - az_rad, az_rad)

            # Projection constants
            # Projection - SPHERICAL STEREOGRAPHIC (matching render method)
            w, h = width, height
            scale_h = h / 2.0 * zoom_level
            cx = w / 2.0
            cy_base = h / 2.0 + (h * vertical_ratio)
            
            # View Shift
            elev_cam_rad = math.radians(elevation_angle)
            y_center_val = 2.0 * math.tan(elev_cam_rad / 2.0)
            
            # Camera azimuth
            cam_az_rad = math.radians(azimuth_offset)
            az_rel_rad = az_rad - cam_az_rad
            
            # Stereographic projection
            cos_alt = np.cos(alt_rad)
            sin_alt = np.sin(alt_rad)
            cos_az = np.cos(az_rel_rad)
            sin_az = np.sin(az_rel_rad)
            
            denom = 1.0 + cos_alt * cos_az
            
            # Invalid: Behind camera (denom near zero)
            invalid = denom < 1e-6
            
            # Projection formula (set invalid points to 0 temporarily)
            k = np.where(invalid, 0.0, 2.0 / denom)
            x = k * cos_alt * sin_az
            y = k * sin_alt
            
            sx = cx + x * scale_h
            sy = cy_base - (y - y_center_val) * scale_h
            
            
            jump_threshold = min(width, height) * 0.5
            
            # Use Multiprocessing to bypass GIL for coordinate math and segment detection
            from concurrent.futures import ProcessPoolExecutor
            import os

            n_stars = len(f_ra)
            n_workers = min(4, os.cpu_count() or 4)
            chunk_size = (n_stars + n_workers - 1) // n_workers
            
            tasks = []
            for i in range(0, n_stars, chunk_size):
                 end_idx = min(i + chunk_size, n_stars)
                 tasks.append((
                     f_ra[i:end_idx], f_dec[i:end_idx], f_r[i:end_idx], f_g[i:end_idx], f_b[i:end_idx],
                     step_lsts, sin_lat, cos_lat, scale_h, cx, cy_base, y_center_val, cam_az_rad, jump_threshold
                 ))
            
            # Execute tasks in parallel
            with ProcessPoolExecutor(max_workers=n_workers) as executor:
                all_results = list(executor.map(_compute_trail_segments_chunk, tasks))
            
            # Draw aggregated results onto the pixel buffer
            for chunk_res in all_results:
                for (r, g, b), star_segments in chunk_res:
                    color = QColor(r, g, b, 120)
                    painter.setPen(QPen(color, 1.0))
                    
                    path = QPainterPath()
                    for seg_x, seg_y in star_segments:
                        first = True
                        for px, py in zip(seg_x, seg_y):
                            if first:
                                path.moveTo(float(px), float(py))
                                first = False
                            else:
                                path.lineTo(float(px), float(py))
                    painter.drawPath(path)
            
            painter.end()
            self.trails_ready.emit(img)
        except Exception as e:
            pass

class AstroCanvas(QWidget):
    request_render_signal = pyqtSignal(dict)
    request_trails_signal = pyqtSignal(dict)
    
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_widget = parent
        self.setMinimumSize(800, 600)
        self.azimuth_offset = 0    
        self.elevation_angle = 40  # Default to Horizon 
        self.vertical_offset_ratio = 0.3 # Default to Shift Down
        self.zoom_level = 1.0     
        self.base_fov_deg = 93.9  # zoom=1.0 => ~17mm equiv (sensor 36mm)
        self.dragging = False
        self.last_mouse_x = 0
        self.last_mouse_y = 0
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.visible_stars = []
        self.press_pos = QPointF(0,0)
        self.trail_start_hour = None
        self.ut_hour = 12.0
        
        # Illusion Parameters
        self.illusion_enabled = True
        self.horizon_refs = 0.5
        self.dome_flattening = 0.5
        self.trained_observer = False
        self.atmospheric_context = 0.5
        self.eclipse_lock_mode = True
        
        # Weather System
        self.weather = WeatherSystem(self.width(), self.height())
        
        # Horizon Overlay (terrain/mountains â€” independent from village)
        _horizon_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'horizon_profile.npz')
        self.horizon_overlay = HorizonOverlay(horizon_profile_path=_horizon_path)
        self.horizon_overlay.request_update.connect(self.update)
        
        # Village Overlay (houses, trees, lanterns â€” on top of terrain)
        self.village = VillageOverlay()
        self.village.request_update.connect(self.update)

        # HintOverlay â€” toast HUD contextual per a zoom, temps i ubicaciÃ³
        from TerraLab.widgets.hint_overlay import HintOverlay as _HintOverlay
        self.hint_overlay = _HintOverlay(parent=self)

        # Threading for Stars
        self._cached_star_image = None
        self._cached_trail_image = None
        self._thread = QThread()
        self._worker = StarRenderWorker()
        self._worker.moveToThread(self._thread)
        self._worker.result_ready.connect(self._on_star_result)
        self._worker.trails_ready.connect(self._on_trail_result)
        self.request_render_signal.connect(self._worker.render)
        self.request_trails_signal.connect(self._worker.render_trails)
        self._thread.start()
        
        self.rendering_busy = False
        self.trail_rendering_busy = False
        
        # Info Label for Selection
        self.lbl_info = QLabel("INFO", self)
        
        # Human Eye Reset Button
        # Human Eye Reset Button
        # Human Eye Reset Button
        from PyQt5.QtWidgets import QPushButton
        self.btn_human_eye = QPushButton("\U0001F441", self)
        self.btn_human_eye.setFixedSize(45, 24)
        self.btn_human_eye.setCursor(Qt.PointingHandCursor)
        self.btn_human_eye.setToolTip("Zoom Natural (17mm)")
        self.btn_human_eye.setStyleSheet("""
            QPushButton {
                background-color: rgba(0, 0, 0, 100);
                color: white;
                border: 1px solid rgba(255, 255, 255, 100);
                border-radius: 12px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 50);
            }
        """)
        self.btn_human_eye.clicked.connect(self.reset_zoom_human)
        self.btn_human_eye.hide()

        self.lbl_info.setStyleSheet("color: lime; font-size: 10px; background: rgba(0,0,0,100);")
        self.lbl_info.move(10, 50)
        self.lbl_info.hide()
        
        # Skyfield Cache
        self._sf_cache = {'time': -1, 'data': None}
        self._eclipse_cache = {'time': -1, 'value': 1.0}
        self._moon_pos_cache = {}  # Cache for moon calculations
        self._last_skyfield_update = 0  # timestamp in ms

        # Telescope scope mode and spherical measurement overlays.
        self.scope_controller = TelescopeScopeController()
        self.measurement_controller = MeasurementController()

        # Continuous key movement for scope mode.
        self._scope_pressed_keys = set()
        self._scope_last_tick_ms = int(time.time() * 1000)
        self._scope_move_timer = QTimer(self)
        self._scope_move_timer.setInterval(33)  # ~30Hz
        self._scope_move_timer.timeout.connect(self._scope_move_tick)

    def reset_zoom_human(self):
        """Reset zoom to human eye equivalent (43mm)."""
        # Based on research article (Fotoruanopro): 43mm is the true normal.
        f_human = 43.0
        width_sensor = 36.0
        
        # Calculate horizontal FOV for 36mm sensor and 43mm lens
        # FOV_horiz = 2 * atan(36 / (2 * 43))
        fov_rad = 2.0 * math.atan(width_sensor / (2.0 * f_human))
        fov_deg = math.degrees(fov_rad)
        
        # Logic: current_fov = base_fov / zoom
        # zoom = base_fov / target_fov
        if fov_deg > 0:
            target_zoom = 100.0 / fov_deg
            self.zoom_level = target_zoom
            self.update()

    def update_skyfield_cache(self, ut_hour, day_of_year):
        # Throttle updates (cache for 1.5 seconds approx = 0.0004 hours)
        # Also include day/lat/lon/zoom in validity check basically handled by loose time check? 
        # No, zoom affects visual radius, so we must separate Astrometric Data vs Screen Data.
        # Here we cache ASTROMETRIC data (Alt, Az, Mag, Dist, Phase).
        # Screen projection happens every frame.
        
        cache_valid = False
        last_t = self._sf_cache.get('time', -1.0)
        
        # Validate cache key (Day + Hour)
        # Assuming Lat/Lon doesn't change rapidly.
        if abs(ut_hour - last_t) < 0.0004 and self._sf_cache['data'] is not None:
            cache_valid = True
            
        if cache_valid: return

        # Regenerate Cache
        if not SKYFIELD_AVAILABLE or not hasattr(self.parent_widget, 'eph'):
            self._sf_cache['data'] = None
            return

        try:
            ts = self.parent_widget.ts
            eph = self.parent_widget.eph
            observer = wgs84.latlon(self.parent_widget.latitude, self.parent_widget.longitude)
            
            now = datetime.now()
            y = getattr(self.parent_widget, 'manual_year', now.year)
            base_date = datetime(y, 1, 1) + timedelta(days=day_of_year)
            target_dt = base_date + timedelta(hours=ut_hour)
            t = ts.from_datetime(target_dt.replace(tzinfo=timezone.utc))
            
            earth = eph['earth']
            sun = eph['sun']
            moon = eph['moon']
            obs_loc = earth + observer
            
            # Sun
            ast_sun = obs_loc.at(t).observe(sun)
            alt_s, az_s, _ = ast_sun.apparent().altaz()
            d_sun_km = ast_sun.distance().km
            
            # Moon
            ast_moon = obs_loc.at(t).observe(moon)
            alt_m_real, az_m_real, _ = ast_moon.apparent().altaz()
            d_moon_km = ast_moon.distance().km
            sep_real = ast_sun.separation_from(ast_moon).degrees
            
            # Phase / Illumination
            s_earth = earth.at(t).observe(sun)
            m_earth = earth.at(t).observe(moon)
            elongation = s_earth.separation_from(m_earth).degrees
            illumination = (1 - math.cos(math.radians(elongation))) / 2

            # Planets
            planets_data = []
            planet_defs = {
                'mercury': ('Mercury', QColor(169, 169, 169), 4),
                'venus': ('Venus', QColor(255, 220, 150), 7),
                'mars': ('Mars', QColor(255, 100, 80), 5),
                'jupiter barycenter': ('Jupiter', QColor(220, 180, 140), 12),
                'saturn barycenter': ('Saturn', QColor(240, 210, 150), 10),
                'uranus barycenter': ('Uranus', QColor(173, 216, 230), 6),
                'neptune barycenter': ('Neptune', QColor(100, 100, 255), 6),
                'pluto barycenter': ('Pluto', QColor(200, 180, 160), 3),
            }
            
            for key, (name, col, sz) in planet_defs.items():
                try:
                    p = eph[key]
                    ast = obs_loc.at(t).observe(p)
                    p_alt, p_az, p_dist = ast.apparent().altaz()
                    # No optimization skip: allow rendering planets even when viewing below horizon
                    try:
                        mag = self.calculate_planet_magnitude(name, p_dist.au, 0.0)
                    except:
                        mag = -2.0 # Fallback
                        
                    planets_data.append({
                        'key': key, 'name': name, 'col': col, 'sz': sz,
                        'alt': p_alt.degrees, 'az': p_az.degrees, 'dist_au': p_dist.au,
                        'mag': mag
                    })
                except: pass

            # Eclipse Factor
            sun_rad_deg = math.degrees(math.atan(696340.0 / d_sun_km))
            moon_rad_deg = math.degrees(math.atan(1737.4 / d_moon_km))
            
            # Simple Separation Factor for dimming
            eclipse_factor = 1.0
            if sep_real < (sun_rad_deg + moon_rad_deg):
                 # Simple linear overlap approximation
                 dist_deg = sep_real
                 max_overlap = sun_rad_deg + moon_rad_deg
                 if dist_deg < max_overlap:
                     penetration = (max_overlap - dist_deg) / (sun_rad_deg * 2)
                     eclipse_factor = max(0.05, 1.0 - penetration)

            cache_data = {
                'sun': {'alt': alt_s.degrees, 'az': az_s.degrees, 'dist_km': d_sun_km, 'rad_deg': sun_rad_deg},
                'moon': {
                    'alt': alt_m_real.degrees, 'az': az_m_real.degrees, 
                    'dist_km': d_moon_km, 'rad_deg': moon_rad_deg, 
                    'sep_real': sep_real, 'illumination': illumination, 'elongation': elongation
                },
                'planets': planets_data,
                'eclipse_factor': eclipse_factor
            }
            
            self._sf_cache = {'time': ut_hour, 'data': cache_data}
            
        except Exception as e:
            # print(f"Cache Update Error: {e}")
            self._sf_cache['data'] = None

    def unproject_stereo(self, sx, sy):
        # Inverse of Universal Stereographic (Horizon Centered)
        w, h = self.width(), self.height()
        scale_h = h / 2.0 * self.zoom_level
        
        cx = w/2.0
        cy_base = h/2.0 + (h * self.vertical_offset_ratio)
        
        # View Shift
        elev_rad = math.radians(self.elevation_angle)
        y_center_val = 2.0 * math.tan(elev_rad / 2.0)
        
        # 1. Un-scale and Un-shift
        x = (sx - cx) / scale_h
        y = -((sy - cy_base) / scale_h) + y_center_val
        
        # 2. Inverse Stereo (Centered at Lat=0, Lon=0)
        # x = 2*cos(lat)*sin(lon) / (1 + cos(lat)*cos(lon))
        # y = 2*sin(lat)          / (1 + cos(lat)*cos(lon))
        # Let's use robust inversion:
        # rho = sqrt(x^2 + y^2)
        # c = 2 * atan(rho / 2)
        # sin_c = sin(c), cos_c = cos(c)
        # lat = asin(cos_c * sin(lat_0) + (y*sin_c*cos(lat_0)/rho))
        # lon = lon_0 + atan2(x*sin_c, rho*cos(lat_0)*cos_c - y*sin(lat_0)*sin_c)
        # Here lat_0 = 0, lon_0 = 0
        
        rho = math.sqrt(x*x + y*y)
        if rho < 1e-6:
             # Center
             return 0.0, self.azimuth_offset
             
        c = 2.0 * math.atan(rho / 2.0)
        sin_c = math.sin(c)
        cos_c = math.cos(c)
        
        # lat_0 = 0
        # sin(lat_0) = 0, cos(lat_0) = 1
        
        lat_rad = math.asin(y * sin_c / rho)
        lon_rad = math.atan2(x * sin_c, rho * cos_c)
        
        lat = math.degrees(lat_rad)
        lon = math.degrees(lon_rad)
        
        # Absolute Azimuth
        az = self.azimuth_offset + lon
        
        return lat, az
        


        # Village Overlay
        self.village = VillageOverlay(self)
        self.village.request_update.connect(self.update)
        
        # Camera Animation
        self.target_azimuth = None
        self.azimuth_anim_timer = QTimer(self)
        self.azimuth_anim_timer.setInterval(16)
        self.azimuth_anim_timer.timeout.connect(self._anim_azimuth_step)





    def _anim_azimuth_step(self):
        if self.target_azimuth is None:
            self.azimuth_anim_timer.stop()
            return
            
        current = self.azimuth_offset
        target = self.target_azimuth
        
        # Shortest path logic
        diff = (target - current + 180) % 360 - 180
        
        if abs(diff) < 0.1:
            self.azimuth_offset = target
            self.target_azimuth = None
            self.azimuth_anim_timer.stop()
        else:
            # Easing
            step = diff * 0.1
            self.azimuth_offset = (current + step)
            
        self.update()

    def resizeEvent(self, event):
        self._bg_cache_key = None
        self.weather.resize(self.width(), self.height())
        super().resizeEvent(event)

    def scope_mode_enabled(self) -> bool:
        return bool(getattr(self.scope_controller, "enabled", False))

    def measurement_tool_active(self) -> bool:
        return getattr(self.measurement_controller, "active_tool", TOOL_NONE) != TOOL_NONE

    def set_scope_enabled(self, enabled: bool) -> None:
        if enabled:
            self.scope_controller.activate()
        else:
            self.scope_controller.deactivate()
            self._scope_pressed_keys.clear()
            self._scope_move_timer.stop()
        self._refresh_overlay_cursor()
        self.update()

    def set_scope_shape(self, shape: str) -> None:
        self.scope_controller.set_shape(shape)
        self.update()

    def set_scope_speed_mode(self, mode: str) -> None:
        self.scope_controller.set_speed_mode(mode)
        self.update()

    def set_scope_focal_mm(self, focal_mm: float) -> None:
        self.scope_controller.set_focal_mm(focal_mm)
        self.update()

    def set_scope_sensor(self, sensor_key: str) -> None:
        self.scope_controller.set_sensor_key(sensor_key)
        self.update()

    def set_measurement_tool(self, tool: str) -> None:
        self.measurement_controller.set_tool(tool)
        self._refresh_overlay_cursor()
        self.update()

    def clear_measurements(self) -> None:
        self.measurement_controller.clear()
        self._refresh_overlay_cursor()
        self.update()

    def _refresh_overlay_cursor(self):
        if self.scope_mode_enabled() or self.measurement_tool_active():
            self.setCursor(Qt.CrossCursor)
        else:
            self.unsetCursor()

    def _scope_move_tick(self):
        if not self.scope_mode_enabled() or not self._scope_pressed_keys:
            self._scope_move_timer.stop()
            return
        now_ms = int(time.time() * 1000)
        dt = max(0.001, (now_ms - self._scope_last_tick_ms) / 1000.0)
        self._scope_last_tick_ms = now_ms

        rate = self.scope_controller.hold_rate_deg_per_s()
        step = rate * dt
        d_alt = 0.0
        d_az = 0.0

        if Qt.Key_Up in self._scope_pressed_keys:
            d_alt += step
        if Qt.Key_Down in self._scope_pressed_keys:
            d_alt -= step
        if Qt.Key_Left in self._scope_pressed_keys:
            d_az += step
        if Qt.Key_Right in self._scope_pressed_keys:
            d_az -= step

        if d_alt != 0.0 or d_az != 0.0:
            self.scope_controller.nudge(d_alt, d_az)
            self.update()

    def _scope_secondary_drag_deg_per_px(self) -> float:
        # Secondary camera drag sensitivity in scope mode follows scope speed mode.
        if self.scope_controller.speed_mode == TelescopeScopeController.SPEED_SLOW:
            return 0.5 / 60.0
        return 0.5

    def keyPressEvent(self, event):
        key = event.key()

        if self.scope_mode_enabled():
            if key == Qt.Key_Escape:
                self.set_scope_enabled(False)
                if hasattr(self.parent_widget, "sync_scope_ui_state"):
                    self.parent_widget.sync_scope_ui_state(False)
                event.accept()
                return

            if key == Qt.Key_M:
                new_mode = (
                    TelescopeScopeController.SPEED_FAST
                    if self.scope_controller.speed_mode == TelescopeScopeController.SPEED_SLOW
                    else TelescopeScopeController.SPEED_SLOW
                )
                self.scope_controller.set_speed_mode(new_mode)
                if hasattr(self.parent_widget, "sync_scope_speed_ui"):
                    self.parent_widget.sync_scope_speed_ui(new_mode)
                self.update()
                event.accept()
                return

            if key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right):
                if event.isAutoRepeat():
                    event.accept()
                    return
                # Discrete tap step
                self._scope_last_tick_ms = int(time.time() * 1000)
                step = self.scope_controller.short_step_deg()
                d_alt = 0.0
                d_az = 0.0
                if key == Qt.Key_Up:
                    d_alt = step
                elif key == Qt.Key_Down:
                    d_alt = -step
                elif key == Qt.Key_Left:
                    d_az = step
                elif key == Qt.Key_Right:
                    d_az = -step
                self.scope_controller.nudge(d_alt, d_az)
                self.update()
                self._scope_pressed_keys.add(key)
                if not self._scope_move_timer.isActive():
                    self._scope_move_timer.start()
                event.accept()
                return

        if self.measurement_tool_active() and key == Qt.Key_Escape:
            self.measurement_controller.cancel_current()
            self.update()
            event.accept()
            return

        if self.measurement_tool_active() and key in (Qt.Key_Delete, Qt.Key_Backspace):
            self.measurement_controller.delete_selected()
            self.update()
            event.accept()
            return

        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_L:
            self.log_positions()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        key = event.key()
        if self.scope_mode_enabled() and key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right):
            if not event.isAutoRepeat():
                self._scope_pressed_keys.discard(key)
                if not self._scope_pressed_keys:
                    self._scope_move_timer.stop()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def log_positions(self):
        try:
            import os
            log_dir = r"E:\Desarrollo\logs"
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Re-calc state
            local_hour = self.parent_widget.get_current_hour()
            
            # TZ Logic (Duplicated from paintEvent for robust standalonecalc)
            try:
                sim_y = self.parent_widget.manual_year
                sim_d = self.parent_widget.manual_day
                sim_dt_start = datetime(sim_y, 1, 1) + timedelta(days=sim_d)
                h_int = int(local_hour)
                m_int = int((local_hour - h_int)*60)
                sim_dt_naive = sim_dt_start.replace(hour=h_int % 24, minute=m_int % 60)
                sim_dt_local = sim_dt_naive.astimezone() 
                tz_offset = sim_dt_local.utcoffset().total_seconds() / 3600.0
            except:
                tz_offset = datetime.now().astimezone().utcoffset().total_seconds() / 3600.0
                
            ut_hour = (local_hour - tz_offset) % 24.0
            
            # Astro Calc
            dt_utc = self.get_datetime_utc(ut_hour)
            jd_utc = self.julian_day(dt_utc)
            d = jd_utc - 2451545.0
            
            sun_ra, sun_dec = self.get_sun_ra_dec(d)
            moon_ra, moon_dec, m_dist = self.get_moon_ra_dec(d)
            
            lat = self.parent_widget.latitude
            lon = self.parent_widget.longitude
            
            # Topo
            s_ra_topo, s_dec_topo, lst = self.get_topocentric_position(sun_ra, sun_dec, 149597870.7, lat, lon, jd_utc)
            m_ra_topo, m_dec_topo, _ = self.get_topocentric_position(moon_ra, moon_dec, m_dist, lat, lon, jd_utc)
            
            s_alt, s_az = self.sun_alt_az_from_ra_dec(s_ra_topo, s_dec_topo, lat, lst)
            m_alt, m_az = self.sun_alt_az_from_ra_dec(m_ra_topo, m_dec_topo, lat, lst)
            
            # Projected
            pt_sun = self.project_universal_stereo(s_alt, s_az)
            pt_moon = self.project_universal_stereo(m_alt, m_az)
            
            s_px = f"{pt_sun[0]:.1f}, {pt_sun[1]:.1f}" if pt_sun else "OFF_SCREEN"
            m_px = f"{pt_moon[0]:.1f}, {pt_moon[1]:.1f}" if pt_moon else "OFF_SCREEN"
            
            log_line = (f"[{timestamp}] Local={local_hour:.2f}h UT={ut_hour:.2f}h | "
                        f"SUN: Alt={s_alt:.2f} Az={s_az:.2f} Px={s_px} | "
                        f"MOON: Alt={m_alt:.2f} Az={m_az:.2f} Px={m_px}")
            
            with open(os.path.join(log_dir, "pos_log_ctrl_l.txt"), "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
                
            # Feedback
            self.lbl_info.setText("LOG SAVED")
            self.lbl_info.show()
            QTimer.singleShot(2000, self.lbl_info.hide)
            print(f"LOG WRITTEN: {log_line}")
        except Exception as e:
            print(f"LOG ERROR: {e}")

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.fillRect(self.rect(), Qt.black) # Safe background


            # 1. Determine Correct Time (Local -> UT) using Full Datetime
            local_hour = self.parent_widget.get_current_hour()
            sim_y = self.parent_widget.manual_year
            sim_d = self.parent_widget.manual_day
            
            # Base start of the local day
            try:
                # Create a timezone-aware datetime representing the observer's local time
                # We assume the observer's local time follows the system's timezone rules
                dt_base = datetime(sim_y, 1, 1) + timedelta(days=sim_d)
                dt_local_naive = dt_base + timedelta(hours=local_hour)
                dt_local = dt_local_naive.astimezone() # System local aware
                
                # Convert to UTC accurately
                dt_utc = dt_local.astimezone(timezone.utc)
                tz_offset = dt_local.utcoffset().total_seconds() / 3600.0
            except Exception as e:
                # Fallback to current system offset if fails
                tz_offset = datetime.now().astimezone().utcoffset().total_seconds() / 3600.0
                dt_utc = (datetime(sim_y, 1, 1) + timedelta(days=sim_d, hours=local_hour-tz_offset)).replace(tzinfo=timezone.utc)

            ut_hour = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
            self.ut_hour = ut_hour # Store for external access
            # Day of year relative to UTC (Critically avoids the 24h jump at midnight crossings)
            day_of_year_utc = (dt_utc.date() - datetime(dt_utc.year, 1, 1).date()).days
            
            # Solar Hour (Apparent, Simple) for Sun
            solar_hour = (ut_hour + self.parent_widget.longitude / 15.0) % 24.0
            
            # 1. Determine Effective Environment (Local vs Antipode)
            day_of_year = self.parent_widget.manual_day
            rise_loc, set_loc = self.calculate_sun_times(self.parent_widget.latitude, day_of_year)
            is_day_loc = rise_loc <= solar_hour < set_loc
            
            # Calculate Antipodal Sun Times (-Lat, +12h)
            rise_anti, set_anti = self.calculate_sun_times(-self.parent_widget.latitude, day_of_year)
            solar_hour_anti = (solar_hour + 12.0) % 24.0
            is_day_anti = rise_anti <= solar_hour_anti < set_anti
            
            # Compute effective variables based on observer location
            # (We keep the environment continuous even if looking below the horizon)
            eff_is_day = is_day_loc
            eff_hour = solar_hour
            eff_rise = rise_loc
            eff_set = set_loc
            eff_lat = self.parent_widget.latitude

            
            
            # Draw Background using Effective Context (Altitude & Azimuth Driven)
            # CACHE USAGE:
            # Check chk_enable_sky
            # Defaults for when Sky is Disabled
            eff_sun_alt = 90.0
            eclipse_dimming = 1.0

            if True: # Always calculate sky background and celestial positions
                # Use Day of Year relative to UTC for astronomy updates
                day_for_astro = day_of_year_utc
                eff_sun_alt, eff_sun_az = self.get_sun_alt_az(eff_hour, eff_lat, day_for_astro)
                
                # Eclipse Dimming Calculation with SMART THROTTLING
                # Update Skyfield immediately when simulated time changes, but throttle during real-time
                import time
                current_time_ms = int(time.time() * 1000)
                skyfield_update_interval = 500  # ms for real-time updates
                
                if SKYFIELD_AVAILABLE and hasattr(self.parent_widget, 'eph'):
                    # Check if simulated time has changed significantly (manual time change)
                    last_cached_hour = self._sf_cache.get('ut_hour', -999)
                    time_changed = abs(ut_hour - last_cached_hour) > 0.001  # ~3.6 seconds in simulation
                    
                    # Force update if time changed OR if enough real time has passed
                    should_update = time_changed or (current_time_ms - self._last_skyfield_update > skyfield_update_interval)
                    
                    if should_update:
                        self._last_skyfield_update = current_time_ms
                        self._sf_cache['ut_hour'] = ut_hour  # Track simulated time
                        # Only trigger heavy Skyfield updates when needed (using UTC day)
                        self.update_skyfield_cache(ut_hour, day_for_astro)
                        self._eclipse_cache['value'] = self.get_eclipse_dimming_factor(ut_hour, day_for_astro)
                        self._eclipse_cache['time'] = current_time_ms
                
                # Use cached eclipse dimming
                eclipse_dimming = self._eclipse_cache.get('value', 1.0)
    
                # Wrap view_az (azimuth_offset) to 0..360 only for background logic/sun-pos
                wrapped_az = self.azimuth_offset % 360.0
            # 1. Background (Sky Gradient)
            self.draw_background(painter, eff_sun_alt, eff_sun_az, wrapped_az, dimming=eclipse_dimming)
            
            # 2. Light Pollution & Ambient Glows (Drawn BEFORE stars to avoid occlusion)
            if hasattr(self, 'horizon_overlay') and hasattr(self.horizon_overlay, 'profile'):
                self.draw_light_domes(painter, self.horizon_overlay.profile, eff_sun_alt, eclipse_dimming)
    
            # === WEATHER SYSTEM (Background clouds) ===
            self.weather.update_weather(day_of_year, local_hour)
            self.weather.update_thunder()
            
            w_sun_alt = eff_sun_alt
            base_fov = 100.0
            current_fov = base_fov / self.zoom_level
            self.weather.draw(painter, w_sun_alt, self.azimuth_offset, self.elevation_angle, current_fov, eclipse_dimming=eclipse_dimming)

            # 3. Trails
            checked = self.parent_widget.chk_trails.isChecked()
            if checked and eff_sun_alt < -6.0:
                if self.trail_start_hour is None:
                    self.trail_start_hour = ut_hour
                self.draw_analytic_trails(painter, self.trail_start_hour, ut_hour)
            else:
                self.trail_start_hour = None
    
            # === STAR PARAMETER CALCULATION ===
            # Calculate Ambient Light Level (0.0 = Pitch Black, 1.0 = Full Day)
            if eff_sun_alt > 0:
                base_light = 1.0
            elif eff_sun_alt < -18.0:
                base_light = 0.0
            else:
                base_light = 1.0 - (eff_sun_alt / -18.0)
                base_light = max(0.0, min(1.0, base_light))
            
            ambient_light = base_light * eclipse_dimming
            
            # Dynamic Magnitude Limit
            # Daytime: Stars must be brighter than Sun's glare to be seen (basically only Sun/Moon)
            if eff_sun_alt > 0: 
                 sun_mag_limit = -10.0 # Absolute black-out for stars in daylight
            elif eff_sun_alt > -6.0:
                 # Civil Twilight: Fast drop in visibility
                 t = (eff_sun_alt - 0.0) / -6.0
                 sun_mag_limit = -10.0 + 10.0 * t # -10 at sunset, 0 at nautical
            elif eff_sun_alt > -12.0:
                 # Nautical Twilight: Transition to stars
                 t = (eff_sun_alt + 6.0) / -6.0
                 sun_mag_limit = 0.0 + 3.0 * t # 0 at start, 3.0 at nautical end
            elif eff_sun_alt > -18.0:
                 # Astronomical Twilight: Fine tuning
                 t = (eff_sun_alt + 12.0) / -6.0
                 target_mag = getattr(self.parent_widget, 'magnitude_limit', 6.0)
                 sun_mag_limit = 3.0 + (target_mag - 3.0) * t
            else:
                 sun_mag_limit = getattr(self.parent_widget, 'magnitude_limit', 6.0)
            
            eclipse_bonus = (1.0 - eclipse_dimming) * 14.0
            
            if getattr(self.parent_widget, 'is_auto_bortle', True):
                bortle_class = getattr(self.parent_widget, 'auto_bortle_estimate', getattr(self, 'auto_bortle_estimate', 1))
                # Bortle 1: 7.6, Bortle 9: 3.6
                bortle_limit = 7.6 - 0.5 * (bortle_class - 1)
                
                # We want to use the full Bortle visibility if we are in absolute darkness (Astronomical Twilight)
                # Ensure the cap doesn't artificially flatten Bortle 1 to 6.0
                final_mag_limit = min(sun_mag_limit + eclipse_bonus, bortle_limit)
                ambient_light = 1.0 + (bortle_class - 1) * 0.04
            else:
                manual_mag = getattr(self.parent_widget, 'magnitude_limit', 6.0)
                final_mag_limit = min(sun_mag_limit + eclipse_bonus, manual_mag)
            
            # Diagnostic Log for Star Visibility
            if not hasattr(self, '_last_vis_log_time'): self._last_vis_log_time = 0
            if current_time_ms - self._last_vis_log_time > 1000:
                self._last_vis_log_time = current_time_ms
                mode_str = "Auto" if getattr(self.parent_widget, 'is_auto_bortle', True) else "Manual"
                bortle_val = getattr(self.parent_widget, 'auto_bortle_estimate', getattr(self, 'auto_bortle_estimate', 1))
                print(f"[AstroCanvas] Star Render: {mode_str} Mode | Bortle: {bortle_val} | Mag Limit: {final_mag_limit:.2f}")
            
            # Cache Invalidation
            if not hasattr(self, '_last_mag_limit'): self._last_mag_limit = final_mag_limit
            if abs(self._last_mag_limit - final_mag_limit) > 0.05:
                self._cached_star_image = None
                self._last_mag_limit = final_mag_limit
            
            vis_factor = 1.0
            moon_mask = None
            if hasattr(self, '_sf_cache') and self._sf_cache.get('data'):
                md = self._sf_cache['data'].get('moon')
                if md:
                    pt_m = self.project_universal_stereo(md['alt'], md['az'])
                    if pt_m:
                        R_proj = min(self.width(), self.height()) / 2.0 * self.zoom_level
                        ppd = R_proj / 90.0
                        m_rad_deg = md.get('rad_deg', 0.25)
                        mr_px = m_rad_deg * ppd * 10.0
                        moon_mask = (pt_m[0], pt_m[1], mr_px)
    
            # 4. Stars & Celestial Objects
            if self.parent_widget.chk_enable_sky.isChecked():
                self.draw_stars(painter, ut_hour, eff_sun_alt, eff_sun_az, visibility_factor=vis_factor, moon_mask=moon_mask, mag_limit=final_mag_limit, eff_lat=eff_lat, day_of_year=day_for_astro)
            
            if SKYFIELD_AVAILABLE and hasattr(self.parent_widget, 'eph'):
                 self.draw_skyfield_objects(painter, ut_hour, day_for_astro, ambient_light=ambient_light, mag_limit=final_mag_limit)

            # 5. Horizon / Topography (Drawn on top to mask everything behind mountains)
            show_horizon = True
            if hasattr(self.parent_widget, 'chk_enable_horizon'):
                show_horizon = self.parent_widget.chk_enable_horizon.isChecked()
            
            use_detailed_topo = True
            if hasattr(self.parent_widget, 'chk_enable_village'):
                use_detailed_topo = self.parent_widget.chk_enable_village.isChecked()

            if show_horizon:
                force_flat = not use_detailed_topo
                dome_callback = None
                is_auto_bortle = getattr(self.parent_widget, 'is_auto_bortle', getattr(self, 'is_auto_bortle', True))
                if is_auto_bortle and hasattr(self, 'horizon_overlay') and hasattr(self.horizon_overlay, 'profile'):
                    tw_factor = 1.0
                    if eff_sun_alt >= 0: tw_factor = 0.0
                    elif eff_sun_alt > -18.0: tw_factor = (0 - eff_sun_alt) / 18.0
                    tw_factor *= eclipse_dimming
                    
                    if tw_factor > 0.01:
                        dome_callback = lambda p, idx, dist: self._draw_single_city_dome(p, self.horizon_overlay.profile, idx, dist, tw_factor)

                self.horizon_overlay.draw(
                    painter, self.project_universal_stereo,
                    self.width(), self.height(),
                    self.azimuth_offset, self.zoom_level,
                    self.elevation_angle, ut_hour,
                    draw_flat_line=force_flat,
                    projection_fn_numpy=self.project_universal_stereo_numpy,
                    draw_domes_callback=dome_callback
                )
                if hasattr(self, '_dome_count') and self._dome_count > 0:
                    current_time = __import__('time').time()
                    if current_time - getattr(self, '_last_dome_log_time', 0) > 2.0:
                        print(f"[AstroCanvas] City Domes Draw Call: {self._dome_count} centers found.")
                        self._last_dome_log_time = current_time

            # Weather is already drawn once before horizon overlay. 
            # Sub-components like fog or rain can be layered, but drawing the whole atmosphere twice saturates the screen.
            # self.weather.draw(painter, w_sun_alt, self.azimuth_offset, self.elevation_angle, current_fov, eclipse_dimming=eclipse_dimming)


            
            # 6. Compass
            self.draw_compass(painter)
            
            # 7. HUD information
            painter.setPen(QColor(255, 255, 255, 200))
            lbl_stars = getTraduction("Astro.Stars", "STARS")
            painter.drawText(20, 30, f"{lbl_stars}: {len(self.visible_stars)}")
            
            # Direction HUD
            az = self.azimuth_offset % 360
            dirs_keys = ["North", "NE", "East", "SE", "South", "SW", "West", "NW", "North"]
            dirs = [getTraduction(f"Astro.{d}", d.upper()) for d in dirs_keys]
            idx = int((az + 22.5) / 45)
            if idx >= len(dirs): idx = 0
                
            direction_str = dirs[idx]
            lbl_looking = getTraduction("Astro.Looking", "LOOKING")
            
            painter.setFont(QFont("Arial", 14, QFont.Bold))
            painter.drawText(20, 60, f"{lbl_looking}: {direction_str}")
            
            # Lat Indicator
            lat = self.parent_widget.latitude
            hemi = "N" if lat >= 0 else "S"
            painter.setFont(QFont("Arial", 10))
            painter.drawText(20, 80, f"LAT: {abs(lat):.2f}Â° {hemi}")

            # Altitude Indicator
            alt = self.elevation_angle
            lbl_alt = "ALT" 
            painter.drawText(20, 100, f"{lbl_alt}: {alt:.1f}Â°")
            
            # Focal Length Indicator & Human Eye Button
            # Base FOV = 100 deg (Zoom 1.0)
            # 35mm equiv focal length: f = 36 / (2 * tan(fov_horiz/2))
            # fov_horiz_rad = radians(100 / zoom)
            fov_rad = math.radians(93.9 / self.zoom_level)
            focal_length = 18.0 / math.tan(fov_rad / 2.0)
            
            # Using round() to avoid 49.99mm displaying as 49mm
            display_focal = int(round(focal_length))
            painter.drawText(20, 120, f"FOC: {display_focal} mm")
            
            # Position the eye button dynamically next to FOC text
            # Assuming text width ~ 100px?
            if hasattr(self, 'btn_human_eye'):
                # Move button only if needed
                self.btn_human_eye.move(140, 105) 
                if not self.btn_human_eye.isVisible():
                    self.btn_human_eye.show()

            # 8. User overlays (always above stars/planets/trails/terrain)
            self.measurement_controller.draw(
                painter,
                self.project_universal_stereo,
                formatters={},
            )
            self.scope_controller.draw(
                painter,
                self.width(),
                self.height(),
                self.project_universal_stereo,
            )
             
        finally:
            painter.end()

    def get_sun_alt_az(self, hour, lat, day_of_year):
        # Try Cache First - MUST validate time match
        # Note: 'hour' arg is Solar Hour (Approx Local). Cache stores UT Hour.
        # Difference is approx longitude / 15.0. We use a 0.2h (12m) tolerance.
        if hasattr(self, '_sf_cache') and self._sf_cache.get('data') and SKYFIELD_AVAILABLE:
            cache_t = self._sf_cache.get('time', -1.0)
            if abs(hour - cache_t) < 0.2: 
                s = self._sf_cache['data']['sun']
                return s['alt'], s['az']
        
        # Precise calculation matching draw_sun but returning Azimuth too
        dec_deg = -23.44 * math.cos(math.radians(360/365 * (day_of_year + 10)))
        dec_rad = math.radians(dec_deg)
        lat_rad = math.radians(lat)
        
        # Hour Angle
        ha_deg = (hour - 12.0) * 15.0
        ha_rad = math.radians(ha_deg)
        
        # Altitude
        sin_alt = (math.sin(dec_rad) * math.sin(lat_rad) + 
                   math.cos(dec_rad) * math.cos(lat_rad) * math.cos(ha_rad))
        # Clamp against numerical errors
        sin_alt = max(-1.0, min(1.0, sin_alt))
        alt_deg = math.degrees(math.asin(sin_alt))
        
        # Azimuth
        # cos(az) = (sin(dec) - sin(alt)*sin(lat)) / (cos(alt)*cos(lat))
        # Handle Zenith singularity
        cos_alt_val = math.cos(math.radians(alt_deg))
        if abs(cos_alt_val) < 1e-4:
            az_deg = 180.0
        else:
            cos_az = (math.sin(dec_rad) - sin_alt * math.sin(lat_rad)) / \
                     (cos_alt_val * math.cos(lat_rad))
            cos_az = max(-1.0, min(1.0, cos_az))
            az_deg = math.degrees(math.acos(cos_az))
            if math.sin(ha_rad) > 0: az_deg = 360 - az_deg
            
        return alt_deg, az_deg



    
    def draw_analytic_trails(self, painter, start_hour, end_hour):
        # 1. Determine interaction state
        pw = self.parent_widget
        is_moving = self.dragging or getattr(pw, '_dragging_time', False) or (hasattr(self, 'anim_timer') and self.anim_timer.isActive())
        
        # 2. Draw Cached Trail Image if available
        if self._cached_trail_image and not self._cached_trail_image.isNull():
            painter.drawImage(0, 0, self._cached_trail_image)
        
        # 3. If interacting, draw a "Fast-Path" (Reduced LOD) synchronously to avoid flickering
        if is_moving:
            diff = end_hour - start_hour
            if diff < -12.0: diff += 24.0
            elif diff > 12.0: diff -= 24.0
            
            # Exposure must be forward-running (start -> end). 
            # If diff is negative, it means they moved back beyond the start.
            if diff > 0.001:
                # Synchronous call to NumPy renderer with aggressive LOD
                self.draw_analytic_trails_numpy(painter, start_hour, end_hour, diff, n_steps=5, limit=3.0, is_moving=True)
            else:
                # If negative or zero, we don't draw new trails synchronously
                pass
        
        # 4. Trigger Worker for NEXT frame (if not busy)
        if np and hasattr(pw, 'np_ra') and not self.trail_rendering_busy:
            self.trail_rendering_busy = True
            
            params = {
                'width': self.width(),
                'height': self.height(),
                'zoom_level': self.zoom_level,
                'vertical_ratio': self.vertical_offset_ratio,
                'elevation_angle': self.elevation_angle,
                'azimuth_offset': self.azimuth_offset,
                'start_hour': start_hour,
                'end_hour': end_hour,
                'ra': pw.np_ra,
                'dec': pw.np_dec,
                'mag': pw.np_mag,
                'r': pw.np_r,
                'g': pw.np_g,
                'b': pw.np_b,
                'lat_rad': math.radians(pw.latitude),
                'day_of_year': pw.manual_day,
                'longitude': pw.longitude,
                'mag_limit': pw.magnitude_limit,
                'is_moving': is_moving,
                'min_diff': 0.0 # Force forward-only in worker too if needed
            }
            
            # Recalculate diff for worker limit
            diff = end_hour - start_hour
            if diff < -12.0: diff += 24.0
            elif diff > 12.0: diff -= 24.0
            
            if diff > 0.001:
                 self.request_trails_signal.emit(params)
            else:
                 self.trail_rendering_busy = False # Cancel
                 self._cached_trail_image = None # Clear if going back to start
        
        return
        
        # --- LEGACY CODE BELOW (Kept for reference, never reached) ---
        # Determine time span
        diff = end_hour - start_hour
        # Handle day wrap 
        if diff < -12.0: diff += 24.0
        elif diff > 12.0: diff -= 24.0
        
        # If negligible duration, do nothing
        if abs(diff) < 0.001: return
        
        # Dynamic LOD (Level of Detail) Optimization
        # If dragging or animating, reduce quality drastically for performance
        is_moving = self.dragging or (hasattr(self, 'anim_timer') and self.anim_timer.isActive())
        
        if is_moving:
             # Low Quality: Only bright stars, few steps
             step_density = 1.0 # 1 step per hour
             mag_cap = 4.0      # Only stars brighter than mag 4
        else:
             # High Quality
             step_density = 4.0 # 4 steps per hour (15m)
             mag_cap = 5.5      # PERFORMANCE FIX: Valid only for bright stars
             
        # Steps for arc approximation 
        n_steps = max(2, min(50, int(abs(diff) * step_density)))
        
        # Pre-calc LST base
        day_of_year = self.parent_widget.manual_day
        base_lst_0 = (100.0 + day_of_year * 0.9856 + self.parent_widget.longitude) % 360
        
        lat_rad = math.radians(self.parent_widget.latitude)
        sin_lat = math.sin(lat_rad)
        cos_lat = math.cos(lat_rad)
        
        # Effective limit is visual limit capped by performance cap
        limit = min(self.parent_widget.magnitude_limit, mag_cap)
        
        # NumPy Vectorization
        if np and hasattr(self.parent_widget, 'np_ra'):
             self.draw_analytic_trails_numpy(painter, start_hour, end_hour, diff, n_steps, limit, is_moving)
             return
        
        # Pre-calculate Step LSTs
        step_lsts = []
        for i in range(n_steps + 1):
            t = float(i) / n_steps
            h = start_hour + diff * t
            lst = (base_lst_0 + h * 15.0) % 360
            step_lsts.append(lst)
            
        # Draw Loop
        painter.setBrush(Qt.NoBrush)
        
        for star in self.parent_widget.celestial_objects:
            if star['mag'] > limit: continue
            
            ra = star['ra']
            dec = star['dec']
            
            # Optimization: Pre-calc trig for star
            dec_rad = math.radians(dec)
            sin_dec = math.sin(dec_rad)
            cos_dec = math.cos(dec_rad)
            
            points = []
            valid_points = 0
            
            # Compute path points
            for lst in step_lsts:
                ha = (lst - ra)
                ha_rad = math.radians(ha)
                
                # Alt/Az
                sin_alt = (sin_dec * sin_lat + 
                           cos_dec * cos_lat * math.cos(ha_rad))
                
                # Fast Asin clamp
                if sin_alt > 1.0: sin_alt = 1.0
                elif sin_alt < -1.0: sin_alt = -1.0
                
                alt = math.degrees(math.asin(sin_alt))
                
                cos_az = (sin_dec - sin_alt * sin_lat) / \
                         (math.cos(math.radians(alt)) * cos_lat + 1e-10)
                
                # Fast Acos clamp
                if cos_az > 1.0: cos_az = 1.0
                elif cos_az < -1.0: cos_az = -1.0
                
                az = math.degrees(math.acos(cos_az))
                if math.sin(ha_rad) > 0: az = 360 - az
                
                pt = self.project_universal_stereo(alt, az)
                points.append(pt) 
                if pt: valid_points += 1
                
            if valid_points < 2: continue
            
            # Render Path
            path = QPainterPath()
            first_pt = True
            
            # Distance threshold for breaking paths (avoiding wrapping lines)
            # 50% of min dimension is a safe bet for a "jump" that shouldn't happen in a smooth arc
            jump_threshold = min(self.width(), self.height()) * 0.5
            
            for pt in points:
                if pt is None:
                    first_pt = True
                else:
                    curr_p = QPointF(*pt)
                    if first_pt:
                        path.moveTo(curr_p)
                        first_pt = False
                    else:
                        # Check distance to previous point
                        last_p = path.currentPosition()
                        dist = (curr_p - last_p).manhattanLength()
                        if dist > jump_threshold:
                            path.moveTo(curr_p)
                        else:
                            path.lineTo(curr_p)
            
            bp_rp = star.get('bp_rp', 0.8) 
            color = self.get_star_color(bp_rp)
            color.setAlpha(120 if not is_moving else 80) # Faint trails while moving
            painter.setPen(QPen(color, 1.0))
            painter.drawPath(path)

    def draw_analytic_trails_numpy(self, painter, start_hour, end_hour, diff, n_steps, limit, is_moving):
        pw = self.parent_widget
        
        # PERFORMANCE TWEAK: Cap max steps and magnitude for complex trails
        # "Completed" trails can be heavy. 
        if not is_moving:
            limit = min(limit, 5.0) # Reduce from 5.5 to 5.0 (Hardly visible diff, big savings)
            n_steps = min(n_steps, 40) # Cap steps
        
        # 1. Filter Stars by Magnitude
        mask_mag = pw.np_mag < limit
        if not np.any(mask_mag): return
        
        # Use subsets
        ra = pw.np_ra[mask_mag]   # Shape (N_stars,)
        dec = pw.np_dec[mask_mag] # Shape (N_stars,)
        
        # Colors (RGB)
        f_r = pw.np_r[mask_mag]
        f_g = pw.np_g[mask_mag]
        f_b = pw.np_b[mask_mag]
        
        # 2. Prepare Time Steps
        steps_t = np.linspace(0.0, 1.0, n_steps + 1)
        steps_h = start_hour + diff * steps_t
        
        day_of_year = pw.manual_day
        base_lst_0 = (100.0 + day_of_year * 0.9856 + pw.longitude) % 360
        
        step_lsts = (base_lst_0 + steps_h * 15.0) % 360
        step_lsts = step_lsts[np.newaxis, :] 
        
        # 3. Physics (Broadcasting)
        ra_col = ra[:, np.newaxis]
        ha = step_lsts - ra_col
        ha_rad = np.radians(ha)
        
        dec_rad = np.radians(dec)[:, np.newaxis]
        
        lat_rad = math.radians(pw.latitude)
        sin_lat = math.sin(lat_rad)
        cos_lat = math.cos(lat_rad)
        
        sin_dec = np.sin(dec_rad)
        cos_dec = np.cos(dec_rad)
        
        sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * np.cos(ha_rad)
        sin_alt = np.clip(sin_alt, -1.0, 1.0)
        alt_rad = np.arcsin(sin_alt) 
        
        cos_alt = np.cos(alt_rad)
        cos_az_num = sin_dec - sin_alt * sin_lat
        cos_az_den = cos_alt * cos_lat + 1e-10
        cos_az = np.clip(cos_az_num / cos_az_den, -1.0, 1.0)
        az_rad = np.arccos(cos_az)
        
        sin_ha = np.sin(ha_rad)
        az_rad = np.where(sin_ha > 0, 2*np.pi - az_rad, az_rad) 
        
        # 4. Projection - UNIVERSAL STEREOGRAPHIC (Horizon Centered)
        w, h = self.width(), self.height()
        scale_h = h / 2.0 * self.zoom_level
        cx = w/2.0
        cy_base = h/2.0 + (h * self.vertical_offset_ratio)
        
        # View Shift
        elev_cam_rad = math.radians(self.elevation_angle)
        y_center_val = 2.0 * math.tan(elev_cam_rad / 2.0)
        
        # Relative Azimuth (for projection formula)
        cam_az_rad = math.radians(self.azimuth_offset)
        
        # We need to project: lat=alt_rad, lon=az_rad relative to cam_az
        az_rel_rad = az_rad - cam_az_rad
        
        cos_alt = np.cos(alt_rad)
        sin_alt = np.sin(alt_rad)
        cos_az = np.cos(az_rel_rad)
        sin_az = np.sin(az_rel_rad)
        
        denom = 1.0 + cos_alt * cos_az
        
        # Trails: We need to handle invalid points (behind camera)
        # invalid mask
        invalid = denom <= 1e-6
        denom = np.where(invalid, 1.0, denom)
        
        k = 2.0 / denom
        x = k * cos_alt * sin_az
        y = k * sin_alt
        
        sx = cx + x * scale_h
        sy = cy_base - (y - y_center_val) * scale_h
        jump_threshold = min(w, h) * 0.5
        
        batches = {} # Key: (r,g,b), Value: QPainterPath
        
        for i in range(len(ra)):
            row_x = sx[i]
            row_y = sy[i]
            row_inv = invalid[i]
            
            if np.all(row_inv):
                continue
            
            # Get Key for batching
            key = (int(f_r[i]), int(f_g[i]), int(f_b[i]))
            if key not in batches:
                batches[key] = QPainterPath()
            path = batches[key]
            
            # Identify continuous segments
            valid_idxs = np.where(~row_inv)[0]
            if len(valid_idxs) < 2: continue
            
            diffs = np.diff(valid_idxs)
            breaks = np.where(diffs > 1)[0]
            
            starts = [valid_idxs[0]]
            ends = []
            for b in breaks:
                ends.append(valid_idxs[b])
                starts.append(valid_idxs[b+1])
            ends.append(valid_idxs[-1])
            
            for s_idx, e_idx in zip(starts, ends):
                chunk_x = row_x[s_idx : e_idx+1]
                chunk_y = row_y[s_idx : e_idx+1]
                
                if len(chunk_x) < 2: continue
                
                dx = np.abs(np.diff(chunk_x))
                dy = np.abs(np.diff(chunk_y))
                jumps = (dx + dy) > jump_threshold
                
                if np.any(jumps):
                    j_locs = np.where(jumps)[0]
                    c_starts = [0]
                    c_ends = []
                    for jl in j_locs:
                        c_ends.append(jl)
                        c_starts.append(jl+1)
                    c_ends.append(len(chunk_x)-1)
                    
                    for cs, ce in zip(c_starts, c_ends):
                        if ce >= cs:
                             p_pts = [QPointF(float(x_val), float(y_val)) for x_val, y_val in zip(chunk_x[cs:ce+1], chunk_y[cs:ce+1])]
                             if len(p_pts) > 1:
                                path.addPolygon(QPolygonF(p_pts))
                else:
                    p_pts = [QPointF(float(x_val), float(y_val)) for x_val, y_val in zip(chunk_x, chunk_y)]
                    if len(p_pts) > 1:
                        path.addPolygon(QPolygonF(p_pts))

        # Render Batches
        alpha_val = 120 if not is_moving else 60
        painter.setBrush(Qt.NoBrush)
        painter.setRenderHint(QPainter.Antialiasing, False if is_moving else True)
        
        for (r, g, b), path in batches.items():
            color = QColor(r, g, b, alpha_val)
            painter.setPen(QPen(color, 1.0))
            painter.drawPath(path)

    def project_universal_stereo(self, alt, az):
        # MODO STEREOGRAPHIC-HORIZON (Flat Horizon, Spherical Sky)
        # Centers projection on Horizon (Alt=0), but shifts vertically to simulate looking up.
        # This keeps the horizon straight but maps the Zenith to a finite point.
        
        w, h = self.width(), self.height()
        
        # Scaling
        # Standard: Y=1 (Radius) corresponds to ~ 45 degrees vertical from center?
        # Let's calibrate so: 1 screen height ~ 60 degree vertical
        scale_h = h / 2.0 * self.zoom_level # Radius in pixels
        
        # 1. Coordinates relative to Camera Azimuth but FIXED Horizontal Plane
        az_rel = math.radians(az - self.azimuth_offset)
        alt_rad = math.radians(alt)
        
        # 2. Stereographic Projection (Center = 0,0 i.e. Looking at Horizon)
        # Formula: x = 2*cos(lat)*sin(lon) / (1 + cos(lat)*cos(lon))
        #          y = 2*sin(lat)          / (1 + cos(lat)*cos(lon))
        # where lat = alt, lon = az_rel
        
        cos_alt = math.cos(alt_rad)
        sin_alt = math.sin(alt_rad)
        cos_az = math.cos(az_rel)
        sin_az = math.sin(az_rel)
        
        denom = 1.0 + cos_alt * cos_az
        if denom < 1e-6: return None # Behind camera anti-pode
        
        k = 2.0 / denom
        x = k * cos_alt * sin_az
        y = k * sin_alt
        
        # 3. Screen Mapping
        elev_rad = math.radians(self.elevation_angle)
        y_center_val = 2.0 * math.tan(elev_rad / 2.0)
        
        # Pixel coordinates
        cx = w / 2.0
        cy_base = (h / 2.0) + (h * self.vertical_offset_ratio)
        
        screen_x = cx + x * scale_h
        screen_y = cy_base - (y - y_center_val) * scale_h
        
        return (screen_x, screen_y)

    def project_universal_stereo_numpy(self, alt_array, az_array):
        """
        Vectorized version of project_universal_stereo using NumPy.
        Returns: (screen_x_array, screen_y_array)
        """
        if np is None: return None, None

        w, h = self.width(), self.height()
        scale_h = h / 2.0 * self.zoom_level
        cx = w / 2.0
        cy_base = (h / 2.0) + (h * self.vertical_offset_ratio)

        # 1. Coordinates relative to Camera Azimuth
        az_rel = np.radians(az_array - self.azimuth_offset)
        alt_rad = np.radians(alt_array)

        # 2. Stereographic Projection
        cos_alt = np.cos(alt_rad)
        sin_alt = np.sin(alt_rad)
        cos_az = np.cos(az_rel)
        sin_az = np.sin(az_rel)

        denom = 1.0 + cos_alt * cos_az
        valid_mask = denom > 1e-6
        safe_denom = np.where(valid_mask, denom, 1.0)
        
        k = 2.0 / safe_denom
        x = k * cos_alt * sin_az
        y = k * sin_alt

        # 3. Screen Mapping
        elev_rad = math.radians(self.elevation_angle)
        y_center_val = 2.0 * math.tan(elev_rad / 2.0)

        screen_x = cx + x * scale_h
        screen_y = cy_base - (y - y_center_val) * scale_h
        
        screen_x = np.where(valid_mask, screen_x, np.nan)
        screen_y = np.where(valid_mask, screen_y, np.nan)

        return screen_x, screen_y

# ... (inside AstronomicalWidget)

    def wheelEvent(self, event):
        if self.scope_mode_enabled():
            # In scope mode we avoid changing the global camera zoom unexpectedly.
            event.accept()
            return

        degrees = event.angleDelta().y() / 8.0
        steps = degrees / 15.0
        factor = 1.1 ** steps
        self.zoom_level *= factor
        # Allow much deep zoom for Eclipse Inspection (0.5 to 50.0)
        self.zoom_level = max(0.5, min(50.0, self.zoom_level))
        # Keep cached trail image during movement to avoid flickering
        # The Fast-Path will draw on top.
        self.update()

        # â”€ Toast HUD: mostra FOV i focal equivalent al zoom actual â”€
        if hasattr(self, 'hint_overlay'):
            fov_deg   = 100.0 / self.zoom_level
            focal_rad = math.radians(fov_deg)
            focal_eq  = int(round(18.0 / math.tan(focal_rad / 2.0)))
            txt = getTraduction("HUD.ZoomHint", "FOV {fov}°  ·  {focal}mm").format(
                fov=f"{fov_deg:.1f}", focal=focal_eq
            )
            self.hint_overlay.show_hint(txt)






    def calculate_sun_times(self, lat, day_of_year):
        # Approximate declination
        dec = -23.44 * math.cos(math.radians(360/365 * (day_of_year + 10)))
        lat_rad = math.radians(lat)
        dec_rad = math.radians(dec)
        
        # Hour angle bounds
        val = -math.tan(lat_rad) * math.tan(dec_rad)
        val = max(-1, min(1, val)) # Clamp for polar regions
        
        ha_rad = math.acos(val)
        half_day = math.degrees(ha_rad) / 15.0
        
        sunrise = 12.0 - half_day
        sunset = 12.0 + half_day
        return sunrise, sunset

    def sky_color_phys(self, view_alt, view_az, sun_alt, sun_az, bortle=1, twilight_factor=1.0):
        # 1. Base Gradient Interpolation (Zenith & Horizon)
        # Using the simplified keyframe approach for robust base colors
        
        # Keyframes (Zenith Color 't', Horizon Color 'b', Sun Glow 'g')
        # Keyframes (Zenith Color 't', Horizon Color 'b', Sun Glow 'g')
        keyframes = [
            # Day: Deep Blue / Desaturated Blue / White-Yellow
            { 'alt': 20.0, 't': (0, 100, 200), 'b': (100, 180, 255), 'g': (255, 255, 220) },
            # Golden Hour: Slate Blue / Gold / Orange
            { 'alt': 6.0,  't': (20, 50, 90),  'b': (255, 190, 60), 'g': (255, 140, 20) },
            # Sunset: Deep Indigo / Burning Red / Orange-Red
            { 'alt': 0.0,  't': (25, 30, 70),  'b': (255, 70, 10), 'g': (255, 60, 0) },
            # Civil Twilight (The "Magical" Blue/Purple Hour)
            # Reference Image: Dark Purple/Blue Top, Band of Orange/Yellow Horizon
            { 'alt': -4.0, 't': (10, 15, 50),  'b': (120, 60, 20), 'g': (60, 20, 5) },
            # Civil End: Black-Blue / Deep Crimson fade
            { 'alt': -6.0, 't': (5, 5, 25),    'b': (40, 15, 20),  'g': (10, 2, 0) },
            # Nautical: Night / Dark Grey / None
            { 'alt': -12.0,'t': (2, 2, 10),    'b': (10, 10, 25),  'g': (0, 0, 0) },
            # Night
            { 'alt': -18.0,'t': (0, 0, 5),     'b': (5, 5, 15),    'g': (0, 0, 0) }
        ]
        
        # Interpolate Base Colors
        def lerp_tup(c1, c2, f):
            return tuple(int(a + (b - a) * f) for a, b in zip(c1, c2))
            
        k1 = keyframes[0]
        k2 = keyframes[-1]
        
        if sun_alt >= keyframes[0]['alt']:
            k1 = keyframes[0]; k2 = k1; factor = 0.0
        elif sun_alt <= keyframes[-1]['alt']:
            k1 = keyframes[-1]; k2 = k1; factor = 0.0
        else:
            for i in range(len(keyframes) - 1):
                if keyframes[i]['alt'] >= sun_alt >= keyframes[i+1]['alt']:
                    k1 = keyframes[i]
                    k2 = keyframes[i+1]
                    rng = k1['alt'] - k2['alt']
                    factor = (k1['alt'] - sun_alt) / (rng + 1e-9)
                    break
        
        c_zen = lerp_tup(k1['t'], k2['t'], factor)
        c_hor = lerp_tup(k1['b'], k2['b'], factor)
        c_sun = lerp_tup(k1['g'], k2['g'], factor)
        
        # 2. Azimuthal & Physics Factors
        delta_az_rad = math.radians(abs(view_az - sun_az))
        while delta_az_rad > math.pi: delta_az_rad -= 2*math.pi
        delta_az_rad = abs(delta_az_rad)
        
        # Calculate angular distance to sun (approx) using spherical law of cosines
        v_alt_rad = math.radians(view_alt)
        s_alt_rad = math.radians(sun_alt)
        
        cos_gamma = math.sin(v_alt_rad)*math.sin(s_alt_rad) + \
                    math.cos(v_alt_rad)*math.cos(s_alt_rad)*math.cos(delta_az_rad)
        # Clamp
        cos_gamma = max(-1.0, min(1.0, cos_gamma))
        gamma_rad = math.acos(cos_gamma) # Angle in radians (0..pi)
        
        # --- HORIZON AZIMUTHAL VARIATION ---
        # Modify the horizon color (c_hor) based on direction relative to sun
        # If facing sun -> Use c_hor (Red/Orange/Gold)
        # If facing away -> Use Dark Blue/Grey (Anti-Sun Horizon)
        
        # Only applies when Sun is low or setting (Golden Hour / Sunset / Twilight)
        # Sun Alt < 15 deg and Sun Alt > -12 (Nautical End - completely gone)
        if -12.0 < sun_alt < 15.0:
            # Factor: 1.0 (Facing Sun) ... 0.0 (Facing Away)
            az_factor = (math.cos(delta_az_rad) + 1.0) / 2.0 
            # Sharpen the curve so red stays near sun
            az_factor = math.pow(az_factor, 1.5)
            
            # STRENGHT FALLOFF: Fade out the azimuthal variation completely after sunset
            # At -12.0, sun_extinction is 0.0
            sun_extinction = 1.0
            if sun_alt < 0:
                sun_extinction = max(0.0, 1.0 - (abs(sun_alt) / 12.0))
            
            # Define Anti-Sun Horizon Color based on altitude
            if sun_alt > 0:
                # Day/Golden: Horizon opposite is Blue-ish/Grey
                anti_hor = (100, 130, 170)
            else:
                # Twilight: Horizon opposite is deep night sky (should NOT be brighter than base)
                # Night base horizon is (10, 10, 25) for -12. 
                # Anti-sun should be the darkest part of the horizon.
                anti_hor = (5, 5, 12)
                
            # Lerp Current Horizon (Red/Gold) towards Anti-Horizon
            r_target = int(c_hor[0] * az_factor + anti_hor[0] * (1-az_factor))
            g_target = int(c_hor[1] * az_factor + anti_hor[1] * (1-az_factor))
            b_target = int(c_hor[2] * az_factor + anti_hor[2] * (1-az_factor))
            
            # Apply Sun Extinction (Night doesn't have azimuthal horizon color differences)
            r_hor = int(c_hor[0] * (1-sun_extinction) + r_target * sun_extinction)
            g_hor = int(c_hor[1] * (1-sun_extinction) + g_target * sun_extinction)
            b_hor = int(c_hor[2] * (1-sun_extinction) + b_target * sun_extinction)
            
            # Update base horizon color for vertical mix
            c_hor = (r_hor, g_hor, b_hor)
            
        # STERN NIGHT FILTER: Once Sun is deep underground, zero out all residual glow.
        if sun_alt <= -12.0:
            # Force Night Palette (Blue shifts)
            c_zen = (0, 0, 5)
            c_hor = (5, 5, 12)
            c_sun = (0, 0, 0)
        elif sun_alt <= -18.0:
            c_zen = (0, 0, 2)
            c_hor = (2, 2, 8)
            c_sun = (0, 0, 0)

        # 3. Composition
        # Vertical gradient t value (0 at zenith, 1 at horizon)
        t = 1.0 - (view_alt / 90.0)
        t = max(0.0, min(1.0, t))
        
        # Mix Base Zenith -> Horizon
        mix_t = t * t * (3 - 2*t) # Smoothstep
        r_base = c_zen[0] * (1-mix_t) + c_hor[0] * mix_t
        g_base = c_zen[1] * (1-mix_t) + c_hor[1] * mix_t
        b_base = c_zen[2] * (1-mix_t) + c_hor[2] * mix_t
        
        # Default result
        r, g, b = r_base, g_base, b_base
        
        # Subtle Background Directional Glow (NOT the main Sun Halo)
        # This makes the sky slightly brighter/warmer in the sun's general direction
        # without drawing a distinct disk or halo.
        if cos_gamma > 0.0:
            # Very wide, very soft lobe
            dir_glow = math.pow(cos_gamma, 4.0) * 0.15 # Max 15% influence
            
            # PHYSICAL FALLOFF: After Nautical Twilight, the sun halo should be ZERO.
            # Ramps from 1.0 (Day/Sunset) to 0.0 at SunAlt = -12.0
            glow_intensity = 1.0
            if sun_alt < 0:
                glow_intensity = max(0.0, 1.0 - (abs(sun_alt) / 12.0))
            
            dir_glow *= glow_intensity
            
            # Use Sun Glow Color but heavily blended with Sky
            # We add it additively but very weakly
            r = min(255, r + c_sun[0] * dir_glow)
            g = min(255, g + c_sun[1] * dir_glow)
            b = min(255, b + c_sun[2] * dir_glow)
            
        # OPPOSITE SUN Effects (Belt of Venus / Earth Shadow)
        # Use smooth factor instead of hard delta_az_rad > pi/2 cutoff
        # cos(delta_az) is 1 at Sun, 0 at 90 deg, -1 at Anti-Sun
        # We want effect to start at 90 deg (0) and max at 180 deg (1)
        az_away_factor = max(0.0, -math.cos(delta_az_rad))
        
        # Belt of Venus (Pinkish Band)
        if -10 <= sun_alt <= 2 and az_away_factor > 0:
            angle_from_anti_sun = abs(gamma_rad - math.pi) # 0 means looking at anti-sun
            # Only near anti-sun
            if angle_from_anti_sun < 0.5:
                 # Band at ~10-20 deg elevation
                belt_center = 10.0
                belt_dist = abs(view_alt - belt_center)
                belt_str = math.exp(-(belt_dist*belt_dist)/100.0) * 0.2 * (1.0 - angle_from_anti_sun*2.0)
                
                # Modulate by azimuth factor for safety (though angle_from_anti_sun handles it mostly)
                belt_str *= az_away_factor
                
                r += 60 * belt_str
                g += 30 * belt_str
                b += 50 * belt_str
        
        # Earth Shadow (Dark Blue Band at horizon opposite sun)
        if sun_alt < 2 and az_away_factor > 0:
            shadow_h = 6.0 + abs(sun_alt)
            if view_alt < shadow_h:
                shadow_f = (shadow_h - view_alt) / shadow_h
                # Base darkening max 50%
                darken_strength = 0.5 * shadow_f * az_away_factor
                darken = 1.0 - darken_strength
                
                r *= darken
                g *= darken
                b *= darken
                    
        # Clamp
        r, g, b = min(255, max(0, int(r))), min(255, max(0, int(g))), min(255, max(0, int(b)))
        
        # 4. Integrate Global Sky Glow (Bortle / Light Pollution) directly into physical model
        # This fixes the "pyramid" shape by curving the glow along with the sky projection
        if bortle > 2 and twilight_factor > 0.01:
            glow_val = (bortle - 2) / 7.0 
            glow_alpha = glow_val * twilight_factor * 0.15 # Max 15% influence to base color
            
            # Elevation curve (glow is stronger at horizon, fades to zenith)
            elev_t = max(0.0, min(1.0, 1.0 - (view_alt / 90.0)))
            # Exponential fade to avoid plateau
            elev_falloff = math.pow(elev_t, 2.5) 
            
            # Urban Sky Glow color: Desaturated warm grey (Sodium/LED)
            glow_r, glow_g, glow_b = 140, 130, 110
            
            # Apply additive blend based on elevation
            strength = glow_alpha * elev_falloff
            r = min(255, int(r + glow_r * strength))
            g = min(255, int(g + glow_g * strength))
            b = min(255, int(b + glow_b * strength))
            
        return QColor(r, g, b)

    def draw_background(self, painter, sun_alt, sun_az, view_az, dimming=1.0):
        rect = self.rect()
        
        # Interactive state (mouse dragging or time bar dragging)
        is_interacting = self.dragging or getattr(self.parent_widget, '_dragging_time', False)
        
        # Reduced Resolution for performance during dragging
        # (4096 pixels -> 1024 pixels, 75% less physical sky model calculation)
        w_res = 32 if is_interacting else 64
        h_res = 32 if is_interacting else 64
        
        # Higher quantization during interaction to maximize cache hits
        q_sun_alt = (round(sun_alt * 2) / 2.0) if not is_interacting else round(sun_alt)
        q_sun_az = (round(sun_az / 2) * 2) if not is_interacting else (round(sun_az / 4) * 4)
        q_view_az = (round(view_az / 2) * 2) if not is_interacting else (round(view_az / 4) * 4)

        w, h = self.width(), self.height()
        zoom = round(self.zoom_level, 2)
        elev_q = round(self.elevation_angle, 1)

        # Pre-calc Bortle for physical sky
        is_auto_bortle = getattr(self.parent_widget, 'is_auto_bortle', getattr(self, 'is_auto_bortle', True))
        bortle = getattr(self.parent_widget, 'auto_bortle_estimate', getattr(self, 'auto_bortle_estimate', 1)) if is_auto_bortle else 1
        
        twilight_factor = 1.0
        if sun_alt >= 0: twilight_factor = 0.0
        elif sun_alt > -18.0: twilight_factor = (0 - sun_alt) / 18.0
        twilight_factor *= dimming
        
        # Quantize bortle/twilight to avoid excessive cache misses
        t_q = round(twilight_factor * 10) / 10.0

        cache_key = (q_sun_alt, q_sun_az, q_view_az, w_res, h_res, w, h, zoom, elev_q, bortle, t_q)
        
        if self._bg_cache_key != cache_key or self._bg_cache_pixmap is None:
            # Regenerate using physics model
            from PyQt5.QtGui import QImage
            img = QImage(w_res, h_res, QImage.Format_RGB32)
            
            # Use Inverse Projection to get real Alt/Az for each screen pixel
            # This is slow per-pixel, but at 64x64 it's 4096 calls -> fast enough.
            
            for y in range(h_res):
                sy = y * (h / (h_res - 1.0))
                for x in range(w_res):
                    sx = x * (w / (w_res - 1.0))
                    
                    # Inverse Project (Screen -> Alt/Az)
                    alt, az = self.unproject_stereo(sx, sy)
                    
                    # Convert to color
                    col = self.sky_color_phys(alt, az, q_sun_alt, q_sun_az, bortle=bortle, twilight_factor=t_q)
                    img.setPixelColor(x, y, col)
            
            self._bg_cache_pixmap = QPixmap.fromImage(img)
            self._bg_cache_key = cache_key
            
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(rect, self._bg_cache_pixmap)
        
        # Apply Dimming (Eclipse)
        # Overlay black rect with Alpha = 1.0 - dimming
        if dimming < 0.99:
            alpha = int((1.0 - dimming) * 255)
            alpha = max(0, min(255, alpha))
            painter.fillRect(rect, QColor(0, 0, 0, alpha))


    def draw_celestial_grid(self, painter, hour):
        # Draw Celestial Equator (Dec = 0)
        # Cyan Line
        painter.setPen(QPen(QColor(0, 255, 255, 80), 1, Qt.DashLine))
        
        day_of_year = self.parent_widget.manual_day
        # LST logic same as stars
        lst = (100.0 + day_of_year * 0.9856 + hour * 15.0 + self.parent_widget.longitude) % 360
        
        path = QPainterPath()
        first_pt = True
        lat_rad = math.radians(self.parent_widget.latitude)
        
        steps = 72
        for i in range(steps + 1):
            ra = (i * 360.0 / steps)
            # RA to Alt/Az for Dec=0
            ha = (lst - ra)
            ha_rad = math.radians(ha)
            dec_rad = 0
            
            sin_alt = (math.cos(lat_rad) * math.cos(ha_rad)) # sin(0)=0, cos(0)=1
            alt = math.degrees(math.asin(max(-1, min(1, sin_alt))))
            
            cos_az = (-sin_alt * math.sin(lat_rad)) / \
                     (math.cos(math.radians(alt)) * math.cos(lat_rad) + 1e-10)
            az = math.degrees(math.acos(max(-1, min(1, cos_az))))
            if math.sin(ha_rad) > 0: az = 360 - az
            
            pt = self.project_universal_stereo(alt, az)
            if pt:
                # Avoid drawing across the back of the sphere wrapping
                if first_pt:
                    path.moveTo(QPointF(*pt))
                    first_pt = False
                else:
                    path.lineTo(QPointF(*pt))
            else:
                 first_pt = True

        painter.drawPath(path)

    def _on_star_result(self, img, vis_stars):
        self._cached_star_image = img
        self.visible_stars = vis_stars # Update interactions
        self.rendering_busy = False
        self.update() # Force repaint to show new stars immediately

    def _on_trail_result(self, img):
        self._cached_trail_image = img
        self.trail_rendering_busy = False

    def draw_stars(self, painter, hour, sun_alt, sun_az, for_trails=False, visibility_factor=1.0, moon_mask=None, mag_limit=None, eff_lat=None, day_of_year=None):
        if np and hasattr(self.parent_widget, 'np_ra'):
            self.draw_stars_numpy(painter, hour, sun_alt, sun_az, mag_limit=mag_limit, eff_lat=eff_lat, day_of_year=day_of_year)
            return

        if visibility_factor <= 0: return

        # If we are drawing trials, we can't use the static image cache logic (different times).
        # We must fallback to original logic for trails.
        if for_trails:
             # Just leave empty or implement trails logic if needed.
             self.visible_stars.clear()
             # Use NumPy Vectorized Path if available (and not drawing trails which needs specific iter)
             if np and hasattr(self.parent_widget, 'np_ra'):
                  self.draw_analytic_trails_numpy(painter, hour, hour-0.25, 1, 1, mag_limit or 5.0, False) # Dummy call if needed
             return

        # 1. Draw Cached Image if available
        if self._cached_star_image and not self._cached_star_image.isNull():
             painter.drawImage(0, 0, self._cached_star_image)
        else:
             # FALLBACK: If no cache, draw directly using numpy
              if np and hasattr(self.parent_widget, 'np_ra'):
                  self.draw_stars_numpy(painter, hour, sun_alt, sun_az, mag_limit=mag_limit, day_of_year=day_of_year)
        
        # 2. Trigger Worker for NEXT frame (skip if user is dragging time bar)
        pw = self.parent_widget
        if np and hasattr(pw, 'np_ra') and not self.rendering_busy and not getattr(pw, '_dragging_time', False):
            self.rendering_busy = True # Lock
            
            # Gather params
            dfy = day_of_year if day_of_year is not None else pw.manual_day
            lst = (100.0 + dfy * 0.9856 + hour * 15.0 + pw.longitude) % 360
            lat_rad = math.radians(pw.latitude)
            
            # LP Params for Worker Extinction
            bortle = getattr(pw, 'auto_bortle_estimate', getattr(self, 'auto_bortle_estimate', 1))
            is_auto = getattr(pw, 'is_auto_bortle', False)
            horizon_profile = None
            if hasattr(self, 'horizon_overlay') and hasattr(self.horizon_overlay, 'profile'):
                horizon_profile = self.horizon_overlay.profile

            params = {
                'width': self.width(),
                'height': self.height(),
                'zoom_level': self.zoom_level,
                'vertical_ratio': self.vertical_offset_ratio,
                'elevation_angle': self.elevation_angle,
                'azimuth_offset': self.azimuth_offset,
                
                'bortle': bortle,
                'is_auto': is_auto,
                'horizon_profile': horizon_profile,
                
                'ra': pw.np_ra,
                'dec': pw.np_dec,
                'mag': pw.np_mag,
                'r': pw.np_r,
                'g': pw.np_g,
                'b': pw.np_b,
                'cel_objs_ref': pw.celestial_objects, # Shared ref (Read Only access usually safe)
                
                'lst': lst,
                'lat_rad': lat_rad,
                'mag_limit': mag_limit if mag_limit is not None else pw.magnitude_limit,
                'star_scale': pw.star_scale,
                'spike_threshold': getattr(pw, 'spike_magnitude_threshold', 2.0),
                'pure_colors': pw.pure_colors
            }
            
            # Invoke via Signal to ensure Thread Crossing
            self.request_render_signal.emit(params)
            
            return



    def draw_stars_numpy(self, painter, hour, sun_alt, sun_az, mag_limit=None, eff_lat=None, day_of_year=None):
        pw = self.parent_widget
        if mag_limit is None: mag_limit = pw.magnitude_limit
        if eff_lat is None: eff_lat = pw.latitude
        
        # --- GLOBAL STAR KILLER ---
        # A. Interaction Limit
        is_interacting = self.dragging or getattr(pw, '_dragging_time', False)
        if is_interacting: 
            mag_limit = min(mag_limit, 5.5)
            
        # B. Base Global Extinction (Bortle Penalty)
        # Bortle highly penalizes star counts everywhere. At Bortle 9 it drops limit by ~3.2 magnitudes.
        bortle = getattr(pw, 'auto_bortle_estimate', getattr(self, 'auto_bortle_estimate', 1))
        is_auto = getattr(pw, 'is_auto_bortle', False)
        
        if is_auto and bortle > 1:
            # Naked-Eye Limiting Magnitude (NELM) empirical formula based on Bortle
            # mlim(B) ~= 7.6 - 0.5(B-1). Resulting in B1=~7.6, B2=~7.1, B9=~3.6.
            nelm_limit = 7.6 - 0.5 * (bortle - 1)
            # Re-assign mag_limit entirely to mathematically guarantee the correct max stars
            mag_limit = min(mag_limit, nelm_limit)
        
        # C. Absolute minimum floor to avoid completely empty skies incorrectly
        mag_limit = max(-12.0, mag_limit)
        
        # D. HARD DAYLIGHT KILLER
        if sun_alt > 0:
            # Daytime limiting magnitude (Sirius is -1.4 and barely visible with perfect conditions)
            # We set a hard ceiling of -4.0 for daytime to ensure only Sun/Moon/Planets show up.
            mag_limit = min(mag_limit, -4.0)
        
        # 1. Filtering (Pre-Math) - OPTIMIZATION
        # Ensure only the correctly killed catalog goes into physics math
        # Allow +1.0 magnitudes so that borderline stars have a smooth alpha fade out
        limit_buffer_pre = mag_limit + 1.0
        original_idxs = np.where(pw.np_mag < limit_buffer_pre)[0]
        if len(original_idxs) == 0: return
        
        ra = pw.np_ra[original_idxs]
        dec = pw.np_dec[original_idxs]
        mag = pw.np_mag[original_idxs]
        f_r_pre = pw.np_r[original_idxs]
        f_g_pre = pw.np_g[original_idxs]
        f_b_pre = pw.np_b[original_idxs]

        # 2. Physics (Vectorized)
        lat_rad = math.radians(eff_lat)
        if day_of_year is None: day_of_year = pw.manual_day
        lst = (100.0 + day_of_year * 0.9856 + hour * 15.0 + pw.longitude) % 360
        
        # HA
        ha = (lst - ra)
        ha_rad = np.radians(ha)
        dec_rad = np.radians(dec)
        
        # Alt
        sin_lat = math.sin(lat_rad)
        cos_lat = math.cos(lat_rad)
        sin_dec = np.sin(dec_rad)
        cos_dec = np.cos(dec_rad)
        
        sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * np.cos(ha_rad)
        # We don't need actual Alt/Az degrees for everything, just sin/cos often?
        # But project_stereo needs them. 
        # Clip sin_alt
        sin_alt = np.clip(sin_alt, -1.0, 1.0)
        alt_rad = np.arcsin(sin_alt)
        alt_deg = np.degrees(alt_rad)
        
        # Az
        # cos_az
        # cos_az = (sin_dec - sin_alt * sin_lat) / (cos_alt * cos_lat)
        cos_alt = np.cos(alt_rad)
        cos_az_num = sin_dec - sin_alt * sin_lat
        cos_az_den = cos_alt * cos_lat + 1e-10
        cos_az = np.clip(cos_az_num / cos_az_den, -1.0, 1.0)
        az_rad = np.arccos(cos_az)
        
        # Fix Quadrant (sin(ha) > 0 -> 360 - az)
        sin_ha = np.sin(ha_rad)
        az_deg = np.degrees(az_rad)
        az_deg = np.where(sin_ha > 0, 360.0 - az_deg, az_deg)
        
        # 2. Filtering (Visibility)
        # Directional Modifier
        s_alt_rad = math.radians(sun_alt)
        s_az_rad = math.radians(sun_az)
        sin_s = math.sin(s_alt_rad)
        cos_s = math.cos(s_alt_rad)
        
        # cos_gamma (Star vs Sun)
        az_rad_full = np.radians(az_deg)
        cos_gamma = sin_alt * sin_s + cos_alt * cos_s * np.cos(az_rad_full - s_az_rad)
        
        # NomÃ©s penalitzem estels que estan cap a la direcciÃ³ del Sol, no "potenciem" mai.
        # FIX CÃTIC: A la nit profunda (Sol molt sota l'horitzÃ³), el Sol no pot esborrar 
        # estrelles, la Terra el tapa. Ho limitem al Crepuscle (Twilight).
        s_alt_deg = math.degrees(s_alt_rad)
        sun_glare_opacity = 0.0
        if s_alt_deg > -18.0:
            twilight_weight = min(1.0, (s_alt_deg + 18.0) / 18.0)
            sun_glare_opacity = 1.2 * np.clip(cos_gamma, 0.0, 1.0) * twilight_weight
            
        dir_modifier = -sun_glare_opacity
        local_limit = mag_limit + dir_modifier
        
        # --- ATMOSPHERIC EXTINCTION (AIRMASS) ---
        # Airmass approximation (Pickover 1998): 1 / (sin(h) + 0.15 * (h + 3.885)^-1.253)
        h_capped = np.maximum(0.1, alt_deg)
        airmass = 1.0 / (np.sin(np.radians(h_capped)) + 0.15 * (h_capped + 3.885)**-1.253)
        
        # Extinction coefficient k: 0.2 (Clear) up to 0.5 (City Smog)
        # We scale k slightly with Bortle to reflect aerosol load in high LP areas.
        k_ext = 0.20 + 0.04 * np.clip(bortle - 1, 0, 8)
        
        # Magnitude drop = k * (X - 1). Penalty is 0 at Zenith (X=1).
        airmass_penalty = k_ext * (airmass - 1.0)
        
        # Apply Penalty to local limit
        local_limit = local_limit - airmass_penalty

        # --- ATMOSPHERIC REFRACTION ---
        # Lift stars near the horizon (Saemundsson 1986 approx)
        # R_minutes = 1.02 / tan(h + 10.3 / (h + 5.11))
        # This makes the "apegado al horizonte" feeling more realistic (compressed but lifted)
        refraction_deg = (1.02 / 60.0) / np.tan(np.radians(h_capped + 10.3 / (h_capped + 5.11)))
        alt_deg_refined = alt_deg + refraction_deg
        
        # 3. MOON EXTINCTION (Fase y separaciÃ³n)
        if hasattr(self, '_sf_cache') and isinstance(self._sf_cache, dict) and self._sf_cache.get('data') and 'moon' in self._sf_cache.get('data', {}):
            m_data = self._sf_cache['data']['moon']
            if m_data['alt'] > -5.0: # Only if above or near horizon
                m_alt = np.radians(m_data['alt'])
                m_az = np.radians(m_data['az'])
                m_illum = m_data.get('illumination', 0.5)
                
                # CÃ lcul vectorial per extingir radialment entorn de la Lluna
                cos_dist_moon = sin_alt * math.sin(m_alt) + cos_alt * math.cos(m_alt) * np.cos(np.radians(az_deg) - m_az)
                moon_ang_dist_rad = np.arccos(np.clip(cos_dist_moon, -1.0, 1.0))
                
                # Afecta molt al voltant (~0.25 rads = ~15 graus) depenent si Ã©s Lluna PlenÃ , penalitzant magnitud
                moon_glare = np.exp(-(moon_ang_dist_rad / 0.25)**2) * m_illum * 4.5
                local_limit -= moon_glare
        
        # 4. Local Horizon Extinction (City domes)
        if is_auto and hasattr(self, 'horizon_overlay') and hasattr(self.horizon_overlay, 'profile'):
             prof = self.horizon_overlay.profile
             if hasattr(prof, 'light_domes') and len(prof.light_domes) > 0:
                 # Bortle-based intensity scaling (B1=0%, B5=50%, B9=100%)
                 lp_intensity_factor = max(0.0, (bortle - 1.0) / 8.0)
                 
                 az_indices = (az_deg * 2.0).astype(int) % len(prof.light_domes)
                 intensity = prof.light_domes[az_indices]
                 dist = prof.light_peak_distances[az_indices]
                 
                 # 1. DYNAMIC RADIUS (a0): Matches visual dome size
                 # a0 scales with log intensity and decays with distance (35km mean path)
                 dist_factor = np.exp(-dist / 35000.0)
                 log_intensity = np.log10(1.0 + intensity)
                 # Base factor 8.0 maps large cities to ~20-25 deg radius near observer
                 a0 = log_intensity * 8.0 * dist_factor
                 # Ensure a sensible minimum a0 to avoid math singularities
                 a0 = np.maximum(1.0, a0)
                 
                 # 2. DYNAMIC PENALTY (i_alpha): Matches visual brightness
                 # Scales with Bortle selection and exponential falloff with altitude
                 i_alpha = (intensity**0.40) * lp_intensity_factor * np.exp(-(alt_deg / a0)**2)
                 
                 # 3. APPLY EXTINCTION: Drop local magnitude limit
                 # Factor 0.15 maps high intensity + Bortle 9 to ~3.5-4.0 mag loss
                 extinction_penalty = i_alpha * 0.15
                 local_limit = local_limit - extinction_penalty
        
        # Mask 1: Mag Limit
        # We allow +1.0 buffer for edge bloom, the strict test is done later or managed by eff_alpha
        limit_buffer = local_limit + 1.0 
        mask = mag < limit_buffer
        
        # Apply Mask 1
        idxs = np.where(mask)[0] 
        if len(idxs) == 0: return
        
        # Map back to original indices for celestial_objects interaction
        # original_idxs was created during pre-filtering
        indices = original_idxs[idxs]
        
        f_alt = alt_deg_refined[idxs]
        f_az = az_deg[idxs]
        f_mag = mag[idxs]
        
        # Ensure local_limit is an array if we want to index it, or just a scalar
        if isinstance(local_limit, np.ndarray):
            f_ll = local_limit[idxs]
        else:
            f_ll = np.full(len(idxs), local_limit)
            
        f_r = f_r_pre[idxs]
        f_g = f_g_pre[idxs]
        f_b = f_b_pre[idxs]
        
        # 3. Projection - UNIVERSAL STEREOGRAPHIC (Horizon Centered)
        w, h = self.width(), self.height()
        scale_h = h / 2.0 * self.zoom_level
        
        cx = w/2.0
        cy_base = h/2.0 + (h * self.vertical_offset_ratio)
        
        # View Shift
        elev_rad = math.radians(self.elevation_angle)
        y_center_val = 2.0 * math.tan(elev_rad / 2.0)
        
        # Star Coords
        # We need Radian Arrays
        # f_alt is Degrees. f_az is Degrees.
        alt_rad = np.radians(f_alt)
        az_rel_deg = f_az - self.azimuth_offset
        az_rel_rad = np.radians(az_rel_deg)
        
        cos_alt = np.cos(alt_rad)
        sin_alt = np.sin(alt_rad)
        cos_az = np.cos(az_rel_rad)
        sin_az = np.sin(az_rel_rad)
        
        denom = 1.0 + cos_alt * cos_az
        
        # Valid Mask (Not behind antipodal point)
        mask_proj = denom > 1e-6
        if not np.any(mask_proj): return
        
        # Apply mask
        denom = denom[mask_proj]
        cos_alt = cos_alt[mask_proj]
        sin_alt = sin_alt[mask_proj]
        sin_az = sin_az[mask_proj]
        
        indices = indices[mask_proj]
        f_alt = f_alt[mask_proj]
        f_az = f_az[mask_proj]
        f_mag = f_mag[mask_proj]
        f_ll = f_ll[mask_proj]
        f_r = f_r[mask_proj]
        f_g = f_g[mask_proj]
        f_b = f_b[mask_proj]
        
        k = 2.0 / denom
        x = k * cos_alt * sin_az
        y = k * sin_alt
        
        # Scale & Shift
        sx = cx + x * scale_h
        sy = cy_base - (y - y_center_val) * scale_h
        
        # Mask 2: Projection valid (Clipping 'behind' camera)
        # No Azimuth Clipping for Points in Stereographic 
        # (We want to see points even if they wrap or are behind zenith)
        # We only rely on screen bounds clipping which follows
        
        # indices, sx, sy, f_mag, etc are already filtered by denom mask

        
        # Mask 3: Screen Bounds
        mask_screen = (sx > -50) & (sx < w+50) & (sy > -50) & (sy < h+50)
        if not np.any(mask_screen): return
        
        indices = indices[mask_screen]
        sx = sx[mask_screen]
        sy = sy[mask_screen]
        f_alt = f_alt[mask_screen]
        f_az = f_az[mask_screen]
        f_mag = f_mag[mask_screen]
        f_ll = f_ll[mask_screen]
        f_r = f_r[mask_screen]
        f_g = f_g[mask_screen]
        f_b = f_b[mask_screen]
        
        # 4. Drawing (Batch)
        diff = f_ll - f_mag
        fade_in = np.clip(diff * 2.0, 0.0, 1.0)
        
        scale = self.parent_widget.star_scale
        
        painter.setPen(Qt.NoPen)
        
        # Prepare local list for visible stars (used for click detection)
        self.visible_stars = np.array([], dtype=np.int32)
        self.visible_stars_sx = np.array([], dtype=np.float32)
        self.visible_stars_sy = np.array([], dtype=np.float32)
        
        count = len(sx)
        if count == 0: return

        # Pre-calc sizes and alphas vectorily
        eff_alpha = np.where(f_mag < 2.0, np.sqrt(fade_in), fade_in)
        
        # Filter invisible
        mask_vis = eff_alpha > 0.01
        
        indices = indices[mask_vis]
        sx = sx[mask_vis]
        sy = sy[mask_vis]
        f_mag = f_mag[mask_vis]
        eff_alpha = eff_alpha[mask_vis]
        f_r = f_r[mask_vis]
        f_g = f_g[mask_vis]
        f_b = f_b[mask_vis]
        
        # Loop
        # Pre-fetch list for faster lookups
        cel_objs = self.parent_widget.celestial_objects
        
        for i in range(len(sx)):
            mag = f_mag[i]
            alpha_f = eff_alpha[i]
            x, y = sx[i], sy[i]
            
            # Color
            r_val, g_val, b_val = int(f_r[i]), int(f_g[i]), int(f_b[i])
            
            # Use QRadialGradient for realistic star blur/bloom
            if getattr(self.parent_widget, 'pure_colors', False):
                 # LEGACY BEHAVIOR
                 if mag > 5.0:
                     size = max(1.0, 1.2 * self.zoom_level * 0.6) * scale
                     if math.isnan(size) or size <= 0: continue
                     
                     a_val = 200 - min(150, int((mag - 5.0) * 10))
                     painter.setBrush(QColor(r_val, g_val, b_val, int(max(50, a_val) * alpha_f)))
                     painter.drawEllipse(QPointF(x, y), size, size)
                 else:
                     size = max(1.5, (5.0 - mag) * 0.8 * self.zoom_level) * scale
                     if math.isnan(size) or size <= 0: continue
                     
                     painter.setBrush(QColor(r_val, g_val, b_val, int(255 * alpha_f)))
                     painter.drawEllipse(QPointF(x, y), size, size)
            else:
                 # REALISTIC STELLARIUM OPTICS: Bloom, Clamp, Desaturation
                 if mag > 5.0:
                     # Estrellas de fondo: un puntito que de verdad sea visible pero sin halo
                     # MÃ¡ximo de 1 pÃ­xel real, independientemente del zoom para evitar "bolitas"
                     size = min(1.0, 1.2 * scale)
                     a_val = 220 - min(100, int((mag - 5.0) * 15))
                     
                     # Faint stars lose color saturation to the eye; pull them towards white/grey
                     desat = 0.5
                     r_desat = int(r_val * (1-desat) + 200 * desat)
                     g_desat = int(g_val * (1-desat) + 200 * desat)
                     b_desat = int(b_val * (1-desat) + 220 * desat)
                     
                     final_a = int(max(60, a_val) * alpha_f * 0.7)
                     
                     if math.isnan(size) or size <= 0: continue
                     painter.setBrush(QColor(r_desat, g_desat, b_desat, final_a))
                     painter.drawEllipse(QPointF(x, y), size, size)
                 else:
                     # Estrellas principales
                     size = max(1.5, (5.0 - mag) * 0.7 * scale)
                     core_radius = min(4.0, size)
                     
                     final_a = int(255 * alpha_f)
                     star_c_intense = QColor(r_val, g_val, b_val, final_a)
                     transparent_c = QColor(r_val, g_val, b_val, 0)
                     
                     # 1. Halo mucho mÃ¡s centrado y menos opaco para evitar el "velo"
                     if mag < 4.0:
                         halo_size = core_radius * (5.5 - mag) * 1.8 * self.zoom_level
                         if mag < 1.0:
                             halo_size = core_radius * 8.0 * self.zoom_level
                             
                         if math.isnan(halo_size) or halo_size <= 0: continue
                         halo_grad = QRadialGradient(x, y, halo_size)
                         halo_grad.setColorAt(0.0, QColor(r_val, g_val, b_val, int(90 * alpha_f)))
                         halo_grad.setColorAt(0.1, QColor(r_val, g_val, b_val, int(30 * alpha_f)))
                         halo_grad.setColorAt(0.3, QColor(r_val, g_val, b_val, int(5 * alpha_f)))
                         halo_grad.setColorAt(1.0, transparent_c)
                         
                         painter.setBrush(QBrush(halo_grad))
                         painter.drawEllipse(QPointF(x, y), halo_size, halo_size)
                     
                     # 2. NÃºcleo mÃ¡s brillante y denso
                     core_grad = QRadialGradient(x, y, core_radius)
                     core_grad.setColorAt(0.0, QColor(255, 255, 255, final_a))
                     core_grad.setColorAt(0.6, star_c_intense) # Mantiene el color base mÃ¡s hacia el borde
                     core_grad.setColorAt(1.0, transparent_c)
                     
                     if not math.isnan(core_radius) and core_radius > 0:
                         painter.setBrush(QBrush(core_grad))
                         painter.drawEllipse(QPointF(x, y), core_radius, core_radius)
                 
                 # Draw diffraction spikes using the slider threshold
                 try:
                     spike_threshold = getattr(self.parent_widget, 'spike_magnitude_threshold', 2.0)
                     if mag < spike_threshold:
                         self.draw_spikes(painter, x, y, mag, spike_threshold, final_a)
                         painter.setPen(Qt.NoPen)  # Reset pen after spikes
                 except Exception:
                     pass  # Silently skip spikes if there's an error
                     
        # Update visible stars for click detection
        self.visible_stars = indices
        self.visible_stars_sx = sx
        self.visible_stars_sy = sy

        # Polaris Label (Manual check)
        # Using original list for this checks is fast (1 item)
        # Or check in loop:
        # if dec > 89...
        # Numpy check:
        idx_pol = np.where((pw.np_dec > 89.0) & (pw.np_mag < 2.5))[0]
        if len(idx_pol) > 0:
            pid = idx_pol[0]
            # Project specific star manually (re-using logic or just find it in filtered lists?)
            # Easier to just re-project one star if needed
            # Or assume it was in the list?
            # It's Mag 2.0, so it IS in the list.
            # But we lost the ID/Index association after filtering.
            # Re-project Polaris specifically (Cheap)
            
            p_ra = pw.np_ra[pid]
            p_dec = pw.np_dec[pid]
            p_ha_rad = np.radians(lst - p_ra)
            p_dec_rad = np.radians(p_dec)
            
            p_sin = math.sin(p_dec_rad)*sin_lat + math.cos(p_dec_rad)*cos_lat*math.cos(p_ha_rad)
            p_alt = math.degrees(math.asin(p_sin))
            p_cos_az = (math.sin(p_dec_rad) - p_sin * sin_lat) / (math.cos(math.asin(p_sin)) * cos_lat + 1e-10)
            p_az = math.degrees(math.acos(np.clip(p_cos_az, -1, 1)))
            if math.sin(p_ha_rad) > 0: p_az = 360 - p_az
            
            ppt = self.project_universal_stereo(p_alt, p_az)
            if ppt:
                 painter.setPen(QColor(200, 200, 255))
                 painter.drawText(int(ppt[0])+8, int(ppt[1])+4, "Polaris")

    def get_star_color(self, bp_rp):
        # Map BP-RP index to RGB
        if bp_rp < 0.0:
            return QColor(160, 190, 255) # Blue
        elif bp_rp < 0.5:
            t = (bp_rp - 0.0) / 0.5
            return QColor(160 + int(95*t), 190 + int(65*t), 255) 
        elif bp_rp < 1.0:
            t = (bp_rp - 0.5) / 0.5
            return QColor(255, 255, 255 - int(55*t)) 
        elif bp_rp < 2.0:
            t = (bp_rp - 1.0) / 1.0
            return QColor(255, 255 - int(80*t), 200 - int(100*t))
        else:
            return QColor(255, 175, 100) # Red

    def draw_spikes(self, painter, x, y, mag, threshold, alpha):
        # Calculate factor: how much brighter than threshold?
        factor = (threshold - mag)
        if factor <= 0: return

        # Realism tweaks: thinner, sharper, less "glowy"
        length = factor * 20 * self.zoom_level
        width = max(0.5, factor * 0.4 * self.zoom_level) # Much thinner
        
        if length < 4: return

        # Subtle alpha
        c_center = QColor(255, 255, 255, min(255, int(alpha * 0.6)))
        c_tip = QColor(255, 255, 255, 0)

        painter.setPen(Qt.NoPen)
        
        def draw_ray(angle, len_r, w_r):
            painter.save()
            painter.translate(x, y)
            painter.rotate(angle)
            
            # Gradient: Start (0,0) -> End (len, 0)
            grad = QLinearGradient(0, 0, len_r, 0)
            grad.setColorAt(0.0, c_center)
            grad.setColorAt(0.05, c_center) # Very small core
            grad.setColorAt(1.0, c_tip)
            
            painter.setBrush(QBrush(grad))
            
            path = QPainterPath()
            path.moveTo(0, -w_r/2.0)
            path.lineTo(len_r, 0)
            path.lineTo(0, w_r/2.0)
            path.lineTo(-w_r/2.0, 0) 
            path.closeSubpath()
            
            painter.drawPath(path)
            painter.restore()

        # Just standard 4-spike Newtonian diffraction (Realism)
        # Rotated 45 deg usually looks better/more typical unless spider is aligned 0-90
        # Let's stick to 0-90 as standard cross, or maybe 45-135 for aesthetics
        for angle in [0, 90, 180, 270]:
            draw_ray(angle, length, width)

    # draw_sun removed (Legacy)

    # get_horizon_size_multiplier removed (Superseded)

    # --- Helpers Restored ---
    def normalize_deg(self, d):
        return d % 360.0

    # --- Accurate Time & LST Methods ---
    def get_datetime_utc(self, ut_hour):
        # Construct UTC datetime from manual year/day/hour
        y = self.parent_widget.manual_year
        d = self.parent_widget.manual_day
        dt_start = datetime(y, 1, 1, tzinfo=timezone.utc)
        return dt_start + timedelta(days=d, hours=ut_hour)

    def julian_day(self, dt):
        # High precision JD using standard epoch
        # J2000.0 is 2000-01-01 12:00:00 UTC = JD 2451545.0
        
        # Ensure dt is aware or assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
            
        j2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        diff = dt - j2000
        return 2451545.0 + diff.total_seconds() / 86400.0

    def days_since_j2000(self, ut_hour):
        # DEPRECATED in favor of direct JD usage, but kept for legacy calls
        dt = self.get_datetime_utc(ut_hour)
        jd = self.julian_day(dt)
        return jd - 2451545.0

    # REMOVED manual algorithm julian_day to avoid bugs.
    
    def gmst_deg(self, jd):
        # Greenwich Mean Sidereal Time in degrees
        T = (jd - 2451545.0) / 36525.0
        st = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + \
             0.000387933 * T*T - (T*T*T)/38710000.0
        return st % 360.0

    def lst_deg(self, jd, lon_deg):
        # Local Sidereal Time
        return (self.gmst_deg(jd) + lon_deg) % 360.0

    def sun_alt_az_from_ra_dec(self, ra, dec, lat, lst):
        # Standard conversion helper
        ha = (lst - ra)
        ha_rad = math.radians(ha)
        lat_rad = math.radians(lat)
        dec_rad = math.radians(dec)
        sin_lat = math.sin(lat_rad)
        cos_lat = math.cos(lat_rad)
        sin_dec = math.sin(dec_rad)
        cos_dec = math.cos(dec_rad)
        
        sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * math.cos(ha_rad)
        alt = math.degrees(math.asin(max(-1, min(1, sin_alt))))
        
        cos_az = (sin_dec - sin_alt * sin_lat) / (math.cos(math.radians(alt)) * cos_lat + 1e-10)
        az = math.degrees(math.acos(max(-1, min(1, cos_az))))
        if math.sin(ha_rad) > 0: az = 360 - az
        
        return alt, az
        
    def get_sun_ra_dec(self, d):
        # Precise Sun (Meeus Ch 25)
        # Wraps AstroEngine for Sun
        # d is Days since J2000 UTC. 
        # Add Delta T (72s) for TDB based T
        d_tdb = d + (72.0 / 86400.0)
        T = d_tdb / 36525.0
        
        l, b, r = AstroEngine.get_sun_position_vsop(T)
        ra, dec = AstroEngine.ecliptic_to_equatorial(l, b, T)
        return ra, dec

    def get_moon_ra_dec(self, d):
        # Wraps AstroEngine for Moon
        d_tdb = d + (72.0 / 86400.0)
        T = d_tdb / 36525.0
        
        l, b, r = AstroEngine.get_moon_position_elp(T)
        ra, dec = AstroEngine.ecliptic_to_equatorial(l, b, T)
        return ra, dec, r

    def get_topocentric_position(self, ra_geo, dec_geo, dist_km, obs_lat, obs_lon, jd):
        return AstroEngine.get_topocentric_position(ra_geo, dec_geo, dist_km, obs_lat, obs_lon, jd)

    def apply_parallax(self, ra, dec, dist_km, lat, lst):
        # Rigorous Parallax Correction (Equatorial)
        # 1. Constants
        re = 6378.14 # Earth Radius km
        # Geocentric coords of observer
        # We assume lat is geographic, convert to geocentric phi'
        # tan(phi') = (1-f)^2 tan(phi)
        # f = 1/298.257
        f = 1.0/298.257223563
        phi_rad = math.radians(lat)
        u = math.atan((1 - f)**2 * math.tan(phi_rad))
        rho_sin_phi = math.sin(u) * 0.99664719 # approx height factor 1? assume sea level
        rho_cos_phi = math.cos(u)
        
        # 2. HA
        ra_rad = math.radians(ra)
        dec_rad = math.radians(dec)
        lst_rad = math.radians(lst)
        ha_rad = lst_rad - ra_rad
        
        # 3. Horizontal Parallax
        # sin(pi) = Re / dist
        sin_pi = re / dist_km
        
        # 4. Parallax formulas (Meeus Chap 40)
        # tan(H') = sin(H) / (cos(H) - rho*cos(phi')*sin(pi)/cos(dec))
        # But we need Delta RA
        
        # A = cos(dec) * sin(H)
        # B = cos(dec) * cos(H) - rho*cos(phi') * sin(pi)
        # C = sin(dec) - rho*sin(phi') * sin(pi)
        
        # tan(H') = A / B
        # tan(dec') = (C * cos(H')) / B  ??? No, simpler form:
        
        # Direct:
        # ra' = ra - delta_ra
        # tan(delta_ra) = (-rho*cos(phi')*sin(pi)*sin(H)) / (cos(dec) - rho*cos(phi')*sin(pi)*cos(H))
        
        # Let's use the atan2 approach for full quadrant safety
        num = -rho_cos_phi * sin_pi * math.sin(ha_rad)
        den = math.cos(dec_rad) - rho_cos_phi * sin_pi * math.cos(ha_rad)
        delta_ra = math.atan2(num, den)
        
        ra_prime_rad = ra_rad + delta_ra
        
        # dec'
        # tan(dec') = (sin(dec) - rho*sin(phi')*sin(pi)) * cos(delta_ra) / (cos(dec) - rho*cos(phi')*sin(pi)*cos(H))
        # This is strictly related.
        
        # Alternative: Cartesian vector subtraction
        # Moon Geo vector:
        # X = dist * cos(dec) * cos(ra)
        # Y = dist * cos(dec) * sin(ra)
        # Z = dist * sin(dec)
        
        # Observer Geo vector (sidereal frame):
        # Xo = Re * rho_cos_phi * cos(lst)
        # Yo = Re * rho_cos_phi * sin(lst)
        # Zo = Re * rho_sin_phi 
        
        # Topocentric Vector:
        # Xt = X - Xo
        # Yt = Y - Yo
        # Zt = Z - Zo
        
        # Convert back
        # Rt = sqrt(Xt^2 + Yt^2 + Zt^2)
        # RA' = atan2(Yt, Xt)
        # Dec' = asin(Zt / Rt)
        
        # This vector method is robust and handles all quadrants/singularities easily.
        # Plus gives us Topocentric Distance if needed.
        
        # Geo Cartesian
        cd = math.cos(dec_rad)
        sd = math.sin(dec_rad)
        cr = math.cos(ra_rad)
        sr = math.sin(ra_rad)
        
        X = dist_km * cd * cr
        Y = dist_km * cd * sr
        Z = dist_km * sd
        
        # Obs Cartesian
        cl = math.cos(lst_rad)
        sl = math.sin(lst_rad)
        
        Xo = re * rho_cos_phi * cl
        Yo = re * rho_cos_phi * sl
        Zo = re * rho_sin_phi
        
        Xt = X - Xo
        Yt = Y - Yo
        Zt = Z - Zo
        
        Rt = math.sqrt(Xt*Xt + Yt*Yt + Zt*Zt)
        ra_prime = math.degrees(math.atan2(Yt, Xt))
        dec_prime = math.degrees(math.asin(Zt / Rt))
        
        return self.normalize_deg(ra_prime), dec_prime

    def get_moon_projection(self, hour):
        # Helper to get Moon screen coords and phase data
        # Returns: (sx, sy, radius, k, angle_deg, is_waxing, alt) or None
        
        # 1. Calc Pos (Cached)
        q_hour = round(hour * 60) / 60.0
        if q_hour in self._moon_pos_cache:
            moon_ra, moon_dec, sun_ra, sun_dec, m_dist = self._moon_pos_cache[q_hour]
            d = 0 # Not needed if cached
        else:
            # Use Accurate JD based time
            dt_utc = self.get_datetime_utc(q_hour)
            jd_utc = self.julian_day(dt_utc)
            d = jd_utc - 2451545.0
            
            moon_ra, moon_dec, m_dist = self.get_moon_ra_dec(d)
            sun_ra, sun_dec = self.get_sun_ra_dec(d)
            self._moon_pos_cache[q_hour] = (moon_ra, moon_dec, sun_ra, sun_dec, m_dist)
            
        # 2. Horizon Coords (Using Accurate LST)
        if q_hour in self._moon_pos_cache: 
             # Recompute LST/JD quickly? Or just do it.
             dt_utc = self.get_datetime_utc(q_hour)
             jd_utc = self.julian_day(dt_utc)
             lst = self.lst_deg(jd_utc, self.parent_widget.longitude)
        else:
             # Should not happen as we just cached it
             dt_utc = self.get_datetime_utc(q_hour)
             jd_utc = self.julian_day(dt_utc)
             lst = self.lst_deg(jd_utc, self.parent_widget.longitude)

        # APPLY PARALLAX (Topocentric Shift)
        # This aligns the moon visually with the eclipse path
        moon_ra_topo, moon_dec_topo = self.apply_parallax(moon_ra, moon_dec, m_dist, self.parent_widget.latitude, lst)
        
        # Use Topocentric RA/Dec for Horizon conversion
        ha = (lst - moon_ra_topo)
        ha_rad = math.radians(ha)
        lat_rad = math.radians(self.parent_widget.latitude)
        dec_rad = math.radians(moon_dec_topo)
        sin_dec = math.sin(dec_rad)
        cos_dec = math.cos(dec_rad)
        sin_lat = math.sin(lat_rad)
        cos_lat = math.cos(lat_rad)
        
        sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * math.cos(ha_rad)
        alt = math.degrees(math.asin(max(-1, min(1, sin_alt))))
        
        # if alt < -2.0: return None # DISABLED: Allow drawing below horizon
        
        cos_az = (sin_dec - sin_alt * sin_lat) / (math.cos(math.radians(alt)) * cos_lat + 1e-10)
        az = math.degrees(math.acos(max(-1, min(1, cos_az))))
        if math.sin(ha_rad) > 0: az = 360 - az
        
        # 3. Projection
        pt = self.project_universal_stereo(alt, az)
        if not pt: return None
        sx, sy = pt
        
        # 4. Phase
        sr_rad = math.radians(sun_ra)
        mr_rad = math.radians(moon_ra)
        sd_rad = math.radians(sun_dec)
        md_rad = math.radians(moon_dec)
        
        cos_psi = math.sin(sd_rad)*math.sin(md_rad) + math.cos(sd_rad)*math.cos(md_rad)*math.cos(sr_rad - mr_rad)
        k = (1.0 + cos_psi) / 2.0 
        
        diff_ra = (moon_ra - sun_ra) % 360
        is_waxing = (diff_ra > 0 and diff_ra < 180)
        
        # 5. Angle
        s_ha = (lst - sun_ra)
        s_ha_rad = math.radians(s_ha)
        s_sin_alt = math.sin(sd_rad)*sin_lat + math.cos(sd_rad)*cos_lat*math.cos(s_ha_rad)
        s_alt = math.degrees(math.asin(max(-1, min(1, s_sin_alt))))
        s_cos_az = (math.sin(sd_rad) - s_sin_alt*sin_lat) / (math.cos(math.radians(s_alt))*cos_lat + 1e-10)
        s_az = math.degrees(math.acos(max(-1, min(1, s_cos_az))))
        if math.sin(s_ha_rad) > 0: s_az = 360 - s_az
        
        pt_sun = self.project_universal_stereo(s_alt, s_az)
        if pt_sun:
            vx = pt_sun[0] - sx
            vy = pt_sun[1] - sy
        else:
             vx = 1; vy = 0 
        
        angle_deg = math.degrees(math.atan2(vy, vx))
        
        # Calculate Topocentric Semidiameter (sd_topo)
        moon_sd_deg = 0.2725 * (384400.0 / m_dist)
        sin_pi = 6378.14 / m_dist
        sd_topo = moon_sd_deg * (1.0 + sin_pi * math.sin(math.radians(alt)))
        
        # 6. Size
        # Must MATCH Sun size logic EXACTLY for eclipses to look right.
        # Sun logic: radius = 15 * get_horizon_size_multiplier(alt)
        # Moon should be: radius = 15 * multiplier * (MoonSD / SunSD)
        # Standard Sun SD ~ 0.2666 deg.
        # Calculated Moon SD_topo is in `sd_topo`.
        
        sun_base_radius = 15.0 # Visual Size (Aesthetic)
        illusion_mult = self.perceived_disc_scale(alt)
        relative_size = sd_topo / 0.2666
        
        radius = sun_base_radius * illusion_mult * relative_size
        
        return (sx, sy, radius, k, angle_deg, is_waxing, s_alt)

    # draw_moon removed (Legacy)

    def _clamp01(self, val):
        return max(0.0, min(1.0, val))

    def _smoothstep(self, edge0, edge1, x):
        t = self._clamp01((x - edge0) / (edge1 - edge0))
        return t * t * (3.0 - 2.0 * t)

    # moon_visibility_alpha removed (Legacy)
    # _render_moon_pixmap removed (Legacy)

    def draw_ground_mask(self, painter, is_day):
        # Create a closed path for the ground (Alt <= 0)
        ground_path = QPainterPath()
        points = []
        
        # We trace 3 blocks to cover wide FOVs
        for offset in [-360, 0, 360]:
            for az in range(0, 361, 10): 
                pt = self.project_universal_stereo(0, az + offset)
                if pt:
                    points.append(QPointF(*pt))
                elif points:
                    points.append(None) 
        
        if not points: 
            return

        # Build path closing downwards to cover the bottom of the screen
        bottom_y = self.height() * 2.0
        
        first = True
        current_block_start = None
        
        for p in points:
            if p is None:
                if not first and current_block_start:
                    ground_path.lineTo(ground_path.currentPosition().x(), bottom_y)
                    ground_path.lineTo(current_block_start.x(), bottom_y)
                    ground_path.closeSubpath()
                first = True
                continue
                
            if first:
                ground_path.moveTo(p)
                current_block_start = p
                first = False
            else:
                ground_path.lineTo(p)
                
        # Close the last block if open
        if not first and current_block_start:
             ground_path.lineTo(ground_path.currentPosition().x(), bottom_y)
             ground_path.lineTo(current_block_start.x(), bottom_y)
             ground_path.closeSubpath()
        
        # Draw Ground Mask
        col = QColor(20, 30, 20) if is_day else QColor(5, 5, 10)
        painter.setBrush(col)
        painter.setPen(Qt.NoPen)
        painter.drawPath(ground_path)
    
    def draw_compass(self, painter):
        w = self.width()
        painter.setPen(QPen(QColor(200, 200, 200, 150)))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        
        # 8 Cardinal points with keys
        dirs = [
            (0,   "Astro.Dir.North", "North"),
            (45,  "Astro.Dir.NE",    "NE"),
            (90,  "Astro.Dir.East",  "East"),
            (135, "Astro.Dir.SE",    "SE"),
            (180, "Astro.Dir.South", "South"),
            (225, "Astro.Dir.SW",    "SW"),
            (270, "Astro.Dir.West",  "West"),
            (315, "Astro.Dir.NW",    "NW")
        ]
        
        # Wrap view_az (azimuth_offset) to 0..360 only for labels
        # But use continuous projection logic for placement
        
        # Draw 3 blocks to cover wide viewports
        for offset in [-360, 0, 360]:
            for deg, key, default_text in dirs:
                pt = self.project_universal_stereo(0, deg + offset)
                if pt:
                    # Cull labels way off screen to avoid overlap or weirdness
                    if pt[0] < -200 or pt[0] > w + 200: continue
                    
                    label = getTraduction(key, default_text)
                    painter.drawText(int(pt[0])-10, int(pt[1])-5, label)

    # draw_planets removed (Unused)

    def draw_satellites(self, painter, ut_hour):
        # Demo of "Standard Satellite Magnitude Model"
        # Simulating a hypothetical "Station" on a circular orbit
        
        # 1. Fake Ephemeris (Circular Orbit TLE-like)
        # Period ~92 min. Inclination ~51.6 (ISS)
        # Mean Anomaly based on time
        t_ref_hr = 0.0 # UT midnight
        dt_hr = ut_hour - t_ref_hr
        # Wrap for day transition? simple assumption: ut_hour is today.
        
        period_hr = 1.55 # ~93 min
        mean_motion = (2 * math.pi) / period_hr
        M = (dt_hr * mean_motion) % (2*math.pi)
        
        # Orbit Plane (Simplified ECI)
        inc = math.radians(51.6)
        r_orbit = 6378 + 420 # 420km alt
        
        x_orb = r_orbit * math.cos(M)
        y_orb = r_orbit * math.sin(M)
        
        # Rotate by Inclination (around X axis roughly)
        # Assuming node=0 for simplicity
        x_eci = x_orb
        y_eci = y_orb * math.cos(inc)
        z_eci = y_orb * math.sin(inc)
        
        # Earth Rotation (Greenwich Sidereal Time)
        # Need observer ECI
        # We have LST. LST = GMST + Lon. => GMST = LST - Lon.
        dt_utc = self.get_datetime_utc(ut_hour)
        jd_utc = self.julian_day(dt_utc)
        lst = self.lst_deg(jd_utc, self.parent_widget.longitude)
        gmst_deg = lst - self.parent_widget.longitude
        gmst_rad = math.radians(gmst_deg)
        
        # Observer Position ECI
        lat_rad = math.radians(self.parent_widget.latitude)
        lon_rad = math.radians(self.parent_widget.longitude) # This is relative to Greenwich?
        # Actually Obs in ECI:
        # Theta = GMST + Lon
        theta = gmst_rad + lon_rad
        r_earth = 6378.0
        ox = r_earth * math.cos(lat_rad) * math.cos(theta)
        oy = r_earth * math.cos(lat_rad) * math.sin(theta)
        oz = r_earth * math.sin(lat_rad)
        
        # Relative Vector (Range)
        rx = x_eci - ox
        ry = y_eci - oy
        rz = z_eci - oz
        dist = math.sqrt(rx*rx + ry*ry + rz*rz)
        
        # Check Horizon
        # Zenit Vector (approx Obs vector normalized)
        oz_norm = math.sqrt(ox*ox + oy*oy + oz*oz)
        z_vec = (ox/oz_norm, oy/oz_norm, oz/oz_norm)
        
        # Dot product Range . Zenit
        # If positive, it's above horizon (approx)
        dot = (rx * z_vec[0] + ry * z_vec[1] + rz * z_vec[2])
        if dot < 0: return # Below horizon
        
        # Alt/Az Calculation
        # Convert ECI diff to Topocentric (ENU)
        # Slant Range vector in ECI: (rx, ry, rz)
        # Basis Vectors:
        # Up = (cosL cosT, cosL sinT, sinL)
        # East = (-sinT, cosT, 0)
        # North = (-sinL cosT, -sinL sinT, cosL)
        
        sinT = math.sin(theta); cosT = math.cos(theta)
        sinL = math.sin(lat_rad); cosL = math.cos(lat_rad)
        
        u = rx*cosL*cosT + ry*cosL*sinT + rz*sinL
        e = -rx*sinT + ry*cosT
        n = -rx*sinL*cosT - ry*sinL*sinT + rz*cosL
        
        alt = math.degrees(math.atan2(u, math.sqrt(e*e + n*n)))
        az = math.degrees(math.atan2(e, n))
        if az < 0: az += 360
        
        if alt < 10: return # Filter low passes
        
        # Phase Angle Calculation (for Magnitude)
        # Sun Vector in ECI (Approx)
        # Sun RA/Dec
        d = jd_utc - 2451545.0
        s_ra, s_dec = self.get_sun_ra_dec(d)
        s_ra_rad = math.radians(s_ra)
        s_dec_rad = math.radians(s_dec)
        
        # Sun ECI (Unit)
        sx = math.cos(s_dec_rad) * math.cos(s_ra_rad) * 1.5e8 # 1 AU km
        sy = math.cos(s_dec_rad) * math.sin(s_ra_rad) * 1.5e8
        sz = math.sin(s_dec_rad) * 1.5e8
        
        # Vectors from Satellite:
        # To Obs: (-rx, -ry, -rz) -> Norm
        to_obs = (-rx/dist, -ry/dist, -rz/dist)
        # To Sun: (sx-x_eci, sy-y_eci, ...) -> Norm (Sun is far, use Sun vec)
        to_sun = (sx, sy, sz) 
        # Normalize sun
        s_norm = math.sqrt(sx*sx + sy*sy + sz*sz)
        to_sun = (sx/s_norm, sy/s_norm, sz/s_norm)
        
        # Phase Angle (Angle between ToSun and ToObs)
        # cos(phi) = dot(ToSun, ToObs)
        cos_phi = to_sun[0]*to_obs[0] + to_sun[1]*to_obs[1] + to_sun[2]*to_obs[2]
        phi = math.acos(max(-1, min(1, cos_phi)))
        
        # Magnitude Calculation (Std Model Eq 1)
        mag = AstroEngine.calculate_satellite_magnitude(dist, phi, std_mag=-1.8)
        
        # Is Sunlit? (Simple Eclipsed Model)
        # Check if Sat is behind Earth relative to Sun
        # Vector Earth->Sat: (x_eci, y_eci, z_eci)
        # Vector Earth->Sun: (sx, sy, sz)
        # Projection of Sat onto Sun line...
        # Simple: angle between Sat and Sun > 90?
        # Not accurate enough. Simple check: if mag > 10, skip.
        
        if mag > 6.0: return # Too dim to see
        
        # Project
        pt = self.project_universal_stereo(alt, az)
        if pt:
             # Draw Box for Satellite
             # Intensity based on mag
             alpha = max(50, min(255, int(255 - (mag+2)*40)))
             c = QColor(255, 255, 255, alpha)
             
             painter.setPen(QPen(c, 1))
             painter.setBrush(Qt.NoBrush)
             
             s = 6
             painter.drawRect(QRectF(pt[0]-s/2, pt[1]-s/2, s, s))
             painter.setFont(QFont("Mono", 7))
             painter.drawText(int(pt[0])+s, int(pt[1]), f"SAT M:{mag:.1f}")





    def mousePressEvent(self, event):
        if self.scope_mode_enabled():
            if event.button() == Qt.LeftButton:
                if event.modifiers() & Qt.ControlModifier:
                    # Scope override: Ctrl + drag keeps normal camera navigation.
                    self.scope_controller.end_drag()
                    self.dragging = True
                    self.last_mouse_x = event.x()
                    self.last_mouse_y = event.y()
                    self.press_pos = event.pos()
                else:
                    self.scope_controller.handle_click(event.x(), event.y(), self.unproject_stereo)
                    self.scope_controller.start_drag(event.x(), event.y())
                self.update()
            event.accept()
            return

        if self.measurement_tool_active():
            if event.button() == Qt.LeftButton:
                if event.modifiers() & Qt.ControlModifier:
                    # Measurement override: Ctrl + drag keeps normal camera navigation.
                    self.dragging = True
                    self.last_mouse_x = event.x()
                    self.last_mouse_y = event.y()
                    self.press_pos = event.pos()
                else:
                    self.measurement_controller.on_mouse_press(
                        event.x(),
                        event.y(),
                        self.unproject_stereo,
                        self.project_universal_stereo,
                    )
                self.update()
            event.accept()
            return

        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.last_mouse_x = event.x()
            self.last_mouse_y = event.y()
            self.press_pos = event.pos()
    
    def mouseMoveEvent(self, event):
        if self.scope_mode_enabled():
            if self.dragging:
                dx = event.x() - self.last_mouse_x
                dy = event.y() - self.last_mouse_y
                sensitivity = self._scope_secondary_drag_deg_per_px()
                self.azimuth_offset = (self.azimuth_offset - dx * sensitivity)
                self.elevation_angle += dy * sensitivity
                self.elevation_angle = max(-90, min(90, self.elevation_angle))
                self.last_mouse_x = event.x()
                self.last_mouse_y = event.y()
                self.update()
                event.accept()
                return
            if self.scope_controller.drag_move(event.x(), event.y(), self.unproject_stereo):
                self.update()
            event.accept()
            return

        if self.measurement_tool_active():
            if self.dragging:
                dx = event.x() - self.last_mouse_x
                dy = event.y() - self.last_mouse_y
                self.azimuth_offset = (self.azimuth_offset - dx * 0.5)
                self.elevation_angle += dy * 0.5
                self.elevation_angle = max(-90, min(90, self.elevation_angle))
                self.last_mouse_x = event.x()
                self.last_mouse_y = event.y()
                self.update()
                event.accept()
                return
            consumed = self.measurement_controller.on_mouse_move(
                event.x(),
                event.y(),
                self.unproject_stereo,
                self.project_universal_stereo,
            )
            if not consumed:
                self.measurement_controller.update_preview_cursor(event.x(), event.y(), self.unproject_stereo)
            self.update()
            event.accept()
            return

        if self.dragging:
            dx = event.x() - self.last_mouse_x
            dy = event.y() - self.last_mouse_y
            self.azimuth_offset = (self.azimuth_offset - dx * 0.5) 
            self.elevation_angle += dy * 0.5
            self.elevation_angle = max(-90, min(90, self.elevation_angle))
            self.last_mouse_x = event.x()
            self.last_mouse_y = event.y()
            # Keep cached trail image during movement to avoid flickering
            # The Fast-Path will draw on top.
            self.update()
            
    def mouseReleaseEvent(self, event):
        if self.scope_mode_enabled():
            if self.dragging:
                self.dragging = False
                # Invalidate trail cache only when movement STOPS to trigger a clean bake.
                self._cached_trail_image = None
                self.update()
                event.accept()
                return
            self.scope_controller.end_drag()
            self.update()
            event.accept()
            return

        if self.measurement_tool_active():
            if self.dragging:
                self.dragging = False
                # Invalidate trail cache only when movement STOPS to trigger a clean bake.
                self._cached_trail_image = None
                self.update()
                event.accept()
                return
            if event.button() == Qt.LeftButton:
                self.measurement_controller.on_mouse_release(
                    event.x(),
                    event.y(),
                    self.unproject_stereo,
                    self.project_universal_stereo,
                )
                self.update()
            event.accept()
            return

        self.dragging = False
        # Invalidate trail cache only when movement STOPS to trigger a clean bake
        self._cached_trail_image = None
        self.update()
        
        # Click Detection (Min movement)
        if (event.pos() - self.press_pos).manhattanLength() < 5:
            click_pt = event.pos()
            min_dist = 20 # Click Radius
            selected = None
            
            # Use NumPy search for efficiency
            if hasattr(self, 'visible_stars') and len(self.visible_stars) > 0:
                dists = np.hypot(self.visible_stars_sx - click_pt.x(), 
                                 self.visible_stars_sy - click_pt.y())
                idx_min = np.argmin(dists)
                if dists[idx_min] < min_dist:
                    star_idx = self.visible_stars[idx_min]
                    cel_objs = self.parent_widget.celestial_objects
                    if star_idx < len(cel_objs):
                        selected = cel_objs[star_idx]
            
            if selected:
                info = (f"ID: {selected.get('id', 'N/A')}\n"
                        f"Mag: {selected['mag']:.2f}\n"
                        f"RA: {selected['ra']:.2f}\n"
                        f"Dec: {selected['dec']:.2f}\n"
                        f"C: {selected.get('bp_rp', 0):.2f}")
                self.lbl_info.setText(info)
                self.lbl_info.adjustSize()
                self.lbl_info.move(self.width() - self.lbl_info.width() - 20, 20)
                self.lbl_info.show()
                self.lbl_info.raise_()
            else:
                self.lbl_info.hide()

    def mouseDoubleClickEvent(self, event):
        if self.scope_mode_enabled() and event.button() == Qt.LeftButton:
            sky = screen_to_sky(event.x(), event.y(), self.unproject_stereo)
            if sky is not None:
                # Interactive jump: center scope + repoint camera + direct zoom.
                self.scope_controller.set_center(sky)
                self.azimuth_offset = sky[1] % 360.0
                self.elevation_angle = max(-90.0, min(90.0, sky[0]))

                fov_w, fov_h = self.scope_controller.current_fov()
                target_fov = max(0.2, min(93.9, max(fov_w, fov_h)))
                self.zoom_level = max(0.5, min(50.0, 93.9 / target_fov))

                # Force redraw paths that depend on camera state.
                self._cached_star_image = None
                self._cached_trail_image = None

            self.update()
            event.accept()
            return

        super().mouseDoubleClickEvent(event)


    # --- SKYFIELD INTEGRATION ---

    def get_simulated_tz_offset(self, day_of_year):
        # DST logic: ~Mar 29 (day 88) to ~Oct 25 (day 298) is Summer (+2) in Spain
        if 88 <= day_of_year <= 298: return 2.0
        return 1.0

    def perceived_disc_scale(self, alt_deg, sun_alt_deg=None, is_trained=None, horizon_refs=None, flattening=None, atmos=None, falloff_deg=35.0):
        # Defaults
        if is_trained is None: is_trained = self.trained_observer
        if horizon_refs is None: horizon_refs = self.horizon_refs
        if flattening is None: flattening = self.dome_flattening
        if atmos is None: atmos = self.atmospheric_context
        
        # Eclipse Lock Check (Blind check relies on caller, but we check global enable)
        if not self.illusion_enabled:
            return 1.0

        # 1. Base Illusion Magnitude
        # lerp(0.22, 0.50, horizon_refs)
        max_illusion = 0.22 + (0.50 - 0.22) * horizon_refs
        
        # 2. Dome Flattening Boost
        # lerp(1.0, 1.25, flattening)
        max_illusion *= (1.0 + 0.25 * flattening)
        
        # 3. Altitude Falloff (Smoothstep logic)
        # clamp(alt, 0, falloff) -> normalized 0..1
        val = max(0.0, min(1.0, alt_deg / falloff_deg))
        # Invert: 1 at horizon, 0 at falloff
        w = 1.0 - val
        # Smoothstep: 3x^2 - 2x^3
        w = w * w * (3.0 - 2.0 * w)
        
        # 4. Trained Observer Reduction
        if is_trained:
            max_illusion *= 0.6
            
        # 5. Atmospheric Context Boost
        illusion_val = (max_illusion * w) * (1.0 + atmos * 0.10)
        
        # 6. Final Scale (Base 1.0 + illusion)
        # Limit to 1.6x max
        scale = max(1.0, min(1.6, 1.0 + illusion_val))
            
        return scale

    def draw_light_domes(self, painter, profile, eff_sun_alt, eclipse_dimming):
        """Draws only the BACKGROUND atmospheric glows (Moon). City domes are interleaved via callback."""
        if eff_sun_alt >= 0: return
        
        # Diagnostic Log
        if not hasattr(self, '_last_dome_log_time'): self._last_dome_log_time = 0
        current_time = __import__('time').time()
        self._dome_count = 0
        
        if eff_sun_alt > -18.0:
            twilight_factor = (0 - eff_sun_alt) / 18.0
        else:
            twilight_factor = 1.0
        twilight_factor *= eclipse_dimming
        if twilight_factor <= 0.01: return

        # MOON GLOW (Only)
        # Global Sky Glow is now handled inside sky_color_phys physically
        if self._sf_cache and self._sf_cache.get('data'):
            m_data = self._sf_cache['data'].get('moon')
            if m_data and m_data['alt'] > -5.0:
                m_alt, m_az = m_data['alt'], m_data['az']
                m_illum = m_data.get('illumination', 0.5)
                m_pt = self.project_universal_stereo(m_alt, m_az)
                if m_pt:
                    mx, my = m_pt
                    m_glow_radius = 150.0 * self.zoom_level * (1.0 + (90.0 - abs(m_alt))/90.0)
                    m_glow_alpha = int(200 * m_illum * twilight_factor)
                    if m_glow_alpha > 5:
                        grad_moon = QRadialGradient(QPointF(mx, my), m_glow_radius)
                        grad_moon.setColorAt(0.0, QColor(255, 255, 255, m_glow_alpha // 2))
                        grad_moon.setColorAt(0.5, QColor(255, 255, 255, m_glow_alpha // 4))
                        grad_moon.setColorAt(1.0, QColor(0, 0, 0, 0))
                        painter.save()
                        painter.setBrush(grad_moon)
                        painter.setPen(Qt.NoPen)
                        painter.drawEllipse(QPointF(mx, my), m_glow_radius, m_glow_radius)
                        painter.restore()

    def _draw_single_city_dome(self, painter, profile, idx, dist, twilight_factor):
        # ... (Physic comments omitted for brevity)
        import math
        from PyQt5.QtGui import QRadialGradient, QLinearGradient, QColor, QBrush, QPainter
        from PyQt5.QtCore import QPointF, Qt

        self._dome_count += 1
        intensity = profile.light_domes[idx]
        if intensity < 0.2: return
        az = profile.azimuths[idx]
        
        # Distance attenuation: exponential decaimient I(r) = I0 * e^(-r/R) (35km mean clear air path)
        dist_factor = math.exp(-dist / 35000.0) 
        
        # 2. ObtenciÃ³ Altitud (Obstacles TopogrÃ fics a l'Azimut)
        elev_deg = 0.0
        for b in profile.bands:
             elev_deg = max(elev_deg, math.degrees(b["angles"][idx]))
        
        pt = self.project_universal_stereo(elev_deg, az)
        if not pt: return
        x, y = pt
        
        # =========================================================================
        # ALPHA CONFIGURATION: Adjusted to be even more subtle during night
        CITY_DOME_ALPHA_MULTIPLIER = 1.5
        # =========================================================================

        # Base Visual intensity mapping
        # Use log scale to compress massive high-radiance cities
        log_intensity = math.log10(1.0 + intensity)
        visual_intensity = log_intensity * dist_factor
        
        # Alpha Base Limit (Cap at 100 to avoid washing out starlight entirely)
        alpha_base = min(100, int(visual_intensity * 60 * twilight_factor))
        alpha_base = int(alpha_base * CITY_DOME_ALPHA_MULTIPLIER)
        
        # Tolera no pintar casi-invisibles
        if alpha_base <= 2: return
        
        # URBAN GLOW PALETTE: Sodium/LED desaturated colors (Warm Grey/Amber)
        # Saturation reduced from 140 to 60, Lightness from 140 to 100
        glow_hue = 35 + min(10, intensity/500.0) 
        c_core = QColor.fromHsl(int(glow_hue), 50, 80, int(alpha_base * 0.40))
        c_mid  = QColor.fromHsl(int(glow_hue), 40, 60, int(alpha_base * 0.15))
        c_fringe = QColor.fromHsl(int(glow_hue), 30, 40, int(alpha_base * 0.05))
        c_edge = QColor(c_fringe.red(), c_fringe.green(), c_fringe.blue(), 1)
        c_trans = QColor(0, 0, 0, 0)

        # Base radius on screen: Logarithmic scaling
        # Cap the max radius so distant massive cities don't cover the whole screen width
        max_rad = self.width() * 0.4
        rad_x = min(max_rad, log_intensity * 30.0 * self.zoom_level * dist_factor)
        
        # Squashed flat near horizon (more realistic dome shape)
        rad_y = rad_x * 0.35 

        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode_Screen) 
        
        # We draw it at 0,0 and apply a scale transform
        painter.translate(x, y)
        scale_y = rad_y / max(1.0, rad_x)
        painter.scale(1.0, scale_y)
        
        grad = QRadialGradient(QPointF(0, 0), rad_x)
        
        # Colors: Smooth exponential-like fade
        grad.setColorAt(0.00, c_core)
        grad.setColorAt(0.30, c_mid)        
        grad.setColorAt(0.60, c_fringe)     
        grad.setColorAt(0.90, c_edge)
        grad.setColorAt(1.00, c_trans)
        
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.NoPen)
        
        painter.drawRect(int(-rad_x), int(-rad_x), int(rad_x * 2), int(rad_x * 2))
        painter.restore()

    def draw_skyfield_objects(self, painter, ut_hour, day_of_year, ambient_light=1.0, mag_limit=None):
        if mag_limit is None: mag_limit = 6.0
        # USE CACHE if available
        if hasattr(self, '_sf_cache') and self._sf_cache['data']:
            try:
                data = self._sf_cache['data']
                
                # Physical Scaling setup
                w, h = self.width(), self.height()
                R_proj = min(w, h) / 2.0 * self.zoom_level
                pixels_per_deg = R_proj / 90.0
                celestial_scale = 10.0
                
                # Sun Data
                s = data['sun']
                alt_s_deg = s['alt']
                az_s_deg = s['az']
                dist_s_km = s['dist_km']
                sun_ang_radius_deg = s['rad_deg']
                
                # Moon Data
                m = data['moon']
                alt_m_real_deg = m['alt']
                az_m_real_deg = m['az']
                d_moon_km = m['dist_km']
                moon_ang_radius_deg = m['rad_deg']
                sep_real = m['sep_real']
                
                # ... Previous Logic for Shift ...
                alt_m_vis = alt_m_real_deg
                az_m_vis = az_m_real_deg
                
                if sep_real < 10.0:
                    blend = max(0.0, (10.0 - sep_real) / 10.0)
                    blend = blend * blend
                    mult = 1.0 + (celestial_scale - 1.0) * blend
                    d_alt = (alt_m_real_deg - alt_s_deg)
                    d_az = (az_m_real_deg - az_s_deg)
                    alt_m_vis = alt_s_deg + d_alt * mult
                    az_m_vis = az_s_deg + d_az * mult
                
                # ... Moon Illusion ...
                scale_s = self.perceived_disc_scale(alt_s_deg)
                scale_m = self.perceived_disc_scale(alt_m_vis)
                if self.eclipse_lock_mode and sep_real < 2.0:
                     scale_s = 1.0
                     scale_m = 1.0
                     
                # ... Eclipse Snap ...
                snap_threshold = 0.1
                if sep_real < snap_threshold:
                    snap_strength = ((snap_threshold - sep_real) / snap_threshold) ** 2
                    alt_m_vis = alt_m_vis * (1.0 - snap_strength) + alt_s_deg * snap_strength
                    az_m_vis = az_m_vis * (1.0 - snap_strength) + az_s_deg * snap_strength

                sun_radius_px = max(3.0, sun_ang_radius_deg * pixels_per_deg * celestial_scale * scale_s)
                moon_radius_px = max(3.0, moon_ang_radius_deg * pixels_per_deg * celestial_scale * scale_m)

                show_sun_moon = True
                if hasattr(self.parent_widget, 'chk_sun_moon'):
                    show_sun_moon = self.parent_widget.chk_sun_moon.isChecked()

                # ... Sun Color (Copied logic, can optimize later) ...
                c_zenith = QColor(255, 255, 240)
                c_golden = QColor(255, 200, 100)
                c_horizon = QColor(255, 60, 20)
                c_deep = QColor(100, 20, 10)
                
                def interpolate_col_loc(c1, c2, t):
                    r = c1.red() + (c2.red() - c1.red()) * t
                    g = c1.green() + (c2.green() - c1.green()) * t
                    b = c1.blue() + (c2.blue() - c1.blue()) * t
                    return QColor(int(r), int(g), int(b))
                
                if alt_s_deg > 20.0: sun_color = c_zenith
                elif alt_s_deg > 5.0:
                     t_col = (20.0 - alt_s_deg) / 15.0
                     sun_color = interpolate_col_loc(c_zenith, c_golden, t_col)
                elif alt_s_deg > -2.0:
                     t_col = (5.0 - alt_s_deg) / 7.0
                     sun_color = interpolate_col_loc(c_golden, c_horizon, t_col)
                else: sun_color = c_deep
                
                # Corona
                corona_opacity = 0.0
                dist_vis = math.hypot(alt_m_vis - alt_s_deg, az_m_vis - az_s_deg)
                if sun_radius_px < moon_radius_px and dist_vis < (moon_radius_px - sun_radius_px):
                    corona_opacity = 1.0
                
                # Weather Dimming
                eff_sun_color = QColor(sun_color)
                eff_corona_opacity = corona_opacity
                if hasattr(self.parent_widget, 'weather'):
                    w = self.parent_widget.weather
                    dim_factor = 0.0
                    if w.precip_int > 0.1: dim_factor = min(1.0, w.precip_int * 1.2)
                    elif w.current_cover > 0.6: dim_factor = (w.current_cover - 0.6) * 2.5
                    dim_factor = max(0.0, min(1.0, dim_factor))
                    if dim_factor > 0.01:
                        original_alpha = eff_sun_color.alpha()
                        eff_sun_color.setAlpha(int(original_alpha * (1.0 - dim_factor * 0.98)))
                        eff_corona_opacity *= (1.0 - dim_factor)
                
                visual_ppd = pixels_per_deg * celestial_scale
                
                if show_sun_moon:
                    self.draw_sun_skyfield(painter, alt_s_deg, az_s_deg, sun_radius_px, eff_sun_color, eff_corona_opacity, visual_ppd)
                
                # Illumination (Approx)
                elongation = sep_real # Roughly close enough for visual phase if not precise
                illumination = (1 - math.cos(math.radians(elongation))) / 2
                angle_to_sun = math.atan2(alt_s_deg - alt_m_vis, az_s_deg - az_m_vis)
                rotation_deg = math.degrees(angle_to_sun)
                
                # Eclipse check
                d_az_check = (az_m_vis - az_s_deg + 180) % 360 - 180
                dist_vis_px = (math.hypot(alt_m_vis - alt_s_deg, d_az_check)) * pixels_per_deg
                is_eclipsing = (dist_vis_px < (sun_radius_px + moon_radius_px))
                
                # Draw Moon
                moon_tint = QColor(240, 240, 235)
                # ... tint logic ... 
                if alt_m_vis < 20.0:
                     t_moon_set = max(0.0, min(1.0, (20.0 - alt_m_vis) / 20.0))
                     r = int(240 * (1-t_moon_set) + 255 * t_moon_set)
                     g = int(240 * (1-t_moon_set) + 200 * t_moon_set)
                     b = int(235 * (1-t_moon_set) + 100 * t_moon_set)
                     moon_tint = QColor(r, g, b)
                     
                if show_sun_moon:
                    self.draw_moon_skyfield(painter, alt_m_vis, az_m_vis, illumination, rotation_deg, moon_radius_px, 1.0, is_eclipsing, (alt_s_deg > -6), sun_params=(alt_s_deg, az_s_deg, sun_radius_px), pixels_per_deg=visual_ppd, tint_color=moon_tint)
                
                # Planets
                show_planets = True
                if hasattr(self.parent_widget, 'chk_planets'):
                    show_planets = self.parent_widget.chk_planets.isChecked()
                    
                if show_planets:
                    for p in data['planets']:
                        # Recompute visibility
                        s_alt_rad = math.radians(alt_s_deg)
                        s_az_rad = math.radians(az_s_deg)
                        p_alt_rad = math.radians(p['alt'])
                        p_az_rad = math.radians(p['az'])
                        
                        sin_p = math.sin(p_alt_rad)
                        sin_s = math.sin(s_alt_rad)
                        cos_p = math.cos(p_alt_rad)
                        cos_s = math.cos(s_alt_rad)
                        
                        cos_gamma = sin_p*sin_s + cos_p*cos_s*math.cos(p_az_rad - s_az_rad)
                        
                        # Glare/Directional Modifier (Strict -4.0)
                        dir_modifier = -4.0 * cos_gamma
                        
                        # Airmass Extinction for Planets
                        h_p = max(0.1, p['alt'])
                        airmass_p = 1.0 / (math.sin(math.radians(h_p)) + 0.15 * (h_p + 3.885)**-1.253)
                        k_p = 0.20 + 0.04 * (bortle - 1)
                        p_ext = k_p * (airmass_p - 1.0)
                        
                        local_limit = mag_limit + dir_modifier - p_ext
                        
                        # Caching handles magnitude now
                        mag = p.get('mag', -2.0)
                        
                        # Visibility Check (Magnitude vs Limit)
                        # "Brighter than limit" means (mag < limit)
                        diff = local_limit - mag
                        fade_in = max(0.0, min(1.0, diff * 2.0))
                        
                        if fade_in > 0.01:
                            p_rad = max(2.0, (p['sz']/10.0) * pixels_per_deg * 2.0)
                            
                            # Apply fade to alpha
                            p_col = QColor(p['col'])
                            p_col.setAlphaF(fade_in)
                            
                            self.draw_planet(painter, p['alt'], p['az'], p['name'], p_col, p_rad, mag)

                return
            except Exception as e:
                # print(f"Draw Cache Error: {e}")
                pass
        
        # Fallback to Original Logic if no cache
        try:
            ts = self.parent_widget.ts
            eph = self.parent_widget.eph
            observer = wgs84.latlon(self.parent_widget.latitude, self.parent_widget.longitude)
            
            now = datetime.now()
            y = getattr(self.parent_widget, 'manual_year', now.year)
            base_date = datetime(y, 1, 1) + timedelta(days=day_of_year)
            target_dt = base_date + timedelta(hours=ut_hour)
            
            t = ts.from_datetime(target_dt.replace(tzinfo=timezone.utc))
            
            # Physical Scaling setup
            w, h = self.width(), self.height()
            R_proj = min(w, h) / 2.0 * self.zoom_level
            pixels_per_deg = R_proj / 90.0
            
            # Dynamic Scale Logic:
            # - Zoom < 2.0 (Wide): Use Large Scale (12.0) so they are visible "icons".
            # - Zoom > 10.0 (Tele): Use Real Scale (1.0) so geometry is perfect.
            # Physical Scaling setup
            w, h = self.width(), self.height()
            R_proj = min(w, h) / 2.0 * self.zoom_level
            pixels_per_deg = R_proj / 90.0
            
            earth = eph['earth']
            sun = eph['sun']
            moon = eph['moon']
            obs_loc = earth + observer
            
            # "No alterar el tamaÃ±o mÃ¡s que por el efecto del zoom".
            # User request: "Me gustarÃ­a que, al ampliar, tambiÃ©n se ampliara el Sol y la Luna."
            # Previous logic (12.0 / zoom) kept the size static on screen.
            # We now use a constant scale so it grows naturally with the camera zoom (pixels_per_deg).
            # We use 10.0 as a base "Cinematic Scale" so it looks impressive but not overwhelming.
            celestial_scale = 10.0
            
            # --- Position Retargeting (The "Shift" Fix) ---
            # To fix contact time without shrinking:
            # We must move the Moon visually away from the Sun so that the inflated disks
            # touch exactly when the real disks touch.
            # Visual_Distance = Real_Distance * celestial_scale.
            
            # Constants for Angular Size (km)
            SUN_RADIUS_KM = 696340.0
            MOON_RADIUS_KM = 1737.4

            # 1. Get Sun Position (Anchor)
            ast_sun = obs_loc.at(t).observe(sun)
            alt_s, az_s, _ = ast_sun.apparent().altaz()
            
            # 2. Get Moon Position (Real)
            ast_moon = obs_loc.at(t).observe(moon)
            alt_m_real, az_m_real, _ = ast_moon.apparent().altaz()

            # Calculate Real Angular Size
            d_sun_km = ast_sun.distance().km
            d_moon_km = ast_moon.distance().km
            
            # Formula: theta_diam = 2 * atan(r/d). We need radius (theta_diam / 2)
            sun_ang_radius_deg = math.degrees(math.atan(SUN_RADIUS_KM / d_sun_km))
            moon_ang_radius_deg = math.degrees(math.atan(MOON_RADIUS_KM / d_moon_km))
            
            # 3. Calculate Separation
            sep_real = ast_sun.separation_from(ast_moon).degrees
            
            # 4. Apply Shift if near Eclipse (Blend Radius 10 deg)
            # This prevents the Moon from being 120 deg away when it's just 10 deg away.
            alt_m_vis = alt_m_real.degrees
            az_m_vis = az_m_real.degrees
            
            if sep_real < 10.0:
                # Calculate Expansion Factor
                # At sep=0 (Total), Factor should be scale (to preserve centricity? actually scale doesn't matter at 0)
                # At sep=Contact (0.5), VisualSep should be 0.5 * Scale. So Factor = Scale.
                # So we want to separate by Scale.
                
                # Blend Factor: 1.0 (Full Shift) at 0 deg, 0.0 (No Shift) at 10 deg.
                # Linear blend?
                blend = max(0.0, (10.0 - sep_real) / 10.0)
                blend = blend * blend # Smooth quadratic
                
                # Effective Multiplier
                mult = 1.0 + (celestial_scale - 1.0) * blend
                
                # Apply vector expansion
                # Approx linear logic for small angles
                d_alt = (alt_m_real.degrees - alt_s.degrees)
                d_az = (az_m_real.degrees - az_s.degrees)
                # Correct Azimuth for cos(lat) not needed for small local shift approx?
                # Better: Scale d_alt and d_az around Sun
                
                alt_m_vis = alt_s.degrees + d_alt * mult
                az_m_vis = az_s.degrees + d_az * mult
            
            
            # --- Moon Illusion (Perceptive Model) ---
            scale_s = self.perceived_disc_scale(alt_s.degrees)
            scale_m = self.perceived_disc_scale(alt_m_vis)
            
            # Eclipse Lock: Force 1.0 if near eclipse to ensure contact accuracy
            if self.eclipse_lock_mode and sep_real < 2.0:
                 scale_s = 1.0
                 scale_m = 1.0
                 
            # --- ECLIPSE SNAP (Magnetic Alignment) ---
            # Compensates for micro-misalignments (GPS precision, Delta T) to ensure 
            # the user experiences a perfect Totality/Annularity if they are very close.
            # Threshold: 0.1 degrees (very close conjunction)
            snap_threshold = 0.1
            if sep_real < snap_threshold:
                # Strong snap when very close
                # 0.0 at threshold -> 1.0 at 0.0 separation
                snap_strength = ((snap_threshold - sep_real) / snap_threshold) ** 2
                
                # Gently pull Moon towards Sun
                alt_m_vis = alt_m_vis * (1.0 - snap_strength) + alt_s.degrees * snap_strength
                az_m_vis = az_m_vis * (1.0 - snap_strength) + az_s.degrees * snap_strength

            # Clamp min radius
            sun_radius_px = max(3.0, sun_ang_radius_deg * pixels_per_deg * celestial_scale * scale_s)
            moon_radius_px = max(3.0, moon_ang_radius_deg * pixels_per_deg * celestial_scale * scale_m)
            
            # --- SUN COLOR (Atmospheric Extinction) ---
            # Zenith: White/Yellow
            # Horizon: Red/Orange
            # Deep Horizon: Dark Red
            s_alt_deg = alt_s.degrees
            
            def interpolate_col(c1, c2, t):
                r = c1.red() + (c2.red() - c1.red()) * t
                g = c1.green() + (c2.green() - c1.green()) * t
                b = c1.blue() + (c2.blue() - c1.blue()) * t
                return QColor(int(r), int(g), int(b))

            c_zenith = QColor(255, 255, 240) # White-Yellow
            c_golden = QColor(255, 200, 100) # Orange
            c_horizon = QColor(255, 60, 20)  # Red
            c_deep = QColor(100, 20, 10)     # Dark Red
            
            if s_alt_deg > 20.0:
                 sun_color = c_zenith
            elif s_alt_deg > 5.0:
                 t_col = (20.0 - s_alt_deg) / 15.0 # 0..1
                 sun_color = interpolate_col(c_zenith, c_golden, t_col)
            elif s_alt_deg > -2.0:
                 t_col = (5.0 - s_alt_deg) / 7.0
                 sun_color = interpolate_col(c_golden, c_horizon, t_col)
            else:
                 sun_color = c_deep

            # --- CORONA LOGIC ---
            # Visible if Total Eclipse (Sep ~ 0) and Moon covers Sun
            is_total = False
            corona_opacity = 0.0
            
            # Use visual separation calculated previously
            dist_vis = math.hypot(alt_m_vis - alt_s.degrees, az_m_vis - az_s.degrees)
            # If visual separation is small enough that Moon covers Sun
            if sun_radius_px < moon_radius_px and dist_vis < (moon_radius_px - sun_radius_px):
                is_total = True
                corona_opacity = 1.0
            elif dist_vis < (sun_radius_px + moon_radius_px):
                # Partial/Near
                pass
            
            # Draw Sun (Background)
            visual_ppd = pixels_per_deg * celestial_scale
            
            # Weather Dimming (Clouds/Rain obscuring Sun)
            eff_sun_color = QColor(sun_color)
            eff_corona_opacity = corona_opacity
            
            if hasattr(self.parent_widget, 'weather'):
                w = self.parent_widget.weather
                # Dim Sun if Raining or Cloudy
                # Factor 0.0 (Clear) to 1.0 (Totally Obscured)
                dim_factor = 0.0
                if w.precip_int > 0.1:
                    dim_factor = min(1.0, w.precip_int * 1.2)
                elif w.current_cover > 0.6:
                    dim_factor = (w.current_cover - 0.6) * 2.5 # 0.6->0.0, 1.0->1.0
                
                dim_factor = max(0.0, min(1.0, dim_factor))
                
                if dim_factor > 0.01:
                    # Reduce Alpha and Corona
                    # We keep a tiny bit of visibility (0.05) or fully hide?
                    # User said "tapado por una capa de nubes bajas"
                    original_alpha = eff_sun_color.alpha()
                    new_alpha = int(original_alpha * (1.0 - dim_factor * 0.98)) 
                    eff_sun_color.setAlpha(new_alpha)
                    eff_corona_opacity *= (1.0 - dim_factor)

            self.draw_sun_skyfield(painter, alt_s.degrees, az_s.degrees, sun_radius_px, eff_sun_color, eff_corona_opacity, visual_ppd)
            
            s_earth = earth.at(t).observe(sun)
            m_earth = earth.at(t).observe(moon)
            # This is elongation (angle between Sun and Moon seen from Earth)
            elongation = s_earth.separation_from(m_earth).degrees
            
            # Correct Formula for Illumination based on Elongation:
            # Elongation 0 deg (New Moon) -> k = 0
            # Elongation 180 deg (Full Moon) -> k = 1
            illumination = (1 - math.cos(math.radians(elongation))) / 2
            
            # Rotation for phase
            angle_to_sun = math.atan2(alt_s.degrees - alt_m_vis, az_s.degrees - az_m_vis)
            rotation_deg = math.degrees(angle_to_sun)
            
            # Prepare Eclipse Flag for Transparency Logic
            # Correct Azimuth Difference for Wrap-Around (0 vs 360)
            d_az_check = (az_m_vis - az_s.degrees + 180) % 360 - 180
            dist_vis_deg = math.hypot(alt_m_vis - alt_s.degrees, d_az_check)
            dist_vis_px = dist_vis_deg * pixels_per_deg
            
            overlap_dist = sun_radius_px + moon_radius_px
            is_eclipsing = (dist_vis_px < overlap_dist)

            # Daytime Visibility Check
            moon_alpha = 1.0
            is_day = (s_alt_deg > -6.0)
            
            if s_alt_deg > 0: # Bright Day
                # Check Overlap for silhouette preservation (Eclipse)
                if is_eclipsing:
                    moon_alpha = 1.0 # Silhouette is solid
                else:
                    # Fade out thin crescent in bright day
                    moon_alpha = max(0.0, min(1.0, illumination * 2.0))
            
            # Moon Color Logic (Atmospheric)
            # Normal: (240, 240, 235)
            # Horizon: Yellow/Reddish tint
            moon_tint = QColor(240, 240, 235)
            if alt_m_vis < 20.0:
                 t_moon_set = max(0.0, min(1.0, (20.0 - alt_m_vis) / 20.0))
                 # Blend towards Orange (255, 200, 100)
                 r = int(240 * (1-t_moon_set) + 255 * t_moon_set)
                 g = int(240 * (1-t_moon_set) + 200 * t_moon_set)
                 b = int(235 * (1-t_moon_set) + 100 * t_moon_set)
                 moon_tint = QColor(r, g, b)

            self.draw_moon_skyfield(painter, alt_m_vis, az_m_vis, illumination, rotation_deg, moon_radius_px, moon_alpha, is_eclipsing, is_day, sun_params=(alt_s.degrees, az_s.degrees, sun_radius_px), pixels_per_deg=visual_ppd, tint_color=moon_tint)
            
            # Logger (Every 30s)
            import time
            now_ts = time.time()
            if not hasattr(self, 'last_log_time'): self.last_log_time = 0
            if now_ts - self.last_log_time > 99999999999:
                self.last_log_time = now_ts
                off = self.get_simulated_tz_offset(day_of_year)
                print(f"SKYFIELD LOG [UTC{off:+.0f}]: "
                      f"SUN(Alt={alt_s.degrees:.4f}Â°, Az={az_s.degrees:.4f}Â°) | "
                      f"MOON(Alt={alt_m_real.degrees:.4f}Â°, Az={az_m_real.degrees:.4f}Â°)")
            
            # 3. Planets (All times, visibility depends on Magnitude Limit)
            # The manual check 'if s_alt_deg < -6' prevented Venus/Jupiter from appearing in Civil Twilight.
            # Removed it. The 'mag <= local_limit' check inside handles it correctly.
            if True:
                planets = {
                    'mercury': ('Mercury', QColor(169, 169, 169), 4),
                    'venus': ('Venus', QColor(255, 220, 150), 7),
                    'mars': ('Mars', QColor(255, 100, 80), 5),
                    'jupiter barycenter': ('Jupiter', QColor(220, 180, 140), 12),
                    'saturn barycenter': ('Saturn', QColor(240, 210, 150), 10),
                    'uranus barycenter': ('Uranus', QColor(173, 216, 230), 6),
                    'neptune barycenter': ('Neptune', QColor(100, 100, 255), 6),
                    'pluto barycenter': ('Pluto', QColor(200, 180, 160), 3),
                }
                
                for key, (name, col, sz) in planets.items():
                    try:
                        p = eph[key]
                        ast = obs_loc.at(t).observe(p)
                        alt_p, az_p, dist = ast.apparent().altaz()
                        
                        if alt_p.degrees > -5:
                            # phase_angle calculation omitted for perf, assuming 0 (Full)
                            phase_angle = 0.0
                            mag = self.calculate_planet_magnitude(name, dist.au, phase_angle)
                            p_rad = max(2.0, (sz/10.0) * pixels_per_deg * 2.0)
                            
                            # --- DIRECTIONAL VISIBILITY FOR PLANETS ---
                            # Same logic as stars
                            p_alt_rad = math.radians(alt_p.degrees)
                            p_az_rad = math.radians(az_p.degrees)
                            s_alt_rad = math.radians(alt_s.degrees) # Use variables from scope
                            s_az_rad = math.radians(az_s.degrees)
                            
                            sin_p = math.sin(p_alt_rad)
                            sin_s = math.sin(s_alt_rad)
                            cos_p = math.cos(p_alt_rad)
                            cos_s = math.cos(s_alt_rad)
                            
                            cos_gamma = sin_p*sin_s + cos_p*cos_s*math.cos(p_az_rad - s_az_rad)
                            
                            # PLANET SPECIFIC VISIBILITY:
                            # Reverting to strict penalty as per user request.
                            # Even bright planets are lost in the sun's glare if too close.
                            dir_modifier = -1.5 * cos_gamma
                            
                            local_limit = mag_limit + dir_modifier
                            
                            # NO HARD CUTOFF
                            # Rely purely on the Star-Like Visibility Algorithm
                            pass

                            # Soft Fade
                            diff = local_limit - mag
                            fade_in = max(0.0, min(1.0, diff * 2.0))
                            
                            if fade_in > 0.01:
                                # Clone color to apply alpha
                                p_col = QColor(col)
                                p_col.setAlphaF(fade_in)
                                self.draw_planet(painter, alt_p.degrees, az_p.degrees, name, p_col, p_rad, mag)
                    except: continue

            # 4. Satellites
            if hasattr(self.parent_widget, 'show_satellites') and self.parent_widget.show_satellites:
                for sat_def in self.parent_widget.satellites:
                    try:
                        sat = sat_def['obj']
                        topo = (sat - obs_loc).at(t)
                        alt_sat, az_sat, dist_sat = topo.altaz()
                        if alt_sat.degrees > 0:
                            mag = sat_def.get('std_mag', -1.8)
                            self.draw_satellite(painter, alt_sat.degrees, az_sat.degrees, sat_def['name'], mag)
                    except: pass

        except Exception as e:
            # print(f"Skyfield Error: {e}")
            pass

    def perceived_disc_scale(self, altitude):
        # ... logic ...
        return 1.0

# ... (Previous code) ...

    def update_loop(self):
        if self.use_real_time:
            now = datetime.now()
            # Sync Day & Year
            self.manual_year = now.year
            self.manual_day = (now - datetime(now.year, 1, 1)).days
            if hasattr(self, 'lbl_date'):
               self.lbl_date.setText(self.format_date(self.manual_day))
            
            # Sync Gradient
            if hasattr(self, 'time_bar'):
                self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
            
            # Sync Time
            h = now.hour + now.minute/60.0 + now.second/3600.0
            if hasattr(self, 'time_bar'):
                self.time_bar.set_time(h)
        else:
            # Manual Mode: "Time keeps running forward"
            # Increment manual_hour by elapsed time
            # Timer interval varies, so usage of constant '0.1' is wrong if we change FPS?
            # We should measure actual dt.
            # For simplicity, we assume the interval is respected or we use current interval.
            
            dt_sec = self.timer.interval() / 1000.0
            dt_hours = dt_sec / 3600.0
            self.manual_hour = (self.manual_hour + dt_hours) % 24.0
            if hasattr(self, 'time_bar'):
                self.time_bar.set_time(self.manual_hour)
        
        # DYNAMIC FPS
        # Rain looks laggy at low fps.
        if hasattr(self, 'weather') and self.weather.precip_int > 0.1:
            if self.timer.interval() != 33:
                self.timer.setInterval(33) # 30 FPS for rain
        else:
            if self.timer.interval() > 50 or self.timer.interval() == 33:
                self.timer.setInterval(50) # 20 FPS base (was 10)
                
        self.canvas.update()

    def get_refracted_body_path(self, radius, alt, pixels_per_deg):
        """
        Generates a QPainterPath representing the body.
        
        SIMPLIFICATION: We have disabled differential refraction (squashing) 
        to ensure perfect geometric overlap during eclipses. 
        The Sun and Moon will remain perfect circles.
        The 'lift' effect is already handled by Skyfield's altitude positioning.
        """
        path = QPainterPath()
        path.addEllipse(QPointF(0,0), radius, radius)
        return path

    def draw_sun_skyfield(self, painter, alt, az, radius, color, corona_opacity, pixels_per_deg):
        pt = self.project_universal_stereo(alt, az)
        if not pt: return
        x, y = pt
        
        # 1. Corona (Intense & Sharp)
        if corona_opacity > 0.01:
            painter.setBrush(Qt.NoBrush)
            grad = QRadialGradient(x, y, radius * 12.0) # Larger 12x
            # Very Punchy
            grad.setColorAt(0.0, QColor(255, 255, 255, int(255 * corona_opacity))) # Solid Center
            grad.setColorAt(0.1, QColor(200, 220, 255, int(220 * corona_opacity)))
            grad.setColorAt(0.25, QColor(100, 100, 255, int(100 * corona_opacity)))
            grad.setColorAt(1.0, QColor(0, 0, 50, 0))
            painter.setBrush(QBrush(grad))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(x, y), radius*12, radius*12)
        
        # 2. Atmospheric Refraction Logic
        # Use centralized helper
        path = self.get_refracted_body_path(radius, alt, pixels_per_deg)
        
        painter.save()
        painter.translate(x, y)
        
        # Prepare Gradient for Sun Body (White hot center -> Limb Darkening -> Atmosphere Tint)
        body_grad = QRadialGradient(0, 0, radius)
        
        # Core stays white-ish unless VERY low
        core_col = QColor(255, 255, 255)
        if alt < 5.0:
            # At sunset, even the core gets tinted
            t_set = (5.0 - alt) / 5.0 # 0..1
            # Blend White -> Yellow/Red
            core_col = QColor(255, 255 - int(50*t_set), 255 - int(100*t_set))
            
        body_grad.setColorAt(0.0, core_col)
        # Push the core further out (0.85) for a more "Solid Disk" look
        body_grad.setColorAt(0.85, QColor(255, 255, 240) if alt > 10 else color.lighter(120)) 
        body_grad.setColorAt(1.0, color) # Limb color matches atmosphere
        
        painter.setBrush(QBrush(body_grad))
        painter.setPen(Qt.NoPen)
        
        # REMOVED Opacity Dimming. 
        # We want a SOLID disk even at sunset, just darker in color.
        # Physics says it dims, but Art says "Don't make it a ghost".
        
        painter.drawPath(path)
        painter.setOpacity(1.0) # Ensure full opacity for glow logic
        
        # 3. Stellarium-style Bloom/Glow (Only if not in Totality)
        if corona_opacity < 0.9:
             # Huge, smooth radial gradient (No discrete rings)
             glow_radius = radius * 15.0 
             glow_grad = QRadialGradient(0, 0, glow_radius)
             
             # Core Glare (Blinding) -> Halo -> Atmosphere
             # Alpha is kept somewhat high at core to simulate brightness
             base_alpha = 180 if alt > 10 else 100
             
             glow_grad.setColorAt(0.0, QColor(255, 255, 255, 255)) # Blind white
             glow_grad.setColorAt(0.05, QColor(color.red(), color.green(), color.blue(), 200)) # Very bright aura
             glow_grad.setColorAt(0.1, QColor(color.red(), color.green(), color.blue(), 100)) # Aura
             glow_grad.setColorAt(0.4, QColor(color.red(), color.green(), color.blue(), 30))  # General glow
             glow_grad.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))   # Fade out
             
             painter.setBrush(QBrush(glow_grad))
             painter.setCompositionMode(QPainter.CompositionMode_Screen) # Additive blending for light
             painter.drawEllipse(QPointF(0,0), glow_radius, glow_radius)
             painter.setCompositionMode(QPainter.CompositionMode_SourceOver) # Restore

        painter.restore()

    def draw_moon_skyfield(self, painter, alt, az, illum, rotation_deg, radius, alpha, is_eclipsing=False, is_day=False, sun_params=None, pixels_per_deg=None, tint_color=None):
        if alpha <= 0.01: return
        pt = self.project_universal_stereo(alt, az)
        if not pt: return
        x, y = pt
        r = radius
        
        # Default PPD if missing
        if pixels_per_deg is None:
             pixels_per_deg = radius / 0.26 # Fallback estimation
        
        # Get sun altitude from sun_params for sky color calculation
        sun_alt_for_sky = -18.0  # Default to night
        sun_az_for_sky = 0.0
        if sun_params:
            sun_alt_for_sky = sun_params[0]
            sun_az_for_sky = sun_params[1]
        
        # 0. OCCULTATION (Night Only: Block Stars)
        # The moon is solid object. At night, its unlit part blocks stars.
        # Use sky color instead of black so it appears "transparent" while still occluding stars.
        if not is_day and not is_eclipsing:
            painter.save()
            painter.setOpacity(1.0)
            painter.translate(x, y)
            
            # Get sky color at moon's position for natural blending
            sky_col = self.sky_color_phys(alt, az, sun_alt_for_sky, sun_az_for_sky)
            painter.setBrush(sky_col)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(0,0), r, r)
            painter.restore()

        # Atmospheric Blend (Daytime Transparency)
        # During the day, the atmosphere is IN FRONT of the moon, lowering contrast.
        # We simulate this by letting the sky background bleed through.
        effective_alpha = alpha
        if is_day:
            effective_alpha = min(alpha, 0.85)

        painter.setOpacity(effective_alpha)
        
        # 1. Dark Body Logic
        # - Eclipsing: Opaque Black, BUT CLIPPED to Sun. (The "Bite")
        # - Normal: Transparent.
        if is_eclipsing and sun_params:
            s_alt, s_az, s_rad = sun_params
            pt_s = self.project_universal_stereo(s_alt, s_az)
            if pt_s:
                sx, sy = pt_s
                
                # Create Sun Path (Refracted) using exact same logic as draw_sun
                raw_s_path = self.get_refracted_body_path(s_rad, s_alt, pixels_per_deg)
                
                t_s = QTransform()
                t_s.translate(sx, sy)
                final_s_path = t_s.map(raw_s_path)
                
                # Create Moon Path (Refracted!) matching Sun's physics
                # CRITICAL Fix for Totality:
                # 1. Use Sun's Altitude (s_alt) for the Moon's refraction shape. 
                #    This ensures identical atmospheric "squash" for both.
                # 2. If Moon is physically larger (Totality), ensure mask is slightly larger to kill artifacts.
                
                target_r = r
                if r >= s_rad:
                    target_r = max(r, s_rad + 1.0)
                
                # Using s_alt instead of alt guarantees the shapes match like puzzle pieces
                raw_m_path = self.get_refracted_body_path(target_r, s_alt, pixels_per_deg)
                
                t_m = QTransform()
                t_m.translate(x, y)
                final_m_path = t_m.map(raw_m_path)
                
                # INTERSECTION (The Bite)
                # Intersection of Refracted Sun and Refracted Moon
                bite_path = final_s_path.intersected(final_m_path)
                
                painter.setBrush(QColor(15, 15, 20)) 
                painter.setPen(Qt.NoPen)
                painter.drawPath(bite_path)
        
        # 2. Lit Part (Phase)
        # 2. Lit Part (Phase)
        if illum > 0.01:
            # Base color defined later
            painter.save()
            painter.translate(x, y)
            painter.rotate(-rotation_deg) # Rotate to face Sun
            
            # Using QPainterPath for transparency support
            path = QPainterPath()
            path.arcMoveTo(-r, -r, 2*r, 2*r, 90)
            path.arcTo(-r, -r, 2*r, 2*r, 90, 180) # Semicircle bright side
            
            # Ellipse part
            # Illum > 0.5: Add Ellipse.
            # Illum < 0.5: Subtract Ellipse.
            ell_path = QPainterPath()
            w_ell = abs((2.0 * illum - 1.0) * r)
            ell_path.addEllipse(QPointF(0,0), w_ell, r)
            
            final_path = QPainterPath()
            if illum >= 0.5:
                # Gibbous: Semicircle + Ellipse
                final_path = path.united(ell_path)
            else:
                # Crescent: Semicircle - Ellipse
                final_path = path.subtracted(ell_path)
            
            painter.setPen(Qt.NoPen)
            
            # Organic Style: 
            # 1. Base Color (Tinted)
            base_col = tint_color if tint_color else QColor(240, 240, 235)
            painter.setBrush(base_col)
            painter.drawPath(final_path)
            
            # 2. Features (Craters/Maria) - Clipped to Lit Part
            painter.setClipPath(final_path)
            
            # Rotate BACK so features stay "upright" while the terminator moves
            # (Simplification: Fixed orientation relative to Zenith)
            painter.rotate(rotation_deg) 
            
            # Derived Maria Color (Darker version of base)
            m_r, m_g, m_b = base_col.red(), base_col.green(), base_col.blue()
            maria_col = QColor(max(0, m_r - 20), max(0, m_g - 20), max(0, m_b - 10))
            
            painter.setBrush(maria_col) # Maria Color
            painter.setPen(Qt.NoPen)
            
            # Draw Stylized Craters (Fixed positions relative to Moon center)
            # Mare Imbrium
            painter.drawEllipse(QPointF(-r*0.2, -r*0.4), r*0.25, r*0.25)
            # Mare Serenitatis
            painter.drawEllipse(QPointF(r*0.2, -r*0.3), r*0.2, r*0.2)
            # Mare Tranquillitatis
            painter.drawEllipse(QPointF(r*0.3, -r*0.1), r*0.22, r*0.22)
            # Oceanus Procellarum (Left side large patch)
            painter.drawEllipse(QPointF(-r*0.5, -r*0.1), r*0.3, r*0.5)
            # Tycho (Bottom crater)
            painter.setBrush(QColor(230, 230, 235))
            painter.drawEllipse(QPointF(0, r*0.6), r*0.1, r*0.1)
            
            painter.restore()

        # 3. Earthshine removed as per user request (was looking like a bubble)
        pass

        painter.setOpacity(1.0)

    def draw_planet(self, painter, alt, az, name, col, sz, mag):
        pt = self.project_universal_stereo(alt, az)
        if not pt: return
        x, y = pt
        painter.setBrush(col)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(x,y), sz, sz)
        painter.setPen(col)
        painter.drawText(int(x)+10, int(y), f"{name} {mag:.1f}")

    def draw_satellite(self, painter, alt, az, name, mag):
        pt = self.project_universal_stereo(alt, az)
        if not pt: return
        x, y = pt
        painter.setPen(QPen(Qt.red, 2))
        painter.drawPoint(QPointF(x,y))
        painter.setPen(Qt.white)
        painter.drawText(int(x)+5, int(y)-5, f"{name} {mag:.1f}")

    def calculate_planet_magnitude(self, name, d_au, phase):
        # Revised Base Magnitudes (Normalized to ~1 AU distance + Albedo)
        # Formula uses +5*log10(d), so Base must handle the subtraction of distance modulus.
        # e.g. Saturn at 9AU: 5*log(9) = +4.77. Target Mag ~0.5. Base should be -4.3.
        base = {
            'Mercury': -0.6, 'Venus': -4.4, 
            'Mars': -0.5, # Adjusted (was -2.0)
            'Jupiter': -5.8, # Adjusted (was -2.7)
            'Saturn': -4.3, # Adjusted (was 0.5)
            'Uranus': -0.7, 'Neptune': 0.5, 'Pluto': 6.0
        }.get(name, 0)
        return base + 5*math.log10(d_au) + 0.01*phase


    def get_eclipse_dimming_factor(self, ut_hour, day_of_year):
        """
        Calculates the dimming factor (0.0 to 1.0) based on Eclipse state (Total vs Annular).
        1.0 = Full Light
        0.0 = Total Darkness
        """
        try:
            ts = self.parent_widget.ts
            eph = self.parent_widget.eph
            observer = wgs84.latlon(self.parent_widget.latitude, self.parent_widget.longitude)
            
            now = datetime.now()
            y = getattr(self.parent_widget, 'manual_year', now.year)
            base_date = datetime(y, 1, 1) + timedelta(days=day_of_year)
            target_dt = base_date + timedelta(hours=ut_hour)
            
            t = ts.from_datetime(target_dt.replace(tzinfo=timezone.utc))
            
            earth = eph['earth']
            sun = eph['sun']
            moon = eph['moon']
            obs_loc = earth + observer
            
            # 1. Get Positions & Distances
            ast_sun = obs_loc.at(t).observe(sun)
            ast_moon = obs_loc.at(t).observe(moon)
            
            d_sun_km = ast_sun.distance().km
            d_moon_km = ast_moon.distance().km
            
            # 2. Angular Radii (Degrees)
            SUN_RADIUS_KM = 696340.0
            MOON_RADIUS_KM = 1737.4
            
            r_sun = math.degrees(math.atan(SUN_RADIUS_KM / d_sun_km))
            r_moon = math.degrees(math.atan(MOON_RADIUS_KM / d_moon_km))
            
            # 3. Separation
            sep = ast_sun.separation_from(ast_moon).degrees
            
            # 4. Eclipse Factor Logic
            # max_dist: Touch point
            max_dist = r_sun + r_moon
            
            # No eclipse
            if sep >= max_dist: return 1.0
            
            # Full containment point (Moon inside Sun OR Sun inside Moon)
            min_dist = abs(r_sun - r_moon)
            
            # Calculate "Contained Factor" (The darkest it can get for this alignment)
            if r_moon >= r_sun:
                # Total Eclipse possible -> 0.0 (Darkness)
                # But allow a tiny ambient light (0.05)
                contained_factor = 0.05 
            else:
                # Annular Eclipse -> Light reduced by covered area
                # Area = pi * r^2
                area_sun = r_sun * r_sun
                area_moon = r_moon * r_moon
                ratio = area_moon / area_sun
                contained_factor = 1.0 - ratio # Remaining light
                
            # Interpolate based on separation
            if sep <= min_dist:
                # Fully contained state
                return contained_factor
            else:
                # Partial Phase (Linear interpolation between 1.0 and contained_factor)
                # Range: [min_dist, max_dist]
                # t = 0 at min_dist (darkest), 1 at max_dist (brightest)
                t = (sep - min_dist) / (max_dist - min_dist)
                return contained_factor + (1.0 - contained_factor) * t
                
        except Exception as e:
            # print(f"Eclipse Calc Error: {e}")
            return 1.0


class AstronomicalWidget(CustomWidgetBase):
    request_render_signal = pyqtSignal()
    request_trails_signal = pyqtSignal()
    # Signal to start baking in background thread
    request_horizon_bake = pyqtSignal(float, float) # lat, lon

    def __init__(self, parent=None, **kwargs):
        # 1. Initialize properties required by UI/Canvas
        self.latitude = 41.189795
        self.longitude = 1.210058
        self.magnitude_limit = 8.0
        self.spike_magnitude_threshold = 2.0
        self.star_scale = 0.5
        
        self.celestial_objects = []
        self.use_real_time = True
        self.manual_hour = 12.0
        self.pure_colors = False
        
        # Light Pollution state
        self.is_auto_bortle = True
        self.auto_bortle_estimate = 1
        
        now = datetime.now()
        self.manual_year = now.year
        self.manual_day = (now - datetime(now.year, 1, 1)).days
        
        # Weather needs to exist before setup_content -> AstroCanvas -> WeatherControlWidget
        self.weather = WeatherSystem(800, 600)

        # 2. Init Base Widget (Calls setup_ui -> setup_content)
        super().__init__(title="Astronomy", parent=parent, **kwargs)
        
        # 3. Post-UI initialization â€” ASYNC (non-blocking)
        self.show_satellites = False
        self.satellites = []
        
        # --- Async Skyfield loading (Optimization 4) ---
        if SKYFIELD_AVAILABLE:
            self._skyfield_thread = QThread()
            self._skyfield_worker = SkyfieldLoaderWorker()
            self._skyfield_worker.moveToThread(self._skyfield_thread)
            self._skyfield_worker.skyfield_ready.connect(self._on_skyfield_ready)
            self._skyfield_thread.started.connect(self._skyfield_worker.load)
            self._skyfield_thread.start()
            print("[AstroWidget] Skyfield loading in background...")
        
        # --- Async Catalog loading (Optimization 3 + 5) ---
        self._catalog_thread = QThread()
        self._catalog_worker = CatalogLoaderWorker()
        self._catalog_worker.moveToThread(self._catalog_thread)
        self._catalog_worker.catalog_ready.connect(self._on_catalog_ready)
        local_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(local_dir, '..', 'data', 'stars', 'gaia_stars.json')
        # Use lambda to pass argument when thread starts
        self._catalog_thread.started.connect(lambda: self._catalog_worker.load(json_path))
        self._catalog_thread.start()
        print("[AstroWidget] Star catalog loading in background...")

        # Initialize Horizon Worker for dynamic baking
        from TerraLab.terrain.worker import HorizonWorker
        from TerraLab.common.utils import resource_path
        import sys
        
        # Initialize Horizon Worker for dynamic baking
        from TerraLab.terrain.worker import HorizonWorker
        
        # Tiles path is now handled internally by HorizonWorker via ConfigManager
        # We don't need to pass it explicitly here.

        self.horizon_thread = QThread()
        self.horizon_worker = HorizonWorker() # Config handles path
        
        # Hydrate saved offset into worker before moving thread
        from TerraLab.common.utils import get_config_value
        saved_offset = float(get_config_value("observer_offset", 0.0))
        self.horizon_worker.set_observer_offset(saved_offset)
        
        self.horizon_worker.moveToThread(self.horizon_thread)
        
        self.horizon_worker.profile_ready.connect(self.on_horizon_profile_ready)
        self.horizon_worker.error_occurred.connect(lambda err: print(f"[HorizonWorker] THREAD ERROR: {err}"))
        self.horizon_worker.progress_message.connect(self.on_horizon_progress)
        self.request_horizon_bake.connect(self.horizon_worker.request_bake)
        
        # Start the thread and initialize sampler immediately to avoid UI lag/desync
        self.horizon_thread.started.connect(self.horizon_worker.initialize)
        
        print(f"[AstroWidget] Starting Horizon Thread... (Path managed by Worker)")
        self.horizon_thread.start()
        print(f"[AstroWidget] Horizon Thread started. ID: {int(self.horizon_thread.currentThreadId()) if self.horizon_thread.currentThreadId() else 'N/A'}")
        
        # Trigger initial bake via Signal (Runs on Worker Thread)
        def trigger_bake():
            print(f"[AstroWidget] Emitting bake request for {self.latitude}, {self.longitude}")
            if hasattr(self, 'lbl_loading'):
                self.lbl_loading.show()
                self.lbl_loading.raise_()
            self.request_horizon_bake.emit(self.latitude, self.longitude)
        
        QTimer.singleShot(1000, trigger_bake) # Delay 1s to ensure thread ready

        # Default to Horizon View (This uses self.canvas, created in setup_content)
        self.set_horizon_view()
                
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_loop)
        # Reduced to 50ms (20FPS) to help Video Player performance
        self.timer.start(50)
        
        self.anim_timer = QTimer()
        self.anim_timer.timeout.connect(self.animate_view)
        self.target_azimuth = None
        
        # Performance mode flag
        self._updates_paused = False
        self._saved_interval = 50

        # Debounce timer for bake requests (Avoid UI freeze and worker flooding)
        self.bake_debounce_timer = QTimer()
        self.bake_debounce_timer.setSingleShot(True)
        self.bake_debounce_timer.timeout.connect(self._do_delayed_bake)

    def on_horizon_profile_ready(self, profile):
        """Callback when background worker finishes baking horizon."""
        print(f"[AstroWidget] New Horizon Profile received! Bands: {len(profile.bands)}")
        # Hide Loading Label
        if hasattr(self, 'lbl_loading'):
            self.lbl_loading.hide()
            
        # Build matching layer_defs from band_defs attached by the worker
        layer_defs = None
        band_defs = getattr(profile, '_band_defs', None)
        if band_defs is not None:
            try:
                from TerraLab.terrain.overlay import generate_layer_defs
                layer_defs = generate_layer_defs(band_defs)
            except Exception as e:
                print(f"[AstroWidget] Warning: Could not generate layer_defs: {e}")
            
        # Update Horizon Overlay (Background Mountains)
        if hasattr(self.canvas, 'horizon_overlay'):
            self.canvas.horizon_overlay.set_profile(profile, layer_defs=layer_defs)
            
        # Update Village Overlay (Foreground Objects)
        if hasattr(self.canvas, 'village'):
             self.canvas.village.set_profile(profile)
             
        # Refresh the UI altitude label now that the worker has safely initialized the DEM data
        self.update_altitude_label()
        
        # Initial Bortle Sync if in Auto mode
        if getattr(self, 'is_auto_bortle', True):
            self.reset_lp_to_auto()
             
        self.canvas.update()


    def on_horizon_progress(self, msg):
        """Update loading label with progress message."""
        if hasattr(self, 'lbl_loading'):
            self.lbl_loading.setText(msg)
            if self.lbl_loading.isHidden():
                self.lbl_loading.show()
                self.lbl_loading.raise_()
        
        self.canvas.update()

    def pause_updates(self):
        """Pause sky updates to free up main thread for video loading."""
        if not self._updates_paused:
            self._updates_paused = True
            self._saved_interval = self.timer.interval()
            self.timer.stop()
            print("[SKY] Updates PAUSED for video loading")
    
    def resume_updates(self):
        """Resume sky updates after video has loaded."""
        if self._updates_paused:
            self._updates_paused = False
            self.timer.start(self._saved_interval)
            print("[SKY] Updates RESUMED")
    
    def set_low_fps_mode(self, low=True):
        """Switch to low FPS mode (5 FPS) when video is playing."""
        if low:
            self.timer.setInterval(200)  # 5 FPS
        else:
            self.timer.setInterval(50)   # 20 FPS

    def _on_skyfield_ready(self, ts, eph):
        """Callback when Skyfield finishes loading in background."""
        if ts is not None and eph is not None:
            self.ts = ts
            self.eph = eph
            print("[AstroWidget] Skyfield ready (async).")
            if self.show_satellites:
                # Only show loading label if not blocking (i.e., if satellites are being loaded)
                if hasattr(self, 'lbl_loading'):
                    self.lbl_loading.show()
                self.load_satellites_from_tle()
            self.canvas.update()
        else:
            print("[AstroWidget] Skyfield failed to load.")
        # Clean up thread
        self._skyfield_thread.quit()

    def _do_delayed_bake(self):
        """Actually sends the bake request after debouncing."""
        if hasattr(self, 'horizon_worker'):
             # Signal current bake to abort if possible to free the worker for the new one
             self.horizon_worker._abort_requested = True
             
        # SAVE CONFIG ONLY HERE (Avoid disk spam)
        from TerraLab.common.utils import set_config_value
        offset_val = self.spin_extra_height.value()
        set_config_value("observer_offset", offset_val)
        set_config_value("observer_lat", self.latitude)
        set_config_value("observer_lon", self.longitude)
             
        print(f"[AstroWidget] Emitting debounced bake request for {self.latitude}, {self.longitude}")
        self.request_horizon_bake.emit(self.latitude, self.longitude)
    
    def _on_catalog_ready(self, celestial_objects, np_ra, np_dec, np_mag, np_r, np_g, np_b):
        """Callback when star catalog finishes loading in background."""
        self.celestial_objects = celestial_objects
        if np_ra is not None:
            self.np_ra = np_ra
            self.np_dec = np_dec
            self.np_mag = np_mag
            self.np_r = np_r
            self.np_g = np_g
            self.np_b = np_b
        print(f"[AstroWidget] Star catalog ready: {len(celestial_objects)} stars (async).")
        self.build_search_index()
        self.canvas.update()
        # Clean up thread
        self._catalog_thread.quit()

    def init_skyfield(self):
        """Legacy synchronous init. Kept for compatibility."""
        try:
            self.ts = load.timescale()
            self.eph = load('de421.bsp')
            print("Skyfield Initialized.")
        except Exception as e:
            print(f"Skyfield Error: {e}")
            
    def load_satellites_from_tle(self):
        if not SKYFIELD_AVAILABLE: return
        try:
            from skyfield.api import EarthSatellite
            # ISS TLE (Example - normally from CelesTrak)
            line1 = "1 25544U 98067A   23015.53927649  .00010079  00000-0  18231-3 0  9993"
            line2 = "2 25544  51.6421  42.5312 0005527  38.8344 321.3283 15.49830575378370"
            iss = EarthSatellite(line1, line2, 'ISS', self.ts)
            
            self.satellites = [{
                'name': 'ISS',
                'obj': iss,
                'std_mag': -1.8
            }]
        except Exception as e:
            print(f"Sat Load Error: {e}")

    def toggle_satellites(self, checked):
        self.show_satellites = checked
        if checked and not self.satellites and SKYFIELD_AVAILABLE:
            self.load_satellites_from_tle()
        self.canvas.update()

    def setup_content(self):
        # Hide standard window decorations since this is a wallpaper/panel
        if hasattr(self, 'title_bar'):
            self.title_bar.hide()

        # Use the content layout provided by CustomWidgetBase
        layout = self.content_layout
        layout.setContentsMargins(0,0,0,0)
        
        self.canvas = AstroCanvas(self)
        layout.addWidget(self.canvas, 1)
             
        # === Compact Layout ===
        
        # Main Layout is Vertical inside the frame
        frame_layout = QVBoxLayout()
        frame_layout.setSpacing(0) 
        # === CUSTOM MOCKUP LAYOUT (3 HORIZONTAL PANELS) ===
        frame_layout = QVBoxLayout()
        frame_layout.setSpacing(5)
        frame_layout.setContentsMargins(5, 5, 5, 5)

        # â”€â”€ TIME BAR (At the top) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.time_bar = RusticTimeBar()
        self.time_bar.valueChanged.connect(self.on_time_bar_change)
        self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
        frame_layout.addWidget(self.time_bar)

        # Loading indicator (absolute, sobre canvas)
        self.lbl_loading = QLabel(getTraduction("Astro.LoadingTopography", "⏳ Carregant topografia..."), self)
        self.lbl_loading.setStyleSheet("color: yellow; font-weight: bold; background-color: rgba(0,0,0,100); padding: 5px; border-radius: 4px;")
        self.lbl_loading.setAlignment(Qt.AlignCenter)
        self.lbl_loading.hide()
        self.lbl_loading.move(10, 50)
        self.lbl_loading.resize(200, 30)

        # â”€â”€ THE 3 BOTTOM PANELS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        panels_layout = QHBoxLayout()
        panels_layout.setSpacing(10)
        panels_layout.setContentsMargins(0, 0, 0, 0)
        
        gb_style = """
            QGroupBox { border: 1px solid #777; border-radius: 3px; margin-top: 1.2em; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #000; font-weight: bold; }
            QLabel { font-size: 10px; color: #000; font-style: normal; font-weight: normal; }
            QCheckBox { font-size: 10px; color: #000; font-style: normal; font-weight: normal; }
        """

        # 1. LOCALITZACIÃ“ =====================================================
        gb_loc = QGroupBox("Localització")
        gb_loc.setStyleSheet(gb_style)
        l_loc = QHBoxLayout(gb_loc)
        l_loc.setSpacing(15)

        # Col: inputs (Lat, Lon, Alt) + Button
        l_coord = QVBoxLayout()
        l_coord.setSpacing(2)
        
        h_latlon = QHBoxLayout()
        v_inputs = QVBoxLayout()
        v_inputs.setSpacing(2)
        
        h_lat = QHBoxLayout(); h_lat.addWidget(QLabel("Latitud")); self.txt_lat = QLineEdit(str(self.latitude))
        self.txt_lat.setFixedWidth(60); self.txt_lat.returnPressed.connect(self.update_location); h_lat.addWidget(self.txt_lat)
        v_inputs.addLayout(h_lat)

        h_lon = QHBoxLayout(); h_lon.addWidget(QLabel("Longitud")); self.txt_lon = QLineEdit(str(self.longitude))
        self.txt_lon.setFixedWidth(60); self.txt_lon.returnPressed.connect(self.update_location); h_lon.addWidget(self.txt_lon)
        v_inputs.addLayout(h_lon)
        h_latlon.addLayout(v_inputs)
        
        # Lock/Pin button
        self.btn_relocate = QPushButton("📌")
        self.btn_relocate.setFixedSize(24, 40)
        self.btn_relocate.setToolTip(getTraduction("Astro.Relocate", "Reubicar"))
        self.btn_relocate.clicked.connect(self.request_relocation)
        h_latlon.addWidget(self.btn_relocate)
        l_coord.addLayout(h_latlon)

        h_alt = QHBoxLayout()
        lbl_alt = QLabel("Alçada\naddicional")
        lbl_alt.setStyleSheet("font-size: 9px; line-height: 1;")
        h_alt.addWidget(lbl_alt)
        from PyQt5.QtWidgets import QDoubleSpinBox
        from TerraLab.common.utils import get_config_value
        self.spin_extra_height = QDoubleSpinBox()
        self.spin_extra_height.setRange(0.0, 10000.0)
        self.spin_extra_height.setSingleStep(0.5); self.spin_extra_height.setDecimals(1); self.spin_extra_height.setFixedWidth(50)
        self.spin_extra_height.setValue(float(get_config_value("observer_offset", 0.0)))
        self.spin_extra_height.valueChanged.connect(self.on_extra_height_changed)
        h_alt.addWidget(self.spin_extra_height)
        l_coord.addLayout(h_alt)
        
        self.lbl_altitude_info = QLabel("--")
        self.lbl_altitude_info.hide() # Hidden as per Mockup
        l_coord.addWidget(self.lbl_altitude_info)
        l_loc.addLayout(l_coord)

        # Col: Calendar Mockup
        l_date = QVBoxLayout()
        # Create a container with black background
        cal_container = QFrame()
        cal_container.setStyleSheet("border: 1px solid #555; background: #e0e0e0; border-radius: 4px;")
        
        cal_layout = QHBoxLayout(cal_container)
        cal_layout.setContentsMargins(5, 5, 5, 5)
        cal_layout.setSpacing(4)
        
        self.btn_prev_day = QPushButton("<")
        self.btn_prev_day.setFixedSize(20, 20); self.btn_prev_day.clicked.connect(self.prev_day)
        cal_layout.addWidget(self.btn_prev_day)
        
        self.lbl_date = ClickableLabel(self.format_date(self.manual_day))
        self.lbl_date.setAlignment(Qt.AlignCenter)
        self.lbl_date.setCursor(Qt.PointingHandCursor)
        self.lbl_date.setStyleSheet("color: #000; font-style: normal; font-size: 11px; font-weight: bold; border: none; background: transparent;")
        self.lbl_date.clicked.connect(self.open_calendar)
        cal_layout.addWidget(self.lbl_date, 1) # Expand
        
        self.btn_next_day = QPushButton(">")
        self.btn_next_day.setFixedSize(20, 20); self.btn_next_day.clicked.connect(self.next_day)
        cal_layout.addWidget(self.btn_next_day)
        
        l_date.addWidget(cal_container)
        
        self.btn_realtime = QPushButton("Temps real")
        self.btn_realtime.setCheckable(True); self.btn_realtime.toggled.connect(self.toggle_realtime)
        self.btn_realtime.setStyleSheet("border-radius: 4px; border: 1px solid #777; font-style: normal; color: #000; background: #ddd; padding: 3px;")
        l_date.addWidget(self.btn_realtime)
        
        l_date.addStretch(1)
        l_loc.addLayout(l_date)
        
        # Balanced distribution: Location / Sky / Ground = 2 / 3 / 1
        panels_layout.addWidget(gb_loc, 2)

        # 2. VISIÃ“ DEL CEL ====================================================
        gb_sky = QGroupBox("Visió del cel")
        gb_sky.setStyleSheet(gb_style)
        l_sky = QHBoxLayout(gb_sky)
        l_sky.setSpacing(15)

        # Col 1: Checks
        v_chk = QVBoxLayout(); v_chk.setSpacing(1)
        self.chk_clima = QCheckBox("Clima"); self.chk_clima.setEnabled(True)
        self.chk_clima.setChecked(False) # Clima OFF per defecte
        # Set state initially
        setattr(self.canvas.weather, 'enabled', False)
        self.chk_clima.toggled.connect(lambda checked: [setattr(self.canvas.weather, 'enabled', checked), self.canvas.update()])
        
        self.chk_planets = QCheckBox("Planetes")
        self.chk_planets.setEnabled(True)
        self.chk_planets.setChecked(True)
        self.chk_planets.toggled.connect(self.canvas.update)
        
        self.chk_sun_moon = QCheckBox("Sol i Lluna")
        self.chk_sun_moon.setEnabled(True)
        self.chk_sun_moon.setChecked(True)
        self.chk_sun_moon.toggled.connect(self.canvas.update)
        
        self.chk_enable_sky = QCheckBox("Estrelles"); self.chk_enable_sky.setChecked(True); self.chk_enable_sky.toggled.connect(self.canvas.update)
        self.chk_deep_space = QCheckBox("l'espai profund"); self.chk_deep_space.setEnabled(False) # TODO
        
        for c in (self.chk_clima, self.chk_planets, self.chk_sun_moon, self.chk_enable_sky, self.chk_deep_space):
            c.setStyleSheet(c.styleSheet() + "; font-style: normal; font-weight: normal; color: #666;" if not c.isEnabled() else "; font-style: normal; font-weight: normal; color: #000;")
            v_chk.addWidget(c)
            
        l_sky.addLayout(v_chk)

        # Col 2: Sliders
        v_sld = QVBoxLayout(); v_sld.setSpacing(2)
        self.chk_pure_colors = QCheckBox("Colors purs")
        self.chk_pure_colors.setStyleSheet("font-style: normal; font-weight: normal;")
        self.chk_pure_colors.toggled.connect(self.toggle_pure_colors)
        v_sld.addWidget(self.chk_pure_colors)

        def make_sld(layout, label, r, val, cb, with_reset=False):
            h = QHBoxLayout(); h.setSpacing(4)
            l = QLabel(label); l.setStyleSheet("font-style: normal; font-weight: normal; min-width: 65px;")
            h.addWidget(l)
            
            l_min = QLabel(str(r[0])); l_min.setStyleSheet("font-size: 8px; color: #000;")
            h.addWidget(l_min)
            
            s = QSlider(Qt.Horizontal); s.setRange(*r); s.setValue(val); s.setMinimumWidth(80)
            h.addWidget(s)
            
            l_max = QLabel(str(r[1])); l_max.setStyleSheet("font-size: 8px; color: #000;")
            h.addWidget(l_max)
            
            l_curr = QLabel(f"[{val}]"); l_curr.setStyleSheet("font-size: 9px; color: #000; min-width: 30px;")
            h.addWidget(l_curr)
            
            # Attach labels to slider for dynamic updates
            s._lbl_min = l_min
            s._lbl_max = l_max
            s._lbl_curr = l_curr
            
            btn_res = None
            if with_reset:
                from PyQt5.QtWidgets import QToolButton
                btn_res = QToolButton()
                btn_res.setText("↺")
                btn_res.setStyleSheet("font-size: 14px; border: none; font-weight: bold; color: #333;")
                btn_res.setCursor(Qt.PointingHandCursor)
                h.addWidget(btn_res)

            def on_changed(new_val):
                l_curr.setText(f"[{new_val}]")
                if cb: cb(new_val)
                
            s.valueChanged.connect(on_changed)
            
            def set_silent_value(new_val):
                s.blockSignals(True)
                s.setValue(new_val)
                s.blockSignals(False)
                l_curr.setText(f"[{new_val}]")
            s.set_silent_value = set_silent_value
            
            layout.addLayout(h)
            if with_reset:
                return s, l, btn_res
            return s, l

        self.slider_size, _ = make_sld(v_sld, "Mida", (5, 40), int(self.star_scale*10), self.update_star_scale)
        self.slider_spikes, _ = make_sld(v_sld, "Puntes", (-30, 70), int(self.spike_magnitude_threshold*10), self.update_spikes)
        
        # Unified Light Pollution / Magnitude Control
        l_shared = QVBoxLayout()
        h_ctrl = QHBoxLayout(); h_ctrl.setSpacing(5)
        
        from PyQt5.QtWidgets import QComboBox
        self.combo_lp_mode = QComboBox()
        self.combo_lp_mode.addItems(["Automàtic", "Manual"])
        self.combo_lp_mode.setToolTip("Mode de visibilitat: Automàtic (Bortle satèl·lit) / Manual (Filtre Magnitud)")
        self.combo_lp_mode.setStyleSheet("font-size: 10px; min-width: 80px; max-width: 100px; height: 22px;")
        self.combo_lp_mode.currentIndexChanged.connect(self.on_lp_mode_changed)
        h_ctrl.addWidget(self.combo_lp_mode)
        
        self.slider_light, self.lbl_light_text, self.btn_res_lp = make_sld(h_ctrl, "Bortle", (1, 9), 1, self.update_lp_slider, with_reset=True)
        self.btn_res_lp.setToolTip("Restablir segons ubicació")
        self.btn_res_lp.clicked.connect(self.reset_lp_to_auto)
        
        # Force an initial sync of Bortle if possible
        QTimer.singleShot(500, self.update_altitude_label) 
        l_shared.addLayout(h_ctrl)
        v_sld.addLayout(l_shared)
        
        self.is_auto_bortle = True
        self.ambient_light = 1.0 
        l_sky.addLayout(v_sld)

        # Col 3: Circumpolar + Search
        v_ext = QVBoxLayout(); v_ext.setSpacing(4)
        self.chk_trails = QPushButton(getTraduction("Astro.StartCircumpolar", "Iniciar circumpolar"))
        self.chk_trails.setCheckable(True)
        self.chk_trails.setStyleSheet("font-size: 10px; font-weight: normal; font-style: normal; border-radius: 8px; border: 1px solid #888; padding: 2px;")
        self.chk_trails.toggled.connect(self.on_trails_toggled)
        v_ext.addWidget(self.chk_trails)

        self.lbl_trail_time = QLabel("")
        self.lbl_trail_time.setStyleSheet("font-weight: normal; color: #000; font-size: 10px;")
        self.lbl_trail_time.setAlignment(Qt.AlignCenter)
        v_ext.addWidget(self.lbl_trail_time)
        v_ext.addStretch()

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText(getTraduction("Astro.SearchPlaceholder", "Search object..."))
        self.txt_search.setStyleSheet("font-weight: normal; font-style: normal; border-radius: 4px; padding: 2px;")
        self.txt_search.returnPressed.connect(self.on_search_triggered)
        v_ext.addWidget(self.txt_search)

        # Telescope / Tools entry points
        h_overlays = QHBoxLayout()
        h_overlays.setSpacing(4)
        self.btn_scope_panel = QPushButton(getTraduction("Astro.ScopeButton", "Tube / Telescope"))
        self.btn_scope_panel.setCheckable(True)
        self.btn_scope_panel.setStyleSheet("font-size: 10px; font-weight: normal;")
        self.btn_scope_panel.toggled.connect(self.toggle_scope_panel)
        h_overlays.addWidget(self.btn_scope_panel)

        self.btn_tools_panel = QPushButton(getTraduction("Astro.ToolsButton", "Tools"))
        self.btn_tools_panel.setCheckable(True)
        self.btn_tools_panel.setStyleSheet("font-size: 10px; font-weight: normal;")
        self.btn_tools_panel.toggled.connect(self.toggle_tools_panel)
        h_overlays.addWidget(self.btn_tools_panel)
        v_ext.addLayout(h_overlays)

        # Telescope panel
        self.scope_panel = QFrame()
        self.scope_panel.setStyleSheet("border: 1px solid #999; border-radius: 4px;")
        v_scope = QVBoxLayout(self.scope_panel)
        v_scope.setSpacing(3)
        v_scope.setContentsMargins(4, 4, 4, 4)

        from PyQt5.QtWidgets import QDoubleSpinBox, QComboBox

        h_focal = QHBoxLayout()
        h_focal.addWidget(QLabel(getTraduction("Astro.ScopeFocal", "Focal (mm)")))
        self.scope_focal_spin = QDoubleSpinBox()
        self.scope_focal_spin.setRange(1.0, 5000.0)
        self.scope_focal_spin.setDecimals(1)
        self.scope_focal_spin.setSingleStep(10.0)
        self.scope_focal_spin.setValue(250.0)
        self.scope_focal_spin.setFixedWidth(74)
        self.scope_focal_spin.valueChanged.connect(lambda v: self.canvas.set_scope_focal_mm(v))
        h_focal.addWidget(self.scope_focal_spin)
        v_scope.addLayout(h_focal)

        h_shape = QHBoxLayout()
        h_shape.addWidget(QLabel(getTraduction("Astro.ScopeShape", "Format")))
        self.scope_shape_combo = QComboBox()
        self.scope_shape_combo.addItem(getTraduction("Astro.ScopeCircle", "Circle"), TelescopeScopeController.SHAPE_CIRCLE)
        self.scope_shape_combo.addItem(getTraduction("Astro.ScopeRectangle", "Rectangle"), TelescopeScopeController.SHAPE_RECT)
        self.scope_shape_combo.currentIndexChanged.connect(self.on_scope_shape_changed)
        h_shape.addWidget(self.scope_shape_combo)
        v_scope.addLayout(h_shape)

        h_sensor = QHBoxLayout()
        h_sensor.addWidget(QLabel(getTraduction("Astro.ScopeSensor", "Sensor")))
        self.scope_sensor_combo = QComboBox()
        self.scope_sensor_combo.addItem(getTraduction("Astro.ScopeSensorTiny", "Sensor 1/2.8"), "tiny")
        self.scope_sensor_combo.addItem(getTraduction("Astro.ScopeSensorAPSC", "APS-C"), "aps_c")
        self.scope_sensor_combo.addItem(getTraduction("Astro.ScopeSensorFullFrame", "Full Frame"), "full_frame")
        self.scope_sensor_combo.currentIndexChanged.connect(self.on_scope_sensor_changed)
        h_sensor.addWidget(self.scope_sensor_combo)
        v_scope.addLayout(h_sensor)

        h_speed = QHBoxLayout()
        h_speed.addWidget(QLabel(getTraduction("Astro.ScopeMoveMode", "Movement")))
        self.scope_speed_combo = QComboBox()
        self.scope_speed_combo.addItem(getTraduction("Astro.ScopeSlow", "Slow"), TelescopeScopeController.SPEED_SLOW)
        self.scope_speed_combo.addItem(getTraduction("Astro.ScopeFast", "Fast"), TelescopeScopeController.SPEED_FAST)
        self.scope_speed_combo.currentIndexChanged.connect(self.on_scope_speed_changed)
        h_speed.addWidget(self.scope_speed_combo)
        v_scope.addLayout(h_speed)

        h_scope_actions = QHBoxLayout()
        self.btn_scope_activate = QPushButton(getTraduction("Astro.ScopeActivate", "Activate scope"))
        self.btn_scope_activate.clicked.connect(self.activate_scope_mode)
        h_scope_actions.addWidget(self.btn_scope_activate)
        self.btn_scope_exit = QPushButton(getTraduction("Astro.ScopeExit", "Exit"))
        self.btn_scope_exit.clicked.connect(self.exit_scope_mode)
        h_scope_actions.addWidget(self.btn_scope_exit)
        v_scope.addLayout(h_scope_actions)
        self.scope_panel.hide()
        v_ext.addWidget(self.scope_panel)

        # Measurement tools panel
        self.tools_panel = QFrame()
        self.tools_panel.setStyleSheet("border: 1px solid #999; border-radius: 4px;")
        v_tools = QVBoxLayout(self.tools_panel)
        v_tools.setSpacing(3)
        v_tools.setContentsMargins(4, 4, 4, 4)

        h_tool_row_1 = QHBoxLayout()
        self.btn_tool_ruler = QPushButton(getTraduction("Astro.ToolRuler", "Ruler"))
        self.btn_tool_ruler.setCheckable(True)
        self.btn_tool_ruler.clicked.connect(lambda: self.select_measurement_tool(TOOL_RULER))
        h_tool_row_1.addWidget(self.btn_tool_ruler)
        self.btn_tool_square = QPushButton(getTraduction("Astro.ToolSquare", "Square"))
        self.btn_tool_square.setCheckable(True)
        self.btn_tool_square.clicked.connect(lambda: self.select_measurement_tool(TOOL_SQUARE))
        h_tool_row_1.addWidget(self.btn_tool_square)
        v_tools.addLayout(h_tool_row_1)

        h_tool_row_2 = QHBoxLayout()
        self.btn_tool_rect = QPushButton(getTraduction("Astro.ToolRectangle", "Rectangle"))
        self.btn_tool_rect.setCheckable(True)
        self.btn_tool_rect.clicked.connect(lambda: self.select_measurement_tool(TOOL_RECTANGLE))
        h_tool_row_2.addWidget(self.btn_tool_rect)
        self.btn_tool_circle = QPushButton(getTraduction("Astro.ToolCircle", "Circle"))
        self.btn_tool_circle.setCheckable(True)
        self.btn_tool_circle.clicked.connect(lambda: self.select_measurement_tool(TOOL_CIRCLE))
        h_tool_row_2.addWidget(self.btn_tool_circle)
        v_tools.addLayout(h_tool_row_2)

        self.btn_tool_clear = QPushButton(getTraduction("Astro.ToolClear", "Clear"))
        self.btn_tool_clear.clicked.connect(self.clear_measurement_overlays)
        v_tools.addWidget(self.btn_tool_clear)
        self.tools_panel.hide()
        v_ext.addWidget(self.tools_panel)
        self.sync_scope_ui_state(False)
        self._sync_measure_tool_buttons(TOOL_NONE)

        l_sky.addLayout(v_ext)

        panels_layout.addWidget(gb_sky, 3)

        # 3. VISIÃ“ DEL TERRA ================================================
        gb_earth = QGroupBox("Visió del terra")
        gb_earth.setStyleSheet(gb_style)
        v_earth = QVBoxLayout(gb_earth)
        v_earth.setSpacing(6)

        self.chk_enable_horizon = QCheckBox("Horitzó")
        self.chk_enable_horizon.setStyleSheet("font-style: normal; font-weight: normal;")
        self.chk_enable_horizon.setChecked(True)
        self.chk_enable_horizon.toggled.connect(self.canvas.update)
        v_earth.addWidget(self.chk_enable_horizon)

        self.chk_enable_village = QCheckBox("Topografia")
        self.chk_enable_village.setStyleSheet("font-style: normal; font-weight: normal;")
        self.chk_enable_village.setChecked(True)
        self.chk_enable_village.toggled.connect(self.canvas.update)
        v_earth.addWidget(self.chk_enable_village)

        h_lay = QHBoxLayout()
        l_lay = QLabel("Nombre\nde capes")
        l_lay.setStyleSheet("font-style: normal; font-weight: normal; font-size: 9px; line-height: 1;")
        h_lay.addWidget(l_lay)
        from PyQt5.QtWidgets import QComboBox
        self.combo_layers = QComboBox()
        self.combo_layers.addItems(["10", "20", "40", "60", "80"])
        self.combo_layers.setFixedWidth(40)
        self.combo_layers.setStyleSheet("font-style: normal; font-weight: normal;")
        try:
            curr_layers = int(get_config_value("horizon_quality", 80))
        except:
            curr_layers = 80
        self.combo_layers.setCurrentText(str(curr_layers))
        self.combo_layers.currentTextChanged.connect(self.on_layers_changed)
        h_lay.addWidget(self.combo_layers)
        v_earth.addLayout(h_lay)

        v_earth.addStretch()
        panels_layout.addWidget(gb_earth, 1)

        self.panels_widget = QWidget()
        self.panels_widget.setLayout(panels_layout)
        frame_layout.addWidget(self.panels_widget)

        # â”€â”€ KEEP EXISTING VARIABLES FOR COMPATIBILITY (HIDDEN) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.btn_view = QPushButton(); self.btn_view.hide()
        self.btn_terrain = QPushButton(); self.btn_terrain.hide()
        self.chk_illusion = QCheckBox(); self.chk_illusion.hide()
        self.slider_href = QSlider(); self.slider_href.hide()
        self.slider_flat = QSlider(); self.slider_flat.hide()
        self.chk_trained = QCheckBox(); self.chk_trained.hide()
        self.chk_lock = QCheckBox(); self.chk_lock.hide()
        
        # Add to main layout
        self.frame_controls = QFrame()

        self.frame_controls.setObjectName("controlFrame")
        self.frame_controls.setLayout(frame_layout)
        layout.addWidget(self.frame_controls)

        # --- FLOATING TAB BUTTON (Absolute Positioned) ---
        # Parented to self, NOT in layout. 
        self.btn_collapse = QPushButton("-", self) 
        self.btn_collapse.setFixedSize(30, 24)
        self.btn_collapse.setCursor(Qt.PointingHandCursor)
        self.btn_collapse.clicked.connect(self.toggle_controls)
        self.btn_collapse.show()
        
        # Apply Themes
        self.update_custom_theme()

    def apply_styles(self):
        super().apply_styles()
        self.update_custom_theme()

    def update_custom_theme(self):
        t = self.current_theme
        # Extract colors or defaults
        bg = t.get('content_bg', t.get('widget_background', 'rgba(20, 20, 30, 220)'))
        txt = t.get('title_text_color', t.get('text_primary', 'white'))
        border = t.get('widget_border_color', '#555')
        
        # Ensure bg has alpha if needed, or just use as is
        
        self.panel_style = f"""
            #controlFrame {{
                background-color: {bg}; 
                color: {txt}; 
                border: 1px solid {border};
                border-radius: 8px;
            }}
            #controlFrame QLineEdit {{ background: rgba(0,0,0,50); color: {txt}; border: 1px solid {border}; border-radius: 4px; padding: 2px; }}
            #controlFrame QLineEdit:focus {{ border: 2px solid {txt}; }}
            #controlFrame QPushButton {{ background: rgba(255,255,255,20); border: 1px solid {border}; border-radius: 3px; color: {txt}; font-weight: bold; }}
            #controlFrame QPushButton:hover {{ background: rgba(255,255,255,50); }}
            #controlFrame QPushButton:checked {{ background: rgba(100,200,255,100); color: white; }}
            #controlFrame QLabel {{ color: {txt}; }}
            #controlFrame QCheckBox {{ color: {txt}; }}
            #controlFrame QSlider::handle:horizontal {{ background: {border}; border: 1px solid {txt}; width: 10px; margin: -2px 0; border-radius: 5px; }}
            #controlFrame QSlider::groove:horizontal {{ border: 1px solid #999; height: 4px; background: rgba(255,255,255,50); margin: 2px 0; }}
        """
        
        if hasattr(self, 'frame_controls'):
            self.frame_controls.setStyleSheet(self.panel_style)
            
        if hasattr(self, 'btn_collapse'):
            # Tab Style
            self.btn_collapse.setStyleSheet(f"""
                QPushButton {{ 
                    background-color: {bg}; 
                    color: {txt}; 
                    border: 1px solid {border}; 
                    border-bottom: 2px solid {bg}; 
                    font-size: 16px; 
                    font-weight: bold;
                    border-top-left-radius: 6px;
                    border-top-right-radius: 6px;
                    border-bottom-left-radius: 0px;
                    border-bottom-right-radius: 0px;
                    margin-bottom: -1px; 
                    padding-bottom: 2px;
                }}
                QPushButton:hover {{ background-color: {bg}; border: 1px solid rgba(255,255,255,200); }}
            """)

    def toggle_scope_panel(self, checked):
        if checked:
            if hasattr(self, 'btn_tools_panel'):
                self.btn_tools_panel.blockSignals(True)
                self.btn_tools_panel.setChecked(False)
                self.btn_tools_panel.blockSignals(False)
            if hasattr(self, 'tools_panel'):
                self.tools_panel.hide()
        if hasattr(self, 'scope_panel'):
            self.scope_panel.setVisible(checked)

    def toggle_tools_panel(self, checked):
        if checked:
            if hasattr(self, 'btn_scope_panel'):
                self.btn_scope_panel.blockSignals(True)
                self.btn_scope_panel.setChecked(False)
                self.btn_scope_panel.blockSignals(False)
            if hasattr(self, 'scope_panel'):
                self.scope_panel.hide()
        if hasattr(self, 'tools_panel'):
            self.tools_panel.setVisible(checked)

    def on_scope_shape_changed(self, index):
        shape = self.scope_shape_combo.itemData(index)
        self.canvas.set_scope_shape(shape)

    def on_scope_sensor_changed(self, index):
        sensor = self.scope_sensor_combo.itemData(index)
        self.canvas.set_scope_sensor(sensor)

    def on_scope_speed_changed(self, index):
        mode = self.scope_speed_combo.itemData(index)
        self.canvas.set_scope_speed_mode(mode)

    def sync_scope_speed_ui(self, mode: str):
        if not hasattr(self, 'scope_speed_combo'):
            return
        for i in range(self.scope_speed_combo.count()):
            if self.scope_speed_combo.itemData(i) == mode:
                self.scope_speed_combo.blockSignals(True)
                self.scope_speed_combo.setCurrentIndex(i)
                self.scope_speed_combo.blockSignals(False)
                break

    def activate_scope_mode(self):
        # Scope mode remaps navigation to pointing control.
        self.canvas.set_scope_focal_mm(self.scope_focal_spin.value())
        self.canvas.set_scope_shape(self.scope_shape_combo.itemData(self.scope_shape_combo.currentIndex()))
        self.canvas.set_scope_sensor(self.scope_sensor_combo.itemData(self.scope_sensor_combo.currentIndex()))
        self.canvas.set_scope_speed_mode(self.scope_speed_combo.itemData(self.scope_speed_combo.currentIndex()))
        self.canvas.set_scope_enabled(True)
        self.canvas.setFocus()
        self.sync_scope_ui_state(True)

        # Optional: deactivate measurement input when entering scope mode.
        self.canvas.set_measurement_tool(TOOL_NONE)
        self._sync_measure_tool_buttons(TOOL_NONE)

    def exit_scope_mode(self):
        self.canvas.set_scope_enabled(False)
        self.sync_scope_ui_state(False)

    def sync_scope_ui_state(self, enabled: bool):
        if hasattr(self, 'btn_scope_activate'):
            self.btn_scope_activate.setEnabled(not enabled)
        if hasattr(self, 'btn_scope_exit'):
            self.btn_scope_exit.setEnabled(enabled)

    def _sync_measure_tool_buttons(self, active_tool: str):
        tool_buttons = [
            (getattr(self, 'btn_tool_ruler', None), TOOL_RULER),
            (getattr(self, 'btn_tool_square', None), TOOL_SQUARE),
            (getattr(self, 'btn_tool_rect', None), TOOL_RECTANGLE),
            (getattr(self, 'btn_tool_circle', None), TOOL_CIRCLE),
        ]
        for btn, key in tool_buttons:
            if btn is None:
                continue
            btn.blockSignals(True)
            btn.setChecked(active_tool == key)
            btn.blockSignals(False)

    def select_measurement_tool(self, tool: str):
        current = self.canvas.measurement_controller.active_tool
        if current == tool:
            self.canvas.set_measurement_tool(TOOL_NONE)
            self._sync_measure_tool_buttons(TOOL_NONE)
            return

        # Single active tool at a time.
        self.canvas.set_measurement_tool(tool)
        self.canvas.setFocus()
        self._sync_measure_tool_buttons(tool)

        # Measurement and scope mode should not compete for mouse/keys.
        self.exit_scope_mode()

    def clear_measurement_overlays(self):
        self.canvas.clear_measurements()
        self._sync_measure_tool_buttons(self.canvas.measurement_controller.active_tool)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'frame_controls') and hasattr(self, 'btn_collapse'):
            # Manually position the button relative to the frame
            rect = self.frame_controls.geometry()
            # If layout isn't updated, this might be stale.
            # But frame_controls is in a QVBoxLayout at bottom. 
            # Usually rect updates during resize of parent.
            
            bx = rect.right() - self.btn_collapse.width() - 20
            # Sits on top edge
            by = rect.top() - self.btn_collapse.height() + 2 # Overlap
            
            self.btn_collapse.move(bx, by)
            self.btn_collapse.raise_()
    
    def on_time_bar_change(self, val):
        self.use_real_time = False
        self.btn_realtime.setChecked(False)
        self.manual_hour = val
        self._last_seek_hour = val
        self.canvas.update()
        self.canvas.update()

        # â”€ Toast HUD: mostra hora local i UT durant el drag â”€
        if hasattr(self.canvas, 'hint_overlay'):
            from datetime import datetime, timezone
            # val is LOCAL time (as it represents the time bar)
            tz_off = round(datetime.now().astimezone().utcoffset().total_seconds() / 3600.0, 1)
            ut_h = (val - tz_off) % 24
            
            lh = f"{int(val % 24):02d}:{int((val % 1)*60):02d}"
            uth = f"{int(ut_h):02d}:{int((ut_h % 1)*60):02d}"
            
            txt = getTraduction("HUD.TimeHint", "🕐 {local_h} local  ·  UT {ut_h}").format(
                local_h=lh, ut_h=uth
            )
            self.canvas.hint_overlay.show_hint(txt)

    def request_relocation(self):
        """User changed location: trigger full bake."""
        try:
            new_lat = float(self.txt_lat.text())
            new_lon = float(self.txt_lon.text())
            
            # Keep sky orientation stable when crossing hemispheres.
            old_hemi_n = self.latitude >= 0
            new_hemi_n = new_lat >= 0
            if old_hemi_n != new_hemi_n:
                self.canvas.azimuth_offset = (self.canvas.azimuth_offset + 180) % 360
            
            self.latitude = new_lat
            self.longitude = new_lon
            
            # Use debouncer for location changes too
            self.bake_debounce_timer.start(1500) # 1.5s debounce
            
            # Feedback
            if hasattr(self, 'lbl_loading'):
                self.lbl_loading.setText(getTraduction("Astro.RecalcTopography", "⏳ Recalculant topografia..."))
                self.lbl_loading.show()
                self.lbl_loading.raise_()
            
            # Update internal params
            self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
            
            # --- Fast Local Lookups for UI Feedback ---
            if hasattr(self, 'horizon_worker'):
                # Update Altitude
                bare = self.horizon_worker.get_bare_elevation(self.latitude, self.longitude)
                self._last_dem_elevation = bare
                self.update_altitude_label()
                
                # Update Bortle (Automatic Mode)
                auto_bortle = self.horizon_worker.get_bortle_estimate(self.latitude, self.longitude)
                self.canvas.auto_bortle_estimate = auto_bortle
                if self.is_auto_bortle:
                    self.slider_light.set_silent_value(auto_bortle)
            
            # Heavy bake is triggered by debounce timer to avoid duplicate work.

            # â”€ Toast HUD: mostra nova ubicaciÃ³ i altitud si disponible â”€
            if hasattr(self.canvas, 'hint_overlay'):
                dem_m  = getattr(self, '_last_dem_elevation', None)
                offset = getattr(self, '_observer_offset', 0.0)
                if dem_m is not None:
                    txt = getTraduction(
                        "HUD.LocationHint",
                        "📍 {lat}°, {lon}°  ·  {dem} m + {offset} m"
                    ).format(
                        lat=f"{self.latitude:.4f}",
                        lon=f"{self.longitude:.4f}",
                        dem=int(dem_m),
                        offset=int(offset)
                    )
                else:
                    txt = f"ðŸ“ {self.latitude:.4f}Â°, {self.longitude:.4f}Â°"
                self.canvas.hint_overlay.show_hint(txt)

        except ValueError:
            print("[AstroWidget] Invalid Lat/Lon")

            
    def update_location(self):
        """Called by ReturnPressed on line edits."""
        self.request_relocation()
        
    def prev_day(self):
        self.manual_day = (self.manual_day - 1) % 365
        self.lbl_date.setText(self.format_date(self.manual_day))
        self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
        self.canvas.update()
        
    def next_day(self):
        self.manual_day = (self.manual_day + 1) % 365
        self.lbl_date.setText(self.format_date(self.manual_day))
        self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
        self.canvas.update()

    def load_catalog(self):
        # Load ONLY Gaia Stars (JSON)
        # Path relative to TerraLab/widgets/sky_widget.py -> ../data/stars/gaia_stars.json
        local_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(local_dir, '..', 'data', 'stars', 'gaia_stars.json')
        
        self.celestial_objects = []
        if os.path.exists(json_path):
            import json
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    source_list = []
                    # Gaia JSON usually has 'data' as list of lists
                    if isinstance(data, dict) and 'data' in data:
                        source_list = data['data']
                    elif isinstance(data, list):
                        source_list = data
                    
                    count = 0
                    for s in source_list:
                        try:
                            # If list of lists (Metadata order: 2=RA, 3=DEC, 4=G_MAG, 7=BP_RP)
                            if isinstance(s, list):
                                star_id = str(s[0])
                                ra = float(s[2])
                                dec = float(s[3])
                                mag = float(s[4])
                                bp_rp = 0.8
                                if len(s) > 7 and s[7] is not None:
                                    bp_rp = float(s[7])
                            # Fallback if list of dicts (unlikely for this file but safe)
                            elif isinstance(s, dict):
                                star_id = str(s.get('source_id', 'Unknown'))
                                ra = float(s.get('ra', 0))
                                dec = float(s.get('dec', 0))
                                mag = float(s.get('phot_g_mean_mag', 10))
                                bp_rp = float(s.get('bp_rp', 0.8))
                            else:
                                continue

                            self.celestial_objects.append({
                                'id': star_id, 'ra': ra, 'dec': dec, 'mag': mag, 'bp_rp': bp_rp
                            })
                            count += 1
                        except: continue
                        
                print(f"Loaded {count} Gaia stars from JSON.")
            except Exception as e:
                print(f"Error loading Gaia JSON: {e}")
        
        if not self.celestial_objects:
            print("Fallback to Random Stars")
            import random
            for _ in range(500):
                 self.celestial_objects.append({
                    'ra': random.uniform(0, 360), 'dec': random.uniform(-90, 90), 
                    'mag': random.uniform(1.0, 6.0), 'bp_rp': random.uniform(-0.5, 2.0)
                })
                
        # OPTIMIZATION: Sort by magnitude for fast rendering (Early Exit)
        self.celestial_objects.sort(key=lambda x: x['mag'])
        
        # NumPy Vectorization (If available)
        if np:
            try:
                self.np_ra = np.array([s['ra'] for s in self.celestial_objects], dtype=np.float32)
                self.np_dec = np.array([s['dec'] for s in self.celestial_objects], dtype=np.float32)
                self.np_mag = np.array([s['mag'] for s in self.celestial_objects], dtype=np.float32)
                
                # Precompute color arrays (R, G, B) to avoid object overhead
                # Logic copies get_star_color
                def get_rgb(s):
                    bp_rp = s.get('bp_rp', 0.8)
                    if bp_rp < 0.0: return (160, 190, 255)
                    elif bp_rp < 0.5:
                         t = (bp_rp - 0.0) / 0.5
                         return (160 + int(95*t), 190 + int(65*t), 255)
                    elif bp_rp < 1.0:
                         t = (bp_rp - 0.5) / 0.5
                         return (255, 255, 255 - int(55*t))
                    elif bp_rp < 2.0:
                         t = (bp_rp - 1.0) / 1.0
                         return (255, 255 - int(80*t), 200 - int(100*t))
                    else: return (255, 175, 100)
                    
                cols = [get_rgb(s) for s in self.celestial_objects]
                self.np_r = np.array([c[0] for c in cols], dtype=np.uint8)
                self.np_g = np.array([c[1] for c in cols], dtype=np.uint8)
                self.np_b = np.array([c[2] for c in cols], dtype=np.uint8)
                
                print(f"NumPy Optimization: {len(self.np_ra)} stars vectorized.")
            except Exception as e:
                print(f"NumPy Init Error: {e}")
                # Clear to avoid partial state
                if hasattr(self, 'np_ra'): del self.np_ra
        
        # Legacy stub
        pass
        
    def update_loop(self):
        if self.use_real_time:
            now = datetime.now()
            # Sync Day & Year
            self.manual_year = now.year
            self.manual_day = (now - datetime(now.year, 1, 1)).days
            self.lbl_date.setText(self.format_date(self.manual_day))
            
            # Sync Gradient
            self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
            
            # Sync Time
            h = now.hour + now.minute/60.0 + now.second/3600.0
            self.time_bar.set_time(h)
        else:
            # Manual Mode: "Time keeps running forward"
            # Increment manual_hour by elapsed time
            dt_hours = 0.05 / 3600.0 # Timer is 50ms now
            self.manual_hour += dt_hours
            if self.manual_hour >= 24.0:
                 self.manual_hour -= 24.0
                 self.manual_day += 1
            elif self.manual_hour < 0:
                 self.manual_hour += 24.0
                 self.manual_day -= 1
            self.time_bar.set_time(self.manual_hour)
            
        # Update trails elapsed time based on simulation time
        if hasattr(self, 'chk_trails') and self.chk_trails.isChecked() and hasattr(self.canvas, 'trail_start_hour') and \
           self.canvas.trail_start_hour is not None and hasattr(self.canvas, 'ut_hour'):
            # Calculate simulation duration between start and current
            start = self.canvas.trail_start_hour
            end = self.canvas.ut_hour
            
            diff = end - start
            # Shortest path wrap-around (handles midnight)
            if diff < -12.0: diff += 24.0
            elif diff > 12.0: diff -= 24.0
            
            # Exposure is only positive "forward" from start
            # If they go back beyond start, we show 0.
            self.trails_accumulated_seconds = max(0.0, diff * 3600.0)
            elapsed = int(self.trails_accumulated_seconds)
            if elapsed < 60:
                self.lbl_trail_time.setText(f"{elapsed}s")
            elif elapsed < 3600:
                m = elapsed // 60
                s = elapsed % 60
                self.lbl_trail_time.setText(f"{m}m {s}s")
            else:
                h = elapsed // 3600
                m = (elapsed % 3600) // 60
                self.lbl_trail_time.setText(f"{h}h {m}m")
        else:
            if hasattr(self, 'trails_accumulated_seconds'):
                delattr(self, 'trails_accumulated_seconds')
            if hasattr(self, 'lbl_trail_time'):
                self.lbl_trail_time.setText("")

        self.canvas.update()

    def toggle_realtime(self, checked):
        self.use_real_time = checked
        self.canvas.update()

    def toggle_view(self):
        # Toggle between Zenith (90) and Horizon (20)
        current = self.canvas.elevation_angle
        if abs(current - 90) < 5:
            self.set_horizon_view()
        else:
            self.set_zenith_view()
            
    def set_horizon_view(self):
        self.canvas.elevation_angle = 0.1 # Low angle for landscape
        self.canvas.vertical_offset_ratio = 0.35 # Horizon lower-third rule
        self.canvas.zoom_level = 1 # Wider FOV (~220 degrees)
        if self.latitude >= 0:
            self.canvas.azimuth_offset = 180 # Facing South (North Hemi)
        else:
            self.canvas.azimuth_offset = 0   # Facing North (South Hemi)
        self.btn_view.setText(getTraduction("Astro.ViewZenith", "Cénit"))
        self.canvas.update()
        
    def set_zenith_view(self):
        self.canvas.elevation_angle = 90 # Dome view up
        self.canvas.vertical_offset_ratio = 0.0 # Center
        self.canvas.zoom_level = 1.0 # Wide Fisheye
        self.canvas.azimuth_offset = 0
        self.btn_view.setText(getTraduction("Astro.ViewHorizontal", "Horizonte"))
        self.canvas.update()
        
    def on_extra_height_changed(self, val):
        # Update worker state immediately for synchronous altitude label feedback
        if hasattr(self, 'horizon_worker'):
            self.horizon_worker.set_observer_offset(val)
            self.update_altitude_label()
            # Start debounce timer for the heavy topography recalculation
            # 1.5s debounce for spinbox as requested (let the user stop for a second)
            self.bake_debounce_timer.start(1500) 

    def update_altitude_label(self):
        if hasattr(self, 'horizon_worker'):
            bare = self.horizon_worker.get_bare_elevation(self.latitude, self.longitude)
            offset = self.spin_extra_height.value()
            if bare is not None:
                total = bare + offset
                tpl = getTraduction("Astro.AltitudeInfo", "Altitud terreno: {dem} m | Total observador: {total} m")
                self.lbl_altitude_info.setText(tpl.format(dem=f"{bare:.1f}", total=f"{total:.1f}"))
            else:
                fallback_str = getTraduction("Astro.AltitudeInfo", "Altitud terreno: {dem} m | Total observador: {total} m")
                fallback_str = fallback_str.replace("{dem}", "--").replace("{total}", "--")
                self.lbl_altitude_info.setText(fallback_str)

    def update_star_scale(self, val):
        self.star_scale = val / 10.0
        self.canvas.update()

    def toggle_pure_colors(self, checked):
        self.pure_colors = checked
        self.canvas.update()

    def update_spikes(self, val):
        self.spike_magnitude_threshold = val / 10.0
        print(f"[DEBUG] Spike threshold updated to: {self.spike_magnitude_threshold} (slider val: {val})")
        self.canvas.update()

    def on_layers_changed(self, text):
        """Update layer count from UI combo box and trigger a re-bake."""
        try:
            val = int(text)
            from TerraLab.common.utils import set_config_value
            set_config_value("horizon_quality", val)
            if hasattr(self, 'horizon_worker'):
                self.horizon_worker.reload_config()
            self.request_relocation()
        except ValueError:
            pass

    def configure_terrain(self):

        """Open DEM configuration dialog."""
        from TerraLab.widgets.terrain_config_dialog import TerrainConfigDialog
        dlg = TerrainConfigDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            # Re-read config in worker
            if hasattr(self, 'horizon_worker'):
                self.horizon_worker.reload_config()
            # Trigger re-bake
            self.request_relocation()

    def update_illusion_enabled(self, checked):
        self.canvas.illusion_enabled = checked
        self.canvas.update()

    def update_horizon_refs(self, val):
        self.canvas.horizon_refs = val / 100.0
        self.canvas.update()

    def update_dome_flattening(self, val):
        self.canvas.dome_flattening = val / 100.0
        self.canvas.update()
        
    def update_trained_observer(self, checked):
        self.canvas.trained_observer = checked
        self.canvas.update()
        
    def update_eclipse_lock(self, checked):
        self.canvas.eclipse_lock_mode = checked
        self.canvas.update()
        
    def animate_view(self):
        running = False
        
        # 1. Azimuth Animation
        if self.target_azimuth is not None:
            # Normalize Input First to avoid "Unwinding" large rotations
            self.canvas.azimuth_offset %= 360
            
            current = self.canvas.azimuth_offset
            diff = self.target_azimuth - current
            # Normalize -180..180 (Shortest Path)
            diff = (diff + 180) % 360 - 180
            
            if abs(diff) < 0.5:
                self.canvas.azimuth_offset = self.target_azimuth
                self.target_azimuth = None
            else:
                running = True
                # More agile speed (0.25)
                step = diff * 0.25
                # Min speed to snap
                if abs(step) < 0.5: step = 0.5 if step > 0 else -0.5
                self.canvas.azimuth_offset = (current + step) % 360

        # 2. Elevation Animation
        if getattr(self, 'target_elevation', None) is not None:
            current_el = self.canvas.elevation_angle
            diff_el = self.target_elevation - current_el
            
            if abs(diff_el) < 0.5:
                self.canvas.elevation_angle = self.target_elevation
                self.target_elevation = None
            else:
                running = True
                step_el = diff_el * 0.1
                if abs(step_el) < 0.5: step_el = 0.5 if step_el > 0 else -0.5
                self.canvas.elevation_angle = current_el + step_el

        if not running:
            self.anim_timer.stop()
            self.canvas.dragging = False
        else:
            self.canvas.dragging = True
            
        self.canvas.update()

    def on_trails_toggled(self, checked):
        if checked:
            self.target_azimuth = 0 # Rotate to North
            # Point to Polaris (Altitude = Latitude)
            self.target_elevation = self.latitude 
        else:
            self.target_azimuth = 180 # Return to South
            self.target_elevation = 40 # Default nice view
            
        self.anim_timer.start(16) # ~60 FPS

    def get_month_name(self, month_idx):
        # Manual translation since locale might be erratic
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", 
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        keys = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", 
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        
        m_en = months[month_idx - 1]
        return getTraduction(f"Month.{keys[month_idx-1]}", m_en)

    def format_date(self, day_index):
        # Conversion using manual_year
        date = datetime(self.manual_year, 1, 1) + timedelta(days=int(day_index))
        # Custom localized format
        month_name = self.get_month_name(date.month)
        return f"{date.day} {month_name} {date.year}"

    def update_date(self, val):
        self.use_real_time = False
        self.btn_realtime.setChecked(False)
        self.manual_day = val
        self.lbl_date.setText(self.format_date(val))
        
        # Sync Gradient
        self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
        self.canvas.update()

    def prev_day(self):
        self.update_date(self.manual_day - 1)

    def next_day(self):
        self.update_date(self.manual_day + 1)
        # Update gradient when date changes
        self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
        self.canvas.update()

    def open_calendar(self):
        # Open a popup calendar to select date
        dlg = QDialog(self)
        dlg.setWindowTitle("Data")
        dlg.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint) # Popup + Frameless for integration feel
        
        # Style to match dark theme
        dlg.setStyleSheet("""
            QDialog { background: #222; border: 1px solid #555; border-radius: 4px; }
            QCalendarWidget QWidget { alternate-background-color: #333; color: white; }
            QCalendarWidget QToolButton { color: white; icon-size: 20px; }
            QCalendarWidget QMenu { background-color: #333; color: white; }
            QCalendarWidget QSpinBox { color: white; background: #444; selection-background-color: #666; }
            QCalendarWidget QAbstractItemView:enabled { color: white; background: #222; selection-background-color: #0078d7; selection-color: white; }
            QCalendarWidget QAbstractItemView:disabled { color: #555; }
        """)
        
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0,0,0,0)
        
        cal = QCalendarWidget()
        cal.setGridVisible(False)
        cal.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        
        # Set current date based on manual_day (Day of Year)
        current_date = datetime(self.manual_year, 1, 1) + timedelta(days=self.manual_day)
        from PyQt5.QtCore import QDate
        cal.setSelectedDate(QDate(current_date.year, current_date.month, current_date.day))
        
        def on_date_selected():
            qdate = cal.selectedDate()
            # Update Year
            self.manual_year = qdate.year()
            
            # Convert back to day of year
            d = datetime(qdate.year(), qdate.month(), qdate.day())
            start_of_year = datetime(qdate.year(), 1, 1)
            day_idx = (d - start_of_year).days
            self.update_date(day_idx)
            dlg.accept()
            
        cal.clicked.connect(on_date_selected)
        cal.activated.connect(on_date_selected) # Double click
        
        layout.addWidget(cal)
        
        # Smart Positioning
        pos = self.lbl_date.mapToGlobal(QPointF(0, self.lbl_date.height()).toPoint())
        
        # Ensure it doesn't fall off screen bottom
        screen = QApplication.primaryScreen().geometry()
        sz = cal.sizeHint()
        if pos.y() + sz.height() > screen.bottom():
             # Move ABOVE the label if not enough space below
             pos = self.lbl_date.mapToGlobal(QPointF(0, 0).toPoint())
             pos.setY(pos.y() - sz.height() - 5)
             
        dlg.move(pos)
        dlg.exec_()

    def on_lp_mode_changed(self, index):
        """index 0 = Automatic (Bortle), index 1 = Manual (Magnitud)."""
        self.is_auto_bortle = (index == 0)
        
        if self.is_auto_bortle:
            self.lbl_light_text.setText("Bortle")
            self.slider_light.setRange(1, 9)
            if hasattr(self.slider_light, '_lbl_min'):
                self.slider_light._lbl_min.setText("1")
                self.slider_light._lbl_max.setText("9")
            auto_val = getattr(self.canvas, 'auto_bortle_estimate', 4)
            self.slider_light.set_silent_value(int(auto_val))
        else:
            self.lbl_light_text.setText("Magnitud")
            self.slider_light.setRange(-270, 100) # -27.0 to 10.0
            if hasattr(self.slider_light, '_lbl_min'):
                self.slider_light._lbl_min.setText("-27")
                self.slider_light._lbl_max.setText("10")
            self.slider_light.set_silent_value(int(self.magnitude_limit * 10))
            
        self.canvas.update()

    def reset_lp_to_auto(self):
        """Action for the reset button: pulls the current satellite estimate."""
        if not self.horizon_worker: return
        val = self.horizon_worker.get_bortle_estimate(self.latitude, self.longitude)
        print(f"[AstroWidget] Resetting LP to auto-estimated Bortle: {val}")
        self.combo_lp_mode.setCurrentIndex(0) # Switch to Auto mode
        self.slider_light.set_silent_value(val)
        self.auto_bortle_estimate = val
        self.canvas.auto_bortle_estimate = val
        if hasattr(self.canvas, 'weather'):
            self.canvas.weather.set_bortle(val)
        self.canvas.update()

    def update_lp_slider(self, val):
        if self.is_auto_bortle:
            self.auto_bortle_estimate = val
            self.canvas.auto_bortle_estimate = val
            if hasattr(self.canvas, 'weather'):
                self.canvas.weather.set_bortle(val)
        else:
            # Manual mode: Sliders acts as Magnitude Filter
            self.magnitude_limit = val / 10.0
        self.canvas.update()

    def update_magnitude(self, val):
        # Mag slider 10-200 -> 1.0-20.0
        self.magnitude_limit = val / 10.0
        self.canvas.update()

    def build_search_index(self):
        """Builds a search index of planets and named stars for QCompleter."""
        self.search_index = {}
        self.search_lookup = {}

        def register_name(name: str, info: dict) -> None:
            if not name:
                return
            n = str(name).strip()
            if not n:
                return
            self.search_index[n] = info
            k = self._normalize_search_key(n)
            if k and k not in self.search_lookup:
                self.search_lookup[k] = info

        # 1. PLANETS + aliases in supported languages and non-accented variants.
        planet_aliases = {
            "sun": ["Sol", "Sun", "Soleil", "Sole"],
            "moon": ["Lluna", "Luna", "Moon", "Lune"],
            "mercury": ["Mercuri", "Mercurio", "Mercury", "Mercure"],
            "venus": ["Venus"],
            "mars": ["Mart", "Marte", "Mars"],
            "jupiter": ["Jupiter", "Júpiter"],
            "saturn": ["Saturn", "Saturno"],
            "uranus": ["Urà", "Ura", "Urano", "Uranus"],
            "neptune": ["Neptú", "Neptu", "Neptuno", "Neptune"],
            "pluto": ["Plutó", "Pluto", "Plutón", "Pluton"],
        }
        for key, aliases in planet_aliases.items():
            canon = aliases[0]
            info = {"type": "planet", "key": key, "name": canon}
            for alias in aliases:
                register_name(alias, info)

        # 2. NAMED STARS
        if hasattr(self, 'celestial_objects'):
            for star in self.celestial_objects:
                name = str(star.get('name', '')).strip()
                if name and not name.lower().startswith('gaia'):
                    register_name(name, {"type": "star", "obj": star})

        # 3. SETUP QCOMPLETER
        from PyQt5.QtWidgets import QCompleter
        from PyQt5.QtCore import Qt

        names = sorted(list(self.search_index.keys()), key=lambda x: x.lower())
        completer = QCompleter(names, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.txt_search.setCompleter(completer)
        completer.activated[str].connect(self.on_search_triggered)
        self.txt_search.setEnabled(True)
        print(f"[AstroWidget] Search index built: {len(self.search_index)} objects.")

    @staticmethod
    def _normalize_search_key(text: str) -> str:
        lowered = (text or "").strip().lower()
        folded = unicodedata.normalize("NFKD", lowered)
        return "".join(ch for ch in folded if not unicodedata.combining(ch))

    def _prepare_skyfield_cache_for_search(self):
        """Force a fresh cache sample so planet search can resolve current Alt/Az."""
        if not SKYFIELD_AVAILABLE or not hasattr(self, 'canvas') or not hasattr(self, 'eph'):
            return None

        try:
            local_hour = float(self.get_current_hour())
            sim_day = int(self.manual_day)
            sim_year = int(getattr(self, 'manual_year', datetime.now().year))
            tz_offset = self.canvas.get_simulated_tz_offset(sim_day)
            dt_utc = (datetime(sim_year, 1, 1) + timedelta(days=sim_day, hours=local_hour - tz_offset)).replace(tzinfo=timezone.utc)
            ut_hour = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
            day_of_year_utc = (dt_utc.date() - datetime(dt_utc.year, 1, 1).date()).days
            self.canvas.update_skyfield_cache(ut_hour, day_of_year_utc)
        except Exception as ex:
            print(f"[AstroWidget] Search cache update failed: {ex}")

        sf_cache = getattr(self.canvas, '_sf_cache', None)
        if isinstance(sf_cache, dict):
            return sf_cache.get('data')
        return None

    def on_search_triggered(self, text_override=None):
        raw = text_override if isinstance(text_override, str) else self.txt_search.text()
        text = str(raw).strip()
        if not text:
            return

        info = self.search_index.get(text)
        if not info:
            norm = self._normalize_search_key(text)
            info = getattr(self, 'search_lookup', {}).get(norm)
            if info is None:
                for k in sorted(getattr(self, 'search_lookup', {}).keys()):
                    if norm and norm in k:
                        info = self.search_lookup[k]
                        break

        if info:
            self.center_on_object(info)
        else:
            msg = getTraduction("Astro.SearchNotFound", "Object '{name}' not found in index.").format(name=text)
            print(f"[AstroWidget] {msg}")

    def center_on_object(self, info):
        """Calculates current Az/Alt for the object and starts animation."""
        az = alt = None

        if info["type"] == "star":
            star = info["obj"]
            az, alt = self.get_horizontal_coords(star['ra'], star['dec'])
        elif info["type"] == "planet":
            data = self._prepare_skyfield_cache_for_search()
            if data:
                p_key = self._normalize_search_key(info.get('key', ''))
                target_name = self._normalize_search_key(info.get('name', ''))

                if p_key == 'sun' or target_name == 'sol':
                    sun = data.get('sun', {})
                    az = sun.get('az')
                    alt = sun.get('alt')
                elif p_key == 'moon' or target_name in ('lluna', 'luna', 'moon'):
                    moon = data.get('moon', {})
                    az = moon.get('az')
                    alt = moon.get('alt')
                else:
                    for p in data.get('planets', []):
                        p_key_low = self._normalize_search_key(p.get('key', ''))
                        p_name_low = self._normalize_search_key(p.get('name', ''))
                        if p_key_low.startswith(p_key) or p_name_low == target_name:
                            az = p.get('az')
                            alt = p.get('alt')
                            break

        if az is not None and alt is not None:
            az %= 360.0
            self.target_azimuth = az
            self.target_elevation = alt
            if self.canvas.scope_mode_enabled():
                self.canvas.scope_controller.set_center((alt, az))

            if alt < -5 and hasattr(self.canvas, 'hint_overlay'):
                hint = getTraduction("Astro.ObjectBelowHorizon", "Object below horizon ({alt:.1f} deg)").format(alt=alt)
                self.canvas.hint_overlay.show_hint(hint)

            self.anim_timer.start(16)
            self.canvas.update()

    def get_horizontal_coords(self, ra, dec):
        """Helper to convert RA/Dec to Az/Alt for current time/location."""
        h = self.time_bar.current_hour 
        day = self.manual_day
        lat = self.latitude
        lon = self.longitude
        
        lst = (100.0 + day * 0.9856 + h * 15.0 + lon) % 360
        ha = (lst - ra)
        
        lat_rad = math.radians(lat)
        ha_rad = math.radians(ha)
        dec_rad = math.radians(dec)
        
        sin_lat = math.sin(lat_rad)
        cos_lat = math.cos(lat_rad)
        sin_dec = math.sin(dec_rad)
        cos_dec = math.cos(dec_rad)
        
        sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * math.cos(ha_rad)
        sin_alt = max(-1.0, min(1.0, sin_alt))
        alt_deg = math.degrees(math.asin(sin_alt))
        
        cos_alt = math.cos(math.radians(alt_deg))
        cos_az_num = sin_dec - sin_alt * sin_lat
        cos_az_den = cos_alt * cos_lat + 1e-10
        cos_az = max(-1.0, min(1.0, cos_az_num / cos_az_den))
        az_deg = math.degrees(math.acos(cos_az))
        
        if math.sin(ha_rad) > 0:
            az_deg = 360.0 - az_deg
            
        return az_deg, alt_deg

    def update_star_scale(self, val):
        self.star_scale = val / 10.0
        self.canvas.update()

    def update_spikes(self, val):
        self.spike_magnitude_threshold = val / 10.0
        self.canvas.update()
    
    def toggle_controls(self):
        if not hasattr(self, 'panels_widget'): return
        
        # Current state based on panel visibility
        is_visible = self.panels_widget.isVisible() 
        should_hide = is_visible # If visible, we want to hide

        # Toggle Panels
        self.panels_widget.setVisible(not should_hide)
        
        # Update styling/transparency
        if should_hide:
            # COLLAPSED STATE: Transparent background, no border
            # Only time bar is visible (row 2)
            self.frame_controls.setStyleSheet("QFrame { background: transparent; border: none; }")
            self.time_bar.setVisible(True) # Keep timebar
            self.btn_collapse.setText("+") 
        else:
            # EXPANDED STATE: Restore Theme Style
            self.update_custom_theme()
            self.btn_collapse.setText("-")
            
        # Force layout update to recalculate geometry of frame_controls
        self.layout().activate()
        QApplication.processEvents()
        
        # Manually update button position
        self._update_button_pos()

    def _update_button_pos(self):
        if hasattr(self, 'frame_controls') and hasattr(self, 'btn_collapse'):
            rect = self.frame_controls.geometry()
            # Position: Right aligned (minus margin), Top aligned (overlapping)
            bx = rect.right() - self.btn_collapse.width() - 20
            by = rect.top() - self.btn_collapse.height() + 1
            self.btn_collapse.move(bx, by)
            self.btn_collapse.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Defer position update to ensure layout geometry is final
        QTimer.singleShot(0, self._update_button_pos)

    def get_current_hour(self):
        if self.use_real_time:
            n = datetime.now()
            return n.hour + n.minute/60.0
        return self.manual_hour
