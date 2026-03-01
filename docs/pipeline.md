# DVNL → SQM → m_lim(h) Pipeline Documentation

This document explains the technical implementation of the light pollution modeling pipeline in TerraLab.

## Overview

The pipeline converts Day/Night Band (DNB) radiance data from the VIIRS satellite (DVNL) into human-perceivable sky quality metrics:

1. **DVNL Radiance**: Raw light emissions from the ground (nW/cm²/sr).
2. **Convolution**: Aggregation of light using atmospheric scattering kernels.
3. **SQM Prediction**: Mapping aggregated radiance to Zenith Sky Quality (mag/arcsec²).
4. **m_lim(h)**: Calculation of the limiting magnitude at a given altitude.

## 1. Radial Scattering Model

We use two primary kernel types to model how light from a source (a city) scatters into the atmosphere to create the "sky glow" at a distance $r$:

- **Gaussian Kernel**: Better for very short-range scattering (local lamp post effects).
  $$K(r) = A \cdot \exp\left(-\frac{r^2}{2\sigma^2}\right)$$
- **Power-law Kernel**: Better for long-range sky glow (typical urban light domes).
  $$K(r) = A \cdot (1 + r/r_0)^{-\alpha}$$

## 2. Dynamic Sampler Implementation

The `LightPollutionSampler` performs real-time evaluation of the "Sky Quality" for a given coordinate:

- It samples a region of the DVNL raster around the observer.
- It applies the selected kernel to compute a weighted sum of radiance.
- It uses a calibrated model to estimate the Zenith SQM.
- It maps the SQM to the **Bortle Dark-Sky Scale** (1-9).

## 3. UI Integration

- **Dynamic Sky Glow**: The `WeatherSystem` uses theestimated Bortle class to adjust the intensity and color of the night sky's "glow" and the illumination of clouds from below.
- **Horizon Baking**: The `HorizonWorker` calculates light domes in the horizon profile by sampling radiance along each azimuth line.

## 4. Calibration Workflow

To calibrate the model for a new region:

1. Collect ground-truth SQM measurements with GPS coordinates.
2. Use `scripts/dvnl_convolve.py` to process the DVNL raster with multiple kernel parameters ($\sigma$ or $\alpha$).
3. Use `scripts/calibrate_sqm.py` to find the best-fit model between the convolved raster and your measurements.
4. Save the resulting `.joblib` model for use in the application.

## 5. Usage in TerraLab

1. Open **Configuració de Mapes Topogràfics** in the Astronomy widget.
2. Select your `C_DVNL 2022.tif` (or similar) file.
3. The application will automatically update the Bortle estimate as you change your location on the map.
