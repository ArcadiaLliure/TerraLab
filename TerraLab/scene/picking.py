"""Renderer-neutral spatial pick index and pure query resolution."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import numpy as np

from TerraLab.scene.contracts import JSONValue, freeze_json_mapping


@dataclass(frozen=True, slots=True)
class PickRecord:
    """Immutable record for a pickable target in a rendered frame."""

    kind: str
    key: str
    name: str
    alt_deg: float
    az_deg: float
    screen_x: float
    screen_y: float
    radius_px: float = 20.0
    magnitude: float | None = None
    extra_data: Mapping[str, JSONValue] = field(default_factory=dict)

    def to_dict(self, screen_distance: float = 0.0) -> dict[str, Any]:
        """Convert record to a JSON-compatible dict payload matching legacy structure."""
        res: dict[str, Any] = {
            "kind": str(self.kind),
            "key": str(self.key),
            "name": str(self.name),
            "alt": float(self.alt_deg),
            "az": float(self.az_deg) % 360.0,
            "screen_distance": float(screen_distance),
            "screen_x": float(self.screen_x),
            "screen_y": float(self.screen_y),
        }
        if self.kind == "star":
            star_dict: dict[str, Any] = {
                "id": int(self.key) if str(self.key).isdigit() else self.key,
            }
            if self.magnitude is not None:
                star_dict["mag"] = float(self.magnitude)
            ra_val = self.extra_data.get("ra")
            if isinstance(ra_val, (int, float, str)):
                star_dict["ra"] = float(ra_val)
            dec_val = self.extra_data.get("dec")
            if isinstance(dec_val, (int, float, str)):
                star_dict["dec"] = float(dec_val)
            bp_val = self.extra_data.get("bp_rp")
            if isinstance(bp_val, (int, float, str)):
                star_dict["bp_rp"] = float(bp_val)
            res["star"] = star_dict

        elif self.kind == "ngc":
            ra_val = self.extra_data.get("ra")
            if isinstance(ra_val, (int, float, str)):
                res["ra"] = float(ra_val)
            dec_val = self.extra_data.get("dec")
            if isinstance(dec_val, (int, float, str)):
                res["dec"] = float(dec_val)
        elif self.kind == "sky" and "type" in self.extra_data:
            res["type"] = str(self.extra_data["type"])


        if self.magnitude is not None:
            res["mag"] = float(self.magnitude)

        for k, v in self.extra_data.items():
            if k not in res and (
                not isinstance(res.get("star"), dict) or k not in res["star"]
            ):
                res[k] = v
        return res


@dataclass(frozen=True, slots=True)
class PickIndex:
    """Immutable spatial index constructed alongside a SceneFrame generation."""

    generation: int
    sky_objects: tuple[PickRecord, ...] = ()
    ngc_objects: tuple[PickRecord, ...] = ()
    star_catalog_indices: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=np.int32)
    )
    star_screen_x: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=np.float32)
    )
    star_screen_y: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=np.float32)
    )
    star_ra: np.ndarray | None = None
    star_dec: np.ndarray | None = None
    star_mag: np.ndarray | None = None
    star_bp_rp: np.ndarray | None = None
    surface_picker: Callable[[float, float], dict[str, Any] | None] | None = (
        None
    )
    unproject_fn: (
        Callable[[float, float], tuple[float, float] | None] | None
    ) = None

    def query(
        self,
        x: float,
        y: float,
        radius: float = 20.0,
    ) -> dict[str, Any]:
        """Query the index at screen coordinates (x, y) with priority tie-breaker:
        1. Celestial objects (Sun, Moon, Planets) within pick radius
        2. NGC deep sky objects
        3. Stars catalog
        4. Surface / Sky background fallback
        """
        # 1. Sky / Celestial objects (Sun, Moon, Planets)
        nearest_sky: PickRecord | None = None
        nearest_sky_dist = float("inf")
        for obj in self.sky_objects:
            r = max(float(radius), float(obj.radius_px))
            dist = math.hypot(obj.screen_x - x, obj.screen_y - y)
            if dist <= r and dist < nearest_sky_dist:
                nearest_sky = obj
                nearest_sky_dist = dist
        if nearest_sky is not None:
            return nearest_sky.to_dict(screen_distance=nearest_sky_dist)

        # 2. NGC objects
        nearest_ngc: PickRecord | None = None
        nearest_ngc_dist = float("inf")
        for obj in self.ngc_objects:
            r = max(float(radius), float(obj.radius_px))
            dist = math.hypot(obj.screen_x - x, obj.screen_y - y)
            if dist <= r and dist < nearest_ngc_dist:
                nearest_ngc = obj
                nearest_ngc_dist = dist
        if nearest_ngc is not None:
            return nearest_ngc.to_dict(screen_distance=nearest_ngc_dist)

        # 3. Stars catalog
        total_stars = min(
            len(self.star_screen_x),
            len(self.star_screen_y),
            len(self.star_catalog_indices),
        )
        if total_stars > 0:
            dists = np.hypot(
                self.star_screen_x[:total_stars] - float(x),
                self.star_screen_y[:total_stars] - float(y),
            )
            best_idx = int(np.argmin(dists))
            best_dist = float(dists[best_idx])
            if best_dist <= float(radius):
                cat_idx = int(self.star_catalog_indices[best_idx])
                extra: dict[str, JSONValue] = {}
                if self.star_ra is not None and cat_idx < len(self.star_ra):
                    extra["ra"] = float(self.star_ra[cat_idx])
                if self.star_dec is not None and cat_idx < len(self.star_dec):
                    extra["dec"] = float(self.star_dec[cat_idx])
                if self.star_bp_rp is not None and cat_idx < len(
                    self.star_bp_rp
                ):
                    extra["bp_rp"] = float(self.star_bp_rp[cat_idx])
                mag_val = (
                    float(self.star_mag[cat_idx])
                    if self.star_mag is not None
                    and cat_idx < len(self.star_mag)
                    else None
                )
                rec = PickRecord(
                    kind="star",
                    key=str(cat_idx),
                    name=f"Gaia #{cat_idx}",
                    alt_deg=0.0,
                    az_deg=0.0,
                    screen_x=float(self.star_screen_x[best_idx]),
                    screen_y=float(self.star_screen_y[best_idx]),
                    radius_px=radius,
                    magnitude=mag_val,
                    extra_data=freeze_json_mapping(extra),
                )
                return rec.to_dict(screen_distance=best_dist)

        # 4. Surface / Sky background fallback
        if self.surface_picker is not None:
            surf_res = self.surface_picker(x, y)
            if surf_res is not None and surf_res.get("kind") != "none":
                return surf_res

        sky_alt, sky_az = 0.0, 0.0
        if self.unproject_fn is not None:
            res = self.unproject_fn(x, y)
            if res is not None:
                sky_alt, sky_az = res

        return {
            "kind": "sky",
            "alt": float(sky_alt),
            "az": float(sky_az),
        }

    def unproject(self, x: float, y: float) -> tuple[float, float] | None:
        """Return the Model-defined sky coordinate for a normalised pointer."""

        return self.unproject_fn(float(x), float(y)) if self.unproject_fn else None
