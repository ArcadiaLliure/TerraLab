"""Three.js hosted surface renderer backend implementation."""

from __future__ import annotations

from typing import Any, Mapping

from TerraLab.application.ports.rendering import (
    HostedSurfaceOutput,
    HostedSurfaceTarget,
    PickRequest,
    PickResult,
    RenderBackendLifecycleError,
    RenderCapability,
    RenderOutput,
    RenderOutputPort,
    RenderTarget,
    RenderTargetKind,
    RendererBackend,
)
from TerraLab.core.rendering_contracts.plans import RenderPlanBundle
from TerraLab.render.threejs.bridge import ThreeJSBridge
from TerraLab.render.threejs.diagnostic import build_diagnostic_primitive_manifest
from TerraLab.scene.contracts import JSONValue, SceneFrame, freeze_json_mapping


class ThreeJSRendererBackend(RendererBackend):
    """Hosted surface backend utilizing local Three.js bridge and primitives."""

    backend_id = "threejs"
    capabilities = frozenset(
        {
            RenderCapability.HOSTED_SURFACE,
            RenderCapability.SKY_BACKGROUND,
            RenderCapability.STARS,
            RenderCapability.EPHEMERIS_BODIES,
            RenderCapability.DEEP_SKY,
            RenderCapability.GRID,
            RenderCapability.LABELS,
            RenderCapability.SCOPE,
            RenderCapability.CONSTELLATIONS,
            RenderCapability.PICKING,
            RenderCapability.INTERACTION,
            RenderCapability.MEASUREMENTS,
            RenderCapability.TERRAIN_GEOMETRY,
            RenderCapability.TERRAIN_MATERIALS,
        }
    )
    target_kinds = frozenset({RenderTargetKind.HOSTED_SURFACE})

    def __init__(self, bridge: ThreeJSBridge | None = None) -> None:
        self._started = False
        self._closed = False
        self._output_port: RenderOutputPort | None = None
        self._bridge = bridge or ThreeJSBridge()
        self._last_frame_generation = -1
        self._history: dict[int, Mapping[str, JSONValue]] = {}

    @property
    def is_started(self) -> bool:
        return self._started and not self._closed

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def bridge(self) -> ThreeJSBridge:
        return self._bridge

    def start(self, output_port: RenderOutputPort) -> None:
        if self._closed:
            raise RenderBackendLifecycleError(
                "Cannot restart a closed Three.js backend"
            )
        if self._started:
            raise RenderBackendLifecycleError("Three.js backend is already started")

        self._started = True
        self._output_port = output_port
        self._bridge.start({"width": 1920, "height": 1080, "dpr": 1.0})

    def submit_diagnostic_frame(self, surface_id: str = "threejs-surface-0") -> HostedSurfaceOutput:
        """Submit and render the diagnostic scene for Phase 19 verification."""
        self._require_started()
        diag_manifest = build_diagnostic_primitive_manifest()
        gen = int(diag_manifest["generation"])  # type: ignore[arg-type]
        self._bridge.submit_frame(gen, diag_manifest)
        self._history[gen] = diag_manifest
        self._last_frame_generation = gen

        output = HostedSurfaceOutput(
            generation=gen,
            surface_id=surface_id,
            metadata=freeze_json_mapping(
                {
                    "backend": self.backend_id,
                    "diagnostic": True,
                    "primitives_count": len(diag_manifest["primitives"]),  # type: ignore[arg-type]
                }
            ),
        )
        if self._output_port is not None:
            self._output_port.frame_ready(output)
        return output

    def render(
        self,
        plan: RenderPlanBundle,
        target: RenderTarget,
    ) -> RenderOutput:
        """Render a plan bundle onto a HostedSurfaceTarget."""
        self._require_started()
        if not isinstance(target, HostedSurfaceTarget):
            raise TypeError(
                f"Three.js backend requires HostedSurfaceTarget, got {type(target).__name__}"
            )

        gen = int(plan.generation)
        frame_manifest = self._build_plan_bundle_manifest(plan)
        self._bridge.submit_frame(gen, frame_manifest)
        self._history[gen] = frame_manifest
        self._last_frame_generation = gen

        output = HostedSurfaceOutput(
            generation=gen,
            surface_id=target.surface_id,
            metadata=freeze_json_mapping(
                {
                    "backend": self.backend_id,
                    "plans_count": len(plan.plans),
                    "viewport_width": target.width,
                    "viewport_height": target.height,
                }
            ),
        )
        if self._output_port is not None:
            self._output_port.frame_ready(output)
        return output

    def _build_plan_bundle_manifest(self, bundle: RenderPlanBundle) -> Mapping[str, Any]:
        frame = bundle.frame
        primitives: list[dict[str, Any]] = []

        for p in bundle.plans:
            cap = p.capability
            if cap == "sky_background":
                for g in p.geometry:
                    primitives.append({"kind": "sky_gradient", "geometry": dict(g)})
            elif cap == "stars":
                for s in p.sprites:
                    primitives.append({"kind": "star_batch", "name": s.name, "count": len(s.items)})
            elif cap in ("sun_moon", "planets", "solar_system", "ephemeris_bodies"):
                for m in p.materials:
                    primitives.append({"kind": "celestial_body", "name": m.name, "values": dict(m.values)})
            elif cap in ("milkyway", "deep_sky"):
                for g in p.geometry:
                    primitives.append({"kind": "milkyway_mesh", "geometry": dict(g)})
            elif cap == "grid":
                for g in p.geometry:
                    primitives.append({"kind": "grid_lines", "geometry": dict(g)})
            elif cap in ("labels", "hud"):
                for t in p.text:
                    primitives.append({"kind": "label_text", "name": t.name, "count": len(t.items)})
            elif cap == "scope":
                for g in p.geometry:
                    primitives.append({"kind": "scope_mask", "geometry": dict(g)})
            elif cap == "constellations":
                for g in p.geometry:
                    primitives.append({"kind": "constellation_segment", "geometry": dict(g)})
            elif cap in ("measurements", "picking"):
                for g in p.geometry:
                    primitives.append({"kind": "measurement_pulse", "geometry": dict(g)})
            elif cap in ("terrain_geometry", "terrain"):
                for g in p.geometry:
                    primitives.append({"kind": "terrain_mesh", "geometry": dict(g)})
            elif cap == "terrain_materials":
                for m in p.materials:
                    primitives.append({"kind": "terrain_material", "name": m.name, "values": dict(m.values)})

        return {
            "generation": int(frame.generation),
            "viewport": {
                "width": int(frame.viewport.width),
                "height": int(frame.viewport.height),
                "dpr": float(frame.viewport.device_pixel_ratio),
            },
            "bortle": int(frame.bortle),
            "magnitude_limit": float(frame.magnitude_limit),
            "layers": [str(layer) for layer in frame.layers.order],
            "primitives": primitives,
        }

    def request_pick(
        self,
        request: PickRequest,
        plan: RenderPlanBundle | None = None,
    ) -> PickResult:
        self._require_started()
        if request.generation < 0:
            raise ValueError("PickRequest generation cannot be negative")

        gen = int(request.generation)
        req_id = str(request.request_id)
        purpose = str(request.purpose or "select")
        self._bridge.request_pick(gen, req_id, request.x, request.y, purpose)

        payload: dict[str, Any] = {
            "generation": gen,
            "request_id": req_id,
            "backend": self.backend_id,
            "x": float(request.x),
            "y": float(request.y),
            "purpose": purpose,
        }
        if purpose in ("hover", "surface", "ground"):
            payload["kind"] = "surface"
            payload["surface"] = "ground"
            payload["hit"] = True
        else:
            payload["hit"] = False

        result = PickResult(
            generation=gen,
            request_id=req_id,
            payload=freeze_json_mapping(payload),
        )
        if self._output_port is not None:
            self._output_port.pick_ready(result)
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._started = False
        self._bridge.close()
        self._output_port = None

    def get_frame_manifest(self, generation: int) -> Mapping[str, JSONValue] | None:
        return self._history.get(int(generation))

    def _build_frame_manifest(self, frame: SceneFrame) -> Mapping[str, Any]:
        return {
            "generation": int(frame.generation),
            "viewport": {
                "width": int(frame.viewport.width),
                "height": int(frame.viewport.height),
                "dpr": float(frame.viewport.device_pixel_ratio),
            },
            "bortle": int(frame.bortle),
            "magnitude_limit": float(frame.magnitude_limit),
            "layers": [str(layer) for layer in frame.layers.order],
        }

    def _require_started(self) -> None:
        if self._closed:
            raise RenderBackendLifecycleError("Three.js backend is closed")
        if not self._started:
            raise RenderBackendLifecycleError("Three.js backend has not started")
