"""Canonical QImage renderer used only by the render process."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)

from TerraLab.astro.ephemeris_coordinator import (
    planet_apparent_magnitude,
    snapshot_matches_utc_context,
)
from TerraLab.astro.ngc_catalog import ngc_display_label
from TerraLab.data.catalogs.star_catalog import _bp_rp_to_rgb_arrays
from TerraLab.debug.diagnostics import Diagnostics
from TerraLab.light_pollution.modes import is_automatic_mode
from TerraLab.render.qt.context import RenderContext
from TerraLab.render.sky.milkyway_overlay import MilkyWayOverlay
from TerraLab.render.sky_renderer import sky_color_phys
from TerraLab.render.stars_renderer import StarsRenderer
from TerraLab.scene.camera import Camera
from TerraLab.scene.projection import (
    project_universal_stereo_numpy,
    project_universal_stereo_point,
    radec_to_altaz_numpy,
    unproject_universal_stereo_point,
)
from TerraLab.scene.render_state import RenderState
from TerraLab.terrain.overlay import HorizonOverlay
from TerraLab.terrain.render.config import TerrainCelestialLightContext
from TerraLab.weather.system import WeatherSystem
from TerraLab.widgets.telescope_scope_mode import TelescopeScopeController
from TerraLab.widgets.visual_magnitude_engine import (
    VisualMagnitudeEngine,
    VisualMagnitudeInputs,
)
from TerraLab.widgets.measurement_tools import (
    TOOL_NONE,
    MeasurementController,
)
from TerraLab.widgets.constellation_drawing import (
    ConstellationDrawingController,
    ConstellationGroup,
    ConstellationNode,
)
from TerraLab.widgets.spherical_math import altaz_to_ra_dec


_RENDER_FONT_READY = False


def ensure_render_font_available() -> bool:
    """Install one system font for Qt's platform-independent paint engine."""

    global _RENDER_FONT_READY
    if QFontDatabase().families():
        _RENDER_FONT_READY = True
        return True
    # A QGuiApplication can be torn down and recreated in lifecycle tests.
    # Font registrations belong to that application, not to this module.
    _RENDER_FONT_READY = False

    windows_root = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windows_root / "Fonts" / "segoeui.ttf",
        windows_root / "Fonts" / "arial.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/System/Library/Fonts/Helvetica.ttc"),
    )
    for candidate in candidates:
        if (
            candidate.is_file()
            and QFontDatabase.addApplicationFont(str(candidate)) >= 0
        ):
            _RENDER_FONT_READY = True
            return True
    return False


def fast_sun_altaz(
    ut_hour: float, day_of_year_utc: int, latitude: float, longitude: float, year_utc: int = 2026
) -> tuple[float, float]:
    """Fast analytical solar position (altitude, azimuth) in degrees using J2000 orbital elements."""
    try:
        dt = datetime(int(year_utc), 1, 1, tzinfo=timezone.utc) + timedelta(days=int(day_of_year_utc), hours=float(ut_hour))
    except Exception:
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=int(day_of_year_utc), hours=float(ut_hour))
    d = (dt - datetime(2000, 1, 1, 12, 0, tzinfo=timezone.utc)).total_seconds() / 86400.0

    L_sun = (280.460 + 0.9856474 * d) % 360.0
    M_sun = (357.528 + 0.9856003 * d) % 360.0
    l_sun = (L_sun + 1.915 * math.sin(math.radians(M_sun)) + 0.020 * math.sin(math.radians(2 * M_sun))) % 360.0

    eps = 23.439 - 0.0000004 * d
    eps_r = math.radians(eps)
    l_r = math.radians(l_sun)

    sin_dec = math.sin(eps_r) * math.sin(l_r)
    sin_dec = max(-1.0, min(1.0, sin_dec))
    dec_deg = math.degrees(math.asin(sin_dec))

    y_ra = math.cos(eps_r) * math.sin(l_r)
    x_ra = math.cos(l_r)
    ra_deg = math.degrees(math.atan2(y_ra, x_ra)) % 360.0

    lst_deg = (280.46061837 + 360.98564736629 * d + longitude) % 360.0
    ha_deg = (lst_deg - ra_deg) % 360.0

    lat_r = math.radians(latitude)
    dec_r = math.radians(dec_deg)
    ha_r = math.radians(ha_deg)

    sin_alt = math.sin(lat_r) * math.sin(dec_r) + math.cos(lat_r) * math.cos(dec_r) * math.cos(ha_r)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt_deg = math.degrees(math.asin(sin_alt))

    cos_az = (math.sin(dec_r) - math.sin(lat_r) * sin_alt) / (math.cos(lat_r) * math.cos(math.radians(alt_deg)) + 1e-10)
    cos_az = max(-1.0, min(1.0, cos_az))
    az_deg = math.degrees(math.acos(cos_az))
    if math.sin(ha_r) > 0:
        az_deg = 360.0 - az_deg

    return alt_deg, az_deg


def fast_moon_altaz(
    ut_hour: float, day_of_year_utc: int, latitude: float, longitude: float, year_utc: int = 2026
) -> tuple[float, float, float]:
    """Fast analytical moon position (altitude, azimuth, illumination 0..1) using J2000 orbital elements."""
    try:
        dt = datetime(int(year_utc), 1, 1, tzinfo=timezone.utc) + timedelta(days=int(day_of_year_utc), hours=float(ut_hour))
    except Exception:
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=int(day_of_year_utc), hours=float(ut_hour))
    d = (dt - datetime(2000, 1, 1, 12, 0, tzinfo=timezone.utc)).total_seconds() / 86400.0

    L = (218.3164477 + 13.1763965268 * d) % 360.0
    M_moon = (134.9633964 + 13.0649929509 * d) % 360.0
    F = (93.2720950 + 13.2293502605 * d) % 360.0
    D = (297.8501921 + 12.1907491174 * d) % 360.0
    M_sun = (357.5291092 + 0.9856002831 * d) % 360.0

    l_moon = (L + 6.289 * math.sin(math.radians(M_moon))
              - 1.274 * math.sin(math.radians(2*D - M_moon))
              + 0.658 * math.sin(math.radians(2*D))
              - 0.186 * math.sin(math.radians(M_sun))) % 360.0
    b_moon = (5.128 * math.sin(math.radians(F))
              + 0.280 * math.sin(math.radians(M_moon + F))
              + 0.278 * math.sin(math.radians(F - M_moon)))

    elongation = D + 6.289 * math.sin(math.radians(M_moon)) - 2.100 * math.sin(math.radians(M_sun))
    illum = (1.0 - math.cos(math.radians(elongation))) / 2.0
    illum = max(0.0, min(1.0, float(illum)))

    eps = 23.439 - 0.0000004 * d
    eps_r = math.radians(eps)
    l_r = math.radians(l_moon)
    b_r = math.radians(b_moon)

    sin_dec = math.sin(b_r) * math.cos(eps_r) + math.cos(b_r) * math.sin(eps_r) * math.sin(l_r)
    sin_dec = max(-1.0, min(1.0, sin_dec))
    dec_deg = math.degrees(math.asin(sin_dec))

    y_ra = math.sin(l_r) * math.cos(eps_r) - math.tan(b_r) * math.sin(eps_r)
    x_ra = math.cos(l_r)
    ra_deg = math.degrees(math.atan2(y_ra, x_ra)) % 360.0

    lst_deg = (280.46061837 + 360.98564736629 * d + longitude) % 360.0
    ha_deg = (lst_deg - ra_deg) % 360.0

    lat_r = math.radians(latitude)
    dec_r = math.radians(dec_deg)
    ha_r = math.radians(ha_deg)

    sin_alt = math.sin(lat_r) * math.sin(dec_r) + math.cos(lat_r) * math.cos(dec_r) * math.cos(ha_r)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt_deg = math.degrees(math.asin(sin_alt))

    cos_az = (math.sin(dec_r) - math.sin(lat_r) * sin_alt) / (math.cos(lat_r) * math.cos(math.radians(alt_deg)) + 1e-10)
    cos_az = max(-1.0, min(1.0, cos_az))
    az_deg = math.degrees(math.acos(cos_az))
    if math.sin(ha_r) > 0:
        az_deg = 360.0 - az_deg

    return alt_deg, az_deg, illum


