"""Shim de compatibilitat: la implementació UI viu a `TerraLab.ui`."""

from __future__ import annotations

from TerraLab.ui.astro_canvas import AstroCanvas
from TerraLab.ui.astronomical_widget import AstronomicalWidget
from TerraLab.ui.sky_widget_impl import (
    ScopeIndexWarmWorker,
)

__all__ = ["AstroCanvas", "AstronomicalWidget", "ScopeIndexWarmWorker"]
