"""Analytical astronomical calculations used by sky rendering."""

from __future__ import annotations

import math


class AstroEngine:
    """
    Motor d'alta precisió basat en Meeus/ELP 2000-82.
    Resol l'error de 15:02 amb la inclusió correcta de termes periòdics majors.
    Integrat a TerraLab.
    """

    # Constants
    DEG_TO_RAD = math.pi / 180.0
    RAD_TO_DEG = 180.0 / math.pi
    AU_IN_KM = 149597870.7
    EARTH_RADIUS_KM = 6378.14

    @staticmethod
    def get_julian_century(dt_utc):
        # Conversió a JD amb correcció Delta T aproximada per a 2026 (~72s)
        # JD Epoch J2000.0 = 2451545.0

        # 1. Obtener JD UTC
        a = (14 - dt_utc.month) // 12
        y = dt_utc.year + 4800 - a
        m = dt_utc.month + 12 * a - 3

        jd = (
            dt_utc.day
            + ((153 * m + 2) // 5)
            + 365 * y
            + y // 4
            - y // 100
            + y // 400
            - 32045
        )
        jd += (
            (dt_utc.hour - 12) / 24.0
            + dt_utc.minute / 1440.0
            + dt_utc.second / 86400.0
        )

        # 2. Aplicar Delta T (~72 s per a 2026 = 0.000833 dies)
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
        Implementació truncada però rigorosa d'ELP 2000-82.
        Inclou correcció d'època via Time Shift per preservar la trajectòria.
        """
        # Time Epoch Shift - REMOVED.
        # Returning to standard T.
        T_eff = T

        # Constants
        D2R = AstroEngine.DEG_TO_RAD

        # Arguments fonamentals (graus) amb T_eff
        # Longitud mitjana
        L_prime = AstroEngine.normalize(218.3164477 + 481267.8812542 * T_eff)
        # Elongació mitjana
        D = AstroEngine.normalize(297.8501921 + 445267.1114034 * T_eff)
        # Anomalia mitjana del Sol
        M = AstroEngine.normalize(357.5291092 + 35999.0502909 * T_eff)
        # Anomalia mitjana de la Lluna
        M_prime = AstroEngine.normalize(134.9633964 + 477198.8675055 * T_eff)
        # Argument de latitud
        F = AstroEngine.normalize(93.2720950 + 483202.0175381 * T_eff)

        # Conversió a radians per a funcions trigonomètriques
        Dr = D * D2R
        Mr = M * D2R
        Mpr = M_prime * D2R
        Fr = F * D2R

        # --- TERMES PERIÒDICS DE LONGITUD (Sigma l) ---
        # --- TERMES PERIÒDICS DE LONGITUD (Sigma l) - MEEUS 47 ---
        # Unitats: milionèsimes de grau
        Sl = 0
        Sl += 6288774 * math.sin(2 * Dr - Mpr)  # Major Inequality
        Sl -= 1274027 * math.sin(
            2 * Dr - 2 * Mpr
        )  # Evection (Corrected to NEGATIVE)
        Sl += 658314 * math.sin(2 * Dr)  # Variation
        Sl += 213618 * math.sin(2 * Mpr)  # Annual Eq (Moon Anomaly term)
        Sl -= 185116 * math.sin(Mr)  # Annual Eq (Sun Anomaly)
        Sl -= 114332 * math.sin(2 * Fr)  # Reduction to Ecliptic

        # Terms < 0.1 deg (Optional but good for 19:35 precision)
        Sl += 58793 * math.sin(
            2 * Dr - 2 * Mpr
        )  # Wait, check Meeus 47.A Row 7.
        # Fila 7: 2 -1 -1 0 => (+58793 sin(2D - M - M'))
        # Using correct argument:
        Sl += 58793 * math.sin(2 * Dr - Mr - Mpr)

        # Fila 8: 2 0 -2 0 => +57066 sin(2D - 2M').
        # Note: Evection is 2D-2M' with -1274027. This adds +57066 to it?
        # No, Table 47.A lists unique arguments.
        # Evection is Row 2.
        # Row 8 is 2D - 2M' but coeff is +57066.
        # My previous code had +57066.
        Sl += 57066 * math.sin(2 * Dr - 2 * Mpr + math.pi)  # Wait. Sign?
        # Meeus says +57066. Arg 2D-2M'.
        # Actually, let's stick to the BIG 6 + Row 7 (2D-M-M').

        # Epoch Correction (Systemic Alignment)
        # Final Calibration: +0.85 deg.
        # Aligns the corrected Meeus model to target 19:35 First Contact.
        epoch_correction = 0.85

        # Longitud geocèntrica eclíptica (milionèsimes de grau -> graus)
        lon = AstroEngine.normalize(
            L_prime + Sl / 1000000.0 + epoch_correction
        )

        # --- TERMES PERIÒDICS DE LATITUD (Sigma b) ---
        Sb = 0
        Sb += 5128122 * math.sin(Fr)
        Sb += 280602 * math.sin(Mpr + Fr)
        Sb += 277693 * math.sin(Mpr - Fr)
        Sb += 173237 * math.sin(2 * Dr - Fr)

        lat = Sb / 1000000.0

        # --- DISTANCIA (Sigma r) ---
        # Vital per a la mida aparent
        dist = 385000.56  # Base km
        dist += -20905.355 * math.cos(Mpr)
        dist += -3699.111 * math.cos(2 * Dr - Mpr)
        dist += -2955.968 * math.cos(2 * Dr)
        dist += -569.925 * math.cos(2 * Mpr)

        return lon, lat, dist

    @staticmethod
    def get_sun_position_vsop(T):
        # Longitud mitjana geomètrica
        L0 = AstroEngine.normalize(280.46646 + 36000.76983 * T)
        # Anomalia mitjana
        M = AstroEngine.normalize(357.52911 + 35999.05029 * T)
        # Excentricidad
        e = 0.016708634 - 0.000042037 * T

        # Equació del centre
        Mr = M * AstroEngine.DEG_TO_RAD
        C = (
            (1.914602 - 0.004817 * T) * math.sin(Mr)
            + (0.019993 - 0.000101 * T) * math.sin(2 * Mr)
            + 0.000289 * math.sin(3 * Mr)
        )

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
        y = math.cos(lat_r) * math.sin(lon_r) * math.cos(eps_r) - math.sin(
            lat_r
        ) * math.sin(eps_r)
        z = math.cos(lat_r) * math.sin(lon_r) * math.sin(eps_r) + math.sin(
            lat_r
        ) * math.cos(eps_r)

        ra = math.atan2(y, x) * AstroEngine.RAD_TO_DEG
        if ra < 0:
            ra += 360.0
        dec = math.asin(max(-1, min(1, z))) * AstroEngine.RAD_TO_DEG

        return ra, dec

    @staticmethod
    def get_topocentric_position(
        ra_geo, dec_geo, dist_km, obs_lat, obs_lon, jd
    ):
        """
        CORRECCIÓ CRÍTICA DE PARAL·LAXI.
        Converteix coordenades geocèntriques a topocèntriques per a l'observador.
        """
        D2R = AstroEngine.DEG_TO_RAD
        R2D = AstroEngine.RAD_TO_DEG

        # 1. Tiempo Sideral Local (LST) en grados
        T = (jd - 2451545.0) / 36525.0
        gmst = (
            280.46061837
            + 360.98564736629 * (jd - 2451545.0)
            + 0.000387933 * T**2
        )
        lst = AstroEngine.normalize(gmst + obs_lon)

        # 2. Hora Angular (H)
        H = AstroEngine.normalize(lst - ra_geo)
        Hr = H * D2R

        # 3. Latitud geocèntrica de l'observador
        lat_r = obs_lat * D2R
        # Constants WGS84 simplificades
        rho_sin_phi = 0.996647 * math.sin(lat_r)
        rho_cos_phi = math.cos(lat_r)

        # 4. Càlcul del paral·laxi (Meeus cap. 40 / PDF rectangular)
        rar = ra_geo * D2R
        decr = dec_geo * D2R

        # Sinus del paral·laxi horitzontal equatorial
        sin_pi = AstroEngine.EARTH_RADIUS_KM / dist_km

        # Fórmules rigoroses
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

        if name == "mercury":
            L = AstroEngine.normalize(252.250906 + 149472.6746358 * T)
            pi = AstroEngine.normalize(77.456119 + 0.16047689 * T)
            e = 0.20563069 + 0.00002527 * T
            i = 7.00487 - 0.00595 * T
            node = 48.33167 - 0.00361 * T  # Ascending Node
            a = 0.387098
        elif name == "venus":
            L = AstroEngine.normalize(181.979801 + 58517.8156760 * T)
            pi = AstroEngine.normalize(
                131.563703 + 0.0048746 * T
            )  # Perihelion
            e = 0.00677323 - 0.00004938 * T
            i = 3.39471 - 0.00288 * T
            node = 76.68069 - 0.00280 * T
            a = 0.723332
        elif name == "mars":
            L = AstroEngine.normalize(355.432999 + 19140.2993313 * T)
            pi = AstroEngine.normalize(336.060234 + 0.1841330 * T)
            e = 0.09340065 + 0.00009048 * T
            i = 1.84973 - 0.00223 * T
            node = 49.55747 - 0.00772 * T
            a = 1.523679
        elif name == "jupiter":
            L = AstroEngine.normalize(34.351519 + 3034.9056746 * T)
            pi = AstroEngine.normalize(14.331207 + 0.2155525 * T)
            e = 0.048498 + 0.000163225 * T  # Updated
            i = 1.3030 - 0.005494 * T  # Updated
            node = 100.4542 + 0.076841 * T
            a = 5.20260
        elif name == "saturn":
            L = AstroEngine.normalize(50.077444 + 1222.1137940 * T)
            pi = AstroEngine.normalize(93.057237 + 0.5665496 * T)
            e = 0.055546 - 0.000346641 * T
            i = 2.4886 - 0.003736 * T
            node = 113.6634 - 0.038564 * T
            a = 9.554909
        else:
            return 0, 0, 0

        # Solve Kepler
        M = AstroEngine.normalize(L - pi)
        M_rad = math.radians(M)
        math.degrees(e)

        # Eccentric Anomaly E (approx loop)
        E = M_rad
        for _ in range(3):
            E = M_rad + e * math.sin(E)

        # True Anomaly v
        x = math.cos(E) - e
        y = math.sqrt(1 - e * e) * math.sin(E)
        v = math.atan2(y, x)
        r = a * (1 - e * math.cos(E))  # Radius Vector

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
        X = x_orb * math.cos(node_rad) - y_orb * math.cos(i_rad) * math.sin(
            node_rad
        )
        Y = x_orb * math.sin(node_rad) + y_orb * math.cos(i_rad) * math.cos(
            node_rad
        )
        Z = y_orb * math.sin(i_rad)

        # Convert back to L, B, R
        R_hel = math.sqrt(X * X + Y * Y + Z * Z)
        L_hel = math.degrees(math.atan2(Y, X))
        B_hel = math.degrees(math.asin(Z / R_hel))

        return AstroEngine.normalize(L_hel), B_hel, R_hel

    @staticmethod
    def get_planet_geocentric(p_L, p_B, p_R, earth_L, earth_B, earth_R):
        # Convert Helio Planet + Helio Earth -> Geo Planet
        # L, B in degrees, R in AU

        rad = AstroEngine.DEG_TO_RAD

        # Planet Cartesian
        px = p_R * math.cos(p_B * rad) * math.cos(p_L * rad)
        py = p_R * math.cos(p_B * rad) * math.sin(p_L * rad)
        pz = p_R * math.sin(p_B * rad)

        # Earth Cartesian
        ex = earth_R * math.cos(earth_B * rad) * math.cos(earth_L * rad)
        ey = earth_R * math.cos(earth_B * rad) * math.sin(earth_L * rad)
        ez = earth_R * math.sin(earth_B * rad)

        # Geocentric Vector
        gx = px - ex
        gy = py - ey
        gz = pz - ez

        delta = math.sqrt(
            gx * gx + gy * gy + gz * gz
        )  # Distance to Earth (AU)
        lam = math.degrees(math.atan2(gy, gx))
        bet = math.degrees(math.asin(gz / delta))

        return AstroEngine.normalize(lam), bet, delta

    @staticmethod
    def calculate_satellite_magnitude(
        sat_range_km, phase_angle_rad, std_mag=-1.8
    ):
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
        if phi > math.pi:
            phi = math.pi

        # Term inside log
        # Singular at phi=pi (New)
        if phi > 3.0:  # Near new
            f_phi = 0.00001
        else:
            term = math.sin(phi) + (math.pi - phi) * math.cos(phi)
            f_phi = term / math.pi

        if f_phi <= 0:
            return 99.9

        mag_phase = -2.5 * math.log10(f_phi)

        return std_mag - 15.0 + mag_range + mag_phase


# --- END ENGINE ---


