from __future__ import annotations

from TerraLab.common.performance.budget import GiB, PerformanceBudget
from TerraLab.common.performance.flags import PerformanceFlags


def test_performance_budget_uses_shared_40_40_20_split():
    budget = PerformanceBudget.for_machine(16 * GiB)
    assert budget.total_bytes == int(16 * GiB * 0.30)
    assert budget.dem_bytes == int(budget.total_bytes * 0.40)
    assert budget.stars_bytes == int(budget.total_bytes * 0.40)
    assert budget.transient_bytes == budget.total_bytes - budget.dem_bytes - budget.stars_bytes
    assert budget.batch_rows(256) <= 1_000_000
    assert budget.batch_rows(256) * 256 <= 128 * 1024**2


def test_performance_backends_have_independent_rollback_flags(monkeypatch):
    monkeypatch.setenv("TERRALAB_RASTER_BATCH", "0")
    monkeypatch.setenv("TERRALAB_RAYCAST_VECTORIZED", "false")
    monkeypatch.setenv("TERRALAB_RELIEF_CACHED", "off")
    monkeypatch.setenv("TERRALAB_GAIA_OUT_OF_CORE", "no")
    assert PerformanceFlags.from_environment() == PerformanceFlags(
        raster_batch=False,
        raycast_vectorized=False,
        relief_cached=False,
        gaia_out_of_core=False,
    )
