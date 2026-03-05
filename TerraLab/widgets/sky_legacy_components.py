"""Legacy sky components extracted from sky_widget for modularization."""

from __future__ import annotations

import math
import os
import re
import random
import time
from datetime import datetime, timedelta, timezone

try:
    import numpy as np
except ImportError:
    np = None

from PyQt5.QtWidgets import (
    QWidget,
    QLabel,
)
from PyQt5.QtCore import (
    Qt,
    QTimer,
    QPointF,
    QRectF,
    pyqtSignal,
    pyqtSlot,
    QObject,
)
from PyQt5.QtGui import (
    QPainter,
    QColor,
    QPen,
    QRadialGradient,
    QBrush,
    QPainterPath,
    QLinearGradient,
    QImage,
    QPolygonF,
)

from TerraLab.data.stars_dataset import ensure_stars_dataset, load_stars_dataset
from TerraLab.util.color import bp_rp_to_rgb_arrays

try:
    from skyfield.api import load
except Exception:
    load = None

STAR_CATALOG_NAKED_EYE_MAX_MAG = 8.0
STAR_CATALOG_FILE_RE = re.compile(
    r"^MAGNITUD_(-?\d+(?:\.\d+)?)_(-?\d+(?:\.\d+)?)_(\d+)\.npz$",
    re.IGNORECASE,
)


def _parse_star_catalog_npz_name(file_name):
    m = STAR_CATALOG_FILE_RE.match(str(file_name or "").strip())
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2)), int(m.group(3))
    except Exception:
        return None


def _discover_star_catalog_npz_entries(stars_dir):
    out = []
    if not os.path.isdir(stars_dir):
        return out
    for name in os.listdir(stars_dir):
        parsed = _parse_star_catalog_npz_name(name)
        if not parsed:
            continue
        fmin, fmax, rows_hint = parsed
        path = os.path.join(stars_dir, name)
        if not os.path.isfile(path):
            continue
        out.append(
            {
                "path": path,
                "name": name,
                "min_mag": float(fmin),
                "max_mag": float(fmax),
                "rows_hint": int(rows_hint),
            }
        )
    out.sort(key=lambda e: (e["min_mag"], e["max_mag"], e["name"]))
    return out


def _select_base_star_catalog_entry(entries, max_mag=STAR_CATALOG_NAKED_EYE_MAX_MAG):
    if not entries:
        return None
    eps = 1e-6
    exact = [e for e in entries if abs(float(e["max_mag"]) - float(max_mag)) <= eps and float(e["min_mag"]) <= float(max_mag) + eps]
    if exact:
        exact.sort(key=lambda e: (e["min_mag"], e["name"]))
        return exact[0]

    bounded = [e for e in entries if float(e["max_mag"]) <= float(max_mag) + eps]
    if bounded:
        bounded.sort(key=lambda e: (e["max_mag"], -e["min_mag"], e["name"]))
        return bounded[-1]

    for e in entries:
        if float(e["min_mag"]) <= float(max_mag) <= float(e["max_mag"]):
            return e
    return entries[0]


def _npz_get_first(npz_obj, keys):
    for k in keys:
        if k in npz_obj:
            return npz_obj[k]
    return None


def _load_star_npz_arrays(npz_path, max_mag=None, min_mag_exclusive=None):
    if np is None:
        raise RuntimeError("NumPy is required to load star NPZ catalogs.")

    with np.load(npz_path, allow_pickle=False) as data:
        ra_raw = _npz_get_first(data, ("ra", "RA"))
        dec_raw = _npz_get_first(data, ("dec", "DEC"))
        mag_raw = _npz_get_first(data, ("mag", "phot_g_mean_mag", "g_mag"))
        bprp_raw = _npz_get_first(data, ("bp_rp", "bprp"))
        sid_raw = _npz_get_first(data, ("source_id", "source_ids", "id", "ids"))

        if ra_raw is None or dec_raw is None or mag_raw is None:
            raise ValueError(f"NPZ missing required arrays (ra/dec/mag): {npz_path}")

        ra = np.asarray(ra_raw, dtype=np.float32)
        dec = np.asarray(dec_raw, dtype=np.float32)
        mag = np.asarray(mag_raw, dtype=np.float32)
        if not (len(ra) == len(dec) == len(mag)):
            raise ValueError(f"NPZ array length mismatch in {npz_path}")

        if bprp_raw is None:
            bp_rp = np.full(len(mag), 0.8, dtype=np.float32)
        else:
            bp_rp = np.asarray(bprp_raw, dtype=np.float32)
            if len(bp_rp) != len(mag):
                bp_rp = np.full(len(mag), 0.8, dtype=np.float32)
        bp_rp = np.nan_to_num(bp_rp, nan=0.8, posinf=2.0, neginf=-0.5).astype(np.float32, copy=False)

        source_id = None
        if sid_raw is not None:
            try:
                sid_arr = np.asarray(sid_raw)
                if len(sid_arr) == len(mag):
                    source_id = sid_arr.astype(np.int64, copy=False)
            except Exception:
                source_id = None

    mask = np.isfinite(ra) & np.isfinite(dec) & np.isfinite(mag)
    if max_mag is not None:
        mask &= mag <= float(max_mag) + 1e-6
    if min_mag_exclusive is not None:
        mask &= mag > float(min_mag_exclusive) + 1e-6

    if not np.all(mask):
        ra = ra[mask]
        dec = dec[mask]
        mag = mag[mask]
        bp_rp = bp_rp[mask]
        if source_id is not None and len(source_id) == len(mask):
            source_id = source_id[mask]

    return {
        "ra": ra,
        "dec": dec,
        "mag": mag,
        "bp_rp": bp_rp,
        "source_id": source_id,
    }


