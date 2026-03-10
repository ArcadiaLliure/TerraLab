"""Runtime path helpers for TerraLab user data and caches."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


APP_NAME = "TerraLab"


def _appdata_root() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata)
    if sys.platform.startswith("darwin"):
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".local" / "share"


def app_root() -> Path:
    root = _appdata_root() / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_dir() -> Path:
    path = app_root() / "config"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return config_dir() / "config.json"


def data_dir(*parts: str) -> Path:
    path = app_root() / "data"
    for part in parts:
        path = path / str(part)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir(*parts: str) -> Path:
    path = app_root() / "cache"
    for part in parts:
        path = path / str(part)
    path.mkdir(parents=True, exist_ok=True)
    return path


def tmp_dir(*parts: str) -> Path:
    path = app_root() / "tmp"
    for part in parts:
        path = path / str(part)
    path.mkdir(parents=True, exist_ok=True)
    return path


def weather_cache_path() -> Path:
    return cache_dir("weather") / "metno_weather_cache.json"


def constellations_path() -> Path:
    return app_root() / "terralab_constellations.json"


def runtime_layout() -> dict[str, Path]:
    root = app_root()
    layout = {
        "root": root,
        "config": config_dir(),
        "data_gaia": data_dir("gaia"),
        "data_ngc": data_dir("ngc"),
        "data_milkyway": data_dir("milkyway"),
        "data_planck": data_dir("planck"),
        "data_elevation": data_dir("elevation"),
        "data_light_pollution": data_dir("light_pollution"),
        "cache_weather": cache_dir("weather"),
        "tmp": tmp_dir(),
    }
    return layout


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _legacy_config_candidates() -> list[Path]:
    candidates: list[Path] = []
    repo = _repo_root()
    candidates.append(repo / "data" / "config.json")
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "config.json")
        candidates.append(exe_dir / "data" / "config.json")
    return candidates


def migrate_legacy_config() -> Path:
    dst = config_path()
    if dst.exists():
        return dst
    for src in _legacy_config_candidates():
        try:
            if src.exists() and src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                return dst
        except Exception:
            continue
    return dst


def ensure_runtime_layout() -> dict[str, Path]:
    layout = runtime_layout()
    migrate_legacy_config()
    return layout

