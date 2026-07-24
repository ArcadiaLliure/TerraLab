"""Reproducible real-DEM comparison for the 2.5D terrain pipeline.

The command uses identical observer/camera/image parameters for the legacy
fallback and enhanced paths and writes PNG captures plus JSON metrics.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
from PyQt5.QtGui import QColor, QImage, QPainter

from TerraLab.common.utils import get_config_value
from TerraLab.terrain.crs import meridian_convergence_degrees
from TerraLab.terrain.engine import HorizonBaker, HorizonProfile, generate_bands
from TerraLab.terrain.overlay import HorizonOverlay, generate_layer_defs
from TerraLab.terrain.providers import create_elevation_provider
from TerraLab.terrain.render_pipeline import (
    TerrainCelestialLightContext,
    TerrainRenderSettings,
    TerrainSamplingSettings,
)
from TerraLab.terrain.representation import (
    TerrainGeometrySource,
    TerrainRepresentationMode,
)
from TerraLab.terrain.visibility_range import EARTH_RADIUS_M, TerrainRangeSettings


def _projection(width: int, height: int, azimuth: float, fov: float):
    pixels_per_azimuth_degree = float(width) / float(fov)
    pixels_per_altitude_degree = float(height) / 45.0
    horizon_y = float(height) * 0.64

    def scalar(altitude, point_azimuth):
        relative = (
            (float(point_azimuth) - float(azimuth) + 180.0) % 360.0
        ) - 180.0
        return (
            float(width) * 0.5 + relative * pixels_per_azimuth_degree,
            horizon_y - float(altitude) * pixels_per_altitude_degree,
        )

    def vectorized(altitude, point_azimuth):
        altitude_array, azimuth_array = np.broadcast_arrays(
            np.asarray(altitude, dtype=np.float64),
            np.asarray(point_azimuth, dtype=np.float64),
        )
        relative = (
            (azimuth_array - float(azimuth) + 180.0) % 360.0
        ) - 180.0
        return (
            float(width) * 0.5 + relative * pixels_per_azimuth_degree,
            horizon_y - altitude_array * pixels_per_altitude_degree,
            np.isfinite(altitude_array) & np.isfinite(azimuth_array),
        )

    return scalar, vectorized


def _build_profile(
    dem_path: str,
    *,
    latitude: float,
    longitude: float,
    observer_offset_m: float,
    maximum_distance_m: float,
    ray_step_deg: float,
    sampling_settings: TerrainSamplingSettings,
    viewport_height: int,
) -> tuple[HorizonProfile, dict]:
    provider = create_elevation_provider(dem_path)
    try:
        observer_x, observer_y = provider.transform_coordinates(
            latitude, longitude
        )
        ground = provider.get_elevation(observer_x, observer_y)
        if ground is None:
            raise RuntimeError("Observer is outside DEM coverage")
        range_raw = get_config_value("terrain_visibility_range", {})
        range_settings = TerrainRangeSettings.from_mapping(
            range_raw if isinstance(range_raw, dict) else {}
        )
        effective_radius = EARTH_RADIUS_M * (
            range_settings.effective_earth_radius_factor
            if range_settings.atmospheric_refraction_enabled
            else 1.0
        )
        baker = HorizonBaker(
            provider,
            grid_convergence_deg=meridian_convergence_degrees(
                longitude, latitude
            ),
            sampling_settings=sampling_settings,
            sampling_pixels_per_radian=(viewport_height / 45.0)
            * (180.0 / math.pi),
        )
        baker.R = effective_radius
        definitions = generate_bands(20, max_dist_m=maximum_distance_m)
        started = time.perf_counter()
        azimuths, bands, domes, peaks, resolved = baker.bake_progressive(
            obs_x=observer_x,
            obs_y=observer_y,
            obs_h_ground=float(ground) + float(observer_offset_m),
            step_m=50.0,
            d_max=maximum_distance_m,
            delta_az_deg=ray_step_deg,
            band_defs=definitions,
        )
        bake_s = time.perf_counter() - started
        mesh = baker.build_view_mesh(
            obs_x=observer_x,
            obs_y=observer_y,
            obs_h_ground=float(ground) + float(observer_offset_m),
            d_max=maximum_distance_m,
            delta_az_deg=ray_step_deg,
        )
        profile = HorizonProfile(
            azimuths=azimuths,
            bands=bands,
            observer_lat=latitude,
            observer_lon=longitude,
            light_domes=domes,
            light_peak_distances=peaks,
            resolved_mask=resolved,
            terrain_mesh=mesh,
            resolved_radius_m=maximum_distance_m,
            representation_mode=TerrainRepresentationMode.RELIEF,
            geometry_source=TerrainGeometrySource.REAL_ELEVATION,
            geometry_id=(
                f"benchmark:{latitude:.8f}:{longitude:.8f}:"
                f"{sampling_settings.adaptive_sampling_enabled}"
            ),
            observer_x=observer_x,
            observer_y=observer_y,
            grid_convergence_deg=baker.grid_convergence_deg,
        )
        profile._band_defs = definitions
        metrics = {
            "observer_dem_m": float(ground),
            "bake_wall_s": float(bake_s),
            "sampling": dict(baker.last_sampling_metrics),
            "mesh": dict(baker.last_mesh_metrics),
        }
        return profile, metrics
    finally:
        provider.close()


def _render_profile(
    profile: HorizonProfile,
    settings: TerrainRenderSettings,
    *,
    output_path: Path,
    width: int,
    height: int,
    azimuth: float,
    fov: float,
) -> dict:
    overlay = HorizonOverlay(allow_procedural_fallback=False)
    overlay.render_settings = settings
    overlay.set_profile(profile, generate_layer_defs(profile.bands))

    def render_once(
        view_azimuth: float = azimuth,
        *,
        interaction_active: bool = False,
        light_context: TerrainCelestialLightContext | None = None,
    ) -> tuple[QImage, float]:
        scalar_projection, vector_projection = _projection(
            width, height, view_azimuth, fov
        )
        image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor(151, 190, 219))
        painter = QPainter(image)
        started = time.perf_counter()
        context = light_context or TerrainCelestialLightContext(
            35.0, 315.0
        )
        overlay.draw(
            painter,
            scalar_projection,
            width,
            height,
            view_azimuth,
            1.0,
            0.0,
            12.0,
            projection_fn_numpy=vector_projection,
            terrain_3d_enabled=True,
            interaction_active=interaction_active,
            light_context=context,
        )
        elapsed = time.perf_counter() - started
        painter.end()
        return image, elapsed

    image, cold_s = render_once()
    cold_metrics = {
        "geometry_s": float(overlay._last_surface2d_geometry_s),
        "colors_s": float(overlay._last_terrain_color_s),
        "rasterization_s": float(overlay._last_terrain_raster_s),
        "horizon_antialias_s": float(overlay._last_horizon_antialias_s),
        "terrain_total_s": float(overlay._last_terrain_total_s),
        "triangles_in_view": int(overlay._last_surface2d_quads),
        "triangle_indices_in_view": int(overlay._last_surface2d_vertices),
    }
    # Shift the real projection for each sample. Three trials make the reported
    # steady-state result less sensitive to scheduling and filesystem activity.
    warm_trials = []
    warm_detail_trials = []
    for offset in (0.25, 0.50, 0.75):
        _warm_image, elapsed = render_once(azimuth + offset)
        warm_trials.append(float(elapsed))
        warm_detail_trials.append(
            (
                float(overlay._last_surface2d_geometry_s),
                float(overlay._last_terrain_color_s),
                float(overlay._last_terrain_raster_s),
                float(overlay._last_horizon_antialias_s),
                float(overlay._last_terrain_total_s),
            )
        )
    warm_medians = np.median(np.asarray(warm_detail_trials), axis=0)
    warm_metrics = {
        "warm_geometry_s": float(warm_medians[0]),
        "warm_colors_s": float(warm_medians[1]),
        "warm_rasterization_s": float(warm_medians[2]),
        "warm_horizon_antialias_s": float(warm_medians[3]),
        "warm_terrain_total_s": float(warm_medians[4]),
    }
    _cached_image, cached_s = render_once(azimuth + 0.75)
    interaction_trials = []
    for offset in (1.00, 1.25, 1.50):
        _interaction_image, elapsed = render_once(
            azimuth + offset, interaction_active=True
        )
        interaction_trials.append(float(elapsed))

    def cache_snapshot() -> dict:
        diagnostics = overlay.terrain_cache_diagnostics()
        return {
            "base_material_builds": int(
                diagnostics["base_material"]["builds"]
            ),
            "lighting_builds": int(diagnostics["lighting"]["builds"]),
            "resolved_material_builds": int(
                diagnostics["resolved_material"]["builds"]
            ),
            "raster_builds": int(diagnostics["raster"]["builds"]),
        }

    def cache_scenario(name, views, contexts) -> dict:
        before = cache_snapshot()
        trials = []
        for view, context in zip(views, contexts):
            _scenario_image, elapsed = render_once(
                float(view), light_context=context
            )
            trials.append(float(elapsed))
        after = cache_snapshot()
        return {
            "name": name,
            "trials_s": trials,
            "median_s": float(np.median(trials)),
            "build_deltas": {
                key: int(after[key] - before[key])
                for key in before
            },
        }

    fixed_noon = TerrainCelestialLightContext(35.0, 315.0)
    cache_rotation = cache_scenario(
        "continuous_rotation_fixed_time",
        (azimuth + 2.0, azimuth + 2.25, azimuth + 2.5),
        (fixed_noon,) * 3,
    )
    fixed_view = azimuth + 2.5
    # Establish one projected/rasterized material before advancing time.
    render_once(fixed_view, light_context=fixed_noon)
    cache_time = cache_scenario(
        "time_advance_fixed_camera",
        (fixed_view,) * 3,
        (
            TerrainCelestialLightContext(22.0, 250.0),
            TerrainCelestialLightContext(4.0, 270.0),
            TerrainCelestialLightContext(
                -24.0, 300.0, 42.0, 120.0, 0.94
            ),
        ),
    )
    cache_combined = cache_scenario(
        "rotation_and_clock",
        (azimuth + 3.0, azimuth + 3.25, azimuth + 3.5),
        (
            TerrainCelestialLightContext(28.0, 240.0),
            TerrainCelestialLightContext(8.0, 265.0),
            TerrainCelestialLightContext(
                -20.0, 290.0, 35.0, 105.0, 0.72
            ),
        ),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(output_path)):
        raise RuntimeError(f"Could not save {output_path}")
    mesh = profile.terrain_mesh or {}
    vertex_count = int(np.asarray(mesh.get("altitudes", ())).size)
    return {
        "cold_render_s": float(cold_s),
        "warm_uncached_render_s": float(np.median(warm_trials)),
        "warm_uncached_render_trials_s": warm_trials,
        "cached_render_s": float(cached_s),
        "interactive_motion_render_s": float(np.median(interaction_trials)),
        "interactive_motion_render_trials_s": interaction_trials,
        "cache_scenarios": {
            "rotation_fixed_time": cache_rotation,
            "time_fixed_camera": cache_time,
            "rotation_and_clock": cache_combined,
        },
        "cache_diagnostics": overlay.terrain_cache_diagnostics(),
        **cold_metrics,
        **warm_metrics,
        "vertices": vertex_count,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dem-path", default=str(get_config_value("raster_path", "") or "")
    )
    parser.add_argument("--latitude", type=float, default=41.193151965759704)
    parser.add_argument("--longitude", type=float, default=1.2025659030876152)
    parser.add_argument("--observer-offset-m", type=float, default=0.0)
    parser.add_argument("--azimuth", type=float, default=288.329401)
    parser.add_argument("--fov", type=float, default=60.0)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--maximum-distance-km", type=float, default=50.0)
    parser.add_argument("--ray-step-deg", type=float, default=1.0)
    parser.add_argument("--output-dir", default="docs/images/terrain_comparison")
    args = parser.parse_args(argv)
    if not args.dem_path:
        raise SystemExit("A DEM path is required")
    output_dir = Path(args.output_dir)

    baseline_sampling = TerrainSamplingSettings(
        adaptive_sampling_enabled=False
    )
    enhanced_sampling = TerrainSamplingSettings()
    baseline_render = TerrainRenderSettings(
        terrain_lighting_enabled=False,
        terrain_shading_mode="vertex",
        atmospheric_perspective_enabled=False,
        horizon_antialiasing_enabled=False,
    )
    enhanced_render = TerrainRenderSettings()
    shared = {
        "latitude": args.latitude,
        "longitude": args.longitude,
        "observer_offset_m": args.observer_offset_m,
        "maximum_distance_m": args.maximum_distance_km * 1000.0,
        "ray_step_deg": args.ray_step_deg,
        "viewport_height": args.height,
    }

    baseline_profile, baseline_metrics = _build_profile(
        args.dem_path,
        sampling_settings=baseline_sampling,
        **shared,
    )
    baseline_metrics["render"] = _render_profile(
        baseline_profile,
        baseline_render,
        output_path=output_dir / "before.png",
        width=args.width,
        height=args.height,
        azimuth=args.azimuth,
        fov=args.fov,
    )
    enhanced_profile, enhanced_metrics = _build_profile(
        args.dem_path,
        sampling_settings=enhanced_sampling,
        **shared,
    )
    enhanced_metrics["render"] = _render_profile(
        enhanced_profile,
        enhanced_render,
        output_path=output_dir / "after.png",
        width=args.width,
        height=args.height,
        azimuth=args.azimuth,
        fov=args.fov,
    )

    result = {
        "scenario": {
            "latitude": args.latitude,
            "longitude": args.longitude,
            "observer_offset_m": args.observer_offset_m,
            "observer_eye_height_m": 1.7,
            "azimuth_deg": args.azimuth,
            "view_elevation_deg": 0.0,
            "fov_deg": args.fov,
            "resolution": [args.width, args.height],
            "maximum_distance_km": args.maximum_distance_km,
            "ray_step_deg": args.ray_step_deg,
            "terrain_layer": "same built-in terrain palette; no external surface override",
        },
        "before": baseline_metrics,
        "after": enhanced_metrics,
    }
    metrics_path = output_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
