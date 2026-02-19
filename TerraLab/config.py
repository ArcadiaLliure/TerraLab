
import json
import os
import sys

CONFIG_FILENAME = "config.json"

class ConfigManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            cls._instance._load()
        return cls._instance

    def _get_config_path(self):
        # Config lives next to the executable or script
        if getattr(sys, 'frozen', False):
             base_path = os.path.dirname(sys.executable)
        else:
             base_path = os.path.dirname(os.path.abspath(__file__)) # TerraLab/
             # Go up one level to root if in package
             base_path = os.path.abspath(os.path.join(base_path, ".."))
        
        return os.path.join(base_path, CONFIG_FILENAME)

    def _load(self):
        self.config_path = self._get_config_path()
        self.data = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
            except Exception as e:
                print(f"[ConfigManager] Error loading config: {e}")

    def save(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
             print(f"[ConfigManager] Error saving config: {e}")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    # --- Specific Getters/Setters ---

    def get_raster_path(self):
        return self.get("raster_path", None)

    def set_raster_path(self, path):
        self.set("raster_path", path)
