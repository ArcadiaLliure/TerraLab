"""Selection helpers extracted from AstroCanvas."""

from __future__ import annotations

import math
from TerraLab.common.exception_reporting import log_suppressed_exception

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


class CanvasSelection:
    def __init__(self, canvas):
        self._canvas = canvas

    def _debug_enabled(self) -> bool:
        enabled_fn = getattr(self._canvas, "_goto_debug_enabled", None)
        if callable(enabled_fn):
            try:
                return bool(enabled_fn())
            except Exception:
                return False
        return False

    def _debug_log(self, message: str) -> None:
        """Emet log de pick/goto si el canvas te tracer habilitat."""
        log_fn = getattr(self._canvas, "_goto_debug_log", None)
        if callable(log_fn):
            log_fn(str(message))

    @staticmethod
    def _build_star_from_catalog(
        *,
        index_in_catalog: int,
        ra_catalog,
        dec_catalog,
        mag_catalog,
        bp_rp_catalog=None,
        red_catalog=None,
        green_catalog=None,
        blue_catalog=None,
        star_id_catalog=None,
    ):
        """Construeix un objecte estrella des d'un index de cataleg."""
        try:
            total_rows = min(
                len(ra_catalog),
                len(dec_catalog),
                len(mag_catalog),
            )
        except Exception:
            return None
        if int(index_in_catalog) < 0 or int(index_in_catalog) >= int(total_rows):
            return None

        try:
            right_ascension_deg = float(ra_catalog[index_in_catalog])
            declination_deg = float(dec_catalog[index_in_catalog])
            visual_magnitude = float(mag_catalog[index_in_catalog])
        except Exception:
            return None

        if (
            not math.isfinite(right_ascension_deg)
            or not math.isfinite(declination_deg)
            or not math.isfinite(visual_magnitude)
        ):
            return None

        star_identifier = int(index_in_catalog)
        if star_id_catalog is not None:
            try:
                star_identifier = int(star_id_catalog[index_in_catalog])
            except Exception:
                star_identifier = int(index_in_catalog)

        star_object = {
            "id": int(star_identifier),
            "ra": float(right_ascension_deg) % 360.0,
            "dec": max(-90.0, min(90.0, float(declination_deg))),
            "mag": float(visual_magnitude),
        }

        if bp_rp_catalog is not None:
            try:
                bp_rp_value = float(bp_rp_catalog[index_in_catalog])
                if math.isfinite(bp_rp_value):
                    star_object["bp_rp"] = bp_rp_value
            except Exception:
                log_suppressed_exception(__name__, "CanvasSelection._build_star_from_catalog")

        for channel_name, channel_catalog in (
            ("r", red_catalog),
            ("g", green_catalog),
            ("b", blue_catalog),
        ):
            if channel_catalog is None:
                continue
            try:
                channel_value = float(channel_catalog[index_in_catalog])
                if math.isfinite(channel_value):
                    star_object[channel_name] = channel_value
            except Exception:
                continue

        return star_object

    def _resolve_star_from_visible_index(self, star_index: int):
        """Resol una estrella visible usant el mateix cataleg actiu del render."""
        c = self._canvas
        pw = getattr(c, "parent_widget", None)

        # 1) Cataleg actiu del frame actual (injectat per AstroCanvas.render).
        active_ra_catalog = getattr(c, "_active_catalog_ra", None)
        active_dec_catalog = getattr(c, "_active_catalog_dec", None)
        active_mag_catalog = getattr(c, "_active_catalog_mag", None)
        if (
            active_ra_catalog is not None
            and active_dec_catalog is not None
            and active_mag_catalog is not None
        ):
            star_from_active_catalog = self._build_star_from_catalog(
                index_in_catalog=int(star_index),
                ra_catalog=active_ra_catalog,
                dec_catalog=active_dec_catalog,
                mag_catalog=active_mag_catalog,
                bp_rp_catalog=getattr(c, "_active_catalog_bp_rp", None),
                red_catalog=getattr(c, "_active_catalog_r", None),
                green_catalog=getattr(c, "_active_catalog_g", None),
                blue_catalog=getattr(c, "_active_catalog_b", None),
                star_id_catalog=getattr(c, "_active_catalog_ids", None),
            )
            if star_from_active_catalog is not None:
                try:
                    c._last_star_pick_resolution = {
                        "source": "active_catalog",
                        "visible_index": int(star_index),
                        "star_id": int(star_from_active_catalog.get("id", -1)),
                        "ra": float(star_from_active_catalog.get("ra", 0.0)),
                        "dec": float(star_from_active_catalog.get("dec", 0.0)),
                        "mag": float(star_from_active_catalog.get("mag", 0.0)),
                    }
                except Exception:
                    log_suppressed_exception(__name__, "CanvasSelection._resolve_star_from_visible_index")
                return star_from_active_catalog

        # 2) Fallback scope mentre el cataleg profund encara no esta llest.
        scope_fallback_active = bool(
            getattr(pw, "_stars_fallback_active", False)
        )
        if scope_fallback_active:
            fallback_star = self._build_star_from_catalog(
                index_in_catalog=int(star_index),
                ra_catalog=getattr(pw, "_scope_base_ra", []),
                dec_catalog=getattr(pw, "_scope_base_dec", []),
                mag_catalog=getattr(pw, "_scope_base_mag", []),
                bp_rp_catalog=getattr(pw, "_scope_base_bp_rp", None),
                red_catalog=getattr(pw, "_scope_base_r", None),
                green_catalog=getattr(pw, "_scope_base_g", None),
                blue_catalog=getattr(pw, "_scope_base_b", None),
            )
            if fallback_star is not None:
                try:
                    c._last_star_pick_resolution = {
                        "source": "scope_fallback",
                        "visible_index": int(star_index),
                        "star_id": int(fallback_star.get("id", -1)),
                        "ra": float(fallback_star.get("ra", 0.0)),
                        "dec": float(fallback_star.get("dec", 0.0)),
                        "mag": float(fallback_star.get("mag", 0.0)),
                    }
                except Exception:
                    log_suppressed_exception(__name__, "CanvasSelection._resolve_star_from_visible_index")
                return fallback_star

        # 3) Ruta legacy eliminada expressament:
        # no resolem via index global antic per evitar desalineacions pick/render.
        try:
            c._last_star_pick_resolution = {
                "source": "none",
                "visible_index": int(star_index),
            }
        except Exception:
            log_suppressed_exception(__name__, "CanvasSelection._resolve_star_from_visible_index")
        return None

    @staticmethod
    def normalize_planet_key(value):
        """Executa el metode normalize_planet_key de la classe CanvasSelection.

        Par?metres:
        - value (Any): Valor del parametre 'value'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        return str(value or "").strip().lower().replace(" ", "")

    @staticmethod
    def extract_star_coords(star_obj):
        """Executa el metode extract_star_coords de la classe CanvasSelection.

        Par?metres:
        - star_obj (Any): Valor del parametre 'star_obj'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        if not isinstance(star_obj, dict):
            return None
        try:
            ra = float(star_obj.get("ra"))
            dec = float(star_obj.get("dec"))
        except Exception:
            return None
        if not math.isfinite(ra) or not math.isfinite(dec):
            return None
        return ra % 360.0, max(-90.0, min(90.0, dec))

    def pick_sky_object_at(
        self, sx: float, sy: float, click_radius: float = 20.0
    ):
        """Executa el metode pick_sky_object_at de la classe CanvasSelection.

        Par?metres:
        - sx (float): Valor del parametre 'sx'.
        - sy (float): Valor del parametre 'sy'.
        - click_radius (float): Valor del parametre 'click_radius'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        c = self._canvas
        x = float(sx)
        y = float(sy)
        best = None
        best_score = float("inf")
        for item in getattr(c, "visible_sky_objects", []):
            dx = float(item.get("sx", 0.0)) - x
            dy = float(item.get("sy", 0.0)) - y
            d = math.hypot(dx, dy)
            rr = max(
                float(click_radius), float(item.get("radius_px", 0.0)) + 5.0
            )
            if d > rr:
                continue
            score = d / max(1.0, rr)
            if score < best_score:
                best = item
                best_score = score

        if best is None:
            return None

        obj_type = str(best.get("type", "")).lower()
        if obj_type in ("sun", "moon"):
            return {
                "kind": "sky",
                "type": obj_type,
                "key": obj_type,
                "name": best.get("name", obj_type.title()),
                "alt": best.get("alt"),
                "az": best.get("az"),
            }

        if obj_type == "planet":
            pkey = self.normalize_planet_key(best.get("key"))
            if not pkey:
                pkey = self.normalize_planet_key(best.get("name"))
            return {
                "kind": "sky",
                "type": "planet",
                "key": pkey,
                "name": best.get("name", pkey),
                "alt": best.get("alt"),
                "az": best.get("az"),
            }
        return None

    def pick_star_at(self, sx: float, sy: float, click_radius: float = 20.0):
        """Executa el metode pick_star_at de la classe CanvasSelection.

        Par?metres:
        - sx (float): Valor del parametre 'sx'.
        - sy (float): Valor del parametre 'sy'.
        - click_radius (float): Valor del parametre 'click_radius'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        c = self._canvas
        best_star = None
        best_dist = float(click_radius)
        x = float(sx)
        y = float(sy)

        # Fast path: numpy screen buffers from main-thread renderer.
        try:
            if (
                np is not None
                and hasattr(c, "visible_stars_sx")
                and hasattr(c, "visible_stars_sy")
            ):
                sx_arr = np.asarray(c.visible_stars_sx, dtype=np.float32)
                sy_arr = np.asarray(c.visible_stars_sy, dtype=np.float32)
                vis_idx = getattr(c, "visible_stars", [])
                n = min(len(sx_arr), len(sy_arr), len(vis_idx))
                if n > 0:
                    sx_arr = sx_arr[:n]
                    sy_arr = sy_arr[:n]
                    radius = float(best_dist)
                    radius2 = float(radius * radius)
                    best_i = -1
                    best_d2 = radius2

                    # Coarse screen-space window first; avoid full-size hypot allocation.
                    # For very large buffers, scan in chunks to keep temporary arrays bounded.
                    if n > 200_000:
                        chunk_size = 50_000
                        for chunk_start in range(0, n, chunk_size):
                            chunk_end = min(n, chunk_start + chunk_size)
                            xs = sx_arr[chunk_start:chunk_end]
                            ys = sy_arr[chunk_start:chunk_end]
                            mask = (
                                (xs >= (x - radius))
                                & (xs <= (x + radius))
                                & (ys >= (y - radius))
                                & (ys <= (y + radius))
                            )
                            if not np.any(mask):
                                continue
                            local_idx = np.flatnonzero(mask)
                            dx = xs[local_idx] - x
                            dy = ys[local_idx] - y
                            d2 = (dx * dx) + (dy * dy)
                            if len(d2) <= 0:
                                continue
                            local_best = int(np.argmin(d2))
                            cand_d2 = float(d2[local_best])
                            if cand_d2 <= best_d2:
                                best_d2 = cand_d2
                                best_i = int(chunk_start + local_idx[local_best])
                    else:
                        mask = (
                            (sx_arr >= (x - radius))
                            & (sx_arr <= (x + radius))
                            & (sy_arr >= (y - radius))
                            & (sy_arr <= (y + radius))
                        )
                        if np.any(mask):
                            local_idx = np.flatnonzero(mask)
                            dx = sx_arr[local_idx] - x
                            dy = sy_arr[local_idx] - y
                            d2 = (dx * dx) + (dy * dy)
                            if len(d2) > 0:
                                local_best = int(np.argmin(d2))
                                best_i = int(local_idx[local_best])
                                best_d2 = float(d2[local_best])

                    if best_i >= 0 and best_d2 <= radius2:
                        d = float(math.sqrt(max(0.0, best_d2)))
                        star = self._resolve_star_from_visible_index(
                            int(vis_idx[best_i])
                        )
                        if star is not None:
                            best_star = star
                            best_dist = d
        except Exception:
            log_suppressed_exception(__name__, "CanvasSelection.pick_star_at")

        # Worker path: list[(sx, sy, star_obj)]
        vis = getattr(c, "visible_stars", None)
        if isinstance(vis, list) and vis and isinstance(vis[0], tuple):
            for item in vis:
                if len(item) < 3:
                    continue
                try:
                    sx_i = float(item[0])
                    sy_i = float(item[1])
                except Exception:
                    continue
                d = math.hypot(sx_i - x, sy_i - y)
                if d > best_dist:
                    continue
                star = item[2]
                if self.extract_star_coords(star) is None:
                    continue
                best_star = star
                best_dist = d

        if self._debug_enabled():
            if best_star is not None:
                self._debug_log(
                    "pick_star_at "
                    f"s=({x:.1f},{y:.1f}) "
                    f"dist={float(best_dist):.4f} "
                    f"star={best_star} "
                    f"resolution={getattr(c, '_last_star_pick_resolution', None)}"
                )
            else:
                self._debug_log(f"pick_star_at s=({x:.1f},{y:.1f}) miss")
        return best_star

    def pick_target_at(self, sx: float, sy: float):
        """Executa el metode pick_target_at de la classe CanvasSelection.

        Par?metres:
        - sx (float): Valor del parametre 'sx'.
        - sy (float): Valor del parametre 'sy'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        c = self._canvas
        sky_target = self.pick_sky_object_at(float(sx), float(sy))
        if sky_target is not None:
            return sky_target
        star = self.pick_star_at(float(sx), float(sy))
        if star is not None:
            return {"kind": "star", "star": star}
        ngc_target = c._pick_ngc_at(float(sx), float(sy))
        if ngc_target is not None:
            return ngc_target
        return None
