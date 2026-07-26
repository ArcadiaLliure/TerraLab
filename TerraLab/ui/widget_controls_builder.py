"""Deferred controls builder for the astronomical widget."""

from __future__ import annotations

from PyQt5.QtCore import QRectF, QSize, QTimer, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QShortcut,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from TerraLab.common.utils import getTraduction, set_config_value
from TerraLab.data.layer_manager import LayerId
from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    LP_MODE_BORTLE,
    LP_MODE_MAGNITUDE,
)
from TerraLab.ui.design_system import (
    CONTROL_DRAWER_STYLESHEET,
    QUICK_TOOLBAR_STYLESHEET,
)
from TerraLab.ui.time_bar import ClickableLabel, RusticTimeBar
from TerraLab.widgets.measurement_tools import (
    TOOL_CIRCLE,
    TOOL_NONE,
    TOOL_RECTANGLE,
    TOOL_RULER,
    TOOL_SQUARE,
)
from TerraLab.widgets.telescope_scope_mode import TelescopeScopeController


def build_deferred_controls_ui(widget):
    from TerraLab.ui.astro_canvas import AstroCanvas

    self = widget
    if getattr(self, "_deferred_controls_ready", False):
        return
    self._deferred_controls_build_scheduled = False
    # Hide standard window decorations since this is a wallpaper/panel
    if hasattr(self, "title_bar"):
        self.title_bar.hide()
    # Use the content layout provided by CustomWidgetBase
    layout = self.content_layout
    layout.setContentsMargins(0, 0, 0, 0)
    if not hasattr(self, "canvas"):
        self.canvas = AstroCanvas(self)
        layout.addWidget(self.canvas, 1)
    self._shortcut_delete_constellation = QShortcut(Qt.Key_Delete, self)
    self._shortcut_delete_constellation.setContext(
        Qt.WidgetWithChildrenShortcut
    )
    self._shortcut_delete_constellation.activated.connect(
        self._delete_constellation_shortcut
    )
    self._shortcut_backspace_constellation = QShortcut(Qt.Key_Backspace, self)
    self._shortcut_backspace_constellation.setContext(
        Qt.WidgetWithChildrenShortcut
    )
    self._shortcut_backspace_constellation.activated.connect(
        self._delete_constellation_shortcut
    )
    self._shortcut_finish_constellation = QShortcut(Qt.Key_Return, self)
    self._shortcut_finish_constellation.setContext(
        Qt.WidgetWithChildrenShortcut
    )
    self._shortcut_finish_constellation.activated.connect(
        self._finish_constellation_shortcut
    )
    self._shortcut_finish_constellation_enter = QShortcut(Qt.Key_Enter, self)
    self._shortcut_finish_constellation_enter.setContext(
        Qt.WidgetWithChildrenShortcut
    )
    self._shortcut_finish_constellation_enter.activated.connect(
        self._finish_constellation_shortcut
    )
    # Timeline estable i una sola pàgina de controls visible.
    frame_layout = QVBoxLayout()
    frame_layout.setSpacing(8)
    frame_layout.setContentsMargins(8, 8, 8, 8)
    # â”€â”€ TIME BAR (At the top) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    self.time_bar = RusticTimeBar()
    self.time_bar.valueChanged.connect(self.on_time_bar_change)
    self.time_bar.dragStateChanged.connect(self.on_time_bar_drag_state_changed)
    self.time_bar.update_params(self.latitude, self.longitude, self.manual_day)
    frame_layout.addWidget(self.time_bar)
    # Loading indicator (absolute, sobre canvas)
    if not hasattr(self, "lbl_loading"):
        self.lbl_loading = QLabel(
            getTraduction(
                "Astro.LoadingTopography", "? Carregant topografia..."
            ),
            self,
        )
        self.lbl_loading.setStyleSheet(
            "color: #f1cd88; font-weight: 600; "
            "background-color: rgba(2,4,10,220); "
            "border: 1px solid #3b4559; padding: 5px; border-radius: 5px;"
        )
        self.lbl_loading.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_loading.setWordWrap(False)
        self.lbl_loading.hide()
        self.lbl_loading.resize(420, 32)
        self._position_loading_label()
    if not hasattr(self, "lbl_gaia_extension_status"):
        self.lbl_gaia_extension_status = QLabel("", self)
        self.lbl_gaia_extension_status.setStyleSheet(
            "color: #ffe680; font-weight: bold; background-color: rgba(0,0,0,140); "
            "padding: 5px; border-radius: 4px;"
        )
        self.lbl_gaia_extension_status.setAlignment(
            Qt.AlignLeft | Qt.AlignVCenter
        )
        self.lbl_gaia_extension_status.setWordWrap(False)
        self.lbl_gaia_extension_status.hide()
        self._position_gaia_extension_status_label()
    # â”€â”€ THE 3 BOTTOM PANELS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    location_page = QWidget()
    location_page.setObjectName("locationControlsPage")
    l_loc = QHBoxLayout(location_page)
    l_loc.setContentsMargins(10, 8, 10, 8)
    l_loc.setSpacing(15)
    # Col: inputs (Lat, Lon, Alt) + Button
    l_coord = QVBoxLayout()
    l_coord.setSpacing(2)
    h_latlon = QHBoxLayout()
    v_inputs = QVBoxLayout()
    v_inputs.setSpacing(2)
    h_lat = QHBoxLayout()
    h_lat.addWidget(QLabel("Latitud"))
    self.txt_lat = QLineEdit(str(self.latitude))
    self.txt_lat.setFixedWidth(60)
    self.txt_lat.returnPressed.connect(self.update_location)
    h_lat.addWidget(self.txt_lat)
    v_inputs.addLayout(h_lat)
    h_lon = QHBoxLayout()
    h_lon.addWidget(QLabel("Longitud"))
    self.txt_lon = QLineEdit(str(self.longitude))
    self.txt_lon.setFixedWidth(60)
    self.txt_lon.returnPressed.connect(self.update_location)
    h_lon.addWidget(self.txt_lon)
    v_inputs.addLayout(h_lon)
    h_latlon.addLayout(v_inputs)
    # Lock/Pin button
    self.btn_relocate = QPushButton("??")
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
    self.spin_extra_height.setSingleStep(0.5)
    self.spin_extra_height.setDecimals(1)
    self.spin_extra_height.setFixedWidth(50)
    self.spin_extra_height.setValue(
        float(get_config_value("observer_offset", 0.0))
    )
    self.spin_extra_height.valueChanged.connect(self.on_extra_height_changed)
    h_alt.addWidget(self.spin_extra_height)
    l_coord.addLayout(h_alt)
    self.lbl_altitude_info = QLabel("--")
    self.lbl_altitude_info.hide()  # Hidden as per Mockup
    l_coord.addWidget(self.lbl_altitude_info)
    l_loc.addLayout(l_coord)
    # Col: Calendar Mockup
    l_date = QVBoxLayout()
    # Create a container with black background
    cal_container = QFrame()
    cal_container.setStyleSheet(
        "QFrame { border: 1px solid #252c3b; background: #050811; "
        "border-radius: 5px; }"
    )
    cal_layout = QHBoxLayout(cal_container)
    cal_layout.setContentsMargins(5, 5, 5, 5)
    cal_layout.setSpacing(4)
    self.btn_prev_day = QPushButton("<")
    self.btn_prev_day.setFixedSize(20, 20)
    self.btn_prev_day.clicked.connect(self.prev_day)
    cal_layout.addWidget(self.btn_prev_day)
    self.lbl_date = ClickableLabel(self.format_date(self.manual_day))
    self.lbl_date.setAlignment(Qt.AlignCenter)
    self.lbl_date.setCursor(Qt.PointingHandCursor)
    self.lbl_date.setStyleSheet(
        "color: #d8b26a; font-style: normal; font-size: 10px; "
        "font-weight: 600; border: none; background: transparent;"
    )
    self.lbl_date.clicked.connect(self.open_calendar)
    cal_layout.addWidget(self.lbl_date, 1)  # Expand
    self.btn_next_day = QPushButton(">")
    self.btn_next_day.setFixedSize(20, 20)
    self.btn_next_day.clicked.connect(self.next_day)
    cal_layout.addWidget(self.btn_next_day)
    l_date.addWidget(cal_container)
    self.btn_realtime = QPushButton("Temps real")
    self.btn_realtime.setCheckable(True)
    self.btn_realtime.toggled.connect(self.toggle_realtime)
    self.btn_realtime.setStyleSheet(
        "border-radius: 5px; border: 1px solid #3b4559; "
        "font-style: normal; color: #f3f5fa; background: #0d111c; "
        "padding: 3px 7px;"
    )
    l_date.addWidget(self.btn_realtime)
    l_date.addStretch(1)
    l_loc.addLayout(l_date)
    l_loc.addStretch(1)
    sky_page = QWidget()
    sky_page.setObjectName("skyControlsPage")
    l_sky = QHBoxLayout(sky_page)
    l_sky.setContentsMargins(10, 8, 10, 8)
    l_sky.setSpacing(15)
    # Col 1: Checks
    v_chk = QVBoxLayout()
    v_chk.setSpacing(1)
    self.chk_clima = QCheckBox(getTraduction("Astro.Climate", "Climate"))
    self.chk_clima.setEnabled(True)
    self.chk_clima.setChecked(self._load_visibility_state("clima", False))
    # Set state initially
    setattr(self.canvas.weather, "enabled", bool(self.chk_clima.isChecked()))
    self.chk_clima.toggled.connect(self.on_climate_toggled)
    self.chk_light_pollution = QCheckBox("Contaminacio luminica")
    self.chk_light_pollution.setEnabled(True)
    self.chk_light_pollution.setChecked(
        self._load_visibility_state(
            "contaminacio_luminica", bool(self.light_pollution_enabled)
        )
    )
    self.chk_light_pollution.toggled.connect(self.on_light_pollution_toggled)
    self.light_pollution_enabled = bool(self.chk_light_pollution.isChecked())
    set_config_value(
        "light_pollution_enabled", bool(self.light_pollution_enabled)
    )
    self.lbl_climate_fallback = QLabel(
        getTraduction(
            "Astro.ClimateFallbackActive",
            "Real weather unavailable (fallback)",
        )
    )
    self.lbl_climate_fallback.setVisible(False)
    self.lbl_climate_fallback.setStyleSheet(
        "font-size: 9px; color: #8a3b00; background: rgba(255, 208, 140, 165); "
        "border: 1px solid #a06a2f; border-radius: 6px; padding: 1px 6px;"
    )
    # Anti-flicker state for climate fallback badge:
    # show only after fallback persists, hide only after remote is stable.
    self._climate_fallback_since = None
    self._climate_remote_since = None
    self._climate_fallback_show_delay_s = 2.5
    self._climate_remote_hide_delay_s = 1.2
    self._stars_fallback_active = False
    self._stars_fallback_reason = ""
    self._stars_fallback_since = None
    self._stars_primary_since = None
    self._stars_fallback_show_delay_s = 1.2
    self._stars_primary_hide_delay_s = 0.8
    self.chk_planets = QCheckBox("Planetes")
    self.chk_planets.setEnabled(True)
    self.chk_planets.setChecked(self._load_visibility_state("planetes", True))
    self.chk_planets.toggled.connect(self.canvas.update)
    self.chk_planets.toggled.connect(
        lambda c: self._persist_visibility_state("planetes", c)
    )
    self.chk_sun_moon = QCheckBox("Sol i Lluna")
    self.chk_sun_moon.setEnabled(True)
    self.chk_sun_moon.setChecked(
        self._load_visibility_state("sol_i_lluna", True)
    )
    self.chk_sun_moon.toggled.connect(self.canvas.update)
    self.chk_sun_moon.toggled.connect(
        lambda c: self._persist_visibility_state("sol_i_lluna", c)
    )
    self.chk_solar_system = QCheckBox("Sistema solar")
    self.chk_solar_system.setChecked(
        self._load_visibility_state("sistema_solar", True)
    )
    self.chk_solar_system.toggled.connect(self.on_solar_system_toggled)
    self.chk_planets.setText("   Planetes")
    self.chk_sun_moon.setText("   Sol i Lluna")
    self.chk_planets.setEnabled(self.chk_solar_system.isChecked())
    self.chk_sun_moon.setEnabled(self.chk_solar_system.isChecked())
    self.chk_enable_sky = QCheckBox("Estrelles")
    self.chk_enable_sky.setChecked(
        self._load_visibility_state("estrelles", True)
    )
    self.chk_enable_sky.toggled.connect(self.on_stars_toggled)
    self.lbl_stars_fallback = QLabel(
        getTraduction("Astro.StarsFallbackActive", "Catàleg fallback")
    )
    self.lbl_stars_fallback.setVisible(False)
    self.lbl_stars_fallback.setStyleSheet(
        "font-size: 9px; color: #8a3b00; background: rgba(255, 208, 140, 165); "
        "border: 1px solid #a06a2f; border-radius: 6px; padding: 1px 6px;"
    )
    self.chk_enable_milkyway = QCheckBox("Via Lactia")
    self.chk_enable_milkyway.setChecked(
        self._load_visibility_state(
            "via_lactia", bool(self.milkyway_overlay_enabled)
        )
    )
    self.chk_enable_milkyway.toggled.connect(self.on_milkyway_toggled)
    self.chk_enable_planck_dust = QCheckBox("Pols Planck")
    self.chk_enable_planck_dust.setChecked(
        self._load_visibility_state("pols_planck", bool(self.dust_map_enabled))
    )
    self.chk_enable_planck_dust.toggled.connect(self.on_planck_dust_toggled)
    self.chk_deep_space = QCheckBox("Catàleg NGC")
    self.chk_deep_space.setEnabled(True)
    self.chk_deep_space.setChecked(
        self._load_visibility_state("espai_profund", False)
    )
    self.chk_deep_space.toggled.connect(self.on_deep_space_toggled)
    self.lbl_milkyway_status = QLabel("MW: --")
    self.lbl_milkyway_status.setStyleSheet(
        "font-size: 9px; color: #aab1c2;"
    )
    self.lbl_milkyway_status.setVisible(True)
    for c in (
        self.chk_clima,
        self.chk_solar_system,
        self.chk_planets,
        self.chk_sun_moon,
        self.chk_enable_milkyway,
        self.chk_enable_planck_dust,
        self.chk_deep_space,
    ):
        c.setStyleSheet(
            c.styleSheet()
            + "; font-style: normal; font-weight: normal; color: #70798d;"
            if not c.isEnabled()
            else "; font-style: normal; font-weight: normal; color: #f3f5fa;"
        )
        v_chk.addWidget(c)
    self.chk_enable_sky.setStyleSheet(
        self.chk_enable_sky.styleSheet()
        + (
            "; font-style: normal; font-weight: normal; color: #70798d;"
            if not self.chk_enable_sky.isEnabled()
            else "; font-style: normal; font-weight: normal; color: #f3f5fa;"
        )
    )
    h_stars_row = QHBoxLayout()
    h_stars_row.setContentsMargins(0, 0, 0, 0)
    h_stars_row.setSpacing(6)
    h_stars_row.addWidget(self.chk_enable_sky)
    h_stars_row.addWidget(self.lbl_stars_fallback)
    h_stars_row.addStretch(1)
    v_chk.addLayout(h_stars_row)
    self.lbl_gaia_download_status = QLabel("")
    self.lbl_gaia_download_status.setVisible(False)
    self.lbl_gaia_download_status.setStyleSheet(
        "font-size: 9px; color: #204a87;"
    )
    from PyQt5.QtWidgets import QProgressBar as _QProgressBar

    self.progress_gaia_download = _QProgressBar()
    self.progress_gaia_download.setVisible(False)
    self.progress_gaia_download.setRange(0, 100)
    self.progress_gaia_download.setValue(0)
    self.progress_gaia_download.setTextVisible(False)
    self.progress_gaia_download.setFixedHeight(7)
    self.progress_gaia_download.setStyleSheet(
        "QProgressBar {"
        "  border: 1px solid #252c3b;"
        "  border-radius: 3px;"
        "  background: #050811;"
        "}"
        "QProgressBar::chunk {"
        "  background: #d8b26a;"
        "  border-radius: 2px;"
        "}"
    )
    v_chk.addWidget(self.lbl_gaia_download_status)
    v_chk.addWidget(self.progress_gaia_download)
    v_chk.addWidget(self.lbl_milkyway_status)
    v_chk.addWidget(self.lbl_climate_fallback)
    self._refresh_milkyway_status_indicator()
    l_sky.addLayout(v_chk)
    # Col 2: Sliders
    v_sld = QVBoxLayout()
    v_sld.setSpacing(2)
    self.chk_pure_colors = QCheckBox("Colors purs")
    self.chk_pure_colors.setStyleSheet(
        "font-style: normal; font-weight: normal;"
    )
    self.chk_pure_colors.toggled.connect(self.toggle_pure_colors)
    v_sld.addWidget(self.chk_pure_colors)

    def make_sld(layout, label, r, val, cb, value_formatter=None):
        h = QHBoxLayout()
        h.setSpacing(4)
        label_widget = QLabel(label)
        label_widget.setStyleSheet(
            "font-style: normal; font-weight: normal; min-width: 65px;"
        )
        h.addWidget(label_widget)
        l_min = QLabel(str(r[0]))
        l_min.setStyleSheet("font-size: 8px; color: #70798d;")
        h.addWidget(l_min)
        s = QSlider(Qt.Horizontal)
        s.setRange(*r)
        s.setValue(val)
        s.setMinimumWidth(80)
        h.addWidget(s)
        l_max = QLabel(str(r[1]))
        l_max.setStyleSheet("font-size: 8px; color: #70798d;")
        h.addWidget(l_max)

        def format_value(value):
            if value_formatter is None:
                return str(value)
            return str(value_formatter(value))

        l_curr = QLabel(f"[{format_value(val)}]")
        l_curr.setStyleSheet(
            "font-size: 9px; color: #d8b26a; min-width: 30px;"
        )
        h.addWidget(l_curr)
        # Attach labels to slider for dynamic updates
        s._lbl_min = l_min
        s._lbl_max = l_max
        s._lbl_curr = l_curr

        def on_changed(new_val):
            l_curr.setText(f"[{format_value(new_val)}]")
            if cb:
                cb(new_val)

        s.valueChanged.connect(on_changed)

        def set_silent_value(new_val):
            s.blockSignals(True)
            s.setValue(new_val)
            s.blockSignals(False)
            l_curr.setText(f"[{format_value(new_val)}]")

        s.set_silent_value = set_silent_value
        layout.addLayout(h)
        return s, label_widget

    self.slider_size, self.lbl_size_control = make_sld(
        v_sld,
        "Mida",
        (5, 40),
        int(self.star_scale * 10),
        self.update_star_scale,
    )
    self.slider_spikes, self.lbl_spikes_control = make_sld(
        v_sld,
        "Puntes",
        (-30, 70),
        int(self.spike_magnitude_threshold * 10),
        self.update_spikes,
    )
    # Unified Light Pollution Control
    l_shared = QVBoxLayout()
    h_ctrl = QHBoxLayout()
    h_ctrl.setSpacing(5)
    from PyQt5.QtWidgets import QComboBox

    self.combo_lp_mode = QComboBox()
    self.combo_lp_mode.addItem(
        getTraduction("Astro.BortleLabel", "Bortle"), LP_MODE_BORTLE
    )
    self.combo_lp_mode.addItem(
        getTraduction("Astro.MagnitudeLabel", "Magnitude"), LP_MODE_MAGNITUDE
    )
    self.combo_lp_mode.addItem(
        getTraduction("Astro.AutoMode", "Automatic"), LP_MODE_AUTOMATIC
    )
    self.combo_lp_mode.setToolTip(
        getTraduction(
            "Astro.LPModeTooltip",
            "Light pollution mode: Bortle, limiting magnitude, or automatic by location",
        )
    )
    self.combo_lp_mode.setStyleSheet(
        "font-size: 10px; min-width: 80px; max-width: 100px; height: 22px;"
    )
    self.combo_lp_mode.currentIndexChanged.connect(self.on_lp_mode_changed)
    h_ctrl.addWidget(self.combo_lp_mode)
    self.slider_light, self.lbl_light_text = make_sld(
        h_ctrl,
        getTraduction("Astro.BortleLabel", "Bortle"),
        (1, 9),
        int(self.bortle_value),
        self.update_lp_slider,
        value_formatter=self.format_light_pollution_slider_value,
    )
    QTimer.singleShot(500, self.update_altitude_label)
    l_shared.addLayout(h_ctrl)
    v_sld.addLayout(l_shared)
    self.combo_lp_mode.blockSignals(True)
    mode_index = self.combo_lp_mode.findData(self.light_pollution_mode)
    self.combo_lp_mode.setCurrentIndex(max(0, mode_index))
    self.combo_lp_mode.blockSignals(False)
    self.on_lp_mode_changed(self.combo_lp_mode.currentIndex())
    self.ambient_light = 1.0
    l_sky.addLayout(v_sld)
    # Col 3: Circumpolar + Search
    v_ext = QVBoxLayout()
    v_ext.setSpacing(4)
    self.chk_trails = QPushButton(
        getTraduction("Astro.StartCircumpolar", "Iniciar circumpolar")
    )
    self.chk_trails.setCheckable(True)
    self.chk_trails.setStyleSheet(
        "font-size: 10px; font-weight: normal; font-style: normal; "
        "border-radius: 5px; border: 1px solid #3b4559; padding: 3px;"
    )
    self.chk_trails.toggled.connect(self.on_trails_toggled)
    self._update_circumpolar_button_text()
    v_ext.addWidget(self.chk_trails)
    self.lbl_trail_time = QLabel("")
    self.lbl_trail_time.setStyleSheet(
        "font-weight: normal; color: #aab1c2; font-size: 10px;"
    )
    self.lbl_trail_time.setAlignment(Qt.AlignCenter)
    v_ext.addWidget(self.lbl_trail_time)
    v_ext.addStretch()
    self.txt_search = QLineEdit()
    self.txt_search.setPlaceholderText(
        getTraduction("Astro.SearchPlaceholder", "Search object...")
    )
    self.txt_search.setStyleSheet(
        "font-weight: normal; font-style: normal; border-radius: 4px; padding: 2px;"
    )
    self.txt_search.returnPressed.connect(self.on_search_triggered)
    v_ext.addWidget(self.txt_search)
    self.btn_quick_welcome = QPushButton("Assistent de dades i capes")
    self.btn_quick_welcome.setStyleSheet(
        "font-size: 10px; font-weight: normal;"
    )
    self.btn_quick_welcome.clicked.connect(self._open_quick_welcome)
    v_ext.addWidget(self.btn_quick_welcome)
    # Telescope / Tools entry points
    h_overlays = QHBoxLayout()
    h_overlays.setSpacing(4)
    self.btn_scope_panel = QPushButton(
        getTraduction("Astro.ScopeButton", "Tube / Telescope")
    )
    self.btn_scope_panel.setCheckable(True)
    self.btn_scope_panel.setStyleSheet("font-size: 10px; font-weight: normal;")
    self.btn_scope_panel.toggled.connect(self.toggle_scope_panel)
    h_overlays.addWidget(self.btn_scope_panel)
    self.btn_tools_panel = QPushButton(
        getTraduction("Astro.ToolsButton", "Tools")
    )
    self.btn_tools_panel.setCheckable(True)
    self.btn_tools_panel.setStyleSheet("font-size: 10px; font-weight: normal;")
    self.btn_tools_panel.toggled.connect(self.toggle_tools_panel)
    h_overlays.addWidget(self.btn_tools_panel)
    v_ext.addLayout(h_overlays)
    # Telescope panel
    self.scope_panel = QFrame()
    self.scope_panel.setStyleSheet(
        "border: 1px solid #252c3b; border-radius: 6px; "
        "background: #050811;"
    )
    v_scope = QVBoxLayout(self.scope_panel)
    v_scope.setSpacing(3)
    v_scope.setContentsMargins(4, 4, 4, 4)
    from PyQt5.QtWidgets import QComboBox, QDoubleSpinBox, QSpinBox

    h_focal = QHBoxLayout()
    h_focal.addWidget(QLabel(getTraduction("Astro.ScopeFocal", "Focal (mm)")))
    self.scope_focal_spin = QDoubleSpinBox()
    self.scope_focal_spin.setRange(1.0, 20000.0)
    self.scope_focal_spin.setDecimals(1)
    self.scope_focal_spin.setSingleStep(10.0)
    self.scope_focal_spin.setValue(250.0)
    self.scope_focal_spin.setFixedWidth(74)
    self.scope_focal_spin.valueChanged.connect(
        lambda v: self.canvas.set_scope_focal_mm(v)
    )
    h_focal.addWidget(self.scope_focal_spin)
    v_scope.addLayout(h_focal)
    h_instrument = QHBoxLayout()
    self.scope_instrument_label = QLabel(
        getTraduction("Astro.ScopeInstrumentProfile", "Instrument")
    )
    h_instrument.addWidget(self.scope_instrument_label)
    self.scope_instrument_combo = QComboBox()
    self.scope_instrument_combo.addItem(
        getTraduction("Astro.ScopeInstrumentTelescope", "Telescope"),
        "telescope",
    )
    self.scope_instrument_combo.addItem(
        getTraduction("Astro.ScopeInstrumentAPSC", "APS-C"),
        "camera_aps_c",
    )
    self.scope_instrument_combo.addItem(
        getTraduction("Astro.ScopeInstrumentFullFrame", "Full Frame"),
        "camera_full_frame",
    )
    instrument_index = 0
    for i in range(self.scope_instrument_combo.count()):
        if (
            self.scope_instrument_combo.itemData(i)
            == self.scope_instrument_profile
        ):
            instrument_index = i
            break
    self.scope_instrument_combo.setCurrentIndex(instrument_index)
    self.scope_instrument_combo.currentIndexChanged.connect(
        self.on_scope_instrument_changed
    )
    h_instrument.addWidget(self.scope_instrument_combo)
    v_scope.addLayout(h_instrument)
    h_aperture_mode = QHBoxLayout()
    self.scope_aperture_mode_label = QLabel(
        getTraduction("Astro.ScopeApertureInput", "Aperture input")
    )
    h_aperture_mode.addWidget(self.scope_aperture_mode_label)
    self.scope_aperture_mode_combo = QComboBox()
    self.scope_aperture_mode_combo.addItem(
        getTraduction("Astro.ScopeApertureModeMM", "Diameter (mm)"),
        "diameter_mm",
    )
    self.scope_aperture_mode_combo.addItem(
        getTraduction("Astro.ScopeApertureModeF", "f/ number"),
        "f_number",
    )
    mode_index = 0 if self.scope_aperture_input_mode == "diameter_mm" else 1
    self.scope_aperture_mode_combo.setCurrentIndex(mode_index)
    self.scope_aperture_mode_combo.currentIndexChanged.connect(
        self.on_scope_aperture_mode_changed
    )
    h_aperture_mode.addWidget(self.scope_aperture_mode_combo)
    v_scope.addLayout(h_aperture_mode)
    h_aperture = QHBoxLayout()
    self.scope_aperture_label = QLabel(
        getTraduction("Astro.ScopeAperture", "Aperture (mm)")
    )
    h_aperture.addWidget(self.scope_aperture_label)
    self.scope_aperture_spin = QDoubleSpinBox()
    self.scope_aperture_spin.setFixedWidth(74)
    self.scope_aperture_spin.valueChanged.connect(
        self._on_scope_aperture_changed
    )
    h_aperture.addWidget(self.scope_aperture_spin)
    v_scope.addLayout(h_aperture)
    self._sync_scope_aperture_controls()
    h_eyepiece = QHBoxLayout()
    self.scope_eyepiece_label = QLabel(
        getTraduction("Astro.ScopeEyepiece", "Eyepiece (mm)")
    )
    h_eyepiece.addWidget(self.scope_eyepiece_label)
    self.scope_eyepiece_spin = QDoubleSpinBox()
    self.scope_eyepiece_spin.setRange(2.0, 80.0)
    self.scope_eyepiece_spin.setDecimals(1)
    self.scope_eyepiece_spin.setSingleStep(0.5)
    self.scope_eyepiece_spin.setValue(self.scope_eyepiece_mm)
    self.scope_eyepiece_spin.setFixedWidth(74)
    self.scope_eyepiece_spin.valueChanged.connect(
        self._on_scope_eyepiece_changed
    )
    h_eyepiece.addWidget(self.scope_eyepiece_spin)
    v_scope.addLayout(h_eyepiece)
    h_iso = QHBoxLayout()
    h_iso.addWidget(QLabel(getTraduction("Astro.ScopeISO", "ISO")))
    self.scope_iso_spin = QSpinBox()
    self.scope_iso_spin.setRange(100, 51200)
    self.scope_iso_spin.setSingleStep(100)
    self.scope_iso_spin.setValue(int(self.scope_iso))
    self.scope_iso_spin.setFixedWidth(74)
    self.scope_iso_spin.valueChanged.connect(self._on_scope_iso_changed)
    h_iso.addWidget(self.scope_iso_spin)
    v_scope.addLayout(h_iso)
    h_exp = QHBoxLayout()
    h_exp.addWidget(
        QLabel(getTraduction("Astro.ScopeExposure", "Exposure (s)"))
    )
    self.scope_exposure_spin = QDoubleSpinBox()
    self.scope_exposure_spin.setRange(0.1, 60000.0)
    self.scope_exposure_spin.setDecimals(1)
    self.scope_exposure_spin.setSingleStep(0.5)
    self.scope_exposure_spin.setValue(self.scope_exposure_s)
    self.scope_exposure_spin.setFixedWidth(74)
    self.scope_exposure_spin.valueChanged.connect(
        self._on_scope_exposure_changed
    )
    h_exp.addWidget(self.scope_exposure_spin)
    v_scope.addLayout(h_exp)
    h_ra = QHBoxLayout()
    h_ra.addWidget(QLabel(getTraduction("Astro.ScopeInputRA", "RA")))
    self.scope_ra_h_spin = QSpinBox()
    self.scope_ra_h_spin.setRange(0, 23)
    self.scope_ra_h_spin.setFixedWidth(48)
    h_ra.addWidget(self.scope_ra_h_spin)
    h_ra.addWidget(QLabel("h"))
    self.scope_ra_m_spin = QSpinBox()
    self.scope_ra_m_spin.setRange(0, 59)
    self.scope_ra_m_spin.setFixedWidth(48)
    h_ra.addWidget(self.scope_ra_m_spin)
    h_ra.addWidget(QLabel("m"))
    self.scope_ra_s_spin = QDoubleSpinBox()
    self.scope_ra_s_spin.setRange(0.0, 59.9)
    self.scope_ra_s_spin.setDecimals(1)
    self.scope_ra_s_spin.setSingleStep(0.1)
    self.scope_ra_s_spin.setFixedWidth(58)
    h_ra.addWidget(self.scope_ra_s_spin)
    h_ra.addWidget(QLabel("s"))
    v_scope.addLayout(h_ra)
    h_dec = QHBoxLayout()
    h_dec.addWidget(QLabel(getTraduction("Astro.ScopeInputDec", "Dec")))
    self.scope_dec_sign_combo = QComboBox()
    self.scope_dec_sign_combo.addItem("+")
    self.scope_dec_sign_combo.addItem("-")
    self.scope_dec_sign_combo.setFixedWidth(44)
    h_dec.addWidget(self.scope_dec_sign_combo)
    self.scope_dec_d_spin = QSpinBox()
    self.scope_dec_d_spin.setRange(0, 90)
    self.scope_dec_d_spin.setFixedWidth(48)
    h_dec.addWidget(self.scope_dec_d_spin)
    h_dec.addWidget(QLabel("°"))
    self.scope_dec_m_spin = QSpinBox()
    self.scope_dec_m_spin.setRange(0, 59)
    self.scope_dec_m_spin.setFixedWidth(48)
    h_dec.addWidget(self.scope_dec_m_spin)
    h_dec.addWidget(QLabel("'"))
    self.scope_dec_s_spin = QDoubleSpinBox()
    self.scope_dec_s_spin.setRange(0.0, 59.9)
    self.scope_dec_s_spin.setDecimals(1)
    self.scope_dec_s_spin.setSingleStep(0.1)
    self.scope_dec_s_spin.setFixedWidth(58)
    h_dec.addWidget(self.scope_dec_s_spin)
    h_dec.addWidget(QLabel('"'))
    v_scope.addLayout(h_dec)
    self.btn_scope_goto_radec = QPushButton(
        getTraduction("Astro.ScopeGotoRaDec", "Go RA/Dec")
    )
    self.btn_scope_goto_radec.clicked.connect(self.on_scope_goto_radec)
    v_scope.addWidget(self.btn_scope_goto_radec)
    h_shape = QHBoxLayout()
    h_shape.addWidget(QLabel(getTraduction("Astro.ScopeShape", "Format")))
    self.scope_shape_combo = QComboBox()
    self.scope_shape_combo.addItem(
        getTraduction("Astro.ScopeCircle", "Circle"),
        TelescopeScopeController.SHAPE_CIRCLE,
    )
    self.scope_shape_combo.addItem(
        getTraduction("Astro.ScopeRectangle", "Rectangle"),
        TelescopeScopeController.SHAPE_RECT,
    )
    self.scope_shape_combo.currentIndexChanged.connect(
        self.on_scope_shape_changed
    )
    h_shape.addWidget(self.scope_shape_combo)
    v_scope.addLayout(h_shape)
    h_sensor = QHBoxLayout()
    h_sensor.addWidget(QLabel(getTraduction("Astro.ScopeSensor", "Sensor")))
    self.scope_sensor_combo = QComboBox()
    self.scope_sensor_combo.addItem(
        getTraduction("Astro.ScopeSensorTiny", "Sensor 1/2.8"), "tiny"
    )
    self.scope_sensor_combo.addItem(
        getTraduction("Astro.ScopeSensorAPSC", "APS-C"), "aps_c"
    )
    self.scope_sensor_combo.addItem(
        getTraduction("Astro.ScopeSensorFullFrame", "Full Frame"), "full_frame"
    )
    self.scope_sensor_combo.currentIndexChanged.connect(
        self.on_scope_sensor_changed
    )
    h_sensor.addWidget(self.scope_sensor_combo)
    v_scope.addLayout(h_sensor)
    self._sync_scope_instrument_controls()
    h_aspect = QHBoxLayout()
    h_aspect.addWidget(QLabel(getTraduction("Astro.ScopeAspect", "Aspect")))
    self.scope_aspect_combo = QComboBox()
    self.scope_aspect_combo.addItem(
        getTraduction("Astro.ScopeAspectAuto", "Auto (sensor)"), None
    )
    self.scope_aspect_combo.addItem("1:1", 1.0)
    self.scope_aspect_combo.addItem("4:3", 4.0 / 3.0)
    self.scope_aspect_combo.addItem("3:2", 3.0 / 2.0)
    self.scope_aspect_combo.addItem("16:9", 16.0 / 9.0)
    self.scope_aspect_combo.addItem("21:9", 21.0 / 9.0)
    self.scope_aspect_combo.addItem(
        getTraduction("Astro.ScopeAspectCustom", "Custom"), "custom"
    )
    self.scope_aspect_combo.currentIndexChanged.connect(
        self.on_scope_aspect_changed
    )
    h_aspect.addWidget(self.scope_aspect_combo)
    self.scope_aspect_custom_spin = QDoubleSpinBox()
    self.scope_aspect_custom_spin.setRange(0.2, 5.0)
    self.scope_aspect_custom_spin.setDecimals(3)
    self.scope_aspect_custom_spin.setSingleStep(0.05)
    self.scope_aspect_custom_spin.setValue(1.5)
    self.scope_aspect_custom_spin.setFixedWidth(68)
    self.scope_aspect_custom_spin.valueChanged.connect(
        self.on_scope_aspect_custom_changed
    )
    h_aspect.addWidget(self.scope_aspect_custom_spin)
    v_scope.addLayout(h_aspect)
    h_speed = QHBoxLayout()
    h_speed.addWidget(QLabel(getTraduction("Astro.ScopeMoveMode", "Movement")))
    self.scope_speed_combo = QComboBox()
    self.scope_speed_combo.addItem(
        getTraduction("Astro.ScopeSlow", "Slow"),
        TelescopeScopeController.SPEED_SLOW,
    )
    self.scope_speed_combo.addItem(
        getTraduction("Astro.ScopeFast", "Fast"),
        TelescopeScopeController.SPEED_FAST,
    )
    self.scope_speed_combo.currentIndexChanged.connect(
        self.on_scope_speed_changed
    )
    h_speed.addWidget(self.scope_speed_combo)
    v_scope.addLayout(h_speed)
    h_scope_actions = QHBoxLayout()
    self.btn_scope_activate = QPushButton(
        getTraduction("Astro.ScopeActivate", "Activate scope")
    )
    self.btn_scope_activate.clicked.connect(self.activate_scope_mode)
    h_scope_actions.addWidget(self.btn_scope_activate)
    self.btn_scope_exit = QPushButton(getTraduction("Astro.ScopeExit", "Exit"))
    self.btn_scope_exit.clicked.connect(self.exit_scope_mode)
    h_scope_actions.addWidget(self.btn_scope_exit)
    v_scope.addLayout(h_scope_actions)
    self.scope_aspect_custom_spin.setEnabled(False)
    self._sync_scope_aspect_controls(
        self.scope_shape_combo.itemData(self.scope_shape_combo.currentIndex())
    )
    self._sync_scope_coord_inputs_from_canvas()
    self.scope_panel.hide()
    v_ext.addWidget(self.scope_panel)
    # Measurement tools panel
    self.tools_panel = QFrame()
    self.tools_panel.setStyleSheet(
        "border: 1px solid #252c3b; border-radius: 6px; "
        "background: #050811;"
    )
    v_tools = QVBoxLayout(self.tools_panel)
    v_tools.setSpacing(3)
    v_tools.setContentsMargins(4, 4, 4, 4)
    self.lbl_measure_section = QLabel(
        getTraduction("Astro.MeasureToolsSection", "Measurement tools")
    )
    self.lbl_measure_section.setStyleSheet(
        "font-size: 9px; font-weight: bold; color: #d8b26a;"
    )
    v_tools.addWidget(self.lbl_measure_section)
    h_tool_row_1 = QHBoxLayout()
    self.btn_tool_ruler = QPushButton(
        getTraduction("Astro.ToolRuler", "Ruler")
    )
    self.btn_tool_ruler.setCheckable(True)
    self.btn_tool_ruler.clicked.connect(
        lambda: self.select_measurement_tool(TOOL_RULER)
    )
    h_tool_row_1.addWidget(self.btn_tool_ruler)
    self.btn_tool_square = QPushButton(
        getTraduction("Astro.ToolSquare", "Square")
    )
    self.btn_tool_square.setCheckable(True)
    self.btn_tool_square.clicked.connect(
        lambda: self.select_measurement_tool(TOOL_SQUARE)
    )
    h_tool_row_1.addWidget(self.btn_tool_square)
    v_tools.addLayout(h_tool_row_1)
    h_tool_row_2 = QHBoxLayout()
    self.btn_tool_rect = QPushButton(
        getTraduction("Astro.ToolRectangle", "Rectangle")
    )
    self.btn_tool_rect.setCheckable(True)
    self.btn_tool_rect.clicked.connect(
        lambda: self.select_measurement_tool(TOOL_RECTANGLE)
    )
    h_tool_row_2.addWidget(self.btn_tool_rect)
    self.btn_tool_circle = QPushButton(
        getTraduction("Astro.ToolCircle", "Circle")
    )
    self.btn_tool_circle.setCheckable(True)
    self.btn_tool_circle.clicked.connect(
        lambda: self.select_measurement_tool(TOOL_CIRCLE)
    )
    h_tool_row_2.addWidget(self.btn_tool_circle)
    v_tools.addLayout(h_tool_row_2)
    self.btn_tool_clear = QPushButton(
        getTraduction("Astro.ToolClear", "Clear")
    )
    self.btn_tool_clear.clicked.connect(self.clear_measurement_overlays)
    v_tools.addWidget(self.btn_tool_clear)
    sep_tools = QFrame()
    sep_tools.setFrameShape(QFrame.HLine)
    sep_tools.setFrameShadow(QFrame.Sunken)
    sep_tools.setStyleSheet("color: #252c3b;")
    v_tools.addWidget(sep_tools)
    self.lbl_const_section = QLabel(
        getTraduction("Astro.ConstellationToolsSection", "Constellation tools")
    )
    self.lbl_const_section.setStyleSheet(
        "font-size: 9px; font-weight: bold; color: #d8b26a;"
    )
    v_tools.addWidget(self.lbl_const_section)
    h_const_vis = QHBoxLayout()
    self.btn_const_visibility = QPushButton(
        getTraduction("Astro.ConstellationVisibility", "Show constellations")
    )
    self.btn_const_visibility.setCheckable(True)
    self.btn_const_visibility.setChecked(True)
    self.btn_const_visibility.setStyleSheet(
        "QPushButton { color: #f3f5fa; }"
        "QPushButton:checked { color: #02040a; }"
    )
    self.btn_const_visibility.toggled.connect(
        self.toggle_constellation_visibility
    )
    h_const_vis.addWidget(self.btn_const_visibility)
    v_tools.addLayout(h_const_vis)
    h_const_row_1 = QHBoxLayout()
    self.btn_const_draw = QPushButton(
        getTraduction("Astro.ConstellationDrawMode", "Draw mode")
    )
    self.btn_const_draw.setCheckable(True)
    self.btn_const_draw.toggled.connect(self.toggle_constellation_draw_mode)
    h_const_row_1.addWidget(self.btn_const_draw)
    self.btn_const_new = QPushButton(
        getTraduction("Astro.ConstellationNew", "New constellation")
    )
    self.btn_const_new.clicked.connect(self.constellation_primary_action)
    h_const_row_1.addWidget(self.btn_const_new)
    v_tools.addLayout(h_const_row_1)
    h_const_row_2 = QHBoxLayout()
    self.btn_const_eraser = QPushButton(
        getTraduction("Astro.ConstellationDeleteAll", "Delete all")
    )
    self.btn_const_eraser.clicked.connect(self.delete_constellation_action)
    h_const_row_2.addWidget(self.btn_const_eraser)
    self.btn_const_rename = QPushButton(
        getTraduction("Astro.ConstellationRename", "Rename")
    )
    self.btn_const_rename.clicked.connect(self.rename_constellation_group)
    h_const_row_2.addWidget(self.btn_const_rename)
    v_tools.addLayout(h_const_row_2)
    self.lbl_constellation_state = QLabel("")
    self.lbl_constellation_state.setStyleSheet(
        "font-size: 9px; color: #aab1c2;"
    )
    v_tools.addWidget(self.lbl_constellation_state)
    self.tools_panel.show()
    self.sync_scope_ui_state(False)
    self._sync_measure_tool_buttons(TOOL_NONE)
    self._sync_constellation_controls()
    l_sky.addLayout(v_ext)
    earth_page = QWidget()
    earth_page.setObjectName("earthControlsPage")
    v_earth = QVBoxLayout(earth_page)
    v_earth.setContentsMargins(10, 8, 10, 8)
    v_earth.setSpacing(6)
    self.btn_manage_layers = QPushButton("⚙ Gestionar capes")
    self.btn_manage_layers.setToolTip(
        "Configura visibilitat, fonts pròpies, descàrregues i prioritats."
    )
    self.btn_manage_layers.clicked.connect(self.open_data_layers_dialog)
    v_earth.addWidget(self.btn_manage_layers)
    self.chk_enable_horizon = QCheckBox("Horitzó")
    self.chk_enable_horizon.setStyleSheet(
        "font-style: normal; font-weight: normal;"
    )
    self.chk_enable_horizon.setChecked(
        self._load_visibility_state("horitzo", True)
    )
    self.chk_enable_horizon.toggled.connect(self.canvas.update)
    self.chk_enable_horizon.toggled.connect(
        lambda c: self._persist_visibility_state("horitzo", c)
    )
    v_earth.addWidget(self.chk_enable_horizon)
    self.chk_enable_village = QCheckBox("Topografia")
    self.chk_enable_village.setStyleSheet(
        "font-style: normal; font-weight: normal;"
    )
    self.chk_enable_village.setChecked(
        self._load_visibility_state("topografia", True)
    )
    self.chk_enable_village.toggled.connect(self.canvas.update)
    self.chk_enable_village.toggled.connect(self.on_topography_toggled)
    v_earth.addWidget(self.chk_enable_village)
    self.surface_controls_group = QGroupBox("Superfície")
    self.surface_controls_group.setObjectName("surfaceControlsGroup")
    self.surface_controls_group.setStyleSheet(
        "QGroupBox#surfaceControlsGroup {"
        " border: 1px solid #3b4559; border-radius: 6px;"
        " margin-top: 1.05em; background: #0d111c;"
        "}"
        "QGroupBox#surfaceControlsGroup::title {"
        " subcontrol-origin: margin; left: 8px; padding: 0 4px;"
        " color: #d8b26a; background: #0d111c;"
        " font-size: 10px; font-weight: bold;"
        "}"
    )
    v_surface = QVBoxLayout(self.surface_controls_group)
    v_surface.setContentsMargins(7, 5, 7, 5)
    v_surface.setSpacing(3)

    self.chk_surface_layer = QCheckBox("Mostrar")
    self.chk_surface_layer.setChecked(
        self._load_visibility_state("superficie", True)
    )
    self.chk_surface_layer.toggled.connect(self.on_surface_layer_toggled)
    h_surface = QHBoxLayout()
    h_surface.setContentsMargins(0, 0, 0, 0)
    h_surface.setSpacing(6)
    h_surface.addWidget(self.chk_surface_layer)
    h_surface.addStretch(1)
    v_surface.addLayout(h_surface)

    self.surface_mode_selector = QWidget()
    self.surface_mode_selector.setObjectName("surfaceModeSelector")
    h_surface_mode = QHBoxLayout(self.surface_mode_selector)
    h_surface_mode.setContentsMargins(0, 0, 0, 0)
    h_surface_mode.setSpacing(4)
    self.lbl_surface_mode_rgb = QLabel("Ortofoto")
    self.lbl_surface_mode_rgb.setStyleSheet("font-size: 9px;")
    h_surface_mode.addWidget(self.lbl_surface_mode_rgb)
    self.slider_surface_mode = QSlider(Qt.Horizontal)
    self.slider_surface_mode.setObjectName("surfaceModeSwitch")
    self.slider_surface_mode.setRange(0, 1)
    self.slider_surface_mode.setSingleStep(1)
    self.slider_surface_mode.setPageStep(1)
    self.slider_surface_mode.setFixedSize(38, 18)
    self.slider_surface_mode.setFocusPolicy(Qt.StrongFocus)
    self.slider_surface_mode.setAccessibleName(
        "Mode de superfície: ortofoto o categòric"
    )
    self.slider_surface_mode.setAccessibleDescription(
        "Posició esquerra: ortofoto. Posició dreta: cobertura categòrica."
    )
    self.slider_surface_mode.setStyleSheet(
        "QSlider::groove:horizontal {"
        " height: 8px; background: #050811; border: 1px solid #3b4559;"
        " border-radius: 4px; }"
        "QSlider::handle:horizontal {"
        " width: 12px; margin: -3px 0; background: #d8b26a;"
        " border: 1px solid #f1cd88; border-radius: 6px; }"
        "QSlider::handle:horizontal:hover { background: #f1cd88; }"
        "QSlider::handle:horizontal:focus { border: 2px solid #f1cd88; }"
    )
    self.slider_surface_mode.valueChanged.connect(self.on_surface_mode_changed)
    h_surface_mode.addWidget(self.slider_surface_mode)
    self.lbl_surface_mode_categorical = QLabel("Categòric")
    self.lbl_surface_mode_categorical.setStyleSheet("font-size: 9px;")
    h_surface_mode.addWidget(self.lbl_surface_mode_categorical)
    h_surface_mode.addStretch(1)
    v_surface.addWidget(self.surface_mode_selector)

    self.lbl_surface_visual_style = QLabel("Estil visual")
    self.lbl_surface_visual_style.setStyleSheet(
        "font-size: 9px; color: #aab1c2;"
    )
    v_surface.addWidget(self.lbl_surface_visual_style)
    self.surface_visual_style_selector = QWidget()
    self.surface_visual_style_selector.setObjectName(
        "surfaceVisualStyleSelector"
    )
    h_visual_style = QHBoxLayout(self.surface_visual_style_selector)
    h_visual_style.setContentsMargins(0, 0, 0, 0)
    h_visual_style.setSpacing(4)
    self.lbl_surface_style_original = QLabel("Original")
    self.lbl_surface_style_original.setStyleSheet("font-size: 9px;")
    h_visual_style.addWidget(self.lbl_surface_style_original)
    self.slider_surface_visual_style = QSlider(Qt.Horizontal)
    self.slider_surface_visual_style.setObjectName("surfaceVisualStyleSwitch")
    self.slider_surface_visual_style.setRange(0, 1)
    self.slider_surface_visual_style.setSingleStep(1)
    self.slider_surface_visual_style.setPageStep(1)
    self.slider_surface_visual_style.setFixedSize(38, 18)
    self.slider_surface_visual_style.setFocusPolicy(Qt.StrongFocus)
    self.slider_surface_visual_style.setAccessibleName(
        "Estil visual de la superfície: Original o Vibrant"
    )
    self.slider_surface_visual_style.setAccessibleDescription(
        "Posició esquerra: colors originals. Posició dreta: estil Vibrant."
    )
    self.slider_surface_visual_style.setStyleSheet(
        "QSlider::groove:horizontal {"
        " height: 8px; background: #050811; border: 1px solid #3b4559;"
        " border-radius: 4px; }"
        "QSlider::handle:horizontal {"
        " width: 12px; margin: -3px 0; background: #d8b26a;"
        " border: 1px solid #f1cd88; border-radius: 6px; }"
        "QSlider::handle:horizontal:hover { background: #f1cd88; }"
        "QSlider::handle:horizontal:focus {"
        " border: 2px solid #f1cd88; background: #d8b26a; }"
    )
    current_surface_style = (
        str(get_config_value("surface_visual_style", "original") or "original")
        .strip()
        .lower()
    )
    self.slider_surface_visual_style.setValue(
        1 if current_surface_style == "vibrant" else 0
    )
    self.slider_surface_visual_style.valueChanged.connect(
        self.on_surface_visual_style_changed
    )
    h_visual_style.addWidget(self.slider_surface_visual_style)
    self.lbl_surface_style_vibrant = QLabel("Vibrant")
    self.lbl_surface_style_vibrant.setStyleSheet(
        "font-size: 9px; color: #d8b26a; font-weight: bold;"
    )
    h_visual_style.addWidget(self.lbl_surface_style_vibrant)
    h_visual_style.addStretch(1)
    v_surface.addWidget(self.surface_visual_style_selector)

    v_earth.addWidget(self.surface_controls_group)
    self._sync_surface_mode_control()
    # Compatibility alias for integrations that used a surface checkbox name.
    self.chk_surface = self.chk_surface_layer
    v_earth.addWidget(self.chk_light_pollution)
    for checkbox, layer_id in (
        (self.chk_clima, LayerId.SKY_WEATHER),
        (self.chk_light_pollution, LayerId.EARTH_LIGHT_POLLUTION),
        (self.chk_solar_system, LayerId.SKY_SOLAR_SYSTEM),
        (self.chk_planets, LayerId.SKY_SOLAR_SYSTEM),
        (self.chk_sun_moon, LayerId.SKY_SOLAR_SYSTEM),
        (self.chk_enable_sky, LayerId.SKY_STARS),
        (self.chk_enable_milkyway, LayerId.SKY_MILKY_WAY),
        (self.chk_enable_planck_dust, LayerId.SKY_PLANCK_DUST),
        (self.chk_deep_space, LayerId.SKY_NGC),
        (self.chk_enable_village, LayerId.EARTH_TERRAIN),
        (self.chk_surface_layer, LayerId.EARTH_SURFACE),
    ):
        checkbox.clicked.connect(
            lambda checked, target=layer_id: self._guide_missing_layer(
                checked, target
            )
        )
    legacy_terrain_3d = self._load_visibility_state("ombres_terreny", True)
    self.chk_terrain_3d = QCheckBox("Relleu tridimensional")
    self.chk_terrain_3d.setToolTip(
        "Activat: superfície tridimensional. Desactivat: siluetes per distància."
    )
    self.chk_terrain_3d.setStyleSheet(
        "font-style: normal; font-weight: normal;"
    )
    self.chk_terrain_3d.setChecked(
        self._load_visibility_state("relleu_tridimensional", legacy_terrain_3d)
    )
    self.chk_terrain_3d.toggled.connect(self.canvas.update)
    self.chk_terrain_3d.toggled.connect(
        lambda c: self._persist_visibility_state("relleu_tridimensional", c)
    )
    # Compatibility alias for integrations that still reference the old control.
    self.chk_terrain_shading = self.chk_terrain_3d
    v_earth.addWidget(self.chk_terrain_3d)
    self._sync_surface_terrain_3d_control()
    h_lay = QHBoxLayout()
    l_lay = QLabel("Nombre\nde capes")
    l_lay.setStyleSheet(
        "font-style: normal; font-weight: normal; font-size: 9px; line-height: 1;"
    )
    h_lay.addWidget(l_lay)
    from PyQt5.QtWidgets import QComboBox

    self.combo_layers = QComboBox()
    self.combo_layers.addItems(["10", "20", "40", "60", "80"])
    self.combo_layers.setFixedWidth(40)
    self.combo_layers.setStyleSheet("font-style: normal; font-weight: normal;")
    try:
        curr_layers = int(get_config_value("horizon_quality", 80))
    except Exception:
        curr_layers = 80
    self.combo_layers.setCurrentText(str(curr_layers))
    self.combo_layers.currentTextChanged.connect(self.on_layers_changed)
    h_lay.addWidget(self.combo_layers)
    v_earth.addLayout(h_lay)
    h_depth = QHBoxLayout()
    self.lbl_terrain_depth = QLabel()
    self.lbl_terrain_depth.setStyleSheet(
        "font-size: 9px; font-weight: normal;"
    )
    h_depth.addWidget(self.lbl_terrain_depth)
    self.slider_terrain_depth = QSlider(Qt.Horizontal)
    self.slider_terrain_depth.setRange(1, 530)
    self.slider_terrain_depth.setSingleStep(1)
    self.slider_terrain_depth.setPageStep(10)
    from TerraLab.terrain.visibility_range import (
        TerrainRangeSettings,
        resolve_visibility_range,
    )

    range_settings = TerrainRangeSettings.from_mapping(
        get_config_value("terrain_visibility_range", {})
    )
    default_depth = (
        resolve_visibility_range(range_settings, 0.0).resolved_radius_m
        / 1000.0
    )
    initial_depth = int(
        round(
            float(get_config_value("terrain_display_radius_km", default_depth))
        )
    )
    self.slider_terrain_depth.setValue(max(1, min(530, initial_depth)))
    self.slider_terrain_depth.setToolTip(
        getTraduction(
            "Terrain.DepthTooltip", "Profunditat topogràfica visible en km"
        )
    )
    self.slider_terrain_depth.valueChanged.connect(
        self.on_terrain_depth_changed
    )
    h_depth.addWidget(self.slider_terrain_depth, 1)
    v_earth.addLayout(h_depth)
    self._update_terrain_depth_label(self.slider_terrain_depth.value())
    h_rays = QHBoxLayout()
    self.lbl_terrain_ray_precision = QLabel()
    self.lbl_terrain_ray_precision.setStyleSheet(
        "font-size: 9px; font-weight: normal;"
    )
    h_rays.addWidget(self.lbl_terrain_ray_precision)
    self.slider_terrain_ray_precision = QSlider(Qt.Horizontal)
    from TerraLab.terrain.ray_precision import (
        DEFAULT_RAY_STEP_DEG,
        MAX_RAY_STEP_DEG,
        MIN_RAY_STEP_DEG,
        RAY_STEP_SLIDER_SCALE,
        ray_step_to_slider,
    )

    self.slider_terrain_ray_precision.setRange(
        int(MIN_RAY_STEP_DEG * RAY_STEP_SLIDER_SCALE),
        int(MAX_RAY_STEP_DEG * RAY_STEP_SLIDER_SCALE),
    )
    self.slider_terrain_ray_precision.setSingleStep(5)
    self.slider_terrain_ray_precision.setPageStep(50)
    self.slider_terrain_ray_precision.setValue(
        ray_step_to_slider(
            get_config_value("horizon_ray_step_deg", DEFAULT_RAY_STEP_DEG)
        )
    )
    self.slider_terrain_ray_precision.setToolTip(
        getTraduction(
            "Terrain.RayPrecisionTooltip",
            "SeparaciÃ³n angular entre rayos del horizonte "
            "(0,005\N{DEGREE SIGN} a 5\N{DEGREE SIGN})",
        )
    )
    self.slider_terrain_ray_precision.valueChanged.connect(
        self.on_terrain_ray_precision_changed
    )
    h_rays.addWidget(self.slider_terrain_ray_precision, 1)
    v_earth.addLayout(h_rays)
    self._update_terrain_ray_precision_label(
        self.slider_terrain_ray_precision.value()
    )
    v_earth.addStretch()

    def create_drawer_page(object_name):
        page = QWidget()
        page.setObjectName(object_name)
        page.setProperty("drawerPage", True)
        page.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(8, 5, 8, 5)
        page_layout.setSpacing(2)
        return page, page_layout

    def expand_control(control):
        control.setMinimumWidth(0)
        control.setMaximumWidth(16_777_215)
        control.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return control

    def add_section(page_layout, text):
        label = QLabel(text)
        label.setProperty("sectionTitle", True)
        page_layout.addWidget(label)
        return label

    def add_field(page_layout, text, control):
        label = QLabel(text)
        page_layout.addWidget(label)
        page_layout.addWidget(expand_control(control))
        return label

    def add_slider_block(page_layout, title_label, slider):
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        header.addWidget(title_label)
        header.addStretch(1)
        header.addWidget(slider._lbl_curr)
        page_layout.addLayout(header)
        page_layout.addWidget(expand_control(slider))
        limits = QHBoxLayout()
        limits.setContentsMargins(0, 0, 0, 0)
        limits.addWidget(slider._lbl_min)
        limits.addStretch(1)
        limits.addWidget(slider._lbl_max)
        page_layout.addLayout(limits)

    # Ubicació: els controls originals passen a una columna vertical.
    location_drawer_page, location_drawer_layout = create_drawer_page(
        "locationDrawerPage"
    )
    add_field(location_drawer_layout, "Latitud", self.txt_lat)
    add_field(location_drawer_layout, "Longitud", self.txt_lon)
    self.btn_relocate.setText("\u2316  Reubicar")
    location_drawer_layout.addWidget(expand_control(self.btn_relocate))
    add_field(
        location_drawer_layout,
        "Alçada addicional",
        self.spin_extra_height,
    )
    location_drawer_layout.addWidget(self.lbl_altitude_info)
    add_section(location_drawer_layout, "Data")
    location_drawer_layout.addWidget(cal_container)
    location_drawer_layout.addWidget(expand_control(self.btn_realtime))
    location_drawer_layout.addStretch(1)

    # Cel general: una única columna compacta, sense contenidor amb scroll.
    sky_base_page, sky_base_layout = create_drawer_page("skyBaseDrawerPage")
    sky_base_layout.addWidget(expand_control(self.txt_search))
    add_section(sky_base_layout, "Capes del cel")
    self.chk_planets.setText("Planetes")
    self.chk_sun_moon.setText("Sol i Lluna")
    for checkbox in (
        self.chk_clima,
        self.chk_solar_system,
        self.chk_planets,
        self.chk_sun_moon,
        self.chk_enable_milkyway,
        self.chk_enable_planck_dust,
        self.chk_deep_space,
        self.chk_enable_sky,
        self.chk_pure_colors,
    ):
        sky_base_layout.addWidget(checkbox)
    sky_base_layout.addWidget(self.lbl_climate_fallback)
    sky_base_layout.addWidget(self.lbl_stars_fallback)
    sky_base_layout.addWidget(self.lbl_gaia_download_status)
    sky_base_layout.addWidget(self.progress_gaia_download)
    sky_base_layout.addWidget(self.lbl_milkyway_status)
    add_section(sky_base_layout, "Aparença")
    add_slider_block(
        sky_base_layout,
        self.lbl_size_control,
        self.slider_size,
    )
    add_slider_block(
        sky_base_layout,
        self.lbl_spikes_control,
        self.slider_spikes,
    )
    add_section(sky_base_layout, "Contaminació lumínica")
    sky_base_layout.addWidget(expand_control(self.combo_lp_mode))
    add_slider_block(
        sky_base_layout,
        self.lbl_light_text,
        self.slider_light,
    )
    sky_base_layout.addWidget(expand_control(self.chk_trails))
    sky_base_layout.addWidget(self.lbl_trail_time)
    sky_base_layout.addWidget(expand_control(self.btn_quick_welcome))
    sky_base_layout.addWidget(expand_control(self.btn_scope_panel))
    sky_base_layout.addWidget(expand_control(self.btn_tools_panel))
    sky_base_layout.addStretch(1)

    # Scope: subvista que substitueix el cel general quan s'activa.
    legacy_scope_panel = self.scope_panel
    scope_drawer_page, scope_drawer_layout = create_drawer_page(
        "scopeDrawerPage"
    )
    self.btn_scope_back = QPushButton("\u2039  Tornar als controls del cel")
    self.btn_scope_back.setObjectName("scopeBackButton")
    self.btn_scope_back.clicked.connect(
        lambda: self.btn_scope_panel.setChecked(False)
    )
    scope_drawer_layout.addWidget(expand_control(self.btn_scope_back))
    add_section(scope_drawer_layout, "Tub / Telescopi")
    add_field(scope_drawer_layout, "Focal (mm)", self.scope_focal_spin)
    self.scope_instrument_label = add_field(
        scope_drawer_layout,
        "Instrument",
        self.scope_instrument_combo,
    )
    self.scope_aperture_mode_label = add_field(
        scope_drawer_layout,
        "Entrada d’obertura",
        self.scope_aperture_mode_combo,
    )
    self.scope_aperture_label = add_field(
        scope_drawer_layout,
        "Obertura",
        self.scope_aperture_spin,
    )
    self.scope_eyepiece_label = add_field(
        scope_drawer_layout,
        "Ocular (mm)",
        self.scope_eyepiece_spin,
    )
    add_field(scope_drawer_layout, "ISO", self.scope_iso_spin)
    add_field(
        scope_drawer_layout,
        "Exposició (s)",
        self.scope_exposure_spin,
    )
    scope_drawer_layout.addWidget(QLabel("Ascensió recta"))
    ra_row = QHBoxLayout()
    ra_row.setContentsMargins(0, 0, 0, 0)
    for control, suffix in (
        (self.scope_ra_h_spin, "h"),
        (self.scope_ra_m_spin, "m"),
        (self.scope_ra_s_spin, "s"),
    ):
        control.setMinimumWidth(36)
        control.setMaximumWidth(56)
        ra_row.addWidget(control, 1)
        ra_row.addWidget(QLabel(suffix))
    scope_drawer_layout.addLayout(ra_row)
    scope_drawer_layout.addWidget(QLabel("Declinació"))
    dec_row = QHBoxLayout()
    dec_row.setContentsMargins(0, 0, 0, 0)
    self.scope_dec_sign_combo.setMinimumWidth(34)
    self.scope_dec_sign_combo.setMaximumWidth(40)
    dec_row.addWidget(self.scope_dec_sign_combo)
    for control, suffix in (
        (self.scope_dec_d_spin, "\N{DEGREE SIGN}"),
        (self.scope_dec_m_spin, "'"),
        (self.scope_dec_s_spin, '"'),
    ):
        control.setMinimumWidth(34)
        control.setMaximumWidth(50)
        dec_row.addWidget(control, 1)
        dec_row.addWidget(QLabel(suffix))
    scope_drawer_layout.addLayout(dec_row)
    scope_drawer_layout.addWidget(expand_control(self.btn_scope_goto_radec))
    add_field(scope_drawer_layout, "Format", self.scope_shape_combo)
    add_field(scope_drawer_layout, "Sensor", self.scope_sensor_combo)
    scope_drawer_layout.addWidget(QLabel("Aspecte"))
    aspect_row = QHBoxLayout()
    aspect_row.setContentsMargins(0, 0, 0, 0)
    aspect_row.addWidget(expand_control(self.scope_aspect_combo), 1)
    aspect_row.addWidget(self.scope_aspect_custom_spin)
    scope_drawer_layout.addLayout(aspect_row)
    add_field(scope_drawer_layout, "Moviment", self.scope_speed_combo)
    scope_actions = QHBoxLayout()
    scope_actions.setContentsMargins(0, 0, 0, 0)
    scope_actions.addWidget(self.btn_scope_activate)
    scope_actions.addWidget(self.btn_scope_exit)
    scope_drawer_layout.addLayout(scope_actions)
    scope_drawer_layout.addStretch(1)
    self.scope_panel = scope_drawer_page
    legacy_scope_panel.hide()
    self._sync_scope_instrument_controls()

    sky_drawer_page, sky_drawer_layout = create_drawer_page("skyDrawerPage")
    sky_drawer_layout.setContentsMargins(0, 0, 0, 0)
    self.sky_mode_stack = QStackedWidget()
    self.sky_mode_stack.setObjectName("skyModeStack")
    self.sky_mode_stack.setSizePolicy(
        QSizePolicy.Expanding,
        QSizePolicy.Ignored,
    )
    self.sky_mode_stack.addWidget(sky_base_page)
    self.sky_mode_stack.addWidget(self.scope_panel)
    self.sky_mode_stack.setCurrentWidget(sky_base_page)
    sky_drawer_layout.addWidget(self.sky_mode_stack)
    self.sky_base_page = sky_base_page

    # Terra: controls originals, apilats verticalment.
    earth_drawer_page, earth_drawer_layout = create_drawer_page(
        "earthDrawerPage"
    )
    earth_drawer_layout.addWidget(expand_control(self.btn_manage_layers))
    for checkbox in (
        self.chk_enable_horizon,
        self.chk_enable_village,
        self.chk_light_pollution,
        self.chk_terrain_3d,
    ):
        earth_drawer_layout.addWidget(checkbox)
    earth_drawer_layout.addWidget(self.surface_controls_group)
    add_field(
        earth_drawer_layout,
        "Nombre de capes",
        self.combo_layers,
    )
    earth_drawer_layout.addWidget(self.lbl_terrain_depth)
    earth_drawer_layout.addWidget(expand_control(self.slider_terrain_depth))
    earth_drawer_layout.addWidget(self.lbl_terrain_ray_precision)
    earth_drawer_layout.addWidget(
        expand_control(self.slider_terrain_ray_precision)
    )
    earth_drawer_layout.addStretch(1)

    # Eines: cada acció queda en una fila pròpia.
    legacy_tools_panel = self.tools_panel
    tools_drawer_page, tools_drawer_layout = create_drawer_page(
        "toolsDrawerPage"
    )
    tools_drawer_layout.addWidget(self.lbl_measure_section)
    for button in (
        self.btn_tool_ruler,
        self.btn_tool_square,
        self.btn_tool_rect,
        self.btn_tool_circle,
        self.btn_tool_clear,
    ):
        tools_drawer_layout.addWidget(expand_control(button))
    tools_separator = QFrame()
    tools_separator.setFrameShape(QFrame.HLine)
    tools_separator.setStyleSheet("color: #252c3b;")
    tools_drawer_layout.addWidget(tools_separator)
    tools_drawer_layout.addWidget(self.lbl_const_section)
    for button in (
        self.btn_const_visibility,
        self.btn_const_draw,
        self.btn_const_new,
        self.btn_const_eraser,
        self.btn_const_rename,
    ):
        tools_drawer_layout.addWidget(expand_control(button))
    tools_drawer_layout.addWidget(self.lbl_constellation_state)
    tools_drawer_layout.addStretch(1)
    self.tools_panel = tools_drawer_page
    legacy_tools_panel.hide()

    self.location_controls_page = location_drawer_page
    self.sky_controls_page = sky_drawer_page
    self.earth_controls_page = earth_drawer_page
    self.tools_controls_page = tools_drawer_page

    self.control_drawer = QFrame()
    self.control_drawer.setObjectName("controlDrawer")
    self.control_drawer.setStyleSheet(CONTROL_DRAWER_STYLESHEET)
    self.control_drawer.setFixedWidth(258)
    drawer_layout = QVBoxLayout(self.control_drawer)
    drawer_layout.setContentsMargins(0, 0, 0, 0)
    drawer_layout.setSpacing(0)

    drawer_header = QFrame()
    drawer_header.setObjectName("drawerHeader")
    drawer_header.setFixedHeight(36)
    drawer_header_layout = QHBoxLayout(drawer_header)
    drawer_header_layout.setContentsMargins(10, 4, 6, 4)
    self.lbl_drawer_title = QLabel("Cel")
    self.lbl_drawer_title.setObjectName("drawerTitle")
    drawer_header_layout.addWidget(self.lbl_drawer_title)
    drawer_header_layout.addStretch(1)
    self.btn_drawer_close = QPushButton("\u00d7")
    self.btn_drawer_close.setObjectName("drawerCloseButton")
    self.btn_drawer_close.setAccessibleName("Tancar el calaix")
    self.btn_drawer_close.setFixedSize(26, 24)
    self.btn_drawer_close.clicked.connect(self.close_control_drawer)
    drawer_header_layout.addWidget(self.btn_drawer_close)
    drawer_layout.addWidget(drawer_header)

    self.drawer_stack = QStackedWidget()
    self.drawer_stack.setObjectName("controlDrawerStack")
    self.drawer_stack.setSizePolicy(
        QSizePolicy.Expanding,
        QSizePolicy.Ignored,
    )
    self.drawer_pages = {
        "location": self.location_controls_page,
        "sky": self.sky_controls_page,
        "earth": self.earth_controls_page,
        "tools": self.tools_controls_page,
    }
    for drawer_page in self.drawer_pages.values():
        self.drawer_stack.addWidget(drawer_page)
    drawer_layout.addWidget(self.drawer_stack, 1)

    self.drawer_rail = QFrame()
    self.drawer_rail.setObjectName("drawerRail")
    self.drawer_rail.setStyleSheet(CONTROL_DRAWER_STYLESHEET)
    self.drawer_rail.setFixedWidth(44)
    rail_layout = QVBoxLayout(self.drawer_rail)
    rail_layout.setContentsMargins(4, 6, 4, 6)
    rail_layout.setSpacing(6)

    self.drawer_buttons = {}
    drawer_specs = (
        ("location", "Ubicació"),
        ("sky", "Cel"),
        ("earth", "Terra"),
        ("tools", "Eines"),
    )

    def render_drawer_icon(kind, color):
        pixmap = QPixmap(22, 22)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(color), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        if kind == "location":
            pin = QPainterPath()
            pin.moveTo(11, 20)
            pin.cubicTo(9, 16, 4, 12, 4, 7.5)
            pin.cubicTo(4, 3.8, 7.1, 1, 11, 1)
            pin.cubicTo(14.9, 1, 18, 3.8, 18, 7.5)
            pin.cubicTo(18, 12, 13, 16, 11, 20)
            painter.drawPath(pin)
            painter.drawEllipse(QRectF(8.5, 5, 5, 5))
        elif kind == "sky":
            star = QPainterPath()
            star.moveTo(11, 1)
            star.lineTo(12.8, 8.8)
            star.lineTo(21, 11)
            star.lineTo(12.8, 13.2)
            star.lineTo(11, 21)
            star.lineTo(9.2, 13.2)
            star.lineTo(1, 11)
            star.lineTo(9.2, 8.8)
            star.closeSubpath()
            painter.drawPath(star)
            painter.drawEllipse(QRectF(17, 2, 2, 2))
            painter.drawEllipse(QRectF(3, 16, 1.5, 1.5))
        elif kind == "earth":
            mountains = QPainterPath()
            mountains.moveTo(1, 19)
            mountains.lineTo(7.5, 8)
            mountains.lineTo(10.3, 12)
            mountains.lineTo(14.2, 4)
            mountains.lineTo(21, 19)
            mountains.closeSubpath()
            painter.drawPath(mountains)
            painter.drawLine(12, 9, 14, 4)
            painter.drawLine(14, 4, 17, 10)
        else:
            painter.save()
            painter.translate(11, 11)
            painter.rotate(-38)
            painter.translate(-11, -11)
            painter.drawRoundedRect(QRectF(3, 8, 16, 6), 1, 1)
            for x_pos in (6, 9, 12, 15):
                painter.drawLine(x_pos, 8, x_pos, 11)
            painter.restore()

        painter.end()
        return pixmap

    def create_drawer_icon(kind):
        icon = QIcon()
        normal = render_drawer_icon(kind, "#aab1c2")
        active = render_drawer_icon(kind, "#f1cd88")
        icon.addPixmap(normal, QIcon.Normal, QIcon.Off)
        icon.addPixmap(active, QIcon.Active, QIcon.Off)
        icon.addPixmap(active, QIcon.Normal, QIcon.On)
        icon.addPixmap(active, QIcon.Active, QIcon.On)
        return icon

    for key, label in drawer_specs:
        button = QPushButton()
        button.setIcon(create_drawer_icon(key))
        button.setIconSize(QSize(22, 22))
        button.setCheckable(True)
        button.setAccessibleName(label)
        button.setToolTip(label)
        button.clicked.connect(
            lambda _checked=False, drawer_key=key: self.set_control_drawer(
                drawer_key,
                toggle=True,
            )
        )
        rail_layout.addWidget(button, 0, Qt.AlignHCenter)
        self.drawer_buttons[key] = button
    rail_layout.addStretch(1)
    self.panels_widget = self.control_drawer
    # â”€â”€ KEEP EXISTING VARIABLES FOR COMPATIBILITY (HIDDEN) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    self.btn_view = QPushButton()
    self.btn_view.hide()
    self.btn_terrain = QPushButton()
    self.btn_terrain.hide()
    self.chk_illusion = QCheckBox()
    self.chk_illusion.hide()
    self.slider_href = QSlider()
    self.slider_href.hide()
    self.slider_flat = QSlider()
    self.slider_flat.hide()
    self.chk_trained = QCheckBox()
    self.chk_trained.hide()
    self.chk_lock = QCheckBox()
    self.chk_lock.hide()
    # Add to main layout
    self.frame_controls = QFrame()
    self.frame_controls.setObjectName("controlFrame")
    self.frame_controls.setLayout(frame_layout)
    self.frame_controls.setSizePolicy(
        QSizePolicy.Expanding,
        QSizePolicy.Fixed,
    )
    layout.addWidget(self.frame_controls)

    # Viewport principal: el calaix i la barra d'icones comparteixen tota
    # l'alçada disponible amb el canvas. En ocultar el calaix, el layout
    # retorna automàticament els seus 258 px al viewport 3D.
    self.viewport_shell = QFrame()
    self.viewport_shell.setObjectName("viewportShell")
    self.viewport_shell.setSizePolicy(
        QSizePolicy.Expanding,
        QSizePolicy.Expanding,
    )
    viewport_layout = QHBoxLayout(self.viewport_shell)
    viewport_layout.setContentsMargins(0, 0, 0, 0)
    viewport_layout.setSpacing(0)
    layout.removeWidget(self.canvas)
    self.canvas.setMinimumWidth(560)
    viewport_layout.addWidget(self.canvas, 1)
    viewport_layout.addWidget(self.control_drawer)
    viewport_layout.addWidget(self.drawer_rail)
    layout.insertWidget(0, self.viewport_shell, 1)

    # Barra d'accés ràpid: botons proxy sobre els controls originals.
    self.quick_toolbar = QFrame()
    self.quick_toolbar.setObjectName("quickToolbar")
    self.quick_toolbar.setStyleSheet(QUICK_TOOLBAR_STYLESHEET)
    self.quick_toolbar.setFixedHeight(36)
    quick_layout = QHBoxLayout(self.quick_toolbar)
    quick_layout.setContentsMargins(10, 3, 10, 3)
    quick_layout.setSpacing(4)
    toolbar_brand = QLabel("TERRALAB")
    toolbar_brand.setObjectName("toolbarBrand")
    quick_layout.addWidget(toolbar_brand)
    quick_layout.addSpacing(8)

    def add_quick_button(text, object_name):
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setAccessibleName(text)
        button.setToolTip(text)
        button.setCursor(Qt.PointingHandCursor)
        quick_layout.addWidget(button)
        return button

    self.btn_toolbar_realtime = add_quick_button(
        "Temps real",
        "toolbarRealtimeButton",
    )
    self.btn_toolbar_realtime.setCheckable(True)
    self.btn_toolbar_realtime.setChecked(self.btn_realtime.isChecked())

    def set_realtime_from_toolbar(checked):
        if self.btn_realtime.isChecked() != bool(checked):
            self.btn_realtime.setChecked(bool(checked))

    self.btn_toolbar_realtime.toggled.connect(set_realtime_from_toolbar)
    self.btn_realtime.toggled.connect(self.btn_toolbar_realtime.setChecked)

    self.btn_toolbar_search = add_quick_button(
        "Cercar objecte",
        "toolbarSearchButton",
    )

    def focus_search():
        self.btn_scope_panel.setChecked(False)
        self.set_control_drawer("sky")
        self.txt_search.setFocus(Qt.ShortcutFocusReason)
        self.txt_search.selectAll()

    self.btn_toolbar_search.clicked.connect(focus_search)
    self.btn_toolbar_layers = add_quick_button(
        "Gestionar capes",
        "toolbarLayersButton",
    )
    self.btn_toolbar_layers.clicked.connect(self.btn_manage_layers.click)
    self.btn_toolbar_scope = add_quick_button(
        "Tub / Telescopi",
        "toolbarScopeButton",
    )

    def open_scope_controls():
        self.set_control_drawer("sky")
        self.btn_scope_panel.setChecked(True)

    self.btn_toolbar_scope.clicked.connect(open_scope_controls)
    self.btn_toolbar_tools = add_quick_button(
        "Eines",
        "toolbarToolsButton",
    )
    self.btn_toolbar_tools.clicked.connect(
        lambda: self.set_control_drawer("tools")
    )
    quick_layout.addStretch(1)
    layout.insertWidget(0, self.quick_toolbar)
    self.set_control_drawer("sky")
    self._position_loading_label()
    self._position_gaia_extension_status_label()
    # Compatibilitat amb el commutador antic; la barra lateral ja conté els
    # controls de tancament i aquest botó flotant no s'ha de mostrar.
    self.btn_collapse = QPushButton("", self)
    self.btn_collapse.setFixedSize(30, 24)
    self.btn_collapse.clicked.connect(self.toggle_controls)
    self.btn_collapse.hide()
    # Apply Themes
    self.update_custom_theme()
    self._deferred_controls_ready = True
    if getattr(self, "search_index", None):
        self.build_search_index()
    self._refresh_climate_status_indicator()
    self._refresh_stars_status_indicator()
    QTimer.singleShot(0, self._activate_checked_surface_layer_startup)
    QTimer.singleShot(0, self._validate_checked_assets_startup)
    QTimer.singleShot(0, self._update_button_pos)
