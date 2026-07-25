from __future__ import annotations

import numpy as np

from TerraLab.terrain.asc_cache_builder import materialize_asc_caches_spawned


def test_asc_cache_materialization_uses_spawned_bounded_workers(tmp_path):
    source = tmp_path / "tile.asc"
    source.write_text(
        "ncols 2\n"
        "nrows 2\n"
        "xllcorner 0\n"
        "yllcorner 0\n"
        "cellsize 1\n"
        "NODATA_value -9999\n"
        "1 2\n"
        "3 4\n",
        encoding="utf-8",
    )
    events = []
    results = materialize_asc_caches_spawned(
        tmp_path,
        max_workers=4,
        progress_callback=lambda percent, _message: events.append(percent),
    )
    assert len(results) == 1
    assert np.load(source.with_suffix(".npy"), allow_pickle=False).tolist() == [
        [1.0, 2.0],
        [3.0, 4.0],
    ]
    assert events == [100.0]
