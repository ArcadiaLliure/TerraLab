"""Pure terrain material composition and color grading."""

from __future__ import annotations

from typing import Any

import numpy as np

from TerraLab.terrain.render.atmosphere import atmospheric_fog_factor, vibrant_depth_haze_factor
from TerraLab.terrain.render.config import SurfaceVisualStyle, TerrainRenderSettings, normalize_surface_visual_style

def compose_vertex_rgba(
    base_rgba: Any,
    intensity: Any,
    distance_m: Any,
    settings: TerrainRenderSettings,
    *,
    maximum_distance_m: float | None = None,
    horizon_rgb: Any | None = None,
    atmosphere_strength: float = 1.0,
) -> np.ndarray:
    """Apply lighting then atmospheric colour transforms to terrain vertices."""

    rgba = np.asarray(base_rgba)
    if rgba.shape[-1:] != (4,):
        raise ValueError("Terrain base colours must end in RGBA channels")
    rgb = np.asarray(rgba[..., :3], dtype=np.float32)
    alpha = np.asarray(rgba[..., 3:4], dtype=np.float32)
    light = np.broadcast_to(np.asarray(intensity, dtype=np.float32), rgb.shape[:-1])
    if settings.terrain_lighting_enabled:
        rgb = rgb * light[..., None]

    fog = atmospheric_fog_factor(
        distance_m,
        settings,
        maximum_distance_m=maximum_distance_m,
    )
    fog = np.broadcast_to(fog, rgb.shape[:-1]).astype(np.float32)
    fog *= max(0.0, min(1.0, float(atmosphere_strength)))
    if settings.atmospheric_perspective_enabled and np.any(fog > 0.0):
        luminance = np.sum(
            rgb * np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32),
            axis=-1,
            keepdims=True,
        )
        desaturation = (
            fog * float(settings.atmosphere_desaturation_strength)
        )[..., None]
        rgb = rgb * (1.0 - desaturation) + luminance * desaturation

        atmosphere = np.asarray(
            horizon_rgb
            if horizon_rgb is not None
            else settings.atmosphere_horizon_color,
            dtype=np.float32,
        )[:3]
        atmosphere_luminance = float(
            np.dot(atmosphere, np.asarray((0.2126, 0.7152, 0.0722)))
        )
        contrast = (
            1.0 - fog * float(settings.atmosphere_contrast_reduction)
        )[..., None]
        rgb = atmosphere_luminance + (rgb - atmosphere_luminance) * contrast
        atmospheric_daylight = max(
            0.0, min(1.0, atmosphere_luminance / 128.0)
        )
        rgb += (
            fog
            * float(settings.atmosphere_brightness_gain)
            * atmospheric_daylight
            * 255.0
        )[..., None]
        rgb = rgb * (1.0 - fog[..., None]) + atmosphere * fog[..., None]

    return np.concatenate(
        (
            np.clip(np.rint(rgb), 0.0, 255.0),
            np.clip(np.rint(alpha), 0.0, 255.0),
        ),
        axis=-1,
    ).astype(np.uint8)


