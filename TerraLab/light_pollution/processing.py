"""Raster processing services for the supported DVNL/SQM commands."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.signal import fftconvolve

from TerraLab.light_pollution.calibration import SQMCalibrationModel


ProgressCallback = Callable[[int, int], None]


def preprocess_dvnl(
    input_path: str | Path,
    output_path: str | Path,
    *,
    progress: ProgressCallback | None = None,
) -> None:
    """Normalize DVNL nodata/outliers in bounded raster blocks."""

    with rasterio.open(input_path) as source:
        metadata = source.meta.copy()
        metadata.update(dtype="float32", nodata=np.nan, compress="lzw")
        blocks = list(source.block_windows(1))
        with rasterio.open(output_path, "w", **metadata) as destination:
            for position, (_block_index, window) in enumerate(blocks, start=1):
                data = source.read(1, window=window).astype(np.float32)
                if source.nodata is not None:
                    data[data == source.nodata] = np.nan
                data[data > 1e10] = np.nan
                destination.write(data, 1, window=window)
                if progress is not None:
                    progress(position, len(blocks))


def iter_tile_windows(
    width: int,
    height: int,
    *,
    tile_size: int,
    halo: int,
) -> Iterator[tuple[Window, Window]]:
    """Yield bounded write/read windows with the requested convolution halo."""

    if tile_size <= 0 or halo < 0:
        raise ValueError("tile_size must be positive and halo non-negative")
    for row in range(0, height, tile_size):
        for column in range(0, width, tile_size):
            write_width = min(tile_size, width - column)
            write_height = min(tile_size, height - row)
            write_window = Window(column, row, write_width, write_height)
            read_column = max(0, column - halo)
            read_row = max(0, row - halo)
            read_window = Window(
                read_column,
                read_row,
                min(width, column + write_width + halo) - read_column,
                min(height, row + write_height + halo) - read_row,
            )
            yield write_window, read_window


def convolve_dvnl(
    input_path: str | Path,
    output_path: str | Path,
    kernel: np.ndarray,
    *,
    tile_size: int = 1024,
    progress: ProgressCallback | None = None,
) -> None:
    """Apply a 2-D kernel using bounded overlap/halo raster windows."""

    kernel = np.asarray(kernel, dtype=np.float64)
    if kernel.ndim != 2 or min(kernel.shape) < 1:
        raise ValueError("kernel must be a non-empty two-dimensional array")
    halo_y = kernel.shape[0] // 2
    halo_x = kernel.shape[1] // 2
    if halo_x != halo_y:
        raise ValueError("the current raster service requires a square kernel")
    with rasterio.open(input_path) as source:
        metadata = source.meta.copy()
        metadata.update(dtype="float32", compress="lzw", tiled=True)
        windows = list(
            iter_tile_windows(
                source.width,
                source.height,
                tile_size=tile_size,
                halo=halo_y,
            )
        )
        with rasterio.open(output_path, "w", **metadata) as destination:
            for position, (write_window, read_window) in enumerate(
                windows, start=1
            ):
                data = source.read(1, window=read_window)
                convolved = fftconvolve(
                    np.nan_to_num(data, nan=0.0),
                    kernel,
                    mode="same",
                )
                start_y = int(write_window.row_off - read_window.row_off)
                start_x = int(write_window.col_off - read_window.col_off)
                height = int(write_window.height)
                width = int(write_window.width)
                cropped = convolved[
                    start_y : start_y + height,
                    start_x : start_x + width,
                ]
                destination.write(
                    cropped.astype(np.float32),
                    1,
                    window=write_window,
                )
                if progress is not None:
                    progress(position, len(windows))


def predict_sqm_raster(
    input_path: str | Path,
    model_path: str | Path,
    output_path: str | Path,
    *,
    elevation_m: float = 0.0,
    progress: ProgressCallback | None = None,
) -> None:
    """Predict SQM in bounded blocks while preserving input nodata."""

    model = SQMCalibrationModel().load(model_path)
    with rasterio.open(input_path) as source:
        metadata = source.meta.copy()
        metadata.update(dtype="float32", nodata=np.nan, compress="lzw")
        blocks = list(source.block_windows(1))
        with rasterio.open(output_path, "w", **metadata) as destination:
            for position, (_block_index, window) in enumerate(blocks, start=1):
                aggregated = source.read(1, window=window).astype(np.float32)
                sqm = model.predict(aggregated, float(elevation_m))
                sqm[np.isnan(aggregated)] = np.nan
                destination.write(
                    sqm.astype(np.float32),
                    1,
                    window=window,
                )
                if progress is not None:
                    progress(position, len(blocks))
