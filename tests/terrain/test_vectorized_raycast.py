from __future__ import annotations

import math

import numpy as np

from TerraLab.terrain import bake_process
from TerraLab.terrain.providers import ElevationBatch
from TerraLab.terrain.raycast.baker import HorizonBaker


class _ScalarSyntheticProvider:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def _values(x, y):
        distance = np.hypot(x, y)
        values = 120.0 + 0.003 * x - 0.0015 * y + 8.0 * np.sin(distance / 80.0)
        valid = ~((distance >= 91.0) & (distance < 141.0))
        return values, valid

    def get_elevation(self, x: float, y: float):
        self.calls += 1
        values, valid = self._values(float(x), float(y))
        return float(np.float32(values)) if bool(valid) else None

    def get_nominal_resolution_m(self):
        return 5.0


class _BatchSyntheticProvider(_ScalarSyntheticProvider):
    def __init__(self) -> None:
        super().__init__()
        self.batch_calls = 0

    def sample_elevation(self, x, y):
        self.batch_calls += 1
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        values, valid = self._values(x, y)
        return ElevationBatch(values, valid)


def _band_definitions():
    return [
        {"id": "near", "min": 0.0, "max": 100.0},
        {"id": "far", "min": 100.0, "max": 2_000.0},
    ]


def test_vectorized_raycast_matches_scalar_with_nodata_exit():
    kwargs = dict(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=2_000.0,
        delta_az_deg=10.0,
        band_defs=_band_definitions(),
    )
    scalar = HorizonBaker(_ScalarSyntheticProvider()).bake_progressive(**kwargs)
    batch_provider = _BatchSyntheticProvider()
    vector = HorizonBaker(batch_provider).bake_progressive(**kwargs)

    np.testing.assert_array_equal(vector[0], scalar[0])
    np.testing.assert_array_equal(vector[4], scalar[4])
    for scalar_band, vector_band in zip(scalar[1], vector[1]):
        for key in (
            "angles",
            "dists",
            "heights",
            "surface_angles",
            "surface_dists",
            "surface_heights",
        ):
            np.testing.assert_allclose(
                vector_band[key],
                scalar_band[key],
                rtol=0.0,
                atol=1e-7 if "angle" in key or "dist" in key else 1e-5,
            )
    assert batch_provider.batch_calls <= math.ceil(len(vector[0]) / 64)


def test_vectorized_light_sampling_uses_batch_api_and_matches_scalar():
    class ScalarTerrain(_ScalarSyntheticProvider):
        @staticmethod
        def _values(x, y):
            values, _valid = _ScalarSyntheticProvider._values(x, y)
            return values, np.ones(np.asarray(values).shape, dtype=bool)

    class BatchTerrain(ScalarTerrain):
        def __init__(self):
            super().__init__()
            self.batch_calls = 0

        def sample_elevation(self, x, y):
            self.batch_calls += 1
            values, valid = self._values(
                np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
            )
            return ElevationBatch(values, valid)

    class LightSampler:
        def __init__(self):
            self.scalar_calls = 0
            self.batch_calls = 0

        @staticmethod
        def _value(x, y):
            return np.full(np.broadcast(np.asarray(x), np.asarray(y)).shape, 3.5)

        def get_radiance_terrain_xy(self, x, y, input_crs=None):
            self.scalar_calls += 1
            return float(np.float32(self._value(x, y)))

        def get_radiance_terrain_xy_batch(self, x, y, input_crs=None):
            self.batch_calls += 1
            return self._value(x, y).astype(np.float32)

    kwargs = dict(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=5_000.0,
        delta_az_deg=10.0,
        band_defs=_band_definitions(),
    )
    scalar_light = LightSampler()
    scalar = HorizonBaker(ScalarTerrain()).bake_progressive(
        **kwargs, light_sampler=scalar_light
    )
    batch_light = LightSampler()
    vector = HorizonBaker(BatchTerrain()).bake_progressive(
        **kwargs, light_sampler=batch_light
    )
    np.testing.assert_allclose(vector[2], scalar[2], rtol=1e-6, atol=1e-6)
    np.testing.assert_allclose(vector[3], scalar[3], rtol=0.0, atol=1e-6)
    assert batch_light.batch_calls > 0
    assert batch_light.scalar_calls == 0


def test_vectorized_progress_and_previews_are_emitted_at_batch_boundaries():
    provider = _BatchSyntheticProvider()
    progress = []
    previews = []
    order = list(reversed(range(36)))
    result = HorizonBaker(provider).bake_progressive(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=2_000.0,
        delta_az_deg=10.0,
        band_defs=_band_definitions(),
        azimuth_order=order,
        preview_every=8,
        progress_callback=lambda percent, _message: progress.append(percent),
        preview_callback=lambda current, total, *_args: previews.append(
            (current, total, np.flatnonzero(_args[-1]).tolist())
        ),
    )
    assert progress == sorted(progress)
    assert progress[-1] == 100.0
    assert [item[:2] for item in previews] == [(8, 36), (36, 36)]
    assert set(previews[0][2]) == set(order[:8])
    assert np.all(result[4])


