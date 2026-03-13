"""Horizon renderer layer interface.

Current implementation is a no-op placeholder because horizon is still drawn
in the legacy AstroCanvas path. Kept for modular migration compatibility.
"""

from __future__ import annotations


class HorizonRenderer:
    def render(self, ctx, state):
        callback = None
        extras = getattr(state, "extras", {}) or {}
        if isinstance(extras, dict):
            callback = extras.get("render_horizon")
        if callable(callback):
            callback(ctx, state)
