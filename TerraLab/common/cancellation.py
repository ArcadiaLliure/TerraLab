"""Cooperative last-request-wins cancellation primitives."""

from __future__ import annotations

import threading


class CancelledGeneration(InterruptedError):
    """Raised when work belongs to a superseded generation."""


class GenerationToken:
    """A cheap immutable view of one controller generation."""

    __slots__ = ("_owner", "generation")

    def __init__(self, owner: "GenerationController", generation: int) -> None:
        self._owner = owner
        self.generation = int(generation)

    @property
    def cancelled(self) -> bool:
        return not self._owner.is_current(self.generation)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledGeneration("generation superseded")


class GenerationController:
    """Thread-safe source of monotonically increasing generations."""

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
