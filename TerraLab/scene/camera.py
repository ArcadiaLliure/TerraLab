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
        """Defineix azimuth a la instancia de Camera.

        Par?metres:
        - azimuth_deg (float): Valor del parametre 'azimuth_deg'.

        Retorna:
        - None.
        """
        self.azimuth_offset = float(azimuth_deg) % 360.0

    def set_elevation(self, elevation_deg: float) -> None:
        """Defineix elevation a la instancia de Camera.

        Par?metres:
        - elevation_deg (float): Valor del parametre 'elevation_deg'.

        Retorna:
        - None.
        """
        self.elevation_angle = clamp(float(elevation_deg), -90.0, 90.0)

    def set_zoom(self, zoom_level: float) -> None:
        """Defineix zoom a la instancia de Camera.

        Par?metres:
        - zoom_level (float): Valor del parametre 'zoom_level'.

        Retorna:
        - None.
        """
        self.zoom_level = clamp(float(zoom_level), 0.5, 140.0)

    @property
    def fov_deg(self) -> float:
        # Legacy TerraLab relation: base horizontal FOV 100 deg / zoom.
        """Executa el metode fov_deg de la classe Camera.

        Par?metres:
        - Cap.

        Retorna:
        - float: Valor retornat pel metode.
        """
        return 100.0 / max(0.001, float(self.zoom_level))
