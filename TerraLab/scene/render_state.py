"""Canonical immutable snapshot consumed only inside the Render process."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from TerraLab.light_pollution.modes import LP_MODE_AUTOMATIC
from TerraLab.scene.camera import Camera


@dataclass(frozen=True)
class RenderState:
    """Snapshot immutable de tot l'estat necessari per pintar un frame."""

    # Temps
    ut_hour: float
    day_of_year_utc: int
    year_utc: int

    # Observador
    latitude: float
    longitude: float
    altitude_m: float

    # Càmera
    camera: Camera

    # Dades d'estrelles
    np_ra: np.ndarray
    np_dec: np.ndarray
    np_mag: np.ndarray
    np_r: np.ndarray
    np_g: np.ndarray
    np_b: np.ndarray
    np_bp_rp: np.ndarray

    # Teseles carregades
    loaded_tile_ids: frozenset[str] = field(default_factory=frozenset)

    # Dades externes
    horizon_profile: object | None = None
    ephemeris_snapshot: dict[str, Any] | None = None

    # Condicions
    bortle: int = 1
    mag_limit: float = 8.0
    light_pollution_mode: str = LP_MODE_AUTOMATIC
    naked_eye_cap: float = 8.0
    sun_alt: float = -90.0
    sun_az: float = 0.0

    # Capa i extensió
    layers_enabled: frozenset[str] = field(default_factory=frozenset)
    extras: Mapping[str, Any] = field(default_factory=dict)
    pure_colors: bool = False
    spike_magnitude_threshold: float = 2.0
    interaction_active: bool = False
    star_scale: float = 1.0
    auto_star_scale_multiplier: float = 1.0
    scope_k_fallback: float = 0.2

    # Mode scope
    scope_enabled: bool = False
    scope_center_sky: tuple[float, float] | None = None
    scope_fov_deg: tuple[float, float] = (5.0, 5.0)

    @property
    def ra(self) -> np.ndarray:
        """Stable short alias used by renderers."""
        return self.np_ra

    @property
    def dec(self) -> np.ndarray:
        """Alias de compatibilitat per renderers legacy."""
        return self.np_dec

    @property
    def mag(self) -> np.ndarray:
        """Alias de compatibilitat per renderers legacy."""
        return self.np_mag

    @property
    def bp_rp(self) -> np.ndarray:
        """Alias de compatibilitat per renderers legacy."""
        return self.np_bp_rp

    @property
    def color_r(self) -> np.ndarray:
        """Alias de compatibilitat per renderers legacy."""
        return self.np_r

    @property
    def color_g(self) -> np.ndarray:
        """Alias de compatibilitat per renderers legacy."""
        return self.np_g

    @property
    def color_b(self) -> np.ndarray:
        """Alias de compatibilitat per renderers legacy."""
        return self.np_b

    @property
    def magnitude_limit(self) -> float:
        """Alias de compatibilitat per renderers legacy."""
        return float(self.mag_limit)

    @property
    def day_of_year(self) -> int:
        """Alias de compatibilitat per renderers legacy."""
        return int(self.day_of_year_utc)

    @property
    def year(self) -> int:
        """Alias de compatibilitat per renderers legacy."""
        return int(self.year_utc)

    @property
    def scope_fov_w(self) -> float | None:
        """Alias de compatibilitat per renderers legacy."""
        return float(self.scope_fov_deg[0]) if self.scope_fov_deg else None

    @property
    def scope_fov_h(self) -> float | None:
        """Alias de compatibilitat per renderers legacy."""
        return float(self.scope_fov_deg[1]) if self.scope_fov_deg else None

    @property
    def scope_mask_fn(self):
        """Alias de compatibilitat per renderers legacy."""
        if not isinstance(self.extras, Mapping):
            return None
        return self.extras.get("scope_mask_fn")
