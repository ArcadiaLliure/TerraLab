"""New small UI entrypoint for sky widget.

This module keeps a stable import path while the migration from
`TerraLab.widgets.sky_widget` is still in progress.
"""

from __future__ import annotations

from TerraLab.widgets.sky_widget import AstronomicalWidget as _LegacyAstronomicalWidget


class SkyWidget(_LegacyAstronomicalWidget):
    pass


AstronomicalWidget = SkyWidget

__all__ = ["SkyWidget", "AstronomicalWidget"]
