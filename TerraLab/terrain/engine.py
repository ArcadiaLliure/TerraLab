"""
horizon_engine.py — Real topographic horizon profile engine.

Loads ICGC 5×5 DEM tiles (ESRI ASCII Grid .txt), caches them as .npy,
and computes multi-band horizon silhouettes via raycasting with Earth
curvature correction.

"""

import os
import glob
import math
import numpy as np
import pandas as pd
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

# --- Constants ---
R_EARTH = 6_371_000.0

# --- Band definitions for multi-layer depth ---
# --- Band definitions for multi-layer depth (20 Bands) ---
# Denser in near/mid range to resolve foreground roughness
DEFAULT_BANDS = [
    # Immediate Ground (0-1km) - 3 bands
    {"id": "gnd_0_250",    "min": 0,       "max": 250},
    {"id": "gnd_250_500",  "min": 250,     "max": 500},
    {"id": "gnd_500_1k",   "min": 500,     "max": 1000},
    
    # Near Hills (1-5km) - 5 bands
    {"id": "near_1_1.5",   "min": 1000,    "max": 1500},
    {"id": "near_1.5_2",   "min": 1500,    "max": 2000},
    {"id": "near_2_3",     "min": 2000,    "max": 3000},
    {"id": "near_3_4",     "min": 3000,    "max": 4000},
    {"id": "near_4_5",     "min": 4000,    "max": 5000},
    
    # Mid Range (5-25km) - 5 bands
    {"id": "mid_5_7",      "min": 5000,    "max": 7000},
    {"id": "mid_7_10",     "min": 7000,    "max": 10000},
    {"id": "mid_10_15",    "min": 10000,   "max": 15000},
    {"id": "mid_15_20",    "min": 15000,   "max": 20000},
    {"id": "mid_20_25",    "min": 20000,   "max": 25000},
    
    # Far Range (25-100km) - 4 bands
    {"id": "far_25_35",    "min": 25000,   "max": 35000},
    {"id": "far_35_50",    "min": 35000,   "max": 50000},
    {"id": "far_50_70",    "min": 50000,   "max": 70000},
    {"id": "far_70_100",   "min": 70000,   "max": 100000},
    
    # Horizon Haze (100km+) - 3 bands
    {"id": "haze_100_150", "min": 100000,  "max": 150000},
    {"id": "haze_150_220", "min": 150000,  "max": 220000},
    {"id": "haze_220_plus", "min": 220000, "max": 400000},
]


# ─────────────────────────────────────────────
#  Data classes
# ─────────────────────────────────────────────

@dataclass
class HorizonProfile:
    """Serializable horizon profile result."""
    azimuths: np.ndarray            # shape (N,), degrees 0‥360
    bands: List[Dict]               # each band: {id, angles, dists, heights}
    observer_lat: float = 0.0
    observer_lon: float = 0.0

    def get_band_points(self, band_id: str):
        """Return list of (az_deg, elevation_deg) for a given band."""
        for b in self.bands:
            if b["id"] == band_id:
                pts = []
                for i, az in enumerate(self.azimuths):
                    ang_rad = b["angles"][i]
                    if ang_rad <= -np.pi / 2:
                        # No valid data for this azimuth — use -10.0 deg (hidden)
                        pts.append((float(az), -10.0))
                    else:
                        pts.append((float(az), float(np.rad2deg(ang_rad))))
                return pts
        return []

    def save(self, path: str):
        """Save profile to .npy file."""
        data = {
            "azimuths": self.azimuths,
            "observer_lat": self.observer_lat,
            "observer_lon": self.observer_lon,
            "n_bands": len(self.bands),
        }
        for i, b in enumerate(self.bands):
            data[f"band_{i}_id"] = b["id"]
            data[f"band_{i}_angles"] = b["angles"]
            data[f"band_{i}_dists"] = b["dists"]
            data[f"band_{i}_heights"] = b["heights"]
        np.savez_compressed(path, **data)

    @staticmethod
    def load(path: str) -> "HorizonProfile":
        """Load profile from .npz file."""
        d = np.load(path, allow_pickle=True)
        azimuths = d["azimuths"]
        n_bands = int(d["n_bands"])
        bands = []
        for i in range(n_bands):
            bands.append({
                "id": str(d[f"band_{i}_id"]),
                "angles": d[f"band_{i}_angles"],
                "dists": d[f"band_{i}_dists"],
                "heights": d[f"band_{i}_heights"],
            })
        return HorizonProfile(
            azimuths=azimuths,
            bands=bands,
            observer_lat=float(d.get("observer_lat", 0.0)),
            observer_lon=float(d.get("observer_lon", 0.0)),
        )


