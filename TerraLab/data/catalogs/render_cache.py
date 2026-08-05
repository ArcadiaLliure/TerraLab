"""Read-only star-catalogue handles for renderer-neutral scene planners."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from TerraLab.scene.contracts import CatalogResource
from TerraLab.scene.resources import StarCatalogResource
from TerraLab.util.color import bp_rp_to_rgb_arrays


_EMPTY_FLOAT = np.empty(0, dtype=np.float32)
_EMPTY_CHANNEL = np.empty(0, dtype=np.uint8)
for _array in (_EMPTY_FLOAT, _EMPTY_CHANNEL):
    _array.setflags(write=False)


@dataclass(frozen=True, slots=True)
class StarCatalogArrays:
    """Views over one catalogue version; no full catalogue copy is made."""

    resource: StarCatalogResource
    red: np.ndarray
    green: np.ndarray
    blue: np.ndarray

    @classmethod
    def empty(cls) -> "StarCatalogArrays":
        return cls(
            StarCatalogResource(_EMPTY_FLOAT, _EMPTY_FLOAT, _EMPTY_FLOAT),
            _EMPTY_CHANNEL,
            _EMPTY_CHANNEL,
            _EMPTY_CHANNEL,
        )


class StarCatalogRenderCache:
    """Own mmap lifetimes while exposing immutable array views to planners."""

    def __init__(self) -> None:
        self._key: tuple[str, str, str, str, str] | None = None
        self._handles: list[Any] = []
        self._arrays = StarCatalogArrays.empty()

    def close(self) -> None:
        handles, self._handles = self._handles, []
        self._key = None
        self._arrays = StarCatalogArrays.empty()
        for handle in handles:
            close = getattr(handle, "close", None)
            if callable(close):
                close()

    def load(self, artifact: CatalogResource) -> StarCatalogArrays:
        key = (
            str(artifact.catalog_path),
            str(artifact.r_path),
            str(artifact.g_path),
            str(artifact.b_path),
            str(artifact.version),
        )
        if key == self._key:
            return self._arrays
        self.close()
        catalog_path = Path(artifact.catalog_path) if artifact.catalog_path else None
        if catalog_path is None or not catalog_path.is_file():
            self._key = key
            return self._arrays
        loaded = np.load(catalog_path, mmap_mode="r", allow_pickle=False)
        self._handles.append(loaded)
        names = tuple(getattr(getattr(loaded, "dtype", None), "names", ()) or ())
        if names:
            ra = np.asarray(loaded["ra"])
            dec = np.asarray(loaded["dec"])
            magnitude = np.asarray(
                loaded["phot_g_mean_mag" if "phot_g_mean_mag" in names else "mag"]
            )
            bp_rp = (
                np.asarray(loaded["bp_rp"])
                if "bp_rp" in names
                else np.full(len(ra), 0.8, dtype=np.float32)
            )
        else:
            files = set(getattr(loaded, "files", ()))
            ra = np.asarray(loaded["ra" if "ra" in files else "RA"])
            dec = np.asarray(loaded["dec" if "dec" in files else "DEC"])
            magnitude = np.asarray(
                loaded["mag" if "mag" in files else "phot_g_mean_mag"]
            )
            bp_rp = (
                np.asarray(loaded["bp_rp"])
                if "bp_rp" in files
                else np.full(len(ra), 0.8, dtype=np.float32)
            )
        channels = self._load_channels(key[1:4], bp_rp)
        total = min(len(ra), len(dec), len(magnitude), len(bp_rp), *(len(channel) for channel in channels))
        self._arrays = StarCatalogArrays(
            StarCatalogResource(
                np.asarray(ra[:total]),
                np.asarray(dec[:total]),
                np.asarray(magnitude[:total]),
                np.asarray(bp_rp[:total]),
                version=artifact.version or "|".join(key[:4]),
                magnitude_sorted=False,
            ),
            np.asarray(channels[0][:total]),
            np.asarray(channels[1][:total]),
            np.asarray(channels[2][:total]),
        )
        self._key = key
        return self._arrays

    def _load_channels(
        self, paths: tuple[str, str, str], bp_rp: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        channels: list[np.ndarray] = []
        for raw_path in paths:
            path = Path(raw_path) if raw_path else None
            if path is None or not path.is_file():
                channels.clear()
                break
            channel = np.load(path, mmap_mode="r", allow_pickle=False)
            self._handles.append(channel)
            channels.append(np.asarray(channel))
        if len(channels) == 3:
            return channels[0], channels[1], channels[2]
        red, green, blue = bp_rp_to_rgb_arrays(bp_rp)
        return np.asarray(red), np.asarray(green), np.asarray(blue)
