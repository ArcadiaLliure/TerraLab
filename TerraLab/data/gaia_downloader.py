"""Descarregador Gaia per teseles (`tile_manifest.json` + `tile_*.npz`).

Aquest mòdul no depèn de UI. Escriu teseles i manifest al disc.
"""

from __future__ import annotations

import csv
import errno
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import requests

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.data.tile_manifest import build_tile_identifier


TAP_BASE_URL = "https://gea.esac.esa.int/tap-server/tap"
DEFAULT_VISIBLE_MAG_LIMIT = 8.0

ProgressCallback = Callable[[float, str], None]


@dataclass(frozen=True)
class GaiaTileDownloaderConfig:
    """Configuració de descarrega Gaia per teseles."""

    output_dir: Path
    mag_limit: float | None = 0.0
    visible_mag_limit: float = DEFAULT_VISIBLE_MAG_LIMIT
    tile_size_deg: float = 5.0
    timeout_s: float = 120.0
    maxrec: int = -1
    state_file: Path | None = None
    max_concurrent_requests: int = 2
    request_retries: int = 3
    retry_backoff_s: float = 1.5


@dataclass(frozen=True)
class _DeepTileSpec:
    """Especificació immutable d'una tesela profunda pendent de descarregar."""

    tile_id: str
    ra_min: float
    ra_max: float
    dec_min: float
    dec_max: float


@dataclass(frozen=True)
class _DeepTileResult:
    """Resultat immutable de descarrega d'una tesela profunda."""

    tile_id: str
    file_name: str
    ra_min: float
    ra_max: float
    dec_min: float
    dec_max: float
    star_count: int


