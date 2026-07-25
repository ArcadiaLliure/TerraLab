"""Thread-safe, byte-bounded runtime caches."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Generic, Iterator, MutableMapping, TypeVar


K = TypeVar("K")
V = TypeVar("V")


class ByteLRU(Generic[K, V]):
    """Least-recently-used cache bounded by resident bytes.

    Values larger than the complete budget are not cached. Replacing an
    existing key adjusts the resident-byte count before eviction.
    """

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
