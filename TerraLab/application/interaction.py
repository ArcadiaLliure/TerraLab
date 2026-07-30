"""Application controller for picking resolution, selection policy, and measurement tools."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from TerraLab.application.ports.rendering import PickRequest, PickResult
from TerraLab.scene.contracts import freeze_json_mapping
from TerraLab.scene.picking import PickIndex
from TerraLab.scene.plans.interaction import (
    MeasurementItemPlan,
    MeasurementPlan,
    SelectionPlan,
    build_measurement_item_plan,
)


class PickingPipeline(str, Enum):
    """Execution mode for picking, selection, and measurement calculations."""

    LEGACY = "legacy"
    SCENE = "scene"


def resolve_picking_pipeline() -> PickingPipeline:
    """Resolve the active picking pipeline capability flag."""
    raw = os.getenv("TERRALAB_PICKING_PIPELINE", "scene").strip().lower()
    if raw in ("legacy", "old"):
        return PickingPipeline.LEGACY
    return PickingPipeline.SCENE


@dataclass(slots=True)
class SelectionStateStore:
    """Application-owned selection state."""

    selected_kind: str | None = None
    selected_key: str | None = None
    selected_name: str | None = None
    alt_deg: float = 0.0
    az_deg: float = 0.0
    screen_x: float | None = None
    screen_y: float | None = None
    pulse_phase: float = 0.0
    extra: Mapping[str, Any] = field(default_factory=dict)

    def clear(self) -> None:
        self.selected_kind = None
        self.selected_key = None
        self.selected_name = None
        self.screen_x = None
        self.screen_y = None
        self.pulse_phase = 0.0
        self.extra = {}

    def update_from_pick(self, payload: Mapping[str, Any]) -> None:
        kind = str(payload.get("kind", "none"))
        if kind == "none" or kind == "sky":
            self.clear()
            self.alt_deg = float(payload.get("alt", 0.0))
            self.az_deg = float(payload.get("az", 0.0))
            return

        self.selected_kind = kind
        self.selected_key = str(payload.get("key", payload.get("name", "")))
        self.selected_name = str(payload.get("name", payload.get("key", "")))
        self.alt_deg = float(payload.get("alt", 0.0))
        self.az_deg = float(payload.get("az", 0.0))
        if "star" in payload and isinstance(payload["star"], dict):
            star_info = payload["star"]
            self.selected_key = str(star_info.get("id", self.selected_key))
            self.selected_name = f"Gaia #{self.selected_key}"
        sx = payload.get("screen_x")
        self.screen_x = float(sx) if isinstance(sx, (int, float, str)) else None
        sy = payload.get("screen_y")
        self.screen_y = float(sy) if isinstance(sy, (int, float, str)) else None
        self.extra = dict(payload)


class ApplicationInteractionController:
    """Use case coordinator for picking, selection policy, and measurement tools."""

    def __init__(self) -> None:
        self._selection = SelectionStateStore()
        self._active_pipeline = resolve_picking_pipeline()
        self._last_generation = 0
        self._measurement_items: list[dict[str, Any]] = []
        self._measurement_tool = "none"
        self._undo_stack: list[list[dict[str, Any]]] = []

    @property
    def selection(self) -> SelectionStateStore:
        return self._selection

    @property
    def active_pipeline(self) -> PickingPipeline:
        return self._active_pipeline

    def process_pick_request(
        self,
        request: PickRequest,
        pick_index: PickIndex | None = None,
    ) -> PickResult:
        """Resolve a pick request against a generation-tagged PickIndex."""
        if request.generation < self._last_generation:
            # Stale request discarded
            return PickResult(
                generation=request.generation,
                request_id=request.request_id,
                payload=freeze_json_mapping({"kind": "none", "stale": True}),
            )

        self._last_generation = request.generation

        if pick_index is not None:
            payload = pick_index.query(request.x, request.y, request.radius)
        else:
            payload = {"kind": "none", "alt": 0.0, "az": 0.0}

        payload["purpose"] = request.purpose
        if request.purpose in ("select", "context"):
            self._selection.update_from_pick(payload)

        return PickResult(
            generation=request.generation,
            request_id=request.request_id,
            payload=freeze_json_mapping(payload),
        )

    def process_pick_result(self, result: PickResult) -> None:
        """Update selection state from an incoming backend pick result."""
        if result.generation < self._last_generation:
            return
        self._last_generation = result.generation
        payload = dict(result.payload)
        purpose = str(payload.get("purpose", "select"))
        if purpose in ("select", "context"):
            self._selection.update_from_pick(payload)

    def build_selection_plan(
        self, generation: int, pulse_phase: float = 0.0
    ) -> SelectionPlan:
        """Build an immutable SelectionPlan for the view presenter."""
        if self._selection.selected_kind is None:
            return SelectionPlan(generation=generation)

        return SelectionPlan(
            generation=generation,
            selected_kind=self._selection.selected_kind,
            selected_key=self._selection.selected_key,
            selected_name=self._selection.selected_name,
            screen_x=self._selection.screen_x,
            screen_y=self._selection.screen_y,
            pulse_phase=float(pulse_phase),
            pulse_radius_px=24.0,
            pulse_alpha=1.0,
        )

    def build_measurement_plan(self, generation: int) -> MeasurementPlan:
        """Build an immutable MeasurementPlan for the view presenter."""
        item_plans: list[MeasurementItemPlan] = []
        for item in self._measurement_items:
            tool = str(item.get("tool", "none"))
            a = item.get("a", (0.0, 0.0))
            b = item.get("b", (0.0, 0.0))
            rot = float(item.get("rotation_deg", 0.0))
            sel = bool(item.get("selected", False))
            item_plans.append(
                build_measurement_item_plan(
                    tool=tool, a=a, b=b, rotation_deg=rot, selected=sel
                )
            )

        return MeasurementPlan(
            generation=generation,
            items=tuple(item_plans),
            preview=None,
            active_tool=self._measurement_tool,
        )
