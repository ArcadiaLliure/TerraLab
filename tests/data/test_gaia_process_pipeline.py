from __future__ import annotations

import numpy as np

from TerraLab.common.cancellation import GenerationController
from TerraLab.data.star_catalog_store import HealpixStarCatalogStore
from TerraLab.util.gaia_importer import build_gaia_catalog_spawned


def test_gaia_import_and_index_pipeline_runs_in_spawn_process(tmp_path):
    source = tmp_path / "gaia.csv"
    source.write_text(
        "ra,dec,phot_g_mean_mag,bp_rp,source_id\n"
        "10.0,20.0,7.0,0.5,101\n"
        "11.0,21.0,8.0,1.0,102\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    progress = []
    summary = build_gaia_catalog_spawned(
        [str(source)],
        str(output),
        output_basename="small",
        write_npz=False,
        write_npy=True,
        write_zst=False,
        build_healpy_index=True,
        healpy_nside=8,
        healpy_chunk_rows=1,
        progress_callback=lambda percent, _message: progress.append(percent),
    )
    assert summary["rows"] == 2
    assert (output / "small.npy").exists()
    assert (output / "small_healpy.npy").exists()
    with np.load(output / "small_healpy.idx.npz", allow_pickle=False) as index:
        assert int(index["format_version"].item()) == 2
    assert progress and progress[-1] == 100.0

    store = HealpixStarCatalogStore(
        output / "small_healpy.npy", output / "small_healpy.idx.npz"
    )
    try:
        batches = list(store.query_cone(10.0, 20.0, 0.25, 7.5))
        assert [int(source_id) for batch in batches for source_id in batch.source_id] == [101]
        assert not batches[0].ra.flags.writeable

        generations = GenerationController()
        stale = generations.next()
        generations.next()
        with np.testing.assert_raises(InterruptedError):
            list(store.query_cone(10.0, 20.0, 5.0, 22.0, token=stale))
    finally:
        store.close()
