"""Pure, Qt-free interaction, selection, and measurement plans."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from TerraLab.scene.spherical_math import (
    angular_delta_signed,
    angular_distance,
    destination_point,
    slerp_arc_points,
)

SkyCoord = tuple[float, float]


@dataclass(frozen=True, slots=True)
class SelectionPlan:
    """Immutable plan for selection pulse/overlay rendering."""

    generation: int
    selected_kind: str | None = None
    selected_key: str | None = None
    selected_name: str | None = None
    screen_x: float | None = None
    screen_y: float | None = None
    pulse_phase: float = 0.0
    pulse_radius_px: float = 24.0
    pulse_alpha: float = 1.0


@dataclass(frozen=True, slots=True)
class MeasurementItemPlan:
    """Immutable plan for one measurement primitive (ruler, circle, rectangle, square)."""

    tool: str
    a: SkyCoord
    b: SkyCoord
    rotation_deg: float = 0.0
    label: str = ""
    anchor_sky: SkyCoord = (0.0, 0.0)
    paths_sky: tuple[tuple[SkyCoord, ...], ...] = ()
    handles_sky: Mapping[str, SkyCoord] = field(default_factory=dict)
    hit_polygon_sky: tuple[SkyCoord, ...] | None = None
    selected: bool = False


@dataclass(frozen=True, slots=True)
class MeasurementPlan:
    """Immutable collection of measurement plans for a frame generation."""

    generation: int
    items: tuple[MeasurementItemPlan, ...] = ()
    preview: MeasurementItemPlan | None = None
    active_tool: str = "none"


def build_measurement_item_plan(
    tool: str,
    a: SkyCoord,
    b: SkyCoord,
    rotation_deg: float = 0.0,
    selected: bool = False,
) -> MeasurementItemPlan:
    """Build pure geometry, labels, handles and arc paths for a measurement item."""
    tool = str(tool or "none")
    a_norm = (max(-89.9, min(89.9, float(a[0]))), float(a[1]) % 360.0)
    b_norm = (max(-89.9, min(89.9, float(b[0]))), float(b[1]) % 360.0)

    if tool == "ruler":
        dist = angular_distance(a_norm, b_norm)
        arc = tuple(slerp_arc_points(a_norm, b_norm, n_points=72))
        lbl = f"Distance: {dist:.3f}deg"
        mid = arc[len(arc) // 2] if arc else a_norm
        return MeasurementItemPlan(
            tool="ruler",
            a=a_norm,
            b=b_norm,
            rotation_deg=0.0,
            label=lbl,
            anchor_sky=mid,
            paths_sky=(arc,),
            handles_sky={"a": a_norm, "b": b_norm},
            hit_polygon_sky=None,
            selected=selected,
        )

    if tool == "circle":
        r = max(1e-6, angular_distance(a_norm, b_norm))
        dia = 2.0 * r
        area = math.pi * (dia * 0.5) * (dia * 0.5)
        pts: list[SkyCoord] = []
        steps = 128
        for i in range(steps + 1):
            bearing = 360.0 * i / steps
            pts.append(destination_point(a_norm, bearing, r))
        lbl = f"Diameter: {dia:.3f}deg | Area: {area:.3f} deg2"
        anc = destination_point(a_norm, 45.0, max(r * 0.65, 0.2))
        return MeasurementItemPlan(
            tool="circle",
            a=a_norm,
            b=b_norm,
            rotation_deg=0.0,
            label=lbl,
            anchor_sky=anc,
            paths_sky=(tuple(pts),),
            handles_sky={"center": a_norm, "edge": b_norm},
            hit_polygon_sky=tuple(pts),
            selected=selected,
        )

    if tool in ("square", "rectangle"):
        force_square = tool == "square"
        alt0, az0 = a_norm
        alt1, az1 = b_norm
        width_est = max(1e-6, angular_distance((alt0, az0), (alt0, az1)))
        height_est = max(1e-6, angular_distance((alt0, az0), (alt1, az0)))
        sign_alt = 1.0 if (alt1 - alt0) >= 0.0 else -1.0
        sign_az = 1.0 if angular_delta_signed(az0, az1) >= 0.0 else -1.0
        if force_square:
            side = max(width_est, height_est)
            width, height = side, side
        else:
            width, height = width_est, height_est

        center_alt = max(-89.9, min(89.9, (alt0 + alt1) * 0.5))
        center_az = (az0 + angular_delta_signed(az0, az1) * 0.5) % 360.0
        center = (center_alt, center_az)

        # Build rectangle corner points in sky coordinates
        half_w = max(1e-6, width * 0.5)
        half_h = max(1e-6, height * 0.5)
        rot_rad = math.radians(rotation_deg if not force_square else 0.0)
        cos_lat = max(0.05, math.cos(math.radians(center_alt)))

        base = [
            (-half_w, -half_h),
            (half_w, -half_h),
            (half_w, half_h),
            (-half_w, half_h),
        ]
        corners: list[SkyCoord] = []
        for x, y in base:
            xr = (x * sign_az * math.cos(rot_rad)) - (
                y * sign_alt * math.sin(rot_rad)
            )
            yr = (x * sign_az * math.sin(rot_rad)) + (
                y * sign_alt * math.cos(rot_rad)
            )
            alt_pt = max(-89.9, min(89.9, center_alt + yr))
            az_pt = (center_az + (xr / cos_lat)) % 360.0
            corners.append((alt_pt, az_pt))

        p00, p10, p11, p01 = corners[0], corners[1], corners[2], corners[3]
        e1 = tuple(slerp_arc_points(p00, p10, 24))
        e2 = tuple(slerp_arc_points(p10, p11, 24))
        e3 = tuple(slerp_arc_points(p11, p01, 24))
        e4 = tuple(slerp_arc_points(p01, p00, 24))
        poly = e1 + e2[1:] + e3[1:] + e4[1:]

        area = width * height
        lbl = f"Width: {width:.3f}deg\nHeight: {height:.3f}deg\nArea: {area:.3f} deg2"
        handles = {"origin": p00, "corner": p11}
        paths: list[tuple[SkyCoord, ...]] = [poly]

        if not force_square:
            top_mid_xr = (0.0 * math.cos(rot_rad)) - (
                sign_alt * half_h * math.sin(rot_rad)
            )
            top_mid_yr = (0.0 * math.sin(rot_rad)) + (
                sign_alt * half_h * math.cos(rot_rad)
            )
            top_mid = (
                max(-89.9, min(89.9, center_alt + top_mid_yr)),
                (center_az + (top_mid_xr / cos_lat)) % 360.0,
            )
            rot_dist = half_h + max(0.15, 0.15 * max(width, height))
            rot_xr = (0.0 * math.cos(rot_rad)) - (
                sign_alt * rot_dist * math.sin(rot_rad)
            )
            rot_yr = (0.0 * math.sin(rot_rad)) + (
                sign_alt * rot_dist * math.cos(rot_rad)
            )
            rot_handle = (
                max(-89.9, min(89.9, center_alt + rot_yr)),
                (center_az + (rot_xr / cos_lat)) % 360.0,
            )
            handles["rotate"] = rot_handle
            paths.append(tuple(slerp_arc_points(top_mid, rot_handle, 8)))

        return MeasurementItemPlan(
            tool=tool,
            a=a_norm,
            b=b_norm,
            rotation_deg=rotation_deg,
            label=lbl,
            anchor_sky=center,
            paths_sky=tuple(paths),
            handles_sky=handles,
            hit_polygon_sky=poly,
            selected=selected,
        )

    return MeasurementItemPlan(
        tool="none",
        a=a_norm,
        b=b_norm,
        rotation_deg=0.0,
        label="",
        anchor_sky=a_norm,
        paths_sky=(),
        handles_sky={},
        hit_polygon_sky=None,
        selected=False,
    )
