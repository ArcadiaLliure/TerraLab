"""The sole concrete render-backend composition root.

It selects an adapter by ``TERRALAB_RENDER_BACKEND`` and a neutral target
kind.  No implicit fallback is performed when a backend is absent or cannot
produce the requested output family.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Callable, Mapping

from TerraLab.core.rendering_contracts.contracts import (
    BackendRegistration,
    PresenterKind,
    PresenterIncompatibleError,
    RenderCapability,
    RenderTargetKind,
    RendererBackend,
)
from TerraLab.bootstrap.settings import RenderSettings, resolve_render_settings
from TerraLab.render.registry import BackendRegistry


PresenterFactory = Callable[[object, object | None], object]


@dataclass(frozen=True, slots=True)
class RenderRoute:
    """One complete desktop render route selected at composition time.

    The route deliberately retains factories instead of a live presenter or
    renderer instance.  The GUI owns both compatible presentation targets;
    compute workers never receive a frame or a renderer implementation.
    """

    backend_id: str
    target_kind: RenderTargetKind
    presenter_kind: PresenterKind
    _backend_factory: Callable[[], RendererBackend]
    _presenter_factory: PresenterFactory

    def create_backend(self) -> RendererBackend:
        """Construct the selected view adapter in its owning process."""

        return self._backend_factory()

    def create_presenter(
        self,
        runtime: object,
        parent: object | None = None,
    ) -> object:
        """Construct only the presenter compatible with this route's output."""

        return self._presenter_factory(runtime, parent)


def _qpainter_factory() -> RendererBackend:
    from TerraLab.view.pyqt.backend import QPainterRendererBackend

    return QPainterRendererBackend()


def _recording_factory() -> RendererBackend:
    from TerraLab.render.recording.backend import RecordingRendererBackend

    return RecordingRendererBackend()


def _threejs_factory() -> RendererBackend:
    from TerraLab.render.threejs.backend import ThreeJSRendererBackend

    return ThreeJSRendererBackend()


def _shared_raster_presenter_factory(
    runtime: object,
    parent: object | None,
) -> object:
    presenter_type = import_module(
        "TerraLab.ui.frame_presenter"
    ).SharedFramePresenter
    return presenter_type(runtime, parent)


def _hosted_surface_presenter_factory(
    runtime: object,
    parent: object | None,
) -> object:
    presenter_type = import_module(
        "TerraLab.ui.hosted_surface_presenter"
    ).HostedSurfacePresenter
    return presenter_type(runtime, parent)


def create_backend_registry() -> BackendRegistry:
    """Register installed adapters explicitly without import-time side effects."""

    registry = BackendRegistry()
    _register_implemented_backend(registry, _qpainter_factory)
    _register_implemented_backend(registry, _recording_factory)
    _register_implemented_backend(registry, _threejs_factory)
    return registry


def _register_implemented_backend(
    registry: BackendRegistry,
    factory: Callable[[], RendererBackend],
) -> None:
    """Register the capabilities implemented by a single backend class.

    Keeping this assertion in the composition root makes capability drift
    impossible: the registry records the exact values provided by the adapter,
    rather than a duplicated capability manifest.
    """

    backend = factory()
    try:
        registry.register(
            BackendRegistration(
                backend_id=backend.backend_id,
                factory=factory,
                capabilities=backend.capabilities,
                target_kinds=backend.target_kinds,
            )
        )
    finally:
        backend.close()


