"""Phase 22 hardening tests — legacy removal, manifest integrity, backend parity."""

from __future__ import annotations

import json
import pathlib

from TerraLab.core.rendering_contracts.contracts import (
    RenderCapability,
    RenderTargetKind,
)
from TerraLab.bootstrap.composition import build_render_backend, create_backend_registry
from TerraLab.render.recording.backend import RecordingRendererBackend
from TerraLab.render.threejs.backend import ThreeJSRendererBackend


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
MVC_ROLES_PATH = PROJECT_ROOT / "docs" / "architecture" / "mvc_file_roles.json"
TERRALAB_PKG = PROJECT_ROOT / "TerraLab"


def test_no_migration_flags_in_production_code() -> None:
    """No TERRALAB_SCENE_* migration flags exist in production .py files."""
    for py_file in TERRALAB_PKG.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8", errors="ignore")
        assert "TERRALAB_SCENE_" not in content, (
            f"Migration flag TERRALAB_SCENE_* found in {py_file.relative_to(PROJECT_ROOT)}"
        )


def test_mvc_manifest_has_zero_mixed_and_zero_unknown() -> None:
    """The MVC manifest has no MIXED or UNKNOWN entries at end of migration."""
    data = json.loads(MVC_ROLES_PATH.read_text(encoding="utf-8"))
    entries = data if isinstance(data, list) else data.get("files", data.get("entries", []))
    for entry in entries:
        role = entry.get("mvc_role", "")
        path = entry.get("path", "<unknown>")
        assert role != "MIXED", f"MIXED entry remains: {path}"
        assert role != "UNKNOWN", f"UNKNOWN entry remains: {path}"


def test_all_three_backends_registered_and_creatable() -> None:
    """QPainter, Recording, and Three.js are all registered in the backend registry."""
    registry = create_backend_registry()
    available = registry.available_backend_ids()
    assert "qpainter" in available
    assert "recording" in available
    assert "threejs" in available


def test_threejs_full_application_backend_selection() -> None:
    """Three.js can be selected as a full application backend."""
    backend = build_render_backend(
        explicit_backend="threejs",
        target_kind=RenderTargetKind.HOSTED_SURFACE,
    )
    assert isinstance(backend, ThreeJSRendererBackend)
    assert backend.backend_id == "threejs"


def test_recording_backend_selection() -> None:
    """Recording backend can be selected for headless conformance."""
    backend = build_render_backend(
        explicit_backend="recording",
        target_kind=RenderTargetKind.COMMAND_STREAM,
    )
    assert isinstance(backend, RecordingRendererBackend)
    assert backend.backend_id == "recording"


def test_render_backend_env_var_is_the_sole_stable_flag() -> None:
    """TERRALAB_RENDER_BACKEND is the only stable render configuration flag."""
    # This flag must be documented and functional
    backend = build_render_backend(
        environment={"TERRALAB_RENDER_BACKEND": "threejs"},
        target_kind=RenderTargetKind.HOSTED_SURFACE,
    )
    assert backend.backend_id == "threejs"


def test_threejs_declares_complete_capability_set() -> None:
    """Three.js backend declares all required capabilities for full parity."""
    backend = ThreeJSRendererBackend()
    required = {
        RenderCapability.HOSTED_SURFACE,
        RenderCapability.SKY_BACKGROUND,
        RenderCapability.STARS,
        RenderCapability.EPHEMERIS_BODIES,
        RenderCapability.DEEP_SKY,
        RenderCapability.GRID,
        RenderCapability.LABELS,
        RenderCapability.SCOPE,
        RenderCapability.CONSTELLATIONS,
        RenderCapability.PICKING,
        RenderCapability.INTERACTION,
        RenderCapability.MEASUREMENTS,
        RenderCapability.TERRAIN_GEOMETRY,
        RenderCapability.TERRAIN_MATERIALS,
    }
    assert required.issubset(backend.capabilities)
