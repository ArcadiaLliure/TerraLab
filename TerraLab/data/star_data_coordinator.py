"""Coordinador de dades d'estrelles basat en teseles.

No pinta ni toca UI. Orquestra IO de cataleg i emet senyals Qt.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

from TerraLab.common.performance import (
    DEFAULT_PERFORMANCE_BUDGET,
    GenerationController,
    PERFORMANCE_FLAGS,
)
from TerraLab.data.star_catalog_store import create_star_catalog_store
from TerraLab.data.tile_manifest import TileEntry, TileManifest
from TerraLab.render.stars_renderer import build_scope_spatial_index_payload
from TerraLab.widgets.sky_legacy_components import (
    _bp_rp_to_rgb_arrays,
    _load_no_gaia_star_arrays,
)


class StarDataCoordinator(QObject):
    """Orquestra la carrega de teseles Gaia en segon pla."""

    general_tile_ready = pyqtSignal(object)
    deep_tile_ready = pyqtSignal(str, object)
    scope_index_ready = pyqtSignal(object)
    extension_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, manifest_path: str | Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._manifest = TileManifest()
        manifest_file_path = Path(manifest_path).expanduser().resolve()
        self._manifest.load(manifest_file_path)
        self._catalog_dir = manifest_file_path.parent

        self._loaded_tiles: dict[str, dict[str, np.ndarray]] = {}
        self._base_tile_id: str = self._manifest.get_general_tile().tile_id
        self._no_gaia_tile_id: str = "__no_gaia_supplement__"
        self._no_gaia_supplement = _load_no_gaia_supplement_arrays(
            self._catalog_dir
        )
        if self._no_gaia_supplement is not None:
            try:
                print(
                    "[StarDataCoordinator] no-Gaia supplement loaded: "
                    f"{int(len(self._no_gaia_supplement.get('ra', [])))} stars"
                )
            except Exception:
                pass
        self._active_dataset: dict[str, Any] = _empty_dataset()

        io_workers = min(4, max(1, int(os.cpu_count() or 4) // 4))
        self._executor = ThreadPoolExecutor(max_workers=io_workers, thread_name_prefix="star-data")
        self._preload_executor = ThreadPoolExecutor(max_workers=io_workers, thread_name_prefix="star-preload")
        self._index_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scope-index")
        self._query_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="star-query")
        self._lock = threading.Lock()
        self._last_scope_index_signature: Any = None
        self._active_tiles_signature: tuple[str, ...] | None = None
        self._scope_focus_tile_id: str = ""
        self._scope_active_tile_ids: set[str] = set()
        self._scope_pending_focus_tile_id: str = ""
        self._scope_pending_active_tile_ids: set[str] = set()
        self._scope_pending_priority_tile_ids: set[str] = set()
        self._tile_load_inflight: set[str] = set()
        self._tile_priority_inflight: set[str] = set()
        self._tile_access_seq: int = 0
        self._tile_last_access: dict[str, int] = {}
        self._max_cached_deep_bytes = int(DEFAULT_PERFORMANCE_BUDGET.stars_bytes)
        self._tile_resident_bytes: dict[str, int] = {}
        self._query_generations = GenerationController()
        self._catalog_store = None

    def shutdown(self) -> None:
        """Tanca executors interns del coordinador."""
        self._query_generations.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._preload_executor.shutdown(wait=False, cancel_futures=True)
        self._index_executor.shutdown(wait=False, cancel_futures=True)
        # The active query observes its generation between chunks.  Join it
        # before closing the mmap so a shutdown cannot race a worker read.
        self._query_executor.shutdown(wait=True, cancel_futures=True)
        if self._catalog_store is not None:
            self._catalog_store.close()
            self._catalog_store = None

    def query_cone(
        self,
        ra_deg: float,
        dec_deg: float,
        radius_deg: float,
        mag_limit: float,
        *,
        max_batch_rows: int = 1_000_000,
    ):
        """Return a last-request-wins out-of-core query iterator."""

        with self._lock:
            if self._catalog_store is None:
                self._catalog_store = create_star_catalog_store(
                    self._catalog_dir,
                    prefer_healpix=PERFORMANCE_FLAGS.gaia_out_of_core,
                )
            store = self._catalog_store
            token = self._query_generations.next()
        return store.query_cone(
            ra_deg,
            dec_deg,
            radius_deg,
            mag_limit,
            max_batch_rows=max_batch_rows,
            token=token,
        )

    def request_cone_region(
        self,
        ra_deg: float,
        dec_deg: float,
        radius_deg: float,
        mag_limit: float = 22.0,
    ) -> None:
        """Asynchronously publish an exact, cancellable out-of-core scope payload."""

        token = self._query_generations.next()
        self._query_executor.submit(
            self._query_cone_worker,
            float(ra_deg),
            float(dec_deg),
            float(radius_deg),
            float(mag_limit),
            token,
        )

    def _query_cone_worker(
        self,
        ra_deg: float,
        dec_deg: float,
        radius_deg: float,
        mag_limit: float,
        token,
    ) -> None:
        try:
            with self._lock:
                if self._catalog_store is None:
                    self._catalog_store = create_star_catalog_store(
                        self._catalog_dir,
                        prefer_healpix=PERFORMANCE_FLAGS.gaia_out_of_core,
                    )
                store = self._catalog_store

            queried: dict[str, np.ndarray] | None = None
            for batch in store.query_cone(
                ra_deg,
                dec_deg,
                radius_deg,
                mag_limit,
                max_batch_rows=1_000_000,
                token=token,
            ):
                token.raise_if_cancelled()
                normalized = _normalize_tile_arrays(
                    {
                        "ra": batch.ra,
                        "dec": batch.dec,
                        "mag": batch.mag,
                        "bp_rp": batch.bp_rp,
                        "source_id": batch.source_id,
                    }
                )
                queried = (
                    normalized
                    if queried is None
                    else _merge_two_sorted_tiles(queried, normalized)
                )
            token.raise_if_cancelled()

            with self._lock:
                selected: dict[str, dict[str, np.ndarray]] = {}
                if self._base_tile_id in self._loaded_tiles:
                    selected[self._base_tile_id] = self._loaded_tiles[self._base_tile_id]
                if self._no_gaia_tile_id in self._loaded_tiles:
                    selected[self._no_gaia_tile_id] = self._loaded_tiles[
                        self._no_gaia_tile_id
                    ]
                if queried is not None and len(queried.get("mag", ())) > 0:
                    selected["__out_of_core_cone__"] = queried
                payload = _combine_tiles(
                    base_tile_id=self._base_tile_id,
                    loaded_tiles=selected,
                    internal_tile_ids={self._no_gaia_tile_id},
                )
                payload["query_region"] = (
                    ra_deg,
                    dec_deg,
                    radius_deg,
                    mag_limit,
                )
                token.raise_if_cancelled()
                self._active_dataset = payload
                self._active_tiles_signature = (
                    "cone",
                    f"{ra_deg:.6f}",
                    f"{dec_deg:.6f}",
                    f"{radius_deg:.6f}",
                    f"{mag_limit:.3f}",
                )
                self._last_scope_index_signature = None

            self.extension_ready.emit(dict(payload))
            self.build_scope_index("__out_of_core_cone__")
        except InterruptedError:
            return
        except Exception as exc:
            self.error_occurred.emit(f"Error consultant cataleg estel-lar: {exc}")

    def load_general_tile(self) -> None:
        """Inicia la carrega de la tesela general en background."""
        tile = self._manifest.get_general_tile()
        with self._lock:
            if tile.tile_id in self._loaded_tiles:
                return
            if tile.tile_id in self._tile_load_inflight:
                return
            self._tile_load_inflight.add(tile.tile_id)
            self._tile_priority_inflight.add(tile.tile_id)
        self._executor.submit(self._load_tile_worker, tile, True)

    def load_deep_tile(self, tile_id: str) -> None:
        """Carrega una tesela profunda concreta en segon pla."""
        normalized_id = str(tile_id or "").strip()
        if not normalized_id:
            self.error_occurred.emit("tile_id buit en load_deep_tile")
            return

        entry = next((item for item in self._manifest.deep_tiles if item.tile_id == normalized_id), None)
        if entry is None:
            self.error_occurred.emit(f"Tesela no trobada al manifest: {normalized_id}")
            return

        active_snapshot = None
        should_emit_active = False
        should_submit = False
        with self._lock:
            self._set_scope_request_locked(normalized_id)
            if normalized_id in self._loaded_tiles:
                self._activate_scope_focus_locked(normalized_id)
                self._compose_active_dataset_locked()
                active_snapshot = dict(self._active_dataset)
                should_emit_active = True
            elif normalized_id in self._tile_priority_inflight:
                should_submit = False
            else:
                self._tile_priority_inflight.add(normalized_id)
                self._tile_load_inflight.add(normalized_id)
                should_submit = True
        if should_emit_active:
            self.extension_ready.emit(active_snapshot)
        if not should_submit:
            return
        self._executor.submit(self._load_tile_worker, entry, False)

    def preload_adjacent_tiles(self, tile_id: str) -> None:
        """Carrega en segon pla les 8 teseles veines."""
        center_id = str(tile_id or "").strip()
        if not center_id:
            return
        active_snapshot = None
        with self._lock:
            self._set_scope_request_locked(center_id)
            if center_id in self._loaded_tiles:
                self._activate_scope_focus_locked(center_id)
                self._compose_active_dataset_locked()
                active_snapshot = dict(self._active_dataset)
        if active_snapshot is not None:
            self.extension_ready.emit(active_snapshot)
        for neighbor in self._manifest.get_adjacent_tiles(str(tile_id or "")):
            with self._lock:
                if neighbor.tile_id in self._loaded_tiles:
                    self._mark_tile_access_locked(neighbor.tile_id)
                    continue
                if neighbor.tile_id in self._tile_priority_inflight:
                    continue
                if neighbor.tile_id in self._tile_load_inflight:
                    continue
                self._tile_load_inflight.add(neighbor.tile_id)
            self._preload_executor.submit(self._load_tile_worker, neighbor, False)

    def request_scope_region(
        self,
        focus_tile_id: str,
        priority_tile_ids: Iterable[str] | None = None,
    ) -> None:
        """Demana la regio activa del scope prioritzant les teseles realment visibles."""
        normalized_focus_id = str(focus_tile_id or "").strip()
        if not normalized_focus_id:
            self.error_occurred.emit("focus_tile_id buit en request_scope_region")
            return

        focus_entry = next(
            (
                item
                for item in self._manifest.deep_tiles
                if item.tile_id == normalized_focus_id
            ),
            None,
        )
        if focus_entry is None:
            self.error_occurred.emit(
                f"Tesela no trobada al manifest: {normalized_focus_id}"
            )
            return

        requested_priority_ids = self._normalize_priority_tile_ids(
            priority_tile_ids
        )
        if normalized_focus_id not in requested_priority_ids:
            requested_priority_ids.add(normalized_focus_id)

        active_snapshot = None
        priority_entries: list[TileEntry] = []
        preload_entries: list[TileEntry] = []
        with self._lock:
            self._set_scope_request_locked(
                normalized_focus_id,
                priority_tile_ids=requested_priority_ids,
            )
            if normalized_focus_id in self._loaded_tiles:
                self._activate_scope_focus_locked(normalized_focus_id)
            self._compose_active_dataset_locked()
            active_snapshot = dict(self._active_dataset)
            priority_entries = self._claim_priority_entries_locked(
                requested_priority_ids
            )
            preload_entries = self._claim_preload_neighbors_locked(
                normalized_focus_id,
                exclude_tile_ids=requested_priority_ids,
            )

        if active_snapshot is not None and int(len(active_snapshot.get("ra", ()))) > 0:
            self.extension_ready.emit(active_snapshot)
        for entry in priority_entries:
            self._executor.submit(self._load_tile_worker, entry, False)
        for entry in preload_entries:
            self._preload_executor.submit(self._load_tile_worker, entry, False)
        self.build_scope_index(normalized_focus_id)

    def build_scope_index(self, tile_id: str) -> None:
        """Construeix index espacial de la tesela indicada o dataset actiu."""
        normalized_id = str(tile_id or "").strip()
        self._index_executor.submit(self._build_scope_index_worker, normalized_id)

    def get_loaded_tiles(self) -> set[str]:
        """Retorna el conjunt de teseles carregades en memoria."""
        with self._lock:
            return {
                tile_id
                for tile_id in self._loaded_tiles.keys()
                if str(tile_id) != str(self._no_gaia_tile_id)
            }

    def get_active_dataset(self) -> dict[str, Any]:
        """Retorna una copia lleugera del dataset actual actiu."""
        with self._lock:
            payload = dict(self._active_dataset)
        return payload

    def manifest(self) -> TileManifest:
        """Retorna el manifest ja carregat en memoria."""
        return self._manifest

    def _load_tile_worker(self, tile: TileEntry, is_general: bool) -> None:
        try:
            with self._lock:
                if tile.tile_id in self._loaded_tiles:
                    self._tile_load_inflight.discard(tile.tile_id)
                    self._tile_priority_inflight.discard(tile.tile_id)
                    active_snapshot = None
                    if (not is_general) and (
                        str(tile.tile_id) == str(self._scope_pending_focus_tile_id)
                    ):
                        self._activate_scope_focus_locked(tile.tile_id)
                        self._compose_active_dataset_locked()
                        active_snapshot = dict(self._active_dataset)
                else:
                    active_snapshot = None
            if is_general:
                if active_snapshot is not None:
                    self.general_tile_ready.emit(active_snapshot)
                return
            if active_snapshot is not None:
                self.deep_tile_ready.emit(tile.tile_id, self._loaded_tiles[tile.tile_id])
                self.extension_ready.emit(active_snapshot)
                self.build_scope_index(tile.tile_id)
                return

            arrays = _read_tile_npz(tile.file_path)
            arrays = _normalize_tile_arrays(arrays)
            if int(len(arrays["ra"])) <= 0:
                with self._lock:
                    self._tile_load_inflight.discard(tile.tile_id)
                    self._tile_priority_inflight.discard(tile.tile_id)
                return

            with self._lock:
                self._tile_load_inflight.discard(tile.tile_id)
                self._tile_priority_inflight.discard(tile.tile_id)
                self._loaded_tiles[tile.tile_id] = arrays
                self._tile_resident_bytes[tile.tile_id] = _payload_nbytes(arrays)
                self._mark_tile_access_locked(tile.tile_id)
                if is_general and self._no_gaia_supplement is not None:
                    was_already_present = (
                        self._no_gaia_tile_id in self._loaded_tiles
                    )
                    # El suplement no-Gaia s'injecta junt amb la tesela general
                    # (<8) i queda actiu per a posteriors extensions profundes.
                    self._loaded_tiles[self._no_gaia_tile_id] = dict(
                        self._no_gaia_supplement
                    )
                    self._tile_resident_bytes[self._no_gaia_tile_id] = (
                        _payload_nbytes(self._no_gaia_supplement)
                    )
                    if not was_already_present:
                        try:
                            print(
                                "[StarDataCoordinator] no-Gaia supplement "
                                "attached to general tile dataset"
                            )
                        except Exception:
                            pass
                if not is_general:
                    if str(tile.tile_id) == str(
                        self._scope_pending_focus_tile_id
                    ):
                        self._activate_scope_focus_locked(tile.tile_id)
                    elif not self._scope_focus_tile_id:
                        self._activate_scope_focus_locked(tile.tile_id)
                self._compose_active_dataset_locked()
                self._evict_deep_cache_locked()
                active_snapshot = dict(self._active_dataset)

            if is_general:
                self.general_tile_ready.emit(active_snapshot)
            else:
                self.deep_tile_ready.emit(tile.tile_id, arrays)
                self.extension_ready.emit(active_snapshot)
                self.build_scope_index(tile.tile_id)
        except Exception as exc:
            with self._lock:
                self._tile_load_inflight.discard(tile.tile_id)
                self._tile_priority_inflight.discard(tile.tile_id)
            self.error_occurred.emit(f"Error carregant tesela {tile.tile_id}: {exc}")

    def _build_scope_index_worker(self, tile_id: str) -> None:
        try:
            with self._lock:
                arrays = self._active_dataset
                ra = np.asarray(arrays.get("ra", np.empty(0, dtype=np.float32)), dtype=np.float32)
                dec = np.asarray(arrays.get("dec", np.empty(0, dtype=np.float32)), dtype=np.float32)
                mag = np.asarray(arrays.get("mag", np.empty(0, dtype=np.float32)), dtype=np.float32)
                loaded_tile_ids = arrays.get("loaded_tile_ids", frozenset())
                try:
                    loaded_ids_key = tuple(sorted(str(tid) for tid in loaded_tile_ids))
                except Exception:
                    loaded_ids_key = tuple()
                index_signature = (int(len(ra)), loaded_ids_key)
                if self._last_scope_index_signature == index_signature:
                    return

            if len(ra) <= 0 or len(dec) <= 0 or len(mag) <= 0:
                return

            sorted_indices, offsets = build_scope_spatial_index_payload(
                ra,
                dec,
                mag_all=mag,
            )
            try:
                loaded_max_mag = float(np.nanmax(mag))
            except Exception:
                loaded_max_mag = 0.0
            with self._lock:
                self._last_scope_index_signature = index_signature
            self.scope_index_ready.emit(
                {
                    "tile_id": tile_id,
                    "sorted_indices": sorted_indices,
                    "offsets": offsets,
                    "row_count": int(len(ra)),
                    "loaded_max_mag": float(loaded_max_mag),
                }
            )
        except Exception as exc:
            self.error_occurred.emit(f"Error construint index scope: {exc}")

    def _mark_tile_access_locked(self, tile_id: str) -> None:
        tile_id_n = str(tile_id or "").strip()
        if not tile_id_n:
            return
        self._tile_access_seq = int(self._tile_access_seq) + 1
        self._tile_last_access[tile_id_n] = int(self._tile_access_seq)

    def _scope_active_ids_for_tile_locked(self, tile_id: str) -> set[str]:
        center_id = str(tile_id or "").strip()
        if not center_id:
            return set()
        active_ids = {center_id}
        for neighbor in self._manifest.get_adjacent_tiles(center_id):
            active_ids.add(str(neighbor.tile_id))
        return active_ids

    def _set_scope_request_locked(
        self,
        tile_id: str,
        priority_tile_ids: Iterable[str] | None = None,
    ) -> None:
        center_id = str(tile_id or "").strip()
        if not center_id:
            return
        self._scope_pending_focus_tile_id = center_id
        self._scope_pending_active_tile_ids = self._scope_active_ids_for_tile_locked(
            center_id
        )
        requested_priority_ids = self._normalize_priority_tile_ids(
            priority_tile_ids
        )
        if center_id not in requested_priority_ids:
            requested_priority_ids.add(center_id)
        self._scope_pending_priority_tile_ids = requested_priority_ids
        self._mark_tile_access_locked(center_id)

    def _activate_scope_focus_locked(self, tile_id: str) -> None:
        center_id = str(tile_id or "").strip()
        if not center_id:
            return
        self._scope_focus_tile_id = center_id
        self._scope_active_tile_ids = self._scope_active_ids_for_tile_locked(
            center_id
        )
        if str(self._scope_pending_focus_tile_id) == center_id:
            self._scope_pending_focus_tile_id = ""
            self._scope_pending_active_tile_ids = set()
            self._scope_pending_priority_tile_ids = set()
        self._mark_tile_access_locked(center_id)

    def _compose_active_dataset_locked(self) -> None:
        selected_tiles: dict[str, dict[str, np.ndarray]] = {}
        if self._base_tile_id in self._loaded_tiles:
            selected_tiles[self._base_tile_id] = self._loaded_tiles[self._base_tile_id]
        if self._no_gaia_tile_id in self._loaded_tiles:
            selected_tiles[self._no_gaia_tile_id] = self._loaded_tiles[self._no_gaia_tile_id]
        composed_scope_tile_ids = set(self._scope_active_tile_ids)
        composed_scope_tile_ids.update(
            tile_id
            for tile_id in self._scope_pending_active_tile_ids
            if tile_id in self._loaded_tiles
        )
        for tile_id in sorted(composed_scope_tile_ids):
            if tile_id in self._loaded_tiles:
                selected_tiles[tile_id] = self._loaded_tiles[tile_id]

        signature = tuple(sorted(str(tid) for tid in selected_tiles.keys()))
        if self._active_tiles_signature == signature:
            return

        if not selected_tiles:
            self._active_dataset = _empty_dataset()
        else:
            self._active_dataset = _combine_tiles(
                base_tile_id=self._base_tile_id,
                loaded_tiles=selected_tiles,
                internal_tile_ids={self._no_gaia_tile_id},
            )
        self._active_tiles_signature = signature
        self._last_scope_index_signature = None

    def _evict_deep_cache_locked(self) -> None:
        deep_tile_ids = [
            tile_id
            for tile_id in self._loaded_tiles.keys()
            if tile_id not in {self._base_tile_id, self._no_gaia_tile_id}
        ]
        resident = sum(
            int(self._tile_resident_bytes.get(tile_id, 0))
            for tile_id in self._loaded_tiles
        )
        if resident <= self._max_cached_deep_bytes:
            return

        protected_ids = set(self._scope_active_tile_ids)
        protected_ids.update(self._scope_pending_active_tile_ids)
        candidates = [
            tile_id
            for tile_id in deep_tile_ids
            if tile_id not in protected_ids
        ]
        candidates.sort(
            key=lambda tid: int(self._tile_last_access.get(tid, 0))
        )
        for tile_id in candidates:
            if resident <= self._max_cached_deep_bytes:
                break
            self._loaded_tiles.pop(tile_id, None)
            self._tile_last_access.pop(tile_id, None)
            resident -= int(self._tile_resident_bytes.pop(tile_id, 0))

    def _normalize_priority_tile_ids(
        self, priority_tile_ids: Iterable[str] | None
    ) -> set[str]:
        normalized_ids: set[str] = set()
        if priority_tile_ids is None:
            return normalized_ids
        for raw_tile_id in priority_tile_ids:
            tile_id = str(raw_tile_id or "").strip()
            if tile_id and tile_id in self._manifest._tiles_by_id:
                normalized_ids.add(tile_id)
        return normalized_ids

    def _claim_priority_entries_locked(
        self, priority_tile_ids: set[str]
    ) -> list[TileEntry]:
        entries: list[TileEntry] = []
        for tile_id in sorted(priority_tile_ids):
            if tile_id in self._loaded_tiles:
                self._mark_tile_access_locked(tile_id)
                continue
            if tile_id in self._tile_priority_inflight:
                continue
            entry = self._manifest._tiles_by_id.get(tile_id)
            if entry is None:
                continue
            self._tile_priority_inflight.add(tile_id)
            self._tile_load_inflight.add(tile_id)
            entries.append(entry)
        return entries

    def _claim_preload_neighbors_locked(
        self,
        center_tile_id: str,
        *,
        exclude_tile_ids: set[str] | None = None,
    ) -> list[TileEntry]:
        entries: list[TileEntry] = []
        excluded = {str(tile_id) for tile_id in (exclude_tile_ids or set())}
        for neighbor in self._manifest.get_adjacent_tiles(center_tile_id):
            neighbor_id = str(neighbor.tile_id)
            if neighbor_id in excluded:
                continue
            if neighbor_id in self._loaded_tiles:
                self._mark_tile_access_locked(neighbor_id)
                continue
            if neighbor_id in self._tile_priority_inflight:
                continue
            if neighbor_id in self._tile_load_inflight:
                continue
            self._tile_load_inflight.add(neighbor_id)
            entries.append(neighbor)
        return entries


def _payload_nbytes(payload: dict[str, Any]) -> int:
    return int(
        sum(
            np.asarray(value).nbytes
            for value in payload.values()
            if isinstance(value, np.ndarray)
        )
    )


def _empty_dataset() -> dict[str, Any]:
    """Crea l'estructura minima de dataset actiu."""
    return {
        "ra": np.empty(0, dtype=np.float32),
        "dec": np.empty(0, dtype=np.float32),
        "mag": np.empty(0, dtype=np.float32),
        "r": np.empty(0, dtype=np.float32),
        "g": np.empty(0, dtype=np.float32),
        "b": np.empty(0, dtype=np.float32),
        "bp_rp": np.empty(0, dtype=np.float32),
        "source_id": np.empty(0, dtype=np.int64),
        "loaded_tile_ids": frozenset(),
    }


