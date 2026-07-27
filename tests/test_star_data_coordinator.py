from __future__ import annotations

import json
from concurrent.futures import Future

import numpy as np

from TerraLab.data.star_data_coordinator import (
    StarDataCoordinator,
    _combine_tiles,
    _normalize_tile_arrays,
)


def test_normalize_tile_arrays_orders_source_id_by_magnitude() -> None:
    payload = {
        "ra": np.asarray([20.0, 10.0], dtype=np.float32),
        "dec": np.asarray([1.0, 0.0], dtype=np.float32),
        "mag": np.asarray([4.0, 2.0], dtype=np.float32),
        "bp_rp": np.asarray([1.0, 0.5], dtype=np.float32),
        "source_id": np.asarray([200, 100], dtype=np.int64),
    }

    normalized = _normalize_tile_arrays(payload)

    assert normalized["mag"].tolist() == [2.0, 4.0]
    assert normalized["source_id"].tolist() == [100, 200]
    assert len(normalized["r"]) == 2
    assert len(normalized["g"]) == 2
    assert len(normalized["b"]) == 2


def test_combine_tiles_includes_internal_supplement_and_deduplicates_source_id() -> None:
    base_tile = {
        "ra": np.asarray([10.0, 20.0], dtype=np.float32),
        "dec": np.asarray([0.0, 1.0], dtype=np.float32),
        "mag": np.asarray([2.0, 4.0], dtype=np.float32),
        "bp_rp": np.asarray([0.5, 1.0], dtype=np.float32),
        "r": np.asarray([190.0, 220.0], dtype=np.float32),
        "g": np.asarray([200.0, 200.0], dtype=np.float32),
        "b": np.asarray([255.0, 180.0], dtype=np.float32),
        "source_id": np.asarray([100, 200], dtype=np.int64),
    }
    supplement_tile = {
        "ra": np.asarray([30.0, 40.0], dtype=np.float32),
        "dec": np.asarray([2.0, 3.0], dtype=np.float32),
        "mag": np.asarray([1.0, 3.0], dtype=np.float32),
        "bp_rp": np.asarray([0.2, 0.7], dtype=np.float32),
        "r": np.asarray([160.0, 210.0], dtype=np.float32),
        "g": np.asarray([190.0, 205.0], dtype=np.float32),
        "b": np.asarray([255.0, 190.0], dtype=np.float32),
        "source_id": np.asarray([200, -1], dtype=np.int64),
    }

    combined = _combine_tiles(
        base_tile_id="tile_all",
        loaded_tiles={
            "tile_all": base_tile,
            "__no_gaia_supplement__": supplement_tile,
        },
        internal_tile_ids={"__no_gaia_supplement__"},
    )

    # Orden per magnitud + deduplicacio per source_id positiu.
    assert combined["mag"].tolist() == [1.0, 2.0, 3.0]
    assert combined["source_id"].tolist() == [200, 100, -1]
    # El pseudo-tile intern no ha d'apareixer en el conjunt de teseles carregades.
    assert combined["loaded_tile_ids"] == frozenset({"tile_all"})


