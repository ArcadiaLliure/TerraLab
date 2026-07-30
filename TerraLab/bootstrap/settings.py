"""Pure render backend settings with documented precedence."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping


DEFAULT_RENDER_BACKEND = "qpainter"
_BACKEND_ID = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class RenderSettings:
    """The stable backend preference resolved before composition."""

    backend_id: str = DEFAULT_RENDER_BACKEND

    def __post_init__(self) -> None:
        normalized = str(self.backend_id).strip().lower()
        if not _BACKEND_ID.fullmatch(normalized):
            raise ValueError(
                "Render backend IDs must use lowercase letters, digits, "
                "underscores, or hyphens"
            )
        object.__setattr__(self, "backend_id", normalized)


def resolve_render_settings(
    *,
    explicit_backend: str | None = None,
    environment: Mapping[str, str] | None = None,
    user_backend: str | None = None,
) -> RenderSettings:
    """Resolve CLI/test, environment, user preference, then the default."""

    env = os.environ if environment is None else environment
    selected = (
        explicit_backend
        or env.get("TERRALAB_RENDER_BACKEND")
        or user_backend
        or DEFAULT_RENDER_BACKEND
    )
    return RenderSettings(str(selected))
