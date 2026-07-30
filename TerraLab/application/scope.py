"""Scope application controller and capability resolution."""

from enum import Enum
import os
from typing import Any, Dict, Optional, Tuple

from TerraLab.scene.scope import (
    FAST_STEP_DEG,
    SHAPE_CIRCLE,
    SHAPE_RECT,
    SLOW_STEP_DEG,
    SPEED_FAST,
    SPEED_SLOW,
    ScopeState,
)
from TerraLab.scene.plans.scope import ScopePlan


class ScopePipeline(str, Enum):
    SCENE = "scene"
    LEGACY = "legacy"


def resolve_scope_pipeline() -> ScopePipeline:
    env_val = (
        os.environ.get("TERRALAB_SCOPE_PIPELINE", "scene").lower().strip()
    )
    if env_val in ("legacy", "0", "false"):
        return ScopePipeline.LEGACY
    return ScopePipeline.SCENE


class ScopeController:
    """Application use-case controller for scope mode."""

    def __init__(self, state: Optional[ScopeState] = None) -> None:
        self.state = state or ScopeState()

    @property
    def enabled(self) -> bool:
        return self.state.enabled

    @enabled.setter
    def enabled(self, val: bool) -> None:
        self.state.enabled = bool(val)

    @property
    def center(self) -> Optional[Tuple[float, float]]:
        return self.state.center

    @center.setter
    def center(self, val: Optional[Tuple[float, float]]) -> None:
        self.state.center = val

    @property
    def awaiting_center_click(self) -> bool:
        return self.state.awaiting_center_click

    @awaiting_center_click.setter
    def awaiting_center_click(self, val: bool) -> None:
        self.state.awaiting_center_click = bool(val)

    @property
    def shape(self) -> str:
        return self.state.shape

    @shape.setter
    def shape(self, val: str) -> None:
        self.state.shape = str(val)

    def activate(self) -> None:
        self.state.activate()

    def deactivate(self) -> None:
        self.state.deactivate()

    def set_shape(self, shape: str) -> None:
        if shape in (SHAPE_CIRCLE, SHAPE_RECT):
            self.state.shape = shape

    def set_speed_mode(self, mode: str) -> None:
        if mode in (SPEED_SLOW, SPEED_FAST):
            self.state.speed_mode = mode

    def set_focal_mm(self, focal_mm: float) -> None:
        self.state.focal_mm = max(1.0, float(focal_mm))

    def set_sensor_key(self, key: str) -> None:
        self.state.sensor_key = str(key)

    def set_aspect_ratio(self, ratio: Optional[float]) -> None:
        if ratio is None:
            self.state.aspect_ratio_override = None
            return
        try:
            r = float(ratio)
            self.state.aspect_ratio_override = max(0.2, min(5.0, r))
        except (ValueError, TypeError):
            pass

    def set_manual_fov(
        self, width_deg: float, height_deg: Optional[float] = None
    ) -> None:
        h = width_deg if height_deg is None else height_deg
        self.state.manual_override = (float(width_deg), float(h))

    def nudge(self, direction: str, speed_mode: Optional[str] = None) -> None:
        mode = speed_mode or self.state.speed_mode
        step = FAST_STEP_DEG if mode == SPEED_FAST else SLOW_STEP_DEG
        d_alt = 0.0
        d_az = 0.0
        dir_lower = direction.lower()
        if "up" in dir_lower or "north" in dir_lower:
            d_alt += step
        if "down" in dir_lower or "south" in dir_lower:
            d_alt -= step
        if "left" in dir_lower or "west" in dir_lower:
            d_az -= step
        if "right" in dir_lower or "east" in dir_lower:
            d_az += step
        self.state.nudge(d_alt, d_az)

    def build_plan(
        self,
        screen_center_px: Optional[Tuple[float, float]],
        viewport_w: float,
        viewport_h: float,
        px_per_deg: float,
        hud_metrics: Optional[Dict[str, Any]] = None,
    ) -> ScopePlan:
        if not self.state.enabled or screen_center_px is None:
            return ScopePlan(enabled=False)

        w_deg, h_deg = self.state.get_fov_deg()
        scale = max(1e-4, float(px_per_deg))
        w_px = w_deg * scale
        h_px = h_deg * scale

        radius_px = (
            min(w_px, h_px) * 0.5 if self.state.shape == SHAPE_CIRCLE else 0.0
        )
        rect_size = (w_px, h_px) if self.state.shape == SHAPE_RECT else None

        return ScopePlan(
            enabled=True,
            shape=self.state.shape,
            center_px=screen_center_px,
            radius_px=radius_px,
            rect_size_px=rect_size,
            fov_deg=(w_deg, h_deg),
            hud_metrics=dict(hud_metrics or {}),
        )
