"""Pure scope, optics, sensor presets, and field-of-view domain models."""

from dataclasses import dataclass
import math
from typing import Dict, Optional, Tuple

SkyCoord = Tuple[float, float]  # (alt_deg, az_deg) or (ra_deg, dec_deg)


@dataclass(frozen=True)
class SensorPreset:
    key: str
    width_mm: float
    height_mm: float


SENSOR_PRESETS: Dict[str, SensorPreset] = {
    "tiny": SensorPreset("tiny", 5.37, 4.04),  # ~1/2.8"
    "aps_c": SensorPreset("aps_c", 23.6, 15.7),  # APS-C
    "full_frame": SensorPreset("full_frame", 36.0, 24.0),
}


SHAPE_CIRCLE = "circle"
SHAPE_RECT = "rectangle"

SPEED_SLOW = "slow"
SPEED_FAST = "fast"

SLOW_HOLD_DEG_PER_S = 0.5 / 60.0  # 0.5 arcmin / s
FAST_HOLD_DEG_PER_S = 0.5  # 0.5 deg / s
SLOW_STEP_DEG = 0.05 / 60.0  # 0.05 arcmin
FAST_STEP_DEG = 0.05  # 0.05 deg


def compute_scope_fov_deg(
    focal_mm: float,
    sensor_key: str = "tiny",
    aspect_ratio_override: Optional[float] = None,
    manual_override: Optional[Tuple[float, float]] = None,
) -> Tuple[float, float]:
    """Computes field of view (width_deg, height_deg) for given optical parameters."""
    if manual_override is not None:
        return float(manual_override[0]), float(manual_override[1])

    preset = SENSOR_PRESETS.get(sensor_key, SENSOR_PRESETS["tiny"])
    focal = max(1.0, float(focal_mm))

    width_deg = 2.0 * math.degrees(math.atan((preset.width_mm * 0.5) / focal))
    height_deg = 2.0 * math.degrees(
        math.atan((preset.height_mm * 0.5) / focal)
    )

    if aspect_ratio_override is not None and aspect_ratio_override > 1e-4:
        height_deg = width_deg / float(aspect_ratio_override)

    return width_deg, height_deg


@dataclass
class ScopeState:
    """Pure mutable aggregate representing scope mode configuration and tracking."""

    enabled: bool = False
    awaiting_center_click: bool = False
    user_center_fixed_once: bool = False
    shape: str = SHAPE_CIRCLE
    speed_mode: str = SPEED_SLOW
    focal_mm: float = 250.0
    sensor_key: str = "tiny"
    aspect_ratio_override: Optional[float] = None
    manual_override: Optional[Tuple[float, float]] = None
    center: Optional[SkyCoord] = None
    dragging: bool = False

    def activate(self) -> None:
        self.enabled = True
        self.user_center_fixed_once = False
        self.awaiting_center_click = self.center is None
        self.dragging = False

    def deactivate(self) -> None:
        self.enabled = False
        self.awaiting_center_click = False
        self.dragging = False

    def get_fov_deg(self) -> Tuple[float, float]:
        return compute_scope_fov_deg(
            self.focal_mm,
            sensor_key=self.sensor_key,
            aspect_ratio_override=self.aspect_ratio_override,
            manual_override=self.manual_override,
        )

    def nudge(self, d_alt_deg: float, d_az_deg: float) -> None:
        if self.center is None:
            return
        alt, az = self.center
        new_alt = max(-90.0, min(90.0, alt + d_alt_deg))
        new_az = (az + d_az_deg) % 360.0
        self.center = (new_alt, new_az)
        self.user_center_fixed_once = True
        self.awaiting_center_click = False
