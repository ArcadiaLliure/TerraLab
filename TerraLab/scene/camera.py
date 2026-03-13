"""Camera state used by scene and render layers."""

from __future__ import annotations

from dataclasses import dataclass

from TerraLab.util.math2d import clamp


@dataclass
class Camera:
    azimuth_offset: float = 0.0
    elevation_angle: float = 40.0
    zoom_level: float = 1.0
    vertical_offset_ratio: float = 0.3

    def set_azimuth(self, azimuth_deg: float) -> None:
        self.azimuth_offset = float(azimuth_deg) % 360.0

    def set_elevation(self, elevation_deg: float) -> None:
        self.elevation_angle = clamp(float(elevation_deg), -90.0, 90.0)

    def set_zoom(self, zoom_level: float) -> None:
        self.zoom_level = clamp(float(zoom_level), 0.5, 140.0)

    @property
    def fov_deg(self) -> float:
        # Legacy TerraLab relation: base horizontal FOV 100 deg / zoom.
        return 100.0 / max(0.001, float(self.zoom_level))
