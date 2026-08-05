"""Phase 12: the executable renderer route has no legacy branch."""

from __future__ import annotations

from pathlib import Path

from TerraLab.bootstrap.composition import (
    build_render_route,
    create_backend_registry,
)
from TerraLab.core.rendering_contracts.contracts import RenderTargetKind
from TerraLab.runtime.supervisor import RuntimeSupervisor


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "TerraLab"


def test_selected_backends_have_direct_presenters_and_neutral_targets() -> (
    None
):
    qpainter = build_render_route(explicit_backend="qpainter")
    threejs = build_render_route(explicit_backend="threejs")

    assert qpainter.target_kind is RenderTargetKind.SHARED_RASTER
    assert threejs.target_kind is RenderTargetKind.HOSTED_SURFACE
    assert callable(qpainter.create_presenter)
    assert callable(threejs.create_presenter)
    assert "render" not in RuntimeSupervisor._AUXILIARY_MODULES


def test_registry_capabilities_are_the_concrete_backend_capabilities() -> None:
    registry = create_backend_registry()
    for backend_id in registry.available_backend_ids():
        registration = registry.registration_for(backend_id)
        backend = registration.factory()
        try:
            assert registration.capabilities == backend.capabilities
            assert registration.target_kinds == backend.target_kinds
        finally:
            backend.close()


def test_legacy_render_route_and_old_frame_transport_are_absent() -> None:
    removed = (
        "application/hosted_surface_controller.py",
        "application/ports/rendering.py",
        "infrastructure/runtime/render_service.py",
        "infrastructure/runtime/hosted_surface_service.py",
        "runtime/offscreen_renderer.py",
        "runtime/render_service.py",
        "runtime/hosted_surface_service.py",
        "render/threejs/diagnostic.py",
        "render/threejs/assets/diagnostic_runner.html",
    )
    assert not [
        (PACKAGE / relative).exists()
        for relative in removed
        if (PACKAGE / relative).exists()
    ]

    forbidden = (
        "OffscreenSceneRenderer",
        "encode_scene_frame_v1",
        "decode_scene_frame_v1",
        "application.ports.rendering",
        "diagnostic_runner",
        "base64",
        "b64encode",
        "b64decode",
    )
    sources = [
        path
        for path in PACKAGE.rglob("*")
        if path.suffix in {".py", ".js", ".html"}
        and "graphify-out" not in path.parts
        and path.name != "three.min.js"
    ]
    violations = [
        f"{path.relative_to(ROOT)}: {token}"
        for path in sources
        for token in forbidden
        if token in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not violations, "\n".join(violations)