# ─────────────────────────────────────────────
#  Tile Index
# ─────────────────────────────────────────────

class TileIndex:
    """Indexes DEM .txt/.asc tiles by bounding box in projected coordinates."""

    def __init__(self, tiles_dir: str, patterns: list = ["*.asc", "*.txt", "*.npy"], callback=None):
        self.tiles_dir = tiles_dir
        self.patterns = patterns
        self.tiles: List[Dict] = []
        self._build_index(callback)

    def _build_index(self, callback=None):
        files = []
        for pat in self.patterns:
            search_path = os.path.join(self.tiles_dir, pat)
            files.extend(glob.glob(search_path))
        
        print(f"[HorizonEngine] Indexing {len(files)} tiles from {self.tiles_dir} ({self.patterns})...")

        if callback:
            callback(0, len(files), "Indexing files...")

        for i, fpath in enumerate(files):
            try:
                if fpath.endswith(".npy"):
                     # Parse bbox from filename: Y_(ymin_ymax)X_(xmin_xmax).npy
                     # e.g. Y_(4570000.0_4580000.0)X_(418000.0_428000.0).npy
                     basename = os.path.basename(fpath)
                     bbox = self._parse_npy_filename(basename)
                     if bbox:
                         # Dummy header for compatibility if needed, or minimal dict
                         self.tiles.append({
                             "path": fpath,
                             "header": {"NPY": True}, # Marker
                             "bbox": bbox,
                         })
                else:
                    header = self._read_header(fpath)
                    bbox = self._compute_bbox(header)
                    self.tiles.append({
                        "path": fpath,
                        "header": header,
                        "bbox": bbox,
                    })
                
                # Report progress every 50 files
                if callback and i % 50 == 0:
                     callback(i, len(files), f"Indexing tile {i}/{len(files)}")

            except Exception as e:
                pass
                # print(f"[HorizonEngine] Skipping {fpath}: {e}")
        
        if callback:
             callback(len(files), len(files), f"Indexed {len(self.tiles)} tiles.")

        print(f"[HorizonEngine] Indexed {len(self.tiles)} valid tiles.")

    # ... (rest of methods)

    @staticmethod
    def _read_header(path: str) -> Dict:
        # Format: Y_(ymin_ymax)X_(xmin_xmax).npy
        try:
            # Simple parsing strategy
            # Remove .npy
            base = name.replace(".npy", "")
            # Split by X_
            parts = base.split("X_")
            if len(parts) != 2: return None
            
            y_part = parts[0].replace("Y_", "").replace("(", "").replace(")", "")
            x_part = parts[1].replace("(", "").replace(")", "")
            
            y_min, y_max = map(float, y_part.split("_"))
            x_min, x_max = map(float, x_part.split("_"))
            
            return (x_min, y_min, x_max, y_max)
        except:
            return None

    @staticmethod
    def _read_header(path: str) -> Dict:
        header = {}
        with open(path, "r") as f:
            for _ in range(6):
                line = f.readline().strip()
                if not line:
                    break
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].upper()
                    val = parts[1]
                    # Handle typical header keys
                    if key in ["NCOLS", "NROWS", "XLLCORNER", "YLLCORNER", "XLLCENTER", "YLLCENTER", "CELLSIZE", "NODATA_VALUE"]:
                        header[key] = float(val)
        
        # Ensure essential keys exist or error out
        if "NCOLS" not in header or "NROWS" not in header:
             # Try reading more lines if needed? No, standard is top 6.
             # Actually some formats have blank lines.
             pass
             
        # Normalize integer fields
        if "NCOLS" in header: header["NCOLS"] = int(header["NCOLS"])
        if "NROWS" in header: header["NROWS"] = int(header["NROWS"])
        
        return header

    @staticmethod
    def _compute_bbox(h: Dict) -> Tuple[float, float, float, float]:
        s = h.get("CELLSIZE", 5.0)
        half = s / 2.0

        if "XLLCENTER" in h:
            xmin = h["XLLCENTER"] - half
            ymin = h["YLLCENTER"] - half
        elif "XLLCORNER" in h:
            xmin = h["XLLCORNER"]
            ymin = h["YLLCORNER"]
        else:
            raise ValueError(f"Header missing XLLCENTER or XLLCORNER: {h.keys()}")

        xmax = xmin + h["NCOLS"] * s
        ymax = ymin + h["NROWS"] * s
        return (xmin, ymin, xmax, ymax)

    def find_tile(self, x: float, y: float) -> Optional[Dict]:
        for t in self.tiles:
            xmin, ymin, xmax, ymax = t["bbox"]
            if xmin <= x < xmax and ymin <= y < ymax:
                return t
        return None

    def get_overlapping_tiles(self, cx: float, cy: float, radius: float) -> List[Dict]:
        """Return all tiles within radius of (cx, cy)."""
        matches = []
        r2 = radius * radius
        # Sort by distance (closest first)
        candidates = []
        
        for t in self.tiles:
            xmin, ymin, xmax, ymax = t["bbox"]
            # Distance from point to rectangle (squared)
            dx = max(xmin - cx, 0, cx - xmax)
            dy = max(ymin - cy, 0, cy - ymax)
            dist_sq = dx*dx + dy*dy
            if dist_sq <= r2:
                candidates.append((dist_sq, t))
        
        candidates.sort(key=lambda x: x[0])
        return [c[1] for c in candidates]


