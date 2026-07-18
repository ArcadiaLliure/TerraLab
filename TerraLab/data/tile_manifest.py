"""Lector del manifest de teseles Gaia.

Aquest modul defineix el contracte comu entre descarrega i carrega de teseles.
No fa IO de render ni cap logica de UI.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


def build_tile_identifier(ra_min: float, dec_min: float) -> str:
    """Construeix l'identificador canonica d'una tesela profunda."""
    ra_norm = int(round(float(ra_min))) % 360
    dec_norm = int(round(float(dec_min)))
    return f"tile_{ra_norm:04d}_{dec_norm:+03d}"


@dataclass(frozen=True)
class TileEntry:
    """Representa una tesela del cataleg Gaia."""

    tile_id: str
    file_path: Path
    ra_min: float
    ra_max: float
    dec_min: float
    dec_max: float
    mag_min: float
    mag_max: float | None
    star_count: int


class TileManifest:
    """Lector i consulta del fitxer tile_manifest.json."""

    def __init__(self) -> None:
        self.version: int = 1
        self.tile_size_deg: float = 5.0
        self._manifest_path: Path | None = None
        self._general_tile: TileEntry | None = None
        self._deep_tiles: list[TileEntry] = []
        self._tiles_by_id: dict[str, TileEntry] = {}

    @property
    def manifest_path(self) -> Path | None:
        """Retorna la ruta de manifest carregada o None si no n'hi ha."""
        return self._manifest_path

    @property
    def deep_tiles(self) -> tuple[TileEntry, ...]:
        """Retorna una vista immutable de les teseles profundes."""
        return tuple(self._deep_tiles)

    def load(self, path: Path) -> None:
        """Llegeix el manifest des del disc i el deixa en memoria."""
        manifest_path = Path(path).expanduser().resolve()
        with manifest_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        if not isinstance(payload, dict):
            raise ValueError("El manifest de teseles ha de ser un objecte JSON.")

        self.version = int(payload.get("version", 1) or 1)
        self.tile_size_deg = float(payload.get("tile_size_deg", 5.0) or 5.0)
        self._manifest_path = manifest_path

        base_dir = manifest_path.parent
        general_raw = payload.get("general_tile") or {}
        if not isinstance(general_raw, dict):
            raise ValueError("'general_tile' ha de ser un objecte JSON.")

        general_file = Path(str(general_raw.get("file", "tile_all.npz")))
        if not general_file.is_absolute():
            general_file = (base_dir / general_file).resolve()

        general_mag_limit = float(general_raw.get("mag_limit", 8.0) or 8.0)
        general_star_count = int(general_raw.get("star_count", 0) or 0)
        general_tile_id = str(general_raw.get("id", "tile_all") or "tile_all")

        self._general_tile = TileEntry(
            tile_id=general_tile_id,
            file_path=general_file,
            ra_min=0.0,
            ra_max=360.0,
            dec_min=-90.0,
            dec_max=90.0,
            mag_min=-99.0,
            mag_max=general_mag_limit,
            star_count=max(0, general_star_count),
        )

        deep_entries: list[TileEntry] = []
        for raw_item in payload.get("deep_tiles", []) or []:
            if not isinstance(raw_item, dict):
                continue

            file_name = Path(str(raw_item.get("file", "")).strip())
            if str(file_name) == "":
                continue
            if not file_name.is_absolute():
                file_name = (base_dir / file_name).resolve()

            ra_min = _float_or_default(raw_item.get("ra_min"), 0.0) % 360.0
            ra_max = _float_or_default(
                raw_item.get("ra_max"), ra_min + self.tile_size_deg
            )
            dec_min = _float_or_default(raw_item.get("dec_min"), -90.0)
            dec_max = _float_or_default(
                raw_item.get("dec_max"), dec_min + self.tile_size_deg
            )

            tile_id = str(raw_item.get("id", "") or "").strip()
            if not tile_id:
                tile_id = build_tile_identifier(ra_min=ra_min, dec_min=dec_min)

            deep_entries.append(
                TileEntry(
                    tile_id=tile_id,
                    file_path=file_name,
                    ra_min=ra_min,
                    ra_max=ra_max,
                    dec_min=dec_min,
                    dec_max=dec_max,
                    mag_min=float(raw_item.get("mag_min", self._general_tile.mag_max) or self._general_tile.mag_max),
                    mag_max=_optional_float(raw_item.get("mag_max", None)),
                    star_count=max(0, int(raw_item.get("star_count", 0) or 0)),
                )
            )

        self._deep_tiles = deep_entries
        self._tiles_by_id = {entry.tile_id: entry for entry in self._deep_tiles}

    def get_general_tile(self) -> TileEntry:
        """Retorna la tesela general (mag < 8, cobertura total)."""
        if self._general_tile is None:
            raise RuntimeError("Cal cridar load() abans de consultar la tesela general.")
        return self._general_tile

    def get_tiles_for_region(
        self,
        ra_center: float,
        dec_center: float,
        radius_deg: float,
    ) -> list[TileEntry]:
        """Retorna teseles profundes que solapen una regio celeste."""
        if not self._deep_tiles:
            return []

        ra_center_norm = float(ra_center) % 360.0
        dec_center_clamped = max(-90.0, min(90.0, float(dec_center)))
        radius = max(0.0, float(radius_deg))

        ra_min = (ra_center_norm - radius) % 360.0
        ra_max = (ra_center_norm + radius) % 360.0
        dec_min = max(-90.0, dec_center_clamped - radius)
        dec_max = min(90.0, dec_center_clamped + radius)

        matched: list[TileEntry] = []
        for tile in self._deep_tiles:
            if not _dec_overlap(dec_min, dec_max, tile.dec_min, tile.dec_max):
                continue
            if _ra_overlap_wrap(ra_min, ra_max, tile.ra_min, tile.ra_max):
                matched.append(tile)

        return matched

    def get_primary_tile_for_region(
        self,
        ra_center: float,
        dec_center: float,
        radius_deg: float,
    ) -> TileEntry | None:
        """Retorna la tesela solapada més propera al centre consultat."""
        matched = self.get_tiles_for_region(
            ra_center=ra_center,
            dec_center=dec_center,
            radius_deg=radius_deg,
        )
        if not matched:
            return None

        center_ra = float(ra_center) % 360.0
        center_dec = max(-90.0, min(90.0, float(dec_center)))

        def _distance_key(tile: TileEntry) -> tuple[float, int]:
            tile_ra = (
                0.5 * (float(tile.ra_min) + float(tile.ra_max))
            ) % 360.0
            tile_dec = 0.5 * (float(tile.dec_min) + float(tile.dec_max))
            delta_ra = abs(((tile_ra - center_ra + 180.0) % 360.0) - 180.0)
            delta_dec = abs(float(tile_dec - center_dec))
            # Pes lleugerament superior en RA per evitar empats arbitraris
            # entre teseles que només solapen la finestra per radi.
            return (
                float(delta_ra * delta_ra + delta_dec * delta_dec),
                int(getattr(tile, "star_count", 0) or 0) * -1,
            )

        return min(matched, key=_distance_key)

    def get_adjacent_tiles(self, tile_id: str) -> list[TileEntry]:
        """Retorna les 8 teseles veines de la tesela indicada."""
        center = self._tiles_by_id.get(str(tile_id))
        if center is None:
            return []

        tile_size = max(0.1, float(self.tile_size_deg))
        neighbors: list[TileEntry] = []
        seen: set[str] = set()

        for ra_step in (-1, 0, 1):
            for dec_step in (-1, 0, 1):
                if ra_step == 0 and dec_step == 0:
                    continue

                target_ra = (center.ra_min + ra_step * tile_size) % 360.0
                target_dec = center.dec_min + dec_step * tile_size
                if target_dec < -90.0 or target_dec >= 90.0:
                    continue

                target_id = build_tile_identifier(target_ra, target_dec)
                neighbor = self._tiles_by_id.get(target_id)
                if neighbor is None:
                    continue
                if neighbor.tile_id in seen:
                    continue
                seen.add(neighbor.tile_id)
                neighbors.append(neighbor)

        return neighbors


