"""Responsive UI presenter for the process-owned astronomical scene."""

from __future__ import annotations

import html
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from PyQt5.QtCore import QRectF, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QApplication, QMenu, QToolTip, QWidget

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.app_paths import app_root
from TerraLab.common.utils import getTraduction, set_config_value
from TerraLab.ui.canvas_mixins.interaction import CanvasInteractionMixin
from TerraLab.ui.frame_presenter import SharedFramePresenter
from TerraLab.light_pollution.modes import (
    bortle_to_magnitude,
    normalize_light_pollution_mode,
    resolve_bortle_class,
)
from TerraLab.scene.render_state import resolve_earth_layer_visibility
from TerraLab.ui.widget_init_helpers import astro_canvas_init
from TerraLab.widgets.constellation_drawing import (
    ConstellationGroup,
    ConstellationNode,
)


class AstroCanvas(CanvasInteractionMixin, QWidget):
    """Translate input to snapshots and present the newest completed frame."""

    request_render_signal = pyqtSignal(dict)
    request_trails_signal = pyqtSignal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        astro_canvas_init(self, parent)
        app = QApplication.instance()
        runtime = getattr(app, "terralab_runtime", None)
        self._owns_runtime = bool(
            runtime is None or getattr(runtime, "_closing", False)
        )
        if self._owns_runtime:
            from TerraLab.runtime.supervisor import RuntimeSupervisor

            runtime = RuntimeSupervisor(app)
            setattr(app, "terralab_runtime", runtime)
            runtime.start()
            app.aboutToQuit.connect(runtime.stop)

        self._frame_presenter = SharedFramePresenter(runtime, self)
        self._frame_presenter.setAttribute(
            Qt.WA_TransparentForMouseEvents, True
        )
        self._frame_presenter.setGeometry(self.rect())
        self._frame_presenter.show()
        self._frame_presenter.raise_()
        self._raise_local_widgets()
        self._frame_presenter.pick_result.connect(
            self._on_process_pick
        )
        self._frame_presenter.frame_presented.connect(
            self._on_process_frame_presented
        )
        self._remote_submit_timer = QTimer(self)
        self._remote_submit_timer.setSingleShot(True)
        self._remote_submit_timer.timeout.connect(
            self._submit_process_scene
        )
        self._scope_pressed_keys: set[int] = set()
        self._scope_last_tick = time.monotonic()
        self._scope_move_timer = QTimer(self)
        self._scope_move_timer.setTimerType(Qt.PreciseTimer)
        # This timer exists only while a movement key is held; rendering is
        # frame-driven by Render and has no UI-side cadence loop.
        self._scope_move_timer.setInterval(round(1000 / 60))
        self._scope_move_timer.timeout.connect(self._scope_move_tick)
        self._selection_pulse_timer = QTimer(self)
        self._selection_pulse_timer.setTimerType(Qt.PreciseTimer)
        self._selection_pulse_timer.setInterval(33)
        self._selection_pulse_timer.timeout.connect(
            self._selection_pulse_tick
        )
        self._schedule_process_scene()

    def shutdown(self, timeout_ms: int = 15_000) -> None:
        if self._render_thread_shutdown:
            return
        self._frame_presenter.shutdown()
        if self._owns_runtime:
            self._frame_presenter._runtime.stop(timeout_ms=0)
        for timer_name in (
            "_camera_idle_timer",
            "_scope_move_timer",
            "_selection_pulse_timer",
        ):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        self._render_thread_shutdown = True

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(0, 0, 0))
        finally:
            painter.end()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._frame_presenter.setGeometry(self.rect())
        self._frame_presenter.raise_()
        self._raise_local_widgets()
        self._schedule_process_scene()

    def _raise_local_widgets(self) -> None:
        """Keep lightweight controls above the process-owned frame."""

        for name in (
            "hint_overlay",
            "lbl_info",
            "btn_human_eye",
            "btn_hud_toggle",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.raise_()

    def update(self, *args, **kwargs) -> None:  # type: ignore[override]
        self._schedule_process_scene()
        try:
            QWidget.update(self, *args, **kwargs)
        except TypeError:
            QWidget.update(self)

    def _schedule_process_scene(self) -> None:
        timer = getattr(self, "_remote_submit_timer", None)
        if timer is not None and not timer.isActive():
            timer.start(0)

    def _submit_process_scene(self) -> None:
        now = time.monotonic()
        parent = self.parent_widget
        is_interactive = bool(
            self.dragging
            or getattr(parent, "_dragging_time", False)
            or time.monotonic() < float(getattr(self, "_camera_interaction_until", 0.0))
        )
        if is_interactive:
            if now - float(getattr(self, "_last_submit_ts", 0.0)) < 0.015:
                self._schedule_process_scene()
                return
        self._last_submit_ts = now
        try:
            self._frame_presenter.submit(self._process_scene_snapshot())
        except Exception:
            log_suppressed_exception(
                __name__, "AstroCanvas._submit_process_scene"
            )

    def _on_process_frame_presented(
        self, _generation: int, _render_ms: float
    ) -> None:
        parent = self.parent_widget
        if not bool(getattr(self, "_reported_first_useful_paint", False)):
            self._reported_first_useful_paint = True
            callback = getattr(
                parent, "_on_canvas_first_useful_paint", None
            )
            if callable(callback):
                callback()
        if (
            getattr(parent, "target_azimuth", None) is not None
            or getattr(parent, "target_elevation", None) is not None
        ):
            # Render completion is the clock: never queue camera states faster
            # than the isolated renderer can consume them.
            QTimer.singleShot(0, parent.animate_view)

    def _process_scene_snapshot(self) -> dict[str, Any]:
        parent = self.parent_widget
        try:
            ut_hour, day_utc, year_utc, _ = (
                self._get_current_utc_context()
            )
        except (AttributeError, TypeError, ValueError):
            ut_hour = float(getattr(parent, "manual_hour", 12.0))
            day_utc = int(getattr(parent, "manual_day", 0))
            year_utc = int(getattr(parent, "manual_year", 2026))

        coordinator = getattr(parent, "ephemeris_coordinator", None)
        ephemeris = (
            coordinator.get_snapshot()
            if coordinator is not None
            else None
        )
        scope_center = getattr(self.scope_controller, "center", None)
        try:
            scope_fov = self.scope_controller.current_fov()
        except (AttributeError, TypeError, ValueError):
            scope_fov = (5.0, 5.0)
        layers: list[str] = []
        if self._parent_checkbox_checked("chk_enable_sky", True):
            layers.append("stars")
        if self._parent_checkbox_checked("chk_enable_milkyway", True):
            layers.append("milkyway")
        if self._parent_checkbox_checked("chk_solar_system", True):
            if self._parent_checkbox_checked("chk_sun_moon", True):
                layers.append("sun_moon")
            if self._parent_checkbox_checked("chk_planets", True):
                layers.append("planets")
        topography_requested = bool(
            self._parent_checkbox_checked("chk_enable_village", False)
        )
        horizon_requested = bool(
            self._parent_checkbox_checked("chk_enable_horizon", True)
        )
        light_pollution_requested = bool(
            getattr(parent, "light_pollution_enabled", True)
        )
        earth_visibility = resolve_earth_layer_visibility(
            horizon_enabled=horizon_requested,
            topography_enabled=topography_requested,
            surface_enabled=self._parent_checkbox_checked(
                "chk_surface_layer", True
            ),
            terrain_3d_enabled=self._parent_checkbox_checked(
                "chk_terrain_3d", False
            ),
            light_pollution_enabled=light_pollution_requested,
        )
        if earth_visibility.horizon_enabled:
            layers.append("terrain")
        if self._parent_checkbox_checked("chk_grid", False):
            layers.append("grid")
        if self._parent_checkbox_checked("chk_deep_space", False):
            layers.append("deep_sky")
        weather = getattr(parent, "weather", None)
        lp_mode = normalize_light_pollution_mode(
            getattr(parent, "light_pollution_mode", "automatic")
        )
        lp_enabled = earth_visibility.light_pollution_enabled
        effective_bortle = resolve_bortle_class(
            lp_mode,
            automatic_bortle=getattr(parent, "auto_bortle_estimate", 1),
            bortle_value=getattr(parent, "bortle_value", 1),
            magnitude_limit=getattr(parent, "magnitude_limit", 8.0),
            light_pollution_enabled=lp_enabled,
        )
        if lp_mode == "magnitude" and lp_enabled:
            effective_mag_limit = float(getattr(parent, "magnitude_limit", 8.0))
        else:
            effective_mag_limit = bortle_to_magnitude(effective_bortle)
        return {
            "camera": {
                "azimuth": float(self.azimuth_offset),
                "elevation": float(self.elevation_angle),
                "zoom": float(self.zoom_level),
                "vertical_ratio": float(self.vertical_offset_ratio),
            },
            "ut_hour": float(ut_hour),
            "day_of_year_utc": int(day_utc),
            "year_utc": int(year_utc),
            "latitude": float(getattr(parent, "latitude", 0.0)),
            "longitude": float(getattr(parent, "longitude", 0.0)),
            "altitude_m": float(getattr(parent, "_observer_offset", 0.0)),
            "magnitude_limit": float(effective_mag_limit),
            "naked_eye_cap": float(
                getattr(parent, "scope_fallback_mag_limit", 8.0)
            ),
            "bortle": int(round(effective_bortle)),
            "light_pollution_mode": str(lp_mode),
            "light_pollution_enabled": bool(lp_enabled),
            "pure_colors": bool(getattr(parent, "pure_colors", False)),
            "spike_magnitude_threshold": float(
                getattr(parent, "spike_magnitude_threshold", 2.0)
            ),
            "star_scale": float(getattr(parent, "star_scale", 1.0)),
            "auto_star_scale_multiplier": float(
                getattr(parent, "auto_star_scale_multiplier", 1.0)
            ),
            "scope_k_fallback": float(
                getattr(parent, "scope_k_fallback", 0.2)
            ),
            "scope_enabled": self.scope_mode_enabled(),
            "scope_center_sky": (
                [float(scope_center[0]), float(scope_center[1])]
                if scope_center is not None
                else None
            ),
            "scope_fov_deg": [
                float(scope_fov[0]),
                float(scope_fov[1]),
            ],
            "scope_shape": str(
                getattr(self.scope_controller, "shape", "circle")
            ),
            "interaction_active": bool(
                self._camera_interaction_active(
                    include_time_drag=True,
                    include_animation=True,
                )
                or self._scope_motion_active()
            ),
            "debug_render_metrics": bool(self.debug_render_metrics),
            "hud_visible": bool(self.hud_visible),
            "layers": layers,
            "trails": {
                "enabled": bool(
                    self._parent_checkbox_checked("chk_trails", False)
                ),
                "start_hour": (
                    float(self.trail_start_hour)
                    if self.trail_start_hour is not None
                    else None
                ),
            },
            "catalog": self._process_catalog_artifact(),
            "ngc": dict(
                getattr(parent, "_render_ngc_artifact", {}) or {}
            ),
            "terrain": {
                "profile_path": str(
                    getattr(
                        parent, "_remote_terrain_profile_path", ""
                    )
                    or ""
                ),
                "surface_path": str(
                    getattr(
                        parent, "_remote_terrain_surface_path", ""
                    )
                    or ""
                ),
                "terrain_3d_enabled": earth_visibility.terrain_3d_enabled,
                "topography_enabled": earth_visibility.topography_enabled,
                "horizon_enabled": earth_visibility.horizon_enabled,
                "surface_enabled": earth_visibility.surface_enabled,
                "surface_visual_style": str(
                    getattr(
                        parent, "surface_visual_style", "original"
                    )
                    or "original"
                ),
            },
            "weather": (
                weather.snapshot()
                if weather is not None
                and callable(getattr(weather, "snapshot", None))
                else {"enabled": False}
            ),
            "ephemeris": ephemeris or {},
            "selection": self._process_selection_payload(),
            "measurement": {
                "tool": str(
                    getattr(
                        self.measurement_controller,
                        "active_tool",
                        "none",
                    )
                ),
                "clear_revision": int(
                    getattr(self, "_measurement_clear_revision", 0)
                ),
            },
            "constellation": self._process_constellation_payload(),
            "milkyway": {
                "enabled": bool(
                    getattr(parent, "milkyway_overlay_enabled", True)
                ),
                "texture_path": str(
                    getattr(parent, "milkyway_overlay_texture_path", "")
                ),
                "opacity": float(
                    getattr(parent, "milkyway_overlay_opacity", 0.65)
                ),
                "blend_mode": str(
                    getattr(parent, "milkyway_overlay_blend_mode", "add")
                ),
                "ra_offset_deg": float(
                    getattr(parent, "milkyway_overlay_ra_offset_deg", 180.0)
                ),
                "coord_frame": str(
                    getattr(
                        parent,
                        "milkyway_overlay_coord_frame",
                        "galactic",
                    )
                ),
                "lat_flip": bool(
                    getattr(parent, "milkyway_overlay_lat_flip", True)
                ),
                "lon_flip": bool(
                    getattr(parent, "milkyway_overlay_lon_flip", True)
                ),
                "sample_scale": float(
                    getattr(parent, "milkyway_overlay_sample_scale", 1.0)
                ),
                "dust_map_enabled": bool(
                    getattr(parent, "dust_map_enabled", False)
                ),
                "dust_map_path": str(
                    getattr(parent, "dust_map_path", "")
                ),
                "dust_density_strength": float(
                    getattr(parent, "dust_density_strength", 0.0)
                ),
                "dust_extinction_strength": float(
                    getattr(parent, "dust_extinction_strength", 0.65)
                ),
            },
            "extras": {
                "catalog_mag_sorted": bool(
                    getattr(parent, "_catalog_mag_sorted", True)
                ),
                "scope_instrument_profile": str(
                    getattr(
                        parent,
                        "scope_instrument_profile",
                        "telescope",
                    )
                ),
                "scope_iso": int(getattr(parent, "scope_iso", 800)),
                "scope_focal_mm": float(
                    getattr(self.scope_controller, "focal_mm", 250.0)
                ),
                "scope_sensor_profile": str(
                    getattr(self.scope_controller, "sensor_key", "tiny")
                ),
                "scope_aperture_input_mode": str(
                    getattr(
                        parent,
                        "scope_aperture_input_mode",
                        "diameter_mm",
                    )
                ),
                "scope_aperture_mm": float(
                    getattr(parent, "scope_aperture_mm", 80.0)
                ),
                "scope_eyepiece_mm": float(
                    getattr(parent, "scope_eyepiece_mm", 20.0)
                ),
                "scope_eye_pupil_dark_mm": float(
                    getattr(parent, "scope_eye_pupil_dark_mm", 6.5)
                ),
                "scope_dataset_max_mag": float(
                    getattr(parent, "_catalog_max_mag", 8.0)
                ),
                "scope_atmospheric_loss_mag": float(
                    (
                        getattr(parent, "scope_atmo_metrics", {}) or {}
                    )
                    .get("hud_metrics", {})
                    .get("loss_mag", 0.0)
                    or 0.0
                ),
                "scope_exposure_s": float(
                    getattr(parent, "scope_exposure_s", 2.0)
                ),
                "scope_aperture_f_number": float(
                    getattr(parent, "scope_aperture_f_number", 4.0)
                ),
            },
        }

    def _process_selection_payload(self) -> dict[str, Any]:
        selected = self.selected_target
        if not isinstance(selected, dict):
            return {}
        result = {}
        for key in (
            "kind",
            "type",
            "key",
            "name",
            "alt",
            "az",
            "screen_distance",
            "mag",
            "ra",
            "dec",
        ):
            value = selected.get(key)
            if isinstance(value, (str, int, float, bool)):
                result[key] = value
        star = selected.get("star")
        if isinstance(star, dict):
            result["star"] = {
                str(key): value
                for key, value in star.items()
                if isinstance(value, (str, int, float, bool))
            }
        return result

    def _process_constellation_payload(self) -> dict[str, Any]:
        controller = self.constellation_controller
        return {
            "data_path": str(controller.data_path or ""),
            "enabled": bool(controller.enabled),
            "visible": bool(controller.visible),
            "groups": [
                {
                    "name": str(group.name),
                    "nodes": [
                        {
                            "ra": float(node.ra_deg),
                            "dec": float(node.dec_deg),
                            "star_id": str(node.star_id or ""),
                            "star_name": str(node.star_name or ""),
                            "connect": bool(node.connect_from_prev),
                        }
                        for node in group.nodes
                    ],
                }
                for group in controller.groups
            ],
            "active_group_index": controller.active_group_index,
            "selected_group_index": controller.selected_group_index,
            "selected_node_index": controller.selected_node_index,
            "selected_segment_index": controller.selected_segment_index,
            "selected_segments": [
                [int(group), int(segment)]
                for group, segment in sorted(
                    controller.selected_segments
                )
            ],
            "selected_group_indices": sorted(
                int(value)
                for value in controller.selected_group_indices
            ),
            "group_drawing_active": bool(
                controller.group_drawing_active
            ),
            "resume_from_node_index": controller.resume_from_node_index,
            "preview_ra_dec": (
                list(controller.preview_ra_dec)
                if controller.preview_ra_dec is not None
                else None
            ),
            "preview_snapped": bool(controller.preview_snapped),
        }

    def _process_catalog_artifact(self) -> dict[str, str]:
        artifact = getattr(
            self.parent_widget, "_render_catalog_artifact", None
        )
        if not isinstance(artifact, dict):
            return {"catalog_path": ""}
        return {
            str(key): str(value)
            for key, value in artifact.items()
            if key in {"catalog_path", "r_path", "g_path", "b_path"}
        }

    def _resolve_observer_tzinfo(self):
        configured = str(
            getattr(self.parent_widget, "observer_timezone", "") or ""
        ).strip()
        if configured:
            try:
                return ZoneInfo(configured)
            except (KeyError, ValueError):
                pass
        return datetime.now().astimezone().tzinfo or timezone.utc

    def _get_current_utc_context(self):
        parent = self.parent_widget
        local_hour = float(parent.get_current_hour())
        local = datetime(
            int(parent.manual_year), 1, 1
        ) + timedelta(
            days=int(parent.manual_day), hours=local_hour
        )
        local = local.replace(tzinfo=self._resolve_observer_tzinfo())
        utc = local.astimezone(timezone.utc)
        ut_hour = (
            utc.hour + utc.minute / 60.0 + utc.second / 3600.0
        )
        day = (utc.date() - datetime(utc.year, 1, 1).date()).days
        return ut_hour, day, utc.year, utc

    def _set_selected_target(self, target) -> None:
        self.selected_target = target
        self._update_selection_pulse_timer()
        self.update()

    def _selection_pulse_tick(self) -> None:
        if self.selected_target is None or self.scope_mode_enabled():
            self._selection_pulse_timer.stop()
            return
        self.update()

    def _update_selection_pulse_timer(self) -> None:
        timer = getattr(self, "_selection_pulse_timer", None)
        if timer is None:
            return
        should_run = (
            self.selected_target is not None
            and not self.scope_mode_enabled()
        )
        if should_run and not timer.isActive():
            timer.start()
        elif not should_run and timer.isActive():
            timer.stop()

    def _camera_wheel_zoom(self, steps: float) -> None:
        self.zoom_level *= 1.12 ** float(steps)
        self.zoom_level = max(0.5, min(140.0, self.zoom_level))
        self._mark_camera_interaction()
        self.update()

    def _scope_wheel_zoom(self, steps: float) -> None:
        current = max(
            1.0, float(getattr(self.scope_controller, "focal_mm", 250.0))
        )
        self.set_scope_focal_mm(
            max(20.0, min(5000.0, current * (1.12 ** steps)))
        )

    def _pan_camera_from_pointer(
        self,
        x: float,
        y: float,
        *,
        sensitivity: float = 0.5,
        scope_interaction: bool = False,
    ) -> float:
        dx = float(x) - float(self.last_mouse_x)
        dy = float(y) - float(self.last_mouse_y)
        self.azimuth_offset = (
            float(self.azimuth_offset) - dx * float(sensitivity)
        ) % 360.0
        self.elevation_angle = max(
            -89.9,
            min(
                89.9,
                float(self.elevation_angle)
                + dy * float(sensitivity),
            ),
        )
        self.last_mouse_x = float(x)
        self.last_mouse_y = float(y)
        if scope_interaction:
            self._mark_scope_interaction(0.18)
        else:
            self._mark_camera_interaction()
        self.update()
        return abs(dx) + abs(dy)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.press_pos = event.pos()
            self._pointer_modifiers = int(event.modifiers())
            if self.measurement_tool_active():
                if event.modifiers() & Qt.ControlModifier:
                    self._remote_pointer_domain = "measurement_camera"
                    self.dragging = True
                    self.last_mouse_x = event.x()
                    self.last_mouse_y = event.y()
                    self._mark_camera_interaction()
                else:
                    self._remote_pointer_domain = "measurement"
                    self._request_process_interaction(
                        "press", event.x(), event.y()
                    )
                event.accept()
                return
            if self.drawing_mode_enabled():
                if event.modifiers() & Qt.ControlModifier:
                    self._remote_pointer_domain = "constellation_camera"
                    self.dragging = True
                    self.last_mouse_x = event.x()
                    self.last_mouse_y = event.y()
                    self._drawing_ctrl_pan_started = False
                    self._drawing_ctrl_click_pending = True
                else:
                    self._remote_pointer_domain = "constellation"
                event.accept()
                return
            if self.scope_mode_enabled():
                if event.modifiers() & Qt.ControlModifier:
                    self._remote_pointer_domain = "scope_camera"
                    self.scope_camera_lock_to_target = False
                    self.dragging = True
                    self.last_mouse_x = event.x()
                    self.last_mouse_y = event.y()
                else:
                    self._remote_pointer_domain = "scope"
                    self.scope_camera_lock_to_target = False
                    self.scope_reticle_lock_to_target = False
                    if not (
                        event.modifiers() & Qt.ShiftModifier
                    ):
                        self._set_selected_target(None)
                event.accept()
                return
            self.dragging = True
            self.last_mouse_x = event.x()
            self.last_mouse_y = event.y()
            self._mark_camera_interaction()
            event.accept()
            return
        QWidget.mousePressEvent(self, event)

    def mouseMoveEvent(self, event) -> None:
        if self._remote_pointer_domain == "measurement":
            self._request_process_interaction(
                "move", event.x(), event.y()
            )
            event.accept()
            return
        if (
            self._remote_pointer_domain == "measurement_camera"
            and self.dragging
        ):
            self._pan_camera_from_pointer(event.x(), event.y())
            event.accept()
            return
        if self._remote_pointer_domain == "constellation":
            self._request_process_interaction(
                "constellation_move", event.x(), event.y()
            )
            event.accept()
            return
        if (
            self._remote_pointer_domain == "constellation_camera"
            and self.dragging
        ):
            if (
                event.pos() - self.press_pos
            ).manhattanLength() >= 3:
                self._drawing_ctrl_pan_started = True
            if self._drawing_ctrl_pan_started:
                self._pan_camera_from_pointer(event.x(), event.y())
            event.accept()
            return
        if self._remote_pointer_domain == "scope":
            self._request_process_pick(
                event.x(), event.y(), purpose="scope"
            )
            event.accept()
            return
        if self._remote_pointer_domain == "scope_camera" and self.dragging:
            self._pan_camera_from_pointer(
                event.x(),
                event.y(),
                sensitivity=self._scope_secondary_drag_deg_per_px(),
                scope_interaction=True,
            )
            event.accept()
            return
        if self.dragging:
            self._pan_camera_from_pointer(event.x(), event.y())
            event.accept()
            return
        QWidget.mouseMoveEvent(self, event)
        self._last_hover_global_pos = event.globalPos()
        self._request_process_pick(
            event.x(), event.y(), purpose="hover"
        )

    def mouseReleaseEvent(self, event) -> None:
        modifiers = int(getattr(self, "_pointer_modifiers", 0)) | int(
            event.modifiers()
        )
        if (
            event.button() == Qt.LeftButton
            and self._remote_pointer_domain == "measurement"
        ):
            self._remote_pointer_domain = ""
            self._request_process_interaction(
                "release", event.x(), event.y()
            )
            event.accept()
            return
        if (
            event.button() == Qt.LeftButton
            and self._remote_pointer_domain == "measurement_camera"
        ):
            self._remote_pointer_domain = ""
            self.dragging = False
            self.update()
            event.accept()
            return
        if (
            event.button() == Qt.LeftButton
            and self._remote_pointer_domain == "scope"
        ):
            self._remote_pointer_domain = ""
            self._request_process_pick(
                event.x(),
                event.y(),
                purpose=(
                    "scope_select"
                    if modifiers & int(Qt.ShiftModifier)
                    else "scope"
                ),
            )
            event.accept()
            return
        if (
            event.button() == Qt.LeftButton
            and self._remote_pointer_domain == "scope_camera"
        ):
            self._remote_pointer_domain = ""
            self.dragging = False
            self.update()
            event.accept()
            return
        if (
            event.button() == Qt.LeftButton
            and self._remote_pointer_domain == "constellation"
        ):
            self._remote_pointer_domain = ""
            self._request_process_interaction(
                "constellation_click",
                event.x(),
                event.y(),
                force_add=bool(modifiers & int(Qt.ShiftModifier)),
                additive_select=bool(
                    modifiers & int(Qt.ControlModifier)
                ),
            )
            event.accept()
            return
        if (
            event.button() == Qt.LeftButton
            and self._remote_pointer_domain == "constellation_camera"
        ):
            self._remote_pointer_domain = ""
            self.dragging = False
            was_click = not bool(self._drawing_ctrl_pan_started)
            self._drawing_ctrl_pan_started = False
            self._drawing_ctrl_click_pending = False
            if was_click:
                self._request_process_interaction(
                    "constellation_click",
                    event.x(),
                    event.y(),
                    additive_select=True,
                )
            self.update()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self.dragging:
            self.dragging = False
            if (event.pos() - self.press_pos).manhattanLength() < 5:
                if bool(
                    getattr(
                        self,
                        "_suppress_constellation_release_click",
                        False,
                    )
                ):
                    self._suppress_constellation_release_click = False
                elif self.constellation_visible():
                    self._pending_constellation_fallback_pick = (
                        float(event.x()),
                        float(event.y()),
                    )
                    self._request_process_interaction(
                        "constellation_click",
                        event.x(),
                        event.y(),
                        additive_select=bool(
                            modifiers & int(Qt.ControlModifier)
                        ),
                        allow_when_disabled=True,
                    )
                else:
                    self._request_process_pick(event.x(), event.y())
            self.update()
            event.accept()
            return
        QWidget.mouseReleaseEvent(self, event)

    def mouseDoubleClickEvent(self, event) -> None:
        if (
            event.button() == Qt.LeftButton
            and self.constellation_visible()
            and not self.scope_mode_enabled()
            and not self.measurement_tool_active()
        ):
            self._suppress_constellation_release_click = True
            self._request_process_interaction(
                "constellation_double",
                event.x(),
                event.y(),
                additive_select=bool(
                    event.modifiers() & Qt.ControlModifier
                ),
                allow_when_disabled=not self.drawing_mode_enabled(),
            )
            event.accept()
            return
        if event.button() == Qt.LeftButton and self.scope_mode_enabled():
            self._request_process_pick(event.x(), event.y())
            event.accept()
            return
        QWidget.mouseDoubleClickEvent(self, event)

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        if self.drawing_mode_enabled():
            self._request_process_interaction(
                "constellation_right", event.x(), event.y()
            )
            event.accept()
            return
        self._context_menu_global_pos = event.globalPos()
        self._request_process_pick(
            event.x(), event.y(), purpose="context"
        )
        event.accept()

    def wheelEvent(self, event) -> None:
        steps = float(event.angleDelta().y()) / 120.0
        if self.scope_mode_enabled() and (
            event.modifiers() & Qt.ControlModifier
        ):
            self._scope_wheel_zoom(steps)
        else:
            self._camera_wheel_zoom(steps)
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_F9:
            self.debug_render_metrics = not bool(
                self.debug_render_metrics
            )
            set_config_value(
                "debug_render_metrics",
                bool(self.debug_render_metrics),
            )
            print(
                "[SkyDiagnostics] "
                f"debug_render_metrics={self.debug_render_metrics}"
            )
            self.update()
            event.accept()
            return
        if (
            event.key() == Qt.Key_S
            and event.modifiers() & Qt.ControlModifier
            and event.modifiers() & Qt.ShiftModifier
        ):
            run_smoke = getattr(
                self.parent_widget, "run_smoke_scenes", None
            )
            if callable(run_smoke):
                run_smoke()
            event.accept()
            return
        if (
            event.key() == Qt.Key_L
            and event.modifiers() & Qt.ControlModifier
        ):
            self.log_positions()
            event.accept()
            return
        if self.measurement_tool_active():
            if (
                event.key() == Qt.Key_Z
                and event.modifiers() & Qt.ControlModifier
            ):
                self._request_process_interaction("undo")
                event.accept()
                return
            if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                self._request_process_interaction("delete")
                event.accept()
                return
            if event.key() == Qt.Key_Escape:
                self._remote_pointer_domain = ""
                self._request_process_interaction("cancel")
                event.accept()
                return
        if self.drawing_mode_enabled():
            action = None
            if (
                event.key() == Qt.Key_Z
                and event.modifiers() & Qt.ControlModifier
            ):
                action = "constellation_undo"
            elif event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                action = "constellation_delete"
            elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
                action = "constellation_finish"
            elif event.key() == Qt.Key_Escape:
                self._remote_pointer_domain = ""
                action = "constellation_cancel"
            if action is not None:
                self._request_process_interaction(action)
                event.accept()
                return
        if self.scope_mode_enabled() and event.key() in (
            Qt.Key_Up,
            Qt.Key_Down,
            Qt.Key_Left,
            Qt.Key_Right,
        ):
            if event.isAutoRepeat():
                event.accept()
                return
            step = self.scope_controller.short_step_deg()
            delta = {
                Qt.Key_Up: (step, 0.0),
                Qt.Key_Down: (-step, 0.0),
                Qt.Key_Left: (0.0, step),
                Qt.Key_Right: (0.0, -step),
            }[event.key()]
            # A manual telescope correction must survive the next asynchronous
            # target update. Keep the camera following the selected celestial
            # object, but release the reticle from automatic tracking.
            self.scope_reticle_lock_to_target = False
            self.scope_controller.nudge(*delta)
            self._scope_pressed_keys.add(int(event.key()))
            self._scope_last_tick = time.monotonic()
            self._mark_scope_interaction(0.15)
            if not self._scope_move_timer.isActive():
                self._scope_move_timer.start()
            self.update()
            event.accept()
            return
        if self.scope_mode_enabled() and event.key() == Qt.Key_M:
            new_mode = (
                self.scope_controller.SPEED_FAST
                if self.scope_controller.speed_mode
                == self.scope_controller.SPEED_SLOW
                else self.scope_controller.SPEED_SLOW
            )
            self.set_scope_speed_mode(new_mode)
            sync = getattr(
                self.parent_widget, "sync_scope_speed_ui", None
            )
            if callable(sync):
                sync(new_mode)
            event.accept()
            return
        if self.scope_mode_enabled() and event.key() == Qt.Key_Escape:
            manager = getattr(
                self.parent_widget, "_scope_ui_manager", None
            )
            if manager is not None:
                manager.exit()
            else:
                self.set_scope_enabled(False)
            event.accept()
            return
        QWidget.keyPressEvent(self, event)

    def log_positions(self) -> None:
        """Append the current process-published sky positions to a log."""

        try:
            snapshot = self._process_scene_snapshot()
            ephemeris = snapshot.get("ephemeris")
            ephemeris = (
                ephemeris if isinstance(ephemeris, dict) else {}
            )
            sun = (
                ephemeris.get("sun")
                if isinstance(ephemeris.get("sun"), dict)
                else {}
            )
            moon = (
                ephemeris.get("moon")
                if isinstance(ephemeris.get("moon"), dict)
                else {}
            )
            timestamp = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            line = (
                f"[{timestamp}] "
                f"UT={float(snapshot.get('ut_hour', 0.0)):.4f}h | "
                f"CAM: Alt={float(self.elevation_angle):.3f} "
                f"Az={float(self.azimuth_offset) % 360.0:.3f} | "
                f"SUN: Alt={float(sun.get('alt', float('nan'))):.4f} "
                f"Az={float(sun.get('az', float('nan'))):.4f} | "
                f"MOON: Alt={float(moon.get('alt', float('nan'))):.4f} "
                f"Az={float(moon.get('az', float('nan'))):.4f}"
            )
            log_directory = app_root() / "logs"
            log_directory.mkdir(parents=True, exist_ok=True)
            log_path = log_directory / "pos_log_ctrl_l.txt"
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
            self.lbl_info.setText("LOG SAVED")
            self.lbl_info.show()
            QTimer.singleShot(2000, self.lbl_info.hide)
            print(f"LOG WRITTEN: {line}")
        except (OSError, TypeError, ValueError) as exc:
            print(f"LOG ERROR: {exc}")

    def keyReleaseEvent(self, event) -> None:
        if self.scope_mode_enabled() and event.key() in (
            Qt.Key_Up,
            Qt.Key_Down,
            Qt.Key_Left,
            Qt.Key_Right,
        ):
            if not event.isAutoRepeat():
                self._scope_pressed_keys.discard(int(event.key()))
                if not self._scope_pressed_keys:
                    self._scope_move_timer.stop()
            event.accept()
            return
        QWidget.keyReleaseEvent(self, event)

    def _scope_move_tick(self) -> None:
        if not self.scope_mode_enabled() or not self._scope_pressed_keys:
            self._scope_move_timer.stop()
            return
        now = time.monotonic()
        elapsed = max(0.001, min(0.10, now - self._scope_last_tick))
        self._scope_last_tick = now
        step = self.scope_controller.hold_rate_deg_per_s() * elapsed
        delta_altitude = 0.0
        delta_azimuth = 0.0
        if Qt.Key_Up in self._scope_pressed_keys:
            delta_altitude += step
        if Qt.Key_Down in self._scope_pressed_keys:
            delta_altitude -= step
        if Qt.Key_Left in self._scope_pressed_keys:
            delta_azimuth += step
        if Qt.Key_Right in self._scope_pressed_keys:
            delta_azimuth -= step
        if delta_altitude or delta_azimuth:
            self.scope_controller.nudge(
                delta_altitude, delta_azimuth
            )
            self._mark_scope_interaction(0.15)
            self.update()

    def _request_process_pick(
        self, x: float, y: float, *, purpose: str = "select"
    ) -> None:
        request = getattr(self._frame_presenter, "request_pick", None)
        if callable(request):
            request(float(x), float(y), purpose=purpose)

    def _request_process_interaction(
        self,
        action: str,
        x: float = 0.0,
        y: float = 0.0,
        *,
        force_add: bool = False,
        additive_select: bool = False,
        allow_when_disabled: bool = False,
    ) -> None:
        request = getattr(
            self._frame_presenter, "request_interaction", None
        )
        if callable(request):
            request(
                str(action),
                float(x),
                float(y),
                force_add=bool(force_add),
                additive_select=bool(additive_select),
                allow_when_disabled=bool(allow_when_disabled),
            )

    def _on_process_pick(self, result: object) -> None:
        if not isinstance(result, dict):
            return
        if str(result.get("purpose", "")) == "hover":
            if result.get("kind") != "surface":
                QToolTip.hideText()
                return
            QToolTip.showText(
                getattr(self, "_last_hover_global_pos", self.mapToGlobal(self.rect().center())),
                "<b>{}</b><br>{}<br>Classe {} · {}".format(
                    html.escape(str(result.get("name", ""))),
                    html.escape(str(result.get("description", ""))),
                    int(result.get("class_id", -1)),
                    html.escape(str(result.get("product", ""))),
                ),
                self,
            )
            return
        if str(result.get("purpose", "")) == "interaction":
            if (
                result.get("domain") == "constellation"
                and isinstance(result.get("constellation"), dict)
            ):
                self._apply_process_constellation_payload(
                    result["constellation"]
                )
                action_result = result.get("action_result")
                if (
                    isinstance(action_result, dict)
                    and action_result.get("action") == "rename_group"
                ):
                    rename = getattr(
                        self.parent_widget,
                        "rename_constellation_group_by_index",
                        None,
                    )
                    if callable(rename):
                        raw_rect = action_result.get("label_rect")
                        label_rect = None
                        if (
                            isinstance(raw_rect, (list, tuple))
                            and len(raw_rect) == 4
                        ):
                            label_rect = QRectF(
                                float(raw_rect[0]),
                                float(raw_rect[1]),
                                float(raw_rect[2]),
                                float(raw_rect[3]),
                            )
                        rename(
                            int(action_result.get("group_index", -1)),
                            label_rect=label_rect,
                        )
                pending_pick = getattr(
                    self,
                    "_pending_constellation_fallback_pick",
                    None,
                )
                self._pending_constellation_fallback_pick = None
                if (
                    not bool(result.get("handled", False))
                    and isinstance(pending_pick, tuple)
                    and len(pending_pick) == 2
                ):
                    self._request_process_pick(
                        pending_pick[0], pending_pick[1]
                    )
            self.update()
            return
        if str(result.get("purpose", "")) == "context":
            self._show_process_context_menu(result)
            return
        if str(result.get("purpose", "")) == "scope_select":
            self._set_selected_target(
                result if result.get("kind") != "none" else None
            )
            return
        if self.scope_mode_enabled():
            if "alt" in result and "az" in result:
                sky = (
                    float(result.get("alt", 0.0)),
                    float(result.get("az", 0.0)),
                )
            else:
                sky = None
            if sky is not None:
                self.scope_controller.set_center(sky, confirmed=True)
                self.update()
            return
        self._set_selected_target(
            result if result.get("kind") != "none" else None
        )

    def _show_process_context_menu(self, target: dict[str, Any]) -> None:
        if target.get("kind") == "none":
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu {"
            " background-color: rgba(8, 12, 22, 245);"
            " color: #f3f5fa;"
            " border: 1px solid #3b4559;"
            "}"
            "QMenu::item { padding: 6px 14px; }"
            "QMenu::item:selected {"
            " background-color: #211c14;"
            " color: #f1cd88;"
            "}"
        )
        goto_action = menu.addAction(
            getTraduction("Astro.ContextGoto", "Goto")
        )
        chosen = menu.exec_(
            getattr(
                self,
                "_context_menu_global_pos",
                self.mapToGlobal(self.rect().center()),
            )
        )
        if chosen == goto_action:
            self._goto_process_target(target)

    def _goto_process_target(self, target: dict[str, Any]) -> None:
        try:
            altitude = float(target["alt"])
            azimuth = float(target["az"]) % 360.0
        except (KeyError, TypeError, ValueError):
            return
        self._set_selected_target(dict(target))
        self.scope_camera_lock_to_target = True
        self.scope_reticle_lock_to_target = True
        manager = getattr(self.parent_widget, "_scope_ui_manager", None)
        if not self.scope_mode_enabled() and manager is not None:
            manager.activate()
        if not self.scope_mode_enabled():
            self.set_scope_enabled(True)
        self.scope_controller.set_center(
            (altitude, azimuth), confirmed=True
        )
        self.azimuth_offset = azimuth
        self.elevation_angle = max(-90.0, min(90.0, altitude))
        fov_width, fov_height = self.scope_controller.current_fov()
        target_fov = max(0.2, min(93.9, max(fov_width, fov_height)))
        self.zoom_level = max(
            float(self.zoom_level),
            max(0.5, min(140.0, 93.9 / target_fov)),
        )
        self.dragging = False
        self.setFocus(Qt.MouseFocusReason)
        tracking = getattr(
            self.parent_widget, "request_scope_tracking_update", None
        )
        if callable(tracking):
            tracking()
        self.update()

    def _apply_process_constellation_payload(
        self, payload: dict[str, Any]
    ) -> None:
        controller = self.constellation_controller
        groups = []
        for raw_group in payload.get("groups", ()) or ():
            if not isinstance(raw_group, dict):
                continue
            nodes = []
            for raw_node in raw_group.get("nodes", ()) or ():
                if not isinstance(raw_node, dict):
                    continue
                nodes.append(
                    ConstellationNode(
                        ra_deg=float(raw_node.get("ra", 0.0)),
                        dec_deg=float(raw_node.get("dec", 0.0)),
                        star_id=str(raw_node.get("star_id", "") or ""),
                        star_name=str(raw_node.get("star_name", "") or ""),
                        connect_from_prev=bool(
                            raw_node.get("connect", True)
                        ),
                    )
                )
            groups.append(
                ConstellationGroup(
                    name=str(raw_group.get("name", "") or ""),
                    nodes=nodes,
                )
            )
        controller.groups = groups
        controller.enabled = bool(payload.get("enabled", False))
        controller.visible = bool(payload.get("visible", True))
        for name in (
            "active_group_index",
            "selected_group_index",
            "selected_node_index",
            "selected_segment_index",
            "resume_from_node_index",
        ):
            value = payload.get(name)
            setattr(
                controller,
                name,
                int(value) if value is not None else None,
            )
        controller.selected_segments = {
            (int(value[0]), int(value[1]))
            for value in payload.get("selected_segments", ()) or ()
            if isinstance(value, (list, tuple)) and len(value) >= 2
        }
        controller.selected_group_indices = {
            int(value)
            for value in payload.get("selected_group_indices", ()) or ()
        }
        controller.group_drawing_active = bool(
            payload.get("group_drawing_active", False)
        )
        preview = payload.get("preview_ra_dec")
        controller.preview_ra_dec = (
            (float(preview[0]), float(preview[1]))
            if isinstance(preview, (list, tuple)) and len(preview) >= 2
            else None
        )
        controller.preview_snapped = bool(
            payload.get("preview_snapped", False)
        )
        callback = getattr(
            self.parent_widget, "_sync_constellation_controls", None
        )
        if callable(callback):
            callback()
