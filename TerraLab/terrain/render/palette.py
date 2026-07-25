"""Terrain palettes, color transforms, and layer definitions."""

from __future__ import annotations

import math

import numpy as np
from PyQt5.QtGui import (
    QColor,
)
from scipy.ndimage import (
    distance_transform_edt,
    gaussian_filter,
)

try:
    from numba import njit
except Exception:  # pragma: no cover - optional acceleration
    njit = None

from TerraLab.terrain.render.atmosphere import (
    vibrant_depth_haze_factor,
)
from TerraLab.terrain.domain.bands import generate_bands
from TerraLab.terrain.land_cover.visual_styles import (
    preserve_small_region,
    vibrant_land_cover_rgba,
)
from TerraLab.terrain.representation import (
    TerrainGeometrySource,
    TerrainRepresentationMode,
    normalize_terrain_geometry_source,
    normalize_terrain_representation_mode,
)
from TerraLab.terrain.render.overlay_types import TerrainMaterialSamples
from TerraLab.terrain.visibility_range import (
    TerrainRangeSettings,
    resolve_visibility_range,
)


# ─── Layer definitions ───────────────────────────────────────────

# ─── Layer color palette ─────────────────────────────────────────────────────
#
# Color key-stops for the gradient, from index 0 (farthest/deepest) to 1 (nearest/ground).
# (night_rgb, day_rgb) tuples. We interpolate between these stops.
#
_PALETTE_STOPS = [
    # t=0.0  Deepest Haze (farthest)
    ((70, 82, 100), (178, 194, 210)),
    # t=0.25 Mid Haze
    ((58, 70, 88), (152, 172, 186)),
    # t=0.50 Mid-range Green-Blue transition
    ((44, 58, 70), (116, 138, 132)),
    # t=0.75 Near hills
    ((28, 42, 48), (82, 108, 86)),
    # t=1.0  Immediate Foreground (nearest)
    ((18, 32, 34), (62, 86, 64)),
]


def _palette_color(t: float):
    """
    Interpolate a (night_QColor, day_QColor) from the gradient palette at position t in [0, 1].
    t=0 → farthest (Haze Blue), t=1 → nearest (Forest Green).
    """
    stops = _PALETTE_STOPS
    n_seg = len(stops) - 1

    seg_t = t * n_seg
    seg_i = int(seg_t)
    seg_f = seg_t - seg_i

    if seg_i >= n_seg:
        seg_i = n_seg - 1
        seg_f = 1.0

    (nr0, ng0, nb0), (dr0, dg0, db0) = stops[seg_i]
    (nr1, ng1, nb1), (dr1, dg1, db1) = stops[seg_i + 1]

    def lerp(a, b, f):
        return int(a + (b - a) * f)

    night_c = QColor(
        lerp(nr0, nr1, seg_f), lerp(ng0, ng1, seg_f), lerp(nb0, nb1, seg_f)
    )
    day_c = QColor(
        lerp(dr0, dr1, seg_f), lerp(dg0, dg1, seg_f), lerp(db0, db1, seg_f)
    )
    return night_c, day_c


def generate_layer_defs(bands: list) -> list:
    """
    Given a list of band dicts (from engine.generate_bands), produce a LAYER_DEFS-compatible
    list of (band_id, night_QColor, day_QColor) tuples.

    Bands are expected in ASCENDING distance order (nearest first from engine),
    but LAYER_DEFS must be in DESCENDING order (farthest drawn first = painter z-order).
    """
    n = len(bands)
    result = []
    # Reverse so we draw farthest first
    for i, band in enumerate(reversed(bands)):
        # Use non-linear mapping (square root) to stretch near-colors (green) further
        # into the distance, as requested by the user.
        t_linear = i / max(n - 1, 1)
        t = math.sqrt(t_linear)
        # We want t=0 = farthest color, t=1 = nearest color
        # After reversing bands, i=0 is the farthest, so t=0 → farthest → correct.
        night_c, day_c = _palette_color(t)
        result.append((band["id"], night_c, day_c))
    return result


# Static sane default for use before a profile is loaded (20 bands)
LAYER_DEFS = generate_layer_defs(
    generate_bands(
        20,
        max_dist_m=resolve_visibility_range(
            TerrainRangeSettings(), 0.0
        ).resolved_radius_m,
    )
)


# Ground fill (solid color below the nearest horizon line)
GROUND_NIGHT = QColor(5, 10, 25)
GROUND_DAY = QColor(64, 82, 64)  # Matches closest band (muted terrain green)
ATMOSPHERIC_HAZE_NIGHT = QColor(36, 48, 68)
ATMOSPHERIC_HAZE_DAY = QColor(138, 166, 184)
EARTH_RADIUS_M = 6_371_000.0


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    """Linear interpolation between two QColors."""
    r = c1.red() + (c2.red() - c1.red()) * t
    g = c1.green() + (c2.green() - c1.green()) * t
    b = c1.blue() + (c2.blue() - c1.blue()) * t
    a = c1.alpha() + (c2.alpha() - c1.alpha()) * t
    return QColor(int(r), int(g), int(b), int(a))


