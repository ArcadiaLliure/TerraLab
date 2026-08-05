"""Executable coverage checks for the phase-01 MVC file-role manifest."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "architecture" / "mvc_file_roles.json"
VALID_ROLES = {
    "MODEL",
    "VIEW",
    "CONTROLLER",
    "OUTSIDE_MVC",
}
VALID_OUTSIDE_TECHNICAL_ROLES = {
    "BOOTSTRAP",
    "CLI",
    "DATA_ADAPTER",
    "DEV_TOOL",
    "ENTRYPOINT",
    "OBSERVABILITY",
    "PERSISTENCE_ADAPTER",
    "RUNTIME_ADAPTER",
}


def _product_paths() -> set[str]:
    return {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "TerraLab").rglob("*.py")
        if "__pycache__" not in path.parts
    }


def test_mvc_file_role_manifest_has_exact_product_coverage() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = payload["entries"]
    assert isinstance(entries, list)

    manifest_paths = [entry["path"] for entry in entries]
    duplicates = sorted(
        path for path, count in Counter(manifest_paths).items() if count > 1
    )
    assert duplicates == []
    assert set(manifest_paths) == _product_paths()

    for entry in entries:
        assert entry["mvc_role"] in VALID_ROLES
        assert entry["mvc_role"] != "UNKNOWN"
        assert isinstance(entry["technical_role"], str)
        assert entry["technical_role"]
        assert isinstance(entry["status"], str) and entry["status"]
        assert isinstance(entry["replaces"], list)
        assert isinstance(entry["rationale"], str) and entry["rationale"]
        if entry["mvc_role"] == "OUTSIDE_MVC":
            assert entry["technical_role"] in VALID_OUTSIDE_TECHNICAL_ROLES


def test_mvc_file_role_manifest_has_no_mixed_debt() -> None:
    """The manifest is an ownership assertion, not a historic debt counter."""

    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    counts = Counter(entry["mvc_role"] for entry in payload["entries"])
    assert counts["MIXED"] == 0
