"""Photometric stellar renderer with pixel bucketing and PSF."""

from __future__ import annotations

from dataclasses import dataclass
import math

from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QRadialGradient

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

from TerraLab.scene.projection import project_universal_stereo_numpy, radec_to_altaz_numpy
from TerraLab.util.color import color_from_bp_rp
from TerraLab.util.math2d import clamp


@dataclass
class StarsRenderResult:
    visible_indices: object
    visible_sx: object
    visible_sy: object
    total_in_view: int = 0
    after_mag_cut: int = 0
    after_bucket: int = 0
    avg_radius: float = 0.0


def build_scope_spatial_index_payload(
    ra_all,
    dec_all,
    mag_all=None,
    max_mag: float | None = None,
    ra_bins: int = 360,
    dec_bins: int = 180,
    chunk_size: int = 1_000_000,
):
    if np is None or ra_all is None or dec_all is None:
        return None, None

    n_total = int(len(ra_all))
    num_tiles = int(ra_bins * dec_bins)
    if n_total <= 0:
        return np.array([], dtype=np.int32), np.zeros(num_tiles + 1, dtype=np.int64)

    use_mag_cap = (
        (max_mag is not None)
        and (mag_all is not None)
        and (int(len(mag_all)) == n_total)
    )
    max_mag_f = float(max_mag) if max_mag is not None else None

    counts = np.zeros(num_tiles, dtype=np.int64)
    valid_total = 0

    for start in range(0, n_total, chunk_size):
        end = min(start + chunk_size, n_total)
        ra_chunk = np.asarray(ra_all[start:end], dtype=np.float32)
        dec_chunk = np.asarray(dec_all[start:end], dtype=np.float32)
        valid = np.isfinite(ra_chunk) & np.isfinite(dec_chunk)
        if use_mag_cap:
            mag_chunk = np.asarray(mag_all[start:end], dtype=np.float32)
            valid &= np.isfinite(mag_chunk) & (mag_chunk <= max_mag_f + 1e-6)
        if not np.any(valid):
            continue

        ra_valid = np.mod(ra_chunk[valid], 360.0)
        dec_valid = np.clip(dec_chunk[valid], -90.0, 89.99999)
        ra_bin = np.asarray(np.floor(ra_valid), dtype=np.int32)
        dec_bin = np.asarray(np.floor(dec_valid + 90.0), dtype=np.int32)
        tile_ids = np.asarray(dec_bin * ra_bins + ra_bin, dtype=np.int32)
        counts += np.bincount(tile_ids, minlength=num_tiles)
        valid_total += int(len(tile_ids))

    offsets = np.empty(num_tiles + 1, dtype=np.int64)
    offsets[0] = 0
    np.cumsum(counts, out=offsets[1:])
    sorted_indices = np.empty(valid_total, dtype=np.int32)
    cursor = offsets[:-1].copy()

    for start in range(0, n_total, chunk_size):
        end = min(start + chunk_size, n_total)
        ra_chunk = np.asarray(ra_all[start:end], dtype=np.float32)
        dec_chunk = np.asarray(dec_all[start:end], dtype=np.float32)
        valid = np.isfinite(ra_chunk) & np.isfinite(dec_chunk)
        if use_mag_cap:
            mag_chunk = np.asarray(mag_all[start:end], dtype=np.float32)
            valid &= np.isfinite(mag_chunk) & (mag_chunk <= max_mag_f + 1e-6)
        if not np.any(valid):
            continue

        raw_idx = np.arange(start, end, dtype=np.int32)[valid]
        ra_valid = np.mod(ra_chunk[valid], 360.0)
        dec_valid = np.clip(dec_chunk[valid], -90.0, 89.99999)
        ra_bin = np.asarray(np.floor(ra_valid), dtype=np.int32)
        dec_bin = np.asarray(np.floor(dec_valid + 90.0), dtype=np.int32)
        tile_ids = np.asarray(dec_bin * ra_bins + ra_bin, dtype=np.int32)

        local_order = np.argsort(tile_ids, kind="mergesort")
        tile_sorted = tile_ids[local_order]
        idx_sorted = raw_idx[local_order]
        unique_tiles, first_pos = np.unique(tile_sorted, return_index=True)
        next_pos = np.append(first_pos[1:], len(tile_sorted))

        for tile_id, seg_start, seg_end in zip(unique_tiles, first_pos, next_pos):
            dst = int(cursor[int(tile_id)])
            seg_len = int(seg_end - seg_start)
            sorted_indices[dst : dst + seg_len] = idx_sorted[seg_start:seg_end]
            cursor[int(tile_id)] += seg_len

    return sorted_indices, offsets