class GaiaTileDownloader:
    """Implementa el flux complet de descarrega Gaia per teseles."""

    def __init__(
        self,
        config: GaiaTileDownloaderConfig,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self._progress_callback = progress_callback

        self.output_dir = Path(config.output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        if config.state_file is not None:
            self.state_file = Path(config.state_file).expanduser().resolve()
        else:
            self.state_file = self.output_dir / "gaia_tiles_state.json"

        self.manifest_path = self.output_dir / "tile_manifest.json"
        self._state = self._load_state()

    def download(self, *, resume: bool = True) -> dict[str, object]:
        """Executa descarrega general + profunda i escriu manifest final."""
        if not resume:
            self._state = _new_state(
                mag_limit=self.config.mag_limit,
                visible_mag_limit=float(self.config.visible_mag_limit),
                tile_size_deg=float(self.config.tile_size_deg),
                output_dir=str(self.output_dir),
            )
            self._save_state()

        self._state["status"] = "running"
        self._state.pop("last_error", None)
        self._state["current_tile_id"] = ""
        self._set_state_progress(0.0, "Iniciant descarrega Gaia per teseles")
        self._save_state()
        self._emit_progress(0.0, "Iniciant descarrega Gaia per teseles")

        try:
            general_tile = self.download_general_tile()
            # Publica manifest parcial tan aviat com la tesela general estigui llesta.
            self._write_manifest(
                general_tile=general_tile,
                deep_tiles=[],
                partial=True,
            )
            deep_tiles = self.download_deep_tiles()
            deep_failed_count = int(self._state.get("deep_tiles_failed_count", 0) or 0)
            deep_tiles_done = bool(self._state.get("deep_tiles_done", False))
            self._write_manifest(
                general_tile=general_tile,
                deep_tiles=deep_tiles,
                partial=(not deep_tiles_done),
            )

            self._state["current_tile_id"] = ""
            if deep_tiles_done:
                self._state["status"] = "done"
                self._set_state_progress(
                    100.0,
                    "Descarrega Gaia per teseles finalitzada",
                )
                self._save_state()
                self._emit_progress(
                    100.0,
                    "Descarrega Gaia per teseles finalitzada",
                )
            else:
                self._state["status"] = "partial"
                partial_msg = (
                    "Descarrega parcial: "
                    f"{deep_failed_count} teseles amb error (reprenable)"
                )
                current_pct = float(self._state.get("progress_percent", 0.0) or 0.0)
                self._set_state_progress(min(99.0, max(0.0, current_pct)), partial_msg)
                self._save_state()
                self._emit_progress(min(99.0, max(0.0, current_pct)), partial_msg)

            return {
                "manifest_path": str(self.manifest_path),
                "general_tile": general_tile,
                "deep_tile_count": int(len(deep_tiles)),
                "deep_tile_failed_count": int(deep_failed_count),
                "deep_tiles_done": bool(deep_tiles_done),
                "output_dir": str(self.output_dir),
            }
        except Exception as exc:
            self._state["status"] = "error"
            self._state["last_error"] = str(exc)
            self._state["current_tile_id"] = str(
                self._state.get("current_tile_id", "") or ""
            )
            current_pct = float(self._state.get("progress_percent", 0.0) or 0.0)
            err_msg = f"Error descarrega Gaia: {exc}"
            self._set_state_progress(current_pct, err_msg)
            self._save_state()
            self._emit_progress(current_pct, err_msg)
            raise

    def _write_manifest(
        self,
        *,
        general_tile: dict[str, object],
        deep_tiles: list[dict[str, object]],
        partial: bool,
    ) -> None:
        """Escriu `tile_manifest.json` en mode parcial o final."""
        manifest_payload = {
            "version": 1,
            "tile_size_deg": float(self.config.tile_size_deg),
            "partial": bool(partial),
            "general_tile": {
                "id": "tile_all",
                "file": general_tile["file"],
                "mag_limit": float(self.config.visible_mag_limit),
                "coverage": "full_sky",
                "star_count": int(general_tile["star_count"]),
            },
            "deep_tiles": list(deep_tiles or []),
        }
        _write_json_atomic(self.manifest_path, manifest_payload)

    def download_general_tile(self) -> dict[str, object]:
        """Descarrega i escriu la tesela general (`mag < visible_mag_limit`)."""
        general_path = self.output_dir / "tile_all.npz"
        if bool(self._state.get("general_tile_done", False)) and self._is_general_tile_file_valid(
            general_path
        ):
            return {
                "id": "tile_all",
                "file": "tile_all.npz",
                "star_count": int(self._state.get("general_tile_star_count", 0) or 0),
            }

        query = (
            "SELECT source_id, ra, dec, phot_g_mean_mag, bp_rp, pmra, pmdec, parallax "
            "FROM gaiadr3.gaia_source "
            "WHERE phot_g_mean_mag IS NOT NULL "
            f"AND phot_g_mean_mag < {float(self.config.visible_mag_limit):.6f} "
            "ORDER BY phot_g_mean_mag ASC"
        )

        self._state["current_tile_id"] = "tile_all"
        self._set_state_progress(5.0, "Descarregant tesela general")
        self._save_state()
        self._emit_progress(5.0, "Descarregant tesela general")

        rows = self._run_tap_query_csv_with_retry(query, request_label="general")
        arrays = _rows_to_arrays(rows)
        _write_tile_npz(general_path, arrays)

        self._state["general_tile_done"] = True
        self._state["general_tile_star_count"] = int(len(arrays["ra"]))
        self._state["current_tile_id"] = "tile_all"
        self._set_state_progress(9.0, "Tesela general preparada")
        self._save_state()
        self._emit_progress(9.0, "Tesela general preparada")

        return {
            "id": "tile_all",
            "file": "tile_all.npz",
            "star_count": int(len(arrays["ra"])),
        }

    def download_deep_tiles(self) -> list[dict[str, object]]:
        """Descarrega teseles profundes (`mag >= visible_mag_limit`)."""
        manifest_entries: list[dict[str, object]] = []
        tile_size = float(self.config.tile_size_deg)

        deep_status = self._state.setdefault("deep_tiles", {})
        if not isinstance(deep_status, dict):
            deep_status = {}
            self._state["deep_tiles"] = deep_status

        total_tiles = int((360.0 / tile_size) * (180.0 / tile_size))
        processed_tiles = 0
        failed_tiles = 0
        pending_specs: list[_DeepTileSpec] = []

        for dec_min in _frange(-90.0, 90.0, tile_size):
            dec_max = min(90.0, dec_min + tile_size)
            for ra_min in _frange(0.0, 360.0, tile_size):
                ra_max = ra_min + tile_size
                tile_id = build_tile_identifier(ra_min=ra_min, dec_min=dec_min)

                existing = deep_status.get(tile_id)
                if isinstance(existing, dict) and bool(existing.get("done", False)):
                    star_count = int(existing.get("star_count", 0) or 0)
                    file_name = str(existing.get("file", f"{tile_id}.npz"))
                    file_path = self.output_dir / file_name
                    has_expected_file = (star_count <= 0) or self._is_output_tile_file_valid(
                        file_path
                    )
                    if not has_expected_file:
                        deep_status[tile_id] = {
                            "done": False,
                            "file": file_name,
                            "star_count": 0,
                            "last_error": (
                                "Fitxer de tesela absent o invalid; es torna a descarregar"
                            ),
                        }
                    else:
                        if star_count > 0:
                            manifest_entries.append(
                                self._deep_manifest_entry(
                                    tile_id=tile_id,
                                    file_name=file_name,
                                    ra_min=ra_min,
                                    ra_max=ra_max,
                                    dec_min=dec_min,
                                    dec_max=dec_max,
                                    star_count=star_count,
                                )
                            )
                        processed_tiles += 1
                        continue

                pending_specs.append(
                    _DeepTileSpec(
                        tile_id=tile_id,
                        ra_min=float(ra_min),
                        ra_max=float(ra_max),
                        dec_min=float(dec_min),
                        dec_max=float(dec_max),
                    )
                )

        if processed_tiles > 0:
            pct_resume = 10.0 + 85.0 * (float(processed_tiles) / float(max(1, total_tiles)))
            resume_msg = (
                f"Reprenent teseles profundes ({processed_tiles}/{total_tiles})"
            )
            self._set_state_progress(pct_resume, resume_msg)
            self._save_state()
            self._emit_progress(pct_resume, resume_msg)

        if pending_specs:
            worker_count = max(1, min(8, int(self.config.max_concurrent_requests)))
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="gaia-tile-download",
            ) as executor:
                future_map = {
                    executor.submit(self._download_deep_tile_worker, spec): spec
                    for spec in pending_specs
                }
                for future in as_completed(future_map):
                    spec = future_map[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        failed_tiles += 1
                        deep_status[spec.tile_id] = {
                            "done": False,
                            "file": f"{spec.tile_id}.npz",
                            "star_count": 0,
                            "last_error": str(exc),
                        }
                        self._state["current_tile_id"] = str(spec.tile_id)
                        handled_tiles = processed_tiles + failed_tiles
                        pct = 10.0 + 85.0 * (
                            float(handled_tiles) / float(max(1, total_tiles))
                        )
                        msg = (
                            "Procesant teseles profundes "
                            f"({handled_tiles}/{total_tiles}) "
                            f"[ERROR {spec.tile_id}]"
                        )
                        self._set_state_progress(pct, msg)
                        self._save_state()
                        self._emit_progress(pct, msg)
                        continue

                    if int(result.star_count) > 0:
                        manifest_entries.append(
                            self._deep_manifest_entry(
                                tile_id=result.tile_id,
                                file_name=result.file_name,
                                ra_min=result.ra_min,
                                ra_max=result.ra_max,
                                dec_min=result.dec_min,
                                dec_max=result.dec_max,
                                star_count=result.star_count,
                            )
                        )

                    deep_status[result.tile_id] = {
                        "done": True,
                        "file": result.file_name,
                        "star_count": int(result.star_count),
                    }
                    self._state["current_tile_id"] = str(result.tile_id)
                    processed_tiles += 1
                    handled_tiles = processed_tiles + failed_tiles
                    pct = 10.0 + 85.0 * (
                        float(handled_tiles) / float(max(1, total_tiles))
                    )
                    msg = (
                        "Procesant teseles profundes "
                        f"({handled_tiles}/{total_tiles}) "
                        f"[{result.tile_id}]"
                    )
                    self._set_state_progress(pct, msg)
                    self._save_state()
                    self._emit_progress(pct, msg)

        self._state["deep_tiles_failed_count"] = int(failed_tiles)
        self._state["deep_tiles_done"] = bool(failed_tiles == 0)
        self._state["current_tile_id"] = ""
        if failed_tiles > 0:
            warn_msg = (
                "Teseles profundes amb errors: "
                f"{failed_tiles} (reprenable)"
            )
            current_pct = float(self._state.get("progress_percent", 0.0) or 0.0)
            self._set_state_progress(min(99.0, max(0.0, current_pct)), warn_msg)
        self._save_state()

        manifest_entries.sort(
            key=lambda item: (float(item["dec_min"]), float(item["ra_min"]))
        )
        return manifest_entries

    def _download_deep_tile_worker(self, spec: _DeepTileSpec) -> _DeepTileResult:
        """Descarrega una tesela profunda concreta (worker paral·lel)."""
        query = self._build_deep_tile_query(
            ra_min=spec.ra_min,
            ra_max=spec.ra_max,
            dec_min=spec.dec_min,
            dec_max=spec.dec_max,
        )
        rows = self._run_tap_query_csv_with_retry(query, request_label=spec.tile_id)
        arrays = _rows_to_arrays(rows)
        star_count = int(len(arrays["ra"]))
        file_name = f"{spec.tile_id}.npz"

        if star_count > 0:
            tile_path = self.output_dir / file_name
            _write_tile_npz(tile_path, arrays)

        return _DeepTileResult(
            tile_id=spec.tile_id,
            file_name=file_name,
            ra_min=float(spec.ra_min),
            ra_max=float(spec.ra_max),
            dec_min=float(spec.dec_min),
            dec_max=float(spec.dec_max),
            star_count=star_count,
        )

    def _is_output_tile_file_valid(self, tile_path: Path) -> bool:
        """Valida minimament que un fitxer de tesela existeix i no es buit."""
        resolved_path = Path(tile_path).expanduser().resolve()
        if not resolved_path.is_file():
            return False
        try:
            return resolved_path.stat().st_size > 0
        except Exception:
            return False

    def _is_general_tile_file_valid(self, tile_path: Path) -> bool:
        """Valida la tesela general comprovant que conte al menys una estrella."""
        if not self._is_output_tile_file_valid(tile_path):
            return False
        try:
            with np.load(tile_path, allow_pickle=False) as tile_data:
                if "ra" in tile_data:
                    return int(len(tile_data["ra"])) > 0
                if "RA" in tile_data:
                    return int(len(tile_data["RA"])) > 0
        except Exception:
            return False
        return False

    def _deep_manifest_entry(
        self,
        *,
        tile_id: str,
        file_name: str,
        ra_min: float,
        ra_max: float,
        dec_min: float,
        dec_max: float,
        star_count: int,
    ) -> dict[str, object]:
        """Crea una entrada canònica de manifest per a una tesela profunda."""
        return {
            "id": str(tile_id),
            "file": str(file_name),
            "ra_min": float(ra_min),
            "ra_max": float(ra_max % 360.0),
            "dec_min": float(dec_min),
            "dec_max": float(dec_max),
            "mag_min": float(self.config.visible_mag_limit),
            "mag_max": _normalize_mag_limit_value(
                self.config.mag_limit, visible_mag_limit=float(self.config.visible_mag_limit)
            ),
            "star_count": int(star_count),
        }

    def _build_deep_tile_query(
        self,
        *,
        ra_min: float,
        ra_max: float,
        dec_min: float,
        dec_max: float,
    ) -> str:
        """Construeix la query ADQL per a una tesela profunda concreta."""
        visible_limit = float(self.config.visible_mag_limit)
        mag_limit = _normalize_mag_limit_value(
            self.config.mag_limit, visible_mag_limit=visible_limit
        )

        if ra_max <= 360.0:
            ra_clause = f"ra >= {ra_min:.6f} AND ra < {ra_max:.6f}"
        else:
            wrapped = ra_max - 360.0
            ra_clause = (
                f"((ra >= {ra_min:.6f} AND ra < 360.0) OR "
                f"(ra >= 0.0 AND ra < {wrapped:.6f}))"
            )
        mag_clause = (
            "" if mag_limit is None else f"AND phot_g_mean_mag <= {mag_limit:.6f} "
        )

        return (
            "SELECT source_id, ra, dec, phot_g_mean_mag, bp_rp, pmra, pmdec, parallax "
            "FROM gaiadr3.gaia_source "
            "WHERE phot_g_mean_mag IS NOT NULL "
            f"AND phot_g_mean_mag >= {visible_limit:.6f} "
            f"{mag_clause}"
            f"AND {ra_clause} "
            f"AND dec >= {dec_min:.6f} AND dec < {dec_max:.6f} "
            "ORDER BY phot_g_mean_mag ASC"
        )

    def _run_tap_query_csv_with_retry(
        self,
        query: str,
        *,
        request_label: str,
    ) -> list[dict[str, str]]:
        """Executa consulta TAP sync amb reintents i backoff exponencial."""
        attempts = max(1, int(self.config.request_retries))
        base_backoff_s = max(0.1, float(self.config.retry_backoff_s))
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._run_tap_query_csv(query)
            except Exception as exc:
                last_exc = exc
                if attempt >= attempts:
                    break
                wait_s = base_backoff_s * (2.0 ** float(attempt - 1))
                time.sleep(wait_s)

        assert last_exc is not None
        raise RuntimeError(
            f"Consulta TAP fallida ({request_label}) després de {attempts} intents: {last_exc}"
        ) from last_exc

    def _run_tap_query_csv(self, query: str) -> list[dict[str, str]]:
        """Executa consulta TAP sync i retorna files CSV com a diccionaris."""
        assert requests is not None

        endpoint = f"{TAP_BASE_URL}/sync"
        params = {
            "REQUEST": "doQuery",
            "LANG": "ADQL",
            "FORMAT": "csv",
            "QUERY": query,
            "MAXREC": str(int(self.config.maxrec)),
        }

        response = requests.post(
            endpoint,
            data=params,
            timeout=float(self.config.timeout_s),
        )
        response.raise_for_status()
        text = str(response.text or "").strip()
        if not text:
            return []

        reader = csv.DictReader(text.splitlines())
        return [row for row in reader if isinstance(row, dict)]

    def _emit_progress(self, percent: float, message: str) -> None:
        """Publica progrés per callback i `stdout` compatible onboarding."""
        pct = max(0.0, min(100.0, float(percent)))
        msg = str(message)
        print(f"[gaia-progress] {pct:5.1f}% {msg}")
        if callable(self._progress_callback):
            try:
                self._progress_callback(pct, msg)
            except Exception:
                log_suppressed_exception(__name__, "GaiaTileDownloader._emit_progress")

    def _set_state_progress(self, percent: float, message: str) -> None:
        """Actualitza metadades de progrés dins l'estat resumible."""
        self._state["progress_percent"] = float(
            max(0.0, min(100.0, float(percent)))
        )
        self._state["status_message"] = str(message)

    def _load_state(self) -> dict[str, object]:
        """Carrega estat resumible des del fitxer JSON de descarrega."""
        default_state = _new_state(
            mag_limit=self.config.mag_limit,
            visible_mag_limit=float(self.config.visible_mag_limit),
            tile_size_deg=float(self.config.tile_size_deg),
            output_dir=str(self.output_dir),
        )
        if not self.state_file.is_file():
            return default_state

        try:
            with self.state_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                merged = dict(default_state)
                merged.update(payload)
                if not isinstance(merged.get("deep_tiles"), dict):
                    merged["deep_tiles"] = {}
                if self._state_requires_reset_for_config(merged):
                    return default_state
                return merged
        except Exception:
            log_suppressed_exception(__name__, "GaiaTileDownloader._load_state")

        return default_state

    def _state_requires_reset_for_config(self, state: dict[str, object]) -> bool:
        """Comprova si l'estat resumit es incompatible amb la configuracio actual."""
        try:
            state_visible = float(
                state.get("visible_mag_limit", self.config.visible_mag_limit)
            )
        except Exception:
            state_visible = float(self.config.visible_mag_limit)
        try:
            state_tile_size = float(
                state.get("tile_size_deg", self.config.tile_size_deg)
            )
        except Exception:
            state_tile_size = float(self.config.tile_size_deg)

        cfg_visible = float(self.config.visible_mag_limit)
        cfg_tile_size = float(self.config.tile_size_deg)
        if not math.isclose(state_visible, cfg_visible, abs_tol=1e-6):
            print(
                "[gaia-info] State reset: visible_mag_limit changed "
                f"({state_visible:.6f} -> {cfg_visible:.6f})"
            )
            return True
        if not math.isclose(state_tile_size, cfg_tile_size, abs_tol=1e-6):
            print(
                "[gaia-info] State reset: tile_size_deg changed "
                f"({state_tile_size:.6f} -> {cfg_tile_size:.6f})"
            )
            return True

        state_upper = _normalize_mag_limit_value(
            state.get("mag_limit"), visible_mag_limit=state_visible
        )
        cfg_upper = _normalize_mag_limit_value(
            self.config.mag_limit, visible_mag_limit=cfg_visible
        )

        if cfg_upper is None and state_upper is not None:
            print(
                "[gaia-info] State reset: requested unlimited mag_limit but "
                f"state was capped at {state_upper:.2f}"
            )
            return True
        if cfg_upper is not None:
            if state_upper is None:
                # Reusing an unlimited state for a finite run is acceptable.
                return False
            if (state_upper + 1e-6) < cfg_upper:
                print(
                    "[gaia-info] State reset: requested deeper mag_limit "
                    f"({cfg_upper:.2f}) than state ({state_upper:.2f})"
                )
                return True
        return False

    def _save_state(self) -> None:
        """Escriu estat resumible de manera atòmica."""
        try:
            _write_json_atomic(self.state_file, self._state)
        except OSError as exc:
            # On Windows, another process temporarily reading the same file can
            # block atomic replace. State save is best-effort and should not
            # abort the whole download.
            is_access_error = isinstance(exc, PermissionError) or (
                int(getattr(exc, "winerror", 0) or 0) == 5
            ) or (
                int(getattr(exc, "errno", 0) or 0)
                in {int(errno.EACCES), int(errno.EPERM)}
            )
            if not is_access_error:
                raise
            now_mono = float(time.monotonic())
            last_mono = float(getattr(self, "_state_save_warn_mono", 0.0) or 0.0)
            if (now_mono - last_mono) >= 2.0:
                print(
                    "[gaia-warn] Could not persist state (locked file); "
                    "will retry on next tick."
                )
                self._state_save_warn_mono = now_mono


def _normalize_mag_limit_value(
    raw_mag_limit: object,
    *,
    visible_mag_limit: float,
) -> float | None:
    """Normalitza mag_limit: <=0, no numeric o <= visible -> sense limit superior."""
    try:
        value = float(raw_mag_limit)
    except Exception:
        return None
    if not math.isfinite(value):
        return None
    if value <= 0.0:
        return None
    if value <= float(visible_mag_limit):
        return None
    return float(value)


def _new_state(
    *,
    mag_limit: float | None,
    visible_mag_limit: float,
    tile_size_deg: float,
    output_dir: str,
) -> dict[str, object]:
    """Crea estat inicial de descarrega per teseles."""
    normalized_mag_limit = _normalize_mag_limit_value(
        mag_limit, visible_mag_limit=float(visible_mag_limit)
    )
    return {
        "version": 2,
        "status": "running",
        "status_message": "",
        "progress_percent": 0.0,
        "current_tile_id": "",
        "output_dir": str(output_dir),
        "mag_limit": (
            None if normalized_mag_limit is None else float(normalized_mag_limit)
        ),
        "visible_mag_limit": float(visible_mag_limit),
        "tile_size_deg": float(tile_size_deg),
        "general_tile_done": False,
        "general_tile_star_count": 0,
        "deep_tiles_done": False,
        "deep_tiles_failed_count": 0,
        "deep_tiles": {},
    }


def _rows_to_arrays(rows: list[dict[str, str]]) -> dict[str, np.ndarray]:
    """Converteix files CSV en arrays numpy normals."""
    if not rows:
        return {
            "source_id": np.empty(0, dtype=np.int64),
            "ra": np.empty(0, dtype=np.float32),
            "dec": np.empty(0, dtype=np.float32),
            "phot_g_mean_mag": np.empty(0, dtype=np.float32),
            "bp_rp": np.empty(0, dtype=np.float32),
            "pmra": np.empty(0, dtype=np.float32),
            "pmdec": np.empty(0, dtype=np.float32),
            "parallax": np.empty(0, dtype=np.float32),
        }

    source_id = np.empty(len(rows), dtype=np.int64)
    ra = np.empty(len(rows), dtype=np.float32)
    dec = np.empty(len(rows), dtype=np.float32)
    mag = np.empty(len(rows), dtype=np.float32)
    bp_rp = np.empty(len(rows), dtype=np.float32)
    pmra = np.empty(len(rows), dtype=np.float32)
    pmdec = np.empty(len(rows), dtype=np.float32)
    parallax = np.empty(len(rows), dtype=np.float32)

    for index, row in enumerate(rows):
        source_id[index] = _to_int(row.get("source_id"), default=-1)
        ra[index] = _to_float(row.get("ra"), default=np.nan)
        dec[index] = _to_float(row.get("dec"), default=np.nan)
        mag[index] = _to_float(row.get("phot_g_mean_mag"), default=np.nan)
        bp_rp[index] = _to_float(row.get("bp_rp"), default=0.8)
        pmra[index] = _to_float(row.get("pmra"), default=np.nan)
        pmdec[index] = _to_float(row.get("pmdec"), default=np.nan)
        parallax[index] = _to_float(row.get("parallax"), default=np.nan)

    valid_mask = np.isfinite(ra) & np.isfinite(dec) & np.isfinite(mag)
    source_id = source_id[valid_mask]
    ra = ra[valid_mask]
    dec = dec[valid_mask]
    mag = mag[valid_mask]
    bp_rp = bp_rp[valid_mask]
    pmra = pmra[valid_mask]
    pmdec = pmdec[valid_mask]
    parallax = parallax[valid_mask]

    order = np.argsort(mag, kind="mergesort")
    return {
        "source_id": np.asarray(source_id[order], dtype=np.int64),
        "ra": np.asarray(ra[order], dtype=np.float32),
        "dec": np.asarray(dec[order], dtype=np.float32),
        "phot_g_mean_mag": np.asarray(mag[order], dtype=np.float32),
        "bp_rp": np.asarray(bp_rp[order], dtype=np.float32),
        "pmra": np.asarray(pmra[order], dtype=np.float32),
        "pmdec": np.asarray(pmdec[order], dtype=np.float32),
        "parallax": np.asarray(parallax[order], dtype=np.float32),
    }


def _to_int(raw: str | None, *, default: int) -> int:
    """Converteix text a `int` amb fallback."""
    try:
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(str(raw)))
    except Exception:
        return int(default)


def _to_float(raw: str | None, *, default: float) -> float:
    """Converteix text a `float` amb fallback."""
    try:
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw))
    except Exception:
        return float(default)


