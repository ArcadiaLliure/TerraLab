"""Scientific compute process entry point."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
import json
import math
import unicodedata
from datetime import datetime, timedelta
from dataclasses import asdict, is_dataclass
from pathlib import Path

from TerraLab.runtime.protocol import (
    ARTIFACT_READY,
    COMPUTE_REQUEST,
    HEARTBEAT,
    PROGRESS,
    SHUTDOWN,
    WORKER_ERROR,
    WORKER_READY,
    envelope,
)
from TerraLab.runtime.service_io import (
    read_messages,
    reserve_stdout_for_protocol,
    write_message,
)

_CATALOG_LOCK = threading.Lock()


class _TerrainService:
    """Run one cancellable terrain job inside the isolated Compute PID."""

    def __init__(self, tiles_dir: str) -> None:
        self._tiles_dir = str(tiles_dir)
        self._thread: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._pending: tuple[dict, str, int] | None = None
        self._closing = False

    def start(
        self,
        job: dict,
        *,
        request_id: str,
        generation: int,
    ) -> None:
        request = (dict(job), str(request_id), int(generation))
        with self._lock:
            if self._closing:
                return
            if self._thread is not None and self._thread.is_alive():
                self._pending = request
                self._cancel_event.set()
                return
            self._launch_locked(request)

    def _launch_locked(
        self, request: tuple[dict, str, int]
    ) -> None:
        self._cancel_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(*request, self._cancel_event),
            name="terralab-terrain-dispatch",
            daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        with self._lock:
            self._pending = None
            self._cancel_event.set()

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            self._pending = None
            self._cancel_event.set()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def _run(
        self,
        job: dict,
        request_id: str,
        generation: int,
        cancel_event: threading.Event,
    ) -> None:
        from TerraLab.terrain import bake_process

        error_published = False
        temporary_dir = tempfile.mkdtemp(
            prefix=f"tl_horizon_{job.get('job_id', 'terrain')}_"
        )
        try:
            tiles_dir = str(job.get("tiles_dir") or self._tiles_dir)
            if not tiles_dir or not os.path.exists(tiles_dir):
                raise FileNotFoundError(
                    f"Tiles directory not configured or found: {tiles_dir}"
                )
            job = dict(job)
            job.setdefault("job_id", "terrain")
            job.setdefault("observer_offset", 0.0)
            job.setdefault("bands", 20)
            job["tiles_dir"] = tiles_dir
            output_path = os.path.join(
                temporary_dir, "profile_final.npz"
            )
            preview_path = os.path.join(
                temporary_dir, "profile_preview.npz"
            )
            arguments = bake_process.build_cli_arguments(
                job,
                output_path,
                preview_path,
            )

            def publish(event: dict) -> None:
                nonlocal error_published
                error_published = (
                    error_published
                    or str(event.get("type", "")) == "error"
                )
                self._on_event(
                    event,
                    request_id=request_id,
                    generation=generation,
                    cancelled=cancel_event.is_set(),
                )

            bake_process.main(
                arguments,
                event_callback=publish,
                abort_check=cancel_event.is_set,
            )
        except InterruptedError:
            return
        except Exception as exc:
            if not cancel_event.is_set() and not error_published:
                self._send_error(
                    str(exc),
                    request_id=request_id,
                    generation=generation,
                )
        finally:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                pending, self._pending = self._pending, None
                if (
                    pending is not None
                    and not self._closing
                ):
                    self._launch_locked(pending)

    def _artifact_path(self, job_id: str, *, preview: bool) -> Path:
        from TerraLab.common.app_paths import cache_dir

        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_id)[:96]
        suffix = "_preview" if preview else ""
        return cache_dir("terrain", "profiles") / (
            f"horizon_{safe_id}{suffix}.npz"
        )

    def _publish_profile(
        self,
        event: dict,
        *,
        preview: bool,
        request_id: str,
        generation: int,
    ) -> None:
        source_key = "snapshot_path" if preview else "profile_path"
        source = Path(str(event.get(source_key, "") or ""))
        if not source.is_file():
            self._send_error(
                "Terrain bake published a missing profile",
                request_id=request_id,
                generation=generation,
            )
            return
        job_id = str(event.get("job_id", "") or "terrain")
        target = self._artifact_path(job_id, preview=preview)
        temporary = target.with_suffix(".tmp.npz")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
        kind = "terrain_preview" if preview else "terrain_bake"
        value = {
            "job_id": job_id,
            "profile_path": str(target),
            "preview": bool(preview),
            "current": event.get("current"),
            "total": event.get("total"),
        }
        write_message(
            envelope(
                ARTIFACT_READY,
                {"operation": kind, "value": value},
                request_id=request_id,
                generation=generation,
            )
        )

    def _on_event(
        self,
        event: dict,
        *,
        request_id: str,
        generation: int,
        cancelled: bool,
    ) -> None:
        if cancelled:
            return
        event_type = str(event.get("type", ""))
        if event_type == "progress":
            state = {
                key: value
                for key, value in event.items()
                if key != "type"
            }
            write_message(
                envelope(
                    PROGRESS,
                    {"operation": "terrain_bake", "value": state},
                    request_id=request_id,
                    generation=generation,
                )
            )
        elif event_type == "preview":
            self._publish_profile(
                event,
                preview=True,
                request_id=request_id,
                generation=generation,
            )
        elif event_type == "done":
            self._publish_profile(
                event,
                preview=False,
                request_id=request_id,
                generation=generation,
            )
        elif event_type == "error":
            self._send_error(
                str(event.get("message", "Terrain error")),
                request_id=request_id,
                generation=generation,
            )

    @staticmethod
    def _send_error(
        message: str, *, request_id: str, generation: int
    ) -> None:
        write_message(
            envelope(
                WORKER_ERROR,
                {"operation": "terrain_bake", "message": str(message)},
                request_id=request_id,
                generation=generation,
            )
        )


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe(item())
        except (TypeError, ValueError):
            pass
    return str(value)


class _AssetService:
    """Run cancellable asset preparation outside the UI process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, threading.Event] = {}
        self._capacity = threading.BoundedSemaphore(1)
        self._closing = False

    def start(
        self,
        payload: dict,
        *,
        request_id: str,
        generation: int,
    ) -> None:
        job_id = str(payload.get("job_id") or request_id)
        with self._lock:
            if self._closing or job_id in self._jobs:
                return
            cancelled = threading.Event()
            self._jobs[job_id] = cancelled
        threading.Thread(
            target=self._run,
            args=(
                job_id,
                dict(payload),
                request_id,
                generation,
                cancelled,
            ),
            name=f"terralab-asset-{job_id[:24]}",
            daemon=True,
        ).start()

    def cancel(self, job_id: str) -> None:
        with self._lock:
            cancelled = self._jobs.get(str(job_id))
        if cancelled is not None:
            cancelled.set()

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            events = tuple(self._jobs.values())
        for event in events:
            event.set()

    def _run(
        self,
        job_id: str,
        payload: dict,
        request_id: str,
        generation: int,
        cancelled: threading.Event,
    ) -> None:
        try:
            with self._capacity:
                from TerraLab.common.data_library import DataLibrary
                from TerraLab.data.assets_manager import AssetManager

                manager = AssetManager(
                    DataLibrary(str(payload["library_root"]))
                )

                def progress(percent: float, message: str) -> None:
                    if cancelled.is_set():
                        return
                    write_message(
                        envelope(
                            PROGRESS,
                            {
                                "operation": "asset_job",
                                "value": {
                                    "percent": float(percent),
                                    "message": str(message),
                                },
                            },
                            request_id=request_id,
                            generation=generation,
                        )
                    )

                if str(payload.get("mode", "")) == "download":
                    result = manager.download_and_prepare(
                        str(payload["asset_id"]),
                        progress_callback=progress,
                        options=dict(payload.get("options", {})),
                        cancelled=cancelled.is_set,
                    )
                else:
                    result = manager.import_files(
                        str(payload["asset_id"]),
                        tuple(str(v) for v in payload.get("files", ())),
                        progress_callback=progress,
                        options=dict(payload.get("options", {})),
                        cancelled=cancelled.is_set,
                    )
            if cancelled.is_set():
                raise InterruptedError("Asset operation cancelled")
            write_message(
                envelope(
                    ARTIFACT_READY,
                    {
                        "operation": "asset_job",
                        "value": _json_safe(result),
                    },
                    request_id=request_id,
                    generation=generation,
                )
            )
        except Exception as exc:
            write_message(
                envelope(
                    WORKER_ERROR,
                    {
                        "operation": "asset_job",
                        "message": str(exc),
                    },
                    request_id=request_id,
                    generation=generation,
                )
            )
        finally:
            with self._lock:
                self._jobs.pop(job_id, None)


