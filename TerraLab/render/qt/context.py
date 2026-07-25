"""Qt rendering context passed to painter adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class QtRenderContext:
    painter: Any
    width: int
    height: int
    diagnostics: Any = None


# Concise adapter-facing name; the owner remains explicitly in render.qt.
RenderContext = QtRenderContext

__all__ = ["QtRenderContext", "RenderContext"]
