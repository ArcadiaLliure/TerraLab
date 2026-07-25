"""SQM calibration command."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from TerraLab.light_pollution.calibration import SQMCalibrationModel


REQUIRED_COLUMNS = ("agg_dvnl", "elevation_m", "sqm")


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    output: Path
    mean_absolute_error: float
    root_mean_squared_error: float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Calibrate DVNL to SQM.")
    parser.add_argument("csv", type=Path)
    parser.add_argument("output", type=Path)
    return parser


def run(args: argparse.Namespace) -> CalibrationResult:
    if not args.csv.is_file():
        raise FileNotFoundError(f"CSV file does not exist: {args.csv}")
    frame = pd.read_csv(args.csv)
    missing = [name for name in REQUIRED_COLUMNS if name not in frame.columns]
    if missing:
        raise ValueError("missing CSV columns: " + ", ".join(missing))
    model = SQMCalibrationModel()
    model.fit(
        frame["agg_dvnl"].values,
        frame["elevation_m"].values,
        frame["sqm"].values,
    )
    prediction = model.predict(
        frame["agg_dvnl"].values,
        frame["elevation_m"].values,
    )
    residual = prediction - frame["sqm"].values
    result = CalibrationResult(
        output=args.output,
        mean_absolute_error=float(np.mean(np.abs(residual))),
        root_mean_squared_error=float(np.sqrt(np.mean(residual**2))),
    )
    model.save(args.output)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"MAE: {result.mean_absolute_error:.3f} mag/arcsec²")
    print(f"RMSE: {result.root_mean_squared_error:.3f} mag/arcsec²")
    print(f"Model saved to {result.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
