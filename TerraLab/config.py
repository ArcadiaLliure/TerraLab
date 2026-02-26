import os
import sys
from TerraLab.common.utils import get_config_value, set_config_value, _load_config, _save_config

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
        cfg = _load_config()
        _save_config(cfg)

    def get(self, key, default=None):
        return get_config_value(key, default)

    def set(self, key, value):
        set_config_value(key, value)

    # --- Specific Getters/Setters ---

    def get_raster_path(self):
        return self.get("raster_path", None)

    def set_raster_path(self, path):
        self.set("raster_path", path)

    # Horizon quality: number of depth bands (10=Low, 20=Normal, 40=High, 60=Ultra)
    def get_horizon_quality(self):
        return int(self.get("horizon_quality", 20))

    def set_horizon_quality(self, n: int):
        self.set("horizon_quality", int(n))

