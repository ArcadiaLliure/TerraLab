"""Out-of-core star catalogue contracts and HEALPix/tile backends."""

from __future__ import annotations

import abc
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.cache import ByteLRU
from TerraLab.common.cancellation import GenerationToken
from TerraLab.common.performance.budget import DEFAULT_PERFORMANCE_BUDGET
from TerraLab.data.tile_manifest import TileManifest


@dataclass(frozen=True)
class StarBatch:
    """Immutable catalogue chunk; colour is derived later from the BP-RP LUT."""

    ra: np.ndarray
    dec: np.ndarray
    mag: np.ndarray
    bp_rp: np.ndarray
    source_id: np.ndarray

    def __post_init__(self) -> None:
        arrays = (
            np.asarray(self.ra, dtype=np.float64),
            np.asarray(self.dec, dtype=np.float64),
            np.asarray(self.mag, dtype=np.float32),
            np.asarray(self.bp_rp, dtype=np.float32),
            np.asarray(self.source_id, dtype=np.int64),
        )
        size = arrays[0].size
        if any(array.ndim != 1 or array.size != size for array in arrays):
            raise ValueError("Star batch columns must be aligned 1D arrays")
        for name, array in zip(
            ("ra", "dec", "mag", "bp_rp", "source_id"), arrays
        ):
            array.setflags(write=False)
            object.__setattr__(self, name, array)

    def __len__(self) -> int:
        return int(self.ra.size)

    @property
    def nbytes(self) -> int:
        return int(
            self.ra.nbytes
            + self.dec.nbytes
            + self.mag.nbytes
            + self.bp_rp.nbytes
            + self.source_id.nbytes
        )


class StarCatalogStore(abc.ABC):
    """Spatial star access that never requires a full catalogue in RAM."""

    @abc.abstractmethod
    def query_cone(
        self,
        ra_deg: float,
        dec_deg: float,
        radius_deg: float,
        mag_limit: float,
        *,
        max_batch_rows: int = 1_000_000,
        token: GenerationToken | None = None,
    ) -> Iterator[StarBatch]:
        pass

    @abc.abstractmethod
    def close(self) -> None:
        pass


def _catalog_columns(catalog: np.ndarray) -> tuple[str, str, str, str, str | None]:
    names = set(catalog.dtype.names or ())
    ra = "ra" if "ra" in names else "RA"
    dec = "dec" if "dec" in names else "DEC"
    mag = "phot_g_mean_mag" if "phot_g_mean_mag" in names else "mag"
    bp_rp = "bp_rp"
    source_id = "source_id" if "source_id" in names else None
    required = {ra, dec, mag, bp_rp}
    if not required.issubset(names):
        raise ValueError(f"Catalogue columns unavailable: {sorted(required - names)}")
    return ra, dec, mag, bp_rp, source_id


def _exact_cone_mask(
    ra: np.ndarray,
    dec: np.ndarray,
    ra_deg: float,
    dec_deg: float,
    radius_deg: float,
) -> np.ndarray:
    ra_rad = np.radians(np.asarray(ra, dtype=np.float64))
    dec_rad = np.radians(np.asarray(dec, dtype=np.float64))
    center_ra = math.radians(float(ra_deg))
    center_dec = math.radians(float(dec_deg))
    cos_sep = (
        np.sin(dec_rad) * math.sin(center_dec)
        + np.cos(dec_rad)
        * math.cos(center_dec)
        * np.cos(ra_rad - center_ra)
    )
    return cos_sep >= math.cos(math.radians(float(radius_deg)))


