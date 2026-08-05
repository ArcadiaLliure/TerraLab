"""Headless, zero-graphics recording backend for frame and contract validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from TerraLab.core.rendering_contracts.contracts import (
    CommandStreamOutput,
    CommandStreamTarget,
    PickRequest,
    PresenterKind,
    RenderBackendLifecycleError,
    RenderCapability,
    RenderOutput,
    RendererBackend,
    RenderOutputPort,
    RenderTarget,
    RenderTargetKind,
)
from TerraLab.core.rendering_contracts.plans import RenderPlanBundle
from TerraLab.scene.contracts import (
    JSONValue,
    SceneFrame,
    freeze_json_mapping,
)


def _compute_deterministic_hash(data: Mapping[str, Any]) -> str:
    """Return SHA256 hex string for a JSON-serializable dictionary."""
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


class RecordingRendererBackend(RendererBackend):
    """Zero-graphics reference implementation recording frame manifests and primitive metrics."""

    backend_id = "recording"
    capabilities = frozenset(
        {
            RenderCapability.COMMAND_STREAM,
            RenderCapability.SKY_BACKGROUND,
            RenderCapability.RECORDING,
        }
    )
    presenter_kinds = frozenset({PresenterKind.RECORDING})
    target_kinds = frozenset({RenderTargetKind.COMMAND_STREAM})
    requires_host_pick = False

    def __init__(self) -> None:
        self._started = False
        self._closed = False
        self._output_port: RenderOutputPort | None = None
        self._history: dict[int, Mapping[str, JSONValue]] = {}
        self._last_frame: SceneFrame | None = None

    @property
    def is_started(self) -> bool:
        return self._started and not self._closed

    @property
    def is_closed(self) -> bool:
        return self._closed

    def start(self, output_port: RenderOutputPort) -> None:
        if self._closed:
            raise RenderBackendLifecycleError(
                "Cannot restart a closed recording backend"
            )
        if self._started:
            raise RenderBackendLifecycleError(
                "Recording backend is already started"
            )
        self._started = True
        self._output_port = output_port

    def submit(self, frame: SceneFrame) -> None:
        self._require_started()
        if frame.schema_version != 1:
            raise ValueError(
                f"Unsupported scene frame schema: {frame.schema_version}"
            )
        if frame.generation < 0:
            raise ValueError("Scene frame generation cannot be negative")

        # Build layer records in execution order
        layers_order: list[dict[str, Any]] = [
            {
                "name": layer.value if hasattr(layer, "value") else str(layer),
                "enabled": True,
            }
            for layer in frame.layers.order
        ]

        # Primitive metrics & counts
        ephemeris_bodies_count = (
            len(frame.ephemeris.payload) if frame.ephemeris else 0
        )
        constellation_groups_count = (
            len(frame.constellation.groups) if frame.constellation else 0
        )

        primitive_counts = {
            "ephemeris_bodies": ephemeris_bodies_count,
            "constellation_groups": constellation_groups_count,
            "measurement_clear_revision": int(frame.measurement.clear_revision)
            if frame.measurement
            else 0,
            "bortle": int(frame.bortle),
        }

        # Resource summaries (hashes/types only, no raw arrays)
        resources_summary: dict[str, Any] = {
            "stars_revision": getattr(frame.resources, "stars_revision", 0),
            "ngc_revision": getattr(frame.resources, "ngc_revision", 0),
            "dem_revision": getattr(frame.resources, "dem_revision", 0),
        }

        manifest_data: dict[str, Any] = {
            "recording_id": f"rec-{frame.generation}",
            "generation": int(frame.generation),
            "schema_version": int(frame.schema_version),
            "viewport": {
                "width": int(frame.viewport.width),
                "height": int(frame.viewport.height),
                "dpr": float(frame.viewport.device_pixel_ratio),
            },
            "bortle": int(frame.bortle),
            "magnitude_limit": float(frame.magnitude_limit),
            "layers": layers_order,
            "primitive_counts": primitive_counts,
            "resources": resources_summary,
        }

        manifest_hash = _compute_deterministic_hash(manifest_data)
        manifest_data["manifest_hash"] = manifest_hash

        frozen_manifest = freeze_json_mapping(manifest_data)
        self._history[int(frame.generation)] = frozen_manifest
        self._last_frame = frame

        output = CommandStreamOutput(
            generation=int(frame.generation),
            stream_id=str(manifest_data["recording_id"]),
            commands=tuple(
                freeze_json_mapping({"op": "capability", "name": plan["name"]})
                for plan in layers_order
            ),
            metadata=frozen_manifest,
        )
        if self._output_port is not None:
            self._output_port.frame_ready(output)

    def render(
        self,
        plan: RenderPlanBundle,
        target: RenderTarget,
    ) -> RenderOutput:
        """Emit a deterministic command stream for an already resolved plan."""

        self._require_started()
        if not isinstance(target, CommandStreamTarget):
            raise TypeError("Recording backend requires a CommandStreamTarget")
        self.submit(plan.frame)
        manifest = self.get_last_manifest() or freeze_json_mapping({})
        commands = tuple(
            freeze_json_mapping(
                {
                    "op": "render_capability",
                    "capability": item.capability,
                    "generation": plan.generation,
                }
            )
            for item in plan.plans
        )
        return CommandStreamOutput(
            generation=plan.generation,
            stream_id=target.stream_id,
            commands=commands,
            metadata=manifest,
        )

    def request_pick(
        self,
        request: PickRequest,
        plan: RenderPlanBundle | None = None,
    ) -> None:
        """Recording has no visual surface and deliberately has no picking."""

        self._require_started()
        raise RenderBackendLifecycleError(
            "Recording backend does not implement visual picking"
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._started = False
        self._output_port = None
        self._last_frame = None

    def get_manifest(self, generation: int) -> Mapping[str, JSONValue] | None:
        return self._history.get(int(generation))

    def get_last_manifest(self) -> Mapping[str, JSONValue] | None:
        if self._last_frame is None:
            return None
        return self._history.get(int(self._last_frame.generation))

    def get_all_manifests(self) -> tuple[Mapping[str, JSONValue], ...]:
        return tuple(self._history[gen] for gen in sorted(self._history))

    def _require_started(self) -> None:
        if self._closed:
            raise RenderBackendLifecycleError("Recording backend is closed")
        if not self._started:
            raise RenderBackendLifecycleError(
                "Recording backend has not started"
            )
