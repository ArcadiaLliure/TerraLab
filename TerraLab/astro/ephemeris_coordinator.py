"""Coordinador d'efemerides astronomiques.

Calcula snapshots de sol/lluna/planetes fora del cami de render.
"""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from TerraLab.common.app_paths import ephemeris_path

from PyQt5.QtCore import QObject, pyqtSignal

try:
    from skyfield.api import load, load_file, wgs84
except Exception:  # pragma: no cover
    load = None
    load_file = None
    wgs84 = None


EPHEMERIS_VISUAL_MAX_AGE_SECONDS = 1.5


def planet_apparent_magnitude(
    name: str,
    distance_au: float,
    phase_angle_deg: float = 0.0,
) -> float:
    """Return TerraLab's calibrated visual magnitude for a planet.

    The coefficients are the same ones used by the pre-process renderer.  The
    distance term is deliberately kept in Compute so Render only consumes the
    published photometric result.
    """

    base_magnitude = {
        "Mercury": -0.6,
        "Venus": -4.4,
        "Mars": -0.5,
        "Jupiter": -5.8,
        "Saturn": -4.3,
        "Uranus": -0.7,
        "Neptune": 0.5,
        "Pluto": 6.0,
    }.get(str(name), 0.0)
    distance = max(1e-9, float(distance_au))
    phase = max(0.0, float(phase_angle_deg))
    return float(base_magnitude + 5.0 * math.log10(distance) + 0.01 * phase)


def utc_datetime_from_context(
    year_utc: int,
    day_of_year_utc: int,
    ut_hour: float,
) -> datetime:
    """Build UTC time from TerraLab's zero-based day-of-year convention."""

    return datetime(int(year_utc), 1, 1, tzinfo=timezone.utc) + timedelta(
        days=int(day_of_year_utc),
        hours=float(ut_hour),
    )


def snapshot_datetime_utc(payload: Any) -> datetime | None:
    """Return a snapshot timestamp normalized to UTC."""

    if not isinstance(payload, dict):
        return None
    raw_timestamp = payload.get("timestamp_utc")
    if not raw_timestamp:
        return None
    try:
        text = str(raw_timestamp).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        timestamp = datetime.fromisoformat(text)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def snapshot_matches_utc_context(
    payload: Any,
    *,
    year_utc: int,
    day_of_year_utc: int,
    ut_hour: float,
    max_age_seconds: float = EPHEMERIS_VISUAL_MAX_AGE_SECONDS,
) -> bool:
    """Reject ephemerides old enough to skip a short eclipse phase."""

    timestamp = snapshot_datetime_utc(payload)
    if timestamp is None:
        return False
    requested = utc_datetime_from_context(
        year_utc,
        day_of_year_utc,
        ut_hour,
    )
    delta_seconds = abs((timestamp - requested).total_seconds())
    return bool(delta_seconds <= max(0.0, float(max_age_seconds)))


