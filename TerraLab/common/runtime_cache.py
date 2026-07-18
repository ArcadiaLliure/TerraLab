"""Small runtime primitives shared by data and rendering code."""

from __future__ import annotations

import threading
from collections import OrderedDict


class CancelledGeneration(InterruptedError):
    """Raised when work belongs to a superseded generation."""


class GenerationToken:
    def __init__(self, owner: "GenerationController", generation: int) -> None:
        self._owner = owner
        self._generation = generation

    def raise_if_cancelled(self) -> None:
        if not self._owner.is_current(self._generation):
            raise CancelledGeneration("generation superseded")


class GenerationController:
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
            return generation == self._generation


class ByteLRU:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(0, int(max_bytes))
        self._bytes = 0
        self._items: OrderedDict[object, tuple[object, int]] = OrderedDict()

    def get(self, key: object):
        item = self._items.get(key)
        if item is None:
            return None
        self._items.move_to_end(key)
        return item[0]

    def put(self, key: object, value: object, size_bytes: int) -> None:
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= old[1]
        size = max(0, int(size_bytes))
        if size > self.max_bytes:
            return
        self._items[key] = (value, size)
        self._bytes += size
        while self._bytes > self.max_bytes and self._items:
            _, (_, removed_size) = self._items.popitem(last=False)
            self._bytes -= removed_size

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0