def _write_tile_npz(path: Path, arrays: dict[str, np.ndarray]) -> None:
    """Escriu fitxer NPZ de tesela de forma atòmica."""
    out_path = Path(path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(
        f"{out_path.name}.{os.getpid()}.{int(time.time_ns())}.tmp"
    )
    with tmp_path.open("wb") as handle:
        np.savez_compressed(handle, **arrays)
    attempts = 30
    for attempt in range(1, attempts + 1):
        try:
            tmp_path.replace(out_path)
            return
        except OSError as exc:
            is_access_error = isinstance(exc, PermissionError) or (
                int(getattr(exc, "winerror", 0) or 0) == 5
            ) or (
                int(getattr(exc, "errno", 0) or 0)
                in {int(errno.EACCES), int(errno.EPERM)}
            )
            if (not is_access_error) or attempt >= attempts:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    log_suppressed_exception(__name__, "_write_tile_npz")
                raise
            time.sleep(min(0.50, 0.02 * float(attempt)))


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    """Escriu JSON atòmic per estat/manifest."""
    out_path = Path(path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(
        f"{out_path.name}.{os.getpid()}.{int(time.time_ns())}.tmp"
    )
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)
    attempts = 30
    for attempt in range(1, attempts + 1):
        try:
            tmp_path.replace(out_path)
            return
        except OSError as exc:
            is_access_error = isinstance(exc, PermissionError) or (
                int(getattr(exc, "winerror", 0) or 0) == 5
            ) or (
                int(getattr(exc, "errno", 0) or 0)
                in {int(errno.EACCES), int(errno.EPERM)}
            )
            if (not is_access_error) or attempt >= attempts:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    log_suppressed_exception(__name__, "_write_json_atomic")
                raise
            time.sleep(min(0.50, 0.02 * float(attempt)))


def _frange(start: float, stop: float, step: float):
    """Iterador de `float` determinista per graelles de teseles."""
    value = float(start)
    while value < float(stop) - 1e-9:
        yield round(value, 6)
        value += float(step)
