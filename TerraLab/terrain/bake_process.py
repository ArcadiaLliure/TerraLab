import argparse
import contextlib
import json
import math
import os
import sys
import time
from typing import Iterable

import numpy as np

from TerraLab.terrain.engine import (
    HorizonBaker,
    HorizonProfile,
    generate_bands,
)


_EVENT_STREAM = None
_EVENT_STREAM_INIT_FAILED = False


def _resolve_event_stream():
    """Resol el canal de sortida dels esdeveniments JSON del subprocess."""
    global _EVENT_STREAM
    global _EVENT_STREAM_INIT_FAILED

    if _EVENT_STREAM is not None:
        return _EVENT_STREAM
    if _EVENT_STREAM_INIT_FAILED:
        return None

    for stream in (getattr(sys, "__stdout__", None), getattr(sys, "stdout", None)):
        if stream is None:
            continue
        if hasattr(stream, "write") and hasattr(stream, "flush"):
            _EVENT_STREAM = stream
            return _EVENT_STREAM

    try:
        # Important a Windows/pythonw: pot no existir cap stream d'alt nivell.
        _EVENT_STREAM = os.fdopen(
            os.dup(1),
            "w",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )
        return _EVENT_STREAM
    except Exception:
        _EVENT_STREAM_INIT_FAILED = True
        return None


def _emit_event(event_type: str, **payload) -> None:
    """Emet un esdeveniment JSONL cap al pare sense bloquejar el bake."""
    event = {"type": event_type, **payload}
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    event_stream = _resolve_event_stream()
    if event_stream is None:
        print(
            f"[HorizonBakeProcess] Event stream unavailable (event={event_type}).",
            file=sys.stderr,
            flush=True,
        )
        return
    try:
        event_stream.write(line)
        event_stream.flush()
    except Exception as exc:
        print(
            f"[HorizonBakeProcess] Event emit failed ({event_type}): {exc}",
            file=sys.stderr,
            flush=True,
        )


def _phase_progress(job_id: str, phase: str, start_pct: float, end_pct: float):
    span = float(end_pct) - float(start_pct)
    last_emitted = {"percent": None}

    def _callback(percent: float, _msg: str = "") -> None:
        sub_pct = max(0.0, min(100.0, float(percent)))
        mapped = float(start_pct) + (sub_pct / 100.0) * span
        previous = last_emitted["percent"]
        if (
            previous is not None
            and mapped < float(end_pct)
            and mapped - previous < 1.0
        ):
            return
        last_emitted["percent"] = mapped
        _emit_event(
            "progress", job_id=job_id, phase=phase, percent=round(mapped, 1)
        )

    return _callback


def _resolve_light_pollution_path() -> str:
    try:
        from TerraLab.config import ConfigManager

        config = ConfigManager()
        if not bool(config.get("light_pollution_enabled", True)):
            return ""
        lp_path = config.get("dvnl_path", "")
        if lp_path and os.path.exists(lp_path):
            return lp_path
    except Exception:
        pass

    base_dir = os.path.dirname(os.path.dirname(__file__))
    local_default = os.path.join(
        base_dir, "data", "light_pollution", "C_DVNL 2022.tif"
    )
    if os.path.exists(local_default):
        return local_default
    return ""


def _create_provider(tiles_dir: str, progress_callback=None):
    """
    Create the terrain DEM provider used by the bake subprocess.

    Input CRS:
        - The provider API accepts observer coordinates in `EPSG:4326`.
    Internal CRS:
        - Horizon sampling runs in `EPSG:25831`.
    Output CRS:
        - Not applicable (returns a provider object).
    """
    from TerraLab.terrain.providers import create_raster_provider

    return create_raster_provider(
        tiles_dir, progress_callback=progress_callback
    )


