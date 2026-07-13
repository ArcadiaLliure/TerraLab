"""
horizon_engine.py — Real topographic horizon profile engine.

Loads ICGC 5×5 DEM tiles (ESRI ASCII Grid .txt), caches them as .npy,
and computes multi-band horizon silhouettes via raycasting with Earth
curvature correction.

"""

import glob
import math
import os
from pathlib import Path
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from TerraLab.terrain.providers import (
    CRS_GEOGRAPHIC,
    CRS_TERRAIN_INTERNAL,
    PYPROJ_TRANSFORMER_LOCK,
)

# --- Constants ---
R_EARTH = 6_371_000.0


def compute_polar_mesh_normals(
    elevations: np.ndarray,
    valid: np.ndarray,
    distances: np.ndarray,
    azimuths: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    elevations = np.asarray(elevations, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool)
    distances = np.asarray(distances, dtype=np.float32)
    azimuths = np.asarray(azimuths, dtype=np.float32)
    if (
        elevations.ndim != 2
        or elevations.shape != valid.shape
        or elevations.shape != (distances.size, azimuths.size)
        or distances.size < 2
        or azimuths.size < 2
    ):
        shape = elevations.shape
        return (
            np.zeros(shape, dtype=np.float32),
            np.zeros(shape, dtype=np.float32),
            np.ones(shape, dtype=np.float32),
        )

    center = np.where(valid, elevations, 0.0).astype(np.float32)
    previous_rows = np.maximum(np.arange(distances.size) - 1, 0)
    next_rows = np.minimum(np.arange(distances.size) + 1, distances.size - 1)
    previous_height = elevations[previous_rows, :]
    next_height = elevations[next_rows, :]
    previous_valid = valid[previous_rows, :]
    next_valid = valid[next_rows, :]
    previous_height = np.where(previous_valid, previous_height, center)
    next_height = np.where(next_valid, next_height, center)
    radial_span = np.maximum(
        distances[next_rows] - distances[previous_rows], 1e-3
    )[:, None]
    dz_radial = (next_height - previous_height) / radial_span

    left_height = np.roll(elevations, 1, axis=1)
    right_height = np.roll(elevations, -1, axis=1)
    left_valid = np.roll(valid, 1, axis=1)
    right_valid = np.roll(valid, -1, axis=1)
    left_height = np.where(left_valid, left_height, center)
    right_height = np.where(right_valid, right_height, center)
    az_diffs = np.diff(azimuths)
    az_diffs = az_diffs[np.isfinite(az_diffs) & (az_diffs > 0.0)]
    az_step_rad = math.radians(
        float(np.median(az_diffs)) if az_diffs.size else 360.0
    )
    tangential_span = np.maximum(
        2.0 * distances[:, None] * az_step_rad, 1e-3
    )
    dz_tangential = (right_height - left_height) / tangential_span

    az_radians = np.deg2rad(azimuths)[None, :]
    sin_azimuth = np.sin(az_radians)
    cos_azimuth = np.cos(az_radians)
    gradient_x = dz_radial * sin_azimuth + dz_tangential * cos_azimuth
    gradient_y = dz_radial * cos_azimuth - dz_tangential * sin_azimuth

    normal_x = -gradient_x
    normal_y = -gradient_y
    normal_z = np.ones_like(normal_x, dtype=np.float32)
    norm = np.sqrt(
        normal_x * normal_x + normal_y * normal_y + normal_z * normal_z
    )
    safe_norm = np.maximum(norm, 1e-6)
    normal_x = np.where(valid, normal_x / safe_norm, 0.0)
    normal_y = np.where(valid, normal_y / safe_norm, 0.0)
    normal_z = np.where(valid, normal_z / safe_norm, 1.0)
    return (
        normal_x.astype(np.float32),
        normal_y.astype(np.float32),
        normal_z.astype(np.float32),
    )


def generate_bands(n: int = 20, max_dist_m: float = 150_000) -> list:
    """
    Genera N bandes d'horitzó amb distribució logarítmica per zones (piecewise bilog).

    Zones:
      · Zona propera  (0 → near_km):  2/3 de les N bandes.
        Recull detall de primer pla (colls, turons, cingleres).
      · Zona llunyana (near_km → max): 1/3 de les N bandes.
        Representa la boira atmosfèrica amb menys bandes (l'ull no n'aprecia el detall).

    La distància mínima de la primera banda és step_min_m (≥ resolució del baker)
    per evitar artefactes "paret" quan l'observador és a la vora d'un penya-segat.

    Args:
        n: Nombre total de bandes (10=Baix, 20=Normal, 40=Alt, 60=Ultra, 80=Extrem)
        max_dist_m: Distància màxima de càlcul en metres
    Returns:
        Llista de dicts amb 'id', 'min', 'max' en metres
    """
    import math as _math

    # ── Paramètres de partició ────────────────────────────────────────────────
    # Distància de tall entre zona propera i zona llunyana
    near_km = 5_000.0  # metres
    # Fracció de bandes dedicades a la zona propera (2/3)
    near_frac = 2.0 / 3.0
    # Distància mínima del primer extrem de banda
    # El baker ara comença a 0.5m, però les bandes < 1m no aporten informació visual extra
    step_min_m = 1.0

    n_near = max(2, round(n * near_frac))
    n_far = max(1, n - n_near)

    # ── Zona propera: log entre step_min_m i near_km ─────────────────────────
    log_n_min = _math.log(step_min_m)
    log_n_max = _math.log(near_km)

    near_inner = []
    for i in range(1, n_near + 1):
        t = i / n_near
        v = _math.exp(log_n_min + (log_n_max - log_n_min) * t)
        near_inner.append(min(v, near_km))

    # ── Zona llunyana: log entre near_km i max_dist_m ────────────────────────
    log_f_min = _math.log(near_km)
    log_f_max = _math.log(max_dist_m)

    far_inner = []
    for i in range(1, n_far + 1):
        t = i / n_far
        v = _math.exp(log_f_min + (log_f_max - log_f_min) * t)
        far_inner.append(min(v, max_dist_m))

    # ── Punts de tall combinats ───────────────────────────────────────────────
    # Sempre comencem des de 0 i garantim near_km com a punt de transició
    breakpoints = [0.0] + near_inner + far_inner

    # ── Etiquetes de zona i format de noms ───────────────────────────────────
    _zone_labels = [
        (0, 750, "gnd"),
        (750, 5_000, "near"),
        (5_000, 25_000, "mid"),
        (25_000, 100_000, "far"),
        (100_000, 999_999, "haze"),
    ]

    def _zone_for(m):
        for lo, hi, label in _zone_labels:
            if m < hi:
                return label
        return "haze"

    def _fmt(m):
        """Etiqueta de distància llegible: 250→'250', 1500→'1.5k', 35000→'35k'."""
        if m < 1000:
            return str(int(m))
        elif m < 10_000:
            v = m / 1_000
            return (
                f"{v:.1f}k".rstrip("0").rstrip(".") + "k"
                if v != int(v)
                else f"{int(v)}k"
            )
        else:
            return f"{int(round(m / 1000))}k"

    # ── Construcció de la llista de bandes ────────────────────────────────────
    bands = []
    total_bps = len(breakpoints)
    for i in range(total_bps - 1):
        lo = breakpoints[i]
        hi = breakpoints[i + 1]
        if hi <= lo:
            continue  # Salta bandes buides (pot passar per arrodoniments)
        zone = _zone_for(lo)
        band_id = f"{zone}_{_fmt(lo)}_{_fmt(hi)}"
        bands.append({"id": band_id, "min": lo, "max": hi})

    return bands


# Àlies retrocompatible — qualitat per defecte = 20 bandes
DEFAULT_BANDS = generate_bands(20)


# ─────────────────────────────────────────────
#  Data classes
# ─────────────────────────────────────────────


@dataclass
class HorizonProfile:
    """Serializable horizon profile result."""

    azimuths: np.ndarray  # shape (N,), degrees 0‥360
    bands: List[Dict]  # each band: {id, angles, dists, heights}
    observer_lat: float = 0.0
    observer_lon: float = 0.0
    light_domes: Optional[np.ndarray] = None
    light_peak_distances: Optional[np.ndarray] = (
        None  # Distances of max light source per azimuth
    )
    resolved_mask: Optional[np.ndarray] = None
    terrain_mesh: Optional[Dict] = None

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

    def get_band_surface_points(self, band_id: str):
        """Return far-edge surface points for a band when available."""
        for b in self.bands:
            if b["id"] == band_id:
                angles = b.get("surface_angles")
                if angles is None:
                    return []
                pts = []
                for i, az in enumerate(self.azimuths):
                    ang_rad = angles[i]
                    if ang_rad <= -np.pi / 2:
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
            "light_domes": self.light_domes,
            "light_peak_distances": self.light_peak_distances,
        }
        if self.resolved_mask is not None:
            data["resolved_mask"] = np.asarray(
                self.resolved_mask, dtype=np.uint8
            )
        if self.terrain_mesh:
            mesh = self.terrain_mesh
            data["terrain_mesh_present"] = np.asarray(1, dtype=np.uint8)
            data["terrain_mesh_version"] = np.asarray(
                int(mesh.get("version", 1)), dtype=np.uint8
            )
            for key in (
                "azimuths",
                "distances",
                "altitudes",
                "elevations",
                "normal_x",
                "normal_y",
                "normal_z",
                "valid",
                "visible",
            ):
                if key in mesh:
                    data[f"terrain_mesh_{key}"] = np.asarray(mesh[key])
        for i, b in enumerate(self.bands):
            data[f"band_{i}_id"] = b["id"]
            data[f"band_{i}_angles"] = b["angles"]
            data[f"band_{i}_dists"] = b["dists"]
            data[f"band_{i}_heights"] = b["heights"]
            if "surface_angles" in b:
                data[f"band_{i}_surface_angles"] = b["surface_angles"]
                data[f"band_{i}_surface_dists"] = b.get(
                    "surface_dists", np.zeros_like(b["dists"])
                )
                data[f"band_{i}_surface_heights"] = b.get(
                    "surface_heights", np.zeros_like(b["heights"])
                )
        np.savez_compressed(path, **data)

    @staticmethod
    def load(path: str) -> "HorizonProfile":
        """Load profile from .npz file."""
        with np.load(path, allow_pickle=True) as d:
            azimuths = np.asarray(d["azimuths"]).copy()
            n_bands = int(np.asarray(d["n_bands"]).item())
            bands = []
            for i in range(n_bands):
                band = {
                    "id": str(d[f"band_{i}_id"]),
                    "angles": np.asarray(d[f"band_{i}_angles"]).copy(),
                    "dists": np.asarray(d[f"band_{i}_dists"]).copy(),
                    "heights": np.asarray(d[f"band_{i}_heights"]).copy(),
                }
                if f"band_{i}_surface_angles" in d:
                    band["surface_angles"] = np.asarray(
                        d[f"band_{i}_surface_angles"]
                    ).copy()
                    band["surface_dists"] = np.asarray(
                        d[f"band_{i}_surface_dists"]
                    ).copy()
                    band["surface_heights"] = np.asarray(
                        d[f"band_{i}_surface_heights"]
                    ).copy()
                bands.append(band)

            if "light_domes" in d:
                light_domes = np.asarray(d["light_domes"]).copy()
            else:
                light_domes = np.zeros(len(azimuths))
            if "light_peak_distances" in d:
                light_peak_distances = np.asarray(
                    d["light_peak_distances"]
                ).copy()
            else:
                light_peak_distances = np.zeros(len(azimuths))

            resolved_mask = None
            if "resolved_mask" in d:
                resolved_mask = np.asarray(
                    d["resolved_mask"], dtype=np.uint8
                ).astype(bool)

            terrain_mesh = None
            if (
                "terrain_mesh_present" in d
                and "terrain_mesh_azimuths" in d
                and "terrain_mesh_distances" in d
                and "terrain_mesh_altitudes" in d
            ):
                mesh_version = (
                    int(np.asarray(d["terrain_mesh_version"]).item())
                    if "terrain_mesh_version" in d
                    else 1
                )
                terrain_mesh = {
                    "version": mesh_version,
                    "azimuths": np.asarray(d["terrain_mesh_azimuths"]).copy(),
                    "distances": np.asarray(
                        d["terrain_mesh_distances"]
                    ).copy(),
                    "altitudes": np.asarray(
                        d["terrain_mesh_altitudes"]
                    ).copy(),
                    "elevations": np.asarray(
                        d["terrain_mesh_elevations"]
                        if "terrain_mesh_elevations" in d
                        else np.array([])
                    ).copy(),
                    "normal_x": np.asarray(
                        d["terrain_mesh_normal_x"]
                        if "terrain_mesh_normal_x" in d
                        else np.array([])
                    ).copy(),
                    "normal_y": np.asarray(
                        d["terrain_mesh_normal_y"]
                        if "terrain_mesh_normal_y" in d
                        else np.array([])
                    ).copy(),
                    "normal_z": np.asarray(
                        d["terrain_mesh_normal_z"]
                        if "terrain_mesh_normal_z" in d
                        else np.array([])
                    ).copy(),
                    "valid": np.asarray(
                        d["terrain_mesh_valid"]
                        if "terrain_mesh_valid" in d
                        else np.array([]),
                        dtype=np.uint8,
                    )
                    .astype(bool)
                    .copy(),
                    "visible": np.asarray(
                        d["terrain_mesh_visible"]
                        if "terrain_mesh_visible" in d
                        else np.array([]),
                        dtype=np.uint8,
                    )
                    .astype(bool)
                    .copy(),
                }

            observer_lat = (
                float(np.asarray(d["observer_lat"]).item())
                if "observer_lat" in d
                else 0.0
            )
            observer_lon = (
                float(np.asarray(d["observer_lon"]).item())
                if "observer_lon" in d
                else 0.0
            )

        return HorizonProfile(
            azimuths=azimuths,
            bands=bands,
            observer_lat=observer_lat,
            observer_lon=observer_lon,
            light_domes=light_domes,
            light_peak_distances=light_peak_distances,
            resolved_mask=resolved_mask,
            terrain_mesh=terrain_mesh,
        )