def _resolve_render_selection(
    *,
    explicit_backend: str | None = None,
    environment: Mapping[str, str] | None = None,
    user_backend: str | None = None,
    target_kind: RenderTargetKind | None = None,
    presenter_kind: PresenterKind | None = None,
    registry: BackendRegistry | None = None,
) -> tuple[
    BackendRegistry,
    RenderSettings,
    RenderTargetKind,
    frozenset[RenderCapability],
]:
    """Resolve and validate the backend/target pair without constructing it."""

    if presenter_kind is not None:
        if (
            target_kind is not None
            and target_kind is not presenter_kind.target_kind
        ):
            raise ValueError("target_kind and presenter_kind disagree")
        target_kind = presenter_kind.target_kind
    settings: RenderSettings = resolve_render_settings(
        explicit_backend=explicit_backend,
        environment=environment,
        user_backend=user_backend,
    )
    active_registry = registry or create_backend_registry()
    # Every installed desktop backend currently has one output family.  Keep
    # the decision here so changing ``render.backend`` selects its target
    # rather than forcing callers to retain QPainter's raster assumption.
    if target_kind is None:
        known = active_registry.registration_for(settings.backend_id)
        if len(known.target_kinds) != 1:
            supported = ", ".join(
                kind.value for kind in sorted(known.target_kinds, key=str)
            )
            raise ValueError(
                f"Render backend {settings.backend_id!r} has multiple targets "
                f"({supported}); target_kind must be selected explicitly"
            )
        target_kind = next(iter(known.target_kinds))
    required: set[RenderCapability] = set()
    if target_kind is RenderTargetKind.SHARED_RASTER:
        required.add(RenderCapability.RASTER_SHARED_FRAME)
    elif target_kind is RenderTargetKind.HOSTED_SURFACE:
        required.add(RenderCapability.HOSTED_SURFACE)
    else:
        required.add(RenderCapability.COMMAND_STREAM)
    active_registry.validate_selection(
        settings.backend_id,
        target_kind=target_kind,
        required_capabilities=frozenset(required),
    )
    return active_registry, settings, target_kind, frozenset(required)


def build_render_route(
    *,
    explicit_backend: str | None = None,
    environment: Mapping[str, str] | None = None,
    user_backend: str | None = None,
    target_kind: RenderTargetKind | None = None,
    presenter_kind: PresenterKind | None = None,
    registry: BackendRegistry | None = None,
) -> RenderRoute:
    """Select backend, target, presenter and worker lifecycle as one route."""

    active_registry, settings, selected_target, required = (
        _resolve_render_selection(
            explicit_backend=explicit_backend,
            environment=environment,
            user_backend=user_backend,
            target_kind=target_kind,
            presenter_kind=presenter_kind,
            registry=registry,
        )
    )
    if selected_target is RenderTargetKind.SHARED_RASTER:
        route_presenter = PresenterKind.SHARED_FRAME
        presenter_factory = _shared_raster_presenter_factory
    elif selected_target is RenderTargetKind.HOSTED_SURFACE:
        route_presenter = PresenterKind.HOSTED_SURFACE
        presenter_factory = _hosted_surface_presenter_factory
    else:
        raise PresenterIncompatibleError(
            "The desktop composition has no presenter lifecycle for "
            f"{selected_target.value!r}"
        )

    def create_selected_backend() -> RendererBackend:
        return active_registry.create(
            settings.backend_id,
            target_kind=selected_target,
            required_capabilities=required,
        )

    return RenderRoute(
        backend_id=settings.backend_id,
        target_kind=selected_target,
        presenter_kind=route_presenter,
        _backend_factory=create_selected_backend,
        _presenter_factory=presenter_factory,
    )


def build_render_backend(
    *,
    explicit_backend: str | None = None,
    environment: Mapping[str, str] | None = None,
    user_backend: str | None = None,
    target_kind: RenderTargetKind | None = None,
    presenter_kind: PresenterKind | None = None,
    registry: BackendRegistry | None = None,
) -> RendererBackend:
    """Construct one backend compatible with its explicitly resolved target."""

    active_registry, settings, selected_target, required = (
        _resolve_render_selection(
            explicit_backend=explicit_backend,
            environment=environment,
            user_backend=user_backend,
            target_kind=target_kind,
            presenter_kind=presenter_kind,
            registry=registry,
        )
    )
    return active_registry.create(
        settings.backend_id,
        target_kind=selected_target,
        required_capabilities=required,
    )
