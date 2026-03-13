"""Render context passed to independent renderers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RenderContext:
    painter: Any
    width: int
    height: int
    diagnostics: Any = None