class HealpixStarCatalogStore(StarCatalogStore):
    """Memory-mapped v2 catalogue ordered by (pixel, magnitude, source_id)."""

    def __init__(self, catalog_path: str | Path, index_path: str | Path) -> None:
        from TerraLab.util.healpy_catalog_reader import _query_disc_pixels

        self._query_disc_pixels = _query_disc_pixels
        self.catalog_path = Path(catalog_path).expanduser().resolve()
        self.index_path = Path(index_path).expanduser().resolve()
        self._catalog = np.load(self.catalog_path, mmap_mode="r", allow_pickle=False)
        if not isinstance(self._catalog, np.ndarray) or self._catalog.dtype.names is None:
            raise ValueError("HEALPix catalogue must be a structured NPY array")
        self._columns = _catalog_columns(self._catalog)
        with np.load(self.index_path, allow_pickle=False) as index:
            self.pixels = np.asarray(index["pixels_unics"], dtype=np.uint32)
            self.offsets = np.asarray(index["inicis"], dtype=np.int64)
            self.counts = np.asarray(index["comptes"], dtype=np.int64)
            self.nside = int(np.asarray(index["nside"]).reshape(-1)[0])
            self.format_version = int(
                np.asarray(index.get("format_version", [1])).reshape(-1)[0]
            )
        if not (len(self.pixels) == len(self.offsets) == len(self.counts)):
            raise ValueError("HEALPix index arrays are inconsistent")
        self._closed = False
        self.last_query_metrics: dict[str, int] = {}

    def query_cone(
        self,
        ra_deg: float,
        dec_deg: float,
        radius_deg: float,
        mag_limit: float,
        *,
        max_batch_rows: int = 1_000_000,
        token: GenerationToken | None = None,
    ) -> Iterator[StarBatch]:
        if self._closed:
            raise RuntimeError("Star catalogue store is closed")
        radius = float(radius_deg)
        if not np.isfinite(radius) or radius <= 0.0:
            return
        max_rows = DEFAULT_PERFORMANCE_BUDGET.batch_rows(
            max(1, self._catalog.dtype.itemsize), max_batch_rows
        )
        self.last_query_metrics = {"cells": 0, "candidates": 0, "rows": 0, "batches": 0}
        query_pixels = np.unique(
            np.asarray(
                self._query_disc_pixels(
                    self.nside, float(ra_deg), float(dec_deg), radius
                ),
                dtype=np.uint32,
            )
        )
        positions = np.searchsorted(self.pixels, query_pixels)
        in_bounds = positions < self.pixels.size
        positions = positions[in_bounds]
        query_pixels = query_pixels[in_bounds]
        positions = positions[self.pixels[positions] == query_pixels]
        self.last_query_metrics["cells"] = int(positions.size)
        ra_name, dec_name, mag_name, bp_name, id_name = self._columns
        limit = float(mag_limit)
        for position in positions:
            if token is not None:
                token.raise_if_cancelled()
            start = int(self.offsets[position])
            stop = start + int(self.counts[position])
            if stop <= start:
                continue
            magnitudes = np.asarray(self._catalog[mag_name][start:stop])
            if self.format_version >= 2:
                stop = start + int(np.searchsorted(magnitudes, limit, side="right"))
            self.last_query_metrics["candidates"] += max(0, stop - start)
            for chunk_start in range(start, stop, max_rows):
                if token is not None:
                    token.raise_if_cancelled()
                chunk_stop = min(stop, chunk_start + max_rows)
                chunk = self._catalog[chunk_start:chunk_stop]
                if self.format_version < 2:
                    chunk = chunk[np.asarray(chunk[mag_name]) <= limit]
                if len(chunk) == 0:
                    continue
                exact = _exact_cone_mask(
                    chunk[ra_name], chunk[dec_name], ra_deg, dec_deg, radius
                )
                if not np.any(exact):
                    continue
                source_ids = (
                    np.asarray(chunk[id_name][exact], dtype=np.int64)
                    if id_name is not None
                    else np.full(int(np.count_nonzero(exact)), -1, dtype=np.int64)
                )
                self.last_query_metrics["rows"] += int(np.count_nonzero(exact))
                self.last_query_metrics["batches"] += 1
                yield StarBatch(
                    chunk[ra_name][exact],
                    chunk[dec_name][exact],
                    chunk[mag_name][exact],
                    chunk[bp_name][exact],
                    source_ids,
                )

    def close(self) -> None:
        if self._closed:
            return
        try:
            mmap = getattr(self._catalog, "_mmap", None)
            if mmap is not None:
                mmap.close()
        except Exception:
            log_suppressed_exception(__name__, "HealpixStarCatalogStore.close")
        self._closed = True