class _LatestComputeTasks:
    """Run bounded jobs with one latest request per independent stream."""

    def __init__(self, ephemeris) -> None:
        self._ephemeris = ephemeris
        self._lock = threading.Lock()
        self._slots: dict[tuple[str, str], dict] = {}
        self._closing = False
        self._capacity = threading.BoundedSemaphore(
            max(1, min(2, int(os.cpu_count() or 1) - 2))
        )

    def start(
        self,
        operation: str,
        payload: dict,
        *,
        request_id: str,
        generation: int,
    ) -> None:
        request = (
            dict(payload),
            str(request_id),
            int(generation),
        )
        slot_key = (str(operation), str(request_id))
        with self._lock:
            if self._closing:
                return
            slot = self._slots.setdefault(
                slot_key,
                {"running": False, "pending": None},
            )
            if bool(slot["running"]):
                slot["pending"] = request
                return
            slot["running"] = True
            self._launch(str(operation), slot_key, request)

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            for slot in self._slots.values():
                slot["pending"] = None

    def _launch(
        self,
        operation: str,
        slot_key: tuple[str, str],
        request: tuple[dict, str, int],
    ) -> None:
        threading.Thread(
            target=self._run,
            args=(operation, slot_key, *request),
            name=f"terralab-compute-{operation}",
            daemon=True,
        ).start()

    def _run(
        self,
        operation: str,
        slot_key: tuple[str, str],
        payload: dict,
        request_id: str,
        generation: int,
    ) -> None:
        result = None
        error: Exception | None = None
        with self._capacity:
            try:
                if operation == "ephemeris":
                    result = _compute_ephemeris(
                        payload, self._ephemeris
                    )
                elif operation == "catalog_general":
                    with _CATALOG_LOCK:
                        result = _prepare_catalog(payload)
                elif operation == "catalog_scope":
                    with _CATALOG_LOCK:
                        result = _prepare_catalog_scope(payload)
                elif operation == "sleep":
                    seconds = max(
                        0.0, float(payload.get("seconds", 0))
                    )
                    time.sleep(seconds)
                    result = {"slept": seconds}
                elif operation == "copernicus_probe":
                    from TerraLab.data.copernicus import (
                        ArcGISImageServerClient,
                        DownloadRequest,
                    )

                    request = DownloadRequest.from_dict(
                        dict(payload["request"])
                    )
                    estimate = ArcGISImageServerClient(
                        timeout=(8.0, 30.0),
                        max_retries=1,
                    ).estimate_nodata_fraction(
                        request,
                        sample_size=128,
                    )
                    result = _json_safe(estimate)
                elif operation == "search_index":
                    result = _build_search_records(payload)
                elif operation == "search_resolve":
                    result = _resolve_search_record(
                        payload, self._ephemeris
                    )
                elif operation == "weather_sample":
                    result = _prepare_weather_sample(payload)
                elif operation == "coordinate_convert":
                    result = _convert_coordinates(payload)
                elif operation == "circumpolar_align":
                    result = _circumpolar_alignment(payload)
                elif operation == "terrain_surface":
                    def publish_surface_progress(
                        percent: float, phase: str
                    ) -> None:
                        write_message(
                            envelope(
                                PROGRESS,
                                {
                                    "operation": operation,
                                    "value": {
                                        "kind": "surface",
                                        "phase": str(phase),
                                        "percent": float(percent),
                                    },
                                },
                                request_id=request_id,
                                generation=generation,
                            )
                        )

                    result = _prepare_surface_artifact(
                        payload,
                        progress_callback=publish_surface_progress,
                    )
                elif operation.startswith("source_inspection:"):
                    from TerraLab.data.source_catalog import DataSource
                    from TerraLab.terrain.source_inspection import (
                        inspect_registered_source,
                    )

                    source = DataSource.from_dict(
                        dict(payload["source"])
                    )
                    result = inspect_registered_source(
                        source
                    ).registry_fields()
                else:
                    raise ValueError(
                        f"Unknown compute operation: {operation}"
                    )
            except Exception as exc:
                error = exc

        with self._lock:
            slot = self._slots[slot_key]
            pending, slot["pending"] = slot["pending"], None
            obsolete = pending is not None
            closing = self._closing
            if pending is None:
                slot["running"] = False
            else:
                self._launch(operation, slot_key, pending)
        if closing or obsolete:
            return
        if error is not None:
            write_message(
                envelope(
                    WORKER_ERROR,
                    {
                        "operation": operation,
                        "message": str(error),
                    },
                    request_id=request_id,
                    generation=generation,
                )
            )
            return
        write_message(
            envelope(
                ARTIFACT_READY,
                {"operation": operation, "value": result},
                request_id=request_id,
                generation=generation,
            )
        )


