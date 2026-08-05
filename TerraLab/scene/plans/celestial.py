"""Typed, renderer-neutral work order for the celestial layers of a frame.

The individual planners own their scientific decisions.  This module merely
keeps their already-resolved outputs together so presentation backends never
need to rebuild a v1 payload or query frame state again.
"""

from __future__ import annotations

from dataclasses import dataclass

from TerraLab.scene.plans.bodies import CelestialBodiesPlan, TrailPlan
from TerraLab.scene.plans.deep_sky import DeepSkyBatch, MilkyWayPlan
from TerraLab.scene.plans.sky import SkyBackgroundPlan
from TerraLab.scene.plans.stars import StarScenePlan
from TerraLab.scene.render_state import RenderState


@dataclass(frozen=True, slots=True)
class CelestialLayerPlans:
    """Complete presentation inputs for the migrated celestial capabilities."""

    generation: int
    sky_background: SkyBackgroundPlan
    stars: StarScenePlan
    bodies: CelestialBodiesPlan
    trails: TrailPlan | None
    milkyway: MilkyWayPlan
    deep_sky: DeepSkyBatch
    render_state: RenderState
    pure_colors: bool = False

    def __post_init__(self) -> None:
        if int(self.generation) < 0:
            raise ValueError("Celestial plan generation cannot be negative")
