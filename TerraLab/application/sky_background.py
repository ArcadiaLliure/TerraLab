"""Controller-owned selection of the reversible sky-background capability."""

from __future__ import annotations

import os
from enum import Enum
from typing import Mapping


class SkyBackgroundPipeline(str, Enum):
    """The active implementation of the independently reversible capability."""

    LEGACY = "legacy"
    SCENE = "scene"


SKY_BACKGROUND_PIPELINE_ENV = "TERRALAB_SKY_BACKGROUND_PIPELINE"
DEFAULT_SKY_BACKGROUND_PIPELINE = SkyBackgroundPipeline.SCENE


def resolve_sky_background_pipeline(
    value: str | SkyBackgroundPipeline | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> SkyBackgroundPipeline:
    """Resolve explicit input, environment rollback flag, then scene default."""

    selected = value
    if selected is None:
        env = os.environ if environment is None else environment
        selected = env.get(SKY_BACKGROUND_PIPELINE_ENV)
    if selected is None:
        return DEFAULT_SKY_BACKGROUND_PIPELINE
    if isinstance(selected, SkyBackgroundPipeline):
        return selected
    try:
        return SkyBackgroundPipeline(str(selected).strip().lower())
    except ValueError as error:
        accepted = ", ".join(mode.value for mode in SkyBackgroundPipeline)
        raise ValueError(
            f"Sky background pipeline must be one of: {accepted}"
        ) from error
