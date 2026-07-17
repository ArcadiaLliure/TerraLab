"""
horizon_overlay.py  -  Multi-band terrain/mountain renderer

Renders the real DEM horizon profile as layered silhouettes with
atmospheric perspective, inspired by the Topo Horizon POC viewer.

Separated from village_overlay.py so that terrain rendering and
village-object rendering are independent concerns.
"""

import math
import os
import random
import time
from dataclasses import dataclass

import numpy as np
from PyQt5.QtCore import QObject, QPointF, Qt, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)

from TerraLab.common.performance import (
    ByteLRU,
    DEFAULT_PERFORMANCE_BUDGET,
    PERFORMANCE_FLAGS,
)

try:
    from TerraLab.terrain.engine import (
        HorizonProfile,
        compute_polar_mesh_normals,
        load_profile,
    )

    HORIZON_ENGINE_AVAILABLE = True
except ImportError:
    HORIZON_ENGINE_AVAILABLE = False


@dataclass(frozen=True)
class _TerrainRenderAsset:
    """Immutable, camera-independent arrays prepared outside paint work."""

    mesh_id: int
    azimuths: np.ndarray
    azimuths_closed: np.ndarray
    distances: np.ndarray
    altitudes: np.ndarray
    altitudes_closed: np.ndarray
    elevations: np.ndarray
    valid: np.ndarray
    valid_closed: np.ndarray
    visible: np.ndarray
    normal_x: np.ndarray
    normal_y: np.ndarray
    normal_z: np.ndarray

    def __post_init__(self) -> None:
        for name in (
            "azimuths",
            "azimuths_closed",
            "distances",
            "altitudes",
            "altitudes_closed",
            "elevations",
            "valid",
            "valid_closed",
            "visible",
            "normal_x",
            "normal_y",
            "normal_z",
        ):
            np.asarray(getattr(self, name)).setflags(write=False)


# ─── Layer definitions ───────────────────────────────────────────

# ─── Layer color palette ─────────────────────────────────────────────────────
#
# Color key-stops for the gradient, from index 0 (farthest/deepest) to 1 (nearest/ground).
# (night_rgb, day_rgb) tuples. We interpolate between these stops.
#
_PALETTE_STOPS = [
    # t=0.0  Deepest Haze (farthest)
    ((70, 82, 100), (178, 194, 210)),
    # t=0.25 Mid Haze
    ((58, 70, 88), (152, 172, 186)),
    # t=0.50 Mid-range Green-Blue transition
    ((44, 58, 70), (116, 138, 132)),
    # t=0.75 Near hills
    ((28, 42, 48), (82, 108, 86)),
    # t=1.0  Immediate Foreground (nearest)
    ((18, 32, 34), (62, 86, 64)),
]


def _palette_color(t: float):
    """
    Interpolate a (night_QColor, day_QColor) from the gradient palette at position t in [0, 1].
    t=0 → farthest (Haze Blue), t=1 → nearest (Forest Green).
    """
    stops = _PALETTE_STOPS
    n_seg = len(stops) - 1

    seg_t = t * n_seg
    seg_i = int(seg_t)
    seg_f = seg_t - seg_i

    if seg_i >= n_seg:
        seg_i = n_seg - 1
        seg_f = 1.0

    (nr0, ng0, nb0), (dr0, dg0, db0) = stops[seg_i]
    (nr1, ng1, nb1), (dr1, dg1, db1) = stops[seg_i + 1]

    def lerp(a, b, f):
        return int(a + (b - a) * f)

    night_c = QColor(
        lerp(nr0, nr1, seg_f), lerp(ng0, ng1, seg_f), lerp(nb0, nb1, seg_f)
    )
    day_c = QColor(
        lerp(dr0, dr1, seg_f), lerp(dg0, dg1, seg_f), lerp(db0, db1, seg_f)
    )
    return night_c, day_c


def generate_layer_defs(bands: list) -> list:
    """
    Given a list of band dicts (from engine.generate_bands), produce a LAYER_DEFS-compatible
    list of (band_id, night_QColor, day_QColor) tuples.

    Bands are expected in ASCENDING distance order (nearest first from engine),
    but LAYER_DEFS must be in DESCENDING order (farthest drawn first = painter z-order).
    """
    n = len(bands)
    result = []
    # Reverse so we draw farthest first
    for i, band in enumerate(reversed(bands)):
        # Use non-linear mapping (square root) to stretch near-colors (green) further
        # into the distance, as requested by the user.
        t_linear = i / max(n - 1, 1)
        t = math.sqrt(t_linear)
        # We want t=0 = farthest color, t=1 = nearest color
        # After reversing bands, i=0 is the farthest, so t=0 → farthest → correct.
        night_c, day_c = _palette_color(t)
        result.append((band["id"], night_c, day_c))
    return result


# Static sane default for use before a profile is loaded (20 bands)
LAYER_DEFS = generate_layer_defs(
    __import__(
        "TerraLab.terrain.engine", fromlist=["generate_bands"]
    ).generate_bands(
        20,
        max_dist_m=__import__(
            "TerraLab.terrain.visibility_range", fromlist=["resolve_visibility_range"]
        ).resolve_visibility_range(
            __import__(
                "TerraLab.terrain.visibility_range", fromlist=["TerrainRangeSettings"]
            ).TerrainRangeSettings(), 0.0
        ).resolved_radius_m,
    )
)


# Ground fill (solid color below the nearest horizon line)
GROUND_NIGHT = QColor(5, 10, 25)
GROUND_DAY = QColor(64, 82, 64)  # Matches closest band (muted terrain green)
ATMOSPHERIC_HAZE_NIGHT = QColor(36, 48, 68)
ATMOSPHERIC_HAZE_DAY = QColor(138, 166, 184)
EARTH_RADIUS_M = 6_371_000.0


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    """Linear interpolation between two QColors."""
    r = c1.red() + (c2.red() - c1.red()) * t
    g = c1.green() + (c2.green() - c1.green()) * t
    b = c1.blue() + (c2.blue() - c1.blue()) * t
    a = c1.alpha() + (c2.alpha() - c1.alpha()) * t
    return QColor(int(r), int(g), int(b), int(a))


