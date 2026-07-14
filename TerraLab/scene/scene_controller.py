"""Controlador d'escena per al bucle de 60 FPS.

No fa IO ni gestió de threads. Només actualitza estat i crea `RenderState`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import numpy as np

from TerraLab.light_pollution.modes import LP_MODE_AUTOMATIC
from TerraLab.scene.camera import Camera
from TerraLab.scene.render_state import RenderState


@dataclass
class SceneController:
    """Manté l'estat d'observador, temps i càmera per frame."""

    # Observador
    latitude: float
    longitude: float
    altitude_m: float = 0.0

    # Temps simulat
    manual_year: int = 2026
    manual_day: int = 0
    manual_hour: float = 12.0
    use_real_time: bool = True

    # Càmera
    azimuth_offset: float = 0.0
    elevation_angle: float = 40.0
    zoom_level: float = 1.0
    vertical_offset_ratio: float = 0.3

    # Scope
    scope_enabled: bool = False
    scope_center: tuple[float, float] | None = None  # (alt, az)
    scope_fov_deg: tuple[float, float] = (5.0, 5.0)

    # Condicions
    bortle: int = 1
    mag_limit: float = 8.0
    light_pollution_mode: str = LP_MODE_AUTOMATIC
    naked_eye_cap: float = 8.0
    pure_colors: bool = False
    spike_magnitude_threshold: float = 2.0
    interaction_active: bool = False
    star_scale: float = 1.0
    auto_star_scale_multiplier: float = 1.0
    scope_k_fallback: float = 0.2

    def update(self, dt_seconds: float) -> None:
        """Avança el temps simulat quan no està en temps real."""
        if bool(self.use_real_time):
            ara_utc = datetime.now(timezone.utc)
            self.manual_year = int(ara_utc.year)
            self.manual_day = int(
                (ara_utc - datetime(ara_utc.year, 1, 1, tzinfo=timezone.utc)).days
            )
            self.manual_hour = (
                float(ara_utc.hour)
                + float(ara_utc.minute) / 60.0
                + float(ara_utc.second) / 3600.0
                + float(ara_utc.microsecond) / 3_600_000_000.0
            )
            return

        delta_hores = max(0.0, float(dt_seconds)) / 3600.0
        seguent_hora = float(self.manual_hour) + delta_hores
        while seguent_hora >= 24.0:
            seguent_hora -= 24.0
            self.manual_day += 1
        self.manual_hour = seguent_hora

    def build_render_state(
        self,
        *,
        star_data: Mapping[str, Any] | None,
        horizon: object | None,
        ephemeris: dict[str, Any] | None,
        sun_alt: float = -90.0,
        sun_az: float = 0.0,
        layers_enabled: frozenset[str] | set[str] | tuple[str, ...] | None = None,
        extras: Mapping[str, Any] | None = None,
        ut_hour_utc: float | None = None,
        day_of_year_utc: int | None = None,
        year_utc: int | None = None,
    ) -> RenderState:
        """Construeix el `RenderState` immutable per al frame actual.

        Si es proporcionen els camps `*_utc`, tenen prioritat sobre el temps
        simulat intern (`manual_*`). Això evita desalineacions quan la UI
        treballa en hora local simulada però el render necessita UTC.
        """
        dades_estrelles = dict(star_data or {})

        np_ra = _as_array(
            dades_estrelles.get("ra", dades_estrelles.get("np_ra")),
            dtype=np.float32,
        )
        np_dec = _as_array(
            dades_estrelles.get("dec", dades_estrelles.get("np_dec")),
            dtype=np.float32,
        )
        np_mag = _as_array(
            dades_estrelles.get("mag", dades_estrelles.get("phot_g_mean_mag")),
            dtype=np.float32,
        )
        np_r = _as_array(
            dades_estrelles.get("r", dades_estrelles.get("color_r")),
            dtype=np.float32,
        )
        np_g = _as_array(
            dades_estrelles.get("g", dades_estrelles.get("color_g")),
            dtype=np.float32,
        )
        np_b = _as_array(
            dades_estrelles.get("b", dades_estrelles.get("color_b")),
            dtype=np.float32,
        )
        np_bp_rp = _as_array(
            dades_estrelles.get("bp_rp", dades_estrelles.get("np_bp_rp")),
            dtype=np.float32,
        )

        # Equalitza dimensions per evitar errors de render.
        total_files = min(
            len(np_ra),
            len(np_dec),
            len(np_mag),
            len(np_r),
            len(np_g),
            len(np_b),
            len(np_bp_rp),
        )
        np_ra = np_ra[:total_files]
        np_dec = np_dec[:total_files]
        np_mag = np_mag[:total_files]
        np_r = np_r[:total_files]
        np_g = np_g[:total_files]
        np_b = np_b[:total_files]
        np_bp_rp = np_bp_rp[:total_files]

        for array in (np_ra, np_dec, np_mag, np_r, np_g, np_b, np_bp_rp):
            try:
                array.setflags(write=False)
            except Exception:
                pass

        estat_camera = Camera(
            azimuth_offset=float(self.azimuth_offset),
            elevation_angle=float(self.elevation_angle),
            zoom_level=float(self.zoom_level),
            vertical_offset_ratio=float(self.vertical_offset_ratio),
        )

        loaded_tile_ids = frozenset(
            dades_estrelles.get("loaded_tile_ids", set()) or set()
        )

        centre_scope = None
        if self.scope_center is not None:
            centre_scope = (
                float(self.scope_center[0]),
                float(self.scope_center[1]),
            )

        extras_combinats = dict(extras or {})
        extras_legacy = dades_estrelles.get("extras")
        if isinstance(extras_legacy, Mapping):
            extras_combinats.update(extras_legacy)

        if "scope_mask_fn" not in extras_combinats:
            mascara_scope = dades_estrelles.get("scope_mask_fn")
            if callable(mascara_scope):
                extras_combinats["scope_mask_fn"] = mascara_scope

        snapshot_ephemeris = (
            dict(ephemeris or {}) if isinstance(ephemeris, dict) else ephemeris
        )

        # `manual_hour/manual_day/manual_year` poden estar en hora local simulada.
        # Quan arriba context UTC explícit, el fem servir per al pipeline astronòmic.
        ut_hour_frame = (
            float(ut_hour_utc) if ut_hour_utc is not None else float(self.manual_hour)
        )
        day_of_year_frame = (
            int(day_of_year_utc)
            if day_of_year_utc is not None
            else int(self.manual_day)
        )
        year_frame = (
            int(year_utc) if year_utc is not None else int(self.manual_year)
        )

        return RenderState(
            ut_hour=ut_hour_frame,
            day_of_year_utc=day_of_year_frame,
            year_utc=year_frame,
            latitude=float(self.latitude),
            longitude=float(self.longitude),
            altitude_m=float(self.altitude_m),
            camera=estat_camera,
            np_ra=np_ra,
            np_dec=np_dec,
            np_mag=np_mag,
            np_r=np_r,
            np_g=np_g,
            np_b=np_b,
            np_bp_rp=np_bp_rp,
            loaded_tile_ids=loaded_tile_ids,
            horizon_profile=horizon,
            ephemeris_snapshot=snapshot_ephemeris,
            bortle=int(self.bortle),
            mag_limit=float(self.mag_limit),
            light_pollution_mode=str(self.light_pollution_mode),
            naked_eye_cap=float(self.naked_eye_cap),
            sun_alt=float(sun_alt),
            sun_az=float(sun_az),
            layers_enabled=frozenset(layers_enabled or ()),
            extras=extras_combinats,
            pure_colors=bool(self.pure_colors),
            spike_magnitude_threshold=float(self.spike_magnitude_threshold),
            interaction_active=bool(self.interaction_active),
            star_scale=float(self.star_scale),
            auto_star_scale_multiplier=float(self.auto_star_scale_multiplier),
            scope_k_fallback=float(self.scope_k_fallback),
            scope_enabled=bool(self.scope_enabled),
            scope_center_sky=centre_scope,
            scope_fov_deg=(
                float(self.scope_fov_deg[0]),
                float(self.scope_fov_deg[1]),
            ),
        )


def _as_array(raw: Any, *, dtype) -> np.ndarray:
    """Converteix una entrada a `ndarray` amb fallback buit."""
    if raw is None:
        return np.empty(0, dtype=dtype)
    try:
        array = np.asarray(raw, dtype=dtype)
        if array.ndim == 0:
            return array.reshape(1)
        return array.reshape(-1)
    except Exception:
        return np.empty(0, dtype=dtype)
