"""UI input mapping helpers for sky camera interactions."""

from __future__ import annotations

from typing import Any


class SkyController:
    """Thin view controller bridging camera movements to ApplicationController or Camera."""

    def __init__(self, camera: Any = None, controller: Any = None) -> None:
        self.camera = camera
        self._controller = controller

    def apply_wheel_zoom(
        self, wheel_steps: float, factor_per_step: float = 1.10
    ) -> None:
        """Apply zoom step."""
        if self._controller is not None:
            self._controller.zoom_camera(wheel_steps, factor_per_step)
            return
        if self.camera is not None and hasattr(self.camera, "set_zoom"):
            factor = factor_per_step ** float(wheel_steps)
            self.camera.set_zoom(float(getattr(self.camera, "zoom_level", 1.0)) * factor)

    def pan(
        self, delta_azimuth_deg: float, delta_elevation_deg: float
    ) -> None:
        """Apply camera panning."""
        if self._controller is not None:
            self._controller.pan_camera(delta_azimuth_deg, delta_elevation_deg)
            return
        if self.camera is not None and hasattr(self.camera, "set_azimuth"):
            self.camera.set_azimuth(
                float(getattr(self.camera, "azimuth_offset", 0.0)) + float(delta_azimuth_deg)
            )
            self.camera.set_elevation(
                float(getattr(self.camera, "elevation_angle", 0.0)) + float(delta_elevation_deg)
            )

