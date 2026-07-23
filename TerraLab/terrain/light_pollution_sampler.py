"""
light_pollution_sampler.py

Sampler for nighttime-lights rasters (DVNL-like products).

Coordinate contract:
    - User input coordinates are always geographic lat/lon in EPSG:4326.
    - Terrain/horizon internal coordinates are EPSG:25831 meters.
    - Raster sampling is always executed in the raster native CRS.
"""

import os
import threading
from pathlib import Path
from typing import Any, Optional, Tuple

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.windows import Window, from_bounds

from TerraLab.common.locks import RASTERIO_LOCK
from TerraLab.light_pollution.bortle import sqm_to_bortle_class
from TerraLab.terrain.crs import (
    DEFAULT_TRANSFORM_SERVICE,
    transformer_transform,
)
from TerraLab.terrain.providers import (
    CRS_GEOGRAPHIC,
    CRS_TERRAIN_INTERNAL,
)


class LightPollutionSampler:
    """
    Samples radiance/SQM/Bortle from a nighttime-lights raster.

    CRS policy:
        - Inputs in lat/lon are interpreted as EPSG:4326.
        - Inputs in terrain coordinates are interpreted as EPSG:25831 unless
          `input_crs` is explicitly provided.
        - Raster points are always projected to the raster native CRS.
        - If raster CRS is missing, a single explicit fallback policy is used.
    """

    DEFAULT_SQM = 21.0
    DEFAULT_BORTLE = 4
    # 2,048 float32 pixels per side are 16 MiB.  This covers the configured
    # 530 km horizon in the bundled 1 km DVNL raster without truncating its
    # outer samples, while still bounding accidental fine-resolution reads.
    MAX_SQM_WINDOW_PIXELS = 2048
    DIRECT_BATCH_TILE_PIXELS = 512

    def __init__(
        self,
        raster_path: str = None,
        radius_km: float = 5.0,
        res_km: float = 0.5,
        terrain_crs: str = CRS_TERRAIN_INTERNAL,
    ):
        """
        Initialize the light-pollution sampler.

        Args:
            raster_path: Path to the nighttime-lights GeoTIFF.
            radius_km: Sampling radius for SQM estimation around a point.
            res_km: Kernel resolution (km) used by SQM aggregation.
            terrain_crs: Terrain internal CRS used by horizon engine (default EPSG:25831).
        """
        self.raster_path = raster_path
        self.radius_km = float(radius_km)
        self.res_km = float(res_km)
        self.terrain_crs = str(terrain_crs or CRS_TERRAIN_INTERNAL)

        # Cache for the current region loaded in raster native CRS.
        self._cached_data = None
        self._cached_transform = None
        self._cached_bounds = None  # (minx, miny, maxx, maxy) in raster CRS

        # Thread-safety for cached data and transformers.
        self._lock = threading.Lock()

        # SQM aggregation profile (Gaussian) in kilometers.
        self._sqm_sigma_km = 1.5

        # CRS/runtime context
        self._src_crs = None
        self._is_geographic = False
        self._tr_geo_to_src = None
        self._tr_terrain_to_src = None
        self._terrain_crs_for_transform = self.terrain_crs

        debug_values = (
            os.getenv("TERRALAB_LP_DEBUG", ""),
            os.getenv("TERRALAB_CRS_DEBUG", ""),
        )
        self._debug_enabled = any(
            str(value).strip().lower() in {"1", "true", "yes", "on"}
            for value in debug_values
        )

    def _debug(self, message: str) -> None:
        if self._debug_enabled:
            print(f"[LPSampler:CRS] {message}")

    def _trace_estimate_result(
        self,
        lat: float,
        lon: float,
        result: Tuple[float, int],
        source: str,
    ) -> Tuple[float, int]:
        sqm, bortle = float(result[0]), int(result[1])
        print(
            "[LPSampler:Estimate] "
            f"source={source} lat={float(lat):.6f} lon={float(lon):.6f} "
            f"sqm={sqm:.3f} bortle={bortle}"
        )
        return sqm, bortle

    @staticmethod
    def _default_sqm_bortle() -> Tuple[float, int]:
        return LightPollutionSampler.DEFAULT_SQM, LightPollutionSampler.DEFAULT_BORTLE

    @staticmethod
    def _guess_missing_crs(src) -> CRS:
        """
        Guess raster CRS when metadata is missing.

        Policy (single explicit fallback):
            1) Geographic-looking world extents -> EPSG:4326.
            2) Projected world extents:
               - DVNL-like Equal Earth extent/name/grid -> EPSG:8857.
               - Otherwise full-world metric extent -> EPSG:3857.
            3) Conservative fallback -> EPSG:4326.
        """
        try:
            b = src.bounds
            x_span = abs(float(b.right) - float(b.left))
            y_span = abs(float(b.top) - float(b.bottom))
            max_abs = max(
                abs(float(b.left)),
                abs(float(b.right)),
                abs(float(b.top)),
                abs(float(b.bottom)),
            )
            src_name = str(getattr(src, "name", "") or "").lower()
            src_w = int(getattr(src, "width", 0) or 0)
            src_h = int(getattr(src, "height", 0) or 0)

            if max_abs <= 360.0 and x_span <= 360.0 and y_span <= 180.0:
                return CRS.from_epsg(4326)

            if (
                max_abs <= 21000000.0
                and x_span > 1000000.0
                and y_span > 1000000.0
            ):
                likely_equal_earth_extent = (
                    33000000.0 <= x_span <= 36000000.0
                    and 12000000.0 <= y_span <= 18000000.0
                )
                likely_dvnl_name = any(
                    token in src_name
                    for token in ("dvnl", "light_pollution", "night")
                )
                likely_dvnl_grid = (
                    34000 <= src_w <= 35000 and 14500 <= src_h <= 16000
                )
                if (
                    likely_equal_earth_extent
                    or likely_dvnl_name
                    or likely_dvnl_grid
                ):
                    return CRS.from_epsg(8857)

                if 38000000.0 <= x_span <= 41000000.0:
                    return CRS.from_epsg(3857)
                return CRS.from_epsg(3857)
        except Exception:
            pass
        return CRS.from_epsg(4326)

    def _resolve_src_crs(self, src) -> CRS:
        """
        Resolve raster native CRS from metadata or fallback policy.

        Input:
            - Open rasterio dataset.
        Output:
            - CRS used as source-of-truth for raster sampling.
        """
        if src.crs is not None:
            return src.crs
        guessed = self._guess_missing_crs(src)
        src_name = str(getattr(src, "name", "") or "<unknown>")
        print(
            f"[LPSampler] Warning: raster without CRS ({src_name}). Using guessed CRS={guessed}."
        )
        return guessed

    def _build_runtime_context(
        self, src, terrain_crs: Optional[str] = None
    ) -> dict:
        """
        Build runtime CRS context for transformations.

        Inputs:
            - Geographic input CRS: EPSG:4326.
            - Terrain input CRS: EPSG:25831 unless overridden.
        Output:
            - Context with raster CRS and explicit transformers using always_xy.
        """
        resolved_src_crs = self._resolve_src_crs(src)
        terrain_crs_final = str(
            terrain_crs or self._terrain_crs_for_transform or self.terrain_crs
        )
        resolved_src_key = str(resolved_src_crs)
        terrain_key = str(terrain_crs_final)

        with self._lock:
            cached_src_crs = self._src_crs
            cached_src_key = str(cached_src_crs) if cached_src_crs is not None else ""
            cached_terrain_key = str(self._terrain_crs_for_transform or "")
            if (
                cached_src_key == resolved_src_key
                and cached_terrain_key == terrain_key
                and self._tr_geo_to_src is not None
                and self._tr_terrain_to_src is not None
            ):
                return {
                    "src_crs": cached_src_crs,
                    "is_geographic": bool(self._is_geographic),
                    "terrain_crs": terrain_crs_final,
                    "tr_geo_to_src": self._tr_geo_to_src,
                    "tr_terrain_to_src": self._tr_terrain_to_src,
                }

        tr_geo_to_src = DEFAULT_TRANSFORM_SERVICE.transformer(
            CRS_GEOGRAPHIC, resolved_src_crs
        )
        tr_terrain_to_src = DEFAULT_TRANSFORM_SERVICE.transformer(
            terrain_crs_final, resolved_src_crs
        )

        return {
            "src_crs": resolved_src_crs,
            "is_geographic": bool(
                getattr(resolved_src_crs, "is_geographic", False)
            ),
            "terrain_crs": terrain_crs_final,
            "tr_geo_to_src": tr_geo_to_src,
            "tr_terrain_to_src": tr_terrain_to_src,
        }

    def _update_context_locked(self, context: dict) -> None:
        self._src_crs = context["src_crs"]
        self._is_geographic = bool(context["is_geographic"])
        self._tr_geo_to_src = context["tr_geo_to_src"]
        self._tr_terrain_to_src = context["tr_terrain_to_src"]
        self._terrain_crs_for_transform = str(context["terrain_crs"])

    def _get_cached_context(
        self,
        terrain_crs: Optional[str] = None,
        require_terrain_transform: bool = False,
    ) -> Optional[dict]:
        terrain_crs_final = str(
            terrain_crs or self._terrain_crs_for_transform or self.terrain_crs
        )
        with self._lock:
            if self._src_crs is None or self._tr_geo_to_src is None:
                return None
            if require_terrain_transform:
                if self._tr_terrain_to_src is None:
                    return None
                if str(self._terrain_crs_for_transform or "") != terrain_crs_final:
                    return None
            return {
                "src_crs": self._src_crs,
                "is_geographic": bool(self._is_geographic),
                "terrain_crs": str(self._terrain_crs_for_transform or terrain_crs_final),
                "tr_geo_to_src": self._tr_geo_to_src,
                "tr_terrain_to_src": self._tr_terrain_to_src,
            }

    def _radius_to_raster_units(self, radius_m: float, is_geographic: bool) -> float:
        if is_geographic:
            return float(radius_m) / 111320.0
        return float(radius_m)

    @staticmethod
    def _window_bounds_from_transform(
        trans_win, width: int, height: int
    ) -> Tuple[float, float, float, float]:
        """
        Compute native CRS bounds for a cached read window.

        Input CRS:
            - Window transform coordinates in raster native CRS.
        Output CRS:
            - Returns `(minx, miny, maxx, maxy)` in raster native CRS.
        """
        left = float(trans_win.c)
        top = float(trans_win.f)
        right = left + float(trans_win.a) * float(width)
        bottom = top + float(trans_win.e) * float(height)
        minx = min(left, right)
        maxx = max(left, right)
        miny = min(bottom, top)
        maxy = max(bottom, top)
        return (minx, miny, maxx, maxy)

    def _read_window_around_point(
        self, src, x_cen: float, y_cen: float, radius_m: float, is_geographic: bool
    ) -> Tuple[np.ndarray, object, Tuple[float, float, float, float]]:
        """
        Read an integer-aligned native raster window around a source-space point.

        Input CRS:
            - `x_cen`, `y_cen` in raster native CRS.
            - `radius_m` in meters.
        Internal sampling CRS:
            - Raster native CRS.
        Output:
            - `(data, transform, bounds)` where bounds are native CRS extents.
        """
        from rasterio.windows import Window

        radius_native = self._radius_to_raster_units(radius_m, is_geographic)
        bounds = (
            x_cen - radius_native,
            y_cen - radius_native,
            x_cen + radius_native,
            y_cen + radius_native,
        )
        window = from_bounds(*bounds, transform=src.transform)
        dataset_window = Window(
            col_off=0, row_off=0, width=src.width, height=src.height
        )
        window = window.intersection(dataset_window)
        window = window.round_offsets().round_lengths()
        if window.width <= 0 or window.height <= 0:
            return (
                np.empty((0, 0), dtype=np.float32),
                src.transform,
                (0.0, 0.0, 0.0, 0.0),
            )

        # Safety: guard against pathological windows (CRS mismatch/very fine rasters)
        # to avoid OOM crashes in UI-triggered SQM evaluation.
        max_side = int(max(64, self.MAX_SQM_WINDOW_PIXELS))
        if int(window.width) > max_side or int(window.height) > max_side:
            try:
                row_q, col_q = src.index(x_cen, y_cen)
            except Exception:
                row_q = int(float(window.row_off) + float(window.height) * 0.5)
                col_q = int(float(window.col_off) + float(window.width) * 0.5)

            row_q = max(0, min(int(src.height) - 1, int(row_q)))
            col_q = max(0, min(int(src.width) - 1, int(col_q)))
            half = max_side // 2
            row0 = max(0, row_q - half)
            row1 = min(int(src.height), row_q + half + 1)
            col0 = max(0, col_q - half)
            col1 = min(int(src.width), col_q + half + 1)
            window = Window(
                col_off=int(col0),
                row_off=int(row0),
                width=int(max(1, col1 - col0)),
                height=int(max(1, row1 - row0)),
            )
            self._debug(
                f"SQM window clipped to {int(window.width)}x{int(window.height)} "
                f"around row/col=({row_q},{col_q})"
            )

        data = src.read(1, window=window).astype(np.float32)
        trans_win = src.window_transform(window)
        bounds_native = self._window_bounds_from_transform(
            trans_win, int(data.shape[1]), int(data.shape[0])
        )
        data[data < 0] = 0.0
        data[data > 1e10] = 0.0
        return data, trans_win, bounds_native

    def _extract_cached_pixel_locked(
        self, x_src: float, y_src: float
    ) -> Optional[float]:
        if (
            self._cached_data is None
            or self._cached_transform is None
            or self._cached_bounds is None
        ):
            return None

        b = self._cached_bounds
        if not (b[0] <= x_src <= b[2] and b[1] <= y_src <= b[3]):
            self._debug(
                f"OOB cache pixel x={x_src:.3f}, y={y_src:.3f}, bounds={b}"
            )
            return None

        inv = ~self._cached_transform
        c_float, r_float = inv * (x_src, y_src)
        r = int(r_float)
        c = int(c_float)
        if (
            0 <= r < self._cached_data.shape[0]
            and 0 <= c < self._cached_data.shape[1]
        ):
            val = float(self._cached_data[r, c])
            if np.isnan(val) or val < 0 or val > 1e10:
                return 0.0
            return val
        return None

    def _read_single_pixel_from_source(self, src, x_src: float, y_src: float) -> float:
        row, col = src.index(x_src, y_src)
        if not (0 <= row < src.height and 0 <= col < src.width):
            self._debug(
                f"OOB source pixel x={x_src:.3f}, y={y_src:.3f}, row={row}, col={col}"
            )
            return 0.0
        arr = src.read(1, window=((row, row + 1), (col, col + 1))).astype(
            np.float32
        )
        val = float(arr[0, 0])
        nodata = src.nodata
        if nodata is not None and np.isfinite(nodata) and val == float(nodata):
            return 0.0
        if np.isnan(val) or val < 0 or val > 1e10:
            return 0.0
        return val

    def _cache_window(self, data, trans_win, bounds, context: dict) -> None:
        with self._lock:
            self._update_context_locked(context)
            self._cached_data = data
            self._cached_transform = trans_win
            self._cached_bounds = bounds

    def estimate_zenith_sqm(self, lat: float, lon: float) -> Tuple[float, int]:
        """
        Estimate zenith SQM/Bortle for a user geographic coordinate.

        Input CRS:
            - `lat`, `lon` in EPSG:4326.
        Internal sampling CRS:
            - Raster native CRS (resolved from raster metadata/fallback policy).
        Output:
            - `(sqm, bortle)` where bortle is mapped from SQM thresholds.
            - Returns default `(21.0, 4)` on controlled errors.
        """
        if not self.raster_path or not os.path.exists(self.raster_path):
            return self._trace_estimate_result(
                lat,
                lon,
                self._default_sqm_bortle(),
                "fallback_missing_raster",
            )

        try:
            with self._lock:
                if (
                    self._cached_data is not None
                    and self._cached_transform is not None
                    and self._src_crs is not None
                    and self._tr_geo_to_src is not None
                    and self._cached_bounds is not None
                ):
                    x_src, y_src = transformer_transform(
                        self._tr_geo_to_src, lon, lat
                    )
                    self._debug(
                        f"estimate cache hit try lat/lon=({lat:.6f},{lon:.6f}) "
                        f"-> src=({x_src:.3f},{y_src:.3f})"
                    )
                    b = self._cached_bounds
                    if b[0] <= x_src <= b[2] and b[1] <= y_src <= b[3]:
                        return self._trace_estimate_result(
                            lat,
                            lon,
                            self._process_array_to_sqm(
                                arr=self._cached_data,
                                trans_win=self._cached_transform,
                                x_src=x_src,
                                y_src=y_src,
                                is_geographic=bool(self._is_geographic),
                            ),
                            "cache",
                        )

            with RASTERIO_LOCK:
                with rasterio.open(self.raster_path) as src:
                    context = self._get_cached_context()
                    if context is None:
                        context = self._build_runtime_context(src)
                    x_src, y_src = transformer_transform(
                        context["tr_geo_to_src"], lon, lat
                    )
                    self._debug(
                        f"estimate direct lat/lon=({lat:.6f},{lon:.6f}) -> "
                        f"src=({x_src:.3f},{y_src:.3f}) src_crs={context['src_crs']}"
                    )
                    arr, trans_win, _ = self._read_window_around_point(
                        src=src,
                        x_cen=x_src,
                        y_cen=y_src,
                        radius_m=self.radius_km * 1000.0,
                        is_geographic=context["is_geographic"],
                    )
                    with self._lock:
                        self._update_context_locked(context)
                    return self._trace_estimate_result(
                        lat,
                        lon,
                        self._process_array_to_sqm(
                            arr=arr,
                            trans_win=trans_win,
                            x_src=x_src,
                            y_src=y_src,
                            is_geographic=bool(context["is_geographic"]),
                        ),
                        "raster",
                    )
        except Exception as e:
            print(f"[LPSampler] estimate_zenith_sqm error: {e}")
            return self._trace_estimate_result(
                lat,
                lon,
                self._default_sqm_bortle(),
                "fallback_error",
            )

    def _process_array_to_sqm(
        self,
        arr: np.ndarray,
        trans_win,
        x_src: float,
        y_src: float,
        is_geographic: bool,
    ) -> Tuple[float, int]:
        """
        Convert a native raster window into SQM/Bortle for one source-space point.

        Input CRS:
            - `arr` and `trans_win` in raster native CRS.
            - `x_src`, `y_src` in raster native CRS.
        Output:
            - `(sqm, bortle_class)` or default on controlled errors.
        """
        try:
            if arr.size == 0:
                return self._default_sqm_bortle()

            arr_clean = arr.astype(np.float32, copy=True)
            arr_clean[(arr_clean < 0.0) | (arr_clean > 1e6)] = np.nan
            if not np.any(np.isfinite(arr_clean)):
                return self._default_sqm_bortle()

            rows, cols = arr_clean.shape
            col_idx = np.arange(cols, dtype=np.float64) + 0.5
            row_idx = np.arange(rows, dtype=np.float64) + 0.5
            cc, rr = np.meshgrid(col_idx, row_idx)
            xs, ys = trans_win * (cc, rr)

            if is_geographic:
                mean_lat = float(y_src)
                m_per_deg_x = 111320.0 * np.cos(np.deg2rad(mean_lat))
                m_per_deg_x = max(abs(m_per_deg_x), 1.0)
                m_per_deg_y = 111320.0
                dx_m = (xs - float(x_src)) * m_per_deg_x
                dy_m = (ys - float(y_src)) * m_per_deg_y
            else:
                dx_m = xs - float(x_src)
                dy_m = ys - float(y_src)

            dist_km = np.sqrt(dx_m * dx_m + dy_m * dy_m) / 1000.0
            radius_km = max(float(self.radius_km), 1e-6)
            sigma_km = max(float(self._sqm_sigma_km), 1e-6)
            weights = np.exp(-(dist_km * dist_km) / (2.0 * sigma_km * sigma_km))
            weights[dist_km > radius_km] = 0.0

            valid_mask = np.isfinite(arr_clean) & np.isfinite(weights) & (weights > 0.0)
            if not np.any(valid_mask):
                return self._default_sqm_bortle()

            weighted_sum = np.nansum(arr_clean[valid_mask] * weights[valid_mask])
            weight_sum = np.nansum(weights[valid_mask])
            if not np.isfinite(weight_sum) or float(weight_sum) <= 0.0:
                return self._default_sqm_bortle()
            agg_val = weighted_sum / weight_sum

            val = max(float(agg_val), 1e-5)
            sqm = 22.0 - 2.5 * np.log10(val + 0.001)
            sqm = np.clip(sqm, 16.0, 22.0)

            return float(sqm), sqm_to_bortle_class(float(sqm))
        except Exception as e:
            print(f"[LPSampler] Internal Error: {e}")
            return self._default_sqm_bortle()

    def prepare_region(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        input_crs: str = CRS_TERRAIN_INTERNAL,
    ) -> None:
        """
        Preload raster ROI around a geographic query point.

        Input CRS:
            - `lat`, `lon` in EPSG:4326.
            - `input_crs` defines terrain CRS for projected queries (default EPSG:25831).
        Internal sampling CRS:
            - Raster native CRS.
        """
        if not self.raster_path or not os.path.exists(self.raster_path):
            return

        try:
            radius_m = max(1.0, float(radius_km) * 1000.0)
            self._debug(
                f"prepare_region lat/lon=({lat:.6f},{lon:.6f}) "
                f"radius_km={float(radius_km):.3f}"
            )
            with RASTERIO_LOCK:
                with rasterio.open(self.raster_path) as src:
                    context = self._build_runtime_context(src, terrain_crs=input_crs)
                    x_src, y_src = transformer_transform(
                        context["tr_geo_to_src"], lon, lat
                    )
                    data, trans_win, bounds = self._read_window_around_point(
                        src=src,
                        x_cen=x_src,
                        y_cen=y_src,
                        radius_m=radius_m,
                        is_geographic=context["is_geographic"],
                    )
                    self._cache_window(data, trans_win, bounds, context)
            self._debug(
                f"prepare_region cached shape={self._cached_data.shape} "
                f"src_bounds={self._cached_bounds}"
            )
        except Exception as e:
            print(f"[LPSampler] Cache Error: {e}")
            with self._lock:
                self._cached_data = None

    def prepare_region_from_terrain_xy(
        self,
        x_terrain: float,
        y_terrain: float,
        radius_m: float,
        input_crs: str = CRS_TERRAIN_INTERNAL,
    ) -> None:
        """
        Preload raster ROI around a terrain/horizon internal point.

        Input CRS:
            - `x_terrain`, `y_terrain` in `input_crs` (default EPSG:25831).
        Internal sampling CRS:
            - Raster native CRS.
        """
        if not self.raster_path or not os.path.exists(self.raster_path):
            return

        try:
            radius_m = max(1.0, float(radius_m))
            self._debug(
                f"prepare_region_from_terrain_xy x/y=({x_terrain:.3f},{y_terrain:.3f}) "
                f"input_crs={input_crs} radius_m={radius_m:.1f}"
            )
            with RASTERIO_LOCK:
                with rasterio.open(self.raster_path) as src:
                    context = self._get_cached_context(
                        terrain_crs=input_crs,
                        require_terrain_transform=True,
                    )
                    if context is None:
                        context = self._build_runtime_context(
                            src, terrain_crs=input_crs
                        )
                    x_src, y_src = transformer_transform(
                        context["tr_terrain_to_src"], x_terrain, y_terrain
                    )
                    data, trans_win, bounds = self._read_window_around_point(
                        src=src,
                        x_cen=x_src,
                        y_cen=y_src,
                        radius_m=radius_m,
                        is_geographic=context["is_geographic"],
                    )
                    self._cache_window(data, trans_win, bounds, context)
            self._debug(
                f"terrain->raster ({x_terrain:.3f},{y_terrain:.3f}) -> "
                f"({x_src:.3f},{y_src:.3f}) src_crs={context['src_crs']}"
            )
        except Exception as e:
            print(f"[LPSampler] Cache Error: {e}")
            with self._lock:
                self._cached_data = None

    def get_radiance(self, lat: float, lon: float) -> float:
        """
        Sample radiance for geographic coordinates.

        Input CRS:
            - `lat`, `lon` in EPSG:4326.
        Sampling CRS:
            - Raster native CRS.
        Returns:
            - Radiance value or `0.0` when out-of-bounds/error.
        """
        if not self.raster_path or not os.path.exists(self.raster_path):
            return 0.0

        try:
            with self._lock:
                if (
                    self._cached_data is not None
                    and self._tr_geo_to_src is not None
                    and self._cached_bounds is not None
                ):
                    x_src, y_src = transformer_transform(
                        self._tr_geo_to_src, lon, lat
                    )
                    val = self._extract_cached_pixel_locked(x_src, y_src)
                    if val is not None:
                        return float(val)

            with RASTERIO_LOCK:
                with rasterio.open(self.raster_path) as src:
                    context = self._get_cached_context()
                    if context is None:
                        context = self._build_runtime_context(src)
                    x_src, y_src = transformer_transform(
                        context["tr_geo_to_src"], lon, lat
                    )
                    with self._lock:
                        self._update_context_locked(context)
                    return float(
                        self._read_single_pixel_from_source(src, x_src, y_src)
                    )
        except Exception:
            return 0.0

    def get_radiance_terrain_xy(
        self,
        x_terrain: float,
        y_terrain: float,
        input_crs: str = CRS_TERRAIN_INTERNAL,
    ) -> float:
        """
        Sample radiance for terrain/horizon projected coordinates.

        Input CRS:
            - `x_terrain`, `y_terrain` in `input_crs` (default EPSG:25831).
        Sampling CRS:
            - Raster native CRS.
        Returns:
            - Radiance value or `0.0` when out-of-bounds/error.
        """
        if not self.raster_path or not os.path.exists(self.raster_path):
            return 0.0

        try:
            with self._lock:
                same_input_crs = (
                    str(input_crs or CRS_TERRAIN_INTERNAL)
                    == str(self._terrain_crs_for_transform or "")
                )
                if (
                    same_input_crs
                    and self._cached_data is not None
                    and self._tr_terrain_to_src is not None
                    and self._cached_bounds is not None
                ):
                    x_src, y_src = transformer_transform(
                        self._tr_terrain_to_src, x_terrain, y_terrain
                    )
                    val = self._extract_cached_pixel_locked(x_src, y_src)
                    if val is not None:
                        return float(val)

            with RASTERIO_LOCK:
                with rasterio.open(self.raster_path) as src:
                    context = self._get_cached_context(
                        terrain_crs=input_crs,
                        require_terrain_transform=True,
                    )
                    if context is None:
                        context = self._build_runtime_context(
                            src, terrain_crs=input_crs
                        )
                    x_src, y_src = transformer_transform(
                        context["tr_terrain_to_src"], x_terrain, y_terrain
                    )
                    with self._lock:
                        self._update_context_locked(context)
                    return float(
                        self._read_single_pixel_from_source(src, x_src, y_src)
                    )
        except Exception:
            return 0.0

    def get_radiance_terrain_xy_batch(
        self, x_terrain, y_terrain, input_crs: str = CRS_TERRAIN_INTERNAL
    ) -> np.ndarray:
        """Vectorized cached-window radiance sampling in terrain coordinates."""
        x_arr, y_arr = np.broadcast_arrays(
            np.asarray(x_terrain, dtype=np.float64),
            np.asarray(y_terrain, dtype=np.float64),
        )
        output = np.zeros(x_arr.shape, dtype=np.float32)
        with self._lock:
            ready = (
                str(input_crs or CRS_TERRAIN_INTERNAL)
                == str(self._terrain_crs_for_transform or "")
                and self._cached_data is not None
                and self._tr_terrain_to_src is not None
                and self._cached_bounds is not None
                and self._cached_transform is not None
            )
            if ready:
                x_src, y_src = transformer_transform(
                    self._tr_terrain_to_src, x_arr, y_arr
                )
                bounds = self._cached_bounds
                inside = (
                    (x_src >= bounds[0]) & (x_src <= bounds[2])
                    & (y_src >= bounds[1]) & (y_src <= bounds[3])
                )
                inv = ~self._cached_transform
                cols, rows = inv * (x_src, y_src)
                row_i = rows.astype(np.int64)
                col_i = cols.astype(np.int64)
                inside &= (
                    (row_i >= 0) & (col_i >= 0)
                    & (row_i < self._cached_data.shape[0])
                    & (col_i < self._cached_data.shape[1])
                )
                if np.any(inside):
                    values = self._cached_data[row_i[inside], col_i[inside]]
                    output[inside] = np.where(
                        np.isfinite(values)
                        & (values >= 0.0)
                        & (values <= 1e10),
                        values,
                        0.0,
                    ).astype(np.float32, copy=False)
                return output

        # Defensive path for callers that did not preload a region.  Keep the
        # operation batched: open the raster once and gather points from a
        # bounded set of 512x512 windows.  Never degrade to one raster open and
        # one read for every candidate point.
        if not self.raster_path or not os.path.exists(self.raster_path):
            return output
        try:
            with RASTERIO_LOCK:
                with rasterio.open(self.raster_path) as src:
                    context = self._get_cached_context(
                        terrain_crs=input_crs,
                        require_terrain_transform=True,
                    )
                    if context is None:
                        context = self._build_runtime_context(
                            src, terrain_crs=input_crs
                        )
                    x_src, y_src = transformer_transform(
                        context["tr_terrain_to_src"], x_arr, y_arr
                    )
                    inv = ~src.transform
                    cols, rows = inv * (x_src, y_src)
                    finite = np.isfinite(rows) & np.isfinite(cols)
                    row_i = np.zeros(x_arr.shape, dtype=np.int64)
                    col_i = np.zeros(x_arr.shape, dtype=np.int64)
                    row_i[finite] = np.floor(rows[finite]).astype(np.int64)
                    col_i[finite] = np.floor(cols[finite]).astype(np.int64)
                    inside = (
                        finite
                        & (row_i >= 0)
                        & (col_i >= 0)
                        & (row_i < int(src.height))
                        & (col_i < int(src.width))
                    )
                    flat_positions = np.flatnonzero(inside)
                    if flat_positions.size:
                        flat_rows = row_i.ravel()[flat_positions]
                        flat_cols = col_i.ravel()[flat_positions]
                        tile_side = int(max(64, self.DIRECT_BATCH_TILE_PIXELS))
                        tile_columns = int(
                            np.ceil(float(src.width) / float(tile_side))
                        )
                        tile_keys = (
                            (flat_rows // tile_side) * tile_columns
                            + (flat_cols // tile_side)
                        )
                        for tile_key in np.unique(tile_keys):
                            selected = tile_keys == tile_key
                            positions = flat_positions[selected]
                            rows_selected = flat_rows[selected]
                            cols_selected = flat_cols[selected]
                            row0 = int((rows_selected[0] // tile_side) * tile_side)
                            col0 = int((cols_selected[0] // tile_side) * tile_side)
                            height = min(tile_side, int(src.height) - row0)
                            width = min(tile_side, int(src.width) - col0)
                            data = src.read(
                                1,
                                window=Window(col0, row0, width, height),
                            ).astype(np.float32, copy=False)
                            values = data[
                                rows_selected - row0,
                                cols_selected - col0,
                            ]
                            valid_values = (
                                np.isfinite(values)
                                & (values >= 0.0)
                                & (values <= 1e10)
                            )
                            output.ravel()[positions] = np.where(
                                valid_values, values, 0.0
                            ).astype(np.float32, copy=False)
                    with self._lock:
                        self._update_context_locked(context)
            return output
        except Exception:
            return output

    def close(self) -> None:
        """
        Release in-memory cached raster state and transformers.

        All cached coordinates are in raster native CRS.
        """
        with self._lock:
            self._cached_data = None
            self._cached_transform = None
            self._cached_bounds = None
            self._src_crs = None
            self._is_geographic = False
            self._tr_geo_to_src = None
            self._tr_terrain_to_src = None


def _source_value(source: Any, name: str, default=None):
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _expand_light_paths(value: Any) -> list[str]:
    raw_values = value if isinstance(value, (list, tuple)) else [value]
    paths: list[str] = []
    for raw in raw_values:
        path = Path(str(raw or "")).expanduser()
        if path.is_dir():
            candidates = sorted(
                (
                    child.resolve()
                    for child in path.rglob("*")
                    if child.is_file() and child.suffix.lower() in {".tif", ".tiff"}
                ),
                key=lambda child: str(child).lower(),
            )
            paths.extend(str(child) for child in candidates)
        elif path.is_file():
            paths.append(str(path.resolve()))
    return paths


def snapshot_light_pollution_sources(sources: Any) -> list[dict[str, Any]]:
    """Create a process-safe, ordered snapshot of typed DVNL sources."""

    sequence = sources if isinstance(sources, (list, tuple)) else [sources]
    result = []
    for index, source in enumerate(sequence):
        if source is None or not bool(_source_value(source, "enabled", True)):
            continue
        raw_paths = _source_value(source, "paths", None)
        if raw_paths is None:
            raw_paths = _source_value(source, "path", "")
        paths = _expand_light_paths(raw_paths)
        if not paths:
            continue
        result.append(
            {
                "id": str(_source_value(source, "id", f"light-{index}")),
                "paths": paths,
                "fingerprint": str(_source_value(source, "fingerprint", "") or ""),
            }
        )
    return result


class LightPollutionSamplerChain:
    """Nodata-aware per-point fallback across ordered DVNL rasters."""

    def __init__(self, samplers: list[LightPollutionSampler]) -> None:
        self.samplers = tuple(samplers)
        self.raster_path = self.samplers[0].raster_path if self.samplers else ""

    @staticmethod
    def _sample_optional(
        sampler: LightPollutionSampler,
        first: float,
        second: float,
        *,
        input_crs: str,
    ) -> float | None:
        path = str(sampler.raster_path or "")
        if not path or not os.path.isfile(path):
            return None
        try:
            with RASTERIO_LOCK, rasterio.open(path) as src:
                context = sampler._build_runtime_context(src, terrain_crs=input_crs)
                if input_crs == CRS_GEOGRAPHIC:
                    x_src, y_src = transformer_transform(
                        context["tr_geo_to_src"], second, first
                    )
                else:
                    x_src, y_src = transformer_transform(
                        context["tr_terrain_to_src"], first, second
                    )
                row, col = src.index(x_src, y_src)
                if not (0 <= row < src.height and 0 <= col < src.width):
                    return None
                sample = src.read(
                    1,
                    window=((row, row + 1), (col, col + 1)),
                    masked=True,
                )
                if sample.size != 1 or bool(np.ma.getmaskarray(sample)[0, 0]):
                    return None
                value = float(sample[0, 0])
                if not np.isfinite(value) or value < 0.0 or value > 1e10:
                    return None
                return value
        except Exception:
            return None

    def get_radiance(self, lat: float, lon: float) -> float:
        for sampler in self.samplers:
            value = self._sample_optional(
                sampler, lat, lon, input_crs=CRS_GEOGRAPHIC
            )
            if value is not None:
                return float(value)
        return 0.0

    def get_radiance_terrain_xy(
        self,
        x_terrain: float,
        y_terrain: float,
        input_crs: str = CRS_TERRAIN_INTERNAL,
    ) -> float:
        for sampler in self.samplers:
            value = self._sample_optional(
                sampler, x_terrain, y_terrain, input_crs=input_crs
            )
            if value is not None:
                return float(value)
        return 0.0

    def get_radiance_terrain_xy_batch(
        self, x_terrain, y_terrain, input_crs: str = CRS_TERRAIN_INTERNAL
    ) -> np.ndarray:
        x_arr, y_arr = np.broadcast_arrays(
            np.asarray(x_terrain), np.asarray(y_terrain)
        )
        result = np.zeros(x_arr.shape, dtype=np.float32)
        unresolved = np.ones(x_arr.shape, dtype=bool)
        for sampler in self.samplers:
            if not np.any(unresolved):
                break
            positions = np.flatnonzero(unresolved)
            values = sampler.get_radiance_terrain_xy_batch(
                x_arr.flat[positions], y_arr.flat[positions], input_crs
            )
            chosen = np.isfinite(values) & (values > 0.0)
            result.flat[positions[chosen]] = values[chosen]
            unresolved.flat[positions[chosen]] = False
        return result

    def estimate_zenith_sqm(self, lat: float, lon: float) -> Tuple[float, int]:
        for sampler in self.samplers:
            if self._sample_optional(
                sampler, lat, lon, input_crs=CRS_GEOGRAPHIC
            ) is not None:
                return sampler.estimate_zenith_sqm(lat, lon)
        return LightPollutionSampler._default_sqm_bortle()

    def prepare_region(self, *args, **kwargs) -> None:
        for sampler in self.samplers:
            sampler.prepare_region(*args, **kwargs)

    def prepare_region_from_terrain_xy(self, *args, **kwargs) -> None:
        for sampler in self.samplers:
            sampler.prepare_region_from_terrain_xy(*args, **kwargs)

    def close(self) -> None:
        for sampler in self.samplers:
            sampler.close()


def create_light_pollution_sampler(sources: Any) -> LightPollutionSampler | LightPollutionSamplerChain:
    snapshot = snapshot_light_pollution_sources(sources)
    samplers = [
        LightPollutionSampler(path)
        for source in snapshot
        for path in source["paths"]
    ]
    if len(samplers) == 1:
        return samplers[0]
    return LightPollutionSamplerChain(samplers)


__all__ = [
    "LightPollutionSampler",
    "LightPollutionSamplerChain",
    "create_light_pollution_sampler",
    "snapshot_light_pollution_sources",
]