def _with_alpha(color: QColor, alpha: int) -> QColor:
    result = QColor(color)
    result.setAlpha(max(0, min(255, int(alpha))))
    return result


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _atmospheric_haze_color(sky_color: QColor, t_night: float) -> QColor:
    haze_blue = _lerp_color(
        ATMOSPHERIC_HAZE_DAY,
        ATMOSPHERIC_HAZE_NIGHT,
        _clamp01(t_night),
    )
    return _lerp_color(haze_blue, sky_color, 0.22 + 0.18 * _clamp01(t_night))


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    if edge0 == edge1:
        return 1.0 if value >= edge1 else 0.0
    t = _clamp01((float(value) - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


def _lerp_piecewise(value: float, stops) -> float:
    if not stops:
        return 0.0
    if value <= stops[0][0]:
        return float(stops[0][1])
    for (x0, y0), (x1, y1) in zip(stops, stops[1:]):
        if value <= x1:
            t = _smoothstep(x0, x1, value)
            return float(y0 + (y1 - y0) * t)
    return float(stops[-1][1])


def _distance_haze_factor(distance_m: float) -> float:
    return _clamp01(
        _lerp_piecewise(
            max(0.0, float(distance_m or 0.0)),
            (
                (0.0, 0.00),
                (5_000.0, 0.05),
                (15_000.0, 0.24),
                (40_000.0, 0.46),
                (80_000.0, 0.66),
                (150_000.0, 0.78),
            ),
        )
    )


def _distance_haze_factors(distance_m) -> np.ndarray:
    distance = np.maximum(0.0, np.asarray(distance_m, dtype=np.float32))
    stops = (
        (0.0, 0.00),
        (5_000.0, 0.05),
        (15_000.0, 0.24),
        (40_000.0, 0.46),
        (80_000.0, 0.66),
        (150_000.0, 0.78),
    )
    result = np.full(distance.shape, stops[-1][1], dtype=np.float32)
    result = np.where(distance <= stops[0][0], stops[0][1], result)
    for (x0, y0), (x1, y1) in zip(stops, stops[1:]):
        mask = (distance > x0) & (distance <= x1)
        t = np.clip((distance - x0) / max(x1 - x0, 1e-6), 0.0, 1.0)
        t = t * t * (3.0 - 2.0 * t)
        result = np.where(mask, y0 + (y1 - y0) * t, result)
    return np.clip(result, 0.0, 1.0).astype(np.float32)


def _qcolor_from_rgba(value) -> QColor:
    rgba = np.asarray(value, dtype=np.uint8).reshape(-1)
    if rgba.size < 3:
        return QColor(0, 0, 0, 0)
    alpha = int(rgba[3]) if rgba.size >= 4 else 255
    return QColor(int(rgba[0]), int(rgba[1]), int(rgba[2]), alpha)


def _resolve_terrain_render_path(profile):
    """Resolve renderer from explicit representation and geometry provenance."""

    mode = normalize_terrain_representation_mode(
        getattr(profile, "representation_mode", TerrainRepresentationMode.RELIEF)
    )
    source = normalize_terrain_geometry_source(
        getattr(profile, "geometry_source", TerrainGeometrySource.LEGACY_UNKNOWN)
    )
    mesh = getattr(profile, "terrain_mesh", None)
    if mode is TerrainRepresentationMode.PROFILE or source in {
        TerrainGeometrySource.FLAT_FALLBACK,
        TerrainGeometrySource.PROCEDURAL_FALLBACK,
    }:
        return "profile", None, mode, source
    if mesh is None:
        return "profile_preview", None, mode, source
    return "relief", mesh, mode, source


def _sample_cache_value(cache, name: str, default=None):
    return cache.get(name, default) if isinstance(cache, dict) else getattr(cache, name, default)


def _vibrant_categorical_palette(
    materials: TerrainMaterialSamples, cache
) -> TerrainMaterialSamples:
    """Replace known official category colours with the Vibrant palette."""

    legends = tuple(
        _sample_cache_value(cache, "source_legend_ids", ()) or ()
    )
    categorical = (
        np.asarray(materials.valid, dtype=bool)
        & np.asarray(materials.categorical, dtype=bool)
    )
    if not legends or not np.any(categorical):
        return materials

    result_rgba = np.asarray(materials.base_rgba, dtype=np.uint8).copy()
    classes = np.asarray(materials.class_ids, dtype=np.int64)
    sources = np.asarray(materials.source_indices, dtype=np.int16)
    changed = False
    for source_index in np.unique(sources[categorical]):
        index = int(source_index)
        if not (0 <= index < len(legends)):
            continue
        source_mask = categorical & (sources == index)
        for class_id in np.unique(classes[source_mask]):
            color = vibrant_land_cover_rgba(
                str(legends[index] or ""), int(class_id)
            )
            if color is None:
                continue
            result_rgba[
                source_mask & (classes == int(class_id))
            ] = np.asarray(color, dtype=np.uint8)
            changed = True
    if not changed:
        return materials
    return TerrainMaterialSamples(
        result_rgba,
        np.asarray(materials.valid, dtype=bool).copy(),
        classes.copy(),
        np.asarray(materials.categorical, dtype=bool).copy(),
        sources.copy(),
    )


def _protected_categorical_regions(
    materials: TerrainMaterialSamples, cache
) -> np.ndarray:
    """Return original pixels whose semantic identity must stay intact."""

    legends = tuple(
        _sample_cache_value(cache, "source_legend_ids", ()) or ()
    )
    protected = np.zeros(np.asarray(materials.valid).shape, dtype=bool)
    categorical = (
        np.asarray(materials.valid, dtype=bool)
        & np.asarray(materials.categorical, dtype=bool)
    )
    if not legends or not np.any(categorical):
        return protected
    classes = np.asarray(materials.class_ids, dtype=np.int64)
    sources = np.asarray(materials.source_indices, dtype=np.int16)
    for source_index in np.unique(sources[categorical]):
        index = int(source_index)
        if not (0 <= index < len(legends)):
            continue
        source_mask = categorical & (sources == index)
        for class_id in np.unique(classes[source_mask]):
            if preserve_small_region(
                str(legends[index] or ""), int(class_id)
            ):
                protected |= source_mask & (classes == int(class_id))
    return protected


def _apply_categorical_territorial_variation(
    rgba,
    materials: TerrainMaterialSamples,
    world_x,
    world_y,
    valid_mask,
    *,
    strength: float = 1.0,
    luminance_variation: float = 0.045,
    hue_variation: float = 0.018,
    midscale_variation: float = 0.0,
    microscale_variation: float = 0.0,
    altitude_influence: float = 0.0,
    slope_influence: float = 0.0,
    snow_rock_blend: float = 0.0,
    water_shore_variation: float = 0.0,
    light_intensity=None,
    solar_exposure=None,
    elevation_m=None,
    normal_x=None,
    normal_y=None,
    normal_z=None,
    source_legend_ids=(),
    render_scale: float = 1.0,
    include_solar_response: bool = True,
) -> np.ndarray:
    """Build stable multiscale materials from class identity and DEM geometry."""

    image = np.asarray(rgba, dtype=np.uint8)
    valid = np.asarray(valid_mask, dtype=bool)
    categorical = (
        valid
        & np.asarray(materials.valid, dtype=bool)
        & np.asarray(materials.categorical, dtype=bool)
    )
    amount = max(0.0, min(1.0, float(strength)))
    luminance_amount = max(0.0, min(0.12, float(luminance_variation)))
    hue_amount = max(0.0, min(0.08, float(hue_variation)))
    midscale_amount = max(0.0, min(0.08, float(midscale_variation)))
    microscale_amount = max(0.0, min(0.03, float(microscale_variation)))
    altitude_amount = max(0.0, min(0.2, float(altitude_influence)))
    slope_amount = max(0.0, min(0.2, float(slope_influence)))
    snow_amount = max(0.0, min(0.75, float(snow_rock_blend)))
    water_amount = max(0.0, min(0.3, float(water_shore_variation)))
    if (
        image.shape[:2] != categorical.shape
        or image.shape[-1:] != (4,)
        or amount <= 0.0
        or not np.any(categorical)
    ):
        return image.copy()

    east = np.broadcast_to(
        np.asarray(world_x, dtype=np.float64), categorical.shape
    )
    north = np.broadcast_to(
        np.asarray(world_y, dtype=np.float64), categorical.shape
    )
    phase = (
        np.asarray(materials.class_ids, dtype=np.float64) % 37.0
    ) * 0.31
    territorial = (
        0.5
        + 0.25 * np.sin((east + north * 0.37) / 3_800.0 + phase)
        + 0.25 * np.cos((north - east * 0.21) / 9_100.0 - phase * 0.6)
    )
    territorial = np.clip(territorial, 0.0, 1.0)
    luminance_wave = territorial * 2.0 - 1.0
    chromatic_wave = (
        0.58
        * np.sin((east * 0.42 - north) / 6_700.0 - phase * 0.7)
        + 0.42
        * np.cos((east + north * 0.63) / 12_400.0 + phase * 0.4)
    )
    chromatic_wave = np.clip(chromatic_wave, -1.0, 1.0)
    if midscale_amount > 0.0:
        medium_wave = (
            0.55
            * np.sin((east * 0.76 + north * 0.31) / 620.0 + phase * 1.7)
            + 0.45
            * np.cos((north - east * 0.44) / 1_350.0 - phase * 1.1)
        )
        medium_wave = np.clip(medium_wave, -1.0, 1.0)
    else:
        medium_wave = np.zeros(categorical.shape, dtype=np.float32)
    if microscale_amount > 0.0:
        micro_wave = (
            0.57
            * np.sin((east - north * 0.58) / 145.0 + phase * 2.3)
            + 0.43
            * np.cos((north + east * 0.35) / 280.0 - phase * 1.9)
        )
        micro_wave = np.clip(micro_wave, -1.0, 1.0)
    else:
        micro_wave = np.zeros(categorical.shape, dtype=np.float32)
    material_wave = (
        luminance_amount * luminance_wave
        + midscale_amount * medium_wave
        + microscale_amount * micro_wave
    )
    factor = 1.0 + amount * material_wave

    result = image.copy()
    rgb = np.asarray(image[..., :3], dtype=np.float32) / 255.0
    varied = rgb * factor[..., None]
    channel_max = np.max(rgb, axis=-1)
    channel_min = np.min(rgb, axis=-1)
    saturation = np.divide(
        channel_max - channel_min,
        np.maximum(channel_max, 1e-6),
        out=np.zeros(categorical.shape, dtype=np.float32),
        where=channel_max > 1e-6,
    )
    dominant = np.argmax(rgb, axis=-1)
    green = (dominant == 1) & (saturation > 0.16)
    blue = (dominant == 2) & (saturation > 0.16)
    warm = (
        (dominant == 0)
        & (rgb[..., 1] > rgb[..., 2] * 1.06)
        & (saturation > 0.14)
    )
    hue_shift = amount * hue_amount * chromatic_wave
    # Green territories drift between fresh/cool and dry/warm; water varies
    # between deeper blue and atmospheric blue-green; earth gains a restrained
    # ochre/sienna oscillation.
    varied[..., 0] += hue_shift * 0.52 * green
    varied[..., 1] += hue_shift * 0.06 * green
    varied[..., 2] -= hue_shift * 0.28 * green
    varied[..., 0] -= hue_shift * 0.22 * blue
    varied[..., 1] += hue_shift * 0.30 * blue
    varied[..., 2] += hue_shift * 0.20 * blue
    varied[..., 0] += hue_shift * 0.30 * warm
    varied[..., 1] += hue_shift * 0.16 * warm
    varied[..., 2] -= hue_shift * 0.24 * warm

    if include_solar_response and solar_exposure is not None:
        exposure = np.clip(
            np.broadcast_to(
                np.asarray(solar_exposure, dtype=np.float32),
                categorical.shape,
            ),
            0.0,
            1.0,
        ) * amount
    elif include_solar_response and light_intensity is not None:
        light = np.broadcast_to(
            np.asarray(light_intensity, dtype=np.float32),
            categorical.shape,
        )
        exposure = np.clip((light - 1.02) / 0.27, 0.0, 1.0) * amount
        luminance = np.sum(
            varied
            * np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32),
            axis=-1,
        )
        varied = luminance[..., None] + (
            varied - luminance[..., None]
        ) * (1.0 - 0.025 * exposure[..., None])
        varied *= 1.0 + 0.008 * exposure[..., None]
    else:
        exposure = np.zeros(categorical.shape, dtype=np.float32)

    physical_detail = (
        midscale_amount
        + microscale_amount
        + altitude_amount
        + slope_amount
        + snow_amount
        + water_amount
    )
    if physical_detail <= 0.0:
        result_rgb = result[..., :3]
        result_rgb[categorical] = np.clip(
            np.rint(varied[categorical] * 255.0),
            0.0,
            255.0,
        ).astype(np.uint8)
        return result

    classes = np.asarray(materials.class_ids, dtype=np.int64)
    sources = np.asarray(materials.source_indices, dtype=np.int16)
    legends = tuple(source_legend_ids or ())
    if legends:
        s2glc = np.zeros(categorical.shape, dtype=bool)
        for source_index, legend_id in enumerate(legends):
            normalized = (
                str(legend_id or "").strip().lower().replace("-", "_")
            )
            if normalized in {
                "s2glc",
                "s2glc_2017",
                "s2glc_europe_2017",
            }:
                s2glc |= categorical & (sources == source_index)
    else:
        s2glc = categorical.copy()

    def _geometry_channel(value, default):
        if value is None:
            return np.full(categorical.shape, default, dtype=np.float32)
        return np.broadcast_to(
            np.asarray(value, dtype=np.float32), categorical.shape
        )

    elevation = _geometry_channel(elevation_m, 0.0)
    nx = _geometry_channel(normal_x, 0.0)
    ny = _geometry_channel(normal_y, 0.0)
    nz = np.clip(_geometry_channel(normal_z, 1.0), -1.0, 1.0)
    horizontal_normal = np.hypot(nx, ny)
    normal_length = np.maximum(
        np.sqrt(horizontal_normal * horizontal_normal + nz * nz), 1e-5
    )
    steepness = np.clip(horizontal_normal / normal_length, 0.0, 1.0)
    northness = np.divide(
        ny,
        np.maximum(horizontal_normal, 1e-5),
        out=np.zeros(categorical.shape, dtype=np.float32),
        where=horizontal_normal > 1e-5,
    )
    altitude = np.full(categorical.shape, 0.5, dtype=np.float32)
    elevation_valid = categorical & np.isfinite(elevation)
    if np.count_nonzero(elevation_valid) >= 2:
        low, high = np.nanpercentile(
            elevation[elevation_valid], (10.0, 90.0)
        )
        if float(high) > float(low) + 1e-3:
            altitude = np.clip(
                (elevation - float(low)) / float(high - low), 0.0, 1.0
            ).astype(np.float32)

    forest = s2glc & np.isin(classes, (82, 83))
    conifers = s2glc & (classes == 83)
    herbaceous = s2glc & (classes == 102)
    rock = s2glc & (classes == 121)
    snow = s2glc & (classes == 123)
    water = s2glc & (classes == 162)

    # Forest canopies gain broad density changes, cool shaded north faces and
    # subtle sunlit clearings without introducing pixel-scale grain.
    forest_density = (
        midscale_amount * medium_wave
        + microscale_amount * micro_wave
    ) * amount
    forest_cold = (
        np.clip(northness, 0.0, 1.0) * steepness * slope_amount * amount
    )
    varied *= 1.0 + (forest_density * forest)[..., None]
    varied[..., 0] -= 0.16 * forest_cold * forest
    varied[..., 1] -= 0.06 * forest_cold * forest
    varied[..., 2] += 0.10 * forest_cold * forest
    if include_solar_response:
        varied *= 1.0 - (
            0.8
            * (midscale_amount + microscale_amount)
            * conifers
            * (0.45 + 0.55 * (1.0 - exposure))
        )[..., None]

    # Grass alternates between fresh, sheltered greens and dry sun-facing
    # yellow-greens, using DEM orientation rather than arbitrary patches.
    if include_solar_response:
        grass_dry = (
            exposure * (0.45 + 0.55 * steepness) * slope_amount * amount
        )
        grass_fresh = (
            np.clip(northness, 0.0, 1.0)
            * (1.0 - exposure)
            * slope_amount
            * amount
        )
        varied[..., 0] += 0.28 * grass_dry * herbaceous
        varied[..., 1] += 0.08 * grass_dry * herbaceous
        varied[..., 2] -= 0.18 * grass_dry * herbaceous
        varied[..., 0] -= 0.10 * grass_fresh * herbaceous
        varied[..., 1] += 0.20 * grass_fresh * herbaceous
        varied[..., 2] += 0.06 * grass_fresh * herbaceous

    # Exposed high rock becomes lighter and slightly less chromatic while
    # retaining warm mineral variation in shade.
    mineral_exposure = (
        0.55 * altitude + 0.45 * steepness
    ) * altitude_amount * amount
    varied *= 1.0 + (0.65 * mineral_exposure * rock)[..., None]
    mineral_luminance = np.sum(
        varied
        * np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32),
        axis=-1,
    )
    rock_desaturation = (0.45 * mineral_exposure * rock)[..., None]
    varied = (
        varied * (1.0 - rock_desaturation)
        + mineral_luminance[..., None] * rock_desaturation
    )

    # Steep, low or strongly insolated snow reveals mineral substrate.  The
    # narrow support transition dirties its visual edge without changing the
    # categorical snow identity used by LOD and hit-testing.
    if snow_amount > 0.0 and np.any(snow):
        snow_support = gaussian_filter(
            snow.astype(np.float32),
            sigma=max(0.6, 1.15 * float(render_scale)),
            mode="nearest",
        )
        snow_edge = snow * np.clip(1.0 - snow_support, 0.0, 1.0)
        steep_snow = np.clip((steepness - 0.42) / 0.48, 0.0, 1.0)
        snow_melt = (
            0.46 * steep_snow
            + 0.24 * (1.0 - altitude)
            + (
                0.18 * exposure
                if include_solar_response
                else 0.0
            )
            + 0.12 * snow_edge
        )
        snow_mix = np.clip(
            snow_amount * amount * snow_melt * snow, 0.0, 0.72
        )[..., None]
        mineral = np.asarray((0.78, 0.745, 0.63), dtype=np.float32)
        varied = varied * (1.0 - snow_mix) + mineral * snow_mix
        cold_snow = (
            snow
            * np.clip(northness, 0.0, 1.0)
            * steepness
            * 0.018
            * amount
        )
        varied[..., 0] -= cold_snow
        varied[..., 2] += cold_snow

    # Water receives a shallow bright shoreline, a darker visual centre and
    # a small solar/sky response.  Category geometry remains unchanged.
    if water_amount > 0.0 and np.any(water):
        water_depth = distance_transform_edt(water)
        shoreline = np.exp(
            -np.maximum(water_depth - 1.0, 0.0)
            / max(1.0, 3.5 * float(render_scale))
        ).astype(np.float32)
        centre = np.clip(1.0 - shoreline, 0.0, 1.0)
        varied *= 1.0 - (
            water * centre * water_amount * 0.72
        )[..., None]
        sky_water = np.asarray((0.48, 0.72, 0.88), dtype=np.float32)
        shore_mix = (
            water * shoreline * water_amount * 0.62
        )[..., None]
        varied = varied * (1.0 - shore_mix) + sky_water * shore_mix
        if include_solar_response:
            varied += (
                water * exposure * water_amount * 0.16
            )[..., None]

    result_rgb = result[..., :3]
    result_rgb[categorical] = np.clip(
        np.rint(varied[categorical] * 255.0),
        0.0,
        255.0,
    ).astype(np.uint8)
    return result