class StarsRenderer:
    """
    Renderer orientado a look "sky map / night-sky":
    - Mucho punto (1px/2px)
    - Halos suaves solo en brillantes
    - Sprites gaussianos cacheados (rÃ¡pido y consistente)
    """

    def __init__(self) -> None:
        self._qcolor_cache = {}
        self._sprite_cache = {}
        self._bright_sprite_cache = {}

        # Magnitude index cache (non-scope fast prefilter by mag<=pre_limit)
        self._mag_index_key = None
        self._mag_sorted = None
        self._mag_order = None
        self._nonscope_prefilter_key = None
        self._nonscope_prefilter_indices = None

        # Alt/Az cache for repeated camera moves over same star subset.
        self._altaz_cache_key = None
        self._altaz_cache_alt = None
        self._altaz_cache_az = None

        # Scope-only spatial index (RA/Dec tiles) to avoid broad-sky scans.
        self._scope_grid_key = None
        self._scope_grid_indices = None
        self._scope_grid_offsets = None
        self._scope_grid_ra_bins = 360
        self._scope_grid_dec_bins = 180
        self._scope_grid_tile_ra_deg = 1.0
        self._scope_grid_tile_dec_deg = 1.0
        self._scope_query_cache_key = None
        self._scope_query_cache_indices = None
        self._scope_query_cache_tile_count = 0
        self._scope_query_cache_candidate_count = 0
        self._scope_index_pending_key = None


    # -----------------------------
    # Indexing / window selection
    # -----------------------------
    def _catalog_array_key(self, *arrays):
        key_parts = []
        for arr in arrays:
            if arr is None:
                key_parts.append(("none", 0))
                continue
            try:
                np_arr = np.asarray(arr)
                ptr = int(np_arr.__array_interface__["data"][0])
                key_parts.append((ptr, int(len(np_arr))))
            except Exception:
                try:
                    key_parts.append((id(arr), int(len(arr))))
                except Exception:
                    key_parts.append((id(arr), 0))
        return tuple(key_parts)

    def _ensure_mag_index(self, ra_all, dec_all, mag_all):
        if np is None or ra_all is None or dec_all is None or mag_all is None:
            return None, None
        key = self._catalog_array_key(ra_all, dec_all, mag_all)

        if self._mag_index_key == key and self._mag_sorted is not None and self._mag_order is not None:
            return self._mag_sorted, self._mag_order

        finite_mask = np.isfinite(ra_all) & np.isfinite(dec_all) & np.isfinite(mag_all)
        if not np.any(finite_mask):
            self._mag_index_key = key
            self._mag_sorted = np.array([], dtype=np.float32)
            self._mag_order = np.array([], dtype=np.int32)
            return self._mag_sorted, self._mag_order

        valid_idx = np.where(finite_mask)[0].astype(np.int32, copy=False)
        mag_valid = np.asarray(mag_all[valid_idx], dtype=np.float32)
        order_local = np.argsort(mag_valid, kind="mergesort")

        self._mag_index_key = key
        self._mag_sorted = mag_valid[order_local]
        self._mag_order = valid_idx[order_local]
        return self._mag_sorted, self._mag_order

    def _non_scope_prefilter(self, ra_all, dec_all, mag_all, pre_limit: float, assume_sorted: bool = False):
        if np is None or ra_all is None or dec_all is None or mag_all is None:
            return None

        try:
            ptr_ra = int(np.asarray(ra_all).__array_interface__["data"][0])
            ptr_dec = int(np.asarray(dec_all).__array_interface__["data"][0])
            ptr_mag = int(np.asarray(mag_all).__array_interface__["data"][0])
            cache_key = (
                ptr_ra,
                ptr_dec,
                ptr_mag,
                int(len(mag_all)),
                int(round(float(pre_limit) * 20.0)),
                1 if assume_sorted else 0,
            )
        except Exception:
            cache_key = (
                id(ra_all),
                id(dec_all),
                id(mag_all),
                int(len(mag_all)),
                int(round(float(pre_limit) * 20.0)),
                1 if assume_sorted else 0,
            )

        if self._nonscope_prefilter_key == cache_key and self._nonscope_prefilter_indices is not None:
            return self._nonscope_prefilter_indices

        if assume_sorted:
            mag_sorted = np.asarray(mag_all, dtype=np.float32)
            if len(mag_sorted) == 0:
                self._nonscope_prefilter_key = cache_key
                self._nonscope_prefilter_indices = np.array([], dtype=np.int32)
                return self._nonscope_prefilter_indices
            hi = int(np.searchsorted(mag_sorted, pre_limit, side="right"))
            if hi <= 0:
                self._nonscope_prefilter_key = cache_key
                self._nonscope_prefilter_indices = np.array([], dtype=np.int32)
                return self._nonscope_prefilter_indices
            idx = np.arange(hi, dtype=np.int32)
            self._nonscope_prefilter_key = cache_key
            self._nonscope_prefilter_indices = idx
            return idx

        mag_sorted, mag_order = self._ensure_mag_index(ra_all, dec_all, mag_all)
        if mag_sorted is None or mag_order is None or len(mag_sorted) == 0:
            return None

        hi = int(np.searchsorted(mag_sorted, pre_limit, side="right"))
        if hi <= 0:
            self._nonscope_prefilter_key = cache_key
            self._nonscope_prefilter_indices = np.array([], dtype=np.int32)
            return self._nonscope_prefilter_indices

        idx = np.asarray(mag_order[:hi], dtype=np.int32)
        self._nonscope_prefilter_key = cache_key
        self._nonscope_prefilter_indices = idx
        return idx

    def _reset_scope_spatial_index(self) -> None:
        self._scope_grid_key = None
        self._scope_grid_indices = None
        self._scope_grid_offsets = None
        self._scope_query_cache_key = None
        self._scope_query_cache_indices = None
        self._scope_query_cache_tile_count = 0
        self._scope_query_cache_candidate_count = 0

    def begin_scope_index_warmup(self, ra_all, dec_all):
        key = self._catalog_array_key(ra_all, dec_all)
        self._scope_index_pending_key = key
        if self._scope_grid_key != key:
            self._reset_scope_spatial_index()
        return key

    def apply_scope_spatial_index_payload(self, key, sorted_indices, offsets):
        self._reset_scope_spatial_index()
        self._scope_grid_key = key
        self._scope_grid_indices = np.asarray(sorted_indices, dtype=np.int32)
        self._scope_grid_offsets = np.asarray(offsets, dtype=np.int64)
        if self._scope_index_pending_key == key:
            self._scope_index_pending_key = None

    def clear_scope_index_warmup(self, key=None):
        if key is None or self._scope_index_pending_key == key:
            self._scope_index_pending_key = None

    def _ensure_scope_spatial_index(self, ra_all, dec_all):
        if np is None or ra_all is None or dec_all is None:
            return None, None

        key = self._catalog_array_key(ra_all, dec_all)
        if (
            self._scope_grid_key == key
            and self._scope_grid_indices is not None
            and self._scope_grid_offsets is not None
        ):
            return self._scope_grid_indices, self._scope_grid_offsets

        self._reset_scope_spatial_index()
        if self._scope_index_pending_key == key:
            return None, None

        sorted_indices, offsets = build_scope_spatial_index_payload(
            ra_all,
            dec_all,
            ra_bins=int(self._scope_grid_ra_bins),
            dec_bins=int(self._scope_grid_dec_bins),
        )
        if sorted_indices is None or offsets is None:
            return None, None

        self._scope_grid_key = key
        self._scope_grid_indices = sorted_indices
        self._scope_grid_offsets = offsets
        return self._scope_grid_indices, self._scope_grid_offsets

    def _scope_window_tile_ids(self, center_ra, center_dec, ra_pad, dec_pad):
        ra_bins = int(self._scope_grid_ra_bins)
        dec_bins = int(self._scope_grid_dec_bins)

        dec_min = max(-90.0, float(center_dec) - float(dec_pad))
        dec_max = min(90.0, float(center_dec) + float(dec_pad))
        dec_lo_bin = max(0, min(dec_bins - 1, int(math.floor(dec_min + 90.0))))
        dec_hi_bin = max(0, min(dec_bins - 1, int(math.floor(max(dec_min, dec_max - 1e-6) + 90.0))))

        ra_ranges = []
        if float(ra_pad) >= 179.999:
            ra_ranges.append((0, ra_bins - 1))
        else:
            ra_min = (float(center_ra) - float(ra_pad)) % 360.0
            ra_max = (float(center_ra) + float(ra_pad)) % 360.0
            if ra_min <= ra_max:
                ra_ranges.append(
                    (
                        max(0, min(ra_bins - 1, int(math.floor(ra_min)))),
                        max(0, min(ra_bins - 1, int(math.floor(max(ra_min, ra_max - 1e-6))))),
                    )
                )
            else:
                ra_ranges.append((0, max(0, min(ra_bins - 1, int(math.floor(max(0.0, ra_max - 1e-6)))))))
                ra_ranges.append((max(0, min(ra_bins - 1, int(math.floor(ra_min)))), ra_bins - 1))

        tile_ids = []
        for dec_bin in range(dec_lo_bin, dec_hi_bin + 1):
            row_base = dec_bin * ra_bins
            for ra_lo_bin, ra_hi_bin in ra_ranges:
                if ra_lo_bin > ra_hi_bin:
                    continue
                tile_ids.extend(range(row_base + ra_lo_bin, row_base + ra_hi_bin + 1))
        if not tile_ids:
            return np.array([], dtype=np.int32)
        return np.asarray(tile_ids, dtype=np.int32)

    def _scope_spatial_prefilter(
        self,
        ra_all,
        dec_all,
        mag_all,
        center_ra: float,
        center_dec: float,
        ra_pad: float,
        dec_pad: float,
        pre_limit: float,
        interaction_active: bool = False,
    ):
        grid_indices, grid_offsets = self._ensure_scope_spatial_index(ra_all, dec_all)
        if grid_indices is None or grid_offsets is None:
            return None, 0, 0

        tile_pad_ra = float(self._scope_grid_tile_ra_deg) * 1.5
        tile_pad_dec = float(self._scope_grid_tile_dec_deg) * 1.5
        if interaction_active:
            tile_pad_ra += 0.6
            tile_pad_dec += 0.6

        query_ra_pad = min(180.0, float(ra_pad) + tile_pad_ra)
        query_dec_pad = min(90.0, float(dec_pad) + tile_pad_dec)

        tile_ids = self._scope_window_tile_ids(
            center_ra=float(center_ra),
            center_dec=float(center_dec),
            ra_pad=query_ra_pad,
            dec_pad=query_dec_pad,
        )
        if len(tile_ids) == 0:
            return np.array([], dtype=np.int32), 0, 0

        grid_key = self._scope_grid_key
        query_key = (grid_key, tuple(int(t) for t in tile_ids))
        if self._scope_query_cache_key == query_key and self._scope_query_cache_indices is not None:
            candidate_idx = self._scope_query_cache_indices
            tile_count = int(self._scope_query_cache_tile_count)
            candidate_count = int(self._scope_query_cache_candidate_count)
        else:
            candidate_count = 0
            for tile_id in tile_ids:
                candidate_count += int(grid_offsets[int(tile_id) + 1] - grid_offsets[int(tile_id)])

            if candidate_count <= 0:
                self._scope_query_cache_key = query_key
                self._scope_query_cache_indices = np.array([], dtype=np.int32)
                self._scope_query_cache_tile_count = int(len(tile_ids))
                self._scope_query_cache_candidate_count = 0
                return self._scope_query_cache_indices, int(len(tile_ids)), 0

            candidate_idx = np.empty(candidate_count, dtype=np.int32)
            dst = 0
            for tile_id in tile_ids:
                src_lo = int(grid_offsets[int(tile_id)])
                src_hi = int(grid_offsets[int(tile_id) + 1])
                if src_hi <= src_lo:
                    continue
                seg = grid_indices[src_lo:src_hi]
                seg_len = int(len(seg))
                candidate_idx[dst : dst + seg_len] = seg
                dst += seg_len
            if dst != candidate_count:
                candidate_idx = np.asarray(candidate_idx[:dst], dtype=np.int32)
                candidate_count = int(dst)

            self._scope_query_cache_key = query_key
            self._scope_query_cache_indices = candidate_idx
            self._scope_query_cache_tile_count = int(len(tile_ids))
            self._scope_query_cache_candidate_count = int(candidate_count)
            tile_count = int(len(tile_ids))

        if candidate_count <= 0:
            return np.array([], dtype=np.int32), tile_count, candidate_count

        ra_sub = np.asarray(ra_all[candidate_idx], dtype=np.float32)
        dec_sub = np.asarray(dec_all[candidate_idx], dtype=np.float32)
        dra = np.abs(((ra_sub - float(center_ra) + 180.0) % 360.0) - 180.0)
        mask = (
            np.isfinite(ra_sub)
            & np.isfinite(dec_sub)
            & (dra <= float(ra_pad))
            & (np.abs(dec_sub - float(center_dec)) <= float(dec_pad))
        )

        if mag_all is not None:
            mag_sub = np.asarray(mag_all[candidate_idx], dtype=np.float32)
            mask &= np.isfinite(mag_sub) & (mag_sub <= float(pre_limit))

        if not np.any(mask):
            return np.array([], dtype=np.int32), tile_count, candidate_count
        return np.asarray(candidate_idx[mask], dtype=np.int32), tile_count, candidate_count

    def prime_catalog_indices(self, ra_all, dec_all=None, mag_all=None, catalog_mag_sorted: bool = False):
        # Warm caches ahead of the first scope frame so the expensive setup does not happen
        # inside paintEvent.
        if np is None or ra_all is None:
            return
        if dec_all is not None:
            try:
                self._ensure_scope_spatial_index(ra_all, dec_all)
            except Exception:
                pass
        if dec_all is not None and mag_all is not None and (not bool(catalog_mag_sorted)):
            try:
                self._ensure_mag_index(ra_all, dec_all, mag_all)
            except Exception:
                pass

    def _cached_altaz(self, ra_all, dec_all, catalog_idx, state, interaction_active: bool):
        if np is None:
            return None, None
        if catalog_idx is None or len(catalog_idx) == 0:
            return None, None

        # During interaction, 4 Hz bucket keeps motion visually continuous while avoiding
        # redundant trig over identical subsets on consecutive paint events.
        ut_hour = float(getattr(state, "ut_hour", 0.0))
        if interaction_active:
            ut_key = int(round(ut_hour * 3600.0 * 4.0))
        else:
            ut_key = int(round(ut_hour * 3600.0))

        try:
            ptr_ra = int(np.asarray(ra_all).__array_interface__["data"][0])
            ptr_dec = int(np.asarray(dec_all).__array_interface__["data"][0])
            ptr_idx = int(np.asarray(catalog_idx).__array_interface__["data"][0])
        except Exception:
            ptr_ra = id(ra_all)
            ptr_dec = id(dec_all)
            ptr_idx = id(catalog_idx)

        cache_key = (
            ptr_ra,
            ptr_dec,
            ptr_idx,
            int(len(catalog_idx)),
            int(round(float(getattr(state, "latitude", 0.0)) * 1000.0)),
            int(round(float(getattr(state, "longitude", 0.0)) * 1000.0)),
            int(getattr(state, "day_of_year", 0)),
            ut_key,
        )

        if self._altaz_cache_key == cache_key and self._altaz_cache_alt is not None and self._altaz_cache_az is not None:
            return self._altaz_cache_alt, self._altaz_cache_az

        ra = ra_all[catalog_idx]
        dec = dec_all[catalog_idx]
        alt_deg, az_deg = radec_to_altaz_numpy(
            ra,
            dec,
            latitude_deg=float(getattr(state, "latitude", 0.0)),
            longitude_deg=float(getattr(state, "longitude", 0.0)),
            ut_hour=ut_hour,
            day_of_year=int(getattr(state, "day_of_year", 0)),
        )
        if alt_deg is None:
            return None, None

        self._altaz_cache_key = cache_key
        self._altaz_cache_alt = alt_deg
        self._altaz_cache_az = az_deg
        return alt_deg, az_deg

    @staticmethod
    def _empty_result():
        if np is None:
            return StarsRenderResult([], [], [], 0, 0, 0, 0.0)
        return StarsRenderResult(
            np.array([], dtype=np.int32),
            np.array([], dtype=np.float32),
            np.array([], dtype=np.float32),
            0,
            0,
            0,
            0.0,
        )

    # -----------------------------
    # Photometry / limits
    # -----------------------------
    def _limiting_magnitude(self, state) -> float:
        cam = state.camera
        scope_enabled = bool(getattr(state, "scope_enabled", False))
        extras = getattr(state, "extras", {}) if isinstance(getattr(state, "extras", {}), dict) else {}

        base = float(getattr(state, "magnitude_limit", 6.0))
        spike_knob = float(getattr(state, "spike_magnitude_threshold", 2.0))
        spike_bias = (spike_knob - 2.0) * 0.08
        if scope_enabled:
            # In scope mode, use the calibrated optical model directly.
            wide_field_penalty = float(clamp(float(extras.get("scope_fov_penalty_mag", 0.0)) * 0.35, 0.0, 2.8))
            limiting_mag = float(extras.get("scope_limit_mag", base)) + spike_bias - wide_field_penalty
        else:
            # `state.magnitude_limit` already arrives precomputed from the scene layer:
            # it includes the active sky brightness model (sun/twilight/eclipse) and the
            # general Bortle/manual render limit. Reapplying a Bortle penalty here would
            # darken the sky twice and make naked-eye constellations disappear incorrectly.
            limiting_mag = base + spike_bias

        if float(getattr(state, "sun_alt", -90.0)) > 0.0:
            limiting_mag = min(limiting_mag, -4.0)

        if bool(extras.get("scope_force_naked_eye_until_fix", False)):
            naked_eye_cap = float(extras.get("scope_first_fix_mag_cap", getattr(state, "naked_eye_cap", 8.0)))
            limiting_mag = min(limiting_mag, naked_eye_cap)

        if scope_enabled:
            cap = 21.5
            dataset_cap = extras.get("scope_dataset_max_mag", None)
            if dataset_cap is not None:
                try:
                    cap = min(cap, float(dataset_cap) + 0.15)
                except Exception:
                    pass
            cap = max(8.0, cap)
        else:
            cap = 13.5
        return float(clamp(limiting_mag, -12.0, cap))

    def _cached_color(self, bp_rp_value: float, alpha_u8: int, pure_colors: bool) -> QColor:
        bp = 0.8 if bp_rp_value is None else float(bp_rp_value)
        if not math.isfinite(bp):
            bp = 0.8

        bp_bin = int(clamp(round((bp + 0.5) * 10.0), 0.0, 70.0))
        alpha_bin = int(clamp(alpha_u8, 0.0, 255.0))
        mode = 1 if pure_colors else 0

        key = (bp_bin, alpha_bin, mode)
        cached = self._qcolor_cache.get(key)
        if cached is not None:
            return cached

        bp_center = (bp_bin / 10.0) - 0.5
        desat_mix = 0.0 if pure_colors else 0.34
        rgb = color_from_bp_rp(bp_center, pure_colors=pure_colors, desaturate_mix=desat_mix)

        # En el look tipo app: colores presentes, pero no "neÃ³n".
        lum = (float(rgb[0]) + float(rgb[1]) + float(rgb[2])) / 3.0
        sat_boost = 1.22 if pure_colors else 1.10
        rgb = (
            int(clamp(round(lum + (float(rgb[0]) - lum) * sat_boost), 0.0, 255.0)),
            int(clamp(round(lum + (float(rgb[1]) - lum) * sat_boost), 0.0, 255.0)),
            int(clamp(round(lum + (float(rgb[2]) - lum) * sat_boost), 0.0, 255.0)),
        )

        color = QColor(int(rgb[0]), int(rgb[1]), int(rgb[2]), int(alpha_bin))
        self._qcolor_cache[key] = color
        return color

    def _cached_disc_sprite(self, color: QColor, radius_px: float) -> tuple[QImage, float]:
        r = float(clamp(radius_px, 0.35, 6.0))
        r_bin = round(r * 10.0) / 10.0
        key = (int(color.rgba()), int(round(r_bin * 10.0)))
        cached = self._sprite_cache.get(key)
        if cached is not None:
            return cached

        pad = 1
        size = int(max(3, math.ceil(r_bin * 2.0) + 2 * pad + 1))
        center = (size - 1) * 0.5

        img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(QPointF(center, center), r_bin, r_bin)
        p.end()

        out = (img, center)
        self._sprite_cache[key] = out
        return out

    def _cached_bright_sprite(
        self,
        color: QColor,
        alpha_u8: int,
        core_radius_px: float,
        halo_bin: int,
        pure_colors: bool,
    ) -> tuple[QImage, float]:
        r_core = float(clamp(core_radius_px, 1.0, 6.2))
        h = int(max(0, halo_bin))
        mode = 1 if pure_colors else 0
        key = (int(color.rgba()), int(alpha_u8), int(round(r_core * 10.0)), h, mode)
        cached = self._bright_sprite_cache.get(key)
        if cached is not None:
            return cached

        halo_r = 0.0
        if (not pure_colors) and h > 0:
            halo_r = r_core * (2.1 + 0.55 * h)
        outer_r = max(r_core, halo_r)
        pad = 2
        size = int(max(5, math.ceil(outer_r * 2.0) + 2 * pad + 1))
        center = (size - 1) * 0.5

        img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)

        if (not pure_colors) and h > 0:
            halo = QRadialGradient(center, center, halo_r)
            halo.setColorAt(
                0.0,
                QColor(
                    color.red(),
                    color.green(),
                    color.blue(),
                    int(clamp(alpha_u8 * (0.32 + 0.08 * h), 0.0, 220.0)),
                ),
            )
            halo.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
            p.setBrush(QBrush(halo))
            p.drawEllipse(QPointF(center, center), halo_r, halo_r)

        p.setBrush(QBrush(color))
        p.drawEllipse(QPointF(center, center), r_core, r_core)
        if not pure_colors:
            p.setBrush(QBrush(QColor(255, 255, 255, int(clamp(alpha_u8 * 0.75, 0.0, 255.0)))))
            p.drawEllipse(QPointF(center, center), r_core * 0.42, r_core * 0.42)
        p.end()

        out = (img, center)
        self._bright_sprite_cache[key] = out
        return out
    # -----------------------------
    # Main render
    # -----------------------------
    def render(self, ctx, state):
        if np is None:
            return self._empty_result()
        extras = getattr(state, "extras", {}) if isinstance(getattr(state, "extras", {}), dict) else {}
        if not bool(extras.get("stars_enabled", True)):
            return self._empty_result()
        if state.ra is None or state.dec is None or state.mag is None:
            return self._empty_result()

        # Avoid full-catalog dtype casts every frame; keep original array dtype.
        ra_all = np.asarray(state.ra)
        dec_all = np.asarray(state.dec)
        mag_all = np.asarray(state.mag)
        if len(ra_all) == 0:
            return self._empty_result()

        bp_rp_all = None
        if getattr(state, "bp_rp", None) is not None:
            bp_rp_all = np.asarray(state.bp_rp)
            if len(bp_rp_all) != len(mag_all):
                bp_rp_all = None

        scope_enabled = bool(getattr(state, "scope_enabled", False))
        interaction_active = bool(getattr(state, "interaction_active", False))
        catalog_mag_sorted = bool(extras.get("catalog_mag_sorted", False))
        diag = getattr(ctx, "diagnostics", None)

        limiting_mag = self._limiting_magnitude(state)

        # Prefetch margin above frame limit.
        pre_limit = float(limiting_mag + (1.0 if scope_enabled else 2.0))
        if scope_enabled:
            dataset_cap = extras.get("scope_dataset_max_mag", None)
            if dataset_cap is not None:
                try:
                    pre_limit = min(pre_limit, float(dataset_cap) + 0.25)
                except Exception:
                    pass
        if bool(extras.get("scope_force_naked_eye_until_fix", False)):
            pre_limit = min(pre_limit, float(extras.get("scope_first_fix_mag_cap", getattr(state, "naked_eye_cap", 8.0))))

        center_ra = extras.get("scope_center_ra_deg") if scope_enabled else None
        center_dec = extras.get("scope_center_dec_deg") if scope_enabled else None
        ra_pad = extras.get("scope_preselect_ra_pad_deg") if scope_enabled else None
        dec_pad = extras.get("scope_preselect_dec_pad_deg") if scope_enabled else None
        used_scope_spatial_prefilter = False
        scope_spatial_prefilter_pending = False
        scope_tile_count = 0
        scope_tile_candidates = 0

        if (
            scope_enabled
            and center_ra is not None
            and center_dec is not None
            and ra_pad is not None
            and dec_pad is not None
            and int(len(ra_all)) >= 250000
        ):
            if diag is not None:
                diag.start_timer("stars_scope_prefilter")
            catalog_idx, scope_tile_count, scope_tile_candidates = self._scope_spatial_prefilter(
                ra_all=ra_all,
                dec_all=dec_all,
                mag_all=mag_all,
                center_ra=float(center_ra),
                center_dec=float(center_dec),
                ra_pad=float(ra_pad),
                dec_pad=float(dec_pad),
                pre_limit=float(pre_limit),
                interaction_active=interaction_active,
            )
            if diag is not None:
                diag.stop_timer("stars_scope_prefilter")
            if catalog_idx is None:
                scope_spatial_prefilter_pending = True
                catalog_idx = self._non_scope_prefilter(
                    ra_all,
                    dec_all,
                    mag_all,
                    float(pre_limit),
                    assume_sorted=catalog_mag_sorted,
                )
            else:
                used_scope_spatial_prefilter = True
        else:
            catalog_idx = self._non_scope_prefilter(
                ra_all,
                dec_all,
                mag_all,
                float(pre_limit),
                assume_sorted=catalog_mag_sorted,
            )

        if catalog_idx is None or len(catalog_idx) == 0:
            return self._empty_result()

        mag = np.asarray(mag_all[catalog_idx], dtype=np.float32)
        bp_rp = bp_rp_all[catalog_idx] if bp_rp_all is not None else None

        if diag is not None:
            diag.set_counter("scope_spatial_prefilter", 1 if used_scope_spatial_prefilter else 0)
            diag.set_counter("scope_spatial_prefilter_pending", 1 if scope_spatial_prefilter_pending else 0)
            diag.set_counter("scope_tile_count", int(scope_tile_count))
            diag.set_counter("scope_tile_candidates", int(scope_tile_candidates))
            diag.set_counter("scope_prefilter_count", int(len(catalog_idx)))
            diag.set_counter("catalog_mag_sorted", 1 if catalog_mag_sorted else 0)
            diag.start_timer("stars_altaz")
        alt_deg, az_deg = self._cached_altaz(
            ra_all=ra_all,
            dec_all=dec_all,
            catalog_idx=catalog_idx,
            state=state,
            interaction_active=interaction_active,
        )
        if diag is not None:
            diag.stop_timer("stars_altaz")
        if alt_deg is None:
            return self._empty_result()

        # For very large candidate sets, apply coarse sky-window pruning after Alt/Az cache lookup.
        # This keeps Alt/Az cache hits stable while reducing projection/draw load.
        if (
            (not used_scope_spatial_prefilter)
            and scope_enabled
            and center_ra is not None
            and center_dec is not None
            and ra_pad is not None
            and dec_pad is not None
        ):
            threshold = int(extras.get("scope_window_prefilter_threshold", 120000))
            if int(len(catalog_idx)) > max(20000, threshold):
                try:
                    c_ra = float(center_ra) % 360.0
                    c_dec = float(center_dec)
                    r_pad = max(0.0, float(ra_pad))
                    d_pad = max(0.0, float(dec_pad))
                    if interaction_active:
                        r_pad += 0.6
                        d_pad += 0.6

                    ra_sub = np.asarray(ra_all[catalog_idx], dtype=np.float32)
                    dec_sub = np.asarray(dec_all[catalog_idx], dtype=np.float32)
                    dra = np.abs(((ra_sub - c_ra + 180.0) % 360.0) - 180.0)
                    window_mask = (
                        np.isfinite(ra_sub)
                        & np.isfinite(dec_sub)
                        & (dra <= r_pad)
                        & (np.abs(dec_sub - c_dec) <= d_pad)
                    )
                    if not np.any(window_mask):
                        return self._empty_result()

                    catalog_idx = np.asarray(catalog_idx[window_mask], dtype=np.int32)
                    mag = np.asarray(mag[window_mask], dtype=np.float32)
                    alt_deg = np.asarray(alt_deg[window_mask], dtype=np.float32)
                    az_deg = np.asarray(az_deg[window_mask], dtype=np.float32)
                    bp_rp = bp_rp[window_mask] if bp_rp is not None else None
                except Exception:
                    pass

        inside_scope = None
        if scope_enabled and callable(getattr(state, "scope_mask_fn", None)):
            try:
                inside_scope = state.scope_mask_fn(
                    np.asarray(alt_deg, dtype=np.float32),
                    np.asarray(az_deg, dtype=np.float32),
                )
            except Exception:
                inside_scope = None

            if inside_scope is not None:
                inside_scope = np.asarray(inside_scope, dtype=bool)
                if inside_scope.shape[0] == alt_deg.shape[0]:
                    if not np.any(inside_scope):
                        return self._empty_result()
                    catalog_idx = catalog_idx[inside_scope]
                    mag = mag[inside_scope]
                    alt_deg = alt_deg[inside_scope]
                    az_deg = az_deg[inside_scope]
                    bp_rp = bp_rp[inside_scope] if bp_rp is not None else None
        if diag is not None:
            diag.set_counter("scope_after_mask", int(len(catalog_idx)))

        if diag is not None:
            diag.start_timer("stars_projection")
        sx_all, sy_all, proj_valid = project_universal_stereo_numpy(
            alt_deg,
            az_deg,
            width=int(ctx.width),
            height=int(ctx.height),
            camera=state.camera,
        )
        if diag is not None:
            diag.stop_timer("stars_projection")
        if sx_all is None:
            return self._empty_result()

        bounds_mask = (
            proj_valid
            & np.isfinite(sx_all)
            & np.isfinite(sy_all)
            & (sx_all >= -30.0)
            & (sx_all <= float(ctx.width) + 30.0)
            & (sy_all >= -30.0)
            & (sy_all <= float(ctx.height) + 30.0)
        )
        if not np.any(bounds_mask):
            return self._empty_result()

        catalog_idx = catalog_idx[bounds_mask]
        sx = sx_all[bounds_mask]
        sy = sy_all[bounds_mask]
        mag = mag[bounds_mask]
        bp_rp = bp_rp[bounds_mask] if bp_rp is not None else None
        if diag is not None:
            diag.set_counter("scope_after_bounds", int(len(catalog_idx)))

        total_in_view = int(len(mag))
        if total_in_view == 0:
            return self._empty_result()

        # Mag cut final
        mag_mask = mag <= float(limiting_mag)
        if not np.any(mag_mask):
            res = self._empty_result()
            res.total_in_view = total_in_view
            return res

        catalog_idx = catalog_idx[mag_mask]
        sx = sx[mag_mask]
        sy = sy[mag_mask]
        mag = mag[mag_mask]
        bp_rp = bp_rp[mag_mask] if bp_rp is not None else None
        after_mag_cut = int(len(mag))
        if after_mag_cut == 0:
            res = self._empty_result()
            res.total_in_view = total_in_view
            return res

        # Bucket por pixel: evita overdraw en densidad alta
        px = np.asarray(sx, dtype=np.int32)
        py = np.asarray(sy, dtype=np.int32)
        pixel_key = py * max(1, int(ctx.width)) + px

        by_brightness = np.argsort(mag, kind="mergesort")  # mag menor = mÃ¡s brillante
        sorted_keys = pixel_key[by_brightness]
        _, first_occurrence = np.unique(sorted_keys, return_index=True)
        keep = by_brightness[first_occurrence]
        keep = keep[np.argsort(mag[keep], kind="mergesort")]

        catalog_idx = catalog_idx[keep]
        sx = sx[keep]
        sy = sy[keep]
        mag = mag[keep]
        bp_rp = bp_rp[keep] if bp_rp is not None else None
        after_bucket = int(len(mag))

        # No star-count clipping: preserve full visible set.
        density_cap = int(after_bucket)

        # -----------------------------
        # LOOK: mapping mag -> alpha/size/halo
        # -----------------------------
        pure_colors = bool(getattr(state, "pure_colors", False))
        scope_profile = str(extras.get("scope_instrument_profile", "telescope"))
        scope_is_camera = scope_enabled and scope_profile.startswith("camera")
        scope_iso_factor = float(clamp(extras.get("scope_iso_factor", 1.0), 0.0, 1.0))
        scope_exposure_factor = float(clamp(extras.get("scope_exposure_factor", 1.0), 0.0, 1.0))
        scope_alpha_gain = float(max(0.2, extras.get("scope_alpha_gain", 1.0))) if scope_enabled else 1.0
        scope_size_gain = float(max(0.4, extras.get("scope_size_gain", 1.0))) if scope_enabled else 1.0
        scope_signal_gain = float(max(0.4, extras.get("scope_signal_gain", 1.0))) if scope_enabled else 1.0
        # Low-light camera settings (ISO/exposure bajos) must visibly reduce star presence.
        low_light_drive = 1.0
        if scope_is_camera:
            low_light_drive = float(clamp(0.15 + 0.55 * scope_iso_factor + 0.30 * scope_exposure_factor, 0.15, 1.0))

        # Referencia fotométrica: evitar compresión plana en campos profundos.
        m_ref_default = float(limiting_mag) - (2.4 if scope_enabled else 1.8)
        m_ref = float(extras.get("star_m_ref", m_ref_default))
        m_ref = float(clamp(m_ref, -2.0, 17.0))
        gamma = float(extras.get("star_gamma", 0.84))
        gamma = float(clamp(gamma, 0.70, 1.10))
        if scope_is_camera:
            gamma = float(max(0.84, gamma))
        brightness_boost = float(extras.get("star_brightness_boost", 1.00))

        # Intensidad física aproximada
        intensity_raw = np.power(10.0, -0.4 * (mag - m_ref), dtype=np.float64)
        intensity = np.power(np.clip(intensity_raw, 1e-8, 1e8), gamma)

        # Transferencia de contraste: fondo tenue y brillantes con presencia real.
        alpha_floor = 0.006 if scope_enabled else 0.010
        alpha_curve = 0.86 if scope_enabled else 0.92
        intensity_norm = intensity / (1.0 + intensity)
        alpha = np.clip(
            (alpha_floor + 0.995 * np.power(np.clip(intensity_norm, 0.0, 1.0), alpha_curve))
            * brightness_boost
            * scope_alpha_gain
            * scope_signal_gain
            * low_light_drive,
            alpha_floor,
            1.0,
        )

        mag_min_frame = float(np.min(mag)) if len(mag) else 99.0
        delta_best = np.asarray(mag - mag_min_frame, dtype=np.float32)

        # Size bin: dominante 1px; sube solo en muy brillantes
        # 1..5
        # Umbrales pensados para el look del mÃ³vil:
        size_bin = np.ones(after_bucket, dtype=np.int8)
        size_bin = np.where(mag <= 3.0, 2, size_bin)
        size_bin = np.where(mag <= 1.5, 3, size_bin)
        size_bin = np.where(mag <= 0.0, 4, size_bin)
        size_bin = np.where(mag <= -1.0, 5, size_bin)
        if not pure_colors:
            size_bin = np.where(delta_best <= (1.15 if scope_enabled else 0.95), size_bin + 1, size_bin)
            size_bin = np.where(delta_best <= (0.45 if scope_enabled else 0.30), size_bin + 1, size_bin)
        if scope_enabled:
            size_float = np.asarray(size_bin, dtype=np.float32) * scope_size_gain
            if scope_is_camera:
                size_float *= float(clamp(0.40 + 0.60 * low_light_drive, 0.40, 1.0))
            size_bin = np.asarray(np.clip(np.rint(size_float), 1.0, 7.0), dtype=np.int8)
        else:
            size_bin = np.asarray(np.clip(size_bin, 1.0, 6.0), dtype=np.int8)

        # Halo bin: muy restrictivo
        # 0..3
        halo_bin = np.zeros(after_bucket, dtype=np.int8)
        if not pure_colors:
            halo_gain = float(max(1.0, extras.get("scope_halo_gain", 1.0))) if scope_enabled else 1.0
            halo_shift = 0.0
            if scope_enabled:
                halo_shift = max(0.0, math.log2(halo_gain))
                if scope_is_camera:
                    halo_shift *= low_light_drive
            halo_bin = np.where(mag <= (1.8 + halo_shift), 1, halo_bin)
            halo_bin = np.where(mag <= (0.5 + halo_shift), 2, halo_bin)
            halo_bin = np.where(mag <= (-0.8 + halo_shift), 3, halo_bin)
            halo_bin = np.where(delta_best <= (1.00 + 0.35 * halo_shift), np.maximum(halo_bin, 1), halo_bin)
            halo_bin = np.where(delta_best <= (0.38 + 0.22 * halo_shift), np.maximum(halo_bin, 2), halo_bin)
            halo_bin = np.where(delta_best <= (0.14 + 0.10 * halo_shift), np.maximum(halo_bin, 3), halo_bin)

            # En scope wide-field, recorta halos (evita â€œbokehâ€ masivo)
            if scope_enabled:
                scope_fov_diag_deg = float(extras.get("scope_fov_diag_deg", 8.0))
                scope_wide_field_factor = float(clamp((scope_fov_diag_deg - 10.0) / 42.0, 0.0, 1.0))
                if scope_wide_field_factor > 0.25:
                    halo_bin = np.where(halo_bin > 0, halo_bin - 1, halo_bin).astype(np.int8)
            if after_bucket > 0:
                max_halo_frac = 0.10 if scope_enabled else 0.04
                max_halo = max(10, int(after_bucket * max_halo_frac))
                bright_order = np.argsort(mag, kind="mergesort")
                allowed = np.zeros(after_bucket, dtype=bool)
                allowed[bright_order[:max_halo]] = True
                halo_bin = np.where(allowed, halo_bin, 0).astype(np.int8)

        # Alpha final u8 (binning interno)
        alpha_u8 = np.asarray(np.clip(np.rint(alpha * 255.0), 8.0, 255.0), dtype=np.int16)
        avg_alpha_u8 = float(np.mean(alpha_u8)) if len(alpha_u8) else 0.0
        halo_count = int(np.count_nonzero(halo_bin)) if len(halo_bin) else 0

        # -----------------------------
        # Paint
        # -----------------------------
        painter = ctx.painter
        painter.save()

        # Antialiasing dinámico: suaviza bordes cuando la carga lo permite.
        smooth_mode = after_bucket <= 3500
        painter.setRenderHint(QPainter.Antialiasing, bool(smooth_mode))
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        if len(mag):
            n_mag = int(len(mag))
            k05 = max(0, min(n_mag - 1, int(round((n_mag - 1) * 0.05))))
            k22 = max(0, min(n_mag - 1, int(round((n_mag - 1) * 0.22))))
            q = np.partition(np.asarray(mag, dtype=np.float32), (k05, k22))
            q05 = float(q[k05])
            q22 = float(q[k22])
        else:
            q05 = 1.5
            q22 = 4.0
        bright_cut = max(1.5, q05 + 0.35)
        medium_cut = max(4.0, q22 + 0.40)

        bright_mask = mag <= bright_cut
        medium_mask = (mag > bright_cut) & (mag <= medium_cut)
        weak_mask = mag > medium_cut

        weak_idx = np.where(weak_mask)[0]
        medium_idx = np.where(medium_mask)[0]
        sx_i = np.asarray(np.rint(sx), dtype=np.int32)
        sy_i = np.asarray(np.rint(sy), dtype=np.int32)

        if bp_rp is not None:
            bp_vals = np.asarray(bp_rp, dtype=np.float32)
            bp_vals = np.where(np.isfinite(bp_vals), bp_vals, 0.8)
        else:
            bp_vals = np.full(after_bucket, 0.8, dtype=np.float32)
        bp_bin = np.asarray(np.clip(np.rint((bp_vals + 0.5) * 10.0), 0.0, 70.0), dtype=np.int16)
        # Coarser alpha bins reduce style-run fragmentation without visible photometric jumps.
        alpha_bin = np.asarray((alpha_u8 // 16) * 16, dtype=np.int16)
        style_key = np.asarray(bp_bin * 256 + alpha_bin, dtype=np.int32)
        # Weak stars tolerate coarser style quantization, reducing draw calls significantly.
        weak_bp_bin = np.asarray((bp_bin // 4) * 4, dtype=np.int16)
        weak_alpha_bin = np.asarray((alpha_u8 // 32) * 32, dtype=np.int16)
        style_key_weak = np.asarray(weak_bp_bin * 256 + weak_alpha_bin, dtype=np.int32)

        def iter_style_runs(indices, key_source):
            if len(indices) == 0:
                return
            keys = key_source[indices]
            order = np.argsort(keys, kind="mergesort")
            idx_sorted = indices[order]
            key_sorted = keys[order]
            start = 0
            n_runs = len(idx_sorted)
            while start < n_runs:
                key = int(key_sorted[start])
                end = start + 1
                while end < n_runs and int(key_sorted[end]) == key:
                    end += 1
                yield key, idx_sorted[start:end]
                start = end

        try:
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        except Exception:
            pass
        painter.setPen(Qt.NoPen)

        # Weak stars: tiny soft disks (avoid square 1px point look).
        for key, run_idx in iter_style_runs(weak_idx, style_key_weak):
            bpb = int(key // 256)
            ab = int(key % 256)
            col = self._cached_color((bpb / 10.0) - 0.5, ab, pure_colors=pure_colors)
            r = 0.42 + 0.38 * (float(ab) / 255.0)
            sprite, center = self._cached_disc_sprite(col, r)
            c = int(round(center))
            for i in run_idx:
                painter.drawImage(int(sx_i[i] - c), int(sy_i[i] - c), sprite)

        # Mid stars: compact colored core, visible but clean.
        mid_span = max(0.2, medium_cut - bright_cut)
        medium_rel = np.asarray(np.clip((medium_cut - mag) / mid_span, 0.0, 1.0), dtype=np.float32)
        medium_r = np.asarray(0.85 + 0.95 * medium_rel, dtype=np.float32)
        for key, run_idx in iter_style_runs(medium_idx, style_key):
            bpb = int(key // 256)
            ab = int(key % 256)
            col = self._cached_color((bpb / 10.0) - 0.5, ab, pure_colors=pure_colors)
            for i in run_idx:
                sprite, center = self._cached_disc_sprite(col, float(medium_r[i]))
                c = int(round(center))
                painter.drawImage(int(sx_i[i] - c), int(sy_i[i] - c), sprite)

        # Bright stars: radius + halo (if not pure_colors / not interaction).
        bright_idx = np.where((halo_bin > 0) | bright_mask)[0]
        if len(bright_idx):
            bright_idx = bright_idx[np.argsort(mag[bright_idx], kind="mergesort")]
            max_bright = max(16, int(after_bucket * (0.14 if scope_enabled else 0.08)))
            bright_idx = bright_idx[:max_bright]

            for i in bright_idx:
                bp = float(bp_rp[i]) if bp_rp is not None else 0.8
                a = int(alpha_u8[i])
                hbin = int(halo_bin[i])

                r_core = float(clamp(1.10 + 0.55 * float(size_bin[i]), 1.0, 5.8))
                col = self._cached_color(bp, a, pure_colors=pure_colors)
                sprite, center = self._cached_bright_sprite(
                    color=col,
                    alpha_u8=a,
                    core_radius_px=r_core,
                    halo_bin=hbin,
                    pure_colors=pure_colors,
                )
                c = int(round(center))
                painter.drawImage(int(sx_i[i] - c), int(sy_i[i] - c), sprite)

        painter.restore()

        avg_radius = float(np.mean(size_bin)) if len(size_bin) else 0.0

        result = StarsRenderResult(
            visible_indices=np.asarray(catalog_idx, dtype=np.int32),
            visible_sx=np.asarray(sx, dtype=np.float32),
            visible_sy=np.asarray(sy, dtype=np.float32),
            total_in_view=total_in_view,
            after_mag_cut=after_mag_cut,
            after_bucket=after_bucket,
            avg_radius=avg_radius,
        )

        diag = getattr(ctx, "diagnostics", None)
        if diag is not None:
            diag.set_counter("total_in_view", total_in_view)
            diag.set_counter("after_mag_cut", after_mag_cut)
            diag.set_counter("after_bucket", after_bucket)
            diag.set_counter("density_cap", density_cap)
            diag.set_counter("state_mag_limit", round(float(getattr(state, "magnitude_limit", 0.0)), 3))
            diag.set_counter("limiting_mag", round(float(limiting_mag), 3))
            diag.set_counter("pre_limit", round(float(pre_limit), 3))
            diag.set_counter("avg_alpha_u8", round(float(avg_alpha_u8), 3))
            diag.set_counter("halo_count", halo_count)
            diag.set_counter("frame_mag_min", round(float(mag_min_frame), 3))
            diag.set_counter("bright_cut_mag", round(float(bright_cut), 3))
            diag.set_counter("medium_cut_mag", round(float(medium_cut), 3))
            diag.set_counter("weak_count", int(len(weak_idx)))
            diag.set_counter("medium_count", int(len(medium_idx)))
            diag.set_counter("scope_after_mag_mask", int(after_mag_cut))
            diag.set_counter("pure_colors", 1 if pure_colors else 0)
            diag.set_counter("interaction_active", 1 if interaction_active else 0)
            if scope_enabled:
                diag.set_counter("scope_alpha_gain", round(float(scope_alpha_gain), 3))
                diag.set_counter("scope_size_gain", round(float(scope_size_gain), 3))
                diag.set_counter("scope_signal_gain", round(float(scope_signal_gain), 3))
                if scope_is_camera:
                    diag.set_counter("scope_iso_factor", round(float(scope_iso_factor), 3))
                    diag.set_counter("scope_exposure_factor", round(float(scope_exposure_factor), 3))
                    diag.set_counter("scope_low_light_drive", round(float(low_light_drive), 3))
            diag.set_counter("avg_size_bin", round(float(avg_radius), 3))

        return result