class EphemerisCoordinator(QObject):
    """Orquestra calcul d'efemerides i publica snapshots asinc."""

    ephemeris_ready = pyqtSignal(object)
    ephemeris_error = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ephemeris")
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._latitude: float = 0.0
        self._longitude: float = 0.0
        self._request_inflight: bool = False
        self._pending_request: tuple[int, int, float, float, float] | None = None
        self._last_request_key: tuple[int, int, int, int, int] | None = None
        self._last_submit_mono: float = 0.0
        self._min_submit_interval_s: float = 0.20
        self._shutdown = False

        self._ts = None
        self._eph = None
        if load is not None and load_file is not None:
            try:
                self._ts = load.timescale()
                path = ephemeris_path()
                self._eph = load_file(str(path)) if path is not None else None
            except Exception:
                self._ts = None
                self._eph = None

    def shutdown(self) -> None:
        """Atura el pool intern del coordinador."""
        with self._request_lock:
            self._shutdown = True
            self._pending_request = None
        self._executor.shutdown(wait=True, cancel_futures=True)

    def configure_observer(self, latitude: float, longitude: float) -> None:
        """Actualitza coordenades de l'observador per futurs calculs."""
        self._latitude = float(latitude)
        self._longitude = float(longitude)

    def request_snapshot(self, *, year_utc: int, day_of_year_utc: int, ut_hour: float) -> None:
        """Schedule a snapshot using a zero-based UTC day of year."""
        req = (
            int(year_utc),
            int(day_of_year_utc),
            float(ut_hour),
            float(self._latitude),
            float(self._longitude),
        )
        request_key = (
            int(req[0]),
            int(req[1]),
            int(round(float(req[2]) * 3600.0)),
            int(round(float(req[3]) * 10000.0)),
            int(round(float(req[4]) * 10000.0)),
        )
        now_mono = float(time.monotonic())
        with self._request_lock:
            if self._shutdown:
                return
            # Ignore exact duplicate requests arriving too fast.
            if (
                self._last_request_key == request_key
                and (now_mono - float(self._last_submit_mono)) < float(self._min_submit_interval_s)
            ):
                return
            if self._request_inflight:
                # Coalesce to latest request to avoid queue growth.
                self._pending_request = req
                return
            self._request_inflight = True
            self._last_request_key = request_key
            self._last_submit_mono = now_mono
        self._submit_request(req)

    def get_snapshot(self) -> dict[str, Any] | None:
        """Retorna l'ultim snapshot d'efemerides disponible."""
        with self._snapshot_lock:
            return dict(self._snapshot) if isinstance(self._snapshot, dict) else None

    def _submit_request(self, req: tuple[int, int, float, float, float]) -> None:
        try:
            self._executor.submit(self._compute_snapshot_worker, *req)
        except RuntimeError:
            with self._request_lock:
                self._request_inflight = False
                self._pending_request = None

    def _compute_snapshot_worker(
        self,
        year_utc: int,
        day_of_year_utc: int,
        ut_hour: float,
        latitude: float,
        longitude: float,
    ) -> None:
        next_request: tuple[int, int, float, float, float] | None = None
        try:
            snapshot = self._compute_snapshot(
                year_utc=year_utc,
                day_of_year_utc=day_of_year_utc,
                ut_hour=ut_hour,
                latitude=latitude,
                longitude=longitude,
            )
            with self._snapshot_lock:
                self._snapshot = snapshot
            self.ephemeris_ready.emit(snapshot)
        except Exception as exc:
            self.ephemeris_error.emit(f"Error calculant efemerides: {exc}")
        finally:
            with self._request_lock:
                if self._shutdown:
                    self._pending_request = None
                    self._request_inflight = False
                elif self._pending_request is not None:
                    next_request = self._pending_request
                    self._pending_request = None
                    self._request_inflight = True
                    self._last_submit_mono = float(time.monotonic())
                    self._last_request_key = (
                        int(next_request[0]),
                        int(next_request[1]),
                        int(round(float(next_request[2]) * 3600.0)),
                        int(round(float(next_request[3]) * 10000.0)),
                        int(round(float(next_request[4]) * 10000.0)),
                    )
                else:
                    self._request_inflight = False
            if next_request is not None:
                self._submit_request(next_request)

    def _compute_snapshot(
        self,
        *,
        year_utc: int,
        day_of_year_utc: int,
        ut_hour: float,
        latitude: float,
        longitude: float,
    ) -> dict[str, Any]:
        """Calcula snapshot astronomic per context temporal concret."""
        dt_utc = utc_datetime_from_context(
            year_utc,
            day_of_year_utc,
            ut_hour,
        )

        if self._ts is None or self._eph is None or wgs84 is None:
            # Fallback geometric simple si skyfield no esta disponible.
            sun_alt = _sun_altitude_fallback(ut_hour=ut_hour, day_of_year_utc=day_of_year_utc, latitude=latitude)
            return {
                "timestamp_utc": dt_utc.isoformat(),
                "sun": {
                    "alt": float(sun_alt),
                    "az": 0.0,
                    "rad_deg": 0.2666,
                    "dist_km": 149_597_870.0,
                },
                "moon": {
                    "alt": -20.0,
                    "az": 0.0,
                    "rad_deg": 0.2725,
                    "sep_real": 180.0,
                    "illumination": 1.0,
                    "dist_km": 384_400.0,
                },
                "planets": [],
                "eclipse_factor": 1.0,
            }

        observer = wgs84.latlon(latitude_degrees=latitude, longitude_degrees=longitude)
        t = self._ts.from_datetime(dt_utc)

        earth = self._eph["earth"]
        sun = self._eph["sun"]
        moon = self._eph["moon"]

        obs_loc = earth + observer

        ast_sun = obs_loc.at(t).observe(sun)
        ast_moon = obs_loc.at(t).observe(moon)

        app_sun = ast_sun.apparent()
        app_moon = ast_moon.apparent()
        sun_alt, sun_az, sun_dist = app_sun.altaz()
        moon_alt, moon_az, moon_dist = app_moon.altaz()
        sep_real = float(app_sun.separation_from(app_moon).degrees)
        moon_illum = float((1.0 - math.cos(math.radians(sep_real))) / 2.0)
        sun_rad_deg = math.degrees(math.atan(695_700.0 / max(1.0, float(sun_dist.km))))
        moon_rad_deg = math.degrees(math.atan(1_737.4 / max(1.0, float(moon_dist.km))))

        planets: list[dict[str, Any]] = []
        for key in (
            "mercury",
            "venus",
            "mars",
            "jupiter barycenter",
            "saturn barycenter",
            "uranus barycenter",
            "neptune barycenter",
        ):
            try:
                body = self._eph[key]
                ast = obs_loc.at(t).observe(body)
                p_alt, p_az, p_dist = ast.apparent().altaz()
                planets.append(
                    {
                        "key": key,
                        "name": key.replace(" barycenter", "").title(),
                        "alt": float(p_alt.degrees),
                        "az": float(p_az.degrees),
                        "distance_au": float(p_dist.au),
                        "phase_angle_deg": 0.0,
                        "mag": planet_apparent_magnitude(
                            key.replace(" barycenter", "").title(),
                            float(p_dist.au),
                        ),
                    }
                )
            except Exception:
                continue

        return {
            "timestamp_utc": dt_utc.isoformat(),
            "sun": {
                "alt": float(sun_alt.degrees),
                "az": float(sun_az.degrees),
                "rad_deg": float(sun_rad_deg),
                "dist_km": float(sun_dist.km),
            },
            "moon": {
                "alt": float(moon_alt.degrees),
                "az": float(moon_az.degrees),
                "rad_deg": float(moon_rad_deg),
                "sep_real": float(sep_real),
                "illumination": float(moon_illum),
                "dist_km": float(moon_dist.km),
            },
            "planets": planets,
            "eclipse_factor": 1.0,
        }


def _sun_altitude_fallback(*, ut_hour: float, day_of_year_utc: int, latitude: float) -> float:
    """Estimacio simple de l'altura solar per mode fallback."""
    declination = 23.44 * math.sin(math.radians((360.0 / 365.0) * (day_of_year_utc - 81)))
    hour_angle = (float(ut_hour) - 12.0) * 15.0
    lat_r = math.radians(float(latitude))
    dec_r = math.radians(float(declination))
    ha_r = math.radians(float(hour_angle))
    sin_alt = math.sin(lat_r) * math.sin(dec_r) + math.cos(lat_r) * math.cos(dec_r) * math.cos(ha_r)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    return math.degrees(math.asin(sin_alt))