def _apply_categorical_solar_response(
    rgba,
    materials: TerrainMaterialSamples,
    solar_exposure,
    valid_mask,
    *,
    strength: float,
    midscale_variation: float,
    microscale_variation: float,
    slope_influence: float,
    snow_rock_blend: float,
    water_shore_variation: float,
    normal_x=None,
    normal_y=None,
    normal_z=None,
    source_legend_ids=(),
) -> np.ndarray:
    """Apply only time-dependent material responses to cached base colours."""

    image = np.asarray(rgba, dtype=np.uint8)
    valid = np.asarray(valid_mask, dtype=bool)
    categorical = (
        valid
        & np.asarray(materials.valid, dtype=bool)
        & np.asarray(materials.categorical, dtype=bool)
    )
    if (
        image.shape[:2] != categorical.shape
        or image.shape[-1:] != (4,)
        or not np.any(categorical)
    ):
        return image.copy()

    amount = max(0.0, min(1.0, float(strength)))
    midscale_amount = max(
        0.0, min(0.08, float(midscale_variation))
    )
    microscale_amount = max(
        0.0, min(0.03, float(microscale_variation))
    )
    slope_amount = max(0.0, min(0.2, float(slope_influence)))
    snow_amount = max(0.0, min(0.75, float(snow_rock_blend)))
    water_amount = max(
        0.0, min(0.3, float(water_shore_variation))
    )
    exposure = np.clip(
        np.broadcast_to(
            np.asarray(solar_exposure, dtype=np.float32),
            categorical.shape,
        ),
        0.0,
        1.0,
    ) * amount

    classes = np.asarray(materials.class_ids, dtype=np.int64)
    sources = np.asarray(materials.source_indices, dtype=np.int16)
    legends = tuple(source_legend_ids or ())
    if legends:
        s2glc = np.zeros(categorical.shape, dtype=bool)
        for source_index, legend_id in enumerate(legends):
            normalized = (
                str(legend_id or "").strip().lower().replace("-", "_")
            )
            if normalized in {
                "s2glc",
                "s2glc_2017",
                "s2glc_europe_2017",
            }:
                s2glc |= categorical & (sources == source_index)
    else:
        s2glc = categorical.copy()

    def _normal_channel(value, default):
        if value is None:
            return np.full(categorical.shape, default, dtype=np.float32)
        return np.broadcast_to(
            np.asarray(value, dtype=np.float32), categorical.shape
        )

    nx = _normal_channel(normal_x, 0.0)
    ny = _normal_channel(normal_y, 0.0)
    nz = np.clip(_normal_channel(normal_z, 1.0), -1.0, 1.0)
    horizontal = np.hypot(nx, ny)
    normal_length = np.maximum(
        np.sqrt(horizontal * horizontal + nz * nz), 1e-5
    )
    steepness = np.clip(horizontal / normal_length, 0.0, 1.0)
    northness = np.divide(
        ny,
        np.maximum(horizontal, 1e-5),
        out=np.zeros(categorical.shape, dtype=np.float32),
        where=horizontal > 1e-5,
    )

    conifers = s2glc & (classes == 83)
    herbaceous = s2glc & (classes == 102)
    snow = s2glc & (classes == 123)
    water = s2glc & (classes == 162)
    result = image.copy()
    varied = np.asarray(image[..., :3], dtype=np.float32) / 255.0

    canopy_response = (
        0.8
        * (midscale_amount + microscale_amount)
        * conifers
        * (0.45 + 0.55 * (1.0 - exposure))
    )
    varied *= 1.0 - canopy_response[..., None]

    grass_dry = (
        exposure * (0.45 + 0.55 * steepness) * slope_amount * amount
    )
    grass_fresh = (
        np.clip(northness, 0.0, 1.0)
        * (1.0 - exposure)
        * slope_amount
        * amount
    )
    varied[..., 0] += 0.28 * grass_dry * herbaceous
    varied[..., 1] += 0.08 * grass_dry * herbaceous
    varied[..., 2] -= 0.18 * grass_dry * herbaceous
    varied[..., 0] -= 0.10 * grass_fresh * herbaceous
    varied[..., 1] += 0.20 * grass_fresh * herbaceous
    varied[..., 2] += 0.06 * grass_fresh * herbaceous

    if snow_amount > 0.0 and np.any(snow):
        solar_snow_mix = np.clip(
            snow_amount * amount * 0.18 * exposure * snow,
            0.0,
            0.24,
        )[..., None]
        mineral = np.asarray((0.78, 0.745, 0.63), dtype=np.float32)
        varied = (
            varied * (1.0 - solar_snow_mix)
            + mineral * solar_snow_mix
        )
    if water_amount > 0.0 and np.any(water):
        varied += (
            water * exposure * water_amount * 0.16
        )[..., None]

    result_rgb = result[..., :3]
    result_rgb[categorical] = np.clip(
        np.rint(varied[categorical] * 255.0),
        0.0,
        255.0,
    ).astype(np.uint8)
    return result


