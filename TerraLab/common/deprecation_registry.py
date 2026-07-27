"""Read-only deprecation metadata and standard warnings.

The runtime never creates or mutates the registry. Architectural history is a
versioned documentation concern, so the JSON file lives under ``docs`` and is
validated by tests.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any


REGISTRY_VERSION = 1
VALID_STATUS = {
    "deprecated_pending_approval",
    "approved_for_removal",
    "removed",
}


def registry_file_path() -> Path:
    """Return the versioned, read-only deprecation registry path."""

    return Path(__file__).resolve().parents[2] / "docs" / "deprecations.json"


def read_registry() -> dict[str, Any]:
    """Load and minimally validate the static registry."""

    path = registry_file_path()
    with path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("The deprecation registry must be a JSON object.")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("The deprecation registry 'entries' field must be a list.")
    return payload


def emit_deprecation_warning(symbol_id: str, replacement: str) -> None:
    """Emit a standard warning for a temporary public compatibility route."""

    warnings.warn(
        f"{symbol_id} is deprecated; use {replacement}.",
        DeprecationWarning,
        stacklevel=2,
    )
