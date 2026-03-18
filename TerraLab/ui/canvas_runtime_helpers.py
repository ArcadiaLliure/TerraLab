"""AstroCanvas runtime helpers extracted from sky_widget_impl."""

from __future__ import annotations

def canvas_update_skyfield_cache(canvas, ut_hour, day_of_year):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = canvas
    # Throttle updates (cache for 1.5 seconds approx = 0.0004 hours)
    # Also include day/lat/lon/zoom in validity check basically handled by loose time check?
    # No, zoom affects visual radius, so we must separate Astrometric Data vs Screen Data.
    # Here we cache ASTROMETRIC data (Alt, Az, Mag, Dist, Phase).
    # Screen projection happens every frame.
    cache_valid = False
    last_t = self._sf_cache.get('time', -1.0)
    # Validate cache key (Day + Hour)
    # Assuming Lat/Lon doesn't change rapidly.
    if abs(ut_hour - last_t) < 0.0004 and self._sf_cache['data'] is not None:
        cache_valid = True
    if cache_valid: return
    # Regenerate Cache
    if not SKYFIELD_AVAILABLE or not hasattr(self.parent_widget, 'eph'):
        self._sf_cache['data'] = None
        return
    try:
        ts = self.parent_widget.ts
        eph = self.parent_widget.eph
        observer = wgs84.latlon(self.parent_widget.latitude, self.parent_widget.longitude)
        now = datetime.now()
        y = getattr(self.parent_widget, 'manual_year', now.year)
        base_date = datetime(y, 1, 1) + timedelta(days=day_of_year)
        target_dt = base_date + timedelta(hours=ut_hour)
        t = ts.from_datetime(target_dt.replace(tzinfo=timezone.utc))
        earth = eph['earth']
        sun = eph['sun']
        moon = eph['moon']
        obs_loc = earth + observer
        # Sun
        ast_sun = obs_loc.at(t).observe(sun)
        alt_s, az_s, _ = ast_sun.apparent().altaz()
        d_sun_km = ast_sun.distance().km
        # Moon
        ast_moon = obs_loc.at(t).observe(moon)
        alt_m_real, az_m_real, _ = ast_moon.apparent().altaz()
        d_moon_km = ast_moon.distance().km
        sep_real = ast_sun.separation_from(ast_moon).degrees
        # Phase / Illumination
        s_earth = earth.at(t).observe(sun)
        m_earth = earth.at(t).observe(moon)
        elongation = s_earth.separation_from(m_earth).degrees
        illumination = (1 - math.cos(math.radians(elongation))) / 2
        # Planets
        planets_data = []
        planet_defs = {
            'mercury': ('Mercury', QColor(169, 169, 169), 4),
            'venus': ('Venus', QColor(255, 220, 150), 7),
            'mars': ('Mars', QColor(255, 100, 80), 5),
            'jupiter barycenter': ('Jupiter', QColor(220, 180, 140), 12),
            'saturn barycenter': ('Saturn', QColor(240, 210, 150), 10),
            'uranus barycenter': ('Uranus', QColor(173, 216, 230), 6),
            'neptune barycenter': ('Neptune', QColor(100, 100, 255), 6),
            'pluto barycenter': ('Pluto', QColor(200, 180, 160), 3),
        }
        for key, (name, col, sz) in planet_defs.items():
            try:
                p = eph[key]
                ast = obs_loc.at(t).observe(p)
                p_alt, p_az, p_dist = ast.apparent().altaz()
                # No optimization skip: allow rendering planets even when viewing below horizon
                try:
                    mag = self.calculate_planet_magnitude(name, p_dist.au, 0.0)
                except:
                    mag = -2.0 # Fallback
                planets_data.append({
                    'key': key, 'name': name, 'col': col, 'sz': sz,
                    'alt': p_alt.degrees, 'az': p_az.degrees, 'dist_au': p_dist.au,
                    'mag': mag
                })
            except: pass
        # Eclipse Factor
        sun_rad_deg = math.degrees(math.atan(696340.0 / d_sun_km))
        moon_rad_deg = math.degrees(math.atan(1737.4 / d_moon_km))
        # Simple Separation Factor for dimming
        eclipse_factor = 1.0
        if sep_real < (sun_rad_deg + moon_rad_deg):
             # Simple linear overlap approximation
             dist_deg = sep_real
             max_overlap = sun_rad_deg + moon_rad_deg
             if dist_deg < max_overlap:
                 penetration = (max_overlap - dist_deg) / (sun_rad_deg * 2)
                 eclipse_factor = max(0.05, 1.0 - penetration)
        cache_data = {
            'sun': {'alt': alt_s.degrees, 'az': az_s.degrees, 'dist_km': d_sun_km, 'rad_deg': sun_rad_deg},
            'moon': {
                'alt': alt_m_real.degrees, 'az': az_m_real.degrees,
                'dist_km': d_moon_km, 'rad_deg': moon_rad_deg,
                'sep_real': sep_real, 'illumination': illumination, 'elongation': elongation
            },
            'planets': planets_data,
            'eclipse_factor': eclipse_factor
        }
        self._sf_cache = {'time': ut_hour, 'data': cache_data}
    except Exception as e:
        # print(f"Cache Update Error: {e}")
        self._sf_cache['data'] = None

