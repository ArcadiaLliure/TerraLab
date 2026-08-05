"""Application-owned composition of typed renderer-neutral render plans."""

from __future__ import annotations

from TerraLab.core.rendering_contracts.plans import (
    ArtifactResource,
    Bounds,
    InteractionAffordance,
    InteractionPlan,
    PickingPlan,
    RenderPlanBundle,
    ResourcePlan,
    SceneRenderPlan,
)
from TerraLab.scene.contracts import ResourceRef, SceneFrame


class SceneRenderPlanner:
    """Compose the common render envelope without inventing draw primitives.

    Capability planners supply ``SceneRenderPlan.primitives`` only after they
    have resolved geometry, culling, text layout and material inputs on CPU.
    This composer therefore publishes the frame ordering and versioned CPU
    artifacts, but deliberately does not turn a viewport or a selection name
    into a fake drawable primitive.
    """

    @staticmethod
    def _artifact(
        resource_id: str,
        kind: str,
        resource: ResourceRef,
    ) -> ArtifactResource | None:
        source = resource.path or resource.handle
        if not source:
            return None
        return ArtifactResource(
            resource_id=resource_id,
            kind=kind,
            source=source,
            version=resource.version or "unversioned",
            handle=resource.handle,
        )

    def build(self, frame: SceneFrame) -> RenderPlanBundle:
        resource_specs = (
            self._artifact(
                "ngc-catalog", "catalog", frame.resources.ngc
            ),
            self._artifact(
                "terrain-profile", "terrain-profile", frame.resources.terrain_profile
            ),
            self._artifact(
                "terrain-surface", "terrain-surface", frame.resources.terrain_surface
            ),
            self._artifact(
                "milkyway-texture", "texture", frame.resources.milkyway_texture
            ),
            self._artifact("dust-map", "texture", frame.resources.dust_map),
        )
        artifacts = tuple(item for item in resource_specs if item is not None)
        catalog = frame.resources.catalog
        if catalog.catalog_path:
            artifacts += (
                ArtifactResource(
                    resource_id="star-catalog",
                    kind="catalog",
                    source=catalog.catalog_path,
                    version=catalog.version or "unversioned",
                ),
            )

        plans = tuple(
            SceneRenderPlan(
                capability=layer.value,
                layer_order=index,
                frame_generation=frame.generation,
            )
            for index, layer in enumerate(frame.layers.order)
        )
        viewport_bounds = Bounds(
            minimum=(0.0, 0.0, 0.0),
            maximum=(float(frame.viewport.width), float(frame.viewport.height), 0.0),
        )
        return RenderPlanBundle(
            generation=frame.generation,
            frame=frame,
            plans=plans,
            resources=ResourcePlan(
                frame_generation=frame.generation,
                artifacts=artifacts,
            ),
            picking=PickingPlan(frame.generation),
            interaction=InteractionPlan(
                frame.generation,
                affordances=(
                    InteractionAffordance(
                        affordance_id="scene-input",
                        action="scene-input",
                        bounds=viewport_bounds,
                        active=frame.presentation.interaction_active,
                        layer_order=0,
                    ),
                ),
            ),
        )
