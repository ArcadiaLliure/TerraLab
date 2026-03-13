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
import numpy as np

from PyQt5.QtCore import Qt, QPointF, QObject, pyqtSignal
from PyQt5.QtGui import (
    QColor, QPainter, QPainterPath, QPen, QBrush,
    QLinearGradient, QPolygonF,
)

try:
    from TerraLab.terrain.engine import load_profile, HorizonProfile
    HORIZON_ENGINE_AVAILABLE = True
except ImportError:
    HORIZON_ENGINE_AVAILABLE = False


# ─── Layer definitions ───────────────────────────────────────────

# ─── Layer color palette ─────────────────────────────────────────────────────
#
# Color key-stops for the gradient, from index 0 (farthest/deepest) to 1 (nearest/ground).
# (night_rgb, day_rgb) tuples. We interpolate between these stops.
#
_PALETTE_STOPS = [
    # t=0.0  Deepest Haze (farthest)
    ((60, 70, 90),   (170, 185, 205)),
    # t=0.25 Mid Haze
    ((48, 58, 78),   (145, 160, 180)),
    # t=0.50 Mid-range Green-Blue transition
    ((32, 42, 62),   (105, 120, 135)),
    # t=0.75 Near hills
    ((14, 22, 44),   (75, 90, 75)),
    # t=1.0  Immediate Foreground (nearest)
    ((5, 12, 28),    (55, 70, 55)),
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

    def lerp(a, b, f): return int(a + (b - a) * f)

    night_c = QColor(lerp(nr0, nr1, seg_f), lerp(ng0, ng1, seg_f), lerp(nb0, nb1, seg_f))
    day_c   = QColor(lerp(dr0, dr1, seg_f), lerp(dg0, dg1, seg_f), lerp(db0, db1, seg_f))
    return night_c, day_c


def generate_layer_defs(bands: list) -> list:
    """
    Given a list of band dicts (from engine.generate_bands), produce a LAYER_DEFS-compatible
    list of (band_id, parallax, night_QColor, day_QColor) tuples.

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
        result.append((band["id"], 1.0, night_c, day_c))
    return result


# Static sane default for use before a profile is loaded (20 bands)
LAYER_DEFS = generate_layer_defs(__import__('TerraLab.terrain.engine', fromlist=['generate_bands']).generate_bands(20))






# Ground fill (solid color below the nearest horizon line)
GROUND_NIGHT = QColor(5, 10, 25)
GROUND_DAY   = QColor(55, 70, 55)   # Matches closest band (Dark Forest Green)


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    """Linear interpolation between two QColors."""
    r = c1.red()   + (c2.red()   - c1.red())   * t
    g = c1.green() + (c2.green() - c1.green()) * t
    b = c1.blue()  + (c2.blue()  - c1.blue())  * t
    a = c1.alpha() + (c2.alpha() - c1.alpha()) * t
    return QColor(int(r), int(g), int(b), int(a))


def _calc_t_night(ut_hour: float) -> float:
    """Compute a [0..1] night factor from UTC hour (0=midnight, 12=noon)."""
    val = math.cos((ut_hour / 24.0) * 2 * math.pi)
    t = (val + 1.0) / 2.0
    t = t * t * (3.0 - 2.0 * t)   # smoothstep
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
            if 'k' in s:
                return float(s.replace('k', '')) * 1000.0
            return float(s)
        except ValueError:
            return 9999.0

    parts = band_id.rsplit('_', 2)  # zona + min + max (el max és l'últim segment)
    if len(parts) >= 3:
        return _dist_str_to_m(parts[1]), _dist_str_to_m(parts[2])
    return 0.0, 9999.0


# ─── Band data wrapper ───────────────────────────────────────────

class _BandPoints:
    """Holds (az_deg, elev_deg) points for one profile band."""

    VOID_THRESHOLD = -80.0   # DEM voids are stored as ≈ -90°

    def __init__(self, profile, band_id, vert_exaggeration=5.0):
        self.band_id  = band_id
        # Parsegem min/max de l'ID (ex: "far_25k_38k")
        self.band_min, self.band_max = _parse_band_max_from_id(band_id)
        self.points, self.valid_mask = self._build(profile, band_id, vert_exaggeration)


    # ── private ──

    def _build(self, profile, band_id, vert_exag):
        raw = profile.get_band_points(band_id)
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

    def __init__(self, parent=None, horizon_profile_path=None,
                 vert_exaggeration=1.0, allow_procedural_fallback=True):
        super().__init__(parent)
        self.vert_exaggeration = vert_exaggeration
        self.allow_procedural_fallback = bool(allow_procedural_fallback)
        self._layers = []       # list of (_BandPoints, parallax, night_col, day_col)
        self.profile = None      # Store reference to the current profile
        self._loaded = False

        if not HORIZON_ENGINE_AVAILABLE:
             print("[HorizonOverlay] Horizon Engine NOT available (ImportError).")

        if horizon_profile_path and HORIZON_ENGINE_AVAILABLE:
            print(f"[HorizonOverlay] Attempting to load profile from: {horizon_profile_path}")
            if not os.path.exists(horizon_profile_path):
                print(f"[HorizonOverlay] ERROR: Profile file not found at {horizon_profile_path}")
            
            try:
                profile = load_profile(horizon_profile_path)
                if profile is not None:
                    print(f"[HorizonOverlay] Profile loaded. Processing layers...")
                    for band_id, parallax, night_c, day_c in LAYER_DEFS:
                        bp = _BandPoints(profile, band_id, vert_exaggeration)
                        if bp.points[0] is not None:
                            self._layers.append((bp, parallax, night_c, day_c))
                        else:
                            print(f"[HorizonOverlay]   Band '{band_id}': no data, skipped")
                    self._loaded = bool(self._layers)
                else:
                    print(f"[HorizonOverlay] load_profile returned None.")
            except Exception as e:
                print(f"[HorizonOverlay] Exception loading profile: {e}")

        if not self._layers and self.allow_procedural_fallback:
            print("[HorizonOverlay] No real data loaded — activating procedural fallback.")
            self._build_procedural_fallback()

    # ── public API ──

    def set_profile(self, profile, layer_defs=None):
        """Update the overlay with a new HorizonProfile object (e.g. from background worker).
        
        Args:
            profile: HorizonProfile with baked bands
            layer_defs: Optional list of (band_id, parallax, night_QColor, day_QColor).
                        Generated by overlay.generate_layer_defs(bands). If None, uses LAYER_DEFS.
        """
        if profile is None: return
        self.profile = profile
        
        effective_defs = layer_defs if layer_defs is not None else LAYER_DEFS
        
        # Llavor de soroll baseada en la posició de l'observador per
        # garantir coherència visual entre frames sense flickering.
        import math as _m
        self._noise_seed = (_m.sin(profile.observer_lat * 127.1) *
                            _m.cos(profile.observer_lon * 311.7) * 99.0)
        
        print(f"[HorizonOverlay] Updating profile for {profile.observer_lat}, {profile.observer_lon} ({len(effective_defs)} layers)")
        self._layers.clear()
        
        try:
            for band_id, parallax, night_c, day_c in effective_defs:
                bp = _BandPoints(profile, band_id, self.vert_exaggeration)
                if bp.points[0] is not None:
                    self._layers.append((bp, parallax, night_c, day_c))
            
            self._loaded = bool(self._layers)
            self.request_update.emit()
            
        except Exception as e:
            print(f"[HorizonOverlay] Error setting profile: {e}")

    def clear_profile(self):
        self.profile = None
        self._layers.clear()
        self._loaded = False
        if self.allow_procedural_fallback:
            self._build_procedural_fallback()
        self.request_update.emit()

    def draw(self, painter: QPainter, projection_fn, 
             width: int, height: int,
             current_azimuth: float, zoom_level: float,
             elevation_angle: float, ut_hour: float,
             draw_flat_line: bool = False,
             projection_fn_numpy = None,
             draw_domes_callback = None):
        """
        Main entry: draw all terrain layers.
        If draw_flat_line is True, ignores loaded data/fallback and draws a simple straight line.
        """
        if elevation_angle > 60.0:
            return   # Looking at zenith — skip terrain

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
                painter.drawRect(0, y_draw, width, int(bottom_y * 1.5)) # Big enough to cover
            return

        fov_deg = 100.0 / zoom_level
        px_per_deg_h = width / fov_deg
        center_x = width / 2.0
        vert_scale = self.vert_exaggeration * zoom_level
        px_per_alt_deg = (height / 45.0) * vert_scale

        painter.setRenderHint(QPainter.Antialiasing)

        # Pre-calculate Culling range
        # CULLING_MARGIN: degrees outside viewport to keep for smooth transitions
        culling_margin = 10.0
        az_min = current_azimuth - (fov_deg / 2.0) - culling_margin
        az_max = current_azimuth + (fov_deg / 2.0) + culling_margin

        # ── Precomputa soroll orgànic per a les bandes de primer pla ──────────────
        # Les bandes properes (< NEAR_NOISE_MAX_M) poden tenir siluetes massa planes
        # perquè el pas del baker (50m) o la resolució del DEM no captura microterreny.
        # Injectem harmònics sinusoïdals multi-escala per trencar la línia recta.
        # L'amplitud s'esvaeix linealment fins a zero a NEAR_NOISE_MAX_M.
        #
        # ⚠ Les unitats de h_raw són en graus_elevació × vert_exaggeration.
        # El soroll TAMBÉ ha d'estar en les mateixes unitats per ser visible!

        NEAR_NOISE_MAX_M  = 500.0   # metres — bandes més llunyanes no reben soroll
        NEAR_NOISE_AMP_DEG = 3.0    # amplitud màxima de variació en graus d'elevació
        _NOISE_SEED = getattr(self, '_noise_seed', 0.0)

        def _organic_noise(az_arr, band_max_m, seed, vert_exag):
            """
            Retorna delta en les mateixes unitats que h_raw (graus × vert_exag).

            IMPORTANT: Les freqüències HAN de ser nombres enters perquè el soroll
            sigui periòdic a 360°. Amb freqüència n enter:
              sin(2π × n × (az+360°) + s) = sin(2π × n × az + s)
            garantint continuïtat perfecta al seam 0°/360° (Nord).
            Freqüències no enteres com 1.7 o 4.3 creen un tall visible al Nord.
            """
            if band_max_m >= NEAR_NOISE_MAX_M:
                return None  # Cap soroll per a bandes llunyanes
            # factor d'amplitud: 1.0 a 0m, 0.0 a NEAR_NOISE_MAX_M
            frac = 1.0 - (band_max_m / NEAR_NOISE_MAX_M)
            # amp en les mateixes unitats que h_raw
            amp = NEAR_NOISE_AMP_DEG * frac * vert_exag
            az_rad = np.deg2rad(az_arr)
            s = seed
            # Freqüències ENTERES → periodicitat garantida a 0/360°
            # 1 cicle → forma gran de cresteria
            # 3 cicles → detall mig (puigs i valls)
            # 7 cicles → detall fi (roques, irregularitats)
            noise = (
                amp * 0.55 * np.sin(az_rad * 1 + s) +
                amp * 0.30 * np.sin(az_rad * 3 + s * 1.7) +
                amp * 0.15 * np.sin(az_rad * 7 + s * 3.1)
            )
            return noise


        # ── Pre-process Domes (Darrere cap a davant) ─────────────────────────
        pending_domes = []
        if draw_domes_callback and hasattr(self, 'profile') and hasattr(self.profile, 'light_domes'):
             ld = self.profile.light_domes
             lpd = self.profile.light_peak_distances
             n = len(ld)
             # Peak detection to avoid saturation (grouping azimuths)
             for i in range(n):
                 val = ld[i]
                 if val < 0.2: continue # Threshold
                 prev_val = ld[(i - 1) % n]
                 next_val = ld[(i + 1) % n]
                 # Local maximum check
                 if val >= prev_val and val >= next_val:
                     # Simple plateau handling: only pick the first point
                     if val == next_val: continue
                     pending_domes.append({'idx': i, 'dist': lpd[i]})
             pending_domes.sort(key=lambda x: x['dist'], reverse=True)
             
             # Final step: Azimuthal Clustering to avoid 107 centers
             # We group peaks within 15 degrees to consolidate urban centers.
             if pending_domes:
                 clustered = []
                 # Sort by intensity to keep the brightest peak as the cluster center
                 sorted_by_intensity = sorted(pending_domes, key=lambda x: ld[x['idx']], reverse=True)
                 used_indices = set()
                 
                 for d in sorted_by_intensity:
                     if d['idx'] in used_indices: continue
                     
                     # New Cluster
                     center_az = self.profile.azimuths[d['idx']]
                     clustered.append(d)
                     used_indices.add(d['idx'])
                     
                     # Consume neighbors
                     for other in sorted_by_intensity:
                         if other['idx'] in used_indices: continue
                         other_az = self.profile.azimuths[other['idx']]
                         
                         # Shortest angular distance
                         diff = abs(other_az - center_az) % 360
                         if diff > 180: diff = 360 - diff
                         
                         if diff < 15.0: # 15 degree cluster radius
                             used_indices.add(other['idx'])
                 
                 pending_domes = sorted(clustered, key=lambda x: x['dist'], reverse=True)

        # ── Dibuix de cada banda de darrera cap a davant ─────────────────────────
        for band_pts, parallax, night_c, day_c in self._layers:
            # First: Draw any domes that are behind or within this band (further than band_min)
            while pending_domes and pending_domes[0]['dist'] >= band_pts.band_min:
                d_info = pending_domes.pop(0)
                draw_domes_callback(painter, d_info['idx'], d_info['dist'])

            color = _lerp_color(day_c, night_c, t_night)
            # Injectar soroll orgànic si la banda és dins la zona de primer pla
            band_max_m = getattr(band_pts, 'band_max', NEAR_NOISE_MAX_M)
            noise = _organic_noise(band_pts.points[0], band_max_m, _NOISE_SEED, self.vert_exaggeration) \
                    if band_pts.points[0] is not None else None
            if noise is not None:
                # Aplica el soroll temporalment sense modificar les dades originals
                az_raw, h_raw = band_pts.points
                noisy_pts = (az_raw, h_raw + noise)
                band_pts.points = noisy_pts
                self._draw_band_linear(painter, band_pts, color, projection_fn,
                                       width, height, center_x, px_per_deg_h, px_per_alt_deg,
                                       current_azimuth, parallax, az_min, az_max, projection_fn_numpy)
                band_pts.points = (az_raw, h_raw)  # restaura els punts originals
            else:
                self._draw_band_linear(painter, band_pts, color, projection_fn,
                                       width, height, center_x, px_per_deg_h, px_per_alt_deg,
                                       current_azimuth, parallax, az_min, az_max, projection_fn_numpy)


        # ── Farciment del terra amb gradient de perspectiva ───────────────────────
        # Simulem el pla de terra que s'allunya amb un gradient fosc→color terra,
        # evitant el rectangle pla uniforme que trenca el realisme.
        profile_resolved = getattr(self.profile, "resolved_mask", None)
        profile_is_partial = profile_resolved is not None and not bool(np.all(profile_resolved))
        if self._layers and not profile_is_partial:
            ground_c = _lerp_color(GROUND_DAY, GROUND_NIGHT, t_night)
            nearest = self._layers[-1]
            self._draw_ground_linear(painter, nearest[0], ground_c, projection_fn,
                                     width, height, center_x, px_per_deg_h, px_per_alt_deg,
                                     current_azimuth, nearest[1], az_min, az_max,
                                     overlap_px=1.0, projection_fn_numpy=projection_fn_numpy)

    # ── private rendering (Linear Horizontal) ──

    def _draw_band_linear(self, painter, band_pts, color, proj_fn,
                          w, h, cx, px_h, px_alt,
                          cur_az, parallax, az_min, az_max, proj_fn_numpy=None):
        """
        Draw one filled silhouette band using Linear X mapping.
        """
        az_raw, h_raw = band_pts.points
        if az_raw is None: return
        valid_raw = getattr(band_pts, "valid_mask", None)
        if valid_raw is None:
            valid_raw = np.ones_like(az_raw, dtype=bool)

        # Apply parallax shift
        parallax_shift = (parallax - 1.0) * cur_az
        
        # Calculate base offset to center around current azimuth
        # az_raw is 0..360, so center is 180.
        base_offset = round((cur_az - parallax_shift - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        all_sx = []
        all_sy = []

        for offset in offsets:
            final_az = az_raw + parallax_shift + offset
            
            # 1. CULLING: Only keep points within view
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask): continue
            
            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            culled_valid = np.asarray(valid_raw[mask], dtype=bool)
            if not np.any(culled_valid):
                continue
            
            # 2. VECTORIZED PROJECTION
            # Linear X mapping
            sx = cx + (culled_az - cur_az) * px_h
            
            # Curve Y mapping (Stereographic correction)
            if proj_fn_numpy:
                # Optimized vectorized call
                # We only need the Y from the projection as y_base
                _, sy_base = proj_fn_numpy(np.zeros_like(culled_az), culled_az)
                sy = sy_base + 2.0 - (culled_h * px_alt)
            else:
                # Fallback to scalar (slow)
                sy = []
                for a_val, h_val in zip(culled_az, culled_h):
                    anchor = proj_fn(0, a_val)
                    if anchor:
                        sy.append(anchor[1] + 2.0 - h_val * px_alt)
                    else:
                        sy.append(h * 2) # Safety
                sy = np.array(sy)

            finite_mask = np.isfinite(sx) & np.isfinite(sy) & culled_valid
            if not np.any(finite_mask):
                continue
            edge_mask = np.diff(np.pad(finite_mask.astype(np.int8), (1, 1), constant_values=0))
            starts = np.where(edge_mask == 1)[0]
            stops = np.where(edge_mask == -1)[0]
            for start, stop in zip(starts, stops):
                seg_sx = sx[start:stop]
                seg_sy = sy[start:stop]
                if len(seg_sx) >= 2:
                    all_sx.append(seg_sx)
                    all_sy.append(seg_sy)
            
        if all_sx:
            self._fill_strip_downward_numpy(painter, all_sx, all_sy, color, h * 2)


    def _draw_ground_linear(self, painter, band_pts, color, proj_fn,
                            w, h, cx, px_h, px_alt,
                            cur_az, parallax_factor, az_min, az_max, overlap_px=0.0, 
                            projection_fn_numpy=None):
        """
        Draw ground fill using same linear/vectorized logic as bands.
        """
        az_raw, h_raw = band_pts.points
        if az_raw is None: return

        parallax_shift = (parallax_factor - 1.0) * cur_az
        base_offset = round((cur_az - parallax_shift - 180) / 360.0) * 360
        offsets = [base_offset - 360, base_offset, base_offset + 360]

        all_sx = []
        all_sy = []

        for offset in offsets:
            final_az = az_raw + parallax_shift + offset
            mask = (final_az >= az_min) & (final_az <= az_max)
            if not np.any(mask): continue
            
            culled_az = final_az[mask]
            culled_h = h_raw[mask]
            
            sx = cx + (culled_az - cur_az) * px_h
            
            if projection_fn_numpy:
                _, sy_base = projection_fn_numpy(np.zeros_like(culled_az), culled_az)
                sy = sy_base + 2.0 - (culled_h * px_alt) - overlap_px
            else:
                sy = []
                for a_val, h_val in zip(culled_az, culled_h):
                    anchor = proj_fn(0, a_val)
                    if anchor:
                        sy.append(anchor[1] + 2.0 - h_val * px_alt - overlap_px)
                    else:
                        sy.append(h * 2)
                sy = np.array(sy)

            all_sx.append(sx)
            all_sy.append(sy)
            
        if all_sx:
            self._fill_strip_downward_numpy(painter, all_sx, all_sy, color, h * 2, solid=True)

    def _fill_strip_downward_numpy(self, painter, list_sx, list_sy, color, bottom_y, solid=False):
        """Vectorized polygon drawing from NumPy arrays."""
        painter.setBrush(QBrush(color))
        if solid:
            painter.setPen(Qt.NoPen)
        else:
            painter.setPen(QPen(color, 1))

        for sx_arr, sy_arr in zip(list_sx, list_sy):
            if len(sx_arr) < 2: continue
            
            # 1. Filter out NaNs/Infs (projection singularities)
            valid = np.isfinite(sx_arr) & np.isfinite(sy_arr)
            if not np.any(valid): continue
            
            f_sx = sx_arr[valid]
            f_sy = sy_arr[valid]
            
            if len(f_sx) < 2: continue

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
        print("[HorizonOverlay] WARNING: Using procedural fallback (South Flat / North Mountains).")
        rng = random.Random(42)
        
        # 3 simple layers matching POC colors
        configs = [
             ("far_25_60", 1.10, QColor(38, 48, 68), QColor(140, 155, 175), 0.6, 3.0, 1.5),
             ("mid_3_10",  1.22, QColor(18, 25, 42), QColor(100, 120, 135), 0.8, 2.0, 2.0),
             ("near_0_1",  1.40, QColor(8, 12, 22),  QColor(70, 90, 100),   1.0, 5.0, 1.0),
        ]

        for bid, par, nc, dc, base, freq, amp in configs:
            pts_az = []
            pts_h = []
            # Generate 360 degrees
            for step in range(720): # 0.5 deg steps
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
                        t = (norm_az - 90) / 45.0 # 0..1
                        # 1=Flat, 0=Mount
                        val = val * (1.0 - t) + 0.2 * t
                    elif 225 <= norm_az < 270:
                        t = (norm_az - 225) / 45.0 # 0..1
                        # 0=Flat, 1=Mount
                        val = 0.2 * (1.0 - t) + val * t

                pts_az.append(az)
                pts_h.append(max(0.2, val))
            
            bp = _BandPoints.__new__(_BandPoints)
            bp.points = (np.array(pts_az, dtype=np.float32), np.array(pts_h, dtype=np.float32))
            bp.valid_mask = np.ones(len(pts_az), dtype=bool)
            self._layers.append((bp, par, nc, dc))
