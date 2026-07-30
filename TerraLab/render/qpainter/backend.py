"""Deprecated import path for the PyQt renderer adapter.

The implementation now lives under ``TerraLab.view.pyqt``.  Keeping this
small shim avoids breaking third-party extensions while ensuring composition
does not route runtime through the old ``render`` namespace.
"""

from TerraLab.view.pyqt.backend import QPainterRendererBackend


class QPainterLegacyBackend(QPainterRendererBackend):
    """Compatibility name; production code uses ``QPainterRendererBackend``."""


__all__ = ("QPainterLegacyBackend", "QPainterRendererBackend")
