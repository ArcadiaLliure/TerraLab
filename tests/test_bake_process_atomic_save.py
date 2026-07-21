from __future__ import annotations

import os
import shutil
import uuid
import io
from pathlib import Path

import numpy as np
import pytest

from TerraLab.terrain import bake_process
from TerraLab.terrain.bake_process import _atomic_save_profile
from TerraLab.terrain.engine import HorizonProfile


def _build_profile() -> HorizonProfile:
    az = np.array([0.0, 180.0], dtype=np.float32)
    band = {
        "id": "b0",
        "min": 0.0,
        "max": 150000.0,
        "angles": np.array([0.0, 0.1], dtype=np.float32),
        "dists": np.array([10.0, 20.0], dtype=np.float32),
        "heights": np.array([100.0, 120.0], dtype=np.float32),
    }
    return HorizonProfile(
        azimuths=az,
        bands=[band],
        observer_lat=42.5,
        observer_lon=1.0,
        light_domes=np.array([0.0, 1.0], dtype=np.float32),
        light_peak_distances=np.array([0.0, 2000.0], dtype=np.float32),
        resolved_mask=np.array([True, True], dtype=bool),
    )


def _make_local_tmp_dir() -> Path:
    root = Path.cwd() / ".pytest_local_tmp"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"atomic_save_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_atomic_save_profile_retries_permission_error(monkeypatch):
    profile = _build_profile()
    tmp_dir = _make_local_tmp_dir()
    out_path = tmp_dir / "preview.npz"

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr("TerraLab.terrain.bake_process.os.replace", flaky_replace)
    monkeypatch.setattr("TerraLab.terrain.bake_process.time.sleep", lambda _s: None)

    try:
        _atomic_save_profile(profile, str(out_path))
        assert out_path.exists()
        assert calls["n"] == 3
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_atomic_save_profile_propagates_non_permission_errors(
    monkeypatch
):
    profile = _build_profile()
    tmp_dir = _make_local_tmp_dir()
    out_path = tmp_dir / "preview.npz"

    def bad_replace(_src, _dst):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr("TerraLab.terrain.bake_process.os.replace", bad_replace)
    monkeypatch.setattr("TerraLab.terrain.bake_process.time.sleep", lambda _s: None)

    try:
        with pytest.raises(RuntimeError, match="unexpected failure"):
            _atomic_save_profile(profile, str(out_path))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_preview_snapshots_use_immutable_unique_paths(tmp_path):
    base = tmp_path / "profile_preview.npz"

    first = Path(bake_process._unique_preview_path(str(base), 24))
    second = Path(bake_process._unique_preview_path(str(base), 24))

    assert first.parent == tmp_path
    assert first.name.startswith("profile_preview_00000024_")
    assert first.suffix == ".npz"
    assert first != second


def test_emit_event_uses_fd_stdout_when_pythonw_streams_missing(monkeypatch):
    """Verifica que l'emissor JSON funciona quan stdout/stderr Python no existeixen."""
    json_stream = io.StringIO()
    monkeypatch.setattr(
        bake_process.sys, "__stdout__", None, raising=False
    )
    monkeypatch.setattr(bake_process.sys, "stdout", None, raising=False)
    monkeypatch.setattr(
        bake_process.os, "dup", lambda _fd: 1, raising=True
    )
    monkeypatch.setattr(
        bake_process.os,
        "fdopen",
        lambda _fd, mode, encoding=None, errors=None, buffering=None: json_stream,
        raising=True,
    )
    bake_process._EVENT_STREAM = None
    bake_process._EVENT_STREAM_INIT_FAILED = False

    bake_process._emit_event(
        "progress",
        job_id="job-test",
        phase="bake",
        percent=42.0,
    )

    payload = json_stream.getvalue().strip()
    assert payload
    assert '"type":"progress"' in payload
    assert '"job_id":"job-test"' in payload


class _ProviderWithResolution:
    def __init__(self, resolution_m):
        self.resolution_m = resolution_m

    def get_nominal_resolution_m(self):
        return self.resolution_m


def test_resolve_raycast_step_uses_finer_step_for_5m_dem():
    provider = _ProviderWithResolution(5.0)

    assert bake_process._resolve_raycast_step_m(provider) == 5.0


def test_resolve_raycast_step_keeps_default_for_coarser_dem():
    provider = _ProviderWithResolution(25.0)

    assert bake_process._resolve_raycast_step_m(provider) == 50.0