def _vibrant_relief_occlusion(
    elevation_m,
    normal_z,
    valid_mask,
    *,
    radius_px: float = 5.0,
    relief_scale_m: float = 42.0,
) -> np.ndarray:
    """Approximate broad DEM occlusion in valleys and terrain folds."""

    valid = np.asarray(valid_mask, dtype=bool)
    elevation = np.broadcast_to(
        np.asarray(elevation_m, dtype=np.float32), valid.shape
    )
    nz = np.clip(
        np.broadcast_to(np.asarray(normal_z, dtype=np.float32), valid.shape),
        0.0,
        1.0,
    )
    radius = max(0.0, float(radius_px))
    if radius < 0.5 or not np.any(valid):
        return np.zeros(valid.shape, dtype=np.float32)

    weights = gaussian_filter(
        valid.astype(np.float32),
        sigma=radius,
        mode="constant",
        cval=0.0,
        truncate=2.5,
    )
    local_mean = gaussian_filter(
        np.where(valid, elevation, 0.0),
        sigma=radius,
        mode="constant",
        cval=0.0,
        truncate=2.5,
    )
    local_mean = np.divide(
        local_mean,
        np.maximum(weights, 1e-4),
        out=elevation.copy(),
        where=weights > 1e-4,
    )
    depression = np.maximum(local_mean - elevation, 0.0)
    relief_scale = max(1.0, float(relief_scale_m))
    occlusion = np.clip(depression / relief_scale, 0.0, 1.0)
    occlusion = occlusion * occlusion * (3.0 - 2.0 * occlusion)
    fold_weight = 0.42 + 0.58 * np.sqrt(np.clip(1.0 - nz, 0.0, 1.0))
    return np.where(valid, occlusion * fold_weight, 0.0).astype(np.float32)