def angular_separation_deg(
    alt_a: float,
    az_a: float,
    alt_b: float,
    az_b: float,
) -> float:
    """Great-circle separation for two horizontal coordinates."""

    alt_a_r = math.radians(float(alt_a))
    alt_b_r = math.radians(float(alt_b))
    delta_az_r = math.radians(float(az_a) - float(az_b))
    cosine = (
        math.sin(alt_a_r) * math.sin(alt_b_r)
        + math.cos(alt_a_r) * math.cos(alt_b_r) * math.cos(delta_az_r)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def standard_refracted_altitude_deg(true_altitude_deg: float) -> float:
    """Map geometric altitude to apparent altitude in a standard atmosphere.

    This is Skyfield's iterative USNO-style correction evaluated at 10 C and
    1010 mbar.  Skyfield returns zero refraction below -1 degree and close to
    the zenith; matching those bounds prevents below-horizon bodies from being
    displaced indefinitely.
    """

    true_altitude = float(true_altitude_deg)
    if not -1.0 <= true_altitude <= 89.9:
        return true_altitude
    apparent_altitude = true_altitude
    atmosphere_scale = 0.28 * 1010.0 / (10.0 + 273.0)
    for _iteration in range(12):
        if not -1.0 <= apparent_altitude <= 89.9:
            next_altitude = true_altitude
            if abs(next_altitude - apparent_altitude) <= 3e-5:
                apparent_altitude = next_altitude
                break
            apparent_altitude = next_altitude
            continue
        denominator = apparent_altitude + 4.4
        if abs(denominator) < 1e-9:
            break
        angle_deg = apparent_altitude + 7.31 / denominator
        tangent = math.tan(math.radians(angle_deg))
        if abs(tangent) < 1e-12:
            break
        correction = 0.016667 / tangent * atmosphere_scale
        next_altitude = true_altitude + correction
        if abs(next_altitude - apparent_altitude) <= 3e-5:
            apparent_altitude = next_altitude
            break
        apparent_altitude = next_altitude
    return float(apparent_altitude)


def standard_refraction_vertical_scale(true_altitude_deg: float) -> float:
    """Return local vertical compression caused by differential refraction."""

    altitude = float(true_altitude_deg)
    if not -1.0 <= altitude <= 89.9:
        return 1.0
    epsilon = 0.01
    lower = standard_refracted_altitude_deg(max(-0.999, altitude - epsilon))
    upper = standard_refracted_altitude_deg(min(89.899, altitude + epsilon))
    derivative = (upper - lower) / (2.0 * epsilon)
    if not math.isfinite(derivative):
        return 1.0
    # The point-source model has a hard validity boundary at -1 degree.  A
    # bounded local derivative preserves the expected flattened limb without
    # turning an extended disc into an artefact at that boundary.
    return max(0.55, min(1.0, float(derivative)))


@dataclass(frozen=True)
class RefractedDiscGeometry:
    x: float
    y: float
    radius_x: float
    radius_y: float
    apparent_altitude_deg: float


def solar_disc_transmission(
    separation_deg: float,
    sun_radius_deg: float,
    moon_radius_deg: float,
) -> float:
    """Return the unobscured fraction of the physical solar disc."""

    distance = max(0.0, float(separation_deg))
    sun_radius = max(1e-9, float(sun_radius_deg))
    moon_radius = max(1e-9, float(moon_radius_deg))
    if distance >= sun_radius + moon_radius:
        return 1.0
    if distance <= abs(sun_radius - moon_radius):
        covered = (
            math.pi * min(sun_radius, moon_radius) ** 2
            if moon_radius < sun_radius
            else math.pi * sun_radius**2
        )
    else:
        sun_term = (
            distance**2 + sun_radius**2 - moon_radius**2
        ) / (2.0 * distance * sun_radius)
        moon_term = (
            distance**2 + moon_radius**2 - sun_radius**2
        ) / (2.0 * distance * moon_radius)
        sun_angle = math.acos(max(-1.0, min(1.0, sun_term)))
        moon_angle = math.acos(max(-1.0, min(1.0, moon_term)))
        radical = max(
            0.0,
            (-distance + sun_radius + moon_radius)
            * (distance + sun_radius - moon_radius)
            * (distance - sun_radius + moon_radius)
            * (distance + sun_radius + moon_radius),
        )
        covered = (
            sun_radius**2 * sun_angle
            + moon_radius**2 * moon_angle
            - 0.5 * math.sqrt(radical)
        )
    occulted = covered / (math.pi * sun_radius**2)
    return max(0.0, min(1.0, 1.0 - occulted))


def daylight_moon_alpha(
    *,
    illumination: float,
    elongation_deg: float,
    moon_alt_deg: float,
    sun_alt_deg: float,
    eclipsing: bool = False,
) -> float:
    """Approximate naked-eye lunar contrast against a daylight sky."""

    altitude = float(moon_alt_deg)
    if bool(eclipsing):
        return 1.0
    if float(sun_alt_deg) <= -6.0:
        return 1.0

    illumination = max(0.0, min(1.0, float(illumination)))
    elongation = max(0.0, min(180.0, float(elongation_deg)))
    if illumination < 0.003 or elongation < 4.0:
        return 0.0

    phase_contrast = illumination**0.42
    separation_contrast = 0.28 + 0.72 * min(
        1.0, max(0.0, (elongation - 4.0) / 55.0)
    )
    altitude_contrast = 0.32 + 0.68 * min(
        1.0, max(0.0, (altitude + 0.8) / 18.0)
    )
    daylight = min(1.0, max(0.0, (float(sun_alt_deg) + 6.0) / 36.0))
    contrast = phase_contrast * separation_contrast * altitude_contrast
    threshold = 0.09 + 0.08 * daylight
    if contrast <= threshold:
        return 0.0
    return max(0.14, min(0.88, 0.14 + (contrast - threshold) * 1.25))


_EMPTY_F32 = np.empty(0, dtype=np.float32)


class _PayloadWeatherProvider:
    """Render-side forecast source populated only by Compute artifacts."""

    def __init__(self) -> None:
        self._samples: dict[tuple[int, int, int], dict] = {}
        self._status = "compute_pending"
        self.user_agent = "compute-owned"

    def replace(self, artifact: object) -> None:
        payload = artifact if isinstance(artifact, dict) else {}
        samples: dict[tuple[int, int, int], dict] = {}
        for item in payload.get("samples", ()) or ():
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not isinstance(value, dict):
                continue
            key = (
                int(item.get("year", 0)),
                int(item.get("day", 0)),
                int(item.get("hour", 0)),
            )
            samples[key] = dict(value)
        self._samples = samples
        self._status = str(
            payload.get("status", "compute_pending") or "compute_pending"
        )

    def get_weather(self, year: int, day: int, hour: int):
        value = self._samples.get((int(year), int(day), int(hour)))
        return dict(value) if isinstance(value, dict) else None

    def get_last_status(self) -> str:
        return self._status

    def set_location(self, *_args) -> None:
        return

    def set_remote_enabled(self, *_args) -> None:
        return

    def set_cache_enabled(self, *_args) -> None:
        return

    def set_user_agent(self, value: str) -> None:
        self.user_agent = str(value or "")

    def get_cache_path(self):
        return None

    def shutdown(self) -> None:
        return


class CatalogCache:
    """Keep one read-only catalog artifact resident in the render process."""

    def __init__(self) -> None:
        self._key: tuple[Any, ...] | None = None
        self._handles: list[Any] = []
        self.arrays = self._empty()

    @staticmethod
    def _empty() -> dict[str, np.ndarray]:
        return {
            key: _EMPTY_F32
            for key in ("ra", "dec", "mag", "bp_rp", "r", "g", "b")
        }

    def close(self) -> None:
        self.arrays = self._empty()
        handles, self._handles = self._handles, []
        self._key = None
        for handle in handles:
            close = getattr(handle, "close", None)
            if callable(close):
                close()

    def load(self, artifact: dict[str, Any] | None) -> dict[str, np.ndarray]:
        artifact = artifact if isinstance(artifact, dict) else {}
        key = tuple(
            str(artifact.get(name, "") or "")
            for name in ("catalog_path", "r_path", "g_path", "b_path")
        )
        if key == self._key:
            return self.arrays
        self.close()
        catalog_path = key[0]
        if not catalog_path or not Path(catalog_path).is_file():
            return self.arrays

        loaded = np.load(catalog_path, mmap_mode="r", allow_pickle=False)
        self._handles.append(loaded)
        names = tuple(getattr(getattr(loaded, "dtype", None), "names", ()) or ())
        if names:
            ra = np.asarray(loaded["ra"])
            dec = np.asarray(loaded["dec"])
            mag_key = (
                "phot_g_mean_mag"
                if "phot_g_mean_mag" in names
                else "mag"
            )
            mag = np.asarray(loaded[mag_key])
            bp = (
                np.asarray(loaded["bp_rp"])
                if "bp_rp" in names
                else np.full(len(ra), 0.8, dtype=np.float32)
            )
        else:
            files = set(getattr(loaded, "files", ()))
            ra = np.asarray(loaded["ra" if "ra" in files else "RA"])
            dec = np.asarray(loaded["dec" if "dec" in files else "DEC"])
            mag_key = (
                "mag"
                if "mag" in files
                else "phot_g_mean_mag"
            )
            mag = np.asarray(loaded[mag_key])
            bp = (
                np.asarray(loaded["bp_rp"])
                if "bp_rp" in files
                else np.full(len(ra), 0.8, dtype=np.float32)
            )

        channels = []
        for path in key[1:]:
            if path and Path(path).is_file():
                channel = np.load(path, mmap_mode="r", allow_pickle=False)
                self._handles.append(channel)
                channels.append(np.asarray(channel))
            else:
                channels = []
                break
        if len(channels) != 3:
            channels = list(_bp_rp_to_rgb_arrays(bp))

        total = min(
            len(ra),
            len(dec),
            len(mag),
            len(bp),
            *(len(channel) for channel in channels),
        )
        self.arrays = {
            "ra": np.asarray(ra[:total]),
            "dec": np.asarray(dec[:total]),
            "mag": np.asarray(mag[:total]),
            "bp_rp": np.asarray(bp[:total]),
            "r": np.asarray(channels[0][:total]),
            "g": np.asarray(channels[1][:total]),
            "b": np.asarray(channels[2][:total]),
        }
        self._key = key
        return self.arrays


class NgcCache:
    """Keep the immutable deep-sky structured array memory-mapped."""

    def __init__(self) -> None:
        self._path = ""
        self._array = None

    def close(self) -> None:
        array, self._array = self._array, None
        self._path = ""
        mmap = getattr(array, "_mmap", None)
        if mmap is not None:
            mmap.close()

    def load(self, artifact: object):
        payload = artifact if isinstance(artifact, dict) else {}
        path = str(payload.get("catalog_path", "") or "")
        if not path:
            fallback_csv = Path(__file__).resolve().parents[1] / "data" / "sky" / "openngc_catalog.csv"
            if not fallback_csv.is_file():
                fallback_csv = Path(__file__).resolve().parents[1] / "data" / "openngc_catalog.csv"
            if fallback_csv.is_file():
                path = str(fallback_csv)
        if path == self._path and self._array is not None:
            return self._array
        self.close()
        if path and Path(path).is_file():
            if path.endswith(".csv"):
                from TerraLab.astro.ngc_catalog import load_ngc_catalog
                items = load_ngc_catalog(path)
                if items:
                    dt = np.dtype([
                        ("name", "U32"),
                        ("kind", "U16"),
                        ("ra", "f4"),
                        ("dec", "f4"),
                         ("mag", "f4"),
                         ("maj", "f4"),
                         ("min", "f4"),
                         ("pa", "f4"),
                    ])
                    records = np.zeros(len(items), dtype=dt)
                    for idx, item in enumerate(items):
                        records[idx] = (
                            ngc_display_label(item),
                            str(
                                getattr(item, "obj_type", None)
                                or getattr(item, "type", None)
                                or getattr(item, "kind", None)
                                or "G"
                            ),
                            float(getattr(item, "ra_deg", 0.0) or 0.0),
                            float(getattr(item, "dec_deg", 0.0) or 0.0),
                            float(item.effective_mag if getattr(item, "effective_mag", None) is not None else 10.0),
                            float(
                                getattr(
                                    item,
                                    "maj_deg",
                                    (
                                        float(
                                            getattr(
                                                item,
                                                "maj_ax_arcmin",
                                                6.0,
                                            )
                                            or 6.0
                                        )
                                        / 60.0
                                    ),
                                )
                                or 0.1
                            ),
                            float(
                                getattr(
                                    item,
                                    "min_deg",
                                    (
                                        float(
                                            getattr(
                                                item,
                                                "min_ax_arcmin",
                                                6.0,
                                            )
                                            or 6.0
                                        )
                                        / 60.0
                                    ),
                                )
                                or 0.1
                            ),
                            float(
                                getattr(item, "pos_ang_deg", 0.0) or 0.0
                            ),
                        )
                    self._array = records
                    self._path = path
            else:
                self._array = np.load(
                    path, mmap_mode="r", allow_pickle=False
                )
                self._path = path
        return self._array


class OffscreenSceneRenderer:
    """Render the complete process-owned scene into a caller-provided image."""

    def __init__(self) -> None:
        ensure_render_font_available()
        self.catalog = CatalogCache()
        self.ngc = NgcCache()
        self.stars = StarsRenderer()
        self.milkyway = MilkyWayOverlay()
        self.terrain: HorizonOverlay | None = None
        self._terrain_path = ""
        self._terrain_surface_path = ""
        self._background_key = None
        self._background = None
        self.diagnostics = Diagnostics()
        self.weather: WeatherSystem | None = None
        self._weather_sample_revision = -1
        self.scope = TelescopeScopeController()
        self.measurements = MeasurementController()
        self._measurement_revision = -1
        self.constellations = ConstellationDrawingController("")
        self._constellation_payload_cache: dict = {}
        self._last_pick_state: RenderState | None = None
        self._last_pick_result = None
        self._last_pick_size = (1, 1)
        self._visible_ngc: list[dict] = []
        self._visible_sky_objects: list[dict] = []
        self._occupied_labels: list[QRectF] = []
        self._trail_cache_key = None
        self._trail_image: QImage | None = None
        self._last_diagnostics_log_time = 0.0

    def close(self) -> None:
        self.catalog.close()
        self.ngc.close()
        self.terrain = None
        if self.weather is not None:
            self.weather.shutdown()
            self.weather = None

    def render(
        self,
        painter: QPainter,
        width: int,
        height: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        width = max(1, int(width))
        height = max(1, int(height))
        self.diagnostics.reset()
        self.diagnostics.start_timer("renderer_total")
        camera = self._camera(payload)
        ut_hour = float(payload.get("ut_hour", 12.0))
        day_of_year_utc = int(payload.get("day_of_year_utc", 0))
        year_utc = int(payload.get("year_utc", 2026))
        latitude = float(payload.get("latitude", 0.0))
        longitude = float(payload.get("longitude", 0.0))

        ephemeris_payload = payload.get("ephemeris")
        ephemeris = (
            dict(ephemeris_payload)
            if isinstance(ephemeris_payload, dict)
            else {}
        )
        for body_name in ("sun", "moon"):
            body = ephemeris.get(body_name)
            if isinstance(body, dict):
                ephemeris[body_name] = dict(body)

        fast_s_alt, fast_s_az = fast_sun_altaz(
            ut_hour, day_of_year_utc, latitude, longitude, year_utc=year_utc
        )
        snapshot_valid = snapshot_matches_utc_context(
            ephemeris,
            year_utc=year_utc,
            day_of_year_utc=day_of_year_utc,
            ut_hour=ut_hour,
        )

        has_scientific_snapshot = bool(
            ephemeris.get("timestamp_utc")
            and isinstance(ephemeris.get("sun"), dict)
            and isinstance(ephemeris.get("moon"), dict)
            and all(
                key in ephemeris[body_name]
                for body_name in ("sun", "moon")
                for key in ("alt", "az")
            )
        )
        if (
            snapshot_valid or has_scientific_snapshot
        ) and isinstance(ephemeris.get("sun"), dict):
            # When Compute is still resolving a newly selected time, keep the
            # last internally coherent scientific snapshot. Mixing analytical
            # fallback coordinates with its old separation made intermediate
            # eclipse phases disappear or jump.
            sun_alt = float(ephemeris["sun"].get("alt", fast_s_alt))
            sun_az = float(ephemeris["sun"].get("az", fast_s_az))
        else:
            sun_alt = fast_s_alt
            sun_az = fast_s_az
            if not isinstance(ephemeris.get("sun"), dict):
                ephemeris["sun"] = {}
            ephemeris["sun"]["alt"] = sun_alt
            ephemeris["sun"]["az"] = sun_az
            ephemeris["sun"]["rad_deg"] = 0.2666

            if not snapshot_valid:
                m_alt, m_az, m_illum = fast_moon_altaz(
                    ut_hour, day_of_year_utc, latitude, longitude, year_utc=year_utc
                )
                if not isinstance(ephemeris.get("moon"), dict):
                    ephemeris["moon"] = {}
                ephemeris["moon"]["alt"] = m_alt
                ephemeris["moon"]["az"] = m_az
                ephemeris["moon"]["rad_deg"] = 0.2725
                ephemeris["moon"]["illumination"] = m_illum

        sun_data = (
            ephemeris.get("sun")
            if isinstance(ephemeris.get("sun"), dict)
            else {}
        )
        moon_data = (
            ephemeris.get("moon")
            if isinstance(ephemeris.get("moon"), dict)
            else {}
        )
        moon_alt = float(moon_data.get("alt", -90.0))
        moon_az = float(moon_data.get("az", 0.0))
        geometric_separation = angular_separation_deg(
            sun_alt, sun_az, moon_alt, moon_az
        )
        separation = float(
            moon_data.get("sep_real", geometric_separation)
            if has_scientific_snapshot
            else geometric_separation
        )
        sun_radius_deg = max(
            1e-6, float(sun_data.get("rad_deg", 0.2666))
        )
        moon_radius_deg = max(
            1e-6, float(moon_data.get("rad_deg", 0.2725))
        )
        eclipse_factor = (
            solar_disc_transmission(
                separation, sun_radius_deg, moon_radius_deg
            )
            if sun_alt > -2.0 and moon_alt > -2.0
            else 1.0
        )
        ephemeris["eclipse_factor"] = float(eclipse_factor)
        moon_data["sep_real"] = float(separation)

        arrays = self.catalog.load(payload.get("catalog"))
        layers = frozenset(str(v) for v in payload.get("layers", ()))
        extras = dict(payload.get("extras", {}))
        extras["stars_enabled"] = "stars" in layers
        trail_settings = payload.get("trails")
        trail_settings = (
            trail_settings if isinstance(trail_settings, dict) else {}
        )
        try:
            trail_elapsed = abs(
                (
                    float(ut_hour)
                    - float(trail_settings.get("start_hour"))
                    + 12.0
                )
                % 24.0
                - 12.0
            )
        except (TypeError, ValueError):
            trail_elapsed = 0.0
        extras["suppress_star_points"] = bool(
            trail_settings.get("enabled", False)
            and sun_alt <= -6.0
            and trail_elapsed > 1e-4
        )
        extras["milkyway_overlay"] = dict(
            payload.get("milkyway", {})
        )
        extras["eclipse_factor"] = float(eclipse_factor)
        self._prepare_scope_extras(
            extras,
            payload,
            sun_alt=sun_alt,
            catalog_rows=len(arrays["ra"]),
        )
        state = RenderState(
            ut_hour=float(payload.get("ut_hour", 12.0)),
            day_of_year_utc=int(payload.get("day_of_year_utc", 0)),
            year_utc=int(payload.get("year_utc", 2026)),
            latitude=float(payload.get("latitude", 0.0)),
            longitude=float(payload.get("longitude", 0.0)),
            altitude_m=float(payload.get("altitude_m", 0.0)),
            camera=camera,
            np_ra=arrays["ra"],
            np_dec=arrays["dec"],
            np_mag=arrays["mag"],
            np_r=arrays["r"],
            np_g=arrays["g"],
            np_b=arrays["b"],
            np_bp_rp=arrays["bp_rp"],
            ephemeris_snapshot=ephemeris,
            bortle=int(payload.get("bortle", 1)),
            mag_limit=self._magnitude_limit(
                payload, sun_alt, eclipse_factor
            ),
            light_pollution_mode=str(
                payload.get("light_pollution_mode", "automatic")
            ),
            naked_eye_cap=float(payload.get("naked_eye_cap", 8.0)),
            sun_alt=sun_alt,
            sun_az=sun_az,
            layers_enabled=layers,
            extras=extras,
            pure_colors=bool(payload.get("pure_colors", False)),
            spike_magnitude_threshold=float(
                payload.get("spike_magnitude_threshold", 2.0)
            ),
            interaction_active=bool(
                payload.get("interaction_active", False)
            ),
            star_scale=float(payload.get("star_scale", 1.0)),
            auto_star_scale_multiplier=float(
                payload.get("auto_star_scale_multiplier", 1.0)
            ),
            scope_k_fallback=float(payload.get("scope_k_fallback", 0.2)),
            scope_enabled=bool(payload.get("scope_enabled", False)),
            scope_center_sky=self._optional_pair(
                payload.get("scope_center_sky")
            ),
            scope_fov_deg=self._pair(
                payload.get("scope_fov_deg"), (5.0, 5.0)
            ),
        )

        painter.setRenderHint(
            QPainter.Antialiasing, not state.interaction_active
        )
        painter.setRenderHint(
            QPainter.TextAntialiasing, not state.interaction_active
        )
        self.diagnostics.start_timer("renderer_background")
        self._draw_background(painter, width, height, state)
        self.diagnostics.stop_timer("renderer_background")
        context = RenderContext(
            painter=painter,
            width=width,
            height=height,
            diagnostics=self.diagnostics,
        )
        if "milkyway" in layers:
            self.milkyway.render(context, state)
        if "grid" in layers:
            self._draw_grid(painter, width, height, state)
        self._draw_compass(painter, width, height, state)
        self._draw_trails(painter, width, height, state, payload)
        self.diagnostics.start_timer("renderer_stars")
        star_result = self.stars.render(context, state)
        self.diagnostics.stop_timer("renderer_stars")
        self._visible_sky_objects = []
        self._occupied_labels = []
        self._last_pick_state = state
        self._last_pick_result = star_result
        self._last_pick_size = (width, height)
        self.diagnostics.start_timer("renderer_overlays")
        self._draw_ephemeris(painter, width, height, state)
        self._draw_ngc(painter, width, height, state, payload)
        self.diagnostics.stop_timer("renderer_overlays")
        if "terrain" in layers:
            self.diagnostics.start_timer("renderer_terrain")
            self._draw_terrain(painter, width, height, state, payload)
            self.diagnostics.stop_timer("renderer_terrain")
        self._draw_weather(painter, width, height, state, payload)
        self._draw_scope(painter, width, height, state, payload)
        self._draw_selection(painter, width, height, state, payload)
        self._draw_measurements(painter, width, height, state, payload)
        self._draw_constellations(painter, width, height, state, payload)
        visible_indices = getattr(star_result, "visible_indices", ())
        visible_count = (
            int(len(visible_indices))
            if visible_indices is not None
            else 0
        )
        if bool(payload.get("hud_visible", True)):
            self._draw_hud(
                painter,
                width,
                state,
                visible_count,
            )
        self.diagnostics.stop_timer("renderer_total")
        diagnostics = self.diagnostics.snapshot()
        if bool(payload.get("debug_render_metrics", False)):
            now = time.monotonic()
            if now - self._last_diagnostics_log_time >= 1.0:
                self._last_diagnostics_log_time = now
                print(self.diagnostics.to_log_line("[SkyDiagnostics]"))
        return {
            "visible_stars": visible_count,
            "diagnostics": {
                "counters": diagnostics.counters,
                "timings_ms": diagnostics.timings_ms,
            },
        }

    def pick(self, x: float, y: float, radius: float = 20.0) -> dict:
        """Resolve screen coordinates against the last complete frame."""

        state = self._last_pick_state
        result = self._last_pick_result
        width, height = self._last_pick_size
        if state is None:
            return {"kind": "none"}
        sky = unproject_universal_stereo_point(
            float(x), float(y), width, height, state.camera
        )
        response: dict[str, Any] = {
            "kind": "sky",
            "alt": float(sky[0]) if sky is not None else 0.0,
            "az": float(sky[1]) if sky is not None else 0.0,
        }
        sky_object = self._pick_sky_object(
            float(x), float(y), float(radius)
        )
        if sky_object is not None:
            return sky_object
        ngc = self._pick_ngc(float(x), float(y), float(radius))
        if ngc is not None:
            return ngc
        if result is None:
            return response
        sx = np.asarray(getattr(result, "visible_sx", ()))
        sy = np.asarray(getattr(result, "visible_sy", ()))
        indices = np.asarray(getattr(result, "visible_indices", ()))
        total = min(len(sx), len(sy), len(indices))
        if total <= 0:
            return response
        distances = np.hypot(
            sx[:total] - float(x), sy[:total] - float(y)
        )
        local_index = int(np.argmin(distances))
        if float(distances[local_index]) > float(radius):
            return response
        catalog_index = int(indices[local_index])
        arrays = self.catalog.arrays
        if not 0 <= catalog_index < len(arrays["ra"]):
            return response
        response = {
            "kind": "star",
            "star": {
                "id": catalog_index,
                "ra": float(arrays["ra"][catalog_index]),
                "dec": float(arrays["dec"][catalog_index]),
                "mag": float(arrays["mag"][catalog_index]),
                "bp_rp": float(arrays["bp_rp"][catalog_index]),
            },
            "screen_distance": float(distances[local_index]),
        }
        altitudes, azimuths = radec_to_altaz_numpy(
            np.asarray([response["star"]["ra"]], dtype=np.float32),
            np.asarray([response["star"]["dec"]], dtype=np.float32),
            state.latitude,
            state.longitude,
            state.ut_hour,
            state.day_of_year_utc,
            year=state.year_utc,
        )
        if altitudes is not None and azimuths is not None:
            response["alt"] = float(altitudes[0])
            response["az"] = float(azimuths[0])
        return response

    def _pick_sky_object(
        self, x: float, y: float, radius: float
    ) -> dict | None:
        nearest = None
        nearest_distance = float("inf")
        for item in self._visible_sky_objects:
            pick_radius = max(
                float(radius), float(item.get("pick_radius", 0.0))
            )
            distance = math.hypot(
                float(item["sx"]) - x, float(item["sy"]) - y
            )
            if distance <= pick_radius and distance <= nearest_distance:
                nearest = item
                nearest_distance = distance
        if nearest is None:
            return None
        result = {
            "kind": "sky",
            "type": str(nearest["type"]),
            "key": str(nearest["key"]),
            "name": str(nearest["name"]),
            "alt": float(nearest["alt"]),
            "az": float(nearest["az"]),
            "screen_distance": float(nearest_distance),
        }
        if "mag" in nearest:
            result["mag"] = float(nearest["mag"])
        return result

    def _register_sky_object(
        self,
        *,
        body_type: str,
        key: str,
        name: str,
        alt: float,
        az: float,
        sx: float,
        sy: float,
        pick_radius: float,
        mag: float | None = None,
    ) -> None:
        item = {
            "type": str(body_type),
            "key": str(key),
            "name": str(name),
            "alt": float(alt),
            "az": float(az) % 360.0,
            "sx": float(sx),
            "sy": float(sy),
            "pick_radius": max(8.0, float(pick_radius)),
        }
        if mag is not None:
            item["mag"] = float(mag)
        self._visible_sky_objects.append(item)

    def _pick_ngc(
        self, x: float, y: float, radius: float
    ) -> dict | None:
        nearest = None
        nearest_distance = float("inf")
        for item in self._visible_ngc:
            distance = math.hypot(
                float(item["sx"]) - x, float(item["sy"]) - y
            )
            pick_radius = max(
                float(radius), float(item.get("pick_radius", 0.0))
            )
            if distance <= pick_radius and distance <= nearest_distance:
                nearest = item
                nearest_distance = distance
        if nearest is None:
            return None
        return {
            "kind": "ngc",
            "name": str(nearest["name"]),
            "ra": float(nearest["ra"]),
            "dec": float(nearest["dec"]),
            "alt": float(nearest["alt"]),
            "az": float(nearest["az"]),
            "screen_distance": float(nearest_distance),
        }

    def pick_surface(self, x: float, y: float) -> dict:
        """Resolve cached terrain metadata without raster IO."""

        if self.terrain is None:
            return {"kind": "none"}
        info = self.terrain.category_at_screen(float(x), float(y))
        if info is None:
            return {"kind": "none"}
        return {
            "kind": "surface",
            "class_id": int(info.class_id),
            "name": str(info.name),
            "description": str(info.description),
            "product": str(info.product),
        }

    def interact(
        self,
        x: float,
        y: float,
        action: str,
        *,
        options: dict[str, Any] | None = None,
    ) -> dict:
        """Apply an ordered pointer/tool command in the Render process."""

        state = self._last_pick_state
        width, height = self._last_pick_size
        if state is None:
            return {
                "kind": "interaction",
                "handled": False,
                "domain": "measurement",
            }
        if str(action).startswith("constellation_"):
            return self._interact_constellation(
                x,
                y,
                str(action),
                state,
                width,
                height,
                options=options,
            )
        project = lambda alt, az: self._project(
            alt, az, width, height, state.camera
        )
        unproject = lambda sx, sy: unproject_universal_stereo_point(
            sx, sy, width, height, state.camera
        )
        action = str(action or "")
        if action == "press":
            handled = self.measurements.on_mouse_press(
                x, y, unproject, project
            )
        elif action == "move":
            handled = self.measurements.on_mouse_move(
                x, y, unproject, project
            )
            self.measurements.update_preview_cursor(x, y, unproject)
        elif action == "release":
            handled = self.measurements.on_mouse_release(
                x, y, unproject, project
            )
        elif action == "undo":
            handled = self.measurements.undo()
        elif action == "delete":
            handled = self.measurements.delete_selected()
        elif action == "cancel":
            self.measurements.cancel_current()
            handled = True
        else:
            handled = False
        return {
            "kind": "interaction",
            "handled": bool(handled),
            "domain": "measurement",
        }

    def _interact_constellation(
        self,
        x: float,
        y: float,
        action: str,
        state: RenderState,
        width: int,
        height: int,
        *,
        options: dict[str, Any] | None = None,
    ) -> dict:
        options = options if isinstance(options, dict) else {}
        project = lambda alt, az: self._project(
            alt, az, width, height, state.camera
        )
        radec_to_sky = lambda ra, dec: self._radec_to_sky(
            ra, dec, state
        )
        pick_star = lambda sx, sy, radius: (
            self.pick(sx, sy, radius).get("star")
        )

        def screen_to_radec(sx, sy):
            sky = unproject_universal_stereo_point(
                sx, sy, width, height, state.camera
            )
            if sky is None:
                return None
            return altaz_to_ra_dec(
                float(sky[0]),
                float(sky[1]),
                state.ut_hour,
                state.day_of_year_utc,
                state.latitude,
                state.longitude,
                year=state.year_utc,
            )

        action_result = None
        if action == "constellation_click":
            handled = self.constellations.on_left_click(
                x,
                y,
                project,
                radec_to_sky,
                pick_star,
                force_add=bool(options.get("force_add", False)),
                additive_select=bool(
                    options.get("additive_select", False)
                ),
                allow_when_disabled=bool(
                    options.get("allow_when_disabled", False)
                ),
            )
        elif action == "constellation_move":
            handled = self.constellations.on_mouse_move(
                x, y, pick_star, screen_to_radec
            )
        elif action == "constellation_right":
            handled = self.constellations.on_right_click(
                x, y, project, radec_to_sky
            )
        elif action == "constellation_double":
            action_result = self.constellations.on_double_click(
                x,
                y,
                project,
                radec_to_sky,
                additive_select=bool(
                    options.get("additive_select", False)
                ),
                allow_when_disabled=bool(
                    options.get("allow_when_disabled", False)
                ),
            )
            handled = bool(
                isinstance(action_result, dict)
                and action_result.get("action") != "none"
            )
        elif action == "constellation_undo":
            handled = self.constellations.undo()
        elif action == "constellation_delete":
            handled = self.constellations.delete_selected()
        elif action == "constellation_finish":
            handled = self.constellations.finish_active_group()
        elif action == "constellation_cancel":
            self.constellations.clear_selection()
            handled = True
        else:
            handled = False
        state_payload = self._constellation_payload()
        self._constellation_payload_cache = dict(state_payload)
        result = {
            "kind": "interaction",
            "handled": bool(handled),
            "domain": "constellation",
            "constellation": state_payload,
        }
        if isinstance(action_result, dict):
            result["action_result"] = action_result
        return result

    def _draw_weather(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
        payload: dict[str, Any],
    ) -> None:
        settings = payload.get("weather")
        settings = settings if isinstance(settings, dict) else {}
        if not bool(settings.get("enabled", False)):
            return
        if self.weather is None:
            self.weather = WeatherSystem(
                width,
                height,
                latitude=state.latitude,
                longitude=state.longitude,
                use_remote_weather=False,
                cache_enabled=False,
            )
            self.weather.provider.shutdown()
            self.weather.provider = _PayloadWeatherProvider()
        revision = int(settings.get("sample_revision", 0))
        if revision != self._weather_sample_revision:
            self.weather.provider.replace(
                settings.get("sample_artifact")
            )
            self._weather_sample_revision = revision
        self.weather.set_bortle(int(settings.get("bortle", state.bortle)))
        self.weather.resize(width, height)
        self.weather.update_weather(
            state.day_of_year_utc,
            state.ut_hour,
            year=state.year_utc,
        )
        self.weather.update_thunder()
        current_fov = 100.0 / max(
            0.001, float(state.camera.zoom_level)
        )
        self.weather.draw(
            painter,
            state.sun_alt,
            state.camera.azimuth_offset,
            state.camera.elevation_angle,
            current_fov=current_fov,
            project_fn=lambda alt, az: project_universal_stereo_point(
                alt, az, width, height, state.camera
            ),
        )

    def _draw_ngc(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
        payload: dict[str, Any],
    ) -> None:
        self._visible_ngc = []
        if (
            "deep_sky" not in state.layers_enabled
            or state.sun_alt > -6.0
        ):
            return
        catalog = self.ngc.load(payload.get("ngc"))
        if catalog is None or len(catalog) == 0:
            return
        altitudes, azimuths = radec_to_altaz_numpy(
            catalog["ra"],
            catalog["dec"],
            state.latitude,
            state.longitude,
            state.ut_hour,
            state.day_of_year_utc,
            year=state.year_utc,
        )
        sx, sy, valid = project_universal_stereo_numpy(
            altitudes, azimuths, width, height, state.camera
        )
        if sx is None or sy is None or valid is None:
            return
        magnitudes = np.asarray(catalog["mag"], dtype=np.float32)
        major_degrees = np.asarray(catalog["maj"], dtype=np.float32)
        names = tuple(getattr(catalog.dtype, "names", ()) or ())
        mask = (
            np.asarray(valid, dtype=bool)
            & (altitudes > -2.0)
            & (sx >= -48.0)
            & (sx <= width + 48.0)
            & (sy >= -48.0)
            & (sy <= height + 48.0)
            & ((magnitudes <= 8.8) | (major_degrees >= 0.30))
        )
        indices = np.flatnonzero(mask)
        if len(indices) == 0:
            return
        max_markers = 260 if state.camera.zoom_level < 2.0 else 420
        importance = np.where(
            np.isfinite(magnitudes[indices]),
            magnitudes[indices],
            99.0,
        ) - np.minimum(3.0, major_degrees[indices] * 3.0)
        indices = indices[
            np.argsort(importance, kind="stable")[:max_markers]
        ]
        angular_scale = height * 0.5 * state.camera.zoom_level / 90.0
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setBrush(Qt.NoBrush)
            label_font = QFont("Arial", 9, QFont.Bold)
            label_font.setStyleHint(QFont.SansSerif)
            label_font.setPixelSize(12)
            painter.setFont(label_font)
            occupied_labels = self._occupied_labels
            max_labels = 90 if state.camera.zoom_level < 2.0 else 180
            for index in indices:
                x = float(sx[index])
                y = float(sy[index])
                kind = str(catalog["kind"][index]).upper().strip()
                name = str(catalog["name"][index]).strip() or "NGC"
                maj_deg = max(0.05, float(catalog["maj"][index]))
                min_deg = max(
                    0.03,
                    float(catalog["min"][index])
                    if "min" in names
                    else maj_deg,
                )
                radius_x = max(
                    3.0, min(84.0, 0.5 * maj_deg * angular_scale)
                )
                radius_y = max(
                    2.0, min(64.0, 0.5 * min_deg * angular_scale)
                )
                position_angle = (
                    float(catalog["pa"][index]) if "pa" in names else 0.0
                )
                magnitude = float(magnitudes[index])
                alpha = (
                    220
                    if magnitude <= 6.0
                    else (185 if magnitude <= 8.5 else 150)
                )
                if kind.startswith("G"):
                    color = QColor(110, 205, 255, alpha)
                elif "PN" in kind or "N" in kind or "HII" in kind:
                    color = QColor(80, 235, 165, alpha)
                elif "GC" in kind:
                    color = QColor(255, 205, 90, alpha)
                else:
                    color = QColor(255, 225, 125, alpha)

                painter.save()
                painter.translate(x, y)
                painter.rotate(position_angle)
                painter.setPen(QPen(color, 1.2))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(
                    QPointF(0.0, 0.0), radius_x, radius_y
                )
                if "GC" in kind:
                    painter.drawLine(
                        QPointF(-radius_x, 0.0),
                        QPointF(radius_x, 0.0),
                    )
                    painter.drawLine(
                        QPointF(0.0, -radius_y),
                        QPointF(0.0, radius_y),
                    )
                elif "PN" in kind:
                    tick = 3.0
                    painter.drawLine(
                        QPointF(-radius_x - tick, 0.0),
                        QPointF(-radius_x, 0.0),
                    )
                    painter.drawLine(
                        QPointF(radius_x, 0.0),
                        QPointF(radius_x + tick, 0.0),
                    )
                painter.restore()
                label_visible = False
                if len(occupied_labels) < max_labels:
                    metrics = painter.fontMetrics()
                    label_rect = QRectF(
                        x + radius_x + 5.0,
                        y - float(metrics.height()) * 0.8,
                        float(metrics.horizontalAdvance(name) + 8),
                        float(metrics.height() + 3),
                    )
                    label_rect = label_rect.intersected(
                        QRectF(0.0, 0.0, float(width), float(height))
                    )
                    if (
                        label_rect.width() > 8.0
                        and label_rect.height() > 4.0
                        and not any(
                            label_rect.adjusted(-2.0, -1.0, 2.0, 1.0)
                            .intersects(existing)
                            for existing in occupied_labels
                        )
                    ):
                        painter.setPen(Qt.NoPen)
                        painter.setBrush(QColor(3, 7, 15, 190))
                        painter.drawRoundedRect(label_rect, 3.0, 3.0)
                        painter.setPen(
                            QPen(
                                QColor(
                                    232,
                                    244,
                                    255,
                                    min(255, alpha + 28),
                                )
                            )
                        )
                        painter.drawText(
                            QPointF(
                                label_rect.left() + 4.0,
                                label_rect.bottom() - 3.0,
                            ),
                            name,
                        )
                        occupied_labels.append(label_rect)
                        label_visible = True
                self._visible_ngc.append(
                    {
                        "sx": x,
                        "sy": y,
                        "name": name,
                        "ra": float(catalog["ra"][index]),
                        "dec": float(catalog["dec"][index]),
                        "alt": float(altitudes[index]),
                        "az": float(azimuths[index]),
                        "pick_radius": float(
                            max(radius_x, radius_y) + 8.0
                        ),
                        "label_visible": label_visible,
                    }
                )
        finally:
            painter.restore()

    @staticmethod
    def _prepare_scope_extras(
        extras: dict[str, Any],
        payload: dict[str, Any],
        *,
        sun_alt: float,
        catalog_rows: int,
    ) -> None:
        if not bool(payload.get("scope_enabled", False)):
            return
        focal_mm = max(
            1.0, float(extras.get("scope_focal_mm", 250.0))
        )
        aperture_mode = str(
            extras.get("scope_aperture_input_mode", "diameter_mm")
        )
        if aperture_mode == "f_number":
            aperture_mm = focal_mm / max(
                0.7,
                float(extras.get("scope_aperture_f_number", 4.0)),
            )
        else:
            aperture_mm = max(
                1.0, float(extras.get("scope_aperture_mm", 80.0))
            )
        instrument = str(
            extras.get("scope_instrument_profile", "telescope")
        )
        eyepiece_mm = (
            max(0.5, float(extras.get("scope_eyepiece_mm", 20.0)))
            if instrument == "telescope"
            else focal_mm
        )
        dark_pupil = max(
            2.2, float(extras.get("scope_eye_pupil_dark_mm", 6.5))
        )
        if sun_alt >= 0.0:
            eye_pupil = 2.2
        elif sun_alt <= -18.0:
            eye_pupil = dark_pupil
        else:
            eye_pupil = 2.2 + (dark_pupil - 2.2) * (-sun_alt / 18.0)
        result = VisualMagnitudeEngine().compute(
            VisualMagnitudeInputs(
                aperture_mm=aperture_mm,
                telescope_focal_mm=focal_mm,
                eyepiece_focal_mm=eyepiece_mm,
                eye_pupil_mm=eye_pupil,
                atmospheric_loss_mag=float(
                    extras.get("scope_atmospheric_loss_mag", 0.0)
                ),
                light_pollution_mode=str(
                    payload.get("light_pollution_mode", "automatic")
                ),
                bortle_class=float(payload.get("bortle", 4.0)),
                magnitude_limit=float(
                    payload.get("magnitude_limit", 8.0)
                ),
                exposure_seconds=max(
                    0.001, float(extras.get("scope_exposure_s", 2.0))
                ),
                iso=max(1.0, float(extras.get("scope_iso", 800.0))),
                instrument_profile=instrument,
                sensor_profile=str(
                    extras.get("scope_sensor_profile", "tiny")
                ),
            )
        )
        extras["scope_limit_mag"] = float(result.scope_limit_mag)
        extras["scope_dataset_max_mag"] = float(
            extras.get("scope_dataset_max_mag", 8.0)
        )
        extras["scope_exposure_gain_mag"] = float(
            result.exposure_gain_mag
        )
        extras["scope_aperture_gain_mag"] = float(
            result.aperture_gain_mag
        )
        extras["scope_depth_gain_mag"] = max(
            0.0, float(result.scope_limit_mag - result.eye_limit_mag)
        )
        extras["scope_signal_gain"] = max(
            1.0, min(6.0, float(result.star_scale_factor))
        )
        extras["scope_halo_gain"] = max(
            1.0, min(5.0, 0.9 + 0.8 * result.star_scale_factor)
        )
        extras["scope_alpha_gain"] = max(
            1.0, min(4.8, 0.8 + 0.9 * result.star_scale_factor)
        )
        extras["scope_size_gain"] = max(
            0.74, min(2.2, 0.75 + 0.35 * result.star_scale_factor)
        )
        extras["scope_limit_extra_mag"] = max(
            0.0, min(4.0, 0.55 * result.exposure_gain_mag)
        )
        fov = payload.get("scope_fov_deg", (5.0, 5.0))
        if isinstance(fov, (list, tuple)) and len(fov) >= 2:
            fov_width, fov_height = float(fov[0]), float(fov[1])
        else:
            fov_width = fov_height = 5.0
        fov_diagonal = math.hypot(fov_width, fov_height)
        extras["scope_fov_diag_deg"] = float(fov_diagonal)
        extras["scope_fov_penalty_mag"] = min(
            5.5,
            max(0.0, math.log2(max(1.0, fov_diagonal / 9.0)))
            * 1.35,
        )
        center = payload.get("scope_center_sky")
        if isinstance(center, (list, tuple)) and len(center) >= 2:
            center_ra, center_dec = altaz_to_ra_dec(
                float(center[0]),
                float(center[1]),
                float(payload.get("ut_hour", 12.0)),
                int(payload.get("day_of_year_utc", 0)),
                float(payload.get("latitude", 0.0)),
                float(payload.get("longitude", 0.0)),
                year=int(payload.get("year_utc", 2026)),
            )
            half_diagonal = 0.5 * fov_diagonal
            dec_pad = min(90.0, max(3.0, half_diagonal * 1.3 + 2.5))
            ra_pad = min(
                180.0,
                dec_pad
                / max(0.12, math.cos(math.radians(center_dec)))
                + 2.0,
            )
            extras["scope_center_ra_deg"] = float(center_ra)
            extras["scope_center_dec_deg"] = float(center_dec)
            extras["scope_preselect_dec_pad_deg"] = float(dec_pad)
            extras["scope_preselect_ra_pad_deg"] = float(ra_pad)
        if catalog_rows <= 0:
            extras["scope_dataset_max_mag"] = -12.0

    @staticmethod
    def _camera(payload) -> Camera:
        raw = payload.get("camera")
        raw = raw if isinstance(raw, dict) else {}
        return Camera(
            azimuth_offset=float(raw.get("azimuth", 0.0)),
            elevation_angle=float(raw.get("elevation", 40.0)),
            zoom_level=max(0.001, float(raw.get("zoom", 1.0))),
            vertical_offset_ratio=float(raw.get("vertical_ratio", 0.3)),
        )

    @staticmethod
    def _pair(value, default) -> tuple[float, float]:
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return float(value[0]), float(value[1])
        return float(default[0]), float(default[1])

    @classmethod
    def _optional_pair(cls, value) -> tuple[float, float] | None:
        return cls._pair(value, (0.0, 0.0)) if value is not None else None

    @staticmethod
    def _magnitude_limit(
        payload, sun_alt: float, eclipse_factor: float = 1.0
    ) -> float:
        night_limit = float(payload.get("magnitude_limit", 8.0))
        if sun_alt > -1.0 and eclipse_factor < 0.18:
            totality = 1.0 - max(0.0, eclipse_factor) / 0.18
            return min(night_limit, 1.0 + 3.0 * totality)
        if sun_alt > 0.0:
            return -10.0
        if sun_alt > -6.0:
            return -10.0 + 10.0 * (sun_alt / -6.0)
        if sun_alt > -12.0:
            return 3.0 * ((sun_alt + 6.0) / -6.0)
        if sun_alt > -18.0:
            t = (sun_alt + 12.0) / -6.0
            return 3.0 + (night_limit - 3.0) * t
        return night_limit

    def _draw_background(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
    ) -> None:
        resolution = 10 if state.interaction_active else 48
        key = (
            resolution,
            width,
            height,
            round(state.sun_alt * 2.0) / 2.0,
            round(state.sun_az / 2.0) * 2.0,
            round(state.camera.azimuth_offset / 2.0) * 2.0,
            round(state.camera.elevation_angle, 1),
            round(state.camera.zoom_level, 2),
            int(state.bortle),
            round(float(state.extras.get("eclipse_factor", 1.0)), 3),
        )
        if self._background_key != key:
            image = QImage(
                resolution,
                resolution,
                QImage.Format_RGB32,
            )
            for y in range(resolution):
                sy = y * height / max(1.0, resolution - 1.0)
                for x in range(resolution):
                    sx = x * width / max(1.0, resolution - 1.0)
                    sky = unproject_universal_stereo_point(
                        sx,
                        sy,
                        width,
                        height,
                        state.camera,
                    )
                    alt, az = sky if sky is not None else (-90.0, 0.0)
                    color = sky_color_phys(
                        alt,
                        az,
                        state.sun_alt,
                        state.sun_az,
                        bortle=state.bortle,
                    )
                    eclipse_factor = float(
                        state.extras.get("eclipse_factor", 1.0)
                    )
                    if state.sun_alt > -1.0 and eclipse_factor < 0.999:
                        dim = 0.08 + 0.92 * math.sqrt(
                            max(0.0, eclipse_factor)
                        )
                        color = QColor(
                            int(color.red() * dim),
                            int(color.green() * dim),
                            int(color.blue() * dim),
                            color.alpha(),
                        )
                    image.setPixelColor(
                        x,
                        y,
                        color,
                    )
            self._background = image
            self._background_key = key
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(
            QRectF(0.0, 0.0, width, height),
            self._background,
        )

    @staticmethod
    def _project(
        alt: float,
        az: float,
        width: int,
        height: int,
        camera: Camera,
    ):
        return project_universal_stereo_point(
            alt, az, width, height, camera
        )

    def _draw_trails(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
        payload: dict[str, Any],
    ) -> None:
        settings = payload.get("trails")
        settings = settings if isinstance(settings, dict) else {}
        if (
            not bool(settings.get("enabled", False))
            or state.sun_alt > -6.0
            or len(state.np_ra) == 0
        ):
            self._trail_cache_key = None
            self._trail_image = None
            return
        try:
            start_hour = float(settings["start_hour"])
        except (KeyError, TypeError, ValueError):
            return
        difference = (
            (float(state.ut_hour) - start_hour + 12.0) % 24.0
        ) - 12.0
        if difference <= 1e-4:
            return

        interaction = bool(state.interaction_active)
        magnitude_limit = float(state.mag_limit)
        mask = (
            np.isfinite(state.np_ra)
            & np.isfinite(state.np_dec)
            & np.isfinite(state.np_mag)
            & (state.np_mag <= magnitude_limit)
        )
        indices = np.flatnonzero(mask)
        maximum = 6000 if interaction else 20_000
        if len(indices) > maximum:
            local = np.argpartition(
                np.asarray(state.np_mag[indices]), maximum - 1
            )[:maximum]
            indices = indices[local]
        if len(indices) == 0:
            return

        steps = (
            min(18, max(5, int(abs(difference) * 2.0) + 3))
            if interaction
            else min(96, max(12, int(abs(difference) * 8.0) + 4))
        )
        cache_key = (
            width,
            height,
            len(state.np_ra),
            int(round(start_hour * 3600.0)),
            # Trails must visibly grow while the observation advances. Keep
            # the expensive static image stable for a few seconds, not a
            # whole minute as the first process renderer accidentally did.
            int(
                round(
                    float(state.ut_hour)
                    * (3600.0 if interaction else 1200.0)
                )
            ),
            round(state.latitude, 5),
            round(state.longitude, 5),
            state.day_of_year_utc,
            state.year_utc,
            round(state.camera.azimuth_offset, 3),
            round(state.camera.elevation_angle, 3),
            round(state.camera.zoom_level, 4),
            round(state.camera.vertical_offset_ratio, 4),
            round(magnitude_limit, 2),
            interaction,
        )
        if self._trail_cache_key != cache_key or self._trail_image is None:
            trail_image = QImage(
                width,
                height,
                QImage.Format_ARGB32_Premultiplied,
            )
            trail_image.fill(Qt.transparent)
            trail_painter = QPainter(trail_image)
            try:
                trail_painter.setRenderHint(
                    QPainter.Antialiasing, not interaction
                )
                hours = np.linspace(
                    start_hour,
                    start_hour + difference,
                    steps,
                    dtype=np.float64,
                )
                screen_x = np.empty((len(indices), steps), dtype=np.float32)
                screen_y = np.empty((len(indices), steps), dtype=np.float32)
                screen_valid = np.empty(
                    (len(indices), steps), dtype=bool
                )
                ra = np.asarray(state.np_ra[indices])
                dec = np.asarray(state.np_dec[indices])
                for column, hour in enumerate(hours):
                    altitudes, azimuths = radec_to_altaz_numpy(
                        ra,
                        dec,
                        state.latitude,
                        state.longitude,
                        float(hour),
                        state.day_of_year_utc,
                        year=state.year_utc,
                    )
                    sx, sy, valid = project_universal_stereo_numpy(
                        altitudes,
                        azimuths,
                        width,
                        height,
                        state.camera,
                    )
                    screen_x[:, column] = sx
                    screen_y[:, column] = sy
                    screen_valid[:, column] = (
                        np.asarray(valid, dtype=bool)
                        & np.isfinite(sx)
                        & np.isfinite(sy)
                        & (sx >= -width * 0.25)
                        & (sx <= width * 1.25)
                        & (sy >= -height * 0.25)
                        & (sy <= height * 1.25)
                    )

                color_paths: dict[tuple[int, int, int], QPainterPath] = {}
                jump_limit = max(width, height) * 0.42
                for row, catalog_index in enumerate(indices):
                    red = int(state.np_r[catalog_index])
                    green = int(state.np_g[catalog_index])
                    blue = int(state.np_b[catalog_index])
                    color_key = (
                        min(255, (red // 24) * 24 + 15),
                        min(255, (green // 24) * 24 + 15),
                        min(255, (blue // 24) * 24 + 15),
                    )
                    path = color_paths.setdefault(
                        color_key, QPainterPath()
                    )
                    drawing = False
                    previous_x = previous_y = 0.0
                    for column in range(steps):
                        if not bool(screen_valid[row, column]):
                            drawing = False
                            continue
                        x = float(screen_x[row, column])
                        y = float(screen_y[row, column])
                        if (
                            drawing
                            and math.hypot(
                                x - previous_x, y - previous_y
                            )
                            <= jump_limit
                        ):
                            path.lineTo(x, y)
                        else:
                            path.moveTo(x, y)
                        drawing = True
                        previous_x, previous_y = x, y

                alpha = 68 if interaction else 138
                trail_painter.setBrush(Qt.NoBrush)
                for color, path in color_paths.items():
                    trail_painter.setPen(
                        QPen(QColor(*color, alpha), 1.0)
                    )
                    trail_painter.drawPath(path)
            finally:
                trail_painter.end()
            self._trail_image = trail_image
            self._trail_cache_key = cache_key
        painter.drawImage(0, 0, self._trail_image)

    def _draw_ephemeris(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
    ) -> None:
        show_sun_moon = "sun_moon" in state.layers_enabled
        show_planets = (
            "planets" in state.layers_enabled
            or "solar_system" in state.layers_enabled
        )
        if not show_sun_moon and not show_planets:
            return

        snapshot = state.ephemeris_snapshot or {}
        scale = min(width, height) * 0.5 * state.camera.zoom_level / 90.0

        if show_sun_moon:
            sun = (
                snapshot.get("sun")
                if isinstance(snapshot.get("sun"), dict)
                else None
            )
            sun_alt = state.sun_alt
            sun_az = state.sun_az
            if sun is not None:
                sun_alt = float(sun.get("alt", sun_alt))
                sun_az = float(sun.get("az", sun_az))
            self._draw_sun_body(
                painter, width, height, state, sun_alt, sun_az, scale, sun
            )

            moon = (
                snapshot.get("moon")
                if isinstance(snapshot.get("moon"), dict)
                else None
            )
            if moon is not None:
                moon_alt = float(moon.get("alt", -90.0))
                moon_az = float(moon.get("az", 0.0))
                self._draw_moon_body(
                    painter, width, height, state, moon_alt, moon_az, scale, moon
                )

        if show_planets:
            planets = snapshot.get("planets", ()) or ()
            if isinstance(planets, (list, tuple)):
                self._draw_planets(
                    painter, width, height, state, scale, planets
                )

    def _draw_sun_body(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
        alt: float,
        az: float,
        scale: float,
        sun_data: dict | None,
    ) -> None:
        rad_deg = (
            float(sun_data.get("rad_deg", 0.2666)) if sun_data else 0.2666
        )
        geometry = self._refracted_disc_geometry(
            alt,
            az,
            rad_deg,
            width,
            height,
            state.camera,
            fallback_px=rad_deg * scale,
        )
        if geometry is None:
            return
        x, y = geometry.x, geometry.y
        physical_radius = geometry.radius_x
        visual_factor = self._celestial_disc_visual_factor(
            physical_radius
        )
        radius_x = geometry.radius_x * visual_factor
        radius_y = geometry.radius_y * visual_factor
        self._register_sky_object(
            body_type="sun",
            key="sun",
            name="Sun",
            alt=alt,
            az=az,
            sx=x,
            sy=y,
            pick_radius=max(radius_x, radius_y) + 8.0,
        )

        painter.save()
        try:
            snapshot = (
                state.ephemeris_snapshot
                if isinstance(state.ephemeris_snapshot, dict)
                else {}
            )
            moon_data = (
                snapshot.get("moon")
                if isinstance(snapshot.get("moon"), dict)
                else {}
            )
            moon_radius_deg = float(
                moon_data.get("rad_deg", 0.2725)
            )
            separation_deg = float(
                moon_data.get(
                    "sep_real",
                    angular_separation_deg(
                        alt,
                        az,
                        float(moon_data.get("alt", -90.0)),
                        float(moon_data.get("az", 0.0)),
                    ),
                )
            )
            is_totality = bool(
                moon_radius_deg >= rad_deg
                and separation_deg
                <= max(0.0, moon_radius_deg - rad_deg)
            )
            if is_totality:
                self._draw_solar_corona(
                    painter,
                    x,
                    y,
                    radius_x,
                    1.0,
                    orientation_deg=(
                        state.day_of_year_utc * 0.73
                        + state.ut_hour * 4.0
                    ),
                    vertical_scale=radius_y / max(0.05, radius_x),
                )

            body_grad = QRadialGradient(x, y, radius_x)
            if alt < 5.0:
                core_col = QColor(255, 190, 72, 255)
                edge_col = QColor(255, 108, 22, 255)
            elif alt < 20.0:
                warmth = (float(alt) - 5.0) / 15.0
                core_col = QColor(
                    255,
                    int(round(205 + 38 * warmth)),
                    int(round(92 + 105 * warmth)),
                    255,
                )
                edge_col = QColor(
                    255,
                    int(round(125 + 78 * warmth)),
                    int(round(28 + 65 * warmth)),
                    255,
                )
            else:
                core_col = QColor(255, 255, 255, 255)
                edge_col = QColor(255, 225, 120, 255)
            body_grad.setColorAt(0.0, core_col)
            body_grad.setColorAt(0.7, core_col)
            body_grad.setColorAt(1.0, edge_col)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(body_grad))
            painter.drawEllipse(
                QPointF(x, y), radius_x, radius_y
            )
        finally:
            painter.restore()

    @staticmethod
    def _local_angular_radius_px(
        alt: float,
        az: float,
        angular_radius_deg: float,
        width: int,
        height: int,
        camera: Camera,
        *,
        fallback_px: float,
    ) -> float:
        """Measure angular radius in the same local projection as its centre."""

        centre = project_universal_stereo_point(
            alt, az, width, height, camera
        )
        if centre is None:
            return max(0.05, float(fallback_px))
        angular_radius_deg = max(
            1e-6, float(angular_radius_deg)
        )
        samples = []
        for direction in (-1.0, 1.0):
            sample_alt = float(alt) + direction * angular_radius_deg
            if not -89.999 < sample_alt < 89.999:
                continue
            point = project_universal_stereo_point(
                sample_alt,
                az,
                width,
                height,
                camera,
            )
            if point is not None:
                samples.append(
                    math.hypot(
                        float(point[0]) - float(centre[0]),
                        float(point[1]) - float(centre[1]),
                    )
                )
        if samples:
            measured = sum(samples) / len(samples)
            if math.isfinite(measured) and measured > 0.0:
                return max(0.05, float(measured))
        return max(0.05, float(fallback_px))

    @classmethod
    def _refracted_disc_geometry(
        cls,
        alt: float,
        az: float,
        angular_radius_deg: float,
        width: int,
        height: int,
        camera: Camera,
        *,
        fallback_px: float,
    ) -> RefractedDiscGeometry | None:
        """Project a refracted centre and its vertically compressed limb."""

        apparent_altitude = standard_refracted_altitude_deg(alt)
        point = project_universal_stereo_point(
            apparent_altitude,
            az,
            width,
            height,
            camera,
        )
        if point is None:
            return None
        radius_x = cls._local_angular_radius_px(
            apparent_altitude,
            az,
            angular_radius_deg,
            width,
            height,
            camera,
            fallback_px=fallback_px,
        )
        radius_y = max(
            0.05,
            radius_x * standard_refraction_vertical_scale(alt),
        )
        return RefractedDiscGeometry(
            x=float(point[0]),
            y=float(point[1]),
            radius_x=float(radius_x),
            radius_y=float(radius_y),
            apparent_altitude_deg=float(apparent_altitude),
        )

    @staticmethod
    def _celestial_disc_visual_factor(
        physical_radius_px: float,
    ) -> float:
        """Magnify discs uniformly while retaining normalized eclipse geometry."""

        physical_radius_px = max(0.05, float(physical_radius_px))
        # A fourfold presentation scale keeps the physical Sun/Moon readable
        # in the general sky view. The same factor is applied to both radii
        # and their relative displacement during an eclipse, so contact times
        # and obscuration percentages remain unchanged.
        if physical_radius_px <= 36.0:
            scale = 4.0
        elif physical_radius_px < 96.0:
            scale = 4.0 - 3.0 * (
                (physical_radius_px - 36.0) / 60.0
            )
        else:
            # In a narrow telescope field the physical disc is already large;
            # additional inflation would push it outside the viewport.
            scale = 1.0
        return max(scale, 18.0 / physical_radius_px)

    @staticmethod
    def _draw_solar_corona(
        painter: QPainter,
        x: float,
        y: float,
        radius: float,
        strength: float,
        *,
        orientation_deg: float = 0.0,
        vertical_scale: float = 1.0,
    ) -> None:
        """Draw a structured, white-blue totality corona with streamers."""

        strength = max(0.0, min(1.0, float(strength)))
        if strength <= 0.0:
            return
        painter.save()
        try:
            painter.translate(float(x), float(y))
            painter.scale(
                1.0, max(0.55, min(1.0, float(vertical_scale)))
            )
            painter.rotate(float(orientation_deg))
            painter.setPen(Qt.NoPen)

            corona_radius = radius * (2.4 + 0.8 * strength)
            glow = QRadialGradient(0.0, 0.0, corona_radius)
            glow.setColorAt(
                0.0, QColor(246, 251, 255, int(225 * strength))
            )
            glow.setColorAt(
                0.18, QColor(236, 247, 255, int(190 * strength))
            )
            glow.setColorAt(
                0.42, QColor(196, 220, 246, int(72 * strength))
            )
            glow.setColorAt(
                0.72, QColor(154, 190, 230, int(23 * strength))
            )
            glow.setColorAt(1.0, QColor(130, 175, 220, 0))
            painter.setBrush(QBrush(glow))
            painter.drawEllipse(
                QPointF(0.0, 0.0), corona_radius, corona_radius
            )

            # Broad, uneven soft lobes approximate the plasma structures seen
            # around totality without producing a geometric starburst.
            streamer_specs = (
                (6.0, 2.05, 0.55, 1.30),
                (41.0, 1.45, 0.78, 1.16),
                (88.0, 1.75, 0.62, 1.24),
                (137.0, 1.35, 0.88, 1.12),
                (188.0, 2.20, 0.52, 1.34),
                (232.0, 1.50, 0.75, 1.18),
                (281.0, 1.85, 0.58, 1.27),
                (329.0, 1.32, 0.82, 1.10),
            )
            for angle, length, width, offset in streamer_specs:
                painter.save()
                painter.rotate(angle)
                painter.translate(radius * offset, 0.0)
                painter.scale(length, width)
                lobe_radius = radius * 0.95
                lobe = QRadialGradient(
                    0.0,
                    0.0,
                    lobe_radius,
                )
                lobe.setColorAt(
                    0.0,
                    QColor(244, 250, 255, int(44 * strength)),
                )
                lobe.setColorAt(
                    0.35,
                    QColor(226, 240, 255, int(31 * strength)),
                )
                lobe.setColorAt(
                    0.72,
                    QColor(190, 217, 245, int(10 * strength)),
                )
                lobe.setColorAt(
                    1.0,
                    QColor(160, 198, 236, 0),
                )
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(lobe))
                painter.drawEllipse(
                    QPointF(0.0, 0.0), lobe_radius, lobe_radius
                )
                painter.restore()
        finally:
            painter.restore()

    def _draw_moon_body(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
        alt: float,
        az: float,
        scale: float,
        moon_data: dict,
    ) -> None:
        rad_deg = float(moon_data.get("rad_deg", 0.2725))
        geometry = self._refracted_disc_geometry(
            alt,
            az,
            rad_deg,
            width,
            height,
            state.camera,
            fallback_px=rad_deg * scale,
        )
        if geometry is None:
            return
        x, y = geometry.x, geometry.y
        radius_x = geometry.radius_x
        radius_y = geometry.radius_y
        separation = float(
            moon_data.get(
                "sep_real",
                angular_separation_deg(
                    state.sun_alt, state.sun_az, alt, az
                ),
            )
        )
        illum = max(
            0.0,
            min(
                1.0,
                float(
                    moon_data.get(
                        "illumination",
                        (1.0 - math.cos(math.radians(separation)))
                        / 2.0,
                    )
                ),
            ),
        )
        sun_data = (
            state.ephemeris_snapshot.get("sun", {})
            if isinstance(state.ephemeris_snapshot, dict)
            else {}
        )
        sun_radius_deg = float(
            sun_data.get("rad_deg", 0.2666)
            if isinstance(sun_data, dict)
            else 0.2666
        )
        eclipsing = separation < sun_radius_deg + rad_deg
        eclipse_presentation = separation < 4.0
        sun_geometry = self._refracted_disc_geometry(
            state.sun_alt,
            state.sun_az,
            sun_radius_deg,
            width,
            height,
            state.camera,
            fallback_px=sun_radius_deg * scale,
        )
        if eclipse_presentation and sun_geometry is not None:
            physical_sun_radius = sun_geometry.radius_x
            visual_factor = self._celestial_disc_visual_factor(
                physical_sun_radius
            )
            sun_x = sun_geometry.x
            sun_y = sun_geometry.y
            x = sun_x + (x - sun_x) * visual_factor
            y = sun_y + (y - sun_y) * visual_factor
            radius_x *= visual_factor
            radius_y *= visual_factor
        else:
            visual_factor = self._celestial_disc_visual_factor(
                geometry.radius_x
            )
            radius_x *= visual_factor
            radius_y *= visual_factor
        visibility_alpha = daylight_moon_alpha(
            illumination=illum,
            elongation_deg=separation,
            moon_alt_deg=alt,
            sun_alt_deg=state.sun_alt,
            eclipsing=eclipsing,
        )
        if visibility_alpha <= 0.0:
            return
        self._register_sky_object(
            body_type="moon",
            key="moon",
            name="Moon",
            alt=alt,
            az=az,
            sx=x,
            sy=y,
            pick_radius=max(radius_x, radius_y) + 8.0,
        )

        painter.save()
        try:
            is_day = state.sun_alt > -6.0
            alpha = int(round(255.0 * visibility_alpha))

            if not is_day and alt > 0.0 and not eclipsing:
                glow_rx = radius_x * 4.0
                glow_ry = radius_y * 4.0
                g_grad = QRadialGradient(x, y, glow_rx)
                g_grad.setColorAt(
                    0.0,
                    QColor(
                        220,
                        230,
                        245,
                        int(65 * max(0.1, illum)),
                    ),
                )
                g_grad.setColorAt(1.0, QColor(200, 215, 235, 0))
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(g_grad))
                painter.drawEllipse(
                    QPointF(x, y), glow_rx, glow_ry
                )

            painter.setPen(Qt.NoPen)
            if eclipsing and sun_geometry is not None:
                # Against a daylight sky the unlit Moon is not a black
                # cardboard disc. Only the part physically in front of the
                # photosphere is an opaque silhouette.
                sun_visual_factor = (
                    self._celestial_disc_visual_factor(
                        sun_geometry.radius_x
                    )
                )
                moon_disc = QPainterPath()
                moon_disc.addEllipse(
                    QPointF(x, y), radius_x, radius_y
                )
                sun_disc = QPainterPath()
                sun_disc.addEllipse(
                    QPointF(sun_geometry.x, sun_geometry.y),
                    sun_geometry.radius_x * sun_visual_factor,
                    sun_geometry.radius_y * sun_visual_factor,
                )
                painter.setBrush(QColor(2, 3, 5, 255))
                painter.drawPath(moon_disc.intersected(sun_disc))

            painter.translate(x, y)
            vertical_scale = radius_y / max(0.05, radius_x)
            painter.scale(1.0, vertical_scale)
            if not eclipsing and not is_day:
                painter.setBrush(QColor(18, 21, 28, 105))
                painter.drawEllipse(
                    QPointF(0.0, 0.0), radius_x, radius_x
                )

            rotation_deg = 0.0
            if sun_geometry is not None:
                rotation_deg = math.degrees(
                    math.atan2(
                        sun_geometry.y - y,
                        sun_geometry.x - x,
                    )
                )
            painter.rotate(rotation_deg)
            lit_path = self._moon_lit_path(radius_x, illum)
            if not eclipsing:
                base_col = (
                    QColor(239, 239, 233, alpha)
                    if not is_day
                    else QColor(218, 226, 232, alpha)
                )
                painter.setBrush(base_col)
                painter.drawPath(lit_path)
            if (
                not eclipsing
                and radius_x >= 5.0
                and illum > 0.01
            ):
                painter.setClipPath(lit_path)
                painter.rotate(-rotation_deg)
                painter.setBrush(
                    QColor(175, 180, 188, int(alpha * 0.58))
                )
                r = radius_x
                painter.drawEllipse(
                    QPointF(-r * 0.2, -r * 0.4), r * 0.25, r * 0.25
                )
                painter.drawEllipse(
                    QPointF(r * 0.2, -r * 0.3), r * 0.2, r * 0.2
                )
                painter.drawEllipse(
                    QPointF(r * 0.3, -r * 0.1), r * 0.22, r * 0.22
                )
                painter.drawEllipse(
                    QPointF(-r * 0.5, -r * 0.1), r * 0.3, r * 0.5
                )
        finally:
            painter.restore()

    @staticmethod
    def _moon_lit_path(radius: float, illumination: float) -> QPainterPath:
        """Build a phase mask whose positive X limb faces the Sun."""

        radius = max(0.01, float(radius))
        illumination = max(0.0, min(1.0, float(illumination)))
        path = QPainterPath()
        samples = 72
        for index in range(samples + 1):
            y = -radius + 2.0 * radius * index / samples
            half_width = math.sqrt(max(0.0, radius**2 - y**2))
            terminator_x = (1.0 - 2.0 * illumination) * half_width
            if index == 0:
                path.moveTo(terminator_x, y)
            else:
                path.lineTo(terminator_x, y)
        for index in range(samples, -1, -1):
            y = -radius + 2.0 * radius * index / samples
            half_width = math.sqrt(max(0.0, radius**2 - y**2))
            path.lineTo(half_width, y)
        path.closeSubpath()
        return path

    def _draw_planets(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
        scale: float,
        planets: list[dict],
    ) -> None:
        planet_colors = {
            "Mercury": QColor(220, 200, 175),
            "Venus": QColor(255, 245, 210),
            "Mars": QColor(255, 115, 80),
            "Jupiter": QColor(245, 215, 175),
            "Saturn": QColor(235, 205, 135),
            "Uranus": QColor(165, 225, 235),
            "Neptune": QColor(110, 155, 255),
        }
        planet_mags = {
            "Mercury": -0.4,
            "Venus": -4.4,
            "Mars": -1.5,
            "Jupiter": -2.5,
            "Saturn": 0.4,
            "Uranus": 5.7,
            "Neptune": 7.8,
        }
        painter.save()
        try:
            font = QFont("Arial", 9, QFont.Bold)
            font.setStyleHint(QFont.SansSerif)
            font.setPixelSize(12)
            painter.setFont(font)
            for planet in planets:
                if not isinstance(planet, dict):
                    continue
                name = str(planet.get("name", "Planet")).strip()
                if "mag" in planet:
                    mag = float(planet["mag"])
                elif "distance_au" in planet:
                    mag = planet_apparent_magnitude(
                        name,
                        float(planet["distance_au"]),
                        float(planet.get("phase_angle_deg", 0.0)),
                    )
                else:
                    mag = float(planet_mags.get(name, 0.0))

                alt = float(planet.get("alt", -90.0))
                az = float(planet.get("az", 0.0))
                if alt < -5.0:
                    continue

                planet_altitude = math.radians(alt)
                sun_altitude = math.radians(state.sun_alt)
                cosine_solar_distance = (
                    math.sin(planet_altitude)
                    * math.sin(sun_altitude)
                    + math.cos(planet_altitude)
                    * math.cos(sun_altitude)
                    * math.cos(
                        math.radians(az - state.sun_az)
                    )
                )
                directional_modifier = -1.5 * max(
                    -1.0, min(1.0, cosine_solar_distance)
                )
                extinction_altitude = max(0.1, alt)
                airmass = 1.0 / (
                    math.sin(math.radians(extinction_altitude))
                    + 0.15
                    * (extinction_altitude + 3.885) ** -1.253
                )
                atmospheric_extinction = float(
                    state.scope_k_fallback
                ) * (airmass - 1.0)
                local_limit = (
                    state.mag_limit
                    + directional_modifier
                    - atmospheric_extinction
                )
                fade_in = max(
                    0.0, min(1.0, (local_limit - mag) * 2.0)
                )
                if fade_in <= 0.01:
                    continue
                point = project_universal_stereo_point(
                    alt, az, width, height, state.camera
                )
                if point is None:
                    continue
                x, y = float(point[0]), float(point[1])
                if x < -30 or x > width + 30 or y < -30 or y > height + 30:
                    continue

                col = QColor(
                    planet_colors.get(name, QColor(230, 220, 200))
                )
                col.setAlphaF(fade_in)
                sz = 4.0 if name in ("Venus", "Jupiter") else (3.5 if name in ("Mars", "Saturn") else 3.0)

                painter.setPen(Qt.NoPen)
                painter.setBrush(col)
                painter.drawEllipse(QPointF(x, y), sz, sz)

                self._register_sky_object(
                    body_type="planet",
                    key=str(planet.get("key", name)).lower(),
                    name=name,
                    alt=alt,
                    az=az,
                    sx=x,
                    sy=y,
                    pick_radius=sz + 8.0,
                    mag=mag,
                )
                label = f"{name} {mag:.1f}"
                metrics = painter.fontMetrics()
                label_rect = QRectF(
                    x + sz + 5.0,
                    y - metrics.height() * 0.75 - 2.0,
                    float(metrics.horizontalAdvance(label) + 8),
                    float(metrics.height() + 4),
                )
                painter.setPen(Qt.NoPen)
                painter.setBrush(
                    QColor(4, 7, 14, int(round(175 * fade_in)))
                )
                painter.drawRoundedRect(label_rect, 3.0, 3.0)
                painter.setPen(
                    QPen(
                        QColor(
                            245,
                            248,
                            255,
                            int(round(255 * fade_in)),
                        )
                    )
                )
                painter.drawText(
                    QPointF(
                        label_rect.left() + 4.0,
                        label_rect.bottom() - 3.0,
                    ),
                    label,
                )
                self._occupied_labels.append(label_rect)
        finally:
            painter.restore()

    def _draw_grid(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
    ) -> None:
        """Draw the celestial equator without reaching back into the UI."""

        lst = (
            100.0
            + state.day_of_year_utc * 0.9856
            + state.ut_hour * 15.0
            + state.longitude
        ) % 360.0
        latitude = math.radians(state.latitude)
        path = QPainterPath()
        open_segment = False
        previous = None
        for index in range(145):
            ra = index * 2.5
            hour_angle = math.radians(lst - ra)
            sin_alt = math.cos(latitude) * math.cos(hour_angle)
            altitude = math.degrees(
                math.asin(max(-1.0, min(1.0, sin_alt)))
            )
            denominator = (
                math.cos(math.radians(altitude))
                * math.cos(latitude)
                + 1e-10
            )
            cos_azimuth = (
                -sin_alt * math.sin(latitude)
            ) / denominator
            azimuth = math.degrees(
                math.acos(max(-1.0, min(1.0, cos_azimuth)))
            )
            if math.sin(hour_angle) > 0.0:
                azimuth = 360.0 - azimuth
            point = self._project(
                altitude, azimuth, width, height, state.camera
            )
            if point is None:
                open_segment = False
                previous = None
                continue
            current = QPointF(float(point[0]), float(point[1]))
            if (
                previous is not None
                and math.hypot(
                    current.x() - previous.x(),
                    current.y() - previous.y(),
                )
                > max(width, height) * 0.35
            ):
                open_segment = False
            if open_segment:
                path.lineTo(current)
            else:
                path.moveTo(current)
                open_segment = True
            previous = current
        painter.save()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(
            QPen(QColor(0, 255, 255, 80), 1.0, Qt.DashLine)
        )
        painter.drawPath(path)
        painter.restore()

    def _draw_scope(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
        payload: dict[str, Any],
    ) -> None:
        scope = self.scope
        scope.enabled = bool(state.scope_enabled)
        if not scope.enabled:
            return
        scope.center = state.scope_center_sky
        scope.awaiting_center_click = scope.center is None
        scope.set_shape(str(payload.get("scope_shape", "circle")))
        scope.set_manual_fov(*state.scope_fov_deg)
        scope.draw(
            painter,
            width,
            height,
            lambda alt, az: self._project(
                alt, az, width, height, state.camera
            ),
        )

    def _draw_selection(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
        payload: dict[str, Any],
    ) -> None:
        selected = payload.get("selection")
        selected = selected if isinstance(selected, dict) else {}
        try:
            altitude = float(selected["alt"])
            azimuth = float(selected["az"])
        except (KeyError, TypeError, ValueError):
            return
        point = self._project(
            altitude, azimuth, width, height, state.camera
        )
        if point is None:
            return
        x, y = float(point[0]), float(point[1])
        body_radius = 0.0
        object_label_visible = False
        selected_kind = str(selected.get("kind", "") or "").lower()
        selected_type = str(selected.get("type", "") or "").lower()
        selected_key = str(selected.get("key", "") or "").lower()
        if selected_kind == "sky":
            for item in self._visible_sky_objects:
                item_key = str(item.get("key", "") or "").lower()
                key_matches = bool(
                    selected_key
                    and (
                        item_key == selected_key
                        or item_key.startswith(f"{selected_key} ")
                        or selected_key.startswith(f"{item_key} ")
                    )
                )
                type_matches = bool(
                    not selected_key
                    and selected_type
                    and str(item.get("type", "")).lower()
                    == selected_type
                )
                if key_matches or type_matches:
                    x = float(item["sx"])
                    y = float(item["sy"])
                    body_radius = max(
                        0.0, float(item.get("pick_radius", 8.0)) - 8.0
                    )
                    object_label_visible = (
                        str(item.get("type", "")).lower() == "planet"
                    )
                    break
        elif selected_kind == "ngc":
            selected_name = str(selected.get("name", "") or "")
            for item in self._visible_ngc:
                if selected_name and str(item.get("name", "")) != selected_name:
                    continue
                x = float(item["sx"])
                y = float(item["sy"])
                body_radius = max(
                    0.0, float(item.get("pick_radius", 8.0)) - 8.0
                )
                object_label_visible = bool(
                    item.get("label_visible", False)
                )
                break
        pulse = 0.5 * (
            math.sin(time.monotonic() * (2.0 * math.pi / 1.15)) + 1.0
        )
        base_radius = max(9.0, body_radius + 5.0)
        radius = base_radius + pulse * 4.0
        alpha = int(round(85.0 + (1.0 - pulse) * 120.0))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(
            QPen(QColor(255, 255, 255, max(30, alpha // 2)), 2.2)
        )
        painter.drawEllipse(
            QPointF(x, y), radius + 2.5, radius + 2.5
        )
        painter.setPen(QPen(QColor(102, 232, 218, alpha), 1.3))
        painter.drawEllipse(QPointF(x, y), radius, radius)
        name = str(selected.get("name", "") or "")
        if not name and isinstance(selected.get("star"), dict):
            star = selected["star"]
            name = str(star.get("name", "") or "")
            if not name and "mag" in star:
                name = f"Gaia · mag {float(star['mag']):.2f}"
        if name and not object_label_visible:
            painter.setPen(QColor(170, 240, 230, 235))
            painter.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
            painter.drawText(QPointF(x + 15.0, y - 12.0), name)
        painter.restore()

    def _draw_measurements(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
        payload: dict[str, Any],
    ) -> None:
        settings = payload.get("measurement")
        settings = settings if isinstance(settings, dict) else {}
        tool = str(settings.get("tool", TOOL_NONE) or TOOL_NONE)
        if tool != self.measurements.active_tool:
            self.measurements.set_tool(tool)
        revision = int(settings.get("clear_revision", 0))
        if self._measurement_revision < 0:
            self._measurement_revision = revision
        elif revision != self._measurement_revision:
            self.measurements.clear()
            self._measurement_revision = revision
        self.measurements.draw(
            painter,
            lambda alt, az: self._project(
                alt, az, width, height, state.camera
            ),
        )

    def _draw_constellations(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
        payload: dict[str, Any],
    ) -> None:
        settings = payload.get("constellation")
        settings = settings if isinstance(settings, dict) else {}
        if settings != self._constellation_payload_cache:
            self._apply_constellation_payload(settings)
            self._constellation_payload_cache = dict(settings)
        self.constellations.draw(
            painter,
            lambda alt, az: self._project(
                alt, az, width, height, state.camera
            ),
            lambda ra, dec: self._radec_to_sky(ra, dec, state),
        )

    @staticmethod
    def _radec_to_sky(
        ra: float, dec: float, state: RenderState
    ) -> tuple[float, float] | None:
        altitudes, azimuths = radec_to_altaz_numpy(
            np.asarray([float(ra)], dtype=np.float32),
            np.asarray([float(dec)], dtype=np.float32),
            state.latitude,
            state.longitude,
            state.ut_hour,
            state.day_of_year_utc,
            year=state.year_utc,
        )
        if altitudes is None or azimuths is None:
            return None
        return float(altitudes[0]), float(azimuths[0])

    def _apply_constellation_payload(self, payload: dict) -> None:
        controller = self.constellations
        controller.data_path = str(payload.get("data_path", "") or "")
        controller.enabled = bool(payload.get("enabled", False))
        controller.visible = bool(payload.get("visible", True))
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
        for name in (
            "active_group_index",
            "selected_group_index",
            "selected_node_index",
            "selected_segment_index",
            "resume_from_node_index",
        ):
            value = payload.get(name)
            setattr(controller, name, int(value) if value is not None else None)
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

    def _constellation_payload(self) -> dict:
        controller = self.constellations
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

    def _draw_terrain(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
        payload: dict[str, Any],
    ) -> None:
        artifact = payload.get("terrain")
        artifact = artifact if isinstance(artifact, dict) else {}
        topography_enabled = bool(artifact.get("topography_enabled", True))
        horizon_enabled = bool(artifact.get("horizon_enabled", True))
        if not topography_enabled and not horizon_enabled:
            return
        path = str(artifact.get("profile_path", "") or "")
        if path != self._terrain_path or self.terrain is None:
            valid_path = path if (path and Path(path).is_file()) else None
            self.terrain = HorizonOverlay(
                horizon_profile_path=valid_path,
                allow_procedural_fallback=True,
            )
            self._terrain_path = path
            self._terrain_surface_path = ""
        if self.terrain is None:
            return
        self.terrain.set_surface_visual_style(
            str(
                artifact.get("surface_visual_style", "original")
                or "original"
            )
        )
        surface_path = str(artifact.get("surface_path", "") or "")
        if surface_path != self._terrain_surface_path:
            profile = getattr(self.terrain, "profile", None)
            if profile is not None:
                try:
                    profile.surface_samples = (
                        self._load_surface_artifact(surface_path)
                        if surface_path
                        else None
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    # A stale/missing immutable artifact must not take down
                    # Render. The last terrain geometry remains usable and a
                    # later surface publication can replace it.
                    profile.surface_samples = None
                self.terrain.set_profile(profile)
            self._terrain_surface_path = surface_path
        project = lambda alt, az: project_universal_stereo_point(
            alt, az, width, height, state.camera
        )
        project_many = lambda alt, az: project_universal_stereo_numpy(
            alt, az, width, height, state.camera
        )
        terrain_3d_enabled = bool(artifact.get("terrain_3d_enabled", True)) if topography_enabled else False
        surface_enabled = bool(artifact.get("surface_enabled", True)) if topography_enabled else False
        draw_flat_line = bool(not topography_enabled and horizon_enabled)
        snapshot = (
            state.ephemeris_snapshot
            if isinstance(state.ephemeris_snapshot, dict)
            else {}
        )
        moon = (
            snapshot.get("moon")
            if isinstance(snapshot.get("moon"), dict)
            else {}
        )
        light_context = TerrainCelestialLightContext(
            sun_altitude_deg=float(state.sun_alt),
            sun_azimuth_deg=float(state.sun_az),
            moon_altitude_deg=(
                float(moon.get("alt"))
                if moon and moon.get("alt") is not None
                else None
            ),
            moon_azimuth_deg=(
                float(moon.get("az"))
                if moon and moon.get("az") is not None
                else None
            ),
            moon_illumination=float(
                moon.get("illumination", 0.0) if moon else 0.0
            ),
            eclipse_factor=float(
                state.extras.get("eclipse_factor", 1.0)
            ),
        )
        draw_domes_callback = None
        self._dome_count = 0
        if (
            not state.interaction_active
            and bool(payload.get("light_pollution_enabled", True))
            and is_automatic_mode(state.light_pollution_mode)
            and state.sun_alt < 0.0
        ):
            twilight_factor = (
                min(1.0, max(0.0, -state.sun_alt / 18.0))
                * float(state.extras.get("eclipse_factor", 1.0))
            )
            if twilight_factor > 0.01:

                def draw_domes_callback(
                    target_painter, index, distance
                ):
                    self._draw_single_city_dome(
                        target_painter,
                        self.terrain.profile,
                        int(index),
                        float(distance),
                        twilight_factor,
                        width,
                        height,
                        state,
                    )

        self.terrain.draw(
            painter,
            project,
            width,
            height,
            state.camera.azimuth_offset,
            state.camera.zoom_level,
            state.camera.elevation_angle,
            state.ut_hour,
            draw_flat_line=draw_flat_line,
            projection_fn_numpy=project_many,
            draw_domes_callback=draw_domes_callback,
            sun_alt=state.sun_alt,
            sun_az=state.sun_az,
            terrain_3d_enabled=terrain_3d_enabled,
            interaction_active=state.interaction_active,
            surface_enabled=surface_enabled,
            sky_color_fn=sky_color_phys,
            light_context=light_context,
        )
        self.diagnostics.set_counter(
            "light_domes", float(self._dome_count)
        )

    def _draw_single_city_dome(
        self,
        painter: QPainter,
        profile,
        index: int,
        distance_m: float,
        twilight_factor: float,
        width: int,
        height: int,
        state: RenderState,
    ) -> None:
        """Draw one clustered urban glow at the correct terrain depth."""

        try:
            intensity = float(profile.light_domes[index])
            azimuth = float(profile.azimuths[index])
        except (AttributeError, IndexError, TypeError, ValueError):
            return
        if intensity < 0.2:
            return

        elevation_deg = 0.0
        for band in getattr(profile, "bands", ()) or ():
            try:
                elevation_deg = max(
                    elevation_deg,
                    math.degrees(float(band["angles"][index])),
                )
            except (IndexError, KeyError, TypeError, ValueError):
                continue
        point = project_universal_stereo_point(
            elevation_deg,
            azimuth,
            width,
            height,
            state.camera,
        )
        if point is None:
            return

        distance_factor = math.exp(
            -max(0.0, float(distance_m)) / 35_000.0
        )
        log_intensity = math.log10(1.0 + intensity)
        visual_intensity = log_intensity * distance_factor
        alpha = int(
            min(150.0, visual_intensity * 90.0 * twilight_factor)
        )
        if alpha <= 2:
            return

        maximum_radius = float(width) * 0.4
        radius_x = min(
            maximum_radius,
            log_intensity
            * 30.0
            * state.camera.zoom_level
            * distance_factor,
        )
        if radius_x <= 1.0:
            return
        radius_y = radius_x * 0.35
        hue = 35.0 + min(10.0, intensity / 500.0)
        gradient = QRadialGradient(0.0, 0.0, radius_x)
        gradient.setColorAt(
            0.0,
            QColor.fromHsl(
                int(hue), 50, 80, int(alpha * 0.40)
            ),
        )
        gradient.setColorAt(
            0.30,
            QColor.fromHsl(
                int(hue), 40, 60, int(alpha * 0.15)
            ),
        )
        gradient.setColorAt(
            0.60,
            QColor.fromHsl(
                int(hue), 30, 40, max(1, int(alpha * 0.05))
            ),
        )
        gradient.setColorAt(1.0, QColor(0, 0, 0, 0))

        painter.save()
        try:
            painter.setCompositionMode(QPainter.CompositionMode_Screen)
            painter.translate(float(point[0]), float(point[1]))
            painter.scale(1.0, radius_y / radius_x)
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(
                QPointF(0.0, 0.0), radius_x, radius_x
            )
        finally:
            painter.restore()
        self._dome_count += 1

    @staticmethod
    def _load_surface_artifact(path: str):
        """Load a published immutable material cache in Render."""

        artifact_path = Path(str(path or ""))
        if not artifact_path.is_file():
            return None
        from TerraLab.terrain.surface import _surface_cache_from_payload

        with np.load(artifact_path, allow_pickle=False) as archive:
            if "__metadata__" not in archive.files:
                raise ValueError("Surface artifact metadata is missing")
            metadata = json.loads(
                np.asarray(
                    archive["__metadata__"], dtype=np.uint8
                )
                .tobytes()
                .decode("utf-8")
            )
            arrays = {
                name: np.asarray(archive[name]).copy()
                for name in archive.files
                if name != "__metadata__"
            }
        return _surface_cache_from_payload(metadata, arrays)

    def _draw_compass(
        self,
        painter: QPainter,
        width: int,
        height: int,
        state: RenderState,
    ) -> None:
        dirs = [
            (0.0, "N"),
            (45.0, "NE"),
            (90.0, "E"),
            (135.0, "SE"),
            (180.0, "S"),
            (225.0, "SW"),
            (270.0, "W"),
            (315.0, "NW"),
        ]
        painter.save()
        try:
            font = QFont()
            font.setPointSize(10)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(QColor(220, 225, 235, 210), 1.2))
            for deg, label in dirs:
                point = project_universal_stereo_point(
                    0.0, deg, width, height, state.camera
                )
                if point is None:
                    continue
                x, y = float(point[0]), float(point[1])
                if x < -50 or x > width + 50 or y < -50 or y > height + 50:
                    continue
                painter.drawLine(QPointF(x, y - 5.0), QPointF(x, y + 5.0))
                painter.drawText(QPointF(x - 10.0, y - 8.0), label)
        finally:
            painter.restore()

    @staticmethod
    def _draw_hud(
        painter: QPainter,
        width: int,
        state: RenderState,
        visible_stars: int,
    ) -> None:
        azimuth = state.camera.azimuth_offset % 360.0
        directions = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
        direction = directions[int((azimuth + 22.5) / 45.0) % 8]
        fov_deg = 93.9 / max(0.001, state.camera.zoom_level)
        box_width = min(420.0, max(280.0, float(width) - 30.0))
        box_height = 64.0

        painter.save()
        try:
            painter.setPen(QPen(QColor(216, 178, 106, 180), 1.5))
            painter.setBrush(QBrush(QColor(5, 8, 17, 230)))
            painter.drawRoundedRect(
                QRectF(10.0, 10.0, box_width, box_height), 8.0, 8.0
            )

            lat_str = f"{abs(state.latitude):.2f}°{'N' if state.latitude >= 0 else 'S'}"
            lon_str = f"{abs(state.longitude):.2f}°{'E' if state.longitude >= 0 else 'W'}"
            line1 = f"{direction} ({azimuth:.1f}°) · ALT {state.camera.elevation_angle:.1f}° · FOV {fov_deg:.1f}°"

            ut_h = state.ut_hour % 24.0
            time_str = f"UT {int(ut_h):02d}:{int((ut_h % 1) * 60):02d}"
            line2 = f"{time_str} · {lat_str} {lon_str} · STARS {visible_stars} · Bortle {state.bortle}"

            font1 = QFont("Arial", 10, QFont.Bold)
            font1.setStyleHint(QFont.SansSerif)
            painter.setFont(font1)
            painter.setPen(QColor(255, 255, 255, 255))
            painter.drawText(20, 35, line1)

            font2 = QFont("Arial", 9, QFont.Normal)
            font2.setStyleHint(QFont.SansSerif)
            painter.setFont(font2)
            painter.setPen(QColor(190, 215, 245, 255))
            painter.drawText(20, 57, line2)
        finally:
            painter.restore()
