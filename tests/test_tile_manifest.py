from __future__ import annotations

import json

from TerraLab.data.tile_manifest import TileManifest, build_tile_identifier


def test_tile_manifest_region_and_adjacent(tmp_path):
    manifest_path = tmp_path / "tile_manifest.json"
    payload = {
        "version": 1,
        "tile_size_deg": 5.0,
        "general_tile": {
            "file": "tile_all.npz",
            "mag_limit": 8.0,
            "coverage": "full_sky",
            "star_count": 100,
        },
        "deep_tiles": [
            {
                "id": "tile_0355_-05",
                "file": "tile_0355_-05.npz",
                "ra_min": 355.0,
                "ra_max": 360.0,
                "dec_min": -5.0,
                "dec_max": 0.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 10,
            },
            {
                "id": "tile_0000_-05",
                "file": "tile_0000_-05.npz",
                "ra_min": 0.0,
                "ra_max": 5.0,
                "dec_min": -5.0,
                "dec_max": 0.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 12,
            },
            {
                "id": "tile_0000_+00",
                "file": "tile_0000_+00.npz",
                "ra_min": 0.0,
                "ra_max": 5.0,
                "dec_min": 0.0,
                "dec_max": 5.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 7,
            },
        ],
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = TileManifest()
    manifest.load(manifest_path)

    matching = manifest.get_tiles_for_region(ra_center=359.0, dec_center=-2.0, radius_deg=3.0)
    matching_ids = {entry.tile_id for entry in matching}
    assert "tile_0355_-05" in matching_ids
    assert "tile_0000_-05" in matching_ids

    neighbors = manifest.get_adjacent_tiles("tile_0000_-05")
    neighbor_ids = {entry.tile_id for entry in neighbors}
    assert "tile_0355_-05" in neighbor_ids
    assert "tile_0000_+00" in neighbor_ids


def test_tile_manifest_primary_tile_picks_nearest_match(tmp_path):
    manifest_path = tmp_path / "tile_manifest.json"
    payload = {
        "version": 1,
        "tile_size_deg": 5.0,
        "general_tile": {
            "file": "tile_all.npz",
            "mag_limit": 8.0,
            "coverage": "full_sky",
            "star_count": 100,
        },
        "deep_tiles": [
            {
                "id": "tile_0000_+00",
                "file": "tile_0000_+00.npz",
                "ra_min": 0.0,
                "ra_max": 5.0,
                "dec_min": 0.0,
                "dec_max": 5.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 10,
            },
            {
                "id": "tile_0005_+00",
                "file": "tile_0005_+00.npz",
                "ra_min": 5.0,
                "ra_max": 10.0,
                "dec_min": 0.0,
                "dec_max": 5.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 12,
            },
            {
                "id": "tile_0010_+00",
                "file": "tile_0010_+00.npz",
                "ra_min": 10.0,
                "ra_max": 15.0,
                "dec_min": 0.0,
                "dec_max": 5.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 8,
            },
        ],
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = TileManifest()
    manifest.load(manifest_path)

    primary = manifest.get_primary_tile_for_region(
        ra_center=8.8,
        dec_center=2.1,
        radius_deg=4.0,
    )

    assert primary is not None
    assert primary.tile_id == "tile_0005_+00"


def test_tile_manifest_preserves_zero_declination_tiles(tmp_path):
    manifest_path = tmp_path / "tile_manifest.json"
    payload = {
        "version": 1,
        "tile_size_deg": 5.0,
        "general_tile": {
            "file": "tile_all.npz",
            "mag_limit": 8.0,
            "coverage": "full_sky",
            "star_count": 100,
        },
        "deep_tiles": [
            {
                "id": "tile_0010_+00",
                "file": "tile_0010_+00.npz",
                "ra_min": 10.0,
                "ra_max": 15.0,
                "dec_min": 0.0,
                "dec_max": 5.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 10,
            },
            {
                "id": "tile_0010_+05",
                "file": "tile_0010_+05.npz",
                "ra_min": 10.0,
                "ra_max": 15.0,
                "dec_min": 5.0,
                "dec_max": 10.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 12,
            },
        ],
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = TileManifest()
    manifest.load(manifest_path)

    center_tile = next(
        tile for tile in manifest.deep_tiles if tile.tile_id == "tile_0010_+00"
    )
    assert center_tile.dec_min == 0.0
    assert center_tile.dec_max == 5.0

    neighbor_ids = {tile.tile_id for tile in manifest.get_adjacent_tiles("tile_0010_+00")}
    assert "tile_0010_+05" in neighbor_ids


def test_tile_identifier_format():
    assert build_tile_identifier(355.0, -5.0) == "tile_0355_-05"
    assert build_tile_identifier(0.0, 5.0) == "tile_0000_+05"
