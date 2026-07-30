"""Pure domain model and JSON persistence for constellations, groups, and nodes."""

from dataclasses import dataclass, field
import json
import os
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class ConstellationNode:
    ra_deg: float
    dec_deg: float
    star_id: str = ""
    star_name: str = ""
    connect_from_prev: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ra_deg": float(self.ra_deg),
            "dec_deg": float(self.dec_deg),
            "star_id": str(self.star_id),
            "star_name": str(self.star_name),
            "connect_from_prev": bool(self.connect_from_prev),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ConstellationNode":
        return cls(
            ra_deg=float(d.get("ra_deg", d.get("ra", 0.0))),
            dec_deg=float(d.get("dec_deg", d.get("dec", 0.0))),
            star_id=str(d.get("star_id", "")),
            star_name=str(d.get("star_name", "")),
            connect_from_prev=bool(
                d.get("connect_from_prev", d.get("connect", True))
            ),
        )


@dataclass
class ConstellationGroup:
    name: str
    nodes: List[ConstellationNode] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": str(self.name),
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ConstellationGroup":
        nodes_raw = d.get("nodes", [])
        nodes = [ConstellationNode.from_dict(n) for n in nodes_raw]
        return cls(name=str(d.get("name", "Constellation")), nodes=nodes)


def load_constellation_groups_from_json(
    data_path: str,
) -> List[ConstellationGroup]:
    """Loads constellation groups from a JSON file path safely without GUI dependencies."""
    if not data_path or not os.path.exists(data_path):
        return []
    try:
        with open(data_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        groups_raw = raw.get("constellations", [])
        if not isinstance(groups_raw, list):
            return []
        return [
            ConstellationGroup.from_dict(g)
            for g in groups_raw
            if isinstance(g, dict)
        ]
    except Exception:
        return []


def save_constellation_groups_to_json(
    data_path: str, groups: List[ConstellationGroup]
) -> bool:
    """Saves constellation groups to a JSON file path safely without GUI dependencies."""
    if not data_path:
        return False
    try:
        dir_path = os.path.dirname(os.path.abspath(data_path))
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        payload = {"constellations": [g.to_dict() for g in groups]}
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


@dataclass
class ConstellationState:
    """Pure aggregate state for constellation editing, selection, and preview."""

    enabled: bool = False
    visible: bool = True
    snap_radius_px: float = 16.0
    node_pick_radius_px: float = 11.0
    segment_pick_radius_px: float = 9.0

    groups: List[ConstellationGroup] = field(default_factory=list)
    active_group_index: Optional[int] = None
    selected_group_index: Optional[int] = None
    selected_node_index: Optional[int] = None
    selected_segment_index: Optional[int] = None
    selected_segments: Set[Tuple[int, int]] = field(default_factory=set)
    selected_group_indices: Set[int] = field(default_factory=set)

    group_drawing_active: bool = False
    resume_from_node_index: Optional[int] = None
    preview_ra_dec: Optional[Tuple[float, float]] = None
    preview_snapped: bool = False

    def clear_selection(self) -> None:
        self.selected_group_index = None
        self.selected_node_index = None
        self.selected_segment_index = None
        self.selected_segments.clear()
        self.selected_group_indices.clear()
