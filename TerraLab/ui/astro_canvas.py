"""Façana nova d'`AstroCanvas` orientada a `RenderState`.

Rendering responsibilities are composed from focused canvas mixins.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QPainter
from PyQt5.QtWidgets import QWidget
from TerraLab.astro.ephemeris_coordinator import (
    snapshot_datetime_utc,
    snapshot_matches_utc_context,
)
from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    normalize_light_pollution_mode,
    resolve_bortle_class,
)
from TerraLab.render.qt.context import RenderContext
from TerraLab.render.stars_renderer import StarsRenderResult
from TerraLab.scene.render_state import RenderState
from TerraLab.scene.scene_state import build_star_scene_state
from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.ui.canvas_mixins import (
    CanvasEphemerisRenderingMixin,
    CanvasInteractionMixin,
    CanvasProjectionAndRenderingMixin,
    CanvasSelectionAndTrailsMixin,
)
from TerraLab.ui.widget_init_helpers import astro_canvas_init
from TerraLab.widgets.spherical_math import angular_distance


class AstroCanvas(  # type: ignore[reportIncompatibleMethodOverride]  # PyQt5 stubs name Qt callback parameters a0/a1; mixins use descriptive names.
    CanvasEphemerisRenderingMixin,
    CanvasProjectionAndRenderingMixin,
    CanvasSelectionAndTrailsMixin,
    CanvasInteractionMixin,
    QWidget,
):
    """Canvas reduït: renderitza a partir d'un `RenderState` immutable."""

    request_render_signal = pyqtSignal(dict)
    request_trails_signal = pyqtSignal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Initialize the canonical canvas without relying on mixin order."""

        astro_canvas_init(self, parent)

    def shutdown(self, timeout_ms: int = 15_000) -> None:
        """Cancel render work and join the canvas-owned QThread."""

        if self._render_thread_shutdown:
            return
        self._camera_idle_timer.stop()
        self._scope_move_timer.stop()
        self._selection_pulse_timer.stop()
        self._worker.request_shutdown()
        self._thread.requestInterruption()
        self._thread.quit()
        if not self._thread.wait(max(0, int(timeout_ms))):
            raise RuntimeError(
                "Star render worker did not stop cooperatively within "
                f"{int(timeout_ms)} ms"
            )
        self._render_thread_shutdown = True

    @staticmethod
    def _target_debug_repr(target: object) -> str:
        """Construeix una representacio compacta d'un target per logs."""
        if not isinstance(target, dict):
            return repr(target)
        kind = str(target.get("kind", ""))
        if kind == "star":
            star = target.get("star")
            if isinstance(star, dict):
                return (
                    "star("
                    f"id={star.get('id', 'N/A')},"
                    f"ra={star.get('ra', 'N/A')},"
                    f"dec={star.get('dec', 'N/A')},"
                    f"mag={star.get('mag', 'N/A')}"
                    ")"
                )
            return "star(?)"
        if kind == "ngc":
            obj = target.get("obj")
            if obj is not None:
                return (
                    "ngc("
                    f"name={getattr(obj, 'name', 'N/A')},"
                    f"ra={getattr(obj, 'ra_deg', 'N/A')},"
                    f"dec={getattr(obj, 'dec_deg', 'N/A')}"
                    ")"
                )
            return "ngc(?)"
        if kind == "sky":
            return (
                "sky("
                f"type={target.get('type', 'N/A')},"
                f"key={target.get('key', 'N/A')},"
                f"alt={target.get('alt', 'N/A')},"
                f"az={target.get('az', 'N/A')}"
                ")"
            )
        return f"{kind}({target})"

    def _goto_debug_enabled(self) -> bool:
        """Indica si el tracat detallat de goto esta activat."""
        raw_value = str(os.environ.get("TERRALAB_GOTO_DEBUG", "0")).strip().lower()
        return raw_value not in {"0", "false", "off", "no"}

    def _goto_debug_log(self, message: str) -> None:
        """Emet un log de depuracio amb prefix estable."""
        if not self._goto_debug_enabled():
            return
        print(f"[GotoDebug] {str(message)}")
    def render_scene(
        self,
        painter: QPainter,
        render_state: RenderState,
    ) -> StarsRenderResult:
        """Pinta un frame usant exclusivament el `RenderState` rebut."""
        render_context = RenderContext(
            painter=painter,
            width=int(self.width()),
            height=int(self.height()),
            diagnostics=self.scene_diagnostics,
        )
        resultat_estrelles = self.sky_renderer.render(render_context, render_state)
        if not isinstance(resultat_estrelles, StarsRenderResult):
            raise TypeError(
                "SkyRenderer.render() must return StarsRenderResult, got "
                f"{type(resultat_estrelles).__name__}"
            )

        # Publish a complete selection snapshot only after the renderer result
        # has satisfied its contract. A bad frame cannot leave a half-updated
        # catalogue behind.
        self._active_catalog_ra = render_state.np_ra
        self._active_catalog_dec = render_state.np_dec
        self._active_catalog_mag = render_state.np_mag
        self._active_catalog_bp_rp = render_state.np_bp_rp
        self._active_catalog_r = render_state.np_r
        self._active_catalog_g = render_state.np_g
        self._active_catalog_b = render_state.np_b
        self._active_catalog_ids = None
        self.visible_stars = resultat_estrelles.visible_indices
        self.visible_stars_sx = resultat_estrelles.visible_sx
        self.visible_stars_sy = resultat_estrelles.visible_sy
        return resultat_estrelles

    def _show_object_context_menu(self, event):
        """Trace the contextual pick before opening the canonical menu."""
        try:
            click_sky = self.unproject_stereo(float(event.x()), float(event.y()))
            target = self._pick_target_at(float(event.x()), float(event.y()))
            self._last_goto_debug_click = {
                "sx": float(event.x()),
                "sy": float(event.y()),
                "sky": click_sky,
                "target": target,
            }
            self._goto_debug_log(
                "context_menu "
                f"s=({event.x():.1f},{event.y():.1f}) "
                f"sky={click_sky} target={self._target_debug_repr(target)}"
            )
        except Exception as exc:
            self._goto_debug_log(f"context_menu trace_error={exc}")
        return super()._show_object_context_menu(event)

    def _goto_target(self, target):
        """Override amb logs detallats i guardes de coherencia click->target."""
        if target is None:
            self._goto_debug_log("goto_target target=None")
            return

        click_ctx = getattr(self, "_last_goto_debug_click", None)
        click_sky = None
        if isinstance(click_ctx, dict):
            click_sky = click_ctx.get("sky")

        self._set_selected_target(target)
        self.scope_camera_lock_to_target = True
        ut_hour, day_of_year_utc, year_utc, _ = self._get_current_utc_context()
        target_sky = self._selected_target_sky_position(
            ut_hour, day_of_year_utc, year_utc=year_utc
        )

        self._goto_debug_log(
            "goto_target start "
            f"target={self._target_debug_repr(target)} "
            f"ut={ut_hour:.5f} day={day_of_year_utc} year={year_utc} "
            f"target_sky={target_sky} click_sky={click_sky} "
            f"cam=({self.elevation_angle:.5f},{self.azimuth_offset:.5f}) "
            f"scope_center={getattr(self.scope_controller, 'center', None)}"
        )

        if target_sky is None:
            self._goto_debug_log("goto_target abort target_sky=None")
            return

        if (
            isinstance(click_sky, tuple)
            and len(click_sky) >= 2
            and click_sky[0] is not None
            and click_sky[1] is not None
        ):
            delta_deg = float(
                angular_distance(
                    (float(click_sky[0]), float(click_sky[1])),
                    (float(target_sky[0]), float(target_sky[1])),
                )
            )
            self._goto_debug_log(
                f"goto_target click_vs_target_delta_deg={delta_deg:.6f}"
            )
            # Si el pick resol una estrella molt allunyada del punt clicat,
            # prioritzem el punt real de click per evitar salts absurds.
            if delta_deg > 25.0:
                self._goto_debug_log(
                    "goto_target mismatch>25deg -> using click_sky override"
                )
                target_sky = (float(click_sky[0]), float(click_sky[1]))

        self._ensure_scope_mode_for_shortcut()
        moved_ok = bool(self._scope_jump_to_sky(target_sky))
        self._goto_debug_log(
            "goto_target end "
            f"moved={moved_ok} "
            f"final_cam=({self.elevation_angle:.5f},{self.azimuth_offset:.5f}) "
            f"final_scope_center={getattr(self.scope_controller, 'center', None)}"
        )
    def _build_star_scene_state(
        self,
        hour,
        sun_alt,
        sun_az,
        mag_limit,
        eff_lat,
        day_of_year,
    ):
        """Build the star snapshot through the owning scene controller."""
        widget_pare = getattr(self, "parent_widget", None)
        scene_controller = getattr(widget_pare, "scene_controller", None)
        base_state = build_star_scene_state(
            self,
            hour,
            sun_alt,
            sun_az,
            mag_limit,
            eff_lat,
            day_of_year,
        )
        if scene_controller is None:
            return base_state

        dades_estrelles = self._collect_star_payload(widget_pare, base_state)
        capes_actives = self._collect_enabled_layers(widget_pare)

        scene_controller.latitude = float(
            getattr(widget_pare, "latitude", scene_controller.latitude)
        )
        scene_controller.longitude = float(
            getattr(widget_pare, "longitude", scene_controller.longitude)
        )
        scene_controller.altitude_m = float(
            getattr(widget_pare, "_observer_offset", scene_controller.altitude_m)
        )
        scene_controller.manual_year = int(
            getattr(widget_pare, "manual_year", scene_controller.manual_year)
        )
        scene_controller.manual_day = int(
            getattr(widget_pare, "manual_day", scene_controller.manual_day)
        )
        scene_controller.manual_hour = float(
            getattr(widget_pare, "manual_hour", scene_controller.manual_hour)
        )
        # `mag_limit` arriba des de `draw_stars(...)` ja precomputat pel model
        # visual (sol/twilight/eclipse + active light-pollution mode). Aquest és el valor que
        # ha d'arribar al renderer per evitar perdre el filtratge durant
        # descàrrega incremental o fallback temporal.
        mag_limit_render = None
        if mag_limit is not None:
            try:
                mag_limit_render = float(mag_limit)
            except Exception:
                mag_limit_render = None
        if mag_limit_render is None:
            try:
                mag_limit_render = float(
                    getattr(base_state, "magnitude_limit", scene_controller.mag_limit)
                )
            except Exception:
                mag_limit_render = float(scene_controller.mag_limit)
        scene_controller.mag_limit = float(mag_limit_render)
        scene_controller.light_pollution_mode = normalize_light_pollution_mode(
            getattr(widget_pare, "light_pollution_mode", LP_MODE_AUTOMATIC)
        )
        light_pollution_enabled = bool(
            getattr(widget_pare, "light_pollution_enabled", True)
        )
        selected_magnitude_limit = float(
            getattr(widget_pare, "magnitude_limit", scene_controller.mag_limit)
        )
        scene_controller.bortle = int(
            round(
                resolve_bortle_class(
                    scene_controller.light_pollution_mode,
                    automatic_bortle=getattr(
                        widget_pare, "auto_bortle_estimate", 1
                    ),
                    bortle_value=getattr(widget_pare, "bortle_value", 1),
                    magnitude_limit=selected_magnitude_limit,
                    light_pollution_enabled=light_pollution_enabled,
                )
            )
        )
        scene_controller.naked_eye_cap = float(
            getattr(base_state, "naked_eye_cap", scene_controller.naked_eye_cap)
        )
        scene_controller.pure_colors = bool(
            getattr(widget_pare, "pure_colors", scene_controller.pure_colors)
        )
        scene_controller.spike_magnitude_threshold = float(
            getattr(
                widget_pare,
                "spike_magnitude_threshold",
                scene_controller.spike_magnitude_threshold,
            )
        )
        scene_controller.star_scale = float(
            getattr(widget_pare, "star_scale", scene_controller.star_scale)
        )
        scene_controller.auto_star_scale_multiplier = float(
            getattr(
                widget_pare,
                "auto_star_scale_multiplier",
                scene_controller.auto_star_scale_multiplier,
            )
        )
        scene_controller.scope_k_fallback = float(
            getattr(widget_pare, "scope_k_fallback", scene_controller.scope_k_fallback)
        )
        scene_controller.azimuth_offset = float(getattr(self, "azimuth_offset", 0.0))
        scene_controller.elevation_angle = float(
            getattr(self, "elevation_angle", 40.0)
        )
        scene_controller.zoom_level = float(getattr(self, "zoom_level", 1.0))
        scene_controller.vertical_offset_ratio = float(
            getattr(self, "vertical_offset_ratio", 0.3)
        )
        scene_controller.scope_enabled = bool(self.scope_mode_enabled())
        scene_controller.interaction_active = bool(
            self._camera_interaction_active(
                include_time_drag=True,
                include_animation=False,
            )
            or self._scope_motion_active()
        )

        if scene_controller.scope_enabled and hasattr(self, "scope_controller"):
            centre_scope = getattr(self.scope_controller, "center", None)
            if centre_scope is not None:
                scene_controller.scope_center = (
                    float(centre_scope[0]),
                    float(centre_scope[1]),
                )
            try:
                fov_w, fov_h = self.scope_controller.current_fov()
                scene_controller.scope_fov_deg = (float(fov_w), float(fov_h))
            except Exception:
                scene_controller.scope_fov_deg = (5.0, 5.0)
        else:
            scene_controller.scope_center = None

        coordinator_terreny = getattr(widget_pare, "terrain_coordinator", None)
        perfil_terreny = None
        if coordinator_terreny is not None and callable(
            getattr(coordinator_terreny, "get_profile", None)
        ):
            try:
                perfil_terreny = coordinator_terreny.get_profile()
            except Exception:
                perfil_terreny = None
        if perfil_terreny is None:
            perfil_terreny = getattr(base_state, "horizon_profile", None)

        coordinator_ephemeris = getattr(widget_pare, "ephemeris_coordinator", None)
        snapshot_ephemeris = None
        if coordinator_ephemeris is not None and callable(
            getattr(coordinator_ephemeris, "get_snapshot", None)
        ):
            try:
                snapshot_ephemeris = coordinator_ephemeris.get_snapshot()
            except Exception:
                snapshot_ephemeris = None

        hora_utc_frame = None
        dia_utc_frame = None
        any_utc_frame = None
        try:
            hora_utc_frame, dia_utc_frame, any_utc_frame, _ = (
                self._get_current_utc_context()
            )
        except Exception:
            hora_utc_frame = None
            dia_utc_frame = None
            any_utc_frame = None

        return scene_controller.build_render_state(
            star_data=dades_estrelles,
            horizon=perfil_terreny,
            ephemeris=snapshot_ephemeris,
            sun_alt=float(sun_alt),
            sun_az=float(sun_az),
            layers_enabled=capes_actives,
            extras=dict(getattr(base_state, "extras", {}) or {}),
            ut_hour_utc=hora_utc_frame,
            day_of_year_utc=dia_utc_frame,
            year_utc=any_utc_frame,
        )

    def draw_stars_numpy(
        self,
        painter,
        hour,
        sun_alt,
        sun_az,
        mag_limit=None,
        eff_lat=None,
        day_of_year=None,
    ):
        """Render stars from the canonical immutable frame state."""
        render_state = self._build_star_scene_state(
            hour=hour,
            sun_alt=sun_alt,
            sun_az=sun_az,
            mag_limit=mag_limit,
            eff_lat=eff_lat,
            day_of_year=day_of_year,
        )
        if hasattr(self, "scene_diagnostics"):
            self.scene_diagnostics.reset()
        resultat_estrelles = self.render_scene(painter, render_state)
        if hasattr(self, "_emit_render_diagnostics"):
            self._emit_render_diagnostics(resultat_estrelles)
        return None

    def update_skyfield_cache(self, ut_hour, day_of_year, year_utc=None):
        """Refresh the render cache from the ephemeris coordinator."""
        widget_pare = getattr(self, "parent_widget", None)
        coordinator_ephemeris = getattr(widget_pare, "ephemeris_coordinator", None)
        if coordinator_ephemeris is None:
            return None
        requested_year_utc = int(
            year_utc
            if year_utc is not None
            else getattr(widget_pare, "manual_year", 2026)
        )

        def _snapshot_cache_compatible(payload) -> bool:
            if not isinstance(payload, dict):
                return False
            sun = payload.get("sun", None)
            moon = payload.get("moon", None)
            if not isinstance(sun, dict) or not isinstance(moon, dict):
                return False
            if any(k not in sun for k in ("alt", "az", "rad_deg")):
                return False
            if any(k not in moon for k in ("alt", "az", "rad_deg", "sep_real")):
                return False
            planets = payload.get("planets", [])
            if not isinstance(planets, list):
                return False
            for p in planets:
                if not isinstance(p, dict):
                    return False
                if ("alt" not in p) or ("az" not in p):
                    return False
            return True

        try:
            coordinator_ephemeris.configure_observer(
                latitude=float(getattr(widget_pare, "latitude", 0.0)),
                longitude=float(getattr(widget_pare, "longitude", 0.0)),
            )
            coordinator_ephemeris.request_snapshot(
                year_utc=requested_year_utc,
                day_of_year_utc=int(day_of_year),
                ut_hour=float(ut_hour),
            )
            snapshot = coordinator_ephemeris.get_snapshot() or {}
            if not isinstance(snapshot, dict):
                self._last_skyfield_update = 0
                return None
            if "sun" not in snapshot or "moon" not in snapshot:
                self._last_skyfield_update = 0
                return None
            if not _snapshot_cache_compatible(snapshot):
                self._last_skyfield_update = 0
                return None
            if not snapshot_matches_utc_context(
                snapshot,
                year_utc=requested_year_utc,
                day_of_year_utc=int(day_of_year),
                ut_hour=float(ut_hour),
            ):
                # Keep the previous visual snapshot until the coalesced async
                # request completes. Never run Skyfield in the GUI paint path.
                self._last_skyfield_update = 0
                return None
            snapshot_time = snapshot_datetime_utc(snapshot)
            if snapshot_time is None:
                self._last_skyfield_update = 0
                return None
            snapshot_hour = (
                snapshot_time.hour
                + snapshot_time.minute / 60.0
                + snapshot_time.second / 3600.0
                + snapshot_time.microsecond / 3_600_000_000.0
            )
            snapshot_day = (
                snapshot_time.date()
                - datetime(snapshot_time.year, 1, 1).date()
            ).days
            self._sf_cache = {
                # Cache metadata describes the data, not the newer request
                # which may still be waiting in the coordinator.
                "time": float(snapshot_hour),
                "ut_hour": float(snapshot_hour),
                "day": int(snapshot_day),
                "year": int(snapshot_time.year),
                "lat": float(getattr(widget_pare, "latitude", 0.0)),
                "lon": float(getattr(widget_pare, "longitude", 0.0)),
                "data": snapshot,
            }
            return snapshot
        except Exception:
            self._last_skyfield_update = 0
            return None

    def _collect_enabled_layers(self, widget_pare: Any) -> set[str]:
        """Deriva el conjunt de capes visibles des de l'estat actual de la UI."""
        capes_actives: set[str] = set()
        if widget_pare is None:
            return capes_actives
        if self._read_checkbox(widget_pare, "chk_enable_sky", True):
            capes_actives.add("stars")
        if self._read_checkbox(widget_pare, "chk_enable_milkyway", True):
            capes_actives.add("milkyway")
        if self._read_checkbox(widget_pare, "chk_deep_space", False):
            capes_actives.add("deep_space")
        if self._read_checkbox(widget_pare, "chk_enable_horizon", True):
            capes_actives.add("horizon")
        if self._read_checkbox(widget_pare, "chk_sun_moon", True):
            capes_actives.add("solar_system")
        if self._read_checkbox(widget_pare, "chk_planets", True):
            capes_actives.add("planets")
        if self._read_checkbox(widget_pare, "chk_clima", False):
            capes_actives.add("weather")
        return capes_actives

    def _collect_star_payload(self, widget_pare: Any, base_state: Any) -> dict[str, Any]:
        """Munta el payload d'estrelles per a `SceneController`."""
        coordinator_estrelles = getattr(widget_pare, "star_data_coordinator", None)
        if coordinator_estrelles is not None and callable(
            getattr(coordinator_estrelles, "get_active_dataset", None)
        ):
            try:
                dataset_actiu = coordinator_estrelles.get_active_dataset()
                if isinstance(dataset_actiu, dict) and int(
                    len(dataset_actiu.get("ra", []))
                ) > 0:
                    return dict(dataset_actiu)
            except Exception:
                log_suppressed_exception(__name__, "AstroCanvas._collect_star_payload")

        teseles_carregades = set()
        if coordinator_estrelles is not None and callable(
            getattr(coordinator_estrelles, "get_loaded_tiles", None)
        ):
            try:
                teseles_carregades = set(coordinator_estrelles.get_loaded_tiles())
            except Exception:
                teseles_carregades = set()

        return {
            "ra": getattr(base_state, "ra", None),
            "dec": getattr(base_state, "dec", None),
            "mag": getattr(base_state, "mag", None),
            "bp_rp": getattr(base_state, "bp_rp", None),
            "r": getattr(base_state, "color_r", None),
            "g": getattr(base_state, "color_g", None),
            "b": getattr(base_state, "color_b", None),
            "scope_mask_fn": getattr(base_state, "scope_mask_fn", None),
            "loaded_tile_ids": frozenset(teseles_carregades),
        }

    @staticmethod
    def _read_checkbox(widget_pare: Any, attribute_name: str, default: bool) -> bool:
        """Llig l'estat d'un checkbox de manera segura."""
        checkbox = getattr(widget_pare, attribute_name, None)
        if checkbox is None:
            return bool(default)
        try:
            return bool(checkbox.isChecked())
        except Exception:
            return bool(default)
