"""Non-blocking QProcess ownership for TerraLab's isolated services."""

from __future__ import annotations

import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PyQt5.QtCore import (
    QByteArray,
    QObject,
    QProcess,
    QProcessEnvironment,
    QTimer,
    pyqtSignal,
)

from TerraLab.bootstrap.composition import RenderRoute, build_render_route
from TerraLab.runtime.protocol import (
    Envelope,
    HEARTBEAT,
    ProtocolError,
    SHUTDOWN,
    WORKER_ERROR,
    decode,
    encode,
    envelope,
)


@dataclass(slots=True)
class _WorkerState:
    role: str
    module: str
    process: QProcess
    stdout: bytearray
    stderr: bytearray
    pending: deque[Envelope] | None = None
    ready: bool = False
    stopping: bool = False
    restart_count: int = 0
    last_failure_mono: float = 0.0
    started_mono: float = 0.0
    last_heartbeat_mono: float = 0.0
    failure_reason: str = ""


class RuntimeSupervisor(QObject):
    """Own isolated compute processes without blocking the GUI thread."""

    message_received = pyqtSignal(str, object)
    worker_ready = pyqtSignal(str)
    worker_failed = pyqtSignal(str, str)
    worker_unavailable = pyqtSignal(str, str)

    _AUXILIARY_MODULES = {
        "compute": "TerraLab.runtime.compute_service",
    }

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        render_backend: str | None = None,
        render_route: RenderRoute | None = None,
    ) -> None:
        super().__init__(parent)
        self._workers: dict[str, _WorkerState] = {}
        self._render_route = render_route or build_render_route(
            explicit_backend=render_backend
        )
        self._closing = False
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setSingleShot(True)
        self._shutdown_timer.timeout.connect(self._force_stop_remaining)
        self._heartbeat_generation = 0
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(2_000)
        self._heartbeat_timer.timeout.connect(self._check_workers)

    def start(self) -> None:
        for role, module in self._AUXILIARY_MODULES.items():
            if role not in self._workers:
                self._workers[role] = self._create_worker(role, module)
            self._start_worker(self._workers[role])
        self._heartbeat_timer.start()

    @property
    def render_route(self) -> RenderRoute:
        """The composition-selected backend/target/presenter lifecycle."""

        return self._render_route

    def is_ready(self, role: str) -> bool:
        state = self._workers.get(str(role))
        return bool(state is not None and state.ready)

    def send(self, role: str, message: Envelope) -> bool:
        """Queue one latest-wins message for a worker."""

        state = self._workers.get(str(role))
        if (
            state is None
            or state.stopping
            or state.process.state() != QProcess.Running
        ):
            return False
        if state.pending is None:
            state.pending = deque()
        self._queue(state, message)
        self._flush(state)
        return True

    def stop(self, timeout_ms: int = 2_000) -> None:
        """Begin cooperative shutdown and return immediately."""

        if self._closing:
            return
        self._closing = True
        self._heartbeat_timer.stop()
        for state in self._workers.values():
            if state.process.state() == QProcess.NotRunning:
                continue
            state.stopping = True
            state.pending = deque((envelope(SHUTDOWN),))
            self._flush(state, force=True)
            state.process.closeWriteChannel()
        self._shutdown_timer.start(max(0, int(timeout_ms)))

    def finish_shutdown(self, timeout_ms: int = 2_000) -> None:
        """Finish process teardown after the visible Qt loop has exited."""

        self.stop(timeout_ms)
        deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
        for state in self._workers.values():
            process = state.process
            if process.state() == QProcess.NotRunning:
                continue
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000.0))
            if remaining_ms and process.waitForFinished(remaining_ms):
                continue
            process.terminate()
            if not process.waitForFinished(250):
                process.kill()
                process.waitForFinished(250)

    def retry(self, role: str) -> bool:
        """Retry one persistently failed worker on explicit user action."""

        state = self._workers.get(str(role))
        if (
            self._closing
            or state is None
            or state.process.state() != QProcess.NotRunning
        ):
            return False
        state.restart_count = 0
        state.last_failure_mono = 0.0
        self._start_worker(state)
        return True

    def _create_worker(self, role: str, module: str) -> _WorkerState:
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.SeparateChannels)
        state = _WorkerState(
            role=role,
            module=module,
            process=process,
            stdout=bytearray(),
            stderr=bytearray(),
            pending=deque(),
        )
        process.readyReadStandardOutput.connect(
            lambda worker=state: self._read_stdout(worker)
        )
        process.readyReadStandardError.connect(
            lambda worker=state: self._read_stderr(worker)
        )
        process.bytesWritten.connect(
            lambda _count, worker=state: self._flush(worker)
        )
        process.started.connect(lambda worker=state: self._on_started(worker))
        process.errorOccurred.connect(
            lambda error, worker=state: self._on_process_error(worker, error)
        )
        process.finished.connect(
            lambda code, status, worker=state: self._on_finished(
                worker, code, status
            )
        )
        return state

    def _start_worker(self, state: _WorkerState) -> None:
        if self._closing:
            return
        state.ready = False
        state.stopping = False
        state.stdout.clear()
        state.stderr.clear()
        state.pending = deque()
        state.started_mono = time.monotonic()
        state.last_heartbeat_mono = state.started_mono
        state.failure_reason = ""
        import_root = Path(__file__).resolve().parents[2]
        environment = QProcessEnvironment.systemEnvironment()
        existing_pythonpath = environment.value("PYTHONPATH", "")
        pythonpath = str(import_root)
        if existing_pythonpath:
            pythonpath = pythonpath + os.pathsep + existing_pythonpath
        environment.insert("PYTHONPATH", pythonpath)
        state.process.setProcessEnvironment(environment)
        state.process.setWorkingDirectory(str(import_root))
        state.process.setProgram(sys.executable)
        state.process.setArguments(["-m", state.module])
        state.process.start()

    def _on_started(self, state: _WorkerState) -> None:
        # Readiness is confirmed by the versioned handshake, not by process
        # creation alone.
        return

    def _flush(self, state: _WorkerState, *, force: bool = False) -> None:
        if not state.pending or state.process.state() != QProcess.Running:
            return
        # QProcess has its own bounded write buffer. Retain only the newest
        # state while previous bytes are still being delivered.
        if not force and state.process.bytesToWrite() > 0:
            return
        message = state.pending.popleft()
        state.process.write(QByteArray(encode(message)))

    @staticmethod
    def _queue(state: _WorkerState, message: Envelope) -> None:
        """Keep protocol ordering while coalescing replaceable state."""

        if state.pending is None:
            state.pending = deque()
        latest_wins = message.kind == "compute_request" and bool(
            message.request_id
        )
        if latest_wins:
            state.pending = deque(
                candidate
                for candidate in state.pending
                if not (
                    candidate.kind == message.kind
                    and candidate.request_id == message.request_id
                )
            )
        state.pending.append(message)

    def _read_stdout(self, state: _WorkerState) -> None:
        state.stdout.extend(bytes(state.process.readAllStandardOutput()))
        while True:
            newline = state.stdout.find(b"\n")
            if newline < 0:
                break
            raw = bytes(state.stdout[:newline])
            del state.stdout[: newline + 1]
            if not raw:
                continue
            try:
                message = decode(raw)
            except ProtocolError as exc:
                self.worker_failed.emit(
                    state.role, f"Invalid worker message: {exc}"
                )
                continue
            state.last_heartbeat_mono = time.monotonic()
            if message.kind == "worker_ready":
                state.ready = True
                self.worker_ready.emit(state.role)
            elif message.kind == WORKER_ERROR:
                self.worker_failed.emit(
                    state.role,
                    str(message.payload.get("message", "Worker error")),
                )
            self.message_received.emit(state.role, message)

    def _read_stderr(self, state: _WorkerState) -> None:
        state.stderr.extend(bytes(state.process.readAllStandardError()))
        # Keep at most one bounded diagnostic tail.
        if len(state.stderr) > 16_384:
            del state.stderr[:-16_384]

    def _on_process_error(
        self, state: _WorkerState, _error: QProcess.ProcessError
    ) -> None:
        if self._closing or state.stopping:
            return
        detail = bytes(state.stderr).decode("utf-8", errors="replace").strip()
        detail = state.failure_reason or detail
        self.worker_failed.emit(
            state.role, detail or state.process.errorString()
        )

    def _on_finished(
        self,
        state: _WorkerState,
        exit_code: int,
        _exit_status: QProcess.ExitStatus,
    ) -> None:
        state.ready = False
        if self._closing or state.stopping:
            return
        now = time.monotonic()
        if now - state.last_failure_mono > 60.0:
            state.restart_count = 0
        state.last_failure_mono = now
        detail = bytes(state.stderr).decode("utf-8", errors="replace").strip()
        detail = state.failure_reason or detail
        self.worker_failed.emit(
            state.role,
            detail or f"Worker exited with code {int(exit_code)}",
        )
        if state.restart_count >= 1:
            self.worker_unavailable.emit(
                state.role,
                detail or f"Worker exited with code {int(exit_code)}",
            )
            return
        state.restart_count += 1
        QTimer.singleShot(500, lambda worker=state: self._start_worker(worker))

    def _check_workers(self) -> None:
        if self._closing:
            return
        now = time.monotonic()
        self._heartbeat_generation += 1
        for state in self._workers.values():
            if state.process.state() != QProcess.Running:
                continue
            if (state.ready and now - state.last_heartbeat_mono > 6.0) or (
                not state.ready and now - state.started_mono > 10.0
            ):
                state.failure_reason = f"{state.role} worker heartbeat timeout"
                state.process.terminate()
                continue
            self.send(
                state.role,
                envelope(
                    HEARTBEAT,
                    generation=self._heartbeat_generation,
                ),
            )

    def _force_stop_remaining(self) -> None:
        for state in self._workers.values():
            process = state.process
            if process.state() == QProcess.NotRunning:
                continue
            process.terminate()
            QTimer.singleShot(
                250,
                lambda candidate=process: (
                    candidate.kill()
                    if candidate.state() != QProcess.NotRunning
                    else None
                ),
            )