# ─────────────────────────────────────────────
#  Tile Cache
# ─────────────────────────────────────────────

class TileCache:
    """LRU cache for loaded DEM grids, with .npy binary caching on disk. Thread-safe."""

    def __init__(self, capacity: int = 16):
        import threading
        self.capacity = capacity
        self.cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def load(self, tile_info: Dict) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        path = tile_info["path"]
        with self._lock:
            if path in self.cache:
                self.cache.move_to_end(path)
                return self.cache[path]

        header = tile_info["header"]
        filename = os.path.basename(path)
        base_name, ext = os.path.splitext(filename)
        
        # 1. Try packaged cache (TerraLab/data/terrain_cache)
        # Import resource_path here or ensure it's imported at top
        from TerraLab.common.utils import resource_path
        packaged_npy = resource_path(os.path.join("data", "terrain_cache", base_name + ".npy"))
        
        # 2. Try adjacent cache (original location)
        adjacent_npy = os.path.splitext(path)[0] + ".npy"
        
        candidate_npy = None
        if os.path.exists(packaged_npy):
            candidate_npy = packaged_npy
        elif os.path.exists(adjacent_npy):
            candidate_npy = adjacent_npy
            
        if candidate_npy:
            try:
                data = np.load(candidate_npy, mmap_mode="r")
                with self._lock:
                    self.cache[path] = (data, header)
                    if len(self.cache) > self.capacity:
                        self.cache.popitem(last=False)
                # print(f"[HorizonEngine] Loaded cached npy: {os.path.basename(candidate_npy)}")
                return data, header
            except Exception as e:
                print(f"[HorizonEngine] Failed to load cached npy {candidate_npy}: {e}")
                pass

        # Parse text/asc grid
        try:
            # Determine header size
            header_lines = 6
            with open(path, "r") as f:
                header_lines = 0
                for _ in range(10): 
                    line = f.readline()
                    if not line: break
                    parts = line.split()
                    if not parts: continue
                    if parts[0].upper() in ["NCOLS", "NROWS", "XLLCORNER", "YLLCORNER", "XLLCENTER", "YLLCENTER", "CELLSIZE", "NODATA_VALUE", "DX", "DY"]:
                         header_lines += 1
                    else:
                         break
            
            print(f"[HorizonEngine] Parsing grid with Pandas: {os.path.basename(path)} (skip {header_lines})...")
            # Pandas C-engine is vastly faster than np.fromfile(text)
            
            # Use raw string for sep='\s+' to fix warning
            df = pd.read_csv(
                path, 
                skiprows=header_lines, 
                sep=r'\s+', 
                header=None, 
                dtype=np.float32, 
                engine='c'
            )
            data = df.values.flatten()
            
            # Post-processing
            nodata = header.get("NODATA_VALUE", -9999)
            nrows = int(header["NROWS"])
            ncols = int(header["NCOLS"])
            
            # Data validation
            expected = nrows * ncols
            if data.size != expected:
                 # Fallback: maybe just use what we have or pad
                 if data.size > expected:
                     data = data[:expected]
                 else:
                     data = np.pad(data, (0, expected - data.size), constant_values=nodata)

            data = data.reshape((nrows, ncols)).astype(np.float32)

            # Save binary cache
            try:
                np.save(npy_path, data)
                print(f"[HorizonEngine] Saved .npy cache: {os.path.basename(npy_path)}")
            except Exception as e:
                print(f"[HorizonEngine] Could not save .npy cache: {e}")

            # Reload as mmap for RAM efficiency if possible, else use memory
            if os.path.exists(npy_path):
                 data = np.load(npy_path, mmap_mode="r")

            with self._lock:
                self.cache[path] = (data, header)
                if len(self.cache) > self.capacity:
                    self.cache.popitem(last=False)
            
            print(f"[HorizonEngine] Parsed & Cached: {os.path.basename(path)}")
            return data, header

        except Exception as e:
            print(f"[HorizonEngine] Error loading tile {path}: {e}")
            import traceback
            traceback.print_exc()
            return None, None


