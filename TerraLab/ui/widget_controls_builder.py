"""Deferred controls UI builder extracted from sky_widget_impl."""

from __future__ import annotations


def build_deferred_controls_ui(widget):
    # Resolve all legacy symbols from sky_widget_impl without duplicating imports.
    from TerraLab.ui import sky_widget_impl as _impl

    globals().update(_impl.__dict__)
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
    if not hasattr(self, "lbl_loading"):
        self.lbl_loading = QLabel(
            getTraduction(
                "Astro.LoadingTopography", "? Carregant topografia..."
            ),
            self,
        )
        self.lbl_loading.setStyleSheet(
            "color: yellow; font-weight: bold; background-color: rgba(0,0,0,100); padding: 5px; border-radius: 4px;"
        )
        self.lbl_loading.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_loading.setWordWrap(False)
        self.lbl_loading.hide()
        self.lbl_loading.move(10, 50)
        self.lbl_loading.resize(420, 32)
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
        "border: 1px solid #555; background: #e0e0e0; border-radius: 4px;"
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
        "color: #000; font-style: normal; font-size: 11px; font-weight: bold; border: none; background: transparent;"
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
        "border-radius: 4px; border: 1px solid #777; font-style: normal; color: #000; background: #ddd; padding: 3px;"
    )
    l_date.addWidget(self.btn_realtime)
    l_date.addStretch(1)
    l_loc.addLayout(l_date)
    # Proportional distribution by feature density:
    # Location / Sky / Ground = 1 / 4 / 1
    panels_layout.addWidget(gb_loc, 1)
    # 2. VISIÃ“ DEL CEL ====================================================
    gb_sky = QGroupBox("Visió del cel")
    gb_sky.setStyleSheet(gb_style)
    l_sky = QHBoxLayout(gb_sky)
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
    self.chk_deep_space = QCheckBox("l'espai profund")
    self.chk_deep_space.setEnabled(True)
    self.chk_deep_space.setChecked(
        self._load_visibility_state("espai_profund", False)
    )
    self.chk_deep_space.toggled.connect(self.on_deep_space_toggled)
    self.lbl_milkyway_status = QLabel("MW: --")
    self.lbl_milkyway_status.setStyleSheet("font-size: 9px; color: #333;")
    self.lbl_milkyway_status.setVisible(True)
    for c in (
        self.chk_clima,
        self.chk_light_pollution,
        self.chk_planets,
        self.chk_sun_moon,
        self.chk_enable_milkyway,
        self.chk_enable_planck_dust,
        self.chk_deep_space,
    ):
        c.setStyleSheet(
            c.styleSheet()
            + "; font-style: normal; font-weight: normal; color: #666;"
            if not c.isEnabled()
            else "; font-style: normal; font-weight: normal; color: #000;"
        )
        v_chk.addWidget(c)
    self.chk_enable_sky.setStyleSheet(
        self.chk_enable_sky.styleSheet()
        + (
            "; font-style: normal; font-weight: normal; color: #666;"
            if not self.chk_enable_sky.isEnabled()
            else "; font-style: normal; font-weight: normal; color: #000;"
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
        "  border: 1px solid #7f9db9;"
        "  border-radius: 3px;"
        "  background: #f3f7fc;"
        "}"
        "QProgressBar::chunk {"
        "  background: #4a90e2;"
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
        l = QLabel(label)
        l.setStyleSheet(
            "font-style: normal; font-weight: normal; min-width: 65px;"
        )
        h.addWidget(l)
        l_min = QLabel(str(r[0]))
        l_min.setStyleSheet("font-size: 8px; color: #000;")
        h.addWidget(l_min)
        s = QSlider(Qt.Horizontal)
        s.setRange(*r)
        s.setValue(val)
        s.setMinimumWidth(80)
        h.addWidget(s)
        l_max = QLabel(str(r[1]))
        l_max.setStyleSheet("font-size: 8px; color: #000;")
        h.addWidget(l_max)
        def format_value(value):
            if value_formatter is None:
                return str(value)
            return str(value_formatter(value))

        l_curr = QLabel(f"[{format_value(val)}]")
        l_curr.setStyleSheet("font-size: 9px; color: #000; min-width: 30px;")
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
        return s, l

    self.slider_size, _ = make_sld(
        v_sld,
        "Mida",
        (5, 40),
        int(self.star_scale * 10),
        self.update_star_scale,
    )
    self.slider_spikes, _ = make_sld(
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
        "font-size: 10px; font-weight: normal; font-style: normal; border-radius: 8px; border: 1px solid #888; padding: 2px;"
    )
    self.chk_trails.toggled.connect(self.on_trails_toggled)
    self._update_circumpolar_button_text()
    v_ext.addWidget(self.chk_trails)
    self.lbl_trail_time = QLabel("")
    self.lbl_trail_time.setStyleSheet(
        "font-weight: normal; color: #000; font-size: 10px;"
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
    self.btn_quick_welcome = QPushButton("Benvinguda / Guia rapida")
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
        "border: 1px solid #999; border-radius: 4px;"
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
        "border: 1px solid #999; border-radius: 4px;"
    )
    v_tools = QVBoxLayout(self.tools_panel)
    v_tools.setSpacing(3)
    v_tools.setContentsMargins(4, 4, 4, 4)
    self.lbl_measure_section = QLabel(
        getTraduction("Astro.MeasureToolsSection", "Measurement tools")
    )
    self.lbl_measure_section.setStyleSheet(
        "font-size: 9px; font-weight: bold; color: #4a3a20;"
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
    sep_tools.setStyleSheet("color: rgba(106, 82, 48, 120);")
    v_tools.addWidget(sep_tools)
    self.lbl_const_section = QLabel(
        getTraduction("Astro.ConstellationToolsSection", "Constellation tools")
    )
    self.lbl_const_section.setStyleSheet(
        "font-size: 9px; font-weight: bold; color: #4a3a20;"
    )
    v_tools.addWidget(self.lbl_const_section)
    h_const_vis = QHBoxLayout()
    self.btn_const_visibility = QPushButton(
        getTraduction("Astro.ConstellationVisibility", "Show constellations")
    )
    self.btn_const_visibility.setCheckable(True)
    self.btn_const_visibility.setChecked(True)
    self.btn_const_visibility.setStyleSheet(
        "QPushButton { color: #2f2515; }"
        "QPushButton:checked { color: #2f2515; }"
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
        "font-size: 9px; color: #6a5738;"
    )
    v_tools.addWidget(self.lbl_constellation_state)
    self.tools_panel.hide()
    v_ext.addWidget(self.tools_panel)
    self.sync_scope_ui_state(False)
    self._sync_measure_tool_buttons(TOOL_NONE)
    self._sync_constellation_controls()
    l_sky.addLayout(v_ext)
    panels_layout.addWidget(gb_sky, 4)
    # 3. VISIÃ“ DEL TERRA ================================================
    gb_earth = QGroupBox("Visió del terra")
    gb_earth.setStyleSheet(gb_style)
    v_earth = QVBoxLayout(gb_earth)
    v_earth.setSpacing(6)
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
    self.chk_terrain_shading = QCheckBox("Relleu suau")
    self.chk_terrain_shading.setStyleSheet(
        "font-style: normal; font-weight: normal;"
    )
    self.chk_terrain_shading.setChecked(
        self._load_visibility_state("ombres_terreny", True)
    )
    self.chk_terrain_shading.toggled.connect(self.canvas.update)
    self.chk_terrain_shading.toggled.connect(
        lambda c: self._persist_visibility_state("ombres_terreny", c)
    )
    v_earth.addWidget(self.chk_terrain_shading)
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
    self._deferred_controls_ready = True
    if getattr(self, "search_index", None):
        self.build_search_index()
    self._refresh_climate_status_indicator()
    self._refresh_stars_status_indicator()
    QTimer.singleShot(0, self._validate_checked_assets_startup)
    QTimer.singleShot(0, self._update_button_pos)
