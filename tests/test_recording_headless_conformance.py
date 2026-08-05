"""Phase 18 tests for RecordingRendererBackend, shared conformance suite, and headless CLI."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from TerraLab.core.rendering_contracts.contracts import (
    PresenterKind,
    RenderCapability,
)
from TerraLab.bootstrap.composition import create_backend_registry
from TerraLab.render.conformance import (
    BackendConformanceSuite,
    create_minimal_test_frame,
)
from TerraLab.render.recording.backend import RecordingRendererBackend

ROOT = Path(__file__).resolve().parents[1]


def test_recording_backend_conformance_suite() -> None:
    """Run shared conformance suite on RecordingRendererBackend."""
    suite = BackendConformanceSuite(RecordingRendererBackend)
    suite.test_all()


def test_recording_backend_manifest_determinism() -> None:
    """Verify that identical SceneFrame inputs produce identical manifests and hashes."""
    backend = RecordingRendererBackend()
    backend.start(None)  # type: ignore[arg-type]

    frame_a = create_minimal_test_frame(1)
    frame_b = create_minimal_test_frame(1)

    backend.submit(frame_a)
    manifest_a = backend.get_last_manifest()
    assert manifest_a is not None

    backend.submit(frame_b)
    manifest_b = backend.get_last_manifest()
    assert manifest_b is not None

    assert manifest_a["manifest_hash"] == manifest_b["manifest_hash"]
    backend.close()


def test_recording_backend_registry() -> None:
    """Verify recording backend is registered in composition registry."""
    registry = create_backend_registry()
    assert "recording" in registry.available_backend_ids()

    backend = registry.create(
        "recording",
        presenter_kind=PresenterKind.RECORDING,
        required_capabilities=(RenderCapability.RECORDING,),
    )
    assert backend.backend_id == "recording"


def test_recording_backend_ast_no_qt_imports() -> None:
    """AST check verifying zero PyQt or Qt imports in recording backend and conformance suite."""
    for path in (
        ROOT / "TerraLab" / "render" / "recording" / "backend.py",
        ROOT / "TerraLab" / "render" / "recording" / "__init__.py",
        ROOT / "TerraLab" / "render" / "conformance.py",
        ROOT / "TerraLab" / "cli" / "render_scene.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(
                        ("PyQt", "PySide", "Qt")
                    ), f"Forbidden Qt import {alias.name!r} in {path}"
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith(("PyQt", "PySide", "Qt")), (
                    f"Forbidden Qt import from {module!r} in {path}"
                )


def test_render_scene_cli_subprocess_with_blocked_pyqt() -> None:
    """Execute render_scene CLI tool in subprocess with PyQt blocked."""
    cmd = [
        sys.executable,
        "-m",
        "TerraLab.cli.render_scene",
        "--scene",
        "minimal",
        "--block-pyqt",
    ]
    result = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["generation"] == 1
    assert "manifest_hash" in payload