# ─────────────────────────────────────────────
#  DEM Sampler
# ─────────────────────────────────────────────

class DemSampler:
    """Samples elevation from DEM tiles with bilinear interpolation."""

    def __init__(self, tile_index: TileIndex, tile_cache: TileCache):
        self.index = tile_index
        self.cache = tile_cache
        self.last_tile: Optional[Dict] = None

    def sample(self, x: float, y: float) -> Optional[float]:
        # Spatial coherence optimisation: check last tile first
        tile = None
        if self.last_tile:
            xmin, ymin, xmax, ymax = self.last_tile["bbox"]
            if xmin <= x < xmax and ymin <= y < ymax:
                tile = self.last_tile
        if tile is None:
            tile = self.index.find_tile(x, y)
            self.last_tile = tile

        if tile is None:
            return None

        # Robust unpacking
        res = self.cache.load(tile)
        if res is None:
             # Should not happen if load returns (None, None)
             # print(f"[HorizonEngine] CRITICAL: load returned None for {tile['path']}")
             return None
        
        data, h = res
        
        if data is None:
            return None

        s = h["CELLSIZE"]
        if "XLLCENTER" in h:
            x0 = h["XLLCENTER"]
            y0 = h["YLLCENTER"]
        else:
            x0 = h["XLLCORNER"] + s / 2
            y0 = h["YLLCORNER"] + s / 2

        grid_x = (x - x0) / s
        grid_y_from_bottom = (y - y0) / s
        grid_row = (h["NROWS"] - 1) - grid_y_from_bottom

        c0 = int(math.floor(grid_x))
        r0 = int(math.floor(grid_row))

        c0 = max(0, min(c0, int(h["NCOLS"]) - 1))
        r0 = max(0, min(r0, int(h["NROWS"]) - 1))

        if 0 <= r0 < h["NROWS"] - 1 and 0 <= c0 < h["NCOLS"] - 1:
            dx = grid_x - c0
            dy = grid_row - r0

            v00 = float(data[r0, c0])
            v01 = float(data[r0, c0 + 1])
            v10 = float(data[r0 + 1, c0])
            v11 = float(data[r0 + 1, c0 + 1])

            top = v00 * (1 - dx) + v01 * dx
            bot = v10 * (1 - dx) + v11 * dx
            val = top * (1 - dy) + bot * dy
            return val
        else:
            return float(data[r0, c0])


