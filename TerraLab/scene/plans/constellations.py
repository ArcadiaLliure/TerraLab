"""Scene plan DTO for constellation polylines, nodes, labels, and drawing previews."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class ConstellationNodePlan:
    group_index: int
    node_index: int
    pos_px: Tuple[float, float]
    ra_deg: float
    dec_deg: float
    star_id: str = ""
    star_name: str = ""
    is_selected: bool = False


@dataclass(frozen=True)
class ConstellationSegmentPlan:
    group_index: int
    start_node_index: int
    end_node_index: int
    pts_px: List[Tuple[float, float]]
    is_selected: bool = False


@dataclass(frozen=True)
class ConstellationLabelPlan:
    group_index: int
    name: str
    pos_px: Tuple[float, float]
    rect_px: Tuple[float, float, float, float]
    is_selected: bool = False


@dataclass(frozen=True)
class ConstellationPlan:
    """Renderer-neutral plan describing visible constellation elements."""

    enabled: bool = False
    visible: bool = True
    nodes: List[ConstellationNodePlan] = field(default_factory=list)
    segments: List[ConstellationSegmentPlan] = field(default_factory=list)
    labels: List[ConstellationLabelPlan] = field(default_factory=list)
    preview_pts_px: Optional[List[Tuple[float, float]]] = None
    preview_snapped: bool = False
