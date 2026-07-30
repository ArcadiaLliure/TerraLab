"""The sole concrete render-backend composition root.

It selects an adapter by ``TERRALAB_RENDER_BACKEND`` and a neutral target
kind.  No implicit fallback is performed when a backend is absent or cannot
produce the requested output family.
"""

from __future__ import annotations

from typing import Mapping

from TerraLab.application.ports.rendering import (
    BackendRegistration,
    PresenterKind,
    RenderCapability,
    RenderTargetKind,
    RendererBackend,
)
from TerraLab.bootstrap.settings import RenderSettings, resolve_render_settings
from TerraLab.render.registry import BackendRegistry


def _qpainter_factory() -> RendererBackend:
    from TerraLab.view.pyqt.backend import QPainterRendererBackend

    return QPainterRendererBackend()


def _recording_factory() -> RendererBackend:
    from TerraLab.render.recording.backend import RecordingRendererBackend

    return RecordingRendererBackend()


def _threejs_factory() -> RendererBackend:
    from TerraLab.render.threejs.backend import ThreeJSRendererBackend

    return ThreeJSRendererBackend()


def create_backend_registry() -> BackendRegistry:
    """Register installed adapters explicitly without import-time side effects."""

    registry = BackendRegistry()
    registry.register(
        BackendRegistration(
            backend_id="qpainter",
            factory=_qpainter_factory,
            capabilities=frozenset(
                {
                    RenderCapability.RASTER_SHARED_FRAME,
                    RenderCapability.PICKING,
                    RenderCapability.INTERACTION,
                    RenderCapability.SKY_BACKGROUND,
                }
            ),
            target_kinds=frozenset({RenderTargetKind.SHARED_RASTER}),
        )
    )
    registry.register(
        BackendRegistration(
            backend_id="recording",
            factory=_recording_factory,
            capabilities=frozenset(
                {
                    RenderCapability.COMMAND_STREAM,
                    RenderCapability.PICKING,
                    RenderCapability.INTERACTION,
                    RenderCapability.SKY_BACKGROUND,
                    RenderCapability.RECORDING,
                }
            ),
            target_kinds=frozenset({RenderTargetKind.COMMAND_STREAM}),
        )
    )
    registry.register(
        BackendRegistration(
            backend_id="threejs",
            factory=_threejs_factory,
            capabilities=frozenset(
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
            ),
            target_kinds=frozenset({RenderTargetKind.HOSTED_SURFACE}),
        )
    )
    # ``opengl`` and ``vulkan`` deliberately are not registered until their hosts exist.
    return registry


def build_render_backend(
    *,
    explicit_backend: str | None = None,
    environment: Mapping[str, str] | None = None,
    user_backend: str | None = None,
    target_kind: RenderTargetKind = RenderTargetKind.SHARED_RASTER,
    presenter_kind: PresenterKind | None = None,
    registry: BackendRegistry | None = None,
) -> RendererBackend:
    """Resolve one installed backend compatible with a neutral target."""

    if presenter_kind is not None:
        target_kind = presenter_kind.target_kind
    settings: RenderSettings = resolve_render_settings(
        explicit_backend=explicit_backend,
        environment=environment,
        user_backend=user_backend,
    )
    required = {
        RenderCapability.PICKING,
        RenderCapability.INTERACTION,
    }
    if target_kind is RenderTargetKind.SHARED_RASTER:
        required.add(RenderCapability.RASTER_SHARED_FRAME)
    elif target_kind is RenderTargetKind.HOSTED_SURFACE:
        required.add(RenderCapability.HOSTED_SURFACE)
    else:
        required.add(RenderCapability.COMMAND_STREAM)
    return (registry or create_backend_registry()).create(
        settings.backend_id,
        target_kind=target_kind,
        required_capabilities=frozenset(required),
    )