def test_preload_adjacent_tiles_does_not_emit_regressive_snapshot_when_center_missing(
    tmp_path,
) -> None:
    manifest_path = tmp_path / "tile_manifest.json"
    payload = {
        "version": 1,
        "tile_size_deg": 5.0,
        "general_tile": {
            "id": "tile_all",
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
        ],
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    for file_name in ("tile_all.npz", "tile_0000_+00.npz", "tile_0005_+00.npz"):
        np.savez(tmp_path / file_name, ra=np.array([], dtype=np.float32))

    coordinator = StarDataCoordinator(manifest_path)
    try:
        def _no_async_submit(*args, **kwargs):
            future = Future()
            future.set_result(None)
            return future

        coordinator._executor.submit = _no_async_submit
        coordinator._preload_executor.submit = _no_async_submit
        coordinator._index_executor.submit = _no_async_submit

        base_tile = {
            "ra": np.asarray([1.0], dtype=np.float32),
            "dec": np.asarray([1.0], dtype=np.float32),
            "mag": np.asarray([2.0], dtype=np.float32),
            "bp_rp": np.asarray([0.5], dtype=np.float32),
            "r": np.asarray([200.0], dtype=np.float32),
            "g": np.asarray([200.0], dtype=np.float32),
            "b": np.asarray([255.0], dtype=np.float32),
            "source_id": np.asarray([100], dtype=np.int64),
        }
        loaded_focus_tile = {
            "ra": np.asarray([2.0], dtype=np.float32),
            "dec": np.asarray([2.0], dtype=np.float32),
            "mag": np.asarray([9.0], dtype=np.float32),
            "bp_rp": np.asarray([0.8], dtype=np.float32),
            "r": np.asarray([180.0], dtype=np.float32),
            "g": np.asarray([185.0], dtype=np.float32),
            "b": np.asarray([210.0], dtype=np.float32),
            "source_id": np.asarray([200], dtype=np.int64),
        }

        with coordinator._lock:
            coordinator._loaded_tiles["tile_all"] = base_tile
            coordinator._loaded_tiles["tile_0000_+00"] = loaded_focus_tile
            coordinator._scope_focus_tile_id = "tile_0000_+00"
            coordinator._scope_active_tile_ids = {"tile_0000_+00"}
            coordinator._compose_active_dataset_locked()

        emitted_payloads = []
        coordinator.extension_ready.connect(emitted_payloads.append)

        coordinator.preload_adjacent_tiles("tile_0005_+00")

        assert emitted_payloads == []
    finally:
        coordinator.shutdown()


def test_load_deep_tile_keeps_previous_active_dataset_until_new_center_is_loaded(
    tmp_path,
) -> None:
    manifest_path = tmp_path / "tile_manifest.json"
    payload = {
        "version": 1,
        "tile_size_deg": 5.0,
        "general_tile": {
            "id": "tile_all",
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
                "id": "tile_0010_+00",
                "file": "tile_0010_+00.npz",
                "ra_min": 10.0,
                "ra_max": 15.0,
                "dec_min": 0.0,
                "dec_max": 5.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 12,
            },
        ],
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    np.savez(
        tmp_path / "tile_all.npz",
        ra=np.asarray([1.0], dtype=np.float32),
        dec=np.asarray([1.0], dtype=np.float32),
        phot_g_mean_mag=np.asarray([2.0], dtype=np.float32),
        bp_rp=np.asarray([0.5], dtype=np.float32),
        source_id=np.asarray([100], dtype=np.int64),
    )
    np.savez(
        tmp_path / "tile_0000_+00.npz",
        ra=np.asarray([2.0], dtype=np.float32),
        dec=np.asarray([2.0], dtype=np.float32),
        phot_g_mean_mag=np.asarray([9.0], dtype=np.float32),
        bp_rp=np.asarray([0.8], dtype=np.float32),
        source_id=np.asarray([200], dtype=np.int64),
    )
    np.savez(
        tmp_path / "tile_0010_+00.npz",
        ra=np.asarray([12.0], dtype=np.float32),
        dec=np.asarray([2.0], dtype=np.float32),
        phot_g_mean_mag=np.asarray([10.0], dtype=np.float32),
        bp_rp=np.asarray([0.9], dtype=np.float32),
        source_id=np.asarray([300], dtype=np.int64),
    )

    coordinator = StarDataCoordinator(manifest_path)
    try:
        def _no_async_submit(*args, **kwargs):
            future = Future()
            future.set_result(None)
            return future

        coordinator._executor.submit = _no_async_submit
        coordinator._preload_executor.submit = _no_async_submit
        coordinator._index_executor.submit = _no_async_submit

        coordinator._load_tile_worker(coordinator.manifest().get_general_tile(), True)
        old_entry = next(
            item
            for item in coordinator.manifest().deep_tiles
            if item.tile_id == "tile_0000_+00"
        )
        new_entry = next(
            item
            for item in coordinator.manifest().deep_tiles
            if item.tile_id == "tile_0010_+00"
        )
        coordinator._load_tile_worker(old_entry, False)

        before = coordinator.get_active_dataset()
        assert "tile_0000_+00" in before["loaded_tile_ids"]
        before_ids = before["loaded_tile_ids"]
        before_rows = int(len(before["ra"]))

        coordinator.load_deep_tile("tile_0010_+00")
        during = coordinator.get_active_dataset()
        assert during["loaded_tile_ids"] == before_ids
        assert int(len(during["ra"])) == before_rows

        coordinator._load_tile_worker(new_entry, False)
        after = coordinator.get_active_dataset()
        assert "tile_0010_+00" in after["loaded_tile_ids"]
        assert 300 in after["source_id"].tolist()
    finally:
        coordinator.shutdown()


def test_preload_adjacent_tiles_emits_when_center_is_already_loaded(tmp_path) -> None:
    manifest_path = tmp_path / "tile_manifest.json"
    payload = {
        "version": 1,
        "tile_size_deg": 5.0,
        "general_tile": {
            "id": "tile_all",
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
            }
        ],
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    for file_name in ("tile_all.npz", "tile_0000_+00.npz"):
        np.savez(tmp_path / file_name, ra=np.array([], dtype=np.float32))

    coordinator = StarDataCoordinator(manifest_path)
    try:
        def _no_async_submit(*args, **kwargs):
            future = Future()
            future.set_result(None)
            return future

        coordinator._executor.submit = _no_async_submit
        coordinator._preload_executor.submit = _no_async_submit
        coordinator._index_executor.submit = _no_async_submit

        base_tile = {
            "ra": np.asarray([1.0], dtype=np.float32),
            "dec": np.asarray([1.0], dtype=np.float32),
            "mag": np.asarray([2.0], dtype=np.float32),
            "bp_rp": np.asarray([0.5], dtype=np.float32),
            "r": np.asarray([200.0], dtype=np.float32),
            "g": np.asarray([200.0], dtype=np.float32),
            "b": np.asarray([255.0], dtype=np.float32),
            "source_id": np.asarray([100], dtype=np.int64),
        }
        loaded_focus_tile = {
            "ra": np.asarray([2.0], dtype=np.float32),
            "dec": np.asarray([2.0], dtype=np.float32),
            "mag": np.asarray([9.0], dtype=np.float32),
            "bp_rp": np.asarray([0.8], dtype=np.float32),
            "r": np.asarray([180.0], dtype=np.float32),
            "g": np.asarray([185.0], dtype=np.float32),
            "b": np.asarray([210.0], dtype=np.float32),
            "source_id": np.asarray([200], dtype=np.int64),
        }

        with coordinator._lock:
            coordinator._loaded_tiles["tile_all"] = base_tile
            coordinator._loaded_tiles["tile_0000_+00"] = loaded_focus_tile
            coordinator._scope_focus_tile_id = "tile_0000_+00"
            coordinator._scope_active_tile_ids = {"tile_0000_+00"}
            coordinator._compose_active_dataset_locked()

        emitted_payloads = []
        coordinator.extension_ready.connect(emitted_payloads.append)

        coordinator.preload_adjacent_tiles("tile_0000_+00")

        assert len(emitted_payloads) == 1
        assert emitted_payloads[0]["loaded_tile_ids"] == frozenset(
            {"tile_all", "tile_0000_+00"}
        )
    finally:
        coordinator.shutdown()


def test_request_scope_region_prioritizes_all_tiles_intersecting_scope_field(
    tmp_path,
) -> None:
    manifest_path = tmp_path / "tile_manifest.json"
    payload = {
        "version": 1,
        "tile_size_deg": 5.0,
        "general_tile": {
            "id": "tile_all",
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
                "star_count": 11,
            },
            {
                "id": "tile_0000_+05",
                "file": "tile_0000_+05.npz",
                "ra_min": 0.0,
                "ra_max": 5.0,
                "dec_min": 5.0,
                "dec_max": 10.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 12,
            },
            {
                "id": "tile_0005_+05",
                "file": "tile_0005_+05.npz",
                "ra_min": 5.0,
                "ra_max": 10.0,
                "dec_min": 5.0,
                "dec_max": 10.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 13,
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
                "star_count": 14,
            },
        ],
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    for file_name in (
        "tile_all.npz",
        "tile_0000_+00.npz",
        "tile_0005_+00.npz",
        "tile_0000_+05.npz",
        "tile_0005_+05.npz",
        "tile_0000_-05.npz",
    ):
        np.savez(tmp_path / file_name, ra=np.array([], dtype=np.float32))

    coordinator = StarDataCoordinator(manifest_path)
    try:
        priority_submits: list[str] = []
        preload_submits: list[str] = []

        def _capture_priority(fn, entry, is_general):
            priority_submits.append(str(entry.tile_id))
            future = Future()
            future.set_result(None)
            return future

        def _capture_preload(fn, entry, is_general):
            preload_submits.append(str(entry.tile_id))
            future = Future()
            future.set_result(None)
            return future

        coordinator._executor.submit = _capture_priority
        coordinator._preload_executor.submit = _capture_preload
        coordinator._index_executor.submit = lambda *args, **kwargs: Future()

        coordinator.request_scope_region(
            "tile_0000_+00",
            priority_tile_ids=(
                "tile_0000_+00",
                "tile_0005_+00",
                "tile_0000_+05",
                "tile_0005_+05",
            ),
        )

        assert set(priority_submits) == {
            "tile_0000_+00",
            "tile_0005_+00",
            "tile_0000_+05",
            "tile_0005_+05",
        }
        assert preload_submits == ["tile_0000_-05"]
    finally:
        coordinator.shutdown()


def test_request_scope_region_keeps_loaded_pending_tiles_visible_before_focus_load(
    tmp_path,
) -> None:
    manifest_path = tmp_path / "tile_manifest.json"
    payload = {
        "version": 1,
        "tile_size_deg": 5.0,
        "general_tile": {
            "id": "tile_all",
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
                "id": "tile_0010_+00",
                "file": "tile_0010_+00.npz",
                "ra_min": 10.0,
                "ra_max": 15.0,
                "dec_min": 0.0,
                "dec_max": 5.0,
                "mag_min": 8.0,
                "mag_max": 16.0,
                "star_count": 11,
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
    for file_name in (
        "tile_all.npz",
        "tile_0000_+00.npz",
        "tile_0010_+00.npz",
        "tile_0010_+05.npz",
    ):
        np.savez(tmp_path / file_name, ra=np.array([], dtype=np.float32))

    coordinator = StarDataCoordinator(manifest_path)
    try:
        def _no_async_submit(*args, **kwargs):
            future = Future()
            future.set_result(None)
            return future

        coordinator._executor.submit = _no_async_submit
        coordinator._preload_executor.submit = _no_async_submit
        coordinator._index_executor.submit = _no_async_submit

        base_tile = {
            "ra": np.asarray([1.0], dtype=np.float32),
            "dec": np.asarray([1.0], dtype=np.float32),
            "mag": np.asarray([2.0], dtype=np.float32),
            "bp_rp": np.asarray([0.5], dtype=np.float32),
            "r": np.asarray([200.0], dtype=np.float32),
            "g": np.asarray([200.0], dtype=np.float32),
            "b": np.asarray([255.0], dtype=np.float32),
            "source_id": np.asarray([100], dtype=np.int64),
        }
        old_focus_tile = {
            "ra": np.asarray([2.0], dtype=np.float32),
            "dec": np.asarray([2.0], dtype=np.float32),
            "mag": np.asarray([9.0], dtype=np.float32),
            "bp_rp": np.asarray([0.8], dtype=np.float32),
            "r": np.asarray([180.0], dtype=np.float32),
            "g": np.asarray([185.0], dtype=np.float32),
            "b": np.asarray([210.0], dtype=np.float32),
            "source_id": np.asarray([200], dtype=np.int64),
        }
        pending_visible_tile = {
            "ra": np.asarray([12.0], dtype=np.float32),
            "dec": np.asarray([7.0], dtype=np.float32),
            "mag": np.asarray([10.0], dtype=np.float32),
            "bp_rp": np.asarray([0.9], dtype=np.float32),
            "r": np.asarray([170.0], dtype=np.float32),
            "g": np.asarray([180.0], dtype=np.float32),
            "b": np.asarray([220.0], dtype=np.float32),
            "source_id": np.asarray([300], dtype=np.int64),
        }

        with coordinator._lock:
            coordinator._loaded_tiles["tile_all"] = base_tile
            coordinator._loaded_tiles["tile_0000_+00"] = old_focus_tile
            coordinator._loaded_tiles["tile_0010_+05"] = pending_visible_tile
            coordinator._scope_focus_tile_id = "tile_0000_+00"
            coordinator._scope_active_tile_ids = {"tile_0000_+00"}
            coordinator._compose_active_dataset_locked()

        coordinator.request_scope_region(
            "tile_0010_+00",
            priority_tile_ids=("tile_0010_+00", "tile_0010_+05"),
        )

        active_dataset = coordinator.get_active_dataset()
        assert active_dataset["loaded_tile_ids"] == frozenset(
            {"tile_all", "tile_0000_+00", "tile_0010_+05"}
        )
        assert 300 in active_dataset["source_id"].tolist()
        assert "tile_0010_+00" not in active_dataset["loaded_tile_ids"]
    finally:
        coordinator.shutdown()
