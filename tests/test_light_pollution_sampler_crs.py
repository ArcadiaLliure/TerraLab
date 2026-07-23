from types import SimpleNamespace

from rasterio.crs import CRS

from TerraLab.terrain.light_pollution_sampler import LightPollutionSampler


class _FakeSrc:
    def __init__(
        self, *, left, bottom, right, top, width=0, height=0, name="", crs=None
    ) -> None:
        self.bounds = SimpleNamespace(
            left=float(left),
            bottom=float(bottom),
            right=float(right),
            top=float(top),
        )
        self.width = int(width)
        self.height = int(height)
        self.name = str(name)
        self.crs = crs


def test_guess_missing_crs_detects_geographic_bounds() -> None:
    src = _FakeSrc(
        left=-180,
        bottom=-65,
        right=180,
        top=75,
        width=3600,
        height=1400,
        name="world.tif",
    )
    guessed = LightPollutionSampler._guess_missing_crs(src)
    assert guessed.to_epsg() == 4326


def test_guess_missing_crs_detects_web_mercator_world() -> None:
    src = _FakeSrc(
        left=-20037508.34,
        bottom=-20037508.34,
        right=20037508.34,
        top=20037508.34,
        width=40075,
        height=40075,
        name="mercator_world.tif",
    )
    guessed = LightPollutionSampler._guess_missing_crs(src)
    assert guessed.to_epsg() == 3857


def test_guess_missing_crs_detects_dvnl_equal_earth() -> None:
    src = _FakeSrc(
        left=-17243957.9643,
        bottom=-7332277.1789,
        right=17244042.0357,
        top=7982722.8211,
        width=34488,
        height=15315,
        name="light_pollution.tif",
    )
    guessed = LightPollutionSampler._guess_missing_crs(src)
    assert guessed.to_epsg() == 8857


def test_build_runtime_context_reuses_cached_transformers(monkeypatch) -> None:
    sampler = LightPollutionSampler(raster_path=None)
    src = _FakeSrc(
        left=-20037508.34,
        bottom=-20037508.34,
        right=20037508.34,
        top=20037508.34,
        width=4096,
        height=4096,
        name="lp_test.tif",
        crs=CRS.from_epsg(3857),
    )

    calls = {"n": 0}

    class _DummyTransformer:
        def __init__(self, token: int) -> None:
            self.token = int(token)

        def transform(self, x, y):
            return x, y

    def _fake_transformer(_src, _dst):
        calls["n"] += 1
        return _DummyTransformer(calls["n"])

    monkeypatch.setattr(
        "TerraLab.terrain.light_pollution_sampler.DEFAULT_TRANSFORM_SERVICE.transformer",
        _fake_transformer,
    )

    context_1 = sampler._build_runtime_context(src, terrain_crs="EPSG:25831")
    assert calls["n"] == 2
    sampler._update_context_locked(context_1)

    context_2 = sampler._build_runtime_context(src, terrain_crs="EPSG:25831")
    assert calls["n"] == 2
    assert context_2["tr_geo_to_src"] is context_1["tr_geo_to_src"]
    assert context_2["tr_terrain_to_src"] is context_1["tr_terrain_to_src"]
