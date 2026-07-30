"""Capability selection for the reversible Solar-system and trails slice."""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Mapping


class CelestialRenderingPipeline(StrEnum):
    """Implementation selected for the solar-system and trails capabilities."""

    LEGACY = "legacy"
    SCENE = "scene"


DEFAULT_CELESTIAL_RENDERING_PIPELINE = CelestialRenderingPipeline.SCENE
CELESTIAL_RENDERING_PIPELINE_ENV = "TERRALAB_CELESTIAL_RENDERING_PIPELINE"


def resolve_celestial_rendering_pipeline(
    value: str | CelestialRenderingPipeline | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> CelestialRenderingPipeline:
    """Resolve explicit input, a temporary rollback flag, then scene default."""

    raw = value
    if raw is None:
        raw = (os.environ if environment is None else environment).get(
            CELESTIAL_RENDERING_PIPELINE_ENV,
            DEFAULT_CELESTIAL_RENDERING_PIPELINE,
        )
    try:
        return CelestialRenderingPipeline(str(raw).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in CelestialRenderingPipeline)
        raise ValueError(
            f"Unsupported {CELESTIAL_RENDERING_PIPELINE_ENV} value {raw!r}; "
            f"use {allowed}."
        ) from exc
