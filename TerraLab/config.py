import os
import sys

from TerraLab.common.utils import (
    _load_config,
    _save_config,
    get_config_value,
    set_config_value,
)


class ConfigManager:
    """
    Deprecated: Facade to maintain backward compatibility.
    Use TerraLab.common.utils directly for configuration.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
        return cls._instance

    def _load(self):
        # Now handled by utils.py
        pass

    def save(self):
        # Automatically handled by utils.py set_config_value but can be forced
        """Executa el metode save de la classe ConfigManager.

        Par?metres:
        - Cap.

        Retorna:
        - None.
        """
        cfg = _load_config()
        _save_config(cfg)

    def get(self, key, default=None):
        """Executa el metode get de la classe ConfigManager.

        Par?metres:
        - key (Any): Valor del parametre 'key'.
        - default (Any): Valor del parametre 'default'.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        return get_config_value(key, default)

    def set(self, key, value):
        """Executa el metode set de la classe ConfigManager.

        Par?metres:
        - key (Any): Valor del parametre 'key'.
        - value (Any): Valor del parametre 'value'.

        Retorna:
        - None.
        """
        set_config_value(key, value)

    # Historical aliases still used by a few dialogs.
    get_value = get
    set_value = set

    # --- Specific Getters/Setters ---

    def get_raster_path(self):
        """Obte raster path de la instancia de ConfigManager.

        Par?metres:
        - Cap.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        return self.get("raster_path", None)

    def set_raster_path(self, path):
        """Defineix raster path a la instancia de ConfigManager.

        Par?metres:
        - path (Any): Valor del parametre 'path'.

        Retorna:
        - None.
        """
        self.set("raster_path", path)

    # Horizon quality: number of depth bands (10=Low, 20=Normal, 40=High, 60=Ultra)
    def get_horizon_quality(self):
        """Obte horizon quality de la instancia de ConfigManager.

        Par?metres:
        - Cap.

        Retorna:
        - Any: Valor retornat pel metode.
        """
        return int(self.get("horizon_quality", 20))

    def set_horizon_quality(self, n: int):
        """Defineix horizon quality a la instancia de ConfigManager.

        Par?metres:
        - n (int): Valor del parametre 'n'.

        Retorna:
        - None.
        """
        self.set("horizon_quality", int(n))

    def get_horizon_ray_step_deg(self):
        from TerraLab.terrain.ray_precision import normalize_ray_step_deg

        return normalize_ray_step_deg(self.get("horizon_ray_step_deg", 0.5))

    def set_horizon_ray_step_deg(self, value: float):
        from TerraLab.terrain.ray_precision import normalize_ray_step_deg

        self.set("horizon_ray_step_deg", normalize_ray_step_deg(value))

    def get_terrain_range_settings(self):
        """Load range settings; missing legacy keys intentionally mean safe auto mode."""
        from TerraLab.terrain.visibility_range import TerrainRangeSettings

        raw = self.get("terrain_visibility_range", {})
        return TerrainRangeSettings.from_mapping(raw if isinstance(raw, dict) else {})

    def set_terrain_range_settings(self, settings):
        self.set("terrain_visibility_range", settings.validated().to_dict())