def _limit_background_parallelism() -> None:
    available = max(1, int(os.cpu_count() or 1) - 2)
    value = str(available)
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ.setdefault(name, value)


def _lower_process_priority() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(),
            0x00004000,  # BELOW_NORMAL_PRIORITY_CLASS
        )
    except (AttributeError, OSError):
        return


def _compute_ephemeris(payload, coordinator):
    return coordinator._compute_snapshot(
        year_utc=int(payload["year_utc"]),
        day_of_year_utc=int(payload["day_of_year_utc"]),
        ut_hour=float(payload["ut_hour"]),
        latitude=float(payload["latitude"]),
        longitude=float(payload["longitude"]),
    )


def _normalize_search_key(value: object) -> str:
    lowered = str(value or "").strip().lower()
    folded = unicodedata.normalize("NFKD", lowered)
    return "".join(
        character
        for character in folded
        if not unicodedata.combining(character)
    )


def _build_search_records(payload: dict) -> dict:
    """Prepare searchable celestial records entirely inside Compute."""

    from TerraLab.astro.ngc_catalog import iter_ngc_aliases
    from TerraLab.astro.search_engine import (
        load_named_star_entries,
        load_ngc_entries,
    )

    planet_aliases = {
        "sun": ("Sol", "Sun", "Soleil", "Sole"),
        "moon": ("Lluna", "Luna", "Moon", "Lune"),
        "mercury": ("Mercuri", "Mercurio", "Mercury", "Mercure"),
        "venus": ("Venus",),
        "mars": ("Mart", "Marte", "Mars"),
        "jupiter": ("Jupiter", "Júpiter"),
        "saturn": ("Saturn", "Saturno"),
        "uranus": ("Urà", "Ura", "Urano", "Uranus"),
        "neptune": ("Neptú", "Neptu", "Neptuno", "Neptune"),
        "pluto": ("Plutó", "Pluto", "Plutón", "Pluton"),
    }
    records: list[dict] = [
        {
            "kind": "planet",
            "key": key,
            "name": aliases[0],
            "aliases": list(aliases),
        }
        for key, aliases in planet_aliases.items()
    ]
    named_path = str(payload.get("named_stars_path", "") or "")
    for star in load_named_star_entries(named_path or None):
        name = str(star.get("name", "") or "").strip()
        if not name:
            continue
        aliases = [name]
        source_id = str(star.get("source_id", "") or "").strip()
        if source_id and source_id not in {"-1", "0"}:
            aliases.extend(
                (f"Gaia DR3 {source_id}", f"Gaia {source_id}")
            )
        records.append(
            {
                "kind": "star",
                "name": name,
                "aliases": aliases,
                "ra": float(star["ra"]),
                "dec": float(star["dec"]),
                "source_id": source_id,
            }
        )

    gaia_path = Path(
        str(payload.get("gaia_catalog_path", "") or "")
    )
    gaia_limit = max(
        0, min(20_000, int(payload.get("gaia_suggestion_limit", 5000)))
    )
    if gaia_limit and gaia_path.is_file():
        import numpy as np

        gaia_catalog = np.load(
            gaia_path, mmap_mode="r", allow_pickle=False
        )
        try:
            names = tuple(
                getattr(
                    getattr(gaia_catalog, "dtype", None), "names", ()
                )
                or ()
            )
            required = {"source_id", "ra", "dec"}
            if required.issubset(names):
                count = min(len(gaia_catalog), gaia_limit)
                for row in gaia_catalog[:count]:
                    source_id = int(row["source_id"])
                    if source_id <= 0:
                        continue
                    records.append(
                        {
                            "kind": "star",
                            "name": f"Gaia DR3 {source_id}",
                            "aliases": [
                                f"Gaia DR3 {source_id}",
                                f"Gaia {source_id}",
                            ],
                            "ra": float(row["ra"]),
                            "dec": float(row["dec"]),
                            "source_id": str(source_id),
                        }
                    )
        finally:
            close = getattr(gaia_catalog, "close", None)
            if callable(close):
                close()
            mmap = getattr(gaia_catalog, "_mmap", None)
            if mmap is not None:
                mmap.close()

    candidates = [
        str(value or "")
        for value in payload.get("ngc_paths", ()) or ()
    ]
    candidates.append(str(payload.get("ngc_path", "") or ""))
    ngc_path = next(
        (value for value in candidates if value and Path(value).is_file()),
        "",
    )
    ngc_objects = load_ngc_entries(ngc_path)
    for obj in ngc_objects:
        aliases = [
            str(value).strip()
            for value in iter_ngc_aliases(obj)
            if str(value).strip()
        ]
        if not aliases:
            continue
        records.append(
            {
                "kind": "ngc",
                "name": str(
                    getattr(obj, "common_name", None)
                    or getattr(obj, "name", aliases[0])
                ),
                "aliases": aliases,
                "ra": float(obj.ra_deg),
                "dec": float(obj.dec_deg),
            }
        )

    # One canonical record wins each normalized alias. This bounds the JSONL
    # response and makes lookup deterministic in the UI.
    claimed: set[str] = set()
    compact: list[dict] = []
    for record in records:
        aliases = []
        for alias in record.get("aliases", ()):
            key = _normalize_search_key(alias)
            if key and key not in claimed:
                claimed.add(key)
                aliases.append(str(alias))
        if aliases:
            item = dict(record)
            item["aliases"] = aliases
            compact.append(item)
    return {
        "records": compact,
        "aliases": len(claimed),
        "ngc_artifact": _publish_ngc_artifact(
            ngc_path, ngc_objects
        ),
    }


