"""2D math helpers used by scene and render modules."""

from __future__ import annotations


def clamp(value: float, minimum: float, maximum: float) -> float:
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def saturate(value: float) -> float:
    return clamp(value, 0.0, 1.0)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def inv_lerp(a: float, b: float, value: float) -> float:
    denom = b - a
    if abs(denom) < 1e-12:
        return 0.0
    return (value - a) / denom
