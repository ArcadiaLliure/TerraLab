"""Legacy QPainter adapter that composes the remaining sky view layers."""

from __future__ import annotations

from PyQt5.QtGui import QColor, QImage, QPainter, QPixmap

from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    normalize_light_pollution_mode,
    resolve_bortle_class,
)
from TerraLab.render.grid_renderer import GridRenderer
from TerraLab.render.horizon_renderer import LegacyHorizonLayerAdapter
from TerraLab.render.overlays_renderer import OverlaysRenderer
from TerraLab.render.scope_renderer import ScopeRenderer
from TerraLab.render.sky.milkyway_overlay import MilkyWayOverlay
from TerraLab.render.stars_renderer import StarsRenderer
from TerraLab.scene.plans.sky import calculate_sky_rgba


class LegacySkyLayerAdapter:
    """Compose unmigrated QPainter sky layers; it does not own sky science."""

    def __init__(self) -> None:
        self.stars_renderer = StarsRenderer()
        self.scope_renderer = ScopeRenderer(self.stars_renderer)
        self.horizon_layer_adapter = LegacyHorizonLayerAdapter()
        self.grid_renderer = GridRenderer()
        self.milkyway_overlay = MilkyWayOverlay()
        self.overlays_renderer = OverlaysRenderer()

    def paint_layers(self, context, render_state):
        """Paint the legacy layers in their established timing order."""

        diagnostics = getattr(context, "diagnostics", None)

        if diagnostics is not None:
            diagnostics.start_timer("renderer_horizon")
        self.horizon_layer_adapter.paint_layer(context, render_state)
        if diagnostics is not None:
            diagnostics.stop_timer("renderer_horizon")

        if diagnostics is not None:
            diagnostics.start_timer("renderer_milkyway")
        self.milkyway_overlay.render(context, render_state)
        if diagnostics is not None:
            diagnostics.stop_timer("renderer_milkyway")

        if diagnostics is not None:
            diagnostics.start_timer("renderer_stars")
        if bool(getattr(render_state, "scope_enabled", False)):
            stars_result = self.scope_renderer.render(context, render_state)
        else:
            stars_result = self.stars_renderer.render(context, render_state)
        if diagnostics is not None:
            diagnostics.stop_timer("renderer_stars")

        if diagnostics is not None:
            diagnostics.start_timer("renderer_grid")
        self.grid_renderer.render(context, render_state)
        if diagnostics is not None:
            diagnostics.stop_timer("renderer_grid")

        if diagnostics is not None:
            diagnostics.start_timer("renderer_overlays")
        self.overlays_renderer.render(context, render_state)
        if diagnostics is not None:
            diagnostics.stop_timer("renderer_overlays")
        return stars_result


def _legacy_sky_qcolor(
    view_altitude_deg: float,
    view_azimuth_deg: float,
    sun_altitude_deg: float,
    sun_azimuth_deg: float,
    *,
    bortle_class: int,
    twilight_factor: float,
) -> QColor:
    """Convert the canonical Model colour to Qt at the legacy View boundary."""

    red, green, blue, alpha = calculate_sky_rgba(
        view_altitude_deg,
        view_azimuth_deg,
        sun_altitude_deg,
        sun_azimuth_deg,
        bortle_class=bortle_class,
        twilight_factor=twilight_factor,
    )
    return QColor(red, green, blue, alpha)


def paint_legacy_canvas_sky_background(
    canvas,
    painter: QPainter,
    sun_altitude_deg: float,
    sun_azimuth_deg: float,
    view_azimuth_deg: float,
    *,
    dimming: float = 1.0,
    cache: dict | None = None,
):
    """Retain the inactive canvas path as an explicit legacy View adapter."""

    if canvas is None:
        return None

    rect = canvas.rect()
    is_interacting = bool(
        canvas._camera_interaction_active(
            include_time_drag=True, include_animation=False
        )
        or canvas._scope_motion_active()
    )
    sample_width = 32 if is_interacting else 64
    sample_height = 32 if is_interacting else 64
    quantized_sun_altitude = (
        round(sun_altitude_deg)
        if is_interacting
        else round(sun_altitude_deg * 2.0) / 2.0
    )
    quantized_sun_azimuth = (
        round(sun_azimuth_deg / 4.0) * 4.0
        if is_interacting
        else round(sun_azimuth_deg / 2.0) * 2.0
    )
    quantized_view_azimuth = (
        round(view_azimuth_deg / 4.0) * 4.0
        if is_interacting
        else round(view_azimuth_deg / 2.0) * 2.0
    )

    width, height = canvas.width(), canvas.height()
    light_pollution_mode = normalize_light_pollution_mode(
        getattr(
            canvas.parent_widget, "light_pollution_mode", LP_MODE_AUTOMATIC
        )
    )
    bortle_class = resolve_bortle_class(
        light_pollution_mode,
        automatic_bortle=getattr(
            canvas.parent_widget, "auto_bortle_estimate", 1
        ),
        bortle_value=getattr(canvas.parent_widget, "bortle_value", 1),
        magnitude_limit=getattr(canvas.parent_widget, "magnitude_limit", 8.0),
        light_pollution_enabled=bool(
            getattr(canvas.parent_widget, "light_pollution_enabled", True)
        ),
    )
    twilight_factor = 0.0 if sun_altitude_deg >= 0.0 else 1.0
    if -18.0 < sun_altitude_deg < 0.0:
        twilight_factor = -sun_altitude_deg / 18.0
    twilight_factor *= dimming
    quantized_twilight = round(twilight_factor * 10.0) / 10.0
    cache_key = (
        quantized_sun_altitude,
        quantized_sun_azimuth,
        quantized_view_azimuth,
        sample_width,
        sample_height,
        width,
        height,
        round(canvas.zoom_level, 2),
        round(canvas.elevation_angle, 1),
        bortle_class,
        quantized_twilight,
    )

    if canvas._bg_cache_key != cache_key or canvas._bg_cache_pixmap is None:
        image = QImage(sample_width, sample_height, QImage.Format_RGB32)
        for sample_y in range(sample_height):
            screen_y = sample_y * (height / (sample_height - 1.0))
            for sample_x in range(sample_width):
                screen_x = sample_x * (width / (sample_width - 1.0))
                altitude_deg, azimuth_deg = canvas.unproject_stereo(
                    screen_x, screen_y
                )
                image.setPixelColor(
                    sample_x,
                    sample_y,
                    _legacy_sky_qcolor(
                        altitude_deg,
                        azimuth_deg,
                        quantized_sun_altitude,
                        quantized_sun_azimuth,
                        bortle_class=bortle_class,
                        twilight_factor=quantized_twilight,
                    ),
                )
        canvas._bg_cache_pixmap = QPixmap.fromImage(image)
        canvas._bg_cache_key = cache_key

    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.drawPixmap(rect, canvas._bg_cache_pixmap)
    if dimming < 0.99:
        alpha = max(0, min(255, int((1.0 - dimming) * 255)))
        painter.fillRect(rect, QColor(0, 0, 0, alpha))
    result = canvas._bg_cache_pixmap
    if cache is not None:
        cache["value"] = result
    return result
