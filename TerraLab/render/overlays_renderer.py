"""Overlay renderer layer interface.

Current implementation delegates to optional callback for safe migration.
"""

from __future__ import annotations


class OverlaysRenderer:
    def render(self, ctx, state):
        callback = None
        extras = getattr(state, "extras", {}) or {}
        if isinstance(extras, dict):
            callback = extras.get("render_overlays")
        if callable(callback):
            callback(ctx, state)
