"""Renderer especific per mode telescopi.

No fa IO ni accedeix directament al widget.
"""

from __future__ import annotations

import warnings

from TerraLab.render.stars_renderer import StarsRenderer


class ScopeRenderer:
    """Pinta la vista telescopica usant RenderState i dataset actiu."""

    def __init__(self, stars_renderer: StarsRenderer | None = None) -> None:
        self._stars_renderer = stars_renderer or StarsRenderer()

    def render(self, ctx, state):
        """Renderitza únicament les estrelles en mode scope.

        El reticle actiu es pinta des de `TelescopeScopeController.draw` per
        evitar encuadraments duplicats.
        """
        return self._stars_renderer.render(ctx, state)

    def _draw_scope_overlay(self, ctx, state) -> None:
        """DEPRECATED: no es fa servir per evitar doble mira.

        Reemplaçament: `TerraLab.widgets.telescope_scope_mode.TelescopeScopeController.draw`.
        """
        warnings.warn(
            (
                "ScopeRenderer._draw_scope_overlay està deprecated; "
                "feu servir TelescopeScopeController.draw."
            ),
            DeprecationWarning,
            stacklevel=2,
        )
        return None
