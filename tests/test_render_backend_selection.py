"""Phase-02 contracts, registry, and legacy-QPainter adapter tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import pytest

from TerraLab.core.rendering_contracts.contracts import (
    BackendNotFoundError,
    BackendRegistration,
    DuplicateBackendError,
    PickRequest,
    PresenterIncompatibleError,
    PresenterKind,
    RasterFrameHandle,
    RasterFrameOutput,
    RenderCapability,
    RenderBackendLifecycleError,
    SharedRasterTarget,
)
from TerraLab.application.render_planning import SceneRenderPlanner
from TerraLab.bootstrap.composition import build_render_backend
from TerraLab.bootstrap.settings import resolve_render_settings
from TerraLab.view.pyqt.backend import QPainterRendererBackend
from TerraLab.render.registry import BackendRegistry
from TerraLab.render.conformance import create_minimal_test_frame
from TerraLab.scene.contracts import CameraState
from TerraLab.__main__ import build_parser


class _Output:
    def __init__(self) -> None:
        self.frames: list[RasterFrameOutput] = []
        self.picks = []
        self.failures = []

    def frame_ready(self, output: RasterFrameOutput) -> None:
        self.frames.append(output)

    def pick_ready(self, result) -> None:
        self.picks.append(result)

    def backend_failed(self, failure) -> None:
        self.failures.append(failure)


def test_render_settings_follow_the_documented_precedence() -> None:
    assert resolve_render_settings().backend_id == "qpainter"
    assert (
        resolve_render_settings(user_backend="recording").backend_id
        == "recording"
    )
    assert (
        resolve_render_settings(
            environment={"TERRALAB_RENDER_BACKEND": "threejs"},
            user_backend="recording",
        ).backend_id
        == "threejs"
    )


def test_entrypoint_exposes_an_explicit_backend_override() -> None:
    arguments = build_parser().parse_args(["--render-backend", "qpainter"])
    assert arguments.render_backend == "qpainter"
    assert (
        resolve_render_settings(
            explicit_backend="qpainter",
            environment={"TERRALAB_RENDER_BACKEND": "threejs"},
            user_backend="recording",
        ).backend_id
        == "qpainter"
    )


def test_registry_rejects_unknown_duplicate_and_incompatible_backends() -> (
    None
):
    registry = BackendRegistry()
    registration = BackendRegistration(
        backend_id="test",
        factory=QPainterRendererBackend,
        capabilities=frozenset({RenderCapability.RASTER_SHARED_FRAME}),
        presenter_kinds=frozenset({PresenterKind.SHARED_FRAME}),
    )
    registry.register(registration)
    with pytest.raises(DuplicateBackendError, match="already registered"):
        registry.register(registration)
    with pytest.raises(BackendNotFoundError, match="Available backends: test"):
        registry.create("missing", presenter_kind=PresenterKind.SHARED_FRAME)
    with pytest.raises(PresenterIncompatibleError, match="shared_frame"):
        registry.create("test", presenter_kind=PresenterKind.HOSTED_SURFACE)
    with pytest.raises(PresenterIncompatibleError, match="lacks required"):
        registry.create(
            "test",
            presenter_kind=PresenterKind.SHARED_FRAME,
            required_capabilities=frozenset({RenderCapability.PICKING}),
        )


def test_scene_contract_is_frozen_and_typed() -> None:
    frame = create_minimal_test_frame(7)
    assert frame.generation == 7
    assert frame.layers.order
    with pytest.raises((AttributeError, TypeError)):
        frame.bortle = 2  # type: ignore[misc]
    with pytest.raises(ValueError, match="finite"):
        CameraState(0.0, 10.0, float("nan"), 0.3)


def test_contracts_import_without_site_packages_or_qt() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from TerraLab.core.rendering_contracts.contracts import RendererBackend; "
            "from TerraLab.scene.contracts import SceneFrame; "
            "print(RendererBackend.__name__, SceneFrame.__name__)",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if key != "PYTHONPATH"
        },
    )
    assert result.stdout.strip() == "RendererBackend SceneFrame"


def test_qpainter_backend_consumes_a_render_plan_without_a_legacy_bridge() -> (
    None
):
    backend = QPainterRendererBackend()
    output = _Output()
    frame = create_minimal_test_frame(3)
    plan = SceneRenderPlanner().build(frame)
    handle = RasterFrameHandle(
        slot=0,
        width=64,
        height=32,
        stride=256,
        pool_generation=1,
    )
    backend.start(output)
    try:
        metadata = backend.render(
            plan,
            SharedRasterTarget(
                handle, memoryview(bytearray(handle.stride * handle.height))
            ),
        ).metadata
        with pytest.raises(
            RenderBackendLifecycleError,
            match="SceneRenderController",
        ):
            backend.request_pick(
                PickRequest(generation=3, request_id="pick", x=4.0, y=5.0)
            )
    finally:
        backend.close()
    assert metadata["plan_native"] is True


def test_composition_selects_the_qpainter_default_without_importing_it_elsewhere() -> (
    None
):
    backend = build_render_backend()
    assert backend.backend_id == "qpainter"
    assert PresenterKind.SHARED_FRAME in backend.presenter_kinds
    assert RenderCapability.RASTER_SHARED_FRAME in backend.capabilities
    assert RenderCapability.SKY_BACKGROUND in backend.capabilities


def test_raster_output_is_discriminated_and_transport_neutral() -> None:
    frame = create_minimal_test_frame(5)
    output = RasterFrameOutput(
        generation=frame.generation,
        handle=RasterFrameHandle(
            slot=1,
            width=64,
            height=32,
            stride=256,
            pool_generation=2,
        ),
        render_ms=1.5,
        metadata={},
    )
    assert output.handle.slot == 1
