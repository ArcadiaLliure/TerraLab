"""Renderer-neutral plans for informational and interactive sky overlays.

All positions, bounds, text and styles are resolved here or by the
application coordinator.  Presentation backends receive only this vocabulary
of screen-space primitives.
"""

from __future__ import annotations

from dataclasses import dataclass

from TerraLab.scene.plans.interaction import ScreenMeasurementPlan, SelectionPlan
from TerraLab.scene.plans.labels import (
    CompassPlan,
    GridPlan,
    HudPlan,
    LineSegment,
    ScreenRect,
    StrokeStyle,
    TextBatch,
)

RGBA = tuple[int, int, int, int]
ScreenPoint = tuple[float, float]


@dataclass(frozen=True, slots=True)
class CirclePrimitive:
    """A screen-space circle with all visual attributes already chosen."""

    center: ScreenPoint
    radius_px: float
    fill_rgba: RGBA | None = None
    stroke: StrokeStyle | None = None


@dataclass(frozen=True, slots=True)
class ScopeOverlayPlan:
    """Scope mask, reticle and readout ready for a presentation adapter."""

    enabled: bool
    viewport: ScreenRect
    shape: str = "circle"
    center: ScreenPoint | None = None
    radius_px: float = 0.0
    rect_size_px: ScreenPoint | None = None
    mask_rgba: RGBA = (10, 12, 16, 220)
    outline: StrokeStyle = StrokeStyle((255, 200, 80, 200), 1.5)
    crosshair: tuple[LineSegment, ...] = ()
    readout: TextBatch | None = None


@dataclass(frozen=True, slots=True)
class ConstellationOverlayPlan:
    """Resolved constellation segments, nodes, labels and preview."""

    enabled: bool
    visible: bool
    segments: tuple[LineSegment, ...] = ()
    nodes: tuple[CirclePrimitive, ...] = ()
    labels: TextBatch | None = None
    preview: LineSegment | None = None


@dataclass(frozen=True, slots=True)
class OverlayLayerPlans:
    """All phase-06 overlays, interaction visuals and their frame generation."""

    generation: int
    grid: GridPlan
    compass: CompassPlan
    labels: TextBatch
    hud: HudPlan
    scope: ScopeOverlayPlan
    constellations: ConstellationOverlayPlan
    selection: SelectionPlan
    measurements: ScreenMeasurementPlan

    def __post_init__(self) -> None:
        if int(self.generation) < 0:
            raise ValueError("Overlay plan generation cannot be negative")