def test_vectorized_preview_count_is_bounded_and_finishes():
    provider = _BatchSyntheticProvider()
    previews = []
    total_azimuths = 360
    result = HorizonBaker(provider).bake_progressive(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=2_000.0,
        delta_az_deg=1.0,
        band_defs=_band_definitions(),
        preview_every=math.ceil(total_azimuths / 18),
        preview_callback=lambda current, total, *_args: previews.append(
            (current, total)
        ),
    )
    assert 1 <= len(previews) <= 18
    assert previews[-1] == (total_azimuths, total_azimuths)
    assert np.all(result[4])


def test_vectorized_polar_field_retains_only_mesh_distance_rings():
    provider = _BatchSyntheticProvider()
    baker = HorizonBaker(provider)
    baker.bake_progressive(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=20_000.0,
        delta_az_deg=10.0,
        band_defs=_band_definitions(),
    )
    field = baker._last_polar_field
    assert field is not None
    expected_rings = baker._mesh_distance_rings(20_000.0, 5.0)
    assert field.elevations.shape == (expected_rings.size, 36)
    np.testing.assert_array_equal(field.distances, expected_rings)


def test_prepare_progress_is_throttled_to_meaningful_percent_changes(monkeypatch):
    events = []
    monkeypatch.setattr(
        bake_process,
        "_emit_event",
        lambda event_type, **payload: events.append((event_type, payload)),
    )
    callback = bake_process._phase_progress("job", "prepare", 15.0, 35.0)
    for percent in range(101):
        callback(percent)
    mapped = [payload["percent"] for _event_type, payload in events]
    assert mapped == sorted(mapped)
    assert mapped[-1] == 35.0
    assert len(mapped) <= 22


def test_preview_payload_is_resolved_only_and_capped_to_1440_azimuths():
    count = 72_000
    azimuths = np.arange(count, dtype=np.float32) * np.float32(0.005)
    values = np.arange(count, dtype=np.float32)
    resolved = np.ones(count, dtype=bool)
    reduced = bake_process._reduced_preview_payload(
        azimuths,
        [
            {
                "id": "test",
                "min": 0.0,
                "max": 1.0,
                "angles": values,
                "dists": values,
                "heights": values,
            }
        ],
        values,
        values,
        resolved,
    )
    assert reduced[0].size == 1_440
    assert reduced[1][0]["angles"].size == 1_440
    assert np.all(reduced[4])
    assert np.all(np.diff(reduced[0]) > 0.0)


def test_relief_mesh_reuses_polar_field_and_batches_only_cartesian_patch():
    provider = _BatchSyntheticProvider()
    baker = HorizonBaker(provider)
    baker.bake_progressive(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=2_000.0,
        delta_az_deg=10.0,
        band_defs=_band_definitions(),
    )
    calls_after_bake = provider.batch_calls
    mesh = baker.build_view_mesh(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        d_max=2_000.0,
        delta_az_deg=10.0,
    )
    assert provider.batch_calls == calls_after_bake + 1
    assert baker.last_mesh_metrics["reused_field"] is True
    assert baker.last_mesh_metrics["near_patch_vertices"] < 5_000
    assert mesh["altitudes"].shape == mesh["valid"].shape


def test_relief_mesh_precision_is_decoupled_from_subpixel_ray_step():
    assert HorizonBaker._mesh_azimuth_step(0.005) == 0.05
    assert HorizonBaker._mesh_azimuth_step(0.03) == 0.06
    assert HorizonBaker._mesh_azimuth_step(0.331) == 0.331


def test_fine_raycast_retains_only_capped_relief_azimuths():
    provider = _BatchSyntheticProvider()
    baker = HorizonBaker(provider)
    baker.bake_progressive(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        step_m=5.0,
        d_max=200.0,
        delta_az_deg=0.025,
        band_defs=[{"id": "all", "min": 0.0, "max": 200.0}],
    )
    field = baker._last_polar_field
    assert field is not None
    assert field.delta_az_deg == 0.05
    assert field.azimuths.size == 7_200

    calls_after_bake = provider.batch_calls
    mesh = baker.build_view_mesh(
        obs_x=0.0,
        obs_y=0.0,
        obs_h_ground=120.0,
        d_max=200.0,
        delta_az_deg=0.025,
    )
    assert provider.batch_calls == calls_after_bake + 1
    assert baker.last_mesh_metrics["reused_field"] is True
    assert mesh["azimuths"].size == 7_200
