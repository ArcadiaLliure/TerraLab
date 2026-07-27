"""Runtime reader for HEALPix-ordered Gaia catalogs.

The reader keeps the large NPY catalog memory-mapped and loads only a small
index file in RAM. Querying a FoV reads only the row ranges mapped by the
HEALPix pixels intersecting the view.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import numpy as np
from TerraLab.common.exception_reporting import log_suppressed_exception

try:
    import hpgeom as hpg
except Exception:  # pragma: no cover
    hpg = None

try:
    import healpy as hp
except Exception:  # pragma: no cover
    hp = None


def _backend_name() -> str:
    if hpg is not None:
        return "hpgeom"
    if hp is not None:
        return "healpy"
    return "none"


def _is_nside_ok(nside: int) -> bool:
    if hpg is not None:
        try:
            _ = hpg.nside_to_npixel(int(nside))
            return True
        except Exception:
            return False
    if hp is not None:
        return bool(hp.isnsideok(int(nside)))
    return False


def _query_disc_pixels(
    nside: int, ra_deg: float, dec_deg: float, radius_deg: float
) -> np.ndarray:
    if hpg is not None:
        return np.asarray(
            hpg.query_circle(
                int(nside),
                float(ra_deg),
                float(dec_deg),
                float(radius_deg),
                inclusive=True,
                nest=True,
                lonlat=True,
                degrees=True,
            ),
            dtype=np.uint32,
        )
    if hp is not None:
        vec = hp.ang2vec(float(ra_deg), float(dec_deg), lonlat=True)
        return np.asarray(
            hp.query_disc(
                int(nside),
                vec,
                np.radians(float(radius_deg)),
                nest=True,
                inclusive=True,
            ),
            dtype=np.uint32,
        )
    raise RuntimeError(
        "No HEALPix backend available. Install hpgeom or healpy."
    )


class CatalegEstelarHEALPix:
    """Read HEALPix-indexed star catalogs with partial on-demand access."""

    def __init__(self, catalog_npy: str | Path, index_npz: str | Path):
        """Open catalog mmap and load index arrays in memory.

        Parameters
        ----------
        catalog_npy:
            Path to `stars_catalog_healpy.npy`.
        index_npz:
            Path to `stars_catalog_healpy.idx.npz`.
        """
        if _backend_name() == "none":
            raise RuntimeError(
                "A HEALPix backend is required. Install with: pip install hpgeom"
            )

        self.catalog_path = Path(catalog_npy).expanduser().resolve()
        self.index_path = Path(index_npz).expanduser().resolve()

        self._catalog = np.load(
            self.catalog_path, mmap_mode="r", allow_pickle=False
        )
        if (
            not isinstance(self._catalog, np.ndarray)
            or self._catalog.dtype.names is None
        ):
            raise ValueError(
                f"Expected structured NPY catalog: {self.catalog_path}"
            )

        with np.load(self.index_path, allow_pickle=False) as idx:
            self.pixels_unics = np.asarray(
                idx["pixels_unics"], dtype=np.uint32
            )
            self.inicis = np.asarray(idx["inicis"], dtype=np.int64)
            self.comptes = np.asarray(idx["comptes"], dtype=np.int64)
            nside_arr = np.asarray(idx["nside"]).reshape(-1)
            if len(nside_arr) <= 0:
                raise ValueError(
                    f"Index '{self.index_path}' does not contain a valid nside value."
                )
            self.nside = int(nside_arr[0])

        self._validate_index()
        self._closed = False

    def close(self) -> None:
        """Close memory-mapped file handles.

        On Windows this is important to release file locks so files can be
        replaced or deleted.
        """
        if bool(getattr(self, "_closed", False)):
            return
        try:
            mm = getattr(self._catalog, "_mmap", None)
            if mm is not None:
                mm.close()
        except Exception:
            log_suppressed_exception(__name__, "CatalegEstelarHEALPix.close")
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, exc, _tb):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            log_suppressed_exception(__name__, "CatalegEstelarHEALPix.__del__")

    def _validate_index(self) -> None:
        """Validate index shape and monotonic properties."""
        if (
            self.pixels_unics.ndim != 1
            or self.inicis.ndim != 1
            or self.comptes.ndim != 1
        ):
            raise ValueError("HEALPix index arrays must be 1D.")
        if not (
            len(self.pixels_unics) == len(self.inicis) == len(self.comptes)
        ):
            raise ValueError("HEALPix index arrays length mismatch.")
        if len(self.pixels_unics) > 1 and np.any(
            self.pixels_unics[1:] < self.pixels_unics[:-1]
        ):
            raise ValueError(
                "HEALPix index pixels_unics must be sorted ascending."
            )
        if len(self.comptes) > 0 and np.any(self.comptes < 0):
            raise ValueError("HEALPix index comptes cannot be negative.")
        if not _is_nside_ok(int(self.nside)):
            raise ValueError(f"Invalid NSIDE in index: {self.nside}")

    @staticmethod
    def _empty_result() -> (
        Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]
    ):
        """Return empty arrays with expected dtypes."""
        return (
            np.empty(0, dtype=np.float64),  # ra
            np.empty(0, dtype=np.float64),  # dec
            np.empty(0, dtype=np.float32),  # mag
            np.empty(0, dtype=np.float32),  # bp_rp
        )

    def estrelles_en_fov(
        self,
        ra_centre: float,
        dec_centre: float,
        radi_graus: float,
        mag_limit: float,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return stars in a FoV cone using sparse HEALPix lookups.

        Parameters
        ----------
        ra_centre:
            FoV center right ascension in degrees.
        dec_centre:
            FoV center declination in degrees.
        radi_graus:
            FoV angular radius in degrees.
        mag_limit:
            Maximum `phot_g_mean_mag` to include.

        Returns
        -------
        tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]
            `(ra, dec, mag, bp_rp)` arrays for matching stars.
        """
        if bool(getattr(self, "_closed", False)):
            raise RuntimeError("Catalog reader is closed.")
        radius_deg = float(radi_graus)
        if (not np.isfinite(radius_deg)) or radius_deg <= 0.0:
            return self._empty_result()

        pix = _query_disc_pixels(
            int(self.nside),
            float(ra_centre),
            float(dec_centre),
            float(radius_deg),
        )
        if len(pix) <= 0:
            return self._empty_result()

        query_pixels = np.unique(np.asarray(pix, dtype=np.uint32))
        lookup_idx = np.searchsorted(self.pixels_unics, query_pixels)
        valid = (lookup_idx >= 0) & (lookup_idx < len(self.pixels_unics))
        lookup_idx = lookup_idx[valid]
        if len(lookup_idx) <= 0:
            return self._empty_result()

        eq_mask = self.pixels_unics[lookup_idx] == query_pixels[valid]
        lookup_idx = lookup_idx[eq_mask]
        if len(lookup_idx) <= 0:
            return self._empty_result()

        mag_lim = float(mag_limit)
        has_mag_lim = np.isfinite(mag_lim)
        ra0_rad = np.radians(float(ra_centre))
        dec0_rad = np.radians(float(dec_centre))
        sin_dec0 = float(np.sin(dec0_rad))
        cos_dec0 = float(np.cos(dec0_rad))
        cos_radius = float(np.cos(np.radians(radius_deg)))

        ra_parts = []
        dec_parts = []
        mag_parts = []
        bprp_parts = []
        for pos in lookup_idx:
            start = int(self.inicis[pos])
            count = int(self.comptes[pos])
            if count <= 0:
                continue
            stop = start + count
            chunk = self._catalog[start:stop]
            if has_mag_lim:
                keep = (
                    np.asarray(chunk["phot_g_mean_mag"], dtype=np.float32)
                    <= mag_lim
                )
                if not np.any(keep):
                    continue
                chunk = chunk[keep]
            if len(chunk) <= 0:
                continue

            # query_disc returns intersecting pixels; apply exact angular cone filter.
            ra_rad = np.radians(np.asarray(chunk["ra"], dtype=np.float64))
            dec_rad = np.radians(np.asarray(chunk["dec"], dtype=np.float64))
            cos_sep = np.sin(dec_rad) * sin_dec0 + np.cos(
                dec_rad
            ) * cos_dec0 * np.cos(ra_rad - ra0_rad)
            keep_fov = cos_sep >= cos_radius
            if not np.any(keep_fov):
                continue
            chunk = chunk[keep_fov]
            if len(chunk) <= 0:
                continue

            ra_parts.append(np.asarray(chunk["ra"], dtype=np.float64))
            dec_parts.append(np.asarray(chunk["dec"], dtype=np.float64))
            mag_parts.append(
                np.asarray(chunk["phot_g_mean_mag"], dtype=np.float32)
            )
            bprp_parts.append(np.asarray(chunk["bp_rp"], dtype=np.float32))

        if not ra_parts:
            return self._empty_result()

        return (
            np.concatenate(ra_parts),
            np.concatenate(dec_parts),
            np.concatenate(mag_parts),
            np.concatenate(bprp_parts),
        )