def _dec_overlap(a_min: float, a_max: float, b_min: float, b_max: float) -> bool:
    """Comprova solapament entre dos intervals de declinacio."""
    return (a_min <= b_max) and (b_min <= a_max)


def _ra_overlap_wrap(a_min: float, a_max: float, b_min: float, b_max: float) -> bool:
    """Comprova solapament d'intervals RA considerant wrap 0/360."""
    a_ranges = _normalize_ra_interval(a_min, a_max)
    b_ranges = _normalize_ra_interval(b_min, b_max)
    for a_lo, a_hi in a_ranges:
        for b_lo, b_hi in b_ranges:
            if a_lo <= b_hi and b_lo <= a_hi:
                return True
    return False


def _normalize_ra_interval(ra_min: float, ra_max: float) -> list[tuple[float, float]]:
    """Converteix interval RA en 1 o 2 trams no wrap en [0, 360]."""
    lo = float(ra_min) % 360.0
    hi_raw = float(ra_max)

    # Cobertura total explicita.
    if math.isclose((hi_raw - float(ra_min)) % 360.0, 0.0, abs_tol=1e-9) and hi_raw != float(ra_min):
        return [(0.0, 360.0)]

    hi = hi_raw % 360.0
    if lo <= hi:
        return [(lo, hi)]
    return [(0.0, hi), (lo, 360.0)]


def _float_or_default(value: object, default: float) -> float:
    """Converteix a float preservant zeros explicits del manifest."""
    if value is None:
        return float(default)
    return float(value)


def _optional_float(value: object) -> float | None:
    """Converteix a float opcional; retorna None quan no hi ha valor numeric valid."""
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        parsed = float(text)
    except Exception:
        return None
    if not math.isfinite(parsed):
        return None
    return float(parsed)
