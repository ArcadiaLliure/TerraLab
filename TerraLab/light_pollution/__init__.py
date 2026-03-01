"""
TerraLab Light Pollution Module

Core utilities for working with DVNL to SQM to m_lim(h) conversions,
including reading/writing raster data, evaluating convolutional kernels,
calibration, and fast local sampling.
"""

from .dvnl_io import read_raster_metadata, read_raster_window_filtered
from .kernels import create_gaussian_kernel, create_power_law_kernel
from .bortle import sqm_to_bortle_class
from .mlim import calculate_mlim, calculate_mlim_from_sqm
from .calibration import SQMCalibrationModel