# ─────────────────────────────────────────────
#  Horizon Baker
# ─────────────────────────────────────────────

class HorizonBaker:
    """
    Raycasts from observer position to compute horizon elevation angles.
    When a ray exits available DEM coverage, it stops and keeps
    whatever silhouette data was already gathered.
    """

    def __init__(self, sampler: DemSampler, eye_height: float = 1.7, R: float = R_EARTH):
        self.sampler = sampler
        self.eye_height = eye_height
        self.R = R

    def _raycast_chunk(self, args):
        """Process a chunk of azimuths for parallel execution."""
        (az_indices, sin_az_chunk, cos_az_chunk, obs_x, obs_y,
         h_eye_abs, step_m, d_max, band_defs_simple, R) = args
        
        n_bands = len(band_defs_simple)
        n_chunk = len(az_indices)
        
        # Per-chunk band results: list of (angles, dists, heights) arrays
        chunk_angles = [np.full(n_chunk, -np.inf) for _ in range(n_bands)]
        chunk_dists = [np.zeros(n_chunk) for _ in range(n_bands)]
        chunk_heights = [np.zeros(n_chunk) for _ in range(n_bands)]
        
        for local_i in range(n_chunk):
            c = sin_az_chunk[local_i]
            s = cos_az_chunk[local_i]
            d = step_m

            while d < d_max:
                x = obs_x + d * c
                y = obs_y + d * s

                h_terr = self.sampler.sample(x, y)

                if h_terr is not None:
                    drop = (d * d) / (2.0 * R)
                    h_visual = h_terr - drop - h_eye_abs
                    ang = math.atan2(h_visual, d)

                    for b_idx in range(n_bands):
                        b_min, b_max = band_defs_simple[b_idx]
                        if b_min <= d < b_max:
                            if ang > chunk_angles[b_idx][local_i]:
                                chunk_angles[b_idx][local_i] = ang
                                chunk_dists[b_idx][local_i] = d
                                chunk_heights[b_idx][local_i] = h_terr
                            break

                # Dynamic stepping: finer near, coarser far
                if d < 3000:
                    d += step_m
                elif d < 15000:
                    d += step_m * 2
                else:
                    d += step_m * 4

        return az_indices, chunk_angles, chunk_dists, chunk_heights

    def bake(
        self,
        obs_x: float,
        obs_y: float,
        obs_h_ground: Optional[float] = None,
        step_m: float = 50,
        d_max: float = 100_000,
        delta_az_deg: float = 0.5,
        band_defs: Optional[List[Dict]] = None,
        progress_callback=None,
    ) -> Tuple[np.ndarray, List[Dict]]:
        """
        Compute multi-band horizon profile (sequential — CPU-bound under GIL).

        Args:
            progress_callback: Optional callable(percent: int, msg: str).

        Returns (azimuths, bands) where bands is a list of dicts
        each containing 'id', 'angles', 'dists', 'heights' arrays.
        """
        import time

        if obs_h_ground is None:
            val = self.sampler.sample(obs_x, obs_y)
            if val is None:
                print("[HorizonEngine] Observer outside DEM coverage. Using 0.")
                obs_h_ground = 0
            else:
                obs_h_ground = val

        h_eye_abs = obs_h_ground + self.eye_height

        azimuths = np.arange(0, 360, delta_az_deg)
        n_az = len(azimuths)

        if band_defs is None:
            band_defs = DEFAULT_BANDS

        bands = []
        for bd in band_defs:
            bands.append({
                "id": bd["id"],
                "min": bd["min"],
                "max": bd["max"],
                "angles": np.full(n_az, -np.inf),
                "dists": np.zeros(n_az),
                "heights": np.zeros(n_az),
            })

        az_rads = np.deg2rad(azimuths)
        sin_az = np.sin(az_rads)
        cos_az = np.cos(az_rads)

        print(f"[HorizonEngine] Baking {n_az} azimuths, max_dist={d_max / 1000:.0f}km...")
        t0 = time.time()
        last_report = 0

        for i in range(n_az):
            c = sin_az[i]
            s = cos_az[i]
            d = step_m

            while d < d_max:
                x = obs_x + d * c
                y = obs_y + d * s

                h_terr = self.sampler.sample(x, y)

                if h_terr is not None:
                    drop = (d * d) / (2.0 * self.R)
                    h_visual = h_terr - drop - h_eye_abs
                    ang = math.atan2(h_visual, d)

                    for b in bands:
                        if b["min"] <= d < b["max"]:
                            if ang > b["angles"][i]:
                                b["angles"][i] = ang
                                b["dists"][i] = d
                                b["heights"][i] = h_terr
                            break

                # Dynamic stepping: finer near, coarser far
                if d < 3000:
                    d += step_m
                elif d < 15000:
                    d += step_m * 2
                else:
                    d += step_m * 4

            # Progress reporting (every 5%)
            pct = int((i + 1) / n_az * 100)
            if pct >= last_report + 5:
                last_report = pct
                if progress_callback:
                    progress_callback(pct, f"⏳ Calculando horizonte: {pct}%")
                if pct % 25 == 0:
                    elapsed = time.time() - t0
                    print(f"[HorizonEngine]   {pct}% ({i+1}/{n_az} azimuths, {elapsed:.1f}s)")

        elapsed = time.time() - t0
        print(f"[HorizonEngine] Bake complete in {elapsed:.2f}s.")
        return azimuths, bands


