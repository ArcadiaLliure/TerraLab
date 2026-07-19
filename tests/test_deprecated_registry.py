from __future__ import annotations

import json

import pytest

from TerraLab.common.deprecation_registry import (
    VALID_STATUS,
    emit_deprecation_warning,
    registry_file_path,
)


def test_deprecated_registry_schema_and_uniqueness():
    path = registry_file_path()
    assert path.is_file(), "El registre deprecated ha d'existir"

    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    assert isinstance(payload, dict)
    assert int(payload.get("version", 0)) >= 1

    entries = payload.get("entries")
    assert isinstance(entries, list)

    seen_ids: set[str] = set()
    for entry in entries:
        assert isinstance(entry, dict)
        for key in (
            "id",
            "module_path",
            "method_name",
            "replacement",
            "phase_introduced",
            "status",
            "notes",
        ):
            assert key in entry, f"Falta clau obligatoria al registre: {key}"

        entry_id = str(entry["id"])
        assert entry_id not in seen_ids, f"ID duplicat al registre deprecated: {entry_id}"
        seen_ids.add(entry_id)

        status = str(entry["status"])
        assert status in VALID_STATUS


def test_emit_deprecation_warning():
    with pytest.warns(DeprecationWarning):
        emit_deprecation_warning(
            "TerraLab.sample.old_method",
            "TerraLab.sample.new_method",
        )
