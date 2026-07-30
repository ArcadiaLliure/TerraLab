"""Pure photometry used while resolving a celestial scene."""

from __future__ import annotations

import math
from typing import Any, Mapping


PLANET_COLORS: dict[str, tuple[int, int, int, int]] = {
    "Mercury": (220, 200, 175, 255),
    "Venus": (255, 245, 210, 255),
    "Mars": (255, 115, 80, 255),
    "Jupiter": (245, 215, 175, 255),
    "Saturn": (235, 205, 135, 255),
    "Uranus": (165, 225, 235, 255),
    "Neptune": (110, 155, 255, 255),
}

PLANET_DEFAULT_MAGNITUDES: dict[str, float] = {
    "Mercury": -0.4,
    "Venus": -4.4,
    "Mars": -1.5,
    "Jupiter": -2.5,
    "Saturn": 0.4,
    "Uranus": 5.7,
    "Neptune": 7.8,
}


def planet_apparent_magnitude(
    name: str, distance_au: float, phase_angle_deg: float = 0.0
) -> float:
    """Return TerraLab's calibrated visual magnitude for a planet."""

    base_magnitude = {
        "Mercury": -0.6,
        "Venus": -4.4,
        "Mars": -0.5,
        "Jupiter": -5.8,
        "Saturn": -4.3,
        "Uranus": -0.7,
        "Neptune": 0.5,
        "Pluto": 6.0,
    }.get(str(name), 0.0)
    return float(
        base_magnitude
        + 5.0 * math.log10(max(1e-9, float(distance_au)))
        + 0.01 * max(0.0, float(phase_angle_deg))
    )


def apparent_planet_magnitude(body: Mapping[str, Any]) -> float:
    """Resolve a catalogue planet magnitude before it reaches a renderer."""

    if "mag" in body:
        return float(body["mag"])
    if "distance_au" in body:
        return planet_apparent_magnitude(
            str(body.get("name", "Planet")),
            float(body["distance_au"]),
            float(body.get("phase_angle_deg", 0.0)),
        )
    return float(PLANET_DEFAULT_MAGNITUDES.get(str(body.get("name")), 0.0))
