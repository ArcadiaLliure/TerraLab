"""Application-owned contract for renderer-provided logical font measurement."""

from __future__ import annotations

from typing import Protocol

from TerraLab.scene.plans.labels import TextMetrics, TextStyle


class FontMetricsPort(Protocol):
    """Measure a resolved logical style without leaking a graphics API to Model."""

    @property
    def revision(self) -> str: ...

    def measure_text(self, text: str, style: TextStyle) -> TextMetrics: ...