def _resolve_raycast_step_m(provider) -> float:
    default_step_m = 50.0
    fine_dem_threshold_m = 6.0

    resolution_m = None
    try:
        getter = getattr(provider, "get_nominal_resolution_m", None)
        if callable(getter):
            resolution_m = getter()
    except Exception:
        resolution_m = None

    try:
        resolution_m = float(resolution_m)
    except (TypeError, ValueError):
        resolution_m = None

    if resolution_m is not None and resolution_m > 0:
        if resolution_m <= fine_dem_threshold_m:
            step_m = max(5.0, float(resolution_m))
            print(
                "[HorizonBakeProcess] DEM resolution "
                f"{resolution_m:.2f}m -> raycast base step {step_m:.1f}m",
                file=sys.stderr,
                flush=True,
            )
            return step_m
        print(
            "[HorizonBakeProcess] DEM resolution "
            f"{resolution_m:.2f}m -> raycast base step {default_step_m:.1f}m",
            file=sys.stderr,
            flush=True,
        )
    return default_step_m


def _circular_distance_deg(a: float, b: float) -> float:
    diff = abs(float(a) - float(b)) % 360.0
    return min(diff, 360.0 - diff)


def _build_priority_azimuth_order(
    azimuths: Iterable[float],
    view_azimuth: float,
    view_fov_deg: float,
) -> list[int]:
    azimuth_values = list(float(a) for a in azimuths)
    half_fov = max(12.0, min(180.0, float(view_fov_deg) * 0.5 + 8.0))
    primary = []
    secondary = []
    for idx, az in enumerate(azimuth_values):
        dist = _circular_distance_deg(az, view_azimuth)
        target = primary if dist <= half_fov else secondary
        target.append((dist, idx))
    primary.sort(key=lambda item: (item[0], item[1]))
    secondary.sort(key=lambda item: (item[0], item[1]))
    return [idx for _, idx in primary] + [idx for _, idx in secondary]


def _atomic_save_profile(profile: HorizonProfile, path: str) -> None:
    tmp_path = f"{path}.tmp.npz"
    max_attempts = 6
    last_exc = None
    for attempt in range(max_attempts):
        try:
            profile.save(tmp_path)
            os.replace(tmp_path, path)
            return
        except PermissionError as exc:
            last_exc = exc
            # On Windows the preview file can be briefly locked by the reader.
            # Retry a few times before giving up.
            time.sleep(0.03 * (attempt + 1))
        except Exception:
            # Keep non-permission errors visible to caller.
            raise
    if last_exc is not None:
        raise last_exc


def _save_preview_snapshot(
    path: str,
    lat: float,
    lon: float,
    azimuths,
    bands,
    light_domes,
    light_peak_distances,
    resolved_mask,
    resolved_radius_m: float,
) -> None:
    profile = HorizonProfile(
        azimuths=azimuths,
        bands=bands,
        observer_lat=float(lat),
        observer_lon=float(lon),
        light_domes=light_domes,
        light_peak_distances=light_peak_distances,
        resolved_mask=resolved_mask,
        resolved_radius_m=float(resolved_radius_m),
    )
    _atomic_save_profile(profile, path)


