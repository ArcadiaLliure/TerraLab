"""Runtime path helpers for TerraLab user data and caches."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from TerraLab.common.data_library import (
    DataLibrary,
    application_state_root,
    platform_state_base,
)


def _appdata_root() -> Path:
    return platform_state_base()


def app_root() -> Path:
    root = application_state_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_dir() -> Path:
    path = app_root() / "config"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return config_dir() / "config.json"


def data_dir(*parts: str) -> Path:
    layout = DataLibrary.current(create=True).layout(create=True)
    aliases = {
        "gaia": "data_gaia",
        "ngc": "data_ngc",
        "milkyway": "data_milkyway",
        "planck": "data_planck",
        "ephemeris": "data_ephemeris",
        "elevation": "data_elevation",
        "surface": "data_surface",
        "light_pollution": "data_light_pollution",
    }
    normalized = [str(part) for part in parts]
    if normalized and normalized[0] in aliases:
        path = layout[aliases[normalized.pop(0)]]
    else:
        path = layout["root"] / "data"
    for part in normalized:
        path /= part
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir(*parts: str) -> Path:
    path = DataLibrary.current(create=True).root / "cache"
    for part in parts:
        path = path / str(part)
    path.mkdir(parents=True, exist_ok=True)
    return path


def tmp_dir(*parts: str) -> Path:
    path = DataLibrary.current(create=True).root / "tmp"
    for part in parts:
        path = path / str(part)
    path.mkdir(parents=True, exist_ok=True)
    return path


def weather_cache_path() -> Path:
    return cache_dir("weather") / "metno_weather_cache.json"


def constellations_path() -> Path:
    return DataLibrary.current(create=True).root / "terralab_constellations.json"


def data_source_catalog_path() -> Path:
    return DataLibrary.current(create=True).layout(create=True)[
        "data_source_catalog"
    ]


def ephemeris_path() -> Path | None:
    """Resolve DE421 explicitly without allowing Skyfield to download it."""

    library = DataLibrary.current(create=True)
    state = library.asset_state("solar_system_ephemeris")
    configured = str(state.get("path", "") or "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(library.layout(create=True)["data_ephemeris"] / "de421.bsp")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def runtime_layout() -> dict[str, Path]:
    return DataLibrary.current(create=True).layout(create=True)


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