# ─────────────────────────────────────────────
#  Tile Index
# ─────────────────────────────────────────────


class TileIndex:
    """Indexes DEM .txt/.asc tiles by bounding box in projected coordinates."""

    def __init__(
        self,
        tiles_dir: str,
        patterns: list = ["*.asc", "*.txt", "*.npy"],
        callback=None,
    ):
        self.tiles_dir = tiles_dir
        self.patterns = patterns
        self.tiles: List[Dict] = []
        self.global_bbox = [np.inf, np.inf, -np.inf, -np.inf]
        self._build_index(callback)

    def _build_index(self, callback=None):
        files = []
        for pat in self.patterns:
            search_path = os.path.join(self.tiles_dir, pat)
            files.extend(glob.glob(search_path))

        print(
            f"[HorizonEngine] Indexing {len(files)} tiles from {self.tiles_dir} ({self.patterns})..."
        )

        if callback:
            callback(0, len(files), "Indexing files...")

        for i, fpath in enumerate(files):
            try:
                if fpath.endswith(".npy"):
                    # Parse bbox from filename: Y_(ymin_ymax)X_(xmin_xmax).npy
                    basename = os.path.basename(fpath)
                    bbox = self._parse_npy_filename(basename)
                    if bbox:
                        # For NPY tiles, we still need a cellulsize to sample.
                        # Assuming default 5.0m for ICGC tiles if not encoded in filename.
                        # Real NPY tiles should probably have their own header files.
                        self.tiles.append(
                            {
                                "path": fpath,
                                "header": {
                                    "NPY": True,
                                    "CELLSIZE": 5.0,
                                    "NCOLS": 1,
                                    "NROWS": 1,
                                },  # Minimal
                                "bbox": bbox,
                            }
                        )
                else:
                    header = self._read_header(fpath)
                    bbox = self._compute_bbox(header)
                    self.tiles.append(
                        {
                            "path": fpath,
                            "header": header,
                            "bbox": bbox,
                        }
                    )

                # Update global bbox
                self.global_bbox[0] = min(self.global_bbox[0], bbox[0])
                self.global_bbox[1] = min(self.global_bbox[1], bbox[1])
                self.global_bbox[2] = max(self.global_bbox[2], bbox[2])
                self.global_bbox[3] = max(self.global_bbox[3], bbox[3])

                # Report progress every 50 files
                if callback and i % 50 == 0:
                    callback(i, len(files), f"Indexing tile {i}/{len(files)}")

            except Exception as e:
                # print(f"[HorizonEngine] Skipping {fpath}: {e}")
                pass

        if callback:
            callback(
                len(files), len(files), f"Indexed {len(self.tiles)} tiles."
            )

        print(f"[HorizonEngine] Indexed {len(self.tiles)} valid tiles.")

    @staticmethod
    def _parse_npy_filename(
        name: str,
    ) -> Optional[Tuple[float, float, float, float]]:
        # Format: Y_(ymin_ymax)X_(xmin_xmax).npy
        try:
            base = name.replace(".npy", "")
            parts = base.split("X_")
            if len(parts) != 2:
                return None

            y_part = (
                parts[0].replace("Y_", "").replace("(", "").replace(")", "")
            )
            x_part = parts[1].replace("(", "").replace(")", "")

            y_min, y_max = map(float, y_part.split("_"))
            x_min, x_max = map(float, x_part.split("_"))

            return (x_min, y_min, x_max, y_max)
        except:
            return None

    @staticmethod
    def _read_header(path: str) -> Dict:
        header = {}
        try:
            with open(path, "r") as f:
                for _ in range(6):
                    line = f.readline().strip()
                    if not line:
                        break
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].upper()
                        val = parts[1]
                        if key in [
                            "NCOLS",
                            "NROWS",
                            "XLLCORNER",
                            "YLLCORNER",
                            "XLLCENTER",
                            "YLLCENTER",
                            "CELLSIZE",
                            "NODATA_VALUE",
                        ]:
                            header[key] = float(val)

            if "NCOLS" in header:
                header["NCOLS"] = int(header["NCOLS"])
            if "NROWS" in header:
                header["NROWS"] = int(header["NROWS"])
        except:
            pass
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
            raise ValueError(
                f"Header missing XLLCENTER or XLLCORNER: {h.keys()}"
            )

        xmax = xmin + h["NCOLS"] * s
        ymax = ymin + h["NROWS"] * s
        return (xmin, ymin, xmax, ymax)

    def find_tile(self, x: float, y: float) -> Optional[Dict]:
        """Returns the first tile containing (x, y)."""
        # Fast bounds check
        if (
            x < self.global_bbox[0]
            or y < self.global_bbox[1]
            or x > self.global_bbox[2]
            or y > self.global_bbox[3]
        ):
            return None

        # O(N) linear scan (usually fine for ~1000 tiles)
        for t in self.tiles:
            xmin, ymin, xmax, ymax = t["bbox"]
            if xmin <= x < xmax and ymin <= y < ymax:
                return t
        return None

    def get_overlapping_tiles(
        self, cx: float, cy: float, radius: float
    ) -> List[Dict]:
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
            dist_sq = dx * dx + dy * dy
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
        self._tile_io_lock = threading.Lock()
        self._packaged_cache_root = (
            Path(__file__).resolve().parents[1] / "data" / "terrain_cache"
        )

    def load(
        self, tile_info: Dict
    ) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        """Executa el metode load de la classe TileCache.

        Par?metres:
        - tile_info (Dict): Valor del parametre 'tile_info'.

        Retorna:
        - Tuple[Optional[np.ndarray], Optional[Dict]]: Valor retornat pel metode.
        """
        path = tile_info["path"]
        with self._lock:
            if path in self.cache:
                self.cache.move_to_end(path)
                return self.cache[path]

        header = tile_info["header"]
        filename = os.path.basename(path)
        base_name, ext = os.path.splitext(filename)

        # 1. Try packaged cache (TerraLab/data/terrain_cache)
        # Use deterministic path computation without runtime mkdir side-effects.
        packaged_npy = str(
            self._packaged_cache_root / f"{base_name}.npy"
        )

        # 2. Try adjacent cache (original location)
        adjacent_npy = os.path.splitext(path)[0] + ".npy"

        candidate_npy = None
        if os.path.exists(packaged_npy):
            candidate_npy = packaged_npy
        elif os.path.exists(adjacent_npy):
            candidate_npy = adjacent_npy

        if candidate_npy:
            try:
                with self._tile_io_lock:
                    # Stability-first on Windows/Python 3.13:
                    # avoid memmap-backed arrays loaded from worker threads.
                    data = np.load(
                        candidate_npy,
                        mmap_mode=None,
                        allow_pickle=False,
                    )
                    # Ensure independent in-memory buffer (no file-backed view).
                    data = np.asarray(data, dtype=np.float32)
                with self._lock:
                    self.cache[path] = (data, header)
                    if len(self.cache) > self.capacity:
                        self.cache.popitem(last=False)
                # print(f"[HorizonEngine] Loaded cached npy: {os.path.basename(candidate_npy)}")
                return data, header
            except Exception as e:
                print(
                    f"[HorizonEngine] Failed to load cached npy {candidate_npy}: {e}"
                )
                pass

        # Parse text/asc grid
        try:
            # Determine header size
            header_lines = 0
            with open(path, "r") as f:
                for _ in range(10):
                    line = f.readline()
                    if not line:
                        break
                    parts = line.split()
                    if not parts:
                        continue
                    if parts[0].upper() in [
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
                    ]:
                        header_lines += 1
                    else:
                        break

            # Use adjacent folder or packaged folder for saving
            output_npy = adjacent_npy

            print(
                f"[HorizonEngine] Parsing with Pandas: {os.path.basename(path)}..."
            )
            try:
                with self._tile_io_lock:
                    import pandas as pd

                    df = pd.read_csv(
                        path,
                        skiprows=header_lines,
                        sep=r"\s+",
                        header=None,
                        dtype=np.float32,
                        engine="c",
                    )
                    data_raw = df.values.flatten()
            except Exception as e:
                print(f"[HorizonEngine] Error parsing {path} with Pandas: {e}")
                return None, None

            nodata = header.get("NODATA_VALUE", -9999)
            nrows = int(header["NROWS"])
            ncols = int(header["NCOLS"])

            expected = nrows * ncols
            if data_raw.size != expected:
                if data_raw.size > expected:
                    data_raw = data_raw[:expected]
                else:
                    data_raw = np.pad(
                        data_raw,
                        (0, expected - data_raw.size),
                        constant_values=nodata,
                    )

            data = data_raw.reshape((nrows, ncols)).astype(np.float32)

            # Save binary cache to original location if possible
            try:
                with self._tile_io_lock:
                    temp_output_npy = f"{output_npy}.tmp.npy"
                    np.save(temp_output_npy, data)
                    os.replace(temp_output_npy, output_npy)
                # print(f"[HorizonEngine] Saved cache: {os.path.basename(output_npy)}")
            except:
                pass

            with self._lock:
                self.cache[path] = (data, header)
                if len(self.cache) > self.capacity:
                    self.cache.popitem(last=False)

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
        self._transformer_inv = None  # Lazy loaded if needed

    def get_elevation(self, x: float, y: float) -> Optional[float]:
        """Alias for sample() to maintain compatibility with HorizonBaker."""
        return self.sample(x, y)

    def transform_coordinates_inverse(
        self, x: float, y: float
    ) -> Tuple[float, float]:
        """
        Convert terrain internal coordinates back to geographic lat/lon.

        Input CRS:
            - `x`, `y` in terrain internal CRS (`EPSG:25831`).
        Output CRS:
            - `(lat, lon)` in `EPSG:4326`.
        """
        from pyproj import Transformer

        if self._transformer_inv is None:
            with PYPROJ_TRANSFORMER_LOCK:
                self._transformer_inv = Transformer.from_crs(
                    CRS_TERRAIN_INTERNAL, CRS_GEOGRAPHIC, always_xy=True
                )
        lon, lat = self._transformer_inv.transform(x, y)
        return lat, lon

    def sample(self, x: float, y: float) -> Optional[float]:
        # Spatial coherence optimisation: check last tile first
        """Executa el metode sample de la classe DemSampler.

        Par?metres:
        - x (float): Valor del parametre 'x'.
        - y (float): Valor del parametre 'y'.

        Retorna:
        - Optional[float]: Valor retornat pel metode.
        """
        tile = None
        # Optimization: Track if we are in a "void" area to avoid searching the index
        if self.last_tile == "NONE":
            # We need to know when we exit the void.
            # For now, let's just re-find if it's not the last state.
            pass

        if self.last_tile and self.last_tile != "NONE":
            xmin, ymin, xmax, ymax = self.last_tile["bbox"]
            if xmin <= x < xmax and ymin <= y < ymax:
                tile = self.last_tile

        if tile is None:
            tile = self.index.find_tile(x, y)
            self.last_tile = tile if tile else "NONE"

        if tile is None or tile == "NONE":
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

        # Handle NPY tiles where LL mapping might be different or missing
        if h.get("NPY"):
            # For NPY tiles, we assume bbox is already correct from filename
            # and it's a regular grid.
            xmin, ymin, xmax, ymax = tile["bbox"]
            nrows, ncols = data.shape
            sx = (xmax - xmin) / max(1, ncols)
            sy = (ymax - ymin) / max(1, nrows)
            grid_x = (x - xmin) / sx
            # Y in arrays is usually top-to-bottom
            grid_y_from_top = (ymax - y) / sy
            grid_row = grid_y_from_top
        else:
            # Robust header handling: default to 5.0m if missing (standard for ICGC tiles)
            s = h.get("CELLSIZE", 5.0)
            if "XLLCENTER" in h:
                x0 = h["XLLCENTER"]
                y0 = h["YLLCENTER"]
            else:
                x0 = h.get("XLLCORNER", 0.0) + s / 2
                y0 = h.get("YLLCORNER", 0.0) + s / 2

            grid_x = (x - x0) / s
            grid_y_from_bottom = (y - y0) / s
            grid_row = (h.get("NROWS", data.shape[0]) - 1) - grid_y_from_bottom

        c0 = int(math.floor(grid_x))
        r0 = int(math.floor(grid_row))

        nrows, ncols = data.shape
        c0 = max(0, min(c0, ncols - 1))
        r0 = max(0, min(r0, nrows - 1))

        if 0 <= r0 < nrows - 1 and 0 <= c0 < ncols - 1:
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

    def __init__(self, provider, eye_height: float = 1.7, R: float = R_EARTH):
        self.provider = provider
        self.eye_height = eye_height
        self.R = R
        self._ray_miss_exit_threshold = 8
        self._ray_iteration_guard = 1_000_000

    @staticmethod
    def _next_step_distance(
        d: float, step_m: float, near_factor: float = 1.5
    ) -> float:
        """Return the next ray-march distance using the adaptive stepping policy."""
        if d < step_m:
            return min(d * near_factor, step_m)
        if d < 3_000:
            return d + step_m
        if d < 15_000:
            return d + step_m * 2.0
        if d < 50_000:
            return d + step_m * 4.0
        return d + step_m * 8.0

    @staticmethod
    def _build_band_buffers(n_az: int, band_defs: List[Dict]) -> List[Dict]:
        bands = []
        for bd in band_defs:
            bands.append(
                {
                    "id": bd["id"],
                    "min": bd["min"],
                    "max": bd["max"],
                    "angles": np.full(n_az, -np.inf),
                    "dists": np.zeros(n_az),
                    "heights": np.zeros(n_az),
                    "surface_angles": np.full(n_az, -np.inf),
                    "surface_dists": np.zeros(n_az),
                    "surface_heights": np.zeros(n_az),
                }
            )
        return bands

    def _sample_single_azimuth(
        self,
        az_index: int,
        obs_x: float,
        obs_y: float,
        h_eye_abs: float,
        step_m: float,
        d_max: float,
        bands: List[Dict],
        sin_az: np.ndarray,
        cos_az: np.ndarray,
        light_domes: np.ndarray,
        light_peak_distances: np.ndarray,
        max_rad_per_az: np.ndarray,
        light_sampler=None,
    ) -> None:
        c = sin_az[az_index]
        s = cos_az[az_index]

        NEAR_START = 0.5
        d = NEAR_START
        max_ang_so_far = -np.pi / 2.0
        last_light_d = 0.0
        miss_streak = 0
        band_idx = 0
        iterations = 0

        while d < d_max:
            iterations += 1
            if iterations > self._ray_iteration_guard:
                print(
                    f"[HorizonEngine] Warning: ray iteration guard reached at az={az_index}, d={d:.1f}"
                )
                break
            x = obs_x + d * c
            y = obs_y + d * s

            h_terr = self.provider.get_elevation(x, y)
            if h_terr is None:
                miss_streak += 1
                if miss_streak >= self._ray_miss_exit_threshold:
                    break
                d_next = self._next_step_distance(d, step_m)
                if not np.isfinite(d_next) or d_next <= d:
                    print(
                        f"[HorizonEngine] Warning: invalid step progression at az={az_index}, d={d:.3f}, next={d_next}"
                    )
                    break
                d = d_next
                continue
            miss_streak = 0

            drop = (d * d) / (2.0 * self.R)
            h_visual = h_terr - drop - h_eye_abs
            ang = math.atan2(h_visual, d)

            if ang > max_ang_so_far:
                max_ang_so_far = ang

            if light_sampler is not None and (d - last_light_d) >= 2000.0:
                last_light_d = d
                rad = self._sample_light_radiance(light_sampler, x, y)

                if rad and rad > 0.1:
                    dist_mult = 1.0 / max(1.0, (d / 1000.0))
                    if ang > (max_ang_so_far - 0.17):
                        light_domes[az_index] += float(rad * dist_mult * 20.0)
                        if rad > max_rad_per_az[az_index]:
                            max_rad_per_az[az_index] = float(rad)
                            light_peak_distances[az_index] = float(d)

            while band_idx + 1 < len(bands) and d >= bands[band_idx]["max"]:
                band_idx += 1
            if (
                0 <= band_idx < len(bands)
                and bands[band_idx]["min"] <= d < bands[band_idx]["max"]
            ):
                b = bands[band_idx]
                b["surface_angles"][az_index] = ang
                b["surface_dists"][az_index] = d
                b["surface_heights"][az_index] = h_terr
                if ang > b["angles"][az_index]:
                    b["angles"][az_index] = ang
                    b["dists"][az_index] = d
                    b["heights"][az_index] = h_terr

            d_next = self._next_step_distance(d, step_m)
            if not np.isfinite(d_next) or d_next <= d:
                print(
                    f"[HorizonEngine] Warning: invalid step progression at az={az_index}, d={d:.3f}, next={d_next}"
                )
                break
            d = d_next

    def _sample_light_radiance(
        self, light_sampler, x_internal: float, y_internal: float
    ) -> float:
        """
        Sample light-pollution radiance at a terrain internal point.

        Input CRS:
            - `x_internal`, `y_internal` in terrain internal CRS (`EPSG:25831`).
        Output:
            - Radiance float, `0.0` on controlled failure/out-of-bounds.
        """
        if light_sampler is None:
            return 0.0
        try:
            return float(
                light_sampler.get_radiance_terrain_xy(
                    x_internal,
                    y_internal,
                    input_crs=CRS_TERRAIN_INTERNAL,
                )
            )
        except Exception:
            return 0.0

    def _provider_nominal_resolution_m(self) -> float:
        resolution = None
        getter = getattr(self.provider, "get_nominal_resolution_m", None)
        if callable(getter):
            try:
                resolution = float(getter())
            except Exception:
                resolution = None
        if resolution is None or not np.isfinite(resolution) or resolution <= 0:
            resolution = 30.0
        return float(resolution)

    def _normal_sample_step_m(self) -> float:
        resolution = self._provider_nominal_resolution_m()
        return float(max(10.0, min(120.0, resolution * 2.0)))

    def _sample_normal(
        self, x: float, y: float, center_h: float, step_m: float
    ) -> Tuple[float, float, float]:
        h_e = self.provider.get_elevation(x + step_m, y)
        h_w = self.provider.get_elevation(x - step_m, y)
        h_n = self.provider.get_elevation(x, y + step_m)
        h_s = self.provider.get_elevation(x, y - step_m)

        if h_e is None:
            h_e = center_h
        if h_w is None:
            h_w = center_h
        if h_n is None:
            h_n = center_h
        if h_s is None:
            h_s = center_h

        dzdx = (float(h_e) - float(h_w)) / (2.0 * step_m)
        dzdy = (float(h_n) - float(h_s)) / (2.0 * step_m)
        nx = -dzdx
        ny = -dzdy
        nz = 1.0
        norm = math.sqrt(nx * nx + ny * ny + nz * nz)
        if norm <= 0.0 or not np.isfinite(norm):
            return 0.0, 0.0, 1.0
        return nx / norm, ny / norm, nz / norm

    @staticmethod
    def _mesh_distance_rings(
        d_max: float, resolution_m: Optional[float] = None
    ) -> np.ndarray:
        visual_max = max(250.0, min(float(d_max), 80_000.0))
        resolution = (
            float(resolution_m)
            if resolution_m is not None
            and np.isfinite(resolution_m)
            and resolution_m > 0
            else 30.0
        )
        near_step = max(5.0, min(20.0, resolution * 2.0))
        mid_step = max(40.0, min(80.0, resolution * 8.0))
        far_step = max(150.0, min(350.0, resolution * 24.0))
        haze_step = max(500.0, min(1200.0, resolution * 90.0))

        segments = [
            np.arange(40.0, min(1_000.0, visual_max) + near_step, near_step),
        ]
        if visual_max > 1_000.0:
            segments.append(
                np.arange(1_000.0, min(5_000.0, visual_max) + mid_step, mid_step)
            )
        if visual_max > 5_000.0:
            segments.append(
                np.arange(5_000.0, min(20_000.0, visual_max) + far_step, far_step)
            )
        if visual_max > 20_000.0:
            segments.append(
                np.arange(20_000.0, visual_max + haze_step, haze_step)
            )

        rings = np.unique(np.round(np.concatenate(segments)).astype(np.float32))
        rings = rings[rings <= visual_max]
        return rings[rings > 0]

    @staticmethod
    def _compute_mesh_visibility(
        altitudes: np.ndarray,
        valid: np.ndarray,
        margin_deg: float = 0.02,
    ) -> np.ndarray:
        visible = np.zeros_like(valid, dtype=bool)
        if altitudes.shape != valid.shape or altitudes.ndim != 2:
            return visible

        for az_idx in range(altitudes.shape[1]):
            max_alt = -np.inf
            for d_idx in range(altitudes.shape[0]):
                if not valid[d_idx, az_idx]:
                    continue
                alt = float(altitudes[d_idx, az_idx])
                if not np.isfinite(alt):
                    continue
                if alt >= max_alt - float(margin_deg):
                    visible[d_idx, az_idx] = True
                if alt > max_alt:
                    max_alt = alt
        return visible

    def build_view_mesh(
        self,
        obs_x: float,
        obs_y: float,
        obs_h_ground: float,
        *,
        d_max: float,
        delta_az_deg: float = 1.0,
    ) -> Dict:
        h_eye_abs = float(obs_h_ground) + float(self.eye_height)
        resolution_m = self._provider_nominal_resolution_m()
        distances = self._mesh_distance_rings(d_max, resolution_m)
        azimuths = np.arange(0.0, 360.0, delta_az_deg, dtype=np.float32)
        n_d = len(distances)
        n_az = len(azimuths)

        altitudes = np.full((n_d, n_az), -90.0, dtype=np.float32)
        elevations = np.zeros((n_d, n_az), dtype=np.float32)
        normal_x = np.zeros((n_d, n_az), dtype=np.float32)
        normal_y = np.zeros((n_d, n_az), dtype=np.float32)
        normal_z = np.ones((n_d, n_az), dtype=np.float32)
        valid = np.zeros((n_d, n_az), dtype=bool)

        az_rads = np.deg2rad(azimuths.astype(np.float64))
        sin_az = np.sin(az_rads)
        cos_az = np.cos(az_rads)

        for d_idx, d in enumerate(distances.astype(np.float64)):
            drop = (d * d) / (2.0 * self.R)
            for az_idx in range(n_az):
                x = obs_x + d * sin_az[az_idx]
                y = obs_y + d * cos_az[az_idx]
                h_terr = self.provider.get_elevation(x, y)
                if h_terr is None:
                    continue

                h_terr = float(h_terr)
                h_visual = h_terr - drop - h_eye_abs
                altitudes[d_idx, az_idx] = float(
                    math.degrees(math.atan2(h_visual, d))
                )
                elevations[d_idx, az_idx] = h_terr
                valid[d_idx, az_idx] = True

        visible = self._compute_mesh_visibility(altitudes, valid)
        if n_az < 8 or float(delta_az_deg) >= 30.0:
            normal_step_m = self._normal_sample_step_m()
            for d_idx, az_idx in np.argwhere(valid):
                d = float(distances[d_idx])
                x = obs_x + d * sin_az[az_idx]
                y = obs_y + d * cos_az[az_idx]
                h_terr = float(elevations[d_idx, az_idx])
                nx, ny, nz = self._sample_normal(
                    x, y, h_terr, normal_step_m
                )
                normal_x[d_idx, az_idx] = nx
                normal_y[d_idx, az_idx] = ny
                normal_z[d_idx, az_idx] = nz
        else:
            normal_x, normal_y, normal_z = compute_polar_mesh_normals(
                elevations, valid, distances, azimuths
            )

        return {
            "version": 2,
            "azimuths": azimuths,
            "distances": distances.astype(np.float32),
            "altitudes": altitudes,
            "elevations": elevations,
            "normal_x": normal_x,
            "normal_y": normal_y,
            "normal_z": normal_z,
            "valid": valid,
            "visible": visible,
        }

    def _raycast_chunk(self, args):
        """Process a chunk of azimuths for parallel execution."""
        (
            az_indices,
            sin_az_chunk,
            cos_az_chunk,
            obs_x,
            obs_y,
            h_eye_abs,
            step_m,
            d_max,
            band_defs_simple,
            R,
        ) = args

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
            miss_streak = 0
            b_idx = 0
            iterations = 0

            while d < d_max:
                iterations += 1
                if iterations > self._ray_iteration_guard:
                    break
                x = obs_x + d * c
                y = obs_y + d * s

                h_terr = self.provider.get_elevation(x, y)

                if h_terr is None:
                    miss_streak += 1
                    if miss_streak >= self._ray_miss_exit_threshold:
                        break
                    d_next = self._next_step_distance(d, step_m)
                    if not np.isfinite(d_next) or d_next <= d:
                        break
                    d = d_next
                    continue
                miss_streak = 0

                drop = (d * d) / (2.0 * R)
                h_visual = h_terr - drop - h_eye_abs
                ang = math.atan2(h_visual, d)

                while (
                    b_idx + 1 < n_bands and d >= band_defs_simple[b_idx][1]
                ):
                    b_idx += 1
                b_min, b_max = band_defs_simple[b_idx]
                if b_min <= d < b_max and ang > chunk_angles[b_idx][local_i]:
                    chunk_angles[b_idx][local_i] = ang
                    chunk_dists[b_idx][local_i] = d
                    chunk_heights[b_idx][local_i] = h_terr

                d_next = self._next_step_distance(d, step_m)
                if not np.isfinite(d_next) or d_next <= d:
                    break
                d = d_next

        return az_indices, chunk_angles, chunk_dists, chunk_heights

    def bake_progressive(
        self,
        obs_x: float,
        obs_y: float,
        obs_h_ground: Optional[float] = None,
        step_m: float = 50,
        d_max: float = 100_000,
        delta_az_deg: float = 0.5,
        band_defs: Optional[List[Dict]] = None,
        azimuth_order: Optional[List[int]] = None,
        progress_callback=None,
        preview_callback=None,
        preview_every: int = 24,
        light_sampler=None,
        abort_check=None,
    ) -> Tuple[np.ndarray, List[Dict], np.ndarray, np.ndarray, np.ndarray]:
        """
        Compute a horizon profile incrementally.

        This progressive path is intended for subprocess baking and live previews:
        the worker can request previews after useful azimuth blocks without waiting
        for the full 360° solve to finish.
        """
        import time

        if obs_h_ground is None:
            val = self.provider.get_elevation(obs_x, obs_y)
            if val is None:
                print(
                    "[HorizonEngine] Observer outside DEM coverage. Using 0."
                )
                obs_h_ground = 0
            else:
                obs_h_ground = val

        h_eye_abs = obs_h_ground + self.eye_height
        azimuths = np.arange(0, 360, delta_az_deg, dtype=np.float32)
        n_az = len(azimuths)
        light_domes = np.zeros(n_az, dtype=np.float32)
        light_peak_distances = np.zeros(n_az, dtype=np.float32)
        max_rad_per_az = np.zeros(n_az, dtype=np.float32)
        resolved_mask = np.zeros(n_az, dtype=bool)

        if band_defs is None:
            band_defs = DEFAULT_BANDS

        bands = self._build_band_buffers(n_az, band_defs)

        az_rads = np.deg2rad(azimuths)
        sin_az = np.sin(az_rads)
        cos_az = np.cos(az_rads)

        if azimuth_order is None:
            ordered_indices = list(range(n_az))
        else:
            ordered_indices = []
            seen = set()
            for idx in azimuth_order:
                try:
                    idx_int = int(idx)
                except Exception:
                    continue
                if 0 <= idx_int < n_az and idx_int not in seen:
                    ordered_indices.append(idx_int)
                    seen.add(idx_int)
            if len(ordered_indices) < n_az:
                ordered_indices.extend(i for i in range(n_az) if i not in seen)

        print(
            f"[HorizonEngine] Progressive bake {n_az} azimuths, max_dist={d_max / 1000:.0f}km..."
        )
        t0 = time.time()
        last_preview_t = t0
        completed = 0

        for az_index in ordered_indices:
            if abort_check and abort_check():
                print("[HorizonEngine] Progressive bake aborted by caller.")
                raise InterruptedError("Bake aborted")

            self._sample_single_azimuth(
                az_index=az_index,
                obs_x=obs_x,
                obs_y=obs_y,
                h_eye_abs=h_eye_abs,
                step_m=step_m,
                d_max=d_max,
                bands=bands,
                sin_az=sin_az,
                cos_az=cos_az,
                light_domes=light_domes,
                light_peak_distances=light_peak_distances,
                max_rad_per_az=max_rad_per_az,
                light_sampler=light_sampler,
            )
            resolved_mask[az_index] = True
            completed += 1

            progress_pct = (completed / n_az) * 100.0
            if progress_callback:
                progress_callback(progress_pct, f"Azimuth {completed}/{n_az}")

            if preview_callback:
                now = time.time()
                enough_samples = completed >= preview_every and (
                    completed % max(1, preview_every) == 0
                )
                enough_time = (now - last_preview_t) >= 0.35
                if completed == n_az or enough_samples or enough_time:
                    preview_callback(
                        completed,
                        n_az,
                        azimuths,
                        bands,
                        light_domes,
                        light_peak_distances,
                        resolved_mask,
                    )
                    last_preview_t = now

        elapsed = time.time() - t0
        print(f"[HorizonEngine] Progressive bake complete in {elapsed:.2f}s.")
        return (
            azimuths,
            bands,
            light_domes,
            light_peak_distances,
            resolved_mask,
        )

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
        light_sampler=None,
        abort_check=None,
    ) -> Tuple[np.ndarray, List[Dict], np.ndarray]:
        """
        Compute multi-band horizon profile (sequential — CPU-bound under GIL).

        Args:
            progress_callback: Optional callable(percent: int, msg: str).

        Returns (azimuths, bands) where bands is a list of dicts
        each containing 'id', 'angles', 'dists', 'heights' arrays.
        """
        import time

        if obs_h_ground is None:
            val = self.provider.get_elevation(obs_x, obs_y)
            if val is None:
                print(
                    "[HorizonEngine] Observer outside DEM coverage. Using 0."
                )
                obs_h_ground = 0
            else:
                obs_h_ground = val

        h_eye_abs = obs_h_ground + self.eye_height

        azimuths = np.arange(0, 360, delta_az_deg)
        n_az = len(azimuths)
        light_domes = np.zeros(n_az, dtype=np.float32)
        light_peak_distances = np.zeros(n_az, dtype=np.float32)
        max_rad_per_az = np.zeros(n_az, dtype=np.float32)

        if band_defs is None:
            band_defs = DEFAULT_BANDS

        bands = []
        for bd in band_defs:
            bands.append(
                {
                    "id": bd["id"],
                    "min": bd["min"],
                    "max": bd["max"],
                    "angles": np.full(n_az, -np.inf),
                    "dists": np.zeros(n_az),
                    "heights": np.zeros(n_az),
                    "surface_angles": np.full(n_az, -np.inf),
                    "surface_dists": np.zeros(n_az),
                    "surface_heights": np.zeros(n_az),
                }
            )

        az_rads = np.deg2rad(azimuths)
        sin_az = np.sin(az_rads)
        cos_az = np.cos(az_rads)

        print(
            f"[HorizonEngine] Baking {n_az} azimuths, max_dist={d_max / 1000:.0f}km..."
        )
        t0 = time.time()

        for i in range(n_az):
            if i % 10 == 0:
                print(
                    f"[HorizonEngine Debug] Azimuth {i}/{n_az} (ang={azimuths[i]:.1f})",
                    flush=True,
                )

            if abort_check and abort_check():
                print("[HorizonEngine] Bake ABORTED by caller request.")
                raise InterruptedError("Bake aborted")

            try:
                c = sin_az[i]
                s = cos_az[i]

                # ── Escombrat adaptatiu: molt fi a l'acostament, groller a la llunyania ──
                NEAR_START = (
                    0.5  # metres — primera mostra (resolució sub-metre)
                )
                d = NEAR_START
                max_ang_so_far = -np.pi / 2.0

                last_light_d = 0.0
                miss_streak = 0
                band_idx = 0
                iterations = 0

                while d < d_max:
                    iterations += 1
                    if iterations > self._ray_iteration_guard:
                        print(
                            f"[HorizonEngine] Warning: ray iteration guard reached at az={i}, d={d:.1f}"
                        )
                        break
                    x = obs_x + d * c
                    y = obs_y + d * s

                    h_terr = self.provider.get_elevation(x, y)
                    if h_terr is None:
                        miss_streak += 1
                        if miss_streak >= self._ray_miss_exit_threshold:
                            break
                        d_next = self._next_step_distance(d, step_m)
                        if not np.isfinite(d_next) or d_next <= d:
                            print(
                                f"[HorizonEngine] Warning: invalid step progression at az={i}, d={d:.3f}, next={d_next}"
                            )
                            break
                        d = d_next
                        continue
                    miss_streak = 0

                    drop = (d * d) / (2.0 * self.R)
                    h_visual = h_terr - drop - h_eye_abs
                    ang = math.atan2(h_visual, d)

                    if ang > max_ang_so_far:
                        max_ang_so_far = ang

                    # Light pollution sampling with occlusion checks
                    # Optimization: To avoid stalling the baker with thousands of small reads,
                    # we sample light domes sparsely, at most every 2000m.
                    # We only sample if the point is somewhat visible (within 1.5 deg of max horizon)
                    if (
                        light_sampler is not None
                        and (d - last_light_d) >= 2000.0
                    ):
                        last_light_d = d
                        rad = self._sample_light_radiance(
                            light_sampler, x, y
                        )

                        if rad and rad > 0.1:  # Catch everything that glows
                            # Distance multiplier: Inverse linear law (slower decay for atmospheric glow)
                            dist_mult = 1.0 / max(1.0, (d / 1000.0))
                            # Liberal occlusion check: allow up to 10 degrees (-0.17 rad) below horizon
                            if ang > (max_ang_so_far - 0.17):
                                # Multiply by 20.0 (balanced gain)
                                light_domes[i] += float(rad * dist_mult * 20.0)
                                if rad > max_rad_per_az[i]:
                                    max_rad_per_az[i] = float(rad)
                                    light_peak_distances[i] = float(d)

                    while (
                        band_idx + 1 < len(bands)
                        and d >= bands[band_idx]["max"]
                    ):
                        band_idx += 1
                    if (
                        0 <= band_idx < len(bands)
                        and bands[band_idx]["min"] <= d < bands[band_idx]["max"]
                    ):
                        b = bands[band_idx]
                        b["surface_angles"][i] = ang
                        b["surface_dists"][i] = d
                        b["surface_heights"][i] = h_terr
                        if ang > b["angles"][i]:
                            b["angles"][i] = ang
                            b["dists"][i] = d
                            b["heights"][i] = h_terr

                    d_next = self._next_step_distance(d, step_m)
                    if not np.isfinite(d_next) or d_next <= d:
                        print(
                            f"[HorizonEngine] Warning: invalid step progression at az={i}, d={d:.3f}, next={d_next}"
                        )
                        break
                    d = d_next
            except Exception as e:
                print(
                    f"[HorizonEngine] CRITICAL Error at Azimuth {i} (d={d:.1f}): {e}"
                )
                import traceback

                traceback.print_exc()
                raise e

            # Progress reporting (every azimuth).
            progress_pct = ((i + 1) / n_az) * 100.0
            if progress_callback:
                progress_callback(progress_pct, f"Azimuth {i + 1}/{n_az}")
            pct_bucket = int(progress_pct)
            if pct_bucket % 25 == 0 and abs(progress_pct - pct_bucket) < 1e-9:
                elapsed = time.time() - t0
                print(
                    f"[HorizonEngine]   {pct_bucket}% ({i+1}/{n_az} azimuths, {elapsed:.1f}s)"
                )

        elapsed = time.time() - t0
        print(f"[HorizonEngine] Bake complete in {elapsed:.2f}s.")
        return azimuths, bands, light_domes, light_peak_distances


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

    # Transform observer from geographic to terrain internal coordinates.
    with PYPROJ_TRANSFORMER_LOCK:
        transformer = Transformer.from_crs(
            CRS_GEOGRAPHIC, CRS_TERRAIN_INTERNAL, always_xy=True
        )
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
        print(
            "[HorizonEngine] Observer outside DEM. Using fallback 200m (Lleida plains)."
        )
        ground_h = 200.0
    else:
        print(f"[HorizonEngine] Observer altitude from DEM: {ground_h:.2f}m")

    # Bake
    azimuths, bands, light_domes, light_peak_distances = baker.bake(
        obs_x=x_utm,
        obs_y=y_utm,
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
        light_domes=light_domes,
        light_peak_distances=light_peak_distances,
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