def _publish_ngc_artifact(path: str, objects: list) -> dict:
    """Publish a read-only structured array for the Render process."""

    if not path or not objects:
        return {"catalog_path": "", "revision": ""}
    import hashlib

    import numpy as np

    from TerraLab.astro.ngc_catalog import ngc_display_label
    from TerraLab.common.app_paths import cache_dir

    source = Path(path)
    stat = source.stat()
    revision = hashlib.blake2s(
        (
            f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
            ":display-label-v2"
        ).encode(),
        digest_size=12,
    ).hexdigest()
    target = cache_dir("catalogs") / f"ngc_{revision}.npy"
    if not target.is_file():
        dtype = np.dtype(
            [
                ("ra", "<f4"),
                ("dec", "<f4"),
                ("maj", "<f4"),
                ("min", "<f4"),
                ("pa", "<f4"),
                ("mag", "<f4"),
                ("kind", "<U16"),
                ("name", "<U80"),
            ]
        )
        temporary = target.with_suffix(".tmp.npy")
        data = np.lib.format.open_memmap(
            temporary,
            mode="w+",
            dtype=dtype,
            shape=(len(objects),),
        )
        for index, obj in enumerate(objects):
            magnitude = getattr(obj, "mag_v", None)
            data[index] = (
                float(obj.ra_deg),
                float(obj.dec_deg),
                float(getattr(obj, "maj_deg", 0.0) or 0.0),
                float(getattr(obj, "min_deg", 0.0) or 0.0),
                float(getattr(obj, "pos_ang_deg", 0.0) or 0.0),
                (
                    float(magnitude)
                    if magnitude is not None
                    and math.isfinite(float(magnitude))
                    else 99.0
                ),
                str(getattr(obj, "obj_type", "") or "")[:16],
                ngc_display_label(obj)[:80],
            )
        data.flush()
        del data
        os.replace(temporary, target)
    return {"catalog_path": str(target), "revision": revision}


