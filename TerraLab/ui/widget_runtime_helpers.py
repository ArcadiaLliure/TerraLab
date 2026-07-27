"""Runtime helper functions for AstronomicalWidget."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

from PyQt5.QtCore import QDate, QPointF, Qt
from PyQt5.QtWidgets import QApplication, QCalendarWidget, QDialog, QVBoxLayout

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.app_paths import data_dir as runtime_data_dir_for
from TerraLab.common.utils import (
    get_config_value,
    getTraduction,
    set_config_value,
)
from TerraLab.light_pollution.modes import (
    is_automatic_mode,
    resolve_bortle_class,
)
from TerraLab.data.catalogs.star_catalog import (
    STAR_CATALOG_NAKED_EYE_MAX_MAG,
    _bp_rp_to_rgb_arrays,
    _build_celestial_objects_from_arrays,
    _discover_star_catalog_npz_entries,
    _load_star_npz_arrays,
    _select_base_star_catalog_entry,
)
from TerraLab.ui.design_system import CALENDAR_STYLESHEET
from TerraLab.widgets.telescope_runtime import update_telescope_hud
from TerraLab.widgets.visual_magnitude_engine import VisualMagnitudeInputs

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


def _resolve_effective_bortle_class(widget) -> float:
    """Resolve the graphical Bortle-equivalent value for the active mode."""
    return resolve_bortle_class(
        getattr(widget, "light_pollution_mode", None),
        automatic_bortle=getattr(widget, "auto_bortle_estimate", 1.0),
        bortle_value=getattr(widget, "bortle_value", 1.0),
        magnitude_limit=getattr(
            widget, "magnitude_limit", STAR_CATALOG_NAKED_EYE_MAX_MAG
        ),
        light_pollution_enabled=getattr(widget, "light_pollution_enabled", True),
    )


def recompute_visual_magnitude_model(
    widget, target_alt_deg=None, sun_alt_deg=-18.0, now_utc=None
):
    if target_alt_deg is None:
        target_alt_deg = getattr(widget.canvas, "elevation_angle", 40.0)

    bortle_class = _resolve_effective_bortle_class(widget)

    focal_mm = float(
        getattr(widget.canvas.scope_controller, "focal_mm", 250.0)
    )
    if hasattr(widget, "scope_focal_spin"):
        try:
            focal_mm = float(widget.scope_focal_spin.value())
        except Exception:
            log_suppressed_exception(__name__, "recompute_visual_magnitude_model")
    aperture_mm_effective = widget._effective_scope_aperture_mm(focal_mm)
    instrument_profile = str(
        getattr(widget, "scope_instrument_profile", "telescope")
    )
    eyepiece_mm = float(
        widget.scope_eyepiece_mm
        if instrument_profile == "telescope"
        else focal_mm
    )

    scope_enabled = bool(
        getattr(widget.canvas, "scope_mode_enabled", lambda: False)()
    )
    runtime_state = {
        "scope_enabled": scope_enabled,
        "lat": float(widget.latitude),
        "lon": float(widget.longitude),
        "h_deg": float(target_alt_deg),
        "focal_mm": float(focal_mm),
        "aperture_mm": float(aperture_mm_effective),
        "ocular_mm": eyepiece_mm,
        "instrument_profile": instrument_profile,
        "k_fallback": float(widget.scope_k_fallback),
        "weather_enabled": bool(
            getattr(widget.canvas.weather, "enabled", False)
        ),
        "copernicus_api_key": str(
            get_config_value("copernicus_api_key", "") or ""
        ),
        "copernicus_api_url": str(
            get_config_value(
                "copernicus_api_url", "https://cds.climate.copernicus.eu/api"
            )
            or ""
        ),
        "now_utc": now_utc,
        "_wx_cache": widget.scope_atmo_metrics.get("_wx_cache", {}),
    }
    if scope_enabled:
        update_telescope_hud(runtime_state)
        widget.scope_atmo_metrics = dict(runtime_state)
        hud_metrics = runtime_state.get("hud_metrics", {})
        atmo_loss = hud_metrics.get("loss_mag")
    else:
        hud_metrics = widget.scope_atmo_metrics.get("hud_metrics", {})
        atmo_loss = None

    if atmo_loss is None:
        atmo_loss = 0.0

    sensor_profile = str(
        getattr(widget.canvas.scope_controller, "sensor_key", "tiny")
    )
    if hasattr(widget, "scope_sensor_combo"):
        try:
            current_sensor = widget.scope_sensor_combo.itemData(
                widget.scope_sensor_combo.currentIndex()
            )
            if current_sensor:
                sensor_profile = str(current_sensor)
        except Exception:
            log_suppressed_exception(__name__, "recompute_visual_magnitude_model")

    inputs = VisualMagnitudeInputs(
        aperture_mm=aperture_mm_effective,
        telescope_focal_mm=focal_mm,
        eyepiece_focal_mm=eyepiece_mm,
        eye_pupil_mm=widget._estimate_eye_pupil_mm(float(sun_alt_deg)),
        atmospheric_loss_mag=float(atmo_loss),
        light_pollution_mode=str(widget.light_pollution_mode),
        bortle_class=bortle_class,
        magnitude_limit=float(widget.magnitude_limit),
        exposure_seconds=float(widget.scope_exposure_s),
        iso=float(widget.scope_iso),
        instrument_profile=instrument_profile,
        sensor_profile=sensor_profile,
    )
    result = widget.visual_magnitude_engine.compute(inputs)
    widget.visual_magnitude_result = result
    widget.auto_star_scale_multiplier = (
        float(result.star_scale_factor) if scope_enabled else 1.0
    )
    return result


def request_relocation(widget):
    """User changed location: trigger full bake."""
    try:
        new_lat = float(widget.txt_lat.text())
        new_lon = float(widget.txt_lon.text())

        old_hemi_n = widget.latitude >= 0
        new_hemi_n = new_lat >= 0
        if old_hemi_n != new_hemi_n:
            widget.canvas.azimuth_offset = (
                widget.canvas.azimuth_offset + 180
            ) % 360

        widget.latitude = new_lat
        widget.longitude = new_lon
        sync_coverage = getattr(
            widget, "_sync_surface_mode_control", None
        )
        if callable(sync_coverage):
            sync_coverage()

        # Observer changed: invalidate skyfield/eclipses immediately so next frame
        # cannot reuse ephemerides from previous coordinates.
        if hasattr(widget, "canvas"):
            widget.canvas._sf_cache = {
                "time": -1.0,
                "ut_hour": -999.0,
                "day": None,
                "year": None,
                "lat": None,
                "lon": None,
                "data": None,
            }
            widget.canvas._eclipse_cache = {"time": -1, "value": 1.0}
            widget.canvas._last_skyfield_update = 0

        # Keep observer-local civil time aligned with the selected coordinates.
        try:
            from timezonefinder import TimezoneFinder

            tz_name = TimezoneFinder().timezone_at(
                lat=widget.latitude, lng=widget.longitude
            )
            if tz_name:
                widget.observer_timezone = str(tz_name)
                set_config_value("observer_timezone", widget.observer_timezone)
        except Exception:
            log_suppressed_exception(__name__, "request_relocation")

        if hasattr(widget, "weather"):
            widget.weather.set_location(widget.latitude, widget.longitude)
        if hasattr(widget, "canvas") and hasattr(widget.canvas, "weather"):
            widget.canvas.weather.set_location(
                widget.latitude, widget.longitude
            )

        widget.bake_debounce_timer.start(1500)

        if hasattr(widget, "lbl_loading"):
            from TerraLab.terrain.ray_precision import ray_count

            widget.on_horizon_progress_state(
                {
                    "job_id": getattr(widget, "_active_horizon_job_id", "")
                    or "",
                    "phase": "prepare",
                    "percent": 0.0,
                    "current": 0,
                    "total": ray_count(
                        get_config_value("horizon_ray_step_deg", 0.5)
                    ),
                }
            )

        widget.time_bar.update_params(
            widget.latitude, widget.longitude, widget.manual_day
        )

        bare = widget.terrain_coordinator.get_bare_elevation(
            widget.latitude, widget.longitude
        )
        widget._last_dem_elevation = bare
        widget.update_altitude_label()
        if is_automatic_mode(getattr(widget, "light_pollution_mode", None)):
            try:
                widget.recalculate_automatic_light_pollution()
            except Exception:
                log_suppressed_exception(__name__, "request_relocation")

        if hasattr(widget.canvas, "hint_overlay"):
            dem_m = getattr(widget, "_last_dem_elevation", None)
            offset = getattr(widget, "_observer_offset", 0.0)
            if dem_m is not None:
                txt = getTraduction(
                    "HUD.LocationHint",
                    "?? {lat}°, {lon}°  ·  {dem} m + {offset} m",
                ).format(
                    lat=f"{widget.latitude:.4f}",
                    lon=f"{widget.longitude:.4f}",
                    dem=int(dem_m),
                    offset=int(offset),
                )
            else:
                txt = f"Location {widget.latitude:.4f} deg, {widget.longitude:.4f} deg"
            widget.canvas.hint_overlay.show_hint(txt)

    except ValueError:
        print("[AstroWidget] Invalid Lat/Lon")


def widget_update_loop(widget):
    target_interval_ms = 16
    try:
        if bool(getattr(widget, "_updates_paused", False)):
            return
    except Exception:
        target_interval_ms = 16

    if (
        hasattr(widget, "timer")
        and widget.timer.interval() != target_interval_ms
    ):
        widget.timer.setInterval(target_interval_ms)

    now_mono = time.monotonic()
    hud_tick_s = 0.10
    scope_tick_s = 0.08
    climate_tick_s = 0.50

    last_hud = float(getattr(widget, "_last_hud_tick_mono", 0.0))
    last_scope = float(getattr(widget, "_last_scope_tick_mono", 0.0))
    last_climate = float(getattr(widget, "_last_climate_tick_mono", 0.0))

    run_hud_tick = (now_mono - last_hud) >= hud_tick_s
    run_scope_tick = (now_mono - last_scope) >= scope_tick_s
    run_climate_tick = (now_mono - last_climate) >= climate_tick_s

    if run_hud_tick:
        widget._last_hud_tick_mono = now_mono
    if run_scope_tick:
        widget._last_scope_tick_mono = now_mono
    if run_climate_tick:
        widget._last_climate_tick_mono = now_mono

    if widget.use_real_time:
        now = datetime.now()
        prev_year = int(getattr(widget, "manual_year", now.year))
        prev_day = int(getattr(widget, "manual_day", 0))
        widget.manual_year = now.year
        widget.manual_day = (now - datetime(now.year, 1, 1)).days
        day_changed = (widget.manual_year != prev_year) or (
            widget.manual_day != prev_day
        )
        if day_changed and hasattr(widget, "lbl_date"):
            widget.lbl_date.setText(widget.format_date(widget.manual_day))
        if day_changed and hasattr(widget, "time_bar"):
            widget.time_bar.update_params(
                widget.latitude, widget.longitude, widget.manual_day
            )
        h = now.hour + now.minute / 60.0 + now.second / 3600.0
        if (
            run_hud_tick
            and hasattr(widget, "time_bar")
            and widget.time_bar.isVisible()
        ):
            widget.time_bar.set_time(h)
    else:
        dt_hours = (
            max(0.001, float(getattr(widget.timer, "interval", lambda: 16)()))
            / 1000.0
            / 3600.0
        )
        widget.manual_hour += dt_hours
        if widget.manual_hour >= 24.0:
            widget.manual_hour -= 24.0
            widget.manual_day += 1
        elif widget.manual_hour < 0:
            widget.manual_hour += 24.0
            widget.manual_day -= 1
        if (
            run_hud_tick
            and hasattr(widget, "time_bar")
            and widget.time_bar.isVisible()
        ):
            widget.time_bar.set_time(widget.manual_hour)

    if (
        run_hud_tick
        and hasattr(widget, "chk_trails")
        and widget.chk_trails.isChecked()
        and hasattr(widget.canvas, "trail_start_hour")
        and widget.canvas.trail_start_hour is not None
        and hasattr(widget.canvas, "ut_hour")
    ):
        start = widget.canvas.trail_start_hour
        end = widget.canvas.ut_hour
        diff = end - start
        if diff < -12.0:
            diff += 24.0
        elif diff > 12.0:
            diff -= 24.0
        widget.trails_accumulated_seconds = max(0.0, diff * 3600.0)
        elapsed = int(widget.trails_accumulated_seconds)
        if elapsed < 60:
            if hasattr(widget, "lbl_trail_time"):
                widget.lbl_trail_time.setText(f"{elapsed}s")
        elif elapsed < 3600:
            m = elapsed // 60
            s = elapsed % 60
            if hasattr(widget, "lbl_trail_time"):
                widget.lbl_trail_time.setText(f"{m}m {s}s")
        else:
            h = elapsed // 3600
            m = (elapsed % 3600) // 60
            if hasattr(widget, "lbl_trail_time"):
                widget.lbl_trail_time.setText(f"{h}h {m}m")
    elif run_hud_tick:
        if hasattr(widget, "trails_accumulated_seconds"):
            delattr(widget, "trails_accumulated_seconds")
        if hasattr(widget, "lbl_trail_time"):
            widget.lbl_trail_time.setText("")

    if (
        run_scope_tick
        and hasattr(widget, "scope_panel")
        and widget.scope_panel.isVisible()
    ):
        widget._sync_scope_coord_inputs_from_canvas()
        if bool(getattr(widget.canvas, "scope_mode_enabled", lambda: False)()):
            try:
                widget._ensure_scope_catalog_loaded(force_now=False)
            except Exception:
                log_suppressed_exception(__name__, "widget_update_loop")

    if run_climate_tick:
        widget._refresh_climate_status_indicator()
        widget._refresh_stars_status_indicator()
        try:
            from TerraLab.ui.widget_misc_helpers import (
                widget_refresh_gaia_download_feedback,
            )

            widget_refresh_gaia_download_feedback(widget)
        except Exception:
            log_suppressed_exception(__name__, "widget_update_loop")
    widget.canvas.update()


def open_calendar(widget):
    dlg = QDialog(widget)
    dlg.setWindowTitle("Data")
    dlg.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
    dlg.setStyleSheet(CALENDAR_STYLESHEET)

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(0, 0, 0, 0)
    cal = QCalendarWidget()
    cal.setGridVisible(False)
    cal.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)

    current_date = datetime(widget.manual_year, 1, 1) + timedelta(
        days=widget.manual_day
    )
    cal.setSelectedDate(
        QDate(current_date.year, current_date.month, current_date.day)
    )

    def on_date_selected():
        qdate = cal.selectedDate()
        widget.manual_year = qdate.year()
        d = datetime(qdate.year(), qdate.month(), qdate.day())
        start_of_year = datetime(qdate.year(), 1, 1)
        day_idx = (d - start_of_year).days
        widget.update_date(day_idx)
        dlg.accept()

    cal.clicked.connect(on_date_selected)
    cal.activated.connect(on_date_selected)
    layout.addWidget(cal)

    pos = widget.lbl_date.mapToGlobal(
        QPointF(0, widget.lbl_date.height()).toPoint()
    )
    screen = QApplication.primaryScreen().geometry()
    sz = cal.sizeHint()
    if pos.y() + sz.height() > screen.bottom():
        pos = widget.lbl_date.mapToGlobal(QPointF(0, 0).toPoint())
        pos.setY(pos.y() - sz.height() - 5)
    dlg.move(pos)
    dlg.exec_()


def run_smoke_scenes(widget):
    if not hasattr(widget, "canvas"):
        return

    c = widget.canvas
    prev = {
        "zoom": float(c.zoom_level),
        "az": float(c.azimuth_offset),
        "el": float(c.elevation_angle),
        "scope_enabled": bool(c.scope_mode_enabled()),
        "scope_center": (
            tuple(c.scope_controller.center)
            if getattr(c.scope_controller, "center", None)
            else None
        ),
        "use_real_time": bool(getattr(widget, "use_real_time", False)),
        "manual_hour": float(getattr(widget, "manual_hour", 22.0)),
        "timebar_hour": float(getattr(widget.time_bar, "current_hour", 22.0)),
    }

    scenes = [
        {
            "name": "normal",
            "zoom": 1.0,
            "az": 180.0,
            "el": 35.0,
            "scope": False,
        },
        {
            "name": "telescope",
            "zoom": 12.0,
            "az": 180.0,
            "el": 45.0,
            "scope": True,
        },
        {
            "name": "high_density",
            "zoom": 24.0,
            "az": 180.0,
            "el": 60.0,
            "scope": False,
        },
    ]

    print("[SmokeScenes] start")
    try:
        widget.use_real_time = False
        widget.manual_hour = 22.0
        widget.time_bar.set_time(22.0)
        for scene in scenes:
            c.zoom_level = float(scene["zoom"])
            c.azimuth_offset = float(scene["az"])
            c.elevation_angle = float(scene["el"])
            c.set_scope_enabled(bool(scene["scope"]))
            if (
                scene["scope"]
                and getattr(c.scope_controller, "center", None) is None
            ):
                c.scope_controller.set_center(
                    (float(scene["el"]), float(scene["az"]))
                )

            c.debug_render_metrics = True
            c.update()
            QApplication.processEvents()
            c.repaint()
            QApplication.processEvents()

            stars_count = (
                c._scope_hud_star_count()
                if c.scope_mode_enabled()
                else c._visible_star_count_raw()
            )
            snap = c.scene_diagnostics.snapshot()
            fov = 100.0 / max(0.001, float(c.zoom_level))
            print(
                f"[SmokeScenes] {scene['name']} viewport={c.width()}x{c.height()} "
                f"zoom={c.zoom_level:.2f} fov={fov:.2f} cam_ra={c.azimuth_offset % 360.0:.2f} "
                f"cam_dec={c.elevation_angle:.2f} stars_hud={stars_count} "
                f"total_in_view={snap.counters.get('total_in_view', 0)} "
                f"after_mag={snap.counters.get('after_mag_cut', 0)} "
                f"after_bucket={snap.counters.get('after_bucket', 0)} "
                f"avg_radius={snap.counters.get('avg_radius', 0)} "
                f"ms_stars_renderer={snap.timings_ms.get('renderer_stars', 0.0):.2f}"
            )
    finally:
        c.zoom_level = prev["zoom"]
        c.azimuth_offset = prev["az"]
        c.elevation_angle = prev["el"]
        c.set_scope_enabled(prev["scope_enabled"])
        if prev["scope_center"] is not None:
            c.scope_controller.set_center(prev["scope_center"])
        widget.use_real_time = prev["use_real_time"]
        widget.manual_hour = prev["manual_hour"]
        widget.time_bar.set_time(prev["timebar_hour"])
        c.update()
        QApplication.processEvents()
        print("[SmokeScenes] done")


def load_catalog(widget):
    stars_dir = str(getattr(widget, "_stars_catalog_dir", "") or "").strip()
    if not stars_dir:
        try:
            runtime_layout = getattr(widget, "runtime_layout", {}) or {}
            stars_dir = str(runtime_layout.get("data_gaia", "") or "").strip()
        except Exception:
            stars_dir = ""
    if not stars_dir:
        stars_dir = str(runtime_data_dir_for("gaia"))
    stars_dir = os.path.normpath(os.path.expanduser(stars_dir))

    widget.celestial_objects = []
    if np is not None:
        try:
            entries = _discover_star_catalog_npz_entries(stars_dir)
            base_entry = _select_base_star_catalog_entry(
                entries, max_mag=STAR_CATALOG_NAKED_EYE_MAX_MAG
            )
            if base_entry is not None:
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
                    sid = (
                        base["source_id"][order]
                        if base["source_id"] is not None
                        else None
                    )
                    widget.celestial_objects = (
                        _build_celestial_objects_from_arrays(
                            ra,
                            dec,
                            mag,
                            bp_rp,
                            source_id=sid,
                        )
                    )
                    widget.np_ra = np.asarray(ra, dtype=np.float32)
                    widget.np_dec = np.asarray(dec, dtype=np.float32)
                    widget.np_mag = np.asarray(mag, dtype=np.float32)
                    widget.np_r, widget.np_g, widget.np_b = (
                        _bp_rp_to_rgb_arrays(bp_rp)
                    )
                    widget._scope_catalog_loaded_max_mag = max(
                        float(
                            getattr(
                                widget,
                                "_scope_catalog_loaded_max_mag",
                                STAR_CATALOG_NAKED_EYE_MAX_MAG,
                            )
                        ),
                        float(STAR_CATALOG_NAKED_EYE_MAX_MAG),
                    )
                    print(
                        f"Loaded base NPZ catalog: {len(widget.np_ra)} stars "
                        f"from {os.path.basename(base_entry['path'])}."
                    )
        except Exception as e:
            print(f"Error loading base NPZ catalog: {e}")

    if not widget.celestial_objects:
        print("Fallback to Random Stars")
        import random

        for _ in range(500):
            widget.celestial_objects.append(
                {
                    "id": "rnd",
                    "ra": random.uniform(0, 360),
                    "dec": random.uniform(-90, 90),
                    "mag": random.uniform(1.0, 6.0),
                    "bp_rp": random.uniform(-0.5, 2.0),
                }
            )

    widget.celestial_objects.sort(key=lambda x: x["mag"])

    if np is not None:
        try:
            widget.np_ra = np.array(
                [s["ra"] for s in widget.celestial_objects], dtype=np.float32
            )
            widget.np_dec = np.array(
                [s["dec"] for s in widget.celestial_objects], dtype=np.float32
            )
            widget.np_mag = np.array(
                [s["mag"] for s in widget.celestial_objects], dtype=np.float32
            )
            bprp = np.array(
                [s.get("bp_rp", 0.8) for s in widget.celestial_objects],
                dtype=np.float32,
            )
            widget.np_r, widget.np_g, widget.np_b = _bp_rp_to_rgb_arrays(bprp)
            print(f"NumPy Optimization: {len(widget.np_ra)} stars vectorized.")
        except Exception as e:
            print(f"NumPy Init Error: {e}")
            if hasattr(widget, "np_ra"):
                del widget.np_ra
