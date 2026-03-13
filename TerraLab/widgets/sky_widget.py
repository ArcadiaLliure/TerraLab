"""Compatibility shim: UI widget implementation now lives under `TerraLab.ui`."""

from __future__ import annotations

from TerraLab.ui.sky_widget_impl import AstroCanvas, AstronomicalWidget, ScopeIndexWarmWorker

__all__ = ["AstroCanvas", "AstronomicalWidget", "ScopeIndexWarmWorker"]

