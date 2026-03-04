import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen

from TerraLab.common.utils import getTraduction
from TerraLab.widgets.spherical_math import slerp_arc_points


SkyCoord = Tuple[float, float]  # (alt_deg, az_deg)


@dataclass
class ConstellationNode:
    ra_deg: float
    dec_deg: float
    star_id: str = ""
    star_name: str = ""
    # If False, this node starts a new stroke inside the same constellation group.
    # This allows discontinuous constellations (e.g. Orion-like branching) without
    # forcing users to create a separate group for each stroke.
    connect_from_prev: bool = True


@dataclass
class ConstellationGroup:
    name: str
    nodes: List[ConstellationNode] = field(default_factory=list)


class ConstellationDrawingController:
    """
    Interactive constellation drawing with:
    - camera-locked draw mode
    - snap to visible stars
    - independent groups
    - JSON persistence (name + ordered nodes)
    - selective eraser (node-level and segment-level)
    - persistent labels
    """

    def __init__(self, data_path: str):
        self.data_path = str(data_path)
        self.enabled = False
        self.visible = True
        self.eraser_mode = False
        self.snap_radius_px = 16.0
        self.node_pick_radius_px = 11.0
        self.segment_pick_radius_px = 9.0

        self.groups: List[ConstellationGroup] = []
        self.active_group_index: Optional[int] = None
        self.selected_group_index: Optional[int] = None
        self.selected_node_index: Optional[int] = None
        self.selected_segment_index: Optional[int] = None
        self.selected_segments: Set[Tuple[int, int]] = set()
        self.selected_group_indices: Set[int] = set()
        self.group_drawing_active: bool = False
        self.resume_from_node_index: Optional[int] = None
        self._label_hit_rects: Dict[int, QRectF] = {}
        self.preview_ra_dec: Optional[Tuple[float, float]] = None
        self.preview_snapped: bool = False
        self._undo_stack: List[dict] = []
        self._max_undo_states: int = 256

        self.load()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not self.enabled:
            self.clear_selection()

    def set_visible(self, visible: bool) -> None:
        self.visible = bool(visible)
        if not self.visible:
            self.clear_selection()

    def set_eraser_mode(self, enabled: bool) -> None:
        self.eraser_mode = bool(enabled)

    def clear_selection(self) -> None:
        self.selected_group_index = None
        self.selected_node_index = None
        self.selected_segment_index = None
        self.selected_segments = set()
        self.selected_group_indices = set()
        self.resume_from_node_index = None
        self.preview_ra_dec = None
        self.preview_snapped = False

    def _has_any_selection(self) -> bool:
        return (
            (self.selected_group_index is not None)
            or (self.selected_node_index is not None)
            or (self.selected_segment_index is not None)
            or bool(self.selected_segments)
            or bool(self.selected_group_indices)
        )

    @staticmethod
    def _clone_groups(groups: List[ConstellationGroup]) -> List[ConstellationGroup]:
        cloned: List[ConstellationGroup] = []
        for group in groups:
            cloned.append(
                ConstellationGroup(
                    name=str(group.name),
                    nodes=[
                        ConstellationNode(
                            ra_deg=float(node.ra_deg),
                            dec_deg=float(node.dec_deg),
                            star_id=str(node.star_id or ""),
                            star_name=str(node.star_name or ""),
                            connect_from_prev=bool(node.connect_from_prev),
                        )
                        for node in group.nodes
                    ],
                )
            )
        return cloned

    def _snapshot_state(self) -> dict:
        return {
            "groups": self._clone_groups(self.groups),
            "active_group_index": self.active_group_index,
            "selected_group_index": self.selected_group_index,
            "selected_node_index": self.selected_node_index,
            "selected_segment_index": self.selected_segment_index,
            "selected_segments": set(self.selected_segments),
            "selected_group_indices": set(self.selected_group_indices),
            "group_drawing_active": bool(self.group_drawing_active),
            "resume_from_node_index": self.resume_from_node_index,
            "preview_ra_dec": tuple(self.preview_ra_dec) if self.preview_ra_dec is not None else None,
            "preview_snapped": bool(self.preview_snapped),
        }

    def _restore_snapshot(self, snap: dict) -> None:
        self.groups = self._clone_groups(list(snap.get("groups", [])))
        self.active_group_index = snap.get("active_group_index")
        self.selected_group_index = snap.get("selected_group_index")
        self.selected_node_index = snap.get("selected_node_index")
        self.selected_segment_index = snap.get("selected_segment_index")
        self.selected_segments = set(
            (int(t[0]), int(t[1]))
            for t in snap.get("selected_segments", set())
            if isinstance(t, (tuple, list)) and len(t) == 2
        )
        self.selected_group_indices = set(snap.get("selected_group_indices", set()))
        self.group_drawing_active = bool(snap.get("group_drawing_active", False))
        self.resume_from_node_index = snap.get("resume_from_node_index")
        prv = snap.get("preview_ra_dec")
        if isinstance(prv, (tuple, list)) and len(prv) == 2:
            self.preview_ra_dec = (float(prv[0]), float(prv[1]))
        else:
            self.preview_ra_dec = None
        self.preview_snapped = bool(snap.get("preview_snapped", False))

    def _push_undo_state(self) -> None:
        snap = self._snapshot_state()
        if self._undo_stack and self._undo_stack[-1] == snap:
            return
        self._undo_stack.append(snap)
        if len(self._undo_stack) > self._max_undo_states:
            self._undo_stack = self._undo_stack[-self._max_undo_states :]

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        snap = self._undo_stack.pop()
        self._restore_snapshot(snap)
        self.save()
        return True

    def next_default_name(self) -> str:
        base = getTraduction("Astro.ConstellationDefaultName", "Constellation")
        return f"{base} {len(self.groups) + 1}"

    def create_group(self, name: Optional[str] = None, record_undo: bool = True) -> int:
        if record_undo:
            self._push_undo_state()
        group_name = str(name or "").strip() or self.next_default_name()
        self.groups.append(ConstellationGroup(name=group_name))
        self.active_group_index = len(self.groups) - 1
        self.selected_group_index = self.active_group_index
        self.selected_node_index = None
        self.selected_segment_index = None
        self.selected_segments = set()
        self.selected_group_indices = {int(self.active_group_index)}
        self.group_drawing_active = True
        self.resume_from_node_index = None
        self.save()
        return self.active_group_index

    def finish_active_group(self) -> bool:
        gi = self.active_group_index
        if gi is None or not (0 <= gi < len(self.groups)):
            self.group_drawing_active = False
            self.resume_from_node_index = None
            self.preview_ra_dec = None
            self.preview_snapped = False
            return False

        group = self.groups[gi]
        self._push_undo_state()
        if len(group.nodes) == 0:
            del self.groups[gi]
            self.active_group_index = min(gi, len(self.groups) - 1) if self.groups else None
            self.selected_group_index = self.active_group_index
            self.selected_node_index = None
            self.selected_segment_index = None
            self.selected_segments = set()
            self.selected_group_indices = {int(self.active_group_index)} if self.active_group_index is not None else set()
            self.group_drawing_active = False
            self.resume_from_node_index = None
            self.preview_ra_dec = None
            self.preview_snapped = False
            self.save()
            return True

        self.group_drawing_active = False
        self.resume_from_node_index = None
        self.selected_group_index = gi
        self.selected_node_index = None
        self.selected_segment_index = None
        self.selected_segments = set()
        self.selected_group_indices = {int(gi)}
        self.preview_ra_dec = None
        self.preview_snapped = False
        self.save()
        return True

    def rename_active(self, new_name: str) -> bool:
        if self.active_group_index is None:
            return False
        if not (0 <= self.active_group_index < len(self.groups)):
            return False
        name = str(new_name or "").strip()
        if not name:
            return False
        if self.groups[self.active_group_index].name == name:
            return False
        self._push_undo_state()
        self.groups[self.active_group_index].name = name
        self.save()
        return True

    def delete_selected(self) -> bool:
        self._normalize_selected_segments()
        if len(self.selected_segments) > 1:
            return self.delete_selected_segments() > 0
        if len(self.selected_segments) == 1:
            gi_s, si_s = next(iter(self.selected_segments))
            self.selected_group_index = gi_s
            self.selected_node_index = None
            self.selected_segment_index = si_s

        gi = self.selected_group_index
        ni = self.selected_node_index
        si = self.selected_segment_index
        if gi is None or not (0 <= gi < len(self.groups)):
            return self.delete_selected_groups() > 0

        if (si is None) and (ni is None) and len(self.selected_group_indices) > 1:
            return self.delete_selected_groups() > 0

        if si is not None:
            return self._delete_segment(gi, si, record_undo=True)
        if ni is None:
            self._push_undo_state()
            del self.groups[gi]
            self.active_group_index = min(gi, len(self.groups) - 1) if self.groups else None
            self.selected_group_index = self.active_group_index
            self.selected_node_index = None
            self.selected_segment_index = None
            self.selected_segments = set()
            self.selected_group_indices = {int(self.active_group_index)} if self.active_group_index is not None else set()
            self.resume_from_node_index = None
            self.save()
            return True
        nodes = self.groups[gi].nodes
        if not (0 <= ni < len(nodes)):
            return False
        self._push_undo_state()
        next_connect = False
        if (ni + 1) < len(nodes):
            next_connect = bool(nodes[ni + 1].connect_from_prev)
        del nodes[ni]
        if not nodes:
            del self.groups[gi]
            self.active_group_index = min(gi, len(self.groups) - 1) if self.groups else None
            self.selected_group_index = self.active_group_index
            self.selected_node_index = None
            self.selected_segment_index = None
            self.selected_segments = set()
            self.selected_group_indices = {int(self.active_group_index)} if self.active_group_index is not None else set()
            self.resume_from_node_index = None
        else:
            if ni < len(nodes):
                # Keep path continuity semantics after removing an intermediate node.
                nodes[ni].connect_from_prev = bool(next_connect)
            self._normalize_group_connections(self.groups[gi])
            self.selected_node_index = min(ni, len(nodes) - 1)
            self.selected_segment_index = None
            self.selected_segments = set()
            self.active_group_index = gi
            self.selected_group_index = gi
            self.selected_group_indices = {int(gi)}
            self.resume_from_node_index = None
        self.save()
        return True

    def has_deletable_selection(self) -> bool:
        self._normalize_selected_segments()
        if self.selected_segments:
            return True
        if self.selected_node_index is not None:
            gi = self.selected_group_index
            ni = self.selected_node_index
            return (
                gi is not None
                and 0 <= gi < len(self.groups)
                and ni is not None
                and 0 <= ni < len(self.groups[gi].nodes)
            )
        if self.selected_segment_index is not None:
            gi = self.selected_group_index
            si = self.selected_segment_index
            if gi is None or not (0 <= gi < len(self.groups)):
                return False
            nodes = self.groups[gi].nodes
            return 1 <= si < len(nodes) and bool(nodes[si].connect_from_prev)
        # Only allow constellation-level delete from keyboard when it's an explicit multi-selection.
        return len(self.selected_group_indices) > 1

    def toggle_group_multi_selection(self, group_index: int) -> bool:
        gi = int(group_index)
        if not (0 <= gi < len(self.groups)):
            return False
        if gi in self.selected_group_indices:
            self.selected_group_indices.remove(gi)
        else:
            self.selected_group_indices.add(gi)
        self.active_group_index = gi
        self.selected_group_index = gi
        self.selected_node_index = None
        self.selected_segment_index = None
        self.selected_segments = set()
        self.resume_from_node_index = None
        return True

    def delete_selected_groups(self, record_undo: bool = True) -> int:
        valid = {int(i) for i in self.selected_group_indices if 0 <= int(i) < len(self.groups)}
        if not valid:
            return 0
        if record_undo:
            self._push_undo_state()
        self.groups = [g for idx, g in enumerate(self.groups) if idx not in valid]
        removed = len(valid)
        if self.groups:
            self.active_group_index = min(self.active_group_index or 0, len(self.groups) - 1)
            self.selected_group_index = self.active_group_index
            self.selected_group_indices = {int(self.active_group_index)} if self.active_group_index is not None else set()
        else:
            self.active_group_index = None
            self.selected_group_index = None
            self.selected_group_indices = set()
        self.selected_node_index = None
        self.selected_segment_index = None
        self.selected_segments = set()
        self.resume_from_node_index = None
        self.save()
        return removed

    def delete_selected_segments(self, record_undo: bool = True) -> int:
        self._normalize_selected_segments()
        if not self.selected_segments:
            return 0
        if record_undo:
            self._push_undo_state()
        deleted = 0
        touched_groups: Set[int] = set()
        for gi, si in sorted(self.selected_segments):
            if not (0 <= gi < len(self.groups)):
                continue
            nodes = self.groups[gi].nodes
            if not (1 <= si < len(nodes)):
                continue
            if not bool(nodes[si].connect_from_prev):
                continue
            nodes[si].connect_from_prev = False
            self._normalize_group_connections(self.groups[gi])
            deleted += 1
            touched_groups.add(int(gi))
        self._remove_groups_without_segments(touched_groups)
        self.selected_segments = set()
        self.selected_segment_index = None
        self.selected_node_index = None
        if deleted > 0:
            self.save()
        return deleted

    def _delete_segment(self, group_index: int, segment_index: int, record_undo: bool = True) -> bool:
        if not (0 <= group_index < len(self.groups)):
            return False
        group = self.groups[group_index]
        nodes = group.nodes
        n = len(nodes)
        if n < 2:
            return False
        if not (1 <= segment_index < n):
            return False
        if not bool(nodes[segment_index].connect_from_prev):
            return False
        if record_undo:
            self._push_undo_state()

        # Segment deletion in graph-like constellations should break only the edge,
        # not destroy nodes or force group splitting.
        nodes[segment_index].connect_from_prev = False
        self._normalize_group_connections(group)

        removed = self._remove_groups_without_segments({int(group_index)})
        if removed:
            self.save()
            return True

        self.active_group_index = group_index
        self.selected_group_index = group_index
        self.selected_node_index = None
        self.selected_segment_index = None
        self.selected_segments = set()
        self.selected_group_indices = set()
        self.resume_from_node_index = None
        self.save()
        return True

    def clear_all(self) -> None:
        if self.groups:
            self._push_undo_state()
        self.groups = []
        self.active_group_index = None
        self.selected_group_index = None
        self.selected_node_index = None
        self.selected_segment_index = None
        self.selected_segments = set()
        self.selected_group_indices = set()
        self.resume_from_node_index = None
        self.preview_ra_dec = None
        self.preview_snapped = False
        self.save()

    def _normalize_selected_segments(self) -> None:
        valid: Set[Tuple[int, int]] = set()
        for gi, si in self.selected_segments:
            if not (0 <= gi < len(self.groups)):
                continue
            nodes = self.groups[gi].nodes
            if not (1 <= si < len(nodes)):
                continue
            if not bool(nodes[si].connect_from_prev):
                continue
            valid.add((int(gi), int(si)))
        self.selected_segments = valid

    @staticmethod
    def _group_has_drawn_segments(group: ConstellationGroup) -> bool:
        return any(bool(group.nodes[i].connect_from_prev) for i in range(1, len(group.nodes)))

    def _remove_groups_without_segments(self, group_indices: Set[int]) -> Set[int]:
        candidates = {int(i) for i in group_indices if 0 <= int(i) < len(self.groups)}
        if not candidates:
            return set()

        removed = {
            int(gi)
            for gi in candidates
            if not self._group_has_drawn_segments(self.groups[int(gi)])
        }
        if not removed:
            return set()

        old_active = self.active_group_index
        self.groups = [g for idx, g in enumerate(self.groups) if idx not in removed]

        if self.groups:
            if (old_active is not None) and (old_active not in removed):
                shift = sum(1 for r in removed if r < int(old_active))
                self.active_group_index = max(
                    0,
                    min(len(self.groups) - 1, int(old_active) - int(shift)),
                )
            else:
                self.active_group_index = 0
            self.selected_group_index = self.active_group_index
            self.selected_group_indices = {int(self.active_group_index)}
        else:
            self.active_group_index = None
            self.selected_group_index = None
            self.selected_group_indices = set()

        self.selected_node_index = None
        self.selected_segment_index = None
        self.selected_segments = set()
        self.resume_from_node_index = None
        return removed

    @staticmethod
    def _nodes_equivalent(a: ConstellationNode, b: ConstellationNode, eps: float = 1e-6) -> bool:
        a_sid = str(a.star_id or "").strip()
        b_sid = str(b.star_id or "").strip()
        if a_sid and b_sid:
            return a_sid == b_sid
        ra_diff = abs(float(a.ra_deg) - float(b.ra_deg)) % 360.0
        ra_diff = min(ra_diff, 360.0 - ra_diff)
        dec_diff = abs(float(a.dec_deg) - float(b.dec_deg))
        return (ra_diff <= eps) and (dec_diff <= eps)

    def _edge_exists(self, group: ConstellationGroup, a: ConstellationNode, b: ConstellationNode) -> bool:
        nodes = group.nodes
        for i in range(1, len(nodes)):
            if not bool(nodes[i].connect_from_prev):
                continue
            n0 = nodes[i - 1]
            n1 = nodes[i]
            same_dir = self._nodes_equivalent(n0, a) and self._nodes_equivalent(n1, b)
            inv_dir = self._nodes_equivalent(n0, b) and self._nodes_equivalent(n1, a)
            if same_dir or inv_dir:
                return True
        return False

    def _find_equivalent_node_index(self, group: ConstellationGroup, node: ConstellationNode) -> Optional[int]:
        for i in range(len(group.nodes) - 1, -1, -1):
            if self._nodes_equivalent(group.nodes[i], node):
                return i
        return None

    def on_left_click(
        self,
        sx: float,
        sy: float,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        radec_to_sky_fn: Callable[[float, float], Optional[SkyCoord]],
        pick_star_fn: Callable[[float, float, float], Optional[dict]],
        force_add: bool = False,
        additive_select: bool = False,
        allow_when_disabled: bool = False,
    ) -> bool:
        if not (self.enabled or bool(allow_when_disabled)):
            return False

        label_hit = self._pick_label(sx, sy)
        if label_hit is not None and (not force_add):
            if additive_select:
                self.toggle_group_multi_selection(label_hit)
            else:
                self.active_group_index = label_hit
                self.selected_group_index = label_hit
                self.selected_node_index = None
                self.selected_segment_index = None
                self.selected_segments = set()
                self.selected_group_indices = {int(label_hit)}
                self.resume_from_node_index = None
            return True

        node_hit = self._pick_node(sx, sy, project_fn, radec_to_sky_fn)
        seg_hit = self._pick_segment(sx, sy, project_fn, radec_to_sky_fn)

        if self.eraser_mode:
            if node_hit is not None:
                self.selected_group_index, self.selected_node_index = node_hit
                self.selected_segment_index = None
                self.delete_selected()
                return True
            if seg_hit is not None:
                self.selected_group_index, self.selected_segment_index = seg_hit
                self.selected_node_index = None
                self.delete_selected()
                return True
            return True

        if (node_hit is not None) and (not force_add):
            # Allow closing the active constellation by clicking the first node again.
            hit_gi, hit_ni = node_hit
            if additive_select:
                self.toggle_group_multi_selection(hit_gi)
                return True
            if (
                self.group_drawing_active
                and (self.active_group_index is not None)
                and hit_gi == self.active_group_index
                and (0 <= hit_gi < len(self.groups))
            ):
                group_nodes = self.groups[hit_gi].nodes
                # First click when resuming drawing defines the anchor point.
                if self.resume_from_node_index is None:
                    self.resume_from_node_index = hit_ni
                    n = group_nodes[hit_ni]
                    self.selected_group_index = hit_gi
                    self.selected_node_index = hit_ni
                    self.selected_segment_index = None
                    self.selected_segments = set()
                    self.selected_group_indices = {int(hit_gi)}
                    self.preview_ra_dec = (float(n.ra_deg), float(n.dec_deg))
                    self.preview_snapped = bool(n.star_id)
                    return True

                start_idx = int(self.resume_from_node_index)
                if not (0 <= start_idx < len(group_nodes)):
                    self.resume_from_node_index = hit_ni
                    n = group_nodes[hit_ni]
                    self.selected_group_index = hit_gi
                    self.selected_node_index = hit_ni
                    self.selected_segment_index = None
                    self.selected_segments = set()
                    self.selected_group_indices = {int(hit_gi)}
                    self.preview_ra_dec = (float(n.ra_deg), float(n.dec_deg))
                    self.preview_snapped = bool(n.star_id)
                    return True

                # Clicking the same anchor keeps it as current origin.
                if hit_ni == start_idx:
                    n = group_nodes[hit_ni]
                    self.selected_group_index = hit_gi
                    self.selected_node_index = hit_ni
                    self.selected_segment_index = None
                    self.selected_segments = set()
                    self.selected_group_indices = {int(hit_gi)}
                    self.preview_ra_dec = (float(n.ra_deg), float(n.dec_deg))
                    self.preview_snapped = bool(n.star_id)
                    return True

                start_node = group_nodes[start_idx]
                target = group_nodes[hit_ni]
                if self._edge_exists(self.groups[hit_gi], start_node, target):
                    # Prevent duplicate segment: just move anchor.
                    self.resume_from_node_index = hit_ni
                    self.selected_group_index = hit_gi
                    self.selected_node_index = hit_ni
                    self.selected_segment_index = None
                    self.selected_segments = set()
                    self.selected_group_indices = {int(hit_gi)}
                    self.preview_ra_dec = (float(target.ra_deg), float(target.dec_deg))
                    self.preview_snapped = bool(target.star_id)
                    return True

                self._push_undo_state()
                if start_idx != (len(group_nodes) - 1):
                    anchor = group_nodes[start_idx]
                    group_nodes.append(
                        ConstellationNode(
                            ra_deg=float(anchor.ra_deg),
                            dec_deg=float(anchor.dec_deg),
                            star_id=str(anchor.star_id or ""),
                            star_name=str(anchor.star_name or ""),
                            connect_from_prev=False,
                        )
                    )
                group_nodes.append(
                    ConstellationNode(
                        ra_deg=float(target.ra_deg),
                        dec_deg=float(target.dec_deg),
                        star_id=str(target.star_id or ""),
                        star_name=str(target.star_name or ""),
                        connect_from_prev=True,
                    )
                )
                self.selected_group_index = hit_gi
                self.selected_node_index = len(group_nodes) - 1
                self.selected_segment_index = None
                self.selected_segments = set()
                self.selected_group_indices = {int(hit_gi)}
                self.resume_from_node_index = len(group_nodes) - 1
                self.preview_ra_dec = (float(target.ra_deg), float(target.dec_deg))
                self.preview_snapped = bool(target.star_id)
                self._normalize_group_connections(self.groups[hit_gi])
                self.save()
                return True

            self.selected_group_index, self.selected_node_index = node_hit
            self.selected_segment_index = None
            self.selected_segments = set()
            self.active_group_index = self.selected_group_index
            self.selected_group_indices = {int(hit_gi)}
            if (
                self.group_drawing_active
                and (self.active_group_index is not None)
                and (self.active_group_index == hit_gi)
            ):
                # Re-anchor the next segment from this node to support
                # discontinuous/branched constellations inside one group.
                self.resume_from_node_index = hit_ni
                n = self.groups[hit_gi].nodes[hit_ni]
                self.preview_ra_dec = (float(n.ra_deg), float(n.dec_deg))
                self.preview_snapped = bool(n.star_id)
            return True

        if seg_hit is not None and (not force_add):
            gi, si = seg_hit
            if additive_select:
                key = (int(gi), int(si))
                if key in self.selected_segments:
                    self.selected_segments.remove(key)
                else:
                    self.selected_segments.add(key)
                self._normalize_selected_segments()
                self.active_group_index = gi
                self.selected_group_index = gi
                self.selected_node_index = None
                self.selected_segment_index = None
                self.selected_group_indices = set()
                self.resume_from_node_index = None
            else:
                self.active_group_index = gi
                self.selected_group_index = gi
                self.selected_node_index = None
                self.selected_segment_index = si
                self.selected_segments = {(int(gi), int(si))}
                self.selected_group_indices = set()
                self.resume_from_node_index = None
            return True

        if not self.group_drawing_active:
            if additive_select:
                return False
            had_selection = self._has_any_selection()
            if had_selection:
                self.clear_selection()
                return True
            return False

        star = pick_star_fn(float(sx), float(sy), float(self.snap_radius_px))
        if isinstance(star, dict):
            try:
                ra_deg = float(star.get("ra"))
                dec_deg = float(star.get("dec"))
            except Exception:
                ra_deg = dec_deg = None
            if ra_deg is not None and dec_deg is not None:
                node = ConstellationNode(
                    ra_deg=ra_deg % 360.0,
                    dec_deg=max(-90.0, min(90.0, dec_deg)),
                    star_id=str(star.get("id", "") or ""),
                    star_name=str(star.get("name", "") or ""),
                    connect_from_prev=True,
                )
            else:
                node = None
        else:
            node = None

        # Constellation drawing is strictly star-to-star.
        # If there is no visible star under the cursor, do nothing.
        if node is None:
            return True

        self._push_undo_state()

        if self.active_group_index is None or not (0 <= self.active_group_index < len(self.groups)):
            self.create_group(record_undo=False)

        gi = int(self.active_group_index)
        group_nodes = self.groups[gi].nodes

        anchor_idx = self.resume_from_node_index
        if (anchor_idx is None) and len(group_nodes) > 0:
            # Starting a new stroke in an already partially drawn constellation:
            # first click defines origin and does not connect from the old tail.
            self._push_undo_state()
            group_nodes.append(
                ConstellationNode(
                    ra_deg=float(node.ra_deg),
                    dec_deg=float(node.dec_deg),
                    star_id=str(node.star_id or ""),
                    star_name=str(node.star_name or ""),
                    connect_from_prev=False,
                )
            )
            self._normalize_group_connections(self.groups[gi])
            self.selected_group_index = gi
            self.selected_node_index = len(group_nodes) - 1
            self.selected_segment_index = None
            self.selected_segments = set()
            self.selected_group_indices = {int(gi)}
            self.resume_from_node_index = len(group_nodes) - 1
            self.preview_ra_dec = (node.ra_deg, node.dec_deg)
            self.preview_snapped = bool(node.star_id)
            self.save()
            return True

        if anchor_idx is not None and (0 <= anchor_idx < len(group_nodes)):
            start_node = group_nodes[int(anchor_idx)]
            if self._edge_exists(self.groups[gi], start_node, node):
                # Prevent duplicate segment: just move anchor to the equivalent node if present.
                existing_idx = self._find_equivalent_node_index(self.groups[gi], node)
                if existing_idx is not None:
                    self.resume_from_node_index = int(existing_idx)
                    self.selected_node_index = int(existing_idx)
                self.selected_group_index = gi
                self.selected_segment_index = None
                self.selected_segments = set()
                self.selected_group_indices = {int(gi)}
                self.preview_ra_dec = (float(node.ra_deg), float(node.dec_deg))
                self.preview_snapped = bool(node.star_id)
                return True
            if anchor_idx != (len(group_nodes) - 1):
                anchor = group_nodes[anchor_idx]
                # Pen-lift jump to the chosen anchor so next edge starts here
                # without implicitly connecting from the previous tail.
                group_nodes.append(
                    ConstellationNode(
                        ra_deg=float(anchor.ra_deg),
                        dec_deg=float(anchor.dec_deg),
                        star_id=str(anchor.star_id or ""),
                        star_name=str(anchor.star_name or ""),
                        connect_from_prev=False,
                    )
                )
            self.resume_from_node_index = None

        if len(group_nodes) == 0:
            node.connect_from_prev = False
        else:
            node.connect_from_prev = True

        self.groups[gi].nodes.append(node)
        self._normalize_group_connections(self.groups[gi])
        self.selected_group_index = gi
        self.selected_node_index = len(self.groups[gi].nodes) - 1
        self.selected_segment_index = None
        self.selected_segments = set()
        self.selected_group_indices = {int(gi)}
        self.resume_from_node_index = len(self.groups[gi].nodes) - 1
        self.preview_ra_dec = (node.ra_deg, node.dec_deg)
        self.preview_snapped = bool(node.star_id)
        self.save()
        return True

    def on_mouse_move(
        self,
        sx: float,
        sy: float,
        pick_star_fn: Callable[[float, float, float], Optional[dict]],
        screen_to_radec_fn: Callable[[float, float], Optional[Tuple[float, float]]],
    ) -> bool:
        if not self.enabled:
            return False
        if not self.group_drawing_active:
            self.preview_ra_dec = None
            self.preview_snapped = False
            return False
        if self.eraser_mode:
            self.preview_ra_dec = None
            self.preview_snapped = False
            return False
        gi = self.active_group_index
        if gi is None or not (0 <= gi < len(self.groups)):
            self.preview_ra_dec = None
            self.preview_snapped = False
            return False
        if len(self.groups[gi].nodes) <= 0:
            self.preview_ra_dec = None
            self.preview_snapped = False
            return False

        star = pick_star_fn(float(sx), float(sy), float(self.snap_radius_px))
        if isinstance(star, dict):
            try:
                ra_deg = float(star.get("ra"))
                dec_deg = float(star.get("dec"))
                self.preview_ra_dec = (ra_deg % 360.0, max(-90.0, min(90.0, dec_deg)))
                self.preview_snapped = True
                return True
            except Exception:
                pass

        # Preview can follow cursor, but clicks only commit when snapping to a visible star.
        ra_dec = screen_to_radec_fn(float(sx), float(sy))
        if ra_dec is None:
            self.preview_ra_dec = None
            self.preview_snapped = False
            return False
        self.preview_ra_dec = (float(ra_dec[0]) % 360.0, max(-90.0, min(90.0, float(ra_dec[1]))))
        self.preview_snapped = False
        return True

    def on_double_click(
        self,
        sx: float,
        sy: float,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        radec_to_sky_fn: Callable[[float, float], Optional[SkyCoord]],
        additive_select: bool = False,
        allow_when_disabled: bool = False,
    ) -> Optional[dict]:
        if not (self.enabled or bool(allow_when_disabled)):
            return None

        label_hit = self._pick_label(sx, sy)
        if label_hit is not None:
            if additive_select:
                self.toggle_group_multi_selection(label_hit)
            else:
                self.active_group_index = label_hit
                self.selected_group_index = label_hit
                self.selected_node_index = None
                self.selected_segment_index = None
                self.selected_segments = set()
                self.selected_group_indices = {int(label_hit)}
                self.resume_from_node_index = None
            action = {"action": "rename_group", "group_index": label_hit}
            rect = self._label_hit_rects.get(int(label_hit))
            if rect is not None:
                action["label_rect"] = (
                    float(rect.left()),
                    float(rect.top()),
                    float(rect.width()),
                    float(rect.height()),
                )
            return action

        seg_hit = self._pick_segment(sx, sy, project_fn, radec_to_sky_fn)
        if seg_hit is not None:
            gi, _ = seg_hit
            # Double-click over a segment selects all segments of that constellation.
            if additive_select:
                for i in range(1, len(self.groups[gi].nodes)):
                    if bool(self.groups[gi].nodes[i].connect_from_prev):
                        self.selected_segments.add((int(gi), int(i)))
                self._normalize_selected_segments()
                self.active_group_index = gi
                self.selected_group_index = gi
                self.selected_node_index = None
                self.selected_segment_index = None
                self.selected_group_indices = set()
                self.resume_from_node_index = None
            else:
                self.active_group_index = gi
                self.selected_group_index = gi
                self.selected_node_index = None
                self.selected_segment_index = None
                self.selected_segments = {
                    (int(gi), int(i))
                    for i in range(1, len(self.groups[gi].nodes))
                    if bool(self.groups[gi].nodes[i].connect_from_prev)
                }
                self.selected_group_indices = set()
                self.resume_from_node_index = None
            return {"action": "select_group_segments", "group_index": gi}

        return {"action": "none"}

    def on_right_click(
        self,
        sx: float,
        sy: float,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        radec_to_sky_fn: Callable[[float, float], Optional[SkyCoord]],
    ) -> bool:
        if not self.enabled:
            return False
        node_hit = self._pick_node(sx, sy, project_fn, radec_to_sky_fn)
        if node_hit is not None:
            self.selected_group_index, self.selected_node_index = node_hit
            self.selected_segment_index = None
            self.selected_segments = set()
            self.active_group_index = self.selected_group_index
            self.selected_group_indices = {int(self.selected_group_index)}
            if self.group_drawing_active:
                gi, ni = node_hit
                if self.active_group_index == gi:
                    self.resume_from_node_index = ni
                    n = self.groups[gi].nodes[ni]
                    self.preview_ra_dec = (float(n.ra_deg), float(n.dec_deg))
                    self.preview_snapped = bool(n.star_id)
            return True
        seg_hit = self._pick_segment(sx, sy, project_fn, radec_to_sky_fn)
        if seg_hit is not None:
            gi, si = seg_hit
            self.active_group_index = gi
            self.selected_group_index = gi
            self.selected_node_index = None
            self.selected_segment_index = si
            self.selected_segments = {(int(gi), int(si))}
            self.selected_group_indices = set()
            self.resume_from_node_index = None
            return True
        return True

    def draw(
        self,
        painter: QPainter,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        radec_to_sky_fn: Callable[[float, float], Optional[SkyCoord]],
    ) -> None:
        self._label_hit_rects = {}
        if not self.visible:
            return
        if not self.groups:
            return
        for gi, group in enumerate(self.groups):
            self._draw_group(painter, project_fn, radec_to_sky_fn, gi, group)
        self._draw_preview_segment(painter, project_fn, radec_to_sky_fn)

    def _draw_preview_segment(
        self,
        painter: QPainter,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        radec_to_sky_fn: Callable[[float, float], Optional[SkyCoord]],
    ) -> None:
        if not self.enabled or not self.visible or self.eraser_mode:
            return
        if not self.group_drawing_active:
            return
        gi = self.active_group_index
        if gi is None or not (0 <= gi < len(self.groups)):
            return
        group = self.groups[gi]
        if len(group.nodes) <= 0 or self.preview_ra_dec is None:
            return

        start_idx = len(group.nodes) - 1
        if self.resume_from_node_index is not None and (0 <= self.resume_from_node_index < len(group.nodes)):
            start_idx = int(self.resume_from_node_index)
        start_node = group.nodes[start_idx]
        start_sky = radec_to_sky_fn(start_node.ra_deg, start_node.dec_deg)
        end_sky = radec_to_sky_fn(self.preview_ra_dec[0], self.preview_ra_dec[1])
        if start_sky is None or end_sky is None:
            return
        arc = slerp_arc_points(start_sky, end_sky, n_points=26)
        pts = []
        for alt_deg, az_deg in arc:
            pp = project_fn(alt_deg, az_deg)
            if pp is not None:
                pts.append(QPointF(float(pp[0]), float(pp[1])))
        if len(pts) < 2:
            return

        path = QPainterPath()
        path.moveTo(pts[0])
        for p in pts[1:]:
            path.lineTo(p)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(Qt.NoBrush)
        glow = QColor(150, 220, 255, 140)
        line = QColor(170, 235, 255, 245) if self.preview_snapped else QColor(235, 245, 255, 220)
        pen_glow = QPen(glow, 3.0)
        pen_line = QPen(line, 1.3)
        pen_line.setStyle(Qt.DashLine if not self.preview_snapped else Qt.SolidLine)
        painter.setPen(pen_glow)
        painter.drawPath(path)
        painter.setPen(pen_line)
        painter.drawPath(path)

        tip = pts[-1]
        painter.setPen(QPen(QColor(255, 245, 130, 235) if self.preview_snapped else QColor(210, 230, 255, 210), 1.0))
        painter.setBrush(QColor(25, 30, 40, 170))
        painter.drawEllipse(tip, 4.0 if self.preview_snapped else 3.0, 4.0 if self.preview_snapped else 3.0)
        painter.restore()

    def _draw_group(
        self,
        painter: QPainter,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        radec_to_sky_fn: Callable[[float, float], Optional[SkyCoord]],
        gi: int,
        group: ConstellationGroup,
    ) -> None:
        sky_nodes: List[Optional[SkyCoord]] = []
        screen_nodes: List[Optional[QPointF]] = []
        for node in group.nodes:
            sky = radec_to_sky_fn(node.ra_deg, node.dec_deg)
            sky_nodes.append(sky)
            if sky is None:
                screen_nodes.append(None)
                continue
            pt = project_fn(float(sky[0]), float(sky[1]))
            if pt is None:
                screen_nodes.append(None)
            else:
                screen_nodes.append(QPointF(float(pt[0]), float(pt[1])))

        selected_group = (self.selected_group_index == gi)
        multi_selected_group = gi in self.selected_group_indices
        whole_group_selected = multi_selected_group or (selected_group and (self.selected_node_index is None) and (self.selected_segment_index is None))
        glow_color = QColor(110, 180, 255, 120) if whole_group_selected else QColor(255, 255, 255, 80)
        line_color = QColor(120, 200, 255, 230) if whole_group_selected else QColor(215, 235, 255, 205)

        # Geodesic links on the sky sphere.
        # A segment exists only when node[i].connect_from_prev is True.
        for i in range(1, len(group.nodes)):
            if not bool(group.nodes[i].connect_from_prev):
                continue
            a = sky_nodes[i - 1]
            b = sky_nodes[i]
            if a is None or b is None:
                continue
            arc = slerp_arc_points(a, b, n_points=32)
            pts = []
            for alt_deg, az_deg in arc:
                pp = project_fn(alt_deg, az_deg)
                if pp is not None:
                    pts.append(QPointF(float(pp[0]), float(pp[1])))
            if len(pts) < 2:
                continue
            path = QPainterPath()
            path.moveTo(pts[0])
            for p in pts[1:]:
                path.lineTo(p)

            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            seg_selected = ((gi, i) in self.selected_segments) or (selected_group and (self.selected_segment_index == i))
            painter.setPen(QPen(QColor(255, 240, 120, 135) if seg_selected else glow_color, 3.2 if seg_selected else 3.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
            painter.setPen(QPen(QColor(255, 245, 130, 240) if seg_selected else line_color, 1.35 if seg_selected else 1.15))
            painter.drawPath(path)
            painter.restore()

        # Nodes.
        for ni, pt in enumerate(screen_nodes):
            if pt is None:
                continue
            is_sel = selected_group and (self.selected_node_index == ni)
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            if is_sel:
                painter.setPen(QPen(QColor(255, 245, 120, 230), 1.4))
                painter.setBrush(QColor(30, 30, 35, 200))
                painter.drawEllipse(pt, 5.0, 5.0)
            painter.setPen(QPen(QColor(180, 220, 255, 200), 1.0))
            painter.setBrush(QColor(120, 185, 255, 170))
            painter.drawEllipse(pt, 2.9, 2.9)
            painter.restore()

        # Persistent label per group.
        label_anchor = self._group_label_anchor(screen_nodes)
        if label_anchor is not None:
            rect = self._draw_label(painter, label_anchor.x(), label_anchor.y(), group.name, whole_group_selected or selected_group)
            if rect is not None:
                self._label_hit_rects[gi] = rect

    def _draw_label(self, painter: QPainter, x: float, y: float, text: str, selected: bool) -> Optional[QRectF]:
        if not text:
            return None
        painter.save()
        fm = painter.fontMetrics()
        pad_x = 6.0
        pad_y = 4.0
        txt_w = float(fm.horizontalAdvance(text))
        txt_h = float(fm.lineSpacing())
        box_w = txt_w + 2.0 * pad_x
        box_h = txt_h + 2.0 * pad_y
        # Bottom-centered label placement.
        rect = QRectF(float(x) - box_w * 0.5, float(y) + 10.0, box_w, box_h)
        bg = QColor(10, 14, 28, 185 if selected else 155)
        border = QColor(140, 205, 255, 210) if selected else QColor(200, 220, 245, 150)
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 5.0, 5.0)
        painter.setPen(QColor(245, 250, 255, 235))
        painter.drawText(
            QPointF(rect.left() + pad_x, rect.top() + pad_y + fm.ascent()),
            text,
        )
        painter.restore()
        return rect

    def _pick_label(self, sx: float, sy: float) -> Optional[int]:
        for gi, rect in self._label_hit_rects.items():
            if rect.contains(float(sx), float(sy)):
                return int(gi)
        return None

    def get_label_rect(self, group_index: int) -> Optional[QRectF]:
        rect = self._label_hit_rects.get(int(group_index))
        if rect is None:
            return None
        return QRectF(rect)

    @staticmethod
    def _group_label_anchor(nodes: List[Optional[QPointF]]) -> Optional[QPointF]:
        valid = [p for p in nodes if p is not None]
        if not valid:
            return None
        min_x = min(p.x() for p in valid)
        max_x = max(p.x() for p in valid)
        max_y = max(p.y() for p in valid)
        return QPointF(float((min_x + max_x) * 0.5), float(max_y))

    def _pick_node(
        self,
        sx: float,
        sy: float,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        radec_to_sky_fn: Callable[[float, float], Optional[SkyCoord]],
    ) -> Optional[Tuple[int, int]]:
        best = None
        best_d = float("inf")
        for gi in range(len(self.groups) - 1, -1, -1):
            group = self.groups[gi]
            for ni in range(len(group.nodes) - 1, -1, -1):
                node = group.nodes[ni]
                sky = radec_to_sky_fn(node.ra_deg, node.dec_deg)
                if sky is None:
                    continue
                pp = project_fn(float(sky[0]), float(sky[1]))
                if pp is None:
                    continue
                d = math.hypot(float(sx) - float(pp[0]), float(sy) - float(pp[1]))
                if d <= self.node_pick_radius_px and d < best_d:
                    best = (gi, ni)
                    best_d = d
        return best

    def _pick_segment(
        self,
        sx: float,
        sy: float,
        project_fn: Callable[[float, float], Optional[Tuple[float, float]]],
        radec_to_sky_fn: Callable[[float, float], Optional[SkyCoord]],
    ) -> Optional[Tuple[int, int]]:
        best_segment = None
        best_dist = float("inf")
        for gi in range(len(self.groups) - 1, -1, -1):
            group = self.groups[gi]
            for i in range(1, len(group.nodes)):
                if not bool(group.nodes[i].connect_from_prev):
                    continue
                a = group.nodes[i - 1]
                b = group.nodes[i]
                sa = radec_to_sky_fn(a.ra_deg, a.dec_deg)
                sb = radec_to_sky_fn(b.ra_deg, b.dec_deg)
                if sa is None or sb is None:
                    continue
                pa = project_fn(float(sa[0]), float(sa[1]))
                pb = project_fn(float(sb[0]), float(sb[1]))
                if pa is None or pb is None:
                    continue
                d = self._point_to_segment_dist(float(sx), float(sy), QPointF(float(pa[0]), float(pa[1])), QPointF(float(pb[0]), float(pb[1])))
                if d <= self.segment_pick_radius_px and d < best_dist:
                    best_dist = d
                    best_segment = (gi, i)
        return best_segment

    @staticmethod
    def _point_to_segment_dist(x: float, y: float, a: QPointF, b: QPointF) -> float:
        ax, ay = a.x(), a.y()
        bx, by = b.x(), b.y()
        vx, vy = bx - ax, by - ay
        wx, wy = x - ax, y - ay
        vv = vx * vx + vy * vy
        if vv < 1e-9:
            return math.hypot(x - ax, y - ay)
        t = max(0.0, min(1.0, (wx * vx + wy * vy) / vv))
        px = ax + t * vx
        py = ay + t * vy
        return math.hypot(x - px, y - py)

    @staticmethod
    def _normalize_group_connections(group: ConstellationGroup) -> None:
        if not group.nodes:
            return
        # First node can never connect from previous because there is no previous.
        group.nodes[0].connect_from_prev = False

    def serialize(self) -> dict:
        out_groups = []
        for group in self.groups:
            out_groups.append(
                {
                    "name": str(group.name),
                    "nodes": [
                        {
                            "ra_deg": float(node.ra_deg),
                            "dec_deg": float(node.dec_deg),
                            "star_id": str(node.star_id or ""),
                            "star_name": str(node.star_name or ""),
                            "connect_from_prev": bool(node.connect_from_prev),
                        }
                        for node in group.nodes
                    ],
                }
            )
        return {
            "version": 1,
            "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "constellations": out_groups,
        }

    def save(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self.data_path), exist_ok=True)
            with open(self.data_path, "w", encoding="utf-8") as fh:
                json.dump(self.serialize(), fh, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def load(self) -> bool:
        self._undo_stack = []
        self.groups = []
        self.active_group_index = None
        self.selected_group_index = None
        self.selected_node_index = None
        self.selected_segment_index = None
        self.selected_segments = set()
        self.selected_group_indices = set()
        self.resume_from_node_index = None
        try:
            if not os.path.exists(self.data_path):
                return False
            with open(self.data_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            groups = payload.get("constellations", []) if isinstance(payload, dict) else []
            if not isinstance(groups, list):
                return False
            for group in groups:
                if not isinstance(group, dict):
                    continue
                name = str(group.get("name", "")).strip() or self.next_default_name()
                nodes_raw = group.get("nodes", [])
                if not isinstance(nodes_raw, list):
                    continue
                nodes = []
                for node in nodes_raw:
                    if not isinstance(node, dict):
                        continue
                    try:
                        ra = float(node.get("ra_deg"))
                        dec = float(node.get("dec_deg"))
                    except Exception:
                        continue
                    nodes.append(
                        ConstellationNode(
                            ra_deg=ra % 360.0,
                            dec_deg=max(-90.0, min(90.0, dec)),
                            star_id=str(node.get("star_id", "") or ""),
                            star_name=str(node.get("star_name", "") or ""),
                            connect_from_prev=bool(node.get("connect_from_prev", True)),
                        )
                    )
                if nodes:
                    group_obj = ConstellationGroup(name=name, nodes=nodes)
                    self._normalize_group_connections(group_obj)
                    self.groups.append(group_obj)
            if self.groups:
                self.active_group_index = 0
                self.selected_group_index = 0
                self.selected_group_indices = {0}
            return True
        except Exception:
            self.groups = []
            self.active_group_index = None
            self.selected_group_index = None
            self.selected_node_index = None
            self.selected_segment_index = None
            self.selected_segments = set()
            self.selected_group_indices = set()
            self.resume_from_node_index = None
            return False
