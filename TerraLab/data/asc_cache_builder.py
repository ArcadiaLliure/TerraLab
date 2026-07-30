"""Offline, spawn-based materialisation of ESRI ASCII elevation tiles."""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

import numpy as np


def _read_asc_header(path: Path) -> tuple[dict[str, float], int]:
    header: dict[str, float] = {}
    header_lines = 0
    accepted = {
        "NCOLS",
        "NROWS",
        "XLLCORNER",
        "YLLCORNER",
        "XLLCENTER",
        "YLLCENTER",
        "CELLSIZE",
        "NODATA_VALUE",
        "DX",
        "DY",
    }
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for _ in range(12):
            line = handle.readline()
            if not line:
                break
            fields = line.split()
            if len(fields) < 2 or fields[0].upper() not in accepted:
                break
            header[fields[0].upper()] = float(fields[1])
            header_lines += 1
    if "NCOLS" not in header or "NROWS" not in header:
        raise ValueError(f"Not an ESRI ASCII grid: {path}")
    return header, header_lines


def materialize_asc_tile(path_value: str) -> dict[str, object]:
    """Convert one ASC tile atomically; intended as a process entry point."""

    path = Path(path_value).expanduser().resolve()
    output = path.with_suffix(".npy")
    if output.exists() and output.stat().st_mtime_ns >= path.stat().st_mtime_ns:
        return {"path": str(path), "output": str(output), "cached": True}
    header, header_lines = _read_asc_header(path)
    nrows = int(header["NROWS"])
    ncols = int(header["NCOLS"])
    nodata = float(header.get("NODATA_VALUE", -9999.0))
    raw = np.loadtxt(path, skiprows=header_lines, dtype=np.float32).reshape(-1)
    expected = nrows * ncols
    if raw.size < expected:
        raw = np.pad(raw, (0, expected - raw.size), constant_values=nodata)
    elif raw.size > expected:
        raw = raw[:expected]
    data = np.asarray(raw.reshape(nrows, ncols), dtype=np.float32)
    temporary = Path(str(output) + f".{os.getpid()}.tmp.npy")
    try:
        np.save(temporary, data, allow_pickle=False)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(path),
        "output": str(output),
        "cached": False,
        "bytes": int(data.nbytes),
    }


def materialize_asc_caches_spawned(
    paths: str | Path | Iterable[str | Path],
    *,
    max_workers: int | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
    abort_check: Callable[[], bool] | None = None,
) -> list[dict[str, object]]:
    """Materialize ASC caches with Windows-safe spawn and at most four workers."""

    roots = [paths] if isinstance(paths, (str, Path)) else list(paths)
    candidates: set[Path] = set()
    for raw in roots:
        path = Path(raw).expanduser().resolve()
        if path.is_file() and path.suffix.lower() in {".asc", ".txt"}:
            candidates.add(path)
        elif path.is_dir():
            candidates.update(
                item
                for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() in {".asc", ".txt"}
            )
    ordered = []
    for candidate in sorted(candidates, key=lambda item: str(item).casefold()):
        try:
            _read_asc_header(candidate)
        except (OSError, ValueError):
            continue
        ordered.append(candidate)
    if not ordered:
        return []
    workers = min(4, max(1, int(max_workers or (os.cpu_count() or 2) // 2)))
    results: list[dict[str, object]] = []
    context = mp.get_context("spawn")
    executor = ProcessPoolExecutor(max_workers=workers, mp_context=context)
    try:
        futures = {
            executor.submit(materialize_asc_tile, str(path)): path for path in ordered
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            if abort_check is not None and bool(abort_check()):
                raise InterruptedError("ASC cache materialisation cancelled")
            results.append(future.result())
            if progress_callback is not None:
                progress_callback(
                    completed * 100.0 / len(ordered),
                    f"ASC cache {completed}/{len(ordered)}",
                )
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize TerraLab ASC caches")
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args()
    results = materialize_asc_caches_spawned(
        args.paths,
        max_workers=args.workers,
        progress_callback=lambda percent, message: print(
            f"{percent:6.2f}% {message}", flush=True
        ),
    )
    print(f"Materialized {len(results)} ASC tiles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
