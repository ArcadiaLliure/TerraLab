"""Framework-neutral scene-model exports.

The canonical data classes remain in :mod:`TerraLab.scene.contracts` during
the compatibility window.  Re-exporting them here gives new code the target
architecture without making the model depend on a graphics toolkit.
"""

from TerraLab.scene.contracts import SceneFrame, Viewport

__all__ = ("SceneFrame", "Viewport")
