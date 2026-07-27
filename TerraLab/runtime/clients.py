"""Thin UI-side clients for process-owned domain services."""

from __future__ import annotations

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from TerraLab.runtime.protocol import (
    ARTIFACT_READY,
    COMPUTE_REQUEST,
    PROGRESS,
    WORKER_ERROR,
    Envelope,
    envelope,
)
from TerraLab.runtime.supervisor import RuntimeSupervisor


class ProcessEphemerisClient(QObject):
    """Latest-wins ephemeris client with no scientific dependencies."""

    ephemeris_ready = pyqtSignal(object)
    ephemeris_error = pyqtSignal(str)

    def __init__(
        self,
        runtime: RuntimeSupervisor,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._latitude = 0.0
        self._longitude = 0.0
        self._generation = 0
        self._snapshot = None
        self._shutdown = False
        self._inflight = False
        self._pending: dict | None = None
        self._last_request_key = None
        runtime.message_received.connect(self._on_message)
        runtime.worker_ready.connect(self._on_worker_ready)

    def configure_observer(self, latitude: float, longitude: float) -> None:
        self._latitude = float(latitude)
        self._longitude = float(longitude)

    def request_snapshot(
        self,
        *,
        year_utc: int,
        day_of_year_utc: int,
        ut_hour: float,
    ) -> None:
        if self._shutdown:
            return
        payload = {
            "operation": "ephemeris",
            "year_utc": int(year_utc),
            "day_of_year_utc": int(day_of_year_utc),
            "ut_hour": float(ut_hour),
            "latitude": self._latitude,
            "longitude": self._longitude,
        }
        key = (
            payload["year_utc"],
            payload["day_of_year_utc"],
            int(round(payload["ut_hour"] * 3600.0)),
            int(round(self._latitude * 10_000.0)),
            int(round(self._longitude * 10_000.0)),
        )
        if key == self._last_request_key and not self._inflight:
            return
        self._last_request_key = key
        self._pending = payload
        self._dispatch()

    def _dispatch(self) -> None:
        if self._shutdown or self._inflight or self._pending is None:
            return
        payload, self._pending = self._pending, None
        self._generation += 1
        self._inflight = self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                payload,
                request_id="ephemeris",
                generation=self._generation,
            ),
        )
        if not self._inflight:
            self._pending = payload

    def get_snapshot(self):
        return (
            dict(self._snapshot)
            if isinstance(self._snapshot, dict)
            else None
        )

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._disconnect_runtime()

    def _disconnect_runtime(self) -> None:
        for signal, callback in (
            (self._runtime.message_received, self._on_message),
            (self._runtime.worker_ready, self._on_worker_ready),
        ):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass

    def _on_worker_ready(self, role: str) -> None:
        if role != "compute" or self._shutdown:
            return
        self._inflight = False
        self._dispatch()

    def _on_message(self, role: str, message: Envelope) -> None:
        if (
            self._shutdown
            or role != "compute"
            or message.request_id != "ephemeris"
            or message.generation != self._generation
        ):
            return
        if message.kind == ARTIFACT_READY:
            value = message.payload.get("value")
            if isinstance(value, dict):
                self._snapshot = dict(value)
                self.ephemeris_ready.emit(dict(value))
        elif message.kind == WORKER_ERROR:
            self.ephemeris_error.emit(
                str(message.payload.get("message", "Ephemeris error"))
            )
        self._inflight = False
        if self._pending is not None:
            QTimer.singleShot(0, self._dispatch)


class ProcessLatestClient(QObject):
    """Reusable latest-wins facade for one isolated Compute operation."""

    result_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        runtime: RuntimeSupervisor,
        operation: str,
        *,
        request_id: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._operation = str(operation)
        self._request_id = str(request_id or operation)
        self._generation = 0
        self._last_payload: dict | None = None
        self._last_sent_payload: dict | None = None
        self._shutdown = False
        runtime.message_received.connect(self._on_message)
        runtime.worker_ready.connect(self._on_worker_ready)

    def request(self, payload: dict | None = None, *, force=False) -> None:
        if self._shutdown:
            return
        body = {
            "operation": self._operation,
            **dict(payload or {}),
        }
        self._last_payload = body
        if not force and body == self._last_sent_payload:
            return
        self._generation += 1
        if self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                body,
                request_id=self._request_id,
                generation=self._generation,
            ),
        ):
            self._last_sent_payload = dict(body)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        for signal, callback in (
            (self._runtime.message_received, self._on_message),
            (self._runtime.worker_ready, self._on_worker_ready),
        ):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass

    def _on_worker_ready(self, role: str) -> None:
        if (
            role == "compute"
            and not self._shutdown
            and self._last_payload is not None
        ):
            self._last_sent_payload = None
            self.request(self._last_payload, force=True)

    def _on_message(self, role: str, message: Envelope) -> None:
        if (
            self._shutdown
            or role != "compute"
            or message.request_id != self._request_id
            or message.generation != self._generation
        ):
            return
        if message.kind == ARTIFACT_READY:
            self.result_ready.emit(message.payload.get("value"))
        elif message.kind == WORKER_ERROR:
            self.error_occurred.emit(
                str(message.payload.get("message", "Compute error"))
            )


