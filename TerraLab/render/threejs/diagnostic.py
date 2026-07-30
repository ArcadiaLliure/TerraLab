"""Diagnostic scene generator for Phase 19 Three.js host validation."""

from __future__ import annotations

from typing import Any, Mapping

from TerraLab.render.threejs.protocol import (
    PRIMITIVE_CLIP_BLEND,
    PRIMITIVE_COLOR,
    PRIMITIVE_IMAGE,
    PRIMITIVE_LINE,
    PRIMITIVE_MESH,
    PRIMITIVE_POINT,
    PRIMITIVE_TEXT,
)
from TerraLab.scene.contracts import JSONValue, freeze_json_mapping


def build_diagnostic_primitive_manifest() -> Mapping[str, JSONValue]:
    """Return a complete manifest of diagnostic basic primitives to render on Three.js host."""

    color_prim = {
        "kind": PRIMITIVE_COLOR,
        "name": "clear_color",
        "rgba": [0.05, 0.05, 0.12, 1.0],
    }

    points_prim = {
        "kind": PRIMITIVE_POINT,
        "name": "diagnostic_stars",
        "count": 4,
        "positions": [
            -10.0, 10.0, 0.0,
            10.0, 10.0, 0.0,
            -10.0, -10.0, 0.0,
            10.0, -10.0, 0.0,
        ],
        "sizes": [4.0, 6.0, 8.0, 12.0],
        "colors": [
            1.0, 1.0, 1.0, 1.0,
            1.0, 0.8, 0.4, 1.0,
            0.4, 0.7, 1.0, 1.0,
            1.0, 0.3, 0.3, 1.0,
        ],
    }

    lines_prim = {
        "kind": PRIMITIVE_LINE,
        "name": "diagnostic_grid",
        "count": 2,
        "segments": [
            -20.0, 0.0, 0.0, 20.0, 0.0, 0.0,
            0.0, -20.0, 0.0, 0.0, 20.0, 0.0,
        ],
        "colors": [
            0.0, 1.0, 0.0, 0.8, 0.0, 1.0, 0.0, 0.8,
            1.0, 0.0, 0.0, 0.8, 1.0, 0.0, 0.0, 0.8,
        ],
        "width": 2.0,
    }

    mesh_prim = {
        "kind": PRIMITIVE_MESH,
        "name": "diagnostic_triangle",
        "vertices": [
            0.0, 5.0, 0.0,
            -5.0, -5.0, 0.0,
            5.0, -5.0, 0.0,
        ],
        "indices": [0, 1, 2],
        "colors": [
            1.0, 0.0, 0.0, 1.0,
            0.0, 1.0, 0.0, 1.0,
            0.0, 0.0, 1.0, 1.0,
        ],
    }

    image_prim = {
        "kind": PRIMITIVE_IMAGE,
        "name": "diagnostic_overlay_image",
        "resource_id": "test_texture_01",
        "bounds": [-15.0, -15.0, 10.0, 10.0],
        "opacity": 0.9,
    }

    text_prim = {
        "kind": PRIMITIVE_TEXT,
        "name": "diagnostic_label",
        "text": "Three.js Diagnostic Host Active",
        "position": [0.0, 15.0, 0.0],
        "font_size": 16,
        "color": [1.0, 1.0, 1.0, 1.0],
    }

    clip_blend_prim = {
        "kind": PRIMITIVE_CLIP_BLEND,
        "name": "diagnostic_blend_state",
        "blend_mode": "alpha",
        "clip_bounds": [0, 0, 1920, 1080],
    }

    manifest: dict[str, Any] = {
        "title": "Three.js Diagnostic Scene",
        "generation": 1,
        "primitives": [
            color_prim,
            points_prim,
            lines_prim,
            mesh_prim,
            image_prim,
            text_prim,
            clip_blend_prim,
        ],
    }
    return freeze_json_mapping(manifest)