def apply_vibrant_color_grade(
    rgba: Any,
    intensity: Any,
    distance_m: Any,
    settings: TerrainRenderSettings,
    *,
    maximum_distance_m: float | None = None,
    valid_mask: Any | None = None,
    additional_haze: Any | None = None,
    daylight_factor: Any = 1.0,
    moonlight_factor: Any = 0.0,
    solar_exposure: Any | None = None,
    lunar_exposure: Any | None = None,
    atmosphere_rgb: Any | None = None,
) -> np.ndarray:
    """Apply the naturalistic, non-destructive Vibrant colour pipeline.

    A post-shading 8-10% saturation lift is hue-weighted and bounded by a
    soft shoulder so intense colours do not clip.  Tone, split-lighting and
    distance atmosphere remain independent, and bloom stays in a separate
    screen-space pass because it needs neighbouring pixels.
    """

    source = np.asarray(rgba, dtype=np.uint8)
    if source.shape[-1:] != (4,):
        raise ValueError("Vibrant colour grading expects RGBA channels")
    if normalize_surface_visual_style(settings.surface_visual_style) != (
        SurfaceVisualStyle.VIBRANT.value
    ):
        return source.copy()

    shape = source.shape[:-1]
    mask = (
        np.asarray(source[..., 3] > 0, dtype=bool)
        if valid_mask is None
        else np.broadcast_to(np.asarray(valid_mask, dtype=bool), shape)
    )
    if not np.any(mask):
        return source.copy()

    amount = float(settings.vibrant_intensity)
    rgb = np.asarray(source[..., :3], dtype=np.float32) / 255.0
    working = rgb.copy()
    daylight = np.clip(
        np.broadcast_to(
            np.asarray(daylight_factor, dtype=np.float32), shape
        ),
        0.0,
        1.0,
    )
    moonlight = np.clip(
        np.broadcast_to(
            np.asarray(moonlight_factor, dtype=np.float32), shape
        ),
        0.0,
        1.0,
    )
    light = np.broadcast_to(np.asarray(intensity, dtype=np.float32), shape)
    if solar_exposure is None:
        solar = np.clip((light - 0.98) / 0.31, 0.0, 1.0)
    else:
        solar = np.clip(
            np.broadcast_to(
                np.asarray(solar_exposure, dtype=np.float32), shape
            ),
            0.0,
            1.0,
        )
    if lunar_exposure is None:
        lunar = np.zeros(shape, dtype=np.float32)
    else:
        lunar = np.clip(
            np.broadcast_to(
                np.asarray(lunar_exposure, dtype=np.float32), shape
            ),
            0.0,
            1.0,
        )
    day_amount = amount * daylight

    luma_weights = np.asarray(
        (0.2126, 0.7152, 0.0722), dtype=np.float32
    )

    # A modest toe lift avoids closed black terrain.  The stronger shoulder
    # prevents direct DEM lighting from clipping one colour channel before
    # the chromatic treatment is applied.
    black_lift = float(settings.vibrant_black_lift) * day_amount
    working = (
        black_lift[..., None]
        + (1.0 - black_lift[..., None]) * working
    )
    midtone_lift = float(settings.vibrant_midtone_lift) * day_amount
    tone_luminance = np.sum(working * luma_weights, axis=-1)
    lifted_luminance = tone_luminance + (
        midtone_lift
        * 4.0
        * tone_luminance
        * (1.0 - tone_luminance)
    )
    working *= np.divide(
        lifted_luminance,
        np.maximum(tone_luminance, 1e-5),
        out=np.ones_like(tone_luminance),
        where=tone_luminance > 1e-5,
    )[..., None]
    contrast = float(settings.vibrant_mid_contrast) * day_amount
    working += (
        contrast[..., None]
        * (working - 0.5)
        * 4.0
        * working
        * (1.0 - working)
    )
    shoulder = float(settings.vibrant_highlight_compression) * day_amount
    highlight_excess = np.maximum(working - 0.68, 0.0)
    working -= (
        shoulder[..., None]
        * highlight_excess
        * highlight_excess
        / 0.32
    )
    working = np.clip(working, 0.0, 0.992)

    luminance = np.sum(working * luma_weights, axis=-1)
    channel_max = np.max(working, axis=-1)
    channel_min = np.min(working, axis=-1)
    saturation = np.divide(
        channel_max - channel_min,
        np.maximum(channel_max, 1e-6),
        out=np.zeros(shape, dtype=np.float32),
        where=channel_max > 1e-6,
    )
    dominant = np.argmax(working, axis=-1)
    red, green, blue = (
        working[..., 0],
        working[..., 1],
        working[..., 2],
    )
    warm = (red >= green * 0.90) & (green > blue * 1.08)
    yellow_green = (
        (red > 0.34)
        & (green > 0.34)
        & (blue < np.minimum(red, green) * 0.72)
    )
    grey_brown = (
        (red > green)
        & (green > blue)
        & (saturation < 0.42)
    )

    hue_weight = np.full(shape, 0.90, dtype=np.float32)
    hue_weight[dominant == 1] = 1.0
    hue_weight[dominant == 2] = 0.96
    hue_weight[warm] = 0.82
    hue_weight[luminance < 0.22] *= 0.75
    hue_weight[saturation < 0.08] *= 0.25
    saturation_gain = (
        float(settings.vibrant_saturation_strength) * day_amount
    )
    target_saturation = saturation * (
        1.0 + saturation_gain * hue_weight
    )
    # A soft shoulder is preferable to a hard clamp: already saturated
    # sources retain hue differences, but primary greens and yellows can no
    # longer reach the marker-pen look caused by channel clipping.
    saturation_limit = float(settings.vibrant_saturation_soft_limit)
    saturation_compression = (
        float(settings.vibrant_saturation_compression) * day_amount
    )
    excess = np.maximum(target_saturation - saturation_limit, 0.0)
    target_saturation -= saturation_compression * excess
    high_colour = np.clip(
        (saturation - 0.52) / 0.38, 0.0, 1.0
    )
    target_saturation *= 1.0 - (
        amount
        * high_colour
        * (
            0.045 * yellow_green.astype(np.float32)
            + 0.027
            * ((dominant == 1) & (saturation > 0.58)).astype(np.float32)
        )
    )
    neutral_desaturation = (
        np.clip((0.16 - saturation) / 0.16, 0.0, 1.0)
        * 0.04
        * amount
    )
    shadow_desaturation = (
        np.clip((0.24 - luminance) / 0.24, 0.0, 1.0)
        * 0.035
        * amount
    )
    target_saturation *= 1.0 - np.maximum(
        neutral_desaturation, shadow_desaturation
    )
    chroma_scale = np.divide(
        target_saturation,
        np.maximum(saturation, 1e-5),
        out=np.ones(shape, dtype=np.float32),
        where=saturation > 1e-5,
    )
    working = luminance[..., None] + (
        working - luminance[..., None]
    ) * chroma_scale[..., None]

    # Nudge extreme landscape hues towards neighbouring natural pigments.
    # These offsets are deliberately much smaller than the semantic palette
    # differences and therefore also work safely on orthophotos.
    green_mask = dominant == 1
    blue_mask = dominant == 2
    green_naturalize = (
        green_mask.astype(np.float32)
        * np.clip((saturation - 0.38) / 0.42, 0.0, 1.0)
        * amount
    )
    working[..., 0] += 0.004 * green_naturalize
    working[..., 1] *= 1.0 - 0.002 * green_naturalize
    working[..., 2] += 0.003 * green_naturalize
    working[..., 1] = np.where(
        blue_mask,
        working[..., 1] + 0.005 * working[..., 2] * amount,
        working[..., 1],
    )
    working[..., 2] = np.where(
        blue_mask, working[..., 2] * (1.0 + 0.003 * amount), working[..., 2]
    )
    working[..., 0] = np.where(
        warm, working[..., 0] * (1.0 - 0.003 * amount), working[..., 0]
    )
    working[..., 2] = np.where(
        warm, working[..., 2] + 0.004 * amount, working[..., 2]
    )
    working[..., 0] = np.where(
        grey_brown, working[..., 0] * (1.0 + 0.005 * amount), working[..., 0]
    )
    working[..., 1] = np.where(
        grey_brown, working[..., 1] * (1.0 + 0.003 * amount), working[..., 1]
    )

    # Split-tone grading keeps the local material colour dominant.  Cool
    # shadows and cream-gold highlights are multiplicative and intentionally
    # close to neutral.
    luminance = np.sum(working * luma_weights, axis=-1)
    shadow_weight = np.clip((0.48 - luminance) / 0.48, 0.0, 1.0)
    highlight_weight = np.clip((luminance - 0.52) / 0.48, 0.0, 1.0)
    midtone_weight = np.clip(
        1.0 - shadow_weight - highlight_weight, 0.0, 1.0
    )
    shadow_tint = np.asarray(settings.vibrant_shadow_tint, dtype=np.float32)
    midtone_tint = np.asarray(settings.vibrant_midtones_tint, dtype=np.float32)
    highlight_tint = np.asarray(
        settings.vibrant_highlights_tint, dtype=np.float32
    )
    tint = (
        shadow_weight[..., None] * shadow_tint
        + midtone_weight[..., None] * midtone_tint
        + highlight_weight[..., None] * highlight_tint
    )

    cool_shadow = np.clip((0.88 - light) / 0.42, 0.0, 1.0)
    warm_light = solar * daylight
    tint *= (
        1.0
        + cool_shadow[..., None]
        * np.asarray((-0.008, 0.0, 0.012), dtype=np.float32)
        + warm_light[..., None]
        * np.asarray((0.012, 0.006, -0.006), dtype=np.float32)
    )
    working *= 1.0 + day_amount[..., None] * (tint - 1.0)
    shadow_sky = (
        np.asarray(settings.vibrant_shadow_sky_color, dtype=np.float32)
        / 255.0
    )
    shadow_sky_mix = (
        cool_shadow
        * float(settings.vibrant_shadow_sky_mix)
        * amount
        * daylight
    )[..., None]
    working = (
        working * (1.0 - shadow_sky_mix)
        + shadow_sky * shadow_sky_mix
    )
    working += (
        warm_light[..., None]
        * np.asarray((0.013, 0.012, 0.009), dtype=np.float32)
        * amount
    )
    sun_tint = np.asarray(settings.vibrant_sun_tint, dtype=np.float32)
    moon_tint = np.asarray(settings.vibrant_moon_tint, dtype=np.float32)
    working *= (
        1.0
        + amount
        * warm_light[..., None]
        * (sun_tint - 1.0)
    )
    working *= (
        1.0
        + amount
        * lunar[..., None]
        * (moon_tint - 1.0)
    )

    # At night the categorical or photographic source remains identifiable,
    # but most chroma disappears.  A high full moon restores only part of it.
    luminance = np.sum(working * luma_weights, axis=-1)
    night_chroma = (
        float(settings.vibrant_night_chroma)
        + (
            float(settings.vibrant_full_moon_chroma)
            - float(settings.vibrant_night_chroma)
        )
        * moonlight
    )
    chroma_retention = daylight + (1.0 - daylight) * night_chroma
    working = luminance[..., None] + (
        working - luminance[..., None]
    ) * chroma_retention[..., None]

    # Vibrant uses a configurable linear depth interpolation by default:
    # distance / maximum_distance.  This reproduces the pale blue-grey
    # mountain layering of the art direction without altering Original.
    fog = vibrant_depth_haze_factor(
        distance_m,
        settings,
        maximum_distance_m=maximum_distance_m,
    )
    fog = np.broadcast_to(fog, shape).astype(np.float32)
    if additional_haze is not None:
        local_haze = np.clip(
            np.broadcast_to(
                np.asarray(additional_haze, dtype=np.float32), shape
            ),
            0.0,
            1.0,
        )
        fog = np.clip(fog + local_haze * (1.0 - fog), 0.0, 1.0)
    distance_amount = fog * amount
    luminance = np.sum(working * luma_weights, axis=-1)
    distance_chroma = (
        distance_amount * float(settings.vibrant_distance_desaturation)
    )
    working = luminance[..., None] + (
        working - luminance[..., None]
    ) * (1.0 - distance_chroma[..., None])
    day_atmosphere = (
        np.asarray(settings.vibrant_atmosphere_color, dtype=np.float32)
        / 255.0
    )
    if atmosphere_rgb is None:
        atmospheric_rgb = np.broadcast_to(day_atmosphere, shape + (3,))
    else:
        night_atmosphere = np.asarray(atmosphere_rgb, dtype=np.float32)
        if np.nanmax(night_atmosphere) > 2.0:
            night_atmosphere = night_atmosphere / 255.0
        night_atmosphere = np.broadcast_to(
            night_atmosphere, shape + (3,)
        )
        atmospheric_rgb = (
            night_atmosphere * (1.0 - daylight[..., None])
            + day_atmosphere * daylight[..., None]
        )
    atmospheric_luminance = np.sum(
        atmospheric_rgb * luma_weights, axis=-1
    )
    distance_contrast = 1.0 - (
        distance_amount
        * float(settings.vibrant_distance_contrast_reduction)
    )
    working = atmospheric_luminance[..., None] + (
        working - atmospheric_luminance[..., None]
    ) * distance_contrast[..., None]
    distance_brightness = (
        distance_amount
        * float(settings.vibrant_distance_brightness_gain)
        * (daylight + float(settings.vibrant_moon_bloom_scale) * moonlight)
    )
    working += (
        distance_brightness[..., None]
        * np.clip(1.0 - working, 0.0, 1.0)
    )
    haze = (
        fog * float(settings.vibrant_haze_strength) * amount
    )[..., None]
    working = working * (1.0 - haze) + atmospheric_rgb * haze

    # Compress out-of-gamut chroma around luminance instead of clipping an
    # individual channel.  This is the final guard against pure lemon, green
    # or cyan highlights.
    luminance = np.sum(working * luma_weights, axis=-1)
    anchor = np.clip(luminance, 0.0, 0.975)
    chroma = working - luminance[..., None]
    upper_scale = np.where(
        chroma > 1e-6,
        (0.985 - anchor[..., None]) / np.maximum(chroma, 1e-6),
        np.inf,
    )
    lower_scale = np.where(
        chroma < -1e-6,
        (0.0 - anchor[..., None]) / np.minimum(chroma, -1e-6),
        np.inf,
    )
    gamut_scale = np.minimum(
        1.0,
        np.minimum(
            np.min(upper_scale, axis=-1),
            np.min(lower_scale, axis=-1),
        ),
    )
    working = anchor[..., None] + chroma * gamut_scale[..., None]
    working = np.clip(working, 0.0, 0.985)

    result = source.copy()
    graded = np.clip(np.rint(working * 255.0), 0.0, 255.0).astype(np.uint8)
    result[..., :3][mask] = graded[mask]
    return result
