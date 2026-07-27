from __future__ import annotations

import numpy as np

from TerraLab.terrain.providers import ElevationBatch
from TerraLab.terrain.services import elevation_query


class _Provider:
    internal_crs = "EPSG:25831"
    source_id = "fixture-dem"

    def __init__(self, covered: bool) -> None:
        self.covered = covered
        self.closed = False

    def transform_coordinates(self, latitude, longitude):
        return longitude * 1000.0, latitude * 1000.0

    def sample_elevations(self, x, y, *, input_crs=None):
        return ElevationBatch(
            values=np.asarray(123.5 if self.covered else 0.0, dtype=np.float32),
            valid=np.asarray(self.covered, dtype=bool),
            source_indices=np.asarray(0 if self.covered else -1, dtype=np.int16),
        )

    def get_nominal_resolution_m(self):
        return 30.0

    def get_native_crs(self):
        return self.internal_crs

    def close(self):
        self.closed = True


def test_elevation_query_reports_provenance_and_observer_height(
    tmp_path, monkeypatch
):
    dem = tmp_path / "dem.tif"
    dem.touch()
    provider = _Provider(covered=True)
    monkeypatch.setattr(
        elevation_query,
        "create_elevation_provider",
        lambda _path: provider,
    )
    result = elevation_query.query_elevation(
        41.2,
        1.1,
        dem_path=dem,
        observer_offset_m=2.0,
        eye_height_m=1.7,
    )
    assert result.has_coverage
    assert result.elevation_m == 123.5
    assert result.source_id == "fixture-dem"
    assert result.observer_ground_elevation_m == 125.5
    assert result.observer_eye_elevation_m == 127.2
    assert provider.closed


def test_elevation_query_never_invents_ground_outside_coverage(
    tmp_path, monkeypatch
):
    dem = tmp_path / "dem.tif"
    dem.touch()
    provider = _Provider(covered=False)
    monkeypatch.setattr(
        elevation_query,
        "create_elevation_provider",
        lambda _path: provider,
    )
    result = elevation_query.query_elevation(41.2, 1.1, dem_path=dem)
    assert not result.has_coverage
    assert result.elevation_m is None
    assert result.observer_ground_elevation_m is None
    assert result.observer_eye_elevation_m is None
    assert provider.closed
