import os
import sys
from PyQt5.QtCore import QLocale
from PyQt5.QtGui import QFontDatabase, QTextFormat, QFont, QTextCharFormat, QTextCursor, QFontInfo
from PyQt5.QtWidgets import QTextEdit, QPlainTextEdit

def get_base_dir() -> str:
    """
    Determine the base directory for storing Scriptorium resources and data.

    The application should not persist user files in the same directory as the
    executable.  Instead, it stores everything under a userâ€‘specific folder in
    the operating system's standard application data location.  On Windows this
    corresponds to ``%APPDATA%/Scriptorium``; on macOS it is
    ``~/Library/ApplicationÂ Support/Scriptorium``; and on Linux and other
    POSIX systems it is ``~/.local/share/Scriptorium``.

    Returns:
        str: Absolute path to the base application data directory.
    """
    # Windows: use APPDATA if available, otherwise fall back to the home
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        base = os.path.join(base, "Scriptorium")
    # macOS: Application Support within the user's Library
    elif sys.platform.startswith("darwin"):
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Scriptorium")
        base = resource_path()
    # Linux and others: use the XDG default (~/.local/share)
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share", "Scriptorium")
    # Ensure the directory exists so callers can safely write into it
    os.makedirs(base, exist_ok=True)
    return base


def resource_path(relative_path):
    """
    Get absolute path to resource, works for dev and for PyInstaller.
    Prioritizes external folders (Install Dir) for specific assets (maps, stars, config).
    """
    path = relative_path
    
    # Check if running as compiled EXE (Frozen)
    if getattr(sys, 'frozen', False):
        try:
             # Standard PyInstaller temp folder
             base_path = sys._MEIPASS
             
             # External assets overrides (Install Dir)
             exe_dir = os.path.dirname(sys.executable)
             
             norm_rel = relative_path.replace("\\", "/")
             external_candidate = None
             
             # Map internal paths to external folders
             if "data/terrain_cache" in norm_rel:
                 external_candidate = os.path.join(exe_dir, "maps", os.path.basename(relative_path))
             elif "data/stars" in norm_rel:
                  external_candidate = os.path.join(exe_dir, "stars", os.path.basename(relative_path))
             elif norm_rel.endswith("config.json"):
                  external_candidate = os.path.join(exe_dir, "config.json")
                  
             if external_candidate and os.path.exists(external_candidate):
                 return external_candidate
                 
        except Exception:
             base_path = os.path.abspath(".")
    else:
        # Dev mode: Parent of common/ -> TerraLab root
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    path = os.path.join(base_path, relative_path)
    if not os.path.exists(path):
        # Fallback for dev mode variations
        parent = os.path.dirname(base_path)
        path = os.path.join(parent, relative_path)
        if not os.path.exists(path):
             path = os.path.join(base_path, relative_path)
             
    # Create parent dirs if writing to file
    if "." in os.path.basename(path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except: 
            pass
            
    return path
    return path

# ... (imports)
import json
from typing import Any, Dict, Optional

# Constants
LANGUAGE_OPTIONS: Dict[str, str] = {
    "ca": "Català",
    "es": "Español",
    "en": "English",
    "fr": "Français",
    "it": "Italiano"
}

_translations_cache: Optional[Dict[str, Dict[str, str]]] = None
_config_cache: Optional[Dict[str, Any]] = None

import threading
_config_lock = threading.Lock()

def _load_translations() -> Dict[str, Dict[str, str]]:
    global _translations_cache
    if _translations_cache is None:
        try:
            with open(resource_path("data/translations.json"), "r", encoding="utf-8") as f:
                _translations_cache = json.load(f)
        except Exception:
            _translations_cache = {}
    return _translations_cache

def getTraduction(key: str, default: str) -> str:
    try:
        translations = _load_translations()
        entry = translations.get(key, {}) 
        literal = entry.get(get_config_value("idioma", "ca"), default)
        return literal
    except Exception:
        return default

def _load_config() -> Dict[str, Any]:
    global _config_cache
    with _config_lock:
        if _config_cache is None:
            path = resource_path("data/config.json")
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        _config_cache = json.load(f)
                except Exception:
                    _config_cache = {}
            else:
                _config_cache = {}
        return dict(_config_cache)

def _clear_cache_config():
    global _config_cache
    with _config_lock:
        _config_cache = None

def _save_config(cfg: Dict[str, Any]) -> None:
    global _config_cache
    path = resource_path("data/config.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _config_lock:
        _config_cache = dict(cfg)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_config_cache, f, ensure_ascii=False, indent=2)

def get_config_value(path: str, default=None, *, refresh: bool = False):
    """
    Lee config con soporte de 'audio.clic' (claves anidadas).
    Si refresh=True, limpia la caché antes de leer.
    """
    if refresh:
        try:
            _clear_cache_config()
        except Exception:
            pass

    cfg = _load_config() or {}
    cur = cfg
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur

def set_config_value(key: str, value: Any) -> None:
    cfg = _load_config()
    cfg[key] = value
    _save_config(cfg)

def get_language(default: str = "ca") -> str:
    lang = get_config_value("idioma", default)
    return lang if lang in LANGUAGE_OPTIONS else default

def set_language(lang: str) -> None:
    if lang not in LANGUAGE_OPTIONS:
        raise ValueError(f"Unsupported language: {lang}")
    set_config_value("idioma", lang)
    global _translations_cache
    _translations_cache = None

def load_custom_font(font_rel_path: str, base_resolver=None) -> str | None:
    """
    Carga una fuente (TTF/OTF) y devuelve el *family name* usable por QFont.
    - font_rel_path: ruta relativa que pasará por resource_path (si existe) o se tomará tal cual.
    - base_resolver: inyecta tu resource_path; si no se pasa, intenta importarlo desde utils.
    """
    try:
        if base_resolver is None:
            try:
                # Use module-level resource_path directly
                _resource_path = resource_path
            except Exception:
                _resource_path = lambda p: p
        else:
            _resource_path = base_resolver

        font_path = _resource_path(font_rel_path)
        # print(f"[fonts] Intentando cargar: {font_path}")
        if not os.path.exists(font_path):
            # print(f"[fonts] No existe el archivo: {font_path}")
            return None

        font_id = QFontDatabase.addApplicationFont(font_path)
        if font_id == -1:
            # print(f"[fonts] addApplicationFont falló: {font_path}")
            return None

        families = QFontDatabase.applicationFontFamilies(font_id) or []
        if not families:
            # print(f"[fonts] Sin familias en la fuente: {font_path}")
            return None

        family = families[0]
        # print(f"[fonts] OK: cargada familia «{family}»")
        return family
    except Exception as ex:
        print(f"[fonts] Excepción cargando fuente «{font_rel_path}»: {ex}")
        return None
