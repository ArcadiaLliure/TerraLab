"""Light-pollution mode definitions and shared conversions."""

from __future__ import annotations


LP_MODE_BORTLE = "bortle"
LP_MODE_MAGNITUDE = "magnitude"
LP_MODE_AUTOMATIC = "automatic"

LIGHT_POLLUTION_MODES = (
    LP_MODE_BORTLE,
    LP_MODE_MAGNITUDE,
    LP_MODE_AUTOMATIC,
)


def normalize_light_pollution_mode(value, *, legacy_auto=None) -> str:
    """Return a valid mode, including migration from the former binary setting."""
    normalized = str(value or "").strip().lower()
    aliases = {
        "auto": LP_MODE_AUTOMATIC,
        "automatico": LP_MODE_AUTOMATIC,
        "automatic": LP_MODE_AUTOMATIC,
        "manual": LP_MODE_MAGNITUDE,
        "magnitud": LP_MODE_MAGNITUDE,
        "magnitude": LP_MODE_MAGNITUDE,
        "bortle": LP_MODE_BORTLE,
    }
    resolved = aliases.get(normalized, normalized)
    if resolved in LIGHT_POLLUTION_MODES:
        return resolved
    if legacy_auto is not None:
        return LP_MODE_AUTOMATIC if bool(legacy_auto) else LP_MODE_MAGNITUDE
    return LP_MODE_AUTOMATIC


def is_automatic_mode(mode) -> bool:
    return normalize_light_pollution_mode(mode) == LP_MODE_AUTOMATIC


def mode_uses_bortle(mode) -> bool:
    return normalize_light_pollution_mode(mode) != LP_MODE_MAGNITUDE


def clamp_bortle(value) -> float:
    return max(1.0, min(9.0, float(value)))


def bortle_to_magnitude(bortle_class) -> float:
    return 7.6 - 0.5 * (clamp_bortle(bortle_class) - 1.0)


def magnitude_to_bortle(magnitude_limit) -> float:
    return clamp_bortle(1.0 + (7.6 - float(magnitude_limit)) / 0.5)


def resolve_bortle_class(
    mode,
    *,
    automatic_bortle=1.0,
    bortle_value=1.0,
    magnitude_limit=8.0,
    light_pollution_enabled=True,
) -> float:
    """Resolve the Bortle-equivalent value used by graphical effects."""
    if not bool(light_pollution_enabled):
        return 1.0

    resolved_mode = normalize_light_pollution_mode(mode)
    if resolved_mode == LP_MODE_AUTOMATIC:
        return clamp_bortle(automatic_bortle)
    if resolved_mode == LP_MODE_BORTLE:
        return clamp_bortle(bortle_value)
    return magnitude_to_bortle(magnitude_limit)