def _resolve_search_record(payload: dict, coordinator) -> dict:
    """Resolve a selected record to Alt/Az outside the UI process."""

    record = dict(payload.get("record", {}))
    kind = str(record.get("kind", "") or "")
    year = int(payload["year_utc"])
    day = int(payload["day_of_year_utc"])
    hour = float(payload["ut_hour"])
    latitude = float(payload["latitude"])
    longitude = float(payload["longitude"])
    altitude = azimuth = None
    resolved_body_key = ""
    if kind == "planet":
        snapshot = _compute_ephemeris(
            {
                "year_utc": year,
                "day_of_year_utc": day,
                "ut_hour": hour,
                "latitude": latitude,
                "longitude": longitude,
            },
            coordinator,
        )
        key = str(record.get("key", "") or "").lower()
        body = snapshot.get(key)
        if body is None:
            body = next(
                (
                    planet
                    for planet in snapshot.get("planets", ())
                    if (
                        str(planet.get("key", "")).lower() == key
                        or str(planet.get("key", "")).lower().startswith(
                            f"{key} "
                        )
                        or _normalize_search_key(
                            planet.get("name", "")
                        )
                        == _normalize_search_key(
                            record.get("name", "")
                        )
                    )
                ),
                None,
            )
        if isinstance(body, dict):
            altitude = float(body.get("alt", 0.0))
            azimuth = float(body.get("az", 0.0))
            resolved_body_key = str(
                body.get("key", key) or key
            ).lower()
    else:
        from TerraLab.scene.spherical_math import ra_dec_to_alt_az

        altitude, azimuth = ra_dec_to_alt_az(
            float(record["ra"]),
            float(record["dec"]),
            hour,
            day,
            latitude,
            longitude,
            year=year,
        )
    if altitude is None or azimuth is None:
        raise ValueError("The selected object has no position for this time")
    selected_kind = "sky" if kind == "planet" else kind
    selected = {
        "kind": selected_kind,
        "name": str(record.get("name", "") or kind),
        "alt": float(altitude),
        "az": float(azimuth) % 360.0,
    }
    if kind == "planet":
        body_key = str(record.get("key", "") or "").lower()
        selected["type"] = (
            body_key if body_key in {"sun", "moon"} else "planet"
        )
    for key in ("key", "ra", "dec", "source_id"):
        if key in record:
            selected[key] = record[key]
    if kind == "planet":
        selected["key"] = resolved_body_key or str(
            record.get("key", "") or ""
        ).lower()
    if payload.get("client_token") is not None:
        selected["_client_token"] = str(payload["client_token"])
    if payload.get("view_revision") is not None:
        selected["_view_revision"] = int(payload["view_revision"])
    return selected


