"""Lightweight diagnostics for render counters and timings."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class DiagnosticsSnapshot:
    counters: Dict[str, float] = field(default_factory=dict)
    timings_ms: Dict[str, float] = field(default_factory=dict)


class Diagnostics:
    def __init__(self) -> None:
        self._counters: Dict[str, float] = {}
        self._timings_ms: Dict[str, float] = {}
        self._starts: Dict[str, float] = {}

    def reset(self) -> None:
        self._counters.clear()
        self._timings_ms.clear()
        self._starts.clear()

    def set_counter(self, name: str, value: float) -> None:
        self._counters[name] = value

    def inc_counter(self, name: str, delta: float = 1.0) -> None:
        self._counters[name] = self._counters.get(name, 0.0) + delta

    def start_timer(self, name: str) -> None:
        self._starts[name] = time.perf_counter()

    def stop_timer(self, name: str) -> float:
        start = self._starts.pop(name, None)
        if start is None:
            return 0.0
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self._timings_ms[name] = elapsed_ms
        return elapsed_ms

    def snapshot(self) -> DiagnosticsSnapshot:
        return DiagnosticsSnapshot(
            counters=dict(self._counters),
            timings_ms=dict(self._timings_ms),
        )

    def to_log_line(self, prefix: str = "[Diagnostics]") -> str:
        counters_part = " ".join(f"{k}={v}" for k, v in sorted(self._counters.items()))
        timings_part = " ".join(f"{k}={v:.2f}ms" for k, v in sorted(self._timings_ms.items()))
        if counters_part and timings_part:
            return f"{prefix} {counters_part} | {timings_part}"
        if counters_part:
            return f"{prefix} {counters_part}"
        if timings_part:
            return f"{prefix} {timings_part}"
        return prefix
