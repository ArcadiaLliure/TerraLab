from __future__ import annotations

import numpy as np

from TerraLab.terrain.engine import HorizonBaker


class _AlwaysOutOfCoverageProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get_elevation(self, _x: float, _y: float):
        self.calls += 1
        return None


class _SlopedProvider:
    def get_elevation(self, x: float, _y: float):
        return 100.0 + 0.01 * float(x)

    def get_nominal_resolution_m(self) -> float:
        return 5.0


def test_sample_single_azimuth_stops_after_coverage_exit() -> None:
    provider = _AlwaysOutOfCoverageProvider()
    baker = HorizonBaker(provider)

    band_defs = [{"id": "b0", "min": 0.0, "max": 150000.0}]
    bands = baker._build_band_buffers(1, band_defs)
    sin_az = np.array([0.0], dtype=np.float32)
    cos_az = np.array([1.0], dtype=np.float32)
    light_domes = np.zeros(1, dtype=np.float32)
    light_peak_distances = np.zeros(1, dtype=np.float32)
    max_rad_per_az = np.zeros(1, dtype=np.float32)

    baker._sample_single_azimuth(
        az_index=0,
        obs_x=0.0,
        obs_y=0.0,
        h_eye_abs=1000.0,
        step_m=50.0,
        d_max=150000.0,
        bands=bands,
        sin_az=sin_az,
        cos_az=cos_az,
        light_domes=light_domes,
        light_peak_distances=light_peak_distances,
        max_rad_per_az=max_rad_per_az,
        light_sampler=None,
    )

    assert provider.calls <= 12


def test_bake_stops_rays_when_dem_has_no_coverage() -> None:
    provider = _AlwaysOutOfCoverageProvider()
    baker = HorizonBaker(provider)

    azimuths, bands, light_domes, light_peak_distances = baker.bake(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=1000.0,
        step_m=50.0,
        d_max=150000.0,
        delta_az_deg=180.0,
        band_defs=[{"id": "b0", "min": 0.0, "max": 150000.0}],
        progress_callback=None,
        light_sampler=None,
        abort_check=None,
    )

    assert len(azimuths) == 2
    assert len(bands) == 1
    assert light_domes.shape == (2,)
    assert light_peak_distances.shape == (2,)
    assert provider.calls <= 30


def test_build_view_mesh_samples_dem_normals() -> None:
    baker = HorizonBaker(_SlopedProvider())

    mesh = baker.build_view_mesh(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=100.0,
        d_max=250.0,
        delta_az_deg=180.0,
    )

    assert mesh["altitudes"].shape == (
        len(mesh["distances"]),
        len(mesh["azimuths"]),
    )
    assert mesh["version"] == 3
    assert mesh["visible"].shape == mesh["altitudes"].shape
    assert mesh["visible"].dtype == bool
    assert bool(np.all(mesh["valid"]))
    assert np.mean(mesh["normal_x"]) < 0.0
    assert np.mean(mesh["normal_z"]) < 1.0


def test_compute_mesh_visibility_hides_occluded_valleys() -> None:
    altitudes = np.array(
        [
            [0.0, 0.0, 0.0],
            [3.0, 0.5, 3.0],
            [1.0, 1.0, 1.0],
            [4.0, 1.5, 4.0],
        ],
        dtype=np.float32,
    )
    valid = np.ones_like(altitudes, dtype=bool)

    visible = HorizonBaker._compute_mesh_visibility(
        altitudes, valid, margin_deg=0.02
    )

    assert bool(visible[1, 0]) is True
    assert bool(visible[2, 0]) is False
    assert bool(visible[2, 1]) is True
    assert bool(visible[3, 1]) is True
