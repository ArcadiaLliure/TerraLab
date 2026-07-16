"""Persistent typed catalogue and deterministic geospatial layer selection.

This module owns metadata and user selection only.  It deliberately does not
open rasters or depend on Rasterio, Qt, terrain workers, or render code.  Heavy
inspection and provider construction belong to their respective services.

``coverage`` is an optional WGS84 rectangle stored as
``(west, south, east, north)``.  A missing coverage means "unknown" and is
therefore considered applicable; actual providers can still reject nodata or
out-of-coverage samples and continue through the returned fallback chain.
``bounds`` uses the same four-number shape but remains in the source's native
CRS and is metadata only.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from TerraLab.common.app_paths import data_source_catalog_path
from TerraLab.terrain.representation import (
    TerrainRepresentationMode,
    normalize_terrain_representation_mode,
)

CATALOG_SCHEMA_VERSION = 1
LEGACY_PATHS_MIGRATION = "legacy_paths_v1"
_CATALOG_IO_LOCK = threading.RLock()


class _StableStringEnum(str, Enum):
    """String enum with stable config/JSON representation."""

    def __str__(self) -> str:
        return str(self.value)


class LayerType(_StableStringEnum):
    """Semantic type of a geospatial dataset."""

    ELEVATION = "elevation"
    SURFACE_CATEGORICAL = "surface_categorical"
    SURFACE_RGB = "surface_rgb"
    LIGHT_POLLUTION = "light_pollution"


class LayerRole(_StableStringEnum):
    """User-selectable role; both surface types share one selection."""

    ELEVATION = "elevation"
    SURFACE = "surface"
    LIGHT_POLLUTION = "light_pollution"


class SelectionMode(_StableStringEnum):
    """Whether a role follows the deterministic policy or a preferred source."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"


class SourceHealthStatus(_StableStringEnum):
    """Persistent inspection health for a typed geospatial source.

    ``UNVERIFIED`` is intentionally usable for catalogues migrated from older
    TerraLab versions.  New asynchronous imports use ``PENDING`` until their
    descriptor inspection succeeds, then transition to ``HEALTHY``.  A failed
    inspection is ``INVALID`` and can never become effective merely because
    its path exists.
    """

    UNVERIFIED = "unverified"
    PENDING = "pending"
    HEALTHY = "healthy"
    INVALID = "invalid"


Extent = tuple[float, float, float, float]
LegacyReader = Callable[..., Any] | Mapping[str, Any]


def _coerce_layer_type(value: LayerType | str) -> LayerType:
    if isinstance(value, LayerType):
        return value
    normalized = str(value or "").strip().lower()
    for member in LayerType:
        if normalized in {member.value, member.name.lower()}:
            return member
    raise ValueError(f"Unsupported layer type: {value!r}")


def _coerce_selection_mode(value: SelectionMode | str) -> SelectionMode:
    if isinstance(value, SelectionMode):
        return value
    normalized = str(value or "").strip().lower()
    for member in SelectionMode:
        if normalized in {member.value, member.name.lower()}:
            return member
    raise ValueError(f"Unsupported selection mode: {value!r}")


def _role_for(value: LayerRole | LayerType | str) -> LayerRole:
    if isinstance(value, LayerRole):
        return value
    if isinstance(value, LayerType):
        if value in {LayerType.SURFACE_RGB, LayerType.SURFACE_CATEGORICAL}:
            return LayerRole.SURFACE
        return LayerRole(value.value)

    normalized = str(value or "").strip().lower()
    if normalized == LayerRole.SURFACE.value:
        return LayerRole.SURFACE
    try:
        return _role_for(_coerce_layer_type(normalized))
    except ValueError as exc:
        raise ValueError(f"Unsupported layer role: {value!r}") from exc


def _source_matches_role(source: "DataSource", role: LayerRole) -> bool:
    if role is LayerRole.SURFACE:
        return source.layer_type in {
            LayerType.SURFACE_RGB,
            LayerType.SURFACE_CATEGORICAL,
        }
    return source.layer_type.value == role.value


def _normalise_extent(value: object, *, field_name: str) -> Extent | None:
    if value is None or value == "":
        return None
    raw = value
    if isinstance(raw, Mapping):
        if "bounds" in raw:
            raw = raw["bounds"]
        elif all(key in raw for key in ("left", "bottom", "right", "top")):
            raw = (
                raw["left"],
                raw["bottom"],
                raw["right"],
                raw["top"],
            )
        elif all(key in raw for key in ("west", "south", "east", "north")):
            raw = (
                raw["west"],
                raw["south"],
                raw["east"],
                raw["north"],
            )
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError(f"{field_name} must contain four coordinates")
    if len(raw) != 4:
        raise ValueError(f"{field_name} must contain four coordinates")
    extent = tuple(float(item) for item in raw)
    if not all(math.isfinite(item) for item in extent):
        raise ValueError(f"{field_name} coordinates must be finite")
    return extent  # type: ignore[return-value]