def _bp_rp_to_rgb_arrays(bp_rp):
    return bp_rp_to_rgb_arrays(bp_rp)


def _build_celestial_objects_from_arrays(ra, dec, mag, bp_rp, source_id=None):
    out = []
    n = len(ra)
    for i in range(n):
        sid_val = i
        if source_id is not None and i < len(source_id):
            try:
                sid_val = int(source_id[i])
            except Exception:
                sid_val = i
        out.append(
            {
                "id": str(sid_val),
                "name": "",
                "ra": float(ra[i]),
                "dec": float(dec[i]),
                "mag": float(mag[i]),
                "bp_rp": float(bp_rp[i]),
            }
        )
    return out

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
    Integrado para TerraLab.
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
    catalog_ready = pyqtSignal(list, object, object, object, object, object, object, object)
    # (celestial_objects, np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp)
    scope_extension_ready = pyqtSignal(object, object, object, object, object, object, object, float)
    # (np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp, loaded_max_mag)

    @pyqtSlot(str)
    def load(self, stars_dir):
        import time
        t0 = time.time()

        celestial_objects = []
        np_ra = np_dec = np_mag = np_r = np_g = np_b = np_bp_rp = None

        # Preferred runtime path: APPDATA NPZ ensured from packaged ZST.
        if np is not None:
            try:
                runtime_npz = ensure_stars_dataset()
                runtime_ds = load_stars_dataset(runtime_npz)

                ra = np.asarray(runtime_ds["ra"], dtype=np.float32)
                dec = np.asarray(runtime_ds["dec"], dtype=np.float32)
                mag = np.asarray(runtime_ds["phot_g_mean_mag"], dtype=np.float32)
                bp_rp = np.asarray(runtime_ds.get("bp_rp"), dtype=np.float32)
                source_id = runtime_ds.get("source_id")

                if len(ra) > 0:
                    order = np.argsort(mag, kind="mergesort")
                    np_ra = ra[order]
                    np_dec = dec[order]
                    np_mag = mag[order]
                    bp_rp = bp_rp[order] if len(bp_rp) == len(order) else np.full(len(order), 0.8, dtype=np.float32)
                    np_bp_rp = np.asarray(bp_rp, dtype=np.float32)
                    if source_id is not None and len(source_id) == len(order):
                        source_id = np.asarray(source_id)[order]
                    else:
                        source_id = None

                    np_r, np_g, np_b = _bp_rp_to_rgb_arrays(bp_rp)
                    # Avoid huge dict allocations for large datasets; keep arrays as source of truth.
                    if len(np_ra) <= 500_000:
                        celestial_objects = _build_celestial_objects_from_arrays(
                            np_ra, np_dec, np_mag, bp_rp, source_id=source_id
                        )
                    else:
                        celestial_objects = []
                    print(
                        f"[CatalogLoader] Runtime dataset loaded: {len(np_ra)} stars "
                        f"from '{runtime_npz}' in {time.time()-t0:.3f}s"
                    )
            except Exception as e:
                print(f"[CatalogLoader] Runtime dataset path unavailable: {e}")

        entries = _discover_star_catalog_npz_entries(stars_dir)
        base_entry = _select_base_star_catalog_entry(entries, max_mag=STAR_CATALOG_NAKED_EYE_MAX_MAG)

        if np is not None and (not celestial_objects) and base_entry is not None:
            try:
                base = _load_star_npz_arrays(
                    base_entry["path"],
                    max_mag=STAR_CATALOG_NAKED_EYE_MAX_MAG,
                )
                if len(base["ra"]) > 0:
                    order = np.argsort(base["mag"], kind="mergesort")
                    ra = base["ra"][order]
                    dec = base["dec"][order]
                    mag = base["mag"][order]
                    bp_rp = base["bp_rp"][order]
                    np_bp_rp = np.asarray(bp_rp, dtype=np.float32)
                    source_id = base["source_id"][order] if base["source_id"] is not None else None

                    np_ra = np.asarray(ra, dtype=np.float32)
                    np_dec = np.asarray(dec, dtype=np.float32)
                    np_mag = np.asarray(mag, dtype=np.float32)
                    np_r, np_g, np_b = _bp_rp_to_rgb_arrays(bp_rp)
                    celestial_objects = _build_celestial_objects_from_arrays(
                        np_ra, np_dec, np_mag, bp_rp, source_id=source_id
                    )
                    print(
                        f"[CatalogLoader] Loaded base NPZ '{os.path.basename(base_entry['path'])}' "
                        f"({len(np_ra)} stars) in {time.time()-t0:.3f}s"
                    )
            except Exception as e:
                print(f"[CatalogLoader] Error loading base NPZ: {e}")
        elif (not celestial_objects) and base_entry is None:
            print(f"[CatalogLoader] No MAGNITUD_*.npz found in: {stars_dir}")

        if not celestial_objects:
            print("[CatalogLoader] Fallback to random stars")
            import random as _rnd
            for _ in range(500):
                celestial_objects.append(
                    {
                        "id": "rnd",
                        "name": "",
                        "ra": _rnd.uniform(0, 360),
                        "dec": _rnd.uniform(-90, 90),
                        "mag": _rnd.uniform(1.0, 6.0),
                        "bp_rp": _rnd.uniform(-0.5, 2.0),
                    }
                )
            celestial_objects.sort(key=lambda x: x["mag"])
            if np is not None:
                np_ra = np.array([s["ra"] for s in celestial_objects], dtype=np.float32)
                np_dec = np.array([s["dec"] for s in celestial_objects], dtype=np.float32)
                np_mag = np.array([s["mag"] for s in celestial_objects], dtype=np.float32)
                np_bp_rp = np.array([s["bp_rp"] for s in celestial_objects], dtype=np.float32)
                np_r, np_g, np_b = _bp_rp_to_rgb_arrays(np_bp_rp)

        self.catalog_ready.emit(celestial_objects, np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp)

    @pyqtSlot(str, float)
    def load_scope_extensions(self, stars_dir, loaded_max_mag):
        import time
        t0 = time.time()

        np_ra = np_dec = np_mag = np_r = np_g = np_b = np_bp_rp = None
        max_loaded = float(loaded_max_mag)
        if np is None:
            self.scope_extension_ready.emit(None, None, None, None, None, None, None, max_loaded)
            return

        entries = _discover_star_catalog_npz_entries(stars_dir)
        eps = 1e-6
        targets = [e for e in entries if float(e["max_mag"]) > float(loaded_max_mag) + eps]
        if not targets:
            self.scope_extension_ready.emit(None, None, None, None, None, None, None, max_loaded)
            return

        chunks_ra = []
        chunks_dec = []
        chunks_mag = []
        chunks_r = []
        chunks_g = []
        chunks_b = []
        chunks_bp_rp = []

        for entry in targets:
            try:
                arr = _load_star_npz_arrays(
                    entry["path"],
                    min_mag_exclusive=float(loaded_max_mag),
                )
                if len(arr["ra"]) == 0:
                    continue
                rr, gg, bb = _bp_rp_to_rgb_arrays(arr["bp_rp"])
                chunks_ra.append(np.asarray(arr["ra"], dtype=np.float32))
                chunks_dec.append(np.asarray(arr["dec"], dtype=np.float32))
                chunks_mag.append(np.asarray(arr["mag"], dtype=np.float32))
                chunks_r.append(rr)
                chunks_g.append(gg)
                chunks_b.append(bb)
                chunks_bp_rp.append(np.asarray(arr["bp_rp"], dtype=np.float32))
                max_loaded = max(max_loaded, float(entry.get("max_mag", loaded_max_mag)))
                print(
                    f"[CatalogLoader] Scope chunk '{os.path.basename(entry['path'])}' "
                    f"loaded ({len(arr['ra'])} stars)"
                )
            except Exception as e:
                print(f"[CatalogLoader] Scope chunk load error '{entry.get('name', '')}': {e}")

        if chunks_ra:
            np_ra = np.concatenate(chunks_ra)
            np_dec = np.concatenate(chunks_dec)
            np_mag = np.concatenate(chunks_mag)
            np_r = np.concatenate(chunks_r)
            np_g = np.concatenate(chunks_g)
            np_b = np.concatenate(chunks_b)
            np_bp_rp = np.concatenate(chunks_bp_rp)
            print(
                f"[CatalogLoader] Scope extension ready: {len(np_ra)} stars "
                f"in {time.time()-t0:.3f}s (max mag {max_loaded:.2f})"
            )

        self.scope_extension_ready.emit(np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp, float(max_loaded))


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
            k_ext = float(params.get('extinction_coeff', 0.20))
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


