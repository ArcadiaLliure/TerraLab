"""Overlay renderer layer interface.

Current implementation delegates to optional callback for safe migration.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QTransform,
)

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.utils import getTraduction
from TerraLab.astro.engine import AstroEngine

try:
    from skyfield.api import wgs84
except Exception:  # pragma: no cover
    wgs84 = None


class OverlaysRenderer:
    def render(self, ctx, state):
        callback = None
        extras = getattr(state, "extras", {}) or {}
        if isinstance(extras, dict):
            callback = extras.get("render_overlays")
        if callable(callback):
            callback(ctx, state)


def _draw_solar_corona(
    painter,
    x: float,
    y: float,
    radius: float,
    corona_opacity: float,
    sun_alpha_factor: float,
) -> None:
    if corona_opacity <= 0.01 or sun_alpha_factor <= 0.04:
        return
    painter.save()
    try:
        painter.setBrush(Qt.NoBrush)
        grad = QRadialGradient(x, y, radius * 12.0)
        c_scale = sun_alpha_factor * sun_alpha_factor
        grad.setColorAt(
            0.0, QColor(255, 255, 255, int(255 * corona_opacity * c_scale))
        )
        grad.setColorAt(
            0.1,
            QColor(200, 220, 255, int(220 * corona_opacity * c_scale)),
        )
        grad.setColorAt(
            0.25,
            QColor(100, 100, 255, int(100 * corona_opacity * c_scale)),
        )
        grad.setColorAt(1.0, QColor(0, 0, 50, 0))
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(x, y), radius * 12, radius * 12)
    finally:
        painter.restore()


def draw_sun_skyfield(
    canvas, painter, alt, az, radius, color, corona_opacity, pixels_per_deg
):
    if canvas is None:
        return
    pt = canvas.project_universal_stereo(alt, az)
    if not pt:
        return
    x, y = pt
    canvas._register_visible_sky_object(
        "sun",
        "sun",
        getTraduction("Astro.SunName", "Sun"),
        alt,
        az,
        x,
        y,
        radius,
    )
    sun_alpha_factor = max(0.0, min(1.0, QColor(color).alphaF()))
    if sun_alpha_factor <= 0.01:
        return
    _draw_solar_corona(
        painter,
        float(x),
        float(y),
        float(radius),
        float(corona_opacity),
        float(sun_alpha_factor),
    )
    if canvas.scope_mode_enabled():
        painter.save()
        painter.translate(x, y)
        canvas._draw_scope_solar_disc(painter, radius)
        painter.restore()
        return

    path = canvas.get_refracted_body_path(radius, alt, pixels_per_deg)
    painter.save()
    painter.translate(x, y)
    body_grad = QRadialGradient(0, 0, radius)
    alpha_core = int(255 * sun_alpha_factor)
    core_col = QColor(255, 255, 255, alpha_core)
    if alt < 5.0:
        t_set = (5.0 - alt) / 5.0
        core_col = QColor(
            255, 255 - int(50 * t_set), 255 - int(100 * t_set), alpha_core
        )
    body_grad.setColorAt(0.0, core_col)
    body_mid = (
        QColor(255, 255, 240, alpha_core)
        if alt > 10
        else QColor(color.lighter(120))
    )
    body_mid.setAlpha(alpha_core)
    body_grad.setColorAt(0.85, body_mid)
    body_grad.setColorAt(1.0, color)
    painter.setBrush(QBrush(body_grad))
    painter.setPen(Qt.NoPen)
    painter.drawPath(path)
    painter.setOpacity(1.0)

    if corona_opacity < 0.9 and sun_alpha_factor > 0.05:
        glow_radius = radius * 15.0
        glow_grad = QRadialGradient(0, 0, glow_radius)
        g_scale = sun_alpha_factor * sun_alpha_factor
        glow_grad.setColorAt(0.0, QColor(255, 255, 255, int(255 * g_scale)))
        glow_grad.setColorAt(
            0.05,
            QColor(
                color.red(), color.green(), color.blue(), int(200 * g_scale)
            ),
        )
        glow_grad.setColorAt(
            0.1,
            QColor(
                color.red(), color.green(), color.blue(), int(100 * g_scale)
            ),
        )
        glow_grad.setColorAt(
            0.4,
            QColor(
                color.red(), color.green(), color.blue(), int(30 * g_scale)
            ),
        )
        glow_grad.setColorAt(
            1.0, QColor(color.red(), color.green(), color.blue(), 0)
        )
        painter.setBrush(QBrush(glow_grad))
        painter.setCompositionMode(QPainter.CompositionMode_Screen)
        painter.drawEllipse(QPointF(0, 0), glow_radius, glow_radius)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
    painter.restore()


def draw_moon_skyfield(
    canvas,
    painter,
    alt,
    az,
    illum,
    rotation_deg,
    radius,
    alpha,
    *,
    is_eclipsing=False,
    is_day=False,
    sun_params=None,
    pixels_per_deg=None,
    tint_color=None,
):
    if canvas is None:
        return
    if alpha <= 0.01:
        return
    try:
        sf_cache = getattr(canvas, "_sf_cache", None)
        if isinstance(sf_cache, dict):
            data = sf_cache.get("data", None)
            m_cached = (
                data.get("moon", None) if isinstance(data, dict) else None
            )
            if (
                isinstance(m_cached, dict)
                and ("alt" in m_cached)
                and ("az" in m_cached)
            ):
                c_alt = float(m_cached.get("alt"))
                c_az = float(m_cached.get("az")) % 360.0
                dalt = abs(float(alt) - c_alt)
                daz = abs((((float(az) - c_az) + 180.0) % 360.0) - 180.0)
                drift = math.hypot(dalt, daz)
                if drift > 0.5:
                    now_mono = float(time.monotonic())
                    last_log = float(
                        getattr(canvas, "_moon_draw_cache_drift_log_ts", 0.0)
                    )
                    if (now_mono - last_log) >= 2.0:
                        print(
                            "[MoonDebug] draw input vs cache drift "
                            f"deg={drift:.3f} "
                            f"draw=({float(alt):.3f},{float(az) % 360.0:.3f}) "
                            f"cache=({c_alt:.3f},{c_az:.3f})"
                        )
                        canvas._moon_draw_cache_drift_log_ts = now_mono
    except Exception:
        log_suppressed_exception(__name__, "draw_moon_skyfield")
    pt = canvas.project_universal_stereo(alt, az)
    if not pt:
        return
    x, y = pt
    canvas._register_visible_sky_object(
        "moon",
        "moon",
        getTraduction("Astro.MoonName", "Moon"),
        alt,
        az,
        x,
        y,
        radius,
    )
    r = radius
    if pixels_per_deg is None:
        pixels_per_deg = radius / 0.26
    sun_alt_for_sky = -18.0
    sun_az_for_sky = 0.0
    if sun_params:
        sun_alt_for_sky = sun_params[0]
        sun_az_for_sky = sun_params[1]

    if not is_day and not is_eclipsing:
        painter.save()
        painter.setOpacity(1.0)
        painter.translate(x, y)
        sky_col = canvas.sky_color_phys(
            alt, az, sun_alt_for_sky, sun_az_for_sky
        )
        painter.setBrush(sky_col)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(0, 0), r, r)
        painter.restore()

    effective_alpha = float(alpha)
    if is_day:
        effective_alpha = min(float(alpha), 0.85)
    painter.setOpacity(effective_alpha)

    if is_eclipsing and sun_params:
        s_alt, s_az, s_rad = sun_params
        pt_s = canvas.project_universal_stereo(s_alt, s_az)
        if pt_s:
            sx, sy = pt_s
            raw_s_path = canvas.get_refracted_body_path(
                s_rad, s_alt, pixels_per_deg
            )
            t_s = QTransform()
            t_s.translate(sx, sy)
            final_s_path = t_s.map(raw_s_path)
            # Keep strict physical radius parity with the already-computed
            # Sun/Moon pixel radii. A forced +1 px Moon causes abrupt ingress
            # when the discs are small on screen.
            target_r = float(r)
            raw_m_path = canvas.get_refracted_body_path(
                target_r, alt, pixels_per_deg
            )
            t_m = QTransform()
            t_m.translate(x, y)
            final_m_path = t_m.map(raw_m_path)
            bite_path = final_s_path.intersected(final_m_path)
            # Only the overlap against the Sun is fully opaque.
            painter.save()
            painter.setOpacity(1.0)
            painter.setBrush(QColor(15, 15, 20, 255))
            painter.setPen(Qt.NoPen)
            painter.drawPath(bite_path)
            painter.restore()

    if illum > 0.01:
        painter.save()
        painter.translate(x, y)
        painter.rotate(-rotation_deg)
        path = QPainterPath()
        path.arcMoveTo(-r, -r, 2 * r, 2 * r, 90)
        path.arcTo(-r, -r, 2 * r, 2 * r, 90, 180)
        ell_path = QPainterPath()
        w_ell = abs((2.0 * illum - 1.0) * r)
        ell_path.addEllipse(QPointF(0, 0), w_ell, r)
        final_path = (
            path.united(ell_path)
            if illum >= 0.5
            else path.subtracted(ell_path)
        )

        painter.setPen(Qt.NoPen)
        base_col = tint_color if tint_color else QColor(240, 240, 235)
        painter.setBrush(base_col)
        painter.drawPath(final_path)
        painter.setClipPath(final_path)
        painter.rotate(rotation_deg)

        m_r, m_g, m_b = base_col.red(), base_col.green(), base_col.blue()
        maria_col = QColor(
            max(0, m_r - 20), max(0, m_g - 20), max(0, m_b - 10)
        )
        painter.setBrush(maria_col)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(-r * 0.2, -r * 0.4), r * 0.25, r * 0.25)
        painter.drawEllipse(QPointF(r * 0.2, -r * 0.3), r * 0.2, r * 0.2)
        painter.drawEllipse(QPointF(r * 0.3, -r * 0.1), r * 0.22, r * 0.22)
        painter.drawEllipse(QPointF(-r * 0.5, -r * 0.1), r * 0.3, r * 0.5)
        painter.setBrush(QColor(230, 230, 235))
        painter.drawEllipse(QPointF(0, r * 0.6), r * 0.1, r * 0.1)
        painter.restore()

    painter.setOpacity(1.0)


def draw_planet(canvas, painter, alt, az, name, col, sz, mag, *, key=None):
    if canvas is None:
        return
    pt = canvas.project_universal_stereo(alt, az)
    if not pt:
        return
    x, y = pt
    pkey = canvas._normalize_planet_key(key if key is not None else name)
    canvas._register_visible_sky_object(
        "planet", pkey, name, alt, az, x, y, sz, mag=mag
    )
    painter.setBrush(col)
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QPointF(x, y), sz, sz)
    painter.setPen(col)
    painter.drawText(int(x) + 10, int(y), f"{name} {mag:.1f}")


def ngc_symbol_for_type(obj_type: str) -> str:
    kind = str(obj_type or "").upper()
    if kind.startswith("G"):
        return "??"
    if "GC" in kind:
        return "?"
    if "CL+N" in kind or "HII" in kind or "PN" in kind or "N" in kind:
        return "?"
    if "OCL" in kind or "CL" in kind:
        return "?"
    return "??"


def draw_ngc_overlay(
    canvas, painter: QPainter, ut_hour: float, day_of_year_utc: int
) -> None:
    if canvas is None:
        return
    canvas.visible_ngc_objects = []
    parent = getattr(canvas, "parent_widget", None)
    if parent is None or not hasattr(parent, "_load_ngc_search_entries"):
        return
    try:
        sun_alt = float(
            canvas.get_sun_alt_az(
                float(ut_hour), float(parent.latitude), int(day_of_year_utc)
            )[0]
        )
    except Exception:
        sun_alt = 90.0
    if sun_alt > -6.0:
        return
    try:
        catalog = parent._load_ngc_search_entries()
    except Exception:
        return
    if not catalog:
        return

    max_markers = 260 if float(canvas.zoom_level) < 2.0 else 420
    marker_count = 0
    w = float(canvas.width())
    h = float(canvas.height())
    ppd = max(0.02, (min(w, h) * 0.5 * float(canvas.zoom_level)) / 90.0)
    visible_ngc = []

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    font = painter.font()
    if font.pointSize() > 0:
        font.setPointSize(max(8, font.pointSize() - 1))
    else:
        font.setPointSize(9)
    painter.setFont(font)

    for obj in catalog:
        if marker_count >= max_markers:
            break
        try:
            eff_mag = float(getattr(obj, "effective_mag", 99.0))
            maj_deg = max(0.05, float(getattr(obj, "maj_deg", 0.0) or 0.0))
            min_deg = max(
                0.03, float(getattr(obj, "min_deg", maj_deg) or maj_deg)
            )
            if not (
                bool(getattr(obj, "messier_nr", None))
                or eff_mag <= 8.8
                or maj_deg >= 0.30
            ):
                continue

            alt, az = canvas._ra_dec_to_alt_az(
                obj.ra_deg, obj.dec_deg, ut_hour, day_of_year_utc
            )
            if float(alt) < -2.0:
                continue
            pt = canvas.project_universal_stereo(alt, az)
            if pt is None:
                continue
            cx = float(pt[0])
            cy = float(pt[1])
            if (
                cx < -140.0
                or cx > (w + 140.0)
                or cy < -140.0
                or cy > (h + 140.0)
            ):
                continue

            rx = max(3.0, min(84.0, 0.5 * maj_deg * ppd))
            ry = max(2.0, min(64.0, 0.5 * min_deg * ppd))
            pa = float(getattr(obj, "pos_ang_deg", 0.0) or 0.0)
            alpha = 210 if eff_mag <= 6.0 else (170 if eff_mag <= 8.5 else 135)

            painter.save()
            painter.translate(cx, cy)
            painter.rotate(pa)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(120, 190, 255, alpha), 1.2))
            painter.drawEllipse(QPointF(0.0, 0.0), rx, ry)
            painter.restore()

            symbol = ngc_symbol_for_type(getattr(obj, "obj_type", ""))
            label = getattr(obj, "common_name", None) or (
                f"M{obj.messier_nr}"
                if getattr(obj, "messier_nr", None)
                else getattr(obj, "name", "NGC")
            )
            painter.setPen(QPen(QColor(226, 236, 255, min(255, alpha + 24))))
            painter.drawText(
                int(cx + rx + 6.0), int(cy - 5.0), f"{symbol} {label}"
            )
            visible_ngc.append(
                {
                    "obj": obj,
                    "info": {
                        "type": "ngc",
                        "obj": obj,
                        "name": label,
                    },
                    "sx": float(cx),
                    "sy": float(cy),
                    "pick_radius_px": float(max(rx, ry) + 8.0),
                }
            )
            marker_count += 1
        except Exception:
            continue

    painter.restore()
    canvas.visible_ngc_objects = visible_ngc


def draw_light_domes(canvas, painter, profile, eff_sun_alt, eclipse_dimming):
    if canvas is None:
        return
    if eff_sun_alt >= 0:
        return
    canvas._dome_count = 0
    if eff_sun_alt > -18.0:
        twilight_factor = (0 - eff_sun_alt) / 18.0
    else:
        twilight_factor = 1.0
    twilight_factor *= eclipse_dimming
    if twilight_factor <= 0.01:
        return

    show_sun_moon_effects = canvas._parent_checkbox_checked(
        "chk_sun_moon", default=True
    )
    if (
        show_sun_moon_effects
        and canvas._sf_cache
        and canvas._sf_cache.get("data")
    ):
        m_data = canvas._sf_cache["data"].get("moon")
        if m_data and m_data["alt"] > -5.0:
            m_alt, m_az = m_data["alt"], m_data["az"]
            m_illum = m_data.get("illumination", 0.5)
            m_pt = canvas.project_universal_stereo(m_alt, m_az)
            if m_pt:
                mx, my = m_pt
                m_glow_radius = (
                    150.0
                    * canvas.zoom_level
                    * (1.0 + (90.0 - abs(m_alt)) / 90.0)
                )
                m_glow_alpha = int(200 * m_illum * twilight_factor)
                if m_glow_alpha > 5:
                    grad_moon = QRadialGradient(QPointF(mx, my), m_glow_radius)
                    grad_moon.setColorAt(
                        0.0, QColor(255, 255, 255, m_glow_alpha // 2)
                    )
                    grad_moon.setColorAt(
                        0.5, QColor(255, 255, 255, m_glow_alpha // 4)
                    )
                    grad_moon.setColorAt(1.0, QColor(0, 0, 0, 0))
                    painter.save()
                    painter.setBrush(grad_moon)
                    painter.setPen(Qt.NoPen)
                    painter.drawEllipse(
                        QPointF(mx, my), m_glow_radius, m_glow_radius
                    )
                    painter.restore()


def draw_single_city_dome(
    canvas, painter, profile, idx, dist, twilight_factor
):
    if canvas is None:
        return
    canvas._dome_count += 1
    intensity = profile.light_domes[idx]
    if intensity < 0.2:
        return
    az = profile.azimuths[idx]
    dist_factor = math.exp(-dist / 35000.0)
    elev_deg = 0.0
    for b in profile.bands:
        elev_deg = max(elev_deg, math.degrees(b["angles"][idx]))

    pt = canvas.project_universal_stereo(elev_deg, az)
    if not pt:
        return
    x, y = pt
    city_dome_alpha_multiplier = 1.5
    log_intensity = math.log10(1.0 + intensity)
    visual_intensity = log_intensity * dist_factor
    alpha_base = min(100, int(visual_intensity * 60 * twilight_factor))
    alpha_base = int(alpha_base * city_dome_alpha_multiplier)
    if alpha_base <= 2:
        return

    glow_hue = 35 + min(10, intensity / 500.0)
    c_core = QColor.fromHsl(int(glow_hue), 50, 80, int(alpha_base * 0.40))
    c_mid = QColor.fromHsl(int(glow_hue), 40, 60, int(alpha_base * 0.15))
    c_fringe = QColor.fromHsl(int(glow_hue), 30, 40, int(alpha_base * 0.05))
    c_edge = QColor(c_fringe.red(), c_fringe.green(), c_fringe.blue(), 1)
    c_trans = QColor(0, 0, 0, 0)

    max_rad = canvas.width() * 0.4
    rad_x = min(
        max_rad, log_intensity * 30.0 * canvas.zoom_level * dist_factor
    )
    rad_y = rad_x * 0.35

    painter.save()
    painter.setCompositionMode(QPainter.CompositionMode_Screen)
    painter.translate(x, y)
    scale_y = rad_y / max(1.0, rad_x)
    painter.scale(1.0, scale_y)
    grad = QRadialGradient(QPointF(0, 0), rad_x)
    grad.setColorAt(0.00, c_core)
    grad.setColorAt(0.30, c_mid)
    grad.setColorAt(0.60, c_fringe)
    grad.setColorAt(0.90, c_edge)
    grad.setColorAt(1.00, c_trans)
    painter.setBrush(QBrush(grad))
    painter.setPen(Qt.NoPen)
    painter.drawRect(int(-rad_x), int(-rad_x), int(rad_x * 2), int(rad_x * 2))
    painter.restore()


def get_moon_projection(canvas, hour):
    if canvas is None:
        return None

    q_hour = round(float(hour) * 60.0) / 60.0
    if q_hour in canvas._moon_pos_cache:
        moon_ra, moon_dec, sun_ra, sun_dec, m_dist = canvas._moon_pos_cache[
            q_hour
        ]
    else:
        dt_utc = canvas.get_datetime_utc(q_hour)
        jd_utc = canvas.julian_day(dt_utc)
        d = jd_utc - 2451545.0
        moon_ra, moon_dec, m_dist = canvas.get_moon_ra_dec(d)
        sun_ra, sun_dec = canvas.get_sun_ra_dec(d)
        canvas._moon_pos_cache[q_hour] = (
            moon_ra,
            moon_dec,
            sun_ra,
            sun_dec,
            m_dist,
        )

    dt_utc = canvas.get_datetime_utc(q_hour)
    jd_utc = canvas.julian_day(dt_utc)
    lst = canvas.lst_deg(jd_utc, canvas.parent_widget.longitude)

    ha = lst - moon_ra
    ha_rad = math.radians(ha)
    lat_rad = math.radians(canvas.parent_widget.latitude)
    dec_rad = math.radians(moon_dec)
    sin_dec = math.sin(dec_rad)
    cos_dec = math.cos(dec_rad)
    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)

    sin_alt = sin_dec * sin_lat + cos_dec * cos_lat * math.cos(ha_rad)
    alt = math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))

    cos_az = (sin_dec - sin_alt * sin_lat) / (
        math.cos(math.radians(alt)) * cos_lat + 1e-10
    )
    az = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
    if math.sin(ha_rad) > 0:
        az = 360 - az

    pt = canvas.project_universal_stereo(alt, az)
    if not pt:
        return None
    sx, sy = pt

    sr_rad = math.radians(sun_ra)
    mr_rad = math.radians(moon_ra)
    sd_rad = math.radians(sun_dec)
    md_rad = math.radians(moon_dec)
    cos_psi = math.sin(sd_rad) * math.sin(md_rad) + math.cos(
        sd_rad
    ) * math.cos(md_rad) * math.cos(sr_rad - mr_rad)
    k = (1.0 + cos_psi) / 2.0

    diff_ra = (moon_ra - sun_ra) % 360
    is_waxing = diff_ra > 0 and diff_ra < 180

    s_ha = lst - sun_ra
    s_ha_rad = math.radians(s_ha)
    s_sin_alt = math.sin(sd_rad) * sin_lat + math.cos(
        sd_rad
    ) * cos_lat * math.cos(s_ha_rad)
    s_alt = math.degrees(math.asin(max(-1.0, min(1.0, s_sin_alt))))
    s_cos_az = (math.sin(sd_rad) - s_sin_alt * sin_lat) / (
        math.cos(math.radians(s_alt)) * cos_lat + 1e-10
    )
    s_az = math.degrees(math.acos(max(-1.0, min(1.0, s_cos_az))))
    if math.sin(s_ha_rad) > 0:
        s_az = 360 - s_az

    pt_sun = canvas.project_universal_stereo(s_alt, s_az)
    if pt_sun:
        vx = pt_sun[0] - sx
        vy = pt_sun[1] - sy
    else:
        vx = 1
        vy = 0

    angle_deg = math.degrees(math.atan2(vy, vx))
    moon_sd_deg = 0.2725 * (384400.0 / m_dist)
    sin_pi = 6378.14 / m_dist
    sd_topo = moon_sd_deg * (1.0 + sin_pi * math.sin(math.radians(alt)))

    sun_base_radius = 15.0
    illusion_mult = canvas.perceived_disc_scale(alt)
    relative_size = sd_topo / 0.2666
    radius = sun_base_radius * illusion_mult * relative_size

    return (sx, sy, radius, k, angle_deg, is_waxing, s_alt)


def draw_satellites(canvas, painter, ut_hour):
    if canvas is None:
        return

    t_ref_hr = 0.0
    dt_hr = float(ut_hour) - t_ref_hr
    period_hr = 1.55
    mean_motion = (2 * math.pi) / period_hr
    M = (dt_hr * mean_motion) % (2 * math.pi)

    inc = math.radians(51.6)
    r_orbit = 6378 + 420
    x_orb = r_orbit * math.cos(M)
    y_orb = r_orbit * math.sin(M)
    x_eci = x_orb
    y_eci = y_orb * math.cos(inc)
    z_eci = y_orb * math.sin(inc)

    dt_utc = canvas.get_datetime_utc(ut_hour)
    jd_utc = canvas.julian_day(dt_utc)
    lst = canvas.lst_deg(jd_utc, canvas.parent_widget.longitude)
    gmst_deg = lst - canvas.parent_widget.longitude
    gmst_rad = math.radians(gmst_deg)

    lat_rad = math.radians(canvas.parent_widget.latitude)
    lon_rad = math.radians(canvas.parent_widget.longitude)
    theta = gmst_rad + lon_rad
    r_earth = 6378.0
    ox = r_earth * math.cos(lat_rad) * math.cos(theta)
    oy = r_earth * math.cos(lat_rad) * math.sin(theta)
    oz = r_earth * math.sin(lat_rad)

    rx = x_eci - ox
    ry = y_eci - oy
    rz = z_eci - oz
    dist = math.sqrt(rx * rx + ry * ry + rz * rz)

    oz_norm = math.sqrt(ox * ox + oy * oy + oz * oz)
    z_vec = (ox / oz_norm, oy / oz_norm, oz / oz_norm)
    dot = rx * z_vec[0] + ry * z_vec[1] + rz * z_vec[2]
    if dot < 0:
        return

    sin_t = math.sin(theta)
    cos_t = math.cos(theta)
    sin_l = math.sin(lat_rad)
    cos_l = math.cos(lat_rad)
    u = rx * cos_l * cos_t + ry * cos_l * sin_t + rz * sin_l
    e = -rx * sin_t + ry * cos_t
    n = -rx * sin_l * cos_t - ry * sin_l * sin_t + rz * cos_l

    alt = math.degrees(math.atan2(u, math.sqrt(e * e + n * n)))
    az = math.degrees(math.atan2(e, n))
    if az < 0:
        az += 360
    if alt < 10:
        return

    d = jd_utc - 2451545.0
    s_ra, s_dec = canvas.get_sun_ra_dec(d)
    s_ra_rad = math.radians(s_ra)
    s_dec_rad = math.radians(s_dec)
    sx = math.cos(s_dec_rad) * math.cos(s_ra_rad) * 1.5e8
    sy = math.cos(s_dec_rad) * math.sin(s_ra_rad) * 1.5e8
    sz = math.sin(s_dec_rad) * 1.5e8

    to_obs = (-rx / dist, -ry / dist, -rz / dist)
    s_norm = math.sqrt(sx * sx + sy * sy + sz * sz)
    to_sun = (sx / s_norm, sy / s_norm, sz / s_norm)
    cos_phi = (
        to_sun[0] * to_obs[0] + to_sun[1] * to_obs[1] + to_sun[2] * to_obs[2]
    )
    phi = math.acos(max(-1.0, min(1.0, cos_phi)))

    mag = AstroEngine.calculate_satellite_magnitude(dist, phi, std_mag=-1.8)
    if mag > 6.0:
        return

    pt = canvas.project_universal_stereo(alt, az)
    if not pt:
        return

    alpha = max(50, min(255, int(255 - (mag + 2) * 40)))
    c = QColor(255, 255, 255, alpha)
    painter.setPen(QPen(c, 1))
    painter.setBrush(Qt.NoBrush)
    size = 6
    painter.drawRect(QRectF(pt[0] - size / 2, pt[1] - size / 2, size, size))
    painter.setFont(QFont("Mono", 7))
    painter.drawText(int(pt[0]) + size, int(pt[1]), f"SAT M:{mag:.1f}")


def get_eclipse_dimming_factor(canvas, ut_hour, day_of_year):
    if canvas is None or wgs84 is None:
        return 1.0

    try:
        ts = canvas.parent_widget.ts
        eph = canvas.parent_widget.eph
        observer = wgs84.latlon(
            canvas.parent_widget.latitude, canvas.parent_widget.longitude
        )

        now = datetime.now()
        y = getattr(canvas.parent_widget, "manual_year", now.year)
        base_date = datetime(y, 1, 1) + timedelta(days=day_of_year)
        target_dt = base_date + timedelta(hours=ut_hour)

        t = ts.from_datetime(target_dt.replace(tzinfo=timezone.utc))

        earth = eph["earth"]
        sun = eph["sun"]
        moon = eph["moon"]
        obs_loc = earth + observer

        ast_sun = obs_loc.at(t).observe(sun)
        ast_moon = obs_loc.at(t).observe(moon)

        d_sun_km = ast_sun.distance().km
        d_moon_km = ast_moon.distance().km

        sun_radius_km = 696340.0
        moon_radius_km = 1737.4
        r_sun = math.degrees(math.atan(sun_radius_km / d_sun_km))
        r_moon = math.degrees(math.atan(moon_radius_km / d_moon_km))

        sep = ast_sun.separation_from(ast_moon).degrees
        max_dist = r_sun + r_moon
        if sep >= max_dist:
            return 1.0

        min_dist = abs(r_sun - r_moon)
        if r_moon >= r_sun:
            contained_factor = 0.05
        else:
            area_sun = r_sun * r_sun
            area_moon = r_moon * r_moon
            ratio = area_moon / area_sun
            contained_factor = 1.0 - ratio

        if sep <= min_dist:
            return contained_factor

        t_sep = (sep - min_dist) / (max_dist - min_dist)
        return contained_factor + (1.0 - contained_factor) * t_sep
    except Exception:
        return 1.0


def _angular_radius_px_local(
    canvas,
    alt_deg: float,
    az_deg: float,
    angular_radius_deg: float,
    fallback_pixels_per_deg: float,
) -> float:
    """Convert angular radius to pixels using local projection scale."""
    try:
        center = canvas.project_universal_stereo(float(alt_deg), float(az_deg))
        if not center:
            raise RuntimeError("center_not_projectable")

        cx, cy = float(center[0]), float(center[1])
        ang = float(angular_radius_deg)
        samples = []

        # Sample 1: altitude offset.
        p_alt = canvas.project_universal_stereo(
            float(alt_deg) + ang, float(az_deg)
        )
        if p_alt:
            samples.append(
                math.hypot(float(p_alt[0]) - cx, float(p_alt[1]) - cy)
            )

        # Sample 2: azimuth offset corrected by cos(alt) to keep angular distance.
        cos_alt = max(0.15, abs(math.cos(math.radians(float(alt_deg)))))
        p_az = canvas.project_universal_stereo(
            float(alt_deg),
            (float(az_deg) + (ang / cos_alt)) % 360.0,
        )
        if p_az:
            samples.append(
                math.hypot(float(p_az[0]) - cx, float(p_az[1]) - cy)
            )

        if samples:
            value = float(sum(samples) / len(samples))
            if math.isfinite(value) and value > 0.0:
                return value
    except Exception:
        log_suppressed_exception(__name__, "_angular_radius_px_local")

    return max(1.0, float(angular_radius_deg) * float(fallback_pixels_per_deg))


def draw_skyfield_objects(
    canvas, painter, ut_hour, day_of_year, ambient_light=1.0, mag_limit=None
):
    if mag_limit is None:
        mag_limit = 6.0
    show_sun_moon = bool(
        canvas._parent_checkbox_checked("chk_sun_moon", default=True)
    )
    show_planets = bool(
        canvas._parent_checkbox_checked("chk_planets", default=True)
    )
    # USE CACHE if available
    if hasattr(canvas, "_sf_cache") and canvas._sf_cache["data"]:
        try:
            data = canvas._sf_cache["data"]

            # Physical scaling setup (real angular size)
            w, h = canvas.width(), canvas.height()
            R_proj = min(w, h) / 2.0 * canvas.zoom_level
            pixels_per_deg = R_proj / 90.0
            scope_enabled = bool(canvas.scope_mode_enabled())
            scope_disc_cap_px = None
            scope_planet_cap_px = None

            # Minimal anti-bloating guardrail:
            # in wide scope fields (zoomed out), cap disc sizes so they do not
            # explode into oversized balls.
            if scope_enabled:
                pass

            # Sun Data
            s = data.get("sun", {})
            alt_s_deg = float(s.get("alt", -90.0))
            az_s_deg = float(s.get("az", 0.0))
            sun_ang_radius_deg = float(s.get("rad_deg", 0.2666))

            # Moon Data
            m = data.get("moon", {})
            alt_m_real_deg = float(m.get("alt", -90.0))
            az_m_real_deg = float(m.get("az", 0.0))
            moon_ang_radius_deg = float(m.get("rad_deg", 0.2725))
            sep_real = m.get("sep_real", None)
            if sep_real is None:
                try:
                    alt_s_r = math.radians(float(alt_s_deg))
                    az_s_r = math.radians(float(az_s_deg))
                    alt_m_r = math.radians(float(alt_m_real_deg))
                    az_m_r = math.radians(float(az_m_real_deg))
                    cos_sep = math.sin(alt_s_r) * math.sin(alt_m_r) + math.cos(
                        alt_s_r
                    ) * math.cos(alt_m_r) * math.cos(az_s_r - az_m_r)
                    sep_real = math.degrees(
                        math.acos(max(-1.0, min(1.0, cos_sep)))
                    )
                except Exception:
                    sep_real = 180.0
            sep_real = float(sep_real)
            physical_overlap_deg = float(sun_ang_radius_deg) + float(
                moon_ang_radius_deg
            )
            physical_total_margin_deg = abs(
                float(sun_ang_radius_deg) - float(moon_ang_radius_deg)
            )
            is_eclipsing_physical = float(sep_real) < physical_overlap_deg
            is_total_physical = (
                is_eclipsing_physical
                and float(sep_real) <= physical_total_margin_deg
                and float(moon_ang_radius_deg) >= float(sun_ang_radius_deg)
            )

            # Start from physical topocentric positions.
            alt_m_vis = float(alt_m_real_deg)
            az_m_vis = float(az_m_real_deg)

            # Real-size mode: no perceptual inflation.
            scale_s = 1.0
            scale_m = 1.0

            # Real-size mode: physical angular diameter only.
            disc_scale = 1.0

            # Keep Moon center physically anchored.
            # Previous visual "separation compensation" displaced the Moon
            # relative to selection/highlight layers.

            sun_ang_vis_deg = (
                float(sun_ang_radius_deg) * float(disc_scale) * float(scale_s)
            )
            moon_ang_vis_deg = (
                float(moon_ang_radius_deg) * float(disc_scale) * float(scale_m)
            )
            sun_radius_px = _angular_radius_px_local(
                canvas,
                float(alt_s_deg),
                float(az_s_deg),
                sun_ang_vis_deg,
                pixels_per_deg,
            )
            moon_radius_px = _angular_radius_px_local(
                canvas,
                float(alt_m_vis),
                float(az_m_vis),
                moon_ang_vis_deg,
                pixels_per_deg,
            )
            sun_radius_px = max(0.05, float(sun_radius_px))
            moon_radius_px = max(0.05, float(moon_radius_px))
            if scope_disc_cap_px is not None:
                sun_radius_px = min(sun_radius_px, float(scope_disc_cap_px))
                moon_radius_px = min(moon_radius_px, float(scope_disc_cap_px))

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

            if alt_s_deg > 20.0:
                sun_color = c_zenith
            elif alt_s_deg > 5.0:
                t_col = (20.0 - alt_s_deg) / 15.0
                sun_color = interpolate_col_loc(c_zenith, c_golden, t_col)
            elif alt_s_deg > -2.0:
                t_col = (5.0 - alt_s_deg) / 7.0
                sun_color = interpolate_col_loc(c_golden, c_horizon, t_col)
            else:
                sun_color = c_deep

            # Corona visibility must follow physical totality, not enlarged visual discs.
            corona_opacity = 1.0 if is_total_physical else 0.0

            # Weather Dimming
            eff_sun_color = QColor(sun_color)
            eff_corona_opacity = corona_opacity
            dim_factor = canvas._sun_weather_dim_factor()
            if dim_factor > 0.01:
                original_alpha = eff_sun_color.alpha()
                eff_sun_color.setAlpha(
                    int(original_alpha * (1.0 - dim_factor * 0.995))
                )
                eff_corona_opacity *= 1.0 - dim_factor

            visual_ppd = pixels_per_deg * disc_scale

            if show_sun_moon:
                canvas.draw_sun_skyfield(
                    painter,
                    alt_s_deg,
                    az_s_deg,
                    sun_radius_px,
                    eff_sun_color,
                    eff_corona_opacity,
                    visual_ppd,
                )

            # Illumination (Approx)
            elongation = sep_real  # Roughly close enough for visual phase if not precise
            illumination = (1 - math.cos(math.radians(elongation))) / 2
            angle_to_sun = math.atan2(
                alt_s_deg - alt_m_vis, az_s_deg - az_m_vis
            )
            rotation_deg = math.degrees(angle_to_sun)

            # Eclipse check for daytime moon visibility should follow physical overlap.
            is_eclipsing = is_eclipsing_physical

            # Draw Moon
            moon_tint = QColor(240, 240, 235)
            # ... tint logic ...
            if alt_m_vis < 20.0:
                t_moon_set = max(0.0, min(1.0, (20.0 - alt_m_vis) / 20.0))
                r = int(240 * (1 - t_moon_set) + 255 * t_moon_set)
                g = int(240 * (1 - t_moon_set) + 200 * t_moon_set)
                b = int(235 * (1 - t_moon_set) + 100 * t_moon_set)
                moon_tint = QColor(r, g, b)

            if show_sun_moon:
                canvas.draw_moon_skyfield(
                    painter,
                    alt_m_vis,
                    az_m_vis,
                    illumination,
                    rotation_deg,
                    moon_radius_px,
                    1.0,
                    is_eclipsing,
                    (alt_s_deg > -6),
                    sun_params=(alt_s_deg, az_s_deg, sun_radius_px),
                    pixels_per_deg=visual_ppd,
                    tint_color=moon_tint,
                )

            # Planets
            if show_planets:
                for p in data.get("planets", []):
                    # Recompute visibility
                    p_alt = float(p.get("alt", -90.0))
                    p_az = float(p.get("az", 0.0))
                    s_alt_rad = math.radians(alt_s_deg)
                    s_az_rad = math.radians(az_s_deg)
                    p_alt_rad = math.radians(p_alt)
                    p_az_rad = math.radians(p_az)

                    sin_p = math.sin(p_alt_rad)
                    sin_s = math.sin(s_alt_rad)
                    cos_p = math.cos(p_alt_rad)
                    cos_s = math.cos(s_alt_rad)

                    cos_gamma = sin_p * sin_s + cos_p * cos_s * math.cos(
                        p_az_rad - s_az_rad
                    )

                    # Glare/Directional Modifier (Strict -4.0)
                    dir_modifier = -4.0 * cos_gamma

                    # Airmass Extinction for Planets
                    h_p = max(0.1, p_alt)
                    airmass_p = 1.0 / (
                        math.sin(math.radians(h_p))
                        + 0.15 * (h_p + 3.885) ** -1.253
                    )
                    k_p = float(
                        getattr(canvas.parent_widget, "scope_k_fallback", 0.20)
                    )
                    p_ext = k_p * (airmass_p - 1.0)

                    local_limit = mag_limit + dir_modifier - p_ext

                    # Caching handles magnitude now
                    mag = p.get("mag", -2.0)

                    # Visibility Check (Magnitude vs Limit)
                    # "Brighter than limit" means (mag < limit)
                    diff = local_limit - mag
                    fade_in = max(0.0, min(1.0, diff * 2.0))

                    if fade_in > 0.01:
                        p_sz = float(p.get("sz", 6.0))
                        p_rad = max(2.0, (p_sz / 10.0) * pixels_per_deg * 2.0)
                        if scope_planet_cap_px is not None:
                            p_rad = min(p_rad, float(scope_planet_cap_px))

                        # Apply fade to alpha
                        p_col_src = p.get("col", QColor(200, 200, 200))
                        if isinstance(p_col_src, QColor):
                            p_col = QColor(p_col_src)
                        else:
                            try:
                                p_col = QColor(*p_col_src)
                            except Exception:
                                p_col = QColor(200, 200, 200)
                        p_col.setAlphaF(fade_in)
                        p_name = str(p.get("name", p.get("key", "Planet")))
                        canvas.draw_planet(
                            painter,
                            p_alt,
                            p_az,
                            p_name,
                            p_col,
                            p_rad,
                            mag,
                            key=p.get("key"),
                        )

            return
        except Exception as e:
            now_mono = float(time.monotonic())
            last_log = float(
                getattr(canvas, "_overlay_cache_render_error_log_ts", 0.0)
            )
            if (now_mono - last_log) >= 2.0:
                print(f"[Overlay] Skyfield cache render fallback: {e}")
                canvas._overlay_cache_render_error_log_ts = now_mono
            pass

    # Fallback to Original Logic if no cache
    try:
        ts = canvas.parent_widget.ts
        eph = canvas.parent_widget.eph
        observer = wgs84.latlon(
            canvas.parent_widget.latitude, canvas.parent_widget.longitude
        )

        now = datetime.now()
        y = getattr(canvas.parent_widget, "manual_year", now.year)
        base_date = datetime(y, 1, 1) + timedelta(days=day_of_year)
        target_dt = base_date + timedelta(hours=ut_hour)

        t = ts.from_datetime(target_dt.replace(tzinfo=timezone.utc))

        # Physical Scaling setup
        w, h = canvas.width(), canvas.height()
        R_proj = min(w, h) / 2.0 * canvas.zoom_level
        pixels_per_deg = R_proj / 90.0

        # Dynamic Scale Logic:
        # - Zoom < 2.0 (Wide): Use Large Scale (12.0) so they are visible "icons".
        # - Zoom > 10.0 (Tele): Use Real Scale (1.0) so geometry is perfect.
        # Physical Scaling setup
        w, h = canvas.width(), canvas.height()
        R_proj = min(w, h) / 2.0 * canvas.zoom_level
        pixels_per_deg = R_proj / 90.0

        earth = eph["earth"]
        sun = eph["sun"]
        moon = eph["moon"]
        obs_loc = earth + observer

        # "No alterar el tamaÃ±o mÃ¡s que por el efecto del zoom".
        # User request: "Me gustarÃ­a que, al ampliar, tambiÃ©n se ampliara el Sol y la Luna."
        # Previous logic (12.0 / zoom) kept the size static on screen.
        # We now use a constant scale so it grows naturally with the camera zoom (pixels_per_deg).
        # We use 10.0 as a base "Cinematic Scale" so it looks impressive but not overwhelming.
        scope_enabled = bool(canvas.scope_mode_enabled())
        if scope_enabled:
            pass

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
        moon_ang_radius_deg = math.degrees(
            math.atan(MOON_RADIUS_KM / d_moon_km)
        )

        # 3. Calculate Separation
        sep_real = ast_sun.separation_from(ast_moon).degrees
        physical_overlap_deg = float(sun_ang_radius_deg) + float(
            moon_ang_radius_deg
        )
        physical_total_margin_deg = abs(
            float(sun_ang_radius_deg) - float(moon_ang_radius_deg)
        )
        is_eclipsing_physical = float(sep_real) < physical_overlap_deg
        is_total_physical = (
            is_eclipsing_physical
            and float(sep_real) <= physical_total_margin_deg
            and float(moon_ang_radius_deg) >= float(sun_ang_radius_deg)
        )

        # Start from physical topocentric positions.
        alt_m_vis = float(alt_m_real.degrees)
        az_m_vis = float(az_m_real.degrees)

        # If cache data exists, keep Moon center aligned with the same source
        # used by selection/highlight layers to avoid split positions.
        cache_data = None
        try:
            sf_cache = getattr(canvas, "_sf_cache", None)
            if isinstance(sf_cache, dict):
                cache_data = sf_cache.get("data", None)
        except Exception:
            cache_data = None
        if isinstance(cache_data, dict):
            m_cached = cache_data.get("moon", None)
            if (
                isinstance(m_cached, dict)
                and ("alt" in m_cached)
                and ("az" in m_cached)
            ):
                try:
                    cached_alt = float(m_cached.get("alt"))
                    cached_az = float(m_cached.get("az")) % 360.0
                    raw_dalt = abs(float(alt_m_vis) - cached_alt)
                    raw_daz = abs(
                        (((float(az_m_vis) - cached_az) + 180.0) % 360.0)
                        - 180.0
                    )
                    drift_deg = math.hypot(raw_dalt, raw_daz)
                    if drift_deg > 0.5:
                        now_mono = float(time.monotonic())
                        last_log = float(
                            getattr(canvas, "_moon_cache_drift_log_ts", 0.0)
                        )
                        if (now_mono - last_log) >= 2.0:
                            print(
                                "[MoonDebug] fallback/live vs cache drift "
                                f"deg={drift_deg:.3f} "
                                f"live=({float(alt_m_vis):.3f},{float(az_m_vis) % 360.0:.3f}) "
                                f"cache=({cached_alt:.3f},{cached_az:.3f})"
                            )
                            canvas._moon_cache_drift_log_ts = now_mono
                    alt_m_vis = cached_alt
                    az_m_vis = cached_az
                except Exception:
                    log_suppressed_exception(__name__, "draw_skyfield_objects")

        # Real-size mode: no perceptual/cinematic inflation.
        scale_s = 1.0
        scale_m = 1.0
        disc_scale = 1.0

        # Keep Moon center physically anchored.
        # Previous visual "separation compensation" displaced the Moon
        # relative to selection/highlight layers.

        # Clamp min radius
        sun_ang_vis_deg = (
            float(sun_ang_radius_deg) * float(disc_scale) * float(scale_s)
        )
        moon_ang_vis_deg = (
            float(moon_ang_radius_deg) * float(disc_scale) * float(scale_m)
        )
        sun_radius_px = _angular_radius_px_local(
            canvas,
            float(alt_s.degrees),
            float(az_s.degrees),
            sun_ang_vis_deg,
            pixels_per_deg,
        )
        moon_radius_px = _angular_radius_px_local(
            canvas,
            float(alt_m_vis),
            float(az_m_vis),
            moon_ang_vis_deg,
            pixels_per_deg,
        )
        sun_radius_px = max(0.05, float(sun_radius_px))
        moon_radius_px = max(0.05, float(moon_radius_px))

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

        c_zenith = QColor(255, 255, 240)  # White-Yellow
        c_golden = QColor(255, 200, 100)  # Orange
        c_horizon = QColor(255, 60, 20)  # Red
        c_deep = QColor(100, 20, 10)  # Dark Red

        if s_alt_deg > 20.0:
            sun_color = c_zenith
        elif s_alt_deg > 5.0:
            t_col = (20.0 - s_alt_deg) / 15.0  # 0..1
            sun_color = interpolate_col(c_zenith, c_golden, t_col)
        elif s_alt_deg > -2.0:
            t_col = (5.0 - s_alt_deg) / 7.0
            sun_color = interpolate_col(c_golden, c_horizon, t_col)
        else:
            sun_color = c_deep

        # --- CORONA LOGIC ---
        # Totality must be decided from physical overlap, not inflated disc rendering.
        corona_opacity = 1.0 if is_total_physical else 0.0

        # Draw Sun (Background)
        visual_ppd = pixels_per_deg * disc_scale

        # Weather Dimming (Clouds/Rain obscuring Sun)
        eff_sun_color = QColor(sun_color)
        eff_corona_opacity = corona_opacity
        dim_factor = canvas._sun_weather_dim_factor()
        if dim_factor > 0.01:
            original_alpha = eff_sun_color.alpha()
            new_alpha = int(original_alpha * (1.0 - dim_factor * 0.995))
            eff_sun_color.setAlpha(new_alpha)
            eff_corona_opacity *= 1.0 - dim_factor

        if show_sun_moon:
            canvas.draw_sun_skyfield(
                painter,
                alt_s.degrees,
                az_s.degrees,
                sun_radius_px,
                eff_sun_color,
                eff_corona_opacity,
                visual_ppd,
            )

        s_earth = earth.at(t).observe(sun)
        m_earth = earth.at(t).observe(moon)
        # This is elongation (angle between Sun and Moon seen from Earth)
        elongation = s_earth.separation_from(m_earth).degrees

        # Correct Formula for Illumination based on Elongation:
        # Elongation 0 deg (New Moon) -> k = 0
        # Elongation 180 deg (Full Moon) -> k = 1
        illumination = (1 - math.cos(math.radians(elongation))) / 2

        # Rotation for phase
        angle_to_sun = math.atan2(
            alt_s.degrees - alt_m_vis, az_s.degrees - az_m_vis
        )
        rotation_deg = math.degrees(angle_to_sun)

        # Eclipse flag for daytime moon visibility follows physical overlap.
        is_eclipsing = is_eclipsing_physical

        # Daytime Visibility Check
        moon_alpha = 1.0
        is_day = s_alt_deg > -6.0

        if s_alt_deg > 0:  # Bright Day
            # Check Overlap for silhouette preservation (Eclipse)
            if is_eclipsing:
                moon_alpha = 1.0  # Silhouette is solid
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
            r = int(240 * (1 - t_moon_set) + 255 * t_moon_set)
            g = int(240 * (1 - t_moon_set) + 200 * t_moon_set)
            b = int(235 * (1 - t_moon_set) + 100 * t_moon_set)
            moon_tint = QColor(r, g, b)

        if show_sun_moon:
            canvas.draw_moon_skyfield(
                painter,
                alt_m_vis,
                az_m_vis,
                illumination,
                rotation_deg,
                moon_radius_px,
                moon_alpha,
                is_eclipsing,
                is_day,
                sun_params=(alt_s.degrees, az_s.degrees, sun_radius_px),
                pixels_per_deg=visual_ppd,
                tint_color=moon_tint,
            )

        # Logger (Every 30s)
        now_ts = time.time()
        if not hasattr(canvas, "last_log_time"):
            canvas.last_log_time = 0
        if now_ts - canvas.last_log_time > 99999999999:
            canvas.last_log_time = now_ts
            off = canvas.get_simulated_tz_offset(day_of_year)
            print(
                f"SKYFIELD LOG [UTC{off:+.0f}]: "
                f"SUN(Alt={alt_s.degrees:.4f}Â°, Az={az_s.degrees:.4f}Â°) | "
                f"MOON(Alt={alt_m_real.degrees:.4f}Â°, Az={az_m_real.degrees:.4f}Â°)"
            )

        # 3. Planets (All times, visibility depends on Magnitude Limit)
        # The manual check 'if s_alt_deg < -6' prevented Venus/Jupiter from appearing in Civil Twilight.
        # Removed it. The 'mag <= local_limit' check inside handles it correctly.
        if show_planets:
            planets = {
                "mercury": ("Mercury", QColor(169, 169, 169), 4),
                "venus": ("Venus", QColor(255, 220, 150), 7),
                "mars": ("Mars", QColor(255, 100, 80), 5),
                "jupiter barycenter": ("Jupiter", QColor(220, 180, 140), 12),
                "saturn barycenter": ("Saturn", QColor(240, 210, 150), 10),
                "uranus barycenter": ("Uranus", QColor(173, 216, 230), 6),
                "neptune barycenter": ("Neptune", QColor(100, 100, 255), 6),
                "pluto barycenter": ("Pluto", QColor(200, 180, 160), 3),
            }

            for key, (name, col, sz) in planets.items():
                try:
                    p = eph[key]
                    ast = obs_loc.at(t).observe(p)
                    alt_p, az_p, dist = ast.apparent().altaz()

                    if alt_p.degrees > -5:
                        # phase_angle calculation omitted for perf, assuming 0 (Full)
                        phase_angle = 0.0
                        mag = canvas.calculate_planet_magnitude(
                            name, dist.au, phase_angle
                        )
                        p_rad = max(2.0, (sz / 10.0) * pixels_per_deg * 2.0)

                        # --- DIRECTIONAL VISIBILITY FOR PLANETS ---
                        # Same logic as stars
                        p_alt_rad = math.radians(alt_p.degrees)
                        p_az_rad = math.radians(az_p.degrees)
                        s_alt_rad = math.radians(
                            alt_s.degrees
                        )  # Use variables from scope
                        s_az_rad = math.radians(az_s.degrees)

                        sin_p = math.sin(p_alt_rad)
                        sin_s = math.sin(s_alt_rad)
                        cos_p = math.cos(p_alt_rad)
                        cos_s = math.cos(s_alt_rad)

                        cos_gamma = sin_p * sin_s + cos_p * cos_s * math.cos(
                            p_az_rad - s_az_rad
                        )

                        # PLANET SPECIFIC VISIBILITY:
                        # Reverting to strict penalty as per user request.
                        # Even bright planets are lost in the sun's glare if too close.
                        dir_modifier = -1.5 * cos_gamma

                        h_p = max(0.1, alt_p.degrees)
                        airmass_p = 1.0 / (
                            math.sin(math.radians(h_p))
                            + 0.15 * (h_p + 3.885) ** -1.253
                        )
                        k_p = float(
                            getattr(
                                canvas.parent_widget, "scope_k_fallback", 0.20
                            )
                        )
                        p_ext = k_p * (airmass_p - 1.0)

                        local_limit = mag_limit + dir_modifier - p_ext

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
                            canvas.draw_planet(
                                painter,
                                alt_p.degrees,
                                az_p.degrees,
                                name,
                                p_col,
                                p_rad,
                                mag,
                                key=key,
                            )
                except Exception:
                    continue

        # 4. Satellites
        if (
            hasattr(canvas.parent_widget, "show_satellites")
            and canvas.parent_widget.show_satellites
        ):
            for sat_def in canvas.parent_widget.satellites:
                try:
                    sat = sat_def["obj"]
                    topo = (sat - obs_loc).at(t)
                    alt_sat, az_sat, dist_sat = topo.altaz()
                    if alt_sat.degrees > 0:
                        mag = sat_def.get("std_mag", -1.8)
                        canvas.draw_satellite(
                            painter,
                            alt_sat.degrees,
                            az_sat.degrees,
                            sat_def["name"],
                            mag,
                        )
                except Exception:
                    log_suppressed_exception(__name__, "draw_skyfield_objects")

    except Exception:
        # print(f"Skyfield Error: {e}")
        log_suppressed_exception(__name__, "draw_skyfield_objects")
