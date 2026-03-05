"""Grid renderer layer interface.

Current implementation delegates to optional callback for safe migration.
"""

from __future__ import annotations


class GridRenderer:
    def render(self, ctx, state):
        callback = None
        extras = getattr(state, "extras", {}) or {}
        if isinstance(extras, dict):
            callback = extras.get("render_grid")
        if callable(callback):
            callback(ctx, state)