def _apply_vibrant_ambient_occlusion(
    rgba,
    occlusion,
    valid_mask,
    *,
    strength: float = 0.12,
) -> np.ndarray:
    """Apply subtle colour-preserving occlusion to Vibrant terrain only."""

    image = np.asarray(rgba, dtype=np.uint8)
    valid = np.asarray(valid_mask, dtype=bool)
    ao = np.clip(
        np.broadcast_to(np.asarray(occlusion, dtype=np.float32), valid.shape),
        0.0,
        1.0,
    )
    amount = max(0.0, min(0.4, float(strength)))
    if (
        image.shape[:2] != valid.shape
        or image.shape[-1:] != (4,)
        or amount <= 0.0
        or not np.any(valid & (ao > 0.0))
    ):
        return image.copy()

    result = image.copy()
    rgb = np.asarray(image[..., :3], dtype=np.float32) / 255.0
    shaded = rgb * (1.0 - amount * ao[..., None])
    result[..., :3][valid] = np.clip(
        np.rint(shaded[valid] * 255.0), 0.0, 255.0
    ).astype(np.uint8)
    return result


def _vibrant_valley_haze(
    occlusion,
    distance_m,
    valid_mask,
    *,
    maximum_distance_m: float,
    strength: float = 0.10,
) -> np.ndarray:
    """Return a restrained second atmospheric layer for distant hollows."""

    valid = np.asarray(valid_mask, dtype=bool)
    ao = np.clip(
        np.broadcast_to(np.asarray(occlusion, dtype=np.float32), valid.shape),
        0.0,
        1.0,
    )
    distance = np.maximum(
        np.broadcast_to(
            np.asarray(distance_m, dtype=np.float32), valid.shape
        ),
        0.0,
    )
    depth = np.sqrt(
        np.clip(distance / max(1.0, float(maximum_distance_m)), 0.0, 1.0)
    )
    amount = max(0.0, min(0.4, float(strength)))
    return np.where(
        valid,
        np.clip(ao * (0.25 + 0.75 * depth) * amount, 0.0, 1.0),
        0.0,
    ).astype(np.float32)


