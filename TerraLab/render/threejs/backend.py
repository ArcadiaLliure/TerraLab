"""Three.js View adapter for resolved celestial and overlay render plans.

The adapter owns lifecycle, bridge transport and GPU presentation only.  It
receives the same immutable celestial plans that QPainter consumes; it neither
selects scientific data nor recalculates projection, photometry or visibility.
"""

from __future__ import annotations

from collections.abc import Mapping

from TerraLab.core.rendering_contracts.contracts import (
    HostedSurfaceOutput,
    HostedSurfaceTarget,
    PickRequest,
    PickResult,
    RenderBackendLifecycleError,
    RenderCapability,
    RenderFailure,
    RenderOutput,
    RenderOutputPort,
    RenderTarget,
    RenderTargetKind,
    RendererBackend,
)
from TerraLab.core.rendering_contracts.plans import RenderPlanBundle
from TerraLab.render.threejs.bridge import ThreeJSBridge
from TerraLab.render.threejs.protocol import (
    BridgeViewport,
    OP_ERROR,
    OP_PICK_RESULT,
    PreparedBridgeFrame,
    prepare_celestial_plan_bundle,
)
from TerraLab.scene.contracts import JSONValue, freeze_json_mapping


class ThreeJSRendererBackend(RendererBackend):
    """Present resolved sky and overlay layers in a hosted WebGL view."""

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
    requires_host_pick = True

    def __init__(self, bridge: ThreeJSBridge | None = None) -> None:
        self._started = False
        self._closed = False
        self._output_port: RenderOutputPort | None = None
        self._bridge = bridge or ThreeJSBridge()
        self._attached_viewport: BridgeViewport | None = None
        self._history: dict[int, Mapping[str, JSONValue]] = {}
        self._latest_plan: RenderPlanBundle | None = None
        self._pending_picks: dict[tuple[int, str], PickRequest] = {}
        self._bridge.add_inbound_listener(self._on_bridge_message)

    @property
    def is_started(self) -> bool:
        return self._started and not self._closed

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def bridge(self) -> ThreeJSBridge:
        return self._bridge

    def bind_host_bridge(self, bridge: ThreeJSBridge) -> None:
        """Bind the single bridge owned by the live WebEngine host.

        Composition creates the renderer without Qt. The presenter supplies
        its already-connected host bridge before the backend is started, which
        prevents a second, unbound presentation route.
        """

        if self._started or self._closed:
            raise RenderBackendLifecycleError(
                "Three.js host bridge can only be bound before start"
            )
        if bridge is self._bridge:
            return
        self._bridge.remove_inbound_listener(self._on_bridge_message)
        self._bridge = bridge
        self._bridge.add_inbound_listener(self._on_bridge_message)

    def start(self, output_port: RenderOutputPort) -> None:
        if self._closed:
            raise RenderBackendLifecycleError(
                "Cannot restart a closed Three.js backend"
            )
        if self._started:
            raise RenderBackendLifecycleError(
                "Three.js backend is already started"
            )
        self._started = True
        self._output_port = output_port

    def attach_surface(self, target: HostedSurfaceTarget) -> None:
        """Start or resize the bridge with the live host dimensions."""

        self._require_started()
        viewport = BridgeViewport(
            width=target.width,
            height=target.height,
            device_pixel_ratio=target.device_pixel_ratio,
        )
        if self._bridge.is_started:
            if viewport != self._attached_viewport:
                self._bridge.resize(viewport)
        else:
            self._bridge.start(viewport)
        self._attached_viewport = viewport

    @staticmethod
    def prepare_celestial_plan_for_bridge(
        plan: RenderPlanBundle,
    ) -> PreparedBridgeFrame:
        """Pack resolved celestial and overlay attributes for WebGL."""

        return prepare_celestial_plan_bundle(plan)

    def render(
        self,
        plan: RenderPlanBundle,
        target: RenderTarget,
    ) -> RenderOutput:
        self._require_started()
        if not isinstance(target, HostedSurfaceTarget):
            raise TypeError(
                "Three.js requires a HostedSurfaceTarget, got "
                f"{type(target).__name__}"
            )
        self.attach_surface(target)
        prepared = self.prepare_celestial_plan_for_bridge(plan)
        self._bridge.submit_prepared_frame(prepared)
        self._history[plan.generation] = prepared.manifest
        self._latest_plan = plan
        self._pending_picks = {
            key: request
            for key, request in self._pending_picks.items()
            if key[0] >= plan.generation
        }
        output = HostedSurfaceOutput(
            generation=plan.generation,
            surface_id=target.surface_id,
            metadata=freeze_json_mapping(
                {
                    "backend": self.backend_id,
                    "plan_native": True,
                    "celestial_plan_native": plan.celestial is not None,
                    "overlay_plan_native": plan.overlays is not None,
                    "terrain_plan_native": any(
                        primitive.kind == "terrain_mesh"
                        for scene_plan in plan.plans
                        for primitive in scene_plan.primitives
                    ),
                    "typed_binary_transport": True,
                }
            ),
        )
        if self._output_port is not None:
            self._output_port.frame_ready(output)
        return output

    def request_pick(
        self,
        request: PickRequest,
        plan: RenderPlanBundle | None = None,
    ) -> None:
        """Ask the live Three.js host for an asynchronous visual hit.

        There is deliberately no CPU PickIndex fallback here: a Three.js pick
        is valid only when its matching host response arrives.
        """

        self._require_started()
        active_plan = plan or self._latest_plan
        if (
            active_plan is None
            or active_plan.generation != request.generation
            or not self._bridge.is_started
        ):
            self._report_failure(
                operation="pick_request",
                message="No matching rendered Three.js generation for pick",
                generation=request.generation,
                request_id=request.request_id,
                code="stale_or_unrendered_generation",
            )
            return
        key = (request.generation, request.request_id)
        self._pending_picks[key] = request
        self._bridge.request_pick(
            request.generation,
            request.request_id,
            request.x,
            request.y,
            request.radius,
            request.purpose,
            request.action,
            request.options,
        )

    def get_frame_manifest(
        self, generation: int
    ) -> Mapping[str, JSONValue] | None:
        """Expose the exact typed transport only for backend conformance tests."""

        return self._history.get(int(generation))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._started = False
        self._bridge.close()
        self._output_port = None
        self._attached_viewport = None
        self._latest_plan = None
        self._history.clear()
        self._pending_picks.clear()
        self._bridge.remove_inbound_listener(self._on_bridge_message)

    def _require_started(self) -> None:
        if self._closed:
            raise RenderBackendLifecycleError("Three.js backend is closed")
        if not self._started:
            raise RenderBackendLifecycleError(
                "Three.js backend has not started"
            )

    def _on_bridge_message(self, message: Mapping[str, JSONValue]) -> None:
        """Route validated host events into the neutral output port."""

        operation = str(message.get("op", ""))
        if operation == OP_PICK_RESULT:
            self._publish_host_pick(message)
        elif operation == OP_ERROR:
            payload = message.get("payload")
            details = payload if isinstance(payload, Mapping) else {}
            raw_generation = message.get("gen", 0)
            self._report_failure(
                operation=str(details.get("operation", "host")),
                message=str(details.get("message", "Unknown Three.js error")),
                generation=(
                    int(raw_generation)
                    if isinstance(raw_generation, int)
                    else 0
                ),
                request_id=(
                    str(details["request_id"])
                    if isinstance(details.get("request_id"), str)
                    else None
                ),
                code=(
                    str(details["code"])
                    if isinstance(details.get("code"), str)
                    else None
                ),
            )

    def _publish_host_pick(self, message: Mapping[str, JSONValue]) -> None:
        payload = message.get("payload")
        if not isinstance(payload, Mapping):
            self._report_failure(
                operation="pick_result",
                message="Three.js returned a non-object pick payload",
                generation=_message_generation(message),
                code="invalid_pick_payload",
            )
            return
        generation = _message_generation(message)
        request_id = str(payload.get("request_id", ""))
        request = self._pending_picks.pop((generation, request_id), None)
        if request is None:
            return
        if (
            self._latest_plan is None
            or generation != request.generation
            or generation != self._latest_plan.generation
        ):
            return
        response = dict(payload)
        response["purpose"] = request.purpose
        response["generation"] = generation
        if isinstance(response.get("magnitude"), (int, float)):
            response.setdefault("mag", response["magnitude"])
        if not isinstance(response.get("kind"), str):
            object_kind = response.get("object_kind")
            response["kind"] = (
                object_kind
                if isinstance(object_kind, str) and object_kind
                else "none"
            )
        result = PickResult(
            generation=generation,
            request_id=request_id,
            payload=freeze_json_mapping(response),
        )
        if self._output_port is not None:
            self._output_port.pick_ready(result)

    def _report_failure(
        self,
        *,
        operation: str,
        message: str,
        generation: int = 0,
        request_id: str | None = None,
        code: str | None = None,
    ) -> None:
        if self._output_port is not None:
            self._output_port.backend_failed(
                RenderFailure(
                    backend_id=self.backend_id,
                    operation=operation,
                    message=message,
                    generation=generation,
                    request_id=request_id,
                    code=code,
                )
            )


def _message_generation(message: Mapping[str, JSONValue]) -> int:
    value = message.get("gen", 0)
    return int(value) if isinstance(value, int) else 0