def _with_alpha(color: QColor, alpha: int) -> QColor:
    result = QColor(color)
    result.setAlpha(max(0, min(255, int(alpha))))
    return result


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _atmospheric_haze_color(sky_color: QColor, t_night: float) -> QColor:
    haze_blue = _lerp_color(
        ATMOSPHERIC_HAZE_DAY,
        ATMOSPHERIC_HAZE_NIGHT,
        _clamp01(t_night),
    )
    return _lerp_color(haze_blue, sky_color, 0.22 + 0.18 * _clamp01(t_night))


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    if edge0 == edge1:
        return 1.0 if value >= edge1 else 0.0
    t = _clamp01((float(value) - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


def _lerp_piecewise(value: float, stops) -> float:
    if not stops:
        return 0.0
    if value <= stops[0][0]:
        return float(stops[0][1])
    for (x0, y0), (x1, y1) in zip(stops, stops[1:]):
        if value <= x1:
            t = _smoothstep(x0, x1, value)
            return float(y0 + (y1 - y0) * t)
    return float(stops[-1][1])


def _distance_haze_factor(distance_m: float) -> float:
    return _clamp01(
        _lerp_piecewise(
            max(0.0, float(distance_m or 0.0)),
            (
                (0.0, 0.00),
                (5_000.0, 0.05),
                (15_000.0, 0.24),
                (40_000.0, 0.46),
                (80_000.0, 0.66),
                (150_000.0, 0.78),
            ),
        )
    )


def _distance_haze_factors(distance_m) -> np.ndarray:
    distance = np.maximum(0.0, np.asarray(distance_m, dtype=np.float32))
    stops = (
        (0.0, 0.00),
        (5_000.0, 0.05),
        (15_000.0, 0.24),
        (40_000.0, 0.46),
        (80_000.0, 0.66),
        (150_000.0, 0.78),
    )
    result = np.full(distance.shape, stops[-1][1], dtype=np.float32)
    result = np.where(distance <= stops[0][0], stops[0][1], result)
    for (x0, y0), (x1, y1) in zip(stops, stops[1:]):
        mask = (distance > x0) & (distance <= x1)
        t = np.clip((distance - x0) / max(x1 - x0, 1e-6), 0.0, 1.0)
        t = t * t * (3.0 - 2.0 * t)
        result = np.where(mask, y0 + (y1 - y0) * t, result)
    return np.clip(result, 0.0, 1.0).astype(np.float32)


def _solar_shading_strength(sun_alt: float) -> float:
    alt = float(sun_alt if sun_alt is not None else -90.0)
    twilight_side_light = 0.34 * _smoothstep(-12.0, 0.0, alt)
    daylight = 0.66 * _smoothstep(0.0, 22.0, alt)
    return _clamp01(twilight_side_light + daylight)


def _terrain_direct_strength(sun_alt: float) -> float:
    alt = float(sun_alt if sun_alt is not None else -90.0)
    if alt <= -6.0:
        return 0.0
    twilight = 0.10 * _smoothstep(-6.0, 0.0, alt)
    daylight = 0.90 * _smoothstep(0.0, 22.0, alt)
    return _clamp01(twilight + daylight)


def _shade_color(color: QColor, factor: float, sky_color: QColor | None = None) -> QColor:
    factor = max(0.55, min(1.35, float(factor)))
    if factor < 1.0:
        result = QColor(
            int(color.red() * factor),
            int(color.green() * factor),
            int(color.blue() * factor),
            color.alpha(),
        )
        return result

    target = sky_color if sky_color is not None else QColor(255, 255, 255)
    return _lerp_color(color, target, min(0.32, (factor - 1.0) * 1.35))


def _calc_t_night(ut_hour: float) -> float:
    """Compute a [0..1] night factor from UTC hour (0=midnight, 12=noon)."""
    val = math.cos((ut_hour / 24.0) * 2 * math.pi)
    t = (val + 1.0) / 2.0
    t = t * t * (3.0 - 2.0 * t)  # smoothstep
    return max(0.0, min(1.0, t))


def _parse_band_max_from_id(band_id: str) -> float:
    """
    Extreu la distància màxima en metres de l'ID d'una banda generada per generate_bands().

    Format esperat:  zone_minFmt_maxFmt
    Exemples:
        'gnd_0_71'       → 71.0
        'near_144_208'   → 208.0
        'mid_1.5k_2k'    → 2000.0
        'far_25k_38k'    → 38000.0
        'haze_111k_150k' → 150000.0
    Retorna 9999.0 si no es pot parsejar.
    """

    def _dist_str_to_m(s: str) -> float:
        s = s.strip()
        try:
            if "k" in s:
                return float(s.replace("k", "")) * 1000.0
            return float(s)
        except ValueError:
            return 9999.0

    parts = band_id.rsplit(
        "_", 2
    )  # zona + min + max (el max és l'últim segment)
    if len(parts) >= 3:
        return _dist_str_to_m(parts[1]), _dist_str_to_m(parts[2])
    return 0.0, 9999.0


# ─── Band data wrapper ───────────────────────────────────────────


class _BandPoints:
    """Holds (az_deg, elev_deg) points for one profile band."""

    VOID_THRESHOLD = -80.0  # DEM voids are stored as ≈ -90°

    def __init__(self, profile, band_id, vert_exaggeration=5.0):
        self.band_id = band_id
        # Parsegem min/max de l'ID (ex: "far_25k_38k")
        self.band_min, self.band_max = _parse_band_max_from_id(band_id)
        self.points, self.valid_mask = self._build(
            profile.get_band_points(band_id), profile, vert_exaggeration
        )
        surface_raw = []
        surface_getter = getattr(profile, "get_band_surface_points", None)
        if callable(surface_getter):
            surface_raw = surface_getter(band_id)
        self.surface_points, self.surface_valid_mask = self._build(
            surface_raw, profile, vert_exaggeration
        )

    # ── private ──

    def _build(self, raw, profile, vert_exag):
        if not raw:
            return (None, None), None

        # Unpack raw data (list of (az, elev))
        # Check if raw is already a numpy array from the engine
        if isinstance(raw, np.ndarray):
            az = raw[:, 0]
            elev = raw[:, 1]
        else:
            az = np.array([pt[0] for pt in raw], dtype=np.float32)
            elev = np.array([pt[1] for pt in raw], dtype=np.float32)
        resolved_mask = getattr(profile, "resolved_mask", None)
        if resolved_mask is not None and len(resolved_mask) == len(az):
            valid_mask = np.asarray(resolved_mask, dtype=bool)
        else:
            valid_mask = np.ones_like(az, dtype=bool)

        # Handle voids
        h = np.where(elev < self.VOID_THRESHOLD, -20.0, elev * vert_exag)

        # Ensure perfect 360-degree closure
        if len(az) > 0 and az[0] == 0 and az[-1] < 360:
            az = np.append(az, 360.0)
            h = np.append(h, h[0])
            valid_mask = np.append(valid_mask, valid_mask[0])

        # Ensure sorting for polygon continuity
        sort_idx = np.argsort(az)
        return (az[sort_idx], h[sort_idx]), valid_mask[sort_idx]


# ─── Main overlay class ──────────────────────────────────────────


class HorizonOverlay(QObject):
    """
    Renders terrain silhouettes using a Hybrid Projection:
    - X: Linear mapping based on Azimuth (fixes fisheye 'squeeze')
    - Y: Vertical displacement from the Sky's horizon curve (keeps registration)
    """

    request_update = pyqtSignal()

    def __init__(
        self,
        parent=None,
        horizon_profile_path=None,
        vert_exaggeration=1.0,
        allow_procedural_fallback=True,
        terrain_surface_opaque=True,
    ):
        super().__init__(parent)
        self.vert_exaggeration = vert_exaggeration
        self.allow_procedural_fallback = bool(allow_procedural_fallback)
        self.terrain_surface_opaque = bool(terrain_surface_opaque)
        self._layers = (
            []
        )  # list of (_BandPoints, night_col, day_col)
        self.profile = None  # Store reference to the current profile
        self._loaded = False
        self._max_terrain_surface_quads = 4500
        self._last_surface2d_quads = 0
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._terrain_normal_cache_key = None
        self._terrain_normal_cache = None
        self._terrain_render_asset = None
        self._terrain_shade_cache = ByteLRU(
            max(16 * 1024**2, DEFAULT_PERFORMANCE_BUDGET.transient_bytes // 4)
        )

        if not HORIZON_ENGINE_AVAILABLE:
            print(
                "[HorizonOverlay] Horizon Engine NOT available (ImportError)."
            )

        if horizon_profile_path and HORIZON_ENGINE_AVAILABLE:
            print(
                f"[HorizonOverlay] Attempting to load profile from: {horizon_profile_path}"
            )
            if not os.path.exists(horizon_profile_path):
                print(
                    f"[HorizonOverlay] ERROR: Profile file not found at {horizon_profile_path}"
                )

            try:
                profile = load_profile(horizon_profile_path)
                if profile is not None:
                    self.profile = profile
                    if PERFORMANCE_FLAGS.relief_cached:
                        self._terrain_render_asset = self._prepare_terrain_render_asset(
                            getattr(profile, "terrain_mesh", None)
                        )
                    print(
                        f"[HorizonOverlay] Profile loaded. Processing layers..."
                    )
                    for band_id, night_c, day_c in LAYER_DEFS:
                        bp = _BandPoints(profile, band_id, vert_exaggeration)
                        if bp.points[0] is not None:
                            self._layers.append((bp, night_c, day_c))
                        else:
                            print(
                                f"[HorizonOverlay]   Band '{band_id}': no data, skipped"
                            )
                    self._loaded = bool(self._layers)
                else:
                    print(f"[HorizonOverlay] load_profile returned None.")
            except Exception as e:
                print(f"[HorizonOverlay] Exception loading profile: {e}")

        if not self._layers and self.allow_procedural_fallback:
            print(
                "[HorizonOverlay] No real data loaded — activating procedural fallback."
            )
            self._build_procedural_fallback()

    # ── public API ──

    def set_terrain_surface_opaque(self, enabled: bool) -> None:
        self.terrain_surface_opaque = bool(enabled)
        self.request_update.emit()

    def set_profile(self, profile, layer_defs=None):
        """Update the overlay with a new HorizonProfile object (e.g. from background worker).

        Args:
            profile: HorizonProfile with baked bands
            layer_defs: Optional list of (band_id, night_QColor, day_QColor).
                        Generated by overlay.generate_layer_defs(bands). If None, uses LAYER_DEFS.
        """
        if profile is None:
            return
        self.profile = profile
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._terrain_normal_cache_key = None
        self._terrain_normal_cache = None
        self._terrain_render_asset = (
            self._prepare_terrain_render_asset(getattr(profile, "terrain_mesh", None))
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        self._terrain_shade_cache.clear()

        effective_defs = layer_defs if layer_defs is not None else LAYER_DEFS

        now_mono = float(time.monotonic())
        last_log = float(getattr(self, "_last_set_profile_log_mono", 0.0))
        if (now_mono - last_log) >= 2.0:
            print(
                f"[HorizonOverlay] Updating profile for {profile.observer_lat}, {profile.observer_lon} ({len(effective_defs)} layers)"
            )
            self._last_set_profile_log_mono = now_mono
        self._layers.clear()

        try:
            for band_id, night_c, day_c in effective_defs:
                bp = _BandPoints(profile, band_id, self.vert_exaggeration)
                if bp.points[0] is not None:
                    self._layers.append((bp, night_c, day_c))

            self._loaded = bool(self._layers)
            self.request_update.emit()

        except Exception as e:
            print(f"[HorizonOverlay] Error setting profile: {e}")

    def clear_profile(self):
        """Executa el metode clear_profile de la classe HorizonOverlay.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        self.profile = None
        self._terrain_shadow_cache_key = None
        self._terrain_shadow_cache = None
        self._terrain_normal_cache_key = None
        self._terrain_normal_cache = None
        self._terrain_render_asset = None
        self._terrain_shade_cache.clear()
        self._layers.clear()
        self._loaded = False
        if self.allow_procedural_fallback:
            self._build_procedural_fallback()
        self.request_update.emit()

    def draw(
        self,
        painter: QPainter,
        projection_fn,
        width: int,
        height: int,
        current_azimuth: float,
        zoom_level: float,
        elevation_angle: float,
        ut_hour: float,
        draw_flat_line: bool = False,
        projection_fn_numpy=None,
        draw_domes_callback=None,
        sun_alt: float | None = None,
        sun_az: float | None = None,
        terrain_shading_enabled: bool = True,
        sky_color_fn=None,
    ):
        """
        Main entry: draw all terrain layers.
        If draw_flat_line is True, ignores loaded data/fallback and draws a simple straight line.
        """
        if elevation_angle > 60.0:
            return  # Looking at zenith — skip terrain

        t_night = _calc_t_night(ut_hour)

        bottom_y = height * 2.0

        # Flat Line Mode
        if draw_flat_line:
            color = _lerp_color(GROUND_DAY, GROUND_NIGHT, t_night)
            painter.setPen(QPen(color, 2))
            painter.setBrush(QBrush(color))

            pt = projection_fn(0.0, current_azimuth)
            if pt:
                y = pt[1]
                # If y is off-screen top, drawn from top of screen
                y_draw = int(max(-bottom_y, y))
                painter.drawRect(
                    0, y_draw, width, int(bottom_y * 1.5)
                )  # Big enough to cover
            return

        fov_deg = math.degrees(
            4.0 * math.atan(width / (2.0 * height * max(zoom_level, 1e-6)))
        )
        vert_scale = self.vert_exaggeration * zoom_level
        px_per_alt_deg = (height / 45.0) * vert_scale

        painter.setRenderHint(QPainter.Antialiasing)

        # Pre-calculate Culling range
        # CULLING_MARGIN: degrees outside viewport to keep for smooth transitions
        culling_margin = 10.0
        az_min = current_azimuth - (fov_deg / 2.0) - culling_margin
        az_max = current_azimuth + (fov_deg / 2.0) + culling_margin

        # ── Pre-process Domes (Darrere cap a davant) ─────────────────────────
        pending_domes = []
        if (
            draw_domes_callback
            and hasattr(self, "profile")
            and hasattr(self.profile, "light_domes")
        ):
            ld = self.profile.light_domes
            lpd = self.profile.light_peak_distances
            n = len(ld)
            # Peak detection to avoid saturation (grouping azimuths)
            for i in range(n):
                val = ld[i]
                if val < 0.2:
                    continue  # Threshold
                prev_val = ld[(i - 1) % n]
                next_val = ld[(i + 1) % n]
                # Local maximum check
                if val >= prev_val and val >= next_val:
                    # Simple plateau handling: only pick the first point
                    if val == next_val:
                        continue
                    pending_domes.append({"idx": i, "dist": lpd[i]})
            pending_domes.sort(key=lambda x: x["dist"], reverse=True)

            # Final step: Azimuthal Clustering to avoid 107 centers
            # We group peaks within 15 degrees to consolidate urban centers.
            if pending_domes:
                clustered = []
                # Sort by intensity to keep the brightest peak as the cluster center
                sorted_by_intensity = sorted(
                    pending_domes, key=lambda x: ld[x["idx"]], reverse=True
                )
                used_indices = set()

                for d in sorted_by_intensity:
                    if d["idx"] in used_indices:
                        continue

                    # New Cluster
                    center_az = self.profile.azimuths[d["idx"]]
                    clustered.append(d)
                    used_indices.add(d["idx"])

                    # Consume neighbors
                    for other in sorted_by_intensity:
                        if other["idx"] in used_indices:
                            continue
                        other_az = self.profile.azimuths[other["idx"]]

                        # Shortest angular distance
                        diff = abs(other_az - center_az) % 360
                        if diff > 180:
                            diff = 360 - diff

                        if diff < 15.0:  # 15 degree cluster radius
                            used_indices.add(other["idx"])

                pending_domes = sorted(
                    clustered, key=lambda x: x["dist"], reverse=True
                )

        # ── Dibuix de cada banda de darrera cap a davant ─────────────────────────
        sky_ref = self._reference_sky_color(
            sky_color_fn, sun_alt, sun_az, current_azimuth, t_night
        )
        terrain_mesh = getattr(self.profile, "terrain_mesh", None)
        has_terrain_mesh = bool(terrain_mesh)
        has_surface2d = self._has_terrain_surface_2d(terrain_mesh)

        if has_surface2d and self._layers:
            self._draw_profile_horizon_cap(
                painter,
                projection_fn,
                height,
                px_per_alt_deg,
                current_azimuth,
                az_min,
                az_max,
                t_night,
                sky_ref,
                projection_fn_numpy=projection_fn_numpy,
                fill_to_bottom=True,
                draw_ridge=False,
            )

        if has_surface2d:
            while pending_domes:
                d_info = pending_domes.pop(0)
                draw_domes_callback(painter, d_info["idx"], d_info["dist"])
        else:
            terrain_layers = self._terrain_polygon_layers(has_terrain_mesh)
            for band_pts, night_c, day_c in terrain_layers:
                # First: Draw any domes that are behind or within this band (further than band_min)
                while (
                    pending_domes and pending_domes[0]["dist"] >= band_pts.band_min
                ):
                    d_info = pending_domes.pop(0)
                    draw_domes_callback(painter, d_info["idx"], d_info["dist"])

                base_color = _lerp_color(day_c, night_c, t_night)
                color = self._apply_atmospheric_perspective(
                    base_color, sky_ref, band_pts, t_night
                )
                ridge_color, shadow_color = self._band_edge_colors(
                    color, t_night, band_pts
                )
                surface_color = self._band_surface_color(color, t_night, band_pts)
                self._draw_band_linear(
                    painter,
                    band_pts,
                    color,
                    projection_fn,
                    width,
                    height,
                    px_per_alt_deg,
                    current_azimuth,
                    az_min,
                    az_max,
                    projection_fn_numpy,
                    ridge_color=ridge_color,
                    shadow_color=shadow_color,
                    surface_color=surface_color,
                    sun_alt=sun_alt,
                    sun_az=sun_az,
                    terrain_shading_enabled=(
                        terrain_shading_enabled and has_terrain_mesh
                    ),
                    sky_color=sky_ref,
                )

        # ── Farciment del terra amb gradient de perspectiva ───────────────────────
        # Simulem el pla de terra que s'allunya amb un gradient fosc→color terra,
        # evitant el rectangle pla uniforme que trenca el realisme.
        profile_resolved = getattr(self.profile, "resolved_mask", None)
        profile_is_partial = profile_resolved is not None and not bool(
            np.all(profile_resolved)
        )
        if self._layers and (has_terrain_mesh or not profile_is_partial):
            ground_c = _lerp_color(GROUND_DAY, GROUND_NIGHT, t_night)
            nearest = self._layers[-1]
            self._draw_ground_linear(
                painter,
                nearest[0],
                ground_c,
                projection_fn,
                width,
                height,
                px_per_alt_deg,
                current_azimuth,
                az_min,
                az_max,
                overlap_px=1.0,
                projection_fn_numpy=projection_fn_numpy,
                ridge_color=self._band_edge_colors(
                    ground_c, t_night, nearest[0]
                )[0],
            )

        if has_surface2d:
            self._draw_terrain_surface_2d(
                painter,
                terrain_mesh,
                projection_fn,
                width,
                height,
                px_per_alt_deg,
                current_azimuth,
                az_min,
                az_max,
                t_night,
                sky_ref,
                sun_alt,
                sun_az,
                terrain_shading_enabled,
                projection_fn_numpy=projection_fn_numpy,
            )
        elif has_terrain_mesh and not self._layers:
            self._draw_terrain_mesh(
                painter,
                terrain_mesh,
                projection_fn,
                height,
                px_per_alt_deg,
                current_azimuth,
                az_min,
                az_max,
                t_night,
                sky_ref,
                sun_alt,
                sun_az,
                terrain_shading_enabled,
                projection_fn_numpy=projection_fn_numpy,
            )
        if has_terrain_mesh and self._layers:
            self._draw_profile_horizon_cap(
                painter,
                projection_fn,
                height,
                px_per_alt_deg,
                current_azimuth,
                az_min,
                az_max,
                t_night,
                sky_ref,
                projection_fn_numpy=projection_fn_numpy,
                fill_to_bottom=False,
                draw_ridge=True,
            )

    # ── private rendering ──

    def _reference_sky_color(
        self, sky_color_fn, sun_alt, sun_az, current_azimuth, t_night
    ):
        if callable(sky_color_fn) and sun_alt is not None and sun_az is not None:
            try:
                color = sky_color_fn(
                    0.0,
                    float(current_azimuth) % 360.0,
                    float(sun_alt),
                    float(sun_az),
                )
                if isinstance(color, QColor):
                    return color
            except Exception:
                pass
        return _lerp_color(QColor(170, 195, 215), QColor(5, 5, 12), t_night)

    def _terrain_polygon_layers(self, has_terrain_mesh: bool):
        if not has_terrain_mesh or len(self._layers) <= 16:
            return self._layers

        target_layers = 12
        indices = np.linspace(0, len(self._layers) - 1, target_layers)
        indices = np.unique(np.rint(indices).astype(np.int32))
        if indices[-1] != len(self._layers) - 1:
            indices = np.append(indices, len(self._layers) - 1)
        return [self._layers[int(i)] for i in indices]

    def _has_terrain_surface_2d(self, mesh) -> bool:
        if not mesh:
            return False
        try:
            version = int(np.asarray(mesh.get("version", 1)).item())
            altitudes = np.asarray(mesh.get("altitudes"), dtype=np.float32)
            visible = np.asarray(mesh.get("visible"), dtype=bool)
            valid = np.asarray(mesh.get("valid"), dtype=bool)
            azimuths = np.asarray(mesh.get("azimuths"), dtype=np.float32)
            distances = np.asarray(mesh.get("distances"), dtype=np.float32)
        except Exception:
            return False
        return (
            version >= 2
            and altitudes.ndim == 2
            and altitudes.shape == visible.shape
            and altitudes.shape == valid.shape
            and distances.size == altitudes.shape[0]
            and azimuths.size == altitudes.shape[1]
            and bool(np.any(visible & valid))
        )

    def _apply_atmospheric_perspective(
        self, base_color: QColor, sky_color: QColor, band_pts, t_night: float
    ) -> QColor:
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        haze = _distance_haze_factor(band_max_m)
        haze *= 1.0 - 0.22 * _clamp01(t_night)
        haze_color = _atmospheric_haze_color(sky_color, t_night)
        return _lerp_color(base_color, haze_color, haze)

    def _terrain_shade_values(
        self, az_arr, h_arr, sun_alt, sun_az, band_pts
    ) -> np.ndarray:
        del h_arr
        if sun_alt is None or sun_az is None:
            return np.ones_like(az_arr, dtype=np.float32)

        strength = _solar_shading_strength(float(sun_alt))
        if strength <= 0.001 or len(az_arr) < 2:
            return np.ones_like(az_arr, dtype=np.float32)

        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        distance_contrast = 1.0 - 0.78 * _distance_haze_factor(band_max_m)
        strength *= max(0.18, distance_contrast)

        az = np.asarray(az_arr, dtype=np.float32)
        delta = np.deg2rad(((float(sun_az) - az + 180.0) % 360.0) - 180.0)
        broad_facing = np.cos(delta)

        shade = 1.0 + strength * 0.10 * broad_facing
        return np.clip(shade, 0.91, 1.08).astype(np.float32)

    def _sun_vector_enu(self, sun_alt, sun_az):
        if sun_alt is None or sun_az is None:
            return None
        alt_rad = math.radians(float(sun_alt))
        az_rad = math.radians(float(sun_az))
        cos_alt = math.cos(alt_rad)
        return np.array(
            [
                math.sin(az_rad) * cos_alt,
                math.cos(az_rad) * cos_alt,
                math.sin(alt_rad),
            ],
            dtype=np.float32,
        )

    @staticmethod
    def _sample_polar_elevations(
        elevations,
        valid,
        distances,
        azimuths,
        sample_x,
        sample_y,
    ):
        sample_x = np.asarray(sample_x, dtype=np.float32)
        sample_y = np.asarray(sample_y, dtype=np.float32)
        sample_distance = np.hypot(sample_x, sample_y).astype(np.float32)
        sample_azimuth = (
            np.degrees(np.arctan2(sample_x, sample_y)) % 360.0
        ).astype(np.float32)

        distance_hi = np.searchsorted(
            distances, sample_distance, side="right"
        )
        inside = (distance_hi > 0) & (distance_hi < len(distances))
        distance_hi = np.clip(distance_hi, 1, len(distances) - 1)
        distance_lo = distance_hi - 1
        distance_span = np.maximum(
            distances[distance_hi] - distances[distance_lo], 1e-6
        )
        distance_t = np.clip(
            (sample_distance - distances[distance_lo]) / distance_span,
            0.0,
            1.0,
        )

        az_diffs = np.diff(azimuths.astype(np.float32))
        az_diffs = az_diffs[np.isfinite(az_diffs) & (az_diffs > 0.0)]
        az_step = float(np.median(az_diffs)) if az_diffs.size else 360.0
        az_position = ((sample_azimuth - float(azimuths[0])) % 360.0) / max(
            az_step, 1e-6
        )
        az_lo_float = np.floor(az_position)
        az_t = (az_position - az_lo_float).astype(np.float32)
        az_lo = az_lo_float.astype(np.int32) % len(azimuths)
        az_hi = (az_lo + 1) % len(azimuths)

        w00 = (1.0 - distance_t) * (1.0 - az_t)
        w01 = (1.0 - distance_t) * az_t
        w10 = distance_t * (1.0 - az_t)
        w11 = distance_t * az_t
        samples = (
            (distance_lo, az_lo, w00),
            (distance_lo, az_hi, w01),
            (distance_hi, az_lo, w10),
            (distance_hi, az_hi, w11),
        )

        weighted_height = np.zeros(sample_distance.shape, dtype=np.float32)
        weight_sum = np.zeros(sample_distance.shape, dtype=np.float32)
        for distance_idx, azimuth_idx, weight in samples:
            corner_valid = valid[distance_idx, azimuth_idx]
            corner_weight = np.where(corner_valid, weight, 0.0).astype(
                np.float32
            )
            weighted_height += (
                elevations[distance_idx, azimuth_idx] * corner_weight
            )
            weight_sum += corner_weight

        sampled = np.divide(
            weighted_height,
            np.maximum(weight_sum, 1e-6),
            out=np.zeros_like(weighted_height),
            where=weight_sum > 1e-6,
        )
        sampled_valid = inside & (weight_sum >= 0.50)
        return sampled, sample_distance, sampled_valid

    def _prepare_terrain_render_asset(self, mesh):
        if not isinstance(mesh, dict):
            return None
        try:
            azimuths = np.asarray(mesh.get("azimuths"), dtype=np.float32)
            distances = np.asarray(mesh.get("distances"), dtype=np.float32)
            altitudes = np.asarray(mesh.get("altitudes"), dtype=np.float32)
            elevations = np.asarray(mesh.get("elevations"), dtype=np.float32)
            valid = np.asarray(mesh.get("valid"), dtype=bool)
            visible = np.asarray(mesh.get("visible"), dtype=bool)
        except Exception:
            return None
        shape = (distances.size, azimuths.size)
        if azimuths.size < 2 or distances.size < 2 or altitudes.shape != shape:
            return None
        if elevations.shape != shape:
            elevations = np.zeros(shape, dtype=np.float32)
        if valid.shape != shape:
            valid = np.isfinite(altitudes)
        if visible.shape != shape:
            visible = valid.copy()
        computed = compute_polar_mesh_normals(
            elevations, valid, distances, azimuths
        )
        saved = tuple(
            np.asarray(mesh.get(name), dtype=np.float32)
            for name in ("normal_x", "normal_y", "normal_z")
        )
        if all(item.shape == shape for item in saved):
            saved_norm = np.sqrt(saved[0] ** 2 + saved[1] ** 2 + saved[2] ** 2)
            use_saved = visible & np.isfinite(saved_norm) & (saved_norm > 1e-5)
            normals = tuple(
                np.where(use_saved, saved[index], computed[index]).astype(np.float32)
                for index in range(3)
            )
        else:
            normals = tuple(np.asarray(item, dtype=np.float32) for item in computed)
        azimuths_closed = np.concatenate(
            [azimuths, [azimuths[0] + 360.0]]
        ).astype(np.float32)
        return _TerrainRenderAsset(
            mesh_id=id(mesh),
            azimuths=azimuths,
            azimuths_closed=azimuths_closed,
            distances=distances,
            altitudes=altitudes,
            altitudes_closed=np.concatenate([altitudes, altitudes[:, :1]], axis=1),
            elevations=elevations,
            valid=valid,
            valid_closed=np.concatenate([valid, valid[:, :1]], axis=1),
            visible=visible,
            normal_x=normals[0],
            normal_y=normals[1],
            normal_z=normals[2],
        )

    def _terrain_surface_normals(
        self, mesh, elevations, valid, distances, azimuths
    ):
        cache_key = (id(mesh), elevations.shape)
        if (
            cache_key == self._terrain_normal_cache_key
            and self._terrain_normal_cache is not None
        ):
            return self._terrain_normal_cache

        normals = compute_polar_mesh_normals(
            elevations, valid, distances, azimuths
        )
        self._terrain_normal_cache_key = cache_key
        self._terrain_normal_cache = normals
        return normals

    def _terrain_sun_visibility(
        self,
        mesh,
        elevations,
        valid,
        visible,
        distances,
        azimuths,
        sun_alt,
        sun_az,
        terrain_shading_enabled=True,
    ):
        shape = elevations.shape
        if (
            not terrain_shading_enabled
            or sun_alt is None
            or sun_az is None
            or float(sun_alt) <= -0.5
        ):
            return np.ones(shape, dtype=np.float32)

        cache_key = (
            id(mesh),
            shape,
            round(float(sun_alt) * 4.0) / 4.0,
            round(float(sun_az) * 4.0) / 4.0,
        )
        if (
            cache_key == self._terrain_shadow_cache_key
            and self._terrain_shadow_cache is not None
            and self._terrain_shadow_cache.shape == shape
        ):
            return self._terrain_shadow_cache

        result = np.ones(shape, dtype=np.float32)
        active = valid & visible & np.isfinite(elevations)
        active_rows, active_cols = np.nonzero(active)
        if active_rows.size == 0:
            self._terrain_shadow_cache_key = cache_key
            self._terrain_shadow_cache = result
            return result

        target_distance = distances[active_rows].astype(np.float32)
        target_azimuth = np.deg2rad(
            azimuths[active_cols].astype(np.float32)
        )
        target_x = target_distance * np.sin(target_azimuth)
        target_y = target_distance * np.cos(target_azimuth)
        target_z = elevations[active_rows, active_cols].astype(np.float32)
        target_z -= (target_distance * target_distance) / (
            2.0 * EARTH_RADIUS_M
        )

        sun_az_rad = math.radians(float(sun_az))
        sun_dx = math.sin(sun_az_rad)
        sun_dy = math.cos(sun_az_rad)
        sun_slope = math.tan(math.radians(max(0.15, float(sun_alt))))

        near_steps = np.diff(distances[: min(len(distances), 32)])
        near_steps = near_steps[np.isfinite(near_steps) & (near_steps > 0.0)]
        ray_start = max(
            20.0,
            min(80.0, float(np.median(near_steps)) * 2.0)
            if near_steps.size
            else 40.0,
        )
        ray_limit = max(ray_start * 2.0, float(distances[-1]) * 1.35)
        ray_offsets = np.geomspace(ray_start, ray_limit, 38).astype(
            np.float32
        )
        max_clearance = np.full(active_rows.shape, -np.inf, dtype=np.float32)

        for ray_offset in ray_offsets:
            sample_x = target_x + ray_offset * sun_dx
            sample_y = target_y + ray_offset * sun_dy
            sampled_elevation, sample_distance, sampled_valid = (
                self._sample_polar_elevations(
                    elevations,
                    valid,
                    distances,
                    azimuths,
                    sample_x,
                    sample_y,
                )
            )
            sampled_z = sampled_elevation - (
                sample_distance * sample_distance
            ) / (2.0 * EARTH_RADIUS_M)
            ray_z = target_z + ray_offset * sun_slope
            self_bias = 2.0 + ray_offset * 0.00015
            clearance = sampled_z - ray_z - self_bias
            max_clearance = np.where(
                sampled_valid,
                np.maximum(max_clearance, clearance),
                max_clearance,
            )

        penumbra = np.clip((max_clearance + 2.0) / 14.0, 0.0, 1.0)
        penumbra = penumbra * penumbra * (3.0 - 2.0 * penumbra)
        result[active_rows, active_cols] = 1.0 - penumbra.astype(np.float32)
        result = self._smooth_light_grid(
            result, active, min_value=0.0, max_value=1.0
        )
        result = np.where(active, np.clip(result, 0.0, 1.0), 1.0).astype(
            np.float32
        )

        self._terrain_shadow_cache_key = cache_key
        self._terrain_shadow_cache = result
        return result

    def _terrain_light_factor(
        self,
        normal_x,
        normal_y,
        normal_z,
        distance_m,
        sun_vec,
        sun_alt,
        terrain_shading_enabled=True,
        sun_visibility=None,
    ):
        nx, ny, nz = np.broadcast_arrays(
            np.asarray(normal_x, dtype=np.float32),
            np.asarray(normal_y, dtype=np.float32),
            np.asarray(normal_z, dtype=np.float32),
        )
        if (
            not terrain_shading_enabled
            or sun_vec is None
            or not np.all(np.isfinite(sun_vec))
        ):
            return np.ones(nx.shape, dtype=np.float32)

        direct_strength = _terrain_direct_strength(
            float(sun_alt) if sun_alt is not None else -90.0
        )
        if direct_strength <= 0.001:
            return np.ones(nx.shape, dtype=np.float32)

        norm = np.sqrt(nx * nx + ny * ny + nz * nz)
        safe = np.isfinite(norm) & (norm > 1e-6)
        nx = np.where(safe, nx / np.where(safe, norm, 1.0), 0.0)
        ny = np.where(safe, ny / np.where(safe, norm, 1.0), 0.0)
        nz = np.where(safe, nz / np.where(safe, norm, 1.0), 1.0)

        sun_vec = np.asarray(sun_vec, dtype=np.float32)
        lambert = np.clip(
            nx * sun_vec[0] + ny * sun_vec[1] + nz * sun_vec[2],
            0.0,
            1.0,
        )
        if sun_visibility is not None:
            direct_visibility = np.broadcast_to(
                np.asarray(sun_visibility, dtype=np.float32), nx.shape
            )
            lambert *= np.clip(direct_visibility, 0.0, 1.0)
        raw = 0.84 + 0.26 * lambert

        haze = np.broadcast_to(_distance_haze_factors(distance_m), nx.shape)
        distance_contrast = np.clip(1.0 - 0.72 * haze, 0.30, 1.0)
        factor = 1.0 + (raw - 1.0) * direct_strength * distance_contrast
        return np.clip(factor, 0.84, 1.10).astype(np.float32)

    @staticmethod
    def _smooth_light_grid(
        light_grid, valid_mask, min_value=0.84, max_value=1.10
    ):
        values = np.asarray(light_grid, dtype=np.float32)
        valid = np.asarray(valid_mask, dtype=bool)
        if values.ndim != 2 or valid.shape != values.shape:
            return values

        source = np.where(valid, values, 1.0).astype(np.float32)
        weights = valid.astype(np.float32)
        source_pad = np.pad(source, ((1, 1), (1, 1)), mode="edge")
        weight_pad = np.pad(weights, ((1, 1), (1, 1)), mode="edge")
        kernel = (
            (1.0, 2.0, 1.0),
            (2.0, 4.0, 2.0),
            (1.0, 2.0, 1.0),
        )

        acc = np.zeros_like(source, dtype=np.float32)
        weight_sum = np.zeros_like(source, dtype=np.float32)
        for row in range(3):
            for col in range(3):
                weight = kernel[row][col]
                sample_weight = weight_pad[
                    row : row + values.shape[0], col : col + values.shape[1]
                ] * weight
                acc += (
                    source_pad[
                        row : row + values.shape[0],
                        col : col + values.shape[1],
                    ]
                    * sample_weight
                )
                weight_sum += sample_weight

        smoothed = np.divide(
            acc,
            np.maximum(weight_sum, 1e-6),
            out=np.ones_like(values, dtype=np.float32),
            where=weight_sum > 1e-6,
        )
        return np.where(
            valid,
            np.clip(smoothed, float(min_value), float(max_value)),
            values,
        ).astype(np.float32)

    def _apply_terrain_light(
        self, color: QColor, light_factor: float, sky_color: QColor, t_night: float
    ) -> QColor:
        factor = max(0.84, min(1.10, float(light_factor)))
        if factor < 1.0:
            shadow_day = QColor(30, 50, 46)
            shadow_night = QColor(9, 13, 22)
            shadow = _lerp_color(shadow_day, shadow_night, _clamp01(t_night))
            amount = min(0.24, ((1.0 - factor) / 0.16) * 0.24)
            return _lerp_color(color, shadow, amount)
        if factor > 1.0:
            warm_day = QColor(184, 176, 138)
            warm_night = _atmospheric_haze_color(sky_color, t_night)
            highlight = _lerp_color(warm_day, warm_night, _clamp01(t_night))
            amount = min(0.16, ((factor - 1.0) / 0.10) * 0.16)
            return _lerp_color(color, highlight, amount)
        return color

    def _mesh_quad_color(
        self,
        distance_m,
        nx,
        ny,
        nz,
        t_night,
        sky_color,
        sun_vec,
        sun_alt,
        terrain_shading_enabled=True,
        light_factor=None,
    ):
        haze = _distance_haze_factor(distance_m)
        palette_t = _clamp01(1.0 - haze)
        night_c, day_c = _palette_color(palette_t)
        base = _lerp_color(day_c, night_c, t_night)

        if light_factor is None:
            light_factor = float(
                np.asarray(
                    self._terrain_light_factor(
                        nx,
                        ny,
                        nz,
                        distance_m,
                        sun_vec,
                        sun_alt,
                        terrain_shading_enabled=terrain_shading_enabled,
                    )
                )
            )

        shaded = self._apply_terrain_light(base, light_factor, sky_color, t_night)
        haze_mix = haze * (1.0 - 0.20 * _clamp01(t_night))
        haze_color = _atmospheric_haze_color(sky_color, t_night)
        return _lerp_color(shaded, haze_color, haze_mix)

    def _terrain_surface_color(
        self,
        distance_m,
        light_factor,
        t_night,
        sky_color,
        sun_vec,
        sun_alt,
        terrain_shading_enabled,
    ):
        color = self._mesh_quad_color(
            distance_m,
            0.0,
            0.0,
            1.0,
            t_night,
            sky_color,
            sun_vec,
            sun_alt,
            terrain_shading_enabled=terrain_shading_enabled,
            light_factor=light_factor,
        )
        haze = _distance_haze_factor(distance_m)
        calm_night, calm_day = _palette_color(
            _clamp01(0.62 + 0.18 * (1.0 - haze))
        )
        calm_base = _lerp_color(calm_day, calm_night, t_night)
        haze_color = _atmospheric_haze_color(sky_color, t_night)
        calm_base = _lerp_color(calm_base, haze_color, 0.08 + 0.22 * haze)
        color = _lerp_color(calm_base, color, 0.70)
        alpha = int(82 + 58 * (1.0 - haze))
        alpha = int(alpha * (1.0 - 0.30 * _clamp01(t_night)))
        if self.terrain_surface_opaque:
            color.setAlpha(255)
        else:
            color.setAlpha(max(68, min(140, alpha)))
        return color

    def _terrain_span_brush(
        self,
        seg_x,
        segment_shade,
        distance_m,
        t_night,
        sky_color,
        sun_vec,
        sun_alt,
        terrain_shading_enabled,
    ):
        finite = np.isfinite(seg_x) & np.isfinite(segment_shade)
        if np.count_nonzero(finite) < 2:
            finite_shade = np.asarray(segment_shade)[
                np.isfinite(segment_shade)
            ]
            light_factor = (
                float(np.mean(finite_shade)) if finite_shade.size else 1.0
            )
            return QBrush(
                self._terrain_surface_color(
                    distance_m,
                    light_factor,
                    t_night,
                    sky_color,
                    sun_vec,
                    sun_alt,
                    terrain_shading_enabled,
                )
            )

        x_values = np.asarray(seg_x[finite], dtype=np.float32)
        shade_values = np.asarray(segment_shade[finite], dtype=np.float32)
        x_min = float(np.min(x_values))
        x_max = float(np.max(x_values))
        if x_max - x_min < 1.0 or np.ptp(shade_values) < 0.006:
            light_factor = float(np.mean(shade_values))
            return QBrush(
                self._terrain_surface_color(
                    distance_m,
                    light_factor,
                    t_night,
                    sky_color,
                    sun_vec,
                    sun_alt,
                    terrain_shading_enabled,
                )
            )

        gradient = QLinearGradient(x_min, 0.0, x_max, 0.0)
        stop_count = min(18, len(x_values))
        stop_indices = np.unique(
            np.linspace(0, len(x_values) - 1, stop_count).astype(np.int32)
        )
        stops = []
        for index in stop_indices:
            position = _clamp01(
                (float(x_values[index]) - x_min) / (x_max - x_min)
            )
            color = self._terrain_surface_color(
                distance_m,
                float(shade_values[index]),
                t_night,
                sky_color,
                sun_vec,
                sun_alt,
                terrain_shading_enabled,
            )
            stops.append((position, color))
        for position, color in sorted(stops, key=lambda item: item[0]):
            gradient.setColorAt(position, color)
        return QBrush(gradient)

    def _project_mesh_column(
        self,
        projection_fn,
        projection_fn_numpy,
        az_value,
        altitudes,
        height,
        px_alt,
    ):
        if projection_fn_numpy:
            az_arr = np.full_like(altitudes, float(az_value), dtype=np.float32)
            sx, sy_base = projection_fn_numpy(np.zeros_like(az_arr), az_arr)
            sy = sy_base + 2.0 - (altitudes * px_alt)
            return np.asarray(sx, dtype=np.float32), np.asarray(sy, dtype=np.float32)

        sx = []
        sy = []
        for alt in altitudes:
            anchor = projection_fn(0.0, float(az_value))
            if anchor:
                sx.append(anchor[0])
                sy.append(anchor[1] + 2.0 - float(alt) * px_alt)
            else:
                sx.append(np.nan)
                sy.append(height * 2)
        return np.asarray(sx, dtype=np.float32), np.asarray(sy, dtype=np.float32)

    def _profile_horizon_lookup(self):
        if not self._layers:
            return None
        az_ref = None
        max_h = None
        for band_pts, _night_c, _day_c in self._layers:
            az_raw, h_raw = band_pts.points
            if az_raw is None:
                continue
            valid = getattr(band_pts, "valid_mask", None)
            if valid is None:
                valid = np.ones_like(az_raw, dtype=bool)
            az = np.asarray(az_raw, dtype=np.float32)
            h = np.asarray(h_raw, dtype=np.float32)
            valid = np.asarray(valid, dtype=bool)
            if az_ref is None:
                az_ref = az
                max_h = np.full_like(az_ref, -np.inf, dtype=np.float32)
            if len(az) != len(az_ref) or not np.allclose(az, az_ref):
                h = np.interp(az_ref, az, h, left=-np.inf, right=-np.inf)
                valid = np.isfinite(h)
            max_h = np.maximum(
                max_h,
                np.where(valid & np.isfinite(h), h, -np.inf).astype(
                    np.float32
                ),
            )
        if az_ref is None or max_h is None:
            return None
        valid = np.isfinite(max_h) & (max_h > -80.0)
        if not np.any(valid):
            return None
        return az_ref[valid], max_h[valid]

    @staticmethod
    def _quad_area_px(points) -> float:
        area = 0.0
        for i, p0 in enumerate(points):
            p1 = points[(i + 1) % len(points)]
            area += float(p0.x()) * float(p1.y())
            area -= float(p1.x()) * float(p0.y())
        return abs(area) * 0.5

    def _draw_terrain_surface_2d(
        self,
        painter,
        mesh,
        projection_fn,
        width,
        height,
        px_alt,
        cur_az,
        az_min,
        az_max,
        t_night,
        sky_color,
        sun_alt,
        sun_az,
        terrain_shading_enabled=True,
        projection_fn_numpy=None,
    ):
        self._last_surface2d_quads = 0
        asset = self._terrain_render_asset
        if asset is None or asset.mesh_id != id(mesh):
            asset = self._prepare_terrain_render_asset(mesh)
            if PERFORMANCE_FLAGS.relief_cached:
                self._terrain_render_asset = asset
        if asset is None:
            return
        az_raw = asset.azimuths
        distances = asset.distances
        altitudes = asset.altitudes
        elevations = asset.elevations
        valid = asset.valid
        visible = asset.visible
        normal_x = asset.normal_x
        normal_y = asset.normal_y
        normal_z = asset.normal_z
        az_closed = asset.azimuths_closed
        alt_closed = asset.altitudes_closed
        valid_closed = asset.valid_closed

        az_diffs = np.diff(az_raw.astype(np.float32))
        az_diffs = az_diffs[np.isfinite(az_diffs) & (az_diffs > 0)]
        az_step_deg = float(np.median(az_diffs)) if az_diffs.size else 1.0
        base_col_stride = max(1, int(round(0.8 / max(0.1, az_step_deg))))
        visible_rows = max(1, int(distances.size))
        approx_cols = max(
            1, int(math.ceil((az_max - az_min) / max(az_step_deg, 0.1)))
        )
        approx_cells = int((approx_cols / base_col_stride) * visible_rows)
        stride_boost = max(
            1,
            int(
                math.ceil(
                    math.sqrt(
                        max(1.0, approx_cells)
                        / float(self._max_terrain_surface_quads)
                    )
                )
            ),
        )
        col_stride = base_col_stride * stride_boost
        d_stride = stride_boost

        sun_vec = self._sun_vector_enu(sun_alt, sun_az)
        shade_key = (
            asset.mesh_id,
            bool(terrain_shading_enabled),
            None if sun_alt is None else round(float(sun_alt) * 4.0) / 4.0,
            None if sun_az is None else round(float(sun_az) * 4.0) / 4.0,
        )
        shade_grid = (
            self._terrain_shade_cache.get(shade_key)
            if PERFORMANCE_FLAGS.relief_cached
            else None
        )
        if shade_grid is None:
            sun_visibility = self._terrain_sun_visibility(
                mesh,
                elevations,
                valid,
                visible,
                distances,
                az_raw,
                sun_alt,
                sun_az,
                terrain_shading_enabled=terrain_shading_enabled,
            )
            shade_grid = self._terrain_light_factor(
                normal_x,
                normal_y,
                normal_z,
                distances[:, None],
                sun_vec,
                sun_alt,
                terrain_shading_enabled=terrain_shading_enabled,
                sun_visibility=sun_visibility,
            )
            shade_grid = self._smooth_light_grid(shade_grid, valid & visible)
            if PERFORMANCE_FLAGS.relief_cached:
                self._terrain_shade_cache.put(
                    shade_key, shade_grid, int(shade_grid.nbytes)
                )
        shade_closed = np.concatenate([shade_grid, shade_grid[:, :1]], axis=1)
        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        drawn = 0
        for offset in offsets:
            if drawn >= self._max_terrain_surface_quads:
                break
            final_az = az_closed + offset
            left_cols = np.arange(
                0, len(az_closed) - 1, col_stride, dtype=np.int32
            )
            right_cols = np.minimum(
                left_cols + col_stride, len(az_closed) - 1
            )
            visible_mask = (
                (final_az[left_cols] <= az_max)
                & (final_az[right_cols] >= az_min)
            )
            visible_pairs = list(
                zip(left_cols[visible_mask], right_cols[visible_mask])
            )
            if not visible_pairs:
                continue

            needed_cols = set()
            for left_col, right_col in visible_pairs:
                needed_cols.add(int(left_col))
                needed_cols.add(int(right_col))

            sx_cols = {}
            sy_cols = {}
            ordered_cols = np.asarray(sorted(needed_cols), dtype=np.int32)
            if projection_fn_numpy and ordered_cols.size:
                projected_azimuths = final_az[ordered_cols].astype(np.float32)
                anchor_x, anchor_y = projection_fn_numpy(
                    np.zeros(projected_azimuths.shape, dtype=np.float32),
                    projected_azimuths,
                )
                anchor_x = np.asarray(anchor_x, dtype=np.float32)
                anchor_y = np.asarray(anchor_y, dtype=np.float32)
                projected_y = (
                    anchor_y[None, :]
                    + 2.0
                    - alt_closed[:, ordered_cols] * float(px_alt)
                )
                for position, col in enumerate(ordered_cols):
                    sx_cols[int(col)] = np.full(
                        distances.shape, anchor_x[position], dtype=np.float32
                    )
                    sy_cols[int(col)] = projected_y[:, position]
            else:
                for col in ordered_cols:
                    sx, sy = self._project_mesh_column(
                        projection_fn,
                        projection_fn_numpy,
                        float(final_az[col]),
                        alt_closed[:, col],
                        height,
                        px_alt,
                    )
                    sx_cols[int(col)] = sx
                    sy_cols[int(col)] = sy

            y_limits = np.full(
                ordered_cols.shape, float(height) * 2.0, dtype=np.float32
            )
            for d_idx in range(0, len(distances), d_stride):
                if drawn >= self._max_terrain_surface_quads:
                    break
                quad_distance = float(distances[d_idx])
                row_sx = np.asarray(
                    [sx_cols[int(col)][d_idx] for col in ordered_cols],
                    dtype=np.float32,
                )
                row_sy = np.asarray(
                    [sy_cols[int(col)][d_idx] for col in ordered_cols],
                    dtype=np.float32,
                )
                row_valid = np.asarray(
                    [valid_closed[d_idx, int(col)] for col in ordered_cols],
                    dtype=bool,
                )
                finite = np.isfinite(row_sx) & np.isfinite(row_sy)
                row_valid &= finite
                if np.count_nonzero(row_valid) < 2:
                    continue

                top_y = np.minimum(row_sy, y_limits)
                improves = top_y < (y_limits - 0.25)
                if not np.any(row_valid & improves):
                    continue

                edge_mask = np.diff(
                    np.pad(
                        row_valid.astype(np.int8),
                        (1, 1),
                        constant_values=0,
                    )
                )
                starts = np.where(edge_mask == 1)[0]
                stops = np.where(edge_mask == -1)[0]

                for start, stop in zip(starts, stops):
                    if drawn >= self._max_terrain_surface_quads:
                        break
                    if stop - start < 2:
                        continue
                    if not np.any(improves[start:stop]):
                        continue

                    seg_x = row_sx[start:stop]
                    seg_top = top_y[start:stop]
                    seg_bottom = y_limits[start:stop] + 0.5
                    if len(seg_x) < 2:
                        continue

                    points = [
                        QPointF(float(x), float(y))
                        for x, y in zip(seg_x, seg_top)
                    ]
                    points.extend(
                        QPointF(float(x), float(y))
                        for x, y in zip(seg_x[::-1], seg_bottom[::-1])
                    )
                    if not all(
                        np.isfinite(p.x()) and np.isfinite(p.y())
                        for p in points
                    ):
                        continue

                    xs = [float(p.x()) for p in points]
                    ys = [float(p.y()) for p in points]
                    if max(xs) < -64.0 or min(xs) > float(width) + 64.0:
                        continue
                    if max(ys) < -64.0 or min(ys) > float(height) + 64.0:
                        continue
                    if self._quad_area_px(points) < 0.35:
                        continue

                    seg_cols = ordered_cols[start:stop]
                    segment_shade = shade_closed[d_idx, seg_cols]
                    brush = self._terrain_span_brush(
                        seg_x,
                        segment_shade,
                        quad_distance,
                        t_night,
                        sky_color,
                        sun_vec,
                        sun_alt,
                        terrain_shading_enabled,
                    )
                    painter.setBrush(brush)
                    painter.drawPolygon(QPolygonF(points))
                    drawn += 1
                    y_limits[start:stop] = top_y[start:stop]

        self._last_surface2d_quads = drawn
        painter.setRenderHint(QPainter.Antialiasing, True)

    def _draw_terrain_mesh(
        self,
        painter,
        mesh,
        projection_fn,
        height,
        px_alt,
        cur_az,
        az_min,
        az_max,
        t_night,
        sky_color,
        sun_alt,
        sun_az,
        terrain_shading_enabled=True,
        projection_fn_numpy=None,
    ):
        try:
            az_raw = np.asarray(mesh.get("azimuths"), dtype=np.float32)
            distances = np.asarray(mesh.get("distances"), dtype=np.float32)
            altitudes = np.asarray(mesh.get("altitudes"), dtype=np.float32)
            valid = np.asarray(mesh.get("valid"), dtype=bool)
            normal_x = np.asarray(mesh.get("normal_x"), dtype=np.float32)
            normal_y = np.asarray(mesh.get("normal_y"), dtype=np.float32)
            normal_z = np.asarray(mesh.get("normal_z"), dtype=np.float32)
        except Exception:
            return

        if (
            az_raw.size < 2
            or distances.size < 2
            or altitudes.shape != valid.shape
            or altitudes.shape != (distances.size, az_raw.size)
        ):
            return
        if normal_x.shape != altitudes.shape:
            normal_x = np.zeros_like(altitudes, dtype=np.float32)
            normal_y = np.zeros_like(altitudes, dtype=np.float32)
            normal_z = np.ones_like(altitudes, dtype=np.float32)

        az_closed = np.concatenate([az_raw, [az_raw[0] + 360.0]]).astype(
            np.float32
        )
        alt_closed = np.concatenate([altitudes, altitudes[:, :1]], axis=1)
        valid_closed = np.concatenate([valid, valid[:, :1]], axis=1)
        nx_closed = np.concatenate([normal_x, normal_x[:, :1]], axis=1)
        ny_closed = np.concatenate([normal_y, normal_y[:, :1]], axis=1)
        nz_closed = np.concatenate([normal_z, normal_z[:, :1]], axis=1)

        sun_vec = self._sun_vector_enu(sun_alt, sun_az)
        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        az_diffs = np.diff(az_raw.astype(np.float32))
        az_diffs = az_diffs[np.isfinite(az_diffs) & (az_diffs > 0)]
        az_step_deg = float(np.median(az_diffs)) if az_diffs.size else 1.0
        col_stride = max(1, int(round(2.0 / max(0.1, az_step_deg))))

        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(Qt.NoPen)
        for offset in offsets:
            final_az = az_closed + offset
            left_cols = np.arange(
                0, len(az_closed) - 1, col_stride, dtype=np.int32
            )
            right_cols = np.minimum(
                left_cols + col_stride, len(az_closed) - 1
            )
            visible_mask = (
                (final_az[left_cols] <= az_max)
                & (final_az[right_cols] >= az_min)
            )
            visible_pairs = list(
                zip(left_cols[visible_mask], right_cols[visible_mask])
            )
            if not visible_pairs:
                continue

            sx_cols = {}
            sy_cols = {}
            needed_cols = set()
            for left_col, right_col in visible_pairs:
                needed_cols.add(int(left_col))
                needed_cols.add(int(right_col))
            for col in sorted(needed_cols):
                sx, sy = self._project_mesh_column(
                    projection_fn,
                    projection_fn_numpy,
                    float(final_az[col]),
                    alt_closed[:, col],
                    height,
                    px_alt,
                )
                sx_cols[col] = sx
                sy_cols[col] = sy

            for d_idx in range(len(distances) - 2, -1, -1):
                quad_distance = float(
                    0.5 * (distances[d_idx] + distances[d_idx + 1])
                )
                for col, right_col in visible_pairs:
                    if not (
                        valid_closed[d_idx, col]
                        and valid_closed[d_idx, right_col]
                        and valid_closed[d_idx + 1, col]
                        and valid_closed[d_idx + 1, right_col]
                    ):
                        continue

                    y_left_far = float(sy_cols[col][d_idx + 1])
                    y_right_far = float(sy_cols[right_col][d_idx + 1])
                    y_right_near = float(sy_cols[right_col][d_idx])
                    y_left_near = float(sy_cols[col][d_idx])

                    points = [
                        QPointF(
                            float(sx_cols[col][d_idx + 1]),
                            y_left_far,
                        ),
                        QPointF(
                            float(sx_cols[right_col][d_idx + 1]),
                            y_right_far,
                        ),
                        QPointF(
                            float(sx_cols[right_col][d_idx]),
                            y_right_near,
                        ),
                        QPointF(
                            float(sx_cols[col][d_idx]),
                            y_left_near,
                        ),
                    ]
                    if not all(
                        np.isfinite(p.x()) and np.isfinite(p.y())
                        for p in points
                    ):
                        continue

                    nx = float(
                        np.mean(
                            [
                                nx_closed[d_idx, col],
                                nx_closed[d_idx, right_col],
                                nx_closed[d_idx + 1, col],
                                nx_closed[d_idx + 1, right_col],
                            ]
                        )
                    )
                    ny = float(
                        np.mean(
                            [
                                ny_closed[d_idx, col],
                                ny_closed[d_idx, right_col],
                                ny_closed[d_idx + 1, col],
                                ny_closed[d_idx + 1, right_col],
                            ]
                        )
                    )
                    nz = float(
                        np.mean(
                            [
                                nz_closed[d_idx, col],
                                nz_closed[d_idx, right_col],
                                nz_closed[d_idx + 1, col],
                                nz_closed[d_idx + 1, right_col],
                            ]
                        )
                    )
                    color = self._mesh_quad_color(
                        quad_distance,
                        nx,
                        ny,
                        nz,
                        t_night,
                        sky_color,
                        sun_vec,
                        sun_alt,
                        terrain_shading_enabled=terrain_shading_enabled,
                    )
                    painter.setBrush(QBrush(color))
                    painter.drawPolygon(QPolygonF(points))
        painter.setRenderHint(QPainter.Antialiasing, True)

    def _draw_profile_horizon_cap(
        self,
        painter,
        projection_fn,
        height,
        px_alt,
        cur_az,
        az_min,
        az_max,
        t_night,
        sky_color,
        projection_fn_numpy=None,
        fill_to_bottom=True,
        draw_ridge=True,
    ):
        lookup = self._profile_horizon_lookup()
        if lookup is None:
            return
        az_raw, h_raw = lookup
        if az_raw.size < 2:
            return

        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        all_sx = []
        all_sy = []
        for offset in offsets:
            final_az = az_raw + offset
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask):
                continue

            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            if projection_fn_numpy:
                sx, sy_base = projection_fn_numpy(
                    np.zeros_like(culled_az), culled_az
                )
                sy = sy_base + 2.0 - (culled_h * px_alt)
            else:
                sx = []
                sy = []
                for az_value, height_value in zip(culled_az, culled_h):
                    anchor = projection_fn(0.0, float(az_value))
                    if anchor:
                        sx.append(anchor[0])
                        sy.append(anchor[1] + 2.0 - height_value * px_alt)
                    else:
                        sx.append(np.nan)
                        sy.append(height * 2.0)
                sx = np.asarray(sx, dtype=np.float32)
                sy = np.asarray(sy, dtype=np.float32)

            finite_mask = np.isfinite(sx) & np.isfinite(sy)
            if not np.any(finite_mask):
                continue
            edge_mask = np.diff(
                np.pad(finite_mask.astype(np.int8), (1, 1), constant_values=0)
            )
            starts = np.where(edge_mask == 1)[0]
            stops = np.where(edge_mask == -1)[0]
            for start, stop in zip(starts, stops):
                seg_sx = np.asarray(sx[start:stop], dtype=np.float32)
                seg_sy = np.asarray(sy[start:stop], dtype=np.float32)
                if len(seg_sx) >= 2:
                    all_sx.append(seg_sx)
                    all_sy.append(seg_sy)

        if not all_sx:
            return

        cap_night, cap_day = _palette_color(0.56)
        cap_base = _lerp_color(cap_day, cap_night, t_night)
        cap_fill = _lerp_color(
            cap_base, _atmospheric_haze_color(sky_color, t_night), 0.16
        )
        if fill_to_bottom:
            self._fill_strip_downward_numpy(
                painter, all_sx, all_sy, cap_fill, height * 2.0, solid=True
            )

        if draw_ridge:
            ridge = _with_alpha(QColor(cap_fill).lighter(105), 58)
            shadow = _with_alpha(QColor(cap_fill).darker(110), 36)
            self._stroke_ridge_lines_numpy(
                painter, all_sx, all_sy, shadow, width=0.9, y_offset=0.9
            )
            self._stroke_ridge_lines_numpy(
                painter, all_sx, all_sy, ridge, width=0.65
            )

    def _band_edge_colors(self, fill_color, t_night, band_pts):
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        distance_factor = 1.0 - _distance_haze_factor(band_max_m)
        ridge_alpha = 9 + int(16 * distance_factor)
        shadow_alpha = 12 + int(20 * distance_factor)

        if t_night >= 0.45:
            ridge = QColor(fill_color).lighter(108)
            shadow = QColor(fill_color).darker(106)
            ridge_alpha += int(5 * t_night)
            shadow_alpha += int(5 * t_night)
        else:
            ridge = QColor(fill_color).darker(108)
            shadow = QColor(fill_color).lighter(104)

        return _with_alpha(ridge, ridge_alpha), _with_alpha(shadow, shadow_alpha)

    def _band_surface_color(self, fill_color, t_night, band_pts):
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        distance_factor = 1.0 - _distance_haze_factor(band_max_m)
        alpha = 7 + int(14 * distance_factor)
        if t_night >= 0.45:
            color = QColor(fill_color).lighter(106)
            alpha += int(5 * t_night)
        else:
            color = QColor(fill_color).darker(108)
        return _with_alpha(color, alpha)

    def _draw_band_linear(
        self,
        painter,
        band_pts,
        color,
        proj_fn,
        w,
        h,
        px_alt,
        cur_az,
        az_min,
        az_max,
        proj_fn_numpy=None,
        ridge_color=None,
        shadow_color=None,
        surface_color=None,
        sun_alt=None,
        sun_az=None,
        terrain_shading_enabled=True,
        sky_color=None,
    ):
        """
        Draw one filled silhouette band using the shared sky projection.
        """
        az_raw, h_raw = band_pts.points
        if az_raw is None:
            return
        valid_raw = getattr(band_pts, "valid_mask", None)
        if valid_raw is None:
            valid_raw = np.ones_like(az_raw, dtype=bool)

        # Calculate base offset to center around current azimuth
        # az_raw is 0..360, so center is 180.
        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        all_sx = []
        all_sy = []
        all_az = []
        all_h = []

        for offset in offsets:
            final_az = az_raw + offset

            # 1. CULLING: Only keep points within view
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask):
                continue

            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            culled_valid = np.asarray(valid_raw[mask], dtype=bool)
            if not np.any(culled_valid):
                continue

            # 2. VECTORIZED PROJECTION
            if proj_fn_numpy:
                # Optimized vectorized call
                sx, sy_base = proj_fn_numpy(
                    np.zeros_like(culled_az), culled_az
                )
                sy = sy_base + 2.0 - (culled_h * px_alt)
            else:
                # Fallback to scalar (slow)
                sx = []
                sy = []
                for a_val, h_val in zip(culled_az, culled_h):
                    anchor = proj_fn(0, a_val)
                    if anchor:
                        sx.append(anchor[0])
                        sy.append(anchor[1] + 2.0 - h_val * px_alt)
                    else:
                        sx.append(np.nan)
                        sy.append(h * 2)  # Safety
                sx = np.array(sx)
                sy = np.array(sy)

            finite_mask = np.isfinite(sx) & np.isfinite(sy) & culled_valid
            if not np.any(finite_mask):
                continue
            edge_mask = np.diff(
                np.pad(finite_mask.astype(np.int8), (1, 1), constant_values=0)
            )
            starts = np.where(edge_mask == 1)[0]
            stops = np.where(edge_mask == -1)[0]
            for start, stop in zip(starts, stops):
                seg_sx = sx[start:stop]
                seg_sy = sy[start:stop]
                seg_az = culled_az[start:stop]
                seg_h = culled_h[start:stop]
                if len(seg_sx) >= 2:
                    all_sx.append(seg_sx)
                    all_sy.append(seg_sy)
                    all_az.append(seg_az)
                    all_h.append(seg_h)

        if all_sx:
            if terrain_shading_enabled:
                self._fill_shaded_strip_downward_numpy(
                    painter,
                    all_sx,
                    all_sy,
                    all_az,
                    all_h,
                    color,
                    h * 2,
                    sun_alt,
                    sun_az,
                    band_pts,
                    sky_color,
                )
            else:
                self._fill_strip_downward_numpy(
                    painter, all_sx, all_sy, color, h * 2, solid=True
                )
            if shadow_color is not None:
                self._stroke_ridge_lines_numpy(
                    painter,
                    all_sx,
                    all_sy,
                    shadow_color,
                    width=1.2,
                    y_offset=1.15,
                )
            if ridge_color is not None:
                self._stroke_ridge_lines_numpy(
                    painter, all_sx, all_sy, ridge_color, width=0.8
                )
                self._draw_edge_texture_numpy(
                    painter, all_sx, all_sy, all_az, ridge_color, band_pts
                )
            if surface_color is not None:
                self._draw_band_surface_linear(
                    painter,
                    band_pts,
                    proj_fn,
                    h,
                    px_alt,
                    cur_az,
                    az_min,
                    az_max,
                    proj_fn_numpy,
                    surface_color,
                )

    def _draw_band_surface_linear(
        self,
        painter,
        band_pts,
        proj_fn,
        h,
        px_alt,
        cur_az,
        az_min,
        az_max,
        proj_fn_numpy,
        color,
    ):
        az_raw, h_raw = getattr(band_pts, "surface_points", (None, None))
        if az_raw is None:
            return
        valid_raw = getattr(band_pts, "surface_valid_mask", None)
        if valid_raw is None:
            valid_raw = np.ones_like(az_raw, dtype=bool)

        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]
        all_sx = []
        all_sy = []

        for offset in offsets:
            final_az = az_raw + offset
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask):
                continue

            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            culled_valid = np.asarray(valid_raw[mask], dtype=bool)
            if not np.any(culled_valid):
                continue

            if proj_fn_numpy:
                sx, sy_base = proj_fn_numpy(
                    np.zeros_like(culled_az), culled_az
                )
                sy = sy_base + 2.0 - (culled_h * px_alt)
            else:
                sx = []
                sy = []
                for az_value, height_value in zip(culled_az, culled_h):
                    anchor = proj_fn(0, az_value)
                    if anchor:
                        sx.append(anchor[0])
                        sy.append(anchor[1] + 2.0 - height_value * px_alt)
                    else:
                        sx.append(np.nan)
                        sy.append(h * 2)
                sx = np.array(sx)
                sy = np.array(sy)

            finite_mask = np.isfinite(sx) & np.isfinite(sy) & culled_valid
            if not np.any(finite_mask):
                continue
            edge_mask = np.diff(
                np.pad(finite_mask.astype(np.int8), (1, 1), constant_values=0)
            )
            starts = np.where(edge_mask == 1)[0]
            stops = np.where(edge_mask == -1)[0]
            for start, stop in zip(starts, stops):
                seg_sx = sx[start:stop]
                seg_sy = sy[start:stop]
                if len(seg_sx) >= 2:
                    all_sx.append(seg_sx)
                    all_sy.append(seg_sy)

        if all_sx:
            self._stroke_ridge_lines_numpy(
                painter, all_sx, all_sy, color, width=0.9
            )

    def _fill_shaded_strip_downward_numpy(
        self,
        painter,
        list_sx,
        list_sy,
        list_az,
        list_h,
        base_color,
        bottom_y,
        sun_alt,
        sun_az,
        band_pts,
        sky_color,
    ):
        for sx_arr, sy_arr, az_arr, h_arr in zip(
            list_sx, list_sy, list_az, list_h
        ):
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue

            f_sx = np.asarray(sx_arr[valid], dtype=np.float32)
            f_sy = np.asarray(sy_arr[valid], dtype=np.float32)
            f_az = np.asarray(az_arr[valid], dtype=np.float32)
            f_h = np.asarray(h_arr[valid], dtype=np.float32)
            if len(f_sx) < 2:
                continue

            self._fill_strip_downward_numpy(
                painter, [f_sx], [f_sy], base_color, bottom_y, solid=True
            )

            shade_values = self._terrain_shade_values(
                f_az, f_h, sun_alt, sun_az, band_pts
            )
            if np.nanmax(np.abs(shade_values - 1.0)) < 0.006:
                continue

            min_x = float(np.nanmin(f_sx))
            max_x = float(np.nanmax(f_sx))
            if max_x - min_x < 1.0:
                continue

            path = QPainterPath()
            path.moveTo(float(f_sx[0]), float(f_sy[0]))
            for x, y in zip(f_sx[1:], f_sy[1:]):
                path.lineTo(float(x), float(y))
            path.lineTo(float(f_sx[-1]), float(bottom_y))
            path.lineTo(float(f_sx[0]), float(bottom_y))
            path.closeSubpath()

            gradient = QLinearGradient(min_x, 0.0, max_x, 0.0)
            stop_count = min(18, len(f_sx))
            stop_indices = np.linspace(0, len(f_sx) - 1, stop_count).astype(int)
            stops = []
            for idx in stop_indices:
                pos = _clamp01((float(f_sx[idx]) - min_x) / (max_x - min_x))
                stops.append((pos, float(shade_values[idx])))
            stops.sort(key=lambda item: item[0])

            last_pos = -1.0
            for pos, shade in stops:
                if pos <= last_pos + 0.001:
                    continue
                color = _shade_color(base_color, shade, sky_color)
                color.setAlpha(255)
                gradient.setColorAt(pos, color)
                last_pos = pos
            if last_pos < 1.0:
                color = _shade_color(
                    base_color, float(shade_values[-1]), sky_color
                )
                color.setAlpha(255)
                gradient.setColorAt(1.0, color)

            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(gradient))
            painter.drawPath(path)

    def _draw_edge_texture_numpy(
        self, painter, list_sx, list_sy, list_az, color, band_pts
    ):
        band_max_m = float(getattr(band_pts, "band_max", 0.0) or 0.0)
        distance_factor = 1.0 - _distance_haze_factor(band_max_m)
        if distance_factor <= 0.18:
            return

        texture_color = QColor(color)
        texture_color.setAlpha(min(color.alpha(), 5 + int(10 * distance_factor)))
        if texture_color.alpha() <= 0:
            return

        pen = QPen(texture_color)
        pen.setWidthF(0.55)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        for sx_arr, sy_arr, az_arr in zip(list_sx, list_sy, list_az):
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue
            f_sx = np.asarray(sx_arr[valid], dtype=np.float32)
            f_sy = np.asarray(sy_arr[valid], dtype=np.float32)
            f_az = np.asarray(az_arr[valid], dtype=np.float32)
            if len(f_sx) < 2:
                continue

            step = max(2, int(math.ceil(len(f_sx) / 110.0)))
            for i in range(0, len(f_sx), step):
                seed = float(f_az[i]) * 12.9898 + band_max_m * 0.001
                noise = math.sin(seed) * 43758.5453
                noise -= math.floor(noise)
                if noise < 0.52:
                    continue
                length = 0.6 + 2.0 * noise * distance_factor
                x = float(f_sx[i])
                y = float(f_sy[i]) + 0.55
                painter.drawLine(QPointF(x, y), QPointF(x, y + length))

    def _draw_ground_linear(
        self,
        painter,
        band_pts,
        color,
        proj_fn,
        w,
        h,
        px_alt,
        cur_az,
        az_min,
        az_max,
        overlap_px=0.0,
        projection_fn_numpy=None,
        ridge_color=None,
    ):
        """
        Draw ground fill using the same projection logic as bands.
        """
        az_raw, h_raw = band_pts.points
        if az_raw is None:
            return

        base_offset = round((cur_az - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        all_sx = []
        all_sy = []

        for offset in offsets:
            final_az = az_raw + offset
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask):
                continue

            culled_az = final_az[mask]
            culled_h = h_raw[mask]

            if projection_fn_numpy:
                sx, sy_base = projection_fn_numpy(
                    np.zeros_like(culled_az), culled_az
                )
                sy = sy_base + 2.0 - (culled_h * px_alt) - overlap_px
            else:
                sx = []
                sy = []
                for a_val, h_val in zip(culled_az, culled_h):
                    anchor = proj_fn(0, a_val)
                    if anchor:
                        sx.append(anchor[0])
                        sy.append(
                            anchor[1] + 2.0 - h_val * px_alt - overlap_px
                        )
                    else:
                        sx.append(np.nan)
                        sy.append(h * 2)
                sx = np.array(sx)
                sy = np.array(sy)

            all_sx.append(sx)
            all_sy.append(sy)

        if all_sx:
            self._fill_strip_downward_numpy(
                painter, all_sx, all_sy, color, h * 2, solid=True
            )
            if ridge_color is not None:
                self._stroke_ridge_lines_numpy(
                    painter, all_sx, all_sy, ridge_color, width=0.65
                )

    def _stroke_ridge_lines_numpy(
        self, painter, list_sx, list_sy, color, width=1.0, y_offset=0.0
    ):
        pen = QPen(color)
        pen.setWidthF(float(width))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        for sx_arr, sy_arr in zip(list_sx, list_sy):
            if len(sx_arr) < 2:
                continue

            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue

            f_sx = sx_arr[valid]
            f_sy = sy_arr[valid] + float(y_offset)
            if len(f_sx) < 2:
                continue

            path = QPainterPath()
            path.moveTo(float(f_sx[0]), float(f_sy[0]))
            for x, y in zip(f_sx[1:], f_sy[1:]):
                path.lineTo(float(x), float(y))
            painter.drawPath(path)

    def _fill_strip_downward_numpy(
        self, painter, list_sx, list_sy, color, bottom_y, solid=False
    ):
        """Vectorized polygon drawing from NumPy arrays."""
        painter.setBrush(QBrush(color))
        if solid:
            painter.setPen(Qt.NoPen)
        else:
            painter.setPen(QPen(color, 1))

        for sx_arr, sy_arr in zip(list_sx, list_sy):
            if len(sx_arr) < 2:
                continue

            # 1. Filter out NaNs/Infs (projection singularities)
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid):
                continue

            f_sx = sx_arr[valid]
            f_sy = sy_arr[valid]

            if len(f_sx) < 2:
                continue

            # Constructing QPolygonF from list of QPointF
            # Convert to float explicit for compatibility
            pts = [QPointF(float(x), float(y)) for x, y in zip(f_sx, f_sy)]

            # Close downward
            pts.append(QPointF(float(f_sx[-1]), float(bottom_y)))
            pts.append(QPointF(float(f_sx[0]), float(bottom_y)))

            poly = QPolygonF(pts)
            painter.drawPolygon(poly)

    # ── procedural fallback ──

    def _build_procedural_fallback(self):
        print(
            "[HorizonOverlay] WARNING: Using procedural fallback (South Flat / North Mountains)."
        )
        rng = random.Random(42)

        # 3 simple layers matching POC colors
        configs = [
            (
                "far_25_60",
                QColor(38, 48, 68),
                QColor(140, 155, 175),
                0.6,
                3.0,
                1.5,
            ),
            (
                "mid_3_10",
                QColor(18, 25, 42),
                QColor(100, 120, 135),
                0.8,
                2.0,
                2.0,
            ),
            (
                "near_0_1",
                QColor(8, 12, 22),
                QColor(70, 90, 100),
                1.0,
                5.0,
                1.0,
            ),
        ]

        for bid, nc, dc, base, freq, amp in configs:
            pts_az = []
            pts_h = []
            # Generate 360 degrees
            for step in range(720):  # 0.5 deg steps
                az = step * 0.5
                # Normalize az to 0..360
                norm_az = az % 360.0

                # Logic: South is approx 90..270. North is 270..360 + 0..90.
                # Let's define "Flat Zone" as 110 to 250 to have some transition

                is_flat = False
                transition = 0.0

                if 135 < norm_az < 225:
                    # Pure Flat
                    val = 0.2
                else:
                    # Mountains
                    rad = math.radians(az)

                    # Noise composition
                    n1 = abs(math.sin(rad * freq)) * amp
                    n2 = abs(math.sin(rad * freq * 2.3)) * (amp * 0.5)
                    n3 = abs(math.sin(rad * freq * 5.1)) * (amp * 0.25)

                    val = base + (n1 + n2 + n3) * rng.uniform(0.9, 1.1)

                    # Smooth transition to flat zone?
                    # Simple lerp if near boundaries (90..135 and 225..270)
                    if 90 < norm_az <= 135:
                        t = (norm_az - 90) / 45.0  # 0..1
                        # 1=Flat, 0=Mount
                        val = val * (1.0 - t) + 0.2 * t
                    elif 225 <= norm_az < 270:
                        t = (norm_az - 225) / 45.0  # 0..1
                        # 0=Flat, 1=Mount
                        val = 0.2 * (1.0 - t) + val * t

                pts_az.append(az)
                pts_h.append(max(0.2, val))

            bp = _BandPoints.__new__(_BandPoints)
            bp.points = (
                np.array(pts_az, dtype=np.float32),
                np.array(pts_h, dtype=np.float32),
            )
            bp.valid_mask = np.ones(len(pts_az), dtype=bool)
            self._layers.append((bp, nc, dc))