def canvas_scope_hud_star_count(canvas):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = canvas
    """
    Count stars visible inside the scope aperture only.
    This is used for HUD display while scope mode is active.
    """
    if not self.scope_mode_enabled() or not hasattr(self, "scope_controller"):
        return self._visible_star_count_raw()
    if self._scope_motion_active():
        return self._visible_star_count_raw()
    ctrl = self.scope_controller
    if (not getattr(ctrl, "enabled", False)) or getattr(ctrl, "center", None) is None or getattr(ctrl, "awaiting_center_click", False):
        return self._visible_star_count_raw()
    try:
        boundary_sky = ctrl._boundary_points()
        boundary_screen = ctrl._project_valid(boundary_sky, self.project_universal_stereo)
    except Exception:
        return self._visible_star_count_raw()
    if not boundary_screen or len(boundary_screen) < 8:
        return self._visible_star_count_raw()
    hole = QPainterPath()
    hole.moveTo(boundary_screen[0])
    for p in boundary_screen[1:]:
        hole.lineTo(p)
    hole.closeSubpath()
    rect = hole.boundingRect()
    count = 0
    # Fast path: numpy screen buffers.
    try:
        if np is not None and hasattr(self, "visible_stars_sx") and hasattr(self, "visible_stars_sy"):
            sx_arr = np.asarray(self.visible_stars_sx)
            sy_arr = np.asarray(self.visible_stars_sy)
            if sx_arr.size > 0 and sy_arr.size > 0:
                mask = (
                    (sx_arr >= rect.left())
                    & (sx_arr <= rect.right())
                    & (sy_arr >= rect.top())
                    & (sy_arr <= rect.bottom())
                )
                idx = np.where(mask)[0]
                for i in idx:
                    if hole.contains(QPointF(float(sx_arr[i]), float(sy_arr[i]))):
                        count += 1
                return int(count)
    except Exception:
        pass
    # Worker fallback: list of tuples (sx, sy, star_obj)
    vis = getattr(self, "visible_stars", None)
    if isinstance(vis, list) and vis and isinstance(vis[0], tuple):
        for item in vis:
            if len(item) < 2:
                continue
            try:
                sx_i = float(item[0])
                sy_i = float(item[1])
            except Exception:
                continue
            if sx_i < rect.left() or sx_i > rect.right() or sy_i < rect.top() or sy_i > rect.bottom():
                continue
            if hole.contains(QPointF(sx_i, sy_i)):
                count += 1
        return int(count)
    return self._visible_star_count_raw()

def canvas_set_selected_target(canvas, target):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = canvas
    if target is None:
        self.selected_target = None
        self.lbl_info.hide()
        self.scope_reticle_lock_to_target = False
        self._update_selection_pulse_timer()
        return
    if isinstance(target, dict) and target.get("kind") == "star":
        star_obj = target.get("star")
        if self._extract_star_coords(star_obj) is None:
            return
        self.selected_target = {"kind": "star", "star": star_obj}
        info = (f"ID: {star_obj.get('id', 'N/A')}\n"
                f"Mag: {star_obj.get('mag', 0.0):.2f}\n"
                f"RA: {star_obj.get('ra', 0.0):.2f}\n"
                f"Dec: {star_obj.get('dec', 0.0):.2f}\n"
                f"C: {star_obj.get('bp_rp', 0):.2f}")
    elif isinstance(target, dict) and target.get("kind") == "sky":
        kind = str(target.get("type", "")).lower()
        if kind not in ("sun", "moon", "planet"):
            return
        self.selected_target = {
            "kind": "sky",
            "type": kind,
            "key": str(target.get("key", kind)).lower(),
            "name": str(target.get("name", kind.title())),
            "alt": float(target.get("alt", 0.0)),
            "az": float(target.get("az", 0.0)) % 360.0,
        }
        if kind == "planet":
            name_txt = self.selected_target["name"]
        elif kind == "sun":
            name_txt = getTraduction("Astro.SunName", "Sun")
        else:
            name_txt = getTraduction("Astro.MoonName", "Moon")
        info = (f"{name_txt}\n"
                f"Alt: {self.selected_target['alt']:.2f}\n"
                f"Az: {self.selected_target['az']:.2f}")
    elif isinstance(target, dict) and target.get("kind") == "ngc":
        obj = target.get("obj")
        if obj is None:
            return
        self.selected_target = {
            "kind": "ngc",
            "obj": obj,
            "info": target.get("info"),
        }
        display_name = getattr(obj, "common_name", None) or getattr(obj, "name", "NGC")
        mag_text = getattr(obj, "effective_mag", None)
        try:
            mag_str = f"{float(mag_text):.2f}"
        except Exception:
            mag_str = "N/A"
        info = (
            f"{display_name}\n"
            f"RA: {float(getattr(obj, 'ra_deg', 0.0)):.2f}\n"
            f"Dec: {float(getattr(obj, 'dec_deg', 0.0)):.2f}\n"
            f"Mag: {mag_str}"
        )
    else:
        return
    self.lbl_info.setText(info)
    self.lbl_info.adjustSize()
    self.lbl_info.move(self.width() - self.lbl_info.width() - 20, 20)
    self.lbl_info.show()
    self.lbl_info.raise_()
    # New target selection restores camera lock unless user manually overrides with Ctrl+drag.
    self.scope_camera_lock_to_target = True
    self.scope_reticle_lock_to_target = True
    self._update_selection_pulse_timer()

