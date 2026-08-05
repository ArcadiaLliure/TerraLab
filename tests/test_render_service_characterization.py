"""Smoke test the phase-01 render-service characterization command."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "dev" / "characterize_render_service.py"


def test_render_service_characterization_captures_every_fixture(
    tmp_path: Path,
) -> None:
    output = tmp_path / "characterization"
    result = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--output-dir",
            str(output),
            "--repeats",
            "1",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    metadata_path = Path(result.stdout.strip())
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert payload["backend"].startswith("runtime/render_service")
    assert len(payload["scenes"]) == 11
    assert {scene["family"] for scene in payload["scenes"]} == {
        "day_twilight_night",
        "dense_scope",
        "solar_moon_planets_eclipse",
        "milky_way_ngc",
        "terrain_profile_relief",
        "surface_rgb_categorical",
        "selection_measurement_constellation",
    }
    for scene in payload["scenes"]:
        image = metadata_path.parent / scene["image"]["path"]
        assert image.is_file()
        assert scene["image"]["bytes"] > 0
        assert len(scene["image"]["sha256"]) == 64
        assert scene["snapshot_bytes"] > 0
        assert scene["metrics"]["frame_render_ms"]["p50"] >= 0.0
        assert scene["metrics"]["present_ms"]["p95"] >= 0.0
