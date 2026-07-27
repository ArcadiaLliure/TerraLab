import json
from types import SimpleNamespace

from TerraLab.terrain import worker as worker_module
from TerraLab.terrain.worker import HorizonWorker


class _FailingInProcessSampler:
    def __init__(self, raster_path: str) -> None:
        self.raster_path = raster_path

    def estimate_zenith_sqm(self, lat: float, lon: float):
        raise AssertionError("in-process sampler must not be used")


def test_isolated_sampler_route_is_used_when_enabled(monkeypatch, tmp_path) -> None:
    raster_path = tmp_path / "light.tif"
    raster_path.touch()
    worker = HorizonWorker()
    worker.is_initialized = True
    worker.light_sampler = _FailingInProcessSampler(str(raster_path))
    worker._use_isolated_lp_sampler = True

    calls = []

    def fake_isolated(lat: float, lon: float):
        calls.append((lat, lon))
        return 20.25, 5

    monkeypatch.setattr(
        worker,
        "_estimate_light_pollution_isolated",
        fake_isolated,
    )

    assert worker.get_light_pollution_estimate(41.5, 1.25) == (20.25, 5)
    assert calls == [(41.5, 1.25)]


def test_isolated_sampler_parses_result_and_forwards_trace(
    monkeypatch, tmp_path, capsys
) -> None:
    raster_path = tmp_path / "light.tif"
    raster_path.touch()
    worker = HorizonWorker()
    worker.light_sampler = SimpleNamespace(raster_path=str(raster_path))
    payload = json.dumps({"sqm": 19.75, "bortle": 5})

    def fake_run(command, **kwargs):
        assert command[:3] == [
            worker_module.sys.executable,
            "-m",
            "TerraLab.terrain.light_pollution_query",
        ]
        assert kwargs["timeout"] == 30.0
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "[LPSampler:Estimate] source=raster lat=41.500000 "
                "lon=1.250000 sqm=19.750 bortle=5\n"
                f"{worker_module._LP_RESULT_PREFIX}{payload}\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(worker_module.subprocess, "run", fake_run)

    assert worker._estimate_light_pollution_isolated(41.5, 1.25) == (19.75, 5)
    assert "[LPSampler:Estimate]" in capsys.readouterr().out


def test_isolated_sampler_forwards_every_typed_fallback_raster(
    monkeypatch, tmp_path
) -> None:
    first = tmp_path / "first.tif"
    second = tmp_path / "second.tif"
    first.touch()
    second.touch()
    worker = HorizonWorker()
    worker.light_sampler = SimpleNamespace(
        raster_path=str(first),
        raster_paths=(str(first), str(second)),
    )
    payload = json.dumps({"sqm": 20.0, "bortle": 5})

    def fake_run(command, **_kwargs):
        raster_values = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--raster"
        ]
        assert raster_values == [str(first), str(second)]
        return SimpleNamespace(
            returncode=0,
            stdout=f"{worker_module._LP_RESULT_PREFIX}{payload}\n",
            stderr="",
        )

    monkeypatch.setattr(worker_module.subprocess, "run", fake_run)

    assert worker._estimate_light_pollution_isolated(41.5, 1.25) == (20.0, 5)
