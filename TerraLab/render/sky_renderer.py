"""Sky renderer orchestrator with per-layer timing."""

from __future__ import annotations

from TerraLab.render.grid_renderer import GridRenderer
from TerraLab.render.horizon_renderer import HorizonRenderer
from TerraLab.render.overlays_renderer import OverlaysRenderer
from TerraLab.render.sky.milkyway_overlay import MilkyWayOverlay
from TerraLab.render.stars_renderer import StarsRenderer


class SkyRenderer:
    def __init__(self) -> None:
        self.stars_renderer = StarsRenderer()
        self.horizon_renderer = HorizonRenderer()
        self.grid_renderer = GridRenderer()
        self.milkyway_overlay = MilkyWayOverlay()
        self.overlays_renderer = OverlaysRenderer()

    def render(self, ctx, state):
        diag = getattr(ctx, "diagnostics", None)

        if diag is not None:
            diag.start_timer("renderer_horizon")
        self.horizon_renderer.render(ctx, state)
        if diag is not None:
            diag.stop_timer("renderer_horizon")

        if diag is not None:
            diag.start_timer("renderer_milkyway")
        self.milkyway_overlay.render(ctx, state)
        if diag is not None:
            diag.stop_timer("renderer_milkyway")

        if diag is not None:
            diag.start_timer("renderer_stars")
        stars_result = self.stars_renderer.render(ctx, state)
        if diag is not None:
            diag.stop_timer("renderer_stars")

        if diag is not None:
            diag.start_timer("renderer_grid")
        self.grid_renderer.render(ctx, state)
        if diag is not None:
            diag.stop_timer("renderer_grid")

        if diag is not None:
            diag.start_timer("renderer_overlays")
        self.overlays_renderer.render(ctx, state)
        if diag is not None:
            diag.stop_timer("renderer_overlays")

        return stars_result