def _read_tile_npz(path: Path) -> dict[str, np.ndarray]:
    """Llegeix una tesela .npz i retorna arrays en memoria."""
    tile_path = Path(path).expanduser().resolve()
    with np.load(tile_path, allow_pickle=False) as data:
        payload = {
            "ra": np.asarray(data.get("ra", data.get("RA", np.empty(0, dtype=np.float32))), dtype=np.float32),
            "dec": np.asarray(data.get("dec", data.get("DEC", np.empty(0, dtype=np.float32))), dtype=np.float32),
            "mag": np.asarray(data.get("phot_g_mean_mag", data.get("mag", np.empty(0, dtype=np.float32))), dtype=np.float32),
            "bp_rp": np.asarray(data.get("bp_rp", np.full(int(len(data.get("ra", []))), 0.8, dtype=np.float32)), dtype=np.float32),
            "source_id": np.asarray(
                data.get("source_id", np.full(int(len(data.get("ra", []))), -1, dtype=np.int64)),
                dtype=np.int64,
            ),
        }
    return payload


def _normalize_tile_arrays(payload: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Normalitza arrays de tesela i calcula colors RGB."""
    ra = np.asarray(payload.get("ra", np.empty(0, dtype=np.float32)), dtype=np.float32)
    dec = np.asarray(payload.get("dec", np.empty(0, dtype=np.float32)), dtype=np.float32)
    mag = np.asarray(payload.get("mag", np.empty(0, dtype=np.float32)), dtype=np.float32)
    bp_rp = np.asarray(payload.get("bp_rp", np.full(len(mag), 0.8, dtype=np.float32)), dtype=np.float32)
    source_id = np.asarray(
        payload.get("source_id", np.full(len(mag), -1, dtype=np.int64)),
        dtype=np.int64,
    )

    row_count = min(len(ra), len(dec), len(mag), len(bp_rp), len(source_id))
    if row_count <= 0:
        return _empty_dataset()

    ra = ra[:row_count]
    dec = dec[:row_count]
    mag = mag[:row_count]
    bp_rp = bp_rp[:row_count]
    source_id = source_id[:row_count]

    if mag.size > 1 and bool(np.any(mag[1:] < mag[:-1])):
        order = np.argsort(mag, kind="mergesort")
        ra = np.asarray(ra[order], dtype=np.float32)
        dec = np.asarray(dec[order], dtype=np.float32)
        mag = np.asarray(mag[order], dtype=np.float32)
        bp_rp = np.asarray(bp_rp[order], dtype=np.float32)
        source_id = np.asarray(source_id[order], dtype=np.int64)
    else:
        ra = np.asarray(ra, dtype=np.float32)
        dec = np.asarray(dec, dtype=np.float32)
        mag = np.asarray(mag, dtype=np.float32)
        bp_rp = np.asarray(bp_rp, dtype=np.float32)
        source_id = np.asarray(source_id, dtype=np.int64)

    r, g, b = _bp_rp_to_rgb_arrays(bp_rp)

    return {
        "ra": ra,
        "dec": dec,
        "mag": mag,
        "bp_rp": bp_rp,
        "r": np.asarray(r, dtype=np.float32),
        "g": np.asarray(g, dtype=np.float32),
        "b": np.asarray(b, dtype=np.float32),
        "source_id": source_id,
    }


def _merge_two_sorted_tiles(
    left: dict[str, np.ndarray], right: dict[str, np.ndarray]
) -> dict[str, np.ndarray]:
    """Linear vector merge by magnitude; avoids a global argsort per update."""

    left_mag = np.asarray(left.get("mag", ()), dtype=np.float32)
    right_mag = np.asarray(right.get("mag", ()), dtype=np.float32)
    if left_mag.size == 0:
        return {key: np.asarray(value) for key, value in right.items()}
    if right_mag.size == 0:
        return {key: np.asarray(value) for key, value in left.items()}
    right_positions = (
        np.searchsorted(left_mag, right_mag, side="right")
        + np.arange(right_mag.size, dtype=np.int64)
    )
    is_right = np.zeros(left_mag.size + right_mag.size, dtype=bool)
    is_right[right_positions] = True
    left_positions = np.flatnonzero(~is_right)
    result: dict[str, np.ndarray] = {}
    for key in ("ra", "dec", "mag", "bp_rp", "r", "g", "b", "source_id"):
        dtype = np.int64 if key == "source_id" else np.float32
        left_values = np.asarray(left.get(key, ()), dtype=dtype)
        right_values = np.asarray(right.get(key, ()), dtype=dtype)
        merged = np.empty(left_mag.size + right_mag.size, dtype=dtype)
        merged[left_positions] = left_values
        merged[right_positions] = right_values
        result[key] = merged
    return result


def _combine_tiles(
    *,
    base_tile_id: str,
    loaded_tiles: dict[str, dict[str, np.ndarray]],
    internal_tile_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Combina tesela base i profundes en un dataset actiu ordenat per magnitud."""
    if not loaded_tiles:
        return _empty_dataset()

    internal_ids = {str(tile_id) for tile_id in (internal_tile_ids or set())}
    ordered_ids: list[str] = []
    if base_tile_id in loaded_tiles:
        ordered_ids.append(base_tile_id)
    ordered_ids.extend(
        sorted(
            tile_id
            for tile_id in loaded_tiles.keys()
            if tile_id != base_tile_id
        )
    )

    result: dict[str, Any] | None = None
    for tile_id in ordered_ids:
        tile = loaded_tiles.get(tile_id)
        if not tile:
            continue
        normalized: dict[str, np.ndarray] = {}
        row_count = int(len(np.asarray(tile.get("mag", ()))) )
        for key in ("ra", "dec", "mag", "bp_rp", "r", "g", "b"):
            normalized[key] = np.asarray(
                tile.get(key, np.empty(row_count, dtype=np.float32)),
                dtype=np.float32,
            )
        normalized["source_id"] = np.asarray(
            tile.get("source_id", np.full(row_count, -1, dtype=np.int64)),
            dtype=np.int64,
        )
        result = normalized if result is None else _merge_two_sorted_tiles(result, normalized)

    if result is None:
        result = _empty_dataset()
    elif len(result["mag"]) > 0:
        _deduplicate_positive_source_ids_in_place(result)

    result["loaded_tile_ids"] = frozenset(
        tile_id
        for tile_id in ordered_ids
        if str(tile_id) not in internal_ids
    )
    return result


def _deduplicate_positive_source_ids_in_place(
    payload: dict[str, Any]
) -> None:
    """Elimina duplicats de `source_id` positius mantenint el primer (més brillant)."""
    source_id = np.asarray(
        payload.get("source_id", np.empty(0, dtype=np.int64)), dtype=np.int64
    )
    if source_id.size <= 0:
        return

    keep_mask = np.ones(source_id.shape[0], dtype=bool)
    positive_positions = np.flatnonzero(source_id > 0)
    if positive_positions.size:
        _unique, first = np.unique(
            source_id[positive_positions], return_index=True
        )
        positive_keep = np.zeros(positive_positions.size, dtype=bool)
        positive_keep[first] = True
        keep_mask[positive_positions] = positive_keep

    if bool(np.all(keep_mask)):
        return

    for key_name in ("ra", "dec", "mag", "bp_rp", "r", "g", "b", "source_id"):
        current_values = np.asarray(payload.get(key_name, np.empty(0)))
        if current_values.size == keep_mask.size:
            payload[key_name] = current_values[keep_mask]


def _load_no_gaia_supplement_arrays(
    catalog_dir: Path,
) -> dict[str, np.ndarray] | None:
    """Carrega i normalitza el suplement `no_gaia_stars.json` per unir-lo a Gaia."""
    try:
        raw_payload = _load_no_gaia_star_arrays(str(catalog_dir))
    except Exception:
        return None
    if not isinstance(raw_payload, dict):
        return None

    supplement_ra = np.asarray(
        raw_payload.get("ra", np.empty(0, dtype=np.float32)), dtype=np.float32
    )
    supplement_dec = np.asarray(
        raw_payload.get("dec", np.empty(0, dtype=np.float32)), dtype=np.float32
    )
    supplement_mag = np.asarray(
        raw_payload.get("mag", np.empty(0, dtype=np.float32)), dtype=np.float32
    )
    supplement_bp_rp = np.asarray(
        raw_payload.get(
            "bp_rp",
            np.full(int(len(supplement_mag)), 0.8, dtype=np.float32),
        ),
        dtype=np.float32,
    )
    supplement_source_id = np.asarray(
        raw_payload.get(
            "source_id",
            np.full(int(len(supplement_mag)), -1, dtype=np.int64),
        ),
        dtype=np.int64,
    )

    row_count = min(
        len(supplement_ra),
        len(supplement_dec),
        len(supplement_mag),
        len(supplement_bp_rp),
        len(supplement_source_id),
    )
    if row_count <= 0:
        return None

    supplement_ra = supplement_ra[:row_count]
    supplement_dec = supplement_dec[:row_count]
    supplement_mag = supplement_mag[:row_count]
    supplement_bp_rp = supplement_bp_rp[:row_count]
    supplement_source_id = supplement_source_id[:row_count]

    valid_rows_mask = (
        np.isfinite(supplement_ra)
        & np.isfinite(supplement_dec)
        & np.isfinite(supplement_mag)
    )
    if not bool(np.any(valid_rows_mask)):
        return None

    supplement_ra = supplement_ra[valid_rows_mask]
    supplement_dec = supplement_dec[valid_rows_mask]
    supplement_mag = supplement_mag[valid_rows_mask]
    supplement_bp_rp = supplement_bp_rp[valid_rows_mask]
    supplement_source_id = supplement_source_id[valid_rows_mask]

    sort_order = np.argsort(supplement_mag, kind="mergesort")
    supplement_ra = np.asarray(supplement_ra[sort_order], dtype=np.float32)
    supplement_dec = np.asarray(supplement_dec[sort_order], dtype=np.float32)
    supplement_mag = np.asarray(supplement_mag[sort_order], dtype=np.float32)
    supplement_bp_rp = np.asarray(
        supplement_bp_rp[sort_order], dtype=np.float32
    )
    supplement_source_id = np.asarray(
        supplement_source_id[sort_order], dtype=np.int64
    )
    supplement_r, supplement_g, supplement_b = _bp_rp_to_rgb_arrays(
        supplement_bp_rp
    )

    normalized_payload: dict[str, Any] = {
        "ra": supplement_ra,
        "dec": supplement_dec,
        "mag": supplement_mag,
        "bp_rp": supplement_bp_rp,
        "r": np.asarray(supplement_r, dtype=np.float32),
        "g": np.asarray(supplement_g, dtype=np.float32),
        "b": np.asarray(supplement_b, dtype=np.float32),
        "source_id": supplement_source_id,
    }
    _deduplicate_positive_source_ids_in_place(normalized_payload)
    return {
        "ra": np.asarray(normalized_payload["ra"], dtype=np.float32),
        "dec": np.asarray(normalized_payload["dec"], dtype=np.float32),
        "mag": np.asarray(normalized_payload["mag"], dtype=np.float32),
        "bp_rp": np.asarray(normalized_payload["bp_rp"], dtype=np.float32),
        "r": np.asarray(normalized_payload["r"], dtype=np.float32),
        "g": np.asarray(normalized_payload["g"], dtype=np.float32),
        "b": np.asarray(normalized_payload["b"], dtype=np.float32),
        "source_id": np.asarray(
            normalized_payload["source_id"], dtype=np.int64
        ),
    }
