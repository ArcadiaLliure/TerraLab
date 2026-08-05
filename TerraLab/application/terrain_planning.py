"""Application coordination for the renderer-neutral terrain work order."""

from __future__ import annotations

from TerraLab.data.terrain_plan_resources import TerrainPlanResourceRepository
from TerraLab.scene.contracts import SceneFrame
from TerraLab.scene.plans.terrain import TerrainLayerPlans, build_terrain_layer_plan
from TerraLab.scene.render_state import RenderState


class TerrainPlanCoordinator:
    """Resolve CPU terrain artifacts before a View backend begins painting."""

    def __init__(self, resources: TerrainPlanResourceRepository | None = None) -> None:
        self._resources = resources or TerrainPlanResourceRepository()

    def build(self, frame: SceneFrame, state: RenderState) -> TerrainLayerPlans:
        profile = self._resources.profile(
            frame.resources.terrain_profile,
            latitude=frame.observer.latitude,
            longitude=frame.observer.longitude,
        )
        surface = self._resources.surface(frame.resources.terrain_surface)
        return build_terrain_layer_plan(frame, state, profile, surface)

    def close(self) -> None:
        self._resources.close()


__all__ = ("TerrainPlanCoordinator",)