def _prepare_weather_sample(payload: dict) -> dict:
    """Fetch/cache forecast samples without involving Render or UI."""

    from TerraLab.weather.metno_provider import MetNoWeatherProvider

    latitude = float(payload.get("latitude", 0.0))
    longitude = float(payload.get("longitude", 0.0))
    year = int(payload.get("year_utc", datetime.utcnow().year))
    day = int(payload.get("day_of_year_utc", 0))
    hour = float(payload.get("ut_hour", 0.0)) % 24.0
    floor_hour = int(math.floor(hour))
    base = datetime(year, 1, 1) + timedelta(
        days=day, hours=floor_hour
    )
    slots = (base, base + timedelta(hours=1))
    provider = MetNoWeatherProvider(
        latitude=latitude,
        longitude=longitude,
        use_remote=bool(payload.get("use_remote", True)),
        cache_enabled=bool(payload.get("cache_enabled", True)),
    )
    provider.set_user_agent(str(payload.get("user_agent", "") or ""))
    samples = []
    try:
        for slot in slots:
            slot_day = (
                slot.date() - datetime(slot.year, 1, 1).date()
            ).days
            value = provider.get_weather(
                slot.year, slot_day, slot.hour
            )
            samples.append(
                {
                    "year": int(slot.year),
                    "day": int(slot_day),
                    "hour": int(slot.hour),
                    "value": (
                        _json_safe(value)
                        if isinstance(value, dict)
                        else None
                    ),
                }
            )
        status = str(provider.get_last_status())
    finally:
        provider.shutdown()
    return {"samples": samples, "status": status}


def _convert_coordinates(payload: dict) -> dict:
    """Convert astronomical coordinates only inside Compute."""

    from TerraLab.scene.spherical_math import (
        altaz_to_ra_dec,
        ra_dec_to_alt_az,
    )

    common = (
        float(payload["ut_hour"]),
        int(payload["day_of_year_utc"]),
        float(payload["latitude"]),
        float(payload["longitude"]),
    )
    year = int(payload["year_utc"])
    direction = str(payload.get("direction", "radec_to_altaz"))
    if direction == "altaz_to_radec":
        ra, dec = altaz_to_ra_dec(
            float(payload["alt"]),
            float(payload["az"]),
            *common,
            year=year,
        )
        return {
            "direction": direction,
            "ra": float(ra),
            "dec": float(dec),
        }
    alt, az = ra_dec_to_alt_az(
        float(payload["ra"]),
        float(payload["dec"]),
        *common,
        year=year,
    )
    return {
        "direction": direction,
        "ra": float(payload["ra"]) % 360.0,
        "dec": max(-90.0, min(90.0, float(payload["dec"]))),
        "alt": float(alt),
        "az": float(az) % 360.0,
    }


def _circumpolar_alignment(payload: dict) -> dict:
    """Resolve Polaris to the current observer without UI-side astronomy."""

    converted = _convert_coordinates(
        {
            **dict(payload),
            "direction": "radec_to_altaz",
            # Polaris, ICRS/J2000. The renderer receives only resolved Alt/Az.
            "ra": 37.95456067,
            "dec": 89.26410897,
        }
    )
    return {
        **converted,
        "name": "Polaris",
        "ra": 37.95456067,
        "dec": 89.26410897,
    }


def _prepare_surface_artifact(
    payload: dict, *, progress_callback=None
) -> dict:
    """Sample surface rasters for an immutable terrain profile in Compute."""

    profile_path = Path(str(payload.get("profile_path", "") or ""))
    if not profile_path.is_file():
        raise FileNotFoundError(
            f"Terrain profile is not ready for surface sampling: {profile_path}"
        )
    from TerraLab.common.app_paths import cache_dir
    from TerraLab.terrain.persistence.profile_npz import load_profile
    from TerraLab.terrain.surface import (
        SurfaceSamplingRequest,
        _surface_cache_payload,
    )
    from TerraLab.terrain.surface_store import AtomicNpzStore
    from TerraLab.terrain.worker import SurfaceSamplingRuntime

    profile = load_profile(str(profile_path))
    if profile is None:
        raise ValueError("Terrain profile could not be loaded")
    generation = int(payload.get("surface_generation", 0))
    request = SurfaceSamplingRequest(
        profile=profile,
        visible_radius_m=payload.get("visible_radius_m"),
        view_azimuth_deg=float(
            payload.get("view_azimuth_deg", 0.0) or 0.0
        ),
        view_fov_deg=float(payload.get("view_fov_deg", 360.0) or 360.0),
        viewport_width_px=payload.get("viewport_width_px"),
        viewport_height_px=payload.get("viewport_height_px"),
        generation=generation,
        stage="complete",
        surface_mode=(
            str(payload.get("surface_mode", "") or "").strip() or None
        ),
    )
    surface_runtime = SurfaceSamplingRuntime()
    try:
        prepared = surface_runtime.prepare_surface_samples(
            profile,
            surface_request=request,
            progress_callback=progress_callback,
        )
        cache = getattr(prepared, "surface_samples", None)
        if cache is None:
            if callable(progress_callback):
                progress_callback(100.0, "completed")
            return {
                "profile_path": str(profile_path),
                "surface_path": "",
                "surface_generation": generation,
                "source_id": "",
                "status": str(
                    getattr(prepared, "surface_source_status", "") or ""
                ),
            }
        metadata, arrays = _surface_cache_payload(cache)
        store = AtomicNpzStore(
            cache_dir("terrain", "surface_artifacts"),
            budget_bytes=2 * 1024**3,
        )
        artifact_path = store.save(
            f"published/{cache.cache_id}",
            metadata,
            arrays,
        )
        if callable(progress_callback):
            progress_callback(100.0, "completed")
        return {
            "profile_path": str(profile_path),
            "surface_path": str(artifact_path),
            "surface_generation": generation,
            "source_id": str(
                getattr(prepared, "effective_surface_source_id", "") or ""
            ),
            "status": str(
                getattr(prepared, "surface_source_status", "") or ""
            ),
        }
    finally:
        surface_runtime.close()


