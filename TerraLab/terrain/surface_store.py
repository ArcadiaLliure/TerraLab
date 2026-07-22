"""Atomic, byte-bounded NPZ storage used by terrain surface sampling.

The store deliberately knows nothing about rasterio or Qt.  Surface-result
snapshots and sparse categorical LOD tiles share one root and therefore one
LRU budget.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np


@dataclass
class NpzStoreStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    corrupt: int = 0
    evictions: int = 0
    loads_ns: int = 0
    writes_ns: int = 0
    fsync_ns: int = 0
    prunes_ns: int = 0
    prunes: int = 0


class AtomicNpzStore:
    """Small persistent cache with atomic writes and a shared disk LRU."""

    _process_lock = threading.RLock()

    def __init__(self, root: str | os.PathLike[str], *, budget_bytes: int) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.budget_bytes = max(0, int(budget_bytes))
        self.stats = NpzStoreStats()
        self._last_touch_ns: dict[Path, int] = {}
        self._touch_interval_ns = 60 * 1_000_000_000
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, relative_key: str) -> Path:
        parts = [part for part in str(relative_key).replace("\\", "/").split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise ValueError("Invalid persistent-cache key")
        target = self.root.joinpath(*parts).with_suffix(".npz").resolve(strict=False)
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Persistent-cache key escapes its root") from exc
        return target

    @staticmethod
    def _metadata_array(metadata: Mapping[str, Any]) -> np.ndarray:
        payload = json.dumps(
            dict(metadata), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return np.frombuffer(payload, dtype=np.uint8).copy()

    @staticmethod
    def _decode_metadata(value: np.ndarray) -> dict[str, Any]:
        raw = np.asarray(value, dtype=np.uint8).tobytes().decode("utf-8")
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("NPZ cache metadata is not an object")
        return decoded

    def load(self, relative_key: str) -> tuple[dict[str, Any], dict[str, np.ndarray]] | None:
        started_ns = time.perf_counter_ns()
        path = self._path(relative_key)
        with self._process_lock:
            if not path.is_file():
                self.stats.misses += 1
                self.stats.loads_ns += time.perf_counter_ns() - started_ns
                return None
            try:
                with np.load(path, allow_pickle=False) as archive:
                    if "__metadata__" not in archive.files:
                        raise ValueError("NPZ cache metadata is missing")
                    metadata = self._decode_metadata(archive["__metadata__"])
                    arrays = {
                        name: np.asarray(archive[name]).copy()
                        for name in archive.files
                        if name != "__metadata__"
                    }
                now_ns = time.monotonic_ns()
                if now_ns - self._last_touch_ns.get(path, 0) >= self._touch_interval_ns:
                    os.utime(path, None)
                    self._last_touch_ns[path] = now_ns
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self.stats.corrupt += 1
                self.stats.misses += 1
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                self.stats.loads_ns += time.perf_counter_ns() - started_ns
                return None
            self.stats.hits += 1
            self.stats.loads_ns += time.perf_counter_ns() - started_ns
            return metadata, arrays

    def save(
        self,
        relative_key: str,
        metadata: Mapping[str, Any],
        arrays: Mapping[str, np.ndarray],
        *,
        durable: bool = True,
        prune_after: bool = True,
    ) -> Path:
        started_ns = time.perf_counter_ns()
        path = self._path(relative_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "__metadata__": self._metadata_array(metadata),
            **{
                str(name): np.asarray(value)
                for name, value in arrays.items()
                if value is not None
            },
        }
        temporary_name = ""
        with self._process_lock:
            try:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
                )
                with os.fdopen(descriptor, "wb") as handle:
                    np.savez_compressed(handle, **payload)
                    handle.flush()
                    if durable:
                        fsync_started_ns = time.perf_counter_ns()
                        os.fsync(handle.fileno())
                        self.stats.fsync_ns += (
                            time.perf_counter_ns() - fsync_started_ns
                        )
                os.replace(temporary_name, path)
                temporary_name = ""
                self.stats.writes += 1
                self._last_touch_ns[path] = time.monotonic_ns()
                if prune_after:
                    self.prune()
                self.stats.writes_ns += time.perf_counter_ns() - started_ns
                return path
            finally:
                if temporary_name:
                    try:
                        Path(temporary_name).unlink(missing_ok=True)
                    except OSError:
                        pass

    def prune(self) -> None:
        """Evict oldest NPZ files across results and tiles."""

        if self.budget_bytes <= 0:
            return
        started_ns = time.perf_counter_ns()
        with self._process_lock:
            entries: list[tuple[int, int, Path]] = []
            total = 0
            try:
                candidates = tuple(self.root.rglob("*.npz"))
            except OSError:
                return
            for path in candidates:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                size = int(stat.st_size)
                total += size
                entries.append((int(stat.st_mtime_ns), size, path))
            if total <= self.budget_bytes:
                self.stats.prunes += 1
                self.stats.prunes_ns += time.perf_counter_ns() - started_ns
                return
            for _mtime, size, path in sorted(entries, key=lambda item: item[0]):
                if total <= self.budget_bytes:
                    break
                try:
                    path.unlink()
                except OSError:
                    continue
                total -= size
                self.stats.evictions += 1
            self.stats.prunes += 1
            self.stats.prunes_ns += time.perf_counter_ns() - started_ns

    def metrics(self) -> dict[str, int]:
        return {
            "hits": int(self.stats.hits),
            "misses": int(self.stats.misses),
            "writes": int(self.stats.writes),
            "corrupt": int(self.stats.corrupt),
            "evictions": int(self.stats.evictions),
            "loads_ms": int(self.stats.loads_ns // 1_000_000),
            "writes_ms": int(self.stats.writes_ns // 1_000_000),
            "fsync_ms": int(self.stats.fsync_ns // 1_000_000),
            "prunes_ms": int(self.stats.prunes_ns // 1_000_000),
            "prunes": int(self.stats.prunes),
            "budget_bytes": int(self.budget_bytes),
            "timestamp_ns": time.time_ns(),
        }


__all__ = ["AtomicNpzStore", "NpzStoreStats"]
