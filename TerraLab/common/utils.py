import json
import os
import sys
import threading
from typing import Any, Dict, Optional

from PyQt5.QtCore import QLocale
from PyQt5.QtGui import QFont, QFontDatabase, QFontInfo, QTextCharFormat, QTextCursor, QTextFormat
from PyQt5.QtWidgets import QPlainTextEdit, QTextEdit

from TerraLab.common.app_paths import (
    config_path as runtime_config_path,
    ensure_runtime_layout,
)


LANGUAGE_PRIORITY = ("ca", "es", "en", "fr", "it", "pt", "de", "nl", "el")
LANGUAGE_OPTIONS: Dict[str, str] = {
    "ca": "Català",
    "es": "Español",
    "en": "English",
    "fr": "Français",
    "it": "Italiano",
    "pt": "Português",
    "de": "Deutsch",
    "nl": "Nederlands",
    "el": "Ελληνικά",
}

_translations_cache: Optional[Dict[str, Dict[str, str]]] = None
_config_cache: Optional[Dict[str, Any]] = None
_config_lock = threading.Lock()


def get_base_dir() -> str:
    """Return TerraLab runtime root (`%APPDATA%/TerraLab` on Windows)."""
    layout = ensure_runtime_layout()
    return str(layout["root"])


def resource_path(relative_path: str) -> str:
    """
    Return absolute path to bundled resources (dev or PyInstaller).
    Runtime user data is managed by `common.app_paths`, not by this function.
    """
    rel = str(relative_path or "")
    if getattr(sys, "frozen", False):
        try:
            base_path = sys._MEIPASS  # type: ignore[attr-defined]
            exe_dir = os.path.dirname(sys.executable)
            norm_rel = rel.replace("\\", "/")
            external_candidate = None
            if "data/terrain_cache" in norm_rel:
                external_candidate = os.path.join(exe_dir, "maps", os.path.basename(rel))
            elif "data/stars" in norm_rel:
                external_candidate = os.path.join(exe_dir, "stars", os.path.basename(rel))
            if external_candidate and os.path.exists(external_candidate):
                return os.path.abspath(external_candidate)
        except Exception:
            base_path = os.path.abspath(".")
    else:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    path = os.path.join(base_path, rel)
    if not os.path.exists(path):
        parent = os.path.dirname(base_path)
        alt = os.path.join(parent, rel)
        if os.path.exists(alt):
            path = alt
    if "." in os.path.basename(path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            pass
    return os.path.abspath(path)


def _normalize_translation_payload(payload: object) -> Dict[str, Dict[str, str]]:
    if not isinstance(payload, dict):
        return {}

    # Legacy schema: key -> {lang: value}
    if payload and all(not (k in LANGUAGE_OPTIONS) for k in payload.keys()):
        out: Dict[str, Dict[str, str]] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            row: Dict[str, str] = {}
            for lang, literal in value.items():
                if not isinstance(lang, str):
                    continue
                if literal is None:
                    continue
                row[str(lang)] = str(literal)
            if row:
                out[str(key)] = row
        return out

    # Language-first schema: lang -> {key: value}
    by_key: Dict[str, Dict[str, str]] = {}
    for lang, lang_map in payload.items():
        if not isinstance(lang, str) or not isinstance(lang_map, dict):
            continue
        for key, literal in lang_map.items():
            if not isinstance(key, str):
                continue
            if literal is None:
                continue
            row = by_key.setdefault(key, {})
            row[lang] = str(literal)
    return by_key


def _load_translations() -> Dict[str, Dict[str, str]]:
    global _translations_cache
    if _translations_cache is None:
        try:
            with open(resource_path("data/translations.json"), "r", encoding="utf-8") as f:
                raw = json.load(f)
            _translations_cache = _normalize_translation_payload(raw)
        except Exception:
            _translations_cache = {}
    return _translations_cache


def getTraduction(key: str, default: str) -> str:
    try:
        translations = _load_translations()
        entry = translations.get(str(key), {})
        lang = get_language("ca")
        if isinstance(entry, dict):
            if lang in entry:
                return str(entry[lang])
            for candidate in LANGUAGE_PRIORITY:
                if candidate in entry:
                    return str(entry[candidate])
        return str(default)
    except Exception:
        return str(default)


def _load_config() -> Dict[str, Any]:
    global _config_cache
    with _config_lock:
        if _config_cache is None:
            ensure_runtime_layout()
            path = runtime_config_path()
            if path.exists():
                try:
                    with path.open("r", encoding="utf-8") as f:
                        payload = json.load(f)
                    _config_cache = payload if isinstance(payload, dict) else {}
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
    ensure_runtime_layout()
    path = runtime_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _config_lock:
        _config_cache = dict(cfg)
        with path.open("w", encoding="utf-8") as f:
            json.dump(_config_cache, f, ensure_ascii=False, indent=2)


def get_config_value(path: str, default=None, *, refresh: bool = False):
    if refresh:
        try:
            _clear_cache_config()
        except Exception:
            pass

    cfg = _load_config() or {}
    if path in cfg:
        return cfg.get(path, default)

    cur = cfg
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _set_nested_value(container: Dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = [p for p in str(dotted_key).split(".") if p]
    if not parts:
        return
    cur = container
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def set_config_value(key: str, value: Any) -> None:
    cfg = _load_config()
    if "." in str(key):
        # Preserve direct-key compatibility and provide nested schema for new blocks.
        cfg[str(key)] = value
        _set_nested_value(cfg, str(key), value)
    else:
        cfg[str(key)] = value
    _save_config(cfg)


def get_language(default: str = "ca") -> str:
    lang = str(get_config_value("idioma", default) or default)
    if lang in LANGUAGE_OPTIONS:
        return lang
    return str(default)


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
                _resource_path = resource_path
            except Exception:
                _resource_path = lambda p: p
        else:
            _resource_path = base_resolver

        font_path = _resource_path(font_rel_path)
        if not os.path.exists(font_path):
            return None

        font_id = QFontDatabase.addApplicationFont(font_path)
        if font_id == -1:
            return None

        families = QFontDatabase.applicationFontFamilies(font_id) or []
        if not families:
            return None

        family = families[0]
        return family
    except Exception:
        return None
