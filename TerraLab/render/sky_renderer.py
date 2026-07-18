"""Sky renderer orchestrator with per-layer timing."""

from __future__ import annotations

import math

from PyQt5.QtGui import QColor, QImage, QPainter, QPixmap

from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    normalize_light_pollution_mode,
    resolve_bortle_class,
)
from TerraLab.render.grid_renderer import GridRenderer
from TerraLab.render.horizon_renderer import HorizonRenderer
from TerraLab.render.overlays_renderer import OverlaysRenderer
from TerraLab.render.scope_renderer import ScopeRenderer
from TerraLab.render.sky.milkyway_overlay import MilkyWayOverlay
from TerraLab.render.stars_renderer import StarsRenderer


class SkyRenderer:
    def __init__(self) -> None:
        self.stars_renderer = StarsRenderer()
        self.scope_renderer = ScopeRenderer(self.stars_renderer)
        self.horizon_renderer = HorizonRenderer()
        self.grid_renderer = GridRenderer()
        self.milkyway_overlay = MilkyWayOverlay()
        self.overlays_renderer = OverlaysRenderer()

    def render(self, ctx, state):
        """Renderitza el contingut visual segons l'estat actual.

        Par?metres:
        - ctx (Any): Valor del parametre 'ctx'.
        - state (Any): Valor del parametre 'state'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        diag = getattr(ctx, "diagnostics", None)

        if diag is not None:
            diag.start_timer("renderer_horizon")
        self.horizon_renderer.render(ctx, state)
        if diag is not None:
            diag.stop_timer("renderer_horizon")

        if diag is not None:
            diag.start_timer("renderer_milkyway")
        self.milkyway_overlay.render(ctx, state)
        if diag is not None:
            diag.stop_timer("renderer_milkyway")

        if diag is not None:
            diag.start_timer("renderer_stars")
        if bool(getattr(state, "scope_enabled", False)):
            stars_result = self.scope_renderer.render(ctx, state)
        else:
            stars_result = self.stars_renderer.render(ctx, state)
        if diag is not None:
            diag.stop_timer("renderer_stars")

        if diag is not None:
            diag.start_timer("renderer_grid")
        self.grid_renderer.render(ctx, state)
        if diag is not None:
            diag.stop_timer("renderer_grid")

        if diag is not None:
            diag.start_timer("renderer_overlays")
        self.overlays_renderer.render(ctx, state)
        if diag is not None:
            diag.stop_timer("renderer_overlays")

        return stars_result


def sky_color_phys(
    view_alt,
    view_az,
    sun_alt,
    sun_az,
    *,
    bortle=1,
    twilight_factor=1.0,
    impl=None,
):
    """Sky color helper with optional legacy delegation."""
    if callable(impl):
        return impl(
            view_alt,
            view_az,
            sun_alt,
            sun_az,
            bortle=bortle,
            twilight_factor=twilight_factor,
        )
    return sky_color_phys_impl(
        view_alt,
        view_az,
        sun_alt,
        sun_az,
        bortle=bortle,
        twilight_factor=twilight_factor,
    )


def sky_color_phys_impl(
    view_alt, view_az, sun_alt, sun_az, *, bortle=1, twilight_factor=1.0
):
    keyframes = [
        {
            "alt": 20.0,
            "t": (0, 100, 200),
            "b": (100, 180, 255),
            "g": (255, 255, 220),
        },
        {
            "alt": 6.0,
            "t": (20, 50, 90),
            "b": (255, 190, 60),
            "g": (255, 140, 20),
        },
        {"alt": 0.0, "t": (25, 30, 70), "b": (255, 70, 10), "g": (255, 60, 0)},
        {"alt": -4.0, "t": (10, 15, 50), "b": (120, 60, 20), "g": (60, 20, 5)},
        {"alt": -6.0, "t": (5, 5, 25), "b": (40, 15, 20), "g": (10, 2, 0)},
        {"alt": -12.0, "t": (2, 2, 10), "b": (10, 10, 25), "g": (0, 0, 0)},
        {"alt": -18.0, "t": (0, 0, 5), "b": (5, 5, 15), "g": (0, 0, 0)},
    ]

    def lerp_tup(c1, c2, f):
        return tuple(int(a + (b - a) * f) for a, b in zip(c1, c2))

    k1 = keyframes[0]
    k2 = keyframes[-1]
    if sun_alt >= keyframes[0]["alt"]:
        k1 = keyframes[0]
        k2 = k1
        factor = 0.0
    elif sun_alt <= keyframes[-1]["alt"]:
        k1 = keyframes[-1]
        k2 = k1
        factor = 0.0
    else:
        for i in range(len(keyframes) - 1):
            if keyframes[i]["alt"] >= sun_alt >= keyframes[i + 1]["alt"]:
                k1 = keyframes[i]
                k2 = keyframes[i + 1]
                rng = k1["alt"] - k2["alt"]
                factor = (k1["alt"] - sun_alt) / (rng + 1e-9)
                break

    c_zen = lerp_tup(k1["t"], k2["t"], factor)
    c_hor = lerp_tup(k1["b"], k2["b"], factor)
    c_sun = lerp_tup(k1["g"], k2["g"], factor)

    delta_az_rad = math.radians(abs(view_az - sun_az))
    while delta_az_rad > math.pi:
        delta_az_rad -= 2 * math.pi
    delta_az_rad = abs(delta_az_rad)

    v_alt_rad = math.radians(view_alt)
    s_alt_rad = math.radians(sun_alt)
    cos_gamma = math.sin(v_alt_rad) * math.sin(s_alt_rad) + math.cos(
        v_alt_rad
    ) * math.cos(s_alt_rad) * math.cos(delta_az_rad)
    cos_gamma = max(-1.0, min(1.0, cos_gamma))
    gamma_rad = math.acos(cos_gamma)

    if -12.0 < sun_alt < 15.0:
        az_factor = (math.cos(delta_az_rad) + 1.0) / 2.0
        az_factor = math.pow(az_factor, 1.5)
        sun_extinction = 1.0
        if sun_alt < 0:
            sun_extinction = max(0.0, 1.0 - (abs(sun_alt) / 12.0))
        if sun_alt > 0:
            anti_hor = (100, 130, 170)
        else:
            anti_hor = (5, 5, 12)
        r_target = int(c_hor[0] * az_factor + anti_hor[0] * (1 - az_factor))
        g_target = int(c_hor[1] * az_factor + anti_hor[1] * (1 - az_factor))
        b_target = int(c_hor[2] * az_factor + anti_hor[2] * (1 - az_factor))
        r_hor = int(
            c_hor[0] * (1 - sun_extinction) + r_target * sun_extinction
        )
        g_hor = int(
            c_hor[1] * (1 - sun_extinction) + g_target * sun_extinction
        )
        b_hor = int(
            c_hor[2] * (1 - sun_extinction) + b_target * sun_extinction
        )
        c_hor = (r_hor, g_hor, b_hor)

    if sun_alt <= -12.0:
        c_zen = (0, 0, 5)
        c_hor = (5, 5, 12)
        c_sun = (0, 0, 0)
    elif sun_alt <= -18.0:
        c_zen = (0, 0, 2)
        c_hor = (2, 2, 8)
        c_sun = (0, 0, 0)

    t = 1.0 - (view_alt / 90.0)
    t = max(0.0, min(1.0, t))
    mix_t = t * t * (3 - 2 * t)
    r_base = c_zen[0] * (1 - mix_t) + c_hor[0] * mix_t
    g_base = c_zen[1] * (1 - mix_t) + c_hor[1] * mix_t
    b_base = c_zen[2] * (1 - mix_t) + c_hor[2] * mix_t
    r, g, b = r_base, g_base, b_base

    if cos_gamma > 0.0:
        dir_glow = math.pow(cos_gamma, 4.0) * 0.15
        glow_intensity = 1.0
        if sun_alt < 0:
            glow_intensity = max(0.0, 1.0 - (abs(sun_alt) / 12.0))
        dir_glow *= glow_intensity
        r = min(255, r + c_sun[0] * dir_glow)
        g = min(255, g + c_sun[1] * dir_glow)
        b = min(255, b + c_sun[2] * dir_glow)

    az_away_factor = max(0.0, -math.cos(delta_az_rad))
    if -10 <= sun_alt <= 2 and az_away_factor > 0:
        angle_from_anti_sun = abs(gamma_rad - math.pi)
        if angle_from_anti_sun < 0.5:
            belt_center = 10.0
            belt_dist = abs(view_alt - belt_center)
            belt_str = (
                math.exp(-(belt_dist * belt_dist) / 100.0)
                * 0.2
                * (1.0 - angle_from_anti_sun * 2.0)
            )
            belt_str *= az_away_factor
            r += 60 * belt_str
            g += 30 * belt_str
            b += 50 * belt_str

    if sun_alt < 2 and az_away_factor > 0:
        shadow_h = 6.0 + abs(sun_alt)
        if view_alt < shadow_h:
            shadow_f = (shadow_h - view_alt) / shadow_h
            darken_strength = 0.5 * shadow_f * az_away_factor
            darken = 1.0 - darken_strength
            r *= darken
            g *= darken
            b *= darken

    r = min(255, max(0, int(r)))
    g = min(255, max(0, int(g)))
    b = min(255, max(0, int(b)))

    if bortle > 2 and twilight_factor > 0.01:
        glow_val = (bortle - 2) / 7.0
        glow_alpha = glow_val * twilight_factor * 0.15
        elev_t = max(0.0, min(1.0, 1.0 - (view_alt / 90.0)))
        elev_falloff = math.pow(elev_t, 2.5)
        glow_r, glow_g, glow_b = 140, 130, 110
        strength = glow_alpha * elev_falloff
        r = min(255, int(r + glow_r * strength))
        g = min(255, int(g + glow_g * strength))
        b = min(255, int(b + glow_b * strength))
    return QColor(r, g, b)


def draw_background(
    painter,
    sun_alt,
    sun_az,
    view_az,
    *,
    dimming=1.0,
    cache=None,
    canvas=None,
    impl=None,
):
    """Sky background draw helper with optional legacy delegation."""
    if callable(impl):
        result = impl(painter, sun_alt, sun_az, view_az, dimming=dimming)
    else:
        result = draw_background_impl(
            canvas,
            painter,
            sun_alt,
            sun_az,
            view_az,
            dimming=dimming,
        )
    if isinstance(cache, dict):
        cache["value"] = result
    return result


def draw_background_impl(
    canvas, painter, sun_alt, sun_az, view_az, *, dimming=1.0
):
    if canvas is None:
        return None

    rect = canvas.rect()
    is_interacting = bool(
        canvas._camera_interaction_active(
            include_time_drag=True, include_animation=False
        )
        or canvas._scope_motion_active()
    )
    w_res = 32 if is_interacting else 64
    h_res = 32 if is_interacting else 64
    q_sun_alt = (
        (round(sun_alt * 2) / 2.0) if not is_interacting else round(sun_alt)
    )
    q_sun_az = (
        (round(sun_az / 2) * 2)
        if not is_interacting
        else (round(sun_az / 4) * 4)
    )
    q_view_az = (
        (round(view_az / 2) * 2)
        if not is_interacting
        else (round(view_az / 4) * 4)
    )

    w, h = canvas.width(), canvas.height()
    zoom = round(canvas.zoom_level, 2)
    elev_q = round(canvas.elevation_angle, 1)
    light_pollution_mode = normalize_light_pollution_mode(
        getattr(
            canvas.parent_widget,
            "light_pollution_mode",
            LP_MODE_AUTOMATIC,
        )
    )
    light_pollution_enabled = bool(
        getattr(canvas.parent_widget, "light_pollution_enabled", True)
    )
    bortle = resolve_bortle_class(
        light_pollution_mode,
        automatic_bortle=getattr(
            canvas.parent_widget, "auto_bortle_estimate", 1
        ),
        bortle_value=getattr(canvas.parent_widget, "bortle_value", 1),
        magnitude_limit=getattr(canvas.parent_widget, "magnitude_limit", 8.0),
        light_pollution_enabled=light_pollution_enabled,
    )
    twilight_factor = 1.0
    if sun_alt >= 0:
        twilight_factor = 0.0
    elif sun_alt > -18.0:
        twilight_factor = (0 - sun_alt) / 18.0
    twilight_factor *= dimming
    t_q = round(twilight_factor * 10) / 10.0
    cache_key = (
        q_sun_alt,
        q_sun_az,
        q_view_az,
        w_res,
        h_res,
        w,
        h,
        zoom,
        elev_q,
        bortle,
        t_q,
    )

    if canvas._bg_cache_key != cache_key or canvas._bg_cache_pixmap is None:
        img = QImage(w_res, h_res, QImage.Format_RGB32)
        for y in range(h_res):
            sy = y * (h / (h_res - 1.0))
            for x in range(w_res):
                sx = x * (w / (w_res - 1.0))
                alt, az = canvas.unproject_stereo(sx, sy)
                col = sky_color_phys(
                    alt,
                    az,
                    q_sun_alt,
                    q_sun_az,
                    bortle=bortle,
                    twilight_factor=t_q,
                )
                img.setPixelColor(x, y, col)
        canvas._bg_cache_pixmap = QPixmap.fromImage(img)
        canvas._bg_cache_key = cache_key

    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.drawPixmap(rect, canvas._bg_cache_pixmap)
    if dimming < 0.99:
        alpha = int((1.0 - dimming) * 255)
        alpha = max(0, min(255, alpha))
        painter.fillRect(rect, QColor(0, 0, 0, alpha))
    return canvas._bg_cache_pixmap