# ─────────────────────────────────────────────
#  Convenience functions
# ─────────────────────────────────────────────

def bake_and_save(
    lat: float,
    lon: float,
    tiles_dir: str,
    output_path: str,
    radius: float = 100_000,
    step_m: float = 50,
    resolution_deg: float = 0.5,
    eye_height: float = 1.7,
    band_defs: Optional[List[Dict]] = None,
):
    """
    Full pipeline: transform coords, index tiles, bake horizon, save .npz.
    """
    from pyproj import Transformer

    # Transform observer to UTM 31N
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:25831", always_xy=True)
    x_utm, y_utm = transformer.transform(lon, lat)
    print(f"[HorizonEngine] Observer UTM: {x_utm:.2f}, {y_utm:.2f}")

    # Build system
    idx = TileIndex(tiles_dir)
    cache = TileCache(capacity=100)
    sampler = DemSampler(idx, cache)
    baker = HorizonBaker(sampler, eye_height=eye_height)

    # Sample observer altitude
    ground_h = sampler.sample(x_utm, y_utm)
    if ground_h is None:
        print("[HorizonEngine] Observer outside DEM. Using fallback 200m (Lleida plains).")
        ground_h = 200.0
    else:
        print(f"[HorizonEngine] Observer altitude from DEM: {ground_h:.2f}m")

    # Bake
    azimuths, bands = baker.bake(
        x_utm, y_utm,
        obs_h_ground=ground_h,
        step_m=step_m,
        d_max=radius,
        delta_az_deg=resolution_deg,
        band_defs=band_defs,
    )

    # Build & save profile
    profile = HorizonProfile(
        azimuths=azimuths,
        bands=bands,
        observer_lat=lat,
        observer_lon=lon,
    )
    profile.save(output_path)
    print(f"[HorizonEngine] Profile saved to {output_path}")
    return profile


def load_profile(path: str) -> Optional[HorizonProfile]:
    """Load a pre-baked horizon profile. Returns None if file is missing."""
    if not os.path.exists(path):
        return None
    try:
        return HorizonProfile.load(path)
    except Exception as e:
        print(f"[HorizonEngine] Error loading profile {path}: {e}")
        return None
