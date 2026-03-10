"""Capa de Via Lactia basada en textura equirectangular RGBA."""

from __future__ import annotations

from dataclasses import dataclass
import io
import math
import os
from pathlib import Path
from typing import Optional

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPainter

from TerraLab.common.utils import resource_path
from TerraLab.scene.projection import local_sidereal_angle
from TerraLab.util.math2d import clamp


@dataclass
class _OverlayConfig:
    enabled: bool = True
    texture_path: str = "data/sky/milkyway_overlay.png"
    opacity: float = 0.65
    blend_mode: str = "add"
    ra_offset_deg: float = 180.0
    coord_frame: str = "galactic"
    lat_flip: bool = True
    lon_flip: bool = True
    sample_scale: float = 0.35
    dust_map_enabled: bool = False
    dust_map_path: str = "data/sky/derived/planck_dust_opacity_eq_u16.npz"
    dust_density_strength: float = 0.0
    dust_extinction_strength: float = 0.65
    auto_opacity: bool = True
    is_auto_bortle: bool = True
    bortle: float = 1.0
    manual_mag_limit: float = 6.0
    scope_enabled: bool = False
    scope_iso: float = 800.0
    scope_exposure_s: float = 15.0
    scope_aperture_f_number: float = 2.8


class MilkyWayOverlay:
    def __init__(self) -> None:
        self.enabled: bool = True
        self.opacity: float = 0.65
        self.blend_mode: str = "add"
        self.ra_offset_deg: float = 180.0
        self.coord_frame: str = "galactic"
        self.lat_flip: bool = True
        self.lon_flip: bool = True
        self.texture_path: str = "data/sky/milkyway_overlay.png"

        self._texture_rgba: Optional[np.ndarray] = None
        self._texture_rgb_gain: float = 1.0
        self._texture_path_resolved: Optional[str] = None
        self._texture_stamp: Optional[tuple] = None

        self._dust_map: Optional[np.ndarray] = None
        self._dust_map_path_resolved: Optional[str] = None
        self._dust_map_stamp: Optional[tuple] = None

        self._cached_overlay_key: Optional[tuple] = None
        self._cached_overlay_image: Optional[QImage] = None
        self._screen_grid_cache_key: Optional[tuple] = None
        self._screen_grid_cache: Optional[tuple] = None
        self._warned_missing_texture: bool = False
        self._reported_texture_loaded: bool = False
        self._warned_missing_dust: bool = False
        self._reported_dust_loaded: bool = False
        self._last_runtime_status: dict = {
            "enabled": False,
            "texture_loaded": False,
            "texture_path": None,
            "texture_rgb_gain": 1.0,
            "dust_requested": False,
            "dust_loaded": False,
            "dust_path": None,
            "effective_opacity": 0.0,
            "blend_mode": "add",
            "texture_frame": "galactic",
            "ra_offset_deg": 180.0,
            "texture_lat_flip": True,
            "texture_lon_flip": True,
            "dust_density_strength": 0.0,
            "dust_extinction_strength": 0.65,
            "opacity_reason": "disabled",
        }

    def render(self, ctx, state) -> None:
        if np is None:
            self._last_runtime_status = {
                "enabled": False,
                "texture_loaded": False,
                "texture_path": self._texture_path_resolved,
                "texture_rgb_gain": float(self._texture_rgb_gain),
                "dust_requested": False,
                "dust_loaded": False,
                "dust_path": self._dust_map_path_resolved,
                "effective_opacity": 0.0,
                "blend_mode": str(self.blend_mode or "add"),
                "texture_frame": str(self.coord_frame or "galactic"),
                "ra_offset_deg": float(self.ra_offset_deg),
                "texture_lat_flip": bool(self.lat_flip),
                "texture_lon_flip": bool(self.lon_flip),
                "dust_density_strength": 0.0,
                "dust_extinction_strength": 0.0,
                "opacity_reason": "numpy_unavailable",
            }
            return

        cfg = self._read_config(state)
        if not cfg.enabled:
            self._last_runtime_status = {
                "enabled": False,
                "texture_loaded": self._texture_rgba is not None,
                "texture_path": self._texture_path_resolved,
                "texture_rgb_gain": float(self._texture_rgb_gain),
                "dust_requested": bool(cfg.dust_map_enabled),
                "dust_loaded": self._dust_map is not None,
                "dust_path": self._dust_map_path_resolved,
                "effective_opacity": 0.0,
                "blend_mode": cfg.blend_mode,
                "texture_frame": cfg.coord_frame,
                "ra_offset_deg": float(cfg.ra_offset_deg),
                "texture_lat_flip": bool(cfg.lat_flip),
                "texture_lon_flip": bool(cfg.lon_flip),
                "dust_density_strength": float(cfg.dust_density_strength),
                "dust_extinction_strength": float(cfg.dust_extinction_strength),
                "opacity_reason": "disabled",
            }
            return

        texture_ok = self._ensure_overlay_texture(cfg.texture_path)
        if not texture_ok:
            self._last_runtime_status = {
                "enabled": True,
                "texture_loaded": False,
                "texture_path": self._texture_path_resolved,
                "texture_rgb_gain": float(self._texture_rgb_gain),
                "dust_requested": bool(cfg.dust_map_enabled),
                "dust_loaded": self._dust_map is not None,
                "dust_path": self._dust_map_path_resolved,
                "effective_opacity": 0.0,
                "blend_mode": cfg.blend_mode,
                "texture_frame": cfg.coord_frame,
                "ra_offset_deg": float(cfg.ra_offset_deg),
                "texture_lat_flip": bool(cfg.lat_flip),
                "texture_lon_flip": bool(cfg.lon_flip),
                "dust_density_strength": float(cfg.dust_density_strength),
                "dust_extinction_strength": float(cfg.dust_extinction_strength),
                "opacity_reason": "missing_texture",
            }
            return

        effective_opacity, opacity_reason = self._compute_effective_opacity(state, cfg)
        if effective_opacity <= 1e-4:
            self._last_runtime_status = {
                "enabled": True,
                "texture_loaded": True,
                "texture_path": self._texture_path_resolved,
                "texture_rgb_gain": float(self._texture_rgb_gain),
                "dust_requested": bool(cfg.dust_map_enabled),
                "dust_loaded": self._dust_map is not None,
                "dust_path": self._dust_map_path_resolved,
                "effective_opacity": float(effective_opacity),
                "blend_mode": cfg.blend_mode,
                "texture_frame": cfg.coord_frame,
                "ra_offset_deg": float(cfg.ra_offset_deg),
                "texture_lat_flip": bool(cfg.lat_flip),
                "texture_lon_flip": bool(cfg.lon_flip),
                "dust_density_strength": float(cfg.dust_density_strength),
                "dust_extinction_strength": float(cfg.dust_extinction_strength),
                "opacity_reason": opacity_reason,
            }
            return

        dust_ok = False
        if cfg.dust_map_enabled:
            dust_ok = self._ensure_dust_map(cfg.dust_map_path)
        else:
            self._dust_map = None
            self._dust_map_path_resolved = None
            self._dust_map_stamp = None

        key = self._build_cache_key(ctx, state, cfg, effective_opacity)
        if self._cached_overlay_key != key or self._cached_overlay_image is None:
            image = self._build_overlay_image(ctx, state, cfg, effective_opacity)
            if image is None:
                return
            self._cached_overlay_key = key
            self._cached_overlay_image = image

        painter = ctx.painter
        painter.save()
        painter.setCompositionMode(self._composition_mode(cfg.blend_mode))
        painter.drawImage(0, 0, self._cached_overlay_image)
        painter.restore()

        self._last_runtime_status = {
            "enabled": True,
            "texture_loaded": True,
            "texture_path": self._texture_path_resolved,
            "texture_rgb_gain": float(self._texture_rgb_gain),
            "dust_requested": bool(cfg.dust_map_enabled),
            "dust_loaded": bool(dust_ok),
            "dust_path": self._dust_map_path_resolved,
            "effective_opacity": float(effective_opacity),
            "blend_mode": cfg.blend_mode,
            "texture_frame": cfg.coord_frame,
            "ra_offset_deg": float(cfg.ra_offset_deg),
            "texture_lat_flip": bool(cfg.lat_flip),
            "texture_lon_flip": bool(cfg.lon_flip),
            "dust_density_strength": float(cfg.dust_density_strength),
            "dust_extinction_strength": float(cfg.dust_extinction_strength),
            "opacity_reason": opacity_reason,
        }

    def runtime_status(self) -> dict:
        return dict(self._last_runtime_status)

    def sample_rgba_at_radec(
        self,
        ra_deg: float,
        dec_deg: float,
        *,
        ra_offset_deg: float = 0.0,
        texture_path: Optional[str] = None,
        coord_frame: Optional[str] = None,
        lat_flip: Optional[bool] = None,
        lon_flip: Optional[bool] = None,
    ) -> Optional[tuple[int, int, int, int]]:
        path = texture_path or self.texture_path
        if not self._ensure_overlay_texture(path):
            return None
        ra = np.asarray([[float(ra_deg)]], dtype=np.float32)
        dec = np.asarray([[float(dec_deg)]], dtype=np.float32)
        frame = str(coord_frame or self.coord_frame or "galactic").strip().lower()
        flip = bool(self.lat_flip) if lat_flip is None else bool(lat_flip)
        lon_mirror = bool(self.lon_flip) if lon_flip is None else bool(lon_flip)
        rgba = self._sample_overlay_rgba(
            ra,
            dec,
            float(ra_offset_deg),
            coord_frame=frame,
            lat_flip=flip,
            lon_flip=lon_mirror,
        )
        if rgba is None:
            return None
        px = np.asarray(np.clip(np.rint(rgba[0, 0] * 255.0), 0.0, 255.0), dtype=np.uint8)
        return int(px[0]), int(px[1]), int(px[2]), int(px[3])

    def _read_config(self, state) -> _OverlayConfig:
        extras = getattr(state, "extras", {}) if isinstance(getattr(state, "extras", {}), dict) else {}
        block = extras.get("milkyway_overlay", {}) if isinstance(extras, dict) else {}
        if not isinstance(block, dict):
            block = {}

        cfg = _OverlayConfig(
            enabled=bool(block.get("enabled", self.enabled)),
            texture_path=str(block.get("texture_path", self.texture_path)),
            opacity=float(clamp(block.get("opacity", self.opacity), 0.0, 1.0)),
            blend_mode=str(block.get("blend_mode", self.blend_mode or "add")).lower(),
            ra_offset_deg=float(block.get("ra_offset_deg", self.ra_offset_deg)),
            coord_frame=str(block.get("coord_frame", self.coord_frame or "galactic")).strip().lower(),
            lat_flip=bool(block.get("lat_flip", self.lat_flip)),
            lon_flip=bool(block.get("lon_flip", self.lon_flip)),
            sample_scale=float(clamp(block.get("sample_scale", 0.35), 0.10, 1.0)),
            dust_map_enabled=bool(block.get("dust_map_enabled", False)),
            dust_map_path=str(block.get("dust_map_path", "data/sky/derived/planck_dust_opacity_eq_u16.npz")),
            dust_density_strength=float(max(0.0, block.get("dust_density_strength", 0.0))),
            dust_extinction_strength=float(max(0.0, block.get("dust_extinction_strength", 0.65))),
            auto_opacity=bool(block.get("auto_opacity", True)),
            is_auto_bortle=bool(block.get("is_auto_bortle", getattr(state, "is_auto_bortle", True))),
            bortle=float(block.get("bortle", getattr(state, "bortle", 1.0))),
            manual_mag_limit=float(block.get("manual_mag_limit", getattr(state, "magnitude_limit", 6.0))),
            scope_enabled=bool(block.get("scope_enabled", getattr(state, "scope_enabled", False))),
            scope_iso=float(max(1.0, block.get("scope_iso", 800.0))),
            scope_exposure_s=float(max(1e-3, block.get("scope_exposure_s", 15.0))),
            scope_aperture_f_number=float(max(0.1, block.get("scope_aperture_f_number", 2.8))),
        )
        return cfg

    def _compute_effective_opacity(self, state, cfg: _OverlayConfig) -> tuple[float, str]:
        if not cfg.auto_opacity:
            return float(clamp(cfg.opacity, 0.0, 1.0)), "manual"

        sun_alt = float(getattr(state, "sun_alt", -90.0))
        if sun_alt >= -6.0:
            return 0.0, "daylight_or_civil_twilight"

        if cfg.is_auto_bortle:
            # En visual realista, la Via Làctia cau ràpid a partir de Bortle 3-4.
            base_opacity = (5.0 - float(cfg.bortle)) / 3.0
            reason = "auto_bortle"
        else:
            # En mode manual, més magnitud límit implica cel més fosc i més detall galàctic.
            # Aquesta corba corregeix la inversió anterior (estava al revés).
            base_opacity = (float(cfg.manual_mag_limit) - 4.0) / 3.5
            reason = "auto_mag_manual"
        base_opacity = float(clamp(base_opacity, 0.0, 1.0))

        if sun_alt > -18.0:
            tw_factor = float(clamp((-6.0 - sun_alt) / 12.0, 0.0, 1.0))
            base_opacity = float(base_opacity * tw_factor)
            reason = "twilight_fade"

        if cfg.scope_enabled:
            iso_term = math.log2(max(1e-6, float(cfg.scope_iso) / 800.0))
            exp_term = math.log2(max(1e-6, float(cfg.scope_exposure_s) / 15.0))
            ap_term = math.log2(max(1e-6, (2.8 / float(cfg.scope_aperture_f_number)) ** 2))
            exposure_factor = iso_term + exp_term + ap_term
            base_opacity = float(clamp(base_opacity - 0.12 * exposure_factor, 0.0, 1.0))
            reason = "photo_scope"

        return float(clamp(base_opacity * float(cfg.opacity), 0.0, 1.0)), reason

    def _build_cache_key(self, ctx, state, cfg: _OverlayConfig, effective_opacity: float) -> tuple:
        cam = getattr(state, "camera", None)
        cam_az = float(getattr(cam, "azimuth_offset", 0.0))
        cam_el = float(getattr(cam, "elevation_angle", 0.0))
        cam_zoom = float(getattr(cam, "zoom_level", 1.0))
        cam_voff = float(getattr(cam, "vertical_offset_ratio", 0.3))
        interaction_active = bool(getattr(state, "interaction_active", False))
        sample_scale_eff = float(self._effective_sample_scale(cfg, interaction_active))
        dust_stamp = self._dust_map_stamp if cfg.dust_map_enabled else None
        lst_deg = float(
            local_sidereal_angle(
                day_of_year=int(getattr(state, "day_of_year", 0)),
                ut_hour=float(getattr(state, "ut_hour", 0.0)),
                longitude_deg=float(getattr(state, "longitude", 0.0)),
            )
        )
        # Fem quantitzacio adaptativa per evitar reconstruccio per variacions minimes.
        cam_step = 0.55 if interaction_active else 0.20
        zoom_step = 0.03 if interaction_active else 0.01
        voff_step = 0.010 if interaction_active else 0.005
        latlon_step = 0.01
        lst_step = 0.25 if interaction_active else 0.10

        return (
            int(ctx.width),
            int(ctx.height),
            round(self._quantize(cam_az % 360.0, cam_step), 3),
            round(self._quantize(cam_el, cam_step), 3),
            round(self._quantize(cam_zoom, zoom_step), 4),
            round(self._quantize(cam_voff, voff_step), 4),
            round(self._quantize(float(getattr(state, "latitude", 0.0)), latlon_step), 3),
            round(self._quantize(float(getattr(state, "longitude", 0.0)), latlon_step), 3),
            round(self._quantize(lst_deg, lst_step), 2),
            int(interaction_active),
            round(float(cfg.ra_offset_deg) % 360.0, 5),
            cfg.coord_frame,
            int(bool(cfg.lat_flip)),
            int(bool(cfg.lon_flip)),
            cfg.blend_mode,
            round(float(effective_opacity), 5),
            round(sample_scale_eff, 3),
            self._texture_stamp,
            dust_stamp,
            round(float(cfg.dust_density_strength), 4),
            round(float(cfg.dust_extinction_strength), 4),
        )

    def _build_overlay_image(self, ctx, state, cfg: _OverlayConfig, effective_opacity: float) -> Optional[QImage]:
        if self._texture_rgba is None:
            return None
        cam = getattr(state, "camera", None)
        if cam is None:
            return None

        full_w = int(ctx.width)
        full_h = int(ctx.height)
        interaction_active = bool(getattr(state, "interaction_active", False))
        sample_scale_eff = float(self._effective_sample_scale(cfg, interaction_active))
        sample_w = max(64, int(round(full_w * sample_scale_eff)))
        sample_h = max(32, int(round(full_h * sample_scale_eff)))

        alt_deg, az_deg = self._screen_grid_to_altaz(
            sample_w=sample_w,
            sample_h=sample_h,
            full_w=full_w,
            full_h=full_h,
            camera=cam,
        )
        ra_deg, dec_deg = self._altaz_to_radec(
            alt_deg=alt_deg,
            az_deg=az_deg,
            latitude_deg=float(getattr(state, "latitude", 0.0)),
            longitude_deg=float(getattr(state, "longitude", 0.0)),
            ut_hour=float(getattr(state, "ut_hour", 0.0)),
            day_of_year=int(getattr(state, "day_of_year", 0)),
        )

        lon_deg, lat_deg = self._to_texture_lon_lat(
            np.asarray(ra_deg, dtype=np.float32),
            np.asarray(dec_deg, dtype=np.float32),
            coord_frame=cfg.coord_frame,
        )

        lat_for_overlay = np.asarray(-lat_deg, dtype=np.float32) if bool(cfg.lat_flip) else np.asarray(lat_deg, dtype=np.float32)
        u_overlay = ((lon_deg + float(cfg.ra_offset_deg)) % 360.0) / 360.0
        if bool(cfg.lon_flip):
            u_overlay = np.asarray(np.mod(1.0 - u_overlay, 1.0), dtype=np.float32)
        v_overlay = 1.0 - ((lat_for_overlay + 90.0) / 180.0)

        rgba = self._bilinear_sample_rgba(self._texture_rgba, u=u_overlay, v=v_overlay)
        if rgba is None:
            return None

        rgb = np.asarray(np.clip(rgba[..., :3] * float(self._texture_rgb_gain), 0.0, 1.0), dtype=np.float32)
        alpha = np.asarray(rgba[..., 3] * float(effective_opacity), dtype=np.float32)

        dust_requested = bool(cfg.dust_map_enabled) and self._dust_map is not None
        dust_strength_active = float(cfg.dust_density_strength) > 0.0 or float(cfg.dust_extinction_strength) > 0.0
        if dust_requested and dust_strength_active and (not interaction_active):
            u_dust = ((lon_deg + float(cfg.ra_offset_deg)) % 360.0) / 360.0
            v_dust = 1.0 - ((lat_deg + 90.0) / 180.0)
            dust = self._bilinear_sample_scalar(self._dust_map, u=u_dust, v=v_dust)
            if dust is not None:
                if float(cfg.dust_density_strength) > 0.0:
                    dens = np.clip(1.0 + float(cfg.dust_density_strength) * dust, 0.0, 4.0)
                    alpha = np.asarray(alpha * dens, dtype=np.float32)
                if float(cfg.dust_extinction_strength) > 0.0:
                    ext = np.clip(1.0 - float(cfg.dust_extinction_strength) * dust, 0.0, 1.0)
                    rgb = np.asarray(rgb * ext[..., None], dtype=np.float32)
        elif dust_requested and dust_strength_active:
            # Durant la interaccio prioritzem fluïdesa i ajornem la modulació de pols.
            pass

        out_rgba = np.empty((sample_h, sample_w, 4), dtype=np.uint8)
        out_rgba[..., :3] = np.asarray(np.clip(np.rint(rgb * 255.0), 0.0, 255.0), dtype=np.uint8)
        out_rgba[..., 3] = np.asarray(np.clip(np.rint(alpha * 255.0), 0.0, 255.0), dtype=np.uint8)

        qimg = QImage(
            out_rgba.data,
            int(sample_w),
            int(sample_h),
            int(sample_w * 4),
            QImage.Format_RGBA8888,
        ).copy()

        if sample_w != full_w or sample_h != full_h:
            # En interaccio prioritzem FPS; en estable prioritzem qualitat.
            scale_mode = Qt.FastTransformation if interaction_active else Qt.SmoothTransformation
            qimg = qimg.scaled(full_w, full_h, Qt.IgnoreAspectRatio, scale_mode)
        return qimg

    def _screen_grid_to_altaz(self, sample_w: int, sample_h: int, full_w: int, full_h: int, camera):
        grid_key = (int(sample_w), int(sample_h), int(full_w), int(full_h))
        if self._screen_grid_cache_key == grid_key and self._screen_grid_cache is not None:
            sx_grid, sy_grid = self._screen_grid_cache
        else:
            sx = np.linspace(0.0, max(0.0, full_w - 1.0), num=sample_w, dtype=np.float32)
            sy = np.linspace(0.0, max(0.0, full_h - 1.0), num=sample_h, dtype=np.float32)
            sx_grid, sy_grid = np.meshgrid(sx, sy)
            self._screen_grid_cache_key = grid_key
            self._screen_grid_cache = (sx_grid, sy_grid)

        scale_h = (float(full_h) * 0.5) * float(getattr(camera, "zoom_level", 1.0))
        if scale_h <= 1e-9:
            scale_h = 1.0
        cx = float(full_w) * 0.5
        cy_base = (float(full_h) * 0.5) + (float(full_h) * float(getattr(camera, "vertical_offset_ratio", 0.3)))
        elev_rad = math.radians(float(getattr(camera, "elevation_angle", 0.0)))
        y_center_val = 2.0 * math.tan(elev_rad * 0.5)

        x = (sx_grid - cx) / scale_h
        y = -((sy_grid - cy_base) / scale_h) + y_center_val

        rho = np.sqrt(x * x + y * y, dtype=np.float32)
        c = 2.0 * np.arctan(rho * 0.5)
        sin_c = np.sin(c)
        cos_c = np.cos(c)

        with np.errstate(divide="ignore", invalid="ignore"):
            lat_rad = np.arcsin(np.where(rho > 1e-9, (y * sin_c) / rho, 0.0))
            lon_rad = np.where(rho > 1e-9, np.arctan2(x * sin_c, rho * cos_c), 0.0)

        alt_deg = np.asarray(np.degrees(lat_rad), dtype=np.float32)
        az_deg = np.asarray((np.degrees(lon_rad) + float(getattr(camera, "azimuth_offset", 0.0))) % 360.0, dtype=np.float32)
        return alt_deg, az_deg

    @staticmethod
    def _quantize(value: float, step: float) -> float:
        step_v = float(max(1e-6, step))
        return round(float(value) / step_v) * step_v

    @staticmethod
    def _effective_sample_scale(cfg: _OverlayConfig, interaction_active: bool) -> float:
        base = float(clamp(cfg.sample_scale, 0.10, 1.0))
        if not interaction_active:
            return base
        return float(clamp(max(0.14, base * 0.62), 0.10, base))

    def _altaz_to_radec(
        self,
        alt_deg: np.ndarray,
        az_deg: np.ndarray,
        latitude_deg: float,
        longitude_deg: float,
        ut_hour: float,
        day_of_year: int,
    ):
        lat = math.radians(float(latitude_deg))
        sin_lat = math.sin(lat)
        cos_lat = math.cos(lat)

        alt_rad = np.radians(np.asarray(alt_deg, dtype=np.float32))
        az_rad = np.radians(np.asarray(az_deg, dtype=np.float32) % 360.0)

        sin_alt = np.sin(alt_rad)
        cos_alt = np.cos(alt_rad)

        sin_dec = sin_alt * sin_lat + cos_alt * cos_lat * np.cos(az_rad)
        sin_dec = np.clip(sin_dec, -1.0, 1.0)
        dec_rad = np.arcsin(sin_dec)
        cos_dec = np.cos(dec_rad)

        sin_ha = -np.sin(az_rad) * cos_alt / (cos_dec + 1e-12)
        cos_ha = (sin_alt - sin_lat * sin_dec) / (cos_lat * cos_dec + 1e-12)
        ha_deg = np.degrees(np.arctan2(sin_ha, cos_ha))

        lst = float(local_sidereal_angle(day_of_year=int(day_of_year), ut_hour=float(ut_hour), longitude_deg=float(longitude_deg)))
        ra_deg = np.asarray((lst - ha_deg) % 360.0, dtype=np.float32)
        dec_deg = np.asarray(np.clip(np.degrees(dec_rad), -90.0, 90.0), dtype=np.float32)
        return ra_deg, dec_deg

    def _sample_overlay_rgba(
        self,
        ra_deg: np.ndarray,
        dec_deg: np.ndarray,
        ra_offset_deg: float,
        *,
        coord_frame: str = "galactic",
        lat_flip: bool = False,
        lon_flip: bool = False,
    ) -> Optional[np.ndarray]:
        tex = self._texture_rgba
        if tex is None:
            return None
        lon_deg, lat_deg = self._to_texture_lon_lat(
            np.asarray(ra_deg, dtype=np.float32),
            np.asarray(dec_deg, dtype=np.float32),
            coord_frame=coord_frame,
        )
        if bool(lat_flip):
            lat_deg = np.asarray(-lat_deg, dtype=np.float32)
        u = ((lon_deg + float(ra_offset_deg)) % 360.0) / 360.0
        if bool(lon_flip):
            u = np.asarray(np.mod(1.0 - u, 1.0), dtype=np.float32)
        v = 1.0 - ((lat_deg + 90.0) / 180.0)
        return self._bilinear_sample_rgba(tex, u=u, v=v)

    def _sample_dust_map(
        self,
        ra_deg: np.ndarray,
        dec_deg: np.ndarray,
        ra_offset_deg: float,
        *,
        coord_frame: str = "galactic",
    ) -> Optional[np.ndarray]:
        dust = self._dust_map
        if dust is None:
            return None
        lon_deg, lat_deg = self._to_texture_lon_lat(
            np.asarray(ra_deg, dtype=np.float32),
            np.asarray(dec_deg, dtype=np.float32),
            coord_frame=coord_frame,
        )
        u = ((lon_deg + float(ra_offset_deg)) % 360.0) / 360.0
        v = 1.0 - ((lat_deg + 90.0) / 180.0)
        return self._bilinear_sample_scalar(dust, u=u, v=v)

    @staticmethod
    def _to_texture_lon_lat(ra_deg: np.ndarray, dec_deg: np.ndarray, *, coord_frame: str) -> tuple[np.ndarray, np.ndarray]:
        frame = str(coord_frame or "galactic").strip().lower()
        ra = np.asarray(ra_deg, dtype=np.float32) % 360.0
        dec = np.asarray(np.clip(dec_deg, -90.0, 90.0), dtype=np.float32)
        if frame == "equatorial":
            return ra, dec
        if frame != "galactic":
            return ra, dec
        return MilkyWayOverlay._equatorial_to_galactic_deg(ra, dec)

    @staticmethod
    def _equatorial_to_galactic_deg(ra_deg: np.ndarray, dec_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        ra_rad = np.radians(np.asarray(ra_deg, dtype=np.float32))
        dec_rad = np.radians(np.asarray(np.clip(dec_deg, -90.0, 90.0), dtype=np.float32))
        cos_dec = np.cos(dec_rad)
        x_eq = cos_dec * np.cos(ra_rad)
        y_eq = cos_dec * np.sin(ra_rad)
        z_eq = np.sin(dec_rad)

        # Matriu IAU 2000 (J2000): equatorial -> galàctic.
        x_gal = (-0.0548755604 * x_eq) + (-0.8734370902 * y_eq) + (-0.4838350155 * z_eq)
        y_gal = (0.4941094279 * x_eq) + (-0.4448296300 * y_eq) + (0.7469822445 * z_eq)
        z_gal = (-0.8676661490 * x_eq) + (-0.1980763734 * y_eq) + (0.4559837762 * z_eq)

        l_deg = np.asarray((np.degrees(np.arctan2(y_gal, x_gal)) + 360.0) % 360.0, dtype=np.float32)
        b_deg = np.asarray(np.degrees(np.arcsin(np.clip(z_gal, -1.0, 1.0))), dtype=np.float32)
        return l_deg, b_deg

    @staticmethod
    def _bilinear_sample_rgba(tex: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        h, w, _ = tex.shape
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

        c00 = tex[y0, x0]
        c10 = tex[y0, x1]
        c01 = tex[y1, x0]
        c11 = tex[y1, x1]

        tx3 = tx[..., None]
        ty3 = ty[..., None]
        top = c00 * (1.0 - tx3) + c10 * tx3
        bottom = c01 * (1.0 - tx3) + c11 * tx3
        return np.asarray(top * (1.0 - ty3) + bottom * ty3, dtype=np.float32)

    @staticmethod
    def _bilinear_sample_scalar(tex: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        h, w = tex.shape
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

        c00 = tex[y0, x0]
        c10 = tex[y0, x1]
        c01 = tex[y1, x0]
        c11 = tex[y1, x1]

        top = c00 * (1.0 - tx) + c10 * tx
        bottom = c01 * (1.0 - tx) + c11 * tx
        return np.asarray(top * (1.0 - ty) + bottom * ty, dtype=np.float32)

    def _ensure_overlay_texture(self, texture_path: str) -> bool:
        resolved = self._resolve_path(texture_path)
        stamp = self._file_stamp(resolved)
        if (
            self._texture_rgba is not None
            and self._texture_path_resolved == resolved
            and self._texture_stamp == stamp
        ):
            return True

        if not resolved or not os.path.exists(resolved):
            if not self._warned_missing_texture:
                print(f"[MilkyWayOverlay] Texture not found: {texture_path}")
                self._warned_missing_texture = True
            self._texture_rgba = None
            self._texture_rgb_gain = 1.0
            self._texture_path_resolved = resolved
            self._texture_stamp = stamp
            self._invalidate_overlay_cache()
            return False

        image = QImage(resolved)
        if image.isNull():
            print(f"[MilkyWayOverlay] Could not load texture: {resolved}")
            self._texture_rgba = None
            self._texture_rgb_gain = 1.0
            self._texture_path_resolved = resolved
            self._texture_stamp = stamp
            self._invalidate_overlay_cache()
            return False

        image = image.convertToFormat(QImage.Format_RGBA8888)
        rgba = self._qimage_to_numpy_rgba(image)
        if rgba is None:
            self._texture_rgba = None
            self._texture_rgb_gain = 1.0
            self._texture_path_resolved = resolved
            self._texture_stamp = stamp
            self._invalidate_overlay_cache()
            return False

        self._texture_rgba = np.asarray(rgba / 255.0, dtype=np.float32)
        self._texture_rgb_gain = self._estimate_texture_gain(self._texture_rgba)
        self._texture_path_resolved = resolved
        self._texture_stamp = stamp
        self._warned_missing_texture = False
        if not self._reported_texture_loaded:
            print(
                f"[MilkyWayOverlay] Texture loaded: {resolved} "
                f"(rgb_gain={self._texture_rgb_gain:.2f})"
            )
            self._reported_texture_loaded = True
        self._invalidate_overlay_cache()
        return True

    def _ensure_dust_map(self, dust_map_path: str) -> bool:
        resolved = self._resolve_path(dust_map_path)
        stamp = self._file_stamp(resolved)
        if (
            self._dust_map is not None
            and self._dust_map_path_resolved == resolved
            and self._dust_map_stamp == stamp
        ):
            return True

        data = self._load_dust_map_array(resolved)
        if data is None:
            if not self._warned_missing_dust:
                print(f"[MilkyWayOverlay] Dust map unavailable: {dust_map_path}")
                self._warned_missing_dust = True
            self._dust_map = None
            self._dust_map_path_resolved = resolved
            self._dust_map_stamp = stamp
            self._invalidate_overlay_cache()
            return False

        self._dust_map = np.asarray(np.clip(data, 0.0, 1.0), dtype=np.float32)
        self._dust_map_path_resolved = resolved
        self._dust_map_stamp = stamp
        self._warned_missing_dust = False
        if not self._reported_dust_loaded:
            print(f"[MilkyWayOverlay] Dust map loaded: {resolved}")
            self._reported_dust_loaded = True
        self._invalidate_overlay_cache()
        return True

    def _load_dust_map_array(self, path: Optional[str]) -> Optional[np.ndarray]:
        if np is None or not path or (not os.path.exists(path)):
            return None

        ext = Path(path).suffix.lower()
        try:
            if ext == ".npz":
                with np.load(path) as payload:
                    for key in ("opacity_u16", "dust_u16", "data_u16", "opacity", "dust", "data"):
                        if key in payload:
                            arr = np.asarray(payload[key])
                            return self._normalize_dust_array(arr)
                    keys = list(payload.keys())
                    if keys:
                        return self._normalize_dust_array(np.asarray(payload[keys[0]]))
                return None

            if ext == ".png":
                img = QImage(path)
                if img.isNull():
                    return None
                gray = img.convertToFormat(QImage.Format_Grayscale8)
                ptr = gray.bits()
                ptr.setsize(gray.byteCount())
                arr = np.frombuffer(ptr, dtype=np.uint8).reshape(gray.height(), gray.bytesPerLine())
                arr = arr[:, : gray.width()].copy()
                return np.asarray(arr / 255.0, dtype=np.float32)

            if path.lower().endswith(".npy.zst"):
                try:
                    import zstandard as zstd
                except Exception:
                    return None
                with open(path, "rb") as fh:
                    compressed = fh.read()
                decompressor = zstd.ZstdDecompressor()
                raw = decompressor.decompress(compressed)
                arr = np.load(io.BytesIO(raw))
                return self._normalize_dust_array(np.asarray(arr))

            if ext == ".npy":
                arr = np.load(path)
                return self._normalize_dust_array(np.asarray(arr))
        except Exception as exc:
            print(f"[MilkyWayOverlay] Could not load dust map '{path}': {exc}")
            return None

        return None

    @staticmethod
    def _normalize_dust_array(arr: np.ndarray) -> Optional[np.ndarray]:
        if np is None or arr is None:
            return None
        if arr.ndim != 2:
            return None
        if arr.dtype == np.uint16:
            return np.asarray(arr / 65535.0, dtype=np.float32)
        arr_f = np.asarray(arr, dtype=np.float32)
        if not np.isfinite(arr_f).any():
            return None
        min_v = float(np.nanmin(arr_f))
        max_v = float(np.nanmax(arr_f))
        if max_v <= min_v + 1e-12:
            return np.zeros_like(arr_f, dtype=np.float32)
        return np.asarray((arr_f - min_v) / (max_v - min_v), dtype=np.float32)

    @staticmethod
    def _qimage_to_numpy_rgba(image: QImage) -> Optional[np.ndarray]:
        if np is None or image.isNull():
            return None
        ptr = image.bits()
        ptr.setsize(image.byteCount())
        arr = np.frombuffer(ptr, dtype=np.uint8).reshape(image.height(), image.bytesPerLine() // 4, 4)
        return arr[:, : image.width(), :].copy()

    @staticmethod
    def _estimate_texture_gain(tex_rgba: np.ndarray) -> float:
        if np is None or tex_rgba is None or tex_rgba.ndim != 3 or tex_rgba.shape[2] < 3:
            return 1.0
        rgb = np.asarray(tex_rgba[..., :3], dtype=np.float32)
        if rgb.size <= 0:
            return 1.0
        # Ajust automàtic per overlays foscos: no enfosqueix textures ja ben exposades.
        lum = np.max(rgb, axis=2)
        p_hi = float(np.percentile(lum, 99.99))
        if p_hi <= 1e-6:
            return 1.0
        gain = 0.62 / p_hi
        return float(clamp(gain, 1.0, 4.5))

    @staticmethod
    def _composition_mode(blend_mode: str):
        mode = str(blend_mode or "normal").lower()
        if mode == "add":
            return QPainter.CompositionMode_Plus
        if mode == "screen":
            return QPainter.CompositionMode_Screen
        return QPainter.CompositionMode_SourceOver

    @staticmethod
    def _resolve_path(path_value: str) -> Optional[str]:
        if not path_value:
            return None
        path_str = str(path_value)
        if os.path.isabs(path_str):
            return path_str
        resolved = resource_path(path_str.replace("\\", "/"))
        return os.path.abspath(resolved)

    @staticmethod
    def _file_stamp(path_value: Optional[str]) -> Optional[tuple]:
        if not path_value or not os.path.exists(path_value):
            return None
        st = os.stat(path_value)
        return (int(st.st_size), int(st.st_mtime))

    def _invalidate_overlay_cache(self) -> None:
        self._cached_overlay_key = None
        self._cached_overlay_image = None
