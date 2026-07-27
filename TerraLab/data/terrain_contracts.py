"""Stable terrain representation and geometry-source data contracts.

These values cross the UI/service boundary and are persisted in
``HorizonProfile`` files.  Keep their string values stable.
"""

from __future__ import annotations

from enum import Enum
from typing import TypeVar


class _StableStringEnum(str, Enum):
    """A string enum whose textual form is safe for CLI/config use."""

    def __str__(self) -> str:
        return str(self.value)


class TerrainRepresentationMode(_StableStringEnum):
    """Visual detail requested by the user for terrain rendering."""

    PROFILE = "profile"
    RELIEF = "relief"


class TerrainGeometrySource(_StableStringEnum):
    """Provenance of the geometry carried by a horizon profile."""

    REAL_ELEVATION = "real_elevation"
    FLAT_FALLBACK = "flat_fallback"
    PROCEDURAL_FALLBACK = "procedural_fallback"
    LEGACY_UNKNOWN = "legacy_unknown"


_EnumT = TypeVar("_EnumT", bound=_StableStringEnum)


def _normalize_enum(
    value: object, enum_type: type[_EnumT], default: _EnumT
) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    normalized = str(value or "").strip().lower()
    for member in enum_type:
        if normalized in {member.value, member.name.lower()}:
            return member
    return default


def normalize_terrain_representation_mode(
    value: object,
    *,
    default: TerrainRepresentationMode = TerrainRepresentationMode.RELIEF,
) -> TerrainRepresentationMode:
    """Return a valid representation mode with a legacy-safe default."""

    return _normalize_enum(value, TerrainRepresentationMode, default)


def normalize_terrain_geometry_source(
    value: object,
    *,
    default: TerrainGeometrySource = TerrainGeometrySource.LEGACY_UNKNOWN,
) -> TerrainGeometrySource:
    """Return a valid geometry provenance without inventing real data."""

    return _normalize_enum(value, TerrainGeometrySource, default)
