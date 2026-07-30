"""Compatibility import path for the neutral rendering contract.

New code imports :mod:`TerraLab.core.rendering_contracts`.  This module stays
Qt-free so callers migrating one package at a time do not acquire a graphics
dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from TerraLab.core.rendering_contracts.contracts import (
    BackendNotFoundError,
    CommandStreamOutput,
    CommandStreamTarget,
    DuplicateBackendError,
    HostedSurfaceOutput,
    HostedSurfaceTarget,
    PickRequest,
    PickResult,
    PresenterIncompatibleError,
    RasterFrameHandle,
    RasterFrameOutput,
    RenderBackendConfigurationError,
    RenderBackendLifecycleError,
    RenderCapability,
    RenderFailure,
    RenderOutput,
    RenderOutputKind,
    RenderOutputPort,
    RenderTarget,
    RenderTargetKind,
    RendererBackend,
    SharedRasterTarget,
)


class PresenterKind(str, Enum):
    """Deprecated name retained for callers during the target migration."""

    SHARED_FRAME = "shared_frame"
    HOSTED_SURFACE = "hosted_surface"
    RECORDING = "recording"

    @property
    def target_kind(self) -> RenderTargetKind:
        return {
            PresenterKind.SHARED_FRAME: RenderTargetKind.SHARED_RASTER,
            PresenterKind.HOSTED_SURFACE: RenderTargetKind.HOSTED_SURFACE,
            PresenterKind.RECORDING: RenderTargetKind.COMMAND_STREAM,
        }[self]


@dataclass(frozen=True, slots=True)
class BackendRegistration:
    """Composition-time registration with explicit compatible target kinds."""

    backend_id: str
    factory: Callable[[], RendererBackend]
    capabilities: frozenset[RenderCapability]
    target_kinds: frozenset[RenderTargetKind] = field(default_factory=frozenset)
    presenter_kinds: frozenset[PresenterKind] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        targets = self.target_kinds
        presenters = self.presenter_kinds
        if not targets and presenters:
            targets = frozenset(item.target_kind for item in presenters)
            object.__setattr__(self, "target_kinds", targets)
        if not presenters and targets:
            object.__setattr__(
                self,
                "presenter_kinds",
                frozenset(
                    next(
                        presenter
                        for presenter in PresenterKind
                        if presenter.target_kind is item
                    )
                    for item in targets
                ),
            )
        if not targets:
            raise ValueError("Backend registration needs at least one target kind")


# The old names describe output shape, not an alternate renderer contract.
# Recording is a command stream in the final architecture.
RecordingOutput = CommandStreamOutput

__all__ = (
    "BackendNotFoundError",
    "BackendRegistration",
    "CommandStreamOutput",
    "CommandStreamTarget",
    "DuplicateBackendError",
    "HostedSurfaceOutput",
    "HostedSurfaceTarget",
    "PickRequest",
    "PickResult",
    "PresenterIncompatibleError",
    "PresenterKind",
    "RasterFrameHandle",
    "RasterFrameOutput",
    "RecordingOutput",
    "RenderBackendConfigurationError",
    "RenderBackendLifecycleError",
    "RenderCapability",
    "RenderFailure",
    "RenderOutput",
    "RenderOutputKind",
    "RenderOutputPort",
    "RenderTarget",
    "RenderTargetKind",
    "RendererBackend",
    "SharedRasterTarget",
)
