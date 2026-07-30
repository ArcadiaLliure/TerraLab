"""Legacy compatibility seam for the now-planned celestial grid capability."""

from __future__ import annotations


class GridRenderer:
    """Invoke an explicitly supplied legacy callback; it owns no grid policy."""

    def render(self, ctx, state):
        """Renderitza el contingut visual segons l'estat actual.

        Par?metres:
        - ctx (Any): Valor del parametre 'ctx'.
        - state (Any): Valor del parametre 'state'.

        Retorna:
        - None.
        """
        callback = None
        extras = getattr(state, "extras", {}) or {}
        if isinstance(extras, dict):
            callback = extras.get("render_grid")
        if callable(callback):
            callback(ctx, state)
