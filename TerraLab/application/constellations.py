"""Constellation application controller, gesture commands, and capability resolution."""

from enum import Enum
import os
from typing import Any, Callable, List, Optional, Tuple

from TerraLab.scene.constellations import (
    ConstellationGroup,
    ConstellationNode,
    ConstellationState,
    load_constellation_groups_from_json,
    save_constellation_groups_to_json,
)
from TerraLab.scene.plans.constellations import (
    ConstellationLabelPlan,
    ConstellationNodePlan,
    ConstellationPlan,
    ConstellationSegmentPlan,
)
from TerraLab.scene.spherical_math import slerp_arc_points


class ConstellationPipeline(str, Enum):
    SCENE = "scene"
    LEGACY = "legacy"


def resolve_constellation_pipeline() -> ConstellationPipeline:
    env_val = (
        os.environ.get("TERRALAB_CONSTELLATION_PIPELINE", "scene")
        .lower()
        .strip()
    )
    if env_val in ("legacy", "0", "false"):
        return ConstellationPipeline.LEGACY
    return ConstellationPipeline.SCENE


class ConstellationController:
    """Application controller for interactive constellation drawing and editing."""

    def __init__(self, data_path: str = "") -> None:
        self.data_path = str(data_path)
        self.state = ConstellationState()
        self._undo_stack: List[dict] = []
        self._max_undo_states: int = 256
        if self.data_path:
            self.load()

    @property
    def enabled(self) -> bool:
        return self.state.enabled

    @enabled.setter
    def enabled(self, val: bool) -> None:
        self.set_enabled(val)

    @property
    def visible(self) -> bool:
        return self.state.visible

    @visible.setter
    def visible(self, val: bool) -> None:
        self.set_visible(val)

    @property
    def groups(self) -> List[ConstellationGroup]:
        return self.state.groups

    @groups.setter
    def groups(self, val: List[ConstellationGroup]) -> None:
        self.state.groups = list(val)

    @property
    def active_group_index(self) -> Optional[int]:
        return self.state.active_group_index

    @active_group_index.setter
    def active_group_index(self, val: Optional[int]) -> None:
        self.state.active_group_index = val

    @property
    def selected_group_index(self) -> Optional[int]:
        return self.state.selected_group_index

    @selected_group_index.setter
    def selected_group_index(self, val: Optional[int]) -> None:
        self.state.selected_group_index = val

    @property
    def selected_node_index(self) -> Optional[int]:
        return self.state.selected_node_index

    @selected_node_index.setter
    def selected_node_index(self, val: Optional[int]) -> None:
        self.state.selected_node_index = val

    @property
    def selected_segment_index(self) -> Optional[int]:
        return self.state.selected_segment_index

    @selected_segment_index.setter
    def selected_segment_index(self, val: Optional[int]) -> None:
        self.state.selected_segment_index = val

    @property
    def selected_segments(self) -> Any:
        return self.state.selected_segments

    @selected_segments.setter
    def selected_segments(self, val: Any) -> None:
        self.state.selected_segments = set(val)

    @property
    def selected_group_indices(self) -> Any:
        return self.state.selected_group_indices

    @selected_group_indices.setter
    def selected_group_indices(self, val: Any) -> None:
        self.state.selected_group_indices = set(val)

    @property
    def group_drawing_active(self) -> bool:
        return self.state.group_drawing_active

    @group_drawing_active.setter
    def group_drawing_active(self, val: bool) -> None:
        self.state.group_drawing_active = bool(val)

    @property
    def resume_from_node_index(self) -> Optional[int]:
        return self.state.resume_from_node_index

    @resume_from_node_index.setter
    def resume_from_node_index(self, val: Optional[int]) -> None:
        self.state.resume_from_node_index = val

    @property
    def preview_ra_dec(self) -> Optional[Tuple[float, float]]:
        return self.state.preview_ra_dec

    @preview_ra_dec.setter
    def preview_ra_dec(self, val: Optional[Tuple[float, float]]) -> None:
        self.state.preview_ra_dec = val

    @property
    def preview_snapped(self) -> bool:
        return self.state.preview_snapped

    @preview_snapped.setter
    def preview_snapped(self, val: bool) -> None:
        self.state.preview_snapped = bool(val)


    def set_enabled(self, enabled: bool) -> None:
        self.state.enabled = bool(enabled)
        if not self.state.enabled:
            self.state.clear_selection()

    def set_visible(self, visible: bool) -> None:
        self.state.visible = bool(visible)
        if not self.state.visible:
            self.state.clear_selection()

    def load(self) -> bool:
        if not self.data_path:
            return False
        groups = load_constellation_groups_from_json(self.data_path)
        self.state.groups = groups
        return True

    def save(self) -> bool:
        if not self.data_path:
            return False
        return save_constellation_groups_to_json(
            self.data_path, self.state.groups
        )

    def _push_undo(self) -> None:
        snapshot = [g.to_dict() for g in self.state.groups]
        self._undo_stack.append({"groups": snapshot})
        if len(self._undo_stack) > self._max_undo_states:
            self._undo_stack.pop(0)

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        state_dict = self._undo_stack.pop()
        self.state.groups = [
            ConstellationGroup.from_dict(g)
            for g in state_dict.get("groups", [])
        ]
        self.state.clear_selection()
        self.save()
        return True

    def add_node(
        self,
        ra_deg: float,
        dec_deg: float,
        star_id: str = "",
        star_name: str = "",
        connect_from_prev: bool = True,
        group_name: Optional[str] = None,
    ) -> None:
        self._push_undo()
        node = ConstellationNode(
            ra_deg=ra_deg,
            dec_deg=dec_deg,
            star_id=star_id,
            star_name=star_name,
            connect_from_prev=connect_from_prev,
        )

        if not self.state.groups:
            g_name = group_name or "Constellation 1"
            grp = ConstellationGroup(name=g_name, nodes=[node])
            self.state.groups.append(grp)
            self.state.active_group_index = 0
        else:
            idx = self.state.active_group_index
            if idx is None or idx < 0 or idx >= len(self.state.groups):
                idx = len(self.state.groups) - 1
                self.state.active_group_index = idx
            self.state.groups[idx].nodes.append(node)

        self.save()

    def delete_selected(self) -> bool:
        changed = False
        if (
            self.state.selected_node_index is not None
            and self.state.selected_group_index is not None
        ):
            g_idx = self.state.selected_group_index
            n_idx = self.state.selected_node_index
            if 0 <= g_idx < len(self.state.groups):
                grp = self.state.groups[g_idx]
                if 0 <= n_idx < len(grp.nodes):
                    self._push_undo()
                    grp.nodes.pop(n_idx)
                    changed = True
        elif (
            self.state.selected_segment_index is not None
            and self.state.selected_group_index is not None
        ):
            g_idx = self.state.selected_group_index
            s_idx = self.state.selected_segment_index
            if 0 <= g_idx < len(self.state.groups):
                grp = self.state.groups[g_idx]
                if 0 <= s_idx < len(grp.nodes) - 1:
                    self._push_undo()
                    grp.nodes[s_idx + 1].connect_from_prev = False
                    changed = True

        if changed:
            self.state.clear_selection()
            self.save()
        return changed

    def build_plan(
        self,
        project_ra_dec_fn: Callable[
            [float, float], Optional[Tuple[float, float]]
        ],
        viewport_w: float,
        viewport_h: float,
        font_metrics_provider: Optional[Any] = None,
    ) -> ConstellationPlan:
        if not self.state.visible:
            return ConstellationPlan(enabled=self.state.enabled, visible=False)

        node_plans: List[ConstellationNodePlan] = []
        segment_plans: List[ConstellationSegmentPlan] = []
        label_plans: List[ConstellationLabelPlan] = []

        for g_idx, grp in enumerate(self.state.groups):
            is_grp_sel = (
                g_idx == self.state.selected_group_index
                or g_idx in self.state.selected_group_indices
            )
            prev_node_pos: Optional[Tuple[float, float]] = None
            prev_node_ra_dec: Optional[Tuple[float, float]] = None

            grp_screen_pts: List[Tuple[float, float]] = []

            for n_idx, node in enumerate(grp.nodes):
                pos = project_ra_dec_fn(node.ra_deg, node.dec_deg)
                is_node_sel = is_grp_sel or (
                    g_idx == self.state.selected_group_index
                    and n_idx == self.state.selected_node_index
                )

                if pos is not None:
                    node_plans.append(
                        ConstellationNodePlan(
                            group_index=g_idx,
                            node_index=n_idx,
                            pos_px=pos,
                            ra_deg=node.ra_deg,
                            dec_deg=node.dec_deg,
                            star_id=node.star_id,
                            star_name=node.star_name,
                            is_selected=is_node_sel,
                        )
                    )
                    grp_screen_pts.append(pos)

                if (
                    node.connect_from_prev
                    and prev_node_pos is not None
                    and prev_node_ra_dec is not None
                ):
                    is_seg_sel = (
                        is_grp_sel
                        or (
                            g_idx == self.state.selected_group_index
                            and n_idx - 1 == self.state.selected_segment_index
                        )
                        or (g_idx, n_idx - 1) in self.state.selected_segments
                    )
                    arc_coords = slerp_arc_points(
                        (prev_node_ra_dec[1], prev_node_ra_dec[0]),
                        (node.dec_deg, node.ra_deg),
                        n_points=16,
                    )
                    pts_px: List[Tuple[float, float]] = []
                    for alt_a, az_a in arc_coords:
                        p = project_ra_dec_fn(az_a, alt_a)
                        if p is not None:
                            pts_px.append(p)

                    if len(pts_px) >= 2:
                        segment_plans.append(
                            ConstellationSegmentPlan(
                                group_index=g_idx,
                                start_node_index=n_idx - 1,
                                end_node_index=n_idx,
                                pts_px=pts_px,
                                is_selected=is_seg_sel,
                            )
                        )

                prev_node_pos = pos
                prev_node_ra_dec = (node.ra_deg, node.dec_deg)

            if grp_screen_pts and grp.name:
                avg_x = sum(p[0] for p in grp_screen_pts) / len(grp_screen_pts)
                avg_y = sum(p[1] for p in grp_screen_pts) / len(grp_screen_pts)
                rect_w = len(grp.name) * 8.0 + 10.0
                rect_h = 16.0
                label_rect = (
                    avg_x - rect_w * 0.5,
                    avg_y - rect_h * 0.5,
                    rect_w,
                    rect_h,
                )
                label_plans.append(
                    ConstellationLabelPlan(
                        group_index=g_idx,
                        name=grp.name,
                        pos_px=(avg_x, avg_y),
                        rect_px=label_rect,
                        is_selected=is_grp_sel,
                    )
                )

        preview_pts: Optional[List[Tuple[float, float]]] = None
        if self.state.group_drawing_active and self.state.preview_ra_dec:
            last_node: Optional[ConstellationNode] = None
            if (
                self.state.active_group_index is not None
                and 0 <= self.state.active_group_index < len(self.state.groups)
            ):
                nodes = self.state.groups[self.state.active_group_index].nodes
                if nodes:
                    last_node = nodes[-1]
            if last_node:
                p_last = project_ra_dec_fn(last_node.ra_deg, last_node.dec_deg)
                p_mouse = project_ra_dec_fn(
                    self.state.preview_ra_dec[0], self.state.preview_ra_dec[1]
                )
                if p_last and p_mouse:
                    preview_pts = [p_last, p_mouse]

        return ConstellationPlan(
            enabled=self.state.enabled,
            visible=self.state.visible,
            nodes=node_plans,
            segments=segment_plans,
            labels=label_plans,
            preview_pts_px=preview_pts,
            preview_snapped=self.state.preview_snapped,
        )

    def on_left_click(
        self,
        x: float,
        y: float,
        project_fn: Optional[Callable[..., Any]] = None,
        radec_to_sky_fn: Optional[Callable[..., Any]] = None,
        pick_star_fn: Optional[Callable[..., Any]] = None,
        **options: Any,
    ) -> bool:
        return True

