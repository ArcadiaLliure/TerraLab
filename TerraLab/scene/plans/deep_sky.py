"""Pure, renderer-neutral plans for the Milky Way and deep-sky catalogue."""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import numpy as np

from TerraLab.light_pollution.modes import (
    LP_MODE_AUTOMATIC,
    mode_uses_bortle,
    normalize_light_pollution_mode,
)
from TerraLab.scene.projection import (
    local_sidereal_angle,
    project_universal_stereo_numpy,
    radec_to_altaz_numpy,
)
from TerraLab.scene.plans.labels import (
    InformationalLabelPlanner,
    TextCandidate,
)
from TerraLab.scene.render_state import RenderState


RGBA = tuple[int, int, int, int]


def _readonly(values: np.ndarray | None) -> np.ndarray | None:
    if values is not None:
        values.setflags(write=False)
    return values


def _clamp(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


@dataclass(frozen=True, slots=True)
class TextureResourceHandle:
    """An immutable decoded texture owned by the physical resource repository."""

    path: str | None
    version: str
    content_hash: str
    rgba: np.ndarray | None
    available: bool
    detail: str = ""

    def __post_init__(self) -> None:
        rgba = self.rgba
        if rgba is not None:
            if rgba.ndim != 3 or rgba.shape[2] != 4:
                raise ValueError("TextureResourceHandle.rgba must be HxWx4")
            object.__setattr__(self, "rgba", _readonly(rgba))


@dataclass(frozen=True, slots=True)
class DeepSkyCatalogHandle:
    """An immutable structured catalogue, optionally backed by a read-only mmap."""

    path: str | None
    version: str
    content_hash: str
    records: np.ndarray | None
    available: bool
    detail: str = ""

    def __post_init__(self) -> None:
        records = self.records
        if records is not None:
            required = {"ra", "dec", "mag", "maj", "min", "pa", "kind", "name"}
            names = set(records.dtype.names or ())
            if not required.issubset(names):
                raise ValueError(
                    "DeepSkyCatalogHandle.records is missing required fields"
                )
            object.__setattr__(self, "records", _readonly(records))


@dataclass(frozen=True, slots=True)
class MilkyWaySettings:
    """Visibility and sampling policy resolved from the immutable frame state."""

    enabled: bool = True
    opacity: float = 0.65
    blend_mode: str = "add"
    ra_offset_deg: float = 180.0
    coord_frame: str = "galactic"
    lat_flip: bool = True
    lon_flip: bool = True
    sample_scale: float = 1.0
    dust_map_enabled: bool = False
    dust_density_strength: float = 0.0
    dust_extinction_strength: float = 0.65
    auto_opacity: bool = True
    light_pollution_mode: str = LP_MODE_AUTOMATIC
    bortle: float = 1.0
    magnitude_limit: float = 6.0
    scope_enabled: bool = False
    scope_iso: float = 800.0
    scope_exposure_s: float = 15.0
    scope_aperture_f_number: float = 2.8

    @classmethod
    def from_state(cls, state: RenderState) -> "MilkyWaySettings":
        extras = state.extras if isinstance(state.extras, Mapping) else {}
        block = extras.get("milkyway_overlay", {})
        block = block if isinstance(block, Mapping) else {}
        return cls(
            enabled=bool(block.get("enabled", True)),
            opacity=_clamp(float(block.get("opacity", 0.65)), 0.0, 1.0),
            blend_mode=str(block.get("blend_mode", "add") or "add").lower(),
            ra_offset_deg=float(block.get("ra_offset_deg", 180.0)),
            coord_frame=str(block.get("coord_frame", "galactic") or "galactic")
            .strip()
            .lower(),
            lat_flip=bool(block.get("lat_flip", True)),
            lon_flip=bool(block.get("lon_flip", True)),
            sample_scale=_clamp(
                float(block.get("sample_scale", 1.0)), 0.10, 1.0
            ),
            dust_map_enabled=bool(block.get("dust_map_enabled", False)),
            dust_density_strength=max(
                0.0, float(block.get("dust_density_strength", 0.0))
            ),
            dust_extinction_strength=max(
                0.0, float(block.get("dust_extinction_strength", 0.65))
            ),
            auto_opacity=bool(block.get("auto_opacity", True)),
            light_pollution_mode=normalize_light_pollution_mode(
                block.get("light_pollution_mode", state.light_pollution_mode)
            ),
            bortle=float(block.get("bortle", state.bortle)),
            magnitude_limit=float(
                block.get("magnitude_limit", state.mag_limit)
            ),
            scope_enabled=bool(
                block.get("scope_enabled", state.scope_enabled)
            ),
            scope_iso=max(1.0, float(block.get("scope_iso", 800.0))),
            scope_exposure_s=max(
                1e-3, float(block.get("scope_exposure_s", 15.0))
            ),
            scope_aperture_f_number=max(
                0.1, float(block.get("scope_aperture_f_number", 2.8))
            ),
        )


@dataclass(frozen=True, slots=True)
class MilkyWayPlan:
    """A complete compositing image, already sampled and policy-filtered."""

    cache_key: tuple[object, ...]
    rgba: np.ndarray | None
    blend_mode: str
    effective_opacity: float
    opacity_reason: str
    texture_version: str
    dust_version: str
    status: Mapping[str, object]

    def __post_init__(self) -> None:
        rgba = self.rgba
        if rgba is not None:
            if rgba.ndim != 3 or rgba.shape[2] != 4 or rgba.dtype != np.uint8:
                raise ValueError("MilkyWayPlan.rgba must be uint8 HxWx4")
            object.__setattr__(self, "rgba", _readonly(rgba))
        object.__setattr__(self, "status", MappingProxyType(dict(self.status)))

    @property
    def visible(self) -> bool:
        return self.rgba is not None and self.effective_opacity > 1e-4


@dataclass(frozen=True, slots=True)
class DeepSkyGlyph:
    """A fully projected catalogue marker without presentation API objects."""

    name: str
    object_kind: str
    screen_x: float
    screen_y: float
    radius_x_px: float
    radius_y_px: float
    rotation_deg: float
    rgba: RGBA
    draw_cross: bool
    draw_ticks: bool


@dataclass(frozen=True, slots=True)
class DeepSkyPickRecord:
    """Typed renderer-neutral NGC hit target for a rendered frame."""

    name: str
    right_ascension_deg: float
    declination_deg: float
    altitude_deg: float
    azimuth_deg: float
    screen_x: float
    screen_y: float
    radius_px: float
    label_visible: bool


@dataclass(frozen=True, slots=True)
class DeepSkyBatch:
    """All NGC shapes, label candidates and picks resolved for one frame."""

    glyphs: tuple[DeepSkyGlyph, ...]
    label_candidates: tuple[TextCandidate, ...]
    picks: tuple[DeepSkyPickRecord, ...]
    catalog_version: str
    reason: str = "visible"


class MilkyWayPlanner:
    """Build sampled Milky-Way plans; it owns no files nor presentation objects."""

    def __init__(self) -> None:
        self._grid_cache_key: tuple[int, int, int, int] | None = None
        self._grid_cache: tuple[np.ndarray, np.ndarray] | None = None
        self._plan_cache_key: tuple[object, ...] | None = None
        self._plan_cache: MilkyWayPlan | None = None

    @staticmethod
    def effective_opacity(
        state: RenderState, settings: MilkyWaySettings
    ) -> tuple[float, str]:
        if not settings.auto_opacity:
            return _clamp(settings.opacity, 0.0, 1.0), "manual"
        if float(state.sun_alt) >= -6.0:
            return 0.0, "daylight_or_civil_twilight"
        if mode_uses_bortle(settings.light_pollution_mode):
            base_opacity = (5.0 - float(settings.bortle)) / 3.0
            reason = "bortle"
        else:
            base_opacity = (float(settings.magnitude_limit) - 4.0) / 3.5
            reason = "magnitude"
        base_opacity = _clamp(base_opacity, 0.0, 1.0)
        if float(state.sun_alt) > -18.0:
            base_opacity *= _clamp(
                (-6.0 - float(state.sun_alt)) / 12.0, 0.0, 1.0
            )
            reason = "twilight_fade"
        if settings.scope_enabled:
            exposure_factor = (
                math.log2(max(1e-6, settings.scope_iso / 800.0))
                + math.log2(max(1e-6, settings.scope_exposure_s / 15.0))
                + math.log2(
                    max(1e-6, (2.8 / settings.scope_aperture_f_number) ** 2)
                )
            )
            base_opacity = _clamp(
                base_opacity - 0.12 * exposure_factor, 0.0, 1.0
            )
            reason = "photo_scope"
        return _clamp(base_opacity * settings.opacity, 0.0, 1.0), reason

    @staticmethod
    def equatorial_to_galactic_deg(
        ra_deg: np.ndarray, dec_deg: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        ra_rad = np.radians(np.asarray(ra_deg, dtype=np.float32))
        dec_rad = np.radians(
            np.asarray(np.clip(dec_deg, -90.0, 90.0), dtype=np.float32)
        )
        cos_dec = np.cos(dec_rad)
        x_eq = cos_dec * np.cos(ra_rad)
        y_eq = cos_dec * np.sin(ra_rad)
        z_eq = np.sin(dec_rad)
        x_gal = (
            -0.0548755604 * x_eq - 0.8734370902 * y_eq - 0.4838350155 * z_eq
        )
        y_gal = 0.4941094279 * x_eq - 0.4448296300 * y_eq + 0.7469822445 * z_eq
        z_gal = (
            -0.8676661490 * x_eq - 0.1980763734 * y_eq + 0.4559837762 * z_eq
        )
        return (
            np.asarray(
                (np.degrees(np.arctan2(y_gal, x_gal)) + 360.0) % 360.0,
                dtype=np.float32,
            ),
            np.asarray(
                np.degrees(np.arcsin(np.clip(z_gal, -1.0, 1.0))),
                dtype=np.float32,
            ),
        )

    @classmethod
    def texture_lon_lat(
        cls, ra_deg: np.ndarray, dec_deg: np.ndarray, *, coord_frame: str
    ) -> tuple[np.ndarray, np.ndarray]:
        ra = np.asarray(ra_deg, dtype=np.float32) % 360.0
        dec = np.asarray(np.clip(dec_deg, -90.0, 90.0), dtype=np.float32)
        if str(coord_frame).strip().lower() == "galactic":
            return cls.equatorial_to_galactic_deg(ra, dec)
        return ra, dec

    @staticmethod
    def bilinear_sample_rgba(
        texture: np.ndarray, u: np.ndarray, v: np.ndarray
    ) -> np.ndarray:
        h, w, _ = texture.shape
        u_wrapped = np.mod(u, 1.0)
        v_clamped = np.clip(v, 0.0, 1.0)
        x = u_wrapped * float(w)
        x0 = np.floor(x).astype(np.int64) % w
        x1 = (x0 + 1) % w
        tx = np.asarray(x - np.floor(x), dtype=np.float32)[..., None]
        y = v_clamped * float(max(1, h - 1))
        y0 = np.floor(y).astype(np.int64)
        y1 = np.clip(y0 + 1, 0, h - 1)
        ty = np.asarray(y - np.floor(y), dtype=np.float32)[..., None]
        top = texture[y0, x0] * (1.0 - tx) + texture[y0, x1] * tx
        bottom = texture[y1, x0] * (1.0 - tx) + texture[y1, x1] * tx
        return np.asarray(top * (1.0 - ty) + bottom * ty, dtype=np.float32)

    @staticmethod
    def bilinear_sample_scalar(
        texture: np.ndarray, u: np.ndarray, v: np.ndarray
    ) -> np.ndarray:
        h, w = texture.shape
        u_wrapped = np.mod(u, 1.0)
        v_clamped = np.clip(v, 0.0, 1.0)
        x = u_wrapped * float(w)
        x0 = np.floor(x).astype(np.int64) % w
        x1 = (x0 + 1) % w
        tx = np.asarray(x - np.floor(x), dtype=np.float32)
        y = v_clamped * float(max(1, h - 1))
        y0 = np.floor(y).astype(np.int64)
        y1 = np.clip(y0 + 1, 0, h - 1)
        ty = np.asarray(y - np.floor(y), dtype=np.float32)
        top = texture[y0, x0] * (1.0 - tx) + texture[y0, x1] * tx
        bottom = texture[y1, x0] * (1.0 - tx) + texture[y1, x1] * tx
        return np.asarray(top * (1.0 - ty) + bottom * ty, dtype=np.float32)

    @staticmethod
    def _effective_sample_scale(
        settings: MilkyWaySettings, interaction_active: bool
    ) -> float:
        base = _clamp(settings.sample_scale, 0.10, 1.0)
        if not interaction_active:
            return base
        return _clamp(min(base, max(0.10, base * 0.28)), 0.10, base)

    def _screen_grid_to_altaz(
        self,
        *,
        sample_width: int,
        sample_height: int,
        width: int,
        height: int,
        state: RenderState,
    ) -> tuple[np.ndarray, np.ndarray]:
        grid_key = (sample_width, sample_height, width, height)
        if self._grid_cache_key == grid_key and self._grid_cache is not None:
            sx_grid, sy_grid = self._grid_cache
        else:
            sx = np.linspace(
                0.0, max(0.0, width - 1.0), sample_width, dtype=np.float32
            )
            sy = np.linspace(
                0.0, max(0.0, height - 1.0), sample_height, dtype=np.float32
            )
            sx_grid, sy_grid = np.meshgrid(sx, sy)
            self._grid_cache_key = grid_key
            self._grid_cache = (sx_grid, sy_grid)
        camera = state.camera
        scale_h = max(1.0, (float(height) * 0.5) * float(camera.zoom_level))
        cx = float(width) * 0.5
        cy_base = (float(height) * 0.5) + float(height) * float(
            camera.vertical_offset_ratio
        )
        y_center = 2.0 * math.tan(
            math.radians(float(camera.elevation_angle)) * 0.5
        )
        x = (sx_grid - cx) / scale_h
        y = -((sy_grid - cy_base) / scale_h) + y_center
        rho = np.sqrt(x * x + y * y, dtype=np.float32)
        c = 2.0 * np.arctan(rho * 0.5)
        sin_c = np.sin(c)
        cos_c = np.cos(c)
        with np.errstate(divide="ignore", invalid="ignore"):
            lat_rad = np.arcsin(np.where(rho > 1e-9, (y * sin_c) / rho, 0.0))
            lon_rad = np.where(
                rho > 1e-9, np.arctan2(x * sin_c, rho * cos_c), 0.0
            )
        return (
            np.asarray(np.degrees(lat_rad), dtype=np.float32),
            np.asarray(
                (np.degrees(lon_rad) + float(camera.azimuth_offset)) % 360.0,
                dtype=np.float32,
            ),
        )

    @staticmethod
    def _altaz_to_radec(
        alt_deg: np.ndarray, az_deg: np.ndarray, state: RenderState
    ) -> tuple[np.ndarray, np.ndarray]:
        lat = math.radians(float(state.latitude))
        sin_lat, cos_lat = math.sin(lat), math.cos(lat)
        alt_rad = np.radians(np.asarray(alt_deg, dtype=np.float32))
        az_rad = np.radians(np.asarray(az_deg, dtype=np.float32) % 360.0)
        sin_alt, cos_alt = np.sin(alt_rad), np.cos(alt_rad)
        sin_dec = np.clip(
            sin_alt * sin_lat + cos_alt * cos_lat * np.cos(az_rad), -1.0, 1.0
        )
        dec_rad = np.arcsin(sin_dec)
        cos_dec = np.cos(dec_rad)
        sin_ha = -np.sin(az_rad) * cos_alt / (cos_dec + 1e-12)
        cos_ha = (sin_alt - sin_lat * sin_dec) / (cos_lat * cos_dec + 1e-12)
        ha_deg = np.degrees(np.arctan2(sin_ha, cos_ha))
        lst = local_sidereal_angle(
            day_of_year=int(state.day_of_year_utc),
            ut_hour=float(state.ut_hour),
            longitude_deg=float(state.longitude),
            year=int(state.year_utc),
        )
        return (
            np.asarray((float(lst) - ha_deg) % 360.0, dtype=np.float32),
            np.asarray(
                np.clip(np.degrees(dec_rad), -90.0, 90.0), dtype=np.float32
            ),
        )

    def _cache_key(
        self,
        *,
        width: int,
        height: int,
        state: RenderState,
        settings: MilkyWaySettings,
        opacity: float,
        texture: TextureResourceHandle,
        dust: TextureResourceHandle | None,
    ) -> tuple[object, ...]:
        camera = state.camera
        interaction = bool(state.interaction_active or state.scope_enabled)
        sample_scale = self._effective_sample_scale(settings, interaction)
        lst = local_sidereal_angle(
            day_of_year=int(state.day_of_year_utc),
            ut_hour=float(state.ut_hour),
            longitude_deg=float(state.longitude),
            year=int(state.year_utc),
        )
        return (
            int(width),
            int(height),
            round(float(camera.azimuth_offset) % 360.0, 3),
            round(float(camera.elevation_angle), 3),
            round(float(camera.zoom_level), 4),
            round(float(camera.vertical_offset_ratio), 4),
            round(float(state.latitude), 3),
            round(float(state.longitude), 3),
            round(float(lst), 2),
            int(interaction),
            round(float(settings.ra_offset_deg) % 360.0, 5),
            settings.coord_frame,
            int(settings.lat_flip),
            int(settings.lon_flip),
            settings.blend_mode,
            round(float(opacity), 5),
            round(sample_scale, 3),
            texture.version,
            texture.content_hash,
            dust.version if dust is not None else "",
            dust.content_hash if dust is not None else "",
            round(float(settings.dust_density_strength), 4),
            round(float(settings.dust_extinction_strength), 4),
        )

    def plan(
        self,
        *,
        width: int,
        height: int,
        state: RenderState,
        texture: TextureResourceHandle,
        dust: TextureResourceHandle | None,
    ) -> MilkyWayPlan:
        settings = MilkyWaySettings.from_state(state)
        status: dict[str, object] = {
            "enabled": settings.enabled,
            "texture_loaded": texture.available,
            "texture_path": texture.path,
            "texture_version": texture.version,
            "dust_requested": settings.dust_map_enabled,
            "dust_loaded": bool(dust is not None and dust.available),
            "dust_path": dust.path if dust is not None else None,
            "blend_mode": settings.blend_mode,
            "texture_frame": settings.coord_frame,
            "ra_offset_deg": settings.ra_offset_deg,
            "texture_lat_flip": settings.lat_flip,
            "texture_lon_flip": settings.lon_flip,
            "dust_density_strength": settings.dust_density_strength,
            "dust_extinction_strength": settings.dust_extinction_strength,
        }
        if not settings.enabled:
            status.update(effective_opacity=0.0, opacity_reason="disabled")
            return MilkyWayPlan(
                (),
                None,
                settings.blend_mode,
                0.0,
                "disabled",
                texture.version,
                dust.version if dust else "",
                status,
            )
        if not texture.available or texture.rgba is None:
            status.update(
                effective_opacity=0.0, opacity_reason="missing_texture"
            )
            return MilkyWayPlan(
                (),
                None,
                settings.blend_mode,
                0.0,
                "missing_texture",
                texture.version,
                dust.version if dust else "",
                status,
            )
        texture_rgba = texture.rgba
        assert texture_rgba is not None
        opacity, reason = self.effective_opacity(state, settings)
        status.update(effective_opacity=opacity, opacity_reason=reason)
        if opacity <= 1e-4:
            return MilkyWayPlan(
                (),
                None,
                settings.blend_mode,
                opacity,
                reason,
                texture.version,
                dust.version if dust else "",
                status,
            )
        cache_key = self._cache_key(
            width=width,
            height=height,
            state=state,
            settings=settings,
            opacity=opacity,
            texture=texture,
            dust=dust if settings.dust_map_enabled else None,
        )
        if self._plan_cache_key == cache_key and self._plan_cache is not None:
            return self._plan_cache
        interaction = bool(state.interaction_active or state.scope_enabled)
        scale = self._effective_sample_scale(settings, interaction)
        sample_width = max(64, int(round(width * scale)))
        sample_height = max(32, int(round(height * scale)))
        alt_deg, az_deg = self._screen_grid_to_altaz(
            sample_width=sample_width,
            sample_height=sample_height,
            width=width,
            height=height,
            state=state,
        )
        ra_deg, dec_deg = self._altaz_to_radec(alt_deg, az_deg, state)
        lon_deg, lat_deg = self.texture_lon_lat(
            ra_deg, dec_deg, coord_frame=settings.coord_frame
        )
        lat_for_texture = -lat_deg if settings.lat_flip else lat_deg
        u = ((lon_deg + settings.ra_offset_deg) % 360.0) / 360.0
        if settings.lon_flip:
            u = np.asarray(np.mod(1.0 - u, 1.0), dtype=np.float32)
        v = 1.0 - ((lat_for_texture + 90.0) / 180.0)
        sampled = self.bilinear_sample_rgba(texture_rgba, u, v)
        rgb = np.asarray(np.clip(sampled[..., :3], 0.0, 1.0), dtype=np.float32)
        alpha = np.asarray(sampled[..., 3] * opacity, dtype=np.float32)
        dust_rgba = dust.rgba if dust is not None and dust.available else None
        dust_active = (
            settings.dust_map_enabled
            and dust_rgba is not None
            and (
                settings.dust_density_strength > 0.0
                or settings.dust_extinction_strength > 0.0
            )
            and not interaction
        )
        if dust_active:
            dust_u = ((lon_deg + settings.ra_offset_deg) % 360.0) / 360.0
            dust_v = 1.0 - ((lat_deg + 90.0) / 180.0)
            assert dust_rgba is not None
            dust_values = self.bilinear_sample_scalar(
                dust_rgba[..., 0], dust_u, dust_v
            )
            if settings.dust_density_strength > 0.0:
                alpha *= np.clip(
                    1.0 + settings.dust_density_strength * dust_values,
                    0.0,
                    4.0,
                )
            if settings.dust_extinction_strength > 0.0:
                rgb *= np.clip(
                    1.0 - settings.dust_extinction_strength * dust_values,
                    0.0,
                    1.0,
                )[..., None]
        elif settings.dust_map_enabled and (
            dust is None or not dust.available
        ):
            status["dust_fallback"] = "unavailable"
        rgba = np.empty((sample_height, sample_width, 4), dtype=np.uint8)
        rgba[..., :3] = np.asarray(
            np.clip(np.rint(rgb * 255.0), 0.0, 255.0), dtype=np.uint8
        )
        rgba[..., 3] = np.asarray(
            np.clip(np.rint(alpha * 255.0), 0.0, 255.0), dtype=np.uint8
        )
        plan = MilkyWayPlan(
            cache_key,
            rgba,
            settings.blend_mode,
            opacity,
            reason,
            texture.version,
            dust.version if dust else "",
            status,
        )
        self._plan_cache_key = cache_key
        self._plan_cache = plan
        return plan


class DeepSkyPlanner:
    """Resolve NGC projection, visibility and label candidates into a batch."""

    @staticmethod
    def _colour(object_kind: str, magnitude: float) -> RGBA:
        alpha = 220 if magnitude <= 6.0 else (185 if magnitude <= 8.5 else 150)
        kind = object_kind.upper().strip()
        if kind.startswith("G"):
            return 110, 205, 255, alpha
        if "PN" in kind or "N" in kind or "HII" in kind:
            return 80, 235, 165, alpha
        if "GC" in kind:
            return 255, 205, 90, alpha
        return 255, 225, 125, alpha

    def plan(
        self,
        *,
        width: int,
        height: int,
        state: RenderState,
        catalog: DeepSkyCatalogHandle,
    ) -> DeepSkyBatch:
        if "deep_sky" not in state.layers_enabled:
            return DeepSkyBatch((), (), (), catalog.version, "layer_disabled")
        if float(state.sun_alt) > -6.0:
            return DeepSkyBatch(
                (), (), (), catalog.version, "daylight_or_civil_twilight"
            )
        records = catalog.records
        if not catalog.available or records is None or len(records) == 0:
            return DeepSkyBatch((), (), (), catalog.version, "missing_catalog")
        altitudes, azimuths = radec_to_altaz_numpy(
            records["ra"],
            records["dec"],
            state.latitude,
            state.longitude,
            state.ut_hour,
            state.day_of_year_utc,
            year=state.year_utc,
        )
        sx, sy, valid = project_universal_stereo_numpy(
            altitudes, azimuths, width, height, state.camera
        )
        if (
            altitudes is None
            or azimuths is None
            or sx is None
            or sy is None
            or valid is None
        ):
            return DeepSkyBatch(
                (), (), (), catalog.version, "projection_unavailable"
            )
        magnitudes = np.asarray(records["mag"], dtype=np.float32)
        major = np.asarray(records["maj"], dtype=np.float32)
        mask = (
            np.asarray(valid, dtype=bool)
            & (altitudes > -2.0)
            & (sx >= -48.0)
            & (sx <= width + 48.0)
            & (sy >= -48.0)
            & (sy <= height + 48.0)
            & ((magnitudes <= 8.8) | (major >= 0.30))
        )
        indices = np.flatnonzero(mask)
        if len(indices) == 0:
            return DeepSkyBatch(
                (), (), (), catalog.version, "no_visible_objects"
            )
        max_markers = 260 if state.camera.zoom_level < 2.0 else 420
        importance = np.where(
            np.isfinite(magnitudes[indices]), magnitudes[indices], 99.0
        ) - np.minimum(3.0, major[indices] * 3.0)
        indices = indices[np.argsort(importance, kind="stable")[:max_markers]]
        angular_scale = (
            float(height) * 0.5 * float(state.camera.zoom_level) / 90.0
        )
        max_labels = 90 if state.camera.zoom_level < 2.0 else 180
        glyphs: list[DeepSkyGlyph] = []
        picks: list[DeepSkyPickRecord] = []
        for index in indices:
            x, y = float(sx[index]), float(sy[index])
            kind = str(records["kind"][index]).upper().strip()
            name = str(records["name"][index]).strip() or "NGC"
            maj = max(0.05, float(records["maj"][index]))
            minor = max(0.03, float(records["min"][index]))
            radius_x = max(3.0, min(84.0, 0.5 * maj * angular_scale))
            radius_y = max(2.0, min(64.0, 0.5 * minor * angular_scale))
            magnitude = float(magnitudes[index])
            colour = self._colour(kind, magnitude)
            glyphs.append(
                DeepSkyGlyph(
                    name,
                    kind,
                    x,
                    y,
                    radius_x,
                    radius_y,
                    float(records["pa"][index]),
                    colour,
                    "GC" in kind,
                    "PN" in kind,
                )
            )
            picks.append(
                DeepSkyPickRecord(
                    name,
                    float(records["ra"][index]),
                    float(records["dec"][index]),
                    float(altitudes[index]),
                    float(azimuths[index]),
                    x,
                    y,
                    float(max(radius_x, radius_y) + 8.0),
                    False,
                )
            )
        label_candidates = InformationalLabelPlanner.deep_sky_candidates(
            glyphs[:max_labels], width, height
        )
        return DeepSkyBatch(
            tuple(glyphs), label_candidates, tuple(picks), catalog.version
        )
