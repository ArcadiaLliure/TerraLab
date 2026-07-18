"""User-selected TerraLab data library and migration helpers.

Application preferences remain in the platform state directory.  Scientific
datasets, generated indexes, caches, downloads and their catalogue live below
the library root selected by the user.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


APP_NAME = "TerraLab"
DATA_ROOT_ENV = "TERRALAB_DATA_ROOT"
MANIFEST_NAME = "terralab-data.json"
POINTER_NAME = "data_location.json"
MANIFEST_SCHEMA_VERSION = 1
POINTER_SCHEMA_VERSION = 1

_JSON_LOCK = threading.RLock()


class DataLibraryError(RuntimeError):
    """Base error raised while resolving or updating the data library."""


class DataLibraryNotConfigured(DataLibraryError):
    """Raised when a caller requires a user-selected library but none exists."""


class DataLibraryMigrationCancelled(DataLibraryError):
    """Raised when a data-library migration is cancelled by the caller."""


def platform_state_base() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata)
    if sys.platform.startswith("darwin"):
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".local" / "share"


def application_state_root() -> Path:
    return platform_state_base() / APP_NAME


def data_location_pointer_path() -> Path:
    return application_state_root() / "config" / POINTER_NAME


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    with _JSON_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise DataLibraryError(f"Cannot read TerraLab JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DataLibraryError(f"TerraLab JSON root must be an object: {path}")
    return payload


def configured_data_root() -> Path | None:
    """Resolve an explicitly configured data root without creating anything."""

    from_environment = str(os.getenv(DATA_ROOT_ENV, "") or "").strip()
    if from_environment:
        return Path(from_environment).expanduser().resolve(strict=False)

    pointer = data_location_pointer_path()
    if not pointer.exists():
        return None
    payload = _read_json(pointer)
    version = int(payload.get("schema_version", 0) or 0)
    if version > POINTER_SCHEMA_VERSION:
        raise DataLibraryError(f"Unsupported data-location schema: {version}")
    raw_root = str(payload.get("data_root", "") or "").strip()
    if not raw_root:
        raise DataLibraryError(f"Missing data_root in {pointer}")
    return Path(raw_root).expanduser().resolve(strict=False)


def _safe_relative(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class MigrationReport:
    source_root: Path
    destination_root: Path
    files_copied: int
    bytes_copied: int
    catalogue_rewritten: bool


class DataLibrary:
    """A validated, versioned data-library rooted at a user-controlled path."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.manifest_path = self.root / MANIFEST_NAME

    @classmethod
    def current(
        cls,
        *,
        require_configured: bool = False,
        create: bool = True,
    ) -> "DataLibrary":
        root = configured_data_root()
        if root is None:
            if require_configured:
                raise DataLibraryNotConfigured(
                    "TerraLab data library is not configured. Set "
                    f"{DATA_ROOT_ENV} or choose a folder in the application."
                )
            # Compatibility only.  The GUI uses require_configured=True before
            # constructing any data service, so it never silently adopts this.
            root = application_state_root()
        library = cls(root)
        if create:
            library.initialize(persist_pointer=False)
        return library

    @property
    def configured(self) -> bool:
        selected = configured_data_root()
        return selected is not None and selected == self.root

    def contains(self, path: str | os.PathLike[str]) -> bool:
        """Return whether ``path`` is managed below this library root."""

        return _safe_relative(Path(path), self.root) is not None

    def validate_root(self, *, require_writable: bool = True) -> None:
        if self.root.exists() and not self.root.is_dir():
            raise DataLibraryError(f"Data root is not a directory: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        if not require_writable:
            return
        probe: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=str(self.root), prefix=".terralab-write-", delete=False
            ) as handle:
                probe = Path(handle.name)
        except OSError as exc:
            raise DataLibraryError(f"Data root is not writable: {self.root}") from exc
        finally:
            if probe is not None:
                try:
                    probe.unlink(missing_ok=True)
                except OSError:
                    pass

    def default_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "data_root": str(self.root),
            "catalogs": {"geospatial": "config/data_sources.json"},
            "assets": {},
        }

    def load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return self.default_manifest()
        payload = _read_json(self.manifest_path)
        version = int(payload.get("schema_version", 0) or 0)
        if version > MANIFEST_SCHEMA_VERSION:
            raise DataLibraryError(f"Unsupported data-library schema: {version}")
        payload.setdefault("schema_version", MANIFEST_SCHEMA_VERSION)
        payload.setdefault("catalogs", {"geospatial": "config/data_sources.json"})
        payload.setdefault("assets", {})
        return payload

    def save_manifest(self, payload: Mapping[str, Any]) -> None:
        normalized = dict(payload)
        normalized["schema_version"] = MANIFEST_SCHEMA_VERSION
        normalized["data_root"] = str(self.root)
        normalized.setdefault("catalogs", {"geospatial": "config/data_sources.json"})
        normalized.setdefault("assets", {})
        _atomic_write_json(self.manifest_path, normalized)

    def initialize(self, *, persist_pointer: bool = False) -> dict[str, Path]:
        self.validate_root()
        layout = self.layout(create=True)
        manifest = self.load_manifest()
        if not self.manifest_path.exists() or str(manifest.get("data_root", "")) != str(self.root):
            self.save_manifest(manifest)
        if persist_pointer:
            self.persist_pointer()
        return layout

    def persist_pointer(self) -> None:
        self.validate_root()
        self.save_manifest(self.load_manifest())
        _atomic_write_json(
            data_location_pointer_path(),
            {
                "schema_version": POINTER_SCHEMA_VERSION,
                "data_root": str(self.root),
            },
        )

    def layout(self, *, create: bool = True) -> dict[str, Path]:
        root = self.root
        paths = {
            "root": root,
            "data_root": root,
            "state_root": application_state_root(),
            "config": root / "config",
            "data_source_catalog": root / "config" / "data_sources.json",
            "data_gaia": root / "data" / "sky" / "gaia",
            "data_ngc": root / "data" / "sky" / "ngc",
            "data_milkyway": root / "data" / "sky" / "milky-way",
            "data_planck": root / "data" / "sky" / "planck",
            "data_ephemeris": root / "data" / "sky" / "solar-system",
            "data_elevation": root / "data" / "earth" / "elevation",
            "data_surface": root / "data" / "earth" / "surface",
            "data_light_pollution": root / "data" / "earth" / "light-pollution",
            "cache_weather": root / "cache" / "weather",
            "cache_terrain": root / "cache" / "terrain",
            "cache_stars": root / "cache" / "stars",
            "downloads": root / "downloads",
            "downloads_partial": root / "downloads" / ".partial",
            "logs": root / "logs",
            "tmp": root / "tmp",
        }
        if create:
            for key, path in paths.items():
                if key in {"data_source_catalog"}:
                    path.parent.mkdir(parents=True, exist_ok=True)
                elif key not in {"root", "data_root", "state_root"}:
                    path.mkdir(parents=True, exist_ok=True)
        return paths

    def asset_state(self, asset_id: str) -> dict[str, Any]:
        assets = self.load_manifest().get("assets", {})
        if not isinstance(assets, Mapping):
            return {}
        value = assets.get(str(asset_id), {})
        return dict(value) if isinstance(value, Mapping) else {}

    def update_asset(self, asset_id: str, **changes: Any) -> dict[str, Any]:
        with _JSON_LOCK:
            manifest = self.load_manifest()
            assets = dict(manifest.get("assets", {}) or {})
            state = dict(assets.get(str(asset_id), {}) or {})
            state.update(changes)
            assets[str(asset_id)] = state
            manifest["assets"] = assets
            self.save_manifest(manifest)
        return state

    def legacy_managed_size(self, source_root: Path | None = None) -> int:
        source = Path(source_root or application_state_root())
        total = 0
        for path in self._legacy_files(source):
            try:
                total += int(path.stat().st_size)
            except OSError:
                continue
        return total

    @staticmethod
    def _legacy_files(source_root: Path) -> list[Path]:
        candidates: list[Path] = []
        for folder_name in ("data", "cache"):
            folder = source_root / folder_name
            if folder.exists():
                candidates.extend(path for path in folder.rglob("*") if path.is_file())
        for file_name in (
            "stars_catalog_healpy.npy",
            "terralab_metno_weather_cache.json",
        ):
            path = source_root / file_name
            if path.is_file():
                candidates.append(path)
        catalogue = source_root / "config" / "data_sources.json"
        if catalogue.is_file():
            candidates.append(catalogue)
        return candidates

    def _legacy_destination(self, source_root: Path, source: Path) -> Path:
        relative = source.relative_to(source_root)
        parts = relative.parts
        aliases = {
            "gaia": ("data", "sky", "gaia"),
            "ngc": ("data", "sky", "ngc"),
            "milkyway": ("data", "sky", "milky-way"),
            "planck": ("data", "sky", "planck"),
            "elevation": ("data", "earth", "elevation"),
            "surface": ("data", "earth", "surface"),
            "light_pollution": ("data", "earth", "light-pollution"),
        }
        if len(parts) >= 2 and parts[0] == "data" and parts[1] in aliases:
            return self.root.joinpath(*aliases[parts[1]], *parts[2:])
        if parts[:2] == ("config", "data_sources.json"):
            return self.root / "config" / "data_sources.json"
        if parts and parts[0] == "cache":
            return self.root.joinpath(*parts)
        if relative.name == "stars_catalog_healpy.npy":
            return self.root / "cache" / "stars" / relative.name
        if relative.name == "terralab_metno_weather_cache.json":
            return self.root / "cache" / "weather" / relative.name
        return self.root / "data" / "legacy" / relative

    def _rewrite_catalogue_paths(self, catalogue_path: Path, source_root: Path) -> bool:
        if not catalogue_path.exists():
            return False
        payload = _read_json(catalogue_path)
        changed = False
        sources = payload.get("sources", [])
        if not isinstance(sources, list):
            return False
        for source in sources:
            if not isinstance(source, dict):
                continue
            raw = str(source.get("path", "") or "").strip()
            if not raw:
                continue
            path = Path(raw).expanduser().resolve(strict=False)
            relative = _safe_relative(path, source_root)
            if relative is None:
                continue
            source["path"] = str(self._legacy_destination(source_root, path))
            changed = True
        if changed:
            _atomic_write_json(catalogue_path, payload)
        return changed

    def migrate_from_legacy(
        self,
        source_root: str | os.PathLike[str] | None = None,
        *,
        progress: Callable[[int, int, str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> MigrationReport:
        """Copy and verify managed legacy data, then persist the new pointer."""

        source = Path(source_root or application_state_root()).resolve(strict=False)
        self.initialize(persist_pointer=False)
        files = self._legacy_files(source)
        total_bytes = sum(
            int(path.stat().st_size) for path in files if path.exists()
        )
        available = int(shutil.disk_usage(self.root).free)
        if total_bytes > available:
            raise DataLibraryError(
                "Not enough free space for migration: "
                f"required {total_bytes} bytes, available {available} bytes."
            )
        copied_bytes = 0
        copied_files = 0
        for input_path in files:
            if cancelled is not None and cancelled():
                raise DataLibraryMigrationCancelled("Data-library migration cancelled")
            output_path = self._legacy_destination(source, input_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if output_path.exists() and output_path.stat().st_size == input_path.stat().st_size:
                    pass
                else:
                    temporary = output_path.with_name(f".{output_path.name}.migration-part")
                    shutil.copy2(input_path, temporary)
                    if temporary.stat().st_size != input_path.stat().st_size:
                        raise DataLibraryError(f"Migration verification failed: {input_path}")
                    os.replace(temporary, output_path)
                size = int(input_path.stat().st_size)
            except OSError as exc:
                raise DataLibraryError(f"Cannot migrate {input_path}: {exc}") from exc
            copied_files += 1
            copied_bytes += size
            if progress is not None:
                progress(copied_bytes, total_bytes, str(input_path))

        catalogue = self.root / "config" / "data_sources.json"
        rewritten = self._rewrite_catalogue_paths(catalogue, source)
        self.persist_pointer()
        return MigrationReport(
            source_root=source,
            destination_root=self.root,
            files_copied=copied_files,
            bytes_copied=copied_bytes,
            catalogue_rewritten=rewritten,
        )

    def migrate_from_library(
        self,
        source_root: str | os.PathLike[str],
        *,
        progress: Callable[[int, int, str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> MigrationReport:
        """Copy an existing versioned library without touching external sources."""

        source = Path(source_root).expanduser().resolve(strict=False)
        if source == self.root:
            self.persist_pointer()
            return MigrationReport(source, self.root, 0, 0, False)
        if _safe_relative(self.root, source) is not None or _safe_relative(source, self.root) is not None:
            raise DataLibraryError(
                "The new library cannot be nested inside the current library."
            )
        files = [
            path
            for path in source.rglob("*")
            if path.is_file() and path.name != MANIFEST_NAME
        ]
        total_bytes = sum(int(path.stat().st_size) for path in files)
        self.initialize(persist_pointer=False)
        available = int(shutil.disk_usage(self.root).free)
        if total_bytes > available:
            raise DataLibraryError(
                "Not enough free space for migration: "
                f"required {total_bytes} bytes, available {available} bytes."
            )
        copied_bytes = 0
        copied_files = 0
        for input_path in files:
            if cancelled is not None and cancelled():
                raise DataLibraryMigrationCancelled("Data-library migration cancelled")
            relative = input_path.relative_to(source)
            output_path = self.root / relative
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if not (
                    output_path.exists()
                    and output_path.stat().st_size == input_path.stat().st_size
                ):
                    temporary = output_path.with_name(
                        f".{output_path.name}.migration-part"
                    )
                    shutil.copy2(input_path, temporary)
                    if temporary.stat().st_size != input_path.stat().st_size:
                        raise DataLibraryError(
                            f"Migration verification failed: {input_path}"
                        )
                    os.replace(temporary, output_path)
                size = int(input_path.stat().st_size)
            except OSError as exc:
                raise DataLibraryError(f"Cannot migrate {input_path}: {exc}") from exc
            copied_files += 1
            copied_bytes += size
            if progress is not None:
                progress(copied_bytes, total_bytes, str(input_path))

        catalogue = self.root / "config" / "data_sources.json"
        rewritten = False
        if catalogue.exists():
            payload = _read_json(catalogue)
            sources = payload.get("sources", [])
            if isinstance(sources, list):
                for entry in sources:
                    if not isinstance(entry, dict):
                        continue
                    raw = str(entry.get("path", "") or "").strip()
                    if not raw:
                        continue
                    relative = _safe_relative(Path(raw), source)
                    if relative is None:
                        continue
                    entry["path"] = str(self.root / relative)
                    rewritten = True
            if rewritten:
                _atomic_write_json(catalogue, payload)
        old_manifest = source / MANIFEST_NAME
        manifest = _read_json(old_manifest) if old_manifest.exists() else self.default_manifest()
        assets = manifest.get("assets", {})
        if isinstance(assets, dict):
            for state in assets.values():
                if not isinstance(state, dict):
                    continue
                for field_name in ("path", "source_path"):
                    raw = str(state.get(field_name, "") or "").strip()
                    if not raw:
                        continue
                    relative = _safe_relative(Path(raw), source)
                    if relative is not None:
                        state[field_name] = str(self.root / relative)
        self.save_manifest(manifest)
        self.persist_pointer()
        return MigrationReport(
            source,
            self.root,
            copied_files,
            copied_bytes,
            rewritten,
        )

    @staticmethod
    def _remove_empty_parents(path: Path, *, stop: Path) -> None:
        current = path.resolve(strict=False)
        boundary = stop.resolve(strict=False)
        while current != boundary and _safe_relative(current, boundary) is not None:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def cleanup_legacy_source(
        self, source_root: str | os.PathLike[str] | None = None
    ) -> int:
        """Remove only legacy files copied by :meth:`migrate_from_legacy`.

        This is deliberately a separate, explicit operation so callers can ask
        for confirmation after a successful verified migration.
        """

        source = Path(source_root or application_state_root()).resolve(strict=False)
        removed = 0
        for path in self._legacy_files(source):
            resolved = path.resolve(strict=False)
            if _safe_relative(resolved, source) is None:
                continue
            try:
                resolved.unlink()
                removed += 1
                self._remove_empty_parents(resolved.parent, stop=source)
            except OSError as exc:
                raise DataLibraryError(f"Cannot remove migrated source {resolved}: {exc}") from exc
        return removed

    def cleanup_previous_library(self, source_root: str | os.PathLike[str]) -> int:
        """Remove verified managed content from a previous versioned library.

        Unknown sibling files are never touched.  A matching manifest is
        required to guard against treating an arbitrary directory as a library.
        """

        source = Path(source_root).expanduser().resolve(strict=False)
        manifest_path = source / MANIFEST_NAME
        if not manifest_path.is_file():
            raise DataLibraryError(f"Previous library manifest not found: {manifest_path}")
        manifest = _read_json(manifest_path)
        declared = Path(str(manifest.get("data_root", "") or "")).resolve(strict=False)
        if declared != source:
            raise DataLibraryError("Previous library manifest does not match its directory.")
        if source in {Path.home().resolve(strict=False), application_state_root().resolve(strict=False)}:
            raise DataLibraryError("Refusing to clean a protected broad directory.")

        owned_roots = tuple(
            source / name for name in ("config", "data", "cache", "downloads", "logs", "tmp")
        )
        removed = 0
        for owned_root in owned_roots:
            if not owned_root.exists():
                continue
            for path in sorted(
                (item for item in owned_root.rglob("*") if item.is_file()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    raise DataLibraryError(f"Cannot remove old library file {path}: {exc}") from exc
            for folder in sorted(
                (item for item in owned_root.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                try:
                    folder.rmdir()
                except OSError:
                    pass
            try:
                owned_root.rmdir()
            except OSError:
                pass
        try:
            manifest_path.unlink()
            removed += 1
        except OSError as exc:
            raise DataLibraryError(f"Cannot remove old library manifest {manifest_path}: {exc}") from exc
        return removed


__all__ = [
    "APP_NAME",
    "DATA_ROOT_ENV",
    "DataLibrary",
    "DataLibraryError",
    "DataLibraryMigrationCancelled",
    "DataLibraryNotConfigured",
    "MANIFEST_NAME",
    "MigrationReport",
    "application_state_root",
    "configured_data_root",
    "data_location_pointer_path",
    "platform_state_base",
]
