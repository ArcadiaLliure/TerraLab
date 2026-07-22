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
    scalar_projection, vector_projection = _projection(
        width, height, azimuth, fov
    )

    def render_once(
        view_azimuth: float = azimuth, *, interaction_active: bool = False
    ) -> tuple[QImage, float]:
        image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor(151, 190, 219))
        painter = QPainter(image)
        started = time.perf_counter()
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
            sun_alt=35.0,
            sun_az=315.0,
            interaction_active=interaction_active,
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
    # A slightly shifted view invalidates projected geometry/raster buffers but
    # retains process-level JIT code.  It represents steady interactive motion.
    _warm_image, warm_uncached_s = render_once(azimuth + 0.25)
    warm_metrics = {
        "warm_geometry_s": float(overlay._last_surface2d_geometry_s),
        "warm_colors_s": float(overlay._last_terrain_color_s),
        "warm_rasterization_s": float(overlay._last_terrain_raster_s),
        "warm_horizon_antialias_s": float(overlay._last_horizon_antialias_s),
        "warm_terrain_total_s": float(overlay._last_terrain_total_s),
    }
    _cached_image, cached_s = render_once(azimuth + 0.25)
    _interaction_image, interaction_s = render_once(
        azimuth + 0.50, interaction_active=True
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(output_path)):
        raise RuntimeError(f"Could not save {output_path}")
    mesh = profile.terrain_mesh or {}
    vertex_count = int(np.asarray(mesh.get("altitudes", ())).size)
    return {
        "cold_render_s": float(cold_s),
        "warm_uncached_render_s": float(warm_uncached_s),
        "cached_render_s": float(cached_s),
        "interactive_motion_render_s": float(interaction_s),
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