def _apply_vibrant_bloom(
    rgba,
    valid_mask,
    settings,
    *,
    render_scale: float = 1.0,
    light_intensity=None,
    distance_m=None,
    maximum_distance_m: float | None = None,
    daylight_factor: float = 1.0,
    moonlight_factor: float = 0.0,
) -> np.ndarray:
    """Add a selective, low-opacity additive glow to surface highlights."""

    image = np.asarray(rgba, dtype=np.uint8)
    valid = np.asarray(valid_mask, dtype=bool)
    if image.shape[:2] != valid.shape or image.shape[-1:] != (4,):
        raise ValueError("Vibrant bloom mask must match the RGBA image")
    strength = (
        float(settings.vibrant_bloom_strength)
        * float(settings.vibrant_intensity)
        * (
            max(0.0, min(1.0, float(daylight_factor)))
            + float(settings.vibrant_moon_bloom_scale)
            * max(0.0, min(1.0, float(moonlight_factor)))
        )
    )
    radius = float(settings.vibrant_bloom_radius_px) * max(
        0.1, float(render_scale)
    )
    if strength <= 0.0 or radius < 0.25 or not np.any(valid):
        return image.copy()

    rgb = np.asarray(image[..., :3], dtype=np.float32) / 255.0
    luminance = np.sum(
        rgb
        * np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32),
        axis=-1,
    )
    base_threshold = max(
        0.0, min(0.999, float(settings.vibrant_bloom_threshold))
    )
    sunlight = np.zeros(valid.shape, dtype=np.float32)
    if light_intensity is not None:
        light = np.broadcast_to(
            np.asarray(light_intensity, dtype=np.float32), valid.shape
        )
        sunlight = np.clip((light - 0.98) / 0.33, 0.0, 1.0)
    distance_haze = np.zeros(valid.shape, dtype=np.float32)
    if distance_m is not None:
        distance_haze = np.broadcast_to(
            vibrant_depth_haze_factor(
                distance_m,
                settings,
                maximum_distance_m=maximum_distance_m,
            ),
            valid.shape,
        ).astype(np.float32)
    threshold = np.clip(
        base_threshold - 0.022 * sunlight - 0.012 * distance_haze,
        0.0,
        0.999,
    )
    bright = np.clip(
        (luminance - threshold) / np.maximum(1e-6, 1.0 - threshold),
        0.0,
        1.0,
    )
    bright = bright * bright * (3.0 - 2.0 * bright)
    if light_intensity is not None:
        bright *= 0.78 + 0.22 * sunlight
    bright *= valid
    if not np.any(bright > 0.0):
        return image.copy()

    glow_source = rgb * bright[..., None]
    blurred = gaussian_filter(
        glow_source,
        sigma=(radius, radius, 0.0),
        mode="constant",
        cval=0.0,
        truncate=2.5,
    )
    support = gaussian_filter(
        valid.astype(np.float32),
        sigma=radius,
        mode="constant",
        cval=0.0,
        truncate=2.5,
    )
    blurred = np.divide(
        blurred,
        np.maximum(support[..., None], 1e-4),
        out=np.zeros_like(blurred),
        where=support[..., None] > 1e-4,
    )
    result = image.copy()
    result_rgb = result[..., :3]
    glow = np.clip(blurred * strength, 0.0, 1.0)
    composed = rgb + glow
    result_rgb[valid] = np.clip(
        np.rint(composed[valid] * 255.0),
        0.0,
        255.0,
    ).astype(np.uint8)
    return result


