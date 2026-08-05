"""Pure star selection, photometry, projection, and renderer-neutral batches."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

import numpy as np

from TerraLab.scene.projection import (
    project_universal_stereo_numpy,
    radec_to_altaz_numpy,
)
from TerraLab.scene.resources import StarCatalogResource
from TerraLab.util.color import color_from_bp_rp
from TerraLab.util.math2d import clamp


def _readonly(values: np.ndarray) -> np.ndarray:
    values.setflags(write=False)
    return values


def _empty(dtype: np.dtype) -> np.ndarray:
    return _readonly(np.empty(0, dtype=dtype))


@dataclass(frozen=True, slots=True)
class StarPickBatch:
    """Stable catalogue identities and projected centres for one generation."""

    catalog_indices: np.ndarray
    screen_x: np.ndarray
    screen_y: np.ndarray
    altitude_deg: np.ndarray = field(
        default_factory=lambda: _empty(np.dtype(np.float32))
    )
    azimuth_deg: np.ndarray = field(
        default_factory=lambda: _empty(np.dtype(np.float32))
    )
    magnitude: np.ndarray = field(
        default_factory=lambda: _empty(np.dtype(np.float32))
    )


@dataclass(frozen=True, slots=True)
class StarSpriteBatch:
    """Resolved point/sprite data with no dependency on a graphics API."""

    screen_x: np.ndarray
    screen_y: np.ndarray
    base_rgba: np.ndarray
    medium_rgba: np.ndarray
    weak_rgba: np.ndarray
    medium_style_key: np.ndarray
    weak_style_key: np.ndarray
    medium_radius_tenths: np.ndarray
    halo_bin: np.ndarray
    size_bin: np.ndarray
    weak_indices: np.ndarray
    medium_indices: np.ndarray
    bright_indices: np.ndarray
    smooth: bool


@dataclass(frozen=True, slots=True)
class StarScenePlan:
    """All star decisions required by a renderer for one immutable frame."""

    sprites: StarSpriteBatch
    picks: StarPickBatch
    total_in_view: int
    after_magnitude_cut: int
    after_bucket: int
    average_radius: float
    counters: Mapping[str, float]

    @property
    def visible_count(self) -> int:
        return int(len(self.picks.catalog_indices))


def build_scope_spatial_index_payload(
    ra_all: np.ndarray,
    dec_all: np.ndarray,
    mag_all: np.ndarray | None = None,
    max_mag: float | None = None,
    *,
    ra_bins: int = 360,
    dec_bins: int = 180,
    chunk_size: int = 1_000_000,
) -> tuple[np.ndarray, np.ndarray]:
    """Create a stable RA/Dec tile index without copying the source catalogue."""

    total = int(len(ra_all))
    tiles = int(ra_bins * dec_bins)
    if total <= 0:
        return _empty(np.dtype(np.int32)), _readonly(
            np.zeros(tiles + 1, dtype=np.int64)
        )
    use_cap = max_mag is not None and mag_all is not None
    counts = np.zeros(tiles, dtype=np.int64)
    valid_total = 0
    for start in range(0, total, chunk_size):
        end = min(total, start + chunk_size)
        ra = np.asarray(ra_all[start:end], dtype=np.float32)
        dec = np.asarray(dec_all[start:end], dtype=np.float32)
        valid = np.isfinite(ra) & np.isfinite(dec)
        if use_cap:
            mag = np.asarray(mag_all[start:end], dtype=np.float32)
            valid &= np.isfinite(mag) & (mag <= float(max_mag) + 1e-6)
        if not np.any(valid):
            continue
        ra_bin = np.asarray(np.floor(np.mod(ra[valid], 360.0)), dtype=np.int32)
        dec_bin = np.asarray(
            np.floor(np.clip(dec[valid], -90.0, 89.99999) + 90.0),
            dtype=np.int32,
        )
        tile_ids = np.asarray(dec_bin * ra_bins + ra_bin, dtype=np.int32)
        counts += np.bincount(tile_ids, minlength=tiles)
        valid_total += int(len(tile_ids))
    offsets = np.empty(tiles + 1, dtype=np.int64)
    offsets[0] = 0
    np.cumsum(counts, out=offsets[1:])
    indices = np.empty(valid_total, dtype=np.int32)
    cursor = offsets[:-1].copy()
    for start in range(0, total, chunk_size):
        end = min(total, start + chunk_size)
        ra = np.asarray(ra_all[start:end], dtype=np.float32)
        dec = np.asarray(dec_all[start:end], dtype=np.float32)
        valid = np.isfinite(ra) & np.isfinite(dec)
        if use_cap:
            mag = np.asarray(mag_all[start:end], dtype=np.float32)
            valid &= np.isfinite(mag) & (mag <= float(max_mag) + 1e-6)
        if not np.any(valid):
            continue
        raw_indices = np.arange(start, end, dtype=np.int32)[valid]
        ra_bin = np.asarray(np.floor(np.mod(ra[valid], 360.0)), dtype=np.int32)
        dec_bin = np.asarray(
            np.floor(np.clip(dec[valid], -90.0, 89.99999) + 90.0),
            dtype=np.int32,
        )
        tile_ids = np.asarray(dec_bin * ra_bins + ra_bin, dtype=np.int32)
        order = np.argsort(tile_ids, kind="mergesort")
        tile_ids = tile_ids[order]
        raw_indices = raw_indices[order]
        unique, first = np.unique(tile_ids, return_index=True)
        ends = np.append(first[1:], len(tile_ids))
        for tile, lo, hi in zip(unique, first, ends):
            destination = int(cursor[int(tile)])
            count = int(hi - lo)
            indices[destination : destination + count] = raw_indices[lo:hi]
            cursor[int(tile)] += count
    return _readonly(indices), _readonly(offsets)


class StarScenePlanner:
    """Build star batches from a `RenderState` without Qt or widget imports."""

    def __init__(self) -> None:
        self._mag_index_key: tuple[object, ...] | None = None
        self._mag_sorted: np.ndarray | None = None
        self._mag_order: np.ndarray | None = None
        self._altaz_key: tuple[object, ...] | None = None
        self._altaz_alt: np.ndarray | None = None
        self._altaz_az: np.ndarray | None = None
        self._scope_index_key: tuple[object, ...] | None = None
        self._scope_indices: np.ndarray | None = None
        self._scope_offsets: np.ndarray | None = None
        self._scope_pending_key: tuple[object, ...] | None = None
        self._scope_query_key: tuple[object, ...] | None = None
        self._scope_query_indices: np.ndarray | None = None
        self._scope_query_tile_count = 0
        self._scope_query_candidate_count = 0
        self._rgb_palette_cache: dict[bool, np.ndarray] = {}

    @staticmethod
    def _array_key(*arrays: np.ndarray | None) -> tuple[object, ...]:
        key: list[object] = []
        for array in arrays:
            if array is None:
                key.append(("none", 0))
                continue
            values = np.asarray(array)
            key.append(
                (int(values.__array_interface__["data"][0]), len(values))
            )
        return tuple(key)

    @staticmethod
    def _empty_plan() -> StarScenePlan:
        empty_i = _empty(np.dtype(np.int32))
        empty_f = _empty(np.dtype(np.float32))
        sprites = StarSpriteBatch(
            screen_x=empty_f,
            screen_y=empty_f,
            base_rgba=_readonly(np.empty((0, 4), dtype=np.uint8)),
            medium_rgba=_readonly(np.empty((0, 4), dtype=np.uint8)),
            weak_rgba=_readonly(np.empty((0, 4), dtype=np.uint8)),
            medium_style_key=empty_i,
            weak_style_key=empty_i,
            medium_radius_tenths=empty_i,
            halo_bin=empty_i,
            size_bin=empty_i,
            weak_indices=empty_i,
            medium_indices=empty_i,
            bright_indices=empty_i,
            smooth=True,
        )
        return StarScenePlan(
            sprites=sprites,
            picks=StarPickBatch(empty_i, empty_f, empty_f),
            total_in_view=0,
            after_magnitude_cut=0,
            after_bucket=0,
            average_radius=0.0,
            counters=MappingProxyType({}),
        )

    def _magnitude_indices(
        self,
        ra: np.ndarray,
        dec: np.ndarray,
        magnitude: np.ndarray,
        limit: float,
        *,
        sorted_by_magnitude: bool,
        max_count: int | None = None,
    ) -> np.ndarray:
        if sorted_by_magnitude:
            upper = int(np.searchsorted(magnitude, limit, side="right"))
            if max_count is not None:
                upper = min(upper, int(max_count))
            return np.arange(max(0, upper), dtype=np.int32)
        key = self._array_key(ra, dec, magnitude)
        if self._mag_index_key != key:
            valid = np.isfinite(ra) & np.isfinite(dec) & np.isfinite(magnitude)
            indices = np.where(valid)[0].astype(np.int32, copy=False)
            mags = np.asarray(magnitude[indices], dtype=np.float32)
            order = np.argsort(mags, kind="mergesort")
            self._mag_index_key = key
            self._mag_sorted = mags[order]
            self._mag_order = indices[order]
        assert self._mag_sorted is not None and self._mag_order is not None
        upper = int(np.searchsorted(self._mag_sorted, limit, side="right"))
        if max_count is not None:
            upper = min(upper, int(max_count))
        return np.asarray(self._mag_order[: max(0, upper)], dtype=np.int32)

    @staticmethod
    def _cap_candidates_by_brightness(
        indices: np.ndarray,
        magnitude: np.ndarray,
        maximum: int,
        *,
        already_sorted: bool,
    ) -> np.ndarray:
        """Keep the brightest candidates without materialising catalogue rows."""

        if len(indices) <= maximum:
            return indices
        if already_sorted:
            return np.asarray(indices[:maximum], dtype=np.int32)
        values = np.asarray(magnitude[indices], dtype=np.float32)
        selected = np.argpartition(values, maximum - 1)[:maximum]
        order = np.argsort(values[selected], kind="mergesort")
        return np.asarray(indices[selected[order]], dtype=np.int32)

    def begin_scope_index_warmup(
        self, ra: np.ndarray, dec: np.ndarray
    ) -> tuple[object, ...]:
        """Mark an asynchronously built scope index as pending for this catalog."""

        key = self._array_key(ra, dec)
        self._scope_pending_key = key
        if self._scope_index_key != key:
            self._scope_indices = None
            self._scope_offsets = None
            self._scope_query_key = None
            self._scope_query_indices = None
        return key

    def apply_scope_spatial_index_payload(
        self,
        key: tuple[object, ...],
        sorted_indices: np.ndarray,
        offsets: np.ndarray,
    ) -> None:
        """Publish an index built by a worker for the matching catalog identity."""

        self._scope_index_key = key
        self._scope_indices = _readonly(
            np.asarray(sorted_indices, dtype=np.int32)
        )
        self._scope_offsets = _readonly(np.asarray(offsets, dtype=np.int64))
        self._scope_query_key = None
        self._scope_query_indices = None
        if self._scope_pending_key == key:
            self._scope_pending_key = None

    def clear_scope_index_warmup(
        self, key: tuple[object, ...] | None = None
    ) -> None:
        if key is None or self._scope_pending_key == key:
            self._scope_pending_key = None

    def _ensure_scope_spatial_index(
        self,
        ra: np.ndarray,
        dec: np.ndarray,
        *,
        allow_sync_build: bool,
    ) -> tuple[np.ndarray | None, np.ndarray | None]:
        key = self._array_key(ra, dec)
        if (
            self._scope_index_key == key
            and self._scope_indices is not None
            and self._scope_offsets is not None
        ):
            return self._scope_indices, self._scope_offsets
        if self._scope_pending_key == key or not allow_sync_build:
            return None, None
        indices, offsets = build_scope_spatial_index_payload(ra, dec)
        self._scope_index_key = key
        self._scope_indices = indices
        self._scope_offsets = offsets
        self._scope_query_key = None
        self._scope_query_indices = None
        return indices, offsets

    @staticmethod
    def _scope_window_tile_ids(
        center_ra: float,
        center_dec: float,
        ra_pad: float,
        dec_pad: float,
        *,
        ra_bins: int = 360,
        dec_bins: int = 180,
    ) -> np.ndarray:
        dec_min = max(-90.0, float(center_dec) - float(dec_pad))
        dec_max = min(90.0, float(center_dec) + float(dec_pad))
        dec_first = max(0, min(dec_bins - 1, int(math.floor(dec_min + 90.0))))
        dec_last = max(
            0,
            min(
                dec_bins - 1,
                int(math.floor(max(dec_min, dec_max - 1e-6) + 90.0)),
            ),
        )
        if float(ra_pad) >= 179.999:
            ra_ranges = ((0, ra_bins - 1),)
        else:
            ra_min = (float(center_ra) - float(ra_pad)) % 360.0
            ra_max = (float(center_ra) + float(ra_pad)) % 360.0
            end_bin = max(
                0,
                min(ra_bins - 1, int(math.floor(max(ra_min, ra_max - 1e-6)))),
            )
            if ra_min <= ra_max:
                ra_ranges = (
                    (
                        max(0, min(ra_bins - 1, int(math.floor(ra_min)))),
                        end_bin,
                    ),
                )
            else:
                ra_ranges = (
                    (
                        0,
                        max(
                            0,
                            min(
                                ra_bins - 1,
                                int(math.floor(max(0.0, ra_max - 1e-6))),
                            ),
                        ),
                    ),
                    (
                        max(0, min(ra_bins - 1, int(math.floor(ra_min)))),
                        ra_bins - 1,
                    ),
                )
        tiles: list[int] = []
        for dec_bin in range(dec_first, dec_last + 1):
            row = dec_bin * ra_bins
            for ra_first, ra_last in ra_ranges:
                tiles.extend(range(ra_first + row, ra_last + row + 1))
        return np.asarray(tiles, dtype=np.int32)

    def _scope_indices_for_window(
        self,
        ra: np.ndarray,
        dec: np.ndarray,
        magnitude: np.ndarray,
        *,
        center_ra: float,
        center_dec: float,
        ra_pad: float,
        dec_pad: float,
        pre_limit: float,
        interaction_active: bool,
        allow_sync_build: bool,
    ) -> tuple[np.ndarray | None, int, int]:
        """Use a wrap-safe scope window before expensive Alt/Az projection."""

        sorted_indices, offsets = self._ensure_scope_spatial_index(
            ra, dec, allow_sync_build=allow_sync_build
        )
        if sorted_indices is None or offsets is None:
            return None, 0, 0
        tile_pad = 1.5 + (0.6 if interaction_active else 0.0)
        tiles = self._scope_window_tile_ids(
            center_ra,
            center_dec,
            min(180.0, float(ra_pad) + tile_pad),
            min(90.0, float(dec_pad) + tile_pad),
        )
        if len(tiles) == 0:
            return _empty(np.dtype(np.int32)), 0, 0
        query_key = (self._scope_index_key, tuple(int(tile) for tile in tiles))
        if (
            self._scope_query_key == query_key
            and self._scope_query_indices is not None
        ):
            candidates = self._scope_query_indices
            candidate_count = self._scope_query_candidate_count
        else:
            candidate_count = int(
                sum(
                    int(offsets[int(tile) + 1] - offsets[int(tile)])
                    for tile in tiles
                )
            )
            if candidate_count == 0:
                self._scope_query_key = query_key
                self._scope_query_indices = _empty(np.dtype(np.int32))
                self._scope_query_tile_count = int(len(tiles))
                self._scope_query_candidate_count = 0
                return self._scope_query_indices, int(len(tiles)), 0
            candidates = np.empty(candidate_count, dtype=np.int32)
            destination = 0
            for tile in tiles:
                start = int(offsets[int(tile)])
                end = int(offsets[int(tile) + 1])
                segment = sorted_indices[start:end]
                candidates[destination : destination + len(segment)] = segment
                destination += len(segment)
            candidates = _readonly(
                np.asarray(candidates[:destination], dtype=np.int32)
            )
            candidate_count = int(len(candidates))
            self._scope_query_key = query_key
            self._scope_query_indices = candidates
            self._scope_query_tile_count = int(len(tiles))
            self._scope_query_candidate_count = candidate_count
        ra_sub = np.asarray(ra[candidates], dtype=np.float32)
        dec_sub = np.asarray(dec[candidates], dtype=np.float32)
        magnitude_sub = np.asarray(magnitude[candidates], dtype=np.float32)
        dra = np.abs(((ra_sub - float(center_ra) + 180.0) % 360.0) - 180.0)
        mask = (
            np.isfinite(ra_sub)
            & np.isfinite(dec_sub)
            & np.isfinite(magnitude_sub)
            & (dra <= float(ra_pad))
            & (np.abs(dec_sub - float(center_dec)) <= float(dec_pad))
            & (magnitude_sub <= float(pre_limit))
        )
        return (
            np.asarray(candidates[mask], dtype=np.int32),
            int(len(tiles)),
            int(candidate_count),
        )

    def _altaz(
        self,
        ra: np.ndarray,
        dec: np.ndarray,
        indices: np.ndarray,
        state,
    ) -> tuple[np.ndarray, np.ndarray]:
        interaction = bool(state.interaction_active)
        factor = 4.0 if interaction else 1.0
        key = (
            self._array_key(ra, dec, indices),
            int(round(float(state.latitude) * 1000.0)),
            int(round(float(state.longitude) * 1000.0)),
            int(state.day_of_year_utc),
            int(state.year_utc),
            int(round(float(state.ut_hour) * 3600.0 * factor)),
        )
        if self._altaz_key == key and self._altaz_alt is not None:
            return self._altaz_alt, self._altaz_az
        altitude, azimuth = radec_to_altaz_numpy(
            ra[indices],
            dec[indices],
            latitude_deg=float(state.latitude),
            longitude_deg=float(state.longitude),
            ut_hour=float(state.ut_hour),
            day_of_year=int(state.day_of_year_utc),
            year=int(state.year_utc),
        )
        self._altaz_key = key
        self._altaz_alt = np.asarray(altitude, dtype=np.float32)
        self._altaz_az = np.asarray(azimuth, dtype=np.float32)
        return self._altaz_alt, self._altaz_az

    @staticmethod
    def _limiting_magnitude(state) -> float:
        extras = state.extras if isinstance(state.extras, Mapping) else {}
        spike_bias = (float(state.spike_magnitude_threshold) - 2.0) * 0.08
        if state.scope_enabled:
            penalty = clamp(
                float(extras.get("scope_fov_penalty_mag", 0.0)) * 0.35,
                0.0,
                2.8,
            )
            limit = (
                float(extras.get("scope_limit_mag", state.mag_limit))
                + spike_bias
                - penalty
            )
            dataset_cap = extras.get("scope_dataset_max_mag")
            cap = (
                min(21.5, float(dataset_cap) + 0.15)
                if dataset_cap is not None
                else 21.5
            )
            cap = max(8.0, cap)
        else:
            limit = float(state.mag_limit) + spike_bias
            cap = 13.5
        if float(state.sun_alt) > 0.0:
            limit = min(limit, -4.0)
        if bool(extras.get("scope_force_naked_eye_until_fix", False)):
            limit = min(
                limit,
                float(
                    extras.get("scope_first_fix_mag_cap", state.naked_eye_cap)
                ),
            )
        return float(clamp(limit, -12.0, cap))

    def _rgb_for_bin(
        self, bp_bin: np.ndarray, pure_colors: bool
    ) -> np.ndarray:
        palette = self._rgb_palette_cache.get(pure_colors)
        if palette is None:
            palette = np.empty((71, 3), dtype=np.uint8)
            for index in range(71):
                bp = index / 10.0 - 0.5
                red, green, blue = color_from_bp_rp(
                    bp,
                    pure_colors=pure_colors,
                    desaturate_mix=0.0 if pure_colors else 0.34,
                )
                luminance = (float(red) + float(green) + float(blue)) / 3.0
                boost = 1.22 if pure_colors else 1.10
                palette[index] = (
                    int(
                        clamp(
                            round(luminance + (red - luminance) * boost),
                            0.0,
                            255.0,
                        )
                    ),
                    int(
                        clamp(
                            round(luminance + (green - luminance) * boost),
                            0.0,
                            255.0,
                        )
                    ),
                    int(
                        clamp(
                            round(luminance + (blue - luminance) * boost),
                            0.0,
                            255.0,
                        )
                    ),
                )
            palette.setflags(write=False)
            self._rgb_palette_cache[pure_colors] = palette
        return palette[np.asarray(bp_bin, dtype=np.int16)]

    def build(self, state, *, width: int, height: int) -> StarScenePlan:
        extras = state.extras if isinstance(state.extras, Mapping) else {}
        if not bool(extras.get("stars_enabled", True)):
            return self._empty_plan()
        if state.ra is None or state.dec is None or state.mag is None:
            return self._empty_plan()
        resource = extras.get("star_catalog_resource")
        if not isinstance(resource, StarCatalogResource):
            resource = StarCatalogResource(
                ra_deg=np.asarray(state.ra),
                dec_deg=np.asarray(state.dec),
                magnitude=np.asarray(state.mag),
                bp_rp=getattr(state, "bp_rp", None),
            )
        ra = resource.ra_deg
        dec = resource.dec_deg
        magnitude_all = resource.magnitude
        if (
            len(ra) == 0
            or len(dec) != len(ra)
            or len(magnitude_all) != len(ra)
        ):
            return self._empty_plan()
        raw_bp_rp = resource.bp_rp
        bp_rp_all = (
            np.asarray(raw_bp_rp)
            if raw_bp_rp is not None and len(raw_bp_rp) == len(ra)
            else None
        )
        limiting_mag = self._limiting_magnitude(state)
        scope = bool(state.scope_enabled)
        pre_limit = limiting_mag + (1.0 if scope else 2.0)
        if scope and extras.get("scope_dataset_max_mag") is not None:
            pre_limit = min(
                pre_limit, float(extras["scope_dataset_max_mag"]) + 0.25
            )
        if bool(extras.get("scope_force_naked_eye_until_fix", False)):
            pre_limit = min(
                pre_limit,
                float(
                    extras.get("scope_first_fix_mag_cap", state.naked_eye_cap)
                ),
            )
        sorted_by_magnitude = bool(extras.get("catalog_mag_sorted", False))
        center_ra = extras.get("scope_center_ra_deg")
        center_dec = extras.get("scope_center_dec_deg")
        ra_pad = extras.get("scope_preselect_ra_pad_deg")
        dec_pad = extras.get("scope_preselect_dec_pad_deg")
        scope_prefilter = False
        scope_prefilter_pending = False
        scope_tile_count = 0
        scope_tile_candidates = 0
        interaction_active = bool(state.interaction_active)
        has_scope_window = scope and None not in (
            center_ra,
            center_dec,
            ra_pad,
            dec_pad,
        )
        if has_scope_window and len(ra) >= 250_000:
            indices, scope_tile_count, scope_tile_candidates = (
                self._scope_indices_for_window(
                    ra,
                    dec,
                    magnitude_all,
                    center_ra=float(center_ra),
                    center_dec=float(center_dec),
                    ra_pad=float(ra_pad),
                    dec_pad=float(dec_pad),
                    pre_limit=pre_limit,
                    interaction_active=interaction_active,
                    allow_sync_build=bool(
                        extras.get("scope_allow_sync_index_build", False)
                    ),
                )
            )
            if indices is None:
                scope_prefilter_pending = True
                pending_limit = int(
                    max(
                        5_000,
                        int(
                            extras.get(
                                "scope_pending_prefilter_cap_interaction"
                                if interaction_active
                                else "scope_pending_prefilter_cap_static",
                                40_000 if interaction_active else 70_000,
                            )
                        ),
                    )
                )
                indices = self._magnitude_indices(
                    ra,
                    dec,
                    magnitude_all,
                    pre_limit,
                    sorted_by_magnitude=sorted_by_magnitude,
                    max_count=pending_limit,
                )
            else:
                scope_prefilter = True
        else:
            indices = self._magnitude_indices(
                ra,
                dec,
                magnitude_all,
                pre_limit,
                sorted_by_magnitude=sorted_by_magnitude,
            )
        if len(indices) == 0:
            return self._empty_plan()

        scope_candidate_cap = 0
        if scope:
            scope_candidate_cap = int(
                max(
                    5_000 if scope_prefilter_pending else 10_000,
                    int(
                        extras.get(
                            "scope_pre_altaz_max_candidates_pending"
                            if scope_prefilter_pending
                            else (
                                "scope_pre_altaz_max_candidates_interaction"
                                if interaction_active
                                else "scope_pre_altaz_max_candidates_static"
                            ),
                            60_000
                            if scope_prefilter_pending
                            else (120_000 if interaction_active else 220_000),
                        )
                    ),
                )
            )
            if len(indices) > scope_candidate_cap:
                indices = self._cap_candidates_by_brightness(
                    indices,
                    magnitude_all,
                    scope_candidate_cap,
                    already_sorted=scope_prefilter_pending
                    and sorted_by_magnitude,
                )

        altitude, azimuth = self._altaz(ra, dec, indices, state)
        magnitude = np.asarray(magnitude_all[indices], dtype=np.float32)
        bp_rp = (
            np.asarray(bp_rp_all[indices], dtype=np.float32)
            if bp_rp_all is not None
            else np.full(len(indices), 0.8, dtype=np.float32)
        )
        if has_scope_window and not scope_prefilter:
            threshold = int(
                extras.get("scope_window_prefilter_threshold", 120_000)
            )
            if len(indices) > max(20_000, threshold):
                center_ra_f = float(center_ra) % 360.0
                center_dec_f = float(center_dec)
                ra_pad_f = max(0.0, float(ra_pad)) + (
                    0.6 if interaction_active else 0.0
                )
                dec_pad_f = max(0.0, float(dec_pad)) + (
                    0.6 if interaction_active else 0.0
                )
                ra_sub = np.asarray(ra[indices], dtype=np.float32)
                dec_sub = np.asarray(dec[indices], dtype=np.float32)
                delta_ra = np.abs(
                    ((ra_sub - center_ra_f + 180.0) % 360.0) - 180.0
                )
                window = (
                    np.isfinite(ra_sub)
                    & np.isfinite(dec_sub)
                    & (delta_ra <= ra_pad_f)
                    & (np.abs(dec_sub - center_dec_f) <= dec_pad_f)
                )
                if not np.any(window):
                    return self._empty_plan()
                indices = indices[window]
                altitude = altitude[window]
                azimuth = azimuth[window]
                magnitude = magnitude[window]
                bp_rp = bp_rp[window]
        if scope and callable(getattr(state, "scope_mask_fn", None)):
            try:
                inside_scope = np.asarray(
                    state.scope_mask_fn(altitude, azimuth), dtype=bool
                )
            except Exception:
                inside_scope = None
            if (
                inside_scope is not None
                and inside_scope.shape[0] == altitude.shape[0]
            ):
                if not np.any(inside_scope):
                    return self._empty_plan()
                indices = indices[inside_scope]
                altitude = altitude[inside_scope]
                azimuth = azimuth[inside_scope]
                magnitude = magnitude[inside_scope]
                bp_rp = bp_rp[inside_scope]
        sx_all, sy_all, valid = project_universal_stereo_numpy(
            altitude,
            azimuth,
            width=int(width),
            height=int(height),
            camera=state.camera,
        )
        bounds = (
            valid
            & np.isfinite(sx_all)
            & np.isfinite(sy_all)
            & (sx_all >= -30.0)
            & (sx_all <= float(width) + 30.0)
            & (sy_all >= -30.0)
            & (sy_all <= float(height) + 30.0)
        )
        if not np.any(bounds):
            return self._empty_plan()
        indices = indices[bounds]
        altitude = altitude[bounds]
        azimuth = azimuth[bounds]
        sx = np.asarray(sx_all[bounds], dtype=np.float32)
        sy = np.asarray(sy_all[bounds], dtype=np.float32)
        magnitude = magnitude[bounds]
        bp_rp = bp_rp[bounds]
        total_in_view = int(len(indices))
        visible = magnitude <= limiting_mag
        if not np.any(visible):
            return StarScenePlan(
                sprites=self._empty_plan().sprites,
                picks=self._empty_plan().picks,
                total_in_view=total_in_view,
                after_magnitude_cut=0,
                after_bucket=0,
                average_radius=0.0,
                counters=MappingProxyType(
                    {"total_in_view": float(total_in_view)}
                ),
            )
        indices, altitude, azimuth, sx, sy, magnitude, bp_rp = (
            indices[visible],
            altitude[visible],
            azimuth[visible],
            sx[visible],
            sy[visible],
            magnitude[visible],
            bp_rp[visible],
        )
        after_magnitude_cut = int(len(indices))
        disable_scope_bucket = scope and len(magnitude) <= int(
            max(
                5_000,
                int(extras.get("scope_disable_bucket_max_points", 30_000)),
            )
        )
        if not disable_scope_bucket:
            px = np.asarray(sx, dtype=np.int32)
            py = np.asarray(sy, dtype=np.int32)
            order = np.argsort(magnitude, kind="mergesort")
            keys = (py * max(1, int(width)) + px)[order]
            _, first = np.unique(keys, return_index=True)
            keep = order[first]
            keep = keep[np.argsort(magnitude[keep], kind="mergesort")]
            indices, altitude, azimuth, sx, sy, magnitude, bp_rp = (
                indices[keep],
                altitude[keep],
                azimuth[keep],
                sx[keep],
                sy[keep],
                magnitude[keep],
                bp_rp[keep],
            )
        after_bucket = int(len(indices))
        pure_colors = bool(state.pure_colors)
        scope_profile = str(
            extras.get("scope_instrument_profile", "telescope")
        )
        scope_camera = scope and scope_profile.startswith("camera")
        iso_factor = float(
            clamp(extras.get("scope_iso_factor", 1.0), 0.0, 1.0)
        )
        exposure_factor = float(
            clamp(extras.get("scope_exposure_factor", 1.0), 0.0, 1.0)
        )
        low_light = (
            float(
                clamp(
                    0.15 + 0.55 * iso_factor + 0.30 * exposure_factor,
                    0.15,
                    1.0,
                )
            )
            if scope_camera
            else 1.0
        )
        m_ref = float(
            clamp(
                extras.get(
                    "star_m_ref", limiting_mag - (2.4 if scope else 1.8)
                ),
                -2.0,
                17.0,
            )
        )
        gamma = float(clamp(extras.get("star_gamma", 0.84), 0.70, 1.10))
        if scope_camera:
            gamma = max(0.84, gamma)
        intensity = np.power(
            np.clip(
                np.power(10.0, -0.4 * (magnitude - m_ref), dtype=np.float64),
                1e-8,
                1e8,
            ),
            gamma,
        )
        alpha_floor = 0.006 if scope else 0.010
        alpha_curve = 0.86 if scope else 0.92
        alpha = np.clip(
            (
                alpha_floor
                + 0.995 * np.power(intensity / (1.0 + intensity), alpha_curve)
            )
            * float(extras.get("star_brightness_boost", 1.0))
            * (
                float(max(0.2, extras.get("scope_alpha_gain", 1.0)))
                if scope
                else 1.0
            )
            * (
                float(max(0.4, extras.get("scope_signal_gain", 1.0)))
                if scope
                else 1.0
            )
            * low_light,
            alpha_floor,
            1.0,
        )
        alpha_u8 = np.asarray(
            np.clip(np.rint(alpha * 255.0), 8.0, 255.0), dtype=np.uint8
        )
        best_delta = magnitude - float(np.min(magnitude))
        size_bin = np.ones(after_bucket, dtype=np.int8)
        for threshold, value in ((3.0, 2), (1.5, 3), (0.0, 4), (-1.0, 5)):
            size_bin = np.where(magnitude <= threshold, value, size_bin)
        if not pure_colors:
            size_bin = np.where(
                best_delta <= (1.15 if scope else 0.95), size_bin + 1, size_bin
            )
            size_bin = np.where(
                best_delta <= (0.45 if scope else 0.30), size_bin + 1, size_bin
            )
        if scope:
            size_gain = float(max(0.4, extras.get("scope_size_gain", 1.0)))
            size_value = np.asarray(size_bin, dtype=np.float32) * size_gain
            if scope_camera:
                size_value *= float(clamp(0.40 + 0.60 * low_light, 0.40, 1.0))
            size_bin = np.asarray(
                np.clip(np.rint(size_value), 1.0, 7.0), dtype=np.int8
            )
        else:
            size_bin = np.asarray(np.clip(size_bin, 1.0, 6.0), dtype=np.int8)
        halo = np.zeros(after_bucket, dtype=np.int8)
        if not pure_colors:
            halo_gain = (
                float(max(1.0, extras.get("scope_halo_gain", 1.0)))
                if scope
                else 1.0
            )
            halo_shift = max(0.0, math.log2(halo_gain)) if scope else 0.0
            if scope_camera:
                halo_shift *= low_light
            halo = np.where(magnitude <= 1.8 + halo_shift, 1, halo)
            halo = np.where(magnitude <= 0.5 + halo_shift, 2, halo)
            halo = np.where(magnitude <= -0.8 + halo_shift, 3, halo)
            halo = np.where(
                best_delta <= 1.0 + 0.35 * halo_shift,
                np.maximum(halo, 1),
                halo,
            )
            halo = np.where(
                best_delta <= 0.38 + 0.22 * halo_shift,
                np.maximum(halo, 2),
                halo,
            )
            halo = np.where(
                best_delta <= 0.14 + 0.10 * halo_shift,
                np.maximum(halo, 3),
                halo,
            )
            if scope:
                scope_fov_diag_deg = float(
                    extras.get("scope_fov_diag_deg", 8.0)
                )
                wide_field_factor = float(
                    clamp((scope_fov_diag_deg - 10.0) / 42.0, 0.0, 1.0)
                )
                if wide_field_factor > 0.25:
                    halo = np.where(halo > 0, halo - 1, halo).astype(np.int8)
            allowed = np.zeros(after_bucket, dtype=bool)
            allowed[
                np.argsort(magnitude, kind="mergesort")[
                    : max(10, int(after_bucket * (0.10 if scope else 0.04)))
                ]
            ] = True
            halo = np.where(allowed, halo, 0).astype(np.int8)
        bp = np.where(np.isfinite(bp_rp), bp_rp, 0.8)
        bp_bin = np.asarray(
            np.clip(np.rint((bp + 0.5) * 10.0), 0.0, 70.0), dtype=np.int16
        )
        weak_bp_bin = (
            bp_bin if scope else np.asarray((bp_bin // 4) * 4, dtype=np.int16)
        )
        base_rgb = self._rgb_for_bin(bp_bin, pure_colors)
        weak_rgb = self._rgb_for_bin(weak_bp_bin, pure_colors)
        base_rgba = np.column_stack((base_rgb, alpha_u8)).astype(
            np.uint8, copy=False
        )
        medium_alpha = np.asarray((alpha_u8 // 16) * 16, dtype=np.uint8)
        weak_alpha = np.asarray((alpha_u8 // 32) * 32, dtype=np.uint8)
        medium_rgba = np.column_stack((base_rgb, medium_alpha)).astype(
            np.uint8, copy=False
        )
        weak_rgba = np.column_stack((weak_rgb, weak_alpha)).astype(
            np.uint8, copy=False
        )
        mag_order = np.partition(
            magnitude,
            (
                max(0, round((after_bucket - 1) * 0.05)),
                max(0, round((after_bucket - 1) * 0.22)),
            ),
        )
        bright_cut = max(
            1.5,
            float(mag_order[max(0, round((after_bucket - 1) * 0.05))]) + 0.35,
        )
        medium_cut = max(
            4.0,
            float(mag_order[max(0, round((after_bucket - 1) * 0.22))]) + 0.40,
        )
        weak_indices = np.where(magnitude > medium_cut)[0].astype(np.int32)
        medium_indices = np.where(
            (magnitude > bright_cut) & (magnitude <= medium_cut)
        )[0].astype(np.int32)
        bright_indices = np.where((halo > 0) | (magnitude <= bright_cut))[
            0
        ].astype(np.int32)
        bright_indices = bright_indices[
            np.argsort(magnitude[bright_indices], kind="mergesort")
        ]
        bright_indices = bright_indices[
            : max(16, int(after_bucket * (0.14 if scope else 0.08)))
        ]
        radius = np.asarray(
            np.clip(
                np.rint(
                    (
                        0.85
                        + 0.95
                        * np.clip(
                            (medium_cut - magnitude)
                            / max(0.2, medium_cut - bright_cut),
                            0.0,
                            1.0,
                        )
                    )
                    * 10.0
                ),
                0.0,
                127.0,
            ),
            dtype=np.int32,
        )
        style_key = np.asarray(bp_bin * 256 + medium_alpha, dtype=np.int32)
        weak_style_key = np.asarray(
            weak_bp_bin * 256 + weak_alpha, dtype=np.int32
        )
        medium_style_key = np.asarray(style_key * 128 + radius, dtype=np.int32)
        if bool(extras.get("suppress_star_points", False)):
            weak_indices = medium_indices = bright_indices = _empty(
                np.dtype(np.int32)
            )
        average_radius = float(np.mean(size_bin)) if len(size_bin) else 0.0
        counters = MappingProxyType(
            {
                "total_in_view": float(total_in_view),
                "after_mag_cut": float(after_magnitude_cut),
                "after_bucket": float(after_bucket),
                "density_cap": float(after_bucket),
                "state_mag_limit": round(float(state.mag_limit), 3),
                "limiting_mag": round(limiting_mag, 3),
                "pre_limit": round(pre_limit, 3),
                "avg_alpha_u8": round(float(np.mean(alpha_u8)), 3),
                "halo_count": float(np.count_nonzero(halo)),
                "frame_mag_min": round(float(np.min(magnitude)), 3),
                "bright_cut_mag": round(bright_cut, 3),
                "medium_cut_mag": round(medium_cut, 3),
                "weak_count": float(len(weak_indices)),
                "medium_count": float(len(medium_indices)),
                "scope_spatial_prefilter": float(scope_prefilter),
                "scope_spatial_prefilter_pending": float(
                    scope_prefilter_pending
                ),
                "scope_tile_count": float(scope_tile_count),
                "scope_tile_candidates": float(scope_tile_candidates),
                "scope_prefilter_count": float(len(indices)),
                "scope_candidate_cap": float(scope_candidate_cap),
                "catalog_mag_sorted": float(sorted_by_magnitude),
                "pure_colors": float(pure_colors),
                "interaction_active": float(interaction_active),
                "avg_size_bin": round(average_radius, 3),
            }
        )
        return StarScenePlan(
            sprites=StarSpriteBatch(
                screen_x=_readonly(np.asarray(sx, dtype=np.float32)),
                screen_y=_readonly(np.asarray(sy, dtype=np.float32)),
                base_rgba=_readonly(base_rgba),
                medium_rgba=_readonly(medium_rgba),
                weak_rgba=_readonly(weak_rgba),
                medium_style_key=_readonly(medium_style_key),
                weak_style_key=_readonly(weak_style_key),
                medium_radius_tenths=_readonly(radius),
                halo_bin=_readonly(np.asarray(halo, dtype=np.int8)),
                size_bin=_readonly(np.asarray(size_bin, dtype=np.int8)),
                weak_indices=_readonly(weak_indices),
                medium_indices=_readonly(medium_indices),
                bright_indices=_readonly(bright_indices),
                smooth=after_bucket <= 3500,
            ),
            picks=StarPickBatch(
                _readonly(np.asarray(indices, dtype=np.int32)),
                _readonly(np.asarray(sx, dtype=np.float32)),
                _readonly(np.asarray(sy, dtype=np.float32)),
                _readonly(np.asarray(altitude, dtype=np.float32)),
                _readonly(np.asarray(azimuth, dtype=np.float32)),
                _readonly(np.asarray(magnitude, dtype=np.float32)),
            ),
            total_in_view=total_in_view,
            after_magnitude_cut=after_magnitude_cut,
            after_bucket=after_bucket,
            average_radius=average_radius,
            counters=counters,
        )
