"""Shared renderer backend conformance suite.

Provides transport-neutral, framework-agnostic assertions for validating any
RendererBackend implementation (Recording, QPainter, Three.js, OpenGL, etc.).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from TerraLab.core.rendering_contracts.contracts import (
    CommandStreamOutput,
    PickRequest,
    PickResult,
    RasterFrameOutput,
    RenderBackendLifecycleError,
    RenderCapability,
    RenderFailure,
    RenderOutputPort,
    RendererBackend,
)
from TerraLab.scene.contracts import (
    CameraState,
    ConstellationState,
    EarthLayerState,
    EphemerisSnapshot,
    Layer,
    LayerState,
    LightPollutionMode,
    MeasurementState,
    MilkyWayState,
    Observer,
    PresentationState,
    ResourceVersions,
    SceneFrame,
    SceneTime,
    ScopeState,
    SelectionState,
    TerrainState,
    TrailState,
    Viewport,
    WeatherState,
)


class MockRenderOutputPort(RenderOutputPort):
    """Test output port recording frames, picks, and failures."""

    def __init__(self) -> None:
        self.frames: list[Any] = []
        self.picks: list[PickResult] = []
        self.failures: list[RenderFailure] = []

    def frame_ready(self, output: Any) -> None:
        self.frames.append(output)

    def pick_ready(self, result: PickResult) -> None:
        self.picks.append(result)

    def backend_failed(self, failure: RenderFailure) -> None:
        self.failures.append(failure)


def create_minimal_test_frame(generation: int = 1) -> SceneFrame:
    """Construct a minimal valid SceneFrame for conformance testing."""
    earth_vis = EarthLayerState(
        horizon_enabled=True,
        topography_enabled=False,
        surface_enabled=False,
        terrain_3d_enabled=False,
        light_pollution_enabled=False,
    )
    return SceneFrame(
        generation=generation,
        viewport=Viewport(width=800, height=600, device_pixel_ratio=1.0),
        time=SceneTime(ut_hour=12.0, day_of_year_utc=180, year_utc=2026),
        observer=Observer(latitude=41.38, longitude=2.17, altitude_m=100.0),
        camera=CameraState(
            azimuth=180.0, elevation=45.0, zoom=1.0, vertical_ratio=1.0
        ),
        layers=LayerState(
            order=(
                Layer.STARS,
                Layer.MILKYWAY,
                Layer.SUN_MOON,
                Layer.PLANETS,
                Layer.SOLAR_SYSTEM,
                Layer.TERRAIN,
                Layer.GRID,
                Layer.DEEP_SKY,
            )
        ),
        light_pollution_mode=LightPollutionMode.BORTLE,
        bortle=4,
        magnitude_limit=6.0,
        terrain=TerrainState(visibility=earth_vis),
        scope=ScopeState(),
        resources=ResourceVersions(),
        weather=WeatherState(),
        ephemeris=EphemerisSnapshot(),
        selection=SelectionState(),
        measurement=MeasurementState(),
        constellation=ConstellationState(),
        milkyway=MilkyWayState(),
        trails=TrailState(),
        presentation=PresentationState(),
        schema_version=1,
    )


class BackendConformanceSuite:
    """Executable assertions for backend lifecycle, frame, pick, and error contracts."""

    def __init__(self, backend_factory: Callable[[], RendererBackend]) -> None:
        self._factory = backend_factory

    def test_all(self) -> None:
        """Run the complete conformance assertion suite."""
        self.test_lifecycle()
        self.test_frame_submission()
        self.test_invalid_frames()
        self.test_picking()
        self.test_close_idempotency()

    def test_lifecycle(self) -> None:
        """Verify lifecycle state transitions and error rules."""
        backend = self._factory()
        port = MockRenderOutputPort()

        # Submit or pick before start must fail
        frame = create_minimal_test_frame(1)
        try:
            backend.submit(frame)
            raise AssertionError(
                "Expected RenderBackendLifecycleError on submit before start"
            )
        except RenderBackendLifecycleError:
            pass

        try:
            backend.request_pick(
                PickRequest(generation=1, request_id="p1", x=100.0, y=100.0)
            )
            raise AssertionError(
                "Expected RenderBackendLifecycleError on pick before start"
            )
        except RenderBackendLifecycleError:
            pass

        # Start backend
        backend.start(port)

        # Double start must fail
        try:
            backend.start(port)
            raise AssertionError(
                "Expected RenderBackendLifecycleError on double start"
            )
        except RenderBackendLifecycleError:
            pass

        # Close backend
        backend.close()

        # Submit or pick after close must fail
        try:
            backend.submit(frame)
            raise AssertionError(
                "Expected RenderBackendLifecycleError on submit after close"
            )
        except RenderBackendLifecycleError:
            pass

        try:
            backend.request_pick(
                PickRequest(generation=1, request_id="p2", x=100.0, y=100.0)
            )
            raise AssertionError(
                "Expected RenderBackendLifecycleError on pick after close"
            )
        except RenderBackendLifecycleError:
            pass

        # Start after close must fail
        try:
            backend.start(port)
            raise AssertionError(
                "Expected RenderBackendLifecycleError on restart after close"
            )
        except RenderBackendLifecycleError:
            pass

    def test_frame_submission(self) -> None:
        """Verify valid frame submission and output port notification."""
        backend = self._factory()
        port = MockRenderOutputPort()
        backend.start(port)

        frame = create_minimal_test_frame(1)
        backend.submit(frame)

        # QPainter legacy backend may not emit frame_ready (uses render_to_qpainter),
        # but Recording and async backends emit outputs via output_port.
        if backend.backend_id == "recording":
            assert len(port.frames) == 1
            out = port.frames[0]
            assert isinstance(out, (CommandStreamOutput, RasterFrameOutput))
            assert out.generation == 1

        backend.close()

    def test_invalid_frames(self) -> None:
        """Verify rejection of invalid schema or negative generation frames."""
        backend = self._factory()
        port = MockRenderOutputPort()
        backend.start(port)

        # Negative generation frame
        try:
            bad_gen_frame = create_minimal_test_frame(-1)
            backend.submit(bad_gen_frame)
            raise AssertionError(
                "Expected ValueError for negative frame generation"
            )
        except ValueError:
            pass

        backend.close()

    def test_picking(self) -> None:
        """Verify pick request and response behavior."""
        backend = self._factory()
        port = MockRenderOutputPort()
        backend.start(port)

        if RenderCapability.PICKING not in backend.capabilities:
            backend.close()
            return

        frame = create_minimal_test_frame(1)
        backend.submit(frame)

        req = PickRequest(
            generation=1,
            request_id="test-pick-1",
            x=400.0,
            y=300.0,
            purpose="select",
        )
        res = backend.request_pick(req)
        # Hosted renderers return through RenderOutputPort after their live
        # surface has observed the click.  They must not manufacture a
        # synchronous CPU response just to satisfy this generic suite.
        if res is None:
            assert not port.picks
            backend.close()
            return
        assert isinstance(res, PickResult)
        assert res.generation == 1
        assert res.request_id == "test-pick-1"
        assert "purpose" in res.payload

        backend.close()

    def test_close_idempotency(self) -> None:
        """Verify close can be called multiple times without raising errors."""
        backend = self._factory()
        port = MockRenderOutputPort()
        backend.start(port)
        backend.close()
        backend.close()  # Must not raise
