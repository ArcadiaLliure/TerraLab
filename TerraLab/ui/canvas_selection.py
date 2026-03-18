"""Selection helpers extracted from AstroCanvas."""

from __future__ import annotations

import math
try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


class CanvasSelection:
    def __init__(self, canvas):
        self._canvas = canvas

    @staticmethod
    def normalize_planet_key(value):
        return str(value or "").strip().lower().replace(" ", "")

    @staticmethod
    def extract_star_coords(star_obj):
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

    def pick_sky_object_at(self, sx: float, sy: float, click_radius: float = 20.0):
        c = self._canvas
        x = float(sx)
        y = float(sy)
        best = None
        best_score = float("inf")
        for item in getattr(c, "visible_sky_objects", []):
            dx = float(item.get("sx", 0.0)) - x
            dy = float(item.get("sy", 0.0)) - y
            d = math.hypot(dx, dy)
            rr = max(float(click_radius), float(item.get("radius_px", 0.0)) + 5.0)
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
        c = self._canvas
        best_star = None
        best_dist = float(click_radius)
        x = float(sx)
        y = float(sy)

        # Fast path: numpy screen buffers from main-thread renderer.
        try:
            if np is not None and hasattr(c, "visible_stars_sx") and hasattr(c, "visible_stars_sy"):
                sx_arr = c.visible_stars_sx
                sy_arr = c.visible_stars_sy
                if len(sx_arr) > 0 and len(sy_arr) > 0 and len(getattr(c, "visible_stars", [])) > 0:
                    dists = np.hypot(sx_arr - x, sy_arr - y)
                    if len(dists) > 0:
                        i = int(np.argmin(dists))
                        d = float(dists[i])
                        if d <= best_dist:
                            star = c._resolve_star_by_index(c.visible_stars[i])
                            if star is not None:
                                best_star = star
                                best_dist = d
        except Exception:
            pass

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

        return best_star

    def pick_target_at(self, sx: float, sy: float):
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

