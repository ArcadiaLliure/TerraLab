"""Capability selection for informational grid, compass, labels and HUD."""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Mapping


class OverlayRenderingPipeline(StrEnum):
    """Complete non-interactive informational-overlay implementation choice."""

    LEGACY = "legacy"
    SCENE = "scene"


DEFAULT_OVERLAY_RENDERING_PIPELINE = OverlayRenderingPipeline.SCENE
OVERLAY_RENDERING_PIPELINE_ENV = "TERRALAB_OVERLAY_RENDERING_PIPELINE"


def resolve_overlay_rendering_pipeline(
    value: str | OverlayRenderingPipeline | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> OverlayRenderingPipeline:
    """Resolve an explicit value, temporary rollback environment, then scene."""

    raw = value
    if raw is None:
        raw = (os.environ if environment is None else environment).get(
            OVERLAY_RENDERING_PIPELINE_ENV,
            DEFAULT_OVERLAY_RENDERING_PIPELINE,
        )
    try:
        return OverlayRenderingPipeline(str(raw).strip().lower())
    except ValueError as exc:
        options = ", ".join(item.value for item in OverlayRenderingPipeline)
        raise ValueError(
            f"Unsupported {OVERLAY_RENDERING_PIPELINE_ENV} value {raw!r}; "
            f"use {options}."
        ) from exc
