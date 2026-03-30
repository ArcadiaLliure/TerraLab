"""Punt d'entrada públic de UI per al widget del cel."""

from __future__ import annotations

from TerraLab.ui.astronomical_widget import AstronomicalWidget as SkyWidget

AstronomicalWidget = SkyWidget

__all__ = ["SkyWidget", "AstronomicalWidget"]
