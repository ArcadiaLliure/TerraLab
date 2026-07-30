"""Headless scene rendering CLI tool producing deterministic manifests without PyQt."""

# ruff: noqa: E402 -- direct-script support must establish the project root first.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root to sys.path if executed directly as a script
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from TerraLab.application.ports.rendering import (
    CommandStreamTarget,
    RenderOutputPort,
)
from TerraLab.application.render_planning import SceneRenderPlanner
from TerraLab.render.recording.backend import RecordingRendererBackend
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
    thaw_json_mapping,
)


class NullOutputPort(RenderOutputPort):
    """Null output port for headless CLI execution."""

    def frame_ready(self, output: object) -> None:
        pass

    def pick_ready(self, result: object) -> None:
        pass

    def backend_failed(self, failure: object) -> None:
        pass


def build_scene_frame(
    scene_type: str = "minimal",
    bortle: int = 4,
    time_jd: float = 2460000.5,
) -> SceneFrame:
    """Build a deterministic SceneFrame for CLI testing."""
    full_layers = scene_type == "full"
    earth_vis = EarthLayerState(
        horizon_enabled=True,
        topography_enabled=full_layers,
        surface_enabled=full_layers,
        terrain_3d_enabled=full_layers,
        light_pollution_enabled=full_layers,
    )
    layers_order = (
        (
            Layer.STARS,
            Layer.MILKYWAY,
            Layer.SUN_MOON,
            Layer.PLANETS,
            Layer.SOLAR_SYSTEM,
            Layer.TERRAIN,
            Layer.GRID,
            Layer.DEEP_SKY,
        )
        if full_layers
        else (Layer.STARS, Layer.TERRAIN)
    )
    return SceneFrame(
        generation=1,
        viewport=Viewport(width=1920, height=1080, device_pixel_ratio=1.0),
        time=SceneTime(ut_hour=12.0, day_of_year_utc=180, year_utc=2026),
        observer=Observer(latitude=41.38, longitude=2.17, altitude_m=100.0),
        camera=CameraState(
            azimuth=180.0, elevation=45.0, zoom=1.0, vertical_ratio=1.0
        ),
        layers=LayerState(order=layers_order),
        light_pollution_mode=LightPollutionMode.BORTLE,
        bortle=bortle,
        magnitude_limit=6.5 if full_layers else 4.0,
        terrain=TerrainState(visibility=earth_vis),
        scope=ScopeState(),
        resources=ResourceVersions(),
        weather=WeatherState(),
        ephemeris=EphemerisSnapshot(),
        selection=SelectionState(),
        measurement=MeasurementState(),
        constellation=ConstellationState(),
        milkyway=MilkyWayState(enabled=full_layers),
        trails=TrailState(),
        presentation=PresentationState(),
        schema_version=1,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Headless TerraLab scene rendering CLI"
    )
    parser.add_argument(
        "--scene",
        choices=["minimal", "full"],
        default="minimal",
        help="Scene complexity variant",
    )
    parser.add_argument(
        "--bortle",
        type=int,
        default=4,
        help="Bortle light pollution class [1-9]",
    )
    parser.add_argument(
        "--out", type=str, default="", help="Optional JSON output file path"
    )
    parser.add_argument(
        "--block-pyqt",
        action="store_true",
        help="Block PyQt5 import before rendering",
    )

    args = parser.parse_args(argv)

    if args.block_pyqt:
        sys.modules["PyQt5"] = None  # type: ignore[assignment]
        sys.modules["PyQt5.QtCore"] = None  # type: ignore[assignment]
        sys.modules["PyQt5.QtGui"] = None  # type: ignore[assignment]
        sys.modules["PyQt5.QtWidgets"] = None  # type: ignore[assignment]

    frame = build_scene_frame(scene_type=args.scene, bortle=args.bortle)

    backend = RecordingRendererBackend()
    port = NullOutputPort()
    backend.start(port)
    plan = SceneRenderPlanner().build(frame)
    output = backend.render(plan, CommandStreamTarget("cli"))
    manifest = output.metadata
    backend.close()

    if manifest is None:
        sys.stderr.write("Error: No manifest produced by recording backend\n")
        return 1

    manifest_dict = thaw_json_mapping(manifest)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(manifest_dict, f, indent=2)
    else:
        print(json.dumps(manifest_dict, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
