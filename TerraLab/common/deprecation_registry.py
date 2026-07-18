"""Gestio del registre de metodes deprecated del projecte.

Aquest modul centralitza l'escriptura i consulta del fitxer JSON
"plans/deprecated_methods_registry.json".
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REGISTRY_VERSION = 1
VALID_STATUS = {
    "deprecated_pending_approval",
    "approved_for_removal",
    "removed",
}


@dataclass(frozen=True)
class DeprecatedMethodEntry:
    """Representa una entrada del registre de metodes deprecated."""

    entry_id: str
    module_path: str
    class_name: str | None
    method_name: str
    replacement: str
    phase_introduced: int
    status: str
    notes: str
    removed_in_phase: int | None = None

    def to_json(self) -> dict[str, Any]:
        """Retorna la representacio JSON canonic de l'entrada."""
        payload: dict[str, Any] = {
            "id": str(self.entry_id),
            "module_path": str(self.module_path),
            "class_name": self.class_name,
            "method_name": str(self.method_name),
            "replacement": str(self.replacement),
            "phase_introduced": int(self.phase_introduced),
            "status": str(self.status),
            "notes": str(self.notes),
        }
        if self.removed_in_phase is not None:
            payload["removed_in_phase"] = int(self.removed_in_phase)
        return payload


def registry_file_path() -> Path:
    """Retorna la ruta absoluta del fitxer de registre deprecations."""
    # TerraLab/common/deprecation_registry.py -> repo root at parents[2]
    return Path(__file__).resolve().parents[2] / "plans" / "deprecated_methods_registry.json"


def _read_registry() -> dict[str, Any]:
    path = registry_file_path()
    if not path.exists():
        return {"version": REGISTRY_VERSION, "entries": []}

    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise ValueError("El registre deprecated no te format dict.")

    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("El camp 'entries' ha de ser una llista.")

    version = int(data.get("version", REGISTRY_VERSION))
    return {"version": version, "entries": entries}


def _write_registry(payload: dict[str, Any]) -> None:
    path = registry_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)
    tmp_path.replace(path)


def ensure_registry_file() -> Path:
    """Assegura que el fitxer JSON existeix i te schema valid."""
    payload = _read_registry()
    payload["version"] = REGISTRY_VERSION
    payload.setdefault("entries", [])
    _write_registry(payload)
    return registry_file_path()


def register_deprecated_method(
    *,
    entry_id: str,
    module_path: str,
    class_name: str | None,
    method_name: str,
    replacement: str,
    phase_introduced: int,
    notes: str,
    status: str = "deprecated_pending_approval",
) -> None:
    """Insereix o actualitza una entrada deprecated al registre JSON."""
    if status not in VALID_STATUS:
        raise ValueError(f"Estat invalid per deprecated: {status}")

    payload = _read_registry()
    entries = list(payload.get("entries", []))

    canonical = DeprecatedMethodEntry(
        entry_id=str(entry_id),
        module_path=str(module_path),
        class_name=(None if class_name in (None, "") else str(class_name)),
        method_name=str(method_name),
        replacement=str(replacement),
        phase_introduced=int(phase_introduced),
        status=str(status),
        notes=str(notes),
    ).to_json()

    replaced = False
    for index, existing in enumerate(entries):
        if isinstance(existing, dict) and str(existing.get("id", "")) == canonical["id"]:
            # Preserve removed marker if ja estava eliminat.
            if "removed_in_phase" in existing and "removed_in_phase" not in canonical:
                canonical["removed_in_phase"] = int(existing["removed_in_phase"])
            entries[index] = canonical
            replaced = True
            break

    if not replaced:
        entries.append(canonical)

    # Ordenacio estable per facilitar revisions.
    entries.sort(key=lambda item: (str(item.get("module_path", "")), str(item.get("id", ""))))

    payload["version"] = REGISTRY_VERSION
    payload["entries"] = entries
    _write_registry(payload)


def mark_deprecated_removed(entry_id: str, *, removed_in_phase: int) -> None:
    """Marca una entrada com removed mantenint rastre de la fase."""
    payload = _read_registry()
    entries = list(payload.get("entries", []))
    found = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id", "")) != str(entry_id):
            continue
        entry["status"] = "removed"
        entry["removed_in_phase"] = int(removed_in_phase)
        found = True
        break

    if not found:
        raise KeyError(f"No s'ha trobat la entrada deprecated: {entry_id}")

    payload["entries"] = entries
    _write_registry(payload)


def emit_deprecation_warning(symbol_id: str, replacement: str) -> None:
    """Emet un DeprecationWarning estandard amb stacklevel correcte."""
    warnings.warn(
        (
            f"{symbol_id} esta deprecated i es mantindra fins aprovacio explicita. "
            f"Useu {replacement}."
        ),
        DeprecationWarning,
        stacklevel=2,
    )