class ProcessCatalogClient(QObject):
    """UI-side catalog client publishing mmap artifact descriptors only."""

    general_tile_ready = pyqtSignal(object)
    extension_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        runtime: RuntimeSupervisor,
        manifest_path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._manifest_path = str(manifest_path)
        self._generation = 0
        self._scope_generation = 0
        self._scope_request: dict | None = None
        self._shutdown = False
        runtime.message_received.connect(self._on_message)
        runtime.worker_ready.connect(self._on_worker_ready)

    def load_general_tile(self) -> None:
        if self._shutdown:
            return
        self._generation += 1
        self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                {
                    "operation": "catalog_general",
                    "manifest_path": self._manifest_path,
                },
                request_id="catalog",
                generation=self._generation,
            ),
        )

    def request_scope_region(self, **context) -> None:
        if self._shutdown:
            return
        self._scope_generation += 1
        self._scope_request = {
            "operation": "catalog_scope",
            "manifest_path": self._manifest_path,
            **dict(context),
        }
        self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                self._scope_request,
                request_id="catalog_scope",
                generation=self._scope_generation,
            ),
        )

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        for signal, callback in (
            (self._runtime.message_received, self._on_message),
            (self._runtime.worker_ready, self._on_worker_ready),
        ):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass

    def _on_worker_ready(self, role: str) -> None:
        if role == "compute" and not self._shutdown:
            self.load_general_tile()
            if self._scope_request is not None:
                self.request_scope_region(**self._scope_request)

    def _on_message(self, role: str, message: Envelope) -> None:
        if (
            self._shutdown
            or role != "compute"
        ):
            return
        if message.request_id == "catalog":
            expected_generation = self._generation
        elif message.request_id == "catalog_scope":
            expected_generation = self._scope_generation
        else:
            return
        if message.generation != expected_generation:
            return
        if message.kind == ARTIFACT_READY:
            value = message.payload.get("value")
            if isinstance(value, dict):
                if message.request_id == "catalog_scope":
                    self.extension_ready.emit(dict(value))
                else:
                    self.general_tile_ready.emit(dict(value))
        elif message.kind == WORKER_ERROR:
            self.error_occurred.emit(
                str(message.payload.get("message", "Catalog error"))
            )


