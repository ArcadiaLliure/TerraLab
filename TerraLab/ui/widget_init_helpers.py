"""Initialization helpers for the astronomical widget."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QPointF, QTimer, Qt
from PyQt5.QtWidgets import QLabel, QWidget

from TerraLab.common.app_paths import constellations_path
from TerraLab.common.custom_widget_base import CustomWidgetBase
from TerraLab.common.utils import (
    get_base_dir,
    get_config_value,
    set_config_value,
)
from TerraLab.data.assets_manager import AssetManager
from TerraLab.data.constants import STAR_CATALOG_NAKED_EYE_MAX_MAG
from TerraLab.light_pollution.modes import normalize_light_pollution_mode
from TerraLab.runtime.clients import ProcessWeatherSettings
from TerraLab.widgets.constellation_drawing import (
    ConstellationDrawingController,
)
from TerraLab.widgets.measurement_tools import MeasurementController
from TerraLab.widgets.scope_ui_manager import ScopeUIManager
from TerraLab.widgets.telescope_scope_mode import TelescopeScopeController
from TerraLab.scene.photometry import VisualMagnitudeEngine


def astro_canvas_init(obj, parent):
    self = obj
    QWidget.__init__(self, parent)
    self.parent_widget = parent
    self.setAttribute(Qt.WA_OpaquePaintEvent, True)
    self.setAutoFillBackground(False)
    self.setStyleSheet("background-color: #000000;")
    self._reported_first_useful_paint = False
    self.setMinimumSize(800, 600)
    self.azimuth_offset = 0
    self.elevation_angle = 40  # Default to Horizon
    self.vertical_offset_ratio = 0.3  # Default to Shift Down
    self.zoom_level = 1.0
    self.base_fov_deg = 93.9  # zoom=1.0 => ~17mm equiv (sensor 36mm)
    self.debug_render_metrics = bool(
        get_config_value("debug_render_metrics", False)
    )
    self._last_diagnostics_log_time = 0.0
    self.dragging = False
    self._camera_interaction_until = 0.0
    self._view_interaction_revision = 0
    self._camera_idle_timer = QTimer(self)
    self._camera_idle_timer.setSingleShot(True)
    self._camera_idle_timer.timeout.connect(self.update)
    self.last_mouse_x = 0
    self.last_mouse_y = 0
    self.setFocusPolicy(Qt.StrongFocus)
    self.setMouseTracking(True)
    self.visible_stars = []
    self.visible_stars_sx = []
    self.visible_stars_sy = []
    self.visible_sky_objects = []
    self.visible_ngc_objects = []
    self.press_pos = QPointF(0, 0)
    self._drawing_ctrl_pan_started = False
    self._drawing_ctrl_click_pending = False
    self._scope_camera_pan_started = False
    self._scope_camera_click_pending = False
    self._suppress_constellation_release_click = False
    self.trail_start_hour = None
    self.ut_hour = 12.0
    self.selected_target = None
    self.scope_camera_lock_to_target = True
    self.scope_reticle_lock_to_target = True
    # Illusion Parameters
    self.illusion_enabled = True
    self.horizon_refs = 0.5
    self.dome_flattening = 0.5
    self.trained_observer = False
    self.atmospheric_context = 0.5
    self.eclipse_lock_mode = False
    self.weather = getattr(parent, "weather", None)
    # HintOverlay -- toast HUD contextual per a zoom, temps i ubicacio
    from TerraLab.widgets.hint_overlay import HintOverlay as _HintOverlay

    self.hint_overlay = _HintOverlay(parent=self)
    self._cached_star_image = None
    self._cached_trail_image = None
    self._render_thread_shutdown = False
    self.rendering_busy = False
    self.trail_rendering_busy = False
    # Info Label for Selection
    self.lbl_info = QLabel("INFO", self)
    # Human Eye Reset Button
    # Human Eye Reset Button
    # Human Eye Reset Button
    from PyQt5.QtWidgets import QPushButton

    self.btn_human_eye = QPushButton("\U0001f441", self)
    self.btn_human_eye.setFixedSize(45, 24)
    self.btn_human_eye.setCursor(Qt.PointingHandCursor)
    self.btn_human_eye.setToolTip("Zoom Natural (17mm)")
    self.btn_human_eye.setStyleSheet("""
        QPushButton {
            background-color: rgba(5, 8, 17, 210);
            color: #d8b26a;
            border: 1px solid #3b4559;
            border-radius: 12px;
            font-size: 11px;
            font-weight: bold;
        }
        QPushButton:hover {
            background-color: #151b28;
            border-color: #d8b26a;
        }
    """)
    self.btn_human_eye.clicked.connect(self.reset_zoom_human)
    self.btn_human_eye.hide()
    self.hud_visible = True
    self.btn_hud_toggle = QPushButton("\U0001f441  HUD", self)
    self.btn_hud_toggle.setObjectName("hudToggleButton")
    self.btn_hud_toggle.setAccessibleName("Mostrar o amagar informació HUD")
    self.btn_hud_toggle.setToolTip("Mostrar o amagar la informació del visor")
    self.btn_hud_toggle.setCheckable(True)
    self.btn_hud_toggle.setChecked(True)
    self.btn_hud_toggle.setFixedSize(78, 26)
    self.btn_hud_toggle.setCursor(Qt.PointingHandCursor)
    self.btn_hud_toggle.setStyleSheet(
        """
        QPushButton {
            background-color: rgba(5, 8, 17, 220);
            color: #aab1c2;
            border: 1px solid #3b4559;
            border-radius: 13px;
            padding: 2px 8px;
            font-size: 10px;
            font-weight: 600;
        }
        QPushButton:hover {
            background-color: #151b28;
            color: #f1cd88;
            border-color: #d8b26a;
        }
        QPushButton:checked {
            background-color: #211c14;
            color: #f1cd88;
            border-color: #d8b26a;
        }
        """
    )
    self.btn_hud_toggle.toggled.connect(self.set_hud_visible)
    self.btn_hud_toggle.show()
    self.lbl_info.setStyleSheet(
        "color: #4fd8c4; font-size: 10px; "
        "background: rgba(2,4,10,210); border: 1px solid #252c3b; "
        "border-radius: 4px; padding: 3px 6px;"
    )
    self.lbl_info.move(10, 50)
    self.lbl_info.hide()
    # Skyfield Cache
    self._sf_cache = {
        "time": -1.0,
        "ut_hour": -999.0,
        "day": None,
        "year": None,
        "lat": None,
        "lon": None,
        "data": None,
    }
    self._eclipse_cache = {"time": -1, "value": 1.0}
    self._moon_pos_cache = {}  # Cache for moon calculations
    self._last_skyfield_update = 0  # timestamp in ms
    # Scope solar procedural cache (normalized features, stable across zoom).
    self._scope_solar_pattern_key = None
    self._scope_solar_pattern = None
    # Telescope scope mode and spherical measurement overlays.
    self.scope_controller = TelescopeScopeController()
    self.measurement_controller = MeasurementController()
    self._measurement_clear_revision = 0
    self._remote_pointer_domain = ""
    self.constellation_controller = ConstellationDrawingController(
        str(constellations_path())
    )
    self._constellation_rename_editor = None
    self._constellation_rename_group_index = None
    self._scope_interaction_until = 0.0


def astronomical_widget_init(obj, parent=None, **kwargs):
    self = obj
    self._closing = False
    self._lifecycle_timers = set()
    # 1. Initialize properties required by UI/Canvas
    self.asset_manager = AssetManager()
    from TerraLab.data.layer_manager import LayerManager

    self.layer_manager = LayerManager(self.asset_manager)
    self.runtime_layout = dict(getattr(self.asset_manager, "layout", {}))
    self.latitude = float(get_config_value("observer_lat", 41.189795))
    self.longitude = float(get_config_value("observer_lon", 1.210058))
    self._observer_offset = float(get_config_value("observer_offset", 0.0))
    self.observer_timezone = str(
        get_config_value("observer_timezone", "") or ""
    ).strip()
    # Catalog threshold used in magnitude mode.
    legacy_magnitude_limit = get_config_value("manual_eye_limit_mag", 8.0)
    self.magnitude_limit = float(
        get_config_value("magnitude_limit", legacy_magnitude_limit)
    )
    self.spike_magnitude_threshold = 3.2
    self.star_scale = 0.5
    self.auto_star_scale_multiplier = 1.0
    # Visual magnitude engine parameters.
    self.scope_aperture_mm = float(get_config_value("scope_aperture_mm", 80.0))
    self.scope_aperture_f_number = float(
        get_config_value("scope_aperture_f_number", 4.0)
    )
    self.scope_aperture_input_mode = str(
        get_config_value("scope_aperture_input_mode", "diameter_mm")
    )
    if self.scope_aperture_input_mode not in ("diameter_mm", "f_number"):
        self.scope_aperture_input_mode = "diameter_mm"
    self.scope_instrument_profile = str(
        get_config_value("scope_instrument_profile", "telescope")
    )
    if self.scope_instrument_profile not in (
        "telescope",
        "camera_aps_c",
        "camera_full_frame",
    ):
        self.scope_instrument_profile = "telescope"
    self.scope_eyepiece_mm = float(get_config_value("scope_eyepiece_mm", 20.0))
    self.scope_iso = int(get_config_value("scope_iso", 800))
    self.scope_exposure_s = float(get_config_value("scope_exposure_s", 2.0))
    self.scope_k_fallback = float(get_config_value("scope_k_fallback", 0.20))
    self.milkyway_overlay_enabled = bool(
        get_config_value("milkyway_overlay_enabled", True)
    )
    self.milkyway_overlay_blend_mode = str(
        get_config_value("milkyway_overlay_blend_mode", "add")
    )
    # L'asset Gaia inclos porta el nucli prop del centre horitzontal del PNG.
    # Amb equirectangular classic (lon 0 a l'esquerra) cal un desplacament de 180 deg.
    self.milkyway_overlay_ra_offset_deg = float(
        get_config_value("milkyway_overlay_ra_offset_deg", 180.0)
    )
    self.milkyway_overlay_coord_frame = str(
        get_config_value("milkyway_overlay_coord_frame", "galactic")
    )
    self.milkyway_overlay_lat_flip = bool(
        get_config_value("milkyway_overlay_lat_flip", True)
    )
    self.milkyway_overlay_lon_flip = bool(
        get_config_value("milkyway_overlay_lon_flip", True)
    )
    self.milkyway_overlay_opacity = float(
        get_config_value("milkyway_overlay_opacity", 0.65)
    )
    default_mw_texture = str(
        Path(self.runtime_layout.get("data_milkyway", get_base_dir()))
        / "milkyway_overlay.png"
    )
    self.milkyway_overlay_texture_path = str(
        get_config_value("milkyway_overlay_texture_path", default_mw_texture)
    )
    self.milkyway_overlay_sample_scale = float(
        get_config_value("milkyway_overlay_sample_scale", 1.0)
    )
    # Migration from previous low-quality default (0.35): prefer full-resolution sampling.
    if self.milkyway_overlay_sample_scale <= 0.35 + 1e-6:
        self.milkyway_overlay_sample_scale = 1.0
        set_config_value("milkyway_overlay_sample_scale", 1.0)
    tex_name = os.path.basename(
        str(self.milkyway_overlay_texture_path)
    ).lower()
    _cfg_missing = object()
    _saved_ra_offset = get_config_value(
        "milkyway_overlay_ra_offset_deg", _cfg_missing
    )
    _saved_lat_flip = get_config_value(
        "milkyway_overlay_lat_flip", _cfg_missing
    )
    _saved_lon_flip = get_config_value(
        "milkyway_overlay_lon_flip", _cfg_missing
    )
    _saved_ra_offset_is_zero = False
    try:
        if _saved_ra_offset is _cfg_missing:
            _saved_ra_offset_is_zero = True
        else:
            _saved_ra_offset_is_zero = abs(float(_saved_ra_offset)) < 1e-6
    except Exception:
        _saved_ra_offset_is_zero = False
    if (
        str(self.milkyway_overlay_coord_frame)
        .strip()
        .lower()
        .startswith("gal")
        and tex_name in ("milkyway_overlay.png", "via_negra.png")
        and _saved_ra_offset_is_zero
    ):
        # Migracio conservadora: els assets galactics inclosos tenen lon 0 al centre del mapa.
        self.milkyway_overlay_ra_offset_deg = 180.0
        set_config_value("milkyway_overlay_ra_offset_deg", 180.0)
    if (
        str(self.milkyway_overlay_coord_frame)
        .strip()
        .lower()
        .startswith("gal")
        and tex_name == "milkyway_overlay.png"
    ):
        # One-time conservative migration: do not overwrite user calibration.
        if _saved_lat_flip is _cfg_missing:
            self.milkyway_overlay_lat_flip = True
            set_config_value("milkyway_overlay_lat_flip", True)
        if _saved_lon_flip is _cfg_missing:
            self.milkyway_overlay_lon_flip = True
            set_config_value("milkyway_overlay_lon_flip", True)
    self.dust_map_enabled = bool(get_config_value("dust_map_enabled", False))
    default_dust_map = str(
        Path(self.runtime_layout.get("data_planck", get_base_dir()))
        / "planck_dust_opacity_eq_u16.npz"
    )
    self.dust_map_path = str(
        get_config_value("dust_map_path", default_dust_map)
    )
    self.dust_density_strength = float(
        get_config_value("dust_density_strength", 0.0)
    )
    self.dust_extinction_strength = float(
        get_config_value("dust_extinction_strength", 0.65)
    )
    if (
        bool(self.dust_map_enabled)
        and float(self.dust_density_strength) <= 0.0
        and float(self.dust_extinction_strength) <= 0.0
    ):
        # Preset conservador perque Planck sigui visible quan esta activat.
        self.dust_extinction_strength = 0.65
        set_config_value("dust_extinction_strength", 0.65)
    self.scope_eye_pupil_dark_mm = 6.5
    self.scope_atmo_metrics = {}
    self.visual_magnitude_engine = VisualMagnitudeEngine()
    self.visual_magnitude_result = None
    self.celestial_objects = []
    self.use_real_time = True
    self.manual_hour = 12.0
    self._dragging_time = False
    self.pure_colors = False
    # Light Pollution state
    configured_lp_mode = get_config_value("light_pollution_mode", None)
    legacy_auto_mode = get_config_value("is_auto_bortle", None)
    self.light_pollution_mode = normalize_light_pollution_mode(
        configured_lp_mode,
        legacy_auto=legacy_auto_mode,
    )
    self.auto_bortle_estimate = int(
        get_config_value("auto_bortle_estimate", 1)
    )
    self.bortle_value = int(
        get_config_value("bortle_value", self.auto_bortle_estimate)
    )
    self.light_pollution_enabled = bool(
        get_config_value("light_pollution_enabled", True)
    )
    configured_surface_style = (
        str(get_config_value("surface_visual_style", "original") or "original")
        .strip()
        .lower()
    )
    self.surface_visual_style = (
        "vibrant" if configured_surface_style == "vibrant" else "original"
    )
    now = datetime.now()
    self.manual_year = now.year
    self.manual_day = (now - datetime(now.year, 1, 1)).days
    # Weather runtime flags (persisted in config).
    # - weather_use_remote_metno: enables/disables real forecast provider.
    # - weather_cache_enabled: enables/disables disk cache reuse for weather data.
    self.weather_use_remote_metno = bool(
        get_config_value("weather_use_remote_metno", True)
    )
    self.weather_cache_enabled = bool(
        get_config_value("weather_cache_enabled", True)
    )
    self.scope_fallback_mag_limit = float(
        max(
            0.0,
            float(
                get_config_value("performance.scope_fallback_mag_limit", 8.0)
            ),
        )
    )
    self._perf_boot_t0_mono = time.perf_counter()
    # Weather needs to exist before setup_content -> AstroCanvas -> WeatherControlWidget
    self.weather = ProcessWeatherSettings(
        latitude=self.latitude,
        longitude=self.longitude,
        use_remote=self.weather_use_remote_metno,
        cache_enabled=self.weather_cache_enabled,
    )
    self.weather.set_remote_user_agent(self.asset_manager.get_user_agent())
    self.scene_load_stage = "boot"
    self._active_horizon_job_id = None
    self._deferred_controls_ready = False
    self._deferred_controls_build_scheduled = False
    # Scope/search managers must exist before any deferred UI callback can run.
    # Some platforms may process queued singleShot events during/just-after base init.
    self._scope_ui_manager = ScopeUIManager(self)
    self.search_index = {}
    self.search_lookup = {}
    # 2. Init Base Widget (Calls setup_ui -> setup_content)
    CustomWidgetBase.__init__(self, title="Astronomy", parent=parent, **kwargs)
    self._startup_placeholder_visible = True
    self._create_startup_placeholder()
    self._position_startup_placeholder()
    # 3. Post-UI initialization -- ASYNC (non-blocking)
    self.show_satellites = False
    self.satellites = []
    _gaia_catalog_dir = self.runtime_layout.get(
        "data_gaia", Path(get_base_dir()) / "data" / "gaia"
    )
    self._stars_catalog_dir = str(Path(_gaia_catalog_dir).expanduser())
    self._scope_catalog_loaded_max_mag = float(STAR_CATALOG_NAKED_EYE_MAX_MAG)
    self._catalog_max_mag = float(STAR_CATALOG_NAKED_EYE_MAX_MAG)
    self._gaia_extension_status_hide_timer = QTimer(self)
    self._gaia_extension_status_hide_timer.setSingleShot(True)
    self._gaia_extension_status_hide_timer.timeout.connect(
        self._hide_gaia_extension_status_label
    )
    self._catalog_bootstrap_started = False
    self._gaia_resume_prompt_shown = False
    self._gaia_background_dialog = None
    # Default to Horizon View (This uses self.canvas, created in setup_content)
    self.set_horizon_view()
    self.timer = QTimer(self)
    self.timer.setTimerType(Qt.CoarseTimer)
    self.timer.timeout.connect(self.update_loop)
    # State/clock cadence only. Pointer and animation events submit frames
    # directly, so there is no unconditional 60 Hz UI wake-up.
    self.timer.start(250)
    self.target_azimuth = None
    # Performance mode flag
    self._updates_paused = False
    self._saved_interval = 250
    # Debounce timer for bake requests (Avoid UI freeze and worker flooding)
    self.bake_debounce_timer = QTimer()
    self.bake_debounce_timer.setSingleShot(True)
    self.bake_debounce_timer.timeout.connect(self._do_delayed_bake)
    self.terrain_depth_debounce_timer = QTimer(self)
    self.terrain_depth_debounce_timer.setSingleShot(True)
    self.terrain_depth_debounce_timer.setInterval(2000)
    self.terrain_depth_debounce_timer.timeout.connect(
        self._apply_pending_terrain_depth
    )
    self.terrain_ray_precision_debounce_timer = QTimer(self)
    self.terrain_ray_precision_debounce_timer.setSingleShot(True)
    self.terrain_ray_precision_debounce_timer.setInterval(2000)
    self.terrain_ray_precision_debounce_timer.timeout.connect(
        self._apply_pending_terrain_ray_precision
    )
    self._full_horizon_profile = None
    self._pending_terrain_depth_km = None
    self._pending_terrain_ray_step_deg = None
    self._last_horizon_progress_text = ""
    self._schedule_lifecycle_callback(
        0, lambda: self._set_scene_load_stage("base_sky")
    )
    # UI construction is independent from catalog, ephemeris and horizon
    # readiness. The full-canvas shell is only a short launch transition.
    self._schedule_lifecycle_callback(
        0, self._schedule_deferred_controls_build
    )
    self._schedule_lifecycle_callback(250, self._hide_startup_placeholder)
    self._schedule_lifecycle_callback(200, self._start_async_bootstrap)
    self._schedule_lifecycle_callback(
        1500, self._maybe_resume_pending_gaia_download
    )