def _normalise_path_for_storage(path: str | os.PathLike[str]) -> str:
    raw = os.path.expandvars(os.path.expanduser(str(path or "").strip()))
    if not raw:
        raise ValueError("A data source path cannot be empty")
    return str(Path(raw).resolve(strict=False))


def _path_identity(path: str | os.PathLike[str]) -> str:
    """Canonical comparison key, including Windows case normalization."""

    stored = _normalise_path_for_storage(path)
    return os.path.normcase(os.path.normpath(stored))


def infer_data_format(path: str | os.PathLike[str]) -> str:
    """Infer a descriptive storage format without opening the dataset."""

    candidate = Path(str(path)).expanduser()
    if candidate.is_dir() or not candidate.suffix:
        return "raster_mosaic"
    suffix = candidate.suffix.lower()
    return {
        ".tif": "geotiff",
        ".tiff": "geotiff",
        ".asc": "esri_ascii_grid",
        ".txt": "text_raster",
        ".npy": "npy",
    }.get(suffix, suffix.lstrip(".") or "unknown")


_FINGERPRINT_RASTER_SUFFIXES = frozenset(
    {
        ".asc",
        ".img",
        ".jp2",
        ".jpeg",
        ".jpg",
        ".npy",
        ".png",
        ".tif",
        ".tiff",
        ".txt",
        ".vrt",
        ".webp",
    }
)
_FINGERPRINT_SIDECAR_SUFFIXES = (
    ".aux.xml",
    ".hdr",
    ".msk",
    ".ovr",
    ".prj",
    ".tfw",
    ".wld",
)


def _fingerprint_relevant_file(path: Path) -> bool:
    name = path.name.casefold()
    return path.suffix.casefold() in _FINGERPRINT_RASTER_SUFFIXES or name.endswith(
        _FINGERPRINT_SIDECAR_SUFFIXES
    )


def _append_stat_fingerprint(parts: list[str], label: str, path: Path) -> None:
    """Append a deterministic metadata record, tolerating concurrent edits."""

    try:
        stat = path.stat()
    except OSError:
        parts.extend((label, "missing"))
        return
    parts.extend(
        (
            label,
            str(int(stat.st_size)),
            str(int(stat.st_mtime_ns)),
        )
    )


def build_source_fingerprint(path: str | os.PathLike[str]) -> str:
    """Build a lightweight deterministic source-version fingerprint.

    Files use their size and nanosecond modification time.  Mosaics recursively
    include every supported raster and georeferencing sidecar by relative path,
    so replacing a tile invalidates provider/surface caches even when the root
    directory timestamp does not change.  Pixel payloads are never read.
    """

    start = time.perf_counter()
    identity = _path_identity(path)
    root = Path(identity)
    parts = [identity]
    try:
        is_directory = root.is_dir()
        exists = root.exists()
    except OSError:
        is_directory = False
        exists = False
    if not exists:
        parts.append("missing")
    elif not is_directory:
        parts.append("file")
        _append_stat_fingerprint(parts, root.name, root)
    else:
        parts.append("directory")
        try:
            children = [
                child
                for child in root.rglob("*")
                if child.is_file() and _fingerprint_relevant_file(child)
            ]
        except OSError:
            children = []
        relative_children = sorted(
            ((child.relative_to(root).as_posix(), child) for child in children),
            key=lambda item: (item[0].casefold(), item[0]),
        )
        if not relative_children:
            parts.append("empty")
        for relative_path, child in relative_children:
            _append_stat_fingerprint(parts, relative_path, child)
    fingerprint = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    if is_directory or len(parts) > 3:
        print(
            "[TEMPORAL][DataSourceFingerprint] "
            f"path={identity} entries={max(0, (len(parts) - 2) // 3)} "
            f"elapsed={time.perf_counter() - start:.3f}s"
        )
    return fingerprint


def stable_source_id(
    layer_type: LayerType | str, path: str | os.PathLike[str]
) -> str:
    """Return a deterministic identifier for path-based registration/migration."""

    kind = _coerce_layer_type(layer_type)
    digest = hashlib.sha256(
        f"{kind.value}\0{_path_identity(path)}".encode("utf-8")
    ).hexdigest()[:20]
    return f"{kind.value}-{digest}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


