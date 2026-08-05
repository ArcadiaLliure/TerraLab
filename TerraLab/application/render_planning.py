"""Application-owned composition of generic and celestial render plans."""

from __future__ import annotations

from dataclasses import replace

from TerraLab.application.celestial_planning import CelestialPlanCoordinator
from TerraLab.application.overlay_interaction_planning import (
    OverlayInteractionPlanCoordinator,
)
from TerraLab.application.terrain_planning import TerrainPlanCoordinator
from TerraLab.core.application.planning import SceneRenderPlanner as _EnvelopePlanner
from TerraLab.core.rendering_contracts.plans import RenderPlanBundle
from TerraLab.scene.contracts import SceneFrame


class SceneRenderPlanner:
    """Coordinate pure planners before a View backend receives the bundle."""

    def __init__(
        self,
        *,
        celestial: CelestialPlanCoordinator | None = None,
        overlays: OverlayInteractionPlanCoordinator | None = None,
        terrain: TerrainPlanCoordinator | None = None,
    ) -> None:
        self._envelope = _EnvelopePlanner()
        self._celestial = celestial or CelestialPlanCoordinator()
        self._overlays = overlays or OverlayInteractionPlanCoordinator()
        self._terrain = terrain or TerrainPlanCoordinator()

    def build(self, frame: SceneFrame) -> RenderPlanBundle:
        celestial = self._celestial.build(frame)
        result = self._overlays.build(frame, celestial)
        terrain = self._terrain.build(frame, celestial.render_state)
        envelope = self._envelope.build(frame)
        plans = tuple(
            terrain.scene_plan
            if plan.capability == "terrain" and terrain.scene_plan is not None
            else plan
            for plan in envelope.plans
        )
        pick_index = result.pick_index
        if terrain.surface_hits is not None:
            pick_index = replace(
                pick_index,
                surface_picker=terrain.surface_hits.query,
            )
        return replace(
            envelope,
            plans=plans,
            celestial=celestial,
            overlays=result.overlays,
            pick_index=pick_index,
        )

    def process_pick_request(self, request, plan: RenderPlanBundle):
        """Keep pick/selection/measurement policy in the application layer."""

        pick_index = plan.pick_index
        if pick_index is None:
            raise ValueError("A typed render plan requires a pick index")
        if request.purpose == "interaction":
            return self._overlays.interaction.process_interaction_request(
                request, pick_index
            )
        return self._overlays.interaction.process_pick_request(
            request, pick_index
        )

    def close(self) -> None:
        self._celestial.close()
        self._terrain.close()


__all__ = ("SceneRenderPlanner",)