def main():
    """Punt d'entrada del subprocess de bake d'horitzo."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Bake a horizon profile in a separate process."
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--tiles-dir", required=True)
    parser.add_argument("--observer-offset", type=float, default=0.0)
    parser.add_argument("--bands", type=int, default=20)
    parser.add_argument("--output", required=True)
    parser.add_argument("--preview-path", required=True)
    parser.add_argument("--view-azimuth", type=float, default=180.0)
    parser.add_argument("--view-fov-deg", type=float, default=90.0)
    parser.add_argument("--view-elevation", type=float, default=0.0)
    parser.add_argument("--range-settings-json", required=True)
    args = parser.parse_args()

    job_id = str(args.job_id)
    print(
        (
            "[HorizonBakeProcess] Start "
            f"job={job_id} "
            f"exe={sys.executable} "
            f"event_stream_ready={_resolve_event_stream() is not None}"
        ),
        file=sys.stderr,
        flush=True,
    )
    provider = None
    light_sampler = None
    preview_path = os.path.abspath(args.preview_path)
    output_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    try:
        if not os.path.exists(args.tiles_dir):
            raise FileNotFoundError(
                f"Tiles directory not found: {args.tiles_dir}"
            )

        _emit_event(
            "progress",
            job_id=job_id,
            phase="prepare",
            percent=0.0,
            current=0,
            total=0,
        )
        with contextlib.redirect_stdout(sys.stderr):
            provider = _create_provider(
                args.tiles_dir,
                progress_callback=_phase_progress(
                    job_id, "prepare", 0.0, 15.0
                ),
            )
            baker = HorizonBaker(provider)
            x_utm, y_utm = provider.transform_coordinates(args.lat, args.lon)
            from TerraLab.terrain.visibility_range import EARTH_RADIUS_M, TerrainRangeSettings, resolve_visibility_range
            settings = TerrainRangeSettings.from_mapping(json.loads(args.range_settings_json))
            baker.R = EARTH_RADIUS_M * (
                settings.effective_earth_radius_factor
                if settings.atmospheric_refraction_enabled else 1.0
            )
            try:
                ground_h = provider.get_elevation(x_utm, y_utm)
            except Exception:
                ground_h = None
            if ground_h is None:
                ground_h = 200.0
            range_result = resolve_visibility_range(
                settings, float(ground_h) + float(args.observer_offset) + float(baker.eye_height)
            )
            vis_radius = float(range_result.resolved_radius_m)
            # Providers sample missing tiles lazily during raycasting. Preloading only
            # the configurable near field avoids materialising a 530 km disk at full DEM resolution.
            preload_radius = min(vis_radius, settings.immediate_preload_radius_km * 1000.0)
            print(
                "[HorizonBakeProcess] Visibility range "
                f"mode={settings.mode} observer_elevation_m={range_result.observer_elevation_m:.1f} "
                f"calculated_radius_km={range_result.calculated_radius_m / 1000.0:.1f} "
                f"applied_radius_km={vis_radius / 1000.0:.1f} "
                f"refraction_factor={(settings.effective_earth_radius_factor if settings.atmospheric_refraction_enabled else 1.0):.6f} "
                f"maximum_km={settings.maximum_radius_km:.1f}",
                file=sys.stderr, flush=True,
            )
            try:
                provider.prepare_region(
                    x_utm,
                    y_utm,
                    preload_radius,
                    progress_callback=_phase_progress(
                        job_id, "prepare", 15.0, 35.0
                    ),
                    abort_check=None,
                )
            except TypeError:
                provider.prepare_region(
                    x_utm,
                    y_utm,
                    preload_radius,
                    progress_callback=_phase_progress(
                        job_id, "prepare", 15.0, 35.0
                    ),
                )

            try:
                from TerraLab.terrain.light_pollution_sampler import (
                    LightPollutionSampler,
                )
                from TerraLab.terrain.providers import CRS_TERRAIN_INTERNAL

                lp_path = _resolve_light_pollution_path()
                light_sampler = LightPollutionSampler(
                    lp_path if lp_path else None
                )
                if light_sampler and lp_path:
                    light_sampler.prepare_region_from_terrain_xy(
                        x_terrain=float(x_utm),
                        y_terrain=float(y_utm),
                        radius_m=float(vis_radius),
                        input_crs=CRS_TERRAIN_INTERNAL,
                    )
            except Exception as exc:
                print(
                    f"[HorizonBakeProcess] Light pollution sampler unavailable: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                light_sampler = None

            band_defs = generate_bands(max(1, int(args.bands)), max_dist_m=vis_radius)
            azimuths = [i * 0.5 for i in range(int(round(360.0 / 0.5)))]
            azimuth_order = _build_priority_azimuth_order(
                azimuths=azimuths,
                view_azimuth=float(args.view_azimuth) % 360.0,
                view_fov_deg=max(1.0, float(args.view_fov_deg)),
            )
            preview_every = max(12, min(48, int(max(1, len(azimuths) // 18))))
            last_preview_emit = {"t": 0.0}

            def _preview_callback(
                current,
                total,
                az_arr,
                bands_arr,
                domes,
                peak_distances,
                resolved_mask,
            ):
                now = time.time()
                if current < total and (now - last_preview_emit["t"]) < 0.20:
                    return
                try:
                    _save_preview_snapshot(
                        preview_path,
                        args.lat,
                        args.lon,
                        az_arr,
                        bands_arr,
                        domes,
                        peak_distances,
                        np.asarray(resolved_mask, dtype=bool),
                        vis_radius,
                    )
                except Exception as exc:
                    # Preview persistence is best-effort; do not abort the bake.
                    print(
                        f"[HorizonBakeProcess] Preview snapshot skipped: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return
                last_preview_emit["t"] = now
                _emit_event(
                    "preview",
                    job_id=job_id,
                    observer={"lat": float(args.lat), "lon": float(args.lon)},
                    snapshot_path=preview_path,
                    current=int(current),
                    total=int(total),
                    resolved_radius_m=vis_radius,
                )

            (
                azimuths_arr,
                bands,
                light_domes,
                light_peak_distances,
                resolved_mask,
            ) = baker.bake_progressive(
                obs_x=x_utm,
                obs_y=y_utm,
                obs_h_ground=float(ground_h) + float(args.observer_offset),
                step_m=_resolve_raycast_step_m(provider),
                d_max=vis_radius,
                delta_az_deg=0.5,
                band_defs=band_defs,
                azimuth_order=azimuth_order,
                progress_callback=lambda pct, _msg: _emit_event(
                    "progress",
                    job_id=job_id,
                    phase="bake",
                    percent=round(35.0 + (float(pct) / 100.0) * 63.0, 1),
                    current=int(
                        max(
                            0,
                            min(
                                len(azimuths),
                                round((float(pct) / 100.0) * len(azimuths)),
                            ),
                        )
                    ),
                    total=int(len(azimuths)),
                ),
                preview_callback=_preview_callback,
                preview_every=preview_every,
                light_sampler=light_sampler,
                abort_check=None,
            )

            _emit_event(
                "progress",
                job_id=job_id,
                phase="terrain_mesh",
                percent=98.7,
                current=len(azimuths),
                total=len(azimuths),
            )
            terrain_mesh = baker.build_view_mesh(
                obs_x=x_utm,
                obs_y=y_utm,
                obs_h_ground=float(ground_h) + float(args.observer_offset),
                d_max=vis_radius,
                delta_az_deg=0.5,
            )

            _emit_event(
                "progress",
                job_id=job_id,
                phase="save",
                percent=99.0,
                current=len(azimuths),
                total=len(azimuths),
            )
            final_profile = HorizonProfile(
                azimuths=azimuths_arr,
                bands=bands,
                observer_lat=float(args.lat),
                observer_lon=float(args.lon),
                light_domes=light_domes,
                light_peak_distances=light_peak_distances,
                resolved_mask=np.asarray(resolved_mask, dtype=bool),
                terrain_mesh=terrain_mesh,
                resolved_radius_m=vis_radius,
            )
            _atomic_save_profile(final_profile, output_path)

        _emit_event("done", job_id=job_id, profile_path=output_path, resolved_radius_m=vis_radius)
    except Exception as exc:
        _emit_event("error", job_id=job_id, message=str(exc))
        raise
    finally:
        try:
            if light_sampler and hasattr(light_sampler, "close"):
                light_sampler.close()
        except Exception:
            pass
        try:
            if provider and hasattr(provider, "close"):
                provider.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
