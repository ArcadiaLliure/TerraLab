"""Capability selection for the reversible Milky-Way and deep-sky slice."""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Mapping


class MilkyWayRenderingPipeline(StrEnum):
    """Implementation selected for the Milky-Way capability only."""

    LEGACY = "legacy"
    SCENE = "scene"


class DeepSkyRenderingPipeline(StrEnum):
    """Implementation selected for the NGC/deep-sky capability only."""

    LEGACY = "legacy"
    SCENE = "scene"


DEFAULT_MILKYWAY_RENDERING_PIPELINE = MilkyWayRenderingPipeline.SCENE
DEFAULT_DEEP_SKY_RENDERING_PIPELINE = DeepSkyRenderingPipeline.SCENE
MILKYWAY_RENDERING_PIPELINE_ENV = "TERRALAB_MILKYWAY_RENDERING_PIPELINE"
DEEP_SKY_RENDERING_PIPELINE_ENV = "TERRALAB_DEEP_SKY_RENDERING_PIPELINE"


def _resolve_pipeline(
    value: str | StrEnum | None,
    *,
    environment: Mapping[str, str] | None,
    env_name: str,
    default: StrEnum,
    enum_type: type[MilkyWayRenderingPipeline]
    | type[DeepSkyRenderingPipeline],
) -> MilkyWayRenderingPipeline | DeepSkyRenderingPipeline:
    raw = value
    if raw is None:
        raw = (os.environ if environment is None else environment).get(
            env_name, default
        )
    try:
        return enum_type(str(raw).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(
            f"Unsupported {env_name} value {raw!r}; use {allowed}."
        ) from exc


def resolve_milkyway_rendering_pipeline(
    value: str | MilkyWayRenderingPipeline | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> MilkyWayRenderingPipeline:
    """Resolve the Milky-Way flag without affecting deep-sky rendering."""

    return MilkyWayRenderingPipeline(
        _resolve_pipeline(
            value,
            environment=environment,
            env_name=MILKYWAY_RENDERING_PIPELINE_ENV,
            default=DEFAULT_MILKYWAY_RENDERING_PIPELINE,
            enum_type=MilkyWayRenderingPipeline,
        )
    )


def resolve_deep_sky_rendering_pipeline(
    value: str | DeepSkyRenderingPipeline | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> DeepSkyRenderingPipeline:
    """Resolve the deep-sky flag without affecting Milky-Way rendering."""

    return DeepSkyRenderingPipeline(
        _resolve_pipeline(
            value,
            environment=environment,
            env_name=DEEP_SKY_RENDERING_PIPELINE_ENV,
            default=DEFAULT_DEEP_SKY_RENDERING_PIPELINE,
            enum_type=DeepSkyRenderingPipeline,
        )
    )