def _ensure_catalog_manifest(manifest_path: Path) -> Path:
    import numpy as np

    manifest_path = Path(manifest_path).expanduser().resolve()
    if manifest_path.is_file():
        return manifest_path
    tile_path = manifest_path.parent / "tile_all.npz"
    if not tile_path.is_file():
        raise FileNotFoundError(
            f"Gaia manifest and general tile are missing: {manifest_path}"
        )
    with np.load(tile_path, mmap_mode="r", allow_pickle=False) as tile:
        files = set(getattr(tile, "files", ()))
        coordinate_key = "ra" if "ra" in files else "RA"
        if coordinate_key not in files:
            raise ValueError(
                f"General Gaia tile has no RA column: {tile_path}"
            )
        row_count = int(len(tile[coordinate_key]))
    manifest_payload = {
        "version": 1,
        "tile_size_deg": 5.0,
        "partial": True,
        "general_tile": {
            "id": "tile_all",
            "file": tile_path.name,
            "mag_limit": 8.0,
            "coverage": "full_sky",
            "star_count": row_count,
        },
        "deep_tiles": [],
    }
    temporary = manifest_path.with_suffix(".tmp.json")
    temporary.write_text(
        json.dumps(
            manifest_payload,
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        os.replace(temporary, manifest_path)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
    return manifest_path


def _prepare_catalog(payload):
    import numpy as np

    from TerraLab.data.catalogs.star_catalog import (
        _write_scope_runtime_mmap_bundle,
    )
    from TerraLab.data.star_data_coordinator import (
        _normalize_tile_arrays,
        _read_tile_npz,
    )
    from TerraLab.data.tile_manifest import TileManifest

    manifest_path = _ensure_catalog_manifest(
        Path(str(payload["manifest_path"]))
    )
    manifest = TileManifest()
    manifest.load(manifest_path)
    tile_path = manifest.get_general_tile().file_path
    arrays = _normalize_tile_arrays(_read_tile_npz(tile_path))
    stat = tile_path.stat()
    signature = (
        f"general:{tile_path}:{stat.st_size}:{stat.st_mtime_ns}"
    )
    bundle = _write_scope_runtime_mmap_bundle(
        str(manifest_path.parent),
        arrays.get("source_id"),
        arrays["ra"],
        arrays["dec"],
        arrays["mag"],
        arrays["bp_rp"],
        arrays["r"],
        arrays["g"],
        arrays["b"],
        dataset_signature=signature,
    )
    finite = np.asarray(arrays["mag"])[
        np.isfinite(np.asarray(arrays["mag"]))
    ]
    return {
        "artifact": bundle,
        "rows": int(len(arrays["ra"])),
        "loaded_max_mag": (
            float(np.max(finite)) if len(finite) else 0.0
        ),
    }


def _prepare_catalog_scope(payload):
    import hashlib

    import numpy as np

    from TerraLab.data.catalogs.star_catalog import (
        _write_scope_runtime_mmap_bundle,
    )
    from TerraLab.data.star_data_coordinator import (
        _combine_tiles,
        _normalize_tile_arrays,
        _read_tile_npz,
    )
    from TerraLab.data.tile_manifest import TileManifest
    from TerraLab.scene.spherical_math import altaz_to_ra_dec

    manifest_path = _ensure_catalog_manifest(
        Path(str(payload["manifest_path"]))
    )
    manifest = TileManifest()
    manifest.load(manifest_path)
    ra_center, dec_center = altaz_to_ra_dec(
        float(payload["alt"]),
        float(payload["az"]),
        float(payload["ut_hour"]),
        int(payload["day_of_year_utc"]),
        float(payload["latitude"]),
        float(payload["longitude"]),
        year=int(payload["year_utc"]),
    )
    radius = max(0.1, min(45.0, float(payload.get("radius_deg", 5.0))))
    mag_limit = max(8.0, min(25.0, float(payload.get("mag_limit", 22.0))))
    entries = [manifest.get_general_tile()]
    entries.extend(
        manifest.get_tiles_for_region(ra_center, dec_center, radius)
    )
    loaded = {}
    signatures = []
    for entry in entries:
        if not entry.file_path.is_file():
            continue
        arrays = _normalize_tile_arrays(_read_tile_npz(entry.file_path))
        mask = np.asarray(arrays["mag"]) <= mag_limit
        if not bool(np.all(mask)):
            arrays = {
                key: np.asarray(value)[mask]
                for key, value in arrays.items()
                if key
                in {
                    "ra",
                    "dec",
                    "mag",
                    "bp_rp",
                    "r",
                    "g",
                    "b",
                    "source_id",
                }
            }
        loaded[entry.tile_id] = arrays
        stat = entry.file_path.stat()
        signatures.append(
            (
                entry.tile_id,
                stat.st_size,
                stat.st_mtime_ns,
            )
        )
    combined = _combine_tiles(
        base_tile_id=manifest.get_general_tile().tile_id,
        loaded_tiles=loaded,
    )
    signature = hashlib.blake2s(
        repr(
            (
                round(ra_center, 6),
                round(dec_center, 6),
                round(radius, 4),
                round(mag_limit, 3),
                signatures,
            )
        ).encode("utf-8"),
        digest_size=12,
    ).hexdigest()
    bundle = _write_scope_runtime_mmap_bundle(
        str(manifest_path.parent),
        combined.get("source_id"),
        combined["ra"],
        combined["dec"],
        combined["mag"],
        combined["bp_rp"],
        combined["r"],
        combined["g"],
        combined["b"],
        dataset_signature=f"scope:{signature}",
    )
    return {
        "artifact": bundle,
        "rows": int(len(combined["ra"])),
        "loaded_max_mag": (
            float(np.max(combined["mag"]))
            if len(combined["mag"])
            else 0.0
        ),
        "query": {
            "ra": float(ra_center),
            "dec": float(dec_center),
            "radius_deg": radius,
            "tile_ids": sorted(loaded),
        },
    }


def run() -> int:
    reserve_stdout_for_protocol()
    _limit_background_parallelism()
    _lower_process_priority()
    from TerraLab.astro.ephemeris_coordinator import EphemerisCoordinator

    ephemeris = EphemerisCoordinator()
    tasks = _LatestComputeTasks(ephemeris)
    terrain: _TerrainService | None = None
    assets = _AssetService()
    write_message(
        envelope(
            WORKER_READY,
            {"role": "compute", "pid": os.getpid()},
        )
    )
    try:
        for message in read_messages():
            if message.kind == SHUTDOWN:
                return 0
            if message.kind == HEARTBEAT:
                write_message(
                    envelope(
                        HEARTBEAT,
                        {"role": "compute", "pid": os.getpid()},
                        generation=message.generation,
                    )
                )
                continue
            if message.kind != COMPUTE_REQUEST:
                continue
            operation = str(message.payload.get("operation", ""))
            try:
                if operation == "terrain_bake":
                    if terrain is None:
                        from TerraLab.common.app_paths import runtime_layout

                        terrain = _TerrainService(
                            str(
                                runtime_layout().get(
                                    "data_elevation", ""
                                )
                                or ""
                            )
                        )
                    terrain.start(
                        dict(message.payload.get("job", {})),
                        request_id=message.request_id,
                        generation=message.generation,
                    )
                    continue
                elif operation == "terrain_cancel":
                    if terrain is not None:
                        terrain.cancel()
                    continue
                elif operation == "asset_job":
                    assets.start(
                        dict(message.payload),
                        request_id=message.request_id,
                        generation=message.generation,
                    )
                    continue
                elif operation == "asset_cancel":
                    assets.cancel(
                        str(message.payload.get("job_id", ""))
                    )
                    continue
                else:
                    tasks.start(
                        operation,
                        dict(message.payload),
                        request_id=message.request_id,
                        generation=message.generation,
                    )
                    continue
            except Exception as exc:
                write_message(
                    envelope(
                        WORKER_ERROR,
                        {"operation": operation, "message": str(exc)},
                        request_id=message.request_id,
                        generation=message.generation,
                    )
                )
        return 0
    finally:
        if terrain is not None:
            terrain.shutdown()
        assets.shutdown()
        tasks.shutdown()
        ephemeris.shutdown()


if __name__ == "__main__":
    raise SystemExit(run())
