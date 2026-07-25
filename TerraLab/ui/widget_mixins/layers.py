"""Layer availability, mode selection, and layer-state controls."""

from __future__ import annotations

import math
import os
import sys

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QToolTip

from TerraLab.common.utils import getTraduction, get_config_value, set_config_value
from TerraLab.data.catalogs.constants import STAR_CATALOG_NAKED_EYE_MAX_MAG
from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    LP_MODE_MAGNITUDE,
    is_automatic_mode,
    normalize_light_pollution_mode,
    resolve_bortle_class,
)
from TerraLab.terrain.data_sources import LayerType, SurfaceMode
from TerraLab.ui.widget_runtime_helpers import open_calendar as widget_open_calendar


class WidgetLayersMixin:
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
        return widget_open_calendar(self)

    def _catalog_magnitude_upper_bound(self):
        try:
            catalog_max = float(
                getattr(self, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)
            )
        except Exception:
            catalog_max = float(STAR_CATALOG_NAKED_EYE_MAX_MAG)
        if not math.isfinite(catalog_max):
            catalog_max = float(STAR_CATALOG_NAKED_EYE_MAX_MAG)
        return max(-27.0, math.ceil(catalog_max * 10.0 - 1e-9) / 10.0)

    def format_light_pollution_slider_value(self, value):
        mode = normalize_light_pollution_mode(
            getattr(self, "light_pollution_mode", LP_MODE_AUTOMATIC)
        )
        if mode == LP_MODE_MAGNITUDE:
            return f"{float(value) / 10.0:.1f}"
        return str(int(value))

    def _configure_light_pollution_slider(
        self, minimum, maximum, value, enabled, inverted=False
    ):
        slider = self.slider_light
        slider.blockSignals(True)
        slider.setRange(int(minimum), int(maximum))
        slider.setValue(int(max(minimum, min(maximum, value))))
        slider.setEnabled(bool(enabled))
        slider.setInvertedAppearance(bool(inverted))
        slider.blockSignals(False)
        if hasattr(slider, "_lbl_min"):
            left_value = maximum if inverted else minimum
            right_value = minimum if inverted else maximum
            slider._lbl_min.setText(self.format_light_pollution_slider_value(left_value))
            slider._lbl_max.setText(self.format_light_pollution_slider_value(right_value))
            slider._lbl_curr.setText(
                f"[{self.format_light_pollution_slider_value(slider.value())}]"
            )

    def _sync_light_pollution_controls(self):
        if not hasattr(self, "slider_light"):
            return
        mode = normalize_light_pollution_mode(self.light_pollution_mode)
        if mode == LP_MODE_MAGNITUDE:
            upper = self._catalog_magnitude_upper_bound()
            self.magnitude_limit = max(-27.0, min(upper, float(self.magnitude_limit)))
            self.lbl_light_text.setText(
                getTraduction("Astro.MagnitudeLabel", "Magnitude")
            )
            self._configure_light_pollution_slider(
                -270,
                int(round(upper * 10.0)),
                int(round(self.magnitude_limit * 10.0)),
                True,
                False,
            )
            self.slider_light.setToolTip("")
            return
        self.lbl_light_text.setText(getTraduction("Astro.BortleLabel", "Bortle"))
        displayed_bortle = (
            self.auto_bortle_estimate
            if mode == LP_MODE_AUTOMATIC
            else self.bortle_value
        )
        self._configure_light_pollution_slider(
            1,
            9,
            int(displayed_bortle),
            mode != LP_MODE_AUTOMATIC,
            True,
        )
        self.slider_light.setToolTip(
            getTraduction(
                "Astro.AutomaticLockedTooltip",
                "Calculated automatically from the current location",
            )
            if mode == LP_MODE_AUTOMATIC
            else ""
        )

    def refresh_light_pollution_catalog_range(self):
        if not hasattr(self, "slider_light"):
            return
        if normalize_light_pollution_mode(self.light_pollution_mode) != LP_MODE_MAGNITUDE:
            return
        previous = float(self.magnitude_limit)
        self._sync_light_pollution_controls()
        if float(self.magnitude_limit) != previous:
            set_config_value("magnitude_limit", float(self.magnitude_limit))
            self._apply_light_pollution_graphics()

    def _apply_light_pollution_graphics(self):
        effective_bortle = resolve_bortle_class(
            self.light_pollution_mode,
            automatic_bortle=self.auto_bortle_estimate,
            bortle_value=self.bortle_value,
            magnitude_limit=self.magnitude_limit,
            light_pollution_enabled=self.light_pollution_enabled,
        )
        if hasattr(self.canvas, "weather"):
            self.canvas.weather.set_bortle(effective_bortle)
        self.canvas.update()

    def on_lp_mode_changed(self, index):
        """Apply one of the three explicit light-pollution modes."""
        selected_mode = self.combo_lp_mode.itemData(int(index))
        self.light_pollution_mode = normalize_light_pollution_mode(selected_mode)
        set_config_value("light_pollution_mode", self.light_pollution_mode)
        self._sync_light_pollution_controls()
        if self.light_pollution_mode == LP_MODE_MAGNITUDE:
            set_config_value("magnitude_limit", float(self.magnitude_limit))
        self._apply_light_pollution_graphics()
        if is_automatic_mode(self.light_pollution_mode):
            self.recalculate_automatic_light_pollution()

    def on_stars_toggled(self, checked):
        checked = bool(checked)
        if checked != bool(self.chk_enable_sky.isChecked()):
            self.chk_enable_sky.blockSignals(True)
            self.chk_enable_sky.setChecked(checked)
            self.chk_enable_sky.blockSignals(False)
        self._persist_visibility_state("estrelles", checked)
        self._refresh_stars_status_indicator()
        self.canvas.update()

    def on_climate_toggled(self, checked):
        checked = bool(checked)
        if checked != bool(self.chk_clima.isChecked()):
            self.chk_clima.blockSignals(True)
            self.chk_clima.setChecked(checked)
            self.chk_clima.blockSignals(False)
        self._persist_visibility_state("clima", checked)
        if hasattr(self.canvas, "weather"):
            self.canvas.weather.enabled = checked
            self.canvas.weather.set_remote_user_agent(self.asset_manager.get_user_agent())
        if hasattr(self, "weather"):
            self.weather.enabled = checked
            self.weather.set_remote_user_agent(self.asset_manager.get_user_agent())
        self._refresh_climate_status_indicator()
        self.canvas.update()

    def on_light_pollution_toggled(self, checked):
        checked = bool(checked)
        if checked != bool(self.chk_light_pollution.isChecked()):
            self.chk_light_pollution.blockSignals(True)
            self.chk_light_pollution.setChecked(checked)
            self.chk_light_pollution.blockSignals(False)
        self.light_pollution_enabled = bool(checked)
        self._persist_visibility_state("contaminacio_luminica", checked)
        set_config_value("light_pollution_enabled", bool(checked))
        self.terrain_coordinator.reload_config()
        if not (os.name == "nt" and sys.version_info >= (3, 13)):
            QTimer.singleShot(0, self.terrain_coordinator.initialize)
        if checked and is_automatic_mode(self.light_pollution_mode):
            QTimer.singleShot(80, self.recalculate_automatic_light_pollution)
        self._apply_light_pollution_graphics()

    def on_milkyway_toggled(self, checked):
        checked = bool(checked)
        if checked != bool(self.chk_enable_milkyway.isChecked()):
            self.chk_enable_milkyway.blockSignals(True)
            self.chk_enable_milkyway.setChecked(checked)
            self.chk_enable_milkyway.blockSignals(False)
        self._persist_visibility_state("via_lactia", checked)
        self.milkyway_overlay_enabled = bool(checked)
        set_config_value("milkyway_overlay_enabled", bool(self.milkyway_overlay_enabled))
        self._refresh_milkyway_status_indicator()
        self.canvas.update()

    def on_planck_dust_toggled(self, checked):
        checked = bool(checked)
        if checked != bool(self.chk_enable_planck_dust.isChecked()):
            self.chk_enable_planck_dust.blockSignals(True)
            self.chk_enable_planck_dust.setChecked(checked)
            self.chk_enable_planck_dust.blockSignals(False)
        self._persist_visibility_state("pols_planck", checked)
        self.dust_map_enabled = bool(checked)
        set_config_value("dust_map_enabled", bool(self.dust_map_enabled))
        if self.dust_map_enabled and float(getattr(self, "dust_density_strength", 0.0)) <= 0.0 and float(getattr(self, "dust_extinction_strength", 0.0)) <= 0.0:
            self.dust_extinction_strength = 0.65
            set_config_value("dust_extinction_strength", float(self.dust_extinction_strength))
        self._refresh_milkyway_status_indicator()
        self.canvas.update()

    def on_deep_space_toggled(self, checked):
        checked = bool(checked)
        if checked != bool(self.chk_deep_space.isChecked()):
            self.chk_deep_space.blockSignals(True)
            self.chk_deep_space.setChecked(checked)
            self.chk_deep_space.blockSignals(False)
        self._persist_visibility_state("espai_profund", checked)
        self._ngc_search_entries_cache = None
        self.build_search_index()
        self.canvas.update()

    def on_topography_toggled(self, checked):
        checked = bool(checked)
        if checked != bool(self.chk_enable_village.isChecked()):
            self.chk_enable_village.blockSignals(True)
            self.chk_enable_village.setChecked(checked)
            self.chk_enable_village.blockSignals(False)
        self._persist_visibility_state("topografia", checked)
        self.canvas.update()

    def _surface_refresh_view_kwargs(self):
        canvas = getattr(self, "canvas", None)
        radius_km = float(getattr(self, "_pending_terrain_depth_km", 0.0) or 0.0)
        slider = getattr(self, "slider_terrain_depth", None)
        if radius_km <= 0.0 and slider is not None:
            try:
                radius_km = float(slider.value())
            except Exception:
                radius_km = 0.0
        return {
            "visible_radius_m": radius_km * 1000.0 if radius_km > 0.0 else None,
            "view_azimuth_deg": float(getattr(canvas, "azimuth_offset", 180.0)) % 360.0,
            "view_fov_deg": 100.0
            / max(0.001, float(getattr(canvas, "zoom_level", 1.0))),
            "viewport_width_px": (
                int(canvas.width()) if canvas is not None else None
            ),
            "viewport_height_px": (
                int(canvas.height()) if canvas is not None else None
            ),
        }

    def on_surface_layer_toggled(self, checked):
        checked = bool(checked)
        self._persist_visibility_state("superficie", checked)
        canvas = getattr(self, "canvas", None)
        overlay = getattr(canvas, "horizon_overlay", None)
        set_visible = getattr(overlay, "set_surface_visible", None)
        if callable(set_visible):
            set_visible(checked)
        if not checked:
            QToolTip.hideText()
        coordinator = getattr(self, "terrain_coordinator", None)
        refresh = getattr(coordinator, "request_surface_refresh", None)
        if checked and callable(refresh):
            view_context = getattr(self, "_surface_refresh_view_kwargs", None)
            refresh(
                profile=getattr(self, "_full_horizon_profile", None),
                **(view_context() if callable(view_context) else {}),
            )
        elif not checked:
            cancel = getattr(coordinator, "cancel_surface_refresh", None)
            if callable(cancel):
                cancel()
        sync = getattr(self, "_sync_surface_mode_control", None)
        if callable(sync):
            sync()
        if canvas is not None:
            canvas.update()

    def _usable_surface_mode_sources(self):
        """Return installed, enabled surface sources grouped by render mode."""
        grouped = {
            SurfaceMode.ORTHOPHOTO: [],
            SurfaceMode.LAND_COVER: [],
        }
        manager = getattr(self, "layer_manager", None)
        registry = getattr(manager, "data_sources", None)
        if registry is None:
            return grouped
        for source in registry.list_sources():
            if not (bool(source.enabled) and bool(source.available)):
                continue
            if source.layer_type is LayerType.ORTHOPHOTO_RGB:
                grouped[SurfaceMode.ORTHOPHOTO].append(source)
            elif source.layer_type in {
                LayerType.LAND_COVER_CATEGORICAL,
                LayerType.LAND_COVER_RGB,
            }:
                grouped[SurfaceMode.LAND_COVER].append(source)
        for sources in grouped.values():
            sources.sort(
                key=lambda source: (
                    -int(source.priority),
                    source.resolution_m or float("inf"),
                    source.id,
                )
            )
        return grouped

    def _surface_sources_covering_observer(self, sources):
        latitude = float(getattr(self, "latitude", 0.0))
        longitude = float(getattr(self, "longitude", 0.0))
        applicable = []
        for source in sources:
            covers = getattr(source, "covers", None)
            try:
                covered = (
                    bool(covers(latitude, longitude))
                    if callable(covers)
                    else True
                )
            except (TypeError, ValueError):
                covered = False
            if covered:
                applicable.append(source)
        return applicable

    def _surface_coverage_message(self, mode, sources):
        latitude = float(getattr(self, "latitude", 0.0))
        longitude = float(getattr(self, "longitude", 0.0))
        mode_label = (
            "d'ortofoto"
            if mode is SurfaceMode.ORTHOPHOTO
            else "de cobertura del sòl"
        )
        extents = [
            tuple(source.coverage)
            for source in sources
            if getattr(source, "coverage", None)
            and len(source.coverage) == 4
        ]
        message = (
            f"Cap font {mode_label} cobreix la ubicació actual "
            f"({latitude:.5f}, {longitude:.5f})."
        )
        if extents:
            west = min(float(extent[0]) for extent in extents)
            south = min(float(extent[1]) for extent in extents)
            east = max(float(extent[2]) for extent in extents)
            north = max(float(extent[3]) for extent in extents)
            message += (
                "\n\nLa font instal·lada cobreix aproximadament "
                f"{south:.5f}–{north:.5f} N i "
                f"{west:.5f}–{east:.5f} E."
            )
        message += (
            "\n\nMou la ubicació dins d'aquesta extensió o enllaça una "
            "ortofoto que cobreixi l'observador."
            if mode is SurfaceMode.ORTHOPHOTO
            else "\n\nEnllaça una font que cobreixi l'observador."
        )
        return message

    def _sync_surface_mode_control(self):
        """Align the independent source and visual-style surface switches."""
        selector = getattr(self, "surface_mode_selector", None)
        switch = getattr(self, "slider_surface_mode", None)
        style_selector = getattr(
            self, "surface_visual_style_selector", None
        )
        style_switch = getattr(
            self, "slider_surface_visual_style", None
        )
        grouped = self._usable_surface_mode_sources()
        orthophoto_sources = grouped[SurfaceMode.ORTHOPHOTO]
        land_cover_sources = grouped[SurfaceMode.LAND_COVER]
        both_available = bool(orthophoto_sources and land_cover_sources)
        any_available = bool(orthophoto_sources or land_cover_sources)
        checkbox = getattr(self, "chk_surface_layer", None)
        surface_enabled = (
            bool(checkbox.isChecked())
            if checkbox is not None
            and callable(getattr(checkbox, "isChecked", None))
            else True
        )

        if selector is not None:
            selector.setVisible(both_available)
            enable = getattr(selector, "setEnabled", None)
            if callable(enable):
                enable(surface_enabled and both_available)
        source_label = getattr(self, "lbl_surface_source", None)
        if source_label is not None:
            source_label.setVisible(both_available)

        if style_selector is not None:
            style_selector.setVisible(True)
            enable = getattr(style_selector, "setEnabled", None)
            if callable(enable):
                enable(surface_enabled and any_available)
        style_label = getattr(self, "lbl_surface_visual_style", None)
        if style_label is not None:
            style_label.setEnabled(surface_enabled and any_available)
        if style_switch is not None:
            configured_style = str(
                get_config_value("surface_visual_style", "original")
                or "original"
            ).strip().lower()
            style_switch.blockSignals(True)
            style_switch.setValue(
                1 if configured_style == "vibrant" else 0
            )
            style_switch.blockSignals(False)
            style_switch.setToolTip(
                (
                    "Estil Vibrant actiu: paleta o grading cromàtic, "
                    "contorns suaus, bruma i bloom moderat."
                )
                if configured_style == "vibrant"
                else (
                    "Estil Original actiu: conserva fidelment els colors "
                    "i les regions de la font."
                )
            )

        if not both_available or selector is None or switch is None:
            return

        manager = getattr(self, "layer_manager", None)
        registry = getattr(manager, "data_sources", None)
        categorical = registry.surface_mode is SurfaceMode.LAND_COVER
        active_mode = (
            SurfaceMode.LAND_COVER
            if categorical
            else SurfaceMode.ORTHOPHOTO
        )
        active_sources = grouped[active_mode]
        locally_available = bool(
            self._surface_sources_covering_observer(active_sources)
        )
        switch.blockSignals(True)
        switch.setValue(1 if categorical else 0)
        switch.blockSignals(False)
        active_label = "categòric" if categorical else "ortofoto"
        if locally_available:
            tooltip = (
                "Mode de superfície actiu: "
                f"{active_label}. Mou l'interruptor per canviar de representació."
            )
        else:
            tooltip = self._surface_coverage_message(
                active_mode, active_sources
            )
        switch.setToolTip(tooltip)
        ortho_label = getattr(self, "lbl_surface_mode_rgb", None)
        if ortho_label is not None:
            ortho_local = bool(
                self._surface_sources_covering_observer(
                    grouped[SurfaceMode.ORTHOPHOTO]
                )
            )
            ortho_label.setText(
                "Ortofoto" if ortho_local else "Ortofoto (fora d'àrea)"
            )
            ortho_label.setToolTip(
                ""
                if ortho_local
                else self._surface_coverage_message(
                    SurfaceMode.ORTHOPHOTO,
                    grouped[SurfaceMode.ORTHOPHOTO],
                )
            )
            ortho_label.setStyleSheet(
                "font-size: 9px;"
                + ("" if ortho_local else " color: #9b2f2f;")
            )

    def on_surface_visual_style_changed(self, value):
        """Apply Original or Vibrant without rebuilding source samples."""

        vibrant = bool(int(value))
        style = "vibrant" if vibrant else "original"
        set_config_value("surface_visual_style", style)
        # Keep the former key synchronized for external configurations while
        # the renderer treats the visual style as the authoritative switch.
        set_config_value("categorical_edge_smoothing_enabled", vibrant)

        switch = getattr(self, "slider_surface_visual_style", None)
        if switch is not None:
            switch.setToolTip(
                (
                    "Estil Vibrant actiu: paleta o grading cromàtic, "
                    "contorns suaus, bruma i bloom moderat."
                )
                if vibrant
                else (
                    "Estil Original actiu: conserva fidelment els colors "
                    "i les regions de la font."
                )
            )

        canvas = getattr(self, "canvas", None)
        overlay = getattr(canvas, "horizon_overlay", None)
        reload_settings = getattr(overlay, "reload_render_settings", None)
        if callable(reload_settings):
            reload_settings()
        if canvas is not None:
            canvas.update()

