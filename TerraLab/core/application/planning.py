"""Application-owned conversion from a SceneFrame to renderer-neutral plans."""

from __future__ import annotations

from TerraLab.core.rendering_contracts.plans import (
    InteractionPlan,
    MaterialBatch,
    PickingPlan,
    RenderPlanBundle,
    ResourcePlan,
    SceneRenderPlan,
    SpriteBatch,
    TextBatch,
)
from TerraLab.scene.contracts import SceneFrame, freeze_json_mapping


class SceneRenderPlanner:
    """Resolve the common cross-backend plan envelope for a scene frame.

    Capability-specific planners can enrich the ordered plans without changing
    a backend.  This first composition step deliberately records only plain
    values already resolved by ``SceneFrameBuilder``; adapters never inspect
    widgets or derive visibility policy.
    """

    def build(self, frame: SceneFrame) -> RenderPlanBundle:
        resources = ResourcePlan(
            freeze_json_mapping(
                {
                    "catalog": frame.resources.catalog.catalog_path,
                    "ngc": frame.resources.ngc.path or "",
                    "terrain_profile": frame.resources.terrain_profile.path or "",
                    "terrain_surface": frame.resources.terrain_surface.path or "",
                    "milkyway_texture": frame.resources.milkyway_texture.path or "",
                    "dust_map": frame.resources.dust_map.path or "",
                }
            )
        )
        plans = tuple(
            SceneRenderPlan(
                capability=layer.value,
                geometry=(
                    freeze_json_mapping(
                        {
                            "kind": "viewport",
                            "x": 0.0,
                            "y": 0.0,
                            "width": frame.viewport.width,
                            "height": frame.viewport.height,
                        }
                    ),
                ),
                materials=(
                    MaterialBatch(
                        "layer",
                        freeze_json_mapping(
                            {
                                "opacity": 1.0,
                                "bortle": frame.bortle,
                                "magnitude_limit": frame.magnitude_limit,
                            }
                        ),
                    ),
                ),
                sprites=(
                    SpriteBatch(
                        "selection",
                        (
                            freeze_json_mapping(
                                {
                                    "name": frame.selection.name,
                                    "kind": frame.selection.kind,
                                }
                            ),
                        )
                        if frame.selection.name or frame.selection.kind
                        else (),
                    ),
                ),
                text=(
                    TextBatch(
                        "selection_label",
                        (
                            freeze_json_mapping(
                                {"text": frame.selection.name}
                            ),
                        )
                        if frame.selection.name
                        else (),
                    ),
                ),
                metadata=freeze_json_mapping(
                    {
                        "enabled": True,
                        "generation": frame.generation,
                        "camera_azimuth": frame.camera.azimuth,
                        "camera_elevation": frame.camera.elevation,
                    }
                ),
            )
            for layer in frame.layers.order
        )
        selected = frame.selection
        records = ()
        if selected.kind or selected.key or selected.name:
            records = (
                freeze_json_mapping(
                    {
                        "kind": selected.kind,
                        "key": selected.key,
                        "name": selected.name,
                        "alt": selected.altitude if selected.altitude is not None else 0.0,
                        "az": selected.azimuth if selected.azimuth is not None else 0.0,
                    }
                ),
            )
        return RenderPlanBundle(
            generation=frame.generation,
            frame=frame,
            plans=plans,
            resources=resources,
            picking=PickingPlan(frame.generation, records=records),
            interaction=InteractionPlan(
                frame.generation,
                affordances=(
                    freeze_json_mapping(
                        {"active": frame.presentation.interaction_active}
                    ),
                ),
            ),
            metadata=freeze_json_mapping(
                {
                    "schema_version": frame.schema_version,
                    "plan_count": len(plans),
                }
            ),
        )
