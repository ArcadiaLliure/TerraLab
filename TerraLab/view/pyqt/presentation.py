"""Output-kind dispatch used by PyQt presenters.

The router makes the presentation capability explicit: a shared raster,
hosted surface and command stream are different products and are never
silently coerced into a ``QImage``.
"""

from __future__ import annotations

from collections.abc import Callable

from TerraLab.core.rendering_contracts.contracts import (
    RenderOutput,
    RenderOutputKind,
)


class QtRenderOutputRouter:
    """Dispatch an output to the presenter registered for its kind."""

    def __init__(
        self,
        *,
        raster: Callable[[RenderOutput], None] | None = None,
        hosted_surface: Callable[[RenderOutput], None] | None = None,
        command_stream: Callable[[RenderOutput], None] | None = None,
    ) -> None:
        self._handlers = {
            RenderOutputKind.RASTER: raster,
            RenderOutputKind.HOSTED_SURFACE: hosted_surface,
            RenderOutputKind.COMMAND_STREAM: command_stream,
        }

    def present(self, output: RenderOutput) -> None:
        handler = self._handlers[output.kind]
        if handler is None:
            raise TypeError(
                f"No PyQt presenter is registered for output kind {output.kind.value!r}"
            )
        handler(output)