def canvas_scope_hud_extra_lines(canvas, ut_hour: float, day_of_year_utc: int):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = canvas
    if not self.scope_mode_enabled():
        return []
    center = getattr(self.scope_controller, "center", None)
    if center is None:
        return []
    ra_dec = self._altaz_to_ra_dec(center[0], center[1], ut_hour, day_of_year_utc)
    if ra_dec is None:
        return []
    ra_txt = self._format_ra_hms(ra_dec[0])
    dec_txt = self._format_dec_deg(ra_dec[1])
    line = getTraduction("Scope.HudCoords", "RA {ra} | Dec {dec}").format(ra=ra_txt, dec=dec_txt)
    lines = [line]
    vm_state = getattr(self.parent_widget, "visual_magnitude_result", None)
    if vm_state is not None:
        scope_mlim = float(vm_state.scope_limit_mag)
        catalog_cap = float(getattr(self.parent_widget, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG))
        render_mlim = min(scope_mlim, catalog_cap)
        lines.append(
            getTraduction(
                "Scope.HudMagModel",
                "mLim {mlim:.2f} (render {rmlim:.2f}) | M {magx:.1f}x",
            ).format(
                mlim=scope_mlim,
                rmlim=render_mlim,
                magx=float(vm_state.magnification),
            )
        )
    metrics = getattr(self.parent_widget, "scope_atmo_metrics", {}).get("hud_metrics", {})
    if isinstance(metrics, dict):
        exit_p = metrics.get("exit_pupil_mm")
        x_air = metrics.get("airmass_x")
        k_ext = metrics.get("extinction_k")
        loss_mag = metrics.get("loss_mag")
        trans = metrics.get("transmission")
        if exit_p is not None:
            lines.append(
                getTraduction("Scope.HudExitPupil", "Exit pupil: {v:.2f} mm").format(v=float(exit_p))
            )
        if x_air is not None:
            lines.append(
                getTraduction("Scope.HudAirmass", "Airmass X: {v:.3f}").format(v=float(x_air))
            )
        if k_ext is not None:
            lines.append(
                getTraduction("Scope.HudExtinctionK", "Extinction k: {v:.3f}").format(v=float(k_ext))
            )
        if loss_mag is not None:
            lines.append(
                getTraduction("Scope.HudLossMag", "Loss: {v:.3f} mag").format(v=float(loss_mag))
            )
        if trans is not None:
            lines.append(
                getTraduction("Scope.HudTransmission", "Transmission: {v:.3f}").format(v=float(trans))
            )
    scope_data_state = str(getattr(self.parent_widget, "_scope_data_state", "ready_deep") or "ready_deep")
    if scope_data_state == "loading_deep":
        lines.append(getTraduction("Scope.CapturingPhoto", "Tomando foto..."))
    elif scope_data_state == "error_deep":
        lines.append(
            f"{getTraduction('Scope.CapturingPhoto', 'Tomando foto...')} "
            f"({getTraduction('Scope.DeepError', 'Error de carga')})"
        )
    return lines

