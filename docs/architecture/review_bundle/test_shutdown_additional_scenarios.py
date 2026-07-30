"""Review-only shutdown probes for cache and preview generation."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PyQt5.QtCore import QCoreApplication

from TerraLab.terrain.terrain_coordinator import TerrainCoordinator
from TerraLab.terrain.worker import HorizonWorker


def _wait_for(
    event: threading.Event,
    app: QCoreApplication,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while not event.is_set() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert event.is_set()


def test_close_during_surface_cache_generation(monkeypatch, tmp_path: Path):
    app = QCoreApplication.instance() or QCoreApplication([])
    entered = threading.Event()
    exited = threading.Event()
    cache_path = tmp_path / "surface-cache.partial"

    def generate_cache(worker: HorizonWorker, payload: dict) -> None:
        generation = int(payload["generation"])
        with cache_path.open("wb") as handle:
            entered.set()
            while not worker._surface_request_cancelled(generation):
                handle.write(b"cache-block\n")
                handle.flush()
                time.sleep(0.005)
        exited.set()

    monkeypatch.setattr(
        HorizonWorker,
        "request_surface_refresh",
        generate_cache,
    )
    coordinator = TerrainCoordinator()
    try:
        coordinator.request_surface_refresh(object())
        _wait_for(entered, app)

        started = time.perf_counter()
        coordinator.shutdown()
        elapsed = time.perf_counter() - started

        assert elapsed < 2.0
        assert exited.is_set()
        assert not coordinator.worker_thread.isRunning()
        renamed = cache_path.with_suffix(".unlocked")
        cache_path.replace(renamed)
        renamed.unlink()
    finally:
        coordinator.shutdown()


def test_close_during_preview_file_generation(monkeypatch, tmp_path: Path):
    app = QCoreApplication.instance() or QCoreApplication([])
    entered = threading.Event()
    exited = threading.Event()
    preview_path = tmp_path / "preview.partial"

    def generate_preview(worker: HorizonWorker, _job: dict) -> None:
        with preview_path.open("wb") as handle:
            entered.set()
            while not worker._shutdown_event.is_set():
                handle.write(b"preview-block\n")
                handle.flush()
                time.sleep(0.005)
        exited.set()

    monkeypatch.setattr(HorizonWorker, "request_bake", generate_preview)
    coordinator = TerrainCoordinator()
    try:
        coordinator.request_bake({"job_id": "preview-review"})
        _wait_for(entered, app)

        started = time.perf_counter()
        coordinator.shutdown()
        elapsed = time.perf_counter() - started

        assert elapsed < 2.0
        assert exited.is_set()
        assert not coordinator.worker_thread.isRunning()
        renamed = preview_path.with_suffix(".unlocked")
        preview_path.replace(renamed)
        renamed.unlink()
    finally:
        coordinator.shutdown()
