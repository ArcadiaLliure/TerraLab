"""Application orchestration for the already-resolved celestial scene plans."""

from __future__ import annotations

import math
from typing import Mapping

import numpy as np

from TerraLab.astro.apparent import resolve_ephemeris_snapshot
from TerraLab.data.catalogs.render_cache import StarCatalogRenderCache
from TerraLab.data.sky_resources import SkyResourceRepository
from TerraLab.scene.camera import Camera
from TerraLab.scene.contracts import SceneFrame
from TerraLab.scene.photometry import VisualMagnitudeEngine, VisualMagnitudeInputs
from TerraLab.scene.plans.bodies import CelestialBodiesPlanner, CelestialTrailsPlanner
from TerraLab.scene.plans.celestial import CelestialLayerPlans
from TerraLab.scene.plans.deep_sky import DeepSkyPlanner, MilkyWayPlanner
from TerraLab.scene.plans.sky import SkyBackgroundInputs, SkyBackgroundPlanner
from TerraLab.scene.plans.stars import StarScenePlanner
from TerraLab.scene.render_state import RenderState
from TerraLab.scene.spherical_math import altaz_to_ra_dec


class CelestialPlanCoordinator:
    """Coordinate Model planners and data handles; never paints or builds v1."""

    def __init__(self) -> None:
        self._catalogs = StarCatalogRenderCache()
        self._sky_resources = SkyResourceRepository()
        self._sky = SkyBackgroundPlanner()
        self._stars = StarScenePlanner()
        self._bodies = CelestialBodiesPlanner()
        self._trails = CelestialTrailsPlanner()
        self._milkyway = MilkyWayPlanner()
        self._deep_sky = DeepSkyPlanner()

    def close(self) -> None:
        self._catalogs.close()
        self._sky_resources.close()
        self._trails.clear_cache()

    def build(self, frame: SceneFrame) -> CelestialLayerPlans:
        """Resolve all celestial capabilities once from the typed frame inputs."""

        catalog = self._catalogs.load(frame.resources.catalog)
        resolved = resolve_ephemeris_snapshot(
            frame.ephemeris.payload,
            year_utc=frame.time.year_utc,
            day_of_year_utc=frame.time.day_of_year_utc,
            ut_hour=frame.time.ut_hour,
            latitude=frame.observer.latitude,
            longitude=frame.observer.longitude,
        )
        state = self._state(frame, catalog, resolved.snapshot, resolved.sun_altitude_deg, resolved.sun_azimuth_deg, resolved.eclipse_transmission)
        width, height = int(frame.viewport.width), int(frame.viewport.height)
        sky = self._sky.build_plan(
            SkyBackgroundInputs(
                viewport=frame.viewport,
                camera=state.camera,
                sun_altitude_deg=state.sun_alt,
                sun_azimuth_deg=state.sun_az,
                bortle_class=state.bortle,
                eclipse_transmission=resolved.eclipse_transmission,
                interaction_active=state.interaction_active,
            )
        )
        stars = self._stars.build(state, width=width, height=height)
        bodies = self._bodies.plan(state, width, height)
        trails = self._trails.plan(
            state,
            width,
            height,
            {"enabled": frame.trails.enabled, "start_hour": frame.trails.start_hour},
        )
        milkyway = self._milkyway.plan(
            width=width,
            height=height,
            state=state,
            texture=self._sky_resources.load_texture(
                frame.resources.milkyway_texture.path,
                version=frame.resources.milkyway_texture.version,
            ),
            dust=(
                self._sky_resources.load_texture(
                    frame.resources.dust_map.path,
                    version=frame.resources.dust_map.version,
                    dust=True,
                )
                if frame.milkyway.dust_map_enabled
                else None
            ),
        )
        deep_sky = self._deep_sky.plan(
            width=width,
            height=height,
            state=state,
            catalog=self._sky_resources.load_catalog(
                frame.resources.ngc.path,
                version=frame.resources.ngc.version,
            ),
        )
        return CelestialLayerPlans(
            generation=frame.generation,
            sky_background=sky,
            stars=stars,
            bodies=bodies,
            trails=trails,
            milkyway=milkyway,
            deep_sky=deep_sky,
            render_state=state,
            pure_colors=frame.presentation.pure_colors,
        )

    def _state(
        self,
        frame: SceneFrame,
        catalog,
        ephemeris: Mapping[str, object],
        sun_alt: float,
        sun_az: float,
        eclipse_transmission: float,
    ) -> RenderState:
        camera = Camera(
            azimuth_offset=frame.camera.azimuth,
            elevation_angle=frame.camera.elevation,
            zoom_level=frame.camera.zoom,
            vertical_offset_ratio=frame.camera.vertical_ratio,
        )
        layers = frozenset(layer.value for layer in frame.layers.order)
        extras: dict[str, object] = {
            "stars_enabled": "stars" in layers,
            "star_catalog_resource": catalog.resource,
            "catalog_mag_sorted": frame.scope.settings.catalog_mag_sorted,
            "eclipse_factor": eclipse_transmission,
            "milkyway_overlay": {
                "enabled": frame.milkyway.enabled and "milkyway" in layers,
                "opacity": frame.milkyway.opacity,
                "blend_mode": frame.milkyway.blend_mode,
                "ra_offset_deg": frame.milkyway.ra_offset_deg,
                "coord_frame": frame.milkyway.coord_frame,
                "lat_flip": frame.milkyway.lat_flip,
                "lon_flip": frame.milkyway.lon_flip,
                "sample_scale": frame.milkyway.sample_scale,
                "dust_map_enabled": frame.milkyway.dust_map_enabled,
                "dust_density_strength": frame.milkyway.dust_density_strength,
                "dust_extinction_strength": frame.milkyway.dust_extinction_strength,
                "light_pollution_mode": frame.light_pollution_mode.value,
                "bortle": frame.bortle,
                "magnitude_limit": frame.magnitude_limit,
                "scope_enabled": frame.scope.enabled,
                "scope_iso": frame.scope.settings.iso,
                "scope_exposure_s": frame.scope.settings.exposure_s,
                "scope_aperture_f_number": frame.scope.settings.aperture_f_number,
            },
        }
        self._scope_extras(extras, frame, sun_alt, len(catalog.resource.ra_deg))
        magnitude_limit = self._magnitude_limit(
            frame.magnitude_limit, sun_alt, eclipse_transmission
        )
        return RenderState(
            ut_hour=frame.time.ut_hour,
            day_of_year_utc=frame.time.day_of_year_utc,
            year_utc=frame.time.year_utc,
            latitude=frame.observer.latitude,
            longitude=frame.observer.longitude,
            altitude_m=frame.observer.altitude_m,
            camera=camera,
            np_ra=catalog.resource.ra_deg,
            np_dec=catalog.resource.dec_deg,
            np_mag=catalog.resource.magnitude,
            np_r=catalog.red,
            np_g=catalog.green,
            np_b=catalog.blue,
            np_bp_rp=(catalog.resource.bp_rp if catalog.resource.bp_rp is not None else np.empty(0, dtype=np.float32)),
            ephemeris_snapshot=dict(ephemeris),
            bortle=frame.bortle,
            mag_limit=magnitude_limit,
            light_pollution_mode=frame.light_pollution_mode.value,
            naked_eye_cap=frame.presentation.naked_eye_cap,
            sun_alt=sun_alt,
            sun_az=sun_az,
            layers_enabled=layers,
            extras=extras,
            pure_colors=frame.presentation.pure_colors,
            spike_magnitude_threshold=frame.presentation.spike_magnitude_threshold,
            interaction_active=frame.presentation.interaction_active,
            star_scale=frame.presentation.star_scale,
            auto_star_scale_multiplier=frame.presentation.auto_star_scale_multiplier,
            scope_k_fallback=frame.presentation.scope_k_fallback,
            scope_enabled=frame.scope.enabled,
            scope_center_sky=frame.scope.center_sky,
            scope_fov_deg=frame.scope.fov_deg,
        )

    @staticmethod
    def _magnitude_limit(night_limit: float, sun_alt: float, eclipse: float) -> float:
        if sun_alt > -1.0 and eclipse < 0.18:
            return min(night_limit, 1.0 + 3.0 * (1.0 - max(0.0, eclipse) / 0.18))
        if sun_alt > 0.0:
            return -10.0
        if sun_alt > -6.0:
            return -10.0 + 10.0 * (sun_alt / -6.0)
        if sun_alt > -12.0:
            return 3.0 * ((sun_alt + 6.0) / -6.0)
        if sun_alt > -18.0:
            return 3.0 + (night_limit - 3.0) * ((sun_alt + 12.0) / -6.0)
        return float(night_limit)

    @staticmethod
    def _scope_extras(extras: dict[str, object], frame: SceneFrame, sun_alt: float, catalog_rows: int) -> None:
        if not frame.scope.enabled:
            return
        settings = frame.scope.settings
        focal = max(1.0, settings.focal_mm)
        aperture = focal / max(0.7, settings.aperture_f_number) if settings.aperture_input_mode == "f_number" else max(1.0, settings.aperture_mm)
        dark_pupil = max(2.2, settings.eye_pupil_dark_mm)
        eye_pupil = 2.2 if sun_alt >= 0.0 else dark_pupil if sun_alt <= -18.0 else 2.2 + (dark_pupil - 2.2) * (-sun_alt / 18.0)
        result = VisualMagnitudeEngine().compute(
            VisualMagnitudeInputs(
                aperture_mm=aperture,
                telescope_focal_mm=focal,
                eyepiece_focal_mm=(max(0.5, settings.eyepiece_mm) if settings.instrument_profile == "telescope" else focal),
                eye_pupil_mm=eye_pupil,
                atmospheric_loss_mag=settings.atmospheric_loss_mag,
                light_pollution_mode=frame.light_pollution_mode.value,
                bortle_class=frame.bortle,
                magnitude_limit=frame.magnitude_limit,
                exposure_seconds=settings.exposure_s,
                iso=settings.iso,
                instrument_profile=settings.instrument_profile,
                sensor_profile=settings.sensor_profile,
            )
        )
        extras.update(
            scope_limit_mag=result.scope_limit_mag,
            scope_dataset_max_mag=settings.dataset_max_mag if catalog_rows else -12.0,
            scope_exposure_gain_mag=result.exposure_gain_mag,
            scope_aperture_gain_mag=result.aperture_gain_mag,
            scope_depth_gain_mag=max(0.0, result.scope_limit_mag - result.eye_limit_mag),
            scope_signal_gain=max(1.0, min(6.0, result.star_scale_factor)),
            scope_halo_gain=max(1.0, min(5.0, 0.9 + 0.8 * result.star_scale_factor)),
            scope_alpha_gain=max(1.0, min(4.8, 0.8 + 0.9 * result.star_scale_factor)),
            scope_size_gain=max(0.74, min(2.2, 0.75 + 0.35 * result.star_scale_factor)),
            scope_limit_extra_mag=max(0.0, min(4.0, 0.55 * result.exposure_gain_mag)),
            scope_fov_diag_deg=math.hypot(*frame.scope.fov_deg),
        )
        if frame.scope.center_sky is not None:
            center_ra, center_dec = altaz_to_ra_dec(
                frame.scope.center_sky[0], frame.scope.center_sky[1], frame.time.ut_hour, frame.time.day_of_year_utc, frame.observer.latitude, frame.observer.longitude, year=frame.time.year_utc
            )
            dec_pad = min(90.0, max(3.0, math.hypot(*frame.scope.fov_deg) * 0.65 + 2.5))
            extras.update(
                scope_center_ra_deg=center_ra,
                scope_center_dec_deg=center_dec,
                scope_preselect_dec_pad_deg=dec_pad,
                scope_preselect_ra_pad_deg=min(180.0, dec_pad / max(0.12, math.cos(math.radians(center_dec))) + 2.0),
            )
