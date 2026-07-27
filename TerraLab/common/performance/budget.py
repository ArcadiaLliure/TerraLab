"""Central memory and batch-size budgets."""

from __future__ import annotations

from dataclasses import dataclass

from TerraLab.common.performance.memory import physical_memory_bytes


GiB = 1024**3
MiB = 1024**2
MAX_BATCH_ITEMS = 1_000_000
MAX_BATCH_BYTES = 128 * MiB


@dataclass(frozen=True, slots=True)
class PerformanceBudget:
    """Memory limits for out-of-core runtime work."""

    total_bytes: int
    dem_bytes: int
    stars_bytes: int
    transient_bytes: int
    max_batch_items: int = MAX_BATCH_ITEMS
    max_batch_bytes: int = MAX_BATCH_BYTES

    @classmethod
    def for_machine(cls, physical_bytes: int | None = None) -> "PerformanceBudget":
        physical = max(GiB, int(physical_bytes or physical_memory_bytes()))
        total = min(6 * GiB, int(physical * 0.30))
        dem = int(total * 0.40)
        stars = int(total * 0.40)
        transient = total - dem - stars
        return cls(total, dem, stars, transient)

    def batch_rows(self, itemsize: int, requested: int | None = None) -> int:
        per_item = max(1, int(itemsize))
        by_bytes = max(1, self.max_batch_bytes // per_item)
        limit = min(self.max_batch_items, by_bytes)
        return min(limit, max(1, int(requested))) if requested else limit


DEFAULT_PERFORMANCE_BUDGET = PerformanceBudget.for_machine()