class TileStarCatalogStore(StarCatalogStore):
    """Compatible 5-degree NPZ fallback with byte-bounded tile caching."""

    def __init__(self, manifest_path: str | Path) -> None:
        self.manifest = TileManifest()
        self.manifest.load(Path(manifest_path))
        self._cache = ByteLRU(DEFAULT_PERFORMANCE_BUDGET.stars_bytes)

    @staticmethod
    def _load_tile(path: Path) -> dict[str, np.ndarray]:
        with np.load(path, allow_pickle=False) as data:
            ra = np.asarray(data.get("ra", data.get("RA", [])), dtype=np.float64)
            dec = np.asarray(data.get("dec", data.get("DEC", [])), dtype=np.float64)
            mag = np.asarray(
                data.get("phot_g_mean_mag", data.get("mag", [])), dtype=np.float32
            )
            bp_rp = np.asarray(
                data.get("bp_rp", np.full(len(ra), 0.8, dtype=np.float32)),
                dtype=np.float32,
            )
            source_id = np.asarray(
                data.get("source_id", np.full(len(ra), -1, dtype=np.int64)),
                dtype=np.int64,
            )
        size = min(len(ra), len(dec), len(mag), len(bp_rp), len(source_id))
        return {
            "ra": ra[:size],
            "dec": dec[:size],
            "mag": mag[:size],
            "bp_rp": bp_rp[:size],
            "source_id": source_id[:size],
        }

    def _tile(self, path: Path) -> dict[str, np.ndarray]:
        key = str(path)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        arrays = self._load_tile(path)
        self._cache.put(key, arrays, sum(value.nbytes for value in arrays.values()))
        return arrays

    def query_cone(
        self,
        ra_deg: float,
        dec_deg: float,
        radius_deg: float,
        mag_limit: float,
        *,
        max_batch_rows: int = 1_000_000,
        token: GenerationToken | None = None,
    ) -> Iterator[StarBatch]:
        entries = [self.manifest.get_general_tile()]
        entries.extend(
            self.manifest.get_tiles_for_region(ra_deg, dec_deg, radius_deg)
        )
        max_rows = DEFAULT_PERFORMANCE_BUDGET.batch_rows(28, max_batch_rows)
        seen_positive = np.empty(0, dtype=np.int64)
        for entry in entries:
            if token is not None:
                token.raise_if_cancelled()
            arrays = self._tile(entry.file_path)
            finite = (
                np.isfinite(arrays["ra"])
                & np.isfinite(arrays["dec"])
                & np.isfinite(arrays["mag"])
                & (arrays["mag"] <= float(mag_limit))
            )
            exact = finite & _exact_cone_mask(
                arrays["ra"], arrays["dec"], ra_deg, dec_deg, radius_deg
            )
            indices = np.flatnonzero(exact)
            # Tile overlaps are small.  Deduplicate only selected rows and keep
            # negative synthetic IDs, avoiding a full-catalog Python set.
            if seen_positive.size and indices.size:
                ids = arrays["source_id"][indices]
                keep = (ids <= 0) | ~np.isin(ids, seen_positive, assume_unique=False)
                indices = indices[keep]
            if indices.size:
                positive = arrays["source_id"][indices]
                positive = positive[positive > 0]
                seen_positive = np.unique(
                    np.concatenate((seen_positive, positive))
                )
            for start in range(0, indices.size, max_rows):
                chosen = indices[start : start + max_rows]
                if chosen.size:
                    yield StarBatch(
                        arrays["ra"][chosen],
                        arrays["dec"][chosen],
                        arrays["mag"][chosen],
                        arrays["bp_rp"][chosen],
                        arrays["source_id"][chosen],
                    )

    def close(self) -> None:
        self._cache.clear()


def create_star_catalog_store(
    path: str | Path, *, prefer_healpix: bool = True
) -> StarCatalogStore:
    """Prefer a complete HEALPix v2 pair, then retain tile compatibility."""

    candidate = Path(path).expanduser().resolve()
    base = candidate.parent if candidate.is_file() else candidate
    if candidate.suffix == ".npy":
        catalog = candidate
    else:
        catalog = base / "stars_catalog_healpy.npy"
    index = catalog.with_suffix(".idx.npz")
    if prefer_healpix and catalog.exists() and index.exists():
        return HealpixStarCatalogStore(catalog, index)
    manifest = candidate if candidate.name == "tile_manifest.json" else base / "tile_manifest.json"
    if manifest.exists():
        return TileStarCatalogStore(manifest)
    raise FileNotFoundError("No complete HEALPix index or tile manifest is available")


__all__ = [
    "StarBatch",
    "StarCatalogStore",
    "HealpixStarCatalogStore",
    "TileStarCatalogStore",
    "create_star_catalog_store",
]