def canvas_log_positions(canvas):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = canvas
    try:
        import os
        log_dir = r"E:\Desarrollo\logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Re-calc state
        local_hour = self.parent_widget.get_current_hour()
        # TZ Logic (Duplicated from paintEvent for robust standalonecalc)
        try:
            sim_y = self.parent_widget.manual_year
            sim_d = self.parent_widget.manual_day
            sim_dt_start = datetime(sim_y, 1, 1) + timedelta(days=sim_d)
            h_int = int(local_hour)
            m_int = int((local_hour - h_int)*60)
            sim_dt_naive = sim_dt_start.replace(hour=h_int % 24, minute=m_int % 60)
            sim_dt_local = sim_dt_naive.astimezone()
            tz_offset = sim_dt_local.utcoffset().total_seconds() / 3600.0
        except:
            tz_offset = datetime.now().astimezone().utcoffset().total_seconds() / 3600.0
        ut_hour = (local_hour - tz_offset) % 24.0
        # Astro Calc
        dt_utc = self.get_datetime_utc(ut_hour)
        jd_utc = self.julian_day(dt_utc)
        d = jd_utc - 2451545.0
        sun_ra, sun_dec = self.get_sun_ra_dec(d)
        moon_ra, moon_dec, m_dist = self.get_moon_ra_dec(d)
        lat = self.parent_widget.latitude
        lon = self.parent_widget.longitude
        # Topo
        s_ra_topo, s_dec_topo, lst = self.get_topocentric_position(sun_ra, sun_dec, 149597870.7, lat, lon, jd_utc)
        m_ra_topo, m_dec_topo, _ = self.get_topocentric_position(moon_ra, moon_dec, m_dist, lat, lon, jd_utc)
        s_alt, s_az = self.sun_alt_az_from_ra_dec(s_ra_topo, s_dec_topo, lat, lst)
        m_alt, m_az = self.sun_alt_az_from_ra_dec(m_ra_topo, m_dec_topo, lat, lst)
        # Projected
        pt_sun = self.project_universal_stereo(s_alt, s_az)
        pt_moon = self.project_universal_stereo(m_alt, m_az)
        s_px = f"{pt_sun[0]:.1f}, {pt_sun[1]:.1f}" if pt_sun else "OFF_SCREEN"
        m_px = f"{pt_moon[0]:.1f}, {pt_moon[1]:.1f}" if pt_moon else "OFF_SCREEN"
        log_line = (f"[{timestamp}] Local={local_hour:.2f}h UT={ut_hour:.2f}h | "
                    f"SUN: Alt={s_alt:.2f} Az={s_az:.2f} Px={s_px} | "
                    f"MOON: Alt={m_alt:.2f} Az={m_az:.2f} Px={m_px}")
        with open(os.path.join(log_dir, "pos_log_ctrl_l.txt"), "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
        # Feedback
        self.lbl_info.setText("LOG SAVED")
        self.lbl_info.show()
        QTimer.singleShot(2000, self.lbl_info.hide)
        print(f"LOG WRITTEN: {log_line}")
    except Exception as e:
        print(f"LOG ERROR: {e}")

def canvas_paintEvent(canvas, event):
    from TerraLab.ui import sky_widget_impl as _impl
    globals().update(_impl.__dict__)
    self = canvas
    painter = QPainter(self)
    try:
        painter.fillRect(self.rect(), Qt.black) # Safe background
        stage_order = {
            "boot": 0,
            "base_sky": 1,
            "stars_ready": 2,
            "horizon_preview": 3,
            "scene_ready": 4,
        }
        scene_stage = getattr(self.parent_widget, "scene_load_stage", "scene_ready")
        scene_stage_rank = stage_order.get(scene_stage, 4)
        if scene_stage_rank >= 1 and not self._reported_first_useful_paint:
            self._reported_first_useful_paint = True
            if hasattr(self.parent_widget, "_on_canvas_first_useful_paint"):
                self.parent_widget._on_canvas_first_useful_paint()
        if scene_stage_rank <= 0:
            return
        # Interaction mode: favor smoothness while user moves camera/scope.
        fast_interaction = bool(
            self._camera_interaction_active(include_time_drag=True, include_animation=True)
            or self.scope_mode_enabled()
        )
        painter.setRenderHint(QPainter.Antialiasing, not fast_interaction)
        painter.setRenderHint(QPainter.TextAntialiasing, not fast_interaction)
        # 1. Determine Correct Time (Local -> UT) using Full Datetime
        local_hour = self.parent_widget.get_current_hour()
        sim_y = self.parent_widget.manual_year
        sim_d = self.parent_widget.manual_day
        # Base start of the local day
        try:
            # Create a timezone-aware datetime representing the observer's local time
            # We assume the observer's local time follows the system's timezone rules
            dt_base = datetime(sim_y, 1, 1) + timedelta(days=sim_d)
            dt_local_naive = dt_base + timedelta(hours=local_hour)
            dt_local = dt_local_naive.astimezone() # System local aware
            # Convert to UTC accurately
            dt_utc = dt_local.astimezone(timezone.utc)
            tz_offset = dt_local.utcoffset().total_seconds() / 3600.0
        except Exception as e:
            # Fallback to current system offset if fails
            tz_offset = datetime.now().astimezone().utcoffset().total_seconds() / 3600.0
            dt_utc = (datetime(sim_y, 1, 1) + timedelta(days=sim_d, hours=local_hour-tz_offset)).replace(tzinfo=timezone.utc)
        ut_hour = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
        self.ut_hour = ut_hour # Store for external access
        # Day of year relative to UTC (Critically avoids the 24h jump at midnight crossings)
        day_of_year_utc = (dt_utc.date() - datetime(dt_utc.year, 1, 1).date()).days
        # Scope tracking for selected stars follows simulated sky time.
        self._apply_scope_selected_target_tracking(ut_hour, day_of_year_utc)
        # Solar Hour (Apparent, Simple) for Sun
        solar_hour = (ut_hour + self.parent_widget.longitude / 15.0) % 24.0
        # 1. Determine Effective Environment (Local vs Antipode)
        day_of_year = self.parent_widget.manual_day
        rise_loc, set_loc = self.calculate_sun_times(self.parent_widget.latitude, day_of_year)
        is_day_loc = rise_loc <= solar_hour < set_loc
        # Calculate Antipodal Sun Times (-Lat, +12h)
        rise_anti, set_anti = self.calculate_sun_times(-self.parent_widget.latitude, day_of_year)
        solar_hour_anti = (solar_hour + 12.0) % 24.0
        is_day_anti = rise_anti <= solar_hour_anti < set_anti
        # Compute effective variables based on observer location
        # (We keep the environment continuous even if looking below the horizon)
        eff_is_day = is_day_loc
        eff_hour = solar_hour
        eff_rise = rise_loc
        eff_set = set_loc
        eff_lat = self.parent_widget.latitude
        # Draw Background using Effective Context (Altitude & Azimuth Driven)
        # CACHE USAGE:
        # Check chk_enable_sky
        # Defaults for when Sky is Disabled
        eff_sun_alt = 90.0
        eclipse_dimming = 1.0
        if True: # Always calculate sky background and celestial positions
            # Use Day of Year relative to UTC for astronomy updates
            day_for_astro = day_of_year_utc
            eff_sun_alt, eff_sun_az = self.get_sun_alt_az(eff_hour, eff_lat, day_for_astro)
            # Eclipse Dimming Calculation with SMART THROTTLING
            # Update Skyfield immediately when simulated time changes, but throttle during real-time
            import time
            current_time_ms = int(time.time() * 1000)
            skyfield_update_interval = 500  # ms for real-time updates
            if SKYFIELD_AVAILABLE and hasattr(self.parent_widget, 'eph'):
                # Check if simulated time has changed significantly (manual time change)
                last_cached_hour = self._sf_cache.get('ut_hour', -999)
                time_changed = abs(ut_hour - last_cached_hour) > 0.001  # ~3.6 seconds in simulation
                # Force update if time changed OR if enough real time has passed
                should_update = time_changed or (current_time_ms - self._last_skyfield_update > skyfield_update_interval)
                if should_update:
                    self._last_skyfield_update = current_time_ms
                    self._sf_cache['ut_hour'] = ut_hour  # Track simulated time
                    # Only trigger heavy Skyfield updates when needed (using UTC day)
                    self.update_skyfield_cache(ut_hour, day_for_astro)
                    self._eclipse_cache['value'] = self.get_eclipse_dimming_factor(ut_hour, day_for_astro)
                    self._eclipse_cache['time'] = current_time_ms
            # Use cached eclipse dimming
            eclipse_dimming = self._eclipse_cache.get('value', 1.0)
            # Wrap view_az (azimuth_offset) to 0..360 only for background logic/sun-pos
            wrapped_az = self.azimuth_offset % 360.0
        # 1. Background (Sky Gradient)
        self.draw_background(painter, eff_sun_alt, eff_sun_az, wrapped_az, dimming=eclipse_dimming)
        # 2. Light Pollution & Ambient Glows (Drawn BEFORE stars to avoid occlusion)
        if (not fast_interaction) and scene_stage_rank >= 3 and hasattr(self, 'horizon_overlay') and hasattr(self.horizon_overlay, 'profile'):
            self.draw_light_domes(painter, self.horizon_overlay.profile, eff_sun_alt, eclipse_dimming)
        # === WEATHER SYSTEM ===
        # IMPORTANT: forecast/cache keys are indexed in UTC slots.
        # We keep ephemerides untouched and only map weather indexing to UTC
        # so manual/local timeline changes match cached forecast rows.
        if scene_stage_rank >= 3:
            now_weather = time.monotonic()
            last_weather = float(getattr(self, "_last_weather_update_mono", 0.0))
            if (not fast_interaction) or (now_weather - last_weather >= 0.20):
                self.weather.update_weather(day_of_year_utc, ut_hour, year=dt_utc.year)
                self.weather.update_thunder()
                self._last_weather_update_mono = now_weather
        # 3. Trails
        checked = self._parent_checkbox_checked("chk_trails", default=False)
        if checked and eff_sun_alt < -6.0:
            if self.trail_start_hour is None:
                self.trail_start_hour = ut_hour
            self.draw_analytic_trails(painter, self.trail_start_hour, ut_hour)
        else:
            self.trail_start_hour = None
        # === STAR PARAMETER CALCULATION ===
        # Calculate Ambient Light Level (0.0 = Pitch Black, 1.0 = Full Day)
        if eff_sun_alt > 0:
            base_light = 1.0
        elif eff_sun_alt < -18.0:
            base_light = 0.0
        else:
            base_light = 1.0 - (eff_sun_alt / -18.0)
            base_light = max(0.0, min(1.0, base_light))
        ambient_light = base_light * eclipse_dimming
        vm_state = self.parent_widget.recompute_visual_magnitude_model(
            target_alt_deg=self.elevation_angle,
            sun_alt_deg=eff_sun_alt,
            now_utc=dt_utc,
        )
        # Dynamic Magnitude Limit
        # Daytime: Stars must be brighter than Sun's glare to be seen (basically only Sun/Moon)
        if eff_sun_alt > 0:
             sun_mag_limit = -10.0 # Absolute black-out for stars in daylight
        elif eff_sun_alt > -6.0:
             # Civil Twilight: Fast drop in visibility
             t = (eff_sun_alt - 0.0) / -6.0
             sun_mag_limit = -10.0 + 10.0 * t # -10 at sunset, 0 at nautical
        elif eff_sun_alt > -12.0:
             # Nautical Twilight: Transition to stars
             t = (eff_sun_alt + 6.0) / -6.0
             sun_mag_limit = 0.0 + 3.0 * t # 0 at start, 3.0 at nautical end
        elif eff_sun_alt > -18.0:
             # Astronomical Twilight: Fine tuning
             t = (eff_sun_alt + 12.0) / -6.0
             target_mag = float(vm_state.scope_limit_mag)
             sun_mag_limit = 3.0 + (target_mag - 3.0) * t
        else:
             sun_mag_limit = float(vm_state.scope_limit_mag)
        eclipse_bonus = (1.0 - eclipse_dimming) * 14.0
        auto_bortle = bool(getattr(self.parent_widget, "is_auto_bortle", True))
        if auto_bortle:
            bortle_class = max(1.0, min(9.0, float(getattr(self.parent_widget, "auto_bortle_estimate", 1))))
        else:
            bortle_class = max(1.0, min(9.0, 1.0 + (7.6 - float(self.parent_widget.magnitude_limit)) / 0.5))
        render_state = {
            "scope_enabled": bool(self.scope_mode_enabled()),
            "auto_bortle": auto_bortle,
            "bortle": bortle_class,
            "scope_mlim": float(vm_state.scope_limit_mag),
            "manual_mlim": float(self.parent_widget.magnitude_limit),
        }
        update_star_rendering_params(render_state)
        view_mag_limit = float(render_state.get("render_mag_limit", vm_state.scope_limit_mag))
        final_mag_limit = min(sun_mag_limit + eclipse_bonus, view_mag_limit)
        if not self.scope_mode_enabled():
            self.parent_widget.auto_star_scale_multiplier = 1.0
        ambient_light = base_light * (1.0 + (bortle_class - 1.0) * 0.04)
        # Cache Invalidation
        if not hasattr(self, '_last_mag_limit'): self._last_mag_limit = final_mag_limit
        if abs(self._last_mag_limit - final_mag_limit) > 0.05:
            self._cached_star_image = None
            self._last_mag_limit = final_mag_limit
        show_sun_moon_effects = self._parent_checkbox_checked("chk_sun_moon", default=True)
        vis_factor = 1.0
        moon_mask = None
        # If "Sun & Moon" is hidden, suppress all lunar visual side-effects too.
        if show_sun_moon_effects and hasattr(self, '_sf_cache') and self._sf_cache.get('data'):
            md = self._sf_cache['data'].get('moon')
            if md:
                pt_m = self.project_universal_stereo(md['alt'], md['az'])
                if pt_m:
                    R_proj = min(self.width(), self.height()) / 2.0 * self.zoom_level
                    ppd = R_proj / 90.0
                    m_rad_deg = md.get('rad_deg', 0.25)
                    mr_px = m_rad_deg * ppd * 10.0
                    moon_mask = (pt_m[0], pt_m[1], mr_px)
        # 4. Stars & Celestial Objects
        show_stars_layer = self._parent_checkbox_checked("chk_enable_sky", default=True)
        show_milkyway_layer = self._parent_checkbox_checked("chk_enable_milkyway", default=True)
        if scene_stage_rank >= 1 and (show_stars_layer or show_milkyway_layer):
            self.draw_stars(painter, ut_hour, eff_sun_alt, eff_sun_az, visibility_factor=vis_factor, moon_mask=moon_mask, mag_limit=final_mag_limit, eff_lat=eff_lat, day_of_year=day_for_astro)
        self.visible_sky_objects = []
        if scene_stage_rank >= 2 and SKYFIELD_AVAILABLE and hasattr(self.parent_widget, 'eph'):
             self.draw_skyfield_objects(painter, ut_hour, day_for_astro, ambient_light=ambient_light, mag_limit=final_mag_limit)
        # Constellations are part of sky content, so they must be occluded by terrain.
        if scene_stage_rank >= 2:
            scope_motion = self._scope_motion_active()
            if not (self.scope_mode_enabled() and scope_motion):
                self.constellation_controller.draw(
                    painter,
                    self.project_universal_stereo,
                    lambda ra, dec: self._ra_dec_to_alt_az(ra, dec, ut_hour, day_of_year_utc),
                )
            scope_data_state = str(getattr(self.parent_widget, "_scope_data_state", "ready_deep") or "ready_deep")
            # Keep deep-sky overlays visible in normal mode even if background scope loading is active.
            # Performance guardrails are only needed while scope mode is driving heavy updates.
            scope_loading_heavy = (
                scope_data_state in {"loading_deep", "error_deep"}
                or bool(getattr(self.parent_widget, "_scope_catalog_loading", False))
                or bool(getattr(self.parent_widget, "_scope_preload_in_progress", False))
            )
            ngc_heavy_load = bool(self.scope_mode_enabled() and (scope_motion or scope_loading_heavy))
            if self._parent_checkbox_checked("chk_deep_space", default=False) and (not ngc_heavy_load):
                self.draw_ngc_overlay(painter, ut_hour, day_of_year_utc)
            else:
                self.visible_ngc_objects = []
        # Weather cloud layer goes above stars/sun/moon so overcast can occlude them.
        # Horizon/topography will still be painted after this and remain in front.
        w_sun_alt = eff_sun_alt
        base_fov = 100.0
        current_fov = base_fov / self.zoom_level
        if scene_stage_rank >= 3:
            self.weather.draw(
                painter,
                w_sun_alt,
                self.azimuth_offset,
                self.elevation_angle,
                current_fov,
                eclipse_dimming=eclipse_dimming,
                project_fn=self.project_universal_stereo,
            )
        # 5. Horizon / Topography (Drawn on top to mask everything behind mountains)
        show_horizon = True
        if hasattr(self.parent_widget, 'chk_enable_horizon'):
            show_horizon = self._parent_checkbox_checked("chk_enable_horizon", default=True)
        use_detailed_topo = True
        if hasattr(self.parent_widget, 'chk_enable_village'):
            use_detailed_topo = self._parent_checkbox_checked("chk_enable_village", default=True)
        if scene_stage_rank >= 3 and show_horizon:
            force_flat = not use_detailed_topo
            dome_callback = None
            is_auto_bortle = getattr(self.parent_widget, 'is_auto_bortle', getattr(self, 'is_auto_bortle', True))
            if (not fast_interaction) and is_auto_bortle and hasattr(self, 'horizon_overlay') and hasattr(self.horizon_overlay, 'profile'):
                tw_factor = 1.0
                if eff_sun_alt >= 0: tw_factor = 0.0
                elif eff_sun_alt > -18.0: tw_factor = (0 - eff_sun_alt) / 18.0
                tw_factor *= eclipse_dimming
                if tw_factor > 0.01:
                    dome_callback = lambda p, idx, dist: self._draw_single_city_dome(p, self.horizon_overlay.profile, idx, dist, tw_factor)
            self.horizon_overlay.draw(
                painter, self.project_universal_stereo,
                self.width(), self.height(),
                self.azimuth_offset, self.zoom_level,
                self.elevation_angle, ut_hour,
                draw_flat_line=force_flat,
                projection_fn_numpy=self.project_universal_stereo_numpy,
                draw_domes_callback=dome_callback
            )
            if hasattr(self, '_dome_count') and self._dome_count > 0:
                current_time = __import__('time').time()
                if current_time - getattr(self, '_last_dome_log_time', 0) > 2.0:
                    print(f"[AstroCanvas] City Domes Draw Call: {self._dome_count} centers found.")
                    self._last_dome_log_time = current_time
        # Weather already rendered once (above celestial objects, below terrain).
        # 6. Compass
        self.draw_compass(painter)
        # Selected-star marker (hidden in scope mode by design).
        self._draw_selected_target_marker(painter, ut_hour, day_of_year_utc)
        # 7. HUD information
        painter.setPen(QColor(255, 255, 255, 200))
        lbl_stars = getTraduction("Astro.Stars", "STARS")
        if self.scope_mode_enabled() and self._scope_motion_active():
            stars_count = self._visible_star_count_raw()
        else:
            stars_count = self._scope_hud_star_count() if self.scope_mode_enabled() else self._visible_star_count_raw()
        painter.drawText(20, 30, f"{lbl_stars}: {stars_count}")
        mw_line = self._milkyway_overlay_status_line()
        painter.setFont(QFont("Arial", 9))
        painter.drawText(20, 45, mw_line)
        # Direction HUD
        az = self.azimuth_offset % 360
        dirs_keys = ["North", "NE", "East", "SE", "South", "SW", "West", "NW", "North"]
        dirs = [getTraduction(f"Astro.{d}", d.upper()) for d in dirs_keys]
        idx = int((az + 22.5) / 45)
        if idx >= len(dirs): idx = 0
        direction_str = dirs[idx]
        lbl_looking = getTraduction("Astro.Looking", "LOOKING")
        painter.setFont(QFont("Arial", 14, QFont.Bold))
        painter.drawText(20, 68, f"{lbl_looking}: {direction_str}")
        # Lat Indicator
        lat = self.parent_widget.latitude
        hemi = "N" if lat >= 0 else "S"
        painter.setFont(QFont("Arial", 10))
        painter.drawText(20, 88, f"LAT: {abs(lat):.2f}° {hemi}")
        # Altitude Indicator
        alt = self.elevation_angle
        lbl_alt = "ALT"
        painter.drawText(20, 108, f"{lbl_alt}: {alt:.1f}°")
        # Focal Length Indicator & Human Eye Button
        # Base FOV = 100 deg (Zoom 1.0)
        # 35mm equiv focal length: f = 36 / (2 * tan(fov_horiz/2))
        # fov_horiz_rad = radians(100 / zoom)
        fov_rad = math.radians(93.9 / self.zoom_level)
        focal_length = 1.0
        if (93.9 / self.zoom_level) < 179.0:
            focal_length = max(1.0, 18.0 / math.tan(fov_rad / 2.0))
        # Using round() to avoid 49.99mm displaying as 49mm
        if self.scope_mode_enabled() and hasattr(self, "scope_controller"):
            display_focal = int(round(max(1.0, float(getattr(self.scope_controller, "focal_mm", focal_length)))))
        else:
            display_focal = max(1, int(round(focal_length)))
        painter.drawText(20, 128, f"FOC: {display_focal} mm")
        # Position the eye button dynamically next to FOC text
        # Assuming text width ~ 100px?
        if hasattr(self, 'btn_human_eye'):
            # Move button only if needed
            self.btn_human_eye.move(140, 105)
            if not self.btn_human_eye.isVisible():
                self.btn_human_eye.show()
        # 8. User overlays above terrain
        self.measurement_controller.draw(
            painter,
            self.project_universal_stereo,
            formatters={},
        )
        scope_hud_lines = self._scope_hud_extra_lines(ut_hour, day_of_year_utc)
        scope_data_state = str(getattr(self.parent_widget, "_scope_data_state", "ready_deep") or "ready_deep")
        capture_overlay_text = None
        if scope_data_state == "loading_deep":
            capture_overlay_text = getTraduction("Scope.CapturingPhoto", "Tomando foto...")
        elif scope_data_state == "error_deep":
            capture_overlay_text = (
                f"{getTraduction('Scope.CapturingPhoto', 'Tomando foto...')} "
                f"({getTraduction('Scope.DeepError', 'Error de carga')})"
            )
        self.scope_controller.draw(
            painter,
            self.width(),
            self.height(),
            self.project_universal_stereo,
            hud_extra_lines=scope_hud_lines,
            capture_overlay_text=capture_overlay_text,
        )
        if hasattr(self.parent_widget, "_refresh_milkyway_status_indicator"):
            self.parent_widget._refresh_milkyway_status_indicator()
    finally:
        painter.end()
