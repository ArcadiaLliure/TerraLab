"""Renderer-neutral identities for resources consumed by scene planners."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class StarCatalogResource:
    """Read-only in-process catalogue views owned by the runtime cache.

    The resource deliberately stores the original arrays rather than serialised
    values.  A planner may create slices and derived batches, but must never
    mutate or materialise the complete catalogue merely to render one frame.
    """

    ra_deg: np.ndarray
    dec_deg: np.ndarray
    magnitude: np.ndarray
    bp_rp: np.ndarray | None = None
    version: str = ""
    magnitude_sorted: bool = False

    def __post_init__(self) -> None:
        row_count = len(self.ra_deg)
        if len(self.dec_deg) != row_count or len(self.magnitude) != row_count:
            raise ValueError(
                "Star catalogue arrays must share their row count"
            )
        if self.bp_rp is not None and len(self.bp_rp) != row_count:
            raise ValueError("bp_rp must share the star catalogue row count")