def _surface_cache_has_categorical_material(cache) -> bool:
    """Return whether a prepared surface cache contains categorical material."""

    for prefix in ("visual", "relief", "near_patch"):
        categorical = _sample_cache_value(cache, f"{prefix}_categorical")
        valid = _sample_cache_value(cache, f"{prefix}_valid")
        if categorical is None:
            continue
        categorical = np.asarray(categorical, dtype=bool)
        if valid is not None and np.shape(valid) == categorical.shape:
            categorical = categorical & np.asarray(valid, dtype=bool)
        if np.any(categorical):
            return True
    return False


def _solar_shading_strength(sun_alt: float) -> float:
    alt = float(sun_alt if sun_alt is not None else -90.0)
    twilight_side_light = 0.34 * _smoothstep(-12.0, 0.0, alt)
    daylight = 0.66 * _smoothstep(0.0, 22.0, alt)
    return _clamp01(twilight_side_light + daylight)


def _terrain_direct_strength(sun_alt: float) -> float:
    alt = float(sun_alt if sun_alt is not None else -90.0)
    if alt <= -6.0:
        return 0.0
    twilight = 0.10 * _smoothstep(-6.0, 0.0, alt)
    daylight = 0.90 * _smoothstep(0.0, 22.0, alt)
    return _clamp01(twilight + daylight)


