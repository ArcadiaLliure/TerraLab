from __future__ import annotations

import os
import queue
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from TerraLab.runtime.frame_pool import (
    AttachedFramePool,
    OwnedFramePool,
    first_free_slot,
)
from TerraLab.runtime.protocol import (
    ARTIFACT_READY,
    COMPUTE_REQUEST,
    FRAME_POOL,
    FRAME_READY,
    FRAME_RELEASED,
    HEARTBEAT,
    PROTOCOL_VERSION,
    SCENE_SNAPSHOT,
    SHUTDOWN,
    ProtocolError,
    decode,
    encode,
    envelope,
)


def _start_service(module: str, *, env=None):
    process = subprocess.Popen(
        [sys.executable, "-m", module],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    ready = decode(process.stdout.readline())
    assert ready.kind == "worker_ready"
    assert int(ready.payload["pid"]) != 0
    return process


def _send(process, message):
    assert process.stdin is not None
    process.stdin.write(encode(message))
    process.stdin.flush()


def _stop(process) -> None:
    if process.poll() is None:
        _send(process, envelope(SHUTDOWN))
    assert process.wait(timeout=10) == 0


def test_protocol_is_json_only_and_versioned() -> None:
    original = envelope(
        SCENE_SNAPSHOT,
        {"camera": {"azimuth": 12.5}, "layers": ["stars"]},
        request_id="frame-4",
        generation=4,
    )
    decoded = decode(encode(original))
    assert decoded == original
    assert decoded.version == PROTOCOL_VERSION

    with pytest.raises(ProtocolError):
        envelope(SCENE_SNAPSHOT, {"bad": object()})
    with pytest.raises(ProtocolError):
        decode('{"version":999,"kind":"shutdown","payload":{}}')


def test_frame_pool_has_bounded_non_overwriting_slots() -> None:
    with OwnedFramePool(20, 10) as owner:
        with AttachedFramePool(owner.descriptor) as attached:
            attached.buffer(1)[:4] = b"\x01\x02\x03\x04"
            assert bytes(owner.buffer(1)[:4]) == b"\x01\x02\x03\x04"
        assert first_free_slot(3, {0, 1}, after=1) == 2
        assert first_free_slot(3, {0, 1, 2}) is None


def test_frame_pool_buffer_leases_can_be_released_and_reacquired() -> None:
    with OwnedFramePool(8, 4) as owner:
        first = owner.buffer(0)
        first[:4] = b"\x01\x02\x03\x04"
        first.release()

        second = owner.buffer(0)
        assert bytes(second[:4]) == b"\x01\x02\x03\x04"
        second.release()

        with AttachedFramePool(owner.descriptor) as attached:
            worker_first = attached.buffer(0)
            worker_first.release()
            worker_second = attached.buffer(0)
            worker_second[:4] = b"\x05\x06\x07\x08"
            worker_second.release()

        final = owner.buffer(0)
        assert bytes(final[:4]) == b"\x05\x06\x07\x08"
        final.release()


def test_presenter_can_repaint_the_same_shared_slot_repeatedly(
    tmp_path,
) -> None:
    script = textwrap.dedent(
        """
        from PyQt5.QtCore import QObject, pyqtSignal
        from PyQt5.QtWidgets import QApplication

        from TerraLab.ui.frame_presenter import SharedFramePresenter

        class RuntimeStub(QObject):
            worker_ready = pyqtSignal(str)
            message_received = pyqtSignal(str, object)

            def send(self, _role, _message):
                return True

        app = QApplication.instance() or QApplication([])
        runtime = RuntimeStub()
        presenter = SharedFramePresenter(runtime)
        presenter.resize(64, 32)
        presenter.show()
        app.processEvents()
        if presenter._pool is None:
            presenter._replace_pool()
        pool = presenter._pool
        assert pool is not None
        lease = pool.buffer(0)
        lease[:] = bytes([12, 34, 56, 255]) * (
            pool.descriptor.width * pool.descriptor.height
        )
        lease.release()
        presenter._current_slot = 0
        presenter._current_pool_generation = presenter._pool_generation
        for _ in range(12):
            presenter.repaint()
            app.processEvents()
        assert len(presenter.paint_times_ms) >= 12
        presenter.shutdown()
        presenter.close()
        """
    )
    environment = os.environ.copy()
    environment.update(
        {
            "APPDATA": str(tmp_path / "state"),
            "QT_QPA_PLATFORM": "offscreen",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def test_compute_service_runs_ephemeris_outside_caller_process() -> None:
    process = _start_service("TerraLab.runtime.compute_service")
    try:
        _send(
            process,
            envelope(
                COMPUTE_REQUEST,
                {
                    "operation": "ephemeris",
                    "year_utc": 2026,
                    "day_of_year_utc": 100,
                    "ut_hour": 22.0,
                    "latitude": 41.18,
                    "longitude": 1.21,
                },
                request_id="eph",
                generation=3,
            ),
        )
        result = decode(process.stdout.readline())
        assert result.kind == ARTIFACT_READY
        assert result.request_id == "eph"
        assert result.generation == 3
        assert "sun" in result.payload["value"]
        assert all(
            "mag" in planet
            for planet in result.payload["value"].get("planets", ())
        )
    finally:
        _stop(process)


def test_compute_publishes_search_and_deep_sky_render_artifacts(
    tmp_path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["APPDATA"] = str(tmp_path / "state")
    process = _start_service(
        "TerraLab.runtime.compute_service",
        env=environment,
    )
    try:
        _send(
            process,
            envelope(
                COMPUTE_REQUEST,
                {
                    "operation": "search_index",
                    "named_stars_path": str(
                        root
                        / "TerraLab"
                        / "data"
                        / "stars"
                        / "no_gaia_stars.json"
                    ),
                    "ngc_paths": [
                        str(
                            root
                            / "TerraLab"
                            / "data"
                            / "sky"
                            / "openngc_catalog.csv"
                        )
                    ],
                },
                request_id="search-index",
                generation=4,
            ),
        )
        result = decode(process.stdout.readline())
        assert result.kind == ARTIFACT_READY
        value = result.payload["value"]
        assert value["aliases"] > 100
        assert any(
            record.get("kind") == "planet"
            for record in value["records"]
        )
        artifact = value["ngc_artifact"]
        artifact_path = Path(artifact["catalog_path"])
        assert artifact_path.is_file()
        assert artifact_path.is_relative_to(tmp_path / "state")
    finally:
        _stop(process)

    render = _start_service("TerraLab.runtime.render_service")
    try:
        with OwnedFramePool(96, 64) as owner:
            _send(render, envelope(FRAME_POOL, owner.descriptor.to_payload()))
            _send(
                render,
                envelope(
                    SCENE_SNAPSHOT,
                    {
                        "layers": ["deep_sky"],
                        "ngc": artifact,
                        "latitude": 41.2,
                        "longitude": 0.8,
                        "year_utc": 2026,
                        "day_of_year_utc": 200,
                        "ut_hour": 22.0,
                        "sun_alt": -18.0,
                        "loading": False,
                    },
                    generation=4,
                ),
            )
            frame = decode(render.stdout.readline())
            assert frame.kind == FRAME_READY
            assert any(owner.buffer(int(frame.payload["slot"])))
    finally:
        _stop(render)


def test_compute_service_remains_responsive_while_a_job_runs() -> None:
    process = _start_service("TerraLab.runtime.compute_service")
    try:
        _send(
            process,
            envelope(
                COMPUTE_REQUEST,
                {"operation": "sleep", "seconds": 2.0},
                request_id="long-job",
                generation=1,
            ),
        )
        started = time.monotonic()
        _send(process, envelope(HEARTBEAT, generation=17))

        heartbeat = decode(process.stdout.readline())
        assert heartbeat.kind == HEARTBEAT
        assert heartbeat.generation == 17
        assert time.monotonic() - started < 1.0

        result = decode(process.stdout.readline())
        assert result.kind == ARTIFACT_READY
        assert result.request_id == "long-job"
    finally:
        _stop(process)


def test_compute_service_can_shutdown_during_a_running_job() -> None:
    process = _start_service("TerraLab.runtime.compute_service")
    try:
        _send(
            process,
            envelope(
                COMPUTE_REQUEST,
                {"operation": "sleep", "seconds": 10.0},
                request_id="long-job",
                generation=1,
            ),
        )
        started = time.monotonic()
        _send(process, envelope(SHUTDOWN))
        assert process.wait(timeout=2.0) == 0
        assert time.monotonic() - started < 2.0
    finally:
        if process.poll() is None:
            _stop(process)


def test_compute_terrain_reports_progress_and_publishes_an_artifact(
    tmp_path,
) -> None:
    environment = os.environ.copy()
    environment["APPDATA"] = str(tmp_path / "state")
    environment["TERRALAB_DATA_ROOT"] = str(tmp_path / "library")
    process = _start_service(
        "TerraLab.runtime.compute_service",
        env=environment,
    )
    messages: queue.Queue = queue.Queue()

    def read_messages() -> None:
        assert process.stdout is not None
        for line in iter(process.stdout.readline, b""):
            if not line:
                return
            messages.put(decode(line))

    reader = threading.Thread(target=read_messages, daemon=True)
    reader.start()
    try:
        _send(
            process,
            envelope(
                COMPUTE_REQUEST,
                {
                    "operation": "terrain_bake",
                    "job": {
                        "job_id": "isolated-terrain",
                        "lat": 41.21307879636116,
                        "lon": 0.8060244412739804,
                        "bands": 4,
                        "ray_step_deg": 5.0,
                        "representation_mode": "silhouette",
                    },
                },
                request_id="terrain",
                generation=7,
            ),
        )
        deadline = time.monotonic() + 15.0
        progress = []
        artifact = None
        while time.monotonic() < deadline and artifact is None:
            remaining = max(0.05, deadline - time.monotonic())
            message = messages.get(timeout=remaining)
            assert message.request_id == "terrain"
            assert message.generation == 7
            if message.kind == "progress":
                progress.append(message.payload["value"]["phase"])
            elif message.kind == ARTIFACT_READY:
                value = message.payload["value"]
                if not bool(value.get("preview", False)):
                    artifact = Path(value["profile_path"])
        assert progress and progress[0] == "discovering_sources"
        assert artifact is not None and artifact.is_file()
        assert artifact.is_relative_to(tmp_path / "library")
    finally:
        _stop(process)


def test_render_service_writes_a_shared_frame() -> None:
    process = _start_service("TerraLab.runtime.render_service")
    try:
        with OwnedFramePool(64, 32) as owner:
            _send(process, envelope(FRAME_POOL, owner.descriptor.to_payload()))
            _send(
                process,
                envelope(
                    SCENE_SNAPSHOT,
                    {"sun_alt": -12.0, "loading": False},
                    generation=8,
                ),
            )
            result = decode(process.stdout.readline())
            assert result.kind == FRAME_READY
            assert result.generation == 8
            slot = int(result.payload["slot"])
            assert any(owner.buffer(slot))
    finally:
        _stop(process)


def test_render_service_can_reuse_released_slots_repeatedly() -> None:
    process = _start_service("TerraLab.runtime.render_service")
    try:
        with OwnedFramePool(48, 24) as owner:
            _send(process, envelope(FRAME_POOL, owner.descriptor.to_payload()))
            used_slots = []
            for generation in range(1, 9):
                _send(
                    process,
                    envelope(
                        SCENE_SNAPSHOT,
                        {
                            "sun_alt": -12.0,
                            "camera": {"azimuth": float(generation)},
                            "loading": False,
                        },
                        generation=generation,
                    ),
                )
                result = decode(process.stdout.readline())
                assert result.kind == FRAME_READY
                slot = int(result.payload["slot"])
                used_slots.append(slot)
                lease = owner.buffer(slot)
                assert any(lease)
                lease.release()
                _send(
                    process,
                    envelope(
                        FRAME_RELEASED,
                        {
                            "slot": slot,
                            "pool_generation": 0,
                        },
                    ),
                )
            assert len(set(used_slots)) < len(used_slots)
    finally:
        _stop(process)


def test_supervisor_workers_do_not_resolve_an_installed_shadow_package() -> None:
    root = Path(__file__).resolve().parents[1]
    package_directory = root / "TerraLab"
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(root)!r})

        from PyQt5.QtCore import QCoreApplication, QTimer
        from TerraLab.runtime.supervisor import RuntimeSupervisor

        app = QCoreApplication([])
        runtime = RuntimeSupervisor(app)
        ready = set()

        def on_ready(role):
            ready.add(role)
            if ready == {{"render", "compute"}}:
                runtime.stop(100)
                QTimer.singleShot(500, app.quit)

        runtime.worker_ready.connect(on_ready)
        QTimer.singleShot(12_000, app.quit)
        runtime.start()
        app.exec_()
        if ready != {{"render", "compute"}}:
            raise SystemExit(f"workers not ready: {{sorted(ready)}}")
        """
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=package_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def test_supervisor_restarts_once_then_requires_explicit_retry() -> None:
    script = textwrap.dedent(
        """
        from PyQt5.QtCore import QCoreApplication, QProcess, QTimer

        from TerraLab.runtime.supervisor import RuntimeSupervisor

        app = QCoreApplication([])
        runtime = RuntimeSupervisor(app)
        render_ready = 0
        unavailable = False
        retry_succeeded = False

        def kill_render():
            runtime._workers["render"].process.kill()

        def on_ready(role):
            global render_ready, retry_succeeded
            if role != "render":
                return
            render_ready += 1
            if render_ready <= 2:
                QTimer.singleShot(0, kill_render)
                return
            retry_succeeded = True
            runtime.stop(100)
            QTimer.singleShot(500, app.quit)

        def retry_render():
            assert runtime._workers["render"].process.state() == QProcess.NotRunning
            assert runtime.retry("render")

        def on_unavailable(role, _reason):
            global unavailable
            if role != "render":
                return
            unavailable = True
            QTimer.singleShot(0, retry_render)

        runtime.worker_ready.connect(on_ready)
        runtime.worker_unavailable.connect(on_unavailable)
        QTimer.singleShot(15_000, app.quit)
        runtime.start()
        app.exec_()
        runtime.finish_shutdown(500)

        if render_ready != 3 or not unavailable or not retry_succeeded:
            raise SystemExit(
                f"ready={render_ready} unavailable={unavailable} "
                f"retry={retry_succeeded}"
            )
        """
    )
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
