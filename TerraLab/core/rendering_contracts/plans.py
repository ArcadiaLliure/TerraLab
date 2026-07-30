"""Immutable render plans shared by every graphics backend.

Nothing in this module knows about Qt, a window, a canvas, or a GPU API.  A
planner resolves visibility, geometry, material values, resources, text,
picking and interaction before a view adapter sees a frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from TerraLab.scene.contracts import JSONValue, SceneFrame, freeze_json_mapping


def _frozen_mapping(value: Mapping[str, JSONValue] | None = None) -> Mapping[str, JSONValue]:
    return freeze_json_mapping(value or {})


@dataclass(frozen=True, slots=True)
class MaterialBatch:
    """Resolved material values for one named draw batch."""

    name: str
    values: Mapping[str, JSONValue] = field(default_factory=_frozen_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _frozen_mapping(self.values))


@dataclass(frozen=True, slots=True)
class SpriteBatch:
    """Resolved sprite positions, colour values, and dimensions."""

    name: str
    items: tuple[Mapping[str, JSONValue], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "items", tuple(_frozen_mapping(item) for item in self.items)
        )


@dataclass(frozen=True, slots=True)
class TextBatch:
    """Resolved text labels.  Layout decisions belong to the planner."""

    name: str
    items: tuple[Mapping[str, JSONValue], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "items", tuple(_frozen_mapping(item) for item in self.items)
        )


@dataclass(frozen=True, slots=True)
class ResourcePlan:
    """Versioned resources referenced by a plan, never graphics objects."""

    resources: Mapping[str, JSONValue] = field(default_factory=_frozen_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "resources", _frozen_mapping(self.resources))


@dataclass(frozen=True, slots=True)
class PickingPlan:
    """Resolved hit records and coordinate space for one frame."""

    generation: int
    records: tuple[Mapping[str, JSONValue], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "records", tuple(_frozen_mapping(record) for record in self.records)
        )


@dataclass(frozen=True, slots=True)
class InteractionPlan:
    """Resolved interaction affordances and current interaction state."""

    generation: int
    affordances: tuple[Mapping[str, JSONValue], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "affordances",
            tuple(_frozen_mapping(item) for item in self.affordances),
        )


@dataclass(frozen=True, slots=True)
class SceneRenderPlan:
    """A single ordered, renderer-neutral capability plan."""

    capability: str
    geometry: tuple[Mapping[str, JSONValue], ...] = ()
    materials: tuple[MaterialBatch, ...] = ()
    sprites: tuple[SpriteBatch, ...] = ()
    text: tuple[TextBatch, ...] = ()
    metadata: Mapping[str, JSONValue] = field(default_factory=_frozen_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "geometry", tuple(_frozen_mapping(item) for item in self.geometry)
        )
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class RenderPlanBundle:
    """The complete immutable work order consumed by a view backend."""

    generation: int
    frame: SceneFrame
    plans: tuple[SceneRenderPlan, ...]
    resources: ResourcePlan
    picking: PickingPlan
    interaction: InteractionPlan
    metadata: Mapping[str, JSONValue] = field(default_factory=_frozen_mapping)

    def __post_init__(self) -> None:
        if self.generation != self.frame.generation:
            raise ValueError("Render-plan generation must match its SceneFrame")
        if self.picking.generation != self.generation:
            raise ValueError("Picking plan generation must match its render bundle")
        if self.interaction.generation != self.generation:
            raise ValueError("Interaction plan generation must match its render bundle")
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))
