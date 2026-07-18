"""UI input mapping helpers for sky camera interactions."""

from __future__ import annotations

from TerraLab.scene.camera import Camera


class SkyController:
    """Thin controller used by UI widgets to mutate camera state consistently."""

    def __init__(self, camera: Camera) -> None:
        self.camera = camera

    def apply_wheel_zoom(
        self, wheel_steps: float, factor_per_step: float = 1.10
    ) -> None:
        """Executa el metode apply_wheel_zoom de la classe SkyController.

        Par?metres:
        - wheel_steps (float): Valor del parametre 'wheel_steps'.
        - factor_per_step (float): Valor del parametre 'factor_per_step'.

        Retorna:
        - None.
        """
        factor = factor_per_step ** float(wheel_steps)
        self.camera.set_zoom(float(self.camera.zoom_level) * factor)

    def pan(
        self, delta_azimuth_deg: float, delta_elevation_deg: float
    ) -> None:
        """Executa el metode pan de la classe SkyController.

        Par?metres:
        - delta_azimuth_deg (float): Valor del parametre 'delta_azimuth_deg'.
        - delta_elevation_deg (float): Valor del parametre 'delta_elevation_deg'.

        Retorna:
        - None.
        """
        self.camera.set_azimuth(
            float(self.camera.azimuth_offset) + float(delta_azimuth_deg)
        )
        self.camera.set_elevation(
            float(self.camera.elevation_angle) + float(delta_elevation_deg)
        )
