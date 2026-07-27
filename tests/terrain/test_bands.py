from TerraLab.terrain.domain.bands import _fmt, generate_bands


def test_fractional_thousands_have_one_suffix():
    assert _fmt(1_500) == "1.5k"
    assert _fmt(2_500) == "2.5k"
    assert "kk" not in _fmt(1_500)


def test_generated_band_ids_never_repeat_the_suffix():
    bands = generate_bands(20, max_dist_m=150_000.0)
    assert bands
    assert all("kk" not in band["id"] for band in bands)
