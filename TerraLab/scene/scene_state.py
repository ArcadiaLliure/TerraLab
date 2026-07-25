"""Scene state used by render pipeline."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from TerraLab.common.exception_reporting import log_suppressed_exception
from TerraLab.common.utils import get_base_dir, get_config_value
from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    LP_MODE_MAGNITUDE,
    bortle_to_magnitude,
    normalize_light_pollution_mode,
    resolve_bortle_class,
)
from TerraLab.scene.camera import Camera
from TerraLab.util.math2d import clamp
from TerraLab.data.catalogs.constants import (
    STAR_CATALOG_NAKED_EYE_MAX_MAG,
)


@dataclass
class SceneState:
    camera: Camera

    # Observer/time context
    latitude: float = 0.0
    longitude: float = 0.0
    ut_hour: float = 0.0
    day_of_year: int = 0
    year_utc: int = 0

    # Sky lighting context
    sun_alt: float = -90.0
    sun_az: float = 0.0

    # Star catalog arrays
    ra: Any = None
    dec: Any = None
    mag: Any = None
    bp_rp: Any = None
    color_r: Any = None
    color_g: Any = None
    color_b: Any = None
    star_ids: Any = None

    # Feature flags and knobs
    magnitude_limit: float = 6.0
    star_scale: float = 1.0
    auto_star_scale_multiplier: float = 1.0
    pure_colors: bool = False
    spike_magnitude_threshold: float = 2.0
    scope_k_fallback: float = 0.2
    bortle: float = 1.0
    light_pollution_mode: str = LP_MODE_AUTOMATIC
    scope_enabled: bool = False
    interaction_active: bool = False
    naked_eye_cap: float = 8.0

    # Scope metadata
    scope_shape: str = "circle"
    scope_center_alt: Optional[float] = None
    scope_center_az: Optional[float] = None
    scope_fov_w: Optional[float] = None
    scope_fov_h: Optional[float] = None
    scope_mask_fn: Optional[Callable[[Any, Any], Any]] = None

    # Scene dependencies for optional renderers
    horizon_profile: Any = None
    extras: dict = field(default_factory=dict)

    @property
    def year(self) -> int:
        """Alias de compatibilitat per a codi legacy de render."""
        return int(self.year_utc)


def build_star_scene_state(
    canvas, hour, sun_alt, sun_az, mag_limit, eff_lat, day_of_year
):
    pw = canvas.parent_widget
    if mag_limit is None:
        mag_limit = float(
            getattr(pw, "magnitude_limit", STAR_CATALOG_NAKED_EYE_MAX_MAG)
        )
    if eff_lat is None:
        eff_lat = float(getattr(pw, "latitude", 0.0))
    if day_of_year is None:
        day_of_year = int(getattr(pw, "manual_day", 0))

    year_utc = int(getattr(pw, "manual_year", 0))
    try:
        if callable(getattr(canvas, "_get_current_utc_context", None)):
            _, _, detected_year_utc, _ = canvas._get_current_utc_context()
            year_utc = int(detected_year_utc)
    except Exception:
        log_suppressed_exception(__name__, "build_star_scene_state")

    canvas._sync_camera_state()
    scope_enabled = bool(canvas.scope_mode_enabled())
    light_pollution_mode = normalize_light_pollution_mode(
        getattr(pw, "light_pollution_mode", LP_MODE_AUTOMATIC)
    )
    light_pollution_enabled = bool(
        getattr(pw, "light_pollution_enabled", True)
    )
    selected_magnitude_limit = float(
        getattr(pw, "magnitude_limit", STAR_CATALOG_NAKED_EYE_MAX_MAG)
    )
    effective_bortle_class = resolve_bortle_class(
        light_pollution_mode,
        automatic_bortle=getattr(pw, "auto_bortle_estimate", 1.0),
        bortle_value=getattr(pw, "bortle_value", 1.0),
        magnitude_limit=selected_magnitude_limit,
        light_pollution_enabled=light_pollution_enabled,
    )
    naked_eye_limit = (
        selected_magnitude_limit
        if light_pollution_mode == LP_MODE_MAGNITUDE
        else bortle_to_magnitude(effective_bortle_class)
    )

    extras = {
        "star_gamma": (
            float(getattr(pw, "star_gamma", 0.55))
            if hasattr(pw, "star_gamma")
            else 0.55
        ),
        "star_brightness_boost": (
            float(getattr(pw, "star_brightness_boost", 1.20))
            if hasattr(pw, "star_brightness_boost")
            else 1.20
        ),
        "scope_instrument_profile": str(
            getattr(pw, "scope_instrument_profile", "telescope")
        ),
        "stars_enabled": bool(
            canvas._parent_checkbox_checked("chk_enable_sky", default=True)
        ),
        "catalog_mag_sorted": bool(getattr(pw, "_catalog_mag_sorted", False)),
        # Guardrails for interactive scope rendering on very large catalogs.
        "scope_sync_index_build_max_rows": int(
            max(
                50_000,
                int(
                    get_config_value(
                        "performance.scope_sync_index_build_max_rows", 750_000
                    )
                ),
            )
        ),
        "scope_allow_sync_index_build": bool(
            get_config_value("performance.scope_allow_sync_index_build", True)
        ),
        "scope_pending_prefilter_cap_interaction": int(
            max(
                5_000,
                int(
                    get_config_value(
                        "performance.scope_pending_prefilter_cap_interaction",
                        800_000,
                    )
                ),
            )
        ),
        "scope_pending_prefilter_cap_static": int(
            max(
                5_000,
                int(
                    get_config_value(
                        "performance.scope_pending_prefilter_cap_static",
                        1_200_000,
                    )
                ),
            )
        ),
        "scope_pre_altaz_max_candidates_pending": int(
            max(
                5_000,
                int(
                    get_config_value(
                        "performance.scope_pre_altaz_max_candidates_pending",
                        400_000,
                    )
                ),
            )
        ),
        "scope_pre_altaz_max_candidates_interaction": int(
            max(
                10_000,
                int(
                    get_config_value(
                        "performance.scope_pre_altaz_max_candidates_interaction",
                        800_000,
                    )
                ),
            )
        ),
        "scope_pre_altaz_max_candidates_static": int(
            max(
                10_000,
                int(
                    get_config_value(
                        "performance.scope_pre_altaz_max_candidates_static",
                        1_500_000,
                    )
                ),
            )
        ),
    }

    mw_enabled = bool(
        getattr(
            pw,
            "milkyway_overlay_enabled",
            get_config_value("milkyway_overlay_enabled", True),
        )
    )
    mw_blend_mode = str(
        getattr(
            pw,
            "milkyway_overlay_blend_mode",
            get_config_value("milkyway_overlay_blend_mode", "add"),
        )
    )
    mw_ra_offset = float(
        getattr(
            pw,
            "milkyway_overlay_ra_offset_deg",
            get_config_value("milkyway_overlay_ra_offset_deg", 180.0),
        )
    )
    mw_coord_frame = str(
        getattr(
            pw,
            "milkyway_overlay_coord_frame",
            get_config_value("milkyway_overlay_coord_frame", "galactic"),
        )
    )
    mw_lat_flip = bool(
        getattr(
            pw,
            "milkyway_overlay_lat_flip",
            get_config_value("milkyway_overlay_lat_flip", True),
        )
    )
    mw_lon_flip = bool(
        getattr(
            pw,
            "milkyway_overlay_lon_flip",
            get_config_value("milkyway_overlay_lon_flip", True),
        )
    )
    mw_opacity_factor = float(
        getattr(
            pw,
            "milkyway_overlay_opacity",
            get_config_value("milkyway_overlay_opacity", 0.65),
        )
    )
    runtime_layout = getattr(pw, "runtime_layout", {}) or {}
    data_milkyway_dir = runtime_layout.get("data_milkyway")
    if not data_milkyway_dir:
        data_milkyway_dir = Path(get_base_dir()) / "data" / "milkyway"
    default_mw_texture = str(Path(data_milkyway_dir) / "milkyway_overlay.png")
    mw_texture_path = str(
        getattr(
            pw,
            "milkyway_overlay_texture_path",
            get_config_value(
                "milkyway_overlay_texture_path", default_mw_texture
            ),
        )
    )
    mw_sample_scale = float(
        getattr(
            pw,
            "milkyway_overlay_sample_scale",
            get_config_value("milkyway_overlay_sample_scale", 1.0),
        )
    )

    dust_map_enabled = bool(
        getattr(
            pw, "dust_map_enabled", get_config_value("dust_map_enabled", False)
        )
    )
    data_planck_dir = runtime_layout.get("data_planck")
    if not data_planck_dir:
        data_planck_dir = Path(get_base_dir()) / "data" / "planck"
    default_dust_map = str(
        Path(data_planck_dir) / "planck_dust_opacity_eq_u16.npz"
    )
    dust_map_path = str(
        getattr(
            pw,
            "dust_map_path",
            get_config_value("dust_map_path", default_dust_map),
        )
    )
    dust_density_strength = float(
        getattr(
            pw,
            "dust_density_strength",
            get_config_value("dust_density_strength", 0.0),
        )
    )
    dust_extinction_strength = float(
        getattr(
            pw,
            "dust_extinction_strength",
            get_config_value("dust_extinction_strength", 0.65),
        )
    )
    if (
        bool(dust_map_enabled)
        and float(dust_density_strength) <= 0.0
        and float(dust_extinction_strength) <= 0.0
    ):
        dust_extinction_strength = 0.65

    scope_focal_mm = float(
        getattr(getattr(canvas, "scope_controller", None), "focal_mm", 250.0)
    )
    scope_aperture_mode = str(
        getattr(pw, "scope_aperture_input_mode", "diameter_mm")
    )
    scope_aperture_f_number = float(
        getattr(pw, "scope_aperture_f_number", 4.0)
    )
    if scope_aperture_mode != "f_number":
        aperture_mm = max(1e-6, float(getattr(pw, "scope_aperture_mm", 80.0)))
        scope_aperture_f_number = max(0.7, float(scope_focal_mm) / aperture_mm)

    extras["milkyway_overlay"] = {
        "enabled": mw_enabled,
        "texture_path": mw_texture_path,
        "opacity": float(clamp(mw_opacity_factor, 0.0, 1.0)),
        "blend_mode": mw_blend_mode,
        "ra_offset_deg": mw_ra_offset,
        "coord_frame": mw_coord_frame.strip().lower(),
        "lat_flip": bool(mw_lat_flip),
        "lon_flip": bool(mw_lon_flip),
        "sample_scale": float(clamp(mw_sample_scale, 0.10, 1.0)),
        "auto_opacity": True,
        "light_pollution_mode": light_pollution_mode,
        "bortle": float(effective_bortle_class),
        "magnitude_limit": selected_magnitude_limit,
        "light_pollution_enabled": bool(light_pollution_enabled),
        "scope_enabled": bool(scope_enabled),
        "scope_iso": float(max(1.0, float(getattr(pw, "scope_iso", 800.0)))),
        "scope_exposure_s": float(
            max(1e-3, float(getattr(pw, "scope_exposure_s", 15.0)))
        ),
        "scope_aperture_f_number": float(max(0.1, scope_aperture_f_number)),
        "dust_map_enabled": bool(dust_map_enabled),
        "dust_map_path": dust_map_path,
        "dust_density_strength": float(max(0.0, dust_density_strength)),
        "dust_extinction_strength": float(max(0.0, dust_extinction_strength)),
    }

    vm_state = getattr(pw, "visual_magnitude_result", None)
    if vm_state is not None:
        try:
            exposure_gain_mag = max(
                0.0, float(getattr(vm_state, "exposure_gain_mag", 0.0))
            )
            aperture_gain_mag = max(
                0.0, float(getattr(vm_state, "aperture_gain_mag", 0.0))
            )
            depth_gain_mag = max(
                0.0,
                float(getattr(vm_state, "scope_limit_mag", 0.0))
                - float(getattr(vm_state, "eye_limit_mag", 0.0)),
            )
            iso_value = max(100.0, float(getattr(pw, "scope_iso", 100)))
            exposure_seconds = max(
                0.1, float(getattr(pw, "scope_exposure_s", 1.0))
            )
            iso_factor = max(0.0, min(1.0, math.log2(iso_value / 100.0) / 6.0))
            exposure_factor = max(
                0.0, min(1.0, math.log2(exposure_seconds) / 5.0)
            )
            depth_factor = 0.18 + 0.52 * iso_factor
            extras["scope_exposure_gain_mag"] = exposure_gain_mag
            extras["scope_aperture_gain_mag"] = aperture_gain_mag
            extras["scope_depth_gain_mag"] = depth_gain_mag
            extras["scope_limit_mag"] = float(
                getattr(vm_state, "scope_limit_mag", mag_limit)
            )
            dataset_max_mag = float(
                getattr(pw, "_catalog_max_mag", STAR_CATALOG_NAKED_EYE_MAX_MAG)
            )
            extras["scope_dataset_max_mag"] = dataset_max_mag
            extras["scope_iso_factor"] = float(iso_factor)
            extras["scope_exposure_factor"] = float(exposure_factor)
            photometric_drive = (
                1.05 * exposure_gain_mag * (0.40 + 0.60 * exposure_factor)
                + 0.28 * aperture_gain_mag
                + 0.22 * depth_gain_mag * depth_factor
            )
            extras["scope_signal_gain"] = float(
                max(1.0, min(6.0, 1.0 + 0.26 * photometric_drive))
            )
            extras["scope_halo_gain"] = float(
                max(1.0, min(5.0, 1.0 + 0.22 * photometric_drive))
            )
            if scope_enabled:
                extras["star_gamma"] = float(
                    max(
                        0.38,
                        min(
                            0.65,
                            float(extras["star_gamma"])
                            - 0.05 * min(5.0, exposure_gain_mag),
                        ),
                    )
                )
                extras["star_brightness_boost"] = float(
                    max(
                        1.10,
                        min(
                            3.80,
                            float(extras["star_brightness_boost"])
                            + 0.26
                            * exposure_gain_mag
                            * (0.35 + 0.65 * exposure_factor)
                            + 0.10 * aperture_gain_mag
                            + 0.03 * depth_gain_mag * depth_factor,
                        ),
                    )
                )
                extras["scope_alpha_gain"] = float(
                    max(
                        1.0,
                        min(
                            4.8,
                            1.0
                            + 0.30
                            * exposure_gain_mag
                            * (0.35 + 0.65 * exposure_factor)
                            + 0.22 * iso_factor
                            + 0.08 * aperture_gain_mag
                            + 0.02 * depth_gain_mag * depth_factor,
                        ),
                    )
                )
                extras["scope_size_gain"] = float(
                    max(
                        0.74,
                        min(
                            2.2,
                            0.90
                            + 0.12
                            * exposure_gain_mag
                            * (0.35 + 0.65 * exposure_factor)
                            + 0.04 * aperture_gain_mag
                            + 0.015 * depth_gain_mag * depth_factor,
                        ),
                    )
                )
                extras["scope_limit_extra_mag"] = float(
                    max(
                        0.0,
                        min(
                            4.0,
                            0.55
                            * exposure_gain_mag
                            * (0.30 + 0.70 * exposure_factor)
                            + 0.10 * aperture_gain_mag
                            + 0.08 * depth_gain_mag * depth_factor,
                        ),
                    )
                )
        except Exception:
            log_suppressed_exception(__name__, "build_star_scene_state")

    ra_render = getattr(pw, "np_ra", None)
    dec_render = getattr(pw, "np_dec", None)
    mag_render = getattr(pw, "np_mag", None)
    bp_rp_render = getattr(pw, "np_bp_rp", None)
    r_render = getattr(pw, "np_r", None)
    g_render = getattr(pw, "np_g", None)
    b_render = getattr(pw, "np_b", None)

    if scope_enabled:
        deep_state = str(
            getattr(pw, "_scope_data_state", "ready_deep") or "ready_deep"
        )
        if deep_state in {"loading_deep", "error_deep"}:
            fallback_cap = float(
                max(0.0, float(getattr(pw, "scope_fallback_mag_limit", 8.0)))
            )
            dataset_cap = float(
                extras.get(
                    "scope_dataset_max_mag",
                    getattr(pw, "_catalog_max_mag", fallback_cap),
                )
            )
            extras["scope_dataset_max_mag"] = float(
                min(dataset_cap, fallback_cap)
            )
            extras["scope_fallback_active"] = 1
            extras["scope_fallback_state"] = deep_state
            base_ra = getattr(pw, "_scope_base_ra", None)
            base_dec = getattr(pw, "_scope_base_dec", None)
            base_mag = getattr(pw, "_scope_base_mag", None)
            if (
                base_ra is not None
                and base_dec is not None
                and base_mag is not None
            ):
                try:
                    if int(len(base_ra)) > 0 and int(len(base_ra)) == int(
                        len(base_dec)
                    ) == int(len(base_mag)):
                        ra_render = base_ra
                        dec_render = base_dec
                        mag_render = base_mag
                        bp_rp_render = getattr(pw, "_scope_base_bp_rp", None)
                        r_render = getattr(pw, "_scope_base_r", None)
                        g_render = getattr(pw, "_scope_base_g", None)
                        b_render = getattr(pw, "_scope_base_b", None)
                except Exception:
                    log_suppressed_exception(__name__, "build_star_scene_state")

    if scope_enabled and hasattr(canvas, "scope_controller"):
        ctrl = canvas.scope_controller
        first_fix_pending = not bool(
            getattr(ctrl, "user_center_fixed_once", False)
        )
        extras["scope_first_fix_pending"] = 1 if first_fix_pending else 0
        if first_fix_pending:
            extras["scope_force_naked_eye_until_fix"] = 1
            extras["scope_first_fix_mag_cap"] = float(
                STAR_CATALOG_NAKED_EYE_MAX_MAG
            )
        try:
            fov_w_ctx, fov_h_ctx = ctrl.current_fov()
            fov_diag_ctx = float(
                math.hypot(float(fov_w_ctx), float(fov_h_ctx))
            )
            wide_factor = max(0.0, math.log2(max(1.0, fov_diag_ctx / 9.0)))
            fov_penalty_mag = float(min(5.5, wide_factor * 1.35))
            extras["scope_fov_diag_deg"] = fov_diag_ctx
            extras["scope_fov_penalty_mag"] = fov_penalty_mag
        except Exception:
            log_suppressed_exception(__name__, "build_star_scene_state")
        center = getattr(ctrl, "center", None)
        if center is None:
            center = (
                float(canvas.elevation_angle),
                float(canvas.azimuth_offset),
            )
        if center is not None:
            try:
                center_alt = float(center[0])
                center_az = float(center[1]) % 360.0
                center_ra_dec = canvas._altaz_to_ra_dec(
                    center_alt, center_az, float(hour), int(day_of_year)
                )
                if center_ra_dec is not None:
                    center_ra = float(center_ra_dec[0])
                    center_dec = float(center_ra_dec[1])
                    fov_w, fov_h = ctrl.current_fov()
                    half_diag = 0.5 * math.hypot(float(fov_w), float(fov_h))
                    dec_pad = min(90.0, max(3.0, half_diag * 1.30 + 2.5))
                    ra_pad = min(
                        180.0,
                        dec_pad / max(0.12, math.cos(math.radians(center_dec)))
                        + 2.0,
                    )
                    extras["scope_center_ra_deg"] = center_ra
                    extras["scope_center_dec_deg"] = center_dec
                    extras["scope_preselect_dec_pad_deg"] = float(dec_pad)
                    extras["scope_preselect_ra_pad_deg"] = float(ra_pad)
            except Exception:
                log_suppressed_exception(__name__, "build_star_scene_state")

    return SceneState(
        camera=canvas.camera,
        latitude=float(eff_lat),
        longitude=float(getattr(pw, "longitude", 0.0)),
        ut_hour=float(hour),
        day_of_year=int(day_of_year),
        year_utc=int(year_utc),
        sun_alt=float(sun_alt),
        sun_az=float(sun_az),
        ra=ra_render,
        dec=dec_render,
        mag=mag_render,
        bp_rp=bp_rp_render,
        color_r=r_render,
        color_g=g_render,
        color_b=b_render,
        magnitude_limit=float(mag_limit),
        star_scale=float(getattr(pw, "star_scale", 1.0)),
        auto_star_scale_multiplier=float(
            getattr(pw, "auto_star_scale_multiplier", 1.0)
        ),
        pure_colors=bool(getattr(pw, "pure_colors", False)),
        spike_magnitude_threshold=float(
            getattr(pw, "spike_magnitude_threshold", 2.0)
        ),
        scope_k_fallback=float(getattr(pw, "scope_k_fallback", 0.20)),
        bortle=float(effective_bortle_class),
        light_pollution_mode=light_pollution_mode,
        scope_enabled=scope_enabled,
        interaction_active=bool(
            canvas._camera_interaction_active(
                include_time_drag=True, include_animation=False
            )
            or canvas._scope_motion_active()
        ),
        naked_eye_cap=float(naked_eye_limit),
        scope_mask_fn=canvas._scope_contains_alt_az_mask,
        horizon_profile=getattr(
            getattr(canvas, "horizon_overlay", None), "profile", None
        ),
        extras=extras,
    )
