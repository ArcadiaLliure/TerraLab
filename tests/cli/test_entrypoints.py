from __future__ import annotations

import inspect

import pytest

from TerraLab import __main__ as application_cli
from TerraLab.cli import (
    calibrate_sqm,
    dvnl_convolve,
    dvnl_preprocess,
    gaia,
    predict_sqm_raster,
    query_elevation,
)


@pytest.mark.parametrize(
    "module",
    [
        application_cli,
        query_elevation,
        gaia,
        dvnl_preprocess,
        dvnl_convolve,
        predict_sqm_raster,
        calibrate_sqm,
    ],
)
def test_supported_entrypoint_help_does_not_run_work(module):
    with pytest.raises(SystemExit) as exit_info:
        module.main(["--help"])
    assert exit_info.value.code == 0


def test_calibration_missing_columns_is_an_error(tmp_path):
    csv_path = tmp_path / "measurements.csv"
    csv_path.write_text("agg_dvnl,sqm\n1.0,21.0\n", encoding="utf-8")
    args = calibrate_sqm.build_parser().parse_args(
        [str(csv_path), str(tmp_path / "model.joblib")]
    )
    with pytest.raises(ValueError, match="elevation_m"):
        calibrate_sqm.run(args)


def test_desktop_entrypoint_first_shows_every_route_fullscreen():
    source = inspect.getsource(application_cli.run)

    assert "shell.showFullScreen()" in source
    assert "onboarding.showFullScreen()" in source
    assert "main_window.showFullScreen()" in source
    assert "shell.show()" not in source
    assert "onboarding.show()" not in source
    assert "main_window.show()" not in source
