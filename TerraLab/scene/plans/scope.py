"""Scene plan DTO for scope mask, reticle, and HUD presentation."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class ScopePlan:
    """Renderer-neutral plan describing scope view reticle and HUD metrics."""

    enabled: bool = False
    shape: str = "circle"
    center_px: Optional[Tuple[float, float]] = None
    radius_px: float = 0.0
    rect_size_px: Optional[Tuple[float, float]] = None
    fov_deg: Tuple[float, float] = (0.0, 0.0)
    hud_metrics: Dict[str, Any] = field(default_factory=dict)
