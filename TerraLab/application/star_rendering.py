"""Reversible selection of the star-rendering capability."""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Mapping


class StarRenderingPipeline(StrEnum):
    """Implementation selected for the complete stellar vertical slice."""

    LEGACY = "legacy"
    SCENE = "scene"


DEFAULT_STAR_RENDERING_PIPELINE = StarRenderingPipeline.SCENE
_ENVIRONMENT_KEY = "TERRALAB_STAR_RENDERING_PIPELINE"


def resolve_star_rendering_pipeline(
    value: str | StarRenderingPipeline | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> StarRenderingPipeline:
    """Resolve the temporary star capability flag with an explicit fallback."""

    raw_value = value
    if raw_value is None:
        source = os.environ if environment is None else environment
        raw_value = source.get(
            _ENVIRONMENT_KEY, DEFAULT_STAR_RENDERING_PIPELINE
        )
    try:
        return StarRenderingPipeline(str(raw_value).strip().lower())
    except ValueError as exc:
        choices = ", ".join(item.value for item in StarRenderingPipeline)
        raise ValueError(
            f"Unsupported {_ENVIRONMENT_KEY} value {raw_value!r}; use {choices}."
        ) from exc
