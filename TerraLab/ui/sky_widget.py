"""Public UI entrypoint for the sky widget."""

from __future__ import annotations

from TerraLab.ui.sky_widget_impl import AstronomicalWidget as SkyWidget

AstronomicalWidget = SkyWidget

__all__ = ["SkyWidget", "AstronomicalWidget"]