@dataclass(frozen=True)
class DataSource:
    """Persistent metadata for one logical geospatial dataset or mosaic."""

    id: str
    display_name: str
    layer_type: LayerType
    path: str
    format: str
    crs: str | None = None
    resolution_m: float | None = None
    bounds: Extent | None = None
    coverage: Extent | None = None
    priority: int = 0
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: str = ""
    attribution: str = ""
    license: str = ""
    fingerprint: str = ""
    fingerprint_auto: bool = True

    def __post_init__(self) -> None:
        source_id = str(self.id or "").strip()
        if not source_id:
            raise ValueError("A data source id cannot be empty")
        stored_path = _normalise_path_for_storage(self.path)
        display_name = str(self.display_name or "").strip()
        if not display_name:
            display_name = Path(stored_path).name or source_id
        data_format = str(self.format or "").strip().lower()
        if not data_format:
            data_format = infer_data_format(stored_path)

        resolution = self.resolution_m
        if resolution is not None:
            resolution = float(resolution)
            if not math.isfinite(resolution) or resolution <= 0.0:
                raise ValueError(
                    "resolution_m must be a positive finite value"
                )

        object.__setattr__(self, "id", source_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(
            self, "layer_type", _coerce_layer_type(self.layer_type)
        )
        object.__setattr__(self, "path", stored_path)
        object.__setattr__(self, "format", data_format)
        object.__setattr__(
            self, "crs", str(self.crs).strip() if self.crs else None
        )
        object.__setattr__(self, "resolution_m", resolution)
        object.__setattr__(
            self, "bounds", _normalise_extent(self.bounds, field_name="bounds")
        )
        object.__setattr__(
            self,
            "coverage",
            _normalise_extent(self.coverage, field_name="coverage"),
        )
        object.__setattr__(self, "priority", int(self.priority))
        metadata = dict(self.metadata or {})
        health_status = self._health_status_from_metadata(metadata)
        enabled = bool(self.enabled)
        if health_status in {
            SourceHealthStatus.PENDING,
            SourceHealthStatus.INVALID,
        }:
            enabled = False
        object.__setattr__(self, "enabled", enabled)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "provenance", str(self.provenance or ""))
        object.__setattr__(self, "attribution", str(self.attribution or ""))
        object.__setattr__(self, "license", str(self.license or ""))
        object.__setattr__(
            self,
            "fingerprint",
            str(self.fingerprint or build_source_fingerprint(stored_path)),
        )
        object.__setattr__(self, "fingerprint_auto", bool(self.fingerprint_auto))

    @staticmethod
    def _health_status_from_metadata(
        metadata: Mapping[str, Any],
    ) -> SourceHealthStatus:
        if str(metadata.get("inspection_error", "") or "").strip():
            return SourceHealthStatus.INVALID
        raw = str(metadata.get("inspection_status", "") or "").strip().lower()
        try:
            return SourceHealthStatus(raw)
        except ValueError:
            if metadata.get("inspection_version"):
                return SourceHealthStatus.HEALTHY
            return SourceHealthStatus.UNVERIFIED

    @property
    def source_id(self) -> str:
        """Compatibility alias emphasizing that ``id`` identifies a source."""

        return self.id

    @property
    def health_status(self) -> SourceHealthStatus:
        """Structured descriptor-inspection status for this source."""

        return self._health_status_from_metadata(self.metadata)

    @property
    def healthy(self) -> bool:
        """Whether source health permits compatibility/runtime use."""

        return self.health_status in {
            SourceHealthStatus.UNVERIFIED,
            SourceHealthStatus.HEALTHY,
        }

    @property
    def available(self) -> bool:
        """Whether a healthy-enough configured local path currently exists."""

        return self.healthy and Path(self.path).exists()

    def covers(self, lat: float, lon: float) -> bool:
        """Return whether a WGS84 point is within the declared coverage."""

        if self.coverage is None:
            return True
        latitude = float(lat)
        longitude = float(lon)
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            return False
        west, south, east, north = self.coverage
        if not south <= latitude <= north:
            return False
        if west <= east:
            return west <= longitude <= east
        # Antimeridian-wrapping coverage.
        return longitude >= west or longitude <= east

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "layer_type": self.layer_type.value,
            "path": self.path,
            "format": self.format,
            "crs": self.crs,
            "resolution_m": self.resolution_m,
            "bounds": _json_safe(self.bounds),
            "coverage": _json_safe(self.coverage),
            "priority": self.priority,
            "enabled": self.enabled,
            "metadata": _json_safe(self.metadata),
            "provenance": self.provenance,
            "attribution": self.attribution,
            "license": self.license,
            "fingerprint": self.fingerprint,
            "fingerprint_auto": self.fingerprint_auto,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DataSource":
        return cls(
            id=str(payload.get("id", payload.get("source_id", ""))),
            display_name=str(
                payload.get("display_name", payload.get("name", ""))
            ),
            layer_type=_coerce_layer_type(str(payload.get("layer_type", ""))),
            path=str(payload.get("path", "")),
            format=str(payload.get("format", payload.get("data_format", ""))),
            crs=payload.get("crs"),
            resolution_m=payload.get("resolution_m"),
            bounds=payload.get("bounds"),
            coverage=payload.get("coverage"),
            priority=int(payload.get("priority", 0) or 0),
            enabled=bool(payload.get("enabled", True)),
            metadata=dict(payload.get("metadata", {}) or {}),
            provenance=str(payload.get("provenance", "") or ""),
            attribution=str(payload.get("attribution", "") or ""),
            license=str(payload.get("license", "") or ""),
            fingerprint=str(payload.get("fingerprint", "") or ""),
            fingerprint_auto=bool(payload.get("fingerprint_auto", True)),
        )


