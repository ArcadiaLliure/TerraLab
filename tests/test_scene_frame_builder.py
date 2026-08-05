"""Phase-03 contracts for the pure typed SceneFrame builder."""

from __future__ import annotations

import ast
import math
from pathlib import Path
import subprocess
import sys

import pytest

from TerraLab.application.commands import (
    LayerIntent,
    LightPollutionIntent,
    ModelSnapshots,
    PresentationIntent,
    ScopeIntent,
    TerrainIntent,
    UserViewState,
)
from TerraLab.application.scene_builder import SceneFrameBuilder
from TerraLab.scene.contracts import (
    CameraState,
    CatalogResource,
    ConstellationGroupState,
    ConstellationNodeState,
    ConstellationState,
    EphemerisSnapshot,
    MeasurementState,
    Observer,
    ResourceRef,
    ResourceVersions,
    SceneTime,
    ScopeSettings,
    SelectionStar,
    SelectionState,
    SurfaceVisualStyle,
    Viewport,
    WeatherState,
)


ROOT = Path(__file__).resolve().parents[1]


def _user_view(**overrides) -> UserViewState:
    values = {
        "time": SceneTime(22.5, 200, 2026),
        "observer": Observer(41.2, 0.8, 175.0),
        "camera": CameraState(185.0, 32.0, 1.6, 0.3),
        "layers": LayerIntent(
            stars_enabled=True,
            milkyway_enabled=True,
            solar_system_enabled=True,
            sun_moon_enabled=True,
            planets_enabled=True,
            grid_enabled=True,
            deep_sky_enabled=True,
        ),
        "terrain": TerrainIntent(
            horizon_enabled=True,
            topography_enabled=True,
            surface_enabled=True,
            terrain_3d_enabled=True,
            light_pollution_enabled=True,
            surface_visual_style=SurfaceVisualStyle.VIBRANT,
        ),
        "light_pollution": LightPollutionIntent(
            mode="magnitude",
            automatic_bortle=8.0,
            bortle_value=7.0,
            magnitude_limit=5.25,
        ),
        "scope": ScopeIntent(
            enabled=True,
            center_sky=(35.0, 185.0),
            fov_deg=(3.2, 2.1),
            settings=ScopeSettings(dataset_max_mag=10.5),
        ),
        "presentation": PresentationIntent(interaction_active=True),
    }
    values.update(overrides)
    return UserViewState(**values)


def _model() -> ModelSnapshots:
    return ModelSnapshots(
        ephemeris=EphemerisSnapshot(
            {
                "timestamp_utc": "2026-07-20T22:30:00+00:00",
                "sun": {"alt": -18.0, "az": 280.0},
                "moon": {"alt": 20.0, "az": 90.0},
                "planets": [],
            }
        ),
        weather=WeatherState(
            enabled=True,
            latitude=41.2,
            longitude=0.8,
            bortle=3,
            sample_artifact={"samples": [], "status": "clear"},
            sample_revision=4,
        ),
        selection=SelectionState(
            kind="sky",
            object_type="planet",
            key="jupiter barycenter",
            name="Jupiter",
            altitude=31.0,
            azimuth=205.0,
            star=SelectionStar(name="Jupiter"),
        ),
        measurement=MeasurementState(tool="distance", clear_revision=2),
        constellation=ConstellationState(
            data_path="C:/data/constellations.json",
            groups=(
                ConstellationGroupState(
                    "Summer",
                    (ConstellationNodeState(279.2, 38.8, "vega", "Vega"),),
                ),
            ),
            selected_segments=((0, 0),),
        ),
    )


def _resources() -> ResourceVersions:
    return ResourceVersions(
        catalog=CatalogResource(
            catalog_path="C:/artifacts/gaia.npy", version="g1"
        ),
        ngc=ResourceRef(path="C:/artifacts/ngc.npy", version="n3"),
        terrain_profile=ResourceRef(
            path="C:/artifacts/profile.npz", version="p2"
        ),
        terrain_surface=ResourceRef(
            path="C:/artifacts/surface.npz", version="s4"
        ),
        milkyway_texture=ResourceRef(path="C:/assets/milky.png", version="m1"),
        dust_map=ResourceRef(path="C:/assets/dust.npz", version="d1"),
    )


def test_builder_is_pure_and_resolves_layer_hierarchy_and_light_modes() -> (
    None
):
    frame = SceneFrameBuilder().build(
        _user_view(), _model(), _resources(), Viewport(320, 180), generation=8
    )

    assert [layer.value for layer in frame.layers.order] == [
        "stars",
        "milkyway",
        "sun_moon",
        "planets",
        "terrain",
        "grid",
        "deep_sky",
    ]
    assert frame.bortle == 6
    assert frame.magnitude_limit == pytest.approx(5.25)
    assert frame.weather.bortle == 6
    assert frame.terrain.visibility.surface_enabled is True

    disabled_earth = SceneFrameBuilder().build(
        _user_view(
            terrain=TerrainIntent(
                horizon_enabled=False,
                topography_enabled=True,
                surface_enabled=True,
                terrain_3d_enabled=True,
                light_pollution_enabled=True,
            )
        ),
        _model(),
        _resources(),
        Viewport(320, 180),
    )
    assert "terrain" not in [
        layer.value for layer in disabled_earth.layers.order
    ]
    assert disabled_earth.bortle == 1
    assert disabled_earth.magnitude_limit == pytest.approx(7.6)
    assert disabled_earth.terrain.visibility.surface_enabled is False


def test_builder_preserves_typed_schemas_and_resource_refs() -> None:
    resources = _resources()
    frame = SceneFrameBuilder().build(
        _user_view(), _model(), resources, Viewport(640, 360), generation=11
    )
    assert frame.selection.name == "Jupiter"
    assert frame.constellation.groups[0].nodes[0].star_name == "Vega"
    assert frame.measurement.clear_revision == 2
    assert frame.resources.terrain_profile.path == "C:/artifacts/profile.npz"
    assert frame.resources.catalog.catalog_path == "C:/artifacts/gaia.npy"
    assert frame.resources is resources
    assert frame.resources.terrain_profile is resources.terrain_profile
    assert "extras" not in frame.__dataclass_fields__


def test_contracts_reject_nonfinite_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        CameraState(0.0, 10.0, math.nan, 0.3)
    with pytest.raises(ValueError, match="finite"):
        EphemerisSnapshot({"sun": {"alt": math.inf}})


def test_builder_imports_without_qapplication() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from TerraLab.application.scene_builder import SceneFrameBuilder; "
            "import sys; "
            "assert not any(name.startswith('PyQt5') for name in sys.modules); "
            "print(SceneFrameBuilder.__name__)",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "SceneFrameBuilder"


def test_ui_adapter_does_not_restore_scene_decisions() -> None:
    source = (ROOT / "TerraLab" / "ui" / "astro_canvas.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "resolve_earth_layer_visibility",
        "normalize_light_pollution_mode",
        "resolve_bortle_class",
        "bortle_to_magnitude",
    }.isdisjoint(calls)


def test_typed_frame_boundary_does_not_deepcopy_resources() -> None:
    for relative in (
        "TerraLab/application/scene_builder.py",
        "TerraLab/scene/contracts.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "deepcopy" not in calls