class ProcessTerrainClient(QObject):
    """Terrain facade whose profiles remain immutable process artifacts."""

    horizon_ready = pyqtSignal(object)
    horizon_preview_ready = pyqtSignal(object)
    horizon_progress = pyqtSignal(object)
    horizon_error = pyqtSignal(str)
    bortle_estimate_ready = pyqtSignal(int, float, float, int)
    bare_elevation_ready = pyqtSignal(float, float, object)
    surface_ready = pyqtSignal(object)

    def __init__(
        self,
        runtime: RuntimeSupervisor,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._generation = 0
        self._job: dict | None = None
        self._observer_offset = 0.0
        self._profile_path = ""
        self._surface_generation = 0
        self._progress_text = ""
        self._shutdown = False
        runtime.message_received.connect(self._on_message)
        runtime.worker_ready.connect(self._on_worker_ready)

    @property
    def observer_offset(self) -> float:
        return self._observer_offset

    def initialize(self) -> None:
        return

    def reload_config(self) -> None:
        return

    def set_observer_offset(self, offset: float) -> None:
        self._observer_offset = float(offset)

    def request_bake(self, job: dict) -> None:
        if self._shutdown:
            return
        self._generation += 1
        self._job = dict(job)
        self._job["observer_offset"] = self._observer_offset
        accepted = self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                {"operation": "terrain_bake", "job": self._job},
                request_id="terrain",
                generation=self._generation,
            ),
        )
        if not accepted:
            self.horizon_error.emit(
                "Compute no está disponible; el bake se reintentará "
                "cuando el proceso se recupere."
            )

    def abort_current_job(self) -> None:
        if self._shutdown:
            return
        self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                {"operation": "terrain_cancel"},
                request_id="terrain-control",
                generation=self._generation,
            ),
        )

    def cancel_surface_refresh(self) -> None:
        self._surface_generation += 1

    def request_surface_refresh(self, *args, **kwargs) -> None:
        if self._shutdown:
            return
        profile = kwargs.pop("profile", None)
        if args and profile is None:
            profile = args[0]
        profile_path = self._profile_path
        if isinstance(profile, dict):
            profile_path = str(profile.get("profile_path", "") or profile_path)
        elif isinstance(profile, (str, bytes)):
            profile_path = str(profile)
        if not profile_path:
            return
        self._surface_generation += 1
        generation = self._surface_generation
        payload = {
            key: value
            for key, value in kwargs.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        payload.update(
            {
                "operation": "terrain_surface",
                "profile_path": str(profile_path),
                "surface_generation": generation,
            }
        )
        accepted = self._runtime.send(
            "compute",
            envelope(
                COMPUTE_REQUEST,
                payload,
                request_id="terrain-surface",
                generation=generation,
            ),
        )
        if not accepted:
            self.horizon_error.emit(
                "Compute no está disponible para preparar la superficie."
            )

    def request_bortle_estimate(
        self, lat: float, lon: float, request_id: int = 0
    ) -> None:
        # Retain the configured fallback until the process query is available.
        self.bortle_estimate_ready.emit(
            int(request_id), float(lat), float(lon), 4
        )

    def get_bare_elevation(
        self, _lat: float, _lon: float
    ) -> float | None:
        return None

    def get_progress_text(self) -> str:
        return self._progress_text

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self.abort_current_job()
        self._shutdown = True
        for signal, callback in (
            (self._runtime.message_received, self._on_message),
            (self._runtime.worker_ready, self._on_worker_ready),
        ):
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass

    def _on_worker_ready(self, role: str) -> None:
        if role == "compute" and self._job is not None and not self._shutdown:
            self.request_bake(self._job)

    def _on_message(self, role: str, message: Envelope) -> None:
        if (
            self._shutdown
            or role != "compute"
        ):
            return
        if message.request_id == "terrain-surface":
            if message.generation != self._surface_generation:
                return
            if message.kind == PROGRESS:
                value = message.payload.get("value")
                if isinstance(value, dict):
                    phase = str(value.get("phase", "") or "")
                    percent = float(value.get("percent", 0.0) or 0.0)
                    self._progress_text = f"{phase}: {percent:.1f}%"
                    self.horizon_progress.emit(dict(value))
            elif message.kind == ARTIFACT_READY:
                value = message.payload.get("value")
                if isinstance(value, dict):
                    self._progress_text = ""
                    self.surface_ready.emit(dict(value))
            elif message.kind == WORKER_ERROR:
                self.horizon_error.emit(
                    str(
                        message.payload.get(
                            "message", "Surface preparation error"
                        )
                    )
                )
            return
        if (
            message.request_id != "terrain"
            or message.generation != self._generation
        ):
            return
        if message.kind == PROGRESS:
            value = message.payload.get("value")
            if isinstance(value, dict):
                phase = str(value.get("phase", "") or "")
                percent = float(value.get("percent", 0.0) or 0.0)
                self._progress_text = f"{phase}: {percent:.1f}%"
                self.horizon_progress.emit(dict(value))
        elif message.kind == ARTIFACT_READY:
            value = message.payload.get("value")
            if not isinstance(value, dict):
                return
            if bool(value.get("preview", False)):
                self.horizon_preview_ready.emit(dict(value))
            else:
                self._profile_path = str(
                    value.get("profile_path", "") or ""
                )
                self._progress_text = ""
                self.horizon_ready.emit(dict(value))
        elif message.kind == WORKER_ERROR:
            self.horizon_error.emit(
                str(message.payload.get("message", "Terrain error"))
            )


class ProcessWeatherSettings:
    """UI-owned weather settings with no simulation or rendering code."""

    def __init__(
        self,
        *,
        latitude: float,
        longitude: float,
        use_remote: bool,
        cache_enabled: bool,
    ) -> None:
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.enabled = True
        self.use_remote = bool(use_remote)
        self.cache_enabled = bool(cache_enabled)
        self.user_agent = ""
        self.bortle = 4
        self.sample_artifact: dict = {}
        self.sample_revision = 0

    def set_location(self, latitude: float, longitude: float) -> None:
        self.latitude = float(latitude)
        self.longitude = float(longitude)

    def set_remote_weather_enabled(self, enabled: bool) -> None:
        self.use_remote = bool(enabled)

    def set_cache_enabled(self, enabled: bool) -> None:
        self.cache_enabled = bool(enabled)

    def set_remote_user_agent(self, user_agent: str) -> None:
        self.user_agent = str(user_agent or "").strip()

    def set_bortle(self, value: int) -> None:
        self.bortle = max(1, min(9, int(value)))

    def set_sample_artifact(self, artifact: object) -> None:
        self.sample_artifact = (
            dict(artifact) if isinstance(artifact, dict) else {}
        )
        self.sample_revision += 1

    def resize(self, _width: int, _height: int) -> None:
        return

    def shutdown(self) -> None:
        return

    def get_cache_path(self) -> str:
        from TerraLab.common.app_paths import weather_cache_path

        return str(weather_cache_path())

    def get_runtime_status(self) -> dict:
        return {
            "source": "process",
            "reason": "render_process_owned",
            "requires_user_agent": bool(
                self.use_remote and not self.user_agent
            ),
        }

    def snapshot(self) -> dict:
        return {
            "enabled": bool(self.enabled),
            "latitude": float(self.latitude),
            "longitude": float(self.longitude),
            "use_remote": bool(self.use_remote),
            "cache_enabled": bool(self.cache_enabled),
            "user_agent": self.user_agent,
            "bortle": int(self.bortle),
            "sample_artifact": dict(self.sample_artifact),
            "sample_revision": int(self.sample_revision),
        }