@dataclass(frozen=True)
class LayerSelection:
    """Persistent preference for one user-facing layer role."""

    mode: SelectionMode = SelectionMode.AUTOMATIC
    source_id: str | None = None

    def __post_init__(self) -> None:
        mode = _coerce_selection_mode(self.mode)
        source_id = str(self.source_id or "").strip() or None
        if mode is SelectionMode.AUTOMATIC:
            source_id = None
        elif source_id is None:
            raise ValueError("Manual selection requires a source_id")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "source_id", source_id)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "source_id": self.source_id}

    @classmethod
    def from_dict(cls, payload: object) -> "LayerSelection":
        if not isinstance(payload, Mapping):
            return cls()
        try:
            return cls(
                mode=_coerce_selection_mode(
                    str(payload.get("mode", SelectionMode.AUTOMATIC.value))
                ),
                source_id=payload.get("source_id"),
            )
        except ValueError:
            return cls()


@dataclass(frozen=True)
class LayerSelectionResult:
    """Configured preference and effective ordered source chain for a point."""

    configured: DataSource | None
    effective: DataSource | None
    chain: tuple[DataSource, ...]
    reason: str
    mode: SelectionMode


class DataSourceRegistry:
    """Thread-safe, atomic, versioned persistent data-source registry."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        legacy_reader: LegacyReader | None = None,
    ) -> None:
        self._path = (
            Path(path or data_source_catalog_path())
            .expanduser()
            .resolve(strict=False)
        )
        self._legacy_reader: LegacyReader = (
            legacy_reader
            if legacy_reader is not None
            else self._default_legacy_reader
        )
        self._lock = threading.RLock()
        self._disk_signature: tuple[int, int] | None = None
        self._sources: dict[str, DataSource] = {}
        self._path_index: dict[tuple[LayerType, str], str] = {}
        self._selections: dict[LayerRole, LayerSelection] = {
            role: LayerSelection() for role in LayerRole
        }
        self._representation_mode = TerrainRepresentationMode.RELIEF
        self._migrations: set[str] = set()

        with _CATALOG_IO_LOCK:
            existed = self._path.exists()
            if existed:
                self._load()
            migrated = self._migrate_legacy_paths()
            if migrated or not existed:
                self.save()

    @classmethod
    def default(cls) -> "DataSourceRegistry":
        """Open the application-default persistent catalogue."""

        return cls(path=data_source_catalog_path())

    @staticmethod
    def _default_legacy_reader(key: str, default: Any = None) -> Any:
        from TerraLab.common.utils import get_config_value

        return get_config_value(key, default)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def schema_version(self) -> int:
        return CATALOG_SCHEMA_VERSION

    def _current_disk_signature(self) -> tuple[int, int] | None:
        try:
            stat = self._path.stat()
            return int(stat.st_mtime_ns), int(stat.st_size)
        except OSError:
            return None

    def _refresh_if_changed_locked(self) -> None:
        signature = self._current_disk_signature()
        if signature == self._disk_signature:
            return
        self._sources.clear()
        self._path_index.clear()
        self._selections = {role: LayerSelection() for role in LayerRole}
        self._representation_mode = TerrainRepresentationMode.RELIEF
        self._migrations.clear()
        if signature is not None:
            self._load_locked()
        else:
            self._disk_signature = None

    @property
    def representation_mode(self) -> TerrainRepresentationMode:
        with _CATALOG_IO_LOCK, self._lock:
            self._refresh_if_changed_locked()
            return self._representation_mode

    @representation_mode.setter
    def representation_mode(
        self, mode: TerrainRepresentationMode | str
    ) -> None:
        self.set_representation_mode(mode)

    def set_representation_mode(
        self, mode: TerrainRepresentationMode | str
    ) -> TerrainRepresentationMode:
        normalized = normalize_terrain_representation_mode(mode)
        with _CATALOG_IO_LOCK, self._lock:
            self._refresh_if_changed_locked()
            if self._representation_mode is normalized:
                return normalized
            self._representation_mode = normalized
            self._save_locked()
        return normalized

    def list_sources(
        self, layer_type: LayerType | str | None = None
    ) -> list[DataSource]:
        kind = (
            _coerce_layer_type(layer_type) if layer_type is not None else None
        )
        start = time.perf_counter()
        with _CATALOG_IO_LOCK, self._lock:
            self._refresh_if_changed_locked()
            if self._refresh_source_fingerprints_locked(layer_type=kind):
                self._save_locked()
            sources = list(self._sources.values())
        if kind is not None:
            sources = [
                source for source in sources if source.layer_type is kind
            ]
        if kind is not None:
            print(
                "[TEMPORAL][DataSourceRegistry] "
                f"list_sources layer={kind.value} count={len(sources)} "
                f"elapsed={time.perf_counter() - start:.3f}s"
            )
        return sorted(sources, key=lambda source: source.id)

    def get(self, source_id: str) -> DataSource | None:
        normalized_id = str(source_id or "").strip()
        with _CATALOG_IO_LOCK, self._lock:
            self._refresh_if_changed_locked()
            if self._refresh_source_fingerprints_locked(
                source_ids={normalized_id}
            ):
                self._save_locked()
            return self._sources.get(normalized_id)

    def register_path(
        self,
        path: str | os.PathLike[str],
        layer_type: LayerType | str,
        *,
        source_id: str | None = None,
        display_name: str | None = None,
        data_format: str | None = None,
        crs: str | None = None,
        resolution_m: float | None = None,
        bounds: Extent | Sequence[float] | Mapping[str, Any] | None = None,
        coverage: Extent | Sequence[float] | Mapping[str, Any] | None = None,
        priority: int = 0,
        enabled: bool = True,
        metadata: Mapping[str, Any] | None = None,
        provenance: str = "",
        attribution: str = "",
        license: str = "",
        fingerprint: str | None = None,
    ) -> DataSource:
        kind = _coerce_layer_type(layer_type)
        stored_path = _normalise_path_for_storage(path)
        source = DataSource(
            id=source_id or stable_source_id(kind, stored_path),
            display_name=display_name or Path(stored_path).name,
            layer_type=kind,
            path=stored_path,
            format=data_format or infer_data_format(stored_path),
            crs=crs,
            resolution_m=resolution_m,
            bounds=bounds,  # type: ignore[arg-type]
            coverage=coverage,  # type: ignore[arg-type]
            priority=priority,
            enabled=enabled,
            metadata=dict(metadata or {}),
            provenance=provenance,
            attribution=attribution,
            license=license,
            fingerprint=fingerprint or build_source_fingerprint(stored_path),
            fingerprint_auto=fingerprint is None,
        )
        with _CATALOG_IO_LOCK, self._lock:
            self._refresh_if_changed_locked()
            registered, changed = self._register_locked(source)
            if changed:
                self._save_locked()
            return registered

    def update(self, source_id: str, **changes: Any) -> DataSource:
        normalized_id = str(source_id or "").strip()
        if "id" in changes or "source_id" in changes:
            raise ValueError(
                "A data source id is stable and cannot be changed"
            )
        with _CATALOG_IO_LOCK, self._lock:
            self._refresh_if_changed_locked()
            current = self._sources.get(normalized_id)
            if current is None:
                raise KeyError(f"Unknown data source: {normalized_id}")

            if "layer_type" in changes:
                changes["layer_type"] = _coerce_layer_type(
                    changes["layer_type"]
                )
            if "path" in changes:
                changes["path"] = _normalise_path_for_storage(changes["path"])
                if "fingerprint" not in changes:
                    changes["fingerprint"] = build_source_fingerprint(
                        changes["path"]
                    )
                    changes["fingerprint_auto"] = True
            if "fingerprint" in changes and "fingerprint_auto" not in changes:
                changes["fingerprint_auto"] = False
            if "data_format" in changes:
                if "format" in changes:
                    raise ValueError("Specify only format or data_format")
                changes["format"] = changes.pop("data_format")
            if "metadata" in changes:
                changes["metadata"] = dict(changes["metadata"] or {})

            updated = replace(current, **changes)
            duplicate_id = self._path_index.get(
                (updated.layer_type, _path_identity(updated.path))
            )
            if duplicate_id is not None and duplicate_id != normalized_id:
                raise ValueError(
                    "Another source already uses this layer type and path: "
                    f"{duplicate_id}"
                )

            self._remove_indexes_locked(current)
            self._sources[normalized_id] = updated
            self._add_indexes_locked(updated)
            for role, selection in tuple(self._selections.items()):
                if (
                    selection.source_id == normalized_id
                    and not _source_matches_role(updated, role)
                ):
                    self._selections[role] = LayerSelection()
            self._save_locked()
            return updated

    def remove(self, source_id: str) -> DataSource | None:
        normalized_id = str(source_id or "").strip()
        with _CATALOG_IO_LOCK, self._lock:
            self._refresh_if_changed_locked()
            source = self._sources.pop(normalized_id, None)
            if source is None:
                return None
            self._remove_indexes_locked(source)
            for role, selection in tuple(self._selections.items()):
                if selection.source_id == normalized_id:
                    self._selections[role] = LayerSelection()
            self._save_locked()
            return source

    def get_selection(
        self, role: LayerRole | LayerType | str
    ) -> LayerSelection:
        normalized_role = _role_for(role)
        with _CATALOG_IO_LOCK, self._lock:
            self._refresh_if_changed_locked()
            return self._selections[normalized_role]

    def set_selection(
        self,
        role: LayerRole | LayerType | str,
        source_id: str | None = None,
        *,
        mode: SelectionMode | str = SelectionMode.MANUAL,
    ) -> LayerSelection:
        normalized_role = _role_for(role)
        normalized_mode = _coerce_selection_mode(mode)
        normalized_id = str(source_id or "").strip() or None
        with _CATALOG_IO_LOCK, self._lock:
            self._refresh_if_changed_locked()
            if normalized_mode is SelectionMode.MANUAL:
                if normalized_id is None:
                    raise ValueError("Manual selection requires a source_id")
                source = self._sources.get(normalized_id)
                if source is None:
                    raise KeyError(f"Unknown data source: {normalized_id}")
                if not _source_matches_role(source, normalized_role):
                    raise ValueError(
                        f"Source {normalized_id} does not match role "
                        f"{normalized_role.value}"
                    )
            selection = LayerSelection(normalized_mode, normalized_id)
            if self._selections[normalized_role] == selection:
                return selection
            self._selections[normalized_role] = selection
            self._save_locked()
        return selection

    def set_automatic(
        self, role: LayerRole | LayerType | str
    ) -> LayerSelection:
        return self.set_selection(role, mode=SelectionMode.AUTOMATIC)

    def save(self) -> None:
        with _CATALOG_IO_LOCK, self._lock:
            self._refresh_if_changed_locked()
            self._save_locked()

    def reload(self) -> None:
        start = time.perf_counter()
        with _CATALOG_IO_LOCK, self._lock:
            self._sources.clear()
            self._path_index.clear()
            self._selections = {role: LayerSelection() for role in LayerRole}
            self._representation_mode = TerrainRepresentationMode.RELIEF
            self._migrations.clear()
            if self._path.exists():
                self._load_locked()
            else:
                self._disk_signature = None
        print(
            "[TEMPORAL][DataSourceRegistry] "
            f"reload path={self._path} sources={len(self._sources)} "
            f"elapsed={time.perf_counter() - start:.3f}s"
        )

    def _register_locked(self, source: DataSource) -> tuple[DataSource, bool]:
        existing_by_id = self._sources.get(source.id)
        if existing_by_id is not None:
            if (
                existing_by_id.layer_type is source.layer_type
                and _path_identity(existing_by_id.path)
                == _path_identity(source.path)
            ):
                return existing_by_id, False
            raise ValueError(f"Duplicate data source id: {source.id}")

        existing_id = self._path_index.get(
            (source.layer_type, _path_identity(source.path))
        )
        if existing_id is not None:
            return self._sources[existing_id], False

        self._sources[source.id] = source
        self._add_indexes_locked(source)
        return source, True

    def _add_indexes_locked(self, source: DataSource) -> None:
        self._path_index[(source.layer_type, _path_identity(source.path))] = (
            source.id
        )

    def _remove_indexes_locked(self, source: DataSource) -> None:
        self._path_index.pop(
            (source.layer_type, _path_identity(source.path)), None
        )

    def _refresh_source_fingerprints_locked(
        self,
        *,
        layer_type: LayerType | None = None,
        source_ids: set[str] | None = None,
    ) -> bool:
        """Refresh path-managed versions before selection/cache consumers read."""

        changed = False
        for source_id, source in tuple(self._sources.items()):
            if layer_type is not None and source.layer_type is not layer_type:
                continue
            if source_ids is not None and source_id not in source_ids:
                continue
            if not source.fingerprint_auto:
                continue
            current = build_source_fingerprint(source.path)
            if current == source.fingerprint:
                continue
            self._sources[source_id] = replace(source, fingerprint=current)
            changed = True
        return changed

    def _load(self) -> None:
        with _CATALOG_IO_LOCK, self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Cannot load data-source catalogue {self._path}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ValueError(
                "Data-source catalogue root must be a JSON object"
            )
        version = int(payload.get("version", 0) or 0)
        if version > CATALOG_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported data-source catalogue version: {version}"
            )

        self._sources.clear()
        self._path_index.clear()
        for raw_source in payload.get("sources", []) or []:
            if not isinstance(raw_source, Mapping):
                continue
            source = DataSource.from_dict(raw_source)
            self._register_locked(source)

        raw_selections = payload.get("selections", {}) or {}
        if not isinstance(raw_selections, Mapping):
            raw_selections = {}
        self._selections = {
            role: LayerSelection.from_dict(raw_selections.get(role.value))
            for role in LayerRole
        }
        self._representation_mode = normalize_terrain_representation_mode(
            payload.get(
                "terrain_representation_mode",
                TerrainRepresentationMode.RELIEF.value,
            )
        )
        raw_migrations = payload.get("migrations", {}) or {}
        if isinstance(raw_migrations, Mapping):
            self._migrations = {
                str(name)
                for name, complete in raw_migrations.items()
                if bool(complete)
            }
        else:
            self._migrations = set()
        self._disk_signature = self._current_disk_signature()

    def _save_locked(self) -> None:
        payload = {
            "version": CATALOG_SCHEMA_VERSION,
            "sources": [
                source.to_dict()
                for source in sorted(
                    self._sources.values(), key=lambda item: item.id
                )
            ],
            "selections": {
                role.value: self._selections[role].to_dict()
                for role in LayerRole
            },
            "terrain_representation_mode": self._representation_mode.value,
            "migrations": {name: True for name in sorted(self._migrations)},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self._path.parent),
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
            temp_path = None
            self._disk_signature = self._current_disk_signature()
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _read_legacy(self, key: str, default: Any = None) -> Any:
        reader = self._legacy_reader
        if isinstance(reader, Mapping):
            if key in reader:
                return reader.get(key, default)
            current: Any = reader
            for part in key.split("."):
                if not isinstance(current, Mapping) or part not in current:
                    return default
                current = current[part]
            return current
        try:
            return reader(key, default)
        except TypeError:
            try:
                value = reader(key)
            except Exception:
                return default
            return default if value is None else value
        except Exception:
            return default

    @staticmethod
    def _legacy_path_values(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, Mapping) and "path" in value:
            return DataSourceRegistry._legacy_path_values(value.get("path"))
        if isinstance(value, (list, tuple, set, frozenset)):
            paths: list[str] = []
            for item in value:
                paths.extend(DataSourceRegistry._legacy_path_values(item))
            return paths
        text = str(value or "").strip()
        return [text] if text else []

    def _migrate_legacy_paths(self) -> bool:
        with self._lock:
            if LEGACY_PATHS_MIGRATION in self._migrations:
                return False

            legacy_fields = (
                ("raster_path", LayerType.ELEVATION, "Legacy elevation"),
                (
                    "assets.elevation_dem.path",
                    LayerType.ELEVATION,
                    "Imported elevation",
                ),
                (
                    "dvnl_path",
                    LayerType.LIGHT_POLLUTION,
                    "Legacy light pollution",
                ),
                (
                    "assets.light_pollution.path",
                    LayerType.LIGHT_POLLUTION,
                    "Imported light pollution",
                ),
            )
            for key, layer_type, fallback_name in legacy_fields:
                for raw_path in self._legacy_path_values(
                    self._read_legacy(key, None)
                ):
                    stored_path = _normalise_path_for_storage(raw_path)
                    path_key = (layer_type, _path_identity(stored_path))
                    existing_id = self._path_index.get(path_key)
                    if existing_id is not None:
                        existing = self._sources[existing_id]
                        metadata = dict(existing.metadata)
                        keys = list(metadata.get("legacy_config_keys", []))
                        if key not in keys:
                            keys.append(key)
                            metadata["legacy_config_keys"] = keys
                            updated = replace(existing, metadata=metadata)
                            self._sources[existing_id] = updated
                        continue

                    display_name = Path(stored_path).name or fallback_name
                    source = DataSource(
                        id=stable_source_id(layer_type, stored_path),
                        display_name=display_name,
                        layer_type=layer_type,
                        path=stored_path,
                        format=infer_data_format(stored_path),
                        metadata={
                            "migrated_from_legacy": True,
                            "legacy_config_keys": [key],
                        },
                        provenance="legacy_config",
                    )
                    self._register_locked(source)

            # Mark even an empty migration so legacy config is read only once.
            self._migrations.add(LEGACY_PATHS_MIGRATION)
            return True


class LayerSelectionService:
    """Resolve deterministic applicable source chains for observer positions."""

    def __init__(self, registry: DataSourceRegistry) -> None:
        self.registry = registry

    def select_elevation(self, lat: float, lon: float) -> LayerSelectionResult:
        return self._select(
            LayerRole.ELEVATION,
            (LayerType.ELEVATION,),
            lat,
            lon,
        )

    def select_surface(self, lat: float, lon: float) -> LayerSelectionResult:
        return self._select(
            LayerRole.SURFACE,
            (LayerType.SURFACE_RGB, LayerType.SURFACE_CATEGORICAL),
            lat,
            lon,
        )

    def select_light_pollution(
        self, lat: float, lon: float
    ) -> LayerSelectionResult:
        return self._select(
            LayerRole.LIGHT_POLLUTION,
            (LayerType.LIGHT_POLLUTION,),
            lat,
            lon,
        )

    @staticmethod
    def _sort_key(source: DataSource) -> tuple[int, int, float, str]:
        surface_rank = {
            LayerType.SURFACE_RGB: 0,
            LayerType.SURFACE_CATEGORICAL: 1,
        }.get(source.layer_type, 0)
        resolution = (
            float(source.resolution_m)
            if source.resolution_m is not None
            else math.inf
        )
        return surface_rank, -int(source.priority), resolution, source.id

    def _select(
        self,
        role: LayerRole,
        layer_types: tuple[LayerType, ...],
        lat: float,
        lon: float,
    ) -> LayerSelectionResult:
        selection = self.registry.get_selection(role)
        all_sources: list[DataSource] = []
        for layer_type in layer_types:
            all_sources.extend(self.registry.list_sources(layer_type))
        candidates = sorted(
            (
                source
                for source in all_sources
                if source.enabled
                and source.available
                and source.covers(lat, lon)
            ),
            key=self._sort_key,
        )

        configured = (
            self.registry.get(selection.source_id)
            if selection.source_id is not None
            else None
        )
        reason = "automatic"
        if selection.mode is SelectionMode.MANUAL:
            issue = self._manual_issue(configured, role, lat, lon)
            if issue is None:
                assert configured is not None
                candidates = [
                    configured,
                    *(
                        source
                        for source in candidates
                        if source.id != configured.id
                    ),
                ]
                reason = "manual"
            else:
                reason = issue
                if candidates:
                    reason = f"{reason}_fallback"
                else:
                    reason = f"{reason}_no_fallback"

        chain = tuple(candidates)
        effective = chain[0] if chain else None
        if selection.mode is SelectionMode.AUTOMATIC and effective is None:
            reason = "no_source_available"
        return LayerSelectionResult(
            configured=configured,
            effective=effective,
            chain=chain,
            reason=reason,
            mode=selection.mode,
        )

    @staticmethod
    def _manual_issue(
        configured: DataSource | None,
        role: LayerRole,
        lat: float,
        lon: float,
    ) -> str | None:
        if configured is None:
            return "manual_source_missing"
        if not _source_matches_role(configured, role):
            return "manual_type_mismatch"
        if configured.health_status is SourceHealthStatus.PENDING:
            return "manual_pending"
        if configured.health_status is SourceHealthStatus.INVALID:
            return "manual_invalid"
        if not configured.enabled:
            return "manual_disabled"
        if not configured.available:
            return "manual_unavailable"
        if not configured.covers(lat, lon):
            return "manual_out_of_coverage"
        return None


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "LEGACY_PATHS_MIGRATION",
    "DataSource",
    "DataSourceRegistry",
    "LayerRole",
    "LayerSelection",
    "LayerSelectionResult",
    "LayerSelectionService",
    "LayerType",
    "SelectionMode",
    "SourceHealthStatus",
    "TerrainRepresentationMode",
    "build_source_fingerprint",
    "infer_data_format",
    "stable_source_id",
]