def _shade_color(color: QColor, factor: float, sky_color: QColor | None = None) -> QColor:
    factor = max(0.55, min(1.35, float(factor)))
    if factor < 1.0:
        result = QColor(
            int(color.red() * factor),
            int(color.green() * factor),
            int(color.blue() * factor),
            color.alpha(),
        )
        return result

    target = sky_color if sky_color is not None else QColor(255, 255, 255)
    return _lerp_color(color, target, min(0.32, (factor - 1.0) * 1.35))


def _parse_band_max_from_id(band_id: str) -> float:
    """
    Extreu la distància màxima en metres de l'ID d'una banda generada per generate_bands().

    Format esperat:  zone_minFmt_maxFmt
    Exemples:
        'gnd_0_71'       → 71.0
        'near_144_208'   → 208.0
        'mid_1.5k_2k'    → 2000.0
        'far_25k_38k'    → 38000.0
        'haze_111k_150k' → 150000.0
    Retorna 9999.0 si no es pot parsejar.
    """

    def _dist_str_to_m(s: str) -> float:
        s = s.strip()
        try:
            if "k" in s:
                return float(s.replace("k", "")) * 1000.0
            return float(s)
        except ValueError:
            return 9999.0

    parts = band_id.rsplit(
        "_", 2
    )  # zona + min + max (el max és l'últim segment)
    if len(parts) >= 3:
        return _dist_str_to_m(parts[1]), _dist_str_to_m(parts[2])
    return 0.0, 9999.0


# ─── Band data wrapper ───────────────────────────────────────────


class _BandPoints:
    """Holds (az_deg, elev_deg) points for one profile band."""

    VOID_THRESHOLD = -80.0  # DEM voids are stored as ≈ -90°

    def __init__(self, profile, band_id, vert_exaggeration=5.0):
        self.band_id = band_id
        # Parsegem min/max de l'ID (ex: "far_25k_38k")
        self.band_min, self.band_max = _parse_band_max_from_id(band_id)
        self.points, self.valid_mask = self._build(
            self._profile_band_points(profile, band_id), profile, vert_exaggeration
        )
        self.surface_points, self.surface_valid_mask = self._build(
            self._profile_band_points(profile, band_id, surface=True),
            profile,
            vert_exaggeration,
        )

    # ── private ──

    @staticmethod
    def _profile_band_points(profile, band_id, *, surface=False):
        """Return NumPy arrays directly, avoiding millions of Python tuples."""

        angle_key = "surface_angles" if surface else "angles"
        azimuths = np.asarray(getattr(profile, "azimuths", ()), dtype=np.float32)
        for band in getattr(profile, "bands", ()) or ():
            if str(band.get("id", "")) != str(band_id) or angle_key not in band:
                continue
            angles = np.asarray(band[angle_key], dtype=np.float32)
            if angles.shape != azimuths.shape:
                break
            elevations = np.where(
                angles <= -np.pi / 2.0,
                -10.0,
                np.rad2deg(angles),
            ).astype(np.float32)
            return azimuths, elevations

        getter_name = "get_band_surface_points" if surface else "get_band_points"
        getter = getattr(profile, getter_name, None)
        return getter(band_id) if callable(getter) else []

    def _build(self, raw, profile, vert_exag):
        if raw is None or len(raw) == 0:
            return (None, None), None

        # Unpack raw data (list of (az, elev))
        # Check if raw is already a numpy array from the engine
        if isinstance(raw, tuple) and len(raw) == 2:
            az = np.asarray(raw[0], dtype=np.float32)
            elev = np.asarray(raw[1], dtype=np.float32)
        elif isinstance(raw, np.ndarray):
            az = raw[:, 0]
            elev = raw[:, 1]
        else:
            az = np.array([pt[0] for pt in raw], dtype=np.float32)
            elev = np.array([pt[1] for pt in raw], dtype=np.float32)
        resolved_mask = getattr(profile, "resolved_mask", None)
        if resolved_mask is not None and len(resolved_mask) == len(az):
            valid_mask = np.asarray(resolved_mask, dtype=bool)
        else:
            valid_mask = np.ones_like(az, dtype=bool)

        # Handle voids
        h = np.where(elev < self.VOID_THRESHOLD, -20.0, elev * vert_exag)

        # Ensure perfect 360-degree closure
        if len(az) > 0 and az[0] == 0 and az[-1] < 360:
            az = np.append(az, 360.0)
            h = np.append(h, h[0])
            valid_mask = np.append(valid_mask, valid_mask[0])

        # Ensure sorting for polygon continuity
        sort_idx = np.argsort(az)
        return (az[sort_idx], h[sort_idx]), valid_mask[sort_idx]


# ─── Main overlay class ──────────────────────────────────────────


