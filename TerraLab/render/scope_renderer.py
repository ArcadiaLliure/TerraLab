"""Scope renderer interface and QPainter presentation integration."""

from __future__ import annotations

from TerraLab.render.qpainter.scope import render_qpainter_scope_plan
from TerraLab.render.stars_renderer import StarsRenderer
from TerraLab.scene.plans.scope import ScopePlan


class ScopeRenderer:
    """Renders telescope view stars and reticle overlay plan."""

    def __init__(self, stars_renderer: StarsRenderer | None = None) -> None:
        self._stars_renderer = stars_renderer or StarsRenderer()

    def render(self, ctx, state) -> None:
        """Renders telescope view stars."""
        return self._stars_renderer.render(ctx, state)

    def render_plan(
        self, painter, plan: ScopePlan, viewport_w: float, viewport_h: float
    ) -> None:
        """Renders ScopePlan onto QPainter context."""
        render_qpainter_scope_plan(painter, plan, viewport_w, viewport_h)
