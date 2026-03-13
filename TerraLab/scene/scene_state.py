"""Scene state used by render pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from TerraLab.scene.camera import Camera


@dataclass
class SceneState:
    camera: Camera

    # Observer/time context
    latitude: float = 0.0
    longitude: float = 0.0
    ut_hour: float = 0.0
    day_of_year: int = 0

    # Sky lighting context
    sun_alt: float = -90.0
    sun_az: float = 0.0

    # Star catalog arrays
    ra: Any = None
    dec: Any = None
    mag: Any = None
    bp_rp: Any = None
    color_r: Any = None
    color_g: Any = None
    color_b: Any = None
    star_ids: Any = None

    # Feature flags and knobs
    magnitude_limit: float = 6.0
    star_scale: float = 1.0
    auto_star_scale_multiplier: float = 1.0
    pure_colors: bool = False
    spike_magnitude_threshold: float = 2.0
    scope_k_fallback: float = 0.2
    bortle: float = 1.0
    is_auto_bortle: bool = False
    scope_enabled: bool = False
    interaction_active: bool = False
    naked_eye_cap: float = 8.0

    # Scope metadata
    scope_shape: str = "circle"
    scope_center_alt: Optional[float] = None
    scope_center_az: Optional[float] = None
    scope_fov_w: Optional[float] = None
    scope_fov_h: Optional[float] = None
    scope_mask_fn: Optional[Callable[[Any, Any], Any]] = None

    # Scene dependencies for optional renderers
    horizon_profile: Any = None
    extras: dict = field(default_factory=dict)
