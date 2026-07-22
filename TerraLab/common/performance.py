"""Shared memory budgets, bounded caches and cancellable generations.

The module deliberately has no Qt or optional dependency.  It is used by the
terrain and catalogue pipelines, including subprocesses spawned on Windows.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, Iterator, MutableMapping, TypeVar


GiB = 1024**3
MiB = 1024**2
MAX_BATCH_ITEMS = 1_000_000
MAX_BATCH_BYTES = 128 * MiB


def _environment_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class PerformanceFlags:
    """Independent rollback switches for the optimized runtime backends."""

    raster_batch: bool = True
    raycast_vectorized: bool = True
    relief_cached: bool = True
    gaia_out_of_core: bool = True
    surface_lod_cache: bool = True

    @classmethod
    def from_environment(cls) -> "PerformanceFlags":
        return cls(
            raster_batch=_environment_flag("TERRALAB_RASTER_BATCH"),
            raycast_vectorized=_environment_flag("TERRALAB_RAYCAST_VECTORIZED"),
            relief_cached=_environment_flag("TERRALAB_RELIEF_CACHED"),
            gaia_out_of_core=_environment_flag("TERRALAB_GAIA_OUT_OF_CORE"),
            surface_lod_cache=_environment_flag("TERRALAB_SURFACE_LOD_CACHE"),
        )


def physical_memory_bytes() -> int:
    """Return installed physical RAM without requiring psutil."""

    if os.name == "nt":
        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except Exception:
            pass
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and page_size > 0:
            return pages * page_size
    except (AttributeError, OSError, ValueError):
        pass
    # Conservative fallback matching the supported reference machine.
    return 16 * GiB


def process_memory_bytes() -> tuple[int, int]:
    """Return current RSS and process peak RSS using only the standard library."""

    if os.name == "nt":
        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        try:
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_ProcessMemoryCounters),
                ctypes.c_ulong,
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)
        except Exception:
            pass
    try:
        status = {}
        with open("/proc/self/status", "r", encoding="ascii") as handle:
            for line in handle:
                name, _separator, value = line.partition(":")
                if name in {"VmRSS", "VmHWM"}:
                    status[name] = int(value.strip().split()[0]) * 1024
        if status:
            rss = int(status.get("VmRSS", 0))
            return rss, int(status.get("VmHWM", rss))
    except (OSError, ValueError):
        pass
    try:
        import resource

        maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        maximum_bytes = maximum if sys.platform == "darwin" else maximum * 1024
        return 0, maximum_bytes
    except (ImportError, OSError, ValueError):
        pass
    return 0, 0


@dataclass(frozen=True, slots=True)
class PerformanceBudget:
    """Central memory limits for out-of-core runtime work."""

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
PERFORMANCE_FLAGS = PerformanceFlags.from_environment()


class GenerationToken:
    """Cheap cooperative cancellation token implementing last-request-wins."""

    __slots__ = ("_owner", "generation")

    def __init__(self, owner: "GenerationController", generation: int) -> None:
        self._owner = owner
        self.generation = int(generation)

    @property
    def cancelled(self) -> bool:
        return not self._owner.is_current(self.generation)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise InterruptedError("Superseded by a newer request")


class GenerationController:
    """Thread-safe source of monotonically increasing request generations."""

    def __init__(self) -> None:
        self._generation = 0
        self._lock = threading.Lock()

    def next(self) -> GenerationToken:
        with self._lock:
            self._generation += 1
            generation = self._generation
        return GenerationToken(self, generation)

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1

    def is_current(self, generation: int) -> bool:
        with self._lock:
            return self._generation == int(generation)


K = TypeVar("K")
V = TypeVar("V")


class ByteLRU(Generic[K, V]):
    """Thread-safe LRU bounded by resident bytes instead of entry count."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(0, int(max_bytes))
        self._items: MutableMapping[K, tuple[V, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @property
    def resident_bytes(self) -> int:
        with self._lock:
            return int(self._bytes)

    def get(self, key: K) -> V | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self.misses += 1
                return None
            self.hits += 1
            self._items.move_to_end(key)
            return item[0]

    def put(self, key: K, value: V, size_bytes: int) -> None:
        size = max(0, int(size_bytes))
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._bytes -= previous[1]
            # A single object larger than the whole budget is not cacheable.
            if self.max_bytes <= 0 or size > self.max_bytes:
                return
            self._items[key] = (value, size)
            self._bytes += size
            while self._bytes > self.max_bytes and self._items:
                _old_key, (_old_value, old_size) = self._items.popitem(last=False)
                self._bytes -= old_size
                self.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def values(self) -> Iterator[V]:
        with self._lock:
            snapshot = tuple(item[0] for item in self._items.values())
        return iter(snapshot)
